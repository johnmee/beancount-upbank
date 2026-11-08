"""Reconcile provisional (HELD) Up entries against a fresh pull.

An entry with ``up_hold`` metadata was HELD when imported. Up may later settle
it at a different amount (a tip, a currency conversion) or release it, and a
released hold simply vanishes: ``TransactionStatusEnum`` is ``HELD|SETTLED``
only, with no released state or tombstone. The importer only appends, so it
sees neither outcome.

Each provisional entry resolves one of five ways::

    settled, same amount   remove the up_hold line
    settled, drifted       correct the amount, remove the up_hold line
    released               comment the entry out
    still held             leave alone, report (loudly once stale)
    out of window          leave alone, report -- cannot be checked

Entries are fixed **in place**, keeping their position, narration and
annotations; deleting one and re-importing the settled copy would lose them.
Fixing an entry also shifts every ``balance`` directive taken while the hold
was live, because Up reports the *available* balance, holds already deducted.

Edits are surgical, by line number. Do NOT parse and reprint the ledger: that
discards ``;`` comments, alignment and pushtag/poptag structure.

USAGE
-----

::

    upbank reconcile ACCOUNT LEDGER [--pull FILE] [--dry-run]

``ACCOUNT``
    The beancount account Up transactions post to, e.g. ``Assets:Bank:Up``.
``LEDGER``
    The main ledger, not an included file: alone, an included file has no
    opening balance, so every balance directive in it fails. Edits land in
    whichever file holds each entry.
``--pull FILE``
    Up JSON saved by ``upbank pull``. Without it, Up is read live (the balance,
    and transactions back to the oldest hold, together) and today's balance
    directive is written from that read.
``--dry-run``
    Report the edits without applying them.

Verification needs a balance directive that postdates every release the run
acts on; it is never adjusted. Without ``--pull`` that is today's directive,
from the live read. With ``--pull`` it is the newest one in the ledger, so
append today's first::

    upbank balance Assets:Bank:Up >> up-2026.beancount
    upbank reconcile Assets:Bank:Up main.beancount --pull /tmp/up.json

If any balance directive after the earliest touched entry then fails, every
edited file is restored byte for byte and the run exits non-zero. Git is no
substitute: the run follows a fresh, uncommitted import that a checkout would
discard.
"""

import datetime
import enum
import itertools
import os
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from beancount import loader
from beancount.core import data
from beancount.core.amount import Amount

from .client import CURRENCY, HELD
from .markers import HOLD_KEY

# A line in a ledger file: ``(filename, lineno)``.
Location = tuple[str, int]
# Planned line edits: ``("comment" | "delete" | "amount", new amount or None)``.
Edits = dict[Location, tuple[str, Decimal | None]]
# Planned balance directive amounts.
Balances = dict[Location, Decimal]

# A hold this old is worth looking at: in practice Up settles within a few days,
# so anything lingering past a week is anomalous rather than merely pending.
STALE_DAYS = 7


class Kind(enum.Enum):
    """What became of a hold, as the pull tells it."""

    SETTLED_SAME = "settled at the held amount"
    DRIFTED = "settled at a different amount"
    RELEASED = "released: gone from the pull"
    OUTSTANDING = "still held"
    UNVERIFIABLE = "older than the pull, so cannot be checked"


class ReconcileError(Exception):
    """The ledger cannot be reconciled safely; nothing has been written."""


# eq=False: holds are compared by identity, never by their fields.
@dataclass(eq=False)
class Hold:
    """A provisional ledger entry and what happened to it.

    ``now`` and ``settled_on`` are set only for a settled hold.
    """

    kind: Kind
    entry: data.Transaction
    posting: data.Posting
    was: Decimal
    now: Decimal | None = None
    settled_on: datetime.date | None = None
    transfer: bool = False

    def age(self, today: datetime.date) -> int:
        return (today - self.entry.date).days

    @property
    def editable(self) -> bool:
        settled_or_gone = (Kind.SETTLED_SAME, Kind.DRIFTED, Kind.RELEASED)
        return self.kind in settled_or_gone and not self.transfer

    @property
    def delta(self) -> Decimal:
        if self.kind == Kind.DRIFTED:
            assert self.now is not None
            return self.now - self.was
        if self.kind == Kind.RELEASED:
            return -self.was
        return Decimal(0)


# Find and classify the holds.


def check_opened(entries: data.Directives, account: str, root: str) -> None:
    """Refuse a ledger that never opens the account.

    The usual cause is naming an included file rather than the main ledger:
    loaded on its own it has no ``open`` and no opening balance, so every
    balance directive would fail and the run would only roll back.
    """
    if not any(isinstance(e, data.Open) and e.account == account for e in entries):
        raise ReconcileError(
            f"{root} never opens {account}. Name your main ledger, the one "
            "that includes the others."
        )


def oldest_hold(entries: data.Directives, account: str) -> datetime.date | None:
    """The date of the oldest ``up_hold`` entry for the account; None if none."""
    return min((entry.date for entry, _ in _held(entries, account)), default=None)


def classify_holds(
    entries: data.Directives,
    pull: list[dict[str, Any]],
    account: str,
    date_range: tuple[datetime.date, datetime.date] | None = None,
) -> list[Hold]:
    """Classify every ``up_hold`` entry in the ledger against the pull.

    ``filter[since]``/``filter[until]`` select on ``createdAt``, which never
    changes, so an entry whose date falls inside the pull's range was certainly
    fetched and its absence is real. Outside that range absence means "not
    fetched" and nothing can be concluded.

    ``date_range`` is the ``(first, last)`` date the pull asked Up for. A pull read
    from a file does not say, so by default it is the range of dates the pull
    actually holds. That is safe only because ``upbank pull`` starts at
    midnight: a pull starting part-way through its first day would make a hold
    from earlier that day look released.

    ``entries`` comes from loading the *root* ledger, so balances mean what
    beancount says they mean, and every file it includes is in scope.
    """
    live = {t["id"]: t for t in pull}
    if date_range is None:
        dates = sorted(
            datetime.date.fromisoformat(t["attributes"]["createdAt"][:10]) for t in pull
        )
        date_range = (dates[0], dates[-1]) if dates else None

    holds = []
    for entry, posting in _held(entries, account):
        up_id = entry.meta[HOLD_KEY]
        was = number_of(posting.units)
        transfer = _is_transfer(entry, account)

        if date_range is None or not (date_range[0] <= entry.date <= date_range[1]):
            holds.append(
                Hold(Kind.UNVERIFIABLE, entry, posting, was, transfer=transfer)
            )
            continue

        trans = live.get(up_id)
        if trans is None:
            holds.append(Hold(Kind.RELEASED, entry, posting, was, transfer=transfer))
            continue

        attributes = trans["attributes"]
        settled_at = attributes.get("settledAt")
        if attributes["status"] == HELD or settled_at is None:
            # A SETTLED status with no settledAt is a skew we cannot date, so
            # treat it as still pending rather than guessing.
            holds.append(Hold(Kind.OUTSTANDING, entry, posting, was, transfer=transfer))
            continue

        now = Decimal(attributes["amount"]["value"])
        settled_on = datetime.date.fromisoformat(settled_at[:10])
        kind = Kind.SETTLED_SAME if now == was else Kind.DRIFTED
        holds.append(Hold(kind, entry, posting, was, now, settled_on, transfer))

    return holds


def _held(
    entries: data.Directives, account: str
) -> Iterator[tuple[data.Transaction, data.Posting]]:
    """Yield the entries still marked as held, each with its ``account`` posting."""
    for entry in entries:
        if not isinstance(entry, data.Transaction) or HOLD_KEY not in entry.meta:
            continue
        posting = next((p for p in entry.postings if p.account == account), None)
        if posting is not None and posting.units is not None:
            yield entry, posting


# Choose the anchor: the balance directive the run verifies against.


def newest_balance(entries: data.Directives, account: str) -> data.Balance | None:
    """The most recent balance directive for the account.

    Written by the current run, so it postdates anything the current pull shows
    as gone. It is never adjusted: leaving it untouched is what makes it an
    independent check rather than a restatement of our own guess.
    """
    balances = [
        e for e in entries if isinstance(e, data.Balance) and e.account == account
    ]
    if not balances:
        return None
    newest = max(e.date for e in balances)
    same_day = [e for e in balances if e.date == newest]
    # Running `upbank balance >> file` twice in one day leaves two on the same
    # date. Within one file the later is the newer snapshot, so tie-break on
    # position and treat the other as an ordinary intermediate balance. Line
    # numbers in different files say nothing about which came later.
    if len({e.meta.get("filename") for e in same_day}) > 1:
        raise ReconcileError(
            f"{account} has balance directives dated {newest} in more than one "
            "file, so which is newest is unclear; keep one and run again:\n"
            + "\n".join(f"  {e.meta['filename']}:{e.meta['lineno']}" for e in same_day)
        )
    return max(same_day, key=lambda e: e.meta.get("lineno", 0))


def place_balance(
    entries: data.Directives, account: str, day: datetime.date
) -> tuple[data.Balance | None, str | None, int | None]:
    """Where a fresh ``balance`` directive dated ``day`` goes.

    Returns ``(existing, path, before)``. ``existing`` is the account's
    directive already dated ``day``, which is replaced rather than duplicated;
    then ``path`` and ``before`` are None.

    Otherwise ``existing`` is None and ``path`` is the file to write to: the
    one holding the account's newest-dated directive or transaction, so a new
    year's file takes over as soon as the import writes to it. Transfers from
    your other accounts are left out: they live in the other account's file.
    On a tie a transaction wins, being where the importer writes.

    ``before`` is the line of the first entry in ``path`` dated ``day`` or
    later, which the directive goes in front of: beancount checks a balance at
    the start of its date, so it reads in order above that day's transactions.
    None when there is no such entry, and the directive goes at the end.

    Raises:
        ReconcileError: two directives are already dated ``day``, or the
            ledger holds nothing for the account to write beside.
    """
    balances = [
        e
        for e in entries
        if isinstance(e, data.Balance) and e.account == account and _editable(e)
    ]
    same_day = [e for e in balances if e.date == day]
    if len(same_day) > 1:
        raise ReconcileError(
            f"{account} has {len(same_day)} balance directives dated {day}; "
            "keep one and run again:\n"
            + "\n".join(f"  {e.meta['filename']}:{e.meta['lineno']}" for e in same_day)
        )
    if same_day:
        return same_day[0], None, None
    transactions = [
        e
        for e in entries
        if isinstance(e, data.Transaction)
        and _editable(e)
        and any(p.account == account for p in e.postings)
        and not _is_transfer(e, account)
    ]
    candidates = balances + transactions
    if not candidates:
        raise ReconcileError(
            f"The ledger has no balance directive or transaction for {account}, "
            "so there is no file to write today's balance directive to."
        )
    newest = max(candidates, key=lambda e: (e.date, isinstance(e, data.Transaction)))
    path: str = newest.meta["filename"]
    later = [
        int(e.meta["lineno"])
        for e in entries
        if e.date >= day and e.meta.get("filename") == path and "lineno" in e.meta
    ]
    return None, path, min(later, default=None)


def format_balance(day: datetime.date, account: str, number: Decimal) -> str:
    """A ``balance`` directive line, amounts aligned for a short account name."""
    return f"{f'{day} balance {account}':<37} {number:>10} {CURRENCY}"


# Plan the edits.


class Files(dict[str, list[str]]):
    """Each ledger file's original lines, read on first use and kept for rollback.

    Every file an edit lands in is read here before anything is written, so a
    rollback can put each one back byte for byte.

    ``loaded`` is each file's modification time when the ledger was loaded. A
    file saved since then is refused: the line numbers every edit is anchored
    on may no longer point at the same lines.
    """

    def __init__(self, loaded: dict[str, int] | None = None) -> None:
        super().__init__()
        self.loaded = loaded or {}
        # The files apply_edits has overwritten: the only ones restore() touches.
        self.written: set[str] = set()

    def __missing__(self, path: str) -> list[str]:
        self.check(path)
        lines = self[path] = _read_ledger(path)
        return lines

    def check(self, path: str) -> None:
        """Refuse ``path`` if it was saved after the ledger was loaded."""
        if path in self.loaded and os.stat(path).st_mtime_ns != self.loaded[path]:
            raise ReconcileError(
                f"{path} changed after the ledger was loaded; nothing was "
                "written. Run again."
            )


def modified_times(paths: Iterable[str]) -> dict[str, int]:
    """Each file's modification time, to hand to ``Files``."""
    return {path: os.stat(path).st_mtime_ns for path in paths}


def plan_edits(
    entries: data.Directives,
    holds: list[Hold],
    files: Files,
    account: str,
    anchor: data.Balance | None,
) -> tuple[Edits, Balances, list[data.Balance]]:
    """Work out every change to the ledger, without writing anything.

    Returns ``(edits, balances, unreachable)``: line operations keyed by
    ``(file, lineno)``, new amounts for balance directives keyed the same way,
    and the balance directives a plugin made, which need fixing by hand.

    beancount records a line number per directive and per posting but not per
    metadata key, so the ``up_hold:`` line is found by scanning the entry's span.

    ``files`` is a ``Files``; every file planned for editing is read into it.
    ``anchor`` is the balance directive the run verifies against, so it is
    never adjusted. Balance directives that need adjusting but were not read
    from a file (a plugin made them) are returned so the caller can say so.
    """
    edits: Edits = {}
    balances: Balances = {}
    unreachable: list[data.Balance] = []
    anchor_at = _where(anchor) if anchor is not None else None
    seen: dict[int, Sequence[Hold]] = {}

    for hold in holds:
        if not hold.editable:
            continue

        entry = hold.entry
        path = entry.meta["filename"]
        lines = files[path]
        span = range(
            entry.meta["lineno"],
            max(_posting_line(p) for p in entry.postings) + 1,
        )

        if hold.kind == Kind.RELEASED:
            for lineno in span:
                edits[path, lineno] = ("comment", None)
            # The release time is unknowable -- Up keeps no record of it -- so
            # every balance directive between the entry and the anchor is a
            # candidate. Those taken after the release are told apart below.
            until = anchor.date if anchor else datetime.date.max
        else:
            tag = next((ln for ln in span if f"{HOLD_KEY}:" in lines[ln - 1]), None)
            if tag is None:
                raise ReconcileError(
                    f"{entry.meta['filename']}:{entry.meta['lineno']}: "
                    f"cannot locate the {HOLD_KEY} line to remove"
                )
            edits[path, tag] = ("delete", None)
            if hold.kind == Kind.DRIFTED:
                edits[path, _posting_line(hold.posting)] = ("amount", hold.now)
            # Settled, so classify_holds dated the settlement.
            assert hold.settled_on is not None
            until = hold.settled_on

        if hold.delta:
            for other in entries:
                if not (
                    isinstance(other, data.Balance)
                    and other.account == account
                    and entry.date < other.date <= until
                ):
                    continue
                where = _where(other)
                if other is anchor or (where is not None and where == anchor_at):
                    continue
                if hold.kind == Kind.RELEASED:
                    if id(other) not in seen:
                        seen[id(other)] = _already_released(other, holds)
                    if any(h is hold for h in seen[id(other)]):
                        # Taken after the release: right as it stands.
                        continue
                if where is None:
                    unreachable.append(other)
                    continue
                files[where[0]]
                current = balances.get(where, number_of(other.amount))
                balances[where] = current + hold.delta

    return edits, balances, unreachable


def _already_released(snapshot: data.Balance, holds: list[Hold]) -> Sequence[Hold]:
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
        h
        for h in holds
        if h.editable and h.kind == Kind.RELEASED and h.entry.date < snapshot.date
    ]
    if snapshot.diff_amount is None:
        return ()
    gap = number_of(snapshot.diff_amount)
    for h in holds:
        if (
            h.editable
            and h.kind == Kind.DRIFTED
            and h.entry.date < snapshot.date
            and h.settled_on is not None
            and h.settled_on < snapshot.date
        ):
            gap += h.delta
    # In practice there are only ever a couple; the cap keeps a pathological
    # ledger from turning this into a subset-sum blow-up.
    for size in range(min(len(released), 6) + 1):
        for combo in itertools.combinations(released, size):
            if sum((h.was for h in combo), Decimal(0)) == gap:
                return combo
    return released


def _posting_line(posting: data.Posting) -> int:
    """The line a posting was read from; beancount types its ``meta`` as optional."""
    assert posting.meta is not None
    return int(posting.meta["lineno"])


def _where(entry: data.Directive) -> Location | None:
    """Where a directive was read from: ``(filename, lineno)``; None if not a file.

    A plugin's directives, and one this run has yet to write, have no place in
    a file that an edit could reach.
    """
    if not _editable(entry):
        return None
    return entry.meta["filename"], entry.meta["lineno"]


# Apply the edits, verify them, roll back.


def apply_edits(
    files: Files,
    edits: Edits,
    balances: Balances,
    account: str,
    new: tuple[str, str, int | None] | None = None,
) -> None:
    """Write the planned edits over each file's original lines in ``files``.

    ``new`` is ``(path, line, before)``: a directive to add to that file, in
    front of line ``before`` (and any comment lines directly above it), or at
    the end when ``before`` is None.
    """
    # A deleted line becomes "", which writes nothing but keeps every later
    # line number valid until the write.
    out: dict[str, list[str]] = {}

    def lines(path: str) -> list[str]:
        if path not in out:
            out[path] = list(files[path])
        return out[path]

    for (path, lineno), (op, value) in edits.items():
        i = lineno - 1
        text = lines(path)
        if op == "comment":
            text[i] = ";" + text[i]
        elif op == "delete":
            text[i] = ""
        elif op == "amount":
            assert value is not None
            text[i] = _rewrite_number(text[i], account, value)
    for (path, lineno), number in balances.items():
        text = lines(path)
        text[lineno - 1] = _rewrite_number(text[lineno - 1], account, number)
    if new is not None:
        path, line, before = new
        text = lines(path)
        last = next((ln for ln in reversed(text) if ln), "")
        # Match the file's own line ending, so a CRLF ledger stays CRLF.
        eol = "\r\n" if last.endswith("\r\n") else "\n"
        if before is None:
            if last and not last.endswith("\n"):
                text.append(eol)
            text.append(f"{eol}{line}{eol}")
        else:
            # Last, so every edit above still found its original line. Step
            # back over a comment heading the entry, keeping the two together;
            # "" is a deleted line, which writes nothing.
            i = before - 1
            j = i - 1
            while j >= 0 and (text[j] == "" or text[j].lstrip().startswith(";")):
                if text[j]:
                    i = j
                j -= 1
            text[i] = f"{line}{eol}{eol}{text[i]}"
    # Last chance to notice someone saving a file mid-run: once written, their
    # change would be lost, and restore() would put back the stale copy.
    for path in out:
        files.check(path)
    for path, text in out.items():
        # Marked first, so a write cut short (Ctrl-C) is still put back.
        files.written.add(path)
        _write_ledger(path, text)


def failing_balances(
    root: str, account: str, since: datetime.date
) -> list[data.BeancountError]:
    """Balance directives for the account, dated after ``since``, that fail.

    Absence from a pull is a *hypothesis* that a hold was released; the ledger
    reconciling afterwards is the proof. Every balance directive in range must
    hold, not just the anchor -- that is what catches a wrongly-adjusted
    intermediate snapshot, one taken after the hold was already released and
    which therefore must not move.

    Balance directives dated on or before the earliest entry we touched cannot
    be affected by these edits (a balance asserts as at the *start* of its
    date), so a pre-existing failure further back does not block the run.
    """
    _, errors, _ = loader.load_file(root)
    failures = []
    for error in errors:
        # A plugin's error need not carry an entry at all.
        entry = getattr(error, "entry", None)
        if (
            isinstance(entry, data.Balance)
            and entry.account == account
            and entry.date > since
        ):
            failures.append(error)
    return failures


def restore(files: Files) -> None:
    """Put every file this run wrote back exactly as it was read.

    Only those: a file the run never got as far as writing may have been
    changed by someone else since it was read, and is not ours to overwrite.
    """
    for path in files.written:
        _write_ledger(path, files[path])


# What follows the account on a line it can rewrite: a gap, a plain number, and
# then (after more space) the currency.
_PLAIN_AMOUNT = re.compile(r"(\s+)(-?[\d,]+(?:\.\d+)?)(?=\s+[A-Z])")


def _rewrite_number(line: str, account: str, new_number: Decimal) -> str:
    """Replace the amount that follows ``account``, preserving column width.

    Only a plain number followed by its currency is rewritten. An arithmetic
    expression (``-15.00 * 2 AUD``), a tolerance (``970.00 ~ 0.01 AUD``) or a
    longer account that merely starts with ``account`` is refused rather than
    half-rewritten.
    """
    head, marker, tail = line.partition(account)
    match = _PLAIN_AMOUNT.match(tail) if marker else None
    if match is None:
        raise ReconcileError(
            f"Cannot rewrite the amount on this line; fix it by hand and run "
            f"again:\n  {line.rstrip()}"
        )
    gap, old = match.groups()
    new = str(new_number)
    # Absorb the width change into the gap in front of the number so the
    # ledger's alignment survives.
    pad = len(old) - len(new)
    if pad > 0:
        gap += " " * pad
    elif pad < 0:
        gap = gap[:pad] if len(gap) + pad >= 1 else gap
    return head + marker + gap + new + tail[match.end() :]


# Explain a balance gap.


def shortfall_at(
    entries: data.Directives, account: str, balance: data.Balance
) -> Decimal:
    """How much the balance directive expects beyond what the ledger accumulates.

    Positive means Up holds more money than the ledger accounts for -- which is
    exactly what a released hold looks like, since Up added the money back and
    the ledger still has the entry spending it.
    """
    accumulated = Decimal(0)
    for entry in entries:
        # Balance directives assert as at the *start* of their date.
        if isinstance(entry, data.Transaction) and entry.date < balance.date:
            for posting in entry.postings:
                if posting.account == account and posting.units is not None:
                    accumulated += number_of(posting.units)
    return number_of(balance.amount) - accumulated


def explain(shortfall: Decimal, pending: Sequence[Hold]) -> tuple[Hold, ...] | None:
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
            if sum((h.was for h in combo), Decimal(0)) == -shortfall:
                return combo
    return None


# Helpers shared by the sections above.


def number_of(amount: Amount | None) -> Decimal:
    """The number in a beancount amount, without its currency.

    beancount types the amount and its number as optional, because a posting
    may leave them for interpolation. In a loaded ledger both are always set;
    this asserts so, which lets mypy treat the result as a plain ``Decimal``.
    """
    assert amount is not None and amount.number is not None
    return amount.number


def _is_transfer(entry: data.Transaction, account: str) -> bool:
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


def _editable(entry: data.Directive) -> bool:
    """True if the directive came from a file we can rewrite, not a plugin."""
    return os.path.isfile(entry.meta.get("filename") or "")


def _read_ledger(path: str) -> list[str]:
    """The ledger's lines, line endings intact, so a rollback is byte-exact."""
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.readlines()


def _write_ledger(path: str, lines: Iterable[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.writelines(lines)
