"""Independent stage-1 rejection boundaries on the real encrypted AgentStore.

No product monkey-import, no implementation code copied, and no skips for a
missing guard. Root runs this only after stage1-ready.json / import repair.
"""
from __future__ import annotations

import copy
import inspect
import os
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
TARGET = Path(os.environ.get("AGENT_MEMORY_TARGET", str(Path(__file__).resolve().parents[2]))).resolve()
ACCEPTANCE = Path(__file__).resolve().parent
sys.path.insert(0, str(TARGET / "tests"))
sys.path.insert(0, str(TARGET))

import pytest  # noqa: E402 - imports use the checkout selected above.
from test_agent_runtime import make_runtime  # noqa: E402 - imports use the checkout selected above.

from rynmesh.agent_runtime import (  # noqa: E402 - imports use the checkout selected above.
    knowledge,
    memory,
)
from rynmesh.agent_runtime.store import (  # noqa: E402 - imports use the checkout selected above.
    AgentError,
    AgentStore,
)

assert Path(memory.__file__).resolve().is_relative_to(TARGET), "Imported memory.py from wrong checkout"

DEST_A = {"catalog_model_id": "local:test", "provider_peer_id": "audit-node",
          "source_id": "audit-source-a", "destination_revision": "audit-rev-a"}
DEST_B = {"catalog_model_id": "local:other", "provider_peer_id": "audit-node",
          "source_id": "audit-source-b", "destination_revision": "audit-rev-b"}
PROJECT = {"id": "audit-project-stable-id", "generation": 1}
SCOPE = {"kind": "project", "project_id": PROJECT["id"], "project_generation": 1}


@pytest.fixture
def sandbox():
    parent = ACCEPTANCE / "sandbox"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="stage1-boundary-", dir=parent) as directory:
        path = Path(directory).resolve()
        assert path.is_relative_to(ACCEPTANCE), "Unexpected cleanup target"
        runtime, _, _, key = make_runtime(path)
        yield runtime, key


def owner_source(runtime, *, text="I own AUDIT_CAMERA_SENTINEL", project=None, destination=None):
    """Use existing runtime task initialization to obtain real owner message IDs."""
    created = runtime.create({"message": text})
    def attach_fixture_context(data):
        row = data["tasks"][created["id"]]
        row["status"] = "completed"
        row["model_destination"] = copy.deepcopy(destination or DEST_A)
        if project is not None:
            row["project"] = copy.deepcopy(project)
        return row
    row = runtime.store.transaction(attach_fixture_context)
    message = row["messages"][0]
    assert message["role"] == "user" and message["message_id"]
    return row, message


def enable_capture(store):
    return store.transaction(lambda data: memory.update_memory_settings(
        data, {"capture_enabled": True, "extraction_destination": DEST_A}))


def fence_arguments(lease, *, required=False):
    """Bind an opaque token or an equivalent explicit per-attempt generation.

    API names may change while Gemini writes stage 1. A missing per-attempt
    contract is a test failure, not an implicit exemption for the lease case.
    """
    params = inspect.signature(memory.commit_extraction_job).parameters
    token_contracts = {
        "lease_token": ("lease_token",),
        "expected_lease_token": ("lease_token",),
        "ownership_token": ("ownership_token",),
        "expected_ownership_token": ("ownership_token",),
        "attempt_token": ("attempt_token",),
        "expected_attempt_token": ("attempt_token",),
        "lease_id": ("lease_id",),
    }
    for arg, fields in token_contracts.items():
        for field in fields:
            if arg in params and lease.get(field) is not None:
                return {arg: lease[field]}
    for arg in ("expected_attempt", "expected_attempts", "lease_attempt", "attempt"):
        if arg in params and lease.get("attempts") is not None:
            return {arg: lease["attempts"]}
    assert not required, "claim/commit has no caller-bound per-attempt fencing identity"
    return {}


def commit_claim(store, lease, proposals, *, required_fence=False):
    return store.transaction(lambda data: memory.commit_extraction_job(
        data, job_id=lease["id"], worker_id=lease["lease_owner"],
        proposals=proposals, **fence_arguments(lease, required=required_fence)))


def make_candidate(store, row, message, *, fact_key="audit.camera", text="AUDIT_CAMERA_SENTINEL"):
    queued = store.transaction(lambda data: memory.enqueue_extraction_job(data, task_id=row["id"]))
    assert queued["queued"] is True
    lease = store.transaction(lambda data: memory.claim_extraction_job(
        data, worker_id="audit-worker", job_id=queued["job"]["id"]))
    assert lease, "Eligible source could not be claimed (positive control)"
    committed = commit_claim(store, lease, [{"category": "photography", "fact_key": fact_key,
        "text": text, "source_message_ids": [message["message_id"]]}])
    assert committed["state"] == "committed", committed
    candidates = committed["committed_candidates"]
    assert len(candidates) == 1
    candidate = candidates[0]
    # Current API returns record dictionaries. Fail clearly if a new public
    # contract returns only IDs, so root can bind that interface explicitly.
    assert isinstance(candidate, dict) and candidate.get("id"), "Candidate result lacks record identity"
    return candidate


@pytest.mark.parametrize("prior_state", ["never_extracted", "previously_forgotten"])
def test_scoped_reset_persistently_fences_all_old_project_sources(sandbox, prior_state):
    runtime, key = sandbox
    store = runtime.store
    enable_capture(store)
    row, _ = owner_source(runtime, project=PROJECT)
    if prior_state == "previously_forgotten":
        # Manual owner fact and an independent old chat can describe the same
        # slot. Deleting the manual fact must not require that every old chat
        # had already been extracted and attached as its source.
        made = store.transaction(lambda data: memory.upsert_active_entry(data,
            {"scope": SCOPE, "category": "photography", "fact_key": "project.gear.camera",
             "text": "AUDIT_CAMERA_SENTINEL", "exposure_policy": {"mode": "all_selected"}}, origin="manual"))
        store.transaction(lambda data: memory.delete_memory_entry(data, made["entry"]["id"]))
    store.transaction(lambda data: memory.reset_memory(data, scope=SCOPE))
    restored = AgentStore(store.root, key)
    try:
        queued = restored.transaction(lambda data: memory.enqueue_extraction_job(data, task_id=row["id"], force_relearn=False))
    except AgentError:
        queued = {"queued": False}
    assert queued["queued"] is False, "Project reset allowed a pre-reset source to be re-enqueued"
    assert restored.inspect(memory.list_memories_snapshot)["entries"] == []
    # Reset must not amount to disabling capture for all future project chats.
    fresh, _ = owner_source(runtime, text="New post-reset AUDIT_LENS_SENTINEL", project=PROJECT)
    fresh_queue = restored.transaction(lambda data: memory.enqueue_extraction_job(data, task_id=fresh["id"]))
    assert fresh_queue["queued"] is True, "New post-reset source was incorrectly blocked"


def test_scoped_reset_cannot_accept_a_candidate_from_before_reset(sandbox):
    runtime, key = sandbox
    enable_capture(runtime.store)
    row, message = owner_source(runtime, project=PROJECT)
    candidate = make_candidate(runtime.store, row, message)
    runtime.store.transaction(lambda data: memory.reset_memory(data, scope=SCOPE))
    restored = AgentStore(runtime.store.root, key)
    try:
        response = restored.transaction(lambda data: memory.decide_candidate(data,
            candidate["id"], decision="accept", expected_candidate_revision=candidate["revision"]))
    except AgentError:
        response = None
    # Not-found or a rejected/stale decision are acceptable. A TypeError or a
    # silent missing API is not swallowed as a successful rejection.
    assert response is None or response.get("accepted") is not True, response
    assert restored.inspect(memory.list_memories_snapshot)["entries"] == [], "Old candidate reactivated a reset project fact"


def test_same_project_id_with_new_generation_cannot_read_old_generation_memory(sandbox):
    runtime, _ = sandbox
    store = runtime.store
    made = store.transaction(lambda data: memory.upsert_active_entry(data,
        {"scope": SCOPE, "category": "photography", "fact_key": "project.gear.camera",
         "text": "GEN1_CAMERA_SENTINEL", "exposure_policy": {"mode": "destinations", "allowed_destinations": [DEST_A]}}, origin="manual"))
    old_task, _ = owner_source(runtime, project=PROJECT)
    new_task, _ = owner_source(runtime, project={**PROJECT, "generation": 2})
    old = store.inspect(lambda data: memory.select_memories_for_task(data, data["tasks"][old_task["id"]], query_text="camera"))
    new = store.inspect(lambda data: memory.select_memories_for_task(data, data["tasks"][new_task["id"]], query_text="camera"))
    assert made["entry"]["id"] in {item["id"] for item in old["selected"]}, "Positive-control old generation not selected"
    assert made["entry"]["id"] not in {item["id"] for item in new["selected"]}, "Project generation changed but old memory remained eligible"
    assert "GEN1_CAMERA_SENTINEL" not in str(new)


@pytest.mark.parametrize("denied_destination", [DEST_B, {**DEST_A, "destination_revision": "changed-config"}, None])
def test_candidate_search_enforces_destination_as_strictly_as_active_entries(sandbox, denied_destination):
    runtime, _ = sandbox
    enable_capture(runtime.store)
    row, message = owner_source(runtime, text="I use CANDIDATE_PRIVATE_CAMERA_SENTINEL")
    candidate = make_candidate(runtime.store, row, message, text="CANDIDATE_PRIVATE_CAMERA_SENTINEL")
    allowed = runtime.store.inspect(lambda data: memory.search_memories(data,
        include_candidates=True, destination=DEST_A, enforce_destination=True))
    denied = runtime.store.inspect(lambda data: memory.search_memories(data,
        include_candidates=True, destination=denied_destination, enforce_destination=True))
    assert candidate["id"] in {item["id"] for item in allowed["candidates"]}, "Positive-control candidate missing at its allowed destination"
    assert candidate["id"] not in {item["id"] for item in denied["candidates"]}
    assert "CANDIDATE_PRIVATE_CAMERA_SENTINEL" not in str(denied), "Candidate search leaked another destination's text/source excerpt"


def test_unrelated_preference_edit_does_not_remove_existing_exposure_restriction(sandbox):
    runtime, key = sandbox
    store = runtime.store
    runtime.update_preferences({"expected_revision": 0, "items": [
        {"id": "private-pref", "text": "PRIVATE_PREFERENCE_SENTINEL"}]})
    store.transaction(lambda data: memory.set_manual_preference_exposure(data, "private-pref",
        expected_preferences_revision=1, exposure_policy={"mode": "destinations", "allowed_destinations": [DEST_A]}))
    query, _ = owner_source(runtime, destination=DEST_B)
    before = store.inspect(lambda data: memory.filter_manual_preferences_for_task(data,
        data["tasks"][query["id"]], knowledge.preferences(data)))
    assert "private-pref" not in {item["id"] for item in before["items"]}
    runtime.update_preferences({"expected_revision": 1, "items": [
        {"id": "private-pref", "text": "PRIVATE_PREFERENCE_SENTINEL"},
        {"id": "unrelated-pref", "text": "An unrelated new preference"}]})
    restored = AgentStore(store.root, key)
    after = restored.inspect(lambda data: memory.filter_manual_preferences_for_task(data,
        data["tasks"][query["id"]], knowledge.preferences(data)))
    assert "private-pref" not in {item["id"] for item in after["items"]}, "Unrelated global revision made a private preference fail open"
    assert "PRIVATE_PREFERENCE_SENTINEL" not in str(after)
    # The owner API itself must retain its legacy exact payload and contents.
    raw = restored.inspect(knowledge.preferences)
    assert raw["revision"] == 2 and len(raw["items"]) == 2


def test_same_worker_reclaim_rejects_old_attempt_without_poisoning_new_lease(sandbox, monkeypatch):
    runtime, _ = sandbox
    store = runtime.store
    enable_capture(store)
    clock = [2000000000.0]
    monkeypatch.setattr(memory, "_now", lambda: clock[0])
    row, message = owner_source(runtime, text="My camera is AUDIT_LEASE_CAMERA_SENTINEL")
    queued = store.transaction(lambda data: memory.enqueue_extraction_job(data, task_id=row["id"]))
    first = store.transaction(lambda data: memory.claim_extraction_job(data,
        worker_id="same-logical-worker", job_id=queued["job"]["id"], lease_seconds=1))
    assert first
    first_fence = fence_arguments(first, required=True)
    clock[0] += 2
    second = store.transaction(lambda data: memory.claim_extraction_job(data,
        worker_id="same-logical-worker", job_id=first["id"], lease_seconds=10))
    assert second and second["attempts"] > first["attempts"]
    second_fence = fence_arguments(second, required=True)
    assert first_fence != second_fence, "A reclaimed attempt did not get a new fencing identity"
    before = store.inspect(lambda data: copy.deepcopy(data["memory"]["jobs"][first["id"]]))
    proposals = [{"category": "photography", "fact_key": "audit.lease.camera",
                  "text": "AUDIT_LEASE_CAMERA_SENTINEL", "source_message_ids": [message["message_id"]]}]
    try:
        stale = commit_claim(store, first, proposals, required_fence=True)
    except AgentError:
        stale = None
    assert stale is None or stale.get("state") not in {"committed", "no_op"}, "Old attempt committed under the reused worker ID"
    after = store.inspect(lambda data: copy.deepcopy(data["memory"]["jobs"][first["id"]]))
    assert after == before, "Rejecting an old attempt changed/poisoned the current worker lease"
    accepted = commit_claim(store, second, proposals, required_fence=True)
    assert accepted["state"] == "committed", accepted
    assert len(accepted["committed_candidates"]) == 1
