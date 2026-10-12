"""Opt-in Q01-Q03/Q06 and C03 development collection; no coaching or formal scores.

Fixture mode never reads owner credentials. Live mode reads one existing owner
source without migration; only the isolated model-a dispatcher's inner transport
uses that immutable adapter. The ModelDispatchAdapter gate remains installed.
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
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from planner_policy import assert_mapping, read_planner  # noqa: E402
from query_cases import SUPPORTED, QueryWorld  # noqa: E402
from sharing_cases import SharingFixturePlanner, SharingWorld  # noqa: E402


class FixturePlanner:
    """Deterministic collector exercise, never evidence of Agent effectiveness."""
    def __init__(self, case_id, cohort="ryn"):
        self.case_id, self.cohort = case_id, cohort

    def complete(self, model_id, messages, specs, cancelled, on_event=None, **options):
        if not specs:
            if self.cohort == "reference":
                assert "Verify whether the active session goal" in messages[-1]["content"]
                text = json.dumps({"passed": True, "reason": "Synthetic collector protocol only; not a quality verdict.", "nextAction": ""})
            else:
                evidence = json.loads(messages[-1]["content"])["evidence"]
                text = json.dumps({"status": "unknown", "summary": "Fixture collector only; no independent semantic assessment.",
                    "uncovered_requirements": ["independent report review"], "evidence_refs": [row["id"] for row in evidence]})
            message = {"role": "assistant", "content": text}
        elif self.case_id == "Q06" and len([m for m in messages if m["role"] == "tool"]) < 3:
            # Synthetic transport exercise only: real model receives no script.
            index = len([m for m in messages if m["role"] == "tool"])
            name = ("ryn_list_models", "ryn_list_devices", "ryn_inspect_services")[index]
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "fixture-read-" + str(index), "type": "function",
                "function": {"name": name, "arguments": "{}"}}]}
        elif not any(message["role"] == "tool" for message in messages):
            name = "ryn_list_devices" if self.case_id == "Q03" else "ryn_list_models"
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "fixture-read", "type": "function",
                "function": {"name": name, "arguments": "{}"}}]}
        else:
            message = {"role": "assistant", "content": "Collector exercise only. Evidence is in the actual tool receipt; report review remains pending."}
        return {"message": message, "usage": {"input_tokens": None, "output_tokens": None}, "finish_reason": "stop"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, choices=sorted(SUPPORTED | {"C03"}))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture-only", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--source-id")
    parser.add_argument("--cohort", choices=("ryn", "reference"), default="ryn")
    parser.add_argument("--node", type=Path)
    parser.add_argument("--upstream", type=Path)
    parser.add_argument("--role-contract-v2-draft", action="store_true",
                        help="Explicit Q01/Q06 owner-admin draft only; no formal trials or v1 normalization.")
    from release_collection import add_arguments
    add_arguments(parser)
    args = parser.parse_args()
    role_mode = args.role_contract_v2_draft or (args.release_activation is not None and args.case in {"Q01", "Q06"})
    if args.role_contract_v2_draft and args.release_activation:
        parser.error("draft and registered release collection are mutually exclusive")
    if role_mode and args.case not in {"Q01", "Q06"}:
        parser.error("owner-role collection currently supports Q01/Q06; other controllers remain pending")
    if args.release_activation and not args.live:
        parser.error("registered release requires its real registered planner policy")
    if args.live and not args.source_id:
        parser.error("--live requires --source-id")
    if args.cohort == "reference" and (not args.node or not args.upstream):
        parser.error("reference requires --node and --upstream")
    from fastapi import FastAPI

    from rynmesh.agent_runtime.bridge import build_agent_integrations
    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.store import AgentStore
    from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
    from rynmesh.llm_package.routes import install_llm_routes
    from rynmesh.services import peer_box
    from rynmesh.store import RynmeshStore

    owner_path, owner_hash, source, adapter = None, None, {}, None
    planner_policy = None
    if args.live:
        owner_home = Path(os.environ.get("RYNMESH_HOME", str(Path.home() / ".rynmesh")))
        owner_path = owner_home / "llm/sources.enc"
        owner_hash = hashlib.sha256(owner_path.read_bytes()).hexdigest()
        source, adapter, planner_policy = read_planner(owner_home, args.source_id)
    home = Path(tempfile.mkdtemp(prefix="ryn-query-development-"))
    os.environ.update(RYNMESH_HOME=str(home), RYNMESH_REGISTRY_URL="", RYNMESH_AUTO_REGISTER="0", RYNMESH_LAN_DISCOVERY="0", RYNMESH_DEFAULT_DISCOVERY="0")
    output = Path(__file__).parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + args.case + "-" + args.cohort
                                    + ("-v2-draft" if args.role_contract_v2_draft else "") + ("" if args.live else "-fixture"))
    output.mkdir()
    record = {"case_id": args.case, "kind": "real-planner-fixed-query-world" if args.live else "local-query-collector-check",
              "cohort": args.cohort, "real_model": args.live, "formal_case_score": False, "model_calls": [], "approvals": [],
              "planner_model": planner_policy["model_id"] if planner_policy else "fixture", "planner_policy": planner_policy,
              "provider": source.get("provider", "fixture"), "isolated_run_root": str(home),
              "owner_source_sha256_before": owner_hash, "owner_source_sha256_after": None,
              "owner_source_after_read_status": "pending" if args.live else "not_applicable",
              "owner_source_hash_basis": "sources.enc_ciphertext_bytes" if args.live else "not_applicable",
              "transport_binding_note": "The fixed model-a business identity dispatches to the separately recorded planner_model; sibling model rows are controlled fixtures, not additional real model providers."}
    if role_mode:
        from role_query_world import DraftDispatchCollector, RoleQueryWorld, composer
    if args.role_contract_v2_draft:
        record.update(kind="real-role-query-v2-draft" if args.live else "local-role-query-v2-draft",
                      dataset_version=composer.DATASET_VERSION, collector_profile="owner-admin-query-v2-draft.1",
                      role_contract_status="draft/not_formal_ready", formal_trial_registered=False,
                      run_id=uuid.uuid4().hex, run_nonce=uuid.uuid4().hex)
    (output / "run.json").write_text(json.dumps({"case_id": args.case, "formal_case_score": False,
        "collector_state": "initializing", **{name: value for name, value in record.items() if name.startswith("owner_source_")}}) + "\n", encoding="utf-8")
    paths = [*sorted((ROOT / "rynmesh/agent_runtime").glob("*.py")), *sorted((ROOT / "rynmesh/llm_package").glob("*.py")),
             Path(__file__), Path(__file__).with_name("query_cases.py"), Path(__file__).with_name("sharing_cases.py"), Path(__file__).with_name("planner_policy.py"),
             Path(__file__).with_name("access_query.py"),
             Path(__file__).with_name("query_consumer.py"),
             *sorted((ROOT / "rynmesh/ai_access").glob("*.py")), *sorted((ROOT / "rynmesh/friends").glob("*.py")),
             ROOT / "tests/fixtures/agent_acceptance/source_fixture.py",
             ROOT / "tests/fixtures/agent_acceptance/world.json", ROOT / "docs/acceptance/ryn-agent-delivery/cases-v1.json"]
    if args.role_contract_v2_draft:
        paths += [Path(__file__).with_name("role_query_world.py"), ROOT / "tests/fixtures/agent_acceptance/role_contract_v2.py",
                  ROOT / "tests/fixtures/agent_acceptance/role_grade_v2.py",
                  ROOT / "docs/acceptance/ryn-agent-delivery/role-run-v2.schema.json",
                  ROOT / "docs/acceptance/ryn-agent-delivery/role-contract-v2-decision.json",
                  ROOT / "docs/acceptance/ryn-agent-delivery/role-overlay-v2-draft.json",
                  ROOT / "docs/acceptance/ryn-agent-delivery/role-overlay-v2.schema.json"]
    if args.cohort == "reference":
        reference_path = Path(__file__).parent.parent / "reference"
        paths += [*sorted(reference_path.glob("*.py")), *sorted(reference_path.glob("*.mjs"))]
    record["code_snapshot"] = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    started = time.monotonic()
    record["started_at"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps({"output": str(output), "case_id": args.case, "planner_model": record["planner_model"]}), flush=True)
    dispatch_collector = DraftDispatchCollector(output, record) if args.role_contract_v2_draft else None
    world_type = RoleQueryWorld if role_mode else SharingWorld if args.case == "C03" else QueryWorld
    if args.release_activation and args.case == "C03":
        from release_world import ReleaseSharingWorld
        world_type = ReleaseSharingWorld
    elif args.release_activation and args.case in {"Q02", "Q03"}:
        from release_world import ReleaseQueryWorld
        world_type = ReleaseQueryWorld
    with world_type(home, args.case) as controller:
        app = FastAPI()
        store = RynmeshStore(home=home, network_dir=home / "network")
        record["consumer_peer_id"] = store.peer_id
        key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
        install_llm_routes(app, store=store, home=home, messaging_key=key, resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
        runtime, task = None, None
        try:
            controller.planner_policy = planner_policy
            selected = controller.configure(app, store)
            provider_selected = controller.bindings["model-a"] if args.case == "Q06" or (args.release_activation and args.case in {"Q02", "Q03"}) else selected
            record["planner_policy_mapping"] = controller.planner_policy_mapping
            services = app.state.llm_agent_services
            original = services["provider_for"]
            def provider_for(service_id):
                manager = original(service_id)
                if adapter is not None and service_id == provider_selected.removeprefix("local/"):
                    try:
                        current_owner_hash = hashlib.sha256(owner_path.read_bytes()).hexdigest()
                    except OSError:
                        raise RuntimeError("owner_source_unavailable_before_dispatch") from None
                    if current_owner_hash != owner_hash:
                        raise RuntimeError("owner_source_changed_before_dispatch")
                    assert_mapping(controller.sources, provider_selected, planner_policy)
                    if manager is None or not isinstance(manager.adapter, ModelDispatchAdapter):
                        raise RuntimeError("acceptance_model_dispatch_gate_missing")
                    manager.adapter.inner = adapter
                elif args.fixture_only and (args.case == "Q06" or role_mode) and service_id == provider_selected.removeprefix("local/"):
                    from query_consumer import ScriptedProvider
                    if manager is None or not isinstance(manager.adapter, ModelDispatchAdapter):
                        raise RuntimeError("acceptance_model_dispatch_gate_missing")
                    manager.adapter.inner = ScriptedProvider(FixturePlanner(args.case, args.cohort), selected)
                return manager
            services["provider_for"] = provider_for
            execution_store, execution_home, execution_key = store, home, key
            execution_app = app
            if (args.case == "Q06" and not role_mode) or (args.release_activation and args.case in {"Q02", "Q03"}):
                consumer = controller.access_query.consumer
                bridge, tools, catalog = consumer.bridge, consumer.tools, consumer.models
                execution_store, execution_home = consumer.store, consumer.home
                execution_key = controller.access_query.receiver.messaging_private
                execution_app = consumer.app
                record["consumer_peer_id"] = execution_store.peer_id
            else:
                bridge, tools, catalog = build_agent_integrations(app, store, home)
            if args.fixture_only and args.case != "Q06" and not role_mode:
                bridge = SharingFixturePlanner(args.cohort) if args.case == "C03" else FixturePlanner(args.case, args.cohort)
            original_complete = bridge.complete
            def traced(model_id, messages, specs, cancelled, on_event=None, **options):
                from rynmesh.agent_runtime.output_policy import select
                from rynmesh.agent_runtime.store import AgentError
                if any(secret and secret in json.dumps([messages, specs]) for secret in (source.get("api_key"), controller.upstream.secret)):
                    raise AgentError("agent_source_secret_egress_blocked")
                clean = [{k: copy.deepcopy(v) for k, v in message.items() if k != "reasoning_content"} for message in messages]
                call = {"request_messages": clean, "tools": copy.deepcopy(specs), "requested_model_id": model_id,
                        "expected_destination": copy.deepcopy(options.get("expected_destination")),
                        "credential_scan": "complete_in_memory_requests", "credential_egress_detected": False}
                if args.live:
                    call["output_policy"] = select(catalog.resolve(model_id), messages, specs, request_cap=options.get("max_output_tokens"))
                if dispatch_collector is not None:
                    call["dispatch_provenance"] = dispatch_collector.model_request()
                record["model_calls"].append(call)
                number = len(record["model_calls"])
                begin = time.monotonic()
                print(json.dumps({"phase": "model_started", "call": number, "has_tools": bool(specs)}), flush=True)
                try:
                    result = original_complete(model_id, messages, specs, cancelled, on_event, **options)
                    if result.get("output_policy"):
                        call["output_policy"] = result["output_policy"]
                    call["response"] = {"message": {k: v for k, v in result["message"].items() if k != "reasoning_content"},
                        "usage": result.get("usage"), "finish_reason": result.get("finish_reason", "unknown")}
                    return result
                except Exception as exc:
                    call["error_type"] = type(exc).__name__
                    if isinstance(exc, AgentError):
                        call["error_code"] = str(exc) if str(exc).startswith("agent_") and len(str(exc)) < 100 else "agent_error"
                        if hasattr(exc, "usage") and hasattr(exc, "finish_reason"):
                            call.update(usage=exc.usage, finish_reason=exc.finish_reason)
                    elif isinstance(exc, RuntimeError) and str(exc) in {
                            "owner_source_changed_before_dispatch", "owner_source_unavailable_before_dispatch"}:
                        call["error_code"] = str(exc)
                    raise
                finally:
                    call["elapsed_seconds"] = time.monotonic() - begin
                    print(json.dumps({"phase": "model_finished", "call": number,
                        "elapsed_seconds": round(call["elapsed_seconds"], 3), "error": call.get("error_type")}), flush=True)
            bridge.complete = traced
            runtime = AgentRuntime(AgentStore(execution_home / "agent", execution_key), bridge, tools, catalog,
                                   consumer_node_id=execution_store.peer_id,
                                   **({"owner_id": controller.case["authorization"]["owner_id"]} if role_mode or args.release_activation else {}))
            execution_app.state.agent_runtime = runtime
            runtime.update_settings({"enabled": True, "model_id": controller.bindings["model-c"] if args.case == "C03" else selected,
                                     "max_steps": 24, "max_seconds": 600, "max_context_chars": 100000})
            if args.case == "Q06" or role_mode or (args.release_activation and args.case in {"Q02", "Q03"}):
                runtime.update_settings({"permission_mode": "read_only"})
            if args.role_contract_v2_draft:
                def register_draft_task(created_task):
                    dispatch_collector.register(controller, runtime, created_task)
                    (output / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                controller.on_task_created = register_draft_task
            if args.release_activation:
                def register_release_task(created_task):
                    nonlocal dispatch_collector
                    from release_collection import ReleaseCollection, registry
                    nodes = dict(controller.instantiated_nodes)
                    if controller.access_query:
                        nodes["node-b"] = controller.access_query.receiver_store
                    dispatch_collector = ReleaseCollection(args.release_activation, output, args.case, args.cohort, args.trial,
                        runtime=runtime, task_id=created_task["id"], node_stores=nodes,
                        source_stores={"node-a": controller.sources,
                            **({"node-b": controller.access_query.consumer.sources} if controller.access_query else {})}, connectors={}, history_stores={},
                        bindings=controller.bindings, planner_policy=planner_policy,
                        native=({"home": execution_home, "node": args.node, "upstream": args.upstream,
                                 "audit_path": output / "native-rpc-events.jsonl"} if args.cohort == "reference" else None))
                    if args.cohort == "ryn":
                        path = dispatch_collector.register()
                        dispatch_collector.attach_candidate()
                        if args.case == "C03":
                            from release_world import bind_receiver_dispatch
                            bind_receiver_dispatch(controller, dispatch_collector)
                        record.update(registry.read(path)["binding"], formal_trial_registered=True)
                    else:
                        record.update(dataset_version=registry.VERSION, formal_trial_registered=False)
                controller.on_task_created = register_release_task
            if args.cohort == "ryn":
                record.update(controller.execute(runtime))
            else:
                sys.path.insert(0, str(reference_path))
                from documents import execute
                controller.attach(tools)
                settings_before = runtime.settings()
                task = (controller.create_task(runtime) if role_mode else
                        runtime.create({"message": controller.case["prompt"], "model_id": selected}))
                if args.release_activation and not role_mode:
                    register_release_task(task)
                record["prompt"] = controller.case["prompt"]
                controller.task_id = task["id"]
                def approve_reference(request):
                    approval = {**request, "tool": request["name"]}
                    allowed = controller.permitted(approval)
                    controller.approvals.append({"approved": allowed, "approval": copy.deepcopy(approval)})
                    return allowed
                execute(runtime=runtime, tools=tools, catalog=catalog, selected=selected, task=task, bridge=bridge,
                        record=record, home=execution_home, node=args.node, upstream=args.upstream,
                        allow_write=approve_reference if args.case == "C03" else lambda _: False, real_model=args.live,
                        native_business_operations=args.case == "C03",
                        **({"dispatch_observer": dispatch_collector if args.release_activation else dispatch_collector.reference_dispatch,
                            "dispatch_provenance": dispatch_collector.native_config()} if dispatch_collector is not None else {}))
                record.update(controller.collect_postconditions(runtime, task["id"], settings_before)
                              if args.case == "C03" else controller.collect())
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            if isinstance(exc, RuntimeError) and str(exc) in {
                    "owner_source_changed_before_dispatch", "owner_source_unavailable_before_dispatch"}:
                record["error_code"] = str(exc)
            if runtime is not None and getattr(controller, "task_id", None):
                record["task"] = runtime.get(controller.task_id)
            if hasattr(controller, "sources_before"):
                record.update(controller.collect())
        finally:
            if task is not None:
                # This is the accounting host, never a Ryn executor result.
                record["reference_accounting_task"] = runtime.get(task["id"])
                record["reference_final_answer"] = record.get("reference", {}).get("result", {}).get("response")
            app.state.llm_shutdown()
        owner_after_hash, owner_after_status = None, "not_applicable"
        owner_source_unchanged = owner_path is None
        if owner_path is not None:
            try:
                owner_after_hash = hashlib.sha256(owner_path.read_bytes()).hexdigest()
                owner_after_status = "read"
                owner_source_unchanged = owner_after_hash == owner_hash
            except OSError:
                owner_after_status = "unknown"
                owner_source_unchanged = False
        record.update(elapsed_seconds=time.monotonic() - started,
            ended_at=datetime.now(timezone.utc).isoformat(),
            owner_source_unchanged=owner_source_unchanged,
            owner_source_sha256_after=owner_after_hash, owner_source_after_read_status=owner_after_status,
            code_unchanged=all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest for name, digest in record["code_snapshot"].items()))
        if args.role_contract_v2_draft:
            if (output / "effective-input.json").exists():
                record["effective_input_file_sha256"] = hashlib.sha256((output / "effective-input.json").read_bytes()).hexdigest()
            native_session = execution_home / "reference-session.sqlite"
            dispatch_collector.finalize(native_session if args.cohort == "reference" else None)
            record["role_execution_evidence"] = {
                "runtime_role": "reference_accounting_and_tool_permission_host" if args.cohort == "reference" else "ryn_agent_executor",
                "tool_gateway_peer_id": execution_store.peer_id,
                "task_context_basis": "A runtime constructs the task-scoped tool gateway context from its actual task readback.",
                "reference_native_session": ({"path": str(native_session), "exists": native_session.exists(),
                    "sha256": hashlib.sha256(native_session.read_bytes()).hexdigest() if native_session.exists() else None,
                    "proof_scope": "Pinned reference worker persists its own SQLite session; queued Ryn accounting task is not evidence that a Ryn planner executed."}
                    if args.cohort == "reference" else None)}
        if args.release_activation and dispatch_collector is not None:
            from release_collection import registry
            record["release_collection"] = dispatch_collector.finalize()
            if dispatch_collector.registration_path:
                record.update(registry.read(dispatch_collector.registration_path)["binding"], formal_trial_registered=True)
        text = json.dumps(record, ensure_ascii=False, indent=2)
        if '"reasoning_content"' in text or any(secret and secret in text for secret in (source.get("api_key"), controller.upstream.secret)):
            text = json.dumps({"case_id": args.case, "formal_case_score": False, "evidence_export_blocked": True,
                               "error_code": "evidence_export_guard"})
        (output / "run.json").write_text(text + "\n", encoding="utf-8")
        print(json.dumps({"output": str(output), "fixture_only": args.fixture_only, "formal_case_score": False}), flush=True)


if __name__ == "__main__":
    main()
