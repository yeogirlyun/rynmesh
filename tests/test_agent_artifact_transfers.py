import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_agent_workspace_artifacts import written

from rynmesh.agent_runtime.artifact_transfers import ArtifactTransfers
from rynmesh.agent_runtime.store import AgentError, AgentStore


def test_reference_is_task_owned_immutable_and_expires_without_touching_original(tmp_path):
    store = AgentStore(tmp_path / "agent", X25519PrivateKey.generate())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file = workspace / "report.txt"
    file.write_bytes(b"approved content")
    (workspace / "unprovided.txt").write_bytes(b"not in a receipt")
    row = {"id": "task", "scope_revision": 1, "local_execution": {"workspace_root": str(workspace)},
           "receipts": [written(file, b"approved content", "op1")], "messages": [], "events": [], "status": "queued"}
    store.transaction(lambda data: data["tasks"].update({"task": row}))
    context = {"task_id": "task", "owner_id": "owner-A", "scope_revision": 1}
    clock = [1000]
    transfers = ArtifactTransfers(store, clock=lambda: clock[0])
    result = transfers.issue(context)
    assert len(result["artifacts"]) == 1
    artifact = result["artifacts"][0]
    assert artifact["name"] == "report.txt" and "path" not in artifact
    assert artifact["sha256"] == hashlib.sha256(b"approved content").hexdigest()
    file.write_bytes(b"changed original")
    assert transfers.resolve(context, artifact["artifact_ref"], content=True)["bytes"] == b"approved content"
    for denied in [{**context, "owner_id": "owner-B"}, {**context, "task_id": "other"}, {**context, "scope_revision": 2}]:
        with pytest.raises(AgentError):
            transfers.resolve(denied, artifact["artifact_ref"])
    clock[0] = artifact["expires_at"] + 1
    with pytest.raises(AgentError):
        transfers.resolve(context, artifact["artifact_ref"])
    transfers.prune(context)
    assert not (transfers.root / artifact["artifact_ref"]).exists()
    assert file.read_bytes() == b"changed original"
