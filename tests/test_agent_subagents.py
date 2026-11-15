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
    from rynmesh.agent_runtime import tool_menu
    from rynmesh.agent_runtime.store import encoded

    quota = getattr(tool_menu, "SUBAGENT_SCHEMA_QUOTA_BYTES", None)
    assert isinstance(quota, int) and quota > 3600
    total_bytes = len(encoded(subagents.SUBAGENT_TOOL_SPECS))
    assert total_bytes <= quota

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


def test_s07_i7_t2_coordinator_guidance_uses_constants_and_workspace_explorer_rules():
    guidance = subagents.MULTI_AGENT_COORDINATOR_GUIDANCE
    assert str(subagents.MAX_OPEN_SUBAGENTS) in guidance
    assert str(subagents.MAX_SUBAGENT_DEPTH) in guidance
    custom = subagents.build_coordinator_guidance(max_open=11, max_depth=4)
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

    internal_cap = getattr(tool_menu, "INTERNAL_SPECS_QUOTA_BYTES", None)
    assert isinstance(internal_cap, int) and internal_cap > 11500
    all_internal_bytes = len(encoded(ALL_INTERNAL_SPECS))
    assert 11500 < all_internal_bytes <= internal_cap

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



