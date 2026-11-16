"""Tests for S05-I19 (G05-40): non-blocking concurrent reads during active write (T7) and serialized concurrent writes (T8)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ssl
import threading
import time

import rynmesh.nas as nas_mod
from rynmesh.nas import NasPlugin, WebDAVConnector


def _make_plugin(tmp_path) -> tuple[NasPlugin, str]:
    plugin = NasPlugin(tmp_path / "nas_write_home")

    class _SeedConnector:
        def __init__(self, _cfg):
            return None

        def list(self, _path):
            return []

        def close(self):
            return None

    plugin.connectors["smb"] = _SeedConnector
    plugin.enable(True)
    source = plugin.configure(
        {
            "name": "Concurrent NAS",
            "system": "fnos",
            "protocol": "smb",
            "host": "nas.example.test",
            "share": "docs",
            "username": "owner",
            "password": "secret",
            "writable": True,
            "allow_ai": True,
        }
    )
    return plugin, source["id"]


def test_t7_blocking_write_does_not_block_concurrent_list_or_preview(tmp_path) -> None:
    """T7 (G05-40): While a write is blocked holding gate, list/listing/preview on another thread finish within 1 second using one-shot connections."""
    plugin, source_id = _make_plugin(tmp_path)
    write_entered = threading.Event()
    release_write = threading.Event()
    write_connector_ids: set[int] = set()
    read_connector_ids: set[int] = set()
    id_lock = threading.Lock()

    class _BlockingWriteConnector:
        def __init__(self, _cfg):
            return None

        def list(self, _path):
            with id_lock:
                read_connector_ids.add(id(self))
            return [{"name": "note.txt", "path": "note.txt", "directory": False, "size": 11, "modified": 1}]

        def list_page(self, _path, _cursor=""):
            with id_lock:
                read_connector_ids.add(id(self))
            return {
                "entries": [{"name": "note.txt", "path": "note.txt", "directory": False, "size": 11, "modified": 1}],
                "next_cursor": "",
                "skipped": 0,
            }

        def read(self, path, _limit):
            with id_lock:
                read_connector_ids.add(id(self))
            assert path == "note.txt"
            return b"hello world"

        def write(self, _path, _data):
            with id_lock:
                write_connector_ids.add(id(self))
            write_entered.set()
            assert release_write.wait(timeout=5.0)

        def close(self):
            return None

    plugin.connectors["smb"] = _BlockingWriteConnector

    with ThreadPoolExecutor(max_workers=4) as pool:
        writer = pool.submit(plugin.operate, source_id, "write", "upload.bin", b"payload")
        assert write_entered.wait(timeout=2.0)
        try:
            t0 = time.monotonic()
            listed = pool.submit(plugin.operate, source_id, "list", "").result(timeout=1.0)
            paged = pool.submit(plugin.listing, source_id).result(timeout=1.0)
            preview = pool.submit(plugin.operate, source_id, "preview", "note.txt").result(timeout=1.0)
            elapsed = time.monotonic() - t0
            assert elapsed < 1.0
            assert [e["name"] for e in listed] == ["note.txt"]
            assert [e["name"] for e in paged["entries"]] == ["note.txt"]
            assert preview == {"text": "hello world", "name": "note.txt"}
            assert not (write_connector_ids & read_connector_ids)
        finally:
            release_write.set()
        assert writer.result(timeout=2.0)["ok"] is True


def test_t8_two_concurrent_writes_still_serialize_under_gate(tmp_path) -> None:
    """T8: Two concurrent write operations on the same NAS source still execute serially under gate while reads do not populate self.sessions."""
    plugin, source_id = _make_plugin(tmp_path)
    lock = threading.Lock()
    active_writers = 0
    max_concurrent_writers = 0
    spans: list[tuple[str, float, float]] = []
    first_entered = threading.Event()

    class _SerialWriteConnector:
        def __init__(self, _cfg):
            return None

        def read(self, _path, _limit):
            return b"read-only"

        def write(self, path, _data):
            nonlocal active_writers, max_concurrent_writers
            start = time.monotonic()
            with lock:
                active_writers += 1
                if active_writers > max_concurrent_writers:
                    max_concurrent_writers = active_writers
            first_entered.set()
            time.sleep(0.2)
            end = time.monotonic()
            with lock:
                active_writers -= 1
                spans.append((path, start, end))

        def close(self):
            return None

    plugin.connectors["smb"] = _SerialWriteConnector

    # One-shot read must not leave a reusable session in plugin.sessions
    plugin._close_session(source_id)
    assert plugin.operate(source_id, "read", "doc.txt") == b"read-only"
    assert source_id not in plugin.sessions

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(plugin.operate, source_id, "write", "a.txt", b"1")
        assert first_entered.wait(timeout=2.0)
        f2 = pool.submit(plugin.operate, source_id, "write", "b.txt", b"2")
        assert f1.result(timeout=3.0)["ok"] is True
        assert f2.result(timeout=3.0)["ok"] is True

    assert max_concurrent_writers == 1
    assert len(spans) == 2
    assert spans[1][1] >= spans[0][2]
    assert source_id in plugin.sessions


def test_t10_one_shot_webdav_connectors_do_not_reload_ssl_ca_bundle_per_read(monkeypatch) -> None:
    """Rework #2: One-shot WebDAVConnector instances must skip CA bundle loading for http:// and cache SSLContext for https:// so inflight reads do not stall before sending GET."""
    monkeypatch.setattr(nas_mod, "_HTTP_ONLY_SSL_CONTEXT", None, raising=False)
    monkeypatch.setattr(nas_mod, "_HTTPS_DEFAULT_SSL_CONTEXT", None, raising=False)

    real_create_default_context = ssl.create_default_context
    ca_load_calls = 0

    def counting_create_default_context(*args, **kwargs):
        nonlocal ca_load_calls
        ca_load_calls += 1
        return real_create_default_context(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", counting_create_default_context)

    for _ in range(4):
        http_conn = WebDAVConnector(
            {
                "url": "http://127.0.0.1:18080/dav",
                "root": "",
                "username": "fixture",
                "password": "fixture",
            }
        )
        http_conn.close()
    assert ca_load_calls == 0

    for _ in range(5):
        https_conn = WebDAVConnector(
            {
                "url": "https://dav.example.test/dav",
                "root": "shared",
                "username": "fixture",
                "password": "fixture",
            }
        )
        assert https_conn.url("allowed/source.md") == "https://dav.example.test/dav/shared/allowed/source.md"
        https_conn.close()
    assert ca_load_calls == 1

