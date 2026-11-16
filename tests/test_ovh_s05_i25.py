"""S05-I25 download proxy, byte ceiling, and safe publication acceptance."""
import errno
import hashlib
import socket
import tempfile
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from test_agent_local_tools import local, execute
from rynmesh.agent_runtime import downloads
from rynmesh.agent_runtime.store import AgentToolRejected


URL = "https://files.example.test/report.txt"
PROXY = "http://proxy.example.test:8080"


@pytest.fixture
def transfer(monkeypatch):
    real_client = httpx.Client
    calls, requests = [], []
    options = {"headers": {}, "chunks": [b"hello", b" world"]}

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            options["started"] = True
            yield from options["chunks"]

    def respond(request):
        requests.append(request)
        if options.get("proxy_error"):
            raise httpx.ProxyError("proxy unavailable")
        return httpx.Response(200, headers=options["headers"], stream=Body())

    def client(**kwargs):
        calls.append(kwargs)
        return real_client(transport=httpx.MockTransport(respond), trust_env=False)

    monkeypatch.setattr(downloads.httpx, "Client", client)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": PROXY, "http": PROXY})
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    monkeypatch.delenv("RYN_MAX_DOWNLOAD_BYTES", raising=False)
    return options, calls, requests


def test_t1_proxy_and_public_target_validation(local, transfer):
    tools, context, root, _ = local
    _, calls, requests = transfer
    result = execute(tools, context, "ryn_download_file", {"source": URL})
    assert Path(result["path"]).read_bytes() == b"hello world"
    assert calls[0]["proxy"] == PROXY and calls[0]["trust_env"] is False
    with pytest.raises(AgentToolRejected, match="agent_web_search_blocked_url"):
        downloads.save(tools, "http://127.0.0.1/private", context, lambda: False)
    assert len(requests) == 1


def test_t2_file_ref_bypasses_proxy(local, transfer, monkeypatch):
    tools, context, _, state = local
    _, calls, requests = transfer
    state.transaction(lambda s: s["tasks"]["task"].update(download_refs={
        "fileRef_test": {"url": URL, "name": "plugin.txt", "binding": {}, "scope_revision": context.get("scope_revision")}
    }))
    # The production extensions adapter installs this bound authority callback.
    monkeypatch.setattr(tools, "download_authority", lambda *a: ({"Authorization": "Bearer test-only"}, downloads.origin(URL)), raising=False)
    result = execute(tools, context, "ryn_download_file", {"source": "fileRef_test"})
    assert calls[0].get("proxy") is None and calls[0]["trust_env"] is False
    assert requests[0].headers["authorization"] == "Bearer test-only"
    assert Path(result["path"]).name == "plugin.txt"


@pytest.mark.parametrize("declared", [False, True])
def test_t3_size_limit_cleans_temp_and_rejects_header_before_body(local, transfer, monkeypatch, declared):
    tools, context, root, _ = local
    options, _, _ = transfer
    monkeypatch.setenv("RYN_MAX_DOWNLOAD_BYTES", "8")
    if declared:
        options["headers"]["content-length"] = "11"
    with pytest.raises(AgentToolRejected, match="agent_download_too_large") as caught:
        execute(tools, context, "ryn_download_file", {"source": URL})
    assert "8" in caught.value.correction
    assert list(root.iterdir()) == []
    assert tools.ledger_factory().get("op")["status"] == "not_executed"
    if declared:
        assert not options.get("started")


def test_t4_link_fallback_preserves_existing_file(local, transfer, monkeypatch):
    tools, context, root, _ = local
    (root / "report.txt").write_bytes(b"original")

    def unsupported(*args):
        raise OSError(errno.EPERM, "hard links unavailable")

    monkeypatch.setattr(downloads.os, "link", unsupported)
    result = execute(tools, context, "ryn_download_file", {"source": URL})
    assert Path(result["path"]).name == "report (1).txt"
    assert Path(result["path"]).read_bytes() == b"hello world"
    assert result["sha256"] == hashlib.sha256(b"hello world").hexdigest()
    assert (root / "report.txt").read_bytes() == b"original"
    assert not list(root.glob(".ryn-download-*"))


@pytest.mark.parametrize("name", ["con.txt", "PRN", "aUx.pdf", "NUL", "COM1.txt", "lpt9.csv"])
def test_t5_windows_reserved_names(local, transfer, name):
    tools, context, _, _ = local
    transfer[0]["headers"]["content-disposition"] = f'attachment; filename="{name}"'
    result = execute(tools, context, "ryn_download_file", {"source": URL})
    assert Path(result["path"]).name == "_" + name


def test_t6_long_names_keep_extension_and_collision_suffix(transfer):
    transfer[0]["headers"]["content-disposition"] = 'attachment; filename="' + "a" * 300 + '.txt"'
    # Isolate the component limit from Windows' independent total-path limit.
    with tempfile.TemporaryDirectory(prefix="i25-") as directory:
        tools = SimpleNamespace(path=lambda name, context: Path(directory) / name)
        first = downloads.save(tools, URL, {}, lambda: False)
        second = downloads.save(tools, URL, {}, lambda: False)
        assert len(Path(first["path"]).name) == 200
        assert len(Path(second["path"]).name) == 200
        assert Path(second["path"]).name.endswith(" (1).txt")
        assert Path(first["path"]).read_bytes() == Path(second["path"]).read_bytes() == b"hello world"


def test_t7_save_failure_records_not_executed(local, transfer, monkeypatch):
    tools, context, root, _ = local
    real_replace = downloads.os.replace

    def fail(*args):
        raise OSError(errno.EINVAL, "invalid filename")

    def fail_download_replace(source, target):
        if Path(source).name.startswith(".ryn-download-"):
            raise OSError(errno.EINVAL, "invalid filename")
        return real_replace(source, target)

    monkeypatch.setattr(downloads.os, "link", fail)
    monkeypatch.setattr(downloads.os, "replace", fail_download_replace)
    with pytest.raises(AgentToolRejected, match="agent_download_save_failed") as caught:
        execute(tools, context, "ryn_download_file", {"source": URL})
    assert caught.value.details == {"name": "report.txt"}
    assert caught.value.correction == "Could not save the file under that name; choose a shorter or simpler file name."
    assert tools.ledger_factory().get("op")["status"] == "not_executed"
    assert list(root.iterdir()) == []


def test_proxy_error_is_specific(local, transfer):
    tools, context, root, _ = local
    transfer[0]["proxy_error"] = True
    with pytest.raises(AgentToolRejected, match="agent_download_proxy_error"):
        execute(tools, context, "ryn_download_file", {"source": URL})
    assert list(root.iterdir()) == []
