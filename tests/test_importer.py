import json
import logging
import os

import pytest

from beancount_upbank import UpbankImporter, markers

logging.basicConfig(level=logging.DEBUG)


# Define the importer instance
@pytest.fixture
def importer():
    return UpbankImporter()


# Test the identify method
def test_identify(importer):
    test_file = os.path.join(os.path.dirname(__file__), "data/upbank.json")
    result = importer.identify(test_file)
    assert result is True


def test_extract_characterisation(importer):
    """Pin the full extract() output, so a refactor cannot change it unnoticed."""
    test_file = os.path.join(os.path.dirname(__file__), "data/upbank.json")
    entries = importer.extract(test_file)
    summary = [
        (
            e.date.isoformat(),
            e.payee,
            e.narration,
            str(e.postings[0].units),
            e.meta.get("__up_id__"),
            e.meta.get("up_hold"),
        )
        for e in entries
    ]
    # The API lists newest-first; the ledger wants oldest-first.
    assert summary == [
        (
            "2025-04-23",
            "ALICE EXAMPLE",
            "Up Alice",
            "350.00 AUD",
            "00000000-0000-4000-8000-000000000001",
            None,
        ),
        (
            "2025-04-24",
            "Example Books",
            "EXAMPLE BOOKS PTY LTD Birthday present",
            "-42.10 AUD",
            "00000000-0000-4000-8000-000000000002",
            None,
        ),
        # Only a HELD transaction is marked provisional in the ledger.
        (
            "2025-04-25",
            "Cafe Example",
            "CAFE EXAMPLE SYDNEY",
            "-6.50 AUD",
            "00000000-0000-4000-8000-000000000003",
            "00000000-0000-4000-8000-000000000003",
        ),
    ]
    assert importer.date(test_file).isoformat() == "2025-04-25"


def test_marker_names_are_part_of_the_ledger_format():
    # `up_hold` is written into users' ledgers; renaming it would orphan every
    # entry that is still held.
    assert markers.HOLD_KEY == "up_hold"
    assert markers.ID_KEY == "__up_id__"


def _two_account_file(tmp_path):
    """The fixture, with its middle transaction moved to another Up account."""
    source = os.path.join(os.path.dirname(__file__), "data/upbank.json")
    with open(source) as fh:
        transactions = json.load(fh)
    transactions[1]["relationships"]["account"]["data"]["id"] = "saver-account"
    path = tmp_path / "two-accounts.json"
    path.write_text(json.dumps(transactions))
    return str(path)


def test_account_id_keeps_only_that_accounts_transactions(tmp_path):
    path = _two_account_file(tmp_path)
    spending = "00000000-0000-4000-8000-0000000000a1"

    kept = UpbankImporter(account_id=spending).extract(path)
    assert [e.payee for e in kept] == ["ALICE EXAMPLE", "Cafe Example"]

    saver = UpbankImporter(account_id="saver-account").extract(path)
    assert [e.payee for e in saver] == ["Example Books"]


def test_without_an_account_id_every_transaction_is_imported(tmp_path):
    assert len(UpbankImporter().extract(_two_account_file(tmp_path))) == 3


def test_date_is_none_when_no_transaction_belongs_to_the_account(tmp_path):
    path = _two_account_file(tmp_path)
    assert UpbankImporter(account_id="someone-else").date(path) is None


def test_flag_and_currency_are_configurable(importer):
    test_file = os.path.join(os.path.dirname(__file__), "data/upbank.json")

    class Pending(UpbankImporter):
        FLAG = "!"

    entries = Pending(currency="NZD").extract(test_file)
    assert {e.flag for e in entries} == {"!"}
    assert {e.postings[0].units.currency for e in entries} == {"NZD"}
