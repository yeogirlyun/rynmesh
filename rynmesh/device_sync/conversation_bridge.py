"""Conversation source durability before replication acknowledgement.

The authenticated device transfer worker supplies authorization before these internal
methods are called. They neither grant permission nor discover/send to devices.
"""
from __future__ import annotations

from ..file_transactions import file_transaction
from .records import SyncError

SCOPES = ['conversations']


class ConversationBridge:
    def __init__(self, source, replica):
        self.source, self.replica = source, replica

    def _identity(self):
        if self.source.sync_identity() != self.replica.actor:
            raise SyncError('sync_device_identity_changed')

    def _reconcile(self):
        self._identity()
        self.replica.reconcile_source(self.source.sync_export(), scopes=SCOPES)

    def pending(self, device):
        with file_transaction(self.source.lock):
            self._reconcile()
            return self.replica.pending(device, SCOPES)

    def receive(self, rows):
        with file_transaction(self.source.lock):
            self._identity()
            receipts = self.source.sync_receive(rows)
            self._reconcile()
            return receipts

    def acknowledge(self, device, receipts):
        with file_transaction(self.source.lock):
            self._reconcile()
            return self.replica.acknowledge(device, receipts, scopes=SCOPES)
