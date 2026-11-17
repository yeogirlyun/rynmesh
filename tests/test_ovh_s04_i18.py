"""S04-I18 timeout evidence and corrections reach real model/dispatch paths."""
import json
import os

import pytest
from test_agent_integrations import node, fake_upstream, call, passed_checker
from test_agent_local_execution_reliability import _make_node_runtime
from test_agent_local_tools import local
from test_agent_runtime import make_runtime, assistant, advance
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected


TIMEOUT_CORRECTION = (
    "The command was stopped at its timeout. Narrow it, give a larger timeout_seconds, "
    "or run it with background=true and read its output."
)


def test_t1_streamed_read_shell_needs_no_mutation_preview_but_writes_still_do(local):
    tools, context, workspace, _ = local
    read_command = "Get-Location" if os.name == "nt" else "pwd"
    result = tools.execute("ryn_shell_execute", {"command": read_command}, operation_id="read-without-preview",
        expected_preview=None, cancelled=lambda: False, context=context)
    assert result["exit_code"] == 0
    write_command = "Set-Content forbidden.txt x" if os.name == "nt" else "echo x > forbidden.txt"
    with pytest.raises(AgentToolRejected, match="agent_approval_preview_changed"):
        tools.execute("ryn_shell_execute", {"command": write_command}, operation_id="write-without-preview",
            expected_preview=None, cancelled=lambda: False, context=context)
    assert not (workspace / "forbidden.txt").exists()


def run_to_end(runtime, task_id, limit=22):
    for _ in range(limit):
        runtime.run_once()
        row = runtime.get(task_id)
        if row["status"] in {"completed", "failed", "interrupted", "waiting_approval"}:
            break
    assert row["status"] == "completed", row.get("error")
    return row


def test_t1_agent_error_supports_default_and_explicit_correction_without_becoming_rejection():
    default = AgentError("agent_shell_timeout", details={"output": "partial"})
    assert default.correction == TIMEOUT_CORRECTION and default.details == {"output": "partial"}
    explicit = AgentError("agent_shell_timeout", correction="Specific host correction", details={"stop_reason": "timeout"})
    assert explicit.correction == "Specific host correction"
    assert str(explicit) == "agent_shell_timeout" and not isinstance(explicit, AgentToolRejected)
    assert AgentToolRejected("agent_shell_timeout").correction == TIMEOUT_CORRECTION


def test_t1_timeout_replay_preserves_evidence_without_rerunning(local, monkeypatch):
    tools, context, _, _ = local
    original_wait, original_start = tools.processes.wait, tools.processes.start
    starts = []
    def timeout(*args, **kwargs):
        return {**original_wait(*args, **kwargs), "stop_reason": "timeout", "running": False}
    def start(*args, **kwargs):
        starts.append(1)
        return original_start(*args, **kwargs)
    monkeypatch.setattr(tools.processes, "wait", timeout)
    monkeypatch.setattr(tools.processes, "start", start)
    args = {"command": "Get-Location" if os.name == "nt" else "pwd"}
    def execute():
        return tools.execute("ryn_shell_execute", args, operation_id="same-timeout", expected_preview=None,
            cancelled=lambda: False, context=context)
    with pytest.raises(AgentError, match="agent_shell_timeout") as first:
        execute()
    with pytest.raises(AgentError, match="agent_shell_timeout") as replay:
        execute()
    assert first.value.details and replay.value.details == first.value.details
    assert replay.value.correction == first.value.correction == TIMEOUT_CORRECTION
    assert starts == [1]


def test_t1_actual_read_only_shell_timeout_keeps_output_tail_in_next_model_request(node, tmp_path, monkeypatch):
    runtime, store, _, tools, _, _ = _make_node_runtime(node, tmp_path, "s04-i18-partial")
    runtime.update_settings({"permission_mode": "read_only"})
    (tmp_path / "partial.txt").write_text("PREFIX_MUST_BE_TRUNCATED" + "x" * 5000 + "PARTIAL_OUTPUT_END\n", encoding="utf-8")
    command = "Get-Content -LiteralPath partial.txt -Wait" if os.name == "nt" else "tail -f partial.txt"
    requests = fake_upstream(monkeypatch, [call("ryn_shell_execute", json.dumps({"command": command, "timeout_seconds": 3})),
        {"role": "assistant", "content": "Observed partial output and timeout."}, passed_checker])
    task = runtime.create({"message": "Read the current partial log within three seconds."})
    run_to_end(runtime, task["id"])
    receipt = next(r for r in store.read()["tasks"][task["id"]]["receipts"] if r["tool"] == "ryn_shell_execute")
    result = receipt["result"]
    assert result["error"] == "agent_shell_timeout" and result["correction"] == TIMEOUT_CORRECTION
    assert result["not_executed"] is False and result["no_mutation"] is True
    assert result["details"]["stop_reason"] == "timeout" and result["details"]["timeout_seconds"] == 3
    raw = tools.local.ledger_factory().get(receipt["operation_id"])["result"]["output"]
    assert result["details"]["output"] == raw[-4000:] and len(result["details"]["output"]) == 4000
    assert "PARTIAL_OUTPUT_END" in result["details"]["output"]
    assert "PREFIX_MUST_BE_TRUNCATED" not in result["details"]["output"]
    seen = [json.loads(m["content"]) for m in requests[1]["messages"] if m["role"] == "tool"]
    assert any(r.get("details") == result["details"] and r.get("correction") == TIMEOUT_CORRECTION for r in seen)
    assert not tools.local.processes.running_for_operation(task["id"], receipt["operation_id"])


def test_t1_parallel_read_errors_preserve_base_error_correction_and_details(node, tmp_path, monkeypatch):
    runtime, store, _, tools, _, _ = _make_node_runtime(node, tmp_path, "s04-i18-batch")
    original = tools.local.execute
    def failed_read(name, args, **kwargs):
        if name == "ryn_file_read":
            raise AgentError("agent_shell_timeout", details={"output": args["path"], "stop_reason": "timeout", "timeout_seconds": 3})
        return original(name, args, **kwargs)
    monkeypatch.setattr(tools.local, "execute", failed_read)
    pair = call("ryn_file_read", json.dumps({"path": "a.txt"}), "read_a")
    pair["tool_calls"].extend(call("ryn_file_read", json.dumps({"path": "b.txt"}), "read_b")["tool_calls"])
    requests = fake_upstream(monkeypatch, [pair, {"role": "assistant", "content": "Both reads timed out."}, passed_checker])
    task = runtime.create({"message": "Read a.txt and b.txt independently."})
    run_to_end(runtime, task["id"])
    receipts = [r for r in store.read()["tasks"][task["id"]]["receipts"] if r["tool"] == "ryn_file_read"]
    assert len(receipts) == 2
    assert all(r["result"]["correction"] == TIMEOUT_CORRECTION for r in receipts)
    assert {r["result"]["details"]["output"] for r in receipts} == {"a.txt", "b.txt"}
    assert all(r["result"]["not_executed"] is False and r["result"]["no_mutation"] is True for r in receipts)
    seen = [json.loads(m["content"]) for m in requests[1]["messages"] if m["role"] == "tool"]
    assert len([r for r in seen if r.get("correction") == TIMEOUT_CORRECTION]) == 2


def test_t2_three_timeouts_do_not_prevent_a_fourth_identical_dispatch(node, tmp_path, monkeypatch):
    runtime, store, _, tools, _, _ = _make_node_runtime(node, tmp_path, "s04-i18-retries")
    runtime.update_settings({"permission_mode": "read_only"})
    original_wait = tools.local.processes.wait
    waits = []
    def timeout(ident, task_id, cancelled):
        result = original_wait(ident, task_id, cancelled)
        waits.append(ident)
        return {**result, "running": False, "stop_reason": "timeout"}
    monkeypatch.setattr(tools.local.processes, "wait", timeout)
    command = "Get-Location" if os.name == "nt" else "pwd"
    fake_upstream(monkeypatch, [*[call("ryn_shell_execute", json.dumps({"command": command}), f"read_{i}") for i in range(4)],
        {"role": "assistant", "content": "Reported timeouts."}, passed_checker])
    task = runtime.create({"message": "Inspect the current location; report the observations."})
    run_to_end(runtime, task["id"])
    receipts = [r for r in store.read()["tasks"][task["id"]]["receipts"] if r["tool"] == "ryn_shell_execute"]
    assert len(waits) == 4 and len(receipts) == 4
    assert all(r["result"]["error"] == "agent_shell_timeout" and r["result"]["not_executed"] is False for r in receipts)
    assert all(r["result"]["correction"] == TIMEOUT_CORRECTION for r in receipts)
    assert all(r["result"]["details"]["timeout_seconds"] == 120 for r in receipts)


def test_t3_read_only_refusal_names_mode_and_alternative_tools(node, tmp_path, monkeypatch):
    runtime, store, _, _, _, _ = _make_node_runtime(node, tmp_path, "s04-i18-denial")
    runtime.update_settings({"permission_mode": "read_only"})
    command = "Get-Location | Out-File denied.txt" if os.name == "nt" else "pwd > denied.txt"
    fake_upstream(monkeypatch, [call("ryn_shell_execute", json.dumps({"command": command})),
        {"role": "assistant", "content": "The write was refused in read-only mode."}, passed_checker])
    task = runtime.create({"message": "Try the requested command within the task's read-only permissions."})
    run_to_end(runtime, task["id"])
    result = next(r["result"] for r in store.read()["tasks"][task["id"]]["receipts"] if r["tool"] == "ryn_shell_execute")
    assert result["error"] == "agent_permission_denied" and result["not_executed"] is True and result["no_mutation"] is True
    assert "This task is read-only" in result["correction"]
    assert "ryn_file_read" in result["correction"] and "ryn_file_search" in result["correction"]
    assert not (tmp_path / "denied.txt").exists()


def test_t3_invalid_policy_keeps_original_correction_and_valid_read_pipes_remain_allowed():
    from rynmesh.agent_runtime.execution_policy import PERMISSION_DENIED_CORRECTION, PERMISSION_DENIED_CORRECTIONS, decide
    decision = decide({}, {}, {"permission_mode": "invalid"}, {})
    assert decision.basis == "invalid_policy"
    assert PERMISSION_DENIED_CORRECTIONS[decision.basis] == PERMISSION_DENIED_CORRECTION == (
        "Current host permissions do not allow this operation and nothing ran. Do not work around the setting "
        "with another tool. Continue with what the permissions allow and say in your reply which setting blocks the rest.")
    command = "Get-Location | Select-Object -First 1" if os.name == "nt" else "pwd | head -n 1"
    read = decide({"name": "ryn_shell_execute", "arguments": {"command": command}},
        {"effect": "write"}, {"permission_mode": "read_only"}, {})
    assert read.action == "execute" and read.basis == "read"


@pytest.mark.parametrize("parallel", [False, True])
def test_t1_nonstreaming_error_paths_preserve_properties(tmp_path, monkeypatch, parallel):
    pair = assistant(name="inspect_service", args={"service_id": "a"}, call_id="read_a")
    if parallel:
        pair["message"]["tool_calls"].extend(assistant(name="inspect_service", args={"service_id": "b"}, call_id="read_b")["message"]["tool_calls"])
    runtime, model, tools, _ = make_runtime(tmp_path, pair, assistant("Observed read timeouts."))
    metadata = tools.metadata
    tools.metadata = lambda name: {**metadata(name), "supports_parallel": True}
    def failed_read(name, arguments, **kwargs):
        raise AgentError("agent_shell_timeout", details={"output": arguments["service_id"]})
    tools.execute = failed_read
    batches = []
    original_batch = runtime._tool_batch
    def capture_batch(selected, batch, *args, **kwargs):
        batches.append(len(batch))
        return original_batch(selected, batch, *args, **kwargs)
    monkeypatch.setattr(runtime, "_tool_batch", capture_batch)
    task = runtime.create({"message": "Inspect a and b independently."})
    advance(runtime, 3)
    assert runtime.get(task["id"])["status"] == "completed" and batches == ([2] if parallel else [])
    results = [json.loads(m["content"]) for m in model.calls[-1][1] if m["role"] == "tool"]
    assert {r["details"]["output"] for r in results} == ({"a", "b"} if parallel else {"a"})
    assert all(r["correction"] == TIMEOUT_CORRECTION and r["not_executed"] is False for r in results)
