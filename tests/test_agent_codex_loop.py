"""Acceptance of the model-driven loop with real workstation IO."""
import copy
import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime.execution_policy import decide, shell_read
from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.operation_permissions import projection
from rynmesh.agent_runtime.protocol import digest


class WorkstationTools:
    """Use the production file/process tools, not a fake successful receipt."""
    names = ("ryn_shell_execute", "ryn_file_write", "ryn_file_read")

    def __init__(self, runtime, home):
        ledger = OperationLedger(home)
        self.local = LocalTools(SimpleNamespace(home=home), lambda: ledger)
        self.local.store = runtime.store

    def specs(self):
        return [{"type": "function", "function": {"name": name, "description": SPECS[name][0],
                "parameters": copy.deepcopy(SPECS[name][3])}} for name in self.names]

    def metadata(self, name):
        effect = SPECS[name][1]
        return {"effect": effect, "scope_context_supported": True, "preview_precondition": effect == "write"}

    def preview(self, name, args, *, context):
        return self.local.preview(name, args, context)

    def execute(self, name, args, *, operation_id, cancelled, context, expected_preview=None):
        return self.local.execute(name, args, operation_id=operation_id, cancelled=cancelled,
                                  context=context, expected_preview=expected_preview)

    def verification_specs(self):
        raise AssertionError("Normal turns must not start an independent reviewer")

    def verify(self, *args, **kwargs):
        raise AssertionError("Normal turns must not start an independent reviewer")


def workstation(tmp_path, *responses, mode="workspace"):
    workspace = tmp_path / "work"
    workspace.mkdir()
    runtime, model, _, _ = make_runtime(tmp_path / "private", *responses)
    runtime.tools = WorkstationTools(runtime, tmp_path / "private")
    runtime.update_settings({"workspace_root": str(workspace), "permission_mode": mode})
    return runtime, model, workspace


@pytest.mark.parametrize("mode", ["workspace", "confirm_changes", "read_only", "automatic"])
def test_literal_document_search_never_opens_approval(tmp_path, mode):
    runtime, model, workspace = workstation(tmp_path,
        assistant(name="ryn_shell_execute", args={"command": "Get-Content balance.txt" if os.name == "nt" else "cat balance.txt"}),
        assistant("米宝余额：123.45 元。"), mode=mode)
    (workspace / "balance.txt").write_text("Mibao balance: 123.45 CNY", encoding="utf-8")
    task = runtime.create({"message": "查一下米宝余额"})
    advance(runtime, 3)
    done = runtime.get(task["id"])
    assert done["status"] == "completed", done.get("error")
    assert "123.45" in done["result"]
    assert len(model.calls) == 2
    assert not any(e["type"] == "approval_requested" for e in done["events"])
    assert "123.45" in model.calls[-1][1][-1]["content"]
    receipt = runtime.store.read()["tasks"][task["id"]]["receipts"][0]
    assert receipt["authorization"] == "read"
    assert projection(runtime.store.read()["tasks"][task["id"]])[0]["permission_basis"] == "read_policy"


def test_balance_query_uses_real_http_and_shell_without_confirmation(tmp_path):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body = b'{"account":"mibao","balance":123.45,"currency":"CNY"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/balance"
        # Execute an actual local HTTP query through the production shell tool.
        command = (f"(Invoke-WebRequest -UseBasicParsing '{url}').Content" if os.name == "nt" else
                   f"curl --fail --silent '{url}'")
        runtime, model, _ = workstation(tmp_path,
            assistant("我会查询米宝的当前余额。", name="ryn_shell_execute", args={"command": command}),
            assistant("米宝余额为 123.45 元。"))
        task = runtime.create({"message": "查一下米宝余额"})
        advance(runtime, 3)
        done = runtime.get(task["id"])
        assert requests == ["/balance"]
        assert done["status"] == "completed", done.get("error")
        assert len(model.calls) == 2
        assert "123.45" in model.calls[-1][1][-1]["content"]
        assert not any(e["type"] in {"approval_requested", "verification_started", "change_review_requested"} for e in done["events"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_file_write_readback_and_final_use_one_conversation(tmp_path):
    runtime, model, workspace = workstation(tmp_path,
        assistant(name="ryn_file_write", args={"path": "report.txt", "content": "余额 123.45"}),
        assistant(name="ryn_file_read", args={"path": "report.txt"}),
        assistant("已保存并回读 report.txt。"))
    task = runtime.create({"message": "把余额保存为 report.txt 并检查内容"})
    advance(runtime, 5)
    done = runtime.get(task["id"])
    assert done["status"] == "completed", done.get("error")
    assert (workspace / "report.txt").read_text(encoding="utf-8") == "余额 123.45"
    assert len(model.calls) == 3
    assert done["outcome"]["status"] == "unverified"
    assert not any(e["type"] in {"approval_requested", "verification_started", "change_review_requested"} for e in done["events"])


def test_legacy_pending_search_is_released_without_owner_click(tmp_path):
    runtime, model, workspace = workstation(tmp_path,
        assistant(name="ryn_shell_execute", args={"command": "Get-Content balance.txt" if os.name == "nt" else "cat balance.txt"}),
        assistant("余额 123.45"), mode="confirm_changes")
    (workspace / "balance.txt").write_text("123.45", encoding="utf-8")
    task = runtime.create({"message": "查一下米宝余额"})
    advance(runtime)

    def old_dialog(data):
        row = data["tasks"][task["id"]]
        call = row["queue"][0]
        row.update(status="waiting_approval", pending_approval={"id": "legacy-dialog", "call_id": call["call_id"],
                   "arguments_sha256": digest(call["arguments"]), "tool": call["name"]})
    runtime.store.transaction(old_dialog)
    advance(runtime, 2)
    done = runtime.get(task["id"])
    assert done["status"] == "completed"
    assert done["pending_approval"] is None
    assert any(e["type"] == "approval_no_longer_required" for e in done["events"])
    assert len(model.calls) == 2


@pytest.mark.parametrize("command", [
    "rg -n -C 4 'balance|余额|baseUrl|localhost|127\\.0\\.0\\.1|Authorization|apiKey' docs/context.md src/controller.ts",
    'rg --files -g "*.py"', "Get-Content 'docs/my file.txt'", "Select-String -Path foo -Pattern 'a;b'",
])
def test_literal_shell_reads_include_the_reported_regression(command):
    assert shell_read({"command": command, "shell": "powershell"})


@pytest.mark.parametrize("command", [
    "rg balance docs; Remove-Item notes.txt", "rg balance docs | Set-Content notes.txt",
    "rg balance docs > result.txt", 'rg "$(Remove-Item notes.txt)" docs',
    'rg "`n" docs', "rg --pre=evil balance docs", "rg --hostname-bin=evil balance",
    "Get-Content foo\nRemove-Item bar", "& rg balance docs", "python query.py",
    "rg balance docs && echo ok", "rg $pattern docs", "rg 'unterminated", "rg balance `\n docs",
])
def test_shell_read_detection_never_whitelists_compound_or_executable_content(command):
    assert not shell_read({"command": command, "shell": "powershell"})


def test_strict_mode_still_confirms_mutation(tmp_path):
    runtime, _, workspace = workstation(tmp_path,
        assistant(name="ryn_file_write", args={"path": "report.txt", "content": "hello"}), mode="confirm_changes")
    task = runtime.create({"message": "Create report.txt"})
    advance(runtime, 2)
    assert runtime.get(task["id"])["status"] == "waiting_approval"
    assert not (workspace / "report.txt").exists()


def test_permission_revocation_between_proposal_and_dispatch_wins(tmp_path):
    runtime, _, workspace = workstation(tmp_path,
        assistant(name="ryn_file_write", args={"path": "report.txt", "content": "hello"}))
    task = runtime.create({"message": "Create report.txt"})
    advance(runtime)
    runtime.update_settings({"permission_mode": "read_only"})
    advance(runtime)
    assert not (workspace / "report.txt").exists()
    assert runtime.get(task["id"])["events"][-1]["result"]["error"] == "agent_permission_denied"


def test_output_limit_continues_instead_of_publishing_partial_answer(tmp_path):
    partial = {**assistant("第一部分，"), "finish_reason": "length"}
    runtime, model, _, _ = make_runtime(tmp_path, partial, assistant("余额为 123.45 元。"))
    task = runtime.create({"message": "查余额"})
    advance(runtime)
    assert runtime.get(task["id"])["status"] == "queued"
    advance(runtime)
    assert runtime.get(task["id"])["result"] == "第一部分，余额为 123.45 元。"
    assert len(model.calls) == 2


def test_external_access_change_is_not_granted_by_workspace_mode():
    row = {"permission_mode": "workspace", "allowed_write_tools": [], "approved": None}
    call = {"name": "ryn_set_friend_model_access", "arguments": {}}
    assert decide(call, {"effect": "write"}, row, row).action == "ask"
    assert decide({"name": "ryn_file_write", "arguments": {}},
                  {"effect": "write", "requires_explicit_approval": True}, row, row).action == "ask"


@pytest.mark.skipif(not shutil.which("rg"), reason="ripgrep is not installed")
def test_actual_ripgrep_search_is_automatic_in_legacy_strict_mode(tmp_path):
    runtime, model, workspace = workstation(tmp_path,
        assistant(name="ryn_shell_execute", args={"command": "rg --no-config -n 'balance|余额' context.txt"}),
        assistant("找到了余额查询说明。"), mode="confirm_changes")
    (workspace / "context.txt").write_text("balance: /balance", encoding="utf-8")
    task = runtime.create({"message": "查一下米宝余额"})
    advance(runtime, 3)
    assert runtime.get(task["id"])["status"] == "completed"
    assert '1:balance: /balance' in model.calls[-1][1][-1]["content"]


@pytest.mark.skipif(not shutil.which("rg"), reason="ripgrep is not installed")
def test_automatic_read_ignores_ripgrep_preprocessor_config(tmp_path, monkeypatch):
    config = tmp_path / "ripgrep.conf"
    config.write_text("--pre=ryn-must-not-run-this-executable", encoding="utf-8")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))
    runtime, model, workspace = workstation(tmp_path,
        assistant(name="ryn_shell_execute", args={"command": "rg -n balance context.txt"}),
        assistant("找到了查询说明。"), mode="confirm_changes")
    (workspace / "context.txt").write_text("balance: /balance", encoding="utf-8")
    task = runtime.create({"message": "查余额"})
    advance(runtime, 3)
    assert runtime.get(task["id"])["status"] == "completed"
    assert '1:balance: /balance' in model.calls[-1][1][-1]["content"]


def test_tool_failure_returns_to_the_same_model_for_correction(tmp_path):
    runtime, model, workspace = workstation(tmp_path,
        assistant(name="ryn_file_read", args={"path": "missing.txt"}),
        assistant(name="ryn_file_read", args={"path": "balance.txt"}),
        assistant("余额 123.45"))
    (workspace / "balance.txt").write_text("123.45", encoding="utf-8")
    task = runtime.create({"message": "查一下米宝余额"})
    advance(runtime, 5)
    assert runtime.get(task["id"])["status"] == "completed"
    assert json.loads(model.calls[1][1][-1]["content"])["no_mutation"] is True
    assert "123.45" in model.calls[2][1][-1]["content"]
