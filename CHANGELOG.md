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
