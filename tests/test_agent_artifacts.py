"""Confirmed real DAV files remain readable through task bounds, never prose."""
import copy
from types import SimpleNamespace

import pytest
from test_agent_resource_writes import writable as writable_fixture

from rynmesh.agent_runtime import verification
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.agent_runtime.tools import RynTools
from rynmesh.services import peer_box

writable = writable_fixture


def created(writable, content="Actual saved report."):
    tools, _, _, nas, source, server, root, app, store = writable
    catalog = SimpleNamespace(list_models=lambda: [{"id": "fixture", "source": "local_api", "available": True,
        "agent_supported": True, "peer_id": "local", "source_id": "fixture", "destination_revision": "rev1"}])
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    runtime = AgentRuntime(AgentStore(store.home / "agent", key), SimpleNamespace(supports_destination_binding=True),
                           tools, catalog, consumer_node_id="local")
    runtime.update_settings({"enabled": True, "model_id": "fixture"})
    task = runtime.create({"message": "Create a report", "expected_destination_revision": "rev1", "scope": {
        "resources": [{"connection_id": source["id"], "root_path": "allowed", "recursive": True,
                       "actions": ["list", "read_ai", "create_file"]}], "allow_model_data": True}})
    row = runtime.store.read()["tasks"][task["id"]]
    context = runtime._execution_context(row)
    args = {"directory_ref": context["resource_grants"][0]["root_resource_ref"], "name": "report.md", "content": content}
    preview = tools.preview("ryn_create_text_file", args, context=context)
    result = tools.execute("ryn_create_text_file", args, operation_id="op-artifact", context=context,
                           expected_preview=preview, cancelled=lambda: False)
    def save(data):
        row = data["tasks"][task["id"]]
        call = {"operation_id": "op-artifact", "name": "ryn_create_text_file", "arguments": args, "effect": "write",
                "scope_revision": row["scope_revision"], "goal_revision": row["goal"]["revision"], "context_epoch": row.get("context_epoch", 0)}
        verification.record(row, call, "confirmed", "2026-10-11", result=result, flight={"preview": preview})
        row.update(status="failed", error="agent_time_limit", result="Model prose is not an artifact")
    runtime.store.transaction(save)
    return runtime, task["id"], args, key


def test_failed_task_historical_receipt_opens_after_restart_without_another_write(writable):
    runtime, tid, args, key = created(writable)
    _, _, _, _, _, server, _, app, store = writable
    runtime = AgentRuntime(AgentStore(store.home / "agent", key), runtime.model, RynTools(app, store, {}, None), runtime.catalog, consumer_node_id="local")
    items = runtime.artifacts(tid)["artifacts"]
    assert len(items) == 1 and items[0]["id"] == "op-artifact" and items[0]["name"] == "report.md"
    assert "resource_ref" not in items[0]
    for _ in range(2):
        assert runtime.read_artifact(tid, "op-artifact")["text"] == args["content"]
    assert len([e for e in server.audit if e["method"] == "PUT"]) == 1
    assert runtime.get(tid)["status"] == "failed"


@pytest.mark.parametrize("change", ["forged_result", "cross_task", "read_revoked", "connection_revoked", "content_changed"])
def test_artifact_rejects_forgery_cross_task_and_current_revocation(writable, change):
    runtime, tid, _, _ = created(writable)
    _, _, _, nas, source, server, root, _, _ = writable
    if change == "content_changed":
        (root / "allowed/report.md").write_text("Changed by another actor", encoding="utf-8")
    elif change == "connection_revoked":
        nas.update_source(source["id"], {"allow_ai": False})
    else:
        def alter(data):
            row = data["tasks"][tid]
            if change == "forged_result":
                row["receipts"][0]["result"]["name"] = "other.md"
            elif change == "cross_task":
                row = copy.deepcopy(row); row["id"] = "other-task"; data["tasks"]["other-task"] = row
            else:
                row["scope"]["resource_grants"][0]["actions"].remove("read_ai")
        runtime.store.transaction(alter)
    target = "other-task" if change == "cross_task" else tid
    before = len(server.audit)
    with pytest.raises(AgentError):
        runtime.read_artifact(target, "op-artifact")
    assert not any(e["mutation"] for e in server.audit[before:])
    if change in {"forged_result", "cross_task"}:
        assert runtime.artifacts(target)["artifacts"] == []


def test_scope_change_while_file_read_discards_all_content(writable, monkeypatch):
    runtime, tid, _, _ = created(writable)
    nas = writable[3]
    original = nas.connection
    from contextlib import contextmanager
    @contextmanager
    def connection(*args, **kwargs):
        with original(*args, **kwargs) as real:
            read = real.read
            def changed(path, limit):
                raw = read(path, limit)
                runtime.store.transaction(lambda data: data["tasks"][tid].update(control_revision=999))
                return raw
            monkeypatch.setattr(real, "read", changed)
            yield real
    monkeypatch.setattr(nas, "connection", connection)
    with pytest.raises(AgentError, match="agent_artifact_access_changed"):
        runtime.read_artifact(tid, "op-artifact")


def test_appended_content_at_maximum_file_size_is_not_hidden_by_read_limit(writable):
    from rynmesh.agent_runtime.resource_writes import MAX_TEXT_BYTES
    runtime, tid, _, _ = created(writable, "x" * MAX_TEXT_BYTES)
    with (writable[6] / "allowed/report.md").open("ab") as output:
        output.write(b"changed")
    with pytest.raises(AgentError):
        runtime.read_artifact(tid, "op-artifact")


@pytest.mark.parametrize("change", ["revision", "receipt"])
def test_revocation_during_final_resource_validation_discards_content(writable, monkeypatch, change):
    runtime, tid, _, _ = created(writable)
    resources = runtime.tools.resource_writes.resources
    original = resources._resolve
    calls = 0

    def resolve(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 2:
            def revoke(data):
                row = data["tasks"][tid]
                if change == "revision":
                    row["control_revision"] += 1
                else:
                    row["receipts"] = []
            runtime.store.transaction(revoke)
        return result

    monkeypatch.setattr(resources, "_resolve", resolve)
    with pytest.raises(AgentError, match="agent_artifact_access_changed"):
        runtime.read_artifact(tid, "op-artifact")
    assert calls == 2
