"""Normalize supported development collections without inventing missing evidence.

This is a review bundle, not a formal trial registration. Dependencies, missing
security observations and reviewer claims stay pending. The original run is
retained byte-for-byte and every derived document links to its content hash.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def validator():
    spec = importlib.util.spec_from_file_location("frozen_acceptance_validator", ROOT / "tests/fixtures/agent_acceptance/validate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def token_total(calls, key):
    values = [(call.get("response", {}).get("usage") or call.get("usage") or {}).get(key) for call in calls]
    return sum(values) if values and all(type(value) is int and value >= 0 for value in values) else None


def normalize(source, output):
    source, output = Path(source), Path(output)
    raw = source.read_bytes()
    record = json.loads(raw)
    case_id = record.get("case_id")
    if case_id not in {"Q01", "Q02", "Q03", "Q04", "Q06", "F02", "F08", "R01", "R02", "P01", "P02", "P06", "C03"} or record.get("cohort") != "ryn" or not isinstance(record.get("task"), dict):
        raise ValueError("candidate_collection_not_supported")
    document = case_id in {"F02", "F08", "R01", "P01", "P02", "P06"}
    query = case_id in {"Q01", "Q02", "Q03", "Q06"}
    sharing = case_id in {"C03", "R02"}
    if document and record.get("kind") != "real-document-development-trial":
        raise ValueError("document_collector_kind_required")
    if (query or case_id == "C03") and record.get("kind") not in {"real-planner-fixed-query-world", "local-query-collector-check"}:
        raise ValueError("query_collector_kind_required")
    if case_id == "R02" and record.get("kind") not in {"real-sharing-recovery-development-trial", "local-sharing-recovery-check"}:
        raise ValueError("sharing_recovery_collector_kind_required")
    api = validator()
    case = next(row for row in api.suite("development")["cases"] if row["id"] == case_id)
    if record.get("prompt") != case["prompt"]:
        raise ValueError("frozen_prompt_mismatch")
    # Exclusive new directory; never overwrite a prior run or review decision.
    output.mkdir(parents=True, exist_ok=False)
    manifest = api.template(case_id, "development")

    def save(kind, value, sources=(), *, raw_bytes=None):
        body = raw_bytes if raw_bytes is not None else encode(value)
        sha = digest(body)
        relative = f"evidence/{kind}-{sha}.json"
        (output / "evidence").mkdir(exist_ok=True)
        (output / relative).write_bytes(body)
        identity = kind + ":" + sha
        manifest["evidence"].append({"id": identity, "kind": kind, "path": relative, "sha256": sha,
            "producer": "collector", "source_ids": list(sources)})
        return identity

    raw_id = save("raw_trace", None, raw_bytes=raw)
    calls = record.get("model_calls", [])
    actual_model = record.get("planner_model") if query or sharing else record.get("model")
    save("model_exchange", {"model": actual_model, "provider": record.get("provider"),
        "calls": calls, "provenance_note": "Extracted from the retained collector; source/operator verification is still required."}, [raw_id])
    task = record["task"]
    save("task_state", task, [raw_id])
    events = task.get("events", [])
    trace = {"metrics": {"writes": sum(event.get("type") == "tool_started" and event.get("effect") == "write" for event in events)},
        "events": events, "http_audit_includes_setup": record.get("http_audit", []),
        "unchanged_checks": record.get("checks", {}),
        "security": record.get("security", {}),
        "audit_note": "Absent security observations stay absent; successful collector checks do not imply an unmeasured universal security audit."}
    if query:
        trace.pop("http_audit_includes_setup")
        trace.update(http_health_audit=record.get("http_health_audit", []), setup_probes=record.get("setup_probes", []),
                     source_configuration_unchanged=record.get("source_configuration_unchanged"),
                     identity_mapping_policy=record.get("identity_mapping_policy"))
        business_state = {"source_readback": record.get("source_readback", {}),
                          "signed_registry_evidence": record.get("signed_registry_evidence", [])}
        if case_id == "Q06" and "source_readback_scope" in record:
            business_state["source_readback_scope"] = record["source_readback_scope"]
        save("business_state", business_state, [raw_id])
    if case_id == "Q06":
        # Preserve collector observations, including their original scope and
        # historical status. Signed setup probes do not establish task deployment.
        trace.update({key: record[key] for key in (
            "access_query", "actual_source_http_audit", "consumer_peer_id",
            "transport_binding_note", "physical_hosts", "two_physical_nodes",
            "write_counting_scope", "normalizer_status", "consumer_role", "provider_peer_id",
            "primary_model", "consumer_local_sources", "initial_consumer_models", "current_consumer_models",
            "consumer_wire_audit", "catalog_refresh_errors", "transport_scope", "source_readback_scope",
            "agent_state_isolation") if key in record})
        trace["metrics"]["tool_started_writes"] = trace["metrics"].pop("writes")
        # Only the collector's task interval mutation count supports this grade.
        trace["metrics"].update(record.get("metrics", {}))
    if sharing:
        # Translate only explicit persisted readbacks, never the model's prose.
        settings = record.get("settings_after", {})
        bindings = record.get("bindings", {})
        defaults = [name for name, identity in bindings.items()
                    if name.startswith("model-") and identity == settings.get("model_id")]
        agent = {"enabled": settings["enabled"]} if "enabled" in settings else {}
        if len(defaults) == 1:
            agent["default_model"] = defaults[0]
        models = {name: {target: row[source_key] for source_key, target in
                  (("shared", "sharing"), ("receivers", "receivers"), ("share_scope", "share_scope"))
                  if source_key in row} for name, row in record.get("source_readback", {}).items()}
        save("business_state", {"agent": agent, "models": models}, [raw_id])
        probes = {}
        if "receiver_visible_models" in record:
            probes["receiver_visible_models"] = record["receiver_visible_models"]
        receiver = bindings.get("node-b")
        for name in ("model-a", "model-b"):
            matches = [row for row in record.get("receiver_probes", []) if receiver
                       and row.get("receiver_peer_id") == receiver and row.get("model") == name]
            # The other peer's denied probe is not node-b's inference result.
            if len(matches) == 1 and "result" in matches[0]:
                probes[name] = {"receiver_inference": matches[0]["result"]}
        trace.pop("http_audit_includes_setup")
        trace.update(probes=probes, receiver_probes=record.get("receiver_probes", []),
                     source_http_audit_includes_setup=record.get("actual_source_http_audit", []),
                     catalog_authentication=record.get("catalog_authentication"),
                     physical_hosts=record.get("physical_hosts"),
                     initial_all_models_private=record.get("initial_all_models_private"),
                     sibling_and_source_configuration_unchanged=record.get("sibling_and_source_configuration_unchanged"),
                     settings_except_default_unchanged=record.get("settings_except_default_unchanged"))
        if case_id == "R02":
            trace["metrics"].update(record.get("metrics", {}))
            trace.update(approval=record.get("approval", {}), before_approval=record.get("before_approval", {}),
                         terminated_consumer=record.get("terminated_consumer", {}),
                         replacement_consumer_pid=record.get("replacement_consumer_pid"),
                         source_replacements=record.get("source_replacements", []))
    if document:
        trace.pop("http_audit_includes_setup")
        trace.update(http_audit=record.get("http_audit", []), setup_audit=record.get("setup_audit", []),
                     unchanged_checks=record.get("mechanical_checks", {}),
                     security_audit_coverage=record.get("security_audit_coverage", {}),
                     isolated_source_configuration_unchanged=record.get("isolated_source_configuration_unchanged"))
        # Request bindings are observations at the bridge boundary, not a
        # substitute for a complete upstream/network destination audit.
        if record.get("initial_model_destination"):
            trace["model_request_bindings"] = {"initial_model_id": record.get("initial_model_id"),
                "initial_destination": record["initial_model_destination"], "requests": [
                    {key: call.get(key) for key in ("requested_model_id", "expected_destination", "full_request_private_canary_observed")}
                    for call in calls]}
        if isinstance(record.get("original_resource_changes"), list):
            trace["security"]["original_resource_changes"] = record["original_resource_changes"]
        starts = {event.get("call_id") for event in events if event.get("type") == "tool_started"
                  and event.get("effect") in {"read", "write"}}
        successful = {event.get("call_id") for event in events if event.get("type") == "tool_result"
                      and event.get("call_id") in starts and isinstance(event.get("result"), dict)
                      and not event["result"].get("error") and event["result"].get("ok") is not False}
        trace["metrics"].update(record.get("metrics", {}), meaningful_operations=len(successful))
        trace["metrics"]["resource_read_attempts"] = sum(event.get("type") == "tool_started"
            and event.get("tool") == "ryn_read_resource" for event in events)
        artifacts = record.get("artifact_readback", [])
        save("business_state", {"artifact_count": len(artifacts), "artifact_metadata": [
            {key: value for key, value in artifact.items() if key != "content"} for artifact in artifacts]}, [raw_id])
        for artifact in artifacts:
            content = artifact.get("content", "").encode("utf-8")
            if digest(content) != artifact.get("sha256") or len(content) != artifact.get("bytes"):
                raise ValueError("artifact_readback_hash_mismatch")
            receipts = [entry for entry in record.get("http_audit", []) if entry.get("method") == "GET"
                        and entry.get("status") == 200 and entry.get("mutation") is False
                        and entry.get("response_dropped") is False and entry.get("path") == artifact.get("path")
                        and entry.get("body_sha256") == artifact["sha256"] and entry.get("bytes") == len(content)]
            connections = {entry.get("connection") for entry in receipts}
            if len(connections) != 1 or None in connections:
                # No independent receipt means no artifact claim in the grade.
                continue
            receipt_id = save("raw_receipt", {"http_readbacks": receipts}, [raw_id])
            readback_id = save("artifact_readback", None, [receipt_id], raw_bytes=content)
            manifest["artifacts"].append({"connection_id": next(iter(connections)), "path": artifact["path"],
                "readback_evidence_id": readback_id})
    save("operation_trace", trace, [raw_id])
    manifest["code_revision"] = "workspace-sha256:" + digest(encode(record.get("code_snapshot", {}))) if record.get("code_snapshot") and record.get("code_unchanged") is True else ""
    versions = {match.group() for call in calls for message in call.get("request_messages", []) if message.get("role") == "system"
                for match in re.finditer(r"ryn-agent-control-v[0-9.]+", message.get("content", ""))}
    manifest["prompt_version"] = next(iter(versions)) if len(versions) == 1 else ""
    manifest["started_at"], manifest["ended_at"] = record.get("started_at", ""), record.get("ended_at", "")
    manifest["model"].update(id=actual_model or "", provider=record.get("provider", ""),
        kind="real" if record.get("kind") in {"real-diagnostic-development-trial", "real-document-development-trial", "real-planner-fixed-query-world", "real-sharing-recovery-development-trial"} and calls else "synthetic")
    adapter = record.get("code_snapshot", {}).get("rynmesh/llm_package/adapters.py", "")
    manifest["model"]["adapter_version"] = "sha256:" + adapter if adapter else ""
    manifest["bindings"].update(record.get("bindings", {}))
    if record.get("isolated_run_root"):
        manifest["bindings"]["isolated_run_root"] = record["isolated_run_root"]
    manifest["bindings"]["primary_model"] = task.get("model_id", "")
    manifest["measurements"].update(elapsed_seconds=record.get("elapsed_seconds"),
        input_tokens=token_total(calls, "input_tokens"), output_tokens=token_total(calls, "output_tokens"),
        explicit_approvals=sum(row.get("approved") is True for row in record.get("approvals", [])),
        unknown_reason="Cost/pricing unknown. Missing metadata, universal security audits and reviewer claims are not inferred. Counts cover recorded collector interventions only.")
    (output / "run.json").write_bytes(encode(manifest))
    result = api.grade(manifest, output)
    (output / "grade.json").write_bytes(encode(result))
    collection = {"formal_trial_registered": False, "source_sha256": digest(raw),
        "source": str(source.resolve()), "normalizer_sha256": digest(Path(__file__).read_bytes()),
        "note": "Development review bundle only. No reviewer verdict, dependency verification or absent security audit is synthesized."}
    if case_id == "Q06":
        if record.get("consumer_role") == "node-b_authorized_peer_consumer":
            collection["implementation_limitations"] = [
                "The collector reports a separate node-b consumer using a local signed wire fixture. "
                "Consumer observations and provider ground truth are retained separately. "
                "This declaration does not verify deployment, two physical hosts, security or semantic review dependencies."]
        else:
            collection["implementation_limitations"] = [
                "This historical controller executes the consumer task in the isolated provider-owner home. "
                "Deployment of the frozen consumer_node=node-b has not been verified. "
                "Node-b signed friend admission probes are setup observations, not evidence of consumer task deployment."]
    (output / "collection.json").write_bytes(encode(collection))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(normalize(args.source, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
