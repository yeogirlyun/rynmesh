"""Public long-document evidence through real DAV tools at the fixed 12 KB cap."""
import copy
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

from test_agent_compact_efficiency import request_size
from test_agent_resource_writes import writable as writable_fixture
from test_agent_runtime import advance, approve, assistant

from rynmesh.agent_runtime import tool_menu
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.agent_runtime.tools import RynTools
from rynmesh.services import peer_box

writable = writable_fixture


def observe(history, previous=None):
    state = copy.deepcopy(previous or {})
    names = {c["id"]: c["function"]["name"] for m in history for c in m.get("tool_calls", [])}
    for m in history:
        if m["role"] != "tool":
            continue
        value = json.loads(m["content"]) if isinstance(m["content"], str) else m["content"]
        name = names[m["tool_call_id"]]
        if value.get("error"):
            continue
        if name == "ryn_list_resource_roots":
            state["root"] = value["roots"][0]["resource_ref"]
        elif name == "ryn_list_resources":
            state["file"] = next(e["resource_ref"] for e in value["entries"] if e["name"] == "long-notes.txt")
        elif name == "ryn_search_resource":
            values = state.setdefault("values", {})
            for match in value["matches"]:
                for number, amount in re.findall(r"CHECKPOINT-(\d+): (\d+)", match["text"]):
                    values[number] = int(amount)
            state["cursor"] = value["next_cursor"]
        elif name == "ryn_create_text_file":
            state["created"] = True
    return state


def test_public_long_notes_all_checkpoints_to_approved_anonymous_report(writable, record_property):
    tools, _, _, _, source, server, root, app, store = writable
    public = Path(__file__).parent / "fixtures/agent_acceptance/documents/orion/long-notes.txt"
    source_bytes = public.read_bytes()
    (root / "allowed/long-notes.txt").write_bytes(source_bytes)
    owners = ["Read every CHECKPOINT value in long-notes.txt and create report.md with each observed value and the total. Preserve the source.",
              "The report must be anonymous: omit all customer names. Do not share or alter permissions."]
    catalog = SimpleNamespace(list_models=lambda: [{"id": "fixture", "source": "local_api", "available": True,
        "agent_supported": True, "peer_id": "local", "source_id": "fixture", "destination_revision": "rev1"}])
    calls, summaries = [], []
    read_names = ["ryn_list_resource_roots", "ryn_list_resources", "ryn_search_resource"]

    class Model:
        supports_destination_binding = True

        def complete(self, model_id, messages, specs, cancelled, on_event, *, expected_destination):
            calls.append((copy.deepcopy(messages), copy.deepcopy(specs)))
            assert request_size(messages, specs) <= 12000
            if not specs:
                payload = json.loads(messages[-1]["content"])
                if "history" in payload:
                    summaries.append(payload)
                    previous = json.loads(payload["previous_checkpoint"]) if payload["previous_checkpoint"] else {}
                    content = json.dumps(observe(payload["history"], previous), separators=(",", ":"))
                    maximum = int(re.search(r"hard maximum is (\d+) bytes", messages[0]["content"])[1])
                    assert len(content.encode()) <= maximum, (len(content.encode()), maximum)
                    return assistant(content)
                return assistant(json.dumps({"status": "passed", "summary": "All observed checkpoint values match the created report.",
                    "uncovered_requirements": [], "evidence_refs": [e["id"] for e in payload["evidence"]]}))
            assert [m["content"] for m in messages if m["role"] == "user"] == owners
            state, references = {}, {}
            for m in messages:
                if m.get("content", "").startswith("Historical checkpoint"):
                    note = m["content"]
                    state = json.loads(note.split("\n", 1)[1].split("\nExact resource")[0].split("\nServer-recorded mutation")[0])
                    if "\nExact resource" in note:
                        references = json.loads(note.split("not the label:\n")[1].split("\nServer-recorded")[0])
            state = observe(messages, state)
            for key in ("root", "file", "cursor"):
                if key in state:
                    state[key] = references.get(state[key], state[key])
            exposed = {s["function"]["name"] for s in specs}
            directory = next(s for s in specs if s["function"]["name"] == tool_menu.NAME)
            available = directory["function"]["parameters"]["properties"]["load"]["items"]["enum"]
            assert set(read_names + ["ryn_create_text_file"]) <= set(available)
            if "cursor" not in state or state["cursor"]:
                if not set(read_names) <= exposed:
                    return assistant(name=tool_menu.NAME, args={"load": read_names})
                if "root" not in state:
                    return assistant(name=read_names[0])
                if "file" not in state:
                    return assistant(name=read_names[1], args={"resource_ref": state["root"]})
                args = {"resource_ref": state["file"], "query": "CHECKPOINT"}
                if state.get("cursor"):
                    args["cursor"] = state["cursor"]
                return assistant(name=read_names[2], args=args)
            values = state["values"]
            content = "# Anonymous checkpoint report\n" + "".join(f"CHECKPOINT-{n}: {v}\n" for n, v in sorted(values.items())) + f"Total: {sum(values.values())}\n"
            if not state.get("created"):
                if "ryn_create_text_file" not in exposed:
                    return assistant(name=tool_menu.NAME, args={"load": ["ryn_create_text_file"]})
                return assistant(name="ryn_create_text_file", args={"directory_ref": state["root"], "name": "report.md", "content": content})
            return assistant(f"Created the anonymous report with {len(values)} observed checkpoints and total {sum(values.values())}.")

    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    model = Model()
    runtime = AgentRuntime(AgentStore(store.home / "agent", key), model, tools, catalog, consumer_node_id="local")
    runtime.update_settings({"enabled": True, "model_id": "fixture", "max_context_chars": 12000, "max_steps": 24, "max_seconds": 600})
    task = runtime.create({"message": owners[0], "budget_limits": {"max_total_model_calls": 12},
        "scope": {"resources": [{"connection_id": source["id"], "root_path": "allowed", "actions": ["list", "read_ai", "create_file"]}],
                  "allow_model_data": True}, "expected_destination_revision": "rev1"})
    task = runtime.add_message(task["id"], {"message": owners[1], "intent": "steer",
        "expected_control_revision": task["control_revision"], "expected_goal_revision": task["goal"]["revision"]})
    pressure, restarted, approved_content = [], False, None
    for _ in range(50):
        row = runtime.store.read()["tasks"][task["id"]]
        if row["status"] == "waiting_approval":
            assert not (root / "allowed/report.md").exists()
            approved_content = row["pending_approval"]["arguments"]["content"]
            approve(runtime, task["id"])
        elif row["status"] in {"completed", "failed", "interrupted"}:
            break
        if row["status"] == "queued" and not row["queue"]:
            pressure.append(request_size(runtime._context(row), runtime._specs(row)))
        advance(runtime)
        saved = runtime.store.read()["tasks"][task["id"]]
        if saved.get("context_checkpoint") and not restarted:
            checkpoint = copy.deepcopy(saved["context_checkpoint"])
            runtime = AgentRuntime(AgentStore(store.home / "agent", key), model, RynTools(app, store, {}, None), catalog, consumer_node_id="local")
            runtime.reconcile()
            assert runtime.store.read()["tasks"][task["id"]]["context_checkpoint"] == checkpoint
            restarted = True
    done = runtime.store.read()["tasks"][task["id"]]
    assert done["status"] == "completed", (done.get("error"), [(bool(s), request_size(m, s)) for m, s in calls], done["events"][-3:])
    report = (root / "allowed/report.md").read_text(encoding="utf-8")
    expected = dict(re.findall(r"CHECKPOINT-(\d+): (\d+)", source_bytes.decode()))
    assert dict(re.findall(r"CHECKPOINT-(\d+): (\d+)", report)) == expected and len(expected) == 12
    assert f"Total: {sum(map(int, expected.values()))}" in report
    assert "orion" not in report.lower() and approved_content == report
    assert hashlib.sha256((root / "allowed/long-notes.txt").read_bytes()).digest() == hashlib.sha256(source_bytes).digest()
    assert max(pressure) > 12000 and restarted and summaries
    assert len(calls) <= 12 and done["limits"]["max_context_chars"] == 12000
    assert done["goal"]["revision"] == 2 and done["scope_revision"] == 1 and done.get("context_epoch", 0) == 0
    assert len([e for e in server.audit if e["method"] == "PUT"]) == 1
    assert any(e["method"] == "GET" and e["path"].endswith("long-notes.txt") for e in server.audit)
    seen = {m["tool_call_id"] for messages, _ in calls for m in messages if m["role"] == "tool"}
    seen.update(m["tool_call_id"] for summary in summaries for m in summary["history"] if m["role"] == "tool")
    assert {m["tool_call_id"] for m in done["messages"] if m["role"] == "tool"} <= seen
    assert all(c["status"] == "passed" for c in done["outcome"]["mechanical_scope"])
    record_property("long_resource_context", json.dumps({"source_bytes": len(source_bytes), "raw_projection_bytes": pressure,
        "request_bytes": [request_size(m, s) for m, s in calls], "main_calls": sum(bool(s) for _, s in calls), "summary_calls": len(summaries),
        "checker_calls": sum(not specs and "history" not in json.loads(messages[-1]["content"]) for messages, specs in calls),
        "search_receipts": sum(r["tool"] == "ryn_search_resource" for r in done["receipts"]),
        "read_receipts": sum(r["tool"] == "ryn_read_resource" for r in done["receipts"]),
        "source_http_gets": sum(e["method"] == "GET" and e["path"].endswith("long-notes.txt") for e in server.audit),
        "total_calls": len(calls), "artifact_checkpoints": len(expected), "artifact_total": sum(map(int, expected.values())),
        "source_unchanged": True, "actual_store_restart": restarted, "business_puts": 1, "outcome": done["outcome"]["status"],
        "limitations": done["outcome"]["uncertainties"]}))
