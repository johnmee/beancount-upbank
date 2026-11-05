"""Reconcile provisional (HELD) Up entries against a fresh pull.

An entry carrying ``up_hold`` metadata was still HELD when it was imported, so
it is provisional. Up may settle it at a different amount -- a restaurant tip, a
foreign conversion -- or release it entirely, in which case it simply vanishes
from the API. ``TransactionStatusEnum`` is ``HELD|SETTLED`` only: there is no
terminal "released" state a poller can observe, and no tombstone. The importer
only ever appends, so neither outcome can be seen from the import side.

Each provisional entry resolves one of five ways::

    settled, same amount   remove the up_hold line
    settled, drifted       correct the amount, remove the up_hold line
    released               comment the entry out
    still held             leave alone, report (loudly, once it is stale)
    out of window          leave alone, report -- cannot be checked

Entries are promoted **in place**: the amount is corrected and the marker removed,
but the entry keeps its position, its narration and any hand-written annotation.
Deleting the provisional entry and letting the settled copy re-import would
re-run any automatic completion from scratch and discard those edits.

Correcting an entry also shifts every ``balance`` directive that was snapshotted
while the hold was live, because Up's ``AccountResource.balance`` is *available*
balance and already had the hold deducted from it.

Edits are surgical, anchored on line numbers. Do NOT be tempted to parse the
ledger and reprint it: that would discard the hand-written ``;`` annotations, the
tab alignment and the pushtag/poptag structure the ledger is full of.

USAGE
-----

::

    upbank-reconcile LEDGER PULL --account ACCOUNT [--root ROOT] [--fix]

``LEDGER``
    The file to edit -- the one the importer appended to, e.g.
    ``up-2026.beancount``. Only this file is modified.
``PULL``
    The Up JSON this run downloaded, e.g. ``/tmp/up.json``. Reuse the same
    file the import used; a second ``upbank recent`` would race settlement.
``--root``
    The top-level ledger. Defaults to ``LEDGER``, which is right when your
    ledger is a single file. If ``LEDGER`` is ``include``d by a main ledger,
    name the main ledger here: an included file carries no opening balance, so
    loading it standalone makes every assertion in it look short by the prior
    period's carry-over. Balances are computed over the root; edits stay
    confined to ``LEDGER``.
``--account``
    The beancount account the Up transactions post to, e.g.
    ``Assets:Bank:Up``.
``--fix``
    Apply the edits. The default (``--dry-run``) only reports them.

Run it **last**, after this run's ``upbank balance --midnight`` assertion is
appended::

    upbank recent 30 > /tmp/up.json
    python import.py extract -e main.beancount /tmp/up.json > /tmp/up.beancount
    fuzzyfill --training main.beancount /tmp/up.beancount >> up-2026.beancount
    amount=$(upbank balance --midnight) &&
      echo "$(TZ=Australia/Sydney date +%F) balance Assets:Bank:Up $amount" >> up-2026.beancount
    upbank-reconcile up-2026.beancount /tmp/up.json \\
        --root main.beancount --account Assets:Bank:Up --fix

The ordering is not cosmetic. That freshly-written assertion is the only one
known to postdate a release, so it is the evidence the run verifies against;
earlier assertions were snapshotted while the hold was live and get adjusted.

If any assertion dated after the earliest entry a ``--fix`` run touched fails
afterwards, it rewrites LEDGER from the lines it read at the start and exits
non-zero rather than leaving a half-corrected ledger. Git is no substitute:
the run follows a fresh, uncommitted import, which a checkout would discard.

Sample output::

    2026-09-01  Example Brewing Co.   -30.00 -> -31.25 AUD   settled 2026-09-03

    1 still unsettled:
    !! 2026-07-01  Ancient Hold       -12.00 AUD  66d  -- older than the pull
                                                          window, cannot be checked

      balance directive line 14 -> 968.75 AUD (that snapshot included the hold)

    Applied. Verified against the untouched 2026-09-06 assertion (968.75 AUD).

Two cases are reported but never edited, because neither can be settled safely
from this account alone: a hold that is a leg of a transfer between two of your
own accounts (the other leg would move with it, and its assertions live in a
file this run does not hold), and one that has aged out of the pull window (its
absence proves nothing, since it was never fetched). For the latter the run
still checks whether the gap against Up's balance matches it exactly, which is
the only evidence available once the API has forgotten the hold.
"""

import datetime
import itertools
import re

from beancount import loader
from beancount.core import data

from .markers import HOLD_KEY

# Up's TransactionStatusEnum.
HELD = "HELD"

# A hold this old is worth looking at: in practice Up settles within a few days,
# so anything lingering past a week is anomalous rather than merely pending.
STALE_DAYS = 7

SETTLED_SAME = "settled"
DRIFTED = "drifted"
RELEASED = "released"
OUTSTANDING = "outstanding"
UNVERIFIABLE = "unverifiable"


class ReconcileError(Exception):
    """The ledger cannot be reconciled safely; nothing has been written."""


class Finding:
    """One provisional ledger entry and what the pull says became of it."""

    def __init__(
        self, kind, entry, posting, was, now=None, settled_on=None, transfer=False
    ):
        self.kind = kind
        self.entry = entry
        self.posting = posting
        self.was = was
        self.now = now
        self.settled_on = settled_on
        # A leg of a transfer between two of our own accounts. Editing this side
        # silently moves the other one, whose balance directives live in another
        # file we are not editing, so these are reported rather than fixed.
        self.transfer = transfer

    def age(self, today):
        return (today - self.entry.date).days

    @property
    def editable(self):
        return self.kind in (SETTLED_SAME, DRIFTED, RELEASED) and not self.transfer

    @property
    def delta(self):
        if self.kind == DRIFTED:
            return self.now - self.was
        if self.kind == RELEASED:
            return -self.was
        return 0


def _is_transfer(entry, account):
    """True if the entry also posts to another asset or liability account.

    Any account under ``Assets`` or ``Liabilities`` counts as "one of your own
    accounts", so an entry that also posts to one is treated as a transfer.
    This errs towards leaving an entry for you to fix by hand, which is the safe
    way to be wrong: a transfer's other leg lives in a file this run is not
    editing.
    """
    return any(
        posting.account != account
        and posting.account.split(":")[0] in ("Assets", "Liabilities")
        for posting in entry.postings
    )


def resolve(entries, pull, account, filename=None):
    """Classify every ``up_hold`` entry in the ledger against the pull.

    ``filter[since]``/``filter[until]`` select on ``createdAt``, which never
    changes, so an entry whose date falls inside the pull's range was certainly
    fetched and its absence is real. Outside that range absence means "not
    fetched" and nothing can be concluded.

    ``entries`` comes from loading the *root* ledger so that balances mean what
    beancount says they mean; ``filename`` restricts the findings to the one
    file this run may edit.
    """
    live = {t["id"]: t for t in pull}
    dates = sorted(
        datetime.date.fromisoformat(t["attributes"]["createdAt"][:10]) for t in pull
    )
    window = (dates[0], dates[-1]) if dates else None

    findings = []
    for entry in entries:
        if not isinstance(entry, data.Transaction):
            continue
        up_id = entry.meta.get(HOLD_KEY)
        if up_id is None:
            continue
        if filename is not None and entry.meta.get("filename") != filename:
            continue

        posting = next((p for p in entry.postings if p.account == account), None)
        if posting is None or posting.units is None:
            continue
        was = posting.units.number
        transfer = _is_transfer(entry, account)

        if window is None or not (window[0] <= entry.date <= window[1]):
            findings.append(
                Finding(UNVERIFIABLE, entry, posting, was, transfer=transfer)
            )
            continue

        trans = live.get(up_id)
        if trans is None:
            findings.append(Finding(RELEASED, entry, posting, was, transfer=transfer))
            continue

        attributes = trans["attributes"]
        settled_at = attributes.get("settledAt")
        if attributes["status"] == HELD or settled_at is None:
            # A SETTLED status with no settledAt is a skew we cannot date, so
            # treat it as still pending rather than guessing.
            findings.append(
                Finding(OUTSTANDING, entry, posting, was, transfer=transfer)
            )
            continue

        now = data.D(attributes["amount"]["value"])
        settled_on = datetime.date.fromisoformat(settled_at[:10])
        kind = SETTLED_SAME if now == was else DRIFTED
        findings.append(Finding(kind, entry, posting, was, now, settled_on, transfer))

    return findings


def newest_assertion(entries, account):
    """The most recent balance directive for the account.

    Written by the current run, so it postdates anything the current pull shows
    as gone. It is never adjusted: leaving it untouched is what makes it an
    independent check rather than a restatement of our own guess.
    """
    assertions = [
        e for e in entries if isinstance(e, data.Balance) and e.account == account
    ]
    if not assertions:
        return None
    # The start-of-day assertion is dated today, so running the target twice in
    # one day leaves two on the same date. The later one in the file is the
    # newer snapshot; tie-break on position so the other is treated as an
    # ordinary intermediate assertion.
    return max(assertions, key=lambda e: (e.date, e.meta.get("lineno", 0)))


def plan(entries, findings, lines, account, anchor, filename):
    """Return ``({lineno: (op, value)}, {lineno: new_balance}, [unreachable])``.

    beancount records a line number per directive and per posting but not per
    metadata key, so the ``up_hold:`` line is found by scanning the entry's span.

    Only directives in ``filename`` can be edited -- every line number here
    indexes into that one file's ``lines``. Assertions that need adjusting but
    live elsewhere are returned so the caller can say so rather than silently
    skipping them.
    """
    edits, balances, unreachable = {}, {}, []

    for finding in findings:
        if not finding.editable:
            continue

        entry = finding.entry
        span = range(
            entry.meta["lineno"],
            max(p.meta["lineno"] for p in entry.postings) + 1,
        )

        if finding.kind == RELEASED:
            for lineno in span:
                edits[lineno] = ("comment", None)
            # The release time is unknowable -- Up keeps no record of it -- so
            # every assertion between the entry and the anchor is assumed to
            # have been taken while the hold was still live.
            until = anchor.date if anchor else datetime.date.max
        else:
            tag = next((ln for ln in span if f"{HOLD_KEY}:" in lines[ln - 1]), None)
            if tag is None:
                raise ReconcileError(
                    f"{entry.meta['filename']}:{entry.meta['lineno']}: "
                    f"cannot locate the {HOLD_KEY} line to remove"
                )
            edits[tag] = ("delete", None)
            if finding.kind == DRIFTED:
                edits[finding.posting.meta["lineno"]] = ("amount", finding.now)
            until = finding.settled_on

        if finding.delta:
            for other in entries:
                if not (
                    isinstance(other, data.Balance)
                    and other.account == account
                    and other is not anchor
                    and entry.date < other.date <= until
                ):
                    continue
                if other.meta.get("filename") != filename:
                    unreachable.append(other)
                    continue
                lineno = other.meta["lineno"]
                current = balances.get(lineno, other.amount.number)
                balances[lineno] = current + finding.delta

    return edits, balances, unreachable


def shortfall_at(entries, account, assertion):
    """How much the assertion expects beyond what the ledger actually accumulates.

    Positive means Up holds more money than the ledger accounts for -- which is
    exactly what a released hold looks like, since Up added the money back and
    the ledger still has the entry spending it.
    """
    accumulated = data.D("0")
    for entry in entries:
        # Balance directives assert as at the *start* of their date.
        if isinstance(entry, data.Transaction) and entry.date < assertion.date:
            for posting in entry.postings:
                if posting.account == account and posting.units is not None:
                    accumulated += posting.units.number
    return assertion.amount.number - accumulated


def explain(shortfall, pending):
    """Which pending holds, if released, would account for the shortfall.

    A hold that aged out of the pull window cannot be checked against the API --
    Up keeps no record of a release. But the money coming back is visible in the
    balance, so an exact match against one or more pending holds identifies the
    culprit when the API no longer can.
    """
    if not shortfall or not pending:
        return None
    # In practice there are only ever a couple of these; the cap keeps a
    # pathological ledger from turning this into a subset-sum blow-up.
    for size in range(1, min(len(pending), 6) + 1):
        for combo in itertools.combinations(pending, size):
            if sum((f.was for f in combo), data.D("0")) == -shortfall:
                return combo
    return None


def _rewrite_number(line, after, new_number):
    """Replace the first number following ``after``, preserving column width."""
    head, marker, tail = line.partition(after)
    if not marker:
        raise ValueError(f"{after!r} not found in {line!r}")
    match = re.search(r"-?[\d,]+(?:\.\d+)?", tail)
    if match is None:
        raise ValueError(f"no number after {after!r} in {line!r}")
    old = match.group(0)
    new = str(new_number)
    # Absorb the width change into the whitespace in front of the number so the
    # ledger's alignment survives.
    pad = len(old) - len(new)
    prefix = tail[: match.start()]
    if pad > 0:
        prefix += " " * pad
    elif pad < 0:
        prefix = prefix[:pad] if len(prefix) + pad >= 1 else prefix
    return head + marker + prefix + new + tail[match.end() :]


def read_ledger(path):
    """The ledger's lines, line endings intact, so a rollback is byte-exact."""
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.readlines()


def write_ledger(path, lines):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.writelines(line for line in lines if line is not None)


def apply_edits(path, lines, edits, balances, account):
    """Write the planned edits over the ledger's original ``lines``."""
    out = list(lines)
    for lineno, (op, value) in edits.items():
        i = lineno - 1
        if op == "comment":
            out[i] = ";" + out[i]
        elif op == "delete":
            out[i] = None
        elif op == "amount":
            out[i] = _rewrite_number(out[i], account, value)
    for lineno, number in balances.items():
        i = lineno - 1
        out[i] = _rewrite_number(out[i], account, number)
    write_ledger(path, out)


def failing_assertions(root, account, since):
    """Balance assertions for the account, dated after ``since``, that fail.

    Absence from a pull is a *hypothesis* that a hold was released; the ledger
    reconciling afterwards is the proof. Every assertion in range must hold, not
    just the anchor -- that is what catches a wrongly-adjusted intermediate
    snapshot, one taken after the hold was already released and which therefore
    must not move.

    Assertions dated on or before the earliest entry we touched cannot be
    affected by these edits (a balance asserts as at the *start* of its date),
    so a pre-existing failure further back does not block the run.
    """
    _, errors, _ = loader.load_file(root)
    return [
        error
        for error in errors
        if isinstance(getattr(error, "entry", None), data.Balance)
        and error.entry.account == account
        and error.entry.date > since
    ]
