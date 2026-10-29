"""The ``upbank`` command line."""

import json
import pprint
from dataclasses import dataclass

import click

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


@dataclass
class _Session:
    client: UpbankClient
    # Which of the token's Up accounts to use: an id or a display name; or None.
    account: str | None


@click.group(cls=_Group)
@click.option(
    "--token",
    envvar="UPBANK_TOKEN",
    help="Upbank personal access token. Prefer the UPBANK_TOKEN environment "
    "variable so the secret never appears on the command line or in logs.",
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
    if not token:
        raise click.UsageError(
            "No Upbank token supplied. Set the UPBANK_TOKEN environment variable "
            "(preferred, keeps the secret off the command line) or pass --token."
        )
    ctx.obj = _Session(UpbankClient(token), up_account)


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
