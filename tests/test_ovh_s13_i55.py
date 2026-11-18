"""S13-I55: recover source configuration and distinguish unreadable result files."""
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.agent_runtime import results
from rynmesh.agent_runtime.store import AgentError, AgentStore, AgentToolRejected
from rynmesh.llm_package import sources
from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.llm_package.sources import SourceStore
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


@pytest.mark.parametrize("damage", ["token", "json", "encoding", "changed_key"])
def test_t1_corrupt_sources_do_not_prevent_node_routes_and_expose_backup(tmp_path, damage):
    home = tmp_path / "home"
    node = RynmeshStore(home=home, network_dir=tmp_path / "network")
    original = SourceStore(home, node.private_key_bytes)
    original.path.parent.mkdir(parents=True, exist_ok=True)
    damaged = {
        "token": b"invalid ciphertext",
        "json": original.cipher.encrypt(b"{"),
        "encoding": original.cipher.encrypt(b"\xff"),
        "changed_key": SourceStore(tmp_path / "other", b"different-key").cipher.encrypt(b"{}"),
    }[damage]
    original.path.write_bytes(damaged)
    app = FastAPI()
    install_llm_routes(
        app, store=node, home=home, messaging_key=X25519PrivateKey.generate(),
        resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "",
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/api/local/llm/sources")
        assert response.status_code == 200
        data = response.json()
        assert data["sources"] == []
        warning = data["storage_warning"]
        assert warning == app.state.llm_agent_services["source_store"].recovered
        assert isinstance(warning["at"], int)
        assert warning["reason"] in {"invalid_token", "invalid_json"}
        backup = Path(warning["backup"])
        assert backup.name == f"sources.enc.corrupt-{warning['at']}"
        assert backup.read_bytes() == damaged
        assert not original.path.exists()
    assert SourceStore(home, node.private_key_bytes).read() == {}


def test_t1_source_recovery_keeps_only_three_backups(tmp_path, monkeypatch):
    store = SourceStore(tmp_path, b"key")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    for stamp in (1000000000, 1000000001, 1000000002):
        store.path.with_name(f"sources.enc.corrupt-{stamp}").write_bytes(b"old")
    store.path.write_bytes(b"broken")
    monkeypatch.setattr(sources.time, "time", lambda: 1000000003)
    recovered = SourceStore(tmp_path, b"key")
    assert recovered.read() == {}
    assert sorted(p.name for p in store.path.parent.glob("sources.enc.corrupt-*")) == [
        f"sources.enc.corrupt-{stamp}" for stamp in (1000000001, 1000000002, 1000000003)
    ]


def test_t2_permission_error_does_not_quarantine_source_configuration(tmp_path, monkeypatch):
    store = SourceStore(tmp_path, b"key")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    original = store.cipher.encrypt(b"{}")
    store.path.write_bytes(original)
    read_bytes = Path.read_bytes

    def denied(path):
        if path == store.path:
            raise PermissionError("temporary access failure")
        return read_bytes(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", denied)
        with pytest.raises(PermissionError):
            SourceStore(tmp_path, b"key")
    assert store.path.read_bytes() == original
    assert list(store.path.parent.glob("sources.enc.corrupt-*")) == []


@pytest.mark.parametrize("damaged", [b"{", b"\xff"])
def test_t3_invalid_cli_configuration_is_quarantined_then_real_connect_recovers(tmp_path, monkeypatch, damaged):
    from test_agent_cli_sources import setup, waiting
    from test_agent_runtime import advance, approve

    runtime, model, tools, _, calls = setup(tmp_path, monkeypatch)
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(b"existing manifest")
    path = tools.cli_sources.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(damaged)
    with pytest.raises(AgentToolRejected, match="^agent_cli_configuration_invalid$"):
        tools.cli_sources.read()
    assert tools.cli_sources.read() == {}
    backups = list(path.parent.glob("cli-services.json.corrupt-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == damaged
    assert manifest.read_bytes() == b"existing manifest"
    pending = waiting(runtime, model)
    approve(runtime, pending["id"])
    advance(runtime)
    config = tools.cli_sources.read()["antigravity_cli"]
    assert config["manifest"] == str(manifest)
    assert config["publication_enabled"] is False
    assert [call[0] for call in calls] == ["connect", "chat"]


@pytest.mark.parametrize("damage,code", [
    ("missing", "agent_result_missing"),
    ("tag", "agent_result_corrupt"),
    ("hash", "agent_result_corrupt"),
    ("envelope_json", "agent_result_corrupt"),
    ("payload_json", "agent_result_corrupt"),
    ("permission", "agent_result_unreadable"),
])
def test_t4_result_error_codes_and_diagnostics(tmp_path, monkeypatch, caplog, damage, code):
    store = AgentStore(tmp_path / "agent", X25519PrivateKey.generate())
    row = {"id": "agent_test", "goal": {"revision": 1}, "scope_revision": 1, "context_epoch": 0}
    value = {"text": "RESULT_BODY_NOT_FOR_LOGS"}
    reference = results.save(store, row, {"name": "inspect_service", "operation_id": "op_test"}, value)
    row["result_refs"] = [reference]
    path = results.path(store, row["id"], reference["id"])
    if damage == "missing":
        path.unlink()
    elif damage == "tag":
        envelope = json.loads(path.read_text())
        nonce, ciphertext = peer_box.seal(store.key, store.pub, b"{}", info=b"wrong-channel")
        path.write_text(json.dumps({**envelope, "nonce": nonce, "ciphertext": ciphertext}))
    elif damage == "hash":
        reference["sha256"] = "0" * 64
    elif damage == "envelope_json":
        path.write_text("{")
    elif damage == "payload_json":
        raw = b"{"
        reference["sha256"] = hashlib.sha256(raw).hexdigest()
        nonce, ciphertext = peer_box.seal(store.key, store.pub, raw, info=results.CHANNEL)
        path.write_text(json.dumps({"nonce": nonce, "ciphertext": ciphertext}))
    read_bytes = Path.read_bytes

    def denied(candidate):
        if candidate == path and damage == "permission":
            raise PermissionError("RESULT_BODY_NOT_FOR_LOGS")
        return read_bytes(candidate)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", denied)
        with pytest.raises(AgentError, match=f"^{code}$"):
            results.read(store, row, reference["id"])
    failures = [r.getMessage() for r in caplog.records if "agent_failure" in r.getMessage()]
    assert len(failures) == 1
    assert "stage=result_read" in failures[0] and "RESULT_BODY_NOT_FOR_LOGS" not in failures[0]
    if damage == "permission":
        assert results.read(store, row, reference["id"])[0] == value
