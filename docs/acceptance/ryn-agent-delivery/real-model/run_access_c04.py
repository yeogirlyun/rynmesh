"""One C04 owner-role draft attempt; fixture mode never loads owner credentials.

Original task and revoke follow-up are unchanged. Signed receiver probes use
the same GLM dispatch path in live mode, including the total call/time cap.
One-host fixture evidence never establishes physical-host or formal success.
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

from planner_policy import SOURCE_ID, assert_mapping, read_planner  # noqa: E402
from role_query_world import composer  # noqa: E402
from run_steer_r04 import digest  # noqa: E402
from sharing_cases import SharingWorld  # noqa: E402

from rynmesh.agent_runtime.store import AgentError  # noqa: E402
from rynmesh.llm_package.adapters import RUNTIME_ERROR_CODES, AdapterError  # noqa: E402


class C04World(SharingWorld):
    def __init__(self, home, *, registered_roles=False):
        super().__init__(home, "R02")
        self.registered_roles = registered_roles
        self.instantiated_nodes = {}
        self.effective_input = composer.load_effective_case("C04")
        self.case = copy.deepcopy(self.effective_input["effective_case"])
        self.control, self.stages = {}, []
        self.real_dispatches = None

    def registry_store(self, label, store):
        if self.registered_roles:
            from release_world import ReleaseWorld
            return ReleaseWorld.registry_store(self, label, store)
        if label == "node-a":
            return store
        if label == "node-c":
            from rynmesh.store import RynmeshStore
            return RynmeshStore(home=self.home / "unauthorized-receiver", network_dir=self.home / "network")
        return super().registry_store(label, store)

    def configure(self, app, store):
        selected = super().configure(app, store)
        if self.registered_roles:
            self.instantiated_nodes["node-b"] = self.receiver_store
            if self.other_store.peer_id != self.instantiated_nodes["node-c"].peer_id:
                raise ValueError("release_sharing_other_identity_mismatch")
        self.sid = self.bindings["model-a"].removeprefix("local/")
        # Fixture setup realizes the frozen preexisting A -> B recipient list.
        # It is not a model-performed first grant or an approval observation.
        record = self.sources.read()[self.bindings["source-alpha"]]
        record["model_settings"]["alpha"]["overrides"]["share_scope"] = "friends"
        self.sources.save(record)
        self.grants.set(self.sid, self.rid, allowed=True, expected_revision=0)
        assert self.bindings["node-c"] == self.other.peer_id
        self.initial_sources = copy.deepcopy(self.sources.read())
        self.sources_before = digest(self.sources.path)
        self.grants_before = copy.deepcopy(self.grants.list())
        return selected

    def permitted(self, approval, *, revoke):
        args, preview = approval["arguments"], approval["preview"]
        grant = self.grants.grant(self.sid, self.rid)
        if not grant:
            return False
        if approval["tool"] == "ryn_set_friend_model_access":
            return (args == {"service_id": self.sid, "relationship_id": self.rid,
                            "allowed": not revoke, "expected_revision": grant["revision"]}
                    and preview.get("service_id") == self.sid
                    and preview.get("relationship_id") == self.rid
                    and preview.get("peer_id") == self.receiver.peer_id
                    and preview.get("before") == grant
                    and preview.get("after") == {"allowed": not revoke})
        if not revoke and approval["tool"] == "ryn_share_model_with_friend":
            recipients = preview.get("after", {}).get("recipients", [])
            return (args == {"model_id": self.bindings["model-a"], "relationship_id": self.rid}
                    and preview.get("model_id") == self.bindings["model-a"]
                    and preview.get("service_id") == self.sid
                    and preview.get("after", {}).get("sharing") is True
                    and preview.get("after", {}).get("scope") == "friends"
                    and len(recipients) == 1
                    and recipients[0].get("peer_id") == self.receiver.peer_id
                    and recipients[0].get("relationship_id") == self.rid)
        return False

    def probe_receiver(self, logical, sender, row=None):
        before = len(self.real_dispatches) if self.real_dispatches is not None else None
        result = super().probe_receiver(logical, sender, row)
        result["loopback_http_requests"] = result["upstream_requests"]
        if before is not None:
            result["upstream_requests"] = len(self.real_dispatches) - before
            result["upstream_evidence_basis"] = "counted_actual_GLM_adapter_dispatches"
        else:
            result["upstream_evidence_basis"] = "loopback_synthetic_HTTP_audit"
        return result

    def collect(self):
        evidence = super().collect()
        evidence["fixture_source_http_audit"] = evidence.pop("actual_source_http_audit")
        # SharingWorld's sibling check targets model-b; C04 instead targets A.
        evidence.pop("sibling_and_source_configuration_unchanged", None)
        current = self.sources.read()
        ignored = {"source_revision", "_friend_grants", "_sharing_operations"}
        unchanged = all({k: v for k, v in before.items() if k not in ignored} ==
                        {k: v for k, v in current.get(sid, {}).items() if k not in ignored}
                        for sid, before in self.initial_sources.items())
        evidence.update(source_and_model_settings_unchanged=unchanged,
                        friend_grants_before=copy.deepcopy(self.grants_before),
                        fixture_setup="Frozen preexisting A->B grant and friends recipient scope seeded before task creation; no first grant mutation fabricated.")
        evidence["node_c_identity_binding"] = {"logical_node": "node-c", "peer_id": self.other.peer_id,
            "registry_peer_id": self.bindings["node-c"], "separately_signed": True,
            "physical_hosts": 1, "independent_identity_proof": False}
        return evidence


class CountedAdapter:
    """Bound all live chats, including the allowed receiver probe, before IO."""
    def __init__(self, inner, world, record, limit):
        self.inner, self.world, self.record, self.limit = inner, world, record, limit

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def chat(self, body, **options):
        remaining = self.world.deadline - time.monotonic()
        if len(self.record["real_model_dispatches"]) >= self.limit or remaining <= 0:
            raise AdapterError("c04_development_attempt_budget_exhausted", code="inference_timeout")
        entry = {"phase": self.world.phase, "at": datetime.now(timezone.utc).isoformat(),
                 "request_sha256": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
                 "request_body": {**copy.deepcopy(body), "messages": [
                     {k: copy.deepcopy(v) for k, v in m.items() if k != "reasoning_content"}
                     for m in body.get("messages", [])]},
                 "max_tokens": body.get("max_tokens"), "source_id": SOURCE_ID}
        self.record["real_model_dispatches"].append(entry)
        options["timeout_s"] = min(options["timeout_s"], remaining)
        try:
            response = self.inner.chat(body, **options)
        except Exception as exc:
            entry["error_type"] = type(exc).__name__
            if isinstance(exc, AdapterError):
                if exc.code in RUNTIME_ERROR_CODES:
                    entry["error_code"] = exc.code
                usage = exc.reported_usage if isinstance(exc.reported_usage, dict) else {}
                entry["usage"] = {name: usage.get(name) if type(usage.get(name)) is int and usage[name] >= 0 else None
                                  for name in ("input_tokens", "output_tokens")}
                if exc.finish_reason in {"stop", "length", "tool_calls", "content_filter", "cancelled", "unknown"}:
                    entry["finish_reason"] = exc.finish_reason
            raise
        entry.update(usage=response.get("usage"), finish_reason=response.get("finish_reason"))
        return response

    def infer(self, **options):
        raise AdapterError("c04_requires_audited_chat_path")

    def infer_stream(self, **options):
        raise AdapterError("c04_requires_audited_chat_path")


def execute(runtime, world):
    world.attach(runtime.tools)
    world.deadline, world.phase = time.monotonic() + 600, "initial_task"
    row = runtime.create({"message": world.case["prompt"], "model_id": world.selected})
    world.task_id = row["id"]
    if getattr(world, "on_task_created", None):
        world.on_task_created(row)
    settings_before = runtime.settings()
    control = world.control
    control.update(grant_barrier_observed=False, revoke_barrier_observed=False,
                   initial_grant_preexisting=True, probes_before_followup=False)
    initial_steps = 0

    def drain(revoke):
        for _ in range(160):
            task = runtime.get(world.task_id)
            if time.monotonic() >= world.deadline or initial_steps + task["steps"] >= 24:
                control["stop_reason"] = "attempt_time_or_step_limit"
                return task
            if task["status"] == "waiting_approval":
                approval = task["pending_approval"]
                permitted = world.permitted(approval, revoke=revoke)
                world.approvals.append({"phase": world.phase, "approved": permitted,
                                        "approval": copy.deepcopy(approval)})
                if not permitted:
                    control["stop_reason"] = "unexpected_write_approval"
                    return task  # Keep proposal; no denied-tool feedback/coaching retry.
                flag = "revoke_barrier_observed" if revoke else "grant_barrier_observed"
                if control[flag]:
                    control["stop_reason"] = "repeated_write_proposal"
                    return task
                control[flag] = True
                runtime.action(task["id"], {"action": "approve", "approval_id": approval["id"],
                    "expected_control_revision": task["control_revision"]})
            elif task["status"] not in {"queued", "running"}:
                return task
            else:
                runtime.run_once()
        control["stop_reason"] = "controller_observation_limit"
        return runtime.get(world.task_id)

    row = drain(False)
    world.stages.append({"phase": "initial_task", "task": copy.deepcopy(row), "evidence": world.collect()})
    probes = {"before_revoke": {}, "after_revoke": {}}
    if row["status"] == "completed" and not control.get("stop_reason"):
        world.phase = "before_revoke_probes"
        rows = world.receiver_catalog.refresh(world.store.peer_id)["services"]
        observed = next((r for r in rows if r["service"]["package_id"] == world.sid), None)
        before_b = world.probe_receiver("model-a", world.receiver, observed)
        before_c = world.probe_receiver("model-a", world.other)
        probes["before_revoke"] = {"node-b": before_b, "node-c": before_c}
        control["probes_before_followup"] = True
        world.stages.append({"phase": world.phase, "evidence": world.collect(), "probes": copy.deepcopy(probes["before_revoke"])})
        if before_b["result"] == "allowed" and before_c["result"] == "denied" and not before_c["upstream_requests"]:
            initial_steps = row["steps"]
            correction = world.case["interventions"][1]["message"]
            control["followup"] = {"message": correction, "expected_revision": row["revision"],
                                   "client_request_id": str(uuid.uuid4())}
            runtime.add_message(row["id"], control["followup"])
            world.phase = "revoke_task"
            row = drain(True)
            world.stages.append({"phase": world.phase, "task": copy.deepcopy(row), "evidence": world.collect()})
            if row["status"] == "completed" and not control.get("stop_reason"):
                world.phase = "after_revoke_probe"
                probes["after_revoke"]["node-b"] = world.probe_receiver("model-a", world.receiver)
                world.stages.append({"phase": world.phase, "evidence": world.collect(), "probes": copy.deepcopy(probes["after_revoke"])})
        else:
            control["stop_reason"] = "before_revoke_probe_failed_or_unknown"
    evidence = world.collect()
    a, b = evidence["source_readback"]["model-a"], evidence["source_readback"]["model-b"]
    def other_grants(rows):
        return [g for g in rows if (g["service_id"], g["relationship_id"]) != (world.sid, world.rid)]
    after_b = probes["after_revoke"].get("node-b", {})
    checks = {"initial_grant_preexisting": any(g["service_id"] == world.sid and g["allowed"] for g in world.grants_before),
        "grant_barrier_observed": control["grant_barrier_observed"],
        "revoke_barrier_observed": control["revoke_barrier_observed"],
        "probes_before_followup": control["probes_before_followup"],
        "before_b_allowed": probes["before_revoke"].get("node-b", {}).get("result") == "allowed",
        "before_c_denied_without_upstream": probes["before_revoke"].get("node-c", {}).get("result") == "denied" and
            probes["before_revoke"].get("node-c", {}).get("upstream_requests") == 0,
        "after_b_denied_without_upstream": after_b.get("result") == "denied" and after_b.get("upstream_requests") == 0,
        "model_a_still_shared_exact_friends_scope": a["shared"] and a["share_scope"] == "friends",
        "model_a_receivers_empty": a["receivers"] == [], "model_b_private": not b["shared"],
        "source_and_model_settings_unchanged": evidence["source_and_model_settings_unchanged"],
        "other_grants_unchanged": other_grants(world.grants.list()) == other_grants(world.grants_before),
        "agent_settings_unchanged": runtime.settings() == settings_before,
        "completed": row["status"] == "completed", "no_controller_stop": not control.get("stop_reason")}
    return {"case_id": "C04", "prompt": world.case["prompt"], "task": row, "control": control,
            "stages": world.stages, "probes": probes, "mechanical_checks": checks,
            "known_contract_conflict": "Preexisting grant may need no grant proposal; missing frozen pending-grant barrier remains false, never fabricated.",
            **evidence}


class FixturePlanner:
    """No initial grant fabrication; only exercise original follow-up/revoke."""
    supports_destination_binding = True

    def complete(self, model_id, messages, specs, cancelled, on_event=None, **options):
        if not specs:
            refs = json.loads(messages[-1]["content"])["evidence"]
            content = json.dumps({"status": "unknown", "summary": "Synthetic C04 control only.",
                "uncovered_requirements": ["real model, physical hosts and independent semantic review"],
                "evidence_refs": [r["id"] for r in refs]})
            message = {"role": "assistant", "content": content}
        else:
            correction = composer.load_effective_case("C04")["effective_case"]["interventions"][1]["message"]
            revoke = any(m.get("role") == "user" and m.get("content") == correction for m in messages)
            names = {c["id"]: c["function"]["name"] for m in messages for c in m.get("tool_calls", [])}
            receipts = {names.get(m.get("tool_call_id")): json.loads(m["content"]) for m in messages if m["role"] == "tool"}
            name, args = None, {}
            for tool in ("ryn_list_models", "ryn_list_devices", "ryn_inspect_services"):
                if tool not in receipts:
                    name = tool
                    break
            if name is None and revoke and "ryn_set_friend_model_access" not in receipts:
                model = next(m for m in receipts["ryn_list_models"]["models"] if m.get("fixture_identity") == "model-a")
                grant = next(g for g in receipts["ryn_inspect_services"]["friend_grants"] if g["service_id"] == model["id"].removeprefix("local/"))
                name, args = "ryn_set_friend_model_access", {k: grant[k] for k in ("service_id", "relationship_id")}
                args.update(allowed=False, expected_revision=grant["revision"])
            message = {"role": "assistant", "content": "Synthetic controller observation only; no business pass."}
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
    parser.add_argument("--max-model-calls", type=int, choices=range(1, 25), default=24,
                        help="Attempt-wide real GLM cap, including receiver probes; never more than 24")
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
    paths = [Path(__file__), Path(__file__).with_name("run_steer_r04.py"),
             Path(__file__).with_name("sharing_cases.py"), Path(__file__).with_name("query_cases.py"),
             Path(__file__).with_name("planner_policy.py"), Path(__file__).with_name("role_query_world.py"), Path(composer.__file__),
             ROOT / "docs/acceptance/ryn-agent-delivery/cases-v1.json",
             ROOT / "tests/fixtures/agent_acceptance/world.json", ROOT / "docs/acceptance/ryn-agent-delivery/role-contract-v2-decision.json",
             ROOT / "docs/acceptance/ryn-agent-delivery/role-overlay-v2-draft.json",
             *sorted((ROOT / "rynmesh/agent_runtime").glob("*.py")), *sorted((ROOT / "rynmesh/llm_package").glob("*.py"))]
    snapshot = {p.relative_to(ROOT).as_posix(): digest(p) for p in paths}
    home = Path(tempfile.mkdtemp(prefix="ryn-c04-draft-"))
    saved_env = {k: os.environ.get(k) for k in ("RYNMESH_HOME", "RYNMESH_REGISTRY_URL", "RYNMESH_AUTO_REGISTER", "RYNMESH_LAN_DISCOVERY", "RYNMESH_DEFAULT_DISCOVERY")}
    os.environ.update(RYNMESH_HOME=str(home), RYNMESH_REGISTRY_URL="", RYNMESH_AUTO_REGISTER="0", RYNMESH_LAN_DISCOVERY="0", RYNMESH_DEFAULT_DISCOVERY="0")
    record = {"case_id": "C04", "kind": "owner-role-draft-development", "formal_case_score": False,
              "formal_trial_registered": False, "executed_trials": 0, "real_model": args.live,
              "model_calls": [], "real_model_dispatches": [], "max_real_model_calls": args.max_model_calls,
              "attempt_limits": {"max_seconds": 600, "max_total_steps": 24, "max_new_tasks": 1, "max_original_revoke_followups": 1, "retries": 0}, "planner_policy": policy,
              "isolated_run_root": str(home), "code_snapshot": snapshot, "started_at": datetime.now(timezone.utc).isoformat(),
              "owner_source_sha256_before": owner_hash, "semantic_review": "pending independent review"}
    try:
        with C04World(home, registered_roles=bool(args.release_activation)) as world:
            app, store = FastAPI(), RynmeshStore(home=home, network_dir=home / "network")
            key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
            install_llm_routes(app, store=store, home=home, messaging_key=key, resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
            try:
                world.planner_policy = policy
                selected = world.configure(app, store)
                if adapter is not None:
                    world.real_dispatches = record["real_model_dispatches"]
                    adapter = CountedAdapter(adapter, world, record, args.max_model_calls)
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
                record.update(control=world.control, stages=world.stages)
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
    record["real_model_call_count"] = len(record["real_model_dispatches"])
    record["cost"] = {"amount": None, "currency": None, "status": "unknown_without_authoritative_billing"}
    record["probe_transport"] = "actual_configured_GLM_via_production_signed_admission" if args.live else "loopback_synthetic_HTTP"
    record["fixture_controller_checks_pass"] = bool(args.fixture_only and record.get("mechanical_checks") and
        all(v for k, v in record["mechanical_checks"].items() if k != "grant_barrier_observed") and
        not record["mechanical_checks"]["grant_barrier_observed"] and record["code_unchanged"])
    record["business_success_seen"] = None
    record["business_review_status"] = "pending_operator_and_independent_review"
    (args.output / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "checks": record.get("mechanical_checks"), "error_type": record.get("error_type"), "formal_case_score": False}))
    return 0 if record["fixture_controller_checks_pass"] or record.get("mechanical_checks") and all(record["mechanical_checks"].values()) and record["code_unchanged"] and (not args.live or record["owner_source_unchanged"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
