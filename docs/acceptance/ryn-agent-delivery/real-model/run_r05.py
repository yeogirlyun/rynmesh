"""Thin R05 development controls; no formal verdict and no synthetic live deltas.

stream_controls uses an existing isolated two-host consumer with a bound,
stream-capable peer model. late_write_receipt starts a separate fresh local
consumer and holds the original DAV HTTP response after the actual file commit.
Neither mode changes production code or the frozen case.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
from document_cases import FIXTURES, model_request_record, sha  # noqa: E402
from planner_policy import SOURCE_ID, assert_mapping, map_fixture, read_planner  # noqa: E402

CASE_PATH = Path(__file__).parent.parent / "cases-v1.json"
TARGET = "reports/acceptance/late-receipt.md"
CONTENT = "LATE_RECEIPT_508"
DISPATCH = {"model_started", "tool_started", "verification_model_started", "context_compaction_started"}
TERMINAL = {"completed", "failed", "cancelled", "interrupted", "waiting_input", "waiting_approval"}
MODEL_CALL_CAP = 24


def now():
    return datetime.now(timezone.utc).isoformat()


def load_case():
    suite = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    if suite.get("partition") != "development":
        raise ValueError("development_partition_required")
    return copy.deepcopy(next(row for row in suite["cases"] if row["id"] == "R05"))


def action(request, task, name):
    return request("POST", f"/api/local/agent/tasks/{task['id']}/actions", {
        "action": name, "expected_control_revision": task["control_revision"],
        "client_request_id": str(uuid.uuid4())})


def dispatches(task, after=0):
    return [e for e in task.get("events", []) if e["type"] in DISPATCH and e["seq"] > after]


def trace_complete(complete, calls, *, observer=None):
    """Keep the actual request/usage for every inference stage, without reasoning."""
    from rynmesh.agent_runtime.store import AgentError
    def traced(model_id, messages, specs, cancelled, on_event=None, **options):
        call = {"at": now(), **model_request_record(model_id, messages, specs, options.get("expected_destination"))}
        if observer is not None and observer() is not None:
            call["dispatch_provenance"] = observer().model_request()
        calls.append(call)
        began = time.perf_counter()
        try:
            result = complete(model_id, messages, specs, cancelled, on_event, **options)
            call["response"] = {"message": {k: copy.deepcopy(v) for k, v in result["message"].items() if k != "reasoning_content"},
                "usage": copy.deepcopy(result.get("usage")), "finish_reason": result.get("finish_reason", "unknown")}
            return result
        except AgentError as exc:
            call["error_code"] = str(exc) if str(exc).startswith("agent_") and len(str(exc)) < 100 else "agent_error"
            if hasattr(exc, "usage") and hasattr(exc, "finish_reason"):
                call.update(usage=copy.deepcopy(exc.usage), finish_reason=exc.finish_reason)
            raise
        except Exception as exc:
            call["error_type"] = type(exc).__name__
            raise
        finally:
            call["elapsed_seconds"] = time.perf_counter() - began
    return traced


def stream_controls(request, model_id, *, timeout=240, poll=.1, settle_seconds=3):
    """Observe production event records; an unmet trigger is retained, not invented."""
    result = {"scenario": "stream_controls", "control": {}, "observations": [],
              "formal_pass": False, "dispatch_basis": "durable runtime invocation intents"}
    models = request("GET", "/api/local/agent/models")["models"]
    selected = next((m for m in models if m["id"] == model_id), {})
    result["selected_model"] = selected
    settings = request("GET", "/api/local/agent/settings")
    existing = request("GET", "/api/local/agent/tasks")["tasks"]
    result["preexisting_task_ids"] = [t["id"] for t in existing]
    if (selected.get("source") != "peer" or not selected.get("streaming")
            or not selected.get("available") or not selected.get("agent_supported")
            or not selected.get("destination_binding_supported")):
        result["unmet_trigger"] = "authenticated_streaming_peer_model_required"
        return result
    if not settings.get("enabled") or settings.get("permission_mode") not in {"read_only", "confirm_changes"}:
        result["unmet_trigger"] = "enabled_isolated_read_or_confirm_settings_required"
        return result
    if any(t["status"] not in {"completed", "failed", "cancelled", "interrupted"} for t in existing):
        result["unmet_trigger"] = "other_active_tasks_present"
        return result
    task = request("POST", "/api/local/agent/tasks", {"message": load_case()["prompt"],
        "model_id": model_id, "client_request_id": str(uuid.uuid4()),
        "budget_limits": {"max_total_model_calls": MODEL_CALL_CAP}})
    result["created_task"] = task
    baseline, cursor, deltas, phase = task["control_revision"], 0, [], "deltas"
    deadline, cancel_at, resume_cursor = time.monotonic() + timeout, None, None
    try:
        while time.monotonic() < deadline:
            events = request("GET", f"/api/local/agent/tasks/{task['id']}/events?after={cursor}")
            if events["cursor"] != cursor:
                result["observations"].append(copy.deepcopy(events))
            cursor = events["cursor"]
            task = request("GET", f"/api/local/agent/tasks/{task['id']}")
            if phase == "deltas":
                deltas.extend(e for e in events["events"] if e["type"] == "text_delta" and e.get("text"))
                if events["control_revision"] != baseline:
                    result["unmet_trigger"] = "control_changed_before_pause"
                    break
                if len(deltas) >= 10 and task["status"] == "running":
                    # Use the observer's last seen control revision, not the
                    # changing event cursor or a freshly substituted revision.
                    paused = action(request, {**task, "control_revision": baseline}, "pause")
                    result["pause_receipt"] = paused
                    result["control"]["pause_accepted"] = paused["status"] == "paused"
                    phase = "pause"
            elif phase == "pause" and task["run_state"] == "paused":
                result["safe_checkpoint"] = task
                resumed = action(request, task, "resume")
                result["resume_receipt"] = resumed
                result["control"]["resume_accepted"] = resumed["status"] == "queued"
                resume_cursor, phase = resumed["event_cursor"], "active"
            elif phase == "active" and dispatches(task, resume_cursor):
                if task["status"] != "running" or len(dispatches(task, resume_cursor)) != 1:
                    result["unmet_trigger"] = "next_operation_finished_before_observation"
                    break
                result["active_before_cancel"] = task
                cancelled = action(request, task, "cancel")
                result["cancel_receipt"] = cancelled
                result["control"]["cancel_accepted"] = cancelled["status"] == "cancelled"
                cancel_at, phase = time.monotonic(), "cancelled"
            if phase == "cancelled" and time.monotonic() - cancel_at >= settle_seconds:
                break
            if phase not in {"pause", "cancelled"} and task["status"] in TERMINAL:
                result["unmet_trigger"] = "task_stopped_before_required_intervention"
                break
            time.sleep(poll)
        else:
            result["unmet_trigger"] = "controller_deadline"
    except Exception as exc:
        result["controller_error_type"] = type(exc).__name__
    finally:
        if phase != "cancelled" and task["status"] not in {"completed", "failed", "cancelled"}:
            result["cleanup_cancel_receipt"] = action(request, task, "cancel")
        result["task"] = request("GET", f"/api/local/agent/tasks/{task['id']}")
    result["text_delta_count_before_pause"] = len(deltas)
    result["stable_control_revision"] = baseline
    cancel_cursor = result.get("cancel_receipt", {}).get("event_cursor")
    result["metrics"] = {"dispatches_after_cancel": len(dispatches(result["task"], cancel_cursor)) if cancel_cursor is not None else None,
                         "post_cancel_observation_seconds": settle_seconds if cancel_at is not None else 0}
    result["control"]["cancelled_run_resumed_automatically"] = (
        any(e["type"] == "resumed" and e["seq"] > cancel_cursor for e in result["task"]["events"])
        if cancel_cursor is not None else None)
    return result


_spec = importlib.util.spec_from_file_location("r05_fixture_dav", FIXTURES / "webdav_fixture.py")
_dav = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dav)


class CommitResponseDAV(_dav.FixtureDAV):
    """Only delay original response headers, after FixtureDAV commits and audits."""
    def __init__(self, root):
        super().__init__(root)
        self.committed, self.release = threading.Event(), threading.Event()
        self.barrier = {}

    def handle(self, request):
        send = request.send_response
        if request.command == "PUT" and request.path == "/dav/" + TARGET:
            def held(status, *args, **kwargs):
                if status == 201:
                    self.barrier.update(commit_observed_at=now(), commit_counter_ns=time.perf_counter_ns(), sha256=sha(self.root / TARGET),
                                        original_status=status, original_response_count=1)
                    self.committed.set()
                    if not self.release.wait(30):
                        self.barrier["hold_expired"] = True
                        request.close_connection = True
                        raise TimeoutError("r05_response_hold_expired")
                    self.barrier["original_response_released_at"] = now()
                    self.barrier["release_counter_ns"] = time.perf_counter_ns()
                return send(status, *args, **kwargs)
            request.send_response = held
        return super().handle(request)

    def __exit__(self, *args):
        self.release.set()
        return super().__exit__(*args)


def late_write_controls(runtime, task, dav, *, timeout=600):
    result = {"scenario": "late_write_receipt", "formal_pass": False, "approvals": []}
    deadline, cancelled, worker = time.monotonic() + timeout, None, None
    try:
        for _ in range(160):
            task = runtime.get(task["id"])
            if task["status"] == "waiting_approval":
                approval = task["pending_approval"]
                context = runtime._execution_context(runtime.store.read()["tasks"][task["id"]])
                permitted = False
                if approval["tool"] == "ryn_create_text_file":
                    _, _, path, data = runtime.tools.resource_writes.target(approval["tool"], approval["arguments"], context)
                    permitted = path == TARGET and data == CONTENT.encode()
                result["approvals"].append({"approved": permitted, "approval": copy.deepcopy(approval)})
                runtime.action(task["id"], {"action": "approve" if permitted else "deny",
                    "approval_id": approval["id"], "expected_control_revision": task["control_revision"]})
                if not permitted:
                    result["unmet_trigger"] = "unexpected_write_not_approved"
                    break
                continue
            if task["status"] in TERMINAL or task["status"] == "paused":
                break
            worker = threading.Thread(target=runtime.run_once, daemon=True)
            worker.start()
            while worker.is_alive() and time.monotonic() < deadline:
                if dav.committed.is_set() and cancelled is None:
                    before = runtime.get(task["id"])
                    cancelled = runtime.action(task["id"], {"action": "cancel",
                        "expected_control_revision": before["control_revision"]})
                    persisted = runtime.store.read()["tasks"][task["id"]]
                    result["cancel_receipt"] = cancelled
                    result["cancel_persisted_at"] = now()
                    result["cancel_counter_ns"] = time.perf_counter_ns()
                    result["cancel_persisted_while_response_held"] = persisted["status"] == "cancelled" and not dav.release.is_set()
                    if not result["cancel_persisted_while_response_held"]:
                        raise RuntimeError("cancel_not_persisted")
                    dav.release.set()
                worker.join(.01)
            if worker.is_alive():
                result["unmet_trigger"] = "controller_deadline"
                break
            if cancelled is not None:
                # The real scheduler must also have nothing further to dispatch.
                runtime.run_once()
                runtime.run_once()
                break
    except Exception as exc:
        result["controller_error_type"] = type(exc).__name__
    finally:
        current = runtime.get(task["id"])
        if current["status"] not in {"completed", "failed", "cancelled"}:
            result["cleanup_cancel_receipt"] = runtime.action(task["id"], {"action": "cancel"})
        dav.release.set()
        if worker is not None:
            worker.join(5)
        result["worker_finished"] = worker is None or not worker.is_alive()
        result["task"] = runtime.get(task["id"])
        result["http_audit"] = copy.deepcopy(dav.audit)
        result["response_barrier"] = copy.deepcopy(dav.barrier)
    saved = runtime.store.read()["tasks"][task["id"]]
    receipts = [r for r in saved.get("receipts", []) if r.get("tool") == "ryn_create_text_file"]
    result["receipts"] = receipts
    final = dav.root / TARGET
    result["artifact"] = {"path": TARGET, "exists": final.is_file(), "sha256": sha(final) if final.is_file() else None}
    cancel_cursor = cancelled["event_cursor"] if cancelled else None
    result["checks"] = {
        "receipt_retained": bool(receipts) and any(r.get("status") == "confirmed" and r.get("result", {}).get("business_acknowledged") is True for r in receipts),
        "effective_writes": sum(e["mutation"] and e["method"] == "PUT" for e in dav.audit),
        "put_attempts": sum(e["method"] == "PUT" for e in dav.audit),
        "exact_file": final.is_file() and final.read_bytes() == CONTENT.encode(),
        "new_dispatches_after_cancel": len(dispatches(result["task"], cancel_cursor)) if cancel_cursor is not None else None,
        "cancelled": result["task"]["status"] == "cancelled"}
    return result


def run_late(owner, source_id, *, release=None):
    from fastapi import FastAPI

    from rynmesh.agent_runtime.bridge import build_agent_integrations
    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.store import AgentStore
    from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
    from rynmesh.llm_package.routes import install_llm_routes
    from rynmesh.nas import NasPlugin
    from rynmesh.services import peer_box
    from rynmesh.store import RynmeshStore

    owner_path = owner / "llm/sources.enc"
    owner_hash = sha(owner_path)
    source, adapter, policy = read_planner(owner, source_id)
    home = Path(tempfile.mkdtemp(prefix="ryn-r05-late-"))
    os.environ.update(RYNMESH_HOME=str(home), RYNMESH_REGISTRY_URL="", RYNMESH_AUTO_REGISTER="0",
                      RYNMESH_LAN_DISCOVERY="0", RYNMESH_DEFAULT_DISCOVERY="0")
    root = home / "fixture-documents"
    (root / "reports/acceptance").mkdir(parents=True)
    (root / ".ryn-acceptance-isolated").touch()
    record = {"isolated_run_root": str(home), "model_calls": [], "planner_policy": policy,
              "owner_source_before_sha256": owner_hash}
    collector = None
    with ExitStack() as stack, CommitResponseDAV(root) as dav:
        roles = None
        if release:
            from release_world import ReleaseWorld
            roles = stack.enter_context(ReleaseWorld(home / "roles", "R05", policy))
            home = roles.execution_store.home
        store = roles.execution_store if roles else RynmeshStore(home=home, network_dir=home / "network")
        key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
        app = roles.execution_app if roles else FastAPI()
        if not roles:
            install_llm_routes(app, store=store, home=home, messaging_key=key,
                               resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
        nas = app.state.nas_plugin = NasPlugin(home)
        nas.enable(True)
        try:
            connection = nas.configure({"name": "docs-alpha", "system": "generic", "protocol": "webdav", "url": dav.url,
                "username": "fixture", "password": "fixture", "allow_ai": True, "writable": True})
            services = (roles.owner_app if roles else app).state.llm_agent_services
            sources = services["source_store"]
            model = policy["model_id"]
            if roles:
                selected, provider_selected = roles.selected, roles.provider_selected
                record["planner_policy_mapping"] = copy.deepcopy(roles.planner_policy_mapping)
            else:
                fixture = sources.prepare({"name": "Isolated R05 inference", "kind": "api", "provider": source["provider"],
                    "base_url": source["base_url"], "api_key": "in-memory-adapter-placeholder", "models": [model],
                    "context_window": source["context_window"], "request_defaults": source.get("request_defaults", {})})
                fixture.update(status="ready", tool_support={model: "supported"})
                record["planner_policy_mapping"] = map_fixture(fixture, model, policy)
                sources.save(fixture)
                selected = "local/" + sources.model_service_id(fixture, model)
                provider_selected = selected
            original_provider = services["provider_for"]
            def provider_for(service_id):
                if sha(owner_path) != owner_hash:
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
            bridge.complete = trace_complete(bridge.complete, record["model_calls"], observer=lambda: collector)
            runtime = AgentRuntime(AgentStore(home / "agent", key), bridge, tools, catalog,
                                   owner_id="owner-test", consumer_node_id=store.peer_id)
            app.state.agent_runtime = runtime
            runtime.update_settings({"enabled": True, "model_id": selected, "max_steps": 24,
                "max_seconds": 600, "max_context_chars": 100000, "permission_mode": "confirm_changes"})
            case = load_case()
            scope = {"resources": [{"connection_id": connection["id"], "root_path": "reports/acceptance", "recursive": True,
                "actions": ["list", "read_ai", "create_directory", "create_file"]}], "allow_model_data": True}
            task = runtime.create({"message": next(s for s in case["setup"]["subscenarios"] if s["id"] == "late_write_receipt")["prompt"],
                "scope": scope, "expected_destination_revision": catalog.resolve(selected)["destination_revision"],
                "budget_limits": {"max_total_model_calls": MODEL_CALL_CAP}})
            record.update(consumer_peer_id=store.peer_id, logical_consumer="node-b", scope_selection=scope)
            if release:
                collector = roles.collector(release["activation"], release["output"], "ryn", release["trial"], runtime, task,
                    connectors={"node-b": nas}, bindings={"docs-alpha": connection["id"]}, subscenario="late_write_receipt")
                collector.register()
                collector.attach_candidate()
                record.update(case_id="R05", cohort="ryn", prompt=task["messages"][0]["content"],
                    started_at=collector.initial["at"], planner_model=policy["model_id"], provider=source["provider"], real_model=True,
                    bindings={**roles.bindings, "docs-alpha": connection["id"]})
            record.update(late_write_controls(runtime, task, dav))
        except Exception as exc:
            record.update(controller_error_type=type(exc).__name__, http_audit=copy.deepcopy(dav.audit),
                          response_barrier=copy.deepcopy(dav.barrier))
        finally:
            if collector is not None:
                from release_collection import registry
                record["release_collection"] = collector.finalize()
                record.update(registry.read(collector.registration_path)["binding"], ended_at=now())
            for connection_id in list(nas.sessions):
                nas._close_session(connection_id)
            app.state.llm_shutdown()
    record["owner_source_unchanged"] = sha(owner_path) == owner_hash
    record["owner_source_after_sha256"] = sha(owner_path)
    if source.get("api_key") and source["api_key"] in json.dumps(record):
        raise RuntimeError("evidence_export_guard")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--scenario", choices=("stream_controls", "late_write_receipt", "both"), required=True)
    parser.add_argument("--consumer-home", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--model-id")
    parser.add_argument("--owner-home", type=Path)
    parser.add_argument("--source-id", default=SOURCE_ID)
    parser.add_argument("--output", type=Path, required=True)
    from release_collection import add_arguments
    add_arguments(parser)
    args = parser.parse_args()
    if args.release_activation and args.scenario != "late_write_receipt":
        parser.error("registered streaming task must use the pre-dispatch two-host registration endpoint")
    if not args.live:
        parser.error("--live required; offline tests never call an owner model")
    if args.scenario != "late_write_receipt" and not all((args.consumer_home, args.port, args.model_id)):
        parser.error("stream requires an existing isolated consumer home, port and authenticated peer model id")
    if args.scenario != "stream_controls" and (not args.owner_home or args.source_id != SOURCE_ID):
        parser.error("late write requires --owner-home and the configured GLM source")
    args.output.mkdir(parents=True, exist_ok=False)
    paths = [Path(__file__), CASE_PATH, FIXTURES / "world.json", FIXTURES / "webdav_fixture.py",
             Path(__file__).with_name("planner_policy.py"), Path(__file__).with_name("document_cases.py"),
             Path(__file__).parent.parent / "two-host/control.py", Path(__file__).parent.parent / "two-host/node.py",
             ROOT / "rynmesh/nas.py",
             *sorted((ROOT / "rynmesh/agent_runtime").glob("*.py")), *sorted((ROOT / "rynmesh/llm_package").glob("*.py"))]
    record = {"kind": "R05-development-controller", "formal_pass": False, "formal_trials_executed": 0,
        "started_at": now(), "subscenarios": {}, "independent_review": "pending",
        "code_snapshot": {p.relative_to(ROOT).as_posix(): sha(p) for p in paths},
        "limits": ["Existing peer consumer may have prior development tasks; no claim of a fresh formal repetition.",
                   "Local API Agent catalog streaming remains False; no capability is overridden.",
                   "A capability advertisement is not proof of real vendor SSE; only observed runtime text deltas meet the trigger.",
                   "Post-cancel observation is bounded; supplier compute/billing stop is not established."]}
    try:
        if args.scenario != "late_write_receipt":
            sys.path.insert(0, str(Path(__file__).parent.parent / "two-host"))
            from control import Control
            control = Control(args.consumer_home, args.port)
            def request(method, path, body=None):
                observed = control.request(method, path, body)
                if observed["status_code"] != 200:
                    raise RuntimeError("control_http_" + str(observed["status_code"]))
                return observed["response"]
            try:
                before = request("GET", "/api/local/two-host/evidence")
                record["consumer_before"] = {k: before[k] for k in ("identity", "runtime_owner_id", "runtime_consumer_node_id")}
                record["subscenarios"]["stream_controls"] = stream_controls(request, args.model_id)
                after = request("GET", "/api/local/two-host/evidence")
                record["consumer_after"] = {k: after[k] for k in ("identity", "runtime_owner_id", "runtime_consumer_node_id")}
            finally:
                control.client.close()
        if args.scenario != "stream_controls":
            record["subscenarios"]["late_write_receipt"] = run_late(args.owner_home.resolve(), args.source_id,
                release={"activation": args.release_activation, "trial": args.trial, "output": args.output / "late_write_receipt"}
                if args.release_activation else None)
            if args.release_activation:
                child = record["subscenarios"]["late_write_receipt"]
                child["code_snapshot"] = record["code_snapshot"]
                (args.output / "late_write_receipt/run.json").write_text(json.dumps(child, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        record["controller_error_type"] = type(exc).__name__
    finally:
        record["ended_at"] = now()
        record["code_unchanged"] = all(sha(ROOT / p) == h for p, h in record["code_snapshot"].items())
        (args.output / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "formal_pass": False,
                      "scenarios": {k: {field: value.get(field) for field in ("unmet_trigger", "controller_error_type", "checks", "metrics")}
                                    for k, value in record["subscenarios"].items()},
                      "controller_error_type": record.get("controller_error_type")}), flush=True)


if __name__ == "__main__":
    main()
