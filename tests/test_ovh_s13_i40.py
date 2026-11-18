"""S13-I40: bounded reads through the real published-content HTTP endpoint."""
import tracemalloc

import pytest
from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.store import RynmeshStore

MIB = 1024 * 1024


def publish(tmp_path, monkeypatch, data=None, *, large=False, media_type="text/plain", suffix=".txt"):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DEFAULT_DISCOVERY", "0")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    monkeypatch.delenv("RYNMESH_LOCAL_BODY_MAX_BYTES", raising=False)
    store = RynmeshStore(home=tmp_path / "home", network_dir=tmp_path / "network")
    source = tmp_path / ("document" + suffix)
    if large:
        block = b"x" * MIB
        with source.open("wb") as handle:
            for _ in range(200):
                handle.write(block)
    else:
        source.write_bytes(data)
    record = store.publish_content(source, title="Document", transcript="Safe generated document.",
                                   content_type=media_type, content_kind="document")
    # No lifespan workers run: trace the actual HTTP body handler, not unrelated
    # periodic indexing, discovery or model work in a whole-node startup.
    return TestClient(create_app(store)), record["content_id"], source


def test_t1_200_mib_text_returns_one_mib_with_bounded_peak_allocation(tmp_path, monkeypatch):
    client, content_id, source = publish(tmp_path, monkeypatch, large=True)
    assert source.stat().st_size == 200 * MIB
    try:
        tracemalloc.start()
        try:
            response = client.get(f"/api/local/content/{content_id}/body")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert response.status_code == 200
        result = response.json()
        assert result["text"] == "x" * MIB
        assert result["truncated"] is True
        assert result["content_id"] == content_id
        assert result["content_type"] == "text/plain"
        print(f"200 MiB body request peak traced allocation: {peak / MIB:.2f} MiB")
        assert peak < 32 * MIB, f"body request allocated {peak / MIB:.2f} MiB for a 200 MiB file"
    finally:
        client.close()


@pytest.mark.parametrize("length,truncated", [(7, False), (8, False), (9, True)])
def test_configured_limit_preserves_exact_boundary_and_truncation(tmp_path, monkeypatch, length, truncated):
    client, content_id, _ = publish(tmp_path, monkeypatch, b"x" * length)
    monkeypatch.setenv("RYNMESH_LOCAL_BODY_MAX_BYTES", "8")
    try:
        response = client.get(f"/api/local/content/{content_id}/body")
        assert response.status_code == 200
        assert response.json()["text"] == "x" * min(length, 8)
        assert response.json()["truncated"] is truncated
    finally:
        client.close()


@pytest.mark.parametrize("data,media_type,suffix,detail", [
    (b"\xffinvalid", "text/plain", ".txt", "content_body_not_utf8"),
    (b"binary", "application/octet-stream", ".bin", "content_body_not_text"),
])
def test_existing_content_type_and_utf8_errors_are_unchanged(tmp_path, monkeypatch, data, media_type, suffix, detail):
    client, content_id, _ = publish(tmp_path, monkeypatch, data, media_type=media_type, suffix=suffix)
    try:
        response = client.get(f"/api/local/content/{content_id}/body")
        assert response.status_code == 415
        assert response.json() == {"detail": detail}
    finally:
        client.close()
