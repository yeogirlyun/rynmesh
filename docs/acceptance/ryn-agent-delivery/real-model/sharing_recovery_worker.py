"""Replaceable R02 consumer; owner approvals are recorded, never pre-granted.

The parent hosts only business fixtures and terminates this process after the
production runtime durably reaches waiting_approval. Recovery opens the same
stores without reseeding sources, friends, permissions or Agent settings.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI  # noqa: E402
from planner_policy import assert_mapping, read_planner  # noqa: E402
from sharing_cases import SharingWorld  # noqa: E402

from rynmesh.agent_runtime.bridge import build_agent_integrations  # noqa: E402
from rynmesh.agent_runtime.output_policy import select  # noqa: E402
from rynmesh.agent_runtime.runtime import AgentRuntime  # noqa: E402
from rynmesh.agent_runtime.store import AgentError, AgentStore  # noqa: E402
from rynmesh.atomic_io import atomic_write_json  # noqa: E402
from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter  # noqa: E402
from rynmesh.llm_package.routes import install_llm_routes  # noqa: E402
from rynmesh.services import peer_box  # noqa: E402
from rynmesh.store import RynmeshStore  # noqa: E402


class RecoveryFixturePlanner:
    supports_destination_binding = True

    def complete(self, model_id, messages, specs, cancelled, on_event=None, **options):
        def answer(content):
            return {"message": {"role": "assistant", "content": content},
                    "usage": {"input_tokens": 1, "output_tokens": 1}, "finish_reason": "stop"}
        if not specs:
            evidence = json.loads(messages[-1]["content"])["evidence"]
            return answer(json.dumps({"status": "unknown", "summary": "Synthetic process-controller check only.",
                "uncovered_requirements": ["real independent semantic assessment"], "evidence_refs": [e["id"] for e in evidence]}))
        calls = {c["id"]: c["function"]["name"] for m in messages for c in m.get("tool_calls", [])}
        receipts = {calls.get(m.get("tool_call_id")): json.loads(m["content"]) for m in messages if m["role"] == "tool"}
        name, args = None, {}
        for read in ("ryn_list_models", "ryn_list_devices"):
            if read not in receipts:
                name = read
                break
        if name is None:
            model = next(r for r in receipts["ryn_list_models"]["models"] if r["fixture_identity"] == "model-b")
            friend = next(r for r in receipts["ryn_list_devices"]["friends"] if r["node_name"] == "node-b")
            name = "ryn_share_model_with_friend"
            args = {"model_id": model["id"], "relationship_id": friend["relationship_id"]}
            if name in receipts:
                if "ryn_inspect_services" not in receipts:
                    name, args = "ryn_inspect_services", {}
                else:
                    return answer("Synthetic recovery exercise ended. Real model quality is not assessed.")
        result = answer(None)
        result["message"]["tool_calls"] = [{"id": uuid.uuid4().hex, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]
        result["finish_reason"] = "tool_calls"
        return result


def main():
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    home, output = Path(config["home"]), Path(config["output"])
    phase = config["phase"]
    os.environ.update(RYNMESH_HOME=str(home), RYNMESH_REGISTRY_URL="", RYNMESH_AUTO_REGISTER="0",
                      RYNMESH_LAN_DISCOVERY="0", RYNMESH_DEFAULT_DISCOVERY="0")
    source, adapter, owner_path, owner_hash = {}, None, None, None
    if config["live"]:
        owner_home = Path(config["owner_home"])
        owner_path = owner_home / "llm/sources.enc"
        owner_hash = hashlib.sha256(owner_path.read_bytes()).hexdigest()
        if owner_hash != config["owner_source_sha256"]:
            raise RuntimeError("owner_source_changed_before_dispatch")
        source, adapter, planner_policy = read_planner(owner_home, config["source_id"])
        if planner_policy != config["planner_policy"]:
            raise RuntimeError("owner_planner_policy_changed")
    app = FastAPI()
    if config.get("release_activation"):
        from release_resume import existing_identity
        for path in (home, home / "receiver", home / "node-c"):
            existing_identity(path)
        existing_identity(home / "node-d", messaging=False)
    store = RynmeshStore(home=home, network_dir=home / "network")
    key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    install_llm_routes(app, store=store, home=home, messaging_key=key, resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    world = SharingWorld(home, "R02")
    world.registered_roles = bool(config.get("release_activation"))
    world.reopen(app, store, config["bindings"])
    selected = config["bindings"]["model-a"]
    if config["live"]:
        assert_mapping(world.sources, selected, planner_policy)
    services = app.state.llm_agent_services
    original_provider = services["provider_for"]
    def provider_for(service_id):
        manager = original_provider(service_id)
        if adapter is not None and service_id == selected.removeprefix("local/"):
            assert_mapping(world.sources, selected, planner_policy)
            if manager is None or not isinstance(manager.adapter, ModelDispatchAdapter):
                raise RuntimeError("acceptance_model_dispatch_gate_missing")
            manager.adapter.inner = adapter
        return manager
    services["provider_for"] = provider_for
    bridge, tools, catalog = build_agent_integrations(app, store, home)
    world.attach(tools)
    if not config["live"]:
        bridge = RecoveryFixturePlanner()
    original_complete = bridge.complete
    records = {"pid": os.getpid(), "phase": phase, "model_calls": [], "approvals": [], "source_replacements": [],
               "planner_model": config["planner_policy"]["model_id"] if config["live"] else "fixture",
               "planner_policy": config.get("planner_policy"), "provider": source.get("provider", "fixture")}
    secrets = [r.get("api_key") for r in world.sources.read().values()] + [source.get("api_key")]
    def save_records():
        text = json.dumps(records, ensure_ascii=False)
        if '"reasoning_content"' in text or any(secret and secret in text for secret in secrets):
            raise RuntimeError("evidence_export_guard")
        atomic_write_json(output / (phase + "-worker.json"), records, max_bytes=32 * 1024 * 1024)
    original_write = world.sources._write
    def observed_write(values):
        before = hashlib.sha256(world.sources.path.read_bytes()).hexdigest() if world.sources.path.exists() else None
        try:
            return original_write(values)
        finally:
            after = hashlib.sha256(world.sources.path.read_bytes()).hexdigest() if world.sources.path.exists() else None
            records["source_replacements"].append({"before_sha256": before, "after_sha256": after, "changed": before != after})
            save_records()
    world.sources._write = observed_write
    def traced(model_id, messages, specs, cancelled, on_event=None, **options):
        if any(secret and secret in json.dumps([messages, specs]) for secret in secrets):
            raise AgentError("agent_source_secret_egress_blocked")
        call = {"request_messages": [{k: copy.deepcopy(v) for k, v in m.items() if k != "reasoning_content"} for m in messages],
            "tools": copy.deepcopy(specs), "requested_model_id": model_id,
            "expected_destination": copy.deepcopy(options.get("expected_destination")),
            "credential_scan": "complete_in_memory_requests", "credential_egress_detected": False}
        if config["live"]:
            call["output_policy"] = select(catalog.resolve(model_id), messages, specs, request_cap=options.get("max_output_tokens"))
        records["model_calls"].append(call)
        begin = time.monotonic()
        print(json.dumps({"phase": phase, "model_started": len(records["model_calls"])}), flush=True)
        try:
            result = original_complete(model_id, messages, specs, cancelled, on_event, **options)
            if result.get("output_policy"):
                call["output_policy"] = result["output_policy"]
            call["response"] = {"message": {k: v for k, v in result["message"].items() if k != "reasoning_content"},
                                "usage": result.get("usage"), "finish_reason": result.get("finish_reason", "unknown")}
            return result
        except Exception as exc:
            call["error_type"] = type(exc).__name__
            if isinstance(exc, AgentError):
                call["error_code"] = str(exc) if str(exc).startswith("agent_") and len(str(exc)) < 100 else "agent_error"
                if hasattr(exc, "usage"):
                    call.update(usage=exc.usage, finish_reason=exc.finish_reason)
            raise
        finally:
            call["elapsed_seconds"] = time.monotonic() - begin
            save_records()
    bridge.complete = traced
    runtime = AgentRuntime(AgentStore(home / "agent", key), bridge, tools, catalog, consumer_node_id=store.peer_id,
        **({"owner_id": world.case["authorization"]["owner_id"]} if config.get("release_activation") else {}))
    app.state.agent_runtime = runtime
    tasks = runtime.store.read()["tasks"]
    if phase == "start":
        if tasks:
            raise RuntimeError("fresh_consumer_required")
        runtime.update_settings({"enabled": True, "model_id": selected, "max_steps": 24,
                                 "max_seconds": 600, "max_context_chars": 100000})
        task = runtime.create({"message": world.case["prompt"], "model_id": selected})
    else:
        if len(tasks) != 1:
            raise RuntimeError("exact_recovery_task_required")
        if config.get("cohort", "ryn") == "ryn":
            runtime.reconcile()
        task = runtime.get(next(iter(tasks)))
        records["reloaded_task"] = task
        save_records()
    from release_resume import install, native_objects, native_recovery
    node_stores = {"node-a": store, "node-b": world.receiver_store, "node-c": world.other_store}
    if config.get("release_activation"):
        node_stores["node-d"] = RynmeshStore(home=home / "node-d", network_dir=home / "fixed-network")
    collector = install(config, runtime=runtime, task_id=task["id"],
        node_stores=node_stores,
        source_stores={"node-a": world.sources}, bindings=world.bindings,
        native=native_objects(config, runtime, task["id"]))
    try:
        if config.get("cohort", "ryn") == "reference":
            native_recovery(config, runtime, task, bridge, tools, catalog, records, world.permitted, collector)
            if phase == "start":
                save_records()
                atomic_write_json(output / "approval-ready.json", {"pid": os.getpid(), "task": records["task"]}, max_bytes=16 * 1024 * 1024)
                threading.Event().wait(60)
                raise RuntimeError("parent_failed_to_kill_consumer")
            return
        for _ in range(160):
            task = runtime.get(task["id"])
            if task["status"] == "waiting_approval":
                if phase == "start":
                    records["task"] = task
                    save_records()
                    atomic_write_json(output / "approval-ready.json", {"pid": os.getpid(), "task": task}, max_bytes=16 * 1024 * 1024)
                    threading.Event().wait(60)
                    raise RuntimeError("parent_failed_to_kill_consumer")
                approval = task["pending_approval"]
                allowed = world.permitted(approval)
                records["approvals"].append({"approved": allowed, "approval": copy.deepcopy(approval)})
                runtime.action(task["id"], {"action": "approve" if allowed else "deny", "approval_id": approval["id"],
                                           "expected_control_revision": task["control_revision"]})
            elif task["status"] not in {"queued", "running"}:
                break
            else:
                runtime.run_once()
        records["task"] = runtime.get(task["id"])
    finally:
        if config.get("cohort", "ryn") == "reference":
            records["reference_session"] = runtime.store.read()["tasks"][task["id"]].get("reference_session")
        records["owner_source_unchanged"] = owner_path is None or hashlib.sha256(owner_path.read_bytes()).hexdigest() == owner_hash
        save_records()
        app.state.llm_shutdown()


if __name__ == "__main__":
    main()
