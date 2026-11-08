# beancount-upbank

[![PyPI](https://img.shields.io/pypi/v/beancount-upbank)](https://pypi.org/project/beancount-upbank/)

Import [Up Bank](https://up.com.au/) (Australia) transactions into
[Beancount](https://beancount.github.io/).

- `upbank` connects to the Up Bank API and downloads your transactions as JSON.
- `UpbankImporter` is a [beangulp](https://github.com/beancount/beangulp)
  importer that converts the JSON download into plain-text Beancount entries.
- `upbank reconcile` updates transactions that were **held** when you
  imported them, once Up Bank settles or releases them, and writes today's
  `balance` directive.

This project is not affiliated with or endorsed by Up Bank or Bendigo and
Adelaide Bank. It reads your data with a personal access token you create
yourself; it cannot and does not move money.

## Install

1. Install the library:

   ```
   pip install beancount-upbank
   ```

   It needs Python 3.10 or newer, and Beancount 3.

2. Create your personal access token at <https://api.up.com.au/>.

3. Save it to your shell environment:

   ```
   export UPBANK_TOKEN=up:yeah:...
   ```

   Perhaps add it to your `.profile`, or use [direnv](https://direnv.net/).

4. Test your token by pinging the Up Bank API:

   ```
   ❯  upbank ping
   Ping!
   {"meta":{"id":"123456abcd-8888-8888-8888-789efgh","statusEmoji":"⚡️"}}
   ```

5. Create the importer script by saving this to `up-import.py`, replacing
   `Assets:Bank:Up` with your account's name:

   ```python
   from beangulp import Ingest
   from beancount_upbank import UpbankImporter

   Ingest([UpbankImporter("Assets:Bank:Up")])()
   ```

## Usage

1. Download recent transactions:

   ```
   upbank pull > /tmp/up.json
   ```

   This fetches the last 60 days of Up Bank transactions and saves them to a
   file in Up Bank's JSON format.

2. Run the beangulp importer script you saved earlier:

   ```
   python up-import.py extract -e main.beancount /tmp/up.json
   ```

   `-e` points to your existing ledger so it can avoid creating duplicate
   transactions.

   Paste or append the output to your ledger. Each entry has one posting, to
   the Up Bank account. You complete the other half of the transaction
   manually.

   Or use [`beancount-predict-postings`](https://github.com/johnmee/beancount-predict-postings)
   to fill in the cross-postings based on previous transactions, then review
   them manually.

3. Reconcile held transactions and assert the balance:

   ```
   upbank reconcile Assets:Bank:Up main.beancount
   ```

   This resolves the Up Bank transactions that have settled or been released,
   and writes a balance directive.

   - `Assets:Bank:Up`: the Beancount account the Up Bank transactions post to.
   - `main.beancount`: your main ledger. Any files it `include`s are covered
     too: each edit lands in whichever file holds the entry.

## What reconcile does and why

**The `up_hold` metadata**

Up Bank handles payments in two phases. First the money is *held*: deducted
from your available balance, but not yet a settled transaction.

Days or weeks later the hold is *settled*, possibly at a different amount (a
restaurant tip, a foreign currency conversion).

Or the hold is *released*: the money returns to your available balance and the
transaction disappears.

To handle this, the beangulp importer records each held transaction's Up Bank
transaction id as `up_hold` metadata:

```
2026-11-05 * "Coles" "COLES 0123, SYDNEY"
  up_hold: "c5555555-5555-5555-5555-555555555555"
  Assets:Bank:Up  -22.95 AUD
```

By design, a beangulp importer can only add new entries; it cannot update
historical transactions to remove the hold, or the transaction.


**`upbank reconcile`**

`upbank reconcile` is a follow-up pass to update held transactions.

Using the id in the `up_hold` line, reconcile looks up each held transaction's
status at Up Bank and reacts to it: it removes the `up_hold` line once the
transaction settles, correcting the amount if it changed, or comments the
transaction out if the hold was released.

A fully settled ledger has no `up_hold` lines at all.

**Why bother tracking holds?**

Because when transactions change amount or disappear, they break your balance
assertions. Tracking held transactions saves you from working out where and
why your historical balance assertions began failing after the last import.

**The report**

After it runs, reconcile summarises the holds:

```
  2026-10-29  Coles                              -32.72 AUD   settled as held
  2026-10-30  Example Brewing Co.                -30.00 -> -31.25 AUD   settled 2026-11-01
  2026-10-31  Hotel Example                      -200.00 AUD   released, never settled

2 still unsettled:
   2026-11-05  Silo West                          -5.90 AUD  0d
   2026-11-05  Coles                              -22.95 AUD  0d

Applied. Verified against Up's balance (308.18 AUD).
```

## Balance assertions

Up Bank reports the balance of your account at the moment you ask, assuming
every held transaction will settle.

Beancount checks the balance of your account at the start of the day
(midnight), before any transactions dated that day.

So `upbank balance ACCOUNT` fetches the current balance and today's
transactions from Up Bank, then backs those transactions out to give the
start-of-day balance that Beancount expects:

```
❯  upbank balance Assets:Bank:Up
2026-11-08 balance Assets:Bank:Up         381.76 AUD
```

Without an account, `upbank balance` prints the available balance right now:

```
❯  upbank balance
359.26 AUD
```

## More than one account

If your token can see more than one Up Bank account (say a spending account
and a saver), `upbank accounts` lists them. Choose one with `--up-account` (or
`UPBANK_ACCOUNT`), and give the importer the same account's id, so another
account's transactions are left out of its ledger:

```python
UpbankImporter("Assets:Bank:Up", account_id="<id from `upbank accounts`>")
```

## More than one token

If you have two tokens (say yours and a partner's), keep both in your
environment and name the one to use with `env:NAME`, which reads the token from
that variable. The secret itself still never appears on the command line:

```
export UPBANK_TOKEN_A=up:yeah:...
export UPBANK_TOKEN_B=up:yeah:...

upbank --token env:UPBANK_TOKEN_B pull 30
UPBANK_TOKEN=env:UPBANK_TOKEN_B upbank pull 30    # same thing
```

Only one level is followed: a variable named by `env:` must hold the token
itself. Up Bank tokens start with `up:`; `upbank` warns (without printing the
value) when a token does not, which usually means a variable name was passed
without `env:`.

## Importer customisation

```python
UpbankImporter(
    "Assets:Bank:Up",   # The Beancount name for your Up Bank account
    account_id=None,    # Up Bank's id for the account; see "More than one account"
    tags=...,           # a set of tags to apply to every transaction
)
```

Set `FLAG` on a subclass to change the flag entries are filed with (the default
is `*`).

Up Bank identifies every transaction, so the importer recognises one it has
seen before even if the amount has changed since. Duplicates are decided by
Up Bank's transaction id where there is one, and otherwise by exactly equal
amounts on the same day. A percentage tolerance would merge, say, $1000.00 and
$1000.50.

## `upbank`

```
Usage: upbank [OPTIONS] COMMAND [ARGS]...

Options:
  --token TEXT       Upbank personal access token, or env:NAME to read it from
                     environment variable NAME. Prefer the UPBANK_TOKEN
                     environment variable (which may itself be env:NAME) so
                     the secret never appears on the command line or in logs.
  --up-account TEXT  Which Up account to use, by id or display name (see
                     `upbank accounts`). Defaults to the first account Up
                     lists. This selects an account at Up; it is not a
                     beancount account name.
  --help             Show this message and exit.

Commands:
  accounts    List the token's Up accounts: id, type, name and balance.
  balance     Show the balance now, or a beancount balance directive for...
  categories  Get a list of transaction categories.
  held        Download held transactions.
  month       Download a sequence of transactions.
  ping        Send a ping to Upbank, to verify your token and their API...
  pull        Download a sequence of transactions.
  reconcile   Settle ACCOUNT's held (up_hold) entries in LEDGER.
```

Requests to Up Bank time out after 30 seconds. A rate limit (HTTP 429,
honouring `Retry-After`), a server error or a failure to connect is retried up
to four times, backing off between tries, before it is reported as an error.

## Use from Python

```python
from beancount_upbank import UpbankClient

client = UpbankClient(token)
transactions = client.get_recent(30)  # the last 30 days, as Up Bank's JSON
accounts = client.accounts()
```

Failures raise `beancount_upbank.UpbankError`.

## Development

```
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pyright
```

The tests never reach the network.

## Licence

MIT. See [LICENSE](https://github.com/johnmee/beancount-upbank/blob/main/LICENSE).
