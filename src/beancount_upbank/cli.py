"""The ``upbank`` command line."""

import datetime
import json
import os
import pprint
from dataclasses import dataclass
from decimal import Decimal
from functools import cached_property
from typing import Any

import click
from beancount.core import account as beancount_account
from click.core import ParameterSource

from . import reconcile_cli
from .client import (
    HELD,
    Resource,
    UpbankClient,
    UpbankError,
    live_balance,
    opening_balance_and_transactions,
)
from .reconcile import ReconcileError, format_balance


class _Group(click.Group):
    """Report our own errors as clean command-line errors, not tracebacks."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except (UpbankError, ReconcileError) as e:
            raise click.ClickException(str(e)) from e


ENV_PREFIX = "env:"
UP_PREFIX = "up:"


def _resolve_token(value: str, origin: str) -> str:
    """Return the UP token, following a single ``env:NAME`` reference.

    ``origin`` says where ``value`` came from (``--token`` or ``UPBANK_TOKEN``)
    so a warning can suggest the right fix. Warns, without echoing the secret,
    when the result does not look like an Up token, which begins with ``up:``.
    """
    name = None
    if value.startswith(ENV_PREFIX):
        name = value[len(ENV_PREFIX) :]
        if not name:
            raise click.UsageError(
                f"{origin} '{ENV_PREFIX}' names no environment variable; "
                f"use {ENV_PREFIX}NAME.",
                click.get_current_context(silent=True),
            )
        value = os.environ.get(name, "")
        if not value:
            raise click.UsageError(
                f"Environment variable {name} is unset or empty.",
                click.get_current_context(silent=True),
            )
        if value.startswith(ENV_PREFIX):
            raise click.UsageError(
                f"Environment variable {name} holds another '{ENV_PREFIX}' "
                "reference; only one level is followed.",
                click.get_current_context(silent=True),
            )
    if not value.startswith(UP_PREFIX):
        if name:
            warning = f'{name} does not start with "{UP_PREFIX}"'
        else:
            fix = (
                f"--token {ENV_PREFIX}NAME"
                if origin == "--token"
                else f"{origin}={ENV_PREFIX}NAME"
            )
            warning = f'{origin} does not start with "{UP_PREFIX}"; did you mean {fix}?'
        click.echo(f"Warning: {warning}", err=True)
    return value


@dataclass
class _Session:
    token: str | None
    # Where the token came from, "--token" or "UPBANK_TOKEN", for messages.
    token_origin: str
    # Which of the token's Up accounts to use: an id or a display name; or None.
    account: str | None

    @cached_property
    def client(self) -> UpbankClient:
        """Build the client on first use, so `--help` never needs a token."""
        if not self.token:
            raise click.UsageError(
                "No Upbank token supplied. Set the UPBANK_TOKEN environment "
                "variable (preferred, keeps the secret off the command line) "
                "or pass --token.",
                click.get_current_context(silent=True),
            )
        return UpbankClient(_resolve_token(self.token, self.token_origin))


@click.group(cls=_Group)
@click.option(
    "--token",
    envvar="UPBANK_TOKEN",
    help="Upbank personal access token, or env:NAME to read it from environment "
    "variable NAME. Prefer the UPBANK_TOKEN environment variable (which may "
    "itself be env:NAME) so the secret never appears on the command line or "
    "in logs.",
)
@click.option(
    "--up-account",
    envvar="UPBANK_ACCOUNT",
    default=None,
    help="Which Up account to use, by id or display name (see `upbank "
    "accounts`). Defaults to the first account Up lists. This selects an "
    "account at Up; it is not a beancount account name.",
)
@click.pass_context
def cli(ctx: click.Context, token: str | None, up_account: str | None) -> None:
    source = ctx.get_parameter_source("token")
    origin = "UPBANK_TOKEN" if source == ParameterSource.ENVIRONMENT else "--token"
    ctx.obj = _Session(token, origin, up_account)


@cli.command()
@click.pass_obj
def ping(session: _Session) -> None:
    """Send a ping to Upbank, to verify your token and their API status."""
    response = session.client.ping()
    click.echo("Ping!")
    click.echo(response.text)


@cli.command()
@click.pass_obj
def categories(session: _Session) -> None:
    """Get a list of transaction categories."""
    click.echo(pprint.pformat(session.client.categories()))


@cli.command()
@click.pass_obj
def accounts(session: _Session) -> None:
    """List the token's Up accounts: id, type, name and balance."""
    for acct in session.client.accounts():
        attributes = acct["attributes"]
        click.echo(
            f"{acct['id']}  {attributes.get('accountType', '?'):13} "
            f"{attributes.get('displayName', '')}  {attributes['balance']['value']} AUD"
        )


def _beancount_account(
    ctx: click.Context, param: click.Parameter, value: str | None
) -> str | None:
    if value is not None and not beancount_account.is_valid(value):
        raise click.BadParameter(
            f"{value!r} is not a beancount account name, e.g. Assets:Bank:Up."
        )
    return value


@cli.command()
@click.argument("account", required=False, callback=_beancount_account)
@click.pass_obj
def balance(session: _Session, account: str | None) -> None:
    """Show the balance now, or a beancount balance directive for ACCOUNT.

    With no ACCOUNT, prints Up's balance right now, e.g. `145.00 AUD`.

    With ACCOUNT, a beancount account name, prints a complete `balance`
    directive dated today. Beancount checks a `balance` directive at the
    start of its date, while Up's balance runs through the day, so today's
    transactions are backed out, giving the balance the ledger must show at
    midnight. That midnight is the start of today in Sydney, whatever your
    timezone.
    """
    if account is None:
        _, amount = live_balance(session.client, session.account)
        click.echo(f"{amount} AUD")
        return
    today, amount, _ = opening_balance_and_transactions(
        session.client, account=session.account
    )
    click.echo(format_balance(today, account, amount))


@cli.command()
@click.argument("account", callback=_beancount_account)
@click.argument("ledger", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--pull",
    type=click.Path(exists=True, dir_okay=False),
    help="Use this file saved by `upbank pull` instead of asking Up.",
)
@click.option("--dry-run", is_flag=True, help="Report the edits without making them.")
@click.pass_obj
def reconcile(
    session: _Session, account: str, ledger: str, pull: str | None, dry_run: bool
) -> None:
    """Settle ACCOUNT's held (up_hold) entries in LEDGER.

    LEDGER is your main ledger; the entries are edited in whichever included
    file holds them. ACCOUNT is the beancount account the Up transactions post
    to.

    By default Up is asked now: the transactions back to the oldest hold,
    and the balance, read together. Today's balance directive is written from
    that balance (replacing one already dated today), and the edits are checked
    against it.

    With --pull FILE, a file saved by `upbank pull`, nothing is fetched and no
    directive is written; the edits are checked against the newest balance
    directive already in the ledger, so append today's (`upbank balance
    ACCOUNT`) first.

    If any balance directive fails after the edits, every file is put back
    exactly as it was.

    The Up account is the first one Up lists. If your token sees several (a
    saver, say), name the one ACCOUNT tracks before the command, or set
    UPBANK_ACCOUNT:

    \b
        upbank --up-account NAME reconcile ACCOUNT LEDGER
    """

    def snapshot(
        since: datetime.date,
    ) -> tuple[datetime.date, Decimal, list[Resource]]:
        return opening_balance_and_transactions(
            session.client, since=since, account=session.account
        )

    reconcile_cli.run(account, ledger, pull, dry_run, snapshot)


def _account_id(session: _Session) -> str | None:
    """The id of the selected Up account; or None if none was selected."""
    if session.account is None:
        return None
    return live_balance(session.client, session.account)[0]


@cli.command()
@click.argument("year", type=click.types.INT)
@click.argument("month", type=click.types.INT)
@click.pass_obj
def month(session: _Session, year: int, month: int) -> None:
    """Download a sequence of transactions."""
    transactions = session.client.get_month(
        year, month, account_id=_account_id(session)
    )
    click.echo(json.dumps(transactions, indent=3))


@cli.command()
@click.argument("days", type=click.types.INT, default=60)
@click.pass_obj
def pull(session: _Session, days: int) -> None:
    """Download a sequence of transactions.

    Only the account whose balance `balance` reports, so a saver
    added later cannot leak into that account's ledger.
    """
    account_id, _ = live_balance(session.client, session.account)
    transactions = session.client.get_recent(days, account_id=account_id)
    click.echo(json.dumps(transactions, indent=3))


@cli.command()
@click.argument("days", type=click.types.INT, default=60)
@click.pass_obj
def held(session: _Session, days: int) -> None:
    """Download held transactions."""
    transactions = session.client.get_recent(
        days, account_id=_account_id(session), status=HELD
    )
    click.echo(json.dumps(transactions, indent=3))


if __name__ == "__main__":
    cli()
