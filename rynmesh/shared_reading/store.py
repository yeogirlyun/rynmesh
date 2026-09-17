import json
from copy import deepcopy
from pathlib import Path

from ..atomic_io import atomic_write_json, read_json
from ..crypto import canonical_json
from ..file_transactions import file_transaction
from ..services import peer_box

VERSION = 'ryn.shared-reading.v1'
CHANNEL = b'rynmesh-shared-reading-storage-v1'
MAX_FILE = 4 * 1024 * 1024


class ListError(ValueError):
    pass


class ListStore:
    def __init__(self, home, key):
        self.path = Path(home) / 'shared-reading' / 'state.json'
        self.lock = self.path.with_suffix('.lock')
        self.key, self.pub = key, peer_box.public_key_b64(key)

    def _read(self):
        if not self.path.exists():
            return {'version': VERSION, 'lists': {}}
        try:
            wire = read_json(self.path, max_bytes=MAX_FILE)
            if wire['version'] != VERSION:
                raise ListError('shared_version_unsupported')
            data = json.loads(peer_box.open_sealed(self.key, self.pub, wire['nonce'], wire['ciphertext'], info=CHANNEL))
            if data['version'] != VERSION or not isinstance(data['lists'], dict) or len(data['lists']) > 32:
                raise ListError('shared_store_unavailable')
            return data
        except ListError:
            raise
        except Exception:
            raise ListError('shared_store_unavailable') from None

    def read(self):
        with file_transaction(self.lock):
            return self._read()

    def mutate(self, operation):
        with file_transaction(self.lock):
            data = self._read()
            before = canonical_json(data)
            result = operation(data)
            if before != canonical_json(data):
                if len(data['lists']) > 32:
                    raise ListError('shared_capacity')
                nonce, ciphertext = peer_box.seal(self.key, self.pub, canonical_json(data), info=CHANNEL)
                wire = {'version': VERSION, 'nonce': nonce, 'ciphertext': ciphertext}
                if len(canonical_json(wire)) > MAX_FILE - 4096:
                    raise ListError('shared_capacity')
                atomic_write_json(self.path, wire, max_bytes=MAX_FILE)
            return deepcopy(result)
