"""Construct Q04's prerequisite from an actual failed loopback HTTP read.

Setup uses a deterministic planner, not a scored model. The failed operation is
real. After its receipt is durable the harness ends setup with that failure code,
so the evaluated model only diagnoses an existing task and cannot rerun it.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid

import httpx

from rynmesh.agent_runtime.store import encoded

TARGET_ID = "task-failed-read"


class PreparationModel:
    supports_destination_binding = True
    supports_output_token_limit = True

    def complete(self, model_id, messages, specs, cancelled, on_event=None, **options):
        from importlib import import_module
        helper = import_module("docs.acceptance.ryn-agent-delivery.real-model.planner_discovery")
        business_history, discover = helper.business_history, helper.discover
        discovery = discover(specs, ['ryn_list_resource_roots', 'ryn_list_resources', 'ryn_read_resource'])
        if discovery:
            return discovery
        messages = business_history(messages)
        receipts = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
        if not receipts:
            name, args = "ryn_list_resource_roots", {}
        elif len(receipts) == 1:
            name, args = "ryn_list_resources", {"resource_ref": receipts[-1]["roots"][0]["resource_ref"]}
        elif len(receipts) == 2:
            name, args = "ryn_read_resource", {"resource_ref": receipts[-1]["entries"][0]["resource_ref"]}
        else:
            raise RuntimeError("setup_must_stop_after_actual_failed_read")
        return {"message": {"role": "assistant", "content": "Fixture preparation only.", "tool_calls": [
            {"id": "setup-" + uuid.uuid4().hex, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}


def prepare_failed_task(runtime, catalog, nas, connection, dav, selected, *, canary):
    """Caller supplies an isolated docs-offline connection with failure.md only."""
    if TARGET_ID in runtime.store.read()["tasks"]:
        raise ValueError("fresh_fixture_required")
    original_model = runtime.model
    runtime.model = PreparationModel()
    release = None
    try:
        row = runtime.create({"message": "Read docs-offline; private setup canary token=" + canary,
            "scope": {"resources": [{"connection_id": connection["id"], "root_path": "", "recursive": True,
                                      "actions": ["list", "read_ai"]}], "allow_model_data": True},
            "expected_destination_revision": catalog.resolve(selected)["destination_revision"]})
        def name_fixture(data):
            target = data["tasks"].pop(row["id"])
            target["id"] = TARGET_ID
            data["tasks"][TARGET_ID] = target
        runtime.store.transaction(name_fixture)
        for _ in range(8):
            current = runtime.store.read()["tasks"][TARGET_ID]
            if current["queue"] and current["queue"][0]["name"] == "ryn_read_resource":
                nas.sessions[connection["id"]][1].client.timeout = httpx.Timeout(0.1)
                entered, release = dav.arm("GET", "failure.md", "barrier_before_io")
            runtime.run_once()
        failed = runtime.get(TARGET_ID)
        failure = next(event for event in reversed(failed["events"])
                       if event["type"] == "tool_result" and event.get("result", {}).get("error") == "resource_read_timeout")
        assert entered.is_set() and failure["result"]["connection_id"] == connection["id"]
        assert failure["result"]["connection_name"] == "docs-offline"
        assert failure["result"]["not_executed"] is False and failure["result"]["no_mutation"] is True
        # This setup transition is explicit evidence, not a model's diagnosis or
        # a fabricated network exception. The actual read receipt precedes it.
        runtime._fail(TARGET_ID, "resource_read_timeout")
        stored = runtime.store.read()["tasks"][TARGET_ID]
        return {"task_id": TARGET_ID, "status": stored["status"], "error": stored["error"],
                "setup_kind": "deterministic_planner_actual_http_timeout_then_harness_failure_transition",
                "failure_event": copy.deepcopy(failure), "task_snapshot_sha256": hashlib.sha256(encoded(stored)).hexdigest(),
                "write_dispatched": any(event["type"] == "tool_started" and event.get("effect") == "write" for event in stored["events"])}
    finally:
        runtime.model = original_model
        if release is not None:
            release.set()
            # The client timed out before the handler appended its audit entry.
            # Finish setup before measuring the diagnosis's resource requests.
            deadline = time.monotonic() + 3
            while not any(entry["method"] == "GET" and entry["path"] == "failure.md" for entry in dav.audit):
                if time.monotonic() >= deadline:
                    raise RuntimeError("setup_http_audit_not_settled")
                time.sleep(0.01)
