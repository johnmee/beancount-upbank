# Changelog

## Unreleased

- `--token env:NAME` (or `UPBANK_TOKEN=env:NAME`) reads the token from
  environment variable `NAME`, so several tokens can live in the environment.
- `upbank` warns when a token does not start with `up:`.
- `upbank <command> --help` works without a usable token.
- Requires click 8.2 or newer.
- A token that can see no Up accounts is reported as an error, not a traceback.
- **Breaking:** `upbank assertion` is gone. `upbank balance` prints only the
  live amount, e.g. `145.00 AUD`; `upbank balance ACCOUNT` takes a beancount
  account name and prints a complete `balance` directive dated today (Sydney)
  with today's transactions backed out, e.g.
  `2026-11-06 balance Assets:Bank:Up          99.99 AUD`.
- **Breaking:** in `beancount_upbank.client`, `start_of_day_balance` is now
  `opening_balance_from`, and `start_of_day_assertion` is replaced by
  `opening_balance_and_transactions`, which also returns the transactions the
  balance was computed from: `today, balance, _ = ...`.
- `upbank pull N` (`UpbankClient.get_recent`) starts at midnight Sydney time
  N days ago, so its first day is whole. `upbank reconcile --pull` relies on
  that; a file pulled by an earlier version can make a hold from its first day
  look released.
- Reading today's opening balance is retried if Sydney midnight passes during
  the read, rather than dated the wrong day.
- `upbank reconcile`:
  - leaves a hold that looks released alone when no balance directive dated
    after it could confirm the release, rather than acting unchecked;
  - refuses newest balance directives that share a date across files, rather
    than guessing which is newest;
  - refuses an amount it cannot rewrite safely (an expression such as
    `-15.00 * 2 AUD`, or a tolerance), rather than corrupting it;
  - refuses a ledger file saved while it runs, and a rollback restores only
    the files it wrote;
  - tolerates plugin errors that carry no entry;
  - says "balance directive" where it said "balance assertion".
- Type annotations throughout, checked by `mypy --strict` in CI.
  `UpbankImporter.extract` is typed as returning transactions.

## 0.1.0

First release, extracted from `aussie-bean-tools`.

- `UpbankImporter`, a [beangulp](https://github.com/beancount/beangulp) importer
  for the JSON that `upbank recent` downloads.
- `upbank`: `recent`, `month`, `held`, `accounts`, `balance`, `assertion`,
  `categories` and `ping`.
- `upbank-reconcile`: settles, corrects or comments out entries that were still
  pending when they were imported.
- Choose between several Up accounts with `--up-account` and the importer's
  `account_id`.
- Requests time out, and rate limits, server errors and outages are retried.
