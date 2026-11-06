"""Up Bank API.

Use the Up Bank API to retrieve transactions.
"""

import datetime
import time
from decimal import Decimal
from zoneinfo import ZoneInfo

import requests

URL = "https://api.up.com.au/api/v1"

# Maximum number of transactions upbank return per 'page'.
PAGE_SIZE = 100

# Seconds to wait for Up to answer one request before giving up on it.
TIMEOUT = 30

# How many times a request is tried when Up is rate limiting us, is failing, or
# cannot be reached. Only reads are made, so trying again is safe.
MAX_ATTEMPTS = 4

# The longest we will wait because Up asked us to, however long it asks.
MAX_RETRY_DELAY = 60

# Up accounts are in AUD only: a foreign purchase is converted, and its
# `amount` is the AUD that left the account.
CURRENCY = "AUD"

# Constants
HELD = "HELD"
SETTLED = "SETTLED"

# Up stamps `createdAt` with Sydney's offset, and the importer dates ledger
# entries from it, so a ledger day is a Sydney day whatever this machine's
# clock says. Up's API docs don't say which timezone they use; this is
# observed, not documented: stamps carry +10:00 in winter and +11:00 under
# daylight saving (seen November 2026, for a Sydney customer). Whether a
# customer elsewhere (e.g. Perth) gets their own local time is unconfirmed.
UP_TZ = ZoneInfo("Australia/Sydney")


def sydney_today() -> datetime.date:
    """Today's date in Sydney, the ledger's day."""
    return datetime.datetime.now(UP_TZ).date()


class UpbankError(Exception):
    """The Up API refused a request, or its answers could not be trusted."""


class UpbankClient:
    def __init__(self, token: str):
        """
        token: str: upbank "personal access token" from https://api.up.com.au/getting_started
        """
        self.token = token

    def get_month(self, year: int, month: int, account_id: str | None = None) -> []:
        """Get the transactions created in the given month.

        The month is a calendar month in Up's time, the same time the importer
        dates ledger entries in, whatever this machine's clock says.

        year: int: year to download eg: 2021
        month: int: month to download eg: 3
        account_id: str: only this account's; or None for every account.

        Returns:
              A list of transactions as a dict.
        """
        since = datetime.datetime(year, month, 1, tzinfo=UP_TZ)
        until = datetime.datetime(year + month // 12, month % 12 + 1, 1, tzinfo=UP_TZ)
        return self.transactions(since, until, account_id=account_id)

    def get_recent(
        self, days: int, account_id: str | None = None, status: str | None = None
    ) -> []:
        """Get all recent transactions.

        days: int: commencing this many days ago
        account_id: str: only this account's; or None for every account.
        status: str: "HELD" or "SETTLED"; or None for both.

        Returns:
              A list of transactions as a dict.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        since = now - datetime.timedelta(days=days)
        return self.transactions(since, status=status, account_id=account_id)

    def transactions(
        self,
        since: datetime.datetime,
        until: datetime.datetime | None = None,
        status: str | None = None,
        account_id: str | None = None,
    ) -> list:
        """Fetch a list of transactions.

        Args:
            since: tzaware datetime to start from
            until: tzaware datetime to stop at; or None for all.
            status: "HELD" or "SETTLED"; or None for both.
            account_id: only this account's transactions; or None for every
                account the token can see.

        Returns:
            list of transactions in dict format.

        Raises:
            ValueError: a datetime has no timezone. Up wants RFC 3339 times,
                which carry an offset; guessing one could shift the window.
        """
        for moment in (since, until):
            if moment is not None and moment.tzinfo is None:
                raise ValueError(
                    f"{moment!r} has no timezone; pass a tz-aware datetime"
                )

        # Upbank only returns PAGE_SIZE transactions per request; `get` follows
        # the links to the rest.
        params = {"page[size]": PAGE_SIZE, "filter[since]": since.isoformat()}
        if until is not None:
            params["filter[until]"] = until.isoformat()
        if status is not None:
            params["filter[status]"] = status
        path = (
            "/transactions"
            if account_id is None
            else f"/accounts/{account_id}/transactions"
        )
        response = self.get(path, params=params)
        return response

    def get(self, path, params: dict | None = None) -> list:
        """Send a GET request to Up.

        Args:
            path: includes the preceding slash.
            params: request parameters.

        Returns:
            list of data; probably dicts.

        Raises:
            UpbankError: Up answered with an error instead of data, or could not
                be reached.
        """
        result = []
        uri = f"{URL}{path}"
        while uri is not None:
            response = self._send(uri, params)
            # Up's `next` link already carries the whole query, so the
            # parameters are for the first page only.
            params = None
            try:
                data = response.json()
            except ValueError:
                raise UpbankError(
                    f"Up API request to {path} failed (HTTP {response.status_code}): "
                    f"{response.text[:200]}"
                ) from None
            if not isinstance(data, dict) or "data" not in data:
                # Up returns {"errors": [...]} on failure (e.g. 401 for an
                # invalid/revoked token). Surface a clear message instead of
                # crashing with KeyError: 'data'.
                errors = (isinstance(data, dict) and data.get("errors")) or [{}]
                detail = "; ".join(
                    " ".join(
                        part
                        for part in (e.get("status"), e.get("title"), e.get("detail"))
                        if part
                    )
                    for e in errors
                )
                raise UpbankError(
                    f"Up API request to {path} failed "
                    f"(HTTP {response.status_code}): {detail or response.text}"
                )
            result.extend(data["data"])
            try:
                uri = data["links"]["next"]
            except KeyError:
                break
        return result

    def _send(self, uri, params):
        """GET once, trying again when Up is limiting us, failing or unreachable.

        Returns the final response, which may still be an error: the caller
        reports it. A failure to connect at all raises UpbankError.
        """
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = requests.get(
                    uri, headers=self._headers(), params=params, timeout=TIMEOUT
                )
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt == MAX_ATTEMPTS:
                    raise UpbankError(f"Could not reach Up: {e}") from e
                delay = _backoff(attempt)
            else:
                retryable = response.status_code == 429 or response.status_code >= 500
                if not retryable or attempt == MAX_ATTEMPTS:
                    return response
                delay = _retry_after(response) or _backoff(attempt)
            time.sleep(delay)

    def ping(self):
        """Verify the access token is working.

        Returns:
            requests.Response

        Raises:
            UpbankError: Up could not be reached.
        """
        try:
            return requests.get(
                f"{URL}/util/ping", headers=self._headers(), timeout=TIMEOUT
            )
        except requests.RequestException as e:
            raise UpbankError(f"Could not reach Up: {e}") from e

    def accounts(self):
        """Fetch a list of accounts."""
        return self.get("/accounts")

    def categories(self):
        """Fetch a list of categories."""
        return self.get("/categories")

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}


def _backoff(attempt: int) -> int:
    """Seconds to wait after a failed attempt: 1, 2, 4, ..."""
    return 2 ** (attempt - 1)


def _retry_after(response) -> int | None:
    """How long Up asked us to wait, in whole seconds; None if it did not say."""
    try:
        seconds = int(response.headers.get("Retry-After", ""))
    except ValueError:
        return None
    return min(max(seconds, 0), MAX_RETRY_DELAY)


def start_of_day_balance(
    balance: Decimal, transactions: list, day: datetime.date
) -> Decimal:
    """Back `day`'s transactions out of a balance read during `day`.

    Beancount checks a `balance` directive at the *start* of its date, but Up
    reports the balance *now*, which already includes everything created
    earlier today. HELD transactions count as well as SETTLED ones, because
    Up's balance is the available balance with holds already deducted.

    A transaction's day is its `createdAt` date, the same date the importer
    gives it in the ledger.
    """
    moved = sum(
        (
            Decimal(t["attributes"]["amount"]["value"])
            for t in transactions
            if t["attributes"]["createdAt"][:10] == day.isoformat()
        ),
        Decimal(0),
    )
    return balance - moved


def select_account(accounts: list, selector: str | None = None) -> dict:
    """Pick one of the token's Up accounts.

    Args:
        accounts: the account resources, as ``UpbankClient.accounts()`` lists them.
        selector: an Up account id or display name (case-insensitive); or None
            for the first account Up lists.

    Raises:
        UpbankError: the token can see no accounts, or nothing matches the
            selector.
    """
    if not accounts:
        raise UpbankError("The token can see no Up accounts.")
    if selector is None:
        return accounts[0]
    for acct in accounts:
        name = acct["attributes"].get("displayName") or ""
        if acct["id"] == selector or name.lower() == selector.lower():
            return acct
    raise UpbankError(
        f"No Up account matches {selector!r}; `upbank accounts` lists them."
    )


def live_balance(client, selector: str | None = None) -> tuple:
    """The account's id and its balance right now, as the Up app shows it."""
    acct = select_account(client.accounts(), selector)
    return acct["id"], Decimal(acct["attributes"]["balance"]["value"])


def _fingerprint(transactions: list) -> set:
    return {(t["id"], t["attributes"]["amount"]["value"]) for t in transactions}


def start_of_day_assertion(
    client, today: datetime.date | None = None, account: str | None = None
) -> tuple:
    """The balance the ledger must show at the start of today.

    Beancount checks a `balance` directive at the start of its date, while
    Up's balance runs through the day. Stamping Up's current balance with
    today's date fails whenever something was spent earlier today; stamping
    it with tomorrow's fails when something is spent later today. So today's
    transactions are backed out instead, giving the balance the ledger must
    show at midnight.

    Returns:
        (today, opening balance)

    Raises:
        UpbankError: the transactions kept changing while the balance was read.
    """
    today, opening, _ = consistent_snapshot(client, today=today, account=account)
    return today, opening


def consistent_snapshot(
    client,
    since: datetime.date | None = None,
    today: datetime.date | None = None,
    account: str | None = None,
) -> tuple:
    """Today's opening balance, and the transactions it was computed from.

    Like `start_of_day_assertion`, but the transactions reach back to the start
    of `since` (default: today) and are returned too, so a caller sees exactly
    the state of Up that the balance reflects.

    Returns:
        (today, opening balance, transactions created since `since`)

    Raises:
        UpbankError: the transactions kept changing while the balance was read.
    """
    if today is None:
        today = sydney_today()
    if since is None:
        since = today
    start = datetime.datetime.combine(since, datetime.time(), tzinfo=UP_TZ)
    # The balance and the transactions are separate requests. Pulling the
    # transactions on both sides of the balance read, and retrying until the
    # two pulls agree, ensures nothing landed in between that the balance
    # includes but the subtraction misses (or vice versa).
    account_id, _ = live_balance(client, account)
    for _ in range(3):
        pulled = client.transactions(start, account_id=account_id)
        _, current = live_balance(client, account)
        if _fingerprint(
            client.transactions(start, account_id=account_id)
        ) == _fingerprint(pulled):
            break
    else:
        raise UpbankError(
            "Up transactions kept changing while the balance was read; try again."
        )
    return today, start_of_day_balance(current, pulled, today), pulled
