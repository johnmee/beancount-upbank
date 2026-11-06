import json
from datetime import date

import beancount
import beangulp
from beancount.core import amount, data
from beangulp import cache

from .client import CURRENCY
from .dedup import exact_amount_comparator
from .markers import HOLD_KEY, ID_KEY

# Up's TransactionStatusEnum. There is no terminal "released" state: a hold that
# is cancelled rather than settled simply disappears from the API.
HELD = "HELD"


class UpbankImporter(beangulp.Importer):
    """Interface that all source importers need to comply with."""

    # A flag to use on new transaction. Override this flag as you prefer.
    FLAG = beancount.core.flags.FLAG_OKAY

    def __init__(
        self,
        account_name="Assets:Bank:Upbank",
        tags=data.EMPTY_SET,
        account_id=None,
    ):
        """
        Args:
            account_name: beancount account name for the upbank account.
            tags: set of tags to apply to every transaction.
            account_id: Up's id for the account (see ``upbank accounts``). When
                set, transactions belonging to any other Up account in the file
                are skipped, so a saver's transactions cannot leak into this
                account's ledger. When None, every transaction is imported.
        """
        self.account_name = account_name
        self.tags = tags
        self.account_id = account_id

    # Upbank posts exact amounts, so only exactly-equal amounts are duplicates
    # (see dedup.exact_amount_comparator); a percentage tolerance would falsely
    # merge distinct transactions such as $6.95 and $7.08.
    cmp = staticmethod(exact_amount_comparator)

    @property
    def name(self):
        return "Upbank"

    def identify(self, filepath):
        """Return true if this importer matches the given file.

        Args:
          filepath: Filesystem path to the document to be matched.
        Returns:
          A boolean, true if this importer can handle this file.
        """
        file = cache.get_file(filepath)
        try:
            # Is a json file with specific structure and attributes.
            transactions = json.loads(file.contents())
            assert isinstance(transactions, list), (
                "JSON file is not a list of transactions."
            )
            assert len(transactions) > 0, "JSON list of transactions is empty"
            for trans in transactions:
                assert isinstance(trans, dict), (
                    "JSON list contains a non-dictionary item."
                )
                assert "type" in trans
                assert "id" in trans
                assert "attributes" in trans
                assert "relationships" in trans
                assert "links" in trans
            return True
        except json.JSONDecodeError:
            pass
        except AssertionError:
            pass
        return False

    def extract(self, filepath, existing_entries=None):
        """Extract transactions from a file.

        Args:
          filepath: Filesystem path to the document being imported.
          existing_entries: An optional list of existing directives loaded from
            the ledger which is intended to contain the extracted entries. This
            is only provided if the user provides them via a flag in the
            extractor program.
        Returns:
          A list of new, imported directives (usually mostly Transactions)
          extracted from the file.
        """
        file = cache.get_file(filepath)
        # Open the file as json
        transactions = json.loads(file.contents())
        entries = []

        for trans in reversed(transactions):
            if self.account_id is not None:
                owner = trans["relationships"]["account"]["data"]["id"]
                if owner != self.account_id:
                    continue
            trans_id = trans["id"]
            date_ = date.fromisoformat(trans["attributes"]["createdAt"][:10])
            raw_text = trans["attributes"]["rawText"]
            description = trans["attributes"]["description"]
            message = trans["attributes"]["message"]
            if message is not None and len(message):
                if raw_text is None or raw_text == description:
                    raw_text = message
                else:
                    raw_text += " " + message
            value = amount.Amount(
                beancount.core.number.D(trans["attributes"]["amount"]["value"]),
                CURRENCY,
            )
            posting = data.Posting(self.account_name, value, None, None, None, None)

            # Carry Up's transaction id on every entry, but only *write* it to
            # the ledger for transactions that are still HELD.
            #
            # A HELD transaction is provisional: Up may settle it at a different
            # amount (a restaurant tip, a foreign conversion), or release it
            # entirely so it vanishes from the API with no tombstone
            # (TransactionStatusEnum is HELD|SETTLED only -- there is no
            # terminal "released" state a poller can observe). The HOLD_KEY
            # metadata marks those entries as unsettled and lets a later run
            # find the same transaction again however its amount moved; the
            # reconcile pass then corrects the entry and strips the marker, so a
            # settled ledger keeps no import bookkeeping at all. Its presence is
            # the held marker -- no separate status is needed, and while held
            # the posting amount already *is* the amount Up is holding.
            #
            # ID_KEY is the same id on every entry for the deduplicator's
            # benefit. beancount's printer skips keys starting with "__", so it
            # is never written to the ledger; this is what lets an incoming
            # settled transaction be matched against a held ledger entry without
            # requiring reconcile to have run first.
            meta = data.new_metadata(file.name, 0, {ID_KEY: trans_id})
            if trans["attributes"]["status"] == HELD:
                meta[HOLD_KEY] = trans_id

            txn = data.Transaction(
                meta=meta,
                date=date_,
                flag=self.FLAG,
                payee=description,
                tags=self.tags,
                links=data.EMPTY_SET,
                narration=raw_text,
                postings=[posting],
            )
            entries.append(txn)

        # I'd like to insert a balance line but, to upbank, 'balance' means 'available balance',
        # so includes HELD funds and perhaps others.
        # The api-fetch can only snapshot a 'balance' at the moment of asking.

        return entries

    def account(self, filepath):
        """Return an account associated with the given file.

        Note: If you don't implement this method you won't be able to move the
        files into its preservation hierarchy; the bean-file command won't
        work.

        Also, normally the returned account is not a function of the input
        file--just of the importer--but it is provided anyhow.

        Args:
          file: A cache.FileMemo instance.
        Returns:
          The name of the account that corresponds to this importer.
        """
        return self.account_name

    def filename(self, filepath):
        """A filter that optionally renames a file before filing.

        This is used to make tidy filenames for filed/stored document files. If
        you don't implement this and return None, the same filename is used.
        Note that if you return a filename, a simple, RELATIVE filename must be
        returned, not an absolute filename.

        Args:
          file: A cache.FileMemo instance.
        Returns:
          The tidied up, new filename to store it as.
        """
        # Use the account name.
        account_str = self.account_name.split(":")[-1]
        return f"{account_str}.json"

    def date(self, filepath):
        """Attempt to obtain a date that corresponds to the given file.

        Args:
          filepath: Filesystem path to the document being imported.
        Returns:
          A date object, if successful, or None if a date could not be extracted.
          (If no date is returned, the file creation time is used. This is the
          default.)
        """
        # Date of last transaction in the file; None if none belong to this account.
        entries = self.extract(filepath)
        return entries[-1].date if entries else None
