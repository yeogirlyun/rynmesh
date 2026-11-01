"""Kill/reopen a real consumer at the frozen sharing approval boundary."""
import importlib.util
import json
import os
from pathlib import Path


def test_r02_actual_restart_keeps_approval_and_performs_one_atomic_share(tmp_path, monkeypatch):
    folder = Path(__file__).resolve().parents[1] / "docs/acceptance/ryn-agent-delivery/real-model"
    monkeypatch.syspath_prepend(str(folder))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_LAN_DISCOVERY", "0")
    spec = importlib.util.spec_from_file_location("sharing_recovery_controller", folder / "run_sharing_recovery.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    owned_processes = []
    original_owned = module.OwnedConsumerProcess
    original_popen = module.subprocess.Popen

    def tracked_owned(child):
        owned = original_owned(child)
        owned_processes.append(owned)
        return owned

    def ordered_launch(*args, **kwargs):
        if owned_processes:
            first = owned_processes[0]
            assert first.proof["confirmed_terminal"]
            assert first.proof["actual_worker_terminal_proof"]["confirmed_terminal"]
            if os.name == "nt":
                from consumer_process_proof import _wait
                assert _wait(first.handles[first.worker_pid]) == 0
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(module, "OwnedConsumerProcess", tracked_owned)
    monkeypatch.setattr(module.subprocess, "Popen", ordered_launch)
    output = tmp_path / "evidence"
    result = module.execute(output, home=tmp_path / "consumer")
    assert "error_type" not in result
    assert len(owned_processes) == 2
    assert all(process.closed and not process.handles for process in owned_processes)
    # Recovery is already terminal when its reported PID is read from the saved
    # worker record; it must not be reopened to manufacture historical proof.
    assert owned_processes[1].child.poll() is not None
    assert owned_processes[1].worker_pid is None
    assert result["approval"] == {"identity_preserved": True, "control_revision_preserved": True, "different_process": True}
    assert result["terminated_consumer"]["confirmed_terminal"]
    terminated = result["terminated_consumer"]
    assert terminated["actual_worker_pid"] == result["before_restart"]["pid"] == terminated["pid"]
    assert terminated["launcher_confirmed_terminal"]
    assert terminated["actual_worker_terminal_proof"]["confirmed_terminal"]
    assert terminated["identity_basis"] in {"same_launched_process", "verified_descendant"}
    if os.name == "nt":
        assert terminated["actual_worker_terminal_proof"]["handle_opened_before_termination"]
        assert terminated["actual_worker_terminal_proof"]["wait_status"] == 0
    assert any(name.endswith("/consumer_process_proof.py") for name in result["code_snapshot"])
    assert result["terminated_consumer"]["pid"] != result["replacement_consumer_pid"]
    assert result["before_approval"] == {"sources_unchanged": True, "grants_unchanged": True, "writes": 0}
    assert result["metrics"]["effective_writes"] == result["metrics"]["source_file_replacements"] == 1
    assert result["metrics"]["writes_before_approval"] == 0
    assert result["source_readback"]["model-b"]["receivers"] == ["node-b"]
    assert result["source_readback"]["model-a"]["shared"]  # R02 preserves its different initial world.
    assert result["sibling_and_source_configuration_unchanged"]
    assert [row["result"] for row in result["receiver_probes"]] == ["allowed", "denied"]
    assert [row["upstream_requests"] for row in result["receiver_probes"]] == [1, 0]
    assert result["task"]["messages"][0]["content"] == result["prompt"] == "分享 model-b 给 node-b，等我的具体批准后执行。"
    assert result["task"]["outcome"]["status"] == "unverified"  # Scripted checker cannot claim real quality.
    assert all(c["status"] == "passed" for c in result["task"]["outcome"]["criteria_results"] if c["kind"] == "mechanical")
    assert result["formal_case_score"] is False and result["owner_source_unchanged"]
    records = json.loads((output / "recover-worker.json").read_text(encoding="utf-8"))
    assert result["replacement_consumer_pid"] == records["pid"]
    assert len(records["approvals"]) == 1 and records["approvals"][0]["approved"]
    tool_names = [event["tool"] for event in result["task"]["events"] if event["type"] == "tool_started"]
    # Exercise the actual failed-run sequence: preparation catalog, mutation,
    # then another current-state reader. Repeating the old catalog would hide
    # the superseded-observation lifecycle regression.
    assert tool_names.count("ryn_list_models") == 1
    assert tool_names.count("ryn_inspect_services") == 1
    # The real process boundary must preserve not only dispatch permission but
    # also the evidence both providers receive about that historical approval.
    approved = records["approvals"][0]["approval"]
    main_facts, checker_facts = [], []
    for call in records["model_calls"]:
        messages = call["request_messages"]
        for message in messages:
            if message["role"] == "system" and "Server control state: " in message["content"]:
                state = json.loads(message["content"].split("Server control state: ", 1)[1].split("\n", 1)[0])
                main_facts.extend(state.get("operation_permissions", []))
            elif message["role"] == "user":
                try:
                    payload = json.loads(message["content"])
                except (ValueError, TypeError):
                    continue
                if isinstance(payload, dict):
                    checker_facts.extend(payload.get("operation_permissions", []))
    assert main_facts and checker_facts
    for fact in main_facts + checker_facts:
        assert fact["permission_basis"] == "owner_approved"
        assert fact["approval_id"] == approved["id"]
        assert fact["arguments_sha256"] == approved["arguments_sha256"]
        assert fact["preview_sha256"] == approved["preview_sha256"]
        assert fact["operation_status"] == "confirmed"
    normalizer = importlib.util.spec_from_file_location("sharing_normalizer", folder / "normalize_diagnostic.py")
    normalized = importlib.util.module_from_spec(normalizer)
    normalizer.loader.exec_module(normalized)
    grade = normalized.normalize(output / "run.json", output / "normalized")
    assert grade["status"] == "failed"  # Synthetic unverified is not a real acceptance pass.
    assert any("goal_outcome" in failure for failure in grade["failures"])
    assert not any("metrics_" in failure or "approval_identity" in failure for failure in grade["failures"])
    assert any("process_restart_controller" in item for item in grade["pending"])
