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

    upbank reconcile ACCOUNT LEDGER [--pull FILE] [--dry-run]

``ACCOUNT``
    The beancount account the Up transactions post to, e.g.
    ``Assets:Bank:Up``.
``LEDGER``
    The main ledger. Balances are computed over it, and edits land in
    whichever of its files holds each entry or directive. An included file
    loaded on its own carries no opening balance, which would make every
    assertion in it look short by the prior period's carry-over.
``--pull FILE``
    Optional: Up JSON saved by ``upbank pull``. Without it, Up is asked for
    the balance and the transactions back to the oldest hold in one
    consistent read, and today's balance directive is written from it.
``--dry-run``
    Report the edits without applying them.

The verification needs a balance directive known to postdate every release
the run acts on; it is never adjusted, while earlier directives snapshotted
while a hold was live are. Without --pull that is today's directive, written
from the same read as the transactions. With --pull it is the newest one already
in the ledger, so append it first::

    upbank balance Assets:Bank:Up >> up-2026.beancount
    upbank reconcile Assets:Bank:Up main.beancount --pull /tmp/up.json

If any assertion dated after the earliest entry a run touched fails
afterwards, it rewrites every file it edited from the lines it read at the
start and exits non-zero rather than leaving a half-corrected ledger. Git is no
substitute: the run follows a fresh, uncommitted import, which a checkout would
discard.
"""

import datetime
import itertools
import os
import re

from beancount import loader
from beancount.core import data

from .client import CURRENCY
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
    way to be wrong: a transfer's other leg belongs to an account whose balance
    directives this run does not adjust.
    """
    return any(
        posting.account != account
        and posting.account.split(":")[0] in ("Assets", "Liabilities")
        for posting in entry.postings
    )


def _held(entries, account):
    """Yield the entries still marked as held, each with its ``account`` posting."""
    for entry in entries:
        if not isinstance(entry, data.Transaction) or HOLD_KEY not in entry.meta:
            continue
        posting = next((p for p in entry.postings if p.account == account), None)
        if posting is not None and posting.units is not None:
            yield entry, posting


def check_opened(entries, account, root):
    """Refuse a ledger that never opens the account.

    The usual cause is naming an included file rather than the main ledger:
    loaded on its own it has no ``open`` and no opening balance, so every
    assertion would fail and the run would only roll back.
    """
    if not any(isinstance(e, data.Open) and e.account == account for e in entries):
        raise ReconcileError(
            f"{root} never opens {account}. Name your main ledger, the one "
            "that includes the others."
        )


def oldest_hold(entries, account):
    """The date of the oldest ``up_hold`` entry for the account; None if none."""
    return min((entry.date for entry, _ in _held(entries, account)), default=None)


def resolve(entries, pull, account, window=None):
    """Classify every ``up_hold`` entry in the ledger against the pull.

    ``filter[since]``/``filter[until]`` select on ``createdAt``, which never
    changes, so an entry whose date falls inside the pull's range was certainly
    fetched and its absence is real. Outside that range absence means "not
    fetched" and nothing can be concluded.

    ``window`` is the ``(first, last)`` date the pull asked Up for. A pull read
    from a file does not say, so by default it is the range of dates the pull
    actually holds.

    ``entries`` comes from loading the *root* ledger, so balances mean what
    beancount says they mean, and every file it includes is in scope.
    """
    live = {t["id"]: t for t in pull}
    if window is None:
        dates = sorted(
            datetime.date.fromisoformat(t["attributes"]["createdAt"][:10]) for t in pull
        )
        window = (dates[0], dates[-1]) if dates else None

    findings = []
    for entry, posting in _held(entries, account):
        up_id = entry.meta[HOLD_KEY]
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


def _where(entry):
    """Where a directive lives: ``(filename, lineno)``."""
    return entry.meta.get("filename"), entry.meta.get("lineno")


def _editable(entry):
    """True if the directive came from a file we can rewrite, not a plugin."""
    return os.path.isfile(entry.meta.get("filename") or "")


class Files(dict):
    """Each ledger file's original lines, read on first use and kept for rollback.

    Every file an edit lands in is read here before anything is written, so a
    rollback can put each one back byte for byte.
    """

    def __missing__(self, path):
        lines = self[path] = read_ledger(path)
        return lines


def _already_released(snapshot, findings):
    """The released holds a snapshot already shows the money back for.

    Up keeps no record of when a hold was released. A snapshot taken after the
    release shows the money back while the ledger still spends it, so it fails
    before any edit, by exactly the holds it saw released. beancount's
    ``diff_amount`` is ledger minus snapshot: ``was`` for each such release,
    and ``was - now`` for each settlement that drifted before the snapshot.
    Take the settlements out and find the releases that make up the rest.

    A snapshot that passes saw none of them. One whose gap no set of releases
    explains fails for some other reason; it is treated as having seen all of
    them, so it is not moved, and the verification reports it.
    """
    released = [
        f
        for f in findings
        if f.editable and f.kind == RELEASED and f.entry.date < snapshot.date
    ]
    if snapshot.diff_amount is None:
        return ()
    gap = snapshot.diff_amount.number
    for f in findings:
        if (
            f.editable
            and f.kind == DRIFTED
            and f.entry.date < snapshot.date
            and f.settled_on < snapshot.date
        ):
            gap -= f.was - f.now
    # In practice there are only ever a couple; the cap keeps a pathological
    # ledger from turning this into a subset-sum blow-up.
    for size in range(min(len(released), 6) + 1):
        for combo in itertools.combinations(released, size):
            if sum((f.was for f in combo), data.D("0")) == gap:
                return combo
    return released


def plan(entries, findings, files, account, anchor):
    """Return ``({(file, lineno): (op, value)}, {(file, lineno): balance}, [unreachable])``.

    beancount records a line number per directive and per posting but not per
    metadata key, so the ``up_hold:`` line is found by scanning the entry's span.

    ``files`` is a ``Files``; every file planned for editing is read into it.
    ``anchor`` is the balance directive the run verifies against, so it is
    never adjusted. Assertions that need adjusting but were not read from a
    file (a plugin made them) are returned so the caller can say so.
    """
    edits, balances, unreachable = {}, {}, []
    anchor_at = _where(anchor) if anchor is not None else None
    seen = {}

    for finding in findings:
        if not finding.editable:
            continue

        entry = finding.entry
        path = entry.meta["filename"]
        lines = files[path]
        span = range(
            entry.meta["lineno"],
            max(p.meta["lineno"] for p in entry.postings) + 1,
        )

        if finding.kind == RELEASED:
            for lineno in span:
                edits[path, lineno] = ("comment", None)
            # The release time is unknowable -- Up keeps no record of it -- so
            # every assertion between the entry and the anchor is a candidate.
            # Those taken after the release are told apart below.
            until = anchor.date if anchor else datetime.date.max
        else:
            tag = next((ln for ln in span if f"{HOLD_KEY}:" in lines[ln - 1]), None)
            if tag is None:
                raise ReconcileError(
                    f"{entry.meta['filename']}:{entry.meta['lineno']}: "
                    f"cannot locate the {HOLD_KEY} line to remove"
                )
            edits[path, tag] = ("delete", None)
            if finding.kind == DRIFTED:
                edits[path, finding.posting.meta["lineno"]] = ("amount", finding.now)
            until = finding.settled_on

        if finding.delta:
            for other in entries:
                if not (
                    isinstance(other, data.Balance)
                    and other.account == account
                    and _where(other) != anchor_at
                    and entry.date < other.date <= until
                ):
                    continue
                if finding.kind == RELEASED:
                    if id(other) not in seen:
                        seen[id(other)] = _already_released(other, findings)
                    if any(f is finding for f in seen[id(other)]):
                        # Taken after the release: right as it stands.
                        continue
                if not _editable(other):
                    unreachable.append(other)
                    continue
                files[other.meta["filename"]]
                where = _where(other)
                current = balances.get(where, other.amount.number)
                balances[where] = current + finding.delta

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


def format_balance(day, account, number):
    """A ``balance`` directive line, amounts aligned for a short account name."""
    return f"{f'{day} balance {account}':<37} {number:>10} {CURRENCY}"


def place_assertion(entries, account, day):
    """Where a fresh ``balance`` directive dated ``day`` goes.

    Returns ``(existing, path)``. ``existing`` is the account's directive
    already dated ``day``, which is replaced rather than duplicated; then
    ``path`` is None. Otherwise ``existing`` is None and ``path`` is the file to
    append to: the one holding the account's newest-dated directive or
    transaction, so a new year's file takes over as soon as the import writes
    to it. Transfers from your other accounts are left out: they live in the
    other account's file. On a tie a transaction wins, being where the
    importer writes.

    Raises:
        ReconcileError: two directives are already dated ``day``, or the
            ledger holds nothing for the account to write beside.
    """
    assertions = [
        e
        for e in entries
        if isinstance(e, data.Balance) and e.account == account and _editable(e)
    ]
    same_day = [e for e in assertions if e.date == day]
    if len(same_day) > 1:
        raise ReconcileError(
            f"{account} has {len(same_day)} balance directives dated {day}; "
            "keep one and run again:\n"
            + "\n".join(f"  {e.meta['filename']}:{e.meta['lineno']}" for e in same_day)
        )
    if same_day:
        return same_day[0], None
    transactions = [
        e
        for e in entries
        if isinstance(e, data.Transaction)
        and _editable(e)
        and any(p.account == account for p in e.postings)
        and not _is_transfer(e, account)
    ]
    candidates = assertions + transactions
    if not candidates:
        raise ReconcileError(
            f"The ledger has no balance directive or transaction for {account}, "
            "so there is no file to write today's balance directive to."
        )
    newest = max(candidates, key=lambda e: (e.date, isinstance(e, data.Transaction)))
    return None, newest.meta["filename"]


def apply_edits(files, edits, balances, account, append=None):
    """Write the planned edits over each file's original lines in ``files``.

    ``append`` is ``(path, line)``: a directive to add at the end of that file.
    """
    out = {}

    def lines(path):
        if path not in out:
            out[path] = list(files[path])
        return out[path]

    for (path, lineno), (op, value) in edits.items():
        i = lineno - 1
        text = lines(path)
        if op == "comment":
            text[i] = ";" + text[i]
        elif op == "delete":
            text[i] = None
        elif op == "amount":
            text[i] = _rewrite_number(text[i], account, value)
    for (path, lineno), number in balances.items():
        text = lines(path)
        text[lineno - 1] = _rewrite_number(text[lineno - 1], account, number)
    if append is not None:
        path, line = append
        text = lines(path)
        last = next((ln for ln in reversed(text) if ln is not None), "")
        # Match the file's own line ending, so a CRLF ledger stays CRLF.
        eol = "\r\n" if last.endswith("\r\n") else "\n"
        if last and not last.endswith("\n"):
            text.append(eol)
        text.append(f"{eol}{line}{eol}")
    for path, text in out.items():
        write_ledger(path, text)


def restore(files):
    """Put every file read into ``files`` back exactly as it was."""
    for path, lines in files.items():
        write_ledger(path, lines)


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
