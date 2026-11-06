"""What ``upbank reconcile`` does and says.

The reconciling itself lives in reconcile.py, which prints nothing and knows
nothing of click. This module reports its findings and applies them; the
command's arguments are declared with the rest of ``upbank`` in cli.py.
"""

import datetime
import json
import os

import click
from beancount import loader
from beancount.core import amount, data

from . import client
from .markers import HOLD_KEY
from .reconcile import (
    DRIFTED,
    OUTSTANDING,
    SETTLED_SAME,
    STALE_DAYS,
    UNVERIFIABLE,
    Files,
    ReconcileError,
    apply_edits,
    check_opened,
    explain,
    failing_assertions,
    format_balance,
    newest_assertion,
    oldest_hold,
    place_assertion,
    plan,
    resolve,
    restore,
    shortfall_at,
)


def _at(path, lineno=None):
    """A file (and line) as a message shows it: relative to here when inside."""
    rel = os.path.relpath(path)
    shown = path if rel.startswith("..") else rel
    return shown if lineno is None else f"{shown}:{lineno}"


def report(findings, today):
    """Print what became of every provisional entry, loudest last."""
    transfers = [f for f in findings if f.transfer and f.kind != OUTSTANDING]
    for finding in transfers:
        click.echo(
            f"!! {finding.entry.date}  "
            f"{(finding.entry.payee or '')[:34]:34} {finding.was} AUD  "
            f"transfer between own accounts -- not touched, fix by hand\n"
            f"   {_at(finding.entry.meta['filename'], finding.entry.meta['lineno'])}"
        )
    resolved = [f for f in findings if f.editable]
    for finding in resolved:
        entry = finding.entry
        label = f"  {entry.date}  {(entry.payee or '')[:34]:34}"
        if finding.kind == DRIFTED:
            click.echo(
                f"{label} {finding.was} -> {finding.now} AUD"
                f"   settled {finding.settled_on}"
            )
        elif finding.kind == SETTLED_SAME:
            click.echo(f"{label} {finding.was} AUD   settled as held")
        else:
            click.echo(f"{label} {finding.was} AUD   released, never settled")

    pending = [f for f in findings if f.kind in (OUTSTANDING, UNVERIFIABLE)]
    if not pending:
        if resolved:
            click.echo("\nNothing left unsettled.")
        return pending

    click.echo(f"\n{len(pending)} still unsettled:")
    for finding in sorted(pending, key=lambda f: f.entry.date):
        entry = finding.entry
        age = finding.age(today)
        mark = "  " if age <= STALE_DAYS and finding.kind == OUTSTANDING else "!!"
        note = ""
        if finding.kind == UNVERIFIABLE:
            note = "  -- older than the pull window, cannot be checked"
        elif age > STALE_DAYS:
            note = "  -- unusually old, Up may have dropped it"
        click.echo(
            f"{mark} {entry.date}  {(entry.payee or '')[:34]:34} "
            f"{finding.was} AUD  {age}d{note}"
        )

    stale = [f for f in pending if f.kind == UNVERIFIABLE or f.age(today) > STALE_DAYS]
    if stale:
        click.echo(
            f"\n!! {len(stale)} hold(s) marked !! above have been pending for "
            f"more than {STALE_DAYS} days.\n"
            f"   Holds normally settle within a few days."
        )
    return pending


def report_shortfall(entries, account, anchor, pending, planned=0):
    """Explain any gap between Up's reported balance and the ledger's.

    When Up releases a hold it puts the money back, so the account balance it
    reports rises by the held amount while the ledger still has the entry
    spending it. For a hold inside the pull window that release is visible
    directly, as the transaction's absence. For one that has aged out, this
    arithmetic is the only evidence left that it was released.
    """
    if anchor is None:
        return
    # What the run's own edits would have closed is not part of the mystery.
    gap = shortfall_at(entries, account, anchor) - planned
    if not gap:
        return

    # Only holds the pull could not see are candidates. One the pull confirmed
    # is still HELD is deducted from Up's balance and from the ledger alike, so
    # it contributes nothing to the gap -- blaming it would tell the user to
    # delete a live transaction that is going to settle.
    pending = [f for f in pending if f.kind == UNVERIFIABLE]

    click.echo(
        f"\nUp reports {anchor.amount.number} AUD at {anchor.date}, "
        f"but the ledger{' with these edits' if planned else ''} accumulates "
        f"{anchor.amount.number - gap} AUD "
        f"-- a gap of {gap} AUD."
    )
    culprits = explain(gap, pending)
    if culprits:
        click.echo(
            "   That is exactly the amount of the pending hold(s) below, so Up "
            "has\n   released them and put the money back:"
        )
        for finding in culprits:
            click.echo(
                f"     {finding.entry.date}  "
                f"{(finding.entry.payee or '')[:34]:34} {finding.was} AUD  "
                f"{_at(finding.entry.meta['filename'], finding.entry.meta['lineno'])}"
            )
        click.echo(
            "   Comment the entr(ies) out and add the same amount to every "
            "balance\n   directive between them and this one."
        )
    else:
        click.echo(
            "   No combination of pending holds accounts for it, so this gap is "
            "something else."
        )


def run(account, root, pull, dry_run, snapshot):
    """Reconcile ``account``'s provisional entries across the ledger at ``root``.

    ``pull`` is a path to Up's JSON, or None to ask Up now: ``snapshot(since)``
    must then return ``(today, opening balance, transactions since `since`)``
    as one consistent read, and today's ``balance`` directive is written from
    it, so the verification has evidence that postdates every release.
    """
    entries, _, _ = loader.load_file(root)
    check_opened(entries, account, root)
    today = client.sydney_today()
    fresh = None
    if pull is not None:
        with open(pull) as fh:
            transactions = json.load(fh)
        window = None
    else:
        since = oldest_hold(entries, account) or today
        today, fresh, transactions = snapshot(since)
        window = (since, today)
    findings = resolve(entries, transactions, account, window)

    if not findings and fresh is None:
        click.echo(f"No provisional ({HOLD_KEY}) entries in the ledger.")
        return

    pending = report(findings, today)

    files = Files()
    append = None
    if fresh is None:
        anchor = newest_assertion(entries, account)
    else:
        existing, target = place_assertion(entries, account, today)
        meta = dict(existing.meta) if existing else {}
        anchor = data.Balance(
            meta, today, account, amount.Amount(fresh, client.CURRENCY), None, None
        )
    edits, balances, unreachable = plan(entries, findings, files, account, anchor)

    for other in unreachable:
        click.echo(
            f"\n!! {_at(other.meta['filename'], other.meta['lineno'])}: this "
            f"assertion needs adjusting too but was not read from a file; "
            f"fix it by hand."
        )

    for (path, lineno), number in sorted(balances.items()):
        click.echo(
            f"\n  {_at(path, lineno)}: balance directive -> {number} AUD "
            f"(that snapshot included the hold)"
        )

    if fresh is not None:
        line = format_balance(today, account, fresh)
        if existing is None:
            files[target]
            append = (target, line)
            click.echo(f"\n  {_at(target)}: append  {line}")
        elif existing.amount.number != fresh:
            where = (existing.meta["filename"], existing.meta["lineno"])
            files[where[0]]
            balances[where] = fresh
            click.echo(f"\n  {_at(*where)}: replace with  {line}")

    if not edits and not balances and append is None:
        # Nothing to promote, but a gap against Up's balance still needs
        # explaining -- a hold released after ageing out of the pull window
        # leaves no other trace.
        report_shortfall(entries, account, anchor, pending)
        if anchor is not None and not shortfall_at(entries, account, anchor):
            click.echo("\nNothing to change.")
        return

    if dry_run:
        changes = len(edits) + len(balances) + (append is not None)
        click.echo(f"\n{changes} line edit(s) pending; re-run without --dry-run.")
        return

    # Our edits can only move assertions dated after the earliest entry we
    # touch, and today's directive is dated today, so that is the horizon we
    # hold ourselves to.
    touched = [f.entry.date for f in findings if f.editable]
    if fresh is not None:
        touched.append(today - datetime.timedelta(days=1))
    since = min(touched)
    try:
        apply_edits(files, edits, balances, account, append)
        failures = failing_assertions(root, account, since)
    except BaseException:
        # Including Ctrl-C: the originals exist only in memory, so put them
        # back before they are lost.
        restore(files)
        raise
    if not failures:
        if fresh is not None:
            click.echo(f"\nApplied. Verified against Up's balance ({fresh} AUD).")
        elif anchor is not None:
            click.echo(
                f"\nApplied. Verified against the untouched {anchor.date} "
                f"assertion ({anchor.amount.number} AUD)."
            )
        else:
            click.echo("\nApplied (no balance directive to verify against).")
        return

    restore(files)
    # The edits were sound as far as the API could tell, so a residual gap is
    # most likely a hold that aged out of the window and was released unseen.
    planned = (
        sum((f.delta for f in findings if f.editable and f.entry.date < anchor.date), 0)
        if anchor is not None
        else 0
    )
    report_shortfall(entries, account, anchor, pending, planned)
    raise ReconcileError(
        "Rolled back -- these balance assertions do not hold after the edits:\n"
        + "\n".join(f"  {f.message}" for f in failures)
    )
