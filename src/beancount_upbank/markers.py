"""The metadata keys the Up importer puts on the entries it creates.

These names are part of the ledger format: ``HOLD_KEY`` is written into users'
ledgers, so changing its value would orphan every entry that is still held.
"""

# Written to the ledger, and only on an entry that was still HELD when it was
# imported. Its presence marks the entry as provisional; its value is Up's
# transaction id, which is how a later run finds the same transaction again
# however its amount moved. `upbank-reconcile` removes it once the hold settles.
HOLD_KEY = "up_hold"

# Carried on every incoming entry so the deduplicator can match by Up's
# transaction id. beancount's printer skips keys that start with "__", so this
# never reaches the ledger.
ID_KEY = "__up_id__"
