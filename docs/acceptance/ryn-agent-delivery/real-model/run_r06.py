"""R06 development controller: real compaction, safe process restart, owner 4+8.

No production changes or synthetic live compaction. Missing barriers are evidence,
never manufactured. The parent owns DAV and the actual child-process handles.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
from consumer_process_proof import OwnedConsumerProcess  # noqa: E402
from document_cases import FIXTURES, sha  # noqa: E402
from planner_policy import SOURCE_ID, assert_mapping, map_fixture, read_planner  # noqa: E402
from run_r05 import CASE_PATH, DISPATCH, now, trace_complete  # noqa: E402
from run_recovery import checked_recovery_fixture  # noqa: E402

CONTINUE = "按最新要求继续。"
TARGET = "reports/acceptance/resumed.md"


def write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_case():
    suite = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    if suite.get("partition") != "development":
        raise ValueError("development_partition_required")
    return copy.deepcopy(next(c for c in suite["cases"] if c["id"] == "R06"))


def request_version(task):
    return {"expected_control_revision": task["control_revision"], "client_request_id": str(uuid.uuid4())}


def scope_selection(connection_id):
    # Search is a scoped read operation, not a standalone grant action.
    return {"resources": [{"connection_id": connection_id, "root_path": grant["resource_prefix"],
        "root_kind": "directory", "recursive": True,
        "actions": ["read_ai" if a == "read" else a for a in grant["actions"] if a != "search"]}
        for grant in load_case()["authorization"]["resource_grants"]], "allow_model_data": True}


def checkpoint_state(runtime, task_id):
    row = runtime.store.read()["tasks"][task_id]
    from rynmesh.agent_runtime.runtime import digest
    return {"task_id": task_id, "status": row["status"], "control_revision": row["control_revision"],
        "goal": copy.deepcopy(row["goal"]), "budget": copy.deepcopy(row["budget"]),
        "checkpoint_sha256": digest(row.get("context_checkpoint")),
        "messages_sha256": digest(row["messages"]), "scope_sha256": digest(row["scope"]),
        "safe": not any(row.get(k) for k in ("in_flight", "queue", "pending_control", "pending_approval", "approved")),
        "compactions": [copy.deepcopy(e) for e in row["events"] if e["type"] == "context_compacted"]}


class CorrectionBarrier:
    """Observe a completed production transaction, then use the owner API once."""
    def __init__(self, runtime, task_id, correction):
        self.runtime, self.task_id, self.correction = runtime, task_id, correction
        self.observation = None
        self.original = runtime._compact
        runtime._compact = self.compact

    def compact(self, selected, cancelled, started):
        before = len(checkpoint_state(self.runtime, self.task_id)["compactions"])
        result = self.original(selected, cancelled, started)
        after = checkpoint_state(self.runtime, self.task_id)
        if result and self.observation is None and len(after["compactions"]) > before:
            if before != 0:
                raise RuntimeError("first_compaction_barrier_missed")
            current = self.runtime.get(self.task_id)
            body = {"message": self.correction, "intent": "steer", **request_version(current),
                    "expected_goal_revision": current["goal"]["revision"]}
            updated = self.runtime.add_message(self.task_id, body)
            self.observation = {"at": now(), "compaction": after["compactions"][-1],
                                "owner_request": body, "after_correction": updated}
        return result

    def safe(self):
        if self.observation is None:
            return None
        state = checkpoint_state(self.runtime, self.task_id)
        if not state["safe"] or state["status"] != "queued":
            raise RuntimeError("post_correction_checkpoint_not_safe")
        return {"correction": self.observation, "checkpoint": state, "worker_pid": os.getpid()}


def owner_continue(runtime, task_id):
    """No dispatch while stopped, including after the owner increases the limit."""
    stopped = runtime.get(task_id)
    if stopped["status"] != "failed" or stopped.get("error") != "agent_model_call_budget_exhausted":
        raise RuntimeError("persisted_lifetime_model_budget_stop_required")
    if not checkpoint_state(runtime, task_id)["safe"]:
        raise RuntimeError("budget_stop_not_safe")
    seq = stopped["events"][-1]["seq"]
    before = stopped["budget"]["lifetime"]["charged_model_calls"]
    for _ in range(2):
        runtime.run_once()
    current = runtime.get(task_id)
    old = current["budget"]["limits"]["max_total_model_calls"]
    if old != 4 or before != 4:
        raise RuntimeError("initial_lifetime_budget_changed")
    body = {"limits": {"max_total_model_calls": old + 8}, **request_version(current)}
    increased = runtime.update_budget(task_id, body)
    for _ in range(2):
        runtime.run_once()
    waiting = runtime.get(task_id)
    dispatches = [e for e in waiting["events"] if e["seq"] > seq and e["type"] in DISPATCH]
    if (dispatches or waiting["status"] != "failed" or waiting["error"] != stopped["error"]
            or waiting["budget"]["lifetime"]["charged_model_calls"] != before):
        raise RuntimeError("dispatch_before_owner_continue")
    message = {"message": CONTINUE, **request_version(waiting)}
    resumed = runtime.add_message(task_id, message)
    return {"stopped": stopped, "budget_request": body, "after_budget_update": increased,
        "before_explicit_continue": waiting, "calls_after_budget_before_owner_continue": len(dispatches),
        "continue_request": message, "after_explicit_continue": resumed,
        "protocol": "budget update then explicit owner message; failed is not a resumable action state"}


def drive(runtime, task_id, *, barrier=None, approve=None, max_ticks=240):
    evidence = {}
    for _ in range(max_ticks):
        current = runtime.get(task_id)
        if barrier and barrier.observation is not None:
            evidence["safe_checkpoint"] = barrier.safe()
            return evidence
        if current["status"] == "failed" and current.get("error") == "agent_model_call_budget_exhausted":
            if barrier or "owner_continue" in evidence:
                break
            evidence["owner_continue"] = owner_continue(runtime, task_id)
        elif current["status"] == "waiting_approval":
            approval = current["pending_approval"]
            allowed = bool(approve and approve(approval))
            evidence.setdefault("approvals", []).append({"approval": approval, "approved": allowed})
            runtime.action(task_id, {"action": "approve" if allowed else "deny", "approval_id": approval["id"],
                                    **request_version(current)})
        elif current["status"] in {"completed", "failed", "cancelled", "paused", "interrupted", "waiting_input"}:
            break
        else:
            runtime.run_once()
    if barrier:
        evidence["unmet_trigger"] = "first_compaction_committed_then_safe_correction_checkpoint"
    elif "owner_continue" not in evidence:
        evidence["unmet_trigger"] = "persisted_lifetime_model_budget_stop"
    evidence["task"] = runtime.get(task_id)
    return evidence


def child(config_path):
    from fastapi import FastAPI

    from rynmesh.agent_runtime.bridge import build_agent_integrations
    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.store import AgentStore
    from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
    from rynmesh.llm_package.routes import install_llm_routes
    from rynmesh.llm_package.sources import SourceStore
    from rynmesh.nas import NasPlugin
    from rynmesh.services import peer_box
    from rynmesh.store import RynmeshStore

    config = json.loads(config_path.read_text(encoding="utf-8"))
    home, owner, phase = Path(config["home"]), Path(config["owner_home"]), config["phase"]
    write(home / f"worker-{phase}.json", {"worker_pid": os.getpid(), "home": str(home)})
    owner_path = owner / "llm/sources.enc"
    if sha(owner_path) != config["owner_source_sha256"]:
        raise RuntimeError("owner_source_changed")
    source, adapter, policy = read_planner(owner, config["source_id"])
    if policy != config["planner_policy"]:
        raise RuntimeError("owner_policy_changed")
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
        if phase == "after":
            checked_recovery_fixture(SourceStore(home, store.private_key_bytes, migrate=False), home, policy)
        key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
        app = FastAPI()
        install_llm_routes(app, store=store, home=home, messaging_key=key,
                           resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
        nas = app.state.nas_plugin = NasPlugin(home)
        nas.enable(True)
        services = app.state.llm_agent_services
        sources = services["source_store"]
        if phase == "before":
            if sources.read():
                raise RuntimeError("fresh_fixture_required")
            model = policy["model_id"]
            fixture = sources.prepare({"name": "Isolated R06 inference", "kind": "api", "provider": source["provider"],
                "base_url": source["base_url"], "api_key": "in-memory-adapter-placeholder", "models": [model],
                "context_window": source["context_window"], "request_defaults": source.get("request_defaults", {})})
            fixture.update(status="ready", tool_support={model: "supported"})
            mapping = map_fixture(fixture, model, policy)
            sources.save(fixture)
            write(home / "planner-policy-mapping.json", [mapping])
            nas.configure({"name": "docs-alpha", "system": "generic", "protocol": "webdav", "url": config["dav_url"],
                           "username": "fixture", "password": "fixture", "allow_ai": True, "writable": True})
        _, selected = checked_recovery_fixture(sources, home, policy)
        provider_selected = selected
    original_provider = services["provider_for"]
    def provider_for(service_id):
        if sha(owner_path) != config["owner_source_sha256"]:
            raise RuntimeError("owner_source_changed_before_dispatch")
        assert_mapping(sources, provider_selected, policy)
        manager = original_provider(service_id)
        if service_id == provider_selected.removeprefix("local/"):
            if manager is None or not isinstance(manager.adapter, ModelDispatchAdapter):
                raise RuntimeError("production_dispatch_gate_missing")
            manager.adapter.inner = adapter
        return manager
    services["provider_for"] = provider_for
    bridge, tools, catalog = build_agent_integrations(app, store, home)
    calls = []
    complete = trace_complete(bridge.complete, calls)
    def traced(*args, **kwargs):
        try:
            return complete(*args, **kwargs)
        finally:
            if source.get("api_key") and source["api_key"] in json.dumps(calls):
                raise RuntimeError("evidence_export_guard")
            write(home / f"model-{phase}.json", calls)
    bridge.complete = traced
    runtime = AgentRuntime(AgentStore(home / "agent", key), bridge, tools, catalog,
                           owner_id="owner-test", consumer_node_id=store.peer_id)
    app.state.agent_runtime = runtime
    case = load_case()
    if phase == "before":
        if runtime.store.read()["tasks"]:
            raise RuntimeError("fresh_consumer_required")
        runtime.update_settings({"enabled": True, "model_id": selected, "max_steps": 24, "max_seconds": 600,
                                 "max_context_chars": 12000, "permission_mode": "confirm_changes"})
        connection = next(iter(nas.state["sources"]))
        scope = scope_selection(connection["id"])
        current = runtime.create({"message": case["prompt"], "scope": scope,
            "expected_destination_revision": catalog.resolve(selected)["destination_revision"],
            "budget_limits": {"max_total_model_calls": 4}})
    else:
        tasks = runtime.store.read()["tasks"]
        if len(tasks) != 1:
            raise RuntimeError("one_persisted_task_required")
        current = runtime.get(next(iter(tasks)))
        before = checkpoint_state(runtime, current["id"])
        if config.get("cohort", "ryn") == "ryn":
            runtime.reconcile()
        after = checkpoint_state(runtime, current["id"])
        write(home / "restart-readback.json", {"before_reconcile": before, "after_reconcile": after,
            "identity": store.peer_id, "worker_pid": os.getpid(), "home": str(home)})
        if before != after or before != config["checkpoint"] or store.peer_id != config["identity"]:
            raise RuntimeError("checkpoint_or_identity_changed_on_restart")
    collector = None
    if world is not None:
        from release_resume import install_documents, native_objects
        collector = install_documents(config, world, runtime, current["id"], nas,
                                      native=native_objects(config, runtime, current["id"]))
    def permitted(approval):
        if approval["tool"] != "ryn_create_text_file":
            return False
        arguments = approval["arguments"]
        try:
            context = runtime._execution_context(runtime.store.read()["tasks"][current["id"]])
            _, _, path, _ = tools.resource_writes.target(approval["tool"], arguments, context)
            return path == TARGET and not arguments.get("overwrite", False)
        except Exception:
            return False
    barrier = CorrectionBarrier(runtime, current["id"], case["interventions"][0]["message"]) if phase == "before" and config.get("cohort", "ryn") == "ryn" else None
    try:
        if config.get("cohort", "ryn") == "reference":
            from release_resume import native_compact
            evidence = native_compact(config, runtime, current, bridge, tools, catalog, permitted, collector)
        else:
            evidence = drive(runtime, current["id"], barrier=barrier, approve=permitted)
        evidence.update(identity=store.peer_id, home=str(home), worker_pid=os.getpid())
        if "safe_checkpoint" in evidence:
            write(home / "safe-checkpoint.json", evidence)
            threading.Event().wait(600)
            raise RuntimeError("restart_controller_missing")
        if collector is not None:
            write(Path(config["output"]) / "release-collection.json", collector.finalize())
        write(home / f"result-{phase}.json", evidence)
    finally:
        for connection_id in list(nas.sessions):
            nas._close_session(connection_id)
        app.state.llm_shutdown()
        setup.close()


def wait_file(path, process, timeout):
    deadline = time.monotonic() + timeout
    while not path.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(.1)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def run(args):
    spec = importlib.util.spec_from_file_location("r06_dav", FIXTURES / "webdav_fixture.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    owner_path = args.owner_home / "llm/sources.enc"
    owner_hash = sha(owner_path)
    _, _, policy = read_planner(args.owner_home, args.source_id)
    args.output.mkdir(parents=True, exist_ok=False)
    home = Path(tempfile.mkdtemp(prefix="ryn-r06-"))
    root = home / "fixture-documents"
    input_path = root / "projects/orion/week-41/long-notes.txt"
    input_path.parent.mkdir(parents=True)
    (root / "reports/acceptance").mkdir(parents=True)
    (root / ".ryn-acceptance-isolated").touch()
    shutil.copyfile(FIXTURES / "documents/orion/long-notes.txt", input_path)
    paths = [Path(__file__), CASE_PATH, FIXTURES / "documents/orion/long-notes.txt", FIXTURES / "world.json",
        FIXTURES / "webdav_fixture.py", *[Path(__file__).with_name(n) for n in
        ("run_r05.py", "run_recovery.py", "planner_policy.py", "document_cases.py", "consumer_process_proof.py",
         "release_resume.py", "release_collection.py", "release_world.py", "query_consumer.py")],
        ROOT / "rynmesh/nas.py", *sorted((ROOT / "rynmesh/agent_runtime").glob("*.py")),
        *sorted((ROOT / "rynmesh/llm_package").glob("*.py"))]
    record = {"kind": "R06-development-controller", "formal_pass": False, "formal_trials_executed": 0,
        "case_id": "R06", "cohort": args.cohort, "prompt": load_case()["prompt"],
        "independent_review": "pending", "started_at": now(), "isolated_run_root": str(home),
        "owner_source_before_sha256": owner_hash, "planner_policy": policy, "case": load_case(),
        "code_snapshot": {p.relative_to(ROOT).as_posix(): sha(p) for p in paths}, "processes": [],
        "limitations": ["Single local consumer and HTTP document fixture; no formal repetition or two-host claim.",
            "Real model may fail to reach first compaction within four lifetime calls; the barrier is never injected.",
            "Outcome/semantic completion requires independent review of the actual trace and artifact.",
            "Budget continuation uses explicit owner message because failed is not a resumable action state."]}
    print(json.dumps({"output": str(args.output), "home": str(home)}), flush=True)
    config = {"home": str(home), "owner_home": str(args.owner_home), "source_id": args.source_id,
        "owner_source_sha256": owner_hash, "planner_policy": policy,
        "output": str(args.output.resolve()), "case_id": "R06", "cohort": args.cohort,
        "node": str(args.node) if args.node else None, "upstream": str(args.upstream) if args.upstream else None,
        "trial": getattr(args, "trial", 1),
        "release_activation": str(args.release_activation.resolve()) if getattr(args, "release_activation", None) else None}
    try:
        with ExitStack() as role_setup, fixture.FixtureDAV(root) as dav:
            try:
                config["dav_url"] = dav.url
                if config["release_activation"]:
                    from release_resume import prepare_documents
                    prepare_documents(role_setup, config, home, "R06", policy, dav.url)
                    home = Path(config["home"])
                for phase in ("before", "after"):
                    config["phase"] = phase
                    config_path = home / f"config-{phase}.json"
                    write(config_path, config)
                    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child", str(config_path)],
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    owned = OwnedConsumerProcess(process)
                    try:
                        marker = wait_file(home / f"worker-{phase}.json", process, 20)
                        if not marker:
                            raise RuntimeError("worker_identity_missing")
                        owned.bind_worker(marker["worker_pid"])
                        result_path = home / ("safe-checkpoint.json" if phase == "before" else "result-after.json")
                        result = wait_file(result_path, process, 650)
                        if result is None:
                            record["unmet_trigger"] = "safe_checkpoint_missing" if phase == "before" else "restarted_worker_result_missing"
                            break
                        record[phase] = result
                    finally:
                        proof = owned.terminate()
                        record["processes"].append({"phase": phase, **proof})
                        owned.close()
                    if not proof["confirmed_terminal"]:
                        raise RuntimeError("actual_worker_not_terminal")
                    if phase == "before":
                        from types import SimpleNamespace

                        from rynmesh.agent_runtime.store import AgentStore
                        from rynmesh.services import peer_box

                        # Independent encrypted-store read after the verified stop;
                        # the replacement is never allowed to repair a bad barrier.
                        if not (home / "messaging.x25519").exists():
                            raise RuntimeError("persisted_key_missing")
                        key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
                        persisted = checkpoint_state(SimpleNamespace(store=AgentStore(home / "agent", key)),
                                                     result["safe_checkpoint"]["checkpoint"]["task_id"])
                        record["parent_checkpoint_readback"] = persisted
                        if not persisted["safe"] or persisted != result["safe_checkpoint"]["checkpoint"]:
                            raise RuntimeError("persisted_checkpoint_mismatch")
                        config.update(checkpoint=result["safe_checkpoint"]["checkpoint"], identity=result["identity"])
            finally:
                record["http_audit"] = copy.deepcopy(dav.audit)
                record["source_unchanged"] = sha(input_path) == sha(FIXTURES / "documents/orion/long-notes.txt")
                artifact = root / TARGET
                if artifact.exists():
                    record["artifact"] = {"path": TARGET, "sha256": sha(artifact),
                        "text": artifact.read_text(encoding="utf-8"), "collector": "independent parent filesystem readback"}
    except Exception as exc:
        record["controller_error_type"] = type(exc).__name__
    finally:
        record["owner_source_after_sha256"] = sha(owner_path)
        if (args.output / "release-collection.json").exists():
            record["release_collection"] = json.loads((args.output / "release-collection.json").read_text(encoding="utf-8"))
            from release_collection import registry
            record.update(registry.read(record["release_collection"]["registration"]["path"])["binding"])
        record["owner_source_unchanged"] = sha(owner_path) == owner_hash
        for name in ("model-before.json", "model-after.json", "result-before.json", "result-after.json", "restart-readback.json"):
            if (home / name).exists():
                shutil.copyfile(home / name, args.output / name)
                if name.startswith("result-"):
                    result = json.loads((home / name).read_text(encoding="utf-8"))
                    if "task" in result:
                        record["task"] = result["task"]
        record["finished_at"] = now()
        write(args.output / "run.json", record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--owner-home", type=Path)
    parser.add_argument("--source-id", default=SOURCE_ID)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cohort", choices=("ryn", "reference"), default="ryn")
    parser.add_argument("--node", type=Path)
    parser.add_argument("--upstream", type=Path)
    from release_collection import add_arguments
    add_arguments(parser)
    args = parser.parse_args()
    if not args.child and args.cohort == "reference" and (not args.node or not args.upstream):
        parser.error("reference requires --node and --upstream")
    if args.child:
        child(args.child)
    elif args.live and args.owner_home and args.output and args.source_id == SOURCE_ID:
        run(args)
    else:
        parser.error("live execution requires --live --owner-home OWNER --output NEW_DIRECTORY and configured GLM source")


if __name__ == "__main__":
    main()
