# Changelog

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
