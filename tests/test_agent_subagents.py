from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.agent_runtime import subagents
from rynmesh.agent_runtime.routes import install_agent
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.services import peer_box


class _FakeModel:
    supports_destination_binding = True
    supports_output_token_limit = True
    supports_request_deadline = True
    supports_request_id = True
    supports_conversation_key = True

    def __init__(self) -> None:
        self.parent_responses: list[dict[str, Any]] = []
        self.child_responses: list[dict[str, Any]] = []

    def complete(self, model_id, messages, specs, cancelled, on_event, **options):
        joined = "\n".join(str(m.get("content") or "") for m in messages if isinstance(m, dict))
        if "</subagent_identity>" in joined:
            if self.child_responses:
                return self.child_responses.pop(0)
            return {
                "message": {"role": "assistant", "content": "Subagent task finished."},
                "finish_reason": "stop",
                "usage": {"input_tokens": 20, "output_tokens": 12},
            }
        if self.parent_responses:
            return self.parent_responses.pop(0)
        return {
            "message": {"role": "assistant", "content": "Parent task finished."},
            "finish_reason": "stop",
            "usage": {"input_tokens": 20, "output_tokens": 12},
        }


class _FakeTools:
    def specs(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "ryn_file_read",
                    "description": "Read file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ryn_file_edit",
                    "description": "Edit file",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"},
                        },
                        "required": ["path", "old_text", "new_text"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    def metadata(self, name: str):
        if name == "ryn_file_edit":
            return {"effect": "write", "title": name}
        return {"effect": "read", "title": name}

    def preview(self, name: str, arguments: dict, **options):
        return {
            "path": arguments.get("path"),
            "added_lines": 2,
            "removed_lines": 1,
            "diff": "@@ -1,1 +1,2 @@\n-old\n+new1\n+new2",
        }

    def execute(self, name: str, arguments: dict, **options):
        return {"ok": True, "path": arguments.get("path")}


class _FakeCatalog:
    def list_models(self):
        return [
            {
                "id": "local/gpt-4o",
                "name": "GPT-4o",
                "source": "local_api",
                "available": True,
                "agent_supported": True,
                "destination_binding_supported": True,
                "destination_revision": "rev-1",
                "context_window_tokens": 128000,
            }
        ]


class _FakeNodeStore:
    peer_id = "peer-local-test"


def _build_env(tmp_path: Path):
    app = FastAPI()
    workers = BackgroundWorkerRegistry()
    key = peer_box.load_or_create_messaging_key(tmp_path / "messaging.x25519")
    model = _FakeModel()
    tools = _FakeTools()
    catalog = _FakeCatalog()
    runtime = install_agent(
        app,
        store=_FakeNodeStore(),
        home=tmp_path,
        workers=workers,
        local_control=lambda _req: None,
        messaging_key=key,
        integrations=(model, tools, catalog),
    )
    runtime.update_settings(
        {
            "enabled": True,
            "model_id": "local/gpt-4o",
            "permission_mode": "automatic",
            "workspace_root": str(tmp_path),
        }
    )
    client = TestClient(app)
    return app, runtime, model, client


def test_subagent_spawn_wait_and_projection(tmp_path: Path):
    _, runtime, model, client = _build_env(tmp_path)

    # 1. Parent turn 1: spawns a worker subagent
    model.parent_responses.append(
        {
            "message": {
                "role": "assistant",
                "content": "Spawning Turing to refactor subagents.",
                "tool_calls": [
                    {
                        "id": "call_spawn_1",
                        "type": "function",
                        "function": {
                            "name": "ryn_spawn_agent",
                            "arguments": json.dumps(
                                {
                                    "task_name": "refactor_worker",
                                    "prompt": "Update rynmesh/agent_runtime/subagents.py",
                                    "role": "worker",
                                    "write_scope": ["rynmesh/agent_runtime/subagents.py"],
                                }
                            ),
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
            "usage": {"input_tokens": 30, "output_tokens": 20},
        }
    )
    # 2. Parent turn 2: calls ryn_wait_agent
    model.parent_responses.append(
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_wait_1",
                        "type": "function",
                        "function": {
                            "name": "ryn_wait_agent",
                            "arguments": json.dumps({"targets": ["Turing"], "timeout_seconds": 10}),
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
            "usage": {"input_tokens": 35, "output_tokens": 15},
        }
    )
    # 3. Child turn 1: child completes and produces its final report
    model.child_responses.append(
        {
            "message": {
                "role": "assistant",
                "content": "Completed refactoring subagents.py with zero errors.",
            },
            "finish_reason": "stop",
            "usage": {"input_tokens": 25, "output_tokens": 18},
        }
    )
    # 4. Parent turn 3: synthesizes final answer after receiving <subagent_notification>
    model.parent_responses.append(
        {
            "message": {
                "role": "assistant",
                "content": "All delegated subagent work is complete!",
            },
            "finish_reason": "stop",
            "usage": {"input_tokens": 40, "output_tokens": 16},
        }
    )

    created = runtime.create(
        {
            "message": "Split the refactor work into a subagent.",
            "model_id": "local/gpt-4o",
            "client_request_id": str(uuid.uuid4()),
        }
    )
    parent_id = created["id"]

    # Step through using single-threaded run_once()
    for _ in range(10):
        snap = runtime.get(parent_id)
        if snap["status"] == "completed":
            break
        runtime.run_once()

    final_parent = runtime.get(parent_id)
    assert final_parent["status"] == "completed"
    assert "All delegated subagent work is complete!" in final_parent["result"]
    assert "subagents" in final_parent
    assert len(final_parent["subagents"]) == 1

    sub = final_parent["subagents"][0]
    assert sub["nickname"] == "Turing"
    assert sub["role"] == "worker"
    assert sub["agent_path"] == "/root/refactor_worker"
    assert sub["write_scope"] == ["rynmesh/agent_runtime/subagents.py"]
    assert sub["status"] == "completed"
    assert "Completed refactoring subagents.py" in (sub["final_reply"] or "")

    # Verify top-level list_tasks() excludes child subagent tasks
    top_tasks = runtime.list_tasks()
    assert [t["id"] for t in top_tasks] == [parent_id]

    # Verify unchanged poll includes live subagents projection
    unchanged = runtime.get(parent_id, since_revision=final_parent["revision"])
    assert unchanged.get("unchanged") is True
    assert len(unchanged.get("subagents") or []) == 1


def test_subagent_role_and_write_scope_enforcement():
    explorer_row = {
        "id": "agent_child_1",
        "parent_task_id": "agent_parent",
        "local_execution": {"workspace_root": "/repo"},
        "subagent_meta": {
            "nickname": "Curie",
            "role": "explorer",
            "write_scope": [],
        },
    }
    with pytest.raises(AgentToolRejected) as exc_info:
        subagents.check_subagent_tool_policy(
            explorer_row,
            "ryn_file_edit",
            {"path": "src/app.py", "old_text": "a", "new_text": "b"},
        )
    assert str(exc_info.value) == "agent_subagent_read_only"

    worker_row = {
        "id": "agent_child_2",
        "parent_task_id": "agent_parent",
        "local_execution": {"workspace_root": "/repo"},
        "subagent_meta": {
            "nickname": "Feynman",
            "role": "worker",
            "write_scope": ["src/allowed.py", "webapp/src/screens"],
        },
    }
    # Allowed inside write_scope
    subagents.check_subagent_tool_policy(
        worker_row,
        "ryn_file_edit",
        {"path": "/repo/src/allowed.py", "old_text": "a", "new_text": "b"},
    )
    subagents.check_subagent_tool_policy(
        worker_row,
        "ryn_file_edit",
        {"path": "webapp/src/screens/RynAgent.tsx", "old_text": "a", "new_text": "b"},
    )
    # Rejected outside write_scope
    with pytest.raises(AgentToolRejected) as scope_exc:
        subagents.check_subagent_tool_policy(
            worker_row,
            "ryn_file_edit",
            {"path": "src/forbidden.py", "old_text": "a", "new_text": "b"},
        )
    assert str(scope_exc.value) == "agent_subagent_write_scope_violation"


def test_subagent_interrupt_and_stop_all_actions(tmp_path: Path):
    _, runtime, model, _ = _build_env(tmp_path)

    model.parent_responses.append(
        {
            "message": {
                "role": "assistant",
                "content": "Spawning two subagents.",
                "tool_calls": [
                    {
                        "id": "call_spawn_a",
                        "type": "function",
                        "function": {
                            "name": "ryn_spawn_agent",
                            "arguments": json.dumps(
                                {
                                    "task_name": "audit_api",
                                    "prompt": "Update rynmesh/agent_runtime/subagents.py",
                                    "role": "explorer",
                                }
                            ),
                        },
                    },
                    {
                        "id": "call_spawn_b",
                        "type": "function",
                        "function": {
                            "name": "ryn_spawn_agent",
                            "arguments": json.dumps(
                                {
                                    "task_name": "patch_ui",
                                    "prompt": "Update rynmesh/agent_runtime/subagents.py",
                                    "role": "worker",
                                    "write_scope": ["webapp/src"],
                                }
                            ),
                        },
                    },
                ],
            },
            "finish_reason": "tool_calls",
            "usage": {"input_tokens": 30, "output_tokens": 20},
        }
    )

    created = runtime.create(
        {
            "message": "Split the refactor work across two subagents.",
            "model_id": "local/gpt-4o",
            "client_request_id": str(uuid.uuid4()),
        }
    )
    parent_id = created["id"]

    # Execute inference step + both queued tool execution steps so both subagents are spawned
    runtime.run_once()
    runtime.run_once()
    runtime.run_once()
    snap = runtime.get(parent_id)
    subs = snap.get("subagents") or []
    assert len(subs) == 2
    assert subs[0]["nickname"] == "Turing"
    assert subs[1]["nickname"] == "Curie"

    # Interrupt first subagent by nickname
    updated = runtime.action(
        parent_id,
        {"action": "interrupt_subagent", "subagent_id": "Turing"},
    )
    subs_after_one = {s["nickname"]: s for s in (updated.get("subagents") or [])}
    assert subs_after_one["Turing"]["status"] == "paused"
    assert subs_after_one["Curie"]["status"] in ("queued", "running")

    # Stop all remaining subagents
    updated_all = runtime.action(
        parent_id,
        {"action": "stop_all_subagents"},
    )
    for sub in updated_all.get("subagents") or []:
        assert sub["status"] == "paused"

    # Cancelling parent cascades cancellation to subagents
    cancelled_parent = runtime.action(
        parent_id,
        {"action": "cancel"},
    )
    for sub in cancelled_parent.get("subagents") or []:
        assert sub["status"] == "cancelled"
        assert sub["closed"] is True


def test_subagent_tree_resolution_list_resume_fork_and_eviction(tmp_path: Path):
    _, runtime, _, _ = _build_env(tmp_path)
    created = runtime.create(
        {
            "message": "Coordinate complex multi-agent tree.",
            "model_id": "local/gpt-4o",
            "client_request_id": str(uuid.uuid4()),
        }
    )
    parent_id = created["id"]

    def _tx(data: dict):
        parent_row = data["tasks"][parent_id]
        # 1. Spawn task_a with fork_turns="all"
        res_a = subagents.spawn_agent_in_tx(
            data,
            parent_row,
            {
                "task_name": "task_a",
                "prompt": "Explore module A",
                "role": "explorer",
                "fork_turns": "all",
            },
        )
        child_a = data["tasks"][res_a["agent_id"]]
        # Verify Codex FORK_ISOLATION_PROMPT is injected when fork_turns == "all"
        contents = [m.get("content", "") for m in child_a["messages"]]
        assert any(subagents.FORK_ISOLATION_PROMPT in c for c in contents)

        # 2. Spawn nested child under task_a (/root/task_a/nested_leaf) and sibling task_b (/root/task_b)
        res_leaf = subagents.spawn_agent_in_tx(
            data,
            child_a,
            {
                "task_name": "nested_leaf",
                "prompt": "Check leaf",
                "role": "worker",
                "write_scope": ["src/leaf.py"],
            },
        )
        child_leaf = data["tasks"][res_leaf["agent_id"]]
        # Verify MAX_SUBAGENT_DEPTH = 2 blocks spawning beyond depth 2 (/root/task_a/nested_leaf/too_deep)
        with pytest.raises(AgentToolRejected) as depth_exc:
            subagents.spawn_agent_in_tx(
                data,
                child_leaf,
                {"task_name": "too_deep", "prompt": "Too deep", "role": "explorer"},
            )
        assert str(depth_exc.value) == "agent_subagent_depth_limit_reached"

        res_b = subagents.spawn_agent_in_tx(
            data,
            parent_row,
            {
                "task_name": "task_b",
                "prompt": "Await test run",
                "role": "awaiter",
            },
        )
        child_b = data["tasks"][res_b["agent_id"]]
        # Active awaiter is running/queued, not waiting_input/paused
        assert subagents.project_subagent_summary(child_b)["is_waiting"] is False

        # Sibling/cousin resolution across the root thread tree:
        # child_b (/root/task_b) can resolve /root/task_a/nested_leaf by canonical path or nickname
        resolved = subagents.resolve_subagent_targets(data, child_b, ["/root/task_a/nested_leaf"])
        assert len(resolved) == 1
        assert resolved[0]["id"] == res_leaf["agent_id"]

        # 3. List agents with path_prefix filter
        listed_all = subagents.list_agents_in_tx(data, parent_row, {})
        assert listed_all["count"] == 3
        listed_sub = subagents.list_agents_in_tx(data, parent_row, {"path_prefix": "/root/task_a"})
        assert listed_sub["count"] == 2

        # 4. Close child_b (verifying previous_status) and then resume it via resume_agent_in_tx
        close_res = subagents.close_agent_in_tx(data, parent_row, {"target": "Feynman"})
        assert close_res["closed"][0]["previous_status"] == "queued"
        assert child_b["subagent_meta"]["closed"] is True
        resumed = subagents.resume_agent_in_tx(
            data,
            parent_row,
            {"target": "/root/task_b", "prompt": "Re-run the test suite now"},
        )
        assert resumed["closed"] is False
        assert resumed["status"] == "queued"

        # 5. Mark child_a as completed (still open) and spawn up to MAX_OPEN_SUBAGENTS + 1 to verify resident eviction
        child_a["status"] = "completed"
        for idx in range(4):
            subagents.spawn_agent_in_tx(
                data,
                parent_row,
                {"task_name": f"extra_{idx}", "prompt": f"Extra {idx}", "role": "default"},
            )
        # child_a was the oldest settled open agent and should have been automatically evicted (closed=True)
        assert child_a["subagent_meta"]["closed"] is True

    runtime.store.transaction(_tx)


def test_s07_i7_t1_subagent_tool_specs_within_quota_and_complete_descriptions():
    from rynmesh.agent_runtime.store import encoded

    # Current SUBAGENT_TOOL_SPECS serializes to 4661 UTF-8 bytes; cap at 5130 (~10% headroom).
    total_bytes = len(encoded(subagents.SUBAGENT_TOOL_SPECS))
    assert total_bytes <= 5130

    by_name = {s["function"]["name"]: s["function"] for s in subagents.SUBAGENT_TOOL_SPECS}
    spawn_fn = by_name["ryn_spawn_agent"]
    spawn_desc = spawn_fn["description"]
    assert "critical-path" in spawn_desc
    assert "blocking your immediate next step" in spawn_desc
    assert "fork_turns=none (default) the child sees only this prompt" in spawn_desc
    assert "goal" in spawn_desc and "paths" in spawn_desc and "constraints" in spawn_desc and "what to report" in spawn_desc
    assert "disjoint" in spawn_desc and "write_scope" in spawn_desc
    assert "Do not redo" in spawn_desc
    assert "ryn_wait_agent sparingly" in spawn_desc
    assert "model_id" in spawn_desc and "reasoning_effort" in spawn_desc and "explicitly" in spawn_desc
    assert "Available roles:" in spawn_desc
    for r_name in ("explorer:", "worker:", "awaiter:", "default:"):
        assert r_name in spawn_desc

    spawn_props = spawn_fn["parameters"]["properties"]
    assert "omit" in spawn_props["role"].get("description", "").lower()
    fork_desc = spawn_props["fork_turns"].get("description", "")
    assert "none" in fork_desc and "prompt" in fork_desc and "all" in fork_desc

    for reuse_tool in ("ryn_send_message", "ryn_followup_task"):
        desc = by_name[reuse_tool]["description"]
        assert "Reuse an existing subagent" in desc


def test_s07_i7_t2_coordinator_guidance_uses_constants_and_workspace_explorer_rules(monkeypatch):
    guidance = subagents.MULTI_AGENT_COORDINATOR_GUIDANCE
    assert str(subagents.MAX_OPEN_SUBAGENTS) in guidance
    assert str(subagents.MAX_SUBAGENT_DEPTH) in guidance
    monkeypatch.setattr(subagents, "MAX_OPEN_SUBAGENTS", 11)
    monkeypatch.setattr(subagents, "MAX_SUBAGENT_DEPTH", 4)
    custom = subagents.build_coordinator_guidance()
    assert "11" in custom and "4" in custom

    assert "share the same workspace" in guidance
    assert "immediately visible" in guidance
    assert "earliest settled" in guidance
    assert "Trust explorer" in guidance
    assert "reuse an existing explorer" in guidance
    assert "blocking your immediate next step" not in guidance
    assert "Call ryn_wait_agent sparingly" not in guidance


def test_s07_i7_t3_awaiter_role_guidance_includes_three_new_rules():
    text = subagents.role_guidance(
        {"role": "awaiter", "nickname": "Turing", "agent_path": "/root/waiter"}
    )
    assert "Do not change, reinterpret or optimize the task; keep waiting until a terminal state or an explicit stop." in text
    assert "Keep calling ryn_process_read; never report completion without an exit status." in text
    assert "If asked for status, report it and keep waiting." in text


def test_s07_i7_t4_spawn_prompt_accepts_20000_chars_and_rejects_32001_chars(tmp_path: Path):
    by_name = {s["function"]["name"]: s["function"] for s in subagents.SUBAGENT_TOOL_SPECS}
    assert by_name["ryn_spawn_agent"]["parameters"]["properties"]["prompt"]["maxLength"] == 32000
    assert by_name["ryn_send_message"]["parameters"]["properties"]["message"]["maxLength"] == 32000
    assert by_name["ryn_followup_task"]["parameters"]["properties"]["prompt"]["maxLength"] == 32000
    assert by_name["ryn_resume_agent"]["parameters"]["properties"]["prompt"]["maxLength"] == 32000

    valid_20k = "x" * 20000
    parsed = subagents.validate_subagent_tool(
        "ryn_spawn_agent",
        {"task_name": "long_valid", "prompt": valid_20k, "role": "explorer"},
    )
    assert len(parsed["prompt"]) == 20000

    with pytest.raises(AgentToolRejected):
        subagents.validate_subagent_tool(
            "ryn_spawn_agent",
            {"task_name": "too_long", "prompt": "y" * 32001, "role": "explorer"},
        )

    _, runtime, model, _ = _build_env(tmp_path)
    model.parent_responses.append(
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_spawn_20k",
                        "type": "function",
                        "function": {
                            "name": "ryn_spawn_agent",
                            "arguments": json.dumps(
                                {"task_name": "ok_20k", "prompt": valid_20k, "role": "explorer"}
                            ),
                        },
                    },
                ],
            },
            "finish_reason": "tool_calls",
            "usage": {"input_tokens": 50, "output_tokens": 30},
        }
    )
    model.parent_responses.append(
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_spawn_32001",
                        "type": "function",
                        "function": {
                            "name": "ryn_spawn_agent",
                            "arguments": json.dumps(
                                {"task_name": "bad_32001", "prompt": "z" * 32001, "role": "explorer"}
                            ),
                        },
                    },
                ],
            },
            "finish_reason": "tool_calls",
            "usage": {"input_tokens": 50, "output_tokens": 30},
        }
    )
    model.parent_responses.append(
        {
            "message": {"role": "assistant", "content": "Done testing prompt bounds."},
            "finish_reason": "stop",
            "usage": {"input_tokens": 30, "output_tokens": 10},
        }
    )
    created = runtime.create(
        {
            "message": "Test long subagent spawn prompts.",
            "model_id": "local/gpt-4o",
            "client_request_id": str(uuid.uuid4()),
        }
    )
    parent_id = created["id"]
    for _ in range(8):
        if runtime.get(parent_id)["status"] == "completed":
            break
        runtime.run_once()

    snap = runtime.get(parent_id)
    assert len(snap.get("subagents") or []) == 1
    assert snap["subagents"][0]["agent_path"] == "/root/ok_20k"

    raw_parent = runtime.store.read()["tasks"][parent_id]
    tool_msgs = [m for m in raw_parent["messages"] if isinstance(m, dict) and m.get("role") == "tool"]
    by_call = {m["tool_call_id"]: json.loads(m["content"]) for m in tool_msgs}
    assert by_call["call_spawn_20k"]["ok"] is True
    assert by_call["call_spawn_32001"]["not_executed"] is True


def test_s07_i7_t5_all_internal_specs_size_cap_and_core_tools_pinned():
    from rynmesh.agent_runtime import code_mode, local_specs, tool_menu
    from rynmesh.agent_runtime.loop import ALL_INTERNAL_SPECS
    from rynmesh.agent_runtime.store import encoded

    # Current ALL_INTERNAL_SPECS serializes to 12579 UTF-8 bytes; cap at 13840 (~10% headroom).
    all_internal_bytes = len(encoded(ALL_INTERNAL_SPECS))
    assert all_internal_bytes <= 13840

    business = [
        {"type": "function", "function": {"name": name, "description": desc, "parameters": schema}}
        for name, (desc, _eff, _risk, schema) in local_specs.SPECS.items()
    ]
    business.append(code_mode.SPEC)
    business.append(
        {
            "type": "function",
            "function": {
                "name": "ryn_list_plugins",
                "description": "List installed plugins.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    )
    pinned_request = (
        tool_menu.PLUGIN_DISCOVERY_TOOLS
        + ("ryn_project_inspect",)
        + tool_menu.CORE_TOOLS
        + ("ryn_code_exec",)
    )
    exposed, state = tool_menu.project(
        business,
        ALL_INTERNAL_SPECS,
        {},
        max_context_bytes=60000,
        authority_bytes=8856,
        epoch=0,
        goal_revision=1,
        pinned_names=pinned_request,
    )
    exposed_names = {s["function"]["name"] for s in exposed}
    assert set(tool_menu.CORE_TOOLS) <= set(state["pinned_names"])
    assert set(tool_menu.CORE_TOOLS) <= exposed_names


def test_s07_i1_t1_ten_unread_notifications_summarized_and_persisted(tmp_path: Path):
    _, runtime, model, _ = _build_env(tmp_path)
    created = runtime.create(
        {
            "message": "Summarize all finished subagent reports.",
            "model_id": "local/gpt-4o",
            "client_request_id": str(uuid.uuid4()),
        }
    )
    parent_id = created["id"]

    def _seed_notifications(data: dict):
        parent_row = data["tasks"][parent_id]
        queue = parent_row.setdefault("subagent_notifications", [])
        queue.append(
            {
                "id": "subnotif_already_delivered",
                "agent_id": "child_old",
                "agent_path": "/root/old_worker",
                "nickname": "OldWorker",
                "role": "explorer",
                "status": "completed",
                "summary": "Old summary that must not be resent.",
                "delivered": True,
            }
        )
        for idx in range(10):
            queue.append(
                {
                    "id": f"subnotif_{idx:02d}",
                    "agent_id": f"child_{idx:02d}",
                    "agent_path": f"/root/worker_{idx:02d}",
                    "nickname": f"Nick{idx:02d}",
                    "role": "explorer",
                    "status": "failed" if idx == 1 else "completed",
                    "summary": f"Full body for subagent {idx:02d}",
                    "delivered": False,
                }
            )

    runtime.store.transaction(_seed_notifications)

    model.parent_responses.append(
        {
            "message": {"role": "assistant", "content": "Processed all 10 subagent notifications."},
            "finish_reason": "stop",
            "usage": {"input_tokens": 60, "output_tokens": 20},
        }
    )
    runtime.run_once()

    row = runtime.store.read()["tasks"][parent_id]
    assert row["status"] == "completed"

    notif_indices = [
        idx
        for idx, msg in enumerate(row["messages"])
        if isinstance(msg, dict) and (msg.get("ui") or {}).get("kind") == "subagent_notification"
    ]
    assistant_indices = [
        idx
        for idx, msg in enumerate(row["messages"])
        if isinstance(msg, dict) and msg.get("role") == "assistant"
    ]
    assert len(notif_indices) == 1
    assert len(assistant_indices) == 1
    assert notif_indices[0] < assistant_indices[0]

    notif_msg = row["messages"][notif_indices[0]]
    assert notif_msg["role"] == "user"
    assert notif_msg["ui"] == {"kind": "subagent_notification"}
    assert "context_epoch" in notif_msg

    text = notif_msg["content"]
    assert "OldWorker" not in text
    assert "Old summary that must not be resent." not in text
    assert text.count("<subagent_notification>") == 8
    assert text.count("</subagent_notification>") == 8
    for early_idx, expected_status in ((0, "completed"), (1, "failed")):
        assert f"Nick{early_idx:02d}" in text
        assert f"/root/worker_{early_idx:02d}" in text
        assert expected_status in text
        assert f"Full body for subagent {early_idx:02d}" not in text
    for full_idx in range(2, 10):
        assert f"Full body for subagent {full_idx:02d}" in text

    unread_after = [n for n in row["subagent_notifications"] if not n.get("delivered")]
    assert unread_after == []
    assert all(n["delivered"] is True for n in row["subagent_notifications"])

    # Compatibility: ui.kind == "subagent_notification" is NOT an owner turn start
    refresh_hits: list[int] = []

    class _SpyExtensions:
        def refresh_catalogs_for_turn(self):
            refresh_hits.append(1)

        def plugin_context_note(self, _ctx, *, selected_skill_ids=()):
            return ""

    orig_extensions = getattr(runtime, "extensions", None)
    runtime.extensions = _SpyExtensions()
    try:
        probe_row = {
            **row,
            "messages": [
                {"role": "user", "content": "Owner turn", "message_id": "owner_1"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_plan_1",
                            "type": "function",
                            "function": {"name": "ryn_update_plan", "arguments": "{}"},
                        }
                    ],
                    "context_epoch": 0,
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_plan_1",
                    "content": "{\"ok\":true}",
                    "context_epoch": 0,
                },
                {
                    "role": "user",
                    "content": text,
                    "ui": {"kind": "subagent_notification"},
                    "context_epoch": 0,
                },
            ],
            "plan": {"revision": 1, "goal_revision": 1, "steps": [{"title": "Step 1", "status": "in_progress"}]},
        }
        runtime._refresh_plugin_catalogs_at_turn_start(probe_row)
        assert refresh_hits == []
        ctx_msgs = runtime._context(probe_row)
        assert isinstance(ctx_msgs, list) and len(ctx_msgs) > 0
    finally:
        runtime.extensions = orig_extensions


def test_s07_i1_t2_mid_request_notification_stays_unread_and_requeues_parent(tmp_path: Path):
    _, runtime, model, _ = _build_env(tmp_path)
    created = runtime.create(
        {
            "message": "Coordinate and finish only when all notifications are seen.",
            "model_id": "local/gpt-4o",
            "client_request_id": str(uuid.uuid4()),
        }
    )
    parent_id = created["id"]

    runtime.store.transaction(
        lambda data: data["tasks"][parent_id].setdefault("subagent_notifications", []).append(
            {
                "id": "subnotif_before",
                "agent_id": "child_before",
                "agent_path": "/root/before",
                "nickname": "Turing",
                "role": "explorer",
                "status": "completed",
                "summary": "Initial notification before model call.",
                "delivered": False,
            }
        )
    )

    step_calls = 0

    def _custom_complete(model_id, messages, specs, cancelled, on_event, **options):
        nonlocal step_calls
        step_calls += 1
        if step_calls == 1:
            runtime.store.transaction(
                lambda data: data["tasks"][parent_id].setdefault("subagent_notifications", []).append(
                    {
                        "id": "subnotif_during",
                        "agent_id": "child_during",
                        "agent_path": "/root/during",
                        "nickname": "Curie",
                        "role": "explorer",
                        "status": "completed",
                        "summary": "Arrived mid-request while model was thinking.",
                        "delivered": False,
                    }
                )
            )
            return {
                "message": {"role": "assistant", "content": "First attempt to finish."},
                "finish_reason": "stop",
                "usage": {"input_tokens": 30, "output_tokens": 12},
            }
        return {
            "message": {"role": "assistant", "content": "Final answer incorporating Curie."},
            "finish_reason": "stop",
            "usage": {"input_tokens": 45, "output_tokens": 15},
        }

    model.complete = _custom_complete

    # Step 1: mid-request notification arrives; parent must NOT complete and must leave it unread
    runtime.run_once()
    after_step_1 = runtime.store.read()["tasks"][parent_id]
    by_id_1 = {item["id"]: item for item in after_step_1["subagent_notifications"]}
    assert by_id_1["subnotif_before"]["delivered"] is True
    assert by_id_1["subnotif_during"]["delivered"] is False
    assert after_step_1["status"] == "queued"
    assert any(
        ev.get("type") == "turn_decision"
        and ev.get("action") == "continue"
        and ev.get("reason") == "subagent_notification"
        for ev in after_step_1["events"]
    )

    # Step 2: queued parent runs immediately, injects subnotif_during, persists & marks delivered, and completes
    runtime.run_once()
    after_step_2 = runtime.store.read()["tasks"][parent_id]
    by_id_2 = {item["id"]: item for item in after_step_2["subagent_notifications"]}
    assert by_id_2["subnotif_during"]["delivered"] is True
    assert after_step_2["status"] == "completed"
    notif_msgs = [
        msg["content"]
        for msg in after_step_2["messages"]
        if isinstance(msg, dict) and (msg.get("ui") or {}).get("kind") == "subagent_notification"
    ]
    assert len(notif_msgs) == 2
    assert "Initial notification before model call." in notif_msgs[0]
    assert "Arrived mid-request while model was thinking." in notif_msgs[1]


def test_s07_i1_t3_persisted_notification_retained_in_history_for_compaction(tmp_path: Path):
    _, runtime, model, _ = _build_env(tmp_path)
    created = runtime.create(
        {
            "message": "Read target file after subagent reports back.",
            "model_id": "local/gpt-4o",
            "client_request_id": str(uuid.uuid4()),
        }
    )
    parent_id = created["id"]

    runtime.store.transaction(
        lambda data: data["tasks"][parent_id].setdefault("subagent_notifications", []).append(
            {
                "id": "subnotif_for_compact",
                "agent_id": "child_compact",
                "agent_path": "/root/compact_explorer",
                "nickname": "Feynman",
                "role": "explorer",
                "status": "completed",
                "summary": "UNIQUE_COMPACTION_MARKER_S07_I1: module layout verified.",
                "delivered": False,
            }
        )
    )

    compaction_payloads: list[dict[str, Any]] = []
    normal_turn = 0

    orig_execute = runtime.tools.execute
    runtime.tools.execute = lambda name, arguments, **options: {
        "ok": True,
        "path": arguments.get("path"),
        "blob": "EVIDENCE_" + ("x" * 5200),
    }
    try:
        def _complete_with_compaction(model_id, messages, specs, cancelled, on_event, **options):
            nonlocal normal_turn
            if specs == []:
                compaction_payloads.append(json.loads(messages[-1]["content"]))
                return {
                    "message": {
                        "role": "assistant",
                        "content": "Summary: Feynman verified module layout; file read completed.",
                    },
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 50, "output_tokens": 20},
                }
            normal_turn += 1
            if normal_turn == 1:
                return {
                    "message": {
                        "role": "assistant",
                        "content": "Reading file based on Feynman report.",
                        "tool_calls": [
                            {
                                "id": "call_read_after_notif",
                                "type": "function",
                                "function": {
                                    "name": "ryn_file_read",
                                    "arguments": json.dumps({"path": "README.md"}),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                    "usage": {"input_tokens": 40, "output_tokens": 20},
                }
            return {
                "message": {"role": "assistant", "content": "Completed after compaction."},
                "finish_reason": "stop",
                "usage": {"input_tokens": 40, "output_tokens": 15},
            }

        model.complete = _complete_with_compaction

        # Step 1 (model call -> persists subagent_notification + tool call) & Step 2 (executes ryn_file_read)
        runtime.run_once()
        runtime.run_once()

        # Tighten max_context_chars so Step 3 must run _compact before dispatching the next model turn
        from rynmesh.agent_runtime.store import encoded

        snap_row = runtime.store.read()["tasks"][parent_id]
        pinned_probe = {
            **snap_row,
            "messages": [m for m in snap_row["messages"] if m.get("role") == "user" and (m.get("ui") or {}).get("kind") != "subagent_notification"],
            "context_checkpoint": None,
            "_exclude_optional_memory": True,
        }
        tight_cap = len(encoded(runtime._context(pinned_probe))) + 5200

        def _tighten(data: dict):
            r = data["tasks"][parent_id]
            r["limits"]["max_context_chars"] = tight_cap
            r["context_budget_provenance"] = "owner_manual"

        runtime.store.transaction(_tighten)

        # Step 3 compacts closed prefix (including persisted notification) and finishes
        runtime.run_once()
    finally:
        runtime.tools.execute = orig_execute

    final_row = runtime.store.read()["tasks"][parent_id]
    assert final_row["status"] == "completed"
    checkpoint = final_row.get("context_checkpoint")
    assert isinstance(checkpoint, dict)
    notif_idx = next(
        idx
        for idx, msg in enumerate(final_row["messages"])
        if isinstance(msg, dict) and (msg.get("ui") or {}).get("kind") == "subagent_notification"
    )
    assert checkpoint["through"] > notif_idx
    assert len(compaction_payloads) == 1
    summarized_history = compaction_payloads[0]["history"]
    assert any(
        isinstance(item, dict)
        and "UNIQUE_COMPACTION_MARKER_S07_I1" in str(item.get("content") or "")
        for item in summarized_history
    )


def test_s07_i1_t4_model_messages_exclude_subagent_notification_ids(tmp_path: Path):
    from rynmesh.agent_runtime.prompt import provider_messages

    _, runtime, model, _ = _build_env(tmp_path)
    created = runtime.create(
        {
            "message": "Check wire format of subagent notifications.",
            "model_id": "local/gpt-4o",
            "client_request_id": str(uuid.uuid4()),
        }
    )
    parent_id = created["id"]

    runtime.store.transaction(
        lambda data: data["tasks"][parent_id].setdefault("subagent_notifications", []).append(
            {
                "id": "subnotif_wire_01",
                "agent_id": "child_wire_01",
                "agent_path": "/root/wire_01",
                "nickname": "Lovelace",
                "role": "explorer",
                "status": "completed",
                "summary": "Wire payload verification text.",
                "delivered": False,
            }
        )
    )

    raw_row = runtime.store.read()["tasks"][parent_id]
    pending_tail = subagents.pending_for_request(raw_row)
    assert len(pending_tail) == 1
    assert pending_tail[0]["subagent_notification_ids"] == ["subnotif_wire_01"]
    projected_tail = provider_messages(pending_tail)
    assert "subagent_notification_ids" not in projected_tail[0]

    captured_wire_messages: list[list[dict[str, Any]]] = []

    def _capture_complete(model_id, messages, specs, cancelled, on_event, **options):
        captured_wire_messages.append([dict(m) for m in messages])
        return {
            "message": {"role": "assistant", "content": "Verified wire messages."},
            "finish_reason": "stop",
            "usage": {"input_tokens": 25, "output_tokens": 10},
        }

    model.complete = _capture_complete
    runtime.run_once()

    assert len(captured_wire_messages) == 1
    wire_msgs = captured_wire_messages[0]
    assert all("subagent_notification_ids" not in msg for msg in wire_msgs)
    assert any("Wire payload verification text." in str(msg.get("content") or "") for msg in wire_msgs)


def test_s07_i1_t5_wait_agent_marks_settled_parent_notification_delivered(tmp_path: Path):
    _, runtime, model, _ = _build_env(tmp_path)
    created = runtime.create(
        {
            "message": "Wait on paused child without leaving duplicate unread notifications.",
            "model_id": "local/gpt-4o",
            "client_request_id": str(uuid.uuid4()),
        }
    )
    parent_id = created["id"]

    def _spawn_and_pause_without_prior_settle(data: dict):
        parent_row = data["tasks"][parent_id]
        spawned = subagents.spawn_agent_in_tx(
            data,
            parent_row,
            {"task_name": "wait_target", "prompt": "Check wait settle delivery", "role": "explorer"},
        )
        child_row = data["tasks"][spawned["agent_id"]]
        child_row["status"] = "paused"
        child_row["result"] = "Paused checkpoint report."

    runtime.store.transaction(_spawn_and_pause_without_prior_settle)

    model.parent_responses.append(
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_wait_settle_sync",
                        "type": "function",
                        "function": {
                            "name": "ryn_wait_agent",
                            "arguments": json.dumps({"targets": ["Turing"], "timeout_seconds": 10}),
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
            "usage": {"input_tokens": 30, "output_tokens": 15},
        }
    )
    model.parent_responses.append(
        {
            "message": {"role": "assistant", "content": "Done waiting."},
            "finish_reason": "stop",
            "usage": {"input_tokens": 30, "output_tokens": 10},
        }
    )

    # Step 1: model issues ryn_wait_agent; Step 2: executes ryn_wait_agent
    runtime.run_once()
    runtime.run_once()

    after_wait = runtime.store.read()["tasks"][parent_id]
    parent_notifs = after_wait.get("subagent_notifications") or []
    assert len(parent_notifs) == 1
    assert parent_notifs[0]["delivered"] is True



