"""S05-I35: receipt-bound large files, streaming and bounded transfer copies."""
import hashlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from test_agent_runtime import Catalog, Model, Tools
from test_agent_workspace_artifacts import written

from rynmesh.agent_runtime import artifact_transfers, workspace_artifacts as wa
from rynmesh.agent_runtime.artifact_transfers import ArtifactTransfers
from rynmesh.agent_runtime.routes import install_agent
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.background_workers import BackgroundWorkerRegistry

MIB = 1024 * 1024


def row_for(root, receipts):
    return {"id": str(root), "scope_revision": 1, "local_execution": {"workspace_root": str(root)},
            "receipts": receipts, "messages": [], "events": [], "status": "queued"}


def test_t1_twenty_mib_hashes_and_reads_but_300mib_is_explicitly_too_large(tmp_path):
    raw = b"x" * (20 * MIB)
    path = tmp_path / "report.pdf"
    path.write_bytes(raw)
    row = row_for(tmp_path, [written(path, raw, "op1")])
    item = wa.listing(row)["files"][0]
    assert item["status"] == "current" and item["sha256"] == hashlib.sha256(raw).hexdigest()
    assert wa.read(row, item["id"])["bytes"] == raw
    huge = tmp_path / "huge.pdf"
    with huge.open("wb") as stream:
        stream.truncate(300 * MIB)
    row["receipts"].append(written(huge, b"receipt", "op2"))
    item = next(item for item in wa.listing(row)["files"] if item["name"] == huge.name)
    assert item["status"] == "too_large" and item["sha256"] is None
    assert item["limit_bytes"] == 256 * MIB
    with pytest.raises(AgentError, match="agent_artifact_too_large"):
        wa.read(row, item["id"])


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
@pytest.mark.parametrize("suffix", [".BAT", ".ps1", ".js", ".py", ".exe", ".pdf"])
def test_t2_executables_reveal_without_running_and_pdf_still_opens(tmp_path, monkeypatch, platform, suffix):
    path = tmp_path / ("report" + suffix)
    path.write_bytes(b"inert test file")
    row = row_for(tmp_path, [written(path, b"inert test file", "op1")])
    started, spawned = [], []
    monkeypatch.setattr(wa, "os", SimpleNamespace(name="nt" if platform == "win32" else "posix", path=os.path, startfile=started.append))
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: spawned.append((args, kwargs)))
    result = wa.open_file(row, wa.artifact_id(path), "open")
    if suffix == ".pdf":
        assert result["opened"] is True
        assert started == [str(path)] if platform == "win32" else not started
    else:
        assert result["opened"] is False and result["revealed"] is True
        assert result["reason"] == "executable_not_opened" and not started
        command = spawned[0][0][0]
        if platform == "win32":
            assert "/select" in command
        elif platform == "darwin":
            assert command == ["open", "-R", str(path)]
        else:
            assert command == ["xdg-open", str(path.parent)]


def transfer_env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = AgentStore(tmp_path / "agent", X25519PrivateKey.generate())
    row = row_for(workspace, [])
    store.transaction(lambda data: data["tasks"].update({row["id"]: row}))
    context = {"task_id": row["id"], "owner_id": "owner", "scope_revision": 1}
    return store, workspace, context, ArtifactTransfers(store, clock=lambda: 1000)


def record(store, context, path, raw, operation):
    path.write_bytes(raw)
    def save(data):
        receipts = data["tasks"][context["task_id"]]["receipts"]
        receipt = written(path, raw, operation)
        receipt["confirmed_at"] = f"2026-11-18T00:00:00.{len(receipts):06d}+00:00"
        receipts.append(receipt)
    store.transaction(save)


def test_t3_forty_versions_keep_latest_ref_and_at_most_32_encrypted_copies(tmp_path):
    store, workspace, context, transfers = transfer_env(tmp_path)
    path = workspace / "report.txt"
    for version in range(40):
        raw = f"version {version}".encode()
        record(store, context, path, raw, f"op{version}")
        result = transfers.issue(context)
        assert result["artifacts"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
        refs = store.read()["tasks"][context["task_id"]]["artifact_transfers"]
        assert len(refs) <= 32
        assert len(list(transfers._task_dir(context["task_id"]).iterdir())) == len(refs)
    latest = result["artifacts"][0]
    assert transfers.resolve(context, latest["artifact_ref"], content=True)["bytes"] == b"version 39"


def test_t3_full_cache_evicts_earliest_expiry_without_returning_dead_references(tmp_path):
    store, workspace, context, transfers = transfer_env(tmp_path)
    for i in range(32):
        record(store, context, workspace / f"file{i}.txt", f"file{i}".encode(), f"op{i}")
    first = transfers.issue(context)
    old = next(item for item in first["artifacts"] if item["name"] == "file0.txt")
    record(store, context, workspace / "new.txt", b"new", "op33")
    second = transfers.issue(context)
    assert len(second["artifacts"]) == 32
    assert all(transfers.resolve(context, item["artifact_ref"]) for item in second["artifacts"])
    with pytest.raises(AgentError, match="agent_artifact_access_changed"):
        transfers.resolve(context, old["artifact_ref"])
    assert not transfers._path(context["task_id"], old["artifact_ref"]).exists()


def test_t4_large_transfer_skipped_without_reading_or_losing_other_results(tmp_path, monkeypatch):
    store, workspace, context, transfers = transfer_env(tmp_path)
    large = workspace / "large.pdf"
    with large.open("wb") as stream:
        stream.truncate(65 * MIB)
    store.transaction(lambda data: data["tasks"][context["task_id"]]["receipts"].append(written(large, b"receipt", "op1")))
    record(store, context, workspace / "ok.txt", b"ok", "op2")
    original = Path.read_bytes
    def no_large_read(path):
        assert path != large, "over-limit content must not be read into memory"
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", no_large_read)
    result = transfers.issue(context)
    assert result["skipped"] == [{"name": "large.pdf", "reason": "too_large"}]
    assert [item["name"] for item in result["artifacts"]] == ["ok.txt"]
    assert result["max_file_bytes"] == 64 * MIB
    row = store.read()["tasks"][context["task_id"]]
    file_id = wa.artifact_id(large)
    assert wa.open_stream(row, file_id)["size"] == 65 * MIB
    with pytest.raises(AgentError, match="agent_artifact_too_large"):
        wa.read(row, file_id)


def test_t4_total_transfer_bytes_bounded_and_successful_items_preserved(tmp_path, monkeypatch):
    assert artifact_transfers.MAX_TOTAL_TRANSFER_BYTES == 256 * MIB
    monkeypatch.setattr(artifact_transfers, "MAX_TOTAL_TRANSFER_BYTES", 256)
    store, workspace, context, transfers = transfer_env(tmp_path)
    for i in range(4):
        record(store, context, workspace / f"file{i}.txt", b"x" * 80, f"op{i}")
    result = transfers.issue(context)
    assert len(result["artifacts"]) == 3
    assert result["skipped"] == [{"name": "file3.txt", "reason": "too_large"}]
    refs = store.read()["tasks"][context["task_id"]]["artifact_transfers"]
    assert sum(item["size"] for item in refs.values()) == 240
    assert len(list(transfers._task_dir(context["task_id"]).iterdir())) == 3
    assert all(transfers.resolve(context, item["artifact_ref"], content=True)["bytes"] == b"x" * 80 for item in result["artifacts"])


def test_t4_twenty_mib_transfer_is_encrypted_and_resolvable(tmp_path):
    store, workspace, context, transfers = transfer_env(tmp_path)
    raw = b"x" * (20 * MIB)
    record(store, context, workspace / "report.pdf", raw, "op1")
    result = transfers.issue(context)
    assert result["skipped"] == [] and len(result["artifacts"]) == 1
    item = result["artifacts"][0]
    assert item["size"] == 20 * MIB and item["sha256"] == hashlib.sha256(raw).hexdigest()
    assert transfers.resolve(context, item["artifact_ref"], content=True)["bytes"] == raw
    assert b"x" * 1000 not in transfers._path(context["task_id"], item["artifact_ref"]).read_bytes()


def test_t5_real_twenty_mib_download_uses_file_response_and_cached_hash(tmp_path, monkeypatch):
    path = tmp_path / "report.pdf"
    raw = b"x" * (20 * MIB)
    path.write_bytes(raw)
    os.utime(path, (100, 100))
    digest = hashlib.sha256(raw).hexdigest()
    app = FastAPI()
    runtime = install_agent(app, store=object(), home=tmp_path / "home", workers=BackgroundWorkerRegistry(),
        local_control=lambda _: None, messaging_key=X25519PrivateKey.generate(), integrations=(Model(), Tools(), Catalog()))
    runtime.update_settings({"enabled": True, "model_id": "local:test"})
    task = runtime.create({"message": "Download report"})
    runtime.store.transaction(lambda data: data["tasks"][task["id"]].update(
        receipts=[written(path, raw, "op1")], local_execution={"workspace_root": str(tmp_path)}))
    with TestClient(app) as client:
        item = client.get(f"/api/local/agent/tasks/{task['id']}/artifacts").json()["files"][0]
        assert item["sha256"] == digest
        seen_preview = []
        def preview(name, content, **kwargs):
            seen_preview.append((name, len(content)))
            return {"kind": "document", "blocks": []}
        monkeypatch.setattr("rynmesh.agent_runtime.file_preview.preview_file", preview)
        preview_url = f"/api/local/agent/tasks/{task['id']}/files/{item['id']}/preview?v={digest}"
        assert client.get(preview_url).status_code == 200
        assert seen_preview == [(path.name, 20 * MIB)]
        original_read, original_open = Path.read_bytes, Path.open
        def no_whole_read(candidate):
            assert candidate != path, "download must stream"
            return original_read(candidate)
        def no_rehash(candidate, *args, **kwargs):
            assert candidate != path, "listing hash cache must be reused"
            return original_open(candidate, *args, **kwargs)
        monkeypatch.setattr(Path, "read_bytes", no_whole_read)
        monkeypatch.setattr(Path, "open", no_rehash)
        streamed = []
        class TrackedFileResponse(FileResponse):
            def __init__(self, *args, **kwargs):
                streamed.append(args[0] if args else kwargs["path"])
                super().__init__(*args, **kwargs)
        monkeypatch.setattr("fastapi.responses.FileResponse", TrackedFileResponse)
        url = f"/api/local/agent/tasks/{task['id']}/files/{item['id']}"
        response = client.get(url + "?v=" + digest)
        assert response.status_code == 200 and response.content == raw and streamed == [path]
        assert response.headers["etag"] == f'"{digest}"' and response.headers["x-ryn-artifact-sha256"] == digest
        assert response.headers["cache-control"] == "no-store" and "sandbox" in response.headers["content-security-policy"]
        assert client.get(url + "?v=" + "0" * 64).status_code == 409


def test_t5_unreceipted_and_outside_files_cannot_be_streamed(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"owned")
    row = row_for(tmp_path, [])
    with pytest.raises(AgentError, match="agent_artifact_not_found"):
        wa.open_stream(row, wa.artifact_id(path))
    outside = tmp_path.parent / "outside-i35.txt"
    outside.write_bytes(b"outside")
    row["receipts"].append(written(outside, b"outside", "op1"))
    with pytest.raises(AgentError, match="agent_artifact_unavailable"):
        wa.open_stream(row, wa.artifact_id(outside))
