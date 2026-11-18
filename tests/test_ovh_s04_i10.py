"""S04-I10: owner-authored literal shell prefix rules at the real dispatch gate."""
import copy
import os

import pytest
from test_agent_runtime import make_runtime, assistant, advance
from rynmesh.agent_runtime.execution_policy import decide, classify_operation
from rynmesh.agent_runtime.execution_policy import normalize_shell_prefix, shell_prefix_candidate
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
    assert pending["remember_prefix_candidate"] == {"prefix": ["git", "add"], "options": []}
    approve_prefix(runtime, task_id)
    assert runtime.settings()["shell_allow_prefixes"] == [{"prefix": ["git", "add"], "options": []}]
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
    assert runtime.settings()["shell_allow_prefixes"] == [{"prefix": ["git", "add"], "options": []}, {"prefix": ["npm", "run", "test"], "options": []}]
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
    assert runtime.settings()["shell_allow_prefixes"] == [{"prefix": ["git", "add"], "options": []}]
    events = [e for tid in (first, second) for e in runtime.get(tid)["events"] if e["type"] == "prefix_allowed"]
    assert len(events) == 1


def test_T1_settings_added_rule_notifies_only_its_first_matching_result(tmp_path):
    runtime, _, _, _ = shell_runtime(tmp_path, "git add a", "npm test", "npm test another-case", "git add b")
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
    (workspace / "prefix-source.txt").write_text("synthetic source", encoding="utf-8")
    commands = [f"Copy-Item prefix-source.txt {name}" if os.name == "nt" else f"cp prefix-source.txt {name}"
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


def prefix_decision(command, prefix, *, shell="powershell"):
    return decide({"name": "ryn_shell_execute", "arguments": {"command": command, "shell": shell}},
                  {"effect": "write"}, {"permission_mode": "confirm_changes"},
                  {"permission_mode": "confirm_changes", "shell_allow_prefixes": [prefix]})


@pytest.mark.parametrize("command,prefix", [
    ("sudo rm -rf /", ["sudo", "rm"]),
    ("ri -Recurse -Force C:/Users/x", ["ri", "-Recurse"]),
    ("rd /s /q C:/Users/x", ["rd", "/s"]),
    ("erase a.txt -Force", ["erase", "a.txt"]),
    ("Start-Process https://example.test/", ["start-process", "https://example.test/"]),
])
def test_rework_dangerous_commands_never_use_even_an_old_prefix(command, prefix):
    assert prefix_decision(command, prefix).action == "ask"


REWORK_BROAD_PROGRAMS = ("env sudo doas timeout nice nohup time xargs exec eval command busybox wsl "
    "python3 python3.11 py perl ruby php lua deno bun dash ksh fish csh tcsh osascript cscript wscript "
    "mshta iex invoke-expression ri rd rmdir erase").split()


@pytest.mark.parametrize("program", REWORK_BROAD_PROGRAMS)
def test_rework_wrappers_interpreters_and_delete_aliases_have_no_prefix(program):
    assert normalize_shell_prefix([program, "literal"]) is None
    assert shell_prefix_candidate({"command": f"{program} literal payload", "shell": "sh"}) is None
    assert prefix_decision(f"{program} literal payload", [program, "literal"], shell="sh").action == "ask"


@pytest.mark.parametrize("command", [
    "env CI=true sh -c 'echo arbitrary'", "timeout 30 bash -c 'echo arbitrary'", "python3 -c 'print(1)'",
    "python3.12 -c 'print(1)'", "py -c 'print(1)'", "perl -e 'print 1'", "dash -c 'echo arbitrary'",
    "wsl bash -c 'echo arbitrary'", "NODE_ENV=test npm publish", "npm -c payload", "npm /c payload",
])
def test_rework_specific_wrapper_assignment_and_second_flag_examples(command):
    assert shell_prefix_candidate({"command": command, "shell": "sh"}) is None


@pytest.mark.parametrize("command", [
    "git config core.sshCommand evil", "git CONFIG core.sshCommand evil", "git submodule foreach 'echo arbitrary'", "git bisect run evil",
    "git filter-branch tree-filter", "git difftool file.txt", "git mergetool file.txt",
])
def test_rework_git_execution_subcommands_cannot_be_remembered(command):
    prefix = command.split()[:2]
    assert normalize_shell_prefix(prefix) is None
    assert shell_prefix_candidate({"command": command, "shell": "sh"}) is None
    assert prefix_decision(command, prefix, shell="sh").action == "ask"


@pytest.mark.parametrize("command,prefix", [
    ("git fetch --upload-pack='echo arbitrary' /tmp/local", ["git", "fetch"]),
    ("git fetch --receive-pack=evil /tmp/local", ["git", "fetch"]),
    ("git rebase -x 'echo arbitrary'", ["git", "rebase"]),
    ("git rebase --exec=evil main", ["git", "rebase"]),
    ("git rebase -ixevil main", ["git", "rebase"]),
    ("git grep -Oevil needle", ["git", "grep"]),
    ("git grep --open-files-in-pager=evil needle", ["git", "grep"]),
    ("git diff --output=out.patch", ["git", "diff"]),
    ("git diff --ext-diff file.txt", ["git", "diff"]),
    ("git push --force origin main", ["git", "push"]),
    ("git PUSH --force origin main", ["git", "PUSH"]),
    ("git push --force-with-lease origin main", ["git", "push"]),
    ("git push --mirror origin", ["git", "push"]),
    ("git push --delete origin main", ["git", "push"]),
    ("git push -vf origin main", ["git", "push"]),
    ("git push -d origin main", ["git", "push"]),
])
def test_rework_git_exec_output_and_force_flags_still_ask(command, prefix):
    assert prefix_decision(command, prefix, shell="sh").action == "ask"


@pytest.mark.parametrize("executable", ["/tmp/npm", "C:/temp/npm.cmd", r".\tools\npm.cmd"])
def test_rework_path_qualified_program_cannot_borrow_a_bare_name_rule(executable):
    assert normalize_shell_prefix([executable, "test"]) is None
    assert shell_prefix_candidate({"command": f"{executable} test", "shell": "powershell"}) is None
    assert prefix_decision(f"{executable} test", ["npm", "test"]).action == "ask"


@pytest.mark.parametrize("mode", ["workspace", "confirm_changes"])
def test_rework_explicit_approval_source_has_no_prefix_option(tmp_path, mode):
    runtime, _, tools, _ = shell_runtime(tmp_path, "git add one.txt")
    runtime.update_settings({"permission_mode": mode})
    metadata = tools.metadata
    tools.metadata = lambda name: {**metadata(name), "requires_explicit_approval": True}
    tid = runtime.create({"message": "Prepare a host-required approval"})["id"]
    advance(runtime, 2)
    assert not runtime.get(tid)["pending_approval"].get("remember_prefix_candidate")
    with pytest.raises(AgentError, match="agent_shell_prefix_invalid"):
        approve_prefix(runtime, tid)


@pytest.mark.parametrize("basis", [None, "dangerous_command", "explicit_approval"])
def test_rework_action_rejects_unsupported_or_legacy_approval_source(tmp_path, basis):
    runtime, _, _, _ = shell_runtime(tmp_path, "git add one.txt")
    tid = runtime.create({"message": "Stage one file"})["id"]
    advance(runtime, 2)
    def source(data):
        data["tasks"][tid]["pending_approval"]["approval_basis"] = basis
    runtime.store.transaction(source)
    with pytest.raises(AgentError, match="agent_shell_prefix_invalid"):
        approve_prefix(runtime, tid)


def test_rework_action_rejects_nonconfirmation_task_mode(tmp_path):
    runtime, _, _, _ = shell_runtime(tmp_path, "git add one.txt")
    tid = runtime.create({"message": "Stage one file"})["id"]
    advance(runtime, 2)
    def mode(data):
        data["tasks"][tid]["permission_mode"] = "workspace"
    runtime.store.transaction(mode)
    with pytest.raises(AgentError, match="agent_shell_prefix_invalid"):
        approve_prefix(runtime, tid)


@pytest.mark.parametrize("command,prefix", [
    ("git fetch --upload-pa='touch M;' ../o.git", ["git", "fetch"]),
    ("git rebase --exe 'touch M' HEAD~1", ["git", "rebase"]),
    ("git clone -u 'touch M' remote", ["git", "clone"]),
    ("git clone --config core.sshCommand=evil remote", ["git", "clone"]),
    ("git push origin +main", ["git", "push"]),
    ("git push origin :feature", ["git", "push"]),
    ("git push --dele origin feature", ["git", "push"]),
    ("git push --mirro origin", ["git", "push"]),
    ("git clean -fd", ["git", "clean"]),
    ("git branch -D x", ["git", "branch"]),
    ("git stash clear", ["git", "stash"]),
    ("git reset --hard HEAD~1", ["git", "reset"]),
    ("git checkout -- .", ["git", "checkout"]),
    ("npm test --node-options=evil", ["npm", "test"]),
    ("npm test --script-shell=evil", ["npm", "test"]),
    ("npm exec -c 'touch M'", ["npm", "exec"]),
    ("tar tf a.tar --checkpoint-action=exec=evil", ["tar", "tf"]),
    ("tar tf a.tar --to-command=evil", ["tar", "tf"]),
    ("sed s/a/b/ f -e '1e touch M'", ["sed", "s/a/b/"]),
])
def test_rework2_changed_options_and_execution_forms_do_not_expand_old_grants(command, prefix):
    assert prefix_decision(command, prefix, shell="sh").action == "ask"


@pytest.mark.parametrize("command", [
    "git clean -n", "git branch -a", "git stash list", "git reset HEAD f", "git checkout main",
    "git rebase main", "git clone remote", "git grep needle", "git ls-remote origin", "tar tf a.tar",
    "sed s/a/b/ f", "npm exec eslint", "npm x eslint", "pnpm exec eslint", "pnpm dlx eslint",
    "yarn exec eslint", "yarn dlx eslint", "uv run pytest", "poetry run pytest", "pipenv run pytest",
    "conda run pytest", "SUDO rm file",
])
def test_rework2_only_fixed_git_subcommands_and_nonrunner_programs_have_candidates(command):
    assert shell_prefix_candidate({"command": command, "shell": "sh"}) is None


@pytest.mark.parametrize("approved,reused,changed,prefix,options", [
    ("pytest -q tests/a", "pytest -q tests/b", "pytest -x tests/b", ["pytest"], ["-q"]),
    ("git commit -m x", "git commit -m y", "git commit --amend -m y", ["git", "commit"], ["-m"]),
])
def test_rework2_approved_option_words_are_saved_and_enforced(tmp_path, approved, reused, changed, prefix, options):
    runtime, _, tools, _ = shell_runtime(tmp_path, approved, reused, changed)
    tid = runtime.create({"message": "Run these requested operations"})["id"]
    advance(runtime, 2)
    candidate = runtime.get(tid)["pending_approval"]["remember_prefix_candidate"]
    assert candidate == {"prefix": prefix, "options": options}
    approve_prefix(runtime, tid)
    assert runtime.settings()["shell_allow_prefixes"] == [{"prefix": prefix, "options": options}]
    advance(runtime, 5)
    assert len(tools.calls) == 2
    assert raw(runtime, tid)["receipts"][1]["authorization"] == "prefix_allow"
    assert runtime.get(tid)["status"] == "waiting_approval"


@pytest.mark.parametrize("tail", ["--", "SHELL=evil", "+main", ":feature", "--unknown"])
def test_rework2_tail_syntax_and_unrecorded_options_always_ask(tail):
    assert prefix_decision(f"git add {tail}", ["git", "add"], shell="sh").action == "ask"


@pytest.mark.parametrize("command,prefix,option", [
    ("git fetch --upload-pa 'touch M' ../o.git", ["git", "fetch"], "--upload-pa"),
    ("git rebase --exe 'touch M' HEAD~1", ["git", "rebase"], "--exe"),
    ("git push --dele origin feature", ["git", "push"], "--dele"),
    ("git push --mirro origin", ["git", "push"], "--mirro"),
])
def test_rework2_known_unsafe_option_abbreviations_cannot_be_registered(tmp_path, command, prefix, option):
    runtime, _, _, _ = shell_runtime(tmp_path)
    assert shell_prefix_candidate({"command": command, "shell": "sh"}) is None
    with pytest.raises(AgentError, match="agent_invalid_settings"):
        runtime.update_settings({"shell_allow_prefixes": [{"prefix": prefix, "options": [option]}]})


def test_rework2_two_approved_option_sets_merge_without_broadening_other_flags(tmp_path):
    runtime, _, tools, _ = shell_runtime(tmp_path, "pytest -q tests/a", "pytest -x tests/b")
    first = runtime.create({"message": "First test run"})["id"]
    second = runtime.create({"message": "Second test run"})["id"]
    advance(runtime, 4)
    approve_prefix(runtime, first)
    approve_prefix(runtime, second)
    advance(runtime, 2)
    assert len(tools.calls) == 2
    rules = runtime.settings()["shell_allow_prefixes"]
    assert rules == [{"prefix": ["pytest"], "options": ["-q", "-x"]}]
    row = raw(runtime, first)
    for command, expected in [("pytest -q tests/b", "execute"), ("pytest -x tests/a", "execute"), ("pytest -v tests/a", "ask")]:
        assert decide({"name": "ryn_shell_execute", "arguments": {"command": command, "shell": "powershell"}},
                      tools.metadata("ryn_shell_execute"), row, runtime.settings()).action == expected


def test_rework2_legacy_saved_words_have_no_implicit_option_allowance(tmp_path):
    runtime, _, tools, _ = shell_runtime(tmp_path)
    runtime.store.transaction(lambda data: data["settings"].update(shell_allow_prefixes=[["git", "add"]]))
    assert runtime.settings()["shell_allow_prefixes"] == [{"prefix": ["git", "add"], "options": []}]
    tid = runtime.create({"message": "Stage one file"})["id"]
    for command, expected in [("git add file.txt", "execute"), ("git add -A", "ask")]:
        assert decide({"name": "ryn_shell_execute", "arguments": {"command": command, "shell": "powershell"}},
                      tools.metadata("ryn_shell_execute"), raw(runtime, tid), runtime.settings()).action == expected


@pytest.mark.parametrize("options", [["--"], ["--node-options=evil"], ["q"], "-q", [None]])
def test_rework2_option_schema_rejects_nonliteral_or_nonoption_words(tmp_path, options):
    runtime, _, _, _ = shell_runtime(tmp_path)
    with pytest.raises(AgentError, match="agent_invalid_settings"):
        runtime.update_settings({"shell_allow_prefixes": [{"prefix": ["pytest"], "options": options}]})


@pytest.mark.parametrize("command,prefix", [
    ("git diff --ext-di file.txt", ["git", "diff"]),
    ("git log --out=unsafe.txt", ["git", "log"]),
])
def test_rework2_unsafe_git_option_abbreviations_are_not_classified_as_reads(command, prefix):
    assert prefix_decision(command, prefix, shell="sh").action == "ask"


@pytest.mark.parametrize("command", ["git log -Gfoo", "git log -Sfox"])
def test_rework2_safe_git_search_option_values_keep_read_classification(tmp_path, command):
    from rynmesh.agent_runtime.execution_policy import shell_read
    assert shell_read({"command": command, "shell": "sh", "cwd": str(tmp_path)}, workspace_root=str(tmp_path))
