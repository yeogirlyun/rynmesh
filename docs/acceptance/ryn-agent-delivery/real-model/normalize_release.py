"""Normalize only newly registered public trials; missing observations stay missing."""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "tests/fixtures/agent_acceptance"))
import release_registry as registry  # noqa: E402
from role_grade_v3 import ReleaseRoleGrader, registration  # noqa: E402


def normalize(source, output):
    source, output = Path(source), Path(output)
    raw = source.read_bytes()
    record = json.loads(raw)
    if "release_subscenarios" in record:
        return normalize_bundle(source, output)
    observed = record["release_collection"]
    registry._check_file(observed["registration"])
    registered, manifest, events = registration(observed["registration"]["path"], require_dispatch=True)
    bound = registered["binding"]
    if (record["case_id"] != bound["case_id"] or record["cohort"] != bound["cohort"]
            or record.get("run_id") != bound["run_id"] or record.get("run_nonce") != bound["run_nonce"]
            or observed["task_readback"]["id"] != registered["observation"]["task_id"]):
        raise ValueError("release_raw_registration_mismatch")
    if registry.read(manifest["artifacts"]["normalizer"]["path"])["implementation"] != registry.binding(__file__):
        raise ValueError("release_normalizer_not_activated")
    grader = ReleaseRoleGrader()
    envelope = grader.template_registered(observed["registration"]["path"])
    run = envelope["run"]
    case = grader.cases[bound["case_id"]]["effective_case"]
    scenario = registry.subscenarios(bound["case_id"]).get(bound.get("subscenario"), {})
    expected_prompt = scenario.get("prompt", case["prompt"])
    if bound["case_id"] == "P04":
        if (record["prompt"] != case["prompt"] or record.get("session_prompt") != expected_prompt
                or record.get("session_projection") != manifest["artifacts"]["projection:P04"]):
            raise ValueError("release_p04_session_projection_mismatch")
    elif record["prompt"] != expected_prompt:
        raise ValueError("release_frozen_prompt_mismatch")
    output.mkdir(parents=True, exist_ok=False)
    (output / "evidence").mkdir()

    def save(kind, value=None, sources=(), *, body=None):
        body = registry.canonical(value) if body is None else body
        import hashlib
        sha = hashlib.sha256(body).hexdigest()
        identity = kind + ":" + sha
        relative = f"evidence/{kind}-{sha}.json"
        (output / relative).write_bytes(body)
        entry = {"id": identity, "kind": kind, "path": relative, "sha256": sha,
                 "producer": "collector", "source_ids": list(sources)}
        if not any(e["id"] == identity for e in run["evidence"]):
            run["evidence"].append(entry)
        return identity

    raw_id = save("raw_trace", body=raw)
    registration_path = Path(observed["registration"]["path"])
    reg_id = save("raw_trace", body=registration_path.read_bytes())
    admission_ids = [save("raw_trace", event, [reg_id]) for event in events]
    binding_path = source.parent / "release-task-binding.json"
    task_binding_id = save("raw_trace", body=binding_path.read_bytes(), sources=[reg_id, raw_id])
    binding = registered["role_proof"]["binding"]

    def proof(kind, observation, sources):
        return save("raw_receipt" if kind == "storage" else "raw_trace",
                    {"role_proof": {"kind": kind, "binding": binding, "observations": observation}}, sources)

    auth = save("raw_trace", {"authenticated_runtime": observed["authenticated_runtime"]}, [raw_id])
    audit = save("raw_trace", {"dispatch_audit": observed["dispatch_audit"]}, [raw_id, *admission_ids])
    save("model_exchange", {"model_requests": observed["model_requests"], "calls": record.get("model_calls", [])}, [raw_id, audit])
    storage = {k: observed[k] for k in ("agent_store_path", "agent_store_encryption_public_key", "task_readback",
                                       "task_readback_sha256", "decrypted_from_store")}
    runtime = {**observed["runtime"], **{k: storage[k] for k in ("agent_store_path", "agent_store_encryption_public_key")},
               "authentication_source_ids": [auth], "dispatch_audit_source_ids": [audit]}
    if observed.get("reference_session_readback"):
        native = copy.deepcopy(observed["reference_session_readback"])
        registry._check_file({k: native[k] for k in ("path", "sha256")})
        native["artifact_evidence_id"] = save("artifact_readback", body=Path(native.pop("path")).read_bytes(), sources=[raw_id, audit])
        runtime["reference_execution_evidence_id"] = save("raw_receipt", {"reference_session_readback": native}, [raw_id, audit, native["artifact_evidence_id"]])
    envelope["role_evidence"].update(registration=reg_id, task_binding=task_binding_id,
        storage=proof("storage", storage, [raw_id]), runtime=proof("runtime", runtime, [raw_id, auth, audit]),
        deployment=proof("deployment", observed["deployment"], [raw_id]))
    # Explicit recorded fields only. The unchanged grader decides whether these
    # facts satisfy each original pointer; absent facts do not become successes.
    if isinstance(record.get("task"), dict):
        save("task_state", record["task"], [raw_id])
    trace = {k: copy.deepcopy(record[k]) for k in ("metrics", "control", "approval", "security", "isolation", "subscenarios") if k in record}
    if record.get("task", {}).get("events") is not None:
        task_events = record["task"]["events"]
        trace["events"] = task_events
        trace.setdefault("metrics", {}).setdefault("writes", sum(e.get("type") == "tool_started" and e.get("effect") == "write" for e in task_events))
    # The same concrete derivations used by the development normalizer: all
    # counts come from original events/receipts, never expected case values.
    events = record.get("task", {}).get("events", [])
    for key in ("http_audit", "setup_audit", "security_audit_coverage", "original_resource_changes",
                "http_health_audit", "setup_probes", "source_configuration_unchanged", "identity_mapping_policy",
                "access_query", "actual_source_http_audit", "consumer_peer_id", "transport_binding_note",
                "physical_hosts", "two_physical_nodes", "write_counting_scope", "consumer_role", "provider_peer_id",
                "consumer_local_sources", "initial_consumer_models", "current_consumer_models", "consumer_wire_audit",
                "catalog_refresh_errors", "transport_scope", "source_readback_scope", "agent_state_isolation",
                "before_approval", "terminated_consumer", "replacement_consumer_pid", "source_replacements"):
        if key in record:
            trace[key] = copy.deepcopy(record[key])
    if events:
        starts = {e.get("call_id") for e in events if e.get("type") == "tool_started" and e.get("effect") in {"read", "write"}}
        successful = {e.get("call_id") for e in events if e.get("type") == "tool_result" and e.get("call_id") in starts
                      and isinstance(e.get("result"), dict) and not e["result"].get("error") and e["result"].get("ok") is not False}
        trace.setdefault("metrics", {}).setdefault("meaningful_operations", len(successful))
        trace["metrics"].setdefault("resource_read_attempts", sum(e.get("type") == "tool_started"
            and e.get("tool") == "ryn_read_resource" for e in events))
    if "original_resource_changes" in record:
        trace.setdefault("security", {})["original_resource_changes"] = copy.deepcopy(record["original_resource_changes"])
    trace["dispatch_audit_source_ids"] = [audit]
    business = copy.deepcopy(record.get("business_state", {}))
    if "settings_after" in record:
        settings = record["settings_after"]
        defaults = [name for name, identity in record.get("bindings", {}).items()
                    if name.startswith("model-") and identity == settings.get("model_id")]
        business["agent"] = {"enabled": settings["enabled"]} if "enabled" in settings else {}
        if len(defaults) == 1:
            business["agent"]["default_model"] = defaults[0]
    if "source_readback" in record:
        business["models"] = {name: {target: row[source_key] for source_key, target in
            (("shared", "sharing"), ("receivers", "receivers"), ("share_scope", "share_scope")) if source_key in row}
            for name, row in record["source_readback"].items()}
    if "receiver_probes" in record:
        probes = {}
        receiver = record.get("bindings", {}).get("node-b")
        if "receiver_visible_models" in record:
            probes["receiver_visible_models"] = copy.deepcopy(record["receiver_visible_models"])
        for name in ("model-a", "model-b"):
            matches = [r for r in record["receiver_probes"] if receiver and r.get("receiver_peer_id") == receiver and r.get("model") == name]
            if len(matches) == 1 and "result" in matches[0]:
                probes[name] = {"receiver_inference": matches[0]["result"]}
        trace["probes"] = probes
    if record.get("case_id") == "C01" and "saved_sources" in record:
        matches = [r for r in record["saved_sources"] if r.get("name") == "source-new"]
        if len(matches) == 1:
            business.setdefault("sources", {})["source-new"] = copy.deepcopy(matches[0])
    save("operation_trace", trace, [raw_id, audit])
    if "source_readback" in record:
        business["source_readback"] = copy.deepcopy(record["source_readback"])
    artifacts = record.get("artifact_readback", record.get("artifacts"))
    if isinstance(artifacts, list):
        business["artifact_count"] = len(artifacts)
        business["artifact_metadata"] = [{k: v for k, v in a.items() if k != "content"} for a in artifacts]
        for artifact in artifacts:
            content = artifact.get("content", "").encode()
            import hashlib
            if hashlib.sha256(content).hexdigest() != artifact["sha256"] or len(content) != artifact["bytes"]:
                raise ValueError("release_artifact_readback_mismatch")
            matches = [r for r in record.get("http_audit", []) if r.get("method") == "GET" and r.get("status") == 200
                       and r.get("path") == artifact["path"] and r.get("body_sha256") == artifact["sha256"]
                       and r.get("bytes") == len(content) and r.get("mutation") is False and r.get("response_dropped") is False]
            connections = {r.get("connection") for r in matches}
            if len(connections) == 1 and None not in connections:
                receipt = save("raw_receipt", {"http_readbacks": matches}, [raw_id])
                artifact_id = save("artifact_readback", body=content, sources=[receipt])
                run["artifacts"].append({"connection_id": next(iter(connections)), "path": artifact["path"], "readback_evidence_id": artifact_id})
    if business:
        save("business_state", business, [raw_id])
    run["started_at"], run["ended_at"] = record["started_at"], record["ended_at"]
    run["code_revision"] = "release-manifest:" + bound["manifest_sha256"]
    calls = record.get("model_calls", [])
    versions = {match.group() for call in calls for message in call.get("request_messages", []) if message.get("role") == "system"
                for match in re.finditer(r"ryn-agent-control-v[0-9.]+", message.get("content", ""))}
    run["prompt_version"] = record.get("prompt_version") or (next(iter(versions)) if len(versions) == 1 else "")
    run["model"].update(id=record.get("planner_model", record.get("model", "")), provider=record.get("provider", ""),
                         kind="real" if record.get("real_model") is True else "synthetic",
                         adapter_version=record.get("adapter_version") or ("sha256:" + record["code_snapshot"]["rynmesh/llm_package/adapters.py"]
                             if record.get("code_snapshot", {}).get("rynmesh/llm_package/adapters.py") else ""))
    run["bindings"].update({k: v for k, v in record.get("bindings", {}).items() if isinstance(v, str)})
    run["bindings"].update(isolated_run_root=record.get("isolated_run_root", ""), primary_model=observed["task_readback"]["model_id"])
    def token_total(key):
        values = [(call.get("response", {}).get("usage") or call.get("usage") or call.get("error_usage") or {}).get(key) for call in calls]
        return sum(values) if values and all(type(v) is int and v >= 0 for v in values) else None
    run["measurements"].update(elapsed_seconds=record.get("elapsed_seconds"),
        input_tokens=token_total("input_tokens"), output_tokens=token_total("output_tokens"),
        explicit_approvals=sum(a.get("approved") is True for a in record.get("approvals", [])),
        unknown_reason="Only original registered observations are normalized; missing usage, review and dependency evidence remain unknown.")
    registry._new(output / "run.json", envelope)
    registry._new(output / "grade.json", grader.grade(envelope, output))
    return envelope


def normalize_bundle(source, output):
    """One original case result, retaining every independently registered child."""
    source, output = Path(source), Path(output)
    bundle = registry.read(source)
    rows = bundle["release_subscenarios"]
    case_id = bundle["case_id"]
    required = registry.subscenarios(case_id)
    if not isinstance(rows, dict) or not rows or set(rows) - set(required):
        raise ValueError("release_bundle_slots_invalid")
    output.mkdir(parents=True, exist_ok=False)
    children, originals = {}, {}
    for slot, descriptor in rows.items():
        registry._check_file(descriptor)
        original = registry.read(descriptor["path"])
        registered, _manifest, _events = registration(original["release_collection"]["registration"]["path"], require_dispatch=True)
        bound = registered["binding"]
        if bound["case_id"] != case_id or bound.get("subscenario") != slot:
            raise ValueError("release_bundle_raw_slot_mismatch")
        children[slot] = normalize(descriptor["path"], output / "components" / slot)
        originals[slot] = original
    first_slot = next(iter(children))
    envelope = copy.deepcopy(children[first_slot])
    run = envelope["run"]
    for child in children.values():
        if any(child["run"][key] != run[key] for key in ("case_id", "cohort", "trial")):
            raise ValueError("release_bundle_trial_mismatch")
    normalized = {"task_state", "business_state", "operation_trace", "independent_review"}
    run["evidence"] = [{**entry, "path": f"components/{first_slot}/{entry['path']}"}
                       for entry in run["evidence"] if entry["kind"] not in normalized]
    (output / "evidence").mkdir()
    def save(kind, value, sources=()):
        digest = registry.digest(value)
        relative = f"evidence/{kind}-{digest}.json"
        registry._new(output / relative, value)
        identity = kind + ":" + digest
        run["evidence"].append({"id": identity, "kind": kind, "path": relative, "sha256": digest,
                                "producer": "collector", "source_ids": list(sources)})
        return identity
    raw_ids = [save("raw_trace", {"slot": slot, "original": raw}) for slot, raw in originals.items()]
    raw_ids.append(save("raw_trace", {"bundle": bundle}))
    # Only fields present in every child can describe the aggregate outcome.
    task_state = {}
    tasks = [raw.get("task", {}) for raw in originals.values()]
    if all("run_state" in task for task in tasks):
        values = [task["run_state"] for task in tasks]
        task_state["run_state"] = values[0] if all(value == values[0] for value in values) else "mixed"
    if all("status" in task.get("outcome", {}) for task in tasks):
        values = [task["outcome"]["status"] for task in tasks]
        task_state["outcome"] = {"status": values[0] if all(value == values[0] for value in values) else "mixed"}
    trace, business = {}, {}
    security = [raw.get("security", {}) for raw in originals.values()]
    trace["security"] = {key: [item for value in security for item in value[key]]
                         for key in ReleaseRoleGrader().common.SECURITY_FIELDS
                         if all(isinstance(value.get(key), list) for value in security)}
    if case_id == "C02":
        business["subscenarios"] = {slot: copy.deepcopy(raw.get("business_state", {})) for slot, raw in originals.items()}
        trace["subscenarios"] = {slot: {key: raw["business_state"][key] for key in
            ("config_saves", "old_capability_invalidated", "rollback_conflict_rejected") if key in raw.get("business_state", {})}
            for slot, raw in originals.items()}
    elif case_id == "R05":
        stream = originals.get("stream_controls", {})
        trace.update({key: copy.deepcopy(stream[key]) for key in ("control", "metrics") if key in stream})
        late = originals.get("late_write_receipt", {}).get("checks", {})
        trace["subscenarios"] = {"late_write_receipt": {key: late[key] for key in
            ("receipt_retained", "effective_writes", "new_dispatches_after_cancel") if key in late}}
    elif case_id == "P04":
        observations = [raw.get("isolation", {}) for raw in originals.values()]
        isolation = {}
        for key in ("cross_session_markers", "provider_context_injected", "consumer_tool_node_ids"):
            if all(isinstance(item.get(key), list) for item in observations):
                values = [value for item in observations for value in item[key]]
                isolation[key] = sorted(set(values)) if key == "consumer_tool_node_ids" else values
        if all(type(item.get("provider_tool_executions")) is int for item in observations):
            isolation["provider_tool_executions"] = sum(item["provider_tool_executions"] for item in observations)
        if set(rows) == set(required) and all(isinstance(item.get("provider_agent_enabled"), bool) for item in observations):
            isolation["provider_enabled_and_disabled_checked"] = all(
                raw["isolation"]["provider_agent_enabled"] == required[slot]["provider_agent_enabled"]
                for slot, raw in originals.items())
        trace["isolation"] = isolation
    save("task_state", task_state, raw_ids)
    save("business_state", business, raw_ids)
    save("operation_trace", trace, raw_ids)
    envelope["components"] = {slot: {"path": f"components/{slot}/run.json", "sha256": registry.sha(output / "components" / slot / "run.json")}
                              for slot in children}
    registry._new(output / "run.json", envelope)
    registry._new(output / "grade.json", ReleaseRoleGrader().grade(envelope, output))
    return envelope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    normalize(args.source, args.output)


if __name__ == "__main__":
    main()
