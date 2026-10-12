"""Opt-in C01 real planner trial against a controlled loopback dual-model source.

No original owner source is edited. The new fixture credential is supplied only
through the owner HTTP form after a credential question. No tool coaching. The
controlled source is not an independent second AI source or a formal score.
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
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from planner_policy import SOURCE_ID, assert_mapping, map_fixture, read_planner  # noqa: E402


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def approve_new_source(approval, *, endpoint, existing_targets):
    preview = approval.get("preview", {})
    config = preview.get("configuration", {})
    return (approval.get("tool") == "ryn_commit_source_draft" and not preview.get("source_id") and not existing_targets
            and config.get("name") == "source-new" and config.get("models") == ["alpha", "beta"]
            and config.get("base_url") == endpoint and config.get("kind") == "local"
            and preview.get("test", {}).get("chat_passed") is True)


def export_record(output, record, *, secrets):
    """Never publish polluted evidence; retain an explicit failed-run receipt."""
    body = json.dumps(record, ensure_ascii=False, indent=2)
    blocked = '"reasoning_content"' in body or any(value and value in body for value in secrets)
    if blocked:
        # No model/provider-controlled strings belong in this fallback.
        safe = {"kind": "source-configuration-export-blocked", "case_id": record.get("case_id", "C01"), "formal_case_score": False,
                "evidence_export_blocked": True, "error_code": "evidence_export_guard",
                "model_call_count": len(record.get("model_calls", [])),
                "approval_count": len(record.get("approvals", [])), "semantic_review": "not reviewable; evidence blocked"}
        body = json.dumps(safe, indent=2)
    (output / "run.json").write_text(body + "\n", encoding="utf-8")
    return not blocked


class FixturePlanner:
    """Harness check only; fixed choices must never count as model effectiveness."""
    def __init__(self, endpoint):
        self.endpoint, self.count = endpoint, 0

    def complete(self, model_id, messages, specs, cancelled, on_event=None, **options):
        message = {"role": "assistant", "content": "Both models tested and saved private."}
        if not specs:
            data = json.loads(messages[-1]["content"])
            message["content"] = json.dumps({"status": "passed", "summary": "Two tested private models.",
                "uncovered_requirements": [], "evidence_refs": [e["id"] for e in data["evidence"]]})
        else:
            self.count += 1
            name, args = None, {}
            if self.count == 1:
                name, args = "ryn_request_input", {"goal_revision": 1, "kind": "credential_ref", "prompt": "Use secure local form.",
                    "fields": [{"id": "source", "label": "Source credential", "type": "credential_ref", "required": True}]}
            elif self.count == 2:
                name, args = "ryn_prepare_source_draft", {"configuration": {"name": "source-new", "kind": "local", "base_url": self.endpoint, "models": ["alpha", "beta"]}}
            elif self.count in {3, 4}:
                draft = json.loads(messages[-1]["content"])
                name = "ryn_test_source_draft" if self.count == 3 else "ryn_commit_source_draft"
                args = {"draft_id": draft["draft_id"], "expected_draft_version": draft["version"], **({"test_tools": True} if self.count == 3 else {})}
            if name:
                message.update(content=None, tool_calls=[{"id": "fixture-" + str(self.count), "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}])
        return {"message": message, "usage": {"input_tokens": 8, "output_tokens": 4}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--fixture-only", action="store_true")
    parser.add_argument("--source-id")
    parser.add_argument("--case", choices=("C01", "C02"), default="C01")
    parser.add_argument("--scenario", choices=("pre_save_failure", "post_save_conflict"), default="pre_save_failure")
    parser.add_argument("--cohort", choices=("ryn", "zcode"), default="ryn")
    parser.add_argument("--node", type=Path)
    parser.add_argument("--upstream", type=Path)
    from release_collection import add_arguments
    add_arguments(parser)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.live and not args.source_id:
        parser.error("--live requires --source-id")
    if args.live and args.source_id != SOURCE_ID:
        parser.error("only the configured GLM source is permitted")
    if args.cohort == "zcode" and (not args.node or not args.upstream):
        parser.error("zcode requires --node and --upstream")
    if args.release_activation and not args.live:
        parser.error("registered source collection requires its actual live policy")
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from rynmesh.agent_runtime.bridge import build_agent_integrations
    from rynmesh.agent_runtime.routes import install_agent
    from rynmesh.agent_runtime.store import AgentError, encoded
    from rynmesh.background_workers import BackgroundWorkerRegistry
    from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
    from rynmesh.llm_package.routes import install_llm_routes
    from rynmesh.services import peer_box
    from rynmesh.store import RynmeshStore

    owner_path, original_hash, adapter = None, None, None
    source = {"models": ["fixture"], "kind": "local", "provider": "custom", "base_url": "http://127.0.0.1:9/v1", "context_window": 100000}
    planner_policy = None
    if args.live:
        owner_home = Path(os.environ.get("RYNMESH_HOME", str(Path.home() / ".rynmesh")))
        owner_path = owner_home / "llm/sources.enc"
        original_hash = digest(owner_path)
        source, adapter, planner_policy = read_planner(owner_home, args.source_id)
    model_name = planner_policy["model_id"] if planner_policy else "fixture"
    case = next(c for c in json.loads((ROOT / "docs/acceptance/ryn-agent-delivery/cases-v1.json").read_text(encoding="utf-8"))["cases"] if c["id"] == args.case)
    if args.case == "C02":
        from source_update_case import SourceUpdateWorld, UpdateFixturePlanner, load_case
        case = load_case(args.scenario)
    suffix = args.case + ("-" + args.scenario if args.case == "C02" else "")
    output = args.output or Path(__file__).parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + suffix + "-" + args.cohort + ("" if args.live else "-fixture"))
    output.mkdir(parents=True)
    paths = [*sorted((ROOT / "rynmesh/agent_runtime").glob("*.py")), *sorted((ROOT / "rynmesh/llm_package").glob("*.py")),
             *sorted((ROOT / "rynmesh/ai_access").glob("*.py")), Path(__file__), ROOT / "tests/fixtures/agent_acceptance/source_fixture.py",
             ROOT / "docs/acceptance/ryn-agent-delivery/cases-v1.json", Path(__file__).with_name("source_update_case.py"), Path(__file__).with_name("planner_policy.py")]
    if args.cohort == "zcode":
        reference_path = Path(__file__).parent.parent / "reference"
        paths += [Path(__file__).with_name("source_reference.py"), *sorted(reference_path.glob("*.py")), *sorted(reference_path.glob("*.mjs"))]
    snapshot = {str(p.relative_to(ROOT)).replace("\\", "/"): digest(p) for p in paths}
    record = {"kind": "real-source-configuration-development-trial" if args.live else "local-source-configuration-harness-check", "case_id": args.case, "formal_case_score": False,
        "scenario": args.scenario if args.case == "C02" else None, "cohort": args.cohort, "real_model": args.live, "started_at": datetime.now(timezone.utc).isoformat(),
        "prompt": case["prompt"], "model": model_name, "provider": source.get("provider"), "code_snapshot": snapshot, "model_calls": [],
        "planner_policy": planner_policy, "planner_policy_mapping": [],
        "approvals": [], "owner_inputs": [], "semantic_review": "pending independent review"}
    (output / "run.json").write_text(json.dumps({"case_id": args.case, "formal_case_score": False,
                                                "collector_state": "initializing"}) + "\n", encoding="utf-8")
    home = Path(tempfile.mkdtemp(prefix="ryn-source-trial-"))
    record["isolated_run_root"] = str(home)
    os.environ.update(RYNMESH_HOME=str(home), RYNMESH_REGISTRY_URL="", RYNMESH_AUTO_REGISTER="0", RYNMESH_LAN_DISCOVERY="0", RYNMESH_DEFAULT_DISCOVERY="0")
    role_setup = ExitStack()
    roles = collector = None
    if args.release_activation:
        from release_world import ReleaseWorld
        roles = role_setup.enter_context(ReleaseWorld(home / "roles", args.case, planner_policy))
        home = roles.execution_store.home
        if args.case == "C02":
            # Editing source-alpha must not replace the planner being used to
            # inspect it. The original contract binds primary_model separately.
            planner = roles.sources.read()[roles.bindings["source-other"]]
            roles.planner_policy_mapping = [map_fixture(planner, "alpha", planner_policy)]
            roles.sources.save(planner)
            roles.selected = roles.provider_selected = roles.bindings["model-c"]
    app = roles.execution_app if roles else FastAPI()
    store = roles.execution_store if roles else RynmeshStore(home=home, network_dir=home / "network")
    record["consumer_peer_id"] = store.peer_id
    messaging = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    if not roles:
        install_llm_routes(app, store=store, home=home, messaging_key=messaging, resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    services = (roles.owner_app if roles else app).state.llm_agent_services
    sources = app.state.llm_agent_services["source_store"]
    planner_sources = roles.sources if roles else sources
    primary = None if roles else sources.prepare({"name": "Isolated planner", "kind": source.get("kind", "api"), "provider": source.get("provider", "custom"),
        "base_url": source["base_url"], "api_key": "in-memory-adapter-placeholder", "models": [model_name],
        "context_window": source["context_window"], "request_defaults": source.get("request_defaults", {})})
    if primary is not None:
        primary.update(status="ready", tool_support={model_name: "supported"})
        if planner_policy:
            record["planner_policy_mapping"].append(map_fixture(primary, model_name, planner_policy))
        primary = sources.save(primary)
    else:
        primary = planner_sources.read()[roles.bindings["source-other" if args.case == "C02" else "source-alpha"]]
        record["planner_policy_mapping"] = copy.deepcopy(roles.planner_policy_mapping)
    primary_before = encoded(planner_sources.read()[primary["id"]])
    selected = roles.selected if roles else "local/" + sources.model_service_id(primary, model_name)
    provider_selected = roles.provider_selected if roles else selected
    original_provider = services["provider_for"]
    def provider_for(service_id):
        manager = original_provider(service_id)
        if service_id == provider_selected.removeprefix("local/") and adapter is not None:
            if digest(owner_path) != original_hash:
                raise RuntimeError("owner_source_changed_before_dispatch")
            assert_mapping(planner_sources, provider_selected, planner_policy)
            if manager is None or not isinstance(manager.adapter, ModelDispatchAdapter):
                raise RuntimeError("acceptance_model_dispatch_gate_missing")
            manager.adapter.inner = adapter
        return manager
    services["provider_for"] = provider_for
    spec = importlib.util.spec_from_file_location("source_trial_fixture", ROOT / "tests/fixtures/agent_acceptance/source_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def target_rows():
        return [r for r in sources.read().values() if r["id"] != primary["id"]]
    runtime, task = None, None
    started = time.monotonic()
    print(json.dumps({"output": str(output), "case_id": args.case, "formal_case_score": False}), flush=True)
    with ExitStack() as stack:
        world = stack.enter_context(SourceUpdateWorld(sources, args.scenario,
            **({"initial_source": sources.read()[roles.bindings["source-alpha"]]} if roles else {}))) if args.case == "C02" else None
        upstream = world.candidate if world else stack.enter_context(module.FixtureSource(saved=lambda: bool(target_rows())))
        fixture_secrets = (upstream.secret, world.original.secret, world.newer.secret) if world else (upstream.secret,)
        try:
            bridge, tools, catalog = build_agent_integrations(app, store, home)
            if world:
                world.attach(tools)
            if args.fixture_only:
                if args.cohort == "zcode":
                    from source_reference import NativeSourceFixturePlanner
                    bridge = NativeSourceFixturePlanner(args.case)
                else:
                    bridge = UpdateFixturePlanner(world) if world else FixturePlanner(upstream.url)
            complete = bridge.complete
            def traced(model_id, messages, specs, cancelled, on_event=None, **options):
                safe = [{k: copy.deepcopy(v) for k, v in m.items() if k != "reasoning_content"} for m in messages]
                complete_request = json.dumps({"messages": messages, "tools": specs})
                if any(secret in complete_request for secret in fixture_secrets):
                    raise AgentError("agent_source_secret_egress_blocked")
                call = {"request_messages": safe, "tools": copy.deepcopy(specs), "requested_model_id": model_id,
                        "expected_destination": copy.deepcopy(options.get("expected_destination")),
                        "fixture_credential_egress_detected": False, "credential_scan": "complete_in_memory_requests"}
                if collector is not None:
                    call["dispatch_provenance"] = collector.model_request()
                record["model_calls"].append(call)
                begin = time.monotonic()
                try:
                    response = complete(model_id, messages, specs, cancelled, on_event, **options)
                    call["response"] = {"message": {k: v for k, v in response["message"].items() if k != "reasoning_content"},
                        "usage": response.get("usage"), "finish_reason": response.get("finish_reason", "unknown")}
                    return response
                except Exception as exc:
                    call["error_type"] = type(exc).__name__
                    if isinstance(exc, AgentError):
                        call["error_code"] = str(exc)
                    usage = getattr(exc, "usage", None)
                    if isinstance(usage, dict):
                        call["error_usage"] = {k: usage.get(k) if type(usage.get(k)) is int and usage[k] >= 0 else None
                                               for k in ("input_tokens", "output_tokens")}
                    finish = getattr(exc, "finish_reason", None)
                    if finish in {"stop", "length", "tool_calls", "content_filter", "cancelled"}:
                        call["error_finish_reason"] = finish
                    raise
                finally:
                    call["elapsed_seconds"] = time.monotonic() - begin
            bridge.complete = traced
            def owner(request):
                if request.headers.get("x-fixture-owner") != "yes":
                    raise HTTPException(401)
            runtime = install_agent(app, store=store, home=home, workers=BackgroundWorkerRegistry(), local_control=owner,
                messaging_key=messaging, integrations=(bridge, tools, catalog))
            if roles:
                # This fresh isolated runtime has no task yet. Its actual scope
                # authority is configured before any task or credential input.
                runtime.owner_id = roles.case["authorization"]["owner_id"]
            runtime.update_settings({"enabled": True, "model_id": selected, "max_steps": 24, "max_seconds": 600, "max_context_chars": 100000})
            task = runtime.create({"message": case["prompt"]})
            if roles:
                from release_collection import registry
                collector = roles.collector(args.release_activation, output, "reference" if args.cohort == "zcode" else "ryn",
                    args.trial, runtime, task, node=args.node, upstream=args.upstream,
                    subscenario=args.scenario if args.case == "C02" else None)
                if args.cohort == "ryn":
                    collector.register()
                    collector.attach_candidate()
            with TestClient(app) as client:
                def source_owner_input(row):
                    if row["pending_input"]["kind"] != "credential_ref" or record["owner_inputs"]:
                        raise AgentError("agent_input_mismatch")
                    response = client.post(f'/api/local/agent/tasks/{row["id"]}/source-credential', headers={"x-fixture-owner": "yes"}, json={
                        "expected_control_revision": row["control_revision"], "expected_goal_revision": row["goal"]["revision"],
                        "pending_input_id": row["pending_input"]["id"], "client_request_id": str(uuid.uuid4()),
                        "kind": "local", "base_url": upstream.url, "api_key": upstream.secret,
                        **({"source_id": world.source_id} if world else {})})
                    record["owner_inputs"].append({"kind": "secure_credential_form", "status_code": response.status_code, "grants_permission": False})
                    if response.status_code != 200:
                        raise AgentError("agent_input_mismatch")
                    return response.json()
                if args.cohort == "zcode":
                    sys.path.insert(0, str(reference_path))
                    from documents import execute
                    def native_approve(request):
                        approval = {**request, "tool": request["name"]}
                        return world.approve(approval) if world else approve_new_source(approval, endpoint=upstream.url, existing_targets=target_rows())
                    execute(runtime=runtime, tools=tools, catalog=catalog, selected=selected, task=task, bridge=bridge,
                            record=record, home=home, node=args.node, upstream=args.upstream,
                            allow_write=native_approve, real_model=args.live, native_business_operations=True,
                            source_owner_input=source_owner_input,
                            **({"dispatch_observer": collector, "dispatch_provenance": collector.native_config()} if collector else {}))
                for _ in range(160):
                    if args.cohort == "zcode":
                        break
                    row = runtime.get(task["id"])
                    if row["status"] == "waiting_input":
                        if row["pending_input"]["kind"] != "credential_ref" or record["owner_inputs"]:
                            break  # No extra hints, endpoints in chat, or tool coaching.
                        source_owner_input(row)
                    elif row["status"] == "waiting_approval":
                        approval = row["pending_approval"]
                        allowed = world.approve(approval) if world else approve_new_source(approval, endpoint=upstream.url, existing_targets=target_rows())
                        record["approvals"].append({"approved": allowed, "approval": approval})
                        runtime.action(row["id"], {"action": "approve" if allowed else "deny", "approval_id": approval["id"], "expected_control_revision": row["control_revision"]})
                    elif row["status"] not in {"queued", "running"}:
                        break
                    runtime.run_once()
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            if isinstance(exc, AgentError):
                record["error_code"] = str(exc)
        finally:
            if runtime and task:
                record["task"] = runtime.get(task["id"])
            rows = target_rows()
            record["upstream_audit"] = copy.deepcopy(upstream.audit)
            record["saved_sources"] = [{k: r[k] for k in ("id", "name", "models", "shared_models", "tool_support", "source_revision")} for r in rows]
            before_save = [a for a in upstream.audit if not a["saved"]]
            after_save = [a for a in upstream.audit if a["saved"]]
            record["checks"] = {"one_expected_source": len(rows) == 1 and rows[0]["name"] == "source-new",
                "two_private_models": len(rows) == 1 and rows[0]["models"] == ["alpha", "beta"] and rows[0]["shared_models"] == [],
                "tested_before_save": {a["model"] for a in before_save} == {"alpha", "beta"},
                "retested_after_save": {a["model"] for a in after_save} == {"alpha", "beta"},
                "primary_unchanged": encoded(planner_sources.read()[primary["id"]]) == primary_before,
                "one_secure_form": len(record["owner_inputs"]) == 1 and record["owner_inputs"][0]["status_code"] == 200}
            if world:
                actual = record["business_state"] = world.evidence()
                record["checks"] = {"primary_unchanged": encoded(planner_sources.read()[primary["id"]]) == primary_before,
                    "one_secure_form": len(record["owner_inputs"]) == 1 and record["owner_inputs"][0]["status_code"] == 200,
                    "actual_503_observed": any(a["status"] == 503 for a in actual["candidate_audit"]),
                    "sharing_unchanged": actual["shared_models"] == world.initial["shared_models"]}
                if args.scenario == "pre_save_failure":
                    record["checks"].update(original_unchanged=actual["original_unchanged"], zero_saves=actual["config_saves"] == 0)
                else:
                    record["checks"].update(owner_newer_preserved=actual["source_version"] == 9 and actual["endpoint"] == "endpoint-owner-newer",
                        exactly_one_agent_save=actual["config_saves"] == 1, owner_intervention_occurred=len(actual["owner_interventions"]) == 1)
            if collector is not None:
                record["release_collection"] = collector.finalize()
                if collector.registration_path:
                    record.update(registry.read(collector.registration_path)["binding"])
                record.update(bindings=copy.deepcopy(roles.bindings), isolated_run_root=str(home))
                for row in rows:
                    if row.get("name") == "source-new":
                        record["bindings"]["source-new"] = row["id"]
            app.state.llm_shutdown()
            role_setup.close()
        record.update(elapsed_seconds=time.monotonic() - started, owner_source_unchanged=owner_path is None or digest(owner_path) == original_hash,
                      ended_at=datetime.now(timezone.utc).isoformat(),
                      code_unchanged=all(digest(ROOT / p) == value for p, value in snapshot.items()))
        if not export_record(output, record, secrets=(source.get("api_key"), *fixture_secrets)):
            print(json.dumps({"error_code": "evidence_export_guard", "formal_case_score": False}), flush=True)
            return
        print(json.dumps({"checks": record["checks"], "status": record.get("task", {}).get("status"), "error_type": record.get("error_type")}), flush=True)


if __name__ == "__main__":
    main()
