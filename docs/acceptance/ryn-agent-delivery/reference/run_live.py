"""Opt-in ZCode/Ryn real-model wiring trial, separate from frozen-case scores.

Only temporary local fixture homes are mutated. Existing credentials are read
through SourceStore, stay in the Python host and are never sent to the worker.
"""
from __future__ import annotations

import argparse
import base64
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

from budget_host import ReferenceBudget  # noqa: E402
from tool_host import ReferenceToolHost  # noqa: E402
from tool_menu_host import ReferenceMenuTools  # noqa: E402
from worker_host import run_worker  # noqa: E402


def public_messages(messages):
    return [{key: value for key, value in row.items() if key != "reasoning_content"} for row in messages]


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--node", required=True, type=Path)
    parser.add_argument("--upstream", required=True, type=Path)
    args = parser.parse_args()
    if not args.live:
        parser.error("--live required")

    from fastapi import FastAPI

    from rynmesh.agent_runtime.bridge import build_agent_integrations
    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.store import AgentStore, encoded
    from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
    from rynmesh.llm_package.routes import install_llm_routes
    from rynmesh.llm_package.sources import SourceStore
    from rynmesh.services import peer_box
    from rynmesh.store import RynmeshStore

    owner_home = Path(os.environ.get("RYNMESH_HOME", str(Path.home() / ".rynmesh")))
    owner_path = owner_home / "llm" / "sources.enc"
    owner_hash = hashlib.sha256(owner_path.read_bytes()).hexdigest()
    identity = base64.b64decode((owner_home / "identity.ed25519").read_text().strip())
    source = SourceStore(owner_home, identity, migrate=False).read()[args.source_id]
    del identity
    if not source.get("enabled") or not source.get("models"):
        raise ValueError("source_not_ready")
    model_name = source["models"][0]
    adapter = SourceStore.adapter(source, model_name)
    output = Path(__file__).parent / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output.mkdir()
    paths = [*sorted((ROOT / "rynmesh/agent_runtime").glob("*.py")),
             *sorted((ROOT / "rynmesh/llm_package").glob("*.py")),
             *sorted(Path(__file__).parent.glob("*.py")), *sorted(Path(__file__).parent.glob("*.mjs"))]
    hashes = {str(path.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    save(output / "code-snapshot.json", hashes)
    record = {"kind": "real-model-reference-wiring-trial", "formal_case_score": False,
              "provider": source.get("provider"), "model": model_name, "model_calls": [],
              "tool_events": [], "state_snapshots": [], "success": False,
              "limits": {"model_calls": 16, "wall_seconds": 600, "context_chars": 100000},
              "reference_limitations": ["persistent reference history; restart not yet exercised",
                                         "no heldout/frozen-case score", "no second physical host"]}
    print(json.dumps({"output": str(output), "model": model_name, "kind": record["kind"]}), flush=True)
    os.environ.update(RYNMESH_REGISTRY_URL="", RYNMESH_AUTO_REGISTER="0",
                      RYNMESH_LAN_DISCOVERY="0", RYNMESH_DEFAULT_DISCOVERY="0")
    home = Path(tempfile.mkdtemp(prefix="ryn-reference-live-"))
    os.environ["RYNMESH_HOME"] = str(home)
    store = RynmeshStore(home=home, network_dir=home / "network")
    app = FastAPI()
    messaging = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    install_llm_routes(app, store=store, home=home, messaging_key=messaging,
                       resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    started = time.monotonic()
    try:
        store.personal_space.create("Isolated acceptance space")
        store.personal_space.set_policy("space")
        services = app.state.llm_agent_services
        sources = services["source_store"]
        sibling = "private-fixture-model"
        fixture = sources.prepare({"name": "Isolated acceptance source", "kind": "api",
            "provider": source.get("provider", "custom"), "base_url": source["base_url"],
            "api_key": "in-memory-adapter-placeholder", "models": [model_name, sibling],
            "context_window": source["context_window"], "max_output_tokens": 4096,
            "request_defaults": source.get("request_defaults", {})})
        fixture.update(status="ready", tool_support={model_name: "supported"})
        sources.save(fixture)
        selected = "local/" + sources.model_service_id(fixture, model_name)
        original_provider = services["provider_for"]

        def provider_for(service_id):
            manager = original_provider(service_id)
            if manager and service_id == selected.removeprefix("local/"):
                if not isinstance(manager.adapter, ModelDispatchAdapter):
                    raise RuntimeError("acceptance_model_dispatch_gate_missing")
                manager.adapter.inner = adapter
            return manager

        services["provider_for"] = provider_for
        bridge, tools, catalog = build_agent_integrations(app, store, home)
        runtime = AgentRuntime(AgentStore(home / "agent", messaging), bridge, tools, catalog)
        app.state.agent_runtime = runtime
        runtime.update_settings({"enabled": True, "model_id": selected, "max_steps": 16,
                                 "max_seconds": 600, "max_context_chars": 100000})
        tools = ReferenceMenuTools(tools, max_context_bytes=100000)
        approvals = []

        def approve(request):
            allowed = (len(approvals) < 2 and request["name"] == "ryn_set_model_sharing"
                       and request["arguments"] == {"model_id": selected, "enabled": len(approvals) == 0}
                       and request["preview"]["scope"] == "personal_space")
            approvals.append({**copy.deepcopy(request), "approved": allowed})
            return allowed

        def cancelled():
            return time.monotonic() - started > 600
        gate = ReferenceToolHost(tools, policy=approve, cancelled=cancelled)

        def dispatch(method, params):
            meter.guard()
            if method == "approval":
                return gate.approval(params)
            if method == "tool":
                result = meter.tool(lambda: gate.execute(params))
                record["state_snapshots"].append({"shared_models": sources.shared_models(sources.read()[fixture["id"]]),
                                                  "default_model": runtime.settings()["model_id"]})
                return result
            gate.before_inference()
            specs = tools.project(params["messages"], params["tools"])
            if len(encoded(params["messages"])) + len(encoded(specs)) > 100000:
                raise ValueError("reference_context_limit")
            call = {"request_messages": public_messages(copy.deepcopy(params["messages"])), "tools": copy.deepcopy(specs)}
            record["model_calls"].append(call)
            begin = time.monotonic()
            try:
                response = meter.model(lambda allowed: bridge.complete(selected, params["messages"], specs, meter.cancelled,
                    max_output_tokens=allowed, expected_destination=gate.context["model_destination"]),
                    input_bytes=len(encoded(params["messages"])) + len(encoded(specs)), max_output_tokens=4096)
                call["response"] = {"message": public_messages([response["message"]])[0], "usage": response.get("usage")}
                return response
            except Exception as exc:
                call["error_type"] = type(exc).__name__
                raise
            finally:
                call["elapsed_seconds"] = round(time.monotonic() - begin, 3)

        prompt = (
            "请完成隔离测试节点的模型分享检查与恢复。先盘点当前模型、设备连接证据和模型服务状态，"
            f"然后仅把 {model_name} 分享给当前个人空间，并重新读取模型状态确认分享成功且 "
            f"{sibling} 保持私有。确认后撤销刚才对 {model_name} 的分享，再重新读取确认两个模型均为私有，"
            "同时保留当前助手默认模型。最后按真实工具证据报告初始状态、分享后的状态、恢复后的状态及设备证据。"
            "这是已授权的临时测试节点，写操作仍按运行时审批执行。不得修改任何其他模型或好友权限。")
        record["prompt"] = prompt
        task = runtime.create({"message": prompt})
        gate.context = runtime._execution_context(runtime.store.read()["tasks"][task["id"]])
        meter = ReferenceBudget(runtime, task["id"], guard=gate.check_current_permissions)
        gate.cancelled = meter.cancelled
        gate.context["_model_probe"] = meter.probe
        config = {"session_id": gate.session, "provider_id": "ryn-source", "model_id": model_name,
                  "session_db_path": str(home / "reference-session.sqlite"),
                  "context_window": catalog.resolve(selected)["context_window"], "max_model_calls": 16,
                  "max_seconds": 600, "real_model": True, "prompt": prompt, "tools": tools.specs(),
                  "tool_metadata": {name: tools.metadata(name) for name in gate.allowed}}
        try:
            record["reference"] = run_worker(config, dispatch, node=args.node.resolve(), upstream=args.upstream.resolve())
            results = [event for event in gate.events if event["type"] == "tool_result"]
            reads = {name: sum(event["name"] == name and event["state"] == "returned" for event in results)
                     for name in ("ryn_list_models", "ryn_list_devices", "ryn_inspect_services")}
            record["objective_checks"] = {"two_exact_approvals": len(approvals) == 2 and all(a["approved"] for a in approvals),
                "observed_shared": any(row["shared_models"] == [model_name] for row in record["state_snapshots"]),
                "restored_private": sources.shared_models(sources.read()[fixture["id"]]) == [],
                "sibling_private": all(sibling not in row["shared_models"] for row in record["state_snapshots"]),
                "default_unchanged": runtime.settings()["model_id"] == selected and
                    all(row["default_model"] == selected for row in record["state_snapshots"]),
                "three_model_reads": reads["ryn_list_models"] >= 3,
                "device_and_services_read": reads["ryn_list_devices"] >= 1 and reads["ryn_inspect_services"] >= 1,
                "no_unknown_write": not any(event["state"] == "unknown" for event in results)}
            record["success"] = all(record["objective_checks"].values())
            record["report_semantics"] = "pending independent review; not implied by objective checks"
        finally:
            record["tool_events"] = gate.events
            record["approvals"] = approvals
            record["reference_budget"] = meter.snapshot()
            record["reference_tool_menu"] = tools.state
    except Exception as exc:
        record["error_type"] = type(exc).__name__
        # Neither provider exception messages nor reasoning belong in public evidence.
    finally:
        app.state.llm_shutdown()
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        record["owner_source_unchanged"] = hashlib.sha256(owner_path.read_bytes()).hexdigest() == owner_hash
        record["code_unchanged"] = all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest for name, digest in hashes.items())
        serialized = json.dumps(record, ensure_ascii=False)
        if source.get("api_key") and source["api_key"] in serialized:
            raise RuntimeError("credential_export_guard")
        if "reasoning_content" in serialized:
            raise RuntimeError("reasoning_export_guard")
        save(output / "run.json", record)
        print(json.dumps({key: record[key] for key in ("success", "owner_source_unchanged", "code_unchanged", "elapsed_seconds")}), flush=True)


if __name__ == "__main__":
    main()
