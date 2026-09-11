"""Reading-source durability before replica receipts; no network authorization.

The authenticated device transfer worker calls this adapter only for its approved
scopes. Constructing it does not opt in, discover devices or send any data.
Local reading writes depend only on ConsumptionStore, never on replica health.
"""
from __future__ import annotations

from ..file_transactions import file_transaction
from .reading import scopes as reading_scopes
from .records import SyncError


class ReadingBridge:
    def __init__(self, source, replica):
        self.source = source
        self.replica = replica

    def _reconcile(self, scopes):
        selected = reading_scopes(scopes)
        if self.source.sync_identity() != self.replica.actor:
            raise SyncError('sync_device_identity_changed')
        self.replica.reconcile_source(self.source.sync_export(selected), scopes=selected)
        return selected

    def pending(self, device, scopes):
        # One consistent lock order: source -> replica. Never hold over network IO.
        with file_transaction(self.source.lock_path):
            selected = self._reconcile(scopes)
            return self.replica.pending(device, selected)

    def receive(self, rows, *, scopes):
        selected = reading_scopes(scopes)
        with file_transaction(self.source.lock_path):
            if self.source.sync_identity() != self.replica.actor:
                raise SyncError('sync_device_identity_changed')
            receipts = self.source.sync_receive(rows, scopes=selected)
            # If either commit fails, return no acknowledgement. Source-first
            # commits remain readable and replay safely after restart.
            self._reconcile(selected)
            return receipts

    def acknowledge(self, device, receipts, *, scopes):
        with file_transaction(self.source.lock_path):
            # Include any local edit made since sending before checking the ACK.
            selected = self._reconcile(scopes)
            return self.replica.acknowledge(device, receipts, scopes=selected)
