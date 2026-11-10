"""Development document trials with actual isolated DAV and authoritative scope.

Uses the frozen task verbatim. This early integration trial is not a formal
same-snapshot suite score; independent report/artifact review remains required.
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
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from document_cases import SUPPORTED, DocumentCase, load_case, model_request_record  # noqa: E402
from planner_policy import SOURCE_ID, assert_mapping, map_fixture, read_planner  # noqa: E402


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def public_messages(messages):
    return [{key: value for key, value in row.items() if key != "reasoning_content"} for row in messages]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--case", choices=sorted(SUPPORTED), default="F02")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--cohort", choices=("ryn", "reference"), default="ryn")
    parser.add_argument("--node", type=Path)
    parser.add_argument("--upstream", type=Path)
    from release_collection import add_arguments
    add_arguments(parser)
    args = parser.parse_args()
    if not args.live:
        parser.error("--live required")
    if args.source_id != SOURCE_ID:
        parser.error("only the configured GLM source is permitted")
    if args.cohort == "reference" and (not args.node or not args.upstream):
        parser.error("reference requires --node and --upstream")
    from fastapi import FastAPI

    from rynmesh.agent_runtime.bridge import build_agent_integrations
    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.store import AgentError, AgentStore
    from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
    from rynmesh.llm_package.routes import install_llm_routes
    from rynmesh.nas import NasPlugin
    from rynmesh.services import peer_box
    from rynmesh.store import RynmeshStore

    original_home = Path(os.environ.get("RYNMESH_HOME", str(Path.home() / ".rynmesh")))
    original_path = original_home / "llm/sources.enc"
    original_hash = digest(original_path)
    source, adapter, planner_policy = read_planner(original_home, args.source_id)
    model_name = planner_policy["model_id"]
    case = load_case(args.case)
    output = Path(__file__).parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + args.case + "-" + args.cohort)
    output.mkdir()
    paths = sorted((ROOT / "rynmesh/agent_runtime").glob("*.py")) + sorted((ROOT / "rynmesh/llm_package").glob("*.py"))
    paths += [Path(__file__), Path(__file__).with_name("document_cases.py"), Path(__file__).with_name("planner_policy.py"),
              ROOT / "tests/fixtures/agent_acceptance/webdav_fixture.py", ROOT / "docs/acceptance/ryn-agent-delivery/cases-v1.json"]
    paths += [ROOT / "tests/fixtures/agent_acceptance" / relative for relative in case["fixture_refs"]]
    snapshot = {str(p.relative_to(ROOT)).replace("\\", "/"): digest(p) for p in paths}
    record = {"kind": "real-document-development-trial", "formal_case_score": False, "case_id": args.case, "cohort": args.cohort,
              "prompt": case["prompt"], "model": model_name, "provider": source["provider"],
              "planner_policy": planner_policy, "planner_policy_mapping": [],
              "model_calls": [], "approvals": [], "code_snapshot": snapshot,
              "semantic_review": "pending independent review"}
    (output / "run.json").write_text(json.dumps({"case_id": args.case, "formal_case_score": False,
                                                "collector_state": "initializing"}) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "model": model_name}), flush=True)
    home = Path(tempfile.mkdtemp(prefix="ryn-document-live-"))
    os.environ.update(RYNMESH_HOME=str(home), RYNMESH_REGISTRY_URL="", RYNMESH_AUTO_REGISTER="0",
                      RYNMESH_LAN_DISCOVERY="0", RYNMESH_DEFAULT_DISCOVERY="0")
    started = time.monotonic()
    record["started_at"] = datetime.now(timezone.utc).isoformat()
    record["isolated_run_root"] = str(home)
    collector = None
    with ExitStack() as setup:
        world = None
        if args.release_activation:
            from release_world import ReleaseWorld
            world = setup.enter_context(ReleaseWorld(home / "roles", args.case, planner_policy))
            home = world.execution_store.home
        documents = setup.enter_context(DocumentCase(home, args.case))
        store = world.execution_store if world else RynmeshStore(home=home, network_dir=home / "network")
        messaging = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
        app = world.execution_app if world else FastAPI()
        if not world:
            install_llm_routes(app, store=store, home=home, messaging_key=messaging,
                               resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
        nas = app.state.nas_plugin = NasPlugin(home)
        nas.enable(True)
        scope = documents.configure(nas)
        try:
            sources = world.sources if world else app.state.llm_agent_services["source_store"]
            fixture = None if world else sources.prepare({"name": "Isolated real inference", "kind": "api", "provider": source["provider"],
                "base_url": source["base_url"], "api_key": "in-memory-adapter-placeholder", "models": [model_name],
                "context_window": source["context_window"],
                "request_defaults": source.get("request_defaults", {})})
            if fixture is not None:
                fixture.update(status="ready", tool_support={model_name: "supported"})
                record["planner_policy_mapping"].append(map_fixture(fixture, model_name, planner_policy))
                sources.save(fixture)
            else:
                record["planner_policy_mapping"] = copy.deepcopy(world.planner_policy_mapping)
            selected = world.selected if world else "local/" + sources.model_service_id(fixture, model_name)
            provider_selected = world.provider_selected if world else selected
            services = (world.owner_app if world else app).state.llm_agent_services
            original_provider = services["provider_for"]

            def provider_for(service_id):
                manager = original_provider(service_id)
                if service_id == provider_selected.removeprefix("local/"):
                    if digest(original_path) != original_hash:
                        raise RuntimeError("owner_source_changed_before_dispatch")
                    assert_mapping(sources, provider_selected, planner_policy)
                    if manager is None or not isinstance(manager.adapter, ModelDispatchAdapter):
                        raise RuntimeError("acceptance_model_dispatch_gate_missing")
                    manager.adapter.inner = adapter
                return manager

            services["provider_for"] = provider_for
            bridge, tools, catalog = build_agent_integrations(app, store, home)
            complete = bridge.complete

            def traced_complete(model_id, messages, specs, cancelled, on_event=None, **kwargs):
                call = model_request_record(model_id, messages, specs, kwargs.get("expected_destination"))
                if collector is not None:
                    call["dispatch_provenance"] = collector.model_request()
                record["model_calls"].append(call)
                call_number = len(record["model_calls"])
                print(json.dumps({"phase": "model_started", "call": call_number, "has_tools": bool(specs)}), flush=True)
                begin = time.monotonic()
                try:
                    result = complete(model_id, messages, specs, cancelled, on_event, **kwargs)
                    call["response"] = {"message": public_messages([result["message"]])[0], "usage": result.get("usage"), "finish_reason": result.get("finish_reason", "unknown")}
                    return result
                except (AgentError, ValueError) as exc:
                    call["error_code"] = str(exc) if str(exc).startswith("agent_") and len(str(exc)) < 100 else "agent_error"
                    if hasattr(exc, "usage") and hasattr(exc, "finish_reason"):
                        call.update(usage=exc.usage, finish_reason=exc.finish_reason)
                    raise
                finally:
                    call["elapsed_seconds"] = time.monotonic() - begin
                    print(json.dumps({"phase": "model_finished", "call": call_number,
                        "elapsed_seconds": round(call["elapsed_seconds"], 3), "error": call.get("error_code"),
                        "finish_reason": call.get("finish_reason", call.get("response", {}).get("finish_reason"))}), flush=True)

            bridge.complete = traced_complete
            runtime = AgentRuntime(AgentStore(home / "agent", messaging), bridge, tools, catalog, consumer_node_id=store.peer_id,
                                   **({"owner_id": case["authorization"]["owner_id"]} if world else {}))
            record["consumer_peer_id"] = store.peer_id
            app.state.agent_runtime = runtime
            runtime.update_settings({"enabled": True, "model_id": selected, "max_steps": 24,
                                     "max_seconds": 600, **documents.settings()})
            documents.prepare_previous_task(runtime, selected)
            task = runtime.create({"message": case["prompt"], "scope": scope,
                                   "expected_destination_revision": catalog.resolve(selected)["destination_revision"]})
            if world:
                from release_collection import registry
                collector = world.collector(args.release_activation, output, args.cohort, args.trial, runtime, task,
                    connectors={"node-b": nas}, histories={"node-b": runtime.store}, bindings=documents.bindings,
                    node=args.node, upstream=args.upstream)
                if args.cohort == "ryn":
                    collector.register()
                    collector.attach_candidate()
            record["initial_model_id"] = task["model_id"]
            record["initial_model_destination"] = copy.deepcopy(task["model_destination"])
            record["isolated_source_configuration_before"] = digest(sources.path)
            documents.after_scope(nas)
            documents.start_evaluation()
            record["scope_selection"] = scope
            if args.cohort == "reference":
                sys.path.insert(0, str(Path(__file__).parent.parent / "reference"))
                from documents import execute
                execute(runtime=runtime, tools=tools, catalog=catalog, selected=selected, task=task,
                        bridge=bridge, record=record, home=home, node=args.node, upstream=args.upstream,
                        **({"dispatch_observer": collector, "dispatch_provenance": collector.native_config()} if collector else {}),
                        allow_write=lambda request: documents.approve({**request, "tool": request["name"]}, tools=tools,
                            context=runtime._execution_context(runtime.store.read()["tasks"][task["id"]])))
            for _ in range(160 if args.cohort == "ryn" else 0):
                task = runtime.get(task["id"])
                if task["status"] in {"completed", "failed", "paused", "cancelled", "interrupted", "waiting_input"}:
                    break
                if task["status"] == "waiting_approval":
                    approval = task["pending_approval"]
                    # Exact case paths only. Generated content is judged after
                    # execution, never edited/coached by the harness.
                    permitted = documents.approve(approval, tools=tools, context=runtime._execution_context(runtime.store.read()["tasks"][task["id"]]))
                    record["approvals"].append({"approved": permitted, "approval": copy.deepcopy(approval)})
                    runtime.action(task["id"], {"action": "approve" if permitted else "deny",
                        "approval_id": approval["id"], "expected_control_revision": task["control_revision"]})
                else:
                    runtime.run_once()
            if args.cohort == "ryn":
                record["task"] = runtime.get(task["id"])
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            if isinstance(exc, AgentError):
                record["error_code"] = str(exc) if str(exc).startswith("agent_") and len(str(exc)) < 100 else "agent_error"
        finally:
            # Failed inference can follow a committed write. Always retain the
            # real state, including failed runs; never erase it by early return.
            record.update(documents.collect(task=record.get("task"), model_calls=record["model_calls"]))
            if collector is not None:
                record["release_collection"] = collector.finalize()
                if collector.registration_path:
                    record.update(registry.read(collector.registration_path)["binding"])
                record.update(real_model=True, bindings={**world.bindings, **documents.bindings}, isolated_run_root=str(home))
            record["artifacts"] = record["artifact_readback"]
            if "isolated_source_configuration_before" in record:
                record["isolated_source_configuration_after"] = digest(sources.path)
                record["isolated_source_configuration_unchanged"] = record["isolated_source_configuration_after"] == record["isolated_source_configuration_before"]
            for source_id in list(nas.sessions):
                nas._close_session(source_id)
            app.state.llm_shutdown()
    record["elapsed_seconds"] = time.monotonic() - started
    record["ended_at"] = datetime.now(timezone.utc).isoformat()
    record["owner_source_unchanged"] = original_hash == digest(original_path)
    record["code_unchanged"] = all(digest(ROOT / name) == value for name, value in snapshot.items())
    body = json.dumps(record, ensure_ascii=False, indent=2)
    if (source.get("api_key") and source["api_key"] in body) or '"reasoning_content"' in body:
        raise RuntimeError("evidence_export_guard")
    (output / "run.json").write_text(body, encoding="utf-8")
    print(json.dumps({"status": record.get("task", {}).get("status"), "outcome": record.get("task", {}).get("outcome", {}).get("status"),
                      "reference_target": record.get("reference", {}).get("result", {}).get("target_status"),
                      "error": record.get("error_code") or record.get("task", {}).get("error"), "checks": record.get("mechanical_checks"),
                      "model_calls": len(record["model_calls"]), "elapsed_seconds": record["elapsed_seconds"]}), flush=True)


if __name__ == "__main__":
    main()
