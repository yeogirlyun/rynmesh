"""Native public controls with actual signed-provider streaming callbacks."""
import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from test_agent_integrations import node  # noqa: F401
from test_agent_peer_binding import paired  # noqa: F401
from test_agent_reference_recovery import ROOT, native  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.ai_access.routes import install_ai_access
from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.store import RynmeshStore


def test_native_actual_stream_pause_same_session_continue_cancel(paired, native, monkeypatch, tmp_path, record_property):  # noqa: F811
    from documents import execute

    p = paired
    p.consumer.refresh(p.alice.peer_id)
    store = RynmeshStore(home=p.bob.store.root.parent, network_dir=tmp_path / "network")
    app = FastAPI()
    install_ai_access(app, home=store.home, local_control=lambda _: None, relationship=p.bob.store.relationship)
    app.state.ai_access.catalog = p.consumer
    install_llm_routes(app, store=store, home=store.home, messaging_key=p.bob.messaging_private,
                       resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    sent = []

    def synthetic_stream(body, **options):
        assert body["stream"] is True
        sent.append(body)
        for i in range(12):
            options["on_event"]({"choices": [{"delta": {"content": f"frame-{i}:" + "x" * 64}}]})
            time.sleep(.015)
        text = "".join(f"frame-{i}:" + "x" * 64 for i in range(12))
        return {"text": text, "message": {"role": "assistant", "content": text},
                "finish_reason": "stop", "input_tokens": 10, "output_tokens": 20, "duration_ms": 1,
                "reported_usage": {"input_tokens": 10, "output_tokens": 20}}

    monkeypatch.setattr(p.providers["a"].adapter.inner, "chat", synthetic_stream)

    async def exchange(*, signed_request, **options):
        response = await asyncio.to_thread(p.providers["a"].handle, signed_request, options.get("on_event"))
        return response, {"transport": "signed_envelope_fixture", "relay_used": False}

    monkeypatch.setattr("rynmesh.llm_package.routes.consumer_exchange", exchange)
    try:
        bridge, tools, catalog = build_agent_integrations(app, store, store.home)
        selected = next(m for m in catalog.list_models() if m["source"] == "peer")
        assert selected["streaming"] and selected["destination_binding_supported"]
        runtime = AgentRuntime(AgentStore(store.home / "agent", p.bob.messaging_private), bridge, tools, catalog,
                               consumer_node_id=store.peer_id)
        app.state.agent_runtime = runtime
        runtime.update_settings({"enabled": True, "model_id": selected["id"], "max_context_chars": 100000})
        case = next(c for c in json.loads((ROOT / "docs/acceptance/ryn-agent-delivery/cases-v1.json").read_text(encoding="utf-8"))["cases"] if c["id"] == "R05")
        prompt = case["prompt"]
        task = runtime.create({"message": prompt})
        frames, controls = [], []
        last_seen_revision = task["control_revision"]

        def on_event(event):
            frames.append(event)
            action = "pause" if len(frames) == 10 else "cancel" if len(frames) == 11 else None
            if action:
                assert runtime.get(task["id"])["control_revision"] == last_seen_revision
                controls.append(runtime.action(task["id"], {"action": action,
                    "expected_control_revision": last_seen_revision}))
            return action

        options = dict(runtime=runtime, tools=tools, catalog=catalog, selected=task["model_id"], task=task,
                       bridge=bridge, home=store.home, node=native[0], upstream=native[1],
                       real_model=False, native_business_operations=True, streaming=True, model_event=on_event)
        first = {"prompt": prompt, "approvals": []}
        execute(**options, record=first)
        assert len(frames) == 10 and len(sent) == 1
        assert runtime.get(task["id"])["status"] == "paused"
        stopped = first["reference"]["result"]
        assert stopped["owner_control"]["action"] == "pause"
        assert stopped["owner_control"]["result"]["kind"] == "stopped"
        paused = runtime.get(task["id"])
        continued = runtime.action(task["id"], {"action": "resume", "expected_control_revision": paused["control_revision"]})
        last_seen_revision = continued["control_revision"]
        second = {"prompt": prompt, "approvals": []}
        with pytest.raises(ValueError, match="reference_resume_binding_mismatch"):
            execute(**options, record=second, native_session_id=stopped["session_id"], resume_action="continue",
                    rpc_sequence_start=first["reference"]["last_rpc_id"] + 1)
        with pytest.raises(ValueError, match="reference_owner_turn_not_admitted"):
            execute(**options, record=second, native_session_id=stopped["session_id"], resume_action="owner_turn",
                    owner_message="Unrecorded coaching must be rejected",
                    rpc_sequence_start=first["reference"]["last_rpc_id"])
        execute(**options, record=second, native_session_id=stopped["session_id"], resume_action="continue",
                rpc_sequence_start=first["reference"]["last_rpc_id"])
        resumed = second["reference"]["result"]
        assert resumed["session_id"] == stopped["session_id"]
        assert resumed["owner_control"]["action"] == "cancel"
        assert resumed["owner_control"]["result"]["kind"] == "stopped"
        assert second["reference"]["worker_pid"] != first["reference"]["worker_pid"]
        assert len(sent) == 2 and len(frames) == 11
        assert second["reference"]["last_rpc_id"] > first["reference"]["last_rpc_id"]
        assert runtime.get(task["id"])["status"] == "cancelled"
        accounting = second["reference_budget"]["lifetime"]
        assert accounting["model_calls"] == accounting["charged_model_calls"] == 2
        assert accounting["unsettled_operations"] == 0
        assert all(item["dispatch_counts"] == {"model": 1, "tool": 0, "approval": 0}
                   for item in [first["reference"], second["reference"]])
        record_property("native_controls", json.dumps({"first": first, "second": second,
            "actual_provider_frames": len(frames), "model_calls": len(sent), "controls": controls,
            "paid_calls": False, "formal_pass": False}))
    finally:
        app.state.llm_shutdown()


def test_native_original_owner_correction_stops_unapproved_write_and_reopens_same_history(node, native, tmp_path, record_property):  # noqa: F811
    from documents import execute
    from sharing_cases import SharingWorld
    from sharing_recovery_worker import RecoveryFixturePlanner
    from worker_host import ReferenceOwnerControl

    from rynmesh.services import peer_box

    app, store = node
    case = next(c for c in json.loads((ROOT / "docs/acceptance/ryn-agent-delivery/cases-v1.json").read_text(encoding="utf-8"))["cases"] if c["id"] == "R04")
    correction = case["interventions"][0]["message"]
    with SharingWorld(tmp_path / "world", "R02") as world:
        selected = world.configure(app, store)
        _, tools, catalog = build_agent_integrations(app, store, store.home)
        world.attach(tools)
        bridge = RecoveryFixturePlanner()
        runtime = AgentRuntime(AgentStore(store.home / "agent", peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")),
                               bridge, tools, catalog, consumer_node_id=store.peer_id)
        app.state.agent_runtime = runtime
        runtime.update_settings({"enabled": True, "model_id": selected, "max_context_chars": 100000, "max_steps": 24})
        task = runtime.create({"message": case["prompt"]})
        source_bytes = world.sources.path.read_bytes()
        proposals = []

        def change_owner_goal(operation_id, request):
            proposals.append({"operation_id": operation_id, **request})
            assert request["name"] == "ryn_share_model_with_friend"
            current = runtime.get(task["id"])
            runtime.add_message(task["id"], {"message": correction, "intent": "steer",
                "expected_control_revision": current["control_revision"],
                "expected_goal_revision": current["goal"]["revision"]})
            raise ReferenceOwnerControl("steer")

        options = dict(runtime=runtime, tools=tools, catalog=catalog, selected=selected, task=task,
                       bridge=bridge, home=store.home, node=native[0], upstream=native[1],
                       real_model=False, native_business_operations=True,
                       allow_write=lambda _: pytest.fail("Superseded proposal cannot reach owner approval"))
        first = {"prompt": case["prompt"], "approvals": []}
        execute(**options, record=first, before_approval=change_owner_goal)
        assert first["reference"]["result"]["owner_control"]["result"]["kind"] == "stopped"
        assert len(proposals) == 1 and not first["approvals"] and not world.grants.list()
        captured = []

        def capture(_model, messages, *_args, **_options):
            captured.append(messages)
            # This bounded test stops before simulated model inference. It does
            # not claim that the original R04 status-check task has completed.
            raise ValueError("offline_owner_turn_capture_complete")

        bridge.complete = capture
        second = {"prompt": case["prompt"], "approvals": []}
        with pytest.raises(ValueError, match="offline_owner_turn_capture_complete"):
            execute(**options, record=second, native_session_id=first["reference"]["result"]["session_id"],
                    resume_action="owner_turn", owner_message=correction,
                    rpc_sequence_start=first["reference"]["last_rpc_id"])
        assert len(captured) == 1
        users = [m["content"] for m in captured[0] if m["role"] == "user"]
        assert users.count(correction) == 1 and case["prompt"] in users
        assert source_bytes == world.sources.path.read_bytes() and not world.grants.list()
        assert not second["tool_events"] and not second["approvals"]
        record_property("native_owner_turn", json.dumps({"first": first, "proposals": proposals,
            "original_prompt_retained": True, "correction_once": True, "writes": 0,
            "scope": "native proposal stop and same-session owner-turn admission only", "formal_pass": False}))
