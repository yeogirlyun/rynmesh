"""Real connector/HTTP fixture checks, not Agent effect scores."""
import importlib.util
from pathlib import Path

import httpx
import pytest

from rynmesh.nas import NasError, WebDAVConnector

_spec = importlib.util.spec_from_file_location("agent_fixture_dav", Path(__file__).parent /
    "fixtures/agent_acceptance/webdav_fixture.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
FixtureDAV = _module.FixtureDAV


def fixture(tmp_path):
    (tmp_path / ".ryn-acceptance-isolated").touch()
    (tmp_path / "allowed").mkdir()
    (tmp_path / "allowed/source.md").write_text("Evidence 42", encoding="utf-8")
    return FixtureDAV(tmp_path)


def connector(server):
    return WebDAVConnector({"url": server.url, "root": "", "username": "fixture", "password": "fixture"})


def test_real_dav_list_read_create_and_existing_name_rejection(tmp_path):
    with fixture(tmp_path) as server:
        client = connector(server)
        try:
            assert client.list("allowed")[0]["path"] == "allowed/source.md"
            assert client.read("allowed/source.md", 1024) == b"Evidence 42"
            client.mkdir("allowed/reports")
            client.write("allowed/reports/new.md", b"Generated")
            with pytest.raises(NasError, match="file_exists_or_parent_missing"):
                client.write("allowed/reports/new.md", b"Must not replace")
            assert client.read("allowed/reports/new.md", 1024) == b"Generated"
        finally:
            client.close()
        assert [(e["method"], e["status"]) for e in server.audit if e["mutation"]] == [("MKCOL", 201), ("PUT", 201)]


def test_commit_then_drop_has_real_file_and_independent_audit(tmp_path):
    with fixture(tmp_path) as server:
        server.arm("PUT", "allowed/result.md", "drop_after_commit")
        client = connector(server)
        try:
            with pytest.raises(httpx.RemoteProtocolError):
                client.write("allowed/result.md", b"Committed once")
            assert (tmp_path / "allowed/result.md").read_bytes() == b"Committed once"
            assert server.audit[-1]["mutation"] and server.audit[-1]["response_dropped"]
            assert client.read("allowed/result.md", 1024) == b"Committed once"
        finally:
            client.close()


def test_dav_root_marker_and_path_confinement(tmp_path):
    with pytest.raises(ValueError, match="marker"):
        FixtureDAV(tmp_path)
    with fixture(tmp_path) as server:
        for path in ("/dav/%2e%2e/secret", "/dav/a%5cb", "/dav/C:/secret", "/dav/.ryn-acceptance-isolated", "/other"):
            with pytest.raises(ValueError, match="invalid_fixture_path"):
                server.locate(path)
        assert server.locate("/dav/allowed/source.md")[1] == tmp_path / "allowed/source.md"
