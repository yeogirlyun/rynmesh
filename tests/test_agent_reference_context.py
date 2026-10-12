"""One unchanged public R06 input-window trial, including genuine failure."""
import json
import sqlite3
from types import SimpleNamespace

from test_agent_reference_recovery import ROOT, native  # noqa: F401
from test_agent_resource_writes import writable  # noqa: F401

from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.agent_runtime.tool_menu import NAME
from rynmesh.services import peer_box


def test_public_r06_native_fixed_window_trial(writable, native, record_property):  # noqa: F811
    from documents import execute

    tools, _, _, _, source, server, root, _, store = writable
    case = next(c for c in json.loads((ROOT / "docs/acceptance/ryn-agent-delivery/cases-v1.json").read_text(encoding="utf-8"))["cases"] if c["id"] == "R06")
    source_bytes = (ROOT / "tests/fixtures/agent_acceptance/documents/orion/long-notes.txt").read_bytes()
    folder = root / "projects/orion/week-41"
    folder.mkdir(parents=True)
    (folder / "long-notes.txt").write_bytes(source_bytes)
    (root / "reports/acceptance").mkdir(parents=True)
    model = {"id": "fixture", "model_id": "fixture", "source": "local_api", "available": True,
             "agent_supported": True, "peer_id": "local", "source_id": "fixture", "destination_revision": "rev1",
             "context_window": 100000, "max_output_tokens": 4096}
    catalog = SimpleNamespace(list_models=lambda: [model], resolve=lambda _: model)
    calls, checkpoints = [], []

    class Planner:
        supports_destination_binding = True

        def complete(self, _model, messages, specs, _cancelled, **_options):
            calls.append({"tool_count": len(specs)})
            # Only fixture transport; native owns request history/compaction.
            if not specs:
                return {"message": {"role": "assistant", "content": "The owner requested all 12 CHECKPOINT values. Preserve original source and wait for required owner controls."},
                        "usage": {}}
            name, args = "ryn_list_resource_roots", {}
            tool_results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            roots = next((r["roots"] for r in tool_results if "roots" in r), None)
            entries = next((r["entries"] for r in tool_results if "entries" in r), None)
            if entries:
                name, args = "ryn_search_resource", {"resource_ref": next(e["resource_ref"] for e in entries if e["name"] == "long-notes.txt"), "query": "CHECKPOINT"}
            elif roots:
                name, args = "ryn_list_resources", {"resource_ref": roots[0]["resource_ref"]}
            if name not in {s["function"]["name"] for s in specs}:
                name, args = NAME, {"load": [name]}
            return {"message": {"role": "assistant", "content": "", "tool_calls": [{"id": f"fixture-{len(calls)}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]}, "usage": {}}

    bridge = Planner()
    runtime = AgentRuntime(AgentStore(store.home / "agent", peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")),
                           bridge, tools, catalog, consumer_node_id="local")
    runtime.update_settings({"enabled": True, "model_id": "fixture", "max_context_chars": 12000,
                             "max_steps": 24, "max_seconds": 600})
    scope = {"resources": [{"connection_id": source["id"], "root_path": grant["resource_prefix"], "recursive": True,
             "actions": ["read_ai" if a == "read" else a for a in grant["actions"] if a != "search"]}
             for grant in case["authorization"]["resource_grants"]], "allow_model_data": True}
    task = runtime.create({"message": case["prompt"], "scope": scope, "expected_destination_revision": "rev1",
                           "budget_limits": {"max_total_model_calls": 4}})

    def on_compact(evidence):
        checkpoints.append(evidence)
        current = runtime.get(task["id"])
        runtime.add_message(task["id"], {"message": case["interventions"][0]["message"], "intent": "steer",
            "expected_control_revision": current["control_revision"], "expected_goal_revision": current["goal"]["revision"]})
        return "steer"

    record = {"prompt": case["prompt"], "approvals": []}
    failure = None
    try:
        execute(runtime=runtime, tools=tools, catalog=catalog, selected="fixture", task=task, bridge=bridge,
                record=record, home=store.home, node=native[0], upstream=native[1], real_model=False,
                native_business_operations=True, compact_event=on_compact, allow_write=lambda _: False)
    except ValueError as exc:
        failure = str(exc)
    record_property("native_r06_fixed_window_trial", json.dumps({"record": record, "failure": failure,
        "actual_model_calls": len(calls), "compact_checkpoints": checkpoints, "case_pass": False}))
    assert failure == "agent_tool_schema_limit", (failure, record)
    requests = record["reference_context_requests"]
    assert requests[-1]["projection_rejected"] and requests[-1]["messages_bytes"] > 12000
    admitted = requests[:-1]
    # The available-tool catalog grows as integrations are added. Verify exact
    # wire accounting and admission against the unchanged 12 KB limit.
    assert len(admitted) == 2
    assert all(r["input_bytes"] == r["messages_bytes"] + r["schema_bytes"] <= 12000
               and r["within_limit"] for r in admitted)
    assert admitted[0]["input_bytes"] < admitted[1]["input_bytes"]
    assert len(calls) == 2 and not checkpoints
    policy = record["reference_context_policy"]
    assert policy["initial_schema_bytes"] == requests[0]["schema_bytes"] > 0
    input_estimate = (12000 - policy["initial_schema_bytes"]) // 4
    assert policy["native_compact"] == {
        "contextWindow": min(policy["physical_context_window"], input_estimate + policy["output_reserve_tokens"]),
        "bufferTokens": max(1, input_estimate // 10),
    }
    assert (folder / "long-notes.txt").read_bytes() == source_bytes
    assert not any(row["method"] == "PUT" for row in server.audit)
    record_property("native_r06_fixed_window", json.dumps({"record": record, "failure": failure,
        "actual_model_calls": len(calls), "compact_checkpoints": checkpoints,
        "case_pass": False, "reason": "native_request_authority_exceeds_fixed_input_window_before_compact_commit",
        "unmet_original_interventions": [i["point"] for i in case["interventions"]]}))


def test_native_compact_completed_hook_requires_real_persisted_summary(native, tmp_path, record_property):  # noqa: F811
    from worker_host import run_worker

    calls, observed = [], []
    db = tmp_path / "session.sqlite"
    config = {"session_id": "native-compact-hook", "session_db_path": str(db), "provider_id": "fixture",
              "model_id": "fixture", "context_window": 100000, "max_model_calls": 12,
              "max_output_tokens": 256, "max_seconds": 60, "prompt": "Read the synthetic marker repeatedly.",
              "observe_compaction": True, "compact_policy": {"contextWindow": 1024, "bufferTokens": 64},
              "tools": [{"type": "function", "function": {"name": "ryn_fixture_read", "description": "Read a synthetic marker.",
                         "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}],
              "tool_metadata": {"ryn_fixture_read": {"effect": "read"}}}

    def dispatch(method, params):
        calls.append({"method": method, "input_bytes": len(json.dumps(params, ensure_ascii=False).encode())})
        if method == "tool":
            return {"marker": "SYNTHETIC_READ_ONLY"}
        assert method == "model"
        if any("Your task is to create a detailed summary of the conversation so far" in m["content"] for m in params["messages"]):
            return {"message": {"role": "assistant", "content": "<analysis>Read-only synthetic marker calls.</analysis><summary>Read-only synthetic marker observed; no mutation occurred.</summary>"}}
        return {"message": {"role": "assistant", "content": "", "tool_calls": [{"id": f"marker-{len(calls)}", "type": "function",
                          "function": {"name": "ryn_fixture_read", "arguments": "{}"}}]}}

    def checkpoint(evidence):
        with sqlite3.connect(db) as conn:
            row = conn.execute("select data from message where id=? and session_id=?",
                               (evidence["summary_message_id"], evidence["session_id"])).fetchone()
            assert row is not None
            message = json.loads(row[0])
            assert message["semantics"]["kind"] == "compact_summary"
            parts = [json.loads(r[0]) for r in conn.execute("select data from part where message_id=?", (evidence["summary_message_id"],))]
            assert any(p.get("compactBoundary", {}).get("boundaryId") == evidence["boundary_id"] for p in parts)
        observed.append(evidence)
        return "pause"

    result = run_worker(config, dispatch, node=native[0], upstream=native[1], compact_event=checkpoint)
    assert len(observed) == 1
    assert result["result"]["owner_control"]["result"]["kind"] == "stopped"
    assert result["dispatch_counts"]["checkpoint"] == 1
    assert result["dispatch_counts"]["approval"] == 0
    resumed = run_worker({**config, "resume": True, "rpc_sequence_start": result["last_rpc_id"]},
                         dispatch, node=native[0], upstream=native[1], compact_event=checkpoint)
    assert resumed["result"]["resumed"] and resumed["result"]["persisted_history_unchanged"]
    assert resumed["result"]["session_id"] == result["result"]["session_id"]
    assert resumed["worker_pid"] != result["worker_pid"]
    assert all(count == 0 for count in resumed["dispatch_counts"].values()) and len(observed) == 1
    record_property("native_compact_hook", json.dumps({"result": result, "checkpoint": observed,
        "readback": resumed, "calls": calls,
        "scope": "public compact persistence, stop and cold hydration only, not R06 score", "formal_pass": False}))
