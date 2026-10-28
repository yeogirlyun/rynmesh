"""Preparation reads retain strict facts around an attributable atomic mutation."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_agent_atomic_sharing import CONTEXT
from test_agent_sharing_scope import node, setup  # noqa: F401

from rynmesh.agent_runtime import observation_transitions, verification
from rynmesh.agent_runtime.sharing import NAME


@pytest.fixture
def world(node):  # noqa: F811
    app, store = node
    source, model_id, sources, tools, permissions, relations = setup(app, store)
    return source, sources, tools, permissions, relations, {"model_id": model_id, "relationship_id": "b" * 32}


def task_row():
    return {"goal": {"revision": 1}, "scope_revision": 1, "context_epoch": 0,
            "criteria": [], "receipts": [], "outcome": {}}


def operation(row, tools, name, args, operation_id, index):
    call = {"name": name, "arguments": args, "operation_id": operation_id,
            "effect": "write" if name == NAME else "read", "context_epoch": 0}
    stamp = f"2026-10-10T00:00:{index:02d}+00:00"
    verification.record(row, call, "intent", stamp)
    result = tools.execute(name, args, operation_id=operation_id, context=CONTEXT, cancelled=lambda: False,
                           expected_preview=tools.preview(name, args, context=CONTEXT) if name == NAME else None)
    verification.record(row, call, "confirmed", stamp, result=result)
    return row["receipts"][-1]


def scenario(world, reader="ryn_list_models"):
    _, _, tools, _, _, args = world
    row = task_row()
    before = operation(row, tools, reader, {}, "before", 0)
    writer = operation(row, tools, NAME, args, "write", 1)
    return row, before, writer


def run_checks(row, tools, *, write_status=None):
    verification.begin(row, tools.verification_specs())
    output = {}
    for check in row["verification"]["checks"]:
        receipt = next(r for r in row["receipts"] if r["id"] == check["receipt_id"])
        criterion = next(c for c in row["criteria"] if c["id"] == check["criterion_id"])
        dependencies = observation_transitions.context(row, criterion)
        result = tools.verify(receipt, [criterion], context={**CONTEXT, "observation_dependencies": dependencies}, cancelled=lambda: False)
        if receipt["tool"] == NAME and write_status:
            result["status"] = write_status
        eid = "evidence-" + receipt["operation_id"]
        criterion.update(status=result["status"], evidence_refs=[eid])
        row["verification_evidence"].append({"id": eid, "receipt_id": receipt["id"], "criterion_id": criterion["id"],
            "goal_revision": 1, "scope_revision": 1, **result})
        output[receipt["operation_id"]] = result
    return output


@pytest.mark.parametrize("reader", ["ryn_list_models", "ryn_inspect_services"])
def test_preparation_read_verifies_exact_transition_without_deleting_original_criterion(world, reader):
    _, sources, tools, permissions, _, args = world
    sid = args["model_id"][6:]
    permissions.set(sid, "a" * 32, allowed=True, expected_revision=0)
    row, before, writer = scenario(world, reader)
    # A different postwrite observation does not erase the earlier reader.
    operation(row, tools, "ryn_inspect_services" if reader == "ryn_list_models" else "ryn_list_models", {}, "after", 2)
    audit = copy.deepcopy(row["receipts"])
    output = run_checks(row, tools)
    assert {key: value["status"] for key, value in output.items()} == {"write": "passed", "before": "passed", "after": "passed"}
    assert row["receipts"] == audit
    assert len(row["criteria"]) == 3 and all(c["required"] for c in row["criteria"])
    transition = output["before"]["evidence"]["observation_transitions"][0]
    assert transition["receipt_id"] == writer["id"] and transition["evidence_id"] == "evidence-write"
    assert before["result"] != output["before"]["evidence"]["checked_facts"]
    assert "observation_transition" not in writer["result"]
    assert "observation_transition" not in json.dumps(sources.public(next(iter(sources.read().values()))))


@pytest.mark.parametrize("change", ["label", "sibling_policy", "sibling_sharing", "sibling_deleted", "target_policy", "target_disabled", "unrelated_grant"])
def test_unmentioned_external_drift_is_never_covered_by_share(world, change):
    source, sources, tools, permissions, _, _ = world
    row, _, _ = scenario(world, "ryn_inspect_services")
    saved = sources.read()[source["id"]]
    if change == "label":
        saved["name"] = "Changed externally"
    elif change == "sibling_policy":
        saved["model_settings"]["model-b"]["overrides"]["max_concurrent"] = 2
    elif change == "sibling_sharing":
        saved["shared_models"].append("model-b")
    elif change == "sibling_deleted":
        saved["models"] = ["model-a"]
    elif change == "target_policy":
        saved["model_settings"]["model-a"]["overrides"]["max_concurrent"] = 2
    elif change == "target_disabled":
        saved["enabled"] = False
    else:
        permissions.set(sources.model_service_id(saved, "model-b"), "a" * 32, allowed=True, expected_revision=0)
    if change != "unrelated_grant":
        sources.save(saved)
    output = run_checks(row, tools)
    assert output["before"]["status"] == "failed"
    if change not in {"target_policy", "target_disabled"}:
        assert output["write"]["status"] == "passed"


@pytest.mark.parametrize("status", ["failed", "unknown", "pending"])
def test_missing_passed_write_check_cannot_supersede_observation(world, status):
    row, _, _ = scenario(world)
    output = run_checks(row, world[2], write_status=status)
    assert output["before"]["status"] == "failed"
    assert output["write"]["status"] == status
    assert "observation_transitions" not in output["before"]["evidence"]


@pytest.mark.parametrize("invalid", ["denied", "unknown", "failed", "goal", "scope", "epoch", "ordering", "forged_result", "legacy"])
def test_invalid_dependency_cannot_explain_changed_read(world, invalid):
    _, sources, tools, _, _, _ = world
    row, before, writer = scenario(world)
    if invalid in {"denied", "unknown", "failed"}:
        writer["status"] = invalid
    elif invalid in {"goal", "scope", "epoch"}:
        writer[{"goal": "goal_revision", "scope": "scope_revision", "epoch": "context_epoch"}[invalid]] += 1
    elif invalid == "ordering":
        writer["started_at"] = "2026-10-09T00:00:00+00:00"
    elif invalid == "forged_result":
        writer["result"]["source_revision"] = "forged"
        writer["result"]["observation_transition"] = {"before": before["result"], "after": tools.catalog.list_models()}
    else:
        records = sources.read()
        next(iter(records.values()))["_sharing_operations"]["write"].pop("observation_transition")
        sources._write(records)
    output = run_checks(row, tools)
    assert output["before"]["status"] == "failed"
    assert "observation_transitions" not in output["before"]["evidence"]


def test_external_change_before_commit_cannot_hide_in_committed_before_fields(world):
    source, sources, tools, _, _, args = world
    row = task_row()
    operation(row, tools, "ryn_list_models", {}, "before", 0)
    saved = sources.read()[source["id"]]
    saved["model_settings"]["model-a"]["overrides"]["max_concurrent"] = 2
    sources.save(saved)
    operation(row, tools, NAME, args, "write", 1)
    output = run_checks(row, tools)
    assert output["write"]["status"] == "passed" and output["before"]["status"] == "failed"


def test_overlapping_writes_stay_conservative_and_keep_latest_target_grouping(world):
    row, _, _ = scenario(world)
    tools, args = world[2], world[-1]
    operation(row, tools, NAME, args, "second-write", 2)
    output = run_checks(row, tools)
    assert set(output) == {"before", "second-write"}
    assert output["second-write"]["status"] == "passed" and output["before"]["status"] == "failed"


def test_nonregistered_mutation_remains_strict(world):
    source, sources, tools, _, _, args = world
    row = task_row()
    operation(row, tools, "ryn_list_models", {}, "before", 0)
    call = {"name": "ryn_set_model_sharing", "arguments": {"model_id": args["model_id"], "enabled": True},
            "operation_id": "other-writer", "effect": "write", "context_epoch": 0}
    verification.record(row, call, "intent", "2026-10-10T00:00:01+00:00")
    saved = sources.read()[source["id"]]
    saved["shared_models"] = ["model-a"]
    sources.save(saved)
    verification.record(row, call, "confirmed", "2026-10-10T00:00:02+00:00", result={"ok": True})
    output = run_checks(row, tools)
    assert output["before"]["status"] == "failed"
    assert "observation_dependencies" not in row["criteria"][0]


def test_changed_goal_scope_or_epoch_cannot_reuse_transition(world):
    row, _, _ = scenario(world)
    for field in ("goal", "scope_revision", "context_epoch"):
        changed = copy.deepcopy(row)
        if field == "goal":
            changed["goal"]["revision"] += 1
        else:
            changed[field] += 1
        verification.begin(changed, world[2].verification_specs())
        assert not any(c.get("observation_dependencies") for c in changed["criteria"])


def test_transition_is_revalidated_after_dependency_check_before_read_comparison(world):
    source, sources, tools, _, _, _ = world
    row, before, _ = scenario(world)
    run_checks(row, tools)
    criterion = next(c for c in row["criteria"] if c["source_ref"] == before["id"])
    dependencies = observation_transitions.context(row, criterion)
    assert dependencies
    saved = sources.read()[source["id"]]
    saved["enabled"] = False
    sources.save(saved)
    result = tools.verify(before, [criterion], context={**CONTEXT, "observation_dependencies": dependencies}, cancelled=lambda: False)
    assert result["status"] == "failed" and "observation_transitions" not in result["evidence"]


def test_attributed_transition_survives_actual_process_reopen(tmp_path):
    from test_agent_atomic_sharing import execute, receipt, seed

    _, _, tool, _, args = seed(tmp_path)
    result = execute(tool, args)
    writer = {**receipt(args, result), "goal_revision": 1, "scope_revision": 1}
    task_file = tmp_path / "receipt.json"
    task_file.write_text(json.dumps(writer), encoding="utf-8")
    script = """
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
from test_agent_atomic_sharing import CONTEXT, open_tools
_, _, tool, _ = open_tools(sys.argv[1])
receipt = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
value = tool.observation_transition(receipt, context=CONTEXT, cancelled=lambda: False)
assert value and value['before']['sharing'] is False and value['after']['sharing'] is True
receipt['result']['source_revision'] = 'forged'
assert tool.observation_transition(receipt, context=CONTEXT, cancelled=lambda: False) is None
print('verified-after-reopen')
"""
    child = subprocess.run([sys.executable, "-c", script, str(tmp_path), str(task_file)], cwd=Path(__file__).resolve().parents[1],
                           check=True, capture_output=True, text=True, timeout=30)
    assert child.stdout.strip() == "verified-after-reopen"


def test_oversized_transition_is_omitted_without_losing_write_receipt(world, monkeypatch):
    from rynmesh.agent_runtime import sharing

    monkeypatch.setattr(sharing, "MAX_TRANSITION_BYTES", 1)
    row, _, writer = scenario(world)
    output = run_checks(row, world[2])
    assert output["write"]["status"] == "passed" and output["before"]["status"] == "failed"
    sources = world[1]
    private = next(iter(sources.read().values()))["_sharing_operations"]["write"]
    assert private["result"] == writer["result"] and "observation_transition" not in private
