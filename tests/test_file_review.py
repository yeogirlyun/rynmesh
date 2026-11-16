"""S05-I11 acceptance tests: FileReview snapshot degradation, undo skipping, and legacy manifest compatibility."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from rynmesh.agent_runtime import file_review
from rynmesh.agent_runtime.file_review import FileReview, digest, fingerprint
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.routes import install_agent
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.services import peer_box


def _make_review_env(tmp_path: Path):
    from test_agent_local_tools import execute
    from test_agent_runtime import Catalog, Model, Tools

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)

    app, adapter, model = FastAPI(), Tools(), Model()

    def owner(request):
        if request.headers.get("x-test-owner") != "yes":
            raise HTTPException(403, "local_control_required")

    runtime = install_agent(
        app,
        store=object(),
        home=home,
        workers=BackgroundWorkerRegistry(),
        local_control=owner,
        messaging_key=peer_box.load_or_create_messaging_key(home / "messaging.x25519"),
        integrations=(model, adapter, Catalog()),
    )
    ledger = OperationLedger(home)
    local = LocalTools(SimpleNamespace(home=home), lambda: ledger)
    local.store = runtime.store
    adapter.local = local
    adapter.change_review = local.change_review
    runtime.update_settings({"enabled": True, "model_id": "local:test", "workspace_root": str(workspace)})
    task = runtime.create({"message": "S05-I11 review"})
    runtime.store.transaction(lambda data: data["tasks"][task["id"]].update(status="paused"))
    context = {"task_id": task["id"], "local_execution": runtime.store.read()["tasks"][task["id"]]["local_execution"]}
    client = TestClient(app, headers={"x-test-owner": "yes"})
    review = local.file_review(task["id"], str(workspace))

    def write_file_tool(rel_path: str, content: str):
        return execute(local, context, "ryn_file_write", {"path": rel_path, "content": content}, uuid.uuid4().hex)

    return SimpleNamespace(
        app=app,
        runtime=runtime,
        local=local,
        workspace=workspace,
        task_id=task["id"],
        context=context,
        client=client,
        review=review,
        write=write_file_tool,
    )


def test_s05_i11_t1_65th_path_writes_successfully_and_marks_cannot_undo(tmp_path):
    """T1: 65th distinct path succeeds without snapshot blob and marks can_undo=False (snapshot_skipped_limit)."""
    env = _make_review_env(tmp_path)
    try:
        for idx in range(1, 66):
            env.write(f"file_{idx:03d}.txt", f"content-{idx}\n")

        assert (env.workspace / "file_065.txt").read_text(encoding="utf-8") == "content-65\n"

        rev = env.review.review()
        assert rev["snapshot_skipped"] == 1
        by_path = {item["path"]: item for item in rev["files"]}
        assert by_path["file_001.txt"]["can_undo"] is True
        assert by_path["file_064.txt"]["can_undo"] is True
        assert by_path["file_065.txt"]["can_undo"] is False
        assert by_path["file_065.txt"]["problem"] == "snapshot_skipped_limit"

        # Verify no blob was persisted for file_065's content.
        sha_65 = digest(b"content-65\n")
        assert not (env.review.directory / f"{sha_65}.blob").exists()
    finally:
        env.local.processes.close()
        env.client.close()


def test_s05_i11_t2_8mib_and_single_file_and_op_limits_degrade_to_hash_only(tmp_path, monkeypatch):
    """T2: 8 MiB task limit (plus single-file & operation limits) writes file with snapshot=False; only manifest save failure rejects."""
    env = _make_review_env(tmp_path)
    try:
        # 1) Write four ~2 MiB files via FileReview (just under 8 MiB budget), then a 5th file pushing past 8 MiB MAX_BYTES.
        chunk_2m = b"A" * (2 * 1024 * 1024 - 16384)
        for idx in range(4):
            payload = chunk_2m + f"-{idx:02d}".encode("ascii")
            env.review.apply([(env.workspace / f"blob_{idx}.bin", None, payload, None)], operation_id=f"op_b{idx}")

        fifth_payload = b"E" * 40000
        env.review.apply([(env.workspace / "over_8m.txt", None, fifth_payload, None)], operation_id="op_over_8m")
        assert (env.workspace / "over_8m.txt").read_bytes() == fifth_payload
        assert not (env.review.directory / f"{digest(fifth_payload)}.blob").exists()

        # 2) Single-file > 2 MiB also degrades cleanly without writing blob.
        oversized_single = b"B" * (2 * 1024 * 1024 + 128)
        env.review.apply([(env.workspace / "over_2m_single.bin", None, oversized_single, None)], operation_id="op_over_2m")
        assert (env.workspace / "over_2m_single.bin").read_bytes() == oversized_single

        rev = env.review.review()
        assert rev["snapshot_skipped"] == 2
        by_path = {item["path"]: item for item in rev["files"]}
        assert by_path["over_8m.txt"]["can_undo"] is False
        assert by_path["over_8m.txt"]["problem"] == "snapshot_skipped_limit"
        assert by_path["over_2m_single.bin"]["can_undo"] is False
        assert by_path["over_2m_single.bin"]["problem"] == "snapshot_skipped_limit"

        # 3) Operation count limit merges excess operations into a single unsnapshotted summary record.
        monkeypatch.setattr(file_review, "MAX_OPERATIONS", 6)
        env.review.apply([(env.workspace / "op_overflow_1.txt", None, b"ov1", None)], operation_id="op_ov1")
        env.review.apply([(env.workspace / "op_overflow_2.txt", None, b"ov2", None)], operation_id="op_ov2")
        assert (env.workspace / "op_overflow_1.txt").read_bytes() == b"ov1"
        assert (env.workspace / "op_overflow_2.txt").read_bytes() == b"ov2"
        state = env.review.load()
        overflow_records = [r for r in state["records"] if r.get("overflow_summary")]
        assert len(overflow_records) == 1
        assert len(state["records"]) == 7  # 6 normal + 1 merged overflow summary

        # 4) Only when manifest itself cannot be saved before mutation does apply reject without mutating disk.
        def broken_save(_self, _state):
            raise OSError("disk full writing manifest")

        monkeypatch.setattr(FileReview, "save", broken_save)
        with pytest.raises(AgentToolRejected):
            env.review.apply([(env.workspace / "never_written.txt", None, b"nope", None)], operation_id="op_fail_manifest")
        assert not (env.workspace / "never_written.txt").exists()
    finally:
        env.local.processes.close()
        env.client.close()


def test_s05_i11_t3_undo_all_skips_non_undoable_items_and_reports_skipped(tmp_path, monkeypatch):
    """T3: Undo all restores snapshotted files, skips snapshot:False items and reports them; active task pause check remains."""
    env = _make_review_env(tmp_path)
    try:
        monkeypatch.setattr(file_review, "MAX_FILES", 2)
        (env.workspace / "existing.txt").write_bytes(b"original\n")
        env.write("existing.txt", "modified\n")
        env.write("created_ok.txt", "new-ok\n")
        # 3rd distinct path exceeds MAX_FILES=2 -> snapshot:False
        env.write("created_skipped.txt", "keep-me\n")

        # Step 5 check: while task is running, undo/preview still requires pausing first.
        env.runtime.store.transaction(lambda d: d["tasks"][env.task_id].update(status="running"))
        busy_resp = env.client.post(f"/api/local/agent/tasks/{env.task_id}/changes/preview", json={})
        assert busy_resp.status_code == 409
        assert busy_resp.json()["detail"] == "agent_review_pause_task_first"
        env.runtime.store.transaction(lambda d: d["tasks"][env.task_id].update(status="paused"))

        prev_resp = env.client.post(f"/api/local/agent/tasks/{env.task_id}/changes/preview", json={"ids": None})
        assert prev_resp.status_code == 200
        prev = prev_resp.json()
        assert prev["can_undo"] is True
        assert any(s["path"] == "created_skipped.txt" and s["reason"] == "snapshot_skipped_limit" for s in prev["skipped"])

        undo_resp = env.client.post(
            f"/api/local/agent/tasks/{env.task_id}/changes/undo",
            json={"ids": None, "token": prev["token"], "confirm": True, "request_id": "undo-all-1"},
        )
        assert undo_resp.status_code == 200
        result = undo_resp.json()
        assert result["status"] == "completed"
        assert set(result["completed"]) == {"existing.txt", "created_ok.txt"}
        assert any(s["path"] == "created_skipped.txt" and s["reason"] == "snapshot_skipped_limit" for s in result["skipped"])

        # Snapshotted files were restored; non-snapshotted file stays intact.
        assert (env.workspace / "existing.txt").read_bytes() == b"original\n"
        assert not (env.workspace / "created_ok.txt").exists()
        assert (env.workspace / "created_skipped.txt").read_bytes() == b"keep-me\n"
    finally:
        env.local.processes.close()
        env.client.close()


def test_s05_i11_t4_legacy_manifest_without_snapshot_field_is_treated_as_undoable(tmp_path):
    """T4: Legacy v2 manifest entries without 'snapshot' field remain undoable and report snapshot_skipped=0."""
    env = _make_review_env(tmp_path)
    try:
        target = env.workspace / "legacy.txt"
        before_raw = b"legacy-before\n"
        after_raw = b"legacy-after\n"
        env.review.directory.mkdir(parents=True, exist_ok=True)
        before_meta = env.review.blob(before_raw)
        after_meta = env.review.blob(after_raw)
        target.write_bytes(after_raw)
        after_stamp = fingerprint(target)

        # Write a legacy v2 manifest where step has NO 'snapshot' key at all.
        legacy_state = {
            "version": 2,
            "root": str(env.workspace),
            "records": [
                {
                    "id": "legacy_op_1",
                    "steps": [
                        {
                            "path": str(target),
                            "before": before_meta,
                            "after": after_meta,
                            "mode": 0o644,
                            "group": None,
                            "state": "applied",
                            "before_stamp": [0, 0, len(before_raw), 1],
                            "after_stamp": after_stamp,
                            "order": [0, 0],
                        }
                    ],
                }
            ],
            "undos": [],
        }
        assert "snapshot" not in legacy_state["records"][0]["steps"][0]
        env.review.manifest.write_text(json.dumps(legacy_state), encoding="utf-8")

        rev = env.review.review()
        assert rev["snapshot_skipped"] == 0
        assert len(rev["files"]) == 1
        assert rev["files"][0]["path"] == "legacy.txt"
        assert rev["files"][0]["can_undo"] is True

        prev = env.review.preview()
        assert prev["can_undo"] is True
        res = env.review.undo(None, prev["token"], "legacy-undo-1")
        assert res["status"] == "completed"
        assert res["completed"] == ["legacy.txt"]
        assert target.read_bytes() == before_raw
    finally:
        env.local.processes.close()
        env.client.close()
