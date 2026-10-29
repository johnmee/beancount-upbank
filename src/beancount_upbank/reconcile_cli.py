"""The ``upbank-reconcile`` command line.

The reconciling itself lives in reconcile.py, which prints nothing and knows
nothing of click. This module reports its findings and applies them.
"""

import datetime
import json
import os

import click
from beancount import loader

from .markers import HOLD_KEY
from .reconcile import (
    DRIFTED,
    OUTSTANDING,
    SETTLED_SAME,
    STALE_DAYS,
    UNVERIFIABLE,
    ReconcileError,
    apply_edits,
    explain,
    failing_assertions,
    newest_assertion,
    plan,
    read_ledger,
    resolve,
    shortfall_at,
    write_ledger,
)


def report(findings, today):
    """Print what became of every provisional entry, loudest last."""
    transfers = [f for f in findings if f.transfer and f.kind != OUTSTANDING]
    for finding in transfers:
        click.echo(
            f"!! {finding.entry.date}  "
            f"{(finding.entry.payee or '')[:34]:34} {finding.was} AUD  "
            f"transfer between own accounts -- not touched, fix by hand\n"
            f"   {finding.entry.meta['filename']}:{finding.entry.meta['lineno']}"
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


def report_shortfall(entries, account, anchor, pending):
    """Explain any gap between Up's reported balance and the ledger's.

    When Up releases a hold it puts the money back, so the account balance it
    reports rises by the held amount while the ledger still has the entry
    spending it. For a hold inside the pull window that release is visible
    directly, as the transaction's absence. For one that has aged out, this
    arithmetic is the only evidence left that it was released.
    """
    if anchor is None:
        return
    gap = shortfall_at(entries, account, anchor)
    if not gap:
        return

    # Only holds the pull could not see are candidates. One the pull confirmed
    # is still HELD is deducted from Up's balance and from the ledger alike, so
    # it contributes nothing to the gap -- blaming it would tell the user to
    # delete a live transaction that is going to settle.
    pending = [f for f in pending if f.kind == UNVERIFIABLE]

    click.echo(
        f"\nUp reports {anchor.amount.number} AUD at {anchor.date}, "
        f"but the ledger accumulates {anchor.amount.number - gap} AUD "
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
                f"{finding.entry.meta['filename']}:{finding.entry.meta['lineno']}"
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


class _Command(click.Command):
    """Report a ReconcileError as a clean command-line error, not a traceback."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except ReconcileError as e:
            raise click.ClickException(str(e)) from e


@click.command(cls=_Command)
@click.argument("ledger", type=click.Path(exists=True, dir_okay=False))
@click.argument("pull", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--account",
    required=True,
    help="Beancount account the Up transactions post to.",
)
@click.option(
    "--root",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Top-level ledger to load for balance semantics. Defaults to LEDGER, "
    "which is right when your ledger is one file. If LEDGER is `include`d by a "
    "main ledger, name that here: a sub-ledger loaded on its own has no "
    "opening balance, so every assertion in it would appear to fail.",
)
@click.option(
    "--fix/--dry-run",
    default=False,
    help="Apply the edits. The default only reports them.",
)
def cli(ledger, pull, account, root, fix):
    """Promote settled holds in LEDGER using a fresh Up PULL (json)."""
    if root is None:
        root = ledger
    with open(pull) as fh:
        transactions = json.load(fh)
    entries, _, _ = loader.load_file(root)
    # Balances are computed over the whole ledger; edits are confined to the one
    # file whose lines we hold.
    target = os.path.abspath(ledger)
    findings = resolve(entries, transactions, account, target)

    if not findings:
        click.echo(f"No provisional ({HOLD_KEY}) entries in the ledger.")
        return

    today = datetime.date.today()
    pending = report(findings, today)

    lines = read_ledger(ledger)
    anchor = newest_assertion(entries, account)
    edits, balances, unreachable = plan(
        entries, findings, lines, account, anchor, target
    )

    for other in unreachable:
        click.echo(
            f"\n!! {other.meta['filename']}:{other.meta['lineno']}: this "
            f"assertion needs adjusting too but is outside {ledger}; "
            f"fix it by hand."
        )

    if not edits:
        # Nothing to promote, but a gap against Up's balance still needs
        # explaining -- a hold released after ageing out of the pull window
        # leaves no other trace.
        report_shortfall(entries, account, anchor, pending)
        return

    for lineno, number in sorted(balances.items()):
        click.echo(
            f"\n  balance directive line {lineno} -> {number} AUD "
            f"(that snapshot included the hold)"
        )

    if not fix:
        click.echo(f"\n{len(edits)} line edit(s) pending; re-run with --fix.")
        return

    # Our edits can only move assertions dated after the earliest entry we
    # touch, so that is the horizon we hold ourselves to.
    since = min(f.entry.date for f in findings if f.editable)
    try:
        apply_edits(ledger, lines, edits, balances, account)
        failures = failing_assertions(root, account, since)
    except BaseException:
        # Including Ctrl-C: the original exists only in memory, so put it back
        # before it is lost.
        write_ledger(ledger, lines)
        raise
    if not failures:
        click.echo(
            f"\nApplied. Verified against the untouched {anchor.date} "
            f"assertion ({anchor.amount.number} AUD)."
            if anchor
            else "\nApplied (no balance directive to verify against)."
        )
        return

    write_ledger(ledger, lines)
    # The edits were sound as far as the API could tell, so a residual gap is
    # most likely a hold that aged out of the window and was released unseen.
    report_shortfall(entries, account, anchor, pending)
    raise ReconcileError(
        "Rolled back -- these balance assertions do not hold after the edits:\n"
        + "\n".join(f"  {f.message}" for f in failures)
    )


if __name__ == "__main__":
    cli()
