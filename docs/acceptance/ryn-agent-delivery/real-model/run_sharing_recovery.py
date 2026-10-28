"""R02 real process restart controller; opt-in GLM, no formal scoring."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from consumer_process_proof import OwnedConsumerProcess  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from planner_policy import read_planner  # noqa: E402
from sharing_cases import SharingWorld  # noqa: E402

from rynmesh.agent_runtime.store import AgentStore  # noqa: E402
from rynmesh.atomic_io import atomic_write_json  # noqa: E402
from rynmesh.llm_package.routes import install_llm_routes  # noqa: E402
from rynmesh.services import peer_box  # noqa: E402
from rynmesh.store import RynmeshStore  # noqa: E402

SAFE_ERROR_CODES = frozenset({
    "consumer_exited_at_restart_boundary", "consumer_failed_before_expected_boundary", "consumer_deadline_exceeded",
    "approval_not_durably_waiting", "consumer_actual_worker_not_terminal", "consumer_process_handle_unavailable",
    "consumer_process_time_unavailable", "consumer_process_wait_failed", "consumer_process_ancestry_unavailable",
    "consumer_process_snapshot_unavailable", "consumer_launcher_identity_changed", "consumer_process_identity_unsupported",
    "consumer_process_identity_not_live", "consumer_worker_not_owned", "consumer_worker_invalid_identity",
    "consumer_worker_identity_changed", "consumer_launcher_not_live", "consumer_worker_not_live",
    "consumer_worker_identity_unsupported", "consumer_process_proof_closed", "consumer_owned_process_not_terminal",
    "consumer_injected_cleanup_failure",
})


def safe_error_code(exc):
    value = exc.args[0] if len(exc.args) == 1 else None
    return value if isinstance(value, str) and value in SAFE_ERROR_CODES else None


def failure_diagnostic(exc, stage, allowed_files):
    """No exception text, locals, absolute paths or external frames in evidence."""
    result = {"stage": stage, "exception_type": type(exc).__name__}
    for name in ("errno", "winerror"):
        value = getattr(exc, name, None)
        if type(value) is int:
            result[name] = value
    frames = []
    tb = exc.__traceback__
    while tb is not None:
        filename = Path(tb.tb_frame.f_code.co_filename).resolve()
        if filename.is_relative_to(ROOT):
            relative = filename.relative_to(ROOT).as_posix()
            function = tb.tb_frame.f_code.co_name
            if relative in allowed_files and function.isidentifier() and len(function) <= 128:
                frames.append({"file": relative, "function": function, "line": tb.tb_lineno})
        tb = tb.tb_next
    result["repository_frames"] = frames[-8:]
    return result


def wait_child(child, marker=None, *, timeout=660, owned=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker is not None and marker.exists():
            if child.poll() is not None:
                raise RuntimeError("consumer_exited_at_restart_boundary")
            return
        if child.poll() is not None:
            if child.returncode != 0 or marker is not None:
                raise RuntimeError("consumer_failed_before_expected_boundary")
            return
        time.sleep(.1)
    # Timeouts are terminalized explicitly before any replacement is launched.
    if owned is not None:
        owned.terminate()
    else:
        child.kill()
        child.wait(timeout=10)
    raise RuntimeError("consumer_deadline_exceeded")


def execute(output, *, live=False, source_id=None, home=None, release_activation=None, trial=1,
            fixture_planner_policy=None, cohort="ryn", node=None, upstream=None):
    if cohort not in {"ryn", "reference"} or (cohort == "reference" and (not node or not upstream)):
        raise ValueError("reference_dependencies_required")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    owner_home = Path(os.environ.get("RYNMESH_HOME", str(Path.home() / ".rynmesh")))
    owner_path = owner_home / "llm/sources.enc"
    owner_hash = hashlib.sha256(owner_path.read_bytes()).hexdigest() if live else None
    if live and source_id != "src-7de9fb1d36c44bc8":
        raise ValueError("configured_glm_source_required")
    if live and fixture_planner_policy is not None:
        raise ValueError("fixture_policy_forbidden_in_live_trial")
    planner_policy = read_planner(owner_home, source_id)[2] if live else fixture_planner_policy
    home = Path(home) if home else Path(tempfile.mkdtemp(prefix="ryn-sharing-recovery-"))
    env = {**os.environ, "RYNMESH_HOME": str(home), "RYNMESH_REGISTRY_URL": "", "RYNMESH_AUTO_REGISTER": "0",
           "RYNMESH_LAN_DISCOVERY": "0", "RYNMESH_DEFAULT_DISCOVERY": "0", "PYTHONIOENCODING": "utf-8"}
    paths = [*sorted((ROOT / "rynmesh/agent_runtime").glob("*.py")), *sorted((ROOT / "rynmesh/llm_package").glob("*.py")),
             *sorted((ROOT / "rynmesh/ai_access").glob("*.py")), *sorted((ROOT / "rynmesh/friends").glob("*.py")),
             ROOT / "rynmesh/file_transactions.py", ROOT / "rynmesh/atomic_io.py", ROOT / "tests/fixtures/agent_acceptance/source_fixture.py",
             ROOT / "tests/fixtures/agent_acceptance/world.json", ROOT / "docs/acceptance/ryn-agent-delivery/cases-v1.json",
             *[Path(__file__).with_name(name) for name in ("run_sharing_recovery.py", "consumer_process_proof.py", "sharing_recovery_worker.py", "sharing_cases.py", "query_cases.py", "planner_policy.py", "release_resume.py", "release_world.py", "release_collection.py")]]
    snapshot = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    diagnostic_files = {*snapshot, "tests/test_agent_consumer_process_proof.py"}
    record = {"case_id": "R02", "kind": "real-sharing-recovery-development-trial" if live else "local-sharing-recovery-check",
        "cohort": cohort, "real_model": live, "formal_case_score": False, "code_snapshot": snapshot,
        "isolated_run_root": str(home), "started_at": datetime.now(timezone.utc).isoformat()}
    began = time.monotonic()
    worker = Path(__file__).with_name("sharing_recovery_worker.py")
    child = None
    owned = None
    processes = []
    from release_world import ReleaseSharingWorld
    world_type = ReleaseSharingWorld if release_activation else SharingWorld
    with world_type(home, "R02") as world:
        app = FastAPI()
        store = RynmeshStore(home=home, network_dir=home / "network")
        key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
        install_llm_routes(app, store=store, home=home, messaging_key=key, resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
        stage = "configure_fixture"
        try:
            world.planner_policy = planner_policy
            world.configure(app, store)
            record.update(planner_policy=planner_policy, planner_policy_mapping=world.planner_policy_mapping)
            # No parent AgentRuntime and no parent approvals or business writes.
            config = {"home": str(home), "output": str(output), "bindings": world.bindings,
                      "live": live, "source_id": source_id, "owner_home": str(owner_home),
                      "owner_source_sha256": owner_hash, "planner_policy": planner_policy, "phase": "start",
                      "case_id": "R02", "cohort": cohort, "trial": trial,
                      "node": str(node) if node else None, "upstream": str(upstream) if upstream else None,
                      "release_activation": str(Path(release_activation).resolve()) if release_activation else None}
            config_path = output / "consumer-config.json"
            stage = "write_start_config"
            atomic_write_json(config_path, config)
            stage = "snapshot_initial_state"
            source_hash = hashlib.sha256(world.sources.path.read_bytes()).hexdigest()
            original_grants = world.grants.list()
            stage = "open_start_log"
            with (output / "start-process.log").open("w", encoding="utf-8") as log:
                stage = "launch_start_consumer"
                child = subprocess.Popen([sys.executable, str(worker), str(config_path)], stdout=log, stderr=log, env=env, cwd=ROOT)
                stage = "own_start_consumer"
                owned = OwnedConsumerProcess(child)
                processes.append(owned)
                stage = "wait_approval_boundary"
                wait_child(child, output / "approval-ready.json", owned=owned)
                stage = "read_approval_boundary"
                boundary = json.loads((output / "approval-ready.json").read_text(encoding="utf-8"))
                stage = "bind_original_worker"
                owned.bind_worker(boundary.get("pid"))
                first_pid = owned.worker_pid
                stage = "read_durable_approval"
                stored = AgentStore(home / "agent", key).read()["tasks"][boundary["task"]["id"]]
                stage = "verify_durable_approval"
                if cohort == "reference":
                    session = stored["reference_session"]
                    if stored["reference_operations"][session["interrupted_operation_id"]]["state"] != "waiting_approval":
                        raise RuntimeError("native_approval_not_durably_waiting")
                elif stored["status"] != "waiting_approval" or stored.get("in_flight"):
                    raise RuntimeError("approval_not_durably_waiting")
                record["before_restart"] = boundary
                stage = "collect_before_approval"
                record["before_approval"] = {"sources_unchanged": hashlib.sha256(world.sources.path.read_bytes()).hexdigest() == source_hash,
                    "grants_unchanged": world.grants.list() == original_grants,
                    "writes": sum(e["type"] == "tool_started" and e.get("effect") == "write" for e in stored["events"])}
                stage = "terminate_original_consumer"
                record["terminated_consumer"] = owned.terminate()
                stage = "verify_original_terminal"
                if not record["terminated_consumer"]["confirmed_terminal"]:
                    raise RuntimeError("consumer_actual_worker_not_terminal")
            print(json.dumps({"phase": "consumer_killed_at_waiting_approval", "pid": first_pid,
                              "launcher_pid": owned.launcher_pid}), flush=True)
            config["phase"] = "recover"
            stage = "write_recovery_config"
            atomic_write_json(config_path, config)
            stage = "open_recovery_log"
            with (output / "recover-process.log").open("w", encoding="utf-8") as log:
                stage = "launch_recovery_consumer"
                child = subprocess.Popen([sys.executable, str(worker), str(config_path)], stdout=log, stderr=log, env=env, cwd=ROOT)
                stage = "own_recovery_consumer"
                owned = OwnedConsumerProcess(child)
                processes.append(owned)
                record["replacement_consumer_launcher_pid"] = child.pid
                stage = "wait_recovery_consumer"
                wait_child(child, owned=owned)
            stage = "read_recovery_marker"
            recovered = json.loads((output / "recover-worker.json").read_text(encoding="utf-8"))
            stage = "collect_recovery_state"
            record["replacement_consumer_pid"] = recovered["pid"]
            record["after_restart"] = recovered["reloaded_task"]
            record["task"] = recovered["task"]
            record["approval"] = {"identity_preserved": boundary["task"]["pending_approval"] == recovered["reloaded_task"]["pending_approval"],
                "control_revision_preserved": boundary["task"]["control_revision"] == recovered["reloaded_task"]["control_revision"],
                "different_process": first_pid != recovered["pid"]}
            if cohort == "reference":
                record["approval"].update(identity_preserved=stored["reference_session"] == recovered["reference_session"],
                    native_pending_approval_restored=False, case_status="failed")
                record["reference_recovery"] = recovered["reference_recovery"]
            events = record["task"]["events"]
            starts = [e for e in events if e["type"] == "tool_started" and e.get("effect") == "write"]
            successful = [e for e in events if e["type"] == "tool_result" and e.get("call_id") in {s["call_id"] for s in starts}
                          and isinstance(e.get("result"), dict) and e["result"].get("ok") is True]
            record["metrics"] = {"writes_before_approval": record["before_approval"]["writes"],
                "write_dispatches": len(starts), "effective_writes": len(successful),
                "source_file_replacements": sum(row["changed"] for row in recovered["source_replacements"]),
                "counting_note": "Each successful write tool receipt and actual source-file replacement counted separately; no collapsing a multi-step sequence."}
            record["source_replacements"] = recovered["source_replacements"]
            collector = None
            if release_activation:
                from release_resume import restore_sharing_parent
                collector = restore_sharing_parent(config, world, app, store)
            stage = "refresh_receiver_catalog"
            rows = world.receiver_catalog.refresh(store.peer_id)["services"]
            row_b = next((r for r in rows if r["service"]["package_id"] == world.sid), None)
            stage = "probe_allowed_receiver"
            world.probe_receiver("model-b", world.receiver, row_b)
            stage = "probe_denied_receiver"
            world.probe_receiver("model-b", world.other)
            if collector is not None:
                record["release_collection"] = collector.finalize()
                from release_collection import registry
                record.update(registry.read(collector.registration_path)["binding"])
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            record["failure_diagnostic"] = failure_diagnostic(exc, stage, diagnostic_files)
            if isinstance(exc, RuntimeError) and safe_error_code(exc) is not None:
                record["error_code"] = safe_error_code(exc)
        finally:
            cleanup_errors = []
            try:
                for process in processes:
                    try:
                        process.terminate()
                    except Exception as exc:
                        cleanup_errors.append({"launcher_pid": process.launcher_pid, "error_type": type(exc).__name__,
                                               "error_code": safe_error_code(exc) or "consumer_cleanup_error",
                                               "failure_diagnostic": failure_diagnostic(exc, "cleanup_owned_process", diagnostic_files)})
                if child is not None and child.poll() is None:
                    try:
                        child.kill()
                        child.wait(timeout=10)
                    except Exception as exc:
                        cleanup_errors.append({"launcher_pid": child.pid, "error_type": type(exc).__name__,
                                               "error_code": "consumer_launcher_cleanup_failed",
                                               "failure_diagnostic": failure_diagnostic(exc, "cleanup_launcher", diagnostic_files)})
            finally:
                for process in processes:
                    process.close()
            if cleanup_errors:
                record["cleanup_errors"] = cleanup_errors
                record.setdefault("error_type", "RuntimeError")
                record.setdefault("error_code", "consumer_cleanup_failed")
            record["model_calls"], record["approvals"] = [], []
            for phase in ("start", "recover"):
                path = output / (phase + "-worker.json")
                if path.exists():
                    value = json.loads(path.read_text(encoding="utf-8"))
                    record["model_calls"].extend(value.get("model_calls", []))
                    record["approvals"].extend(value.get("approvals", []))
                    record.update({key: value[key] for key in ("planner_model", "provider", "task", "release_collection") if key in value})
            if hasattr(world, "initial_sources"):
                observed = world.collect()
                observed.pop("approvals")
                record.update(observed)
            record.update(prompt=world.case["prompt"], consumer_peer_id=store.peer_id,
                elapsed_seconds=time.monotonic() - began, ended_at=datetime.now(timezone.utc).isoformat(),
                owner_source_unchanged=not live or hashlib.sha256(owner_path.read_bytes()).hexdigest() == owner_hash,
                code_unchanged=all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest for name, digest in snapshot.items()))
            text = json.dumps(record, ensure_ascii=False, indent=2)
            if world.upstream.secret in text or '"reasoning_content"' in text:
                raise RuntimeError("evidence_export_guard")
            (output / "run.json").write_text(text + "\n", encoding="utf-8")
            app.state.llm_shutdown()
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--live", action="store_true")
    modes.add_argument("--fixture-only", action="store_true")
    parser.add_argument("--source-id")
    parser.add_argument("--cohort", choices=("ryn", "reference"), default="ryn")
    parser.add_argument("--node", type=Path)
    parser.add_argument("--upstream", type=Path)
    from release_collection import add_arguments
    add_arguments(parser)
    args = parser.parse_args()
    if args.live and not args.source_id:
        parser.error("live requires the configured GLM source ID")
    output = Path(__file__).parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + "R02-ryn" + ("" if args.live else "-fixture"))
    print(json.dumps({"output": str(output)}), flush=True)
    result = execute(output, live=args.live, source_id=args.source_id,
                     release_activation=args.release_activation, trial=args.trial,
                     cohort=args.cohort, node=args.node, upstream=args.upstream)
    print(json.dumps({"output": str(output), "metrics": result.get("metrics"), "error": result.get("error_code"), "formal_case_score": False}), flush=True)


if __name__ == "__main__":
    main()
