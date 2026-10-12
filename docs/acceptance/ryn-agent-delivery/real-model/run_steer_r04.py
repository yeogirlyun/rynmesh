"""Small R04 owner-role draft runner; fixture checks never read owner secrets.

Live execution is explicit and uses only the configured GLM transport inside
the existing production dispatch gate. No role decision or formal trial changes.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from planner_policy import SOURCE_ID, assert_mapping, read_planner  # noqa: E402
from role_query_world import composer  # noqa: E402
from sharing_cases import SharingWorld  # noqa: E402

from rynmesh.agent_runtime.store import AgentError  # noqa: E402


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class R04World(SharingWorld):
    def __init__(self, home, *, registered_roles=False):
        super().__init__(home, "R02")  # Same unchanged base world; no recovery execution.
        self.registered_roles = registered_roles
        self.instantiated_nodes = {}
        self.effective_input = composer.load_effective_case("R04")
        self.case = copy.deepcopy(self.effective_input["effective_case"])

    def registry_store(self, label, store):
        actual = store if label == "node-a" else super().registry_store(label, store)
        self.instantiated_nodes[label] = actual
        return actual

    def configure(self, app, store):
        selected = super().configure(app, store)
        self.instantiated_nodes["node-b"] = self.receiver_store
        if self.registered_roles and self.other_store.peer_id != self.instantiated_nodes["node-c"].peer_id:
            raise ValueError("release_sharing_other_identity_mismatch")
        # Setup only: retain the base world's existing model-a/node-b grant.
        self.grants.set(selected.removeprefix("local/"), self.rid, allowed=True, expected_revision=0)
        self.initial_sources = copy.deepcopy(self.sources.read())
        self.sources_before = digest(self.sources.path)
        self.grants_before = copy.deepcopy(self.grants.list())
        return selected


def execute(runtime, world):
    world.attach(runtime.tools)
    created = runtime.create({"message": world.case["prompt"], "model_id": world.selected})
    world.task_id = created["id"]
    if getattr(world, "on_task_created", None):
        world.on_task_created(created)
    correction = world.case["interventions"][0]["message"]
    control = {"write_proposed_not_dispatched": False, "stale_approvals_executable": None,
               "latest_goal_applied": False}
    after_events = None
    for _ in range(160):
        row = runtime.get(created["id"])
        if row["status"] == "waiting_approval":
            approval = row["pending_approval"]
            if after_events is not None or not world.permitted(approval):
                control["error_code"] = "unexpected_write_approval"
                break  # Preserve the unexpected proposal; never approve/coaching-retry.
            control.update(write_proposed_not_dispatched=True, original_approval=copy.deepcopy(approval),
                           before_goal_revision=row["goal"]["revision"],
                           effective_writes_at_barrier=sum(e["type"] == "tool_started" and e.get("effect") == "write" for e in row["events"]))
            changed = runtime.add_message(row["id"], {"message": correction, "intent": "steer",
                "expected_control_revision": row["control_revision"], "expected_goal_revision": row["goal"]["revision"],
                "client_request_id": str(uuid.uuid4())})
            after_events = len(changed["events"])
            control.update(correction=correction, after_goal_revision=changed["goal"]["revision"],
                           stale_approval_request_control_revision=changed["control_revision"])
            try:
                runtime.action(row["id"], {"action": "approve", "approval_id": approval["id"],
                    "expected_control_revision": changed["control_revision"]})
            except AgentError as exc:
                control.update(stale_approvals_executable=False, stale_approval_error=str(exc))
            else:
                control["stale_approvals_executable"] = True
                break
        elif row["status"] not in {"queued", "running"}:
            break
        else:
            runtime.run_once()
    row = runtime.get(created["id"])
    evidence = world.collect()
    writes = [e for e in row["events"] if e["type"] == "tool_started" and e.get("effect") == "write"]
    post_reads = [e["tool"] for e in row["events"][after_events or len(row["events"]):]
                  if e["type"] == "tool_started" and e.get("effect") == "read"]
    control["latest_goal_applied"] = bool(after_events is not None and row["goal"]["revision"] == control["after_goal_revision"]
        and any(m.get("role") == "user" and m.get("content") == correction for m in row["messages"]))
    checks = {"barrier_observed": control["write_proposed_not_dispatched"],
              "stale_approval_rejected": control["stale_approvals_executable"] is False,
              "zero_write_dispatches": not writes,
              "model_b_private": not evidence["source_readback"]["model-b"]["shared"],
              "sources_unchanged": evidence["source_configuration_unchanged"],
              "grants_unchanged": world.grants.list() == world.grants_before,
              "latest_goal_applied": control["latest_goal_applied"],
              "state_read_after_steer": bool(set(post_reads) & {"ryn_list_models", "ryn_inspect_services"}),
              "completed": row["status"] == "completed"}
    return {"case_id": "R04", "prompt": world.case["prompt"], "task": row, "control": control,
            "mechanical_checks": checks, "metrics": {"effective_writes": len(writes)}, **evidence}


class FixturePlanner:
    """Scripted controller exercise; its checker refuses semantic verification."""
    supports_destination_binding = True

    def complete(self, model_id, messages, specs, cancelled, on_event=None, **options):
        if not specs:
            evidence = json.loads(messages[-1]["content"])["evidence"]
            message = {"role": "assistant", "content": json.dumps({"status": "unknown",
                "summary": "Synthetic R04 control check only.", "uncovered_requirements": ["real semantic review"],
                "evidence_refs": [r["id"] for r in evidence]})}
        else:
            correction = "不要分享，只检查 model-b 的状态。"
            steered = any(m.get("role") == "user" and m.get("content") == correction for m in messages)
            calls = {c["id"]: c["function"]["name"] for m in messages for c in m.get("tool_calls", [])}
            receipts = {calls.get(m.get("tool_call_id")): json.loads(m["content"]) for m in messages if m["role"] == "tool"}
            name, args = None, {}
            if steered:
                if "ryn_inspect_services" not in receipts:
                    name = "ryn_inspect_services"
            elif "ryn_list_models" not in receipts:
                name = "ryn_list_models"
            elif "ryn_list_devices" not in receipts:
                name = "ryn_list_devices"
            else:
                model = next(m for m in receipts["ryn_list_models"]["models"] if m.get("fixture_identity") == "model-b")
                friend = next(f for f in receipts["ryn_list_devices"]["friends"] if f["node_name"] == "node-b")
                name, args = "ryn_share_model_with_friend", {"model_id": model["id"], "relationship_id": friend["relationship_id"]}
            message = {"role": "assistant", "content": "Synthetic state inspection only."}
            if name:
                message.update(content=None, tool_calls=[{"id": uuid.uuid4().hex, "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}])
        return {"message": message, "usage": {"input_tokens": 1, "output_tokens": 1},
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}


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
    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.store import AgentStore
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
    paths = [Path(__file__), Path(__file__).with_name("sharing_cases.py"), Path(__file__).with_name("query_cases.py"),
             Path(__file__).with_name("planner_policy.py"), Path(__file__).with_name("role_query_world.py"), Path(composer.__file__),
             ROOT / "docs/acceptance/ryn-agent-delivery/cases-v1.json",
             ROOT / "tests/fixtures/agent_acceptance/world.json", ROOT / "docs/acceptance/ryn-agent-delivery/role-contract-v2-decision.json",
             ROOT / "docs/acceptance/ryn-agent-delivery/role-overlay-v2-draft.json",
             *sorted((ROOT / "rynmesh/agent_runtime").glob("*.py")), *sorted((ROOT / "rynmesh/llm_package").glob("*.py"))]
    snapshot = {p.relative_to(ROOT).as_posix(): digest(p) for p in paths}
    home = Path(tempfile.mkdtemp(prefix="ryn-r04-draft-"))
    saved_env = {k: os.environ.get(k) for k in ("RYNMESH_HOME", "RYNMESH_REGISTRY_URL", "RYNMESH_AUTO_REGISTER", "RYNMESH_LAN_DISCOVERY", "RYNMESH_DEFAULT_DISCOVERY")}
    os.environ.update(RYNMESH_HOME=str(home), RYNMESH_REGISTRY_URL="", RYNMESH_AUTO_REGISTER="0", RYNMESH_LAN_DISCOVERY="0", RYNMESH_DEFAULT_DISCOVERY="0")
    record = {"case_id": "R04", "kind": "owner-role-draft-development", "formal_case_score": False,
              "formal_trial_registered": False, "real_model": args.live, "model_calls": [], "planner_policy": policy,
              "isolated_run_root": str(home), "code_snapshot": snapshot, "started_at": datetime.now(timezone.utc).isoformat(),
              "owner_source_sha256_before": owner_hash, "semantic_review": "pending independent review"}
    try:
        with R04World(home, registered_roles=bool(args.release_activation)) as world:
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

                def traced(model_id, messages, specs, cancelled, on_event=None, **options):
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
                runtime = AgentRuntime(AgentStore(home / "agent", key), bridge, tools, catalog,
                    consumer_node_id=store.peer_id, owner_id=world.case["authorization"]["owner_id"])
                app.state.agent_runtime = runtime
                runtime.update_settings({"enabled": True, "model_id": selected, "max_steps": 24,
                    "max_seconds": 600, "max_context_chars": 100000})
                record.update(effective_input=world.effective_input, planner_policy_mapping=world.planner_policy_mapping,
                    actual_role_binding={"agent_execution_node": "node-a", "runtime_peer_id": runtime.consumer_node_id,
                        "tools_peer_id": tools.store.peer_id, "source_owner_peer_id": store.peer_id, "runtime_owner_id": runtime.owner_id})
                if args.release_activation:
                    from release_world import bind_owner_collection
                    bind_owner_collection(args, world, runtime, policy, record)
                record.update(execute(runtime, world))
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
        and record.get("task", {}).get("outcome", {}).get("status") == "verified" and record["owner_source_unchanged"] and record["code_unchanged"])
    record["business_success_seen"] = None
    record["business_review_status"] = "pending_operator_and_independent_review"
    (args.output / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "checks": record.get("mechanical_checks"), "error_type": record.get("error_type"), "formal_case_score": False}))
    return 0 if record.get("mechanical_checks") and all(record["mechanical_checks"].values()) and record["code_unchanged"] and (not args.live or record["owner_source_unchanged"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
