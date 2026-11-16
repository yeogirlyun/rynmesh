"""S13-I54: encrypted transfer copies and real node-start cleanup."""
import hashlib
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from fastapi import FastAPI
from test_agent_runtime import Catalog, Model, Tools
from test_agent_workspace_artifacts import written

from rynmesh.agent_runtime.artifact_transfers import ArtifactTransfers
from rynmesh.agent_runtime.routes import install_agent
from rynmesh.agent_runtime.store import AgentError
from rynmesh.agent_runtime.tools import RynTools
from rynmesh.background_workers import BackgroundWorkerRegistry

RAW = b"private attachment content S13-I54"


def start(home, key):
    return install_agent(FastAPI(), store=object(), home=home, workers=BackgroundWorkerRegistry(),
                         local_control=lambda _: None, messaging_key=key,
                         integrations=(Model(), Tools(), Catalog()))


@pytest.fixture
def transfer(tmp_path):
    key = X25519PrivateKey.generate()
    runtime = start(tmp_path, key)
    runtime.update_settings({"enabled": True})
    task = runtime.create({"message": "prepare attachment", "model_id": "local:test"})
    file = tmp_path / "workspace" / "report.txt"
    file.parent.mkdir()
    file.write_bytes(RAW)
    def prepare(data):
        row = data["tasks"][task["id"]]
        row["local_execution"] = {"workspace_root": str(file.parent)}
        row["receipts"] = [written(file, RAW, "op1")]
    runtime.store.transaction(prepare)
    row = runtime.store.inspect(lambda data: data["tasks"][task["id"]])
    context = {"task_id": row["id"], "owner_id": "local_owner", "scope_revision": row["scope_revision"]}
    return runtime.store, context, key


def copy_path(store, context, ref):
    return store.root / "artifact-transfers" / hashlib.sha256(context["task_id"].encode()).hexdigest()[:24] / ref


def test_t1_copy_is_encrypted_and_task_scoped(transfer):
    store, context, _ = transfer
    ref = ArtifactTransfers(store).issue(context)["artifacts"][0]["artifact_ref"]
    path = copy_path(store, context, ref)
    assert path.is_file()
    assert RAW not in path.read_bytes()
    assert b"ciphertext" in path.read_bytes()


def test_t2_forget_task_removes_directory_through_tools(transfer):
    store, context, _ = transfer
    ref = ArtifactTransfers(store).issue(context)["artifacts"][0]["artifact_ref"]
    directory = copy_path(store, context, ref).parent
    tools = RynTools.__new__(RynTools)
    tools.store = object()
    for name in ("resource_writes", "source_drafts", "friend_sharing", "local", "craft", "extensions", "code_mode"):
        setattr(tools, name, Mock())
    tools.local.store = store
    import threading
    tools.lock, tools.completed = threading.RLock(), {}
    tools.forget_task(context["task_id"], operation_ids=[])
    assert not directory.exists()
    assert not list((store.root / "artifact-transfers").rglob(ref))


def test_t3_crash_before_transaction_commit_is_swept_on_start(transfer, tmp_path):
    store, context, key = transfer
    secret = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
    script = '''
import os, sys
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.agent_runtime.artifact_transfers import ArtifactTransfers
store = AgentStore(sys.argv[1], X25519PrivateKey.from_private_bytes(bytes.fromhex(sys.argv[2])))
real = store.transaction
calls = 0
def crash(callback):
    global calls
    calls += 1
    if calls == 2:
        os._exit(73)
    return real(callback)
store.transaction = crash
ArtifactTransfers(store).issue({"task_id": sys.argv[3], "owner_id": "local_owner", "scope_revision": int(sys.argv[4])})
'''
    result = subprocess.run([sys.executable, "-c", script, str(store.root), secret, context["task_id"], str(context["scope_revision"])])
    assert result.returncode == 73
    assert len(list((store.root / "artifact-transfers").rglob("artifact_*"))) == 1
    start(tmp_path, key)
    assert not list((store.root / "artifact-transfers").rglob("artifact_*"))


def test_t4_legacy_plaintext_removed_on_node_start(transfer, tmp_path):
    store, _, key = transfer
    legacy = store.root / "artifact-transfers" / ("artifact_" + "a" * 48)
    legacy.parent.mkdir()
    legacy.write_bytes(RAW)
    start(tmp_path, key)
    assert not legacy.exists()


def test_t5_resolve_preserves_original_and_checks_integrity(transfer):
    store, context, _ = transfer
    transfers = ArtifactTransfers(store)
    ref = transfers.issue(context)["artifacts"][0]["artifact_ref"]
    assert transfers.resolve(context, ref, content=True)["bytes"] == RAW
    path = copy_path(store, context, ref)
    path.write_text('{"nonce":"bad","ciphertext":"bad"}')
    with pytest.raises(AgentError, match="agent_artifact_content_changed"):
        transfers.resolve(context, ref, content=True)


def test_startup_sweeps_expired_unlisted_and_deleted_task_copies(transfer, tmp_path):
    store, context, key = transfer
    transfers = ArtifactTransfers(store)
    ref = transfers.issue(context)["artifacts"][0]["artifact_ref"]
    valid = copy_path(store, context, ref)
    unlisted = valid.parent / ("artifact_" + "b" * 48)
    unlisted.parent.mkdir(parents=True, exist_ok=True)
    unlisted.write_bytes(b"orphan")
    missing_task = store.root / "artifact-transfers" / ("c" * 24) / ("artifact_" + "c" * 48)
    missing_task.parent.mkdir()
    missing_task.write_bytes(b"deleted task")
    start(tmp_path, key)
    assert valid.exists()
    assert not unlisted.exists()
    assert not missing_task.parent.exists()
    def expire(data):
        data["tasks"][context["task_id"]]["artifact_transfers"][ref]["expires_at"] = int(time.time()) - 1
    store.transaction(expire)
    start(tmp_path, key)
    assert not valid.exists()


def test_task_path_rejects_traversal(transfer):
    store, context, _ = transfer
    with pytest.raises(AgentError, match="agent_artifact_not_found"):
        ArtifactTransfers(store)._path(context["task_id"], "../outside")




def test_legacy_grant_can_be_reissued_after_upgrade(transfer, tmp_path):
    store, context, key = transfer
    transfers = ArtifactTransfers(store)
    ref = transfers.issue(context)["artifacts"][0]["artifact_ref"]
    copy_path(store, context, ref).unlink()
    legacy = transfers.root / ref
    legacy.write_bytes(RAW)
    start(tmp_path, key)
    assert not legacy.exists()
    replacement = transfers.issue(context)["artifacts"][0]["artifact_ref"]
    assert replacement != ref
    assert transfers.resolve(context, replacement, content=True)["bytes"] == RAW
