"""Regression coverage for permission changes, response retention and usage estimates."""
import pytest
from test_agent_runtime import make_runtime, assistant, advance
from rynmesh.agent_runtime.execution_policy import (
    Decision,
    decide,
    literal_command,
    shell_read,
    verified_read_classification,
)
from rynmesh.agent_runtime import budget, execution_policy, local_processes
from rynmesh.agent_runtime.store import AgentStore, AgentToolRejected, encoded


def test_powershell_paths_preserve_shell_interpolation_guards():
    command = r'Get-ChildItem "D:\code\rynmesh"'
    assert shell_read({"command": command, "shell": "powershell"})
    assert literal_command(command, "sh") is None
    for value in ('$HOME', '`whoami', '$(whoami)'):
        assert not shell_read({"command": f'Get-ChildItem "{value}"', "shell": "powershell"})


def test_global_permissions_do_not_wake_waiting_or_reorder_paused_tasks(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "s"}))
    waiting = runtime.create({"message": "Share"})
    advance(runtime, 2)
    assert runtime.get(waiting["id"])["status"] == "waiting_approval"
    paused = runtime.create({"message": "Old task"})
    def age(data):
        for ident in (waiting["id"], paused["id"]):
            data["tasks"][ident]["updated_at"] = 1
        data["tasks"][paused["id"]]["status"] = "paused"
    runtime.store.transaction(age)
    runtime.update_settings({"permission_mode": "automatic", "allowed_write_tools": ["share_service"]})
    for ident in (waiting["id"], paused["id"]):
        row = runtime.get(ident)
        assert row["status"] == "paused"
        assert row["updated_at"] == 1
        assert row["pending_approval"] is None
        assert row["events"][-1]["type"] == "permissions_changed"
    advance(runtime)
    assert not tools.calls
    assert len(model.calls) == 1


@pytest.mark.parametrize("chunks", [None, [500], [200, 300]])
def test_output_limit_keeps_utf8_prefix_after_store_reload(tmp_path, chunks):
    text = "汉" * 500
    runtime, model, tools, key = make_runtime(tmp_path, assistant(text))
    runtime.update_settings({"max_output_chars": 1000})
    if chunks:
        def complete(model_id, messages, tool_specs, cancelled, on_event):
            for size in chunks:
                on_event({"type": "text_delta", "text": "汉" * size})
            raise AssertionError("overflow must stop generation")
        model.complete = complete
    created = runtime.create({"message": "Answer"})
    advance(runtime)
    row = AgentStore(tmp_path, key).read()["tasks"][created["id"]]
    assert row["error"] == "agent_output_limit"
    partial = "".join(e["text"] for e in row["events"] if e["type"] == "text_delta")
    assert partial == "汉" * 333
    assert len(partial.encode()) <= 1000
    assert not tools.calls


def test_partial_usage_uses_actual_response_size_in_runtime(tmp_path):
    response = {**assistant("A short answer"), "usage": {"input_tokens": 100}}
    runtime, _, _, _ = make_runtime(tmp_path, response)
    created = runtime.create({"message": "Answer"})
    advance(runtime)
    record = runtime.store.read()["tasks"][created["id"]]["budget"]["records"][0]
    assert record["charged_tokens"] == 100 + len(encoded(response["message"]))
    assert record["output_tokens"] is None
    assert record["output_estimate_basis"] == "response_utf8_bytes"


def test_partial_usage_estimate_preserves_cached_discount_and_known_output():
    row = {"limits": {"max_seconds": 30}, "steps": 0, "elapsed_seconds": 0}
    rec = budget.reserve(row, "model", "main", input_bound=1000, output_bound=393216)
    budget.settle(row, rec["id"], usage={"input_tokens": 1000, "cached_tokens": 800}, response_bytes=300, cached_input_weight=0.1)
    assert row["budget"]["records"][0]["charged_tokens"] == 580
    rec = budget.reserve(row, "model", "main", input_bound=1000, output_bound=393216)
    budget.settle(row, rec["id"], usage={"input_tokens": 10, "output_tokens": 0}, response_bytes=300)
    assert row["budget"]["records"][1]["charged_tokens"] == 10


def test_powershell_smart_quotes_and_dashes_rejected_as_read():
    exploit_cmd = 'Get-Content "x\u2019;Remove-Item y;\u2019"'
    assert literal_command(exploit_cmd, "powershell") is None
    assert not shell_read({"command": exploit_cmd, "shell": "powershell"})
    decision = decide(
        {"name": "ryn_shell_execute", "arguments": {"command": exploit_cmd, "shell": "powershell"}},
        {"effect": "write"},
        {"permission_mode": "read_only"},
        {"permission_mode": "read_only"},
    )
    assert decision == Decision("deny", "read_only")

    # U+2014 em-dash and all non-ASCII quote/dash aliases rejected inside or outside quotes
    assert literal_command("Get-ChildItem \u2014Recurse", "powershell") is None
    assert not shell_read({"command": "Get-ChildItem \u2014Recurse", "shell": "powershell"})

    non_ascii_chars = (
        "\u2018", "\u2019", "\u201a", "\u201b",
        "\u201c", "\u201d", "\u201e", "\u201f",
        "\u2013", "\u2014", "\u2015",
    )
    for ch in non_ascii_chars:
        assert literal_command(f'Get-Content "note{ch}name.txt"', "powershell") is None
        assert literal_command(f"Get-ChildItem {ch}Name", "powershell") is None
        assert not shell_read({"command": f'Get-Content "note{ch}name.txt"', "shell": "powershell"})

    # Ordinary ASCII-quoted reads remain read-only
    assert shell_read({"command": "Get-Content 'readme.md'", "shell": "powershell"})
    assert shell_read({"command": 'Get-Content "readme.md"', "shell": "powershell"})
    assert shell_read({"command": "Get-ChildItem -Recurse", "shell": "powershell"})


def test_verified_read_classification_rejects_legacy_smart_quote_records():
    import hashlib

    args = {"command": 'Get-Content "x\u2019;Remove-Item y;\u2019"', "shell": "powershell"}
    args_sha = hashlib.sha256(encoded(args)).hexdigest()
    legacy_record = {
        "name": "ryn_shell_execute",
        "arguments": args,
        "arguments_sha256": args_sha,
        "authorization": "read",
        "operation_id": "op_legacy_smart_quote",
        "task_id": "task_1",
        "operation_classification": {
            "version": 1,
            "effect_class": "read",
            "basis": "literal_shell_read",
            "operation_id": "op_legacy_smart_quote",
            "task_id": "task_1",
            "tool": "ryn_shell_execute",
            "arguments_sha256": args_sha,
            "workspace_root": "",
            "goal_revision": 1,
            "scope_revision": 1,
            "context_epoch": 0,
            "control_revision": 1,
        },
    }
    assert not verified_read_classification(legacy_record)

    ascii_args = {"command": 'Get-Content "readme.md"', "shell": "powershell"}
    ascii_sha = hashlib.sha256(encoded(ascii_args)).hexdigest()
    valid_record = {
        **legacy_record,
        "arguments": ascii_args,
        "arguments_sha256": ascii_sha,
        "operation_classification": {
            **legacy_record["operation_classification"],
            "arguments_sha256": ascii_sha,
        },
    }
    assert verified_read_classification(valid_record)


def test_literal_read_command_double_guard_rejects_smart_quotes(monkeypatch):
    monkeypatch.setattr(execution_policy, "shell_read", lambda arguments: True)
    monkeypatch.setattr(execution_policy, "literal_command", lambda command, shell: ["Get-Content", "x\u2019y"])
    with pytest.raises(AgentToolRejected) as exc_info:
        local_processes.literal_read_command({"command": 'Get-Content "x\u2019y"', "shell": "powershell"})
    assert exc_info.value.args[0] == "agent_invalid_operation_classification"

