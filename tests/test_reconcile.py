"""Tests for promoting provisional (HELD) Up entries once they settle.

The behaviour these lock down, in order of how expensive it is to get wrong:

* a promoted entry is edited **in place** -- hand-written annotations survive.
  Deleting and re-importing the settled copy would redo any automatic completion
  and lose them, and held-at-import entries often carry one.
* a hold that settles at a different amount has its posting corrected, and every
  balance directive snapshotted while it was live moves by the same delta.
* the newest balance directive is never adjusted -- it is the evidence that the
  money really moved, so a wrong conclusion rolls back instead of corrupting the
  ledger.
"""

import datetime
import json
import os

from beancount import loader
from click.testing import CliRunner

from beancount_upbank import reconcile
from beancount_upbank.cli import cli

ACCOUNT = "Assets:Bank:Alice-Upbank"

LEDGER = """\
2000-01-01 open Assets:Bank:Alice-Upbank
2000-01-01 open Expenses:Food:Alcohol
2000-01-01 open Equity:Opening

2026-08-01 * "seed"
  Assets:Bank:Alice-Upbank   1000.00 AUD
  Equity:Opening

2026-09-01 * "Example Brewing Co." "EXAMPLE BREW, SYDNEY"  ;drinks with a friend
  up_hold: "held-example"
  Assets:Bank:Alice-Upbank    -30.00 AUD
  Expenses:Food:Alcohol

2026-09-02 balance Assets:Bank:Alice-Upbank    970.00 AUD

2026-09-06 balance Assets:Bank:Alice-Upbank    {anchor} AUD
"""


def _pull(status, value, settled="2026-09-03T09:00:00+10:00", up_id="held-example"):
    return [
        {
            "type": "transactions",
            "id": up_id,
            "attributes": {
                "status": status,
                "rawText": "EXAMPLE BREW, SYDNEY",
                "description": "Example Brewing Co.",
                "message": None,
                "holdInfo": None,
                "amount": {"currencyCode": "AUD", "value": value},
                "createdAt": "2026-09-01T12:00:00+10:00",
                "settledAt": settled if status == "SETTLED" else None,
            },
            "relationships": {},
            "links": {},
        }
    ]


def _invoke(*args):
    # A pull file means no request is made, so no token is needed.
    return CliRunner().invoke(
        cli, ["reconcile", *map(str, args)], env={"UPBANK_TOKEN": ""}
    )


def _run(tmp_path, ledger_text, pull, *extra):
    ledger = tmp_path / "ledger.beancount"
    ledger.write_text(ledger_text)
    pull_path = tmp_path / "pull.json"
    pull_path.write_text(json.dumps(pull))
    return _invoke(ACCOUNT, ledger, "--pull", pull_path, *extra), ledger


def _split_ledger(tmp_path, anchor):
    """A main ledger that includes two files: the hold, and a snapshot it moves."""
    main = tmp_path / "main.beancount"
    main.write_text('include "other.beancount"\ninclude "up.beancount"\n')
    other = tmp_path / "other.beancount"
    other.write_text(
        """\
2000-01-01 open Assets:Bank:Alice-Upbank
2000-01-01 open Expenses:Food:Alcohol
2000-01-01 open Equity:Opening

2026-08-01 * "seed"
  Assets:Bank:Alice-Upbank   1000.00 AUD
  Equity:Opening

2026-09-02 balance Assets:Bank:Alice-Upbank    970.00 AUD
"""
    )
    up = tmp_path / "up.beancount"
    up.write_text(
        f"""\
2026-09-01 * "Example Brewing Co." "EXAMPLE BREW, SYDNEY"
  up_hold: "held-example"
  Assets:Bank:Alice-Upbank    -30.00 AUD
  Expenses:Food:Alcohol

2026-09-06 balance Assets:Bank:Alice-Upbank    {anchor} AUD
"""
    )
    pull = tmp_path / "pull.json"
    pull.write_text(json.dumps(_pull("SETTLED", "-31.25")))
    return main, up, other, pull


def test_edits_land_in_the_included_files_that_hold_them(tmp_path):
    # The main ledger is all the command is given.
    main, up, other, pull = _split_ledger(tmp_path, "968.75")
    result = _invoke(ACCOUNT, main, "--pull", pull)
    assert result.exit_code == 0, result.output
    assert "-31.25 AUD" in up.read_text()
    assert "up_hold" not in up.read_text()
    assert "968.75 AUD" in other.read_text(), "the other file's snapshot moved"
    assert "Verified against the untouched 2026-09-06 balance" in result.output


def test_a_rollback_restores_every_file_it_edited(tmp_path):
    main, up, other, pull = _split_ledger(tmp_path, "900.00")
    before = {path: path.read_text() for path in (up, other)}
    result = _invoke(ACCOUNT, main, "--pull", pull)
    assert result.exit_code == 1
    assert "Rolled back" in result.output
    assert {path: path.read_text() for path in (up, other)} == before


def test_newest_directives_in_two_files_are_refused_not_guessed(tmp_path):
    # Line numbers in different files say nothing about which was written last,
    # and the wrong pick would adjust the real anchor.
    main, up, other, pull = _split_ledger(tmp_path, "968.75")
    other.write_text(
        other.read_text()
        + "\n2026-09-06 balance Assets:Bank:Alice-Upbank  970.00 AUD\n"
    )
    before = {path: path.read_text() for path in (up, other)}
    result = _invoke(ACCOUNT, main, "--pull", pull)
    assert result.exit_code == 1
    assert "more than one file" in result.output
    assert {path: path.read_text() for path in (up, other)} == before


def test_a_plugin_error_with_no_entry_is_not_a_failed_balance(monkeypatch):
    class Bare:
        message = "a plugin's own error type"

    monkeypatch.setattr(loader, "load_file", lambda root: ([], [Bare()], {}))
    assert reconcile.failing_balances("main", ACCOUNT, datetime.date(2026, 9, 1)) == []


def test_drifted_hold_is_promoted_in_place(tmp_path):
    # Settled $1.25 above the hold. The posting is corrected, the tag removed, and
    # the annotation and narration are untouched.
    result, ledger = _run(
        tmp_path, LEDGER.format(anchor="968.75"), _pull("SETTLED", "-31.25")
    )
    assert result.exit_code == 0, result.output
    text = ledger.read_text()

    assert "-31.25 AUD" in text, "posting corrected to the settled amount"
    assert "up_hold" not in text, "provisional tag removed once settled"
    assert ";drinks with a friend" in text, "hand-written annotation survives"
    assert '"EXAMPLE BREW, SYDNEY"' in text, "narration survives"
    # The balance directive taken while the hold was live moves by the drift;
    # the newest one is the evidence and is left alone.
    assert "968.75 AUD" in text, "anchor balance directive untouched"
    assert "970.00 AUD" not in text, "intermediate balance directive adjusted"
    assert list(tmp_path.glob("*.bak")) == [], "no backup file left behind"


def test_released_hold_is_commented_out(tmp_path):
    # Absent from a pull whose window covers it: the hold was released. The
    # entry is commented rather than deleted, so the history stays readable.
    result, ledger = _run(
        tmp_path,
        LEDGER.format(anchor="1000.00"),
        _pull("SETTLED", "-9.99", up_id="some-other-transaction"),
    )
    assert result.exit_code == 0, result.output
    text = ledger.read_text()
    assert ';2026-09-01 * "Example Brewing Co."' in text
    assert "released, never settled" in result.output


def test_a_release_nothing_can_confirm_is_left_alone(tmp_path):
    # Absence from a pull is only a hypothesis. With no balance directive dated
    # after the entry, nothing would catch a wrong one, so it is not acted on.
    unchecked = "".join(
        line
        for line in LEDGER.format(anchor="1000.00").splitlines(keepends=True)
        if " balance " not in line
    )
    result, ledger = _run(
        tmp_path, unchecked, _pull("SETTLED", "-9.99", up_id="some-other-transaction")
    )
    assert result.exit_code == 0, result.output
    assert "can confirm the release, so it is left alone" in result.output
    assert ledger.read_text() == unchecked


def test_wrong_conclusion_rolls_back(tmp_path):
    # The anchor says the money never came back, so treating the hold as
    # released must fail loudly and leave the ledger untouched.
    original = LEDGER.format(anchor="970.00")
    result, ledger = _run(tmp_path, original, _pull("SETTLED", "-9.99", up_id="other"))
    assert result.exit_code != 0
    assert "Rolled back" in result.output
    assert ledger.read_text() == original, "ledger restored exactly"
    assert list(tmp_path.glob("*.bak")) == [], "rolled back from memory, not a file"


def test_still_held_is_reported_and_left_alone(tmp_path):
    result, ledger = _run(
        tmp_path, LEDGER.format(anchor="970.00"), _pull("HELD", "-30.00")
    )
    assert result.exit_code == 0, result.output
    assert "still unsettled" in result.output
    assert "up_hold" in ledger.read_text(), "an unsettled hold keeps its tag"


def test_stale_hold_is_flagged(tmp_path):
    # Holds normally settle within days; this one is old enough to want a look.
    stale = LEDGER.format(anchor="970.00").replace("2026-09-01", "2026-08-01")
    pull = _pull("HELD", "-30.00")
    pull[0]["attributes"]["createdAt"] = "2026-08-01T18:31:26+10:00"
    result, _ = _run(tmp_path, stale, pull)
    assert result.exit_code == 0, result.output
    assert "!!" in result.output
    assert "pending for more than 7 days" in result.output


def test_dry_run_changes_nothing(tmp_path):
    original = LEDGER.format(anchor="968.75")
    result, ledger = _run(tmp_path, original, _pull("SETTLED", "-31.25"), "--dry-run")
    assert result.exit_code == 0, result.output
    assert "pending; re-run without --dry-run" in result.output
    assert ledger.read_text() == original


AGED = """\
2000-01-01 open Assets:Bank:Alice-Upbank
2000-01-01 open Expenses:Food:Alcohol
2000-01-01 open Equity:Opening

2026-08-01 * "seed"
  Assets:Bank:Alice-Upbank   1000.00 AUD
  Equity:Opening

2026-07-01 * "Ancient Hold" "ANCIENT HOLD PTY LTD   SYDNEY"
  up_hold: "aged-out-of-window"
  Assets:Bank:Alice-Upbank    -12.00 AUD
  Expenses:Food:Alcohol

2026-09-06 balance Assets:Bank:Alice-Upbank    {anchor} AUD
"""


def test_released_hold_outside_the_window_is_identified_by_the_gap(tmp_path):
    # A hold that aged out of the pull window cannot be checked against the API:
    # Up keeps no record of a release. But releasing it puts the money back, so
    # Up's balance rises by the held amount while the ledger still spends it.
    # That gap identifies the culprit when nothing else can.
    result, ledger = _run(
        tmp_path,
        AGED.format(anchor="1000.00"),  # the 12.00 came back
        _pull("SETTLED", "-31.25"),
    )
    assert result.exit_code == 0, result.output
    assert "a gap of 12.00 AUD" in result.output
    assert "released them and put the money back" in result.output
    assert "Ancient Hold" in result.output
    # Identified, not acted on: the API cannot confirm it, so this stays a
    # judgement call for a human.
    assert "up_hold" in ledger.read_text()


def test_gap_that_matches_no_hold_is_not_blamed_on_one(tmp_path):
    result, _ = _run(
        tmp_path, AGED.format(anchor="1007.00"), _pull("SETTLED", "-31.25")
    )
    assert result.exit_code == 0, result.output
    assert "a gap of 19.00 AUD" in result.output
    assert "No combination of pending holds accounts for it" in result.output


def test_no_gap_reports_nothing(tmp_path):
    result, _ = _run(tmp_path, AGED.format(anchor="988.00"), _pull("SETTLED", "-31.25"))
    assert result.exit_code == 0, result.output
    assert "gap of" not in result.output


def test_still_held_is_never_blamed_for_a_gap(tmp_path):
    # A hold the pull confirms is still HELD is deducted from Up's balance and
    # from the ledger alike, so it cannot be the cause of a gap. Blaming it
    # would advise deleting a live transaction that is going to settle.
    ledger = (
        AGED.format(anchor="1000.00")
        .replace("aged-out-of-window", "still-held")
        .replace("2026-07-01", "2026-09-01")
    )
    pull = _pull("HELD", "-12.00", up_id="still-held")
    pull[0]["attributes"]["createdAt"] = "2026-09-01T10:00:00+10:00"
    result, _ = _run(tmp_path, ledger, pull)
    assert result.exit_code == 0, result.output
    assert "released them and put the money back" not in result.output


def test_a_snapshot_taken_after_the_release_is_left_alone(tmp_path):
    # Up keeps no record of when a hold was released. The 09-02 snapshot was
    # taken while it was live and moves; the 09-04 one already has the money
    # back -- it fails until the entry is commented out -- so it stays.
    ledger = """\
2000-01-01 open Assets:Bank:Alice-Upbank
2000-01-01 open Expenses:Food:Alcohol
2000-01-01 open Equity:Opening

2026-08-01 * "seed"
  Assets:Bank:Alice-Upbank   1000.00 AUD
  Equity:Opening

2026-09-01 * "Gone" "GONE PTY LTD           SYDNEY"
  up_hold: "released"
  Assets:Bank:Alice-Upbank    -30.00 AUD
  Expenses:Food:Alcohol

2026-09-02 balance Assets:Bank:Alice-Upbank    970.00 AUD

2026-09-04 balance Assets:Bank:Alice-Upbank   1000.00 AUD

2026-09-06 balance Assets:Bank:Alice-Upbank   1000.00 AUD
"""
    result, path = _run(tmp_path, ledger, _pull("SETTLED", "-9.99", up_id="other"))
    assert result.exit_code == 0, result.output
    after = path.read_text()
    assert "2026-09-02 balance Assets:Bank:Alice-Upbank   1000.00 AUD" in after
    assert "2026-09-04 balance Assets:Bank:Alice-Upbank   1000.00 AUD" in after


def test_a_snapshot_failing_for_another_reason_still_rolls_back(tmp_path):
    # Left alone because it already fails, but leaving it alone does not make
    # it pass: the verification catches it rather than reporting success.
    ledger = LEDGER.format(anchor="1000.00").replace(
        "2026-09-02 balance Assets:Bank:Alice-Upbank    970.00 AUD",
        "2026-09-02 balance Assets:Bank:Alice-Upbank    555.00 AUD",
    )
    result, path = _run(tmp_path, ledger, _pull("SETTLED", "-9.99", up_id="other"))
    assert result.exit_code == 1
    assert "Rolled back" in result.output
    assert path.read_text() == ledger


def test_transfer_leg_is_reported_not_edited(tmp_path):
    # Both legs move together, but only this account's balances are in scope,
    # so editing would silently shift another account with no rollback signal.
    ledger = """\
2000-01-01 open Assets:Bank:Alice-Upbank
2000-01-01 open Assets:Bank:Joint-Everyday
2000-01-01 open Equity:Opening

2026-08-01 * "seed"
  Assets:Bank:Alice-Upbank   1000.00 AUD
  Equity:Opening

2026-09-01 * "ALICE EXAMPLE" "Up Alice"
  up_hold: "held-example"
  Assets:Bank:Alice-Upbank    -30.00 AUD
  Assets:Bank:Joint-Everyday

2026-09-06 balance Assets:Bank:Alice-Upbank    970.00 AUD
"""
    original = ledger
    result, path = _run(tmp_path, ledger, _pull("SETTLED", "-31.25"))
    assert result.exit_code == 0, result.output
    assert "transfer between own accounts" in result.output
    assert path.read_text() == original, "transfer legs are left alone"


def test_settled_without_a_timestamp_is_treated_as_pending(tmp_path):
    pull = _pull("SETTLED", "-31.25")
    pull[0]["attributes"]["settledAt"] = None
    result, path = _run(tmp_path, LEDGER.format(anchor="970.00"), pull)
    assert result.exit_code == 0, result.output
    assert "up_hold" in path.read_text(), "undated settlement stays provisional"


class FakeUp:
    """Stands in for UpbankClient in the fetch-from-Up mode."""

    def __init__(self, balance, transactions):
        self.balance = balance
        self.pulled = transactions
        self.since = []

    def accounts(self):
        return [{"id": "acct-1", "attributes": {"balance": {"value": self.balance}}}]

    def transactions(self, since, until=None, status=None, account_id=None):
        self.since.append(since)
        return self.pulled


def _fetch(monkeypatch, tmp_path, ledger_text, fake, *extra):
    from beancount_upbank import cli as upbank_cli

    monkeypatch.setattr(upbank_cli, "UpbankClient", lambda token: fake)
    ledger = tmp_path / "ledger.beancount"
    ledger.write_text(ledger_text)
    result = CliRunner().invoke(
        cli,
        ["reconcile", ACCOUNT, str(ledger), *extra],
        env={"UPBANK_TOKEN": "up:yeah:test"},
    )
    return result, ledger


def test_without_a_pull_it_asks_up_and_writes_todays_directive(
    monkeypatch, tmp_path, pinned_today
):
    fake = FakeUp("968.75", _pull("SETTLED", "-31.25"))
    result, ledger = _fetch(monkeypatch, tmp_path, LEDGER.format(anchor="968.75"), fake)
    assert result.exit_code == 0, result.output
    text = ledger.read_text()
    assert "up_hold" not in text
    last = text.rstrip("\n").splitlines()[-1]
    assert last.startswith(f"{pinned_today} balance {ACCOUNT}")
    assert last.endswith(" 968.75 AUD")
    assert "Verified against Up's balance (968.75 AUD)" in result.output
    # The fetch reaches back to the oldest hold, so its absence would be real.
    assert {s.date().isoformat() for s in fake.since} == {"2026-09-01"}


def test_a_directive_already_dated_today_is_replaced_not_duplicated(
    monkeypatch, tmp_path, pinned_today
):
    text = LEDGER.format(anchor="968.75") + (
        f"\n{pinned_today} balance Assets:Bank:Alice-Upbank    970.00 AUD\n"
    )
    fake = FakeUp("968.75", _pull("SETTLED", "-31.25"))
    result, ledger = _fetch(monkeypatch, tmp_path, text, fake)
    assert result.exit_code == 0, result.output
    after = ledger.read_text()
    assert after.count(f"{pinned_today} balance") == 1
    assert "970.00" not in after.split(str(pinned_today))[-1]


def test_a_hold_older_than_any_transaction_is_judged_not_skipped(
    monkeypatch, tmp_path, pinned_today
):
    # Up returned nothing at all, but it was asked from the hold's date, so the
    # hold's absence is real: it was released, and the balance confirms it.
    fake = FakeUp("1000.00", [])
    result, ledger = _fetch(monkeypatch, tmp_path, LEDGER.format(anchor="970.00"), fake)
    assert result.exit_code == 0, result.output
    assert "released, never settled" in result.output
    assert ';  up_hold: "held-example"' in ledger.read_text()


def test_a_failed_check_also_takes_the_new_directive_back_out(
    monkeypatch, tmp_path, pinned_today
):
    original = LEDGER.format(anchor="970.00")
    fake = FakeUp("500.00", _pull("HELD", "-30.00"))
    result, ledger = _fetch(monkeypatch, tmp_path, original, fake)
    assert result.exit_code == 1
    assert "Rolled back" in result.output
    assert ledger.read_text() == original


def test_with_no_holds_it_still_writes_todays_directive(
    monkeypatch, tmp_path, pinned_today
):
    settled = LEDGER.format(anchor="970.00").replace('  up_hold: "held-example"\n', "")
    fake = FakeUp("970.00", [])
    result, ledger = _fetch(monkeypatch, tmp_path, settled, fake)
    assert result.exit_code == 0, result.output
    assert ledger.read_text().rstrip().endswith("970.00 AUD")
    assert str(pinned_today) in ledger.read_text()


def _with_todays_purchase(pinned_today, comment=""):
    """The settled ledger plus a purchase dated today, then a closing poptag."""
    settled = LEDGER.format(anchor="970.00").replace('  up_hold: "held-example"\n', "")
    purchase = (
        f'{comment}{pinned_today} * "Coffee"\n'
        "  Assets:Bank:Alice-Upbank     -5.00 AUD\n"
        "  Expenses:Food:Alcohol\n"
    )
    return f"pushtag #alice\n{settled}\n{purchase}\npoptag #alice\n"


def test_todays_directive_goes_before_todays_transactions(
    monkeypatch, tmp_path, pinned_today
):
    # Beancount checks a balance at the start of its date, so it reads in
    # order above the day's transactions, not after them.
    original = _with_todays_purchase(pinned_today)
    result, ledger = _fetch(monkeypatch, tmp_path, original, FakeUp("970.00", []))
    assert result.exit_code == 0, result.output
    assert "insert" in result.output
    directive = f"{pinned_today} balance {ACCOUNT}"
    assert ledger.read_text() == original.replace(
        f"{pinned_today} *", f"{directive:<37}     970.00 AUD\n\n{pinned_today} *"
    )


def test_todays_directive_keeps_a_comment_with_its_transaction(
    monkeypatch, tmp_path, pinned_today
):
    original = _with_todays_purchase(pinned_today, comment="; flat white\n")
    result, ledger = _fetch(monkeypatch, tmp_path, original, FakeUp("970.00", []))
    assert result.exit_code == 0, result.output
    assert f"970.00 AUD\n\n; flat white\n{pinned_today} *" in ledger.read_text()


def test_with_nothing_dated_today_the_directive_goes_at_the_end(
    monkeypatch, tmp_path, pinned_today
):
    settled = LEDGER.format(anchor="970.00").replace('  up_hold: "held-example"\n', "")
    result, ledger = _fetch(monkeypatch, tmp_path, settled, FakeUp("970.00", []))
    assert result.exit_code == 0, result.output
    assert "append" in result.output
    last = ledger.read_text().rstrip("\n").splitlines()[-1]
    assert last.startswith(f"{pinned_today} balance {ACCOUNT}")


def test_an_inserted_directive_in_a_crlf_ledger_stays_crlf(
    monkeypatch, tmp_path, pinned_today
):
    from beancount_upbank import cli as upbank_cli

    monkeypatch.setattr(upbank_cli, "UpbankClient", lambda token: FakeUp("970.00", []))
    ledger = tmp_path / "ledger.beancount"
    text = _with_todays_purchase(pinned_today).replace("\n", "\r\n")
    ledger.write_bytes(text.encode())
    result = CliRunner().invoke(
        cli, ["reconcile", ACCOUNT, str(ledger)], env={"UPBANK_TOKEN": "up:yeah:test"}
    )
    assert result.exit_code == 0, result.output
    after = ledger.read_bytes()
    assert f"{pinned_today} balance".encode() in after
    assert b"\n" not in after.replace(b"\r\n", b"")


def test_fetch_dry_run_changes_nothing(monkeypatch, tmp_path, pinned_today):
    original = LEDGER.format(anchor="968.75")
    fake = FakeUp("968.75", _pull("SETTLED", "-31.25"))
    result, ledger = _fetch(monkeypatch, tmp_path, original, fake, "--dry-run")
    assert result.exit_code == 0, result.output
    assert "append" in result.output
    assert ledger.read_text() == original


def test_nothing_to_change_says_so(monkeypatch, tmp_path, pinned_today):
    settled = LEDGER.format(anchor="970.00").replace('  up_hold: "held-example"\n', "")
    settled += f"\n{pinned_today} balance Assets:Bank:Alice-Upbank    970.00 AUD\n"
    result, ledger = _fetch(monkeypatch, tmp_path, settled, FakeUp("970.00", []))
    assert result.exit_code == 0, result.output
    assert "Nothing to change." in result.output
    assert ledger.read_text() == settled


def test_a_crlf_ledger_stays_crlf(monkeypatch, tmp_path, pinned_today):
    from beancount_upbank import cli as upbank_cli

    monkeypatch.setattr(
        upbank_cli,
        "UpbankClient",
        lambda token: FakeUp("968.75", _pull("SETTLED", "-31.25")),
    )
    ledger = tmp_path / "ledger.beancount"
    ledger.write_bytes(LEDGER.format(anchor="968.75").replace("\n", "\r\n").encode())
    result = CliRunner().invoke(
        cli, ["reconcile", ACCOUNT, str(ledger)], env={"UPBANK_TOKEN": "up:yeah:test"}
    )
    assert result.exit_code == 0, result.output
    assert b"\n" not in ledger.read_bytes().replace(b"\r\n", b"")


def test_an_included_file_given_alone_is_refused_up_front(tmp_path):
    _, up, _, pull = _split_ledger(tmp_path, "968.75")
    before = up.read_text()
    result = _invoke(ACCOUNT, up, "--pull", pull)
    assert result.exit_code == 1
    assert "never opens Assets:Bank:Alice-Upbank" in result.output
    assert "Rolled back" not in result.output
    assert up.read_text() == before


def test_two_directives_for_today_are_refused(monkeypatch, tmp_path, pinned_today):
    line = f"\n{pinned_today} balance Assets:Bank:Alice-Upbank    970.00 AUD\n"
    original = LEDGER.format(anchor="970.00") + line + line
    result, ledger = _fetch(monkeypatch, tmp_path, original, FakeUp("970.00", []))
    assert result.exit_code == 1
    assert "2 balance directives dated 2026-09-07" in result.output
    assert ledger.read_text() == original


def test_todays_directive_follows_the_import_into_a_new_file(
    monkeypatch, tmp_path, pinned_today
):
    # The newest balance directive is in last year's file, but the import has
    # moved on to a new one; a transfer in another bank's file is newer still.
    from beancount_upbank import cli as upbank_cli

    monkeypatch.setattr(upbank_cli, "UpbankClient", lambda token: FakeUp("950.00", []))
    main = tmp_path / "main.beancount"
    main.write_text(
        'include "old.beancount"\ninclude "new.beancount"\ninclude "bank.beancount"\n'
    )
    old = tmp_path / "old.beancount"
    old.write_text(
        """\
2000-01-01 open Assets:Bank:Alice-Upbank
2000-01-01 open Assets:Bank:Other
2000-01-01 open Expenses:Food
2000-01-01 open Equity:Opening

2026-08-01 * "seed"
  Assets:Bank:Alice-Upbank   1000.00 AUD
  Equity:Opening

2026-09-02 balance Assets:Bank:Alice-Upbank    1000.00 AUD
"""
    )
    new = tmp_path / "new.beancount"
    new.write_text(
        """\
2026-09-05 * "Lunch"
  Assets:Bank:Alice-Upbank   -20.00 AUD
  Expenses:Food
"""
    )
    bank = tmp_path / "bank.beancount"
    bank.write_text(
        """\
2026-09-06 * "To Up"
  Assets:Bank:Other          30.00 AUD
  Assets:Bank:Alice-Upbank  -30.00 AUD
"""
    )
    result = CliRunner().invoke(
        cli, ["reconcile", ACCOUNT, str(main)], env={"UPBANK_TOKEN": "up:yeah:test"}
    )
    assert result.exit_code == 0, result.output
    assert f"{pinned_today} balance" in new.read_text()
    assert f"{pinned_today} balance" not in old.read_text() + bank.read_text()


def test_a_snapshot_between_two_releases_moves_for_the_later_one_only(tmp_path):
    # A was released before the 09-05 snapshot, B after it. The snapshot fails
    # by A's 30.00 before the run, so it moves for B alone.
    ledger = """\
2000-01-01 open Assets:Bank:Alice-Upbank
2000-01-01 open Expenses:Food
2000-01-01 open Equity:Opening

2026-08-01 * "seed"
  Assets:Bank:Alice-Upbank   1000.00 AUD
  Equity:Opening

2026-09-01 * "A"
  up_hold: "a"
  Assets:Bank:Alice-Upbank    -30.00 AUD
  Expenses:Food

2026-09-03 * "B"
  up_hold: "b"
  Assets:Bank:Alice-Upbank    -10.00 AUD
  Expenses:Food

2026-09-05 balance Assets:Bank:Alice-Upbank    990.00 AUD

2026-09-06 balance Assets:Bank:Alice-Upbank   1000.00 AUD
"""
    # Two unrelated transactions frame the window; both holds are absent.
    pull = _pull("SETTLED", "-1.00", up_id="first")
    late = _pull("SETTLED", "-1.00", up_id="last")[0]
    late["attributes"]["createdAt"] = "2026-09-06T09:00:00+10:00"
    result, path = _run(tmp_path, ledger, [*pull, late])
    assert result.exit_code == 0, result.output
    assert (
        "2026-09-05 balance Assets:Bank:Alice-Upbank   1000.00 AUD" in path.read_text()
    )


def test_the_runs_own_edits_are_not_reported_as_a_gap(tmp_path):
    # A rollback for an unrelated broken snapshot must not blame a gap the
    # run's own released hold accounts for.
    ledger = LEDGER.format(anchor="1000.00").replace(
        "2026-09-02 balance Assets:Bank:Alice-Upbank    970.00 AUD",
        "2026-09-02 balance Assets:Bank:Alice-Upbank    555.00 AUD",
    )
    result, _ = _run(tmp_path, ledger, _pull("SETTLED", "-9.99", up_id="other"))
    assert result.exit_code == 1
    assert "gap of" not in result.output


def test_an_amount_written_as_an_expression_is_left_to_fix_by_hand(tmp_path):
    # Rewriting "the first number" would turn -15.00 * 2 into -31.25 * 2.
    original = LEDGER.format(anchor="968.75").replace("-30.00 AUD", "-15.00 * 2 AUD")
    result, ledger = _run(tmp_path, original, _pull("SETTLED", "-31.25"))
    assert result.exit_code == 1
    assert "fix it by hand" in result.output
    assert "Traceback" not in result.output
    assert ledger.read_text() == original


def test_a_ledger_saved_while_up_is_read_is_left_alone(
    monkeypatch, tmp_path, pinned_today
):
    # The edit shifts every line number the run planned against, and writing
    # over it would lose it.
    ledger = tmp_path / "ledger.beancount"

    class SavedMeanwhile(FakeUp):
        def accounts(self):
            if not ledger.read_text().startswith(";"):
                ledger.write_text("; a note added mid-run\n" + ledger.read_text())
                stat = ledger.stat()
                os.utime(ledger, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
            return super().accounts()

    fake = SavedMeanwhile("968.75", _pull("SETTLED", "-31.25"))
    result, _ = _fetch(monkeypatch, tmp_path, LEDGER.format(anchor="968.75"), fake)
    assert result.exit_code == 1
    assert "changed after the ledger was loaded" in result.output
    assert ledger.read_text() == "; a note added mid-run\n" + LEDGER.format(
        anchor="968.75"
    )
