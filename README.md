# beancount-upbank

[![PyPI](https://img.shields.io/pypi/v/beancount-upbank)](https://pypi.org/project/beancount-upbank/)

Import [Up Bank](https://up.com.au/) (Australia) transactions into
[Beancount](https://beancount.github.io/).

- `upbank` connects to the Up API and downloads your transactions as json.
- `UpbankImporter` is a [beangulp](https://github.com/beancount/beangulp)
  importer that converts the json download into plain text Beancount entries.
- `upbank reconcile` clears transactions that were **pending** when you
  imported them, once Up settles or releases them, and writes today's
  `balance` directive.

This project is not affiliated with or endorsed by Up or Bendigo and Adelaide
Bank. It reads your data with a personal access token you create yourself; it
cannot and does not move money.

## Install

1. Install the library.

```
pip install beancount-upbank
```

Python 3.10 or newer, and Beancount 3.

2. Create your personal access token at <https://api.up.com.au/>. 

3. Save it to your shell environment:

   ```
   export UPBANK_TOKEN=up:yeah:...
   ```

   Perhaps add it in your .profile, or use [direnv](https://direnv.net/).

4. Test your token by pinging the upbank API:

   ```
   ❯  upbank ping
   Ping!
   {"meta":{"id":"123456abcd-8888-8888-8888-789efgh","statusEmoji":"⚡️"}}
   ```

5. Create the importer script by saving this to file `up-import.py` with the name of your bank account.

   ```python
   from beangulp import Ingest
   from beancount_upbank import UpbankImporter

   Ingest([UpbankImporter("Assets:Bank:Up")])()
   ```

## Usage

1. Download recent transactions:

   ```
   upbank pull
   ```
   Outputs the last 60 days of UP transactions in their json format.

   ```
   upbank pull 7 > /tmp/up.json
   ```
   Saves the last 7 days of json UP transactions to the file.

2. Run the beangulp importer:

   ```
   python up-import.py extract -e main.beancount --pull /tmp/up.json > /tmp/up.beancount
   ```

   `-e` points to your ledger so it can avoid duplicating transactions.

   Each entry generates one posting, to the Up account.  You complete the other half of the transaction manually.

   Or, I use [`beancount-predict-postings`](https://github.com/johnmee/beancount-predict-postings)
   to fill the cross-postings based on my previous transactions, then review them manually.

3. Reconcile the held transactions and assert the balance

   ```
   upbank reconcile Assets:Bank:Up main.beancount
   ```
   Settles the on-hold Up transactions and adds a balance directive.

   - `Assets:Bank:Up`: the beancount account the Up transactions post to.
   - `main.beancount`: your main ledger. Any files it `include`s are covered
     too: each edit lands in whichever file holds the entry.

   It asks Up for the current balance and for transactions back to the oldest
   held entry. It then settles the held entries (see below), writes today's 
   `balance` directive, and checks that every existing `balance` directive 
   still holds. If one doesn't, it puts every file back exactly as it was and 
   exits non-zero.

   Today's directive goes at the end of the file holding the account's newest
   transaction or `balance` directive, so it follows your import into a new
   year's file. Transfers recorded in another account's file don't count. Run
   it twice in a day and today's directive is replaced, not duplicated.

   Add `--dry-run` to see what it would change without changing anything:

   ```
   upbank reconcile Assets:Bank:Up main.beancount --dry-run
   ```

   To work from the download in step 1 instead of asking Up again, pass it
   with `--pull`. Nothing is fetched and no directive is written, so append
   today's first, with `upbank balance`, which prints the directive dated
   today with the balance at the start of today (midnight, Sydney time):

   ```
   upbank balance Assets:Bank:Up >> up-2026.beancount
   upbank reconcile Assets:Bank:Up main.beancount --pull /tmp/up.json
   ```

   Without an account, `upbank balance` prints the available balance right
   now, e.g. `359.26 AUD`.

   **Why: the `up_hold` metadata**

   Up handles a card payment in two phases. First the money is *held*:
   deducted from your available balance, but not final. Days or weeks later
   the transaction *settles*, possibly at a different amount (a restaurant
   tip, a foreign currency conversion). Or the hold is *released*: the money
   comes back and the transaction disappears from the API without a trace.

   Up's balance is the available balance, with holds already deducted, so
   held transactions have to be in your ledger for its `balance` directives to
   pass. The importer writes them, and marks each one with Up's transaction
   id:

   ```
   2026-11-05 * "Coles" "COLES 0123, SYDNEY"
     up_hold: "c55555-5555-5555-5555-26c18559c321"
     Assets:Bank:Up  -22.95 AUD
   ```

   `up_hold` means "provisional": this amount may still change or vanish.
   The importer only ever appends, so it cannot fix the entry later.
   `upbank reconcile` finds each marked entry at Up and finishes it off: it
   removes the `up_hold` line, corrects the amount, or comments the entry
   out, editing it in place so your own postings and comments survive. A
   fully settled ledger has no `up_hold` lines left.

   **Reading the output**

   ```
     2026-10-29  Coles                              -32.72 AUD   settled as held
     2026-10-30  Example Brewing Co.                -30.00 -> -31.25 AUD   settled 2026-11-01
     2026-10-31  Hotel Example                      -200.00 AUD   released, never settled

   2 still unsettled:
      2026-11-05  Silo West                          -5.90 AUD  0d
      2026-11-05  Coles                              -22.95 AUD  0d

     up-2026.beancount:214: balance directive -> 968.75 AUD (that snapshot included the hold)
     up-2026.beancount: append  2026-11-06 balance Assets:Bank:Up         308.18 AUD

   Applied. Verified against Up's balance (308.18 AUD).
   ```

   Nothing to worry about:

   - `settled as held`: the amount was right; the `up_hold` line is removed.
   - `-30.00 -> -31.25 AUD settled ...`: it settled at a different amount;
     the posting is corrected. Check the other posting if you split it.
   - `released, never settled`: the hold was cancelled; the entry is
     commented out.
   - `FILE:LINE: balance directive -> ...`: an earlier `balance` directive
     was taken while that hold was deducted, so it moves by the same amount.
   - `N still unsettled` with a small age (`0d`, `2d`): normal. They stay
     marked, and the next run picks them up.
   - `FILE: append ...` or `FILE:LINE: replace with ...`: today's `balance`
     directive.
   - `N line edit(s) pending; re-run without --dry-run.`: nothing was changed.
   - `Applied. Verified against ...`: done, and the ledger balances.
   - `No provisional (up_hold) entries in the ledger.`: nothing to do (only
     with a download file; otherwise today's directive is still written).
   - `Nothing to change.`: the ledger already matches Up.

   Lines marked `!!` need you:

   - `-- unusually old, Up may have dropped it`: Up has reported it held for
     more than a week. Check it in the Up app.
   - `-- older than the pull window, cannot be checked`: only with a download
     file. The hold is older than the download, so its absence proves
     nothing. Run without the file, or pull more days (`upbank pull 90`).
   - `transfer between own accounts -- not touched, fix by hand`: the entry
     also posts to another of your `Assets` or `Liabilities` accounts, whose
     balances this run does not adjust. Edit both sides yourself.
   - `no balance directive after ... can confirm the release, so it is left
     alone`: only with a download file. The hold looks released, but no later
     balance directive would catch it if it were not. Append one with
     `upbank balance ACCOUNT >> FILE` and run again.
   - `Up reports X AUD ..., but the ledger accumulates Y AUD -- a gap`:
     followed by either the holds that exactly account for the gap (Up has
     released them; comment them out and adjust the `balance` directives
     between), or `No combination of pending holds accounts for it`: the gap
     is something else, such as a missing or mistyped transaction.
   - `Error: Rolled back -- these balance directives do not hold`: every file
     was left exactly as it was. Fix the balance directives listed, usually
     with the help of the gap message above it, and run again.
   - `Error: main.beancount never opens Assets:Bank:Up`: you named an
     included file, or misspelt the account. Name the main ledger.
   - `Error: ... has 2 balance directives dated ...` or `... in more than one
     file`: delete all but one of the lines listed and run again.

   See [Pending transactions and `upbank reconcile`](https://github.com/johnmee/beancount-upbank#pending-transactions-and-upbank-reconcile)
   for the full rules.

## importer customisation

```python
UpbankImporter(
    "Assets:Bank:Up",   # The beancount name for your UP account
    account_id=None,    # Up's id for the account; see "More than one account"
    tags=...,           # a set of tags to apply to every transaction
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
  balance     Show the balance now, or a beancount balance directive for...
  categories  Get a list of transaction categories.
  held        Download held transactions.
  month       Download a sequence of transactions.
  ping        Send a ping to Upbank, to verify your token and their API...
  pull        Download a sequence of transactions.
  reconcile   Settle ACCOUNT's held (up_hold) entries in LEDGER.
```

Requests to Up time out after 30 seconds. A rate limit (HTTP 429, honouring
`Retry-After`), a server error or a failure to connect is retried up to four
times, backing off between tries, before it is reported as an error.

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

upbank --token env:UPBANK_TOKEN_B pull 30
UPBANK_TOKEN=env:UPBANK_TOKEN_B upbank pull 30    # same thing
```

Only one level is followed: a variable named by `env:` must hold the token
itself. Up tokens start with `up:`; `upbank` warns (without printing the value)
when a token does not, which usually means a variable name was passed without
`env:`.

## Pending transactions and `upbank reconcile`

`upbank reconcile` looks up each `up_hold` entry at Up and finishes it off:

| at Up | action |
|---|---|
| settled, same amount | remove the `up_hold` line |
| settled, different amount | correct the amount, remove the `up_hold` line |
| absent | comment the entry out; the hold was released |
| still `HELD` | leave it and report it, loudly once it is over a week old |
| older than the download (file only) | leave it and report it; absence proves nothing |

Up keeps no record of a release: `TransactionStatusEnum` is `HELD|SETTLED`,
with no "released" state and no tombstone. Absence is therefore only a
hypothesis, and the run proves it by checking afterwards that every `balance`
directive for the account holds.

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
uv run mypy
uv run pyright
```

The tests never reach the network.

## Licence

MIT. See [LICENSE](https://github.com/johnmee/beancount-upbank/blob/main/LICENSE).
