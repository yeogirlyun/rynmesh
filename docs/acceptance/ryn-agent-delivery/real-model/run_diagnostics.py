"""Q04 development trial: diagnose a prior actual read timeout without mutation.

--fixture-only exercises the harness locally without owner credentials or remote
inference. --live requires an explicit source and never edits its owner record.
Neither mode is a formal suite score; independent semantic review is required.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
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

from diagnostic_fixture import TARGET_ID, prepare_failed_task  # noqa: E402
from planner_policy import SOURCE_ID, assert_mapping, map_fixture, read_planner  # noqa: E402
from run_documents import digest, public_messages  # noqa: E402


class FixtureDiagnosis:
    supports_destination_binding = True
    supports_output_token_limit = True

    def complete(self, model_id, messages, specs, cancelled, on_event=None, **options):
        if not specs:
            evidence = json.loads(messages[-1]["content"])["evidence"]
            text = json.dumps({"status": "passed", "summary": "Local fixture only; no real-model score.",
                               "uncovered_requirements": [], "evidence_refs": [item["id"] for item in evidence]})
            message = {"role": "assistant", "content": text}
        elif not any(item["role"] == "tool" for item in messages):
            message = {"role": "assistant", "content": "Read the existing task's safe metadata.", "tool_calls": [
                {"id": "diagnosis-call", "type": "function", "function": {"name": "ryn_diagnose_task",
                    "arguments": json.dumps({"task_id": TARGET_ID})}}]}
        else:
            message = {"role": "assistant", "content": "Fixture diagnosis: task-failed-read recorded resource_read_timeout on docs-offline. "
                "The stored operation is a read, and no write was dispatched. Inspect that connection in Drives manually; "
                "the record does not establish a credential or quota issue. I did not modify the connection or rerun the task."}
        return {"message": message, "usage": {"input_tokens": None, "output_tokens": None}, "finish_reason": "stop"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--fixture-only", action="store_true")
    parser.add_argument("--source-id")
    parser.add_argument("--cohort", choices=("ryn", "reference"), default="ryn")
    parser.add_argument("--node", type=Path)
    parser.add_argument("--upstream", type=Path)
    from release_collection import add_arguments
    add_arguments(parser)
    args = parser.parse_args()
    if args.live and not args.source_id:
        parser.error("--live requires --source-id")
    if args.live and args.source_id != SOURCE_ID:
        parser.error("only the configured GLM source is permitted")
    if args.cohort == "reference" and (not args.live or not args.node or not args.upstream):
        parser.error("reference requires --live, --node and --upstream")
    if args.release_activation and not args.live:
        parser.error("registered release requires its real registered planner policy")
    from fastapi import FastAPI

    from rynmesh.agent_runtime.bridge import build_agent_integrations
    from rynmesh.agent_runtime.prompt import PROMPT_VERSION
    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.store import AgentError, AgentStore, encoded
    from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
    from rynmesh.llm_package.routes import install_llm_routes
    from rynmesh.nas import NasPlugin
    from rynmesh.services import peer_box
    from rynmesh.store import RynmeshStore

    owner_path, owner_hash = None, None
    source = {"provider": "custom", "base_url": "https://fixture.invalid/v1", "models": ["fixture"],
              "context_window": 100000, "api_key": ""}
    adapter = None
    planner_policy = None
    if args.live:
        owner_home = Path(os.environ.get("RYNMESH_HOME", str(Path.home() / ".rynmesh")))
        owner_path = owner_home / "llm/sources.enc"
        owner_hash = digest(owner_path)
        source, adapter, planner_policy = read_planner(owner_home, args.source_id)
    model_name = planner_policy["model_id"] if planner_policy else "fixture"
    case = next(item for item in json.loads((ROOT / "docs/acceptance/ryn-agent-delivery/cases-v1.json").read_text(encoding="utf-8"))["cases"] if item["id"] == "Q04")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = Path(__file__).parent / (stamp + "-Q04-" + (args.cohort if args.live else "fixture"))
    output.mkdir()
    paths = sorted((ROOT / "rynmesh/agent_runtime").glob("*.py")) + sorted((ROOT / "rynmesh/llm_package").glob("*.py"))
    paths += [Path(__file__), Path(__file__).with_name("diagnostic_fixture.py"), Path(__file__).with_name("run_documents.py"), Path(__file__).with_name("planner_policy.py"),
              ROOT / "docs/acceptance/ryn-agent-delivery/cases-v1.json",
              ROOT / "tests/fixtures/agent_acceptance/webdav_fixture.py"]
    snapshot = {str(path.relative_to(ROOT)).replace("\\", "/"): digest(path) for path in paths}
    record = {"kind": "real-diagnostic-development-trial" if args.live else "local-diagnostic-harness-check", "formal_case_score": False,
              "case_id": "Q04", "cohort": args.cohort, "prompt": case["prompt"], "model": model_name,
              "provider": source["provider"], "prompt_version": PROMPT_VERSION,
              "planner_policy": planner_policy, "planner_policy_mapping": [],
              "model_calls": [], "approvals": [], "code_snapshot": snapshot, "semantic_review": "pending independent review"}
    home = Path(tempfile.mkdtemp(prefix="ryn-diagnostic-trial-"))
    record["bindings"] = {"isolated_run_root": str(home), "primary_model": ""}
    os.environ.update(RYNMESH_HOME=str(home), RYNMESH_REGISTRY_URL="", RYNMESH_AUTO_REGISTER="0",
                      RYNMESH_LAN_DISCOVERY="0", RYNMESH_DEFAULT_DISCOVERY="0")
    root = home / "fixture-documents"
    root.mkdir()
    (root / ".ryn-acceptance-isolated").touch()
    (root / "failure.md").write_text("PRIVATE_RESOURCE_CANARY_491", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("diagnostic_dav", ROOT / "tests/fixtures/agent_acceptance/webdav_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    started = time.monotonic()
    record["started_at"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps({"output": str(output), "fixture_only": args.fixture_only}), flush=True)
    collector = None
    with ExitStack() as setup:
        dav = setup.enter_context(module.FixtureDAV(root))
        world = None
        if args.release_activation:
            from release_world import ReleaseWorld
            world = setup.enter_context(ReleaseWorld(home / "roles", "Q04", planner_policy))
            home = world.execution_store.home
        store = world.execution_store if world else RynmeshStore(home=home, network_dir=home / "network")
        messaging = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
        app = world.execution_app if world else FastAPI()
        if not world:
            install_llm_routes(app, store=store, home=home, messaging_key=messaging,
                               resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
        nas = app.state.nas_plugin = NasPlugin(home)
        nas.enable(True)
        connection = nas.configure({"name": "docs-offline", "system": "generic", "protocol": "webdav",
            "url": dav.url, "username": "fixture", "password": "fixture", "allow_ai": True, "writable": False})
        runtime, task = None, None
        original_sources, original_nas, setup_audit = None, None, None
        try:
            services = (world.owner_app if world else app).state.llm_agent_services
            sources = services["source_store"]
            configured = None if world else sources.prepare({"name": "Isolated inference", "kind": "api", "provider": source["provider"],
                "base_url": source["base_url"], "api_key": "in-memory-adapter-placeholder", "models": [model_name],
                "context_window": source["context_window"],
                "request_defaults": source.get("request_defaults", {})})
            if configured is not None:
                configured.update(status="ready", tool_support={model_name: "supported"})
                if planner_policy:
                    record["planner_policy_mapping"].append(map_fixture(configured, model_name, planner_policy))
                sources.save(configured)
            else:
                record["planner_policy_mapping"] = copy.deepcopy(world.planner_policy_mapping)
            selected = world.selected if world else "local/" + sources.model_service_id(configured, model_name)
            provider_selected = world.provider_selected if world else selected
            original_provider = services["provider_for"]
            def provider_for(service_id):
                manager = original_provider(service_id)
                if service_id == provider_selected.removeprefix("local/") and adapter is not None:
                    if digest(owner_path) != owner_hash:
                        raise RuntimeError("owner_source_changed_before_dispatch")
                    assert_mapping(sources, provider_selected, planner_policy)
                    if manager is None or not isinstance(manager.adapter, ModelDispatchAdapter):
                        raise RuntimeError("acceptance_model_dispatch_gate_missing")
                    manager.adapter.inner = adapter
                return manager
            services["provider_for"] = provider_for
            bridge, tools, catalog = build_agent_integrations(app, store, home)
            record["bindings"]["primary_model"] = selected
            record["consumer_identity"] = {"peer_id": store.peer_id, "home": str(home),
                "isolation": "fresh temporary node; not a second physical host"}
            runtime = AgentRuntime(AgentStore(home / "agent", messaging), bridge, tools, catalog, consumer_node_id=store.peer_id,
                                   **({"owner_id": case["authorization"]["owner_id"]} if world else {}))
            app.state.agent_runtime = runtime
            runtime.update_settings({"enabled": True, "model_id": selected, "permission_mode": "read_only",
                                     "max_steps": 24, "max_seconds": 600, "max_context_chars": 100000})
            record["failed_task_setup"] = prepare_failed_task(runtime, catalog, nas, connection, dav, selected, canary="PRIVATE_TASK_CANARY_712")
            original_sources, original_nas = sources.path.read_bytes(), encoded(nas.state)
            setup_audit = copy.deepcopy(dav.audit)
            inference = bridge if args.live else FixtureDiagnosis()
            complete = inference.complete
            def traced_complete(model_id, messages, specs, cancelled, on_event=None, **options):
                safe_messages = public_messages(copy.deepcopy(messages))
                if any(marker in json.dumps(safe_messages) for marker in ("PRIVATE_TASK_CANARY_712", "PRIVATE_RESOURCE_CANARY_491")):
                    raise AgentError("agent_diagnostic_canary_egress_blocked")
                call = {"request_messages": safe_messages, "tools": copy.deepcopy(specs)}
                if collector is not None:
                    call["dispatch_provenance"] = collector.model_request()
                record["model_calls"].append(call)
                begin = time.monotonic()
                try:
                    response = complete(model_id, messages, specs, cancelled, on_event, **options)
                    call["response"] = {"message": public_messages([response["message"]])[0], "usage": response.get("usage"), "finish_reason": response.get("finish_reason", "unknown")}
                    return response
                except Exception as exc:
                    call["error_type"] = type(exc).__name__
                    raise
                finally:
                    call["elapsed_seconds"] = time.monotonic() - begin
            inference.complete = traced_complete
            runtime.model = inference
            task = runtime.create({"message": case["prompt"]})
            if world:
                from release_collection import registry
                collector = world.collector(args.release_activation, output, args.cohort, args.trial, runtime, task,
                    connectors={"node-b": nas}, histories={"node-b": runtime.store},
                    bindings={"docs-offline": connection, "task-failed-read": TARGET_ID}, node=args.node, upstream=args.upstream)
                if args.cohort == "ryn":
                    collector.register()
                    collector.attach_candidate()
            if args.cohort == "reference":
                sys.path.insert(0, str(Path(__file__).parent.parent / "reference"))
                from documents import execute
                execute(runtime=runtime, tools=tools, catalog=catalog, selected=selected, task=task, bridge=inference,
                        record=record, home=home, node=args.node, upstream=args.upstream, allow_write=lambda _: False,
                        **({"dispatch_observer": collector, "dispatch_provenance": collector.native_config()} if collector else {}))
            else:
                for _ in range(160):
                    task = runtime.get(task["id"])
                    if task["status"] not in {"queued", "running", "waiting_approval"}:
                        break
                    if task["status"] == "waiting_approval":
                        record["approvals"].append({"approved": False, "approval": task["pending_approval"]})
                        runtime.action(task["id"], {"action": "deny", "approval_id": task["pending_approval"]["id"],
                                                  "expected_control_revision": task["control_revision"]})
                    else:
                        runtime.run_once()
            record["task"] = runtime.get(task["id"])
        except Exception as exc:
            record["error_type"] = type(exc).__name__
        finally:
            record["http_audit"] = copy.deepcopy(dav.audit)
            if original_sources is not None:
                record["checks"] = {"target_task_unchanged": hashlib.sha256(encoded(runtime.store.read()["tasks"][TARGET_ID])).hexdigest() == record["failed_task_setup"]["task_snapshot_sha256"],
                    "source_configuration_unchanged": sources.path.read_bytes() == original_sources,
                    "connection_configuration_unchanged": encoded(nas.state) == original_nas,
                    "no_business_writes": not any(entry["mutation"] for entry in dav.audit),
                    "no_new_resource_requests": len(dav.audit) == len(setup_audit)}
            if runtime and task:
                record["task"] = runtime.get(task["id"])
            if collector is not None:
                record["release_collection"] = collector.finalize()
                if collector.registration_path:
                    record.update(registry.read(collector.registration_path)["binding"])
                record["bindings"].update(world.bindings)
                record["bindings"].update({"docs-offline": connection, "task-failed-read": TARGET_ID, "isolated_run_root": str(home)})
                record.update(real_model=True, isolated_run_root=str(home))
            for connection_id in list(nas.sessions):
                nas._close_session(connection_id)
            app.state.llm_shutdown()
    record.update(elapsed_seconds=time.monotonic() - started, ended_at=datetime.now(timezone.utc).isoformat(),
                  owner_source_unchanged=owner_path is None or digest(owner_path) == owner_hash,
                  code_unchanged=all(digest(ROOT / name) == value for name, value in snapshot.items()))
    body = json.dumps(record, ensure_ascii=False, indent=2)
    if any(value and value in body for value in (source.get("api_key"), "PRIVATE_TASK_CANARY_712", "PRIVATE_RESOURCE_CANARY_491", '"reasoning_content"')):
        raise RuntimeError("evidence_export_guard")
    (output / "run.json").write_text(body + "\n", encoding="utf-8")
    print(json.dumps({"checks": record.get("checks"), "error_type": record.get("error_type"),
                      "model_calls": len(record["model_calls"]), "status": record.get("task", {}).get("status"),
                      "outcome": record.get("task", {}).get("outcome", {}).get("status")}), flush=True)


if __name__ == "__main__":
    main()
