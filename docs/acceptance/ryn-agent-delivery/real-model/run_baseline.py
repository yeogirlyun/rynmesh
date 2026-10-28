"""Opt-in real-model baseline; changes only newly created isolated Ryn homes.

Run from the repository root with --live --source-id <existing-source-id>.
Existing credentials are read through SourceStore and used only in memory.
The existing source file is never written. No registry/LAN publication is used.
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
from planner_policy import (  # noqa: E402
    MODEL_ID,
    SOURCE_ID,
    assert_mapping,
    map_fixture,
    read_planner,
)

SAFE_MODEL_CODES = {"agent_inference_timeout", "agent_model_connection_failed", "agent_model_busy",
                    "agent_model_unavailable", "agent_inference_failed", "agent_cancelled", "agent_context_limit",
                    "agent_invalid_model_response", "agent_model_output_limit", "agent_unknown_tool"}


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def public_messages(messages):
    return [{k: v for k, v in row.items() if k != "reasoning_content"} for row in messages]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--model")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if not args.live or not 1 <= args.runs <= 3:
        parser.error("--live and 1..3 runs required")
    if args.source_id != SOURCE_ID or args.model not in (None, MODEL_ID):
        parser.error("only the configured GLM source and glm-5.3-flash are permitted")

    from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
    from rynmesh.llm_package.sources import SourceStore

    owner_home = Path(os.environ.get("RYNMESH_HOME", str(Path.home() / ".rynmesh")))
    owner_path = owner_home / "llm" / "sources.enc"
    original_hash = hashlib.sha256(owner_path.read_bytes()).hexdigest()
    source, live_adapter, planner_policy = read_planner(owner_home, args.source_id)
    model_name = planner_policy["model_id"]
    run_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = Path(__file__).parent / run_name
    output.mkdir()
    tracked = ["rynmesh/agent_runtime/" + name + ".py" for name in ("runtime", "store", "tools", "bridge", "models")]
    tracked += ["rynmesh/llm_package/" + name + ".py" for name in ("sources", "adapters", "tool_capabilities", "api_formats", "chat", "routes")]
    tracked += [str(Path(__file__).relative_to(ROOT)).replace("\\", "/")]
    tracked += [str(Path(__file__).with_name("planner_policy.py").relative_to(ROOT)).replace("\\", "/")]
    write(output / "code-snapshot.json", {
        "captured_at": datetime.now(timezone.utc).isoformat(), "capture_stage": "before any live inference",
        "files": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in tracked},
        "limits": {"model_calls": 16, "active_seconds": 600, "context_chars": 100000,
                   "output_chars": 20000, "planner_generation": planner_policy["generation"], "capability_probe_max_output_tokens": 4096, "runs": args.runs},
    })
    manifest = {
        "started_at": run_name, "provider": source.get("provider"), "model": model_name,
        "configured_source_id": args.source_id, "real_upstream": True,
        "planner_policy": planner_policy,
        "credentials": "existing SourceStore credential; in-memory adapter only",
        "scope": "isolated Ryn service stack + runtime, no browser or physical-peer test",
        "cost": "unknown", "runs": [], "probe": {},
    }
    print(json.dumps({"output": str(output), "provider": manifest["provider"], "model": model_name}), flush=True)
    try:
        start = time.monotonic()
        try:
            if hashlib.sha256(owner_path.read_bytes()).hexdigest() != original_hash:
                raise RuntimeError("owner_source_changed_before_dispatch")
            SourceStore.probe_adapter_tools(live_adapter, 4096)
            manifest["probe"] = {"passed": True, "elapsed_seconds": round(time.monotonic() - start, 3)}
            print("Explicit two-turn tool probe passed", flush=True)
        except Exception as exc:
            # SourceAdapter errors contain bounded upstream messages; do not print them.
            manifest["probe"] = {"passed": False, "error_type": type(exc).__name__, "code": getattr(exc, "code", None),
                                 "elapsed_seconds": round(time.monotonic() - start, 3)}
            print(json.dumps({"probe_failed": type(exc).__name__}), flush=True)
            return

        os.environ.update(RYNMESH_REGISTRY_URL="", RYNMESH_AUTO_REGISTER="0",
                          RYNMESH_LAN_DISCOVERY="0", RYNMESH_DEFAULT_DISCOVERY="0")
        from fastapi import FastAPI

        from rynmesh.agent_runtime.bridge import build_agent_integrations
        from rynmesh.agent_runtime.runtime import AgentRuntime
        from rynmesh.agent_runtime.store import AgentStore
        from rynmesh.llm_package.routes import install_llm_routes
        from rynmesh.services import peer_box
        from rynmesh.store import RynmeshStore

        for index in range(1, args.runs + 1):
            home = Path(tempfile.mkdtemp(prefix="ryn-real-model-baseline-"))
            os.environ["RYNMESH_HOME"] = str(home)
            store = RynmeshStore(home=home, network_dir=home / "network")
            app = FastAPI()
            messaging = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
            install_llm_routes(app, store=store, home=home, messaging_key=messaging,
                               resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
            store.personal_space.create("Isolated acceptance space")
            store.personal_space.set_policy("space")
            services = app.state.llm_agent_services
            sources = services["source_store"]
            sibling = "private-fixture-model"
            fixture = sources.prepare({
                "name": "Isolated acceptance source", "kind": "api", "provider": source.get("provider", "custom"),
                "base_url": source["base_url"], "api_key": "in-memory-adapter-placeholder",
                "models": [model_name, sibling], "context_window": source["context_window"],
                "request_defaults": source.get("request_defaults", {}),
            })
            fixture.update(status="ready", tool_support={model_name: "supported"})
            mapping = map_fixture(fixture, model_name, planner_policy)
            sources.save(fixture)
            selected = "local/" + sources.model_service_id(fixture, model_name)
            original_provider = services["provider_for"]

            def provider_for(service_id, original_provider=original_provider, selected=selected, sources=sources):
                manager = original_provider(service_id)
                if service_id == selected.removeprefix("local/"):
                    if hashlib.sha256(owner_path.read_bytes()).hexdigest() != original_hash:
                        raise RuntimeError("owner_source_changed_before_dispatch")
                    assert_mapping(sources, selected, planner_policy)
                    if manager is None or not isinstance(manager.adapter, ModelDispatchAdapter):
                        raise RuntimeError("acceptance_model_dispatch_gate_missing")
                    manager.adapter.inner = live_adapter
                return manager

            services["provider_for"] = provider_for
            bridge, tools, catalog = build_agent_integrations(app, store, home)
            model_calls = []
            original_complete = bridge.complete

            def complete(model_id, messages, tool_specs, cancelled, on_event=None, *,
                         model_calls=model_calls, original_complete=original_complete, **kwargs):
                call = {"request_messages": public_messages(copy.deepcopy(messages)),
                        "tools": copy.deepcopy(tool_specs), "started_monotonic": time.monotonic()}
                model_calls.append(call)
                try:
                    response = original_complete(model_id, messages, tool_specs, cancelled, on_event, **kwargs)
                    call["response"] = {"message": public_messages([response["message"]])[0], "usage": response.get("usage")}
                    return response
                except Exception as exc:
                    call["error_type"] = type(exc).__name__
                    call["error_code"] = str(exc) if str(exc) in SAFE_MODEL_CODES else None
                    raise
                finally:
                    call["elapsed_seconds"] = round(time.monotonic() - call.pop("started_monotonic"), 3)

            bridge.complete = complete
            runtime = AgentRuntime(AgentStore(home / "agent", messaging), bridge, tools, catalog)
            app.state.agent_runtime = runtime
            runtime.update_settings({"enabled": True, "model_id": selected, "max_steps": 16,
                                     "max_seconds": 600, "max_context_chars": 100000})
            prompt = (
                "请完成隔离测试节点的模型分享检查与恢复。先盘点当前模型、设备连接证据和模型服务状态，"
                f"然后仅把 {model_name} 分享给当前个人空间，并重新读取模型状态确认分享成功且 "
                f"{sibling} 保持私有。确认后撤销刚才对 {model_name} 的分享，再重新读取确认两个模型均为私有，"
                "同时保留当前助手默认模型。最后按真实工具证据报告初始状态、分享后的状态、恢复后的状态及设备证据。"
                "这是已授权的临时测试节点，写操作仍按运行时审批执行。不得修改任何其他模型或好友权限。"
            )
            task = runtime.create({"message": prompt})
            approvals, snapshots = [], []
            started = time.monotonic()
            try:
                for _ in range(100):
                    current = runtime.get(task["id"])
                    if current["status"] in {"completed", "failed", "interrupted", "cancelled", "paused"}:
                        break
                    if current["status"] == "waiting_approval":
                        pending = current["pending_approval"]
                        expected_enabled = len(approvals) == 0
                        permitted = (len(approvals) < 2 and pending["tool"] == "ryn_set_model_sharing"
                                     and pending["arguments"] == {"model_id": selected, "enabled": expected_enabled}
                                     and pending["preview"]["scope"] == "personal_space")
                        approvals.append({"approved": permitted, "approval": pending})
                        runtime.action(task["id"], {"action": "approve" if permitted else "deny",
                            "expected_revision": current["revision"], "approval_id": pending["id"]})
                    else:
                        runtime.run_once()
                    snapshots.append({"shared_models": sources.shared_models(sources.read()[fixture["id"]]),
                                      "default_model": runtime.settings()["model_id"]})
                current = runtime.get(task["id"])
                results = [e for e in current["events"] if e["type"] == "tool_result"]
                read_counts = {name: sum(e["tool"] == name for e in results)
                               for name in ("ryn_list_models", "ryn_list_devices", "ryn_inspect_services")}
                passed = (current["status"] == "completed" and len(results) >= 7
                          and read_counts["ryn_list_models"] >= 3 and read_counts["ryn_list_devices"] >= 1
                          and read_counts["ryn_inspect_services"] >= 1
                          and len(approvals) == 2 and all(a["approved"] for a in approvals)
                          and any(s["shared_models"] == [model_name] for s in snapshots)
                          and snapshots[-1]["shared_models"] == []
                          and all(s["default_model"] == selected and sibling not in s["shared_models"] for s in snapshots))
                record = {"index": index, "passed": passed, "task": current, "approvals": approvals,
                          "planner_policy": planner_policy, "planner_policy_mapping": [mapping],
                          "state_snapshots": snapshots, "model_calls": model_calls,
                          "tool_results": len(results), "read_counts": read_counts,
                          "elapsed_seconds": round(time.monotonic() - started, 3),
                          "fixture_sibling": "configured private sentinel; not claimed to be a real upstream model"}
                write(output / f"run-{index}.json", record)
                summary = {k: record[k] for k in ("index", "passed", "tool_results", "read_counts", "elapsed_seconds")}
                summary.update(status=current["status"], error=current["error"], model_calls=len(model_calls))
                manifest["runs"].append(summary)
                print(json.dumps(summary), flush=True)
            finally:
                app.state.llm_shutdown()
            write(output / "manifest.json", manifest)
    finally:
        manifest["owner_source_unchanged"] = hashlib.sha256(owner_path.read_bytes()).hexdigest() == original_hash
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        snapshot = json.loads((output / "code-snapshot.json").read_text(encoding="utf-8"))
        manifest["business_code_unchanged"] = all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
            for name, digest in snapshot["files"].items() if name.startswith("rynmesh/"))
        # Ensure neither credential nor provider reasoning is exported as evidence.
        if source.get("api_key") and source["api_key"] in json.dumps(manifest):
            raise RuntimeError("credential export guard")
        write(output / "manifest.json", manifest)
        for evidence in output.glob("*.json"):
            raw = evidence.read_text(encoding="utf-8")
            if (source.get("api_key") and source["api_key"] in raw) or '"reasoning_content"' in raw:
                raise RuntimeError("evidence privacy guard")


if __name__ == "__main__":
    main()
