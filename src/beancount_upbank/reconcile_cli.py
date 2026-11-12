"""What ``upbank reconcile`` does and says.

The reconciling itself lives in reconcile.py, which prints nothing and knows
nothing of click. This module reports what became of each hold and applies the
edits; the command's arguments are declared with the rest of ``upbank`` in
cli.py.
"""

import datetime
import json
import os
from collections.abc import Callable, Sequence
from decimal import Decimal

import click
from beancount import loader
from beancount.core import amount, data

from . import client
from .client import Resource
from .markers import HOLD_KEY
from .reconcile import (
    STALE_DAYS,
    Files,
    Hold,
    Kind,
    Location,
    ReconcileError,
    apply_edits,
    check_opened,
    classify_holds,
    explain,
    failing_balances,
    format_balance,
    modified_times,
    newest_balance,
    number_of,
    oldest_hold,
    place_balance,
    plan_edits,
    redated,
    repeated_balance,
    restore,
    shortfall_at,
)

# Reads Up once: ``snapshot(since)`` -> ``(today, opening balance, transactions
# created since `since`)``.
Snapshot = Callable[[datetime.date], tuple[datetime.date, Decimal, list[Resource]]]


def _at(path: str, lineno: int | None = None) -> str:
    """A file (and line) as a message shows it: relative to here when inside."""
    rel = os.path.relpath(path)
    shown = path if rel.startswith("..") else rel
    return shown if lineno is None else f"{shown}:{lineno}"


def report(holds: list[Hold], today: datetime.date) -> list[Hold]:
    """Print what became of every provisional entry, loudest last."""
    transfers = [h for h in holds if h.transfer and h.kind != Kind.OUTSTANDING]
    for hold in transfers:
        click.echo(
            f"!! {hold.entry.date}  "
            f"{(hold.entry.payee or '')[:34]:34} {hold.was} AUD  "
            f"transfer between own accounts -- not touched, fix by hand\n"
            f"   {_at(hold.entry.meta['filename'], hold.entry.meta['lineno'])}"
        )
    resolved = [h for h in holds if h.editable]
    for hold in resolved:
        entry = hold.entry
        label = f"  {entry.date}  {(entry.payee or '')[:34]:34}"
        if hold.kind == Kind.DRIFTED:
            click.echo(
                f"{label} {hold.was} -> {hold.now} AUD   settled {hold.settled_on}"
            )
        elif hold.kind == Kind.SETTLED_SAME:
            click.echo(f"{label} {hold.was} AUD   settled as held")
        else:
            click.echo(f"{label} {hold.was} AUD   released, never settled")

    pending = [h for h in holds if h.kind in (Kind.OUTSTANDING, Kind.UNVERIFIABLE)]
    if not pending:
        if resolved:
            click.echo("\nNothing left unsettled.")
        return pending

    click.echo(f"\n{len(pending)} still unsettled:")
    for hold in sorted(pending, key=lambda h: h.entry.date):
        entry = hold.entry
        age = hold.age(today)
        mark = "  " if age <= STALE_DAYS and hold.kind == Kind.OUTSTANDING else "!!"
        note = ""
        if hold.kind == Kind.UNVERIFIABLE:
            note = "  -- older than the pull window, cannot be checked"
        elif age > STALE_DAYS:
            note = "  -- unusually old, Up may have dropped it"
        click.echo(
            f"{mark} {entry.date}  {(entry.payee or '')[:34]:34} "
            f"{hold.was} AUD  {age}d{note}"
        )

    stale = [
        h for h in pending if h.kind == Kind.UNVERIFIABLE or h.age(today) > STALE_DAYS
    ]
    if stale:
        click.echo(
            f"\n!! {len(stale)} hold(s) marked !! above have been pending for "
            f"more than {STALE_DAYS} days.\n"
            f"   Holds normally settle within a few days."
        )
    return pending


def report_shortfall(
    entries: data.Directives,
    account: str,
    anchor: data.Balance | None,
    pending: Sequence[Hold],
    planned: Decimal = Decimal(0),
) -> None:
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
    pending = [h for h in pending if h.kind == Kind.UNVERIFIABLE]

    click.echo(
        f"\nUp reports {anchor.amount.number} AUD at {anchor.date}, "
        f"but the ledger{' with these edits' if planned else ''} accumulates "
        f"{number_of(anchor.amount) - gap} AUD "
        f"-- a gap of {gap} AUD."
    )
    culprits = explain(gap, pending)
    if culprits:
        click.echo(
            "   That is exactly the amount of the pending hold(s) below, so Up "
            "has\n   released them and put the money back:"
        )
        for hold in culprits:
            click.echo(
                f"     {hold.entry.date}  "
                f"{(hold.entry.payee or '')[:34]:34} {hold.was} AUD  "
                f"{_at(hold.entry.meta['filename'], hold.entry.meta['lineno'])}"
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


def run(
    account: str, root: str, pull: str | None, dry_run: bool, snapshot: Snapshot
) -> None:
    """Reconcile ``account``'s provisional entries across the ledger at ``root``.

    ``pull`` is a path to Up's JSON, or None to ask Up now: ``snapshot(since)``
    must then return ``(today, opening balance, transactions since `since`)``
    as one consistent read, and today's ``balance`` directive is written from
    it, so the verification has evidence that postdates every release.
    """
    # Load the ledger, fetch Up's view, and classify every hold.
    entries, _, options = loader.load_file(root)
    # Noted now, so a file saved while Up is being read is caught before edit.
    loaded = modified_times(options["include"])
    check_opened(entries, account, root)
    today = client.sydney_today()
    fresh: Decimal | None = None
    transactions: list[Resource]
    date_range: tuple[datetime.date, datetime.date] | None
    if pull is not None:
        with open(pull) as fh:
            transactions = json.load(fh)
        date_range = None
    else:
        since = oldest_hold(entries, account) or today
        today, fresh, transactions = snapshot(since)
        date_range = (since, today)
    holds = classify_holds(entries, transactions, account, date_range)

    # Nothing held and no fresh balance to write: done.
    if not holds and fresh is None:
        click.echo(f"No provisional ({HOLD_KEY}) entries in the ledger.")
        return

    # Print what became of each hold.
    pending = report(holds, today)

    # Pick the anchor balance directive and plan the line edits.
    files = Files(loaded)
    new: tuple[str, str, int | None] | None = None
    existing: data.Balance | None = None
    target: str | None = None
    before: int | None = None
    anchor: data.Balance | None
    if fresh is None:
        anchor = newest_balance(entries, account)
    else:
        existing, target, before = place_balance(entries, account, today)
        meta = dict(existing.meta) if existing else {}
        anchor = data.Balance(
            meta, today, account, amount.Amount(fresh, client.CURRENCY), None, None
        )

    # A release is only inferred from absence, so it takes a balance directive
    # dated after the entry to prove it. Leave alone any that nothing checks.
    unproven = [
        h
        for h in holds
        if h.editable
        and h.kind == Kind.RELEASED
        and (anchor is None or anchor.date <= h.entry.date)
    ]
    for hold in unproven:
        click.echo(
            f"\n!! {_at(hold.entry.meta['filename'], hold.entry.meta['lineno'])}: "
            f"no balance directive after {hold.entry.date} can confirm the release, "
            f"so it is left alone.\n   Append one (`upbank balance {account} "
            f">> FILE`) and run again."
        )
    holds = [h for h in holds if h not in unproven]
    edits, balances, unreachable = plan_edits(entries, holds, files, account, anchor)

    # Warn about plugin-made balance directives we cannot edit.
    for other in unreachable:
        click.echo(
            f"\n!! {_at(other.meta['filename'], other.meta['lineno'])}: this "
            f"balance directive needs adjusting too but was not read from a file; "
            f"fix it by hand."
        )

    # List the balance directives being adjusted.
    for (path, lineno), number in sorted(balances.items()):
        click.echo(
            f"\n  {_at(path, lineno)}: balance directive -> {number} AUD "
            f"(that snapshot included the hold)"
        )

    # Plan today's balance directive: redate the last one if nothing has
    # happened since, else add it, or replace a stale one already dated today.
    if fresh is not None:
        line = format_balance(today, account, fresh)
        last = (
            repeated_balance(entries, account, today, fresh, balances)
            if existing is None
            else None
        )
        at: Location | None = None
        if last is not None:
            at = (last.meta["filename"], last.meta["lineno"])
            # Read the line itself: one that does not open with its date is
            # left as it is, and a new directive written.
            if redated(files[at[0]][at[1] - 1], today) is None:
                at = None
        if last is not None and at is not None:
            edits[at] = ("redate", today)
            click.echo(
                f"\n  {_at(*at)}: redate to {today}  "
                f"({fresh} AUD, nothing new since {last.date})"
            )
        elif existing is None:
            # place_balance names a file whenever there is no directive.
            assert target is not None
            files[target]
            new = (target, line, before)
            if before is None:
                click.echo(f"\n  {_at(target)}: append  {line}")
            else:
                click.echo(f"\n  {_at(target, before)}: insert  {line}")
        elif existing.amount.number != fresh:
            where = (existing.meta["filename"], existing.meta["lineno"])
            files[where[0]]
            balances[where] = fresh
            click.echo(f"\n  {_at(*where)}: replace with  {line}")

    # No edits: explain any balance gap, then stop.
    if not edits and not balances and new is None:
        # Nothing to promote, but a gap against Up's balance still needs
        # explaining -- a hold released after ageing out of the pull window
        # leaves no other trace.
        report_shortfall(entries, account, anchor, pending)
        if anchor is not None and not shortfall_at(entries, account, anchor):
            click.echo("\nNothing to change.")
        return

    # Dry run: count the edits, write nothing.
    if dry_run:
        # A directive both corrected and redated is one line, counted once.
        changes = len(edits.keys() | balances.keys()) + (new is not None)
        click.echo(f"\n{changes} line edit(s) pending; re-run without --dry-run.")
        return

    # Apply the edits and re-check balances; restore on any exception.
    # Our edits can only move balances dated after the earliest entry we
    # touch, and today's directive is dated today, so that is the horizon we
    # hold ourselves to.
    touched = [h.entry.date for h in holds if h.editable]
    if fresh is not None:
        touched.append(today - datetime.timedelta(days=1))
    since = min(touched)
    try:
        apply_edits(files, edits, balances, account, new)
        failures = failing_balances(root, account, since)
    except BaseException:
        # Including Ctrl-C: the originals exist only in memory, so put them
        # back before they are lost.
        restore(files)
        raise
    # All balances hold: report success.
    if not failures:
        if fresh is not None:
            click.echo(f"\nApplied. Verified against Up's balance ({fresh} AUD).")
        elif anchor is not None:
            click.echo(
                f"\nApplied. Verified against the untouched {anchor.date} "
                f"balance directive ({anchor.amount.number} AUD)."
            )
        else:
            click.echo("\nApplied (no balance directive to verify against).")
        return

    # Balances failed: roll back, explain the gap, and error out.
    restore(files)
    # The edits were sound as far as the API could tell, so a residual gap is
    # most likely a hold that aged out of the window and was released unseen.
    planned = (
        sum(
            (h.delta for h in holds if h.editable and h.entry.date < anchor.date),
            Decimal(0),
        )
        if anchor is not None
        else Decimal(0)
    )
    report_shortfall(entries, account, anchor, pending, planned)
    raise ReconcileError(
        "Rolled back -- these balance directives do not hold after the edits:\n"
        + "\n".join(f"  {f.message}" for f in failures)
    )
