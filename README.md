# beancount-upbank

Import [Up](https://up.com.au/) (Australia) transactions into
[Beancount](https://beancount.github.io/).

- `upbank` connects to the Up API and downloads your transactions as json.
- `UpbankImporter` is a [beangulp](https://github.com/beancount/beangulp)
  importer that converts the json download into plain text Beancount entries.
- `upbank balance --midnight` prints the balance a start-of-day `balance`
  directive needs, with **pending** transactions included.
- `upbank-reconcile` clears transactions that were **pending** when you
  imported them, once Up settles or releases them.

This project is not affiliated with or endorsed by Up or Bendigo and Adelaide
Bank. It reads your data with a personal access token you create yourself; it
cannot and does not move money.

## Install

```
pip install beancount-upbank
```

Python 3.10 or newer, and Beancount 3.

## Quick start

1. Create a personal access token at <https://api.up.com.au/>, and put it in
   your environment so it never appears on a command line:

   ```
   export UPBANK_TOKEN=up:yeah:...
   ```

   Put it in your .profile. 
   Or, use [direnv](https://direnv.net/) for automagically loading, and unloading, environment variables per directory.

   Test your token by pinging the upbank API:

   ```
   ❯  upbank ping
   Ping!
   {"meta":{"id":"123456abcd-8888-8888-8888-789efgh","statusEmoji":"⚡️"}}
   ```

2. Download recent transactions:

   Download the last 30 days of UP transactions and save them at `/tmp/up.json`:

   ```
   upbank recent 30 > /tmp/up.json
   ```

3. Create an importer script, `import.py`:

   ```python
   from beangulp import Ingest
   from beancount_upbank import UpbankImporter

   Ingest([UpbankImporter("Assets:Bank:Up")])()
   ```

4. Convert the downloaded json transactions to Beancount. `-e` points at your ledger so
   that transactions you already have can be identified as duplicates:

   ```
   python import.py extract -e main.beancount /tmp/up.json > /tmp/up.beancount
   ```

   Each entry has one posting, to the Up account. Add the other side yourself,
   or let [`beancount-fuzzyfill`](https://github.com/johnmee/beancount-fuzzyfill)
   guess it from your past transactions.

## The importer

```python
UpbankImporter(
    "Assets:Bank:Up",
    tags=...,  # a set of tags applied to every transaction
    account_id=None,  # Up's id for the account; see "More than one account"
    currency="AUD",
)
```

Set `FLAG` on a subclass to change the flag entries are filed with (the default
is `*`).

Up identifies every transaction, so the importer recognises one it has seen
before even if the amount has changed since. Duplicates are decided by Up's
transaction id where there is one, and otherwise by exactly equal amounts on the
same day. A percentage tolerance would merge, say, $1000.00 and $1000.50.

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
  balance     Show the balance, now or at last midnight.
  categories  Get a list of transaction categories.
  held        Download held transactions.
  month       Download a sequence of transactions.
  ping        Send a ping to Upbank, to verify your token and their API...
  recent      Download a sequence of transactions.
```

Requests to Up time out after 30 seconds. A rate limit (HTTP 429, honouring
`Retry-After`), a server error or a failure to connect is retried up to four
times, backing off between tries, before it is reported as an error.

### Balance assertions

Beancount checks a `balance` directive at the **start** of its date, but Up
reports the balance **now**, which already includes everything spent today.
Stamping Up's figure with today's date fails whenever something was spent
earlier today; stamping it with tomorrow's fails when something is spent later.
`upbank balance --midnight` backs today's transactions out, giving the balance
your ledger must show at midnight (Sydney time):

```
$ upbank balance --midnight
200.00 AUD
```

It prints only the amount, so compose the directive yourself, with the date
pinned to Sydney:

```
$ amount=$(upbank balance --midnight) &&
  echo "$(TZ=Australia/Sydney date +%F) balance Assets:Bank:Up $amount" >> up-2026.beancount
```

The `&&` matters: if `upbank` fails, nothing is appended, rather than a
`balance` line with no amount.

### More than one account

If your token can see more than one Up account (say a spending account and a
saver), `upbank accounts` lists them. Choose one with `--up-account` (or
`UPBANK_ACCOUNT`), and give the importer the same account's id, so another
account's transactions are left out of its ledger:

```python
UpbankImporter("Assets:Bank:Up", account_id="<id from `upbank accounts`>")
```

### More than one token

If you have two tokens (say yours and a partner's), keep both in your
environment and name the one to use with `env:NAME`, which reads the token from
that variable. The secret itself still never appears on the command line:

```
export UPBANK_TOKEN_A=up:yeah:...
export UPBANK_TOKEN_B=up:yeah:...

upbank --token env:UPBANK_TOKEN_B recent 30
UPBANK_TOKEN=env:UPBANK_TOKEN_B upbank recent 30    # same thing
```

Only one level is followed: a variable named by `env:` must hold the token
itself. Up tokens start with `up:`; `upbank` warns (without printing the value)
when a token does not, which usually means a variable name was passed without
`env:`.

## Pending transactions and `upbank-reconcile`

Up's balance is the *available* balance: money on hold is already deducted. So a
pending (`HELD`) transaction has to be in your ledger for your `balance`
directive to reconcile, and the importer writes it with an `up_hold` metadata
line to mark it provisional:

```
2026-09-01 * "Example Brewing Co." "EXAMPLE BREW, SYDNEY"
  up_hold: "0c2b1d6e-..."
  Assets:Bank:Up  -30.00 AUD
```

Such an entry is not final. Up may settle it at a **different** amount (a
restaurant tip, a foreign currency conversion), or release it entirely, in which
case it simply disappears from the API: `TransactionStatusEnum` is
`HELD|SETTLED` with no "released" state and no tombstone. The importer only
appends, so it cannot see either outcome.

`upbank-reconcile` looks up each marked entry in a fresh download and finishes
it off:

| in the download | action |
|---|---|
| settled, same amount | remove the `up_hold` line |
| settled, different amount | correct the amount, remove the `up_hold` line |
| absent | comment the entry out; the hold was released |
| still `HELD` | leave it and report it, loudly once it is over a week old |
| outside the download's window | leave it and report it; absence proves nothing |

Entries are edited **in place**, so hand-written `;` annotations, narration and
position survive. Correcting one also shifts every `balance` directive that was
snapshotted while the hold was live.

```
$ upbank-reconcile up-2026.beancount /tmp/up.json --account Assets:Bank:Up --fix
  2026-09-01  Example Brewing Co.   -30.00 -> -31.25 AUD   settled 2026-09-03

  balance directive line 14 -> 968.75 AUD (that snapshot included the hold)

Applied. Verified against the untouched 2026-09-06 assertion (968.75 AUD).
```

Run it **last**, after you have appended this run's `balance --midnight`
assertion. That assertion is the only one known to postdate a release, so it is
the evidence the run checks itself against. If any assertion fails afterwards, a
`--fix` run puts the ledger back exactly as it found it and exits non-zero; no
backup file is written. Without `--fix` it only reports.

```
Usage: upbank-reconcile [OPTIONS] LEDGER PULL

  Promote settled holds in LEDGER using a fresh Up PULL (json).

Options:
  --account TEXT     Beancount account the Up transactions post to.
                     [required]
  --root FILE        Top-level ledger to load for balance semantics. Defaults
                     to LEDGER, which is right when your ledger is one file.
                     If LEDGER is `include`d by a main ledger, name that here:
                     a sub-ledger loaded on its own has no opening balance, so
                     every assertion in it would appear to fail.
  --fix / --dry-run  Apply the edits. The default only reports them.
  --help             Show this message and exit.
```

Two cases are reported but never edited, because neither can be settled safely
from one account's file:

- **A leg of a transfer between your own accounts.** The other leg moves with
  it, and its balance directives live in a file this run does not hold. Any
  entry that also posts to another `Assets` or `Liabilities` account is treated
  as a transfer, which errs towards leaving it for you.
- **A hold older than the download's window.** It was never fetched, so its
  absence means nothing. The run instead checks whether the gap between Up's
  balance and your ledger's matches the held amount exactly, which is the only
  evidence left once the API has forgotten the hold.

## Use from Python

```python
from beancount_upbank import UpbankClient

client = UpbankClient(token)
transactions = client.get_recent(30)  # the last 30 days, as Up's JSON
accounts = client.accounts()
```

Failures raise `beancount_upbank.UpbankError`.

## Development

```
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The tests never reach the network.

## Licence

MIT. See [LICENSE](LICENSE).
