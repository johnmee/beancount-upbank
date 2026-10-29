"""Tests for the ``upbank`` command line: wiring and output only.

The balance arithmetic itself is tested in test_client.py.
"""

import datetime

from click.testing import CliRunner

from beancount_upbank import cli as upbank_cli
from beancount_upbank.client import UP_TZ, UpbankError

ACCOUNT = "Assets:Bank:Alice-Upbank"


def _txn(created, value, status="SETTLED", up_id=None):
    return {
        "id": up_id or f"{created}-{value}",
        "attributes": {
            "createdAt": created,
            "status": status,
            "amount": {"value": value},
        },
    }


class FakeClient:
    """Stands in for UpbankClient; each call pops the next scripted response."""

    def __init__(self, balances, pulls):
        self.balances = list(balances)
        self.pulls = list(pulls)

    def accounts(self):
        value = self.balances.pop(0)
        return [{"id": "acct-1", "attributes": {"balance": {"value": value}}}]

    def transactions(self, since, account_id=None):
        assert account_id == "acct-1"
        return self.pulls.pop(0)


class TwoAccountClient:
    """An Up login with a spending account and a saver."""

    def __init__(self):
        self.calls = []

    def accounts(self):
        return [
            {
                "id": "spend-1",
                "attributes": {
                    "displayName": "Spending",
                    "accountType": "TRANSACTIONAL",
                    "balance": {"value": "10.00"},
                },
            },
            {
                "id": "saver-1",
                "attributes": {
                    "displayName": "Holiday Fund",
                    "accountType": "SAVER",
                    "balance": {"value": "500.00"},
                },
            },
        ]

    def get_recent(self, days, account_id=None, status=None):
        self.calls.append(("recent", days, account_id, status))
        return []

    def get_month(self, year, month, account_id=None):
        self.calls.append(("month", year, month, account_id))
        return []


def _invoke(monkeypatch, fake, *args, env=None):
    monkeypatch.setattr(upbank_cli, "UpbankClient", lambda token: fake)
    return CliRunner().invoke(
        upbank_cli.cli, list(args), env={"UPBANK_TOKEN": "t", **(env or {})}
    )


def test_balance_shows_the_live_figure_unadjusted(monkeypatch):
    # For a human: what the Up app shows, today's spending included.
    fake = FakeClient(["145.00"], [])
    result = _invoke(monkeypatch, fake, "balance", "Alice")
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "Alice: 145.00 AUD"


def test_assertion_prints_start_of_today_balance(monkeypatch):
    today = datetime.datetime.now(UP_TZ).date()
    pulled = [_txn(f"{today}T13:00:00+10:00", "-40.00")]
    fake = FakeClient(["160.00", "160.00"], [pulled, pulled])
    result = _invoke(monkeypatch, fake, "assertion", ACCOUNT)
    assert result.exit_code == 0, result.output
    assert result.output.split() == [str(today), "balance", ACCOUNT, "200.00", "AUD"]


def test_assertion_rejects_a_bare_name_rather_than_writing_a_bad_directive(
    monkeypatch,
):
    # The argument used to be a short name ("Alice") that was spliced into
    # Assets:Bank:{name}-Upbank. A stale caller must fail, not append
    # `balance Alice ...` to a ledger.
    fake = FakeClient(["160.00", "160.00"], [[], []])
    result = _invoke(monkeypatch, fake, "assertion", "Alice")
    assert result.exit_code == 2
    assert "full beancount account name" in result.output
    assert "balance Alice" not in result.output


def test_client_errors_become_clean_command_line_errors(monkeypatch):
    class Failing:
        def accounts(self):
            raise UpbankError("Up API request to /accounts failed (HTTP 401)")

    result = _invoke(monkeypatch, Failing(), "balance", "Alice")
    assert result.exit_code == 1
    assert "Error: Up API request to /accounts failed (HTTP 401)" in result.output
    assert "Traceback" not in result.output


def test_missing_token_is_a_usage_error():
    result = CliRunner().invoke(upbank_cli.cli, ["ping"], env={"UPBANK_TOKEN": ""})
    assert result.exit_code == 2
    assert "UPBANK_TOKEN" in result.output


def test_accounts_lists_every_account(monkeypatch):
    result = _invoke(monkeypatch, TwoAccountClient(), "accounts")
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert len(lines) == 2
    assert "spend-1" in lines[0] and "Spending" in lines[0]
    assert "saver-1" in lines[1] and "Holiday Fund" in lines[1]


def test_recent_defaults_to_the_first_account(monkeypatch):
    fake = TwoAccountClient()
    result = _invoke(monkeypatch, fake, "recent", "30")
    assert result.exit_code == 0, result.output
    assert fake.calls == [("recent", 30, "spend-1", None)]


def test_up_account_selects_by_display_name_case_insensitively(monkeypatch):
    fake = TwoAccountClient()
    result = _invoke(monkeypatch, fake, "--up-account", "holiday fund", "recent", "30")
    assert result.exit_code == 0, result.output
    assert fake.calls == [("recent", 30, "saver-1", None)]


def test_up_account_can_come_from_the_environment_and_select_by_id(monkeypatch):
    fake = TwoAccountClient()
    result = _invoke(
        monkeypatch, fake, "recent", "30", env={"UPBANK_ACCOUNT": "saver-1"}
    )
    assert result.exit_code == 0, result.output
    assert fake.calls == [("recent", 30, "saver-1", None)]


def test_unknown_up_account_is_a_clean_error(monkeypatch):
    result = _invoke(monkeypatch, TwoAccountClient(), "--up-account", "nope", "recent")
    assert result.exit_code == 1
    assert "No Up account matches 'nope'" in result.output


def test_month_and_held_cover_every_account_unless_one_is_selected(monkeypatch):
    fake = TwoAccountClient()
    assert _invoke(monkeypatch, fake, "month", "2026", "3").exit_code == 0
    assert _invoke(monkeypatch, fake, "held", "10").exit_code == 0
    assert fake.calls == [
        ("month", 2026, 3, None),
        ("recent", 10, None, "HELD"),
    ]

    fake = TwoAccountClient()
    assert (
        _invoke(
            monkeypatch, fake, "--up-account", "Spending", "month", "2026", "3"
        ).exit_code
        == 0
    )
    assert (
        _invoke(monkeypatch, fake, "--up-account", "Spending", "held", "10").exit_code
        == 0
    )
    assert fake.calls == [
        ("month", 2026, 3, "spend-1"),
        ("recent", 10, "spend-1", "HELD"),
    ]
