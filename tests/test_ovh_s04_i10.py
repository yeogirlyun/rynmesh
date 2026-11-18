"""S04-I10: owner-authored literal shell prefix rules at the real dispatch gate."""
import copy
import os

import pytest
from test_agent_runtime import make_runtime, assistant, advance
from rynmesh.agent_runtime.execution_policy import decide, classify_operation
from rynmesh.agent_runtime.operation_permissions import projection
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.agent_runtime.local_specs import SPECS
from test_agent_extensions_runtime import ext_env


class ShellTools:
    def __init__(self):
        self.calls = []

    def specs(self):
        return [{"type": "function", "function": {"name": "ryn_shell_execute", "description": "Synthetic shell boundary",
                "parameters": SPECS["ryn_shell_execute"][3]}}]

    def metadata(self, name):
        return {"effect": "write", "title": "Shell", "scope_context_supported": True, "preview_precondition": True}

    def preview(self, name, arguments, **kwargs):
        return {"command": arguments["command"], "cwd": "synthetic-workspace", "shell": arguments.get("shell", "powershell")}

    def execute(self, name, arguments, **kwargs):
        self.calls.append(copy.deepcopy(arguments))
        return {"exit_code": 0, "output": "synthetic shell completed"}


def shell_runtime(tmp_path, *commands):
    runtime, model, _, key = make_runtime(tmp_path, *(assistant(name="ryn_shell_execute", args={"command": command, "shell": "powershell"}) for command in commands))
    runtime.tools = ShellTools()
    runtime.update_settings({"permission_mode": "confirm_changes", "allowed_write_tools": []})
    return runtime, model, runtime.tools, key


def approve_prefix(runtime, task_id, **extra):
    row = runtime.get(task_id)
    return runtime.action(task_id, {"action": "approve", "approval_id": row["pending_approval"]["id"],
        "expected_control_revision": row["control_revision"], "remember_prefix": True, **extra})


def raw(runtime, task_id):
    return runtime.store.inspect(lambda data: copy.deepcopy(data["tasks"][task_id]))


def test_T1_remembered_prefix_runs_in_same_and_new_task_and_notifies_once(tmp_path):
    runtime, model, tools, key = shell_runtime(tmp_path, "git add one.txt", "git add two.txt", "git add three.txt", "git add four.txt")
    task_id = runtime.create({"message": "Stage requested files"})["id"]
    advance(runtime, 2)
    assert not tools.calls
    pending = runtime.get(task_id)["pending_approval"]
    assert pending["remember_prefix_candidate"] == ["git", "add"]
    approve_prefix(runtime, task_id)
    assert runtime.settings()["shell_allow_prefixes"] == [["git", "add"]]
    assert any(e["type"] == "prefix_allowed" and e["prefix"] == ["git", "add"] for e in runtime.get(task_id)["events"])
    advance(runtime, 5)
    row = raw(runtime, task_id)
    assert len(tools.calls) == 3
    assert row["receipts"][0]["authorization"] == "approved"
    assert all(r["authorization"] == r["permission_record"]["basis"] == "prefix_allow" for r in row["receipts"][1:])
    assert all(r["effect"] == "write" and r["operation_classification"]["effect_class"] == "write" for r in row["receipts"])
    notices = [r["result"].get("permission_notice") for r in row["receipts"] if r["result"].get("permission_notice")]
    assert len(notices) == 1 and "git add" in notices[0]
    assert projection(row)[-1]["permission_basis"] == "owner_prefix_rule"
    model.responses.insert(0, assistant("The first task is finished."))
    advance(runtime)
    reopened = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    next_id = reopened.create({"message": "Stage one more requested file"})["id"]
    advance(reopened, 2)
    assert len(tools.calls) == 4
    assert raw(reopened, next_id)["receipts"][0]["permission_record"]["basis"] == "prefix_allow"
    assert not raw(reopened, next_id)["receipts"][0]["result"].get("permission_notice")


@pytest.mark.parametrize("command", ["git add a; git add b", "git add a && git add b", "git add a | cat", "git add $(whoami)", "git add a\ngit add b"])
def test_T2_compound_commands_cannot_remember_or_match_prefix(tmp_path, command):
    runtime, _, tools, _ = shell_runtime(tmp_path, command)
    task_id = runtime.create({"message": "Stage files"})["id"]
    advance(runtime, 2)
    pending = runtime.get(task_id)["pending_approval"]
    assert not pending.get("remember_prefix_candidate")
    with pytest.raises(AgentError, match="agent_shell_prefix_invalid"):
        approve_prefix(runtime, task_id)
    assert not tools.calls
    assert not runtime.settings()["shell_allow_prefixes"]
    runtime.update_settings({"shell_allow_prefixes": [["git", "add"]]})
    row = raw(runtime, task_id)
    assert decide({"name": "ryn_shell_execute", "arguments": {"command": command, "shell": "powershell"}},
                  tools.metadata("ryn_shell_execute"), row, runtime.settings()).action == "ask"


@pytest.mark.parametrize("prefix", [["sh"], ["bash"], ["zsh"], ["cmd"], ["powershell"], ["pwsh"], ["python"],
    ["node"], ["npx"], ["rm"], ["del"], ["remove-item"], ["git"], ["git", "-C"], ["git", "add", "file"],
    [""], ["npm", "test;"], ["npm", "$(run)"], ["npm", "*"], ["npm", "'test'"], ["npm", '"test"'], ["a", "b", "c", "d"]])
def test_T3_settings_reject_broad_or_nonliteral_prefixes(tmp_path, prefix):
    runtime, _, _, _ = shell_runtime(tmp_path)
    with pytest.raises(AgentError, match="agent_invalid_settings"):
        runtime.update_settings({"shell_allow_prefixes": [prefix]})


def test_T3_prefix_normalization_deduplication_and_wrong_action_input(tmp_path):
    runtime, _, _, _ = shell_runtime(tmp_path, "python -c 'print(1)'")
    assert runtime.settings()["shell_allow_prefixes"] == []
    runtime.update_settings({"shell_allow_prefixes": [["GIT.EXE" if os.name == "nt" else "git", "add"], ["git", "add"], ["npm", "run", "test"]]})
    assert runtime.settings()["shell_allow_prefixes"] == [["git", "add"], ["npm", "run", "test"]]
    tid = runtime.create({"message": "Run the requested script"})["id"]
    advance(runtime, 2)
    with pytest.raises(AgentError, match="agent_shell_prefix_invalid"):
        approve_prefix(runtime, tid)
    with pytest.raises(AgentError, match="agent_invalid_request"):
        approve_prefix(runtime, tid, remember_prefix="true")


@pytest.mark.parametrize("mode,expected", [("read_only", "deny"), ("automatic", "execute")])
def test_T4_T5_readonly_rejects_and_automatic_keeps_original_basis(tmp_path, mode, expected):
    runtime, _, tools, _ = shell_runtime(tmp_path)
    runtime.update_settings({"permission_mode": mode, "shell_allow_prefixes": [["git", "add"]]})
    tid = runtime.create({"message": "Stage a requested file"})["id"]
    call = {"name": "ryn_shell_execute", "arguments": {"command": "git add one.txt", "shell": "powershell"}}
    decision = decide(call, tools.metadata(call["name"]), raw(runtime, tid), runtime.settings())
    assert decision.action == expected
    assert decision.basis == ("read_only" if mode == "read_only" else "automatic")
    assert classify_operation(call["name"], call["arguments"], tools.metadata(call["name"]))["effect_class"] == "write"


def test_T6_delete_prefix_restores_approval_for_existing_task(tmp_path):
    runtime, _, tools, _ = shell_runtime(tmp_path, "git add one.txt", "git add two.txt")
    runtime.update_settings({"shell_allow_prefixes": [["git", "add"]]})
    tid = runtime.create({"message": "Stage two requested files"})["id"]
    advance(runtime, 2)
    assert len(tools.calls) == 1
    runtime.update_settings({"shell_allow_prefixes": []})
    advance(runtime, 2)
    assert len(tools.calls) == 1
    assert runtime.get(tid)["status"] == "waiting_approval"


def test_T7_permission_projection_requires_the_existing_receipt_binding(tmp_path):
    runtime, _, _, _ = shell_runtime(tmp_path, "git add a")
    runtime.update_settings({"shell_allow_prefixes": [["git", "add"]]})
    tid = runtime.create({"message": "Stage a"})["id"]
    advance(runtime, 2)
    row = raw(runtime, tid)
    assert projection(row)[0]["permission_basis"] == "owner_prefix_rule"
    row["receipts"][0]["permission_record"]["arguments_sha256"] = "forged"
    assert projection(row)[0]["permission_basis"] == "unknown"


def test_prefix_rule_matches_words_not_text_and_only_normalizes_executable(tmp_path):
    runtime, _, tools, _ = shell_runtime(tmp_path)
    runtime.update_settings({"shell_allow_prefixes": [["git", "add"]]})
    tid = runtime.create({"message": "Stage"})["id"]
    row = raw(runtime, tid)
    executable = "GIT.EXE" if os.name == "nt" else "git"
    for command, basis in [(f'{executable} add "one file.txt"', "prefix_allow"), ("git additive a", "confirm_changes"), ("git ADD a", "confirm_changes"), ("git add", "prefix_allow")]:
        call = {"name": "ryn_shell_execute", "arguments": {"command": command, "shell": "powershell"}}
        assert decide(call, tools.metadata(call["name"]), row, runtime.settings()).basis == basis


def test_T1_two_pending_approvals_deduplicate_transactional_prefix_append(tmp_path):
    runtime, _, _, _ = shell_runtime(tmp_path, "git add a", "git add b")
    first = runtime.create({"message": "First staging request"})["id"]
    second = runtime.create({"message": "Second staging request"})["id"]
    advance(runtime, 4)
    approve_prefix(runtime, first)
    approve_prefix(runtime, second)
    assert runtime.settings()["shell_allow_prefixes"] == [["git", "add"]]
    events = [e for tid in (first, second) for e in runtime.get(tid)["events"] if e["type"] == "prefix_allowed"]
    assert len(events) == 1


def test_T1_settings_added_rule_notifies_only_its_first_matching_result(tmp_path):
    runtime, _, _, _ = shell_runtime(tmp_path, "git add a", "npm test", "npm test -- --run", "git add b")
    runtime.update_settings({"shell_allow_prefixes": [["git", "add"]]})
    tid = runtime.create({"message": "Stage and test"})["id"]
    advance(runtime, 2)
    runtime.update_settings({"shell_allow_prefixes": [["git", "add"], ["npm", "test"]]})
    advance(runtime, 6)
    receipts = raw(runtime, tid)["receipts"]
    assert "git add" in receipts[0]["result"]["permission_notice"]
    assert "npm test" in receipts[1]["result"]["permission_notice"]
    assert not any(r["result"].get("permission_notice") for r in receipts[2:])


def test_T1_real_local_shell_writes_only_after_approval_then_prefix_reuse(ext_env):
    runtime, model, _, workspace, _ = ext_env
    shell = "powershell" if os.name == "nt" else "sh"
    commands = [f"New-Item -ItemType File -Path {name}" if os.name == "nt" else f"touch -- {name}"
                for name in ("prefix-one.txt", "prefix-two.txt")]
    model.responses = [assistant(name="ryn_shell_execute", args={"command": command, "shell": shell}) for command in commands] + [assistant("done")]
    tid = runtime.create({"message": "Create the two requested empty files"})["id"]
    advance(runtime, 2)
    assert runtime.get(tid)["status"] == "waiting_approval"
    assert not (workspace / "prefix-one.txt").exists()
    approve_prefix(runtime, tid)
    advance(runtime, 5)
    assert (workspace / "prefix-one.txt").is_file()
    assert (workspace / "prefix-two.txt").is_file()
    assert [r["authorization"] for r in raw(runtime, tid)["receipts"]] == ["approved", "prefix_allow"]


def test_T5_prefix_settings_do_not_interrupt_an_automatic_task(tmp_path):
    runtime, _, _, _ = shell_runtime(tmp_path)
    runtime.update_settings({"permission_mode": "automatic"})
    tid = runtime.create({"message": "Continue automatic work"})["id"]
    def running(data):
        data["tasks"][tid].update(status="running", in_flight={"kind": "model", "dispatched": True})
    runtime.store.transaction(running)
    runtime.update_settings({"shell_allow_prefixes": [["npm", "test"]]})
    assert not raw(runtime, tid).get("pending_control")
    assert raw(runtime, tid)["status"] == "running"
