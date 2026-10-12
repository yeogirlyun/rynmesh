"""Bounded, encrypted directory metadata. Call under the owning plugin's lock."""
import json
import os
import time
import uuid
from collections import OrderedDict


class DirectoryCache:
    MAX_BYTES = 8 * 1024 * 1024
    MAX_DIRS = 100
    MAX_AGE = 7 * 86400

    def __init__(self, plugin):
        self.plugin = plugin
        self.path = plugin.directory / "directories.enc"
        self.items = OrderedDict()
        self.location = {}
        try:
            if plugin.cipher and self.path.stat().st_size < self.MAX_BYTES * 2:
                value = json.loads(plugin.cipher.decrypt(self.path.read_bytes()))
                self.items = OrderedDict(value.get("items", []))
                self.location = value.get("location", {})
                self.prune()
        except Exception:
            # An unreadable cache must never prevent the app from starting.
            self.items.clear()
            self.location = {}

    @staticmethod
    def key(source, path):
        return json.dumps([source, path], ensure_ascii=False)

    def prune(self):
        valid = {s["id"] for s in self.plugin.state["sources"]}
        now = time.time()
        for key, item in list(self.items.items()):
            if json.loads(key)[0] not in valid or now - item["updated_at"] > self.MAX_AGE:
                self.items.pop(key, None)
        while len(self.items) > self.MAX_DIRS or sum(len(json.dumps(v, ensure_ascii=False).encode()) for v in self.items.values()) > self.MAX_BYTES:
            self.items.popitem(last=False)
        if self.location.get("source_id") not in valid:
            self.location = {}

    def get(self, source, path):
        key = self.key(source, path)
        item = self.items.get(key)
        if item and time.time() - item["updated_at"] <= self.MAX_AGE:
            self.items.move_to_end(key)
            return item
        return None

    def put(self, source, path, entries, **metadata):
        item = {"entries": entries, "updated_at": time.time(), **metadata}
        key = self.key(source, path)
        self.items[key] = item
        self.items.move_to_end(key)
        self.flush()
        return item

    def invalidate(self, source, path=None):
        for key in list(self.items):
            sid, folder = json.loads(key)
            if sid == source and (path is None or folder == path):
                self.items.pop(key, None)
        if path is None and self.location.get("source_id") == source:
            self.location = {}
        self.flush()

    def flush(self):
        self.prune()
        if not self.plugin.cipher:
            return
        temporary = self.path.with_name(uuid.uuid4().hex + ".cache.tmp")
        try:
            raw = json.dumps({"items": list(self.items.items()), "location": self.location}, ensure_ascii=False).encode()
            temporary.write_bytes(self.plugin.cipher.encrypt(raw))
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
        except OSError:
            # In-memory browsing still works if the cache disk is unavailable.
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
