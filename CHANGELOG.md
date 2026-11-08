# Changelog

## Unreleased

## 0.1.1

- `upbank reconcile` writes today's `balance` directive in front of the first
  entry dated today, not after it, so the file reads in the order Beancount
  checks it, and reports it as `FILE:LINE: insert`. A comment directly above
  that entry stays with it. With nothing dated today the directive still goes
  at the end of the file.
- The README is rewritten: how `up_hold` and reconcile work, how Beancount's
  start-of-day balance check relates to Up Bank's live balance, and examples
  of `upbank balance`.

## 0.1.0

First release, extracted from `aussie-bean-tools`.

- `UpbankImporter`, a [beangulp](https://github.com/beancount/beangulp) importer
  for the JSON that `upbank pull` downloads. Entries that are still HELD at Up
  carry an `up_hold` id, so they can be settled later.
- `upbank`: `pull`, `month`, `held`, `accounts`, `balance`, `reconcile`,
  `categories` and `ping`.
- `upbank balance` prints the live amount; `upbank balance ACCOUNT` prints a
  complete `balance` directive dated today (Sydney) with today's transactions
  backed out.
- `upbank reconcile` settles, corrects or comments out entries that were still
  held when they were imported, writes today's `balance` directive from Up, and
  verifies every balance directive before keeping its edits, rolling back
  otherwise.
- Choose between several Up accounts with `--up-account` and the importer's
  `account_id`.
- `--token env:NAME` (or `UPBANK_TOKEN=env:NAME`) reads the token from
  environment variable `NAME`, so several tokens can live in the environment.
- Requests time out, and rate limits, server errors and outages are retried.
- Type annotations throughout (`py.typed`), checked by `mypy --strict` and
  pyright in CI.
- Python 3.10 to 3.14.
