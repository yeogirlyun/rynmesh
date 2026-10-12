"""P03 owner-role draft: exact approval, then a real prewrite revocation barrier.

Fixture execution is offline mechanism evidence. Live mode is explicit, uses only
the configured GLM source read with migrate=False, and never registers a trial.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from planner_policy import SOURCE_ID, assert_mapping, read_planner  # noqa: E402
from role_query_world import composer  # noqa: E402
from run_steer_r04 import FixturePlanner as R04FixturePlanner  # noqa: E402
from run_steer_r04 import R04World, digest  # noqa: E402

from rynmesh.agent_runtime.store import AgentError  # noqa: E402

PREWRITE_WAIT_SECONDS = 15


class P03World(R04World):
    def __init__(self, home, *, registered_roles=False):
        super().__init__(home, registered_roles=registered_roles)
        self.effective_input = composer.load_effective_case("P03")
        self.case = copy.deepcopy(self.effective_input["effective_case"])


def install_runtime(app, store, home, key, integrations, owner_id):
    """Use the actual owner settings endpoint; never run the registered worker."""
    from fastapi import HTTPException

    from rynmesh.agent_runtime.routes import install_agent
    token = uuid.uuid4().hex
    def local_control(request):
        if request.headers.get("x-p03-owner") != token:
            raise HTTPException(403, "owner_control_required")
    runtime = install_agent(app, store=store, home=home, workers=app.state.background_workers,
        local_control=local_control, messaging_key=key, integrations=integrations)
    runtime.owner_id = owner_id
    return runtime, token


def execute(runtime, world, owner_token):
    from fastapi.testclient import TestClient
    world.attach(runtime.tools)
    created = runtime.create({"message": world.case["prompt"], "model_id": world.selected,
        "budget_limits": {"max_total_model_calls": 24, "max_total_seconds": 600}})
    world.task_id = created["id"]
    if getattr(world, "on_task_created", None):
        world.on_task_created(created)
    control = {"prewrite_revocation_observed": False, "approval_consumed": False}
    approval_evidence = {"tampered_request_rejected": False}
    entered, release = threading.Event(), threading.Event()
    expected, errors = {}, []
    original_execute = runtime.tools.execute
    worker = None
    started = time.monotonic()

    def stamp():
        return {"at": datetime.now(timezone.utc).isoformat(), "elapsed_seconds": time.monotonic() - started}

    def barrier(name, arguments, **kwargs):
        if name == expected.get("tool") and arguments == expected.get("arguments"):
            if entered.is_set():
                raise RuntimeError("p03_write_replay_forbidden")
            persisted = runtime.store.read()["tasks"][world.task_id]
            flight = persisted.get("in_flight") or {}
            control.update(barrier_entered=stamp(), worker_thread_id=threading.get_ident(),
                approval_consumed=persisted.get("approved") is None and flight.get("authorization") == "approved"
                and flight.get("permission_record", {}).get("approval_id") == expected["id"],
                in_flight=copy.deepcopy(flight))
            entered.set()
            if not release.wait(PREWRITE_WAIT_SECONDS):
                raise RuntimeError("p03_prewrite_barrier_timeout")
            control["barrier_released"] = stamp()
        # Do not fake rejection: the production tool and its permission guard run.
        return original_execute(name, arguments, **kwargs)

    def tick():
        try:
            runtime.run_once()
        except Exception as exc:
            errors.append(type(exc).__name__)

    runtime.tools.execute = barrier
    try:
        with TestClient(world.app, headers={"x-p03-owner": owner_token}) as client:
            for _ in range(160):
                if time.monotonic() - started >= 600:
                    control["error_code"] = "controller_deadline"
                    break
                row = runtime.get(world.task_id)
                if row["status"] == "waiting_approval":
                    pending = row["pending_approval"]
                    exact = (pending["tool"] == "ryn_share_model_with_friend" and world.permitted(pending))
                    if expected or not exact:
                        control["error_code"] = "unexpected_write_approval"
                        break
                    expected.update(copy.deepcopy(pending))
                    approval_evidence["original"] = copy.deepcopy(pending)
                    request = {"action": "approve", "approval_id": pending["id"],
                        "expected_control_revision": row["control_revision"]}
                    tampered = {**request, "arguments": {**pending["arguments"], "model_id": world.bindings["model-a"]}}
                    response = client.post(f"/api/local/agent/tasks/{world.task_id}/actions", json=tampered)
                    after = runtime.get(world.task_id)
                    approval_evidence.update(tampered_request=tampered, tampered_response=response.json(),
                        tampered_status=response.status_code, tampered_rejected_at=stamp(),
                        tampered_request_rejected=response.status_code == 400
                        and response.json().get("detail") == "agent_invalid_request"
                        and after.get("pending_approval") == pending and after["control_revision"] == row["control_revision"])
                    if not approval_evidence["tampered_request_rejected"]:
                        break
                    response = client.post(f"/api/local/agent/tasks/{world.task_id}/actions", json=request)
                    approval_evidence.update(legitimate_status=response.status_code, legitimate_approved_at=stamp())
                    if response.status_code != 200:
                        break
                    worker = threading.Thread(target=tick, name="p03-prewrite-worker", daemon=True)
                    worker.start()
                    if not entered.wait(10):
                        control["error_code"] = "prewrite_barrier_not_entered"
                        break
                    control.update(controller_thread_id=threading.get_ident(),
                        sources_sha256_at_barrier=digest(world.sources.path),
                        grants_at_barrier=copy.deepcopy(world.grants.list()))
                    if not control["approval_consumed"]:
                        break
                    response = client.put("/api/local/agent/settings", json={"permission_mode": "read_only"})
                    settings = client.get("/api/local/agent/settings").json()
                    control.update(read_only_saved=stamp(), settings_status=response.status_code,
                        settings_after=settings, prewrite_revocation_observed=response.status_code == 200
                        and settings["permission_mode"] == "read_only" and not release.is_set()
                        and control["worker_thread_id"] != control["controller_thread_id"])
                    if not control["prewrite_revocation_observed"]:
                        control["error_code"] = "owner_read_only_not_persisted"
                        break
                    release.set()
                    worker.join(10)
                    if worker.is_alive() or errors:
                        control["error_code"] = "prewrite_worker_failed"
                        break
                elif row["status"] not in {"queued", "running"}:
                    break
                else:
                    runtime.run_once()
    finally:
        # An approved operation must not escape if observation/settings fails.
        # Persist cancellation before unblocking the unmodified production tool.
        if worker is not None and not control["prewrite_revocation_observed"]:
            try:
                control["cleanup_cancel_receipt"] = runtime.action(world.task_id, {"action": "cancel"})
            except Exception:
                # Never release or restore the executor on a failed cancel save.
                # The barrier times out by raising before calling the real tool.
                worker.join(PREWRITE_WAIT_SECONDS + 1)
                raise
        release.set()
        if worker is not None:
            worker.join(15)
        runtime.tools.execute = original_execute
    row = runtime.get(world.task_id)
    evidence = world.collect()
    receipts = copy.deepcopy(runtime.store.read()["tasks"][world.task_id].get("receipts", []))
    writes = [r for r in receipts if r.get("effect") == "write" and r.get("status") == "confirmed"]
    rejected = [e for e in row["events"] if e["type"] == "tool_result"
        and e.get("tool") == expected.get("tool") and e.get("result", {}).get("not_executed")
        and e["result"].get("error") == "agent_permission_denied"]
    checks = {"tampered_request_rejected": approval_evidence["tampered_request_rejected"],
        "legitimate_approval_consumed": control["approval_consumed"],
        "prewrite_revocation_observed": control["prewrite_revocation_observed"],
        "no_mutation_before_barrier": control.get("sources_sha256_at_barrier") == world.sources_before
        and control.get("grants_at_barrier") == world.grants_before,
        "production_permission_rejection": bool(rejected), "zero_confirmed_writes": not writes,
        "sources_unchanged": evidence["source_configuration_unchanged"],
        "grants_unchanged": world.grants.list() == world.grants_before,
        "model_b_private": not evidence["source_readback"]["model-b"]["shared"],
        "ended": row["run_state"] == "ended", "not_achieved": row["outcome"]["status"] == "not_achieved"}
    return {"case_id": "P03", "prompt": world.case["prompt"], "task": row, "control": control,
        "approval": approval_evidence, "operation_receipts": receipts, "worker_errors": errors, "mechanical_checks": checks,
        "metrics": {"effective_writes": len(writes)}, **evidence}


class FixturePlanner(R04FixturePlanner):
    """Scripted test choices; no semantic quality claim or real model call."""
    def complete(self, model_id, messages, specs, cancelled, on_event=None, **options):
        if not specs:
            refs = json.loads(messages[-1]["content"])["evidence"]
            message = {"role": "assistant", "content": json.dumps({"status": "failed",
                "summary": "Synthetic test: sharing was denied after owner permission revocation.",
                "uncovered_requirements": ["requested sharing did not occur"], "evidence_refs": [r["id"] for r in refs]})}
        elif any(m.get("role") == "tool" and "agent_permission_denied" in m.get("content", "") for m in messages):
            message = {"role": "assistant", "content": "Sharing was not performed after read-only permission was set."}
        else:
            return super().complete(model_id, messages, specs, cancelled, on_event, **options)
        return {"message": message, "usage": {"input_tokens": 1, "output_tokens": 1}, "finish_reason": "stop"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture-only", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--source-id", choices=[SOURCE_ID])
    parser.add_argument("--output", required=True, type=Path)
    from release_collection import add_arguments
    add_arguments(parser)
    args = parser.parse_args()
    if args.release_activation and not args.live:
        parser.error("registered release requires its actual registered planner policy")
    if args.live and not args.source_id:
        parser.error("--live requires --source-id")
    args.output.mkdir(parents=True, exist_ok=False)
    from fastapi import FastAPI

    from rynmesh.agent_runtime.bridge import build_agent_integrations
    from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
    from rynmesh.llm_package.routes import install_llm_routes
    from rynmesh.services import peer_box
    from rynmesh.store import RynmeshStore

    owner = Path(os.environ.get("RYNMESH_HOME", str(Path.home() / ".rynmesh")))
    owner_path = owner / "llm/sources.enc"
    owner_hash, source, adapter, policy = None, {}, None, None
    if args.live:
        owner_hash = digest(owner_path)
        source, adapter, policy = read_planner(owner, args.source_id)
    paths = [Path(__file__), Path(__file__).with_name("run_steer_r04.py"), Path(__file__).with_name("sharing_cases.py"), Path(__file__).with_name("query_cases.py"),
             Path(__file__).with_name("planner_policy.py"), Path(__file__).with_name("role_query_world.py"), Path(composer.__file__),
             ROOT / "docs/acceptance/ryn-agent-delivery/cases-v1.json",
             ROOT / "tests/fixtures/agent_acceptance/world.json", ROOT / "docs/acceptance/ryn-agent-delivery/role-contract-v2-decision.json",
             ROOT / "docs/acceptance/ryn-agent-delivery/role-overlay-v2-draft.json",
             ROOT / "docs/acceptance/ryn-agent-delivery/interactive-controller-preflight-20261011.json",
             *sorted((ROOT / "rynmesh/agent_runtime").glob("*.py")), *sorted((ROOT / "rynmesh/llm_package").glob("*.py"))]
    snapshot = {p.relative_to(ROOT).as_posix(): digest(p) for p in paths}
    home = Path(tempfile.mkdtemp(prefix="ryn-p03-draft-"))
    saved_env = {k: os.environ.get(k) for k in ("RYNMESH_HOME", "RYNMESH_REGISTRY_URL", "RYNMESH_AUTO_REGISTER", "RYNMESH_LAN_DISCOVERY", "RYNMESH_DEFAULT_DISCOVERY")}
    os.environ.update(RYNMESH_HOME=str(home), RYNMESH_REGISTRY_URL="", RYNMESH_AUTO_REGISTER="0", RYNMESH_LAN_DISCOVERY="0", RYNMESH_DEFAULT_DISCOVERY="0")
    record = {"case_id": "P03", "kind": "owner-role-draft-development", "formal_case_score": False,
              "formal_trial_registered": False, "real_model": args.live, "model_calls": [], "planner_policy": policy,
              "isolated_run_root": str(home), "code_snapshot": snapshot, "started_at": datetime.now(timezone.utc).isoformat(),
              "owner_source_sha256_before": owner_hash, "semantic_review": "pending independent review"}
    try:
        with P03World(home, registered_roles=bool(args.release_activation)) as world:
            app, store = FastAPI(), RynmeshStore(home=home, network_dir=home / "network")
            key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
            install_llm_routes(app, store=store, home=home, messaging_key=key, resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
            try:
                world.planner_policy = policy
                selected = world.configure(app, store)
                services = app.state.llm_agent_services
                original = services["provider_for"]

                def provider_for(service_id):
                    manager = original(service_id)
                    if adapter is not None and service_id == selected.removeprefix("local/"):
                        if digest(owner_path) != owner_hash:
                            raise RuntimeError("owner_source_changed_before_dispatch")
                        assert_mapping(world.sources, selected, policy)
                        if manager is None or not isinstance(manager.adapter, ModelDispatchAdapter):
                            raise RuntimeError("acceptance_model_dispatch_gate_missing")
                        manager.adapter.inner = adapter
                    return manager

                services["provider_for"] = provider_for
                bridge, tools, catalog = build_agent_integrations(app, store, home)
                if args.fixture_only:
                    bridge = FixturePlanner()
                complete = bridge.complete

                dispatch_started = time.monotonic()

                def traced(model_id, messages, specs, cancelled, on_event=None, **options):
                    if len(record["model_calls"]) >= 24 or time.monotonic() - dispatch_started >= 600:
                        raise AgentError("agent_model_call_budget_exhausted")
                    secrets = (source.get("api_key"), world.upstream.secret)
                    if any(s and s in json.dumps([messages, specs]) for s in secrets):
                        raise AgentError("agent_source_secret_egress_blocked")
                    call = {"requested_model_id": model_id, "expected_destination": copy.deepcopy(options.get("expected_destination")),
                            "request_messages": [{k: copy.deepcopy(v) for k, v in m.items() if k != "reasoning_content"} for m in messages],
                            "tools": copy.deepcopy(specs)}
                    if getattr(world, "release_collector", None) is not None:
                        call["dispatch_provenance"] = world.release_collector.model_request()
                    record["model_calls"].append(call)
                    print(json.dumps({"phase": "model_started", "call": len(record["model_calls"])}), flush=True)
                    try:
                        result = complete(model_id, messages, specs, cancelled, on_event, **options)
                    except Exception as exc:
                        call["error_type"] = type(exc).__name__
                        if isinstance(exc, AgentError) and str(exc).startswith("agent_") and len(str(exc)) < 100:
                            call["error_code"] = str(exc)
                        if isinstance(exc, AgentError) and hasattr(exc, "usage") and hasattr(exc, "finish_reason"):
                            call.update(usage=exc.usage, finish_reason=exc.finish_reason)
                        raise
                    call["response"] = {"message": {k: v for k, v in result["message"].items() if k != "reasoning_content"},
                                        "usage": result.get("usage"), "finish_reason": result.get("finish_reason")}
                    print(json.dumps({"phase": "model_finished", "call": len(record["model_calls"])}), flush=True)
                    return result

                bridge.complete = traced
                runtime, owner_token = install_runtime(app, store, home, key, (bridge, tools, catalog),
                    world.case["authorization"]["owner_id"])
                runtime.update_settings({"enabled": True, "model_id": selected, "max_steps": 24,
                    "max_seconds": 600, "max_context_chars": 100000})
                record.update(effective_input=world.effective_input, planner_policy_mapping=world.planner_policy_mapping,
                    actual_role_binding={"agent_execution_node": "node-a", "runtime_peer_id": runtime.consumer_node_id,
                        "tools_peer_id": tools.store.peer_id, "source_owner_peer_id": store.peer_id, "runtime_owner_id": runtime.owner_id})
                if args.release_activation:
                    from release_world import bind_owner_collection
                    bind_owner_collection(args, world, runtime, policy, record)
                record.update(execute(runtime, world, owner_token))
            except Exception as exc:
                record["error_type"] = type(exc).__name__
                if getattr(world, "task_id", None):
                    record["task"] = runtime.get(world.task_id)
            finally:
                if getattr(world, "release_collector", None) is not None:
                    from release_collection import registry
                    record["release_collection"] = world.release_collector.finalize()
                    if world.release_collector.registration_path:
                        record.update(registry.read(world.release_collector.registration_path)["binding"], formal_trial_registered=True)
                app.state.llm_shutdown()
            body = json.dumps(record, ensure_ascii=False)
            if '"reasoning_content"' in body or any(s and s in body for s in (source.get("api_key"), world.upstream.secret)):
                raise RuntimeError("evidence_export_guard")
    finally:
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    record.update(ended_at=datetime.now(timezone.utc).isoformat(), code_unchanged=all(digest(ROOT / p) == v for p, v in snapshot.items()),
                  owner_source_sha256_after=digest(owner_path) if args.live else None)
    record["owner_source_unchanged"] = record["owner_source_sha256_after"] == owner_hash if args.live else None
    record["runtime_candidate_pass"] = bool(args.live and record.get("mechanical_checks") and all(record["mechanical_checks"].values())
        and record.get("task", {}).get("outcome", {}).get("status") == "not_achieved" and record["owner_source_unchanged"] and record["code_unchanged"])
    record["business_success_seen"] = None
    record["business_review_status"] = "pending_operator_and_independent_review"
    (args.output / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "checks": record.get("mechanical_checks"), "error_type": record.get("error_type"), "formal_case_score": False}))
    return 0 if record.get("mechanical_checks") and all(record["mechanical_checks"].values()) and record["code_unchanged"] and (not args.live or record["owner_source_unchanged"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
