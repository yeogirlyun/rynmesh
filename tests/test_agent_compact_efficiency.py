"""Actual-window admission and an offline resource workflow through real tools."""
import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace

from test_agent_resource_writes import writable as writable_fixture
from test_agent_runtime import advance, approve, assistant, make_runtime
from test_agent_tool_menu import ManyTools

from rynmesh.agent_runtime import tool_menu
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore, encoded
from rynmesh.agent_runtime.tools import RynTools
from rynmesh.services import peer_box

writable = writable_fixture


def request_size(messages, specs):
    return len(encoded(messages)) + len(encoded(specs))


def test_fitting_raw_discovery_evidence_does_not_spend_a_summary(tmp_path):
    # The original R06 first paid summary covered only the pinned owner turn,
    # leaving the discovery exchange intact. Reuse that actual exchange, not
    # an artificially small tool receipt, under today's authority projection.
    path = Path(__file__).parents[1] / "docs/acceptance/ryn-agent-delivery/real-model/20261011T0816Z-R06-recovery/run.json"
    evidence = json.loads(path.read_text(encoding="utf-8-sig"))["after"]["task"]
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Discovery received; no business action yet."))
    runtime.tools = ManyTools()
    runtime.update_settings({"max_context_chars": 12000})
    task = runtime.create({"message": "Inspect the authorized resource; never change the source."})
    def seed(data):
        row = data["tasks"][task["id"]]
        row["messages"].extend(copy.deepcopy(evidence["messages"][1:3]))
        # Fill real owner-authority space until the complete request exceeds
        # the obsolete 80% trigger while staying strictly within the hard cap.
        while request_size(runtime._context(row), runtime._specs(row)) <= 9700:
            row["messages"][0]["content"] += " Preserve the source."
    runtime.store.transaction(seed)
    before = runtime.store.read()["tasks"][task["id"]]
    assert 9600 < request_size(runtime._context(before), runtime._specs(before)) < 12000
    advance(runtime)
    assert len(model.calls) == 1 and model.calls[0][2]
    assert model.calls[0][1][-2:] == [{k: v for k, v in m.items() if k != "context_epoch"}
                                   for m in evidence["messages"][1:3]]
    after = runtime.store.read()["tasks"][task["id"]]
    assert not after.get("context_checkpoint")
    assert after["messages"][:len(before["messages"])] == before["messages"]


def observed_state(history, previous=None):
    """Deterministic model stand-in deriving its decisions only from input."""
    state = copy.deepcopy(previous or {})
    names = {c["id"]: c["function"]["name"] for m in history for c in m.get("tool_calls", [])}
    for m in history:
        if m["role"] != "tool":
            continue
        value = json.loads(m["content"]) if isinstance(m["content"], str) else m["content"]
        name = names[m["tool_call_id"]]
        if name == tool_menu.NAME and "available_tool_names" in value:
            state["available"] = value["available_tool_names"]
        elif name == "ryn_list_resource_roots":
            state["root"] = value["roots"][0]["resource_ref"]
        elif name == "ryn_list_resources":
            state["file"] = next(e["resource_ref"] for e in value["entries"] if e["name"] == "observations.txt")
        elif name == "ryn_read_resource":
            state["values"] = [int(n) for n in re.findall(r"Amount=(\d+)", value["text"])]
        elif name == "ryn_create_text_file":
            assert not value.get("error")
            state["created"] = True
    return state


def test_real_resource_tools_reach_body_and_approved_artifact_under_pressure(writable, record_property):
    tools, _, _, _, source, server, root, app, store = writable
    source_text = "\n".join(f"Amount={n}" for n in [17, 29, 43]) + "\n" + "Observation detail. " * 85
    (root / "allowed/observations.txt").write_bytes(source_text.encode("utf-8"))
    owners = ["Read observations.txt and create totals.md with the sum. Never modify the source.",
              "Include only the observed total; never share or change permissions."]
    catalog = SimpleNamespace(list_models=lambda: [{"id": "fixture", "source": "local_api", "available": True,
        "agent_supported": True, "peer_id": "local", "source_id": "fixture", "destination_revision": "rev1"}])
    calls, summary_sources = [], []
    read_names = ["ryn_list_resource_roots", "ryn_list_resources", "ryn_read_resource"]

    class BoundModel:
        supports_destination_binding = True

        def complete(self, model_id, messages, specs, cancelled, on_event, *, expected_destination):
            calls.append((copy.deepcopy(messages), copy.deepcopy(specs)))
            assert request_size(messages, specs) <= 12000
            if not specs:
                payload = json.loads(messages[-1]["content"])
                if "history" in payload:
                    summary_sources.append(payload)
                    previous = json.loads(payload["previous_checkpoint"]) if payload["previous_checkpoint"] else {}
                    return assistant(json.dumps(observed_state(payload["history"], previous)))
                return assistant(json.dumps({"status": "passed", "summary": "Created content was mechanically read back.",
                    "uncovered_requirements": [], "evidence_refs": [e["id"] for e in payload["evidence"]]}))
            assert [m["content"] for m in messages if m["role"] == "user"] == owners
            state, references = {}, {}
            for m in messages:
                if m.get("content", "").startswith("Historical checkpoint"):
                    note = m["content"]
                    state = json.loads(note.split("\n", 1)[1].split("\nExact resource")[0]
                                       .split("\nServer-recorded mutation")[0])
                    if "\nExact resource" in note:
                        references = json.loads(note.split("not the label:\n")[1].split("\nServer-recorded")[0])
            state = observed_state(messages, state)
            for key in ("root", "file"):
                if key in state:
                    state[key] = references.get(state[key], state[key])
            exposed = {s["function"]["name"] for s in specs}
            directory = next((s for s in specs if s["function"]["name"] == tool_menu.NAME), None)
            if directory:
                state["available"] = directory["function"]["parameters"]["properties"]["load"]["items"].get("enum", state.get("available", []))
            if "available" not in state:
                return assistant(name=tool_menu.NAME, args={"cursor": 0})
            if not state.get("values"):
                if not set(read_names) <= exposed:
                    assert set(read_names) <= set(state["available"])
                    return assistant(name=tool_menu.NAME, args={"load": read_names})
                if "root" not in state:
                    return assistant(name=read_names[0])
                if "file" not in state:
                    return assistant(name=read_names[1], args={"resource_ref": state["root"]})
                return assistant(name=read_names[2], args={"resource_ref": state["file"]})
            if not state.get("created"):
                if "ryn_create_text_file" not in exposed:
                    return assistant(name=tool_menu.NAME, args={"load": ["ryn_create_text_file"]})
                return assistant(name="ryn_create_text_file", args={"directory_ref": state["root"],
                    "name": "totals.md", "content": f"Observed total: {sum(state['values'])}\n"})
            return assistant(f"Created totals.md with observed total {sum(state['values'])}.")

    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    model = BoundModel()
    runtime = AgentRuntime(AgentStore(store.home / "agent", key), model, tools, catalog, consumer_node_id="local")
    runtime.update_settings({"enabled": True, "model_id": "fixture", "max_context_chars": 12000,
                             "max_steps": 24, "max_seconds": 600})
    task = runtime.create({"message": owners[0], "budget_limits": {"max_total_model_calls": 12},
        "scope": {"resources": [{"connection_id": source["id"], "root_path": "allowed",
            "actions": ["list", "read_ai", "create_file"]}], "allow_model_data": True},
        "expected_destination_revision": "rev1"})
    task = runtime.add_message(task["id"], {"message": owners[1], "intent": "steer",
        "expected_control_revision": task["control_revision"], "expected_goal_revision": task["goal"]["revision"]})
    pressure, restarted = [], False
    for _ in range(45):
        row = runtime.store.read()["tasks"][task["id"]]
        if row["status"] == "waiting_approval":
            assert not (root / "allowed/totals.md").exists()
            assert row["pending_approval"]["arguments"]["content"] == "Observed total: 89\n"
            approve(runtime, task["id"])
        elif row["status"] in {"completed", "failed", "interrupted"}:
            break
        if not row["queue"] and row["status"] == "queued":
            pressure.append(request_size(runtime._context(row), runtime._specs(row)))
        advance(runtime)
        saved = runtime.store.read()["tasks"][task["id"]]
        if saved.get("context_checkpoint") and not restarted:
            checkpoint = copy.deepcopy(saved["context_checkpoint"])
            runtime = AgentRuntime(AgentStore(store.home / "agent", key), model,
                RynTools(app, store, {}, None), catalog, consumer_node_id="local")
            runtime.reconcile()
            assert runtime.store.read()["tasks"][task["id"]]["context_checkpoint"] == checkpoint
            restarted = True
    done = runtime.store.read()["tasks"][task["id"]]
    assert done["status"] == "completed", (done.get("error"), [
        {k: e[k] for k in ("bytes", "maximum") if k in e} for e in done["events"] if e["type"] == "context_compaction_rejected"])
    # The separate checker has its own larger evidence envelope and correctly
    # refuses it at 12 KiB. This test establishes observed business completion,
    # not a successful model semantic check or formal acceptance.
    assert done["outcome"]["status"] == "unverified"
    assert "agent_verification_context_limit" in done["outcome"]["uncertainties"]
    assert all(item["status"] == "passed" for item in done["outcome"]["mechanical_scope"])
    assert (root / "allowed/totals.md").read_text(encoding="utf-8") == "Observed total: 89\n"
    assert (root / "allowed/observations.txt").read_text(encoding="utf-8") == source_text
    assert max(pressure) > 12000 and restarted and summary_sources
    assert len(summary_sources) < len([1 for _, specs in calls if specs])
    assert len(calls) <= 12 and runtime.get(task["id"])["budget"]["lifetime"]["model_calls"] == len(calls)
    assert done["goal"]["revision"] == 2 and done["scope_revision"] == 1 and done.get("context_epoch", 0) == 0
    assert done["limits"]["max_context_chars"] == 12000
    assert len([e for e in server.audit if e["method"] == "PUT"]) == 1
    assert any(e["method"] == "GET" and e["path"].endswith("observations.txt") for e in server.audit)
    # Every business result remains either verbatim in main input or in a
    # complete closed summary source, and always in the encrypted transcript.
    seen_ids = {m["tool_call_id"] for messages, _ in calls for m in messages if m["role"] == "tool"}
    seen_ids.update(m["tool_call_id"] for source in summary_sources for m in source["history"] if m["role"] == "tool")
    for summary in summary_sources:
        history = summary["history"]
        assert any(m["role"] != "user" for m in history)
        assert {m["tool_call_id"] for m in history if m["role"] == "tool"} == {
            call["id"] for m in history for call in m.get("tool_calls", [])}
    assert {m["tool_call_id"] for m in done["messages"] if m["role"] == "tool"} <= seen_ids
    assert any(json.loads(m["content"]).get("text") == source_text for m in done["messages"] if m["role"] == "tool")
    assert any(r["tool"] == "ryn_create_text_file" and r["status"] == "confirmed" for r in done["receipts"])
    record_property("compact_efficiency", json.dumps({"raw_projection_bytes": pressure,
        "request_bytes": [request_size(messages, specs) for messages, specs in calls],
        "main_calls": sum(bool(specs) for _, specs in calls), "summary_calls": len(summary_sources),
        "total_calls": len(calls), "outcome": done["outcome"]["status"],
        "limitations": done["outcome"]["uncertainties"], "artifact_total": 89,
        "source_unchanged": True, "actual_store_restart": restarted, "business_puts": 1,
        "goal_revision": done["goal"]["revision"], "scope_revision": done["scope_revision"],
        "context_epoch": done.get("context_epoch", 0)}))
