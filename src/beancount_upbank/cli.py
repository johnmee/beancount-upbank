"""The ``upbank`` command line."""

import json
import os
import pprint
from dataclasses import dataclass
from functools import cached_property

import click
from click.core import ParameterSource

from .client import (
    HELD,
    UpbankClient,
    UpbankError,
    live_balance,
    start_of_day_assertion,
)


class _Group(click.Group):
    """Report an UpbankError as a clean command-line error, not a traceback."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except UpbankError as e:
            raise click.ClickException(str(e)) from e


ENV_PREFIX = "env:"
UP_PREFIX = "up:"


def _resolve_token(value: str, origin: str) -> str:
    """Return the token, following a single ``env:NAME`` reference.

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
def cli(ctx, token, up_account):
    source = ctx.get_parameter_source("token")
    origin = "UPBANK_TOKEN" if source == ParameterSource.ENVIRONMENT else "--token"
    ctx.obj = _Session(token, origin, up_account)


@cli.command()
@click.pass_obj
def ping(session):
    """Send a ping to Upbank, to verify your token and their API status."""
    response = session.client.ping()
    click.echo("Ping!")
    click.echo(response.text)


@cli.command()
@click.pass_obj
def categories(session):
    """Get a list of transaction categories."""
    click.echo(pprint.pformat(session.client.categories()))


@cli.command()
@click.pass_obj
def accounts(session):
    """List the token's Up accounts: id, type, name and balance."""
    for acct in session.client.accounts():
        attributes = acct["attributes"]
        click.echo(
            f"{acct['id']}  {attributes.get('accountType', '?'):13} "
            f"{attributes.get('displayName', '')}  {attributes['balance']['value']} AUD"
        )


@cli.command()
@click.argument("label", type=click.types.STRING)
@click.pass_obj
def balance(session, label):
    """Show the account's balance right now, as the Up app does.

    LABEL is only used to caption the output.
    """
    _, current = live_balance(session.client, session.account)
    click.echo(f"{label}: {current} AUD")


@cli.command()
@click.argument("account", type=click.types.STRING)
@click.pass_obj
def assertion(session, account):
    """Print a ledger balance assertion for the start of today.

    ACCOUNT is the full beancount account name the assertion is for, such as
    Assets:Bank:Up.

    Beancount checks a `balance` directive at the start of its date, while
    Up's balance runs through the day. Stamping Up's current balance with
    today's date fails whenever something was spent earlier today; stamping
    it with tomorrow's fails when something is spent later today. So today's
    transactions are backed out instead, giving the balance the ledger must
    show at midnight.
    """
    if ":" not in account:
        raise click.BadParameter(
            f"{account!r} is not a full beancount account name "
            f"(expected something like Assets:Bank:Up)",
            param_hint="ACCOUNT",
        )
    today, opening = start_of_day_assertion(session.client, account=session.account)
    click.echo(f"{today} balance {account} \t\t {opening} AUD\n")


def _account_id(session):
    """The id of the selected Up account; or None if none was selected."""
    if session.account is None:
        return None
    return live_balance(session.client, session.account)[0]


@cli.command()
@click.argument("year", type=click.types.INT)
@click.argument("month", type=click.types.INT)
@click.pass_obj
def month(session, year, month):
    """Download a sequence of transactions."""
    transactions = session.client.get_month(
        year, month, account_id=_account_id(session)
    )
    click.echo(json.dumps(transactions, indent=3))


@cli.command()
@click.argument("days", type=click.types.INT, default=60)
@click.pass_obj
def recent(session, days):
    """Download a sequence of transactions.

    Only the account whose balance `balance` and `assertion` report, so a saver
    added later cannot leak into that account's ledger.
    """
    account_id, _ = live_balance(session.client, session.account)
    transactions = session.client.get_recent(days, account_id=account_id)
    click.echo(json.dumps(transactions, indent=3))


@cli.command()
@click.argument("days", type=click.types.INT, default=60)
@click.pass_obj
def held(session, days):
    """Download held transactions."""
    transactions = session.client.get_recent(
        days, account_id=_account_id(session), status=HELD
    )
    click.echo(json.dumps(transactions, indent=3))


if __name__ == "__main__":
    cli()
