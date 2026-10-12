"""Live R03 development trial: kill an actual consumer after business ACK.

The parent owns DAV and independently collects state. The child has no access to
its audit. This remains development evidence, not a formal suite envelope.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from planner_policy import SOURCE_ID, assert_mapping, map_fixture, read_planner  # noqa: E402


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_recovery_fixture(sources, home, policy):
    mappings = json.loads((home / "planner-policy-mapping.json").read_text(encoding="utf-8"))
    records = sources.read()
    model = policy["model_id"]
    if (len(mappings) != 1 or len(records) != 1
            or mappings[0].get("fixture_model_id") != model
            or mappings[0].get("planner_policy_sha256") != policy["policy_sha256"]
            or mappings[0].get("after") != policy["generation"]
            or mappings[0].get("fixture_source_id") not in records):
        raise RuntimeError("planner_recovery_fixture_identity_changed")
    fixture = records[mappings[0]["fixture_source_id"]]
    if fixture["models"] != [model]:
        raise RuntimeError("planner_recovery_fixture_identity_changed")
    settings = fixture.get("model_settings")
    if not isinstance(settings, dict) or set(settings) != set(fixture["models"]):
        raise RuntimeError("planner_recovery_fixture_structure_invalid")
    row = settings[model]
    if (not isinstance(row, dict) or not isinstance(row.get("overrides"), dict)
            or not isinstance(row.get("evidence"), dict)):
        raise RuntimeError("planner_recovery_fixture_structure_invalid")
    revisions = [fixture.get("source_revision"), fixture.get("destination_revision"),
                 row.get("rule_revision"), row.get("destination_revision")]
    if any(not isinstance(value, str) or len(value) != 32
           or any(char not in "0123456789abcdef" for char in value) for value in revisions):
        raise RuntimeError("planner_recovery_fixture_structure_invalid")
    selected = "local/" + sources.model_service_id(fixture, model)
    assert_mapping(sources, selected, policy)
    return fixture, selected


def child(config_path):
    from fastapi import FastAPI

    from rynmesh.agent_runtime.bridge import build_agent_integrations
    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.store import AgentError, AgentStore
    from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
    from rynmesh.llm_package.routes import install_llm_routes
    from rynmesh.llm_package.sources import SourceStore
    from rynmesh.nas import NasPlugin
    from rynmesh.services import peer_box
    from rynmesh.store import RynmeshStore

    config = json.loads(config_path.read_text(encoding="utf-8"))
    home, owner = Path(config["home"]), Path(config["owner_home"])
    write(home / ("worker-" + config["phase"] + ".json"), {"worker_pid": os.getpid()})
    owner_path = owner / "llm/sources.enc"
    if digest(owner_path) != config["owner_source_sha256"]:
        raise RuntimeError("owner_source_changed_before_dispatch")
    source, adapter, planner_policy = read_planner(owner, config["source_id"])
    if planner_policy != config["planner_policy"]:
        raise RuntimeError("planner_owner_policy_changed")
    model_name = planner_policy["model_id"]
    os.environ.update(RYNMESH_HOME=str(home), RYNMESH_REGISTRY_URL="", RYNMESH_AUTO_REGISTER="0",
                      RYNMESH_LAN_DISCOVERY="0", RYNMESH_DEFAULT_DISCOVERY="0")
    setup = ExitStack()
    world = None
    if config.get("release_activation"):
        from release_resume import reopen_documents
        world = reopen_documents(config, setup)
        store, app, selected = world.execution_store, world.execution_app, world.selected
        key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
        nas = app.state.nas_plugin = NasPlugin(home)
        if not nas.state.get("sources"):
            raise RuntimeError("release_resume_connector_missing")
        services = world.owner_app.state.llm_agent_services
        sources = world.sources
        provider_selected = world.provider_selected
    else:
        store = RynmeshStore(home=home, network_dir=home / "network")
        if config["phase"] == "recover":
            # Reject damaged historical generation before route setup can migrate
            # an isolated source record. Recovery only verifies; it never remaps.
            checked_recovery_fixture(SourceStore(home, store.private_key_bytes, migrate=False), home, planner_policy)
        key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
        app = FastAPI()
        install_llm_routes(app, store=store, home=home, messaging_key=key,
                           resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
        nas = app.state.nas_plugin = NasPlugin(home)
        nas.enable(True)
        services = app.state.llm_agent_services
        sources = services["source_store"]
        if config["phase"] == "crash" and sources.read():
            raise RuntimeError("planner_fresh_recovery_fixture_required")
        if not sources.read():
            if config["phase"] != "crash":
                raise RuntimeError("planner_recovery_fixture_missing")
            fixture = sources.prepare({"name": "Isolated recovery inference", "kind": "api", "provider": source["provider"],
                "base_url": source["base_url"], "api_key": "in-memory-adapter-placeholder", "models": [model_name],
                "context_window": source["context_window"],
                "request_defaults": source.get("request_defaults", {})})
            fixture.update(status="ready", tool_support={model_name: "supported"})
            mapping = map_fixture(fixture, model_name, planner_policy)
            sources.save(fixture)
            write(home / "planner-policy-mapping.json", [mapping])
            nas.configure({"name": "docs-alpha", "system": "generic", "protocol": "webdav", "url": config["dav_url"],
                           "username": "fixture", "password": "fixture", "allow_ai": True, "writable": True})
        fixture, selected = checked_recovery_fixture(sources, home, planner_policy)
        provider_selected = selected
    original_provider = services["provider_for"]

    def provider_for(service_id):
        manager = original_provider(service_id)
        if service_id == provider_selected.removeprefix("local/"):
            if digest(owner_path) != config["owner_source_sha256"]:
                raise RuntimeError("owner_source_changed_before_dispatch")
            assert_mapping(sources, provider_selected, planner_policy)
            if manager is None or not isinstance(manager.adapter, ModelDispatchAdapter):
                raise RuntimeError("acceptance_model_dispatch_gate_missing")
            manager.adapter.inner = adapter
        return manager

    services["provider_for"] = provider_for
    bridge, tools, catalog = build_agent_integrations(app, store, home)
    complete = bridge.complete
    trace = home / ("model-" + config["phase"] + ".jsonl")

    def append(value):
        body = json.dumps(value, ensure_ascii=False)
        if source.get("api_key") and source["api_key"] in body:
            raise RuntimeError("evidence_export_guard")
        with trace.open("a", encoding="utf-8") as stream:
            stream.write(body + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def traced(model_id, messages, specs, cancelled, on_event=None, **kwargs):
        call_id = time.time_ns()
        public = [{k: v for k, v in m.items() if k != "reasoning_content"} for m in copy.deepcopy(messages)]
        append({"type": "model_request", "call_id": call_id, "messages": public, "tools": specs})
        began = time.monotonic()
        try:
            result = complete(model_id, messages, specs, cancelled, on_event, **kwargs)
            append({"type": "model_response", "call_id": call_id, "elapsed_seconds": time.monotonic() - began,
                    "message": {k: v for k, v in result["message"].items() if k != "reasoning_content"},
                    "usage": result.get("usage"), "finish_reason": result.get("finish_reason", "unknown")})
            return result
        except AgentError as exc:
            append({"type": "model_error", "call_id": call_id, "code": str(exc) if str(exc).startswith("agent_") and len(str(exc)) < 100 else "agent_error"})
            raise

    bridge.complete = traced
    runtime = AgentRuntime(AgentStore(home / "agent", key), bridge, tools, catalog, consumer_node_id=store.peer_id,
        **({"owner_id": "owner-test"} if world is not None else {}))
    app.state.agent_runtime = runtime
    tasks = runtime.store.read()["tasks"]
    if config["phase"] == "crash":
        if tasks:
            raise RuntimeError("fresh_consumer_required")
        runtime.update_settings({"enabled": True, "model_id": selected, "max_steps": 24, "max_seconds": 600,
                                 "max_context_chars": 100000})
        connection = next(iter(nas.state["sources"]))
        task = runtime.create({"message": config["prompt"], "scope": {"resources": [{"connection_id": connection["id"],
            "root_path": "reports/acceptance", "recursive": True, "actions": ["list", "read_ai", "create_directory", "create_file"]}],
            "allow_model_data": True}, "expected_destination_revision": catalog.resolve(selected)["destination_revision"]})
    else:
        if len(tasks) != 1:
            raise RuntimeError("exact_recovery_task_required")
        task = runtime.get(next(iter(tasks)))
        if config.get("cohort", "ryn") == "ryn":
            runtime.reconcile()
    collector = None
    if world is not None:
        from release_resume import install_documents, native_objects
        collector = install_documents(config, world, runtime, task["id"], nas,
                                      native=native_objects(config, runtime, task["id"]))
    if config["phase"] == "crash" and config.get("cohort", "ryn") == "ryn":
        execute = tools.execute

        def crash_boundary(name, arguments, **kwargs):
            result = execute(name, arguments, **kwargs)
            if name == "ryn_create_text_file":
                write(home / "crash-ready.json", {"operation_id": kwargs["operation_id"], "task_id": task["id"]})
                threading.Event().wait(60)
                raise RuntimeError("crash_controller_missing")
            return result

        tools.execute = crash_boundary
    try:
        if config.get("cohort", "ryn") == "reference":
            from release_resume import native_recovery
            evidence = {}
            native_recovery(config, runtime, task, bridge, tools, catalog, evidence,
                lambda a: a["tool"] == "ryn_create_text_file" and a["arguments"].get("name") == "recovery.md"
                          and a["arguments"].get("content") == "RECOVERY_ONCE_731", collector)
            write(Path(config["output"]) / ("native-" + config["phase"] + ".json"), evidence)
            if config["phase"] == "crash":
                saved = runtime.store.read()["tasks"][task["id"]]["reference_session"]
                write(home / "crash-ready.json", {"operation_id": saved["interrupted_operation_id"], "task_id": task["id"]})
                threading.Event().wait(60)
                raise RuntimeError("crash_controller_missing")
            return
        for _ in range(160):
            task = runtime.get(task["id"])
            recovering = task.get("reconciliation", {}).get("state") in {"pending", "running"}
            if not recovering and task["status"] in {"completed", "failed", "paused", "cancelled", "interrupted", "waiting_input"}:
                break
            if task["status"] == "waiting_approval":
                approval = task["pending_approval"]
                permitted = approval["tool"] == "ryn_create_text_file" and approval["arguments"].get("name") == "recovery.md" and approval["arguments"].get("content") == "RECOVERY_ONCE_731"
                append({"type": "approval", "approved": permitted, "approval": approval})
                runtime.action(task["id"], {"action": "approve" if permitted else "deny", "approval_id": approval["id"],
                                          "expected_control_revision": task["control_revision"]})
            else:
                runtime.run_once()
    finally:
        write(home / "consumer-result.json", runtime.get(task["id"]))
        if collector is not None and config["phase"] == "recover":
            write(Path(config["output"]) / "release-collection.json", collector.finalize())
        for connection_id in list(nas.sessions):
            nas._close_session(connection_id)
        app.state.llm_shutdown()
        setup.close()


def run(args):
    from consumer_process_proof import OwnedConsumerProcess

    from rynmesh.agent_runtime.store import AgentStore
    from rynmesh.services import peer_box

    if args.source_id != SOURCE_ID:
        raise ValueError("configured_glm_source_required")
    owner = Path(os.environ.get("RYNMESH_HOME", str(Path.home() / ".rynmesh")))
    owner_hash = digest(owner / "llm/sources.enc")
    _, _, planner_policy = read_planner(owner, args.source_id)
    case = next(c for c in json.loads((Path(__file__).parent.parent / "cases-v1.json").read_text(encoding="utf-8"))["cases"] if c["id"] == "R03")
    output = Path(__file__).parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-R03-ryn")
    output.mkdir()
    home = Path(tempfile.mkdtemp(prefix="ryn-recovery-live-"))
    davroot = home / "fixture-documents"
    (davroot / "reports/acceptance").mkdir(parents=True)
    (davroot / ".ryn-acceptance-isolated").touch()
    module_spec = importlib.util.spec_from_file_location("acceptance_dav", ROOT / "tests/fixtures/agent_acceptance/webdav_fixture.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    paths = sorted((ROOT / "rynmesh/agent_runtime").glob("*.py")) + sorted((ROOT / "rynmesh/llm_package").glob("*.py"))
    paths += [Path(__file__), *[Path(__file__).with_name(name) for name in
        ("planner_policy.py", "release_resume.py", "release_collection.py", "release_world.py", "query_consumer.py")]]
    snapshot = {str(p.relative_to(ROOT)).replace("\\", "/"): digest(p) for p in paths}
    record = {"kind": "real-recovery-development-trial", "formal_case_score": False, "case_id": "R03", "cohort": args.cohort, "prompt": case["prompt"], "code_snapshot": snapshot,
              "planner_policy": planner_policy, "planner_policy_mapping": []}
    print(json.dumps({"output": str(output)}), flush=True)
    began = time.monotonic()
    with ExitStack() as role_setup, module.FixtureDAV(davroot) as dav:
        config_path = home / "consumer-config.json"
        config = {"home": str(home), "owner_home": str(owner), "source_id": args.source_id,
                  "owner_source_sha256": owner_hash, "planner_policy": planner_policy,
                  "dav_url": dav.url, "prompt": case["prompt"], "phase": "crash",
                  "output": str(output.resolve()), "case_id": "R03", "cohort": args.cohort,
                  "node": str(args.node) if args.node else None, "upstream": str(args.upstream) if args.upstream else None,
                  "trial": getattr(args, "trial", 1),
                  "release_activation": str(args.release_activation.resolve()) if getattr(args, "release_activation", None) else None}
        if config["release_activation"]:
            from release_resume import prepare_documents
            prepare_documents(role_setup, config, home, "R03", planner_policy, dav.url)
            home = Path(config["home"])
        write(config_path, config)
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child", str(config_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        owned = OwnedConsumerProcess(process)
        try:
            deadline = time.monotonic() + 600
            while not (home / "crash-ready.json").exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            record["barrier_reached"] = (home / "crash-ready.json").exists()
            record["first_process_exit_before_kill"] = process.poll()
            if record["barrier_reached"]:
                key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
                record["before_kill"] = next(iter(AgentStore(home / "agent", key).read()["tasks"].values()))
                record["fault"] = json.loads((home / "crash-ready.json").read_text(encoding="utf-8"))
                owned.bind_worker(json.loads((home / "worker-crash.json").read_text(encoding="utf-8"))["worker_pid"])
                record["terminated_consumer"] = owned.terminate()
                if not record["terminated_consumer"]["confirmed_terminal"]:
                    raise RuntimeError("consumer_actual_worker_not_terminal")
                record["killed_process_exit"] = process.returncode
                config["phase"] = "recover"
                write(config_path, config)
                resumed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", str(config_path)],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=650)
                record["restarted_process_exit"] = resumed.returncode
        except subprocess.TimeoutExpired:
            record["controller_error"] = "consumer_timeout"
        finally:
            owned.terminate()
            owned.close()
            record["http_audit"] = copy.deepcopy(dav.audit)
            if (output / "release-collection.json").exists():
                record["release_collection"] = json.loads((output / "release-collection.json").read_text(encoding="utf-8"))
                from release_collection import registry
                record.update(registry.read(record["release_collection"]["registration"]["path"])["binding"])
            for native_phase in ("crash", "recover"):
                native_path = output / ("native-" + native_phase + ".json")
                if native_path.exists():
                    record.setdefault("native_phases", {})[native_phase] = json.loads(native_path.read_text(encoding="utf-8"))
            if (home / "planner-policy-mapping.json").exists():
                record["planner_policy_mapping"] = json.loads((home / "planner-policy-mapping.json").read_text(encoding="utf-8"))
            if (home / "consumer-result.json").exists():
                record["task"] = json.loads((home / "consumer-result.json").read_text(encoding="utf-8"))
            record["consumer_errors"] = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(home.glob("consumer-error-*.json"))]
            record["model_trace"] = [json.loads(line) for phase in ("crash", "recover")
                                     if (home / ("model-" + phase + ".jsonl")).exists()
                                     for line in (home / ("model-" + phase + ".jsonl")).read_text(encoding="utf-8").splitlines()]
            artifacts = [p for p in (davroot / "reports/acceptance").rglob("*") if p.is_file()]
            record["artifacts"] = [{"path": str(p.relative_to(davroot)).replace("\\", "/"), "sha256": digest(p), "content": p.read_text(encoding="utf-8")} for p in artifacts]
    task = record.get("task", {})
    record["checks"] = {"barrier_reached": record["barrier_reached"], "one_put": sum(e["method"] == "PUT" for e in record["http_audit"]) == 1,
                        "one_exact_artifact": len(record["artifacts"]) == 1 and record["artifacts"][0]["path"] == "reports/acceptance/recovery.md" and record["artifacts"][0]["content"] == "RECOVERY_ONCE_731",
                        "reconciled_original_operation": task.get("reconciliation", {}).get("state") == "executed",
                        "completed_verified": task.get("status") == "completed" and task.get("outcome", {}).get("status") == "verified"}
    record["owner_source_unchanged"] = digest(owner / "llm/sources.enc") == owner_hash
    record["code_unchanged"] = all(digest(ROOT / name) == value for name, value in snapshot.items())
    record["elapsed_seconds"] = time.monotonic() - began
    write(output / "run.json", record)
    print(json.dumps({"checks": record["checks"], "status": task.get("status"), "outcome": task.get("outcome", {}).get("status"), "elapsed_seconds": record["elapsed_seconds"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--source-id")
    parser.add_argument("--cohort", choices=("ryn", "reference"), default="ryn")
    parser.add_argument("--node", type=Path)
    parser.add_argument("--upstream", type=Path)
    from release_collection import add_arguments
    add_arguments(parser)
    args = parser.parse_args()
    if not args.child and args.cohort == "reference" and (not args.node or not args.upstream):
        parser.error("reference requires --node and --upstream")
    if args.child:
        try:
            child(args.child)
        except Exception as exc:
            config = json.loads(args.child.read_text(encoding="utf-8"))
            error = {"error_type": type(exc).__name__}
            if isinstance(exc, RuntimeError) and str(exc).startswith(("planner_", "owner_source_")) and len(str(exc)) < 100:
                error["error_code"] = str(exc)
            write(Path(config["home"]) / ("consumer-error-" + config["phase"] + ".json"), error)
            raise SystemExit(1) from None
    elif args.live and args.source_id:
        run(args)
    else:
        parser.error("requires --live and --source-id")
