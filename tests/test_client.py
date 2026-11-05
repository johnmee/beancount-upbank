"""Tests for the Up client and its balance assertion.

Beancount checks a `balance` directive at the *start* of its date; Up reports
the balance *now*. These tests pin the conversion between the two.
"""

import datetime
import json
from decimal import Decimal

import pytest
import requests

from beancount_upbank import client as upbank_client
from beancount_upbank.client import (
    UpbankError,
    select_account,
    start_of_day_assertion,
    start_of_day_balance,
)

DAY = datetime.date(2026, 9, 7)


def _txn(created, value, status="SETTLED", up_id=None):
    return {
        "id": up_id or f"{created}-{value}",
        "attributes": {
            "createdAt": created,
            "status": status,
            "amount": {"value": value},
        },
    }


def test_no_transactions_today_leaves_balance_unchanged():
    pulled = [_txn("2026-09-06T20:00:00+10:00", "-30.00")]
    assert start_of_day_balance(Decimal("200.00"), pulled, DAY) == Decimal("200.00")


def test_todays_transactions_are_backed_out():
    # A snapshot taken in the afternoon has the morning's purchase deducted, and
    # a later purchase lands the same day. Yesterday's credit is not today's.
    pulled = [
        _txn("2026-09-07T13:00:00+10:00", "-40.00"),
        _txn("2026-09-07T10:00:00+10:00", "-15.00"),
        _txn("2026-09-06T05:00:00+10:00", "30.00"),
    ]
    assert start_of_day_balance(Decimal("145.00"), pulled, DAY) == Decimal("200.00")


def test_held_transactions_are_backed_out_too():
    # Up's balance is the available balance, with holds already deducted.
    pulled = [_txn("2026-09-07T13:00:00+10:00", "-40.00", status="HELD")]
    assert start_of_day_balance(Decimal("160.00"), pulled, DAY) == Decimal("200.00")


def test_credits_today_are_backed_out():
    pulled = [_txn("2026-09-07T20:00:00+10:00", "250.00")]
    assert start_of_day_balance(Decimal("450.00"), pulled, DAY) == Decimal("200.00")


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


def test_get_recent_looks_back_the_given_number_of_days_from_now(monkeypatch):
    seen = {}

    def fake_get(self, path, params=None):
        seen.update(params)
        return []

    monkeypatch.setattr(upbank_client.UpbankClient, "get", fake_get)
    before = datetime.datetime.now(datetime.timezone.utc)
    upbank_client.UpbankClient("t").get_recent(30)
    after = datetime.datetime.now(datetime.timezone.utc)

    since = datetime.datetime.fromisoformat(seen["filter[since]"])
    assert since.tzinfo is not None
    assert (
        before - datetime.timedelta(days=30)
        <= since
        <= after - datetime.timedelta(days=30)
    )


def test_assertion_backs_out_todays_transactions():
    today = datetime.datetime.now(upbank_client.UP_TZ).date()
    pulled = [_txn(f"{today}T13:00:00+10:00", "-40.00")]
    fake = FakeClient(["160.00", "160.00"], [pulled, pulled])
    assert start_of_day_assertion(fake) == (today, Decimal("200.00"))


def test_assertion_retries_when_a_transaction_lands_mid_read():
    today = datetime.datetime.now(upbank_client.UP_TZ).date()
    first = [_txn(f"{today}T10:00:00+10:00", "-15.00")]
    second = first + [_txn(f"{today}T13:00:00+10:00", "-40.00")]
    # The balance read happens between the two pulls, so it may or may not
    # include the transaction that arrived; the pulls disagree, so retry.
    fake = FakeClient(["145.00"] * 3, [first, second, second, second])
    assert start_of_day_assertion(fake) == (today, Decimal("200.00"))


def test_assertion_gives_up_when_transactions_never_settle():
    today = datetime.datetime.now(upbank_client.UP_TZ).date()
    pulls = [[_txn(f"{today}T10:00:00+10:00", str(-n))] for n in range(1, 7)]
    fake = FakeClient(["100.00"] * 4, pulls)
    with pytest.raises(UpbankError):
        start_of_day_assertion(fake)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """A test must script its own replies; it must never reach the real API."""

    def refuse(*args, **kwargs):
        raise AssertionError(f"test tried to reach the network: {args}")

    monkeypatch.setattr(upbank_client.requests, "get", refuse)


class FakeResponse:
    def __init__(self, status=200, body=None, text=None, headers=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body)
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def _script_requests(monkeypatch, *responses):
    """Make requests.get answer with the given responses in order.

    An exception in the list is raised instead of returned. Returns the calls.
    """
    calls = []
    queue = list(responses)

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append({"url": url, "params": params, "timeout": timeout})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(upbank_client.requests, "get", fake_get)
    return calls


@pytest.fixture
def sleeps(monkeypatch):
    """Record the delays the client would have slept, without sleeping."""
    slept = []
    monkeypatch.setattr(upbank_client.time, "sleep", slept.append)
    return slept


def _ok(*ids, next_link=None):
    return FakeResponse(
        200,
        {"data": [{"id": i} for i in ids], "links": {"next": next_link}},
    )


def test_next_page_is_fetched_without_resending_the_query(monkeypatch, sleeps):
    # Up's `next` link already carries the whole query. Sending the parameters
    # again would append a second copy of each to it.
    next_link = "https://api.up.com.au/api/v1/transactions?page[after]=x&page[size]=100"
    calls = _script_requests(monkeypatch, _ok("a", next_link=next_link), _ok("b"))

    result = upbank_client.UpbankClient("t").get(
        "/transactions", params={"page[size]": 100}
    )

    assert [r["id"] for r in result] == ["a", "b"]
    assert calls[0]["params"] == {"page[size]": 100}
    assert calls[1]["url"] == next_link
    assert calls[1]["params"] is None


def test_times_are_sent_as_rfc3339(monkeypatch, sleeps):
    seen = {}

    def fake_get(self, path, params=None):
        seen.update(params)
        return []

    monkeypatch.setattr(upbank_client.UpbankClient, "get", fake_get)
    since = datetime.datetime(2026, 3, 1, tzinfo=upbank_client.UP_TZ)
    until = datetime.datetime(2026, 4, 1, 13, 5, 9, tzinfo=datetime.timezone.utc)
    upbank_client.UpbankClient("t").transactions(since, until)

    assert seen["filter[since]"] == "2026-03-01T00:00:00+11:00"
    assert seen["filter[until]"] == "2026-04-01T13:05:09+00:00"


def test_naive_times_are_refused_rather_than_sent_without_an_offset():
    with pytest.raises(ValueError, match="timezone"):
        upbank_client.UpbankClient("t").transactions(datetime.datetime(2026, 3, 1))


def test_get_month_uses_up_time_whatever_the_machine_clock(monkeypatch):
    seen = []
    monkeypatch.setattr(
        upbank_client.UpbankClient,
        "transactions",
        lambda self, since, until=None, **kw: seen.append((since, until)) or [],
    )
    client = upbank_client.UpbankClient("t")
    client.get_month(2026, 3)
    client.get_month(2026, 12)

    tz = upbank_client.UP_TZ
    assert seen == [
        (
            datetime.datetime(2026, 3, 1, tzinfo=tz),
            datetime.datetime(2026, 4, 1, tzinfo=tz),
        ),
        (
            datetime.datetime(2026, 12, 1, tzinfo=tz),
            datetime.datetime(2027, 1, 1, tzinfo=tz),
        ),
    ]


def test_every_request_has_a_timeout(monkeypatch, sleeps):
    calls = _script_requests(monkeypatch, _ok("a"), FakeResponse(200, {}, text="pong"))
    client = upbank_client.UpbankClient("t")
    client.get("/accounts")
    client.ping()
    assert [c["timeout"] for c in calls] == [upbank_client.TIMEOUT] * 2


def test_rate_limited_request_waits_as_told_then_succeeds(monkeypatch, sleeps):
    calls = _script_requests(
        monkeypatch,
        FakeResponse(
            429, {"errors": [{"status": "429"}]}, headers={"Retry-After": "7"}
        ),
        _ok("a"),
    )
    assert upbank_client.UpbankClient("t").get("/accounts") == [{"id": "a"}]
    assert sleeps == [7]
    assert len(calls) == 2


def test_server_errors_back_off_and_are_retried(monkeypatch, sleeps):
    calls = _script_requests(
        monkeypatch,
        FakeResponse(503, text="unavailable"),
        FakeResponse(502, text="bad gateway"),
        _ok("a"),
    )
    assert upbank_client.UpbankClient("t").get("/accounts") == [{"id": "a"}]
    assert sleeps == [1, 2]
    assert len(calls) == 3


def test_gives_up_after_the_last_attempt_and_says_why(monkeypatch, sleeps):
    n = upbank_client.MAX_ATTEMPTS
    calls = _script_requests(monkeypatch, *[FakeResponse(503, text="<html>")] * n)
    with pytest.raises(UpbankError, match="HTTP 503"):
        upbank_client.UpbankClient("t").get("/accounts")
    assert len(calls) == n


def test_client_errors_are_not_retried(monkeypatch, sleeps):
    calls = _script_requests(
        monkeypatch,
        FakeResponse(401, {"errors": [{"status": "401", "title": "Not Authorized"}]}),
    )
    with pytest.raises(UpbankError, match="401 Not Authorized"):
        upbank_client.UpbankClient("t").get("/accounts")
    assert len(calls) == 1
    assert sleeps == []


def test_a_non_json_reply_is_reported_not_a_traceback(monkeypatch, sleeps):
    _script_requests(monkeypatch, FakeResponse(200, text="<html>maintenance</html>"))
    with pytest.raises(UpbankError, match="maintenance"):
        upbank_client.UpbankClient("t").get("/accounts")


def test_connection_failures_are_retried_then_reported(monkeypatch, sleeps):
    n = upbank_client.MAX_ATTEMPTS
    boom = requests.ConnectionError("dns")
    calls = _script_requests(monkeypatch, *[boom] * n)
    with pytest.raises(UpbankError, match="Could not reach Up"):
        upbank_client.UpbankClient("t").get("/accounts")
    assert len(calls) == n

    _script_requests(monkeypatch, requests.Timeout("slow"), _ok("a"))
    assert upbank_client.UpbankClient("t").get("/accounts") == [{"id": "a"}]


@pytest.mark.parametrize("selector", [None, "Spending"])
def test_select_account_reports_a_token_with_no_accounts(selector):
    with pytest.raises(UpbankError, match="can see no Up accounts"):
        select_account([], selector)
