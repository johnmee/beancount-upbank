"""Tests for the ``upbank`` command line: wiring and output only.

The balance arithmetic itself is tested in test_client.py.
"""

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from beancount_upbank import cli as upbank_cli
from beancount_upbank.client import Resource, UpbankError


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


def _invoke(monkeypatch, fake, *args, env=None, tokens=None):
    """Run `upbank *args` against `fake`; append each token it gets to `tokens`."""

    def make_client(token):
        if tokens is not None:
            tokens.append(token)
        return fake

    monkeypatch.setattr(upbank_cli, "UpbankClient", make_client)
    return CliRunner().invoke(
        upbank_cli.cli,
        list(args),
        env={"UPBANK_TOKEN": "up:yeah:test", **(env or {})},
    )


class PingClient:
    def ping(self):
        return SimpleNamespace(text="pong")


def test_balance_prints_only_the_live_amount_and_currency(monkeypatch):
    fake = FakeClient(["145.00"], [])
    result = _invoke(monkeypatch, fake, "balance")
    assert result.exit_code == 0, result.output
    assert result.stdout == "145.00 AUD\n"


def test_balance_account_prints_a_midnight_directive(monkeypatch, pinned_today):
    pulled = [_txn(f"{pinned_today}T13:00:00+10:00", "-40.00")]
    fake = FakeClient(["160.00", "160.00"], [pulled, pulled])
    result = _invoke(monkeypatch, fake, "balance", "Assets:Bank:Up")
    assert result.exit_code == 0, result.output
    assert result.stdout == "2026-09-07 balance Assets:Bank:Up         200.00 AUD\n"


def test_balance_account_equals_live_when_nothing_happened_today(
    monkeypatch, pinned_today
):
    fake = FakeClient(["160.00", "160.00"], [[], []])
    result = _invoke(monkeypatch, fake, "balance", "Assets:Bank:Up")
    assert result.stdout == "2026-09-07 balance Assets:Bank:Up         160.00 AUD\n"


def test_balance_long_account_name_keeps_a_separating_space(monkeypatch, pinned_today):
    fake = FakeClient(["160.00", "160.00"], [[], []])
    name = "Assets:Bank:Up:A-Very-Long-Account-Name"
    result = _invoke(monkeypatch, fake, "balance", name)
    assert result.stdout == f"2026-09-07 balance {name}     160.00 AUD\n"


@pytest.mark.parametrize(
    "args",
    [
        ["balance", "Alice"],
        ["balance", "--midnight"],
        ["assertion", "Assets:Bank:Up"],
    ],
)
def test_stale_invocations_fail_and_write_nothing_to_stdout(monkeypatch, args):
    # Callers used to run `upbank assertion ACCOUNT >> ledger`.
    result = _invoke(monkeypatch, FakeClient([], []), *args)
    assert result.exit_code == 2
    assert result.stdout == ""


def test_token_warning_keeps_balance_stdout_clean(monkeypatch):
    fake = FakeClient(["145.00"], [])
    result = _invoke(monkeypatch, fake, "balance", env={"UPBANK_TOKEN": "nope"})
    assert result.exit_code == 0, result.output
    assert result.stdout == "145.00 AUD\n"
    assert "Warning" in result.stderr


def test_balance_midnight_reports_unsettled_transactions_cleanly(
    monkeypatch, pinned_today
):
    a = [_txn(f"{pinned_today}T09:00:00+10:00", "-1.00")]
    b: list[Resource] = []
    fake = FakeClient(["1.00"] * 4, [a, b, a, b, a, b])
    result = _invoke(monkeypatch, fake, "balance", "Assets:Bank:Up")
    assert result.exit_code == 1
    assert "Error: Up transactions kept changing" in result.output
    assert "Traceback" not in result.output


def test_client_errors_become_clean_command_line_errors(monkeypatch):
    class Failing:
        def accounts(self):
            raise UpbankError("Up API request to /accounts failed (HTTP 401)")

    result = _invoke(monkeypatch, Failing(), "balance")
    assert result.exit_code == 1
    assert "Error: Up API request to /accounts failed (HTTP 401)" in result.output
    assert "Traceback" not in result.output


def test_missing_token_is_a_usage_error():
    result = CliRunner().invoke(upbank_cli.cli, ["ping"], env={"UPBANK_TOKEN": ""})
    assert result.exit_code == 2
    assert "UPBANK_TOKEN" in result.output


def _ping(monkeypatch, *args, env):
    """Run `upbank *args ping`; return (result, tokens the client was given)."""
    tokens: list[str] = []
    result = _invoke(monkeypatch, PingClient(), *args, "ping", env=env, tokens=tokens)
    return result, tokens


def test_token_env_reference_on_the_command_line_reads_that_variable(monkeypatch):
    result, tokens = _ping(
        monkeypatch, "--token", "env:OTHER", env={"OTHER": "up:yeah:b"}
    )
    assert result.exit_code == 0, result.output
    assert tokens == ["up:yeah:b"]
    assert result.stderr == ""


def test_token_env_reference_in_upbank_token_reads_that_variable(monkeypatch):
    result, tokens = _ping(
        monkeypatch, env={"UPBANK_TOKEN": "env:OTHER", "OTHER": "up:yeah:b"}
    )
    assert result.exit_code == 0, result.output
    assert tokens == ["up:yeah:b"]


def test_literal_up_token_is_used_unchanged_without_warning(monkeypatch):
    result, tokens = _ping(monkeypatch, env={"UPBANK_TOKEN": "up:yeah:a"})
    assert result.exit_code == 0, result.output
    assert tokens == ["up:yeah:a"]
    assert result.stderr == ""


def test_token_env_reference_to_unset_or_empty_variable_is_a_usage_error(
    monkeypatch,
):
    # None unsets OTHER even if the shell running the tests exports it.
    for other in (None, ""):
        result, tokens = _ping(
            monkeypatch, env={"UPBANK_TOKEN": "env:OTHER", "OTHER": other}
        )
        assert result.exit_code == 2
        assert "Environment variable OTHER is unset or empty" in result.output
        assert tokens == []


def test_token_env_reference_without_a_name_is_a_usage_error(monkeypatch):
    result, tokens = _ping(monkeypatch, env={"UPBANK_TOKEN": "env:"})
    assert result.exit_code == 2
    assert "names no environment variable" in result.output
    assert tokens == []


def test_token_env_reference_is_not_followed_twice(monkeypatch):
    result, tokens = _ping(
        monkeypatch, env={"UPBANK_TOKEN": "env:A", "A": "env:B", "B": "up:yeah:b"}
    )
    assert result.exit_code == 2
    assert "Environment variable A holds another 'env:'" in result.output
    assert tokens == []


def test_literal_token_from_upbank_token_without_up_prefix_warns_but_runs(
    monkeypatch,
):
    result, tokens = _ping(monkeypatch, env={"UPBANK_TOKEN": "s3cr3t"})
    assert result.exit_code == 0, result.output
    assert tokens == ["s3cr3t"]
    assert result.stderr == (
        'Warning: UPBANK_TOKEN does not start with "up:"; '
        "did you mean UPBANK_TOKEN=env:NAME?\n"
    )


def test_literal_token_from_the_command_line_without_up_prefix_warns_but_runs(
    monkeypatch,
):
    result, tokens = _ping(monkeypatch, "--token", "s3cr3t", env={})
    assert result.exit_code == 0, result.output
    assert tokens == ["s3cr3t"]
    assert result.stderr == (
        'Warning: --token does not start with "up:"; did you mean --token env:NAME?\n'
    )


def test_env_token_without_up_prefix_warns_naming_the_variable(monkeypatch):
    result, tokens = _ping(
        monkeypatch, env={"UPBANK_TOKEN": "env:OTHER", "OTHER": "s3cr3t"}
    )
    assert result.exit_code == 0, result.output
    assert tokens == ["s3cr3t"]
    assert result.stderr == 'Warning: OTHER does not start with "up:"\n'


def test_help_needs_no_usable_token(monkeypatch):
    for token in (None, "env:OTHER", "s3cr3t"):
        result = _invoke(
            monkeypatch,
            PingClient(),
            "pull",
            "--help",
            env={"UPBANK_TOKEN": token, "OTHER": None},
        )
        assert result.exit_code == 0, result.output
        assert "Usage:" in result.output
        assert result.stderr == ""


def test_accounts_lists_every_account(monkeypatch):
    result = _invoke(monkeypatch, TwoAccountClient(), "accounts")
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert len(lines) == 2
    assert "spend-1" in lines[0] and "Spending" in lines[0]
    assert "saver-1" in lines[1] and "Holiday Fund" in lines[1]


def test_recent_defaults_to_the_first_account(monkeypatch):
    fake = TwoAccountClient()
    result = _invoke(monkeypatch, fake, "pull", "30")
    assert result.exit_code == 0, result.output
    assert fake.calls == [("recent", 30, "spend-1", None)]


def test_up_account_selects_by_display_name_case_insensitively(monkeypatch):
    fake = TwoAccountClient()
    result = _invoke(monkeypatch, fake, "--up-account", "holiday fund", "pull", "30")
    assert result.exit_code == 0, result.output
    assert fake.calls == [("recent", 30, "saver-1", None)]


def test_up_account_can_come_from_the_environment_and_select_by_id(monkeypatch):
    fake = TwoAccountClient()
    result = _invoke(monkeypatch, fake, "pull", "30", env={"UPBANK_ACCOUNT": "saver-1"})
    assert result.exit_code == 0, result.output
    assert fake.calls == [("recent", 30, "saver-1", None)]


def test_unknown_up_account_is_a_clean_error(monkeypatch):
    result = _invoke(monkeypatch, TwoAccountClient(), "--up-account", "nope", "pull")
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


def test_a_token_with_no_accounts_is_a_clean_error(monkeypatch):
    class NoAccounts:
        def accounts(self):
            return []

    result = _invoke(monkeypatch, NoAccounts(), "balance")
    assert result.exit_code == 1
    assert "Error: The token can see no Up accounts" in result.output
    assert "Traceback" not in result.output
