"""Opt-in custom development trial of default selection; NOT frozen C03.

Two isolated source entries refer to the same actual upstream model. This is
not two independent model providers or physical-node acceptance evidence.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from planner_policy import SOURCE_ID, assert_mapping, map_fixture, read_planner  # noqa: E402


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--source-id", required=True)
    args = parser.parse_args()
    if args.source_id != SOURCE_ID:
        parser.error("only the configured GLM source is permitted")
    from fastapi import FastAPI

    from rynmesh.agent_runtime.bridge import build_agent_integrations
    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.store import AgentError, AgentStore
    from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
    from rynmesh.llm_package.routes import install_llm_routes
    from rynmesh.services import peer_box
    from rynmesh.store import RynmeshStore

    owner = Path(os.environ.get("RYNMESH_HOME", str(Path.home() / ".rynmesh")))
    owner_path = owner / "llm/sources.enc"
    owner_hash = digest(owner_path)
    source, adapter, planner_policy = read_planner(owner, args.source_id)
    model_name = planner_policy["model_id"]
    home = Path(tempfile.mkdtemp(prefix="ryn-default-live-"))
    os.environ.update(RYNMESH_HOME=str(home), RYNMESH_REGISTRY_URL="", RYNMESH_AUTO_REGISTER="0",
                      RYNMESH_LAN_DISCOVERY="0", RYNMESH_DEFAULT_DISCOVERY="0")
    output = Path(__file__).parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-default-model-development")
    output.mkdir()
    paths = [*sorted((ROOT / "rynmesh/agent_runtime").glob("*.py")),
             *sorted((ROOT / "rynmesh/llm_package").glob("*.py")), Path(__file__), Path(__file__).with_name("planner_policy.py")]
    snapshot = {str(p.relative_to(ROOT)).replace("\\", "/"): digest(p) for p in paths}
    record = {"kind": "real-custom-default-model-development", "formal_case_score": False,
              "not_frozen_case": "This isolates the default-model operation, not C03 sharing or reference scoring.",
              "model": model_name, "provider": source["provider"], "isolated_run_root": str(home),
              "planner_policy": planner_policy, "planner_policy_mapping": [],
              "code_snapshot": snapshot, "model_calls": [], "approvals": [],
              "started_at": datetime.now(timezone.utc).isoformat(), "semantic_review": "pending root and non-author review"}
    print(json.dumps({"output": str(output), "model": model_name}), flush=True)
    (output / "run.json").write_text(json.dumps({"collector_state": "initializing", "formal_case_score": False}), encoding="utf-8")
    began = time.monotonic()
    store = RynmeshStore(home=home, network_dir=home / "network")
    key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    app = FastAPI()
    install_llm_routes(app, store=store, home=home, messaging_key=key,
                       resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    services = app.state.llm_agent_services
    sources = services["source_store"]
    runtime, task = None, None
    try:
        bindings = {}
        for label in ("当前推理来源", "新任务专用来源"):
            row = sources.prepare({"name": label, "kind": "api", "provider": source["provider"],
                "base_url": source["base_url"], "api_key": "in-memory-placeholder", "models": [model_name],
                "context_window": source["context_window"],
                "request_defaults": source.get("request_defaults", {})})
            row.update(status="ready", tool_support={model_name: "supported"})
            record["planner_policy_mapping"].append(map_fixture(row, model_name, planner_policy))
            sources.save(row)
            bindings[label] = "local/" + sources.model_service_id(row, model_name)
        original_provider = services["provider_for"]
        def provider_for(service_id):
            manager = original_provider(service_id)
            if "local/" + service_id in bindings.values():
                if digest(owner_path) != owner_hash:
                    raise RuntimeError("owner_source_changed_before_dispatch")
                assert_mapping(sources, "local/" + service_id, planner_policy)
                if manager is None or not isinstance(manager.adapter, ModelDispatchAdapter):
                    raise RuntimeError("acceptance_dispatch_gate_missing")
                manager.adapter.inner = adapter
            return manager
        services["provider_for"] = provider_for
        bridge, tools, catalog = build_agent_integrations(app, store, home)
        complete = bridge.complete
        def traced(model_id, messages, specs, cancelled, on_event=None, **options):
            call = {"requested_model_id": model_id, "expected_destination": copy.deepcopy(options.get("expected_destination")),
                    "messages": [{k: v for k, v in copy.deepcopy(m).items() if k != "reasoning_content"} for m in messages],
                    "tools": copy.deepcopy(specs)}
            record["model_calls"].append(call)
            index, started = len(record["model_calls"]), time.monotonic()
            print(json.dumps({"phase": "model_started", "call": index, "has_tools": bool(specs)}), flush=True)
            try:
                result = complete(model_id, messages, specs, cancelled, on_event, **options)
                call["response"] = {"message": {k: v for k, v in result["message"].items() if k != "reasoning_content"},
                                    "usage": result.get("usage"), "finish_reason": result.get("finish_reason")}
                return result
            except AgentError as exc:
                call["error_code"] = str(exc) if str(exc).startswith("agent_") and len(str(exc)) < 100 else "agent_error"
                if hasattr(exc, "usage"):
                    call.update(usage=exc.usage, finish_reason=exc.finish_reason)
                raise
            finally:
                call["elapsed_seconds"] = time.monotonic() - started
                print(json.dumps({"phase": "model_finished", "call": index, "seconds": round(call["elapsed_seconds"], 3), "error": call.get("error_code")}), flush=True)
        bridge.complete = traced
        runtime = AgentRuntime(AgentStore(home / "agent", key), bridge, tools, catalog, consumer_node_id=store.peer_id)
        app.state.agent_runtime = runtime
        runtime.update_settings({"enabled": True, "model_id": bindings["当前推理来源"], "max_steps": 24,
                                 "max_seconds": 600, "max_context_chars": 100000})
        record.update(bindings=bindings, consumer_peer_id=store.peer_id, settings_before=runtime.settings(),
                      source_hash_before=digest(sources.path))
        prompt = "把“新任务专用来源”里的模型设为之后新任务的默认模型。当前任务继续使用原模型；助手开关、工具权限、预算和所有模型分享设置保持原样。完成后核对保存的实际状态并汇报。"
        record["prompt"] = prompt
        task = runtime.create({"message": prompt})
        for _ in range(160):
            task = runtime.get(task["id"])
            if task["status"] in {"completed", "failed", "paused", "cancelled", "interrupted", "waiting_input"}:
                break
            if task["status"] == "waiting_approval":
                approval = task["pending_approval"]
                permitted = (approval["tool"] == "ryn_set_default_agent_model"
                    and approval["arguments"].get("model_id") == bindings["新任务专用来源"]
                    and approval["preview"].get("applies_to") == "future_tasks_only")
                record["approvals"].append({"approved": permitted, "approval": copy.deepcopy(approval)})
                runtime.action(task["id"], {"action": "approve" if permitted else "deny",
                    "approval_id": approval["id"], "expected_control_revision": task["control_revision"]})
            else:
                runtime.run_once()
    except Exception as exc:
        record["error_type"] = type(exc).__name__
        if isinstance(exc, AgentError):
            record["error_code"] = str(exc) if str(exc).startswith("agent_") and len(str(exc)) < 100 else "agent_error"
    finally:
        if runtime is not None and task is not None:
            record["task"] = runtime.get(task["id"])
            record["settings_after"] = runtime.settings()
            record["source_unchanged"] = digest(sources.path) == record["source_hash_before"]
            record["mechanical_checks"] = {
                "default_selected": record["settings_after"]["model_id"] == bindings["新任务专用来源"],
                "current_task_model_unchanged": record["task"]["model_id"] == bindings["当前推理来源"],
                "other_settings_unchanged": record["settings_after"] == {**record["settings_before"], "model_id": bindings["新任务专用来源"]},
                "source_configuration_and_sharing_unchanged": record["source_unchanged"]}
        app.state.llm_shutdown()
        record.update(elapsed_seconds=time.monotonic() - began, ended_at=datetime.now(timezone.utc).isoformat(),
                      code_unchanged=all(digest(ROOT / name) == sha for name, sha in snapshot.items()),
                      owner_source_unchanged=digest(owner_path) == owner_hash)
        body = json.dumps(record, ensure_ascii=False, indent=2)
        if source.get("api_key") and source["api_key"] in body or '"reasoning_content"' in body:
            raise RuntimeError("evidence_export_guard")
        (output / "run.json").write_text(body, encoding="utf-8")
        print(json.dumps({"status": record.get("task", {}).get("status"), "outcome": record.get("task", {}).get("outcome"),
                          "checks": record.get("mechanical_checks"), "error": record.get("error_type")}), flush=True)


if __name__ == "__main__":
    main()
