"""One owner-local invitation continuation, cleared with compare-and-delete."""
import hashlib
from pathlib import Path

from ..atomic_io import atomic_write_json, read_json
from ..file_transactions import file_transaction
from .crypto import parse_invite


class InvitationContinuation:
    def __init__(self, home: Path, *, allow_loopback: bool = False):
        self.path = Path(home) / 'friends' / 'continuation.json'
        self.lock = self.path.with_suffix('.lock')
        self.allow_loopback = allow_loopback

    def get(self) -> dict:
        with file_transaction(self.lock):
            if not self.path.exists():
                return {}
            return read_json(self.path, max_bytes=24576)

    def save(self, uri: str) -> dict:
        parse_invite(uri, allow_loopback=self.allow_loopback, allow_expired=True)
        row = {'id': hashlib.sha256(uri.encode()).hexdigest(), 'invite_uri': uri}
        with file_transaction(self.lock):
            atomic_write_json(self.path, row, max_bytes=24576)
        return row

    def clear(self, identifier: str) -> bool:
        with file_transaction(self.lock):
            if self.get().get('id') != identifier:
                return False
            self.path.unlink(missing_ok=True)
            return True
