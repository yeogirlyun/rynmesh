import json
from copy import deepcopy
from pathlib import Path

from ..atomic_io import atomic_write_json, read_json
from ..crypto import canonical_json
from ..file_transactions import file_transaction
from ..services import peer_box

VERSION = 'ryn.device-erasure-journal.v1'
CHANNEL = b'rynmesh-device-erasure-journal-v1'
MAX_FILE = 4 * 1024 * 1024
MAX_JOBS = 128


class ErasureError(ValueError):
    pass


class ErasureStore:
    def __init__(self, home, key):
        self.path = Path(home) / 'device-erasure' / 'journal.json'
        self.lock = self.path.with_suffix('.lock')
        self.key, self.pub = key, peer_box.public_key_b64(key)

    def _read(self):
        if not self.path.exists():
            return {'version': VERSION, 'outgoing': {}, 'incoming': {}}
        try:
            wire = read_json(self.path, max_bytes=MAX_FILE)
            if wire['version'] != VERSION:
                raise ErasureError('erasure_version_unsupported')
            data = json.loads(peer_box.open_sealed(self.key, self.pub, wire['nonce'], wire['ciphertext'], info=CHANNEL))
            if data['version'] != VERSION or any(not isinstance(data.get(key), dict) or len(data[key]) > MAX_JOBS for key in ('outgoing', 'incoming')):
                raise ValueError
            return data
        except ErasureError:
            raise
        except Exception:
            raise ErasureError('erasure_journal_unavailable') from None

    def read(self):
        with file_transaction(self.lock):
            return self._read()

    def mutate(self, operation):
        with file_transaction(self.lock):
            data = self._read()
            before = canonical_json(data)
            result = operation(data)
            if canonical_json(data) != before:
                if any(len(data[key]) > MAX_JOBS for key in ('outgoing', 'incoming')):
                    raise ErasureError('erasure_capacity')
                nonce, ciphertext = peer_box.seal(self.key, self.pub, canonical_json(data), info=CHANNEL)
                value = {'version': VERSION, 'nonce': nonce, 'ciphertext': ciphertext}
                if len(canonical_json(value)) > MAX_FILE - 4096:
                    raise ErasureError('erasure_capacity')
                atomic_write_json(self.path, value, max_bytes=MAX_FILE)
            return deepcopy(result)
