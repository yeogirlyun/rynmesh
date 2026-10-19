"""Real local execution boundaries, concurrent progress and private diagnostics."""
import errno
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_agent_integrations import add_source, call, fake_upstream, passed_checker
from test_agent_integrations import node as node
from test_agent_local_tools import execute
from test_agent_local_tools import local as local

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.diagnostic_log import failure
from rynmesh.agent_runtime.local_processes import AgentProcessStartRejected
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore, AgentToolRejected
from rynmesh.services import peer_box


def test_foreground_shell_does_not_block_an_independent_file_operation(local, monkeypatch):
    tools, context, workspace, _ = local
    waiting = threading.Event()
    original = tools.processes.wait

    def wait(*args, **kwargs):
        waiting.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(tools.processes, "wait", wait)
    command = "Start-Sleep 30" if os.name == "nt" else "sleep 30"
    with ThreadPoolExecutor(max_workers=2) as pool:
        shell = pool.submit(execute, tools, context, "ryn_shell_execute", {"command": command}, "slow")
        try:
            assert waiting.wait(5), "shell did not reach its foreground wait"
            write = pool.submit(execute, tools, context, "ryn_file_write",
                                {"path": "independent.txt", "content": "progress"}, "file")
            assert write.result(timeout=2)["written"]
            assert (workspace / "independent.txt").read_text() == "progress"
            assert not shell.done()
        finally:
            tools.processes.close()
        shell.result(timeout=5)


def test_concurrent_delivery_of_same_operation_executes_exactly_once(local):
    tools, context, workspace, _ = local
    command = ("Add-Content counter.txt x; Start-Sleep -Milliseconds 100" if os.name == "nt"
               else "echo x >>counter.txt; sleep 0.1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(execute, tools, context, "ryn_shell_execute", {"command": command}, "same")
                   for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0] == results[1]
    assert (workspace / "counter.txt").read_text().count("x") == 1
    assert not tools._operation_locks


def test_missing_rg_is_recoverable_before_dispatch_and_does_not_snapshot(local, monkeypatch):
    from rynmesh.agent_runtime import local_processes, workspace_artifacts
    tools, context, _, _ = local
    real_which = local_processes.shutil.which
    monkeypatch.setattr(local_processes.shutil, "which", lambda name: None if name.lower() in {"rg", "rg.exe"} else real_which(name))
    monkeypatch.setattr(workspace_artifacts, "snapshot", lambda *_: pytest.fail("read-only rg must not inventory workspace"))
    with pytest.raises(AgentToolRejected, match="agent_ripgrep_unavailable_use_file_search"):
        execute(tools, context, "ryn_shell_execute", {"command": "rg -n 'balance_query' ."}, "missing-rg")
    assert tools.ledger_factory().get("missing-rg") is None
    assert not tools.processes.items


def test_agent_continues_with_native_search_after_rg_is_missing(node, monkeypatch, tmp_path):
    from rynmesh.agent_runtime import local_processes
    app, owner_store = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, owner_store, owner_store.home)
    runtime = AgentRuntime(AgentStore(owner_store.home / "missing-rg-agent",
        peer_box.load_or_create_messaging_key(owner_store.home / "messaging.x25519")), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": model_id, "workspace_root": str(tmp_path)})
    (tmp_path / "balance.py").write_text("balance_query = 1\n", encoding="utf-8")
    real_which = local_processes.shutil.which
    monkeypatch.setattr(local_processes.shutil, "which", lambda name: None if name.lower() in {"rg", "rg.exe"} else real_which(name))
    requests = fake_upstream(monkeypatch, [
        call("ryn_shell_execute", json.dumps({"command": "rg -n balance_query ."}), "rg"),
        call("ryn_file_search", json.dumps({"path": ".", "query": "balance_query"}), "native"),
        {"role": "assistant", "content": "balance.py contains balance_query on line 1."}, passed_checker])
    created = runtime.create({"message": "Find balance_query in this workspace."})
    for _ in range(12):
        runtime.run_once()
        current = runtime.get(created["id"])
        if current["status"] in {"completed", "failed", "interrupted"}:
            break
    assert current["status"] == "completed", current.get("error")
    results = [event for event in current["events"] if event["type"] == "tool_result"]
    rejected = next(event["result"] for event in results if event.get("tool") == "ryn_shell_execute")
    assert rejected["not_executed"] is True and rejected["no_mutation"] is True
    assert "ryn_file_search" in rejected["correction"]
    assert any(event.get("tool") == "ryn_file_search" and event["result"].get("matches") for event in results)
    assert any("agent_ripgrep_unavailable_use_file_search" in str(message.get("content", ""))
               for message in requests[1]["messages"])
    assert not any(event["type"] == "interrupted" for event in current["events"])


def test_process_start_rejection_is_durably_not_executed(local, monkeypatch):
    tools, context, _, _ = local

    def fail_start(*_args, **_kwargs):
        raise AgentProcessStartRejected("agent_process_start_failed")

    monkeypatch.setattr(tools.processes, "start", fail_start)
    with pytest.raises(AgentProcessStartRejected):
        execute(tools, context, "ryn_shell_execute", {"command": "echo hello"}, "no-start")
    assert tools.ledger_factory().get("no-start")["status"] == "not_executed"
    binding = tools.ledger_factory().get("no-start")["binding"]
    reconciled = tools.reconcile("no-start", {**context, "operation": binding})
    assert reconciled["outcome"] == "not_executed"
    assert reconciled["receipt"]["result"]["no_mutation"] is True
    assert tools.reconcile("no-start", {**context, "operation": {**binding, "tool": "different"}})["outcome"] == "unknown"
    with pytest.raises(AgentToolRejected, match="agent_process_start_failed"):
        execute(tools, context, "ryn_shell_execute", {"command": "echo hello"}, "no-start")


def test_error_after_start_remains_unknown_and_is_never_replayed(local, monkeypatch):
    tools, context, _, _ = local
    calls = []

    def fail_after_start(*_args, **_kwargs):
        calls.append(1)
        raise OSError(errno.EIO, "private command or credential")

    monkeypatch.setattr(tools.processes, "start", fail_after_start)
    with pytest.raises(AgentError, match="agent_local_operation_unconfirmed"):
        execute(tools, context, "ryn_shell_execute", {"command": "echo hello"}, "uncertain")
    assert tools.ledger_factory().get("uncertain")["status"] == "dispatched"
    with pytest.raises(AgentError, match="agent_write_outcome_unknown"):
        execute(tools, context, "ryn_shell_execute", {"command": "echo hello"}, "uncertain")
    assert calls == [1]


def test_watch_authority_uses_small_projection_and_sees_revocation(local, monkeypatch):
    tools, context, _, state = local
    # A background process must retain cancellation authority without reading
    # any complete transcript, even after its original dispatch is confirmed.
    context = {**context, "operation_id": "watch"}
    state.transaction(lambda data: data["tasks"]["task"].update(
        receipts=[{"operation_id": "watch", "tool": "ryn_shell_execute", "effect": "write",
                   "arguments": {"command": "Get-Location"}, "authorization": "read"}]))
    monkeypatch.setattr(state, "read", lambda: pytest.fail("watcher cloned full task history"))
    allowed = tools.allowed(context)
    assert allowed()
    state.transaction(lambda data: data["tasks"]["task"].update(status="cancelled"))
    assert not allowed()


def test_failure_diagnostics_keep_cause_and_errno_without_private_exception_text(caplog):
    caplog.set_level(logging.WARNING, logger="rynmesh.agent_runtime.diagnostic_log")
    private = "PRIVATE_PROMPT_PATH_KEY_CANARY"
    try:
        try:
            raise OSError(errno.EACCES, private)
        except OSError as exc:
            raise AgentError("agent_local_operation_unconfirmed") from exc
    except AgentError as exc:
        failure("local_execute", exc, elapsed_seconds=0.25)
    assert private not in caplog.text
    assert "agent_local_operation_unconfirmed" in caplog.text
    assert "PermissionError" in caplog.text
    assert "errno" in caplog.text


def _make_node_runtime(node, tmp_path, store_name="stage01-agent"):
    app, owner_store = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, owner_store, owner_store.home)
    key = peer_box.load_or_create_messaging_key(owner_store.home / "messaging.x25519")
    store = AgentStore(owner_store.home / store_name, key)
    runtime = AgentRuntime(store, bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": model_id, "workspace_root": str(tmp_path)})
    return runtime, store, bridge, tools, catalog, model_id


def test_normal_read_only_shell_records_verified_classification_and_confirms(node, monkeypatch, tmp_path):
    from rynmesh.agent_runtime.execution_policy import verified_read_classification

    runtime, store, _, tools, _, _ = _make_node_runtime(node, tmp_path, "normal-read-shell")
    runtime.update_settings({"permission_mode": "read_only"})
    read_cmd = "Get-Location" if os.name == "nt" else "pwd"
    fake_upstream(
        monkeypatch,
        [
            call("ryn_shell_execute", json.dumps({"command": read_cmd}), "read_loc"),
            {"role": "assistant", "content": "Checked current location."},
            passed_checker,
        ],
    )
    created = runtime.create({"message": "Check current workspace location."})
    for _ in range(10):
        runtime.run_once()
        current = runtime.get(created["id"])
        if current["status"] in {"completed", "failed", "interrupted", "waiting_approval"}:
            break
    assert current["status"] == "completed", current.get("error")
    raw_row = store.read()["tasks"][created["id"]]
    receipt = next(r for r in raw_row["receipts"] if r["tool"] == "ryn_shell_execute")
    assert receipt["status"] == "confirmed"
    assert receipt["authorization"] == "read"
    assert verified_read_classification(
        receipt,
        task_id=created["id"],
        workspace_root=str(tmp_path),
        operation_id=receipt["operation_id"],
        tool="ryn_shell_execute",
        arguments={"command": read_cmd},
    )
    ledger_row = tools.local.ledger_factory().get(receipt["operation_id"])
    assert ledger_row["status"] == "confirmed"
    assert ledger_row["operation_classification"] == receipt["operation_classification"]


def test_read_only_shell_exception_and_timeout_produce_error_receipt_and_continue(node, monkeypatch, tmp_path):
    runtime, store, _, tools, _, _ = _make_node_runtime(node, tmp_path, "exc-timeout-read-shell")
    (tmp_path / "balance.txt").write_text("balance=42\n", encoding="utf-8")
    read_cmd_1 = "Get-Location" if os.name == "nt" else "pwd"
    read_cmd_2 = "Get-ChildItem" if os.name == "nt" else "ls"
    real_wait = tools.local.processes.wait
    calls = {"count": 0}

    def flaky_wait(ident, task_id, cancelled):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError(errno.EIO, "transient pipe read error")
        if calls["count"] == 2:
            tools.local.processes.stop(ident, task_id)
            res = real_wait(ident, task_id, cancelled)
            return {**res, "running": False, "stop_reason": "timeout"}
        return real_wait(ident, task_id, cancelled)

    monkeypatch.setattr(tools.local.processes, "wait", flaky_wait)
    requests = fake_upstream(
        monkeypatch,
        [
            call("ryn_shell_execute", json.dumps({"command": read_cmd_1}), "call_exc"),
            call("ryn_shell_execute", json.dumps({"command": read_cmd_2, "timeout_seconds": 5}), "call_timeout"),
            call("ryn_file_read", json.dumps({"path": "balance.txt"}), "call_file"),
            {"role": "assistant", "content": "Read balance=42 after shell read errors."},
            passed_checker,
        ],
    )
    created = runtime.create({"message": "Inspect workspace and report balance.txt."})
    for _ in range(16):
        runtime.run_once()
        current = runtime.get(created["id"])
        if current["status"] in {"completed", "failed", "interrupted"}:
            break
    assert current["status"] == "completed", current.get("error")
    assert not any(e["type"] == "interrupted" for e in current["events"])
    raw_row = store.read()["tasks"][created["id"]]
    shell_receipts = [r for r in raw_row["receipts"] if r["tool"] == "ryn_shell_execute"]
    assert len(shell_receipts) == 2
    assert shell_receipts[0]["status"] == "failed"
    assert shell_receipts[0]["result"] == {"error": "agent_read_interrupted", "not_executed": False, "no_mutation": True}
    assert shell_receipts[1]["status"] == "failed"
    assert shell_receipts[1]["result"] == {"error": "agent_shell_timeout", "not_executed": False, "no_mutation": True}
    for r in shell_receipts:
        ledger_entry = tools.local.ledger_factory().get(r["operation_id"])
        assert ledger_entry["status"] == "failed"
        assert ledger_entry["no_mutation"] is True
        assert ledger_entry["cleanup_confirmed"] is True
        assert "receipt_reserve" not in ledger_entry
        assert not tools.local.processes.running_for_operation(created["id"], r["operation_id"])
    assert any("agent_read_interrupted" in str(m.get("content", "")) for m in requests[1]["messages"])
    assert any("agent_shell_timeout" in str(m.get("content", "")) for m in requests[2]["messages"])


def test_restart_during_read_only_shell_settles_error_receipt_and_continues(node, monkeypatch, tmp_path):
    runtime, store, bridge, tools, catalog, _ = _make_node_runtime(node, tmp_path, "restart-read-shell")
    (tmp_path / "info.txt").write_text("ok=1\n", encoding="utf-8")
    read_cmd = "Get-Location" if os.name == "nt" else "pwd"
    requests = fake_upstream(
        monkeypatch,
        [
            call("ryn_shell_execute", json.dumps({"command": read_cmd}), "call_restart_read"),
            call("ryn_file_read", json.dumps({"path": "info.txt"}), "call_after_restart"),
            {"role": "assistant", "content": "Recovered after node restart and read info.txt."},
            passed_checker,
        ],
    )
    created = runtime.create({"message": "Check location then read info.txt."})
    # Step 1: model proposes read-only ryn_shell_execute
    runtime.run_once()
    # Simulate crash/restart right after authorize() recorded in_flight and ledger dispatched
    queued_row = store.read()["tasks"][created["id"]]
    queued_call = queued_row["queue"][0]
    from rynmesh.agent_runtime import verification
    from rynmesh.agent_runtime.execution_policy import classify_operation
    from rynmesh.agent_runtime.protocol import digest, model_retry_authority, now

    classification = classify_operation(
        queued_call["name"],
        queued_call["arguments"],
        tools.metadata(queued_call["name"]),
        operation_id=queued_call["operation_id"],
        task_id=created["id"],
        workspace_root=str(tmp_path),
        goal_revision=queued_row["goal"]["revision"],
        scope_revision=queued_row["scope_revision"],
        context_epoch=queued_row.get("context_epoch", 0),
        control_revision=queued_row["control_revision"],
    )
    binding = {
        "operation_id": queued_call["operation_id"],
        "tool": queued_call["name"],
        "task_id": created["id"],
        "arguments_sha256": digest(queued_call["arguments"]),
        "local_execution": queued_row["local_execution"],
    }
    ledger = tools.local.ledger_factory()
    ledger.begin(binding, {"title": "Execute"}, receipt_reserve=32768)
    ledger.update(queued_call["operation_id"], "dispatched", operation_classification=classification)
    assert "receipt_reserve" in ledger.get(queued_call["operation_id"])

    def put_in_flight(data):
        row = data["tasks"][created["id"]]
        row["status"] = "running"
        row["in_flight"] = {
            **queued_call,
            "kind": "tool",
            "dispatched": True,
            "authorization": "read",
            "operation_classification": classification,
            "goal_revision": row["goal"]["revision"],
            "control_revision": row["control_revision"],
            "scope_revision": row["scope_revision"],
            "context_epoch": row.get("context_epoch", 0),
            "authority": model_retry_authority(data["settings"]),
            "preview": None,
        }
        verification.record(row, queued_call, "intent", now(), flight=row["in_flight"])

    store.transaction(put_in_flight)

    restarted = AgentRuntime(store, bridge, tools, catalog)
    restarted.reconcile()
    after_reconcile = restarted.get(created["id"])
    assert after_reconcile["error"] != "agent_write_outcome_unknown"
    assert after_reconcile["status"] == "queued"
    interrupted_event = next(e for e in reversed(after_reconcile["events"]) if e["type"] == "interrupted")
    assert interrupted_event["ambiguous_write"] is False
    assert interrupted_event["continued"] is True
    assert interrupted_event["cleanup_confirmed"] is True
    settled_ledger = tools.local.ledger_factory().get(queued_call["operation_id"])
    assert settled_ledger["status"] == "failed"
    assert settled_ledger["no_mutation"] is True
    assert settled_ledger["cleanup_confirmed"] is True
    assert "receipt_reserve" not in settled_ledger
    assert settled_ledger["error"] == "agent_node_restarted"

    for _ in range(12):
        restarted.run_once()
        current = restarted.get(created["id"])
        if current["status"] in {"completed", "failed", "interrupted"}:
            break
    assert current["status"] == "completed", current.get("error")
    assert any("agent_node_restarted" in str(m.get("content", "")) for m in requests[1]["messages"])


def test_restart_recovery_preserves_cancel_pause_permission_budget_and_revision_constraints(node, tmp_path):
    from rynmesh.agent_runtime import verification
    from rynmesh.agent_runtime.execution_policy import classify_operation
    from rynmesh.agent_runtime.protocol import model_retry_authority, now

    runtime, store, bridge, tools, catalog, _ = _make_node_runtime(node, tmp_path, "constraints-read-shell")
    read_cmd = "Get-Location" if os.name == "nt" else "pwd"

    def make_in_flight_task(label, mutate_row=None):
        created = runtime.create({"message": f"Task {label}"})
        call_item = {
            "call_id": f"call_{label}",
            "operation_id": f"op_{label}",
            "name": "ryn_shell_execute",
            "arguments": {"command": read_cmd},
            "effect": "write",
            "context_epoch": 0,
        }

        def setup(data):
            row = data["tasks"][created["id"]]
            row["status"] = "running"
            row["queue"] = [call_item]
            classification = classify_operation(
                call_item["name"],
                call_item["arguments"],
                tools.metadata(call_item["name"]),
                operation_id=call_item["operation_id"],
                task_id=row["id"],
                workspace_root=str(tmp_path),
                goal_revision=row["goal"]["revision"],
                scope_revision=row["scope_revision"],
                context_epoch=row.get("context_epoch", 0),
                control_revision=row["control_revision"],
            )
            row["in_flight"] = {
                **call_item,
                "kind": "tool",
                "dispatched": True,
                "authorization": "read",
                "operation_classification": classification,
                "goal_revision": row["goal"]["revision"],
                "control_revision": row["control_revision"],
                "scope_revision": row["scope_revision"],
                "context_epoch": row.get("context_epoch", 0),
                "authority": model_retry_authority(data["settings"]),
                "preview": None,
            }
            verification.record(row, call_item, "intent", now(), flight=row["in_flight"])
            if mutate_row is not None:
                mutate_row(row, data)

        store.transaction(setup)
        return created["id"]

    t_cancel = make_in_flight_task("cancel", lambda row, _d: row.update(status="cancelled", pending_control={"kind": "cancel"}))
    t_pause = make_in_flight_task("pause", lambda row, _d: row.update(status="paused", pending_control={"kind": "pause"}))
    t_budget = make_in_flight_task("budget", lambda row, _d: row.update(steps=row["limits"]["max_steps"]))
    t_rev = make_in_flight_task("revision", lambda row, _d: row["goal"].update(revision=row["goal"]["revision"] + 1))
    t_perm = make_in_flight_task("perm", lambda row, _d: row["in_flight"].update(authority="stale_authority"))

    restarted = AgentRuntime(store, bridge, tools, catalog)
    restarted.reconcile()

    assert restarted.get(t_cancel)["status"] == "cancelled"
    assert restarted.get(t_pause)["status"] == "paused"
    assert restarted.get(t_budget)["status"] == "interrupted"
    assert restarted.get(t_budget)["error"] == "agent_node_restarted"
    assert restarted.get(t_rev)["status"] == "interrupted"
    assert restarted.get(t_rev)["error"] == "agent_node_restarted"
    assert restarted.get(t_perm)["status"] == "interrupted"
    assert restarted.get(t_perm)["error"] == "agent_node_restarted"
    for tid in (t_cancel, t_pause, t_budget, t_rev, t_perm):
        task_state = restarted.get(tid)
        assert task_state["error"] != "agent_write_outcome_unknown"
        tool_msgs = [json.loads(m["content"]) for m in task_state["messages"] if m["role"] == "tool"]
        assert tool_msgs and tool_msgs[0] == {"error": "agent_node_restarted", "not_executed": False, "no_mutation": True}


def test_recovery_checks_old_process_and_prevents_duplicate_running_operation(local):
    from rynmesh.agent_runtime.local_processes import prepare_shell_command

    tools, context, workspace, _ = local
    slow_cmd = "Start-Sleep 30" if os.name == "nt" else "sleep 30"
    argv = prepare_shell_command({"command": slow_cmd})
    ident = tools.processes.start(
        argv,
        workspace,
        context["task_id"],
        30,
        lambda: True,
        lambda: False,
        operation_id="op-running-read",
    )
    try:
        assert tools.processes.running_for_operation(context["task_id"], "op-running-read")
        with pytest.raises(AgentProcessStartRejected, match="agent_process_already_running"):
            tools.processes.start(
                argv,
                workspace,
                context["task_id"],
                30,
                lambda: True,
                lambda: False,
                operation_id="op-running-read",
            )
        with pytest.raises(AgentToolRejected, match="agent_process_already_running"):
            execute(tools, context, "ryn_shell_execute", {"command": slow_cmd}, "op-running-read")
        stopped = tools.recover_operation(context["task_id"], "op-running-read")
        assert stopped["had_running"] is True
        assert stopped["still_running"] is False
        assert ident in stopped["stopped_ids"]
        assert not tools.processes.running_for_operation(context["task_id"], "op-running-read")
    finally:
        tools.processes.close()


def test_genuine_write_shell_unknown_outcome_still_enters_reconciliation(node, monkeypatch, tmp_path):
    runtime, store, bridge, tools, catalog, _ = _make_node_runtime(node, tmp_path, "write-shell-unknown")
    runtime.update_settings({"permission_mode": "automatic", "allowed_write_tools": ["ryn_shell_execute"]})
    write_cmd = "Add-Content out.txt hi" if os.name == "nt" else "echo hi >> out.txt"

    def fail_wait(*_args, **_kwargs):
        raise OSError(errno.EIO, "unconfirmed write failure after process start")

    monkeypatch.setattr(tools.local.processes, "wait", fail_wait)
    fake_upstream(
        monkeypatch,
        [
            call("ryn_shell_execute", json.dumps({"command": write_cmd}), "call_write"),
        ],
    )
    created = runtime.create({"message": "Write hi to out.txt."})
    runtime.run_once()
    runtime.run_once()
    interrupted = runtime.get(created["id"])
    assert interrupted["status"] == "interrupted"
    assert interrupted["error"] == "agent_write_outcome_unknown"
    assert interrupted["reconciliation"]["state"] == "pending"
    raw_row = store.read()["tasks"][created["id"]]
    write_op_id = raw_row["in_flight"]["operation_id"]
    write_ledger = tools.local.ledger_factory().get(write_op_id)
    assert write_ledger["status"] == "dispatched"
    assert write_ledger.get("receipt_reserve") == 32768
    with pytest.raises(AgentError, match="agent_operation_unresolved"):
        runtime.action(created["id"], {"action": "resume", "expected_revision": interrupted["revision"]})


def test_forged_or_stale_read_classification_cannot_bypass_write_protection(local):
    from rynmesh.agent_runtime.execution_policy import (
        classify_operation,
        shell_read,
        verified_read_classification,
    )

    tools, context, workspace, _ = local
    read_cmd = "Get-Location" if os.name == "nt" else "pwd"
    write_cmd = "Add-Content out.txt hi" if os.name == "nt" else "echo hi >> out.txt"

    # 1. Self-reported read keys inside arguments are rejected
    assert not shell_read({"command": read_cmd, "authorization": "read"})
    assert not shell_read({"command": read_cmd, "effect": "read"})
    with pytest.raises(AgentToolRejected, match="agent_invalid_operation_classification"):
        execute(tools, context, "ryn_shell_execute", {"command": read_cmd, "authorization": "read"}, "forged-arg")

    # 2. Forged classification claiming read for a write command is rejected
    forged_classification = {
        "version": 1,
        "operation_id": "forged-ctx",
        "task_id": context["task_id"],
        "tool": "ryn_shell_execute",
        "arguments_sha256": classify_operation("ryn_shell_execute", {"command": write_cmd}, {"effect": "write"})["arguments_sha256"],
        "workspace_root": str(workspace),
        "effect_class": "read",
        "basis": "literal_shell_read",
        "goal_revision": None,
        "scope_revision": None,
        "context_epoch": 0,
        "control_revision": None,
    }
    assert not verified_read_classification(
        {"operation_classification": forged_classification, "arguments": {"command": write_cmd}},
        task_id=context["task_id"],
        workspace_root=str(workspace),
    )
    with pytest.raises(AgentToolRejected, match="agent_invalid_operation_classification"):
        execute(
            tools,
            {**context, "operation_classification": forged_classification},
            "ryn_shell_execute",
            {"command": write_cmd},
            "forged-ctx",
        )

    # 3. Stale classification (mismatched workspace_root or goal_revision) is rejected
    stale_classification = classify_operation(
        "ryn_shell_execute",
        {"command": read_cmd},
        {"effect": "write"},
        operation_id="stale-op",
        task_id=context["task_id"],
        workspace_root=str(workspace),
        goal_revision=2,
    )
    assert not verified_read_classification(
        {"operation_classification": stale_classification, "goal_revision": 1, "arguments": {"command": read_cmd}},
        task_id=context["task_id"],
        workspace_root=str(workspace),
    )
    with pytest.raises(AgentToolRejected, match="agent_invalid_operation_classification"):
        execute(
            tools,
            {**context, "goal_revision": 1, "operation_classification": stale_classification},
            "ryn_shell_execute",
            {"command": read_cmd},
            "stale-op",
        )


def test_missing_read_classification_in_historical_record_stays_conservative(node, tmp_path):
    from rynmesh.agent_runtime import verification
    from rynmesh.agent_runtime.protocol import model_retry_authority, now

    read_cmd = "Get-Location" if os.name == "nt" else "pwd"
    runtime, store, bridge, node_tools, catalog, _ = _make_node_runtime(node, tmp_path, "legacy-record")
    created = runtime.create({"message": "Historical task without operation_classification."})
    legacy_call = {
        "call_id": "call_legacy",
        "operation_id": "op_legacy",
        "name": "ryn_shell_execute",
        "arguments": {"command": read_cmd},
        "effect": "write",
    }

    def set_legacy_in_flight(data):
        row = data["tasks"][created["id"]]
        row["status"] = "running"
        row["queue"] = [legacy_call]
        row["in_flight"] = {
            **legacy_call,
            "kind": "tool",
            "dispatched": True,
            "authorization": "read",
            "goal_revision": row["goal"]["revision"],
            "control_revision": row["control_revision"],
            "scope_revision": row["scope_revision"],
            "authority": model_retry_authority(data["settings"]),
            "preview": None,
        }
        verification.record(row, legacy_call, "intent", now(), flight=row["in_flight"])

    store.transaction(set_legacy_in_flight)
    restarted = AgentRuntime(store, bridge, node_tools, catalog)
    restarted.reconcile()
    legacy_after = restarted.get(created["id"])
    assert legacy_after["status"] == "interrupted"
    assert legacy_after["error"] == "agent_write_outcome_unknown"


def test_legacy_migrated_task_read_shell_restart_recovery(node, tmp_path):
    from rynmesh.agent_runtime import budget, verification
    from rynmesh.agent_runtime.execution_policy import classify_operation
    from rynmesh.agent_runtime.protocol import digest, event, model_retry_authority, now

    runtime, store, bridge, tools, catalog, _ = _make_node_runtime(node, tmp_path, "legacy-budget-read-shell")
    read_cmd = "Get-Location" if os.name == "nt" else "pwd"
    created = runtime.create({"message": "Legacy task migrated before budget accounting."})
    initial_row = store.read()["tasks"][created["id"]]
    call_item = {
        "call_id": "call_legacy_migrated",
        "operation_id": "op_legacy_migrated",
        "name": "ryn_shell_execute",
        "arguments": {"command": read_cmd},
        "effect": "write",
        "context_epoch": initial_row.get("context_epoch", 0),
    }
    classification = classify_operation(
        call_item["name"],
        call_item["arguments"],
        tools.metadata(call_item["name"]),
        operation_id=call_item["operation_id"],
        task_id=created["id"],
        workspace_root=str(tmp_path),
        goal_revision=initial_row["goal"]["revision"],
        scope_revision=initial_row["scope_revision"],
        context_epoch=initial_row.get("context_epoch", 0),
        control_revision=initial_row["control_revision"],
    )
    binding = {
        "operation_id": call_item["operation_id"],
        "tool": call_item["name"],
        "task_id": created["id"],
        "arguments_sha256": digest(call_item["arguments"]),
        "local_execution": initial_row["local_execution"],
    }
    ledger = tools.local.ledger_factory()
    ledger.begin(binding, {"title": "Execute"}, receipt_reserve=32768)
    ledger.update(call_item["operation_id"], "dispatched", operation_classification=classification)

    def setup_legacy_row(data):
        row = data["tasks"][created["id"]]
        # Remove budget to simulate pre-budget historical task with prior activity
        row.pop("budget", None)
        row["steps"] = 2
        row["elapsed_seconds"] = 1.25
        event(row, "model_started")
        event(row, "tool_started", tool="ryn_file_list", call_id="old_call", operation_id="old_op")
        # Re-initialize budget from legacy history (sets legacy_usage_unknown=True, lifetime["tool_calls"]=None)
        budget.initialize(row)
        assert budget.public(row)["lifetime"]["tool_calls"] is None
        assert budget.public(row)["lifetime"]["charged_tool_calls"] == 1
        row["status"] = "running"
        row["queue"] = [call_item]
        row["in_flight"] = {
            **call_item,
            "kind": "tool",
            "dispatched": True,
            "authorization": "read",
            "operation_classification": classification,
            "goal_revision": row["goal"]["revision"],
            "control_revision": row["control_revision"],
            "scope_revision": row["scope_revision"],
            "context_epoch": row.get("context_epoch", 0),
            "authority": model_retry_authority(data["settings"]),
            "preview": None,
        }
        verification.record(row, call_item, "intent", now(), flight=row["in_flight"])

    store.transaction(setup_legacy_row)
    restarted = AgentRuntime(store, bridge, tools, catalog)
    restarted.reconcile()
    after = restarted.get(created["id"])
    assert after["status"] == "queued"
    assert after["error"] is None
    assert after["budget"]["lifetime"]["tool_calls"] is None
    assert after["budget"]["lifetime"]["charged_tool_calls"] == 1
    settled_ledger = ledger.get(call_item["operation_id"])
    assert settled_ledger["status"] == "failed"
    assert settled_ledger["cleanup_confirmed"] is True
    assert "receipt_reserve" not in settled_ledger


def test_read_recovery_rejects_still_running_or_cleanup_exception_and_retains_reserve(node, monkeypatch, tmp_path):
    from rynmesh.agent_runtime import verification
    from rynmesh.agent_runtime.execution_policy import classify_operation
    from rynmesh.agent_runtime.protocol import digest, model_retry_authority, now

    runtime, store, bridge, tools, catalog, _ = _make_node_runtime(node, tmp_path, "cleanup-unconfirmed")
    read_cmd = "Get-Location" if os.name == "nt" else "pwd"

    def make_dispatched_read_task(label):
        created = runtime.create({"message": f"Read shell {label}"})
        initial_row = store.read()["tasks"][created["id"]]
        op_id = f"op_{label}"
        call_item = {
            "call_id": f"call_{label}",
            "operation_id": op_id,
            "name": "ryn_shell_execute",
            "arguments": {"command": read_cmd},
            "effect": "write",
            "context_epoch": initial_row.get("context_epoch", 0),
        }
        classification = classify_operation(
            call_item["name"],
            call_item["arguments"],
            tools.metadata(call_item["name"]),
            operation_id=op_id,
            task_id=created["id"],
            workspace_root=str(tmp_path),
            goal_revision=initial_row["goal"]["revision"],
            scope_revision=initial_row["scope_revision"],
            context_epoch=initial_row.get("context_epoch", 0),
            control_revision=initial_row["control_revision"],
        )
        binding = {
            "operation_id": op_id,
            "tool": call_item["name"],
            "task_id": created["id"],
            "arguments_sha256": digest(call_item["arguments"]),
            "local_execution": initial_row["local_execution"],
        }
        ledger = tools.local.ledger_factory()
        ledger.begin(binding, {"title": "Execute"}, receipt_reserve=32768)
        ledger.update(op_id, "dispatched", operation_classification=classification)

        def put_flight(data):
            row = data["tasks"][created["id"]]
            row["status"] = "running"
            row["queue"] = [call_item]
            row["in_flight"] = {
                **call_item,
                "kind": "tool",
                "dispatched": True,
                "authorization": "read",
                "operation_classification": classification,
                "goal_revision": row["goal"]["revision"],
                "control_revision": row["control_revision"],
                "scope_revision": row["scope_revision"],
                "context_epoch": row.get("context_epoch", 0),
                "authority": model_retry_authority(data["settings"]),
                "preview": None,
            }
            verification.record(row, call_item, "intent", now(), flight=row["in_flight"])

        store.transaction(put_flight)
        return created["id"], op_id

    # 1. Reconcile when process is still_running=True: must NOT queue or settle ledger or drop receipt_reserve
    t_still, op_still = make_dispatched_read_task("still_running")
    monkeypatch.setattr(
        tools.local.processes,
        "recover_operation",
        lambda _tid, _oid=None: {
            "ok": False,
            "had_running": True,
            "still_running": True,
            "cleanup_error": False,
            "process_ids": ["process_still_alive"],
            "stopped_ids": [],
        },
    )
    restarted = AgentRuntime(store, bridge, tools, catalog)
    restarted.reconcile()
    state_still = restarted.get(t_still)
    assert state_still["status"] == "interrupted"
    assert state_still["error"] == "agent_node_restarted"
    ev_still = next(e for e in reversed(state_still["events"]) if e["type"] == "interrupted")
    assert ev_still["continued"] is False
    assert ev_still["cleanup_confirmed"] is False
    assert store.read()["tasks"][t_still]["in_flight"] is not None
    ledger_still = tools.local.ledger_factory().get(op_still)
    assert ledger_still["status"] == "dispatched"
    assert ledger_still.get("receipt_reserve") == 32768

    # Reset t_still out of running so it doesn't interfere with case 2
    store.transaction(lambda d: d["tasks"][t_still].update(in_flight=None, status="failed"))

    # 2. Reconcile when process cleanup raises an exception: must NOT queue or settle ledger or drop receipt_reserve
    t_exc, op_exc = make_dispatched_read_task("cleanup_exc")

    def raise_cleanup(_tid, _oid=None):
        raise RuntimeError("simulated OS process termination failure")

    monkeypatch.setattr(tools.local.processes, "recover_operation", raise_cleanup)
    restarted.reconcile()
    state_exc = restarted.get(t_exc)
    assert state_exc["status"] == "interrupted"
    assert state_exc["error"] == "agent_node_restarted"
    ev_exc = next(e for e in reversed(state_exc["events"]) if e["type"] == "interrupted")
    assert ev_exc["continued"] is False
    assert ev_exc["cleanup_confirmed"] is False
    assert store.read()["tasks"][t_exc]["in_flight"] is not None
    ledger_exc = tools.local.ledger_factory().get(op_exc)
    assert ledger_exc["status"] == "dispatched"
    assert ledger_exc.get("receipt_reserve") == 32768

    # 3. Execution exception during run_once when cleanup also fails: must interrupt and retain reserve
    store.transaction(lambda d: d["tasks"][t_exc].update(in_flight=None, status="failed"))
    monkeypatch.setattr(
        tools.local.processes,
        "wait",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError(errno.EIO, "wait failed")),
    )
    fake_upstream(
        monkeypatch,
        [
            call("ryn_shell_execute", json.dumps({"command": read_cmd}), "call_exec_cleanup_fail"),
        ],
    )
    t_run = runtime.create({"message": "Run read shell where both wait and cleanup fail."})
    runtime.run_once()
    runtime.run_once()
    state_run = runtime.get(t_run["id"])
    assert state_run["status"] == "interrupted"
    assert state_run["error"] == "agent_local_operation_unconfirmed"
    raw_run = store.read()["tasks"][t_run["id"]]
    assert raw_run["in_flight"] is not None
    op_run = raw_run["in_flight"]["operation_id"]
    ledger_run = tools.local.ledger_factory().get(op_run)
    assert ledger_run["status"] == "dispatched"
    assert ledger_run.get("receipt_reserve") == 32768


def test_isolated_host_process_exit_cleans_up_child_and_recreated_tools_reconcile(tmp_path):
    import subprocess
    import sys
    import time
    from pathlib import Path
    from types import SimpleNamespace

    from rynmesh.agent_runtime.local_tools import LocalTools
    from rynmesh.agent_runtime.operation_ledger import OperationLedger
    from rynmesh.services import peer_box

    home = tmp_path / "host_home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    marker_file = tmp_path / "spawned_info.json"
    repo_root = str(Path(__file__).resolve().parents[1])

    child_script = f"""
import json, os, sys, time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, {repo_root!r})
from rynmesh.agent_runtime import verification
from rynmesh.agent_runtime.execution_policy import classify_operation
from rynmesh.agent_runtime.local_processes import prepare_shell_command
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.protocol import digest, model_retry_authority, now
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.services import peer_box

home = Path({str(home)!r})
workspace = Path({str(workspace)!r})
key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
store = AgentStore(home / "agent", key)
ledger = OperationLedger(home)
local_tools = LocalTools(SimpleNamespace(home=home), lambda: ledger)
local_tools.store = store

class StubCatalog:
    def list_models(self):
        return [{{"id": "local:test", "name": "Test", "source": "local_api", "available": True, "agent_supported": True}}]

class StubTools:
    def __init__(self, local):
        self.local = local
    def specs(self):
        return []
    def metadata(self, name):
        return {{"effect": "write", "scope_context_supported": True, "preview_precondition": True}}
    def recover_operation(self, task_id, operation_id=None, *, error="agent_read_interrupted", operation_classification=None):
        return self.local.recover_operation(task_id, operation_id, error=error, operation_classification=operation_classification)

runtime = AgentRuntime(store, SimpleNamespace(), StubTools(local_tools), StubCatalog())
runtime.update_settings({{"enabled": True, "model_id": "local:test", "workspace_root": str(workspace)}})
created = runtime.create({{"message": "Isolated host process read shell"}})
task_id = created["id"]
initial_row = store.read()["tasks"][task_id]
op_id = "op_isolated_host_exit"
sleep_cmd = "Start-Sleep 30" if os.name == "nt" else "sleep 30"
read_cmd = "Get-Location" if os.name == "nt" else "pwd"
args = {{"command": read_cmd}}
argv = prepare_shell_command({{"command": sleep_cmd}})
ident = local_tools.processes.start(argv, workspace, task_id, 30, lambda: True, lambda: False, operation_id=op_id)
child_pid = local_tools.processes.items[ident]["process"].pid

classification = classify_operation(
    "ryn_shell_execute", args, {{"effect": "write"}},
    operation_id=op_id, task_id=task_id, workspace_root=initial_row["local_execution"]["workspace_root"],
    goal_revision=initial_row["goal"]["revision"],
    scope_revision=initial_row["scope_revision"],
    context_epoch=initial_row.get("context_epoch", 0),
    control_revision=initial_row["control_revision"],
)
binding = {{
    "operation_id": op_id, "tool": "ryn_shell_execute", "task_id": task_id,
    "arguments_sha256": digest(args),
    "local_execution": initial_row["local_execution"],
}}
ledger.begin(binding, {{"title": "Execute"}}, receipt_reserve=32768)
ledger.update(op_id, "dispatched", operation_classification=classification, host_pid=os.getpid())

def record_flight(data):
    row = data["tasks"][task_id]
    call_item = {{"call_id": "call_iso", "operation_id": op_id, "name": "ryn_shell_execute", "arguments": args, "effect": "write", "context_epoch": row.get("context_epoch", 0)}}
    row["status"] = "running"
    row["queue"] = [call_item]
    row["in_flight"] = {{
        **call_item, "kind": "tool", "dispatched": True, "authorization": "read",
        "operation_classification": classification,
        "goal_revision": row["goal"]["revision"], "control_revision": row["control_revision"],
        "scope_revision": row["scope_revision"], "context_epoch": row.get("context_epoch", 0),
        "authority": model_retry_authority(data["settings"]), "preview": None,
    }}
    verification.record(row, call_item, "intent", now(), flight=row["in_flight"])

store.transaction(record_flight)
Path({str(marker_file)!r}).write_text(json.dumps({{"task_id": task_id, "operation_id": op_id, "child_pid": child_pid, "host_pid": os.getpid()}}), encoding="utf-8")
"""
    proc = subprocess.run([sys.executable, "-c", child_script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    info = json.loads(marker_file.read_text(encoding="utf-8"))
    child_pid = info["child_pid"]

    # Verify child process was cleaned up when the isolated host process exited
    def pid_alive(pid):
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
            return str(pid) in out.stdout
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and pid_alive(child_pid):
        time.sleep(0.1)
    assert not pid_alive(child_pid), f"Child process {child_pid} survived host exit"

    # Now recreate brand-new Store, OperationLedger, LocalTools (and Processes), and AgentRuntime in the new host
    key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    new_store = AgentStore(home / "agent", key)
    new_ledger = OperationLedger(home)
    new_local_tools = LocalTools(SimpleNamespace(home=home), lambda: new_ledger)
    new_local_tools.store = new_store

    class NewCatalog:
        def list_models(self):
            return [{"id": "local:test", "name": "Test", "source": "local_api", "available": True, "agent_supported": True}]

    class NewTools:
        def __init__(self, local):
            self.local = local

        def specs(self):
            return []

        def metadata(self, name):
            return {"effect": "write", "scope_context_supported": True, "preview_precondition": True}

        def recover_operation(self, task_id, operation_id=None, *, error="agent_read_interrupted", operation_classification=None):
            return self.local.recover_operation(task_id, operation_id, error=error, operation_classification=operation_classification)

    try:
        restarted_runtime = AgentRuntime(new_store, SimpleNamespace(), NewTools(new_local_tools), NewCatalog())
        restarted_runtime.reconcile()
        after = restarted_runtime.get(info["task_id"])
        assert after["status"] == "queued"
        assert after["error"] is None
        settled = new_ledger.get(info["operation_id"])
        assert settled["status"] == "failed"
        assert settled["no_mutation"] is True
        assert settled["cleanup_confirmed"] is True
        assert "receipt_reserve" not in settled
    finally:
        new_local_tools.processes.close()


def test_restart_reuses_confirmed_and_not_executed_ledger_receipts_without_rerun(monkeypatch, node, tmp_path):
    from rynmesh.agent_runtime import verification
    from rynmesh.agent_runtime.execution_policy import classify_operation
    from rynmesh.agent_runtime.protocol import digest, model_retry_authority, now

    runtime, store, bridge, tools, catalog, _ = _make_node_runtime(node, tmp_path, "crash-windows")
    workspace = tmp_path
    read_cmd = "Get-Location" if os.name == "nt" else "pwd"
    args = {"command": read_cmd}

    def setup_crash_window_task(suffix, ledger_status, *, result=None, error=None, tamper_binding=False):
        created = runtime.create({"message": f"Read shell crash window {suffix}"})
        task_id = created["id"]
        op_id = f"op_window_{suffix}"
        call_id = f"call_window_{suffix}"
        initial_row = store.read()["tasks"][task_id]
        classification = classify_operation(
            "ryn_shell_execute",
            args,
            {"effect": "write"},
            operation_id=op_id,
            task_id=task_id,
            workspace_root=initial_row["local_execution"]["workspace_root"],
            goal_revision=initial_row["goal"]["revision"],
            scope_revision=initial_row["scope_revision"],
            context_epoch=initial_row.get("context_epoch", 0),
            control_revision=initial_row["control_revision"],
        )
        binding = {
            "operation_id": op_id,
            "tool": "ryn_shell_execute",
            "task_id": task_id,
            "arguments_sha256": digest({"command": "tampered"}) if tamper_binding else digest(args),
            "local_execution": initial_row["local_execution"],
        }
        ledger = tools.local.ledger_factory()
        ledger.begin(binding, {"title": "Execute"}, receipt_reserve=32768)
        if ledger_status == "confirmed":
            ledger.update(op_id, "confirmed", result=result, operation_classification=classification)
        elif ledger_status == "not_executed":
            ledger.update(op_id, "not_executed", error=error or "agent_task_canceled", operation_classification=classification)
        elif ledger_status == "intent":
            ledger.update(op_id, "intent", operation_classification=classification)

        call_item = {
            "call_id": call_id,
            "operation_id": op_id,
            "name": "ryn_shell_execute",
            "arguments": args,
            "effect": "write",
            "context_epoch": initial_row.get("context_epoch", 0),
        }

        def put_flight(data):
            row = data["tasks"][task_id]
            row["messages"].append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "ryn_shell_execute", "arguments": json.dumps(args)},
                        }
                    ],
                    "context_epoch": row.get("context_epoch", 0),
                }
            )
            row["status"] = "running"
            row["queue"] = [call_item]
            row["in_flight"] = {
                **call_item,
                "kind": "tool",
                "dispatched": True,
                "authorization": "read",
                "operation_classification": classification,
                "goal_revision": row["goal"]["revision"],
                "control_revision": row["control_revision"],
                "scope_revision": row["scope_revision"],
                "context_epoch": row.get("context_epoch", 0),
                "authority": model_retry_authority(data["settings"]),
                "preview": None,
            }
            verification.record(row, call_item, "intent", now(), flight=row["in_flight"])

        store.transaction(put_flight)
        return task_id, op_id, call_id

    # 1. LocalTools already wrote status="confirmed", AgentLoop crashed before saving tool result
    expected_result = {"exit_code": 0, "stdout": str(workspace) + "\n", "stderr": "", "timed_out": False}
    t_conf, op_conf, call_conf = setup_crash_window_task("confirmed", "confirmed", result=expected_result)

    spawn_attempts = []
    orig_start = tools.local.processes.start
    monkeypatch.setattr(tools.local.processes, "start", lambda *a, **kw: spawn_attempts.append(1) or orig_start(*a, **kw))

    restarted = AgentRuntime(store, bridge, tools, catalog)
    restarted.reconcile()
    state_conf = restarted.get(t_conf)
    assert state_conf["status"] == "queued"
    assert state_conf["error"] is None
    assert spawn_attempts == []

    raw_conf = store.read()["tasks"][t_conf]
    assert raw_conf["in_flight"] is None
    tool_msgs = [m for m in raw_conf["messages"] if m.get("role") == "tool" and m.get("tool_call_id") == call_conf]
    assert len(tool_msgs) == 1
    saved_payload = json.loads(tool_msgs[0]["content"])
    assert saved_payload["exit_code"] == 0
    assert saved_payload["stdout"] == str(workspace) + "\n"
    assert raw_conf["receipts"][-1]["status"] == "confirmed"
    assert "receipt_reserve" not in tools.local.ledger_factory().get(op_conf)
    ev_conf = next(e for e in reversed(state_conf["events"]) if e["type"] == "interrupted")
    assert ev_conf["continued"] is True
    assert ev_conf["ledger_outcome"] == "confirmed"

    fake_upstream(
        monkeypatch,
        [
            {"role": "assistant", "content": "Used cached confirmed read result without re-running."},
            passed_checker,
        ],
    )
    restarted.run_once()
    assert restarted.get(t_conf)["status"] == "completed"
    assert spawn_attempts == []

    # 2. LocalTools wrote status="not_executed" before crash
    t_ne, op_ne, call_ne = setup_crash_window_task("not_executed", "not_executed", error="agent_read_interrupted")
    restarted.reconcile()
    state_ne = restarted.get(t_ne)
    assert state_ne["status"] == "queued"
    raw_ne = store.read()["tasks"][t_ne]
    assert raw_ne["in_flight"] is None
    ne_msgs = [m for m in raw_ne["messages"] if m.get("role") == "tool" and m.get("tool_call_id") == call_ne]
    assert len(ne_msgs) == 1
    ne_payload = json.loads(ne_msgs[0]["content"])
    assert ne_payload["not_executed"] is True
    assert ne_payload["no_mutation"] is True
    assert raw_ne["receipts"][-1]["status"] == "not_executed"
    assert "receipt_reserve" not in tools.local.ledger_factory().get(op_ne)
    store.transaction(lambda d: d["tasks"][t_ne].update(status="completed"))

    # 3. LocalTools only wrote status="intent" before crash
    t_int, op_int, call_int = setup_crash_window_task("intent", "intent")
    restarted.reconcile()
    state_int = restarted.get(t_int)
    assert state_int["status"] == "queued"
    raw_int = store.read()["tasks"][t_int]
    assert raw_int["in_flight"] is None
    int_msgs = [m for m in raw_int["messages"] if m.get("role") == "tool" and m.get("tool_call_id") == call_int]
    assert len(int_msgs) == 1
    int_payload = json.loads(int_msgs[0]["content"])
    assert int_payload["not_executed"] is True
    assert int_payload["no_mutation"] is True
    assert raw_int["receipts"][-1]["status"] == "not_executed"
    ledger_int = tools.local.ledger_factory().get(op_int)
    assert ledger_int["status"] == "not_executed"
    assert "receipt_reserve" not in ledger_int
    store.transaction(lambda d: d["tasks"][t_int].update(status="completed"))

    # 4. Tampered binding on "confirmed" record must NOT be trusted or settled
    t_bad, op_bad, _call_bad = setup_crash_window_task(
        "tampered", "confirmed", result=expected_result, tamper_binding=True
    )
    restarted.reconcile()
    state_bad = restarted.get(t_bad)
    assert state_bad["status"] == "interrupted"
    assert store.read()["tasks"][t_bad]["in_flight"] is not None


def test_temporary_cleanup_failure_allows_later_resume_or_reconcile_after_process_exits(monkeypatch, node, tmp_path):
    from rynmesh.agent_runtime import verification
    from rynmesh.agent_runtime.execution_policy import classify_operation
    from rynmesh.agent_runtime.protocol import digest, model_retry_authority, now

    runtime, store, bridge, tools, catalog, _ = _make_node_runtime(node, tmp_path, "temp-cleanup-fail")
    read_cmd = "Get-Location" if os.name == "nt" else "pwd"
    args = {"command": read_cmd}

    def create_stuck_read_task(suffix):
        created = runtime.create({"message": f"Temporary cleanup failure {suffix}"})
        task_id = created["id"]
        op_id = f"op_stuck_{suffix}"
        call_id = f"call_stuck_{suffix}"
        initial_row = store.read()["tasks"][task_id]
        classification = classify_operation(
            "ryn_shell_execute",
            args,
            {"effect": "write"},
            operation_id=op_id,
            task_id=task_id,
            workspace_root=initial_row["local_execution"]["workspace_root"],
            goal_revision=initial_row["goal"]["revision"],
            scope_revision=initial_row["scope_revision"],
            context_epoch=initial_row.get("context_epoch", 0),
            control_revision=initial_row["control_revision"],
        )
        binding = {
            "operation_id": op_id,
            "tool": "ryn_shell_execute",
            "task_id": task_id,
            "arguments_sha256": digest(args),
            "local_execution": initial_row["local_execution"],
        }
        ledger = tools.local.ledger_factory()
        ledger.begin(binding, {"title": "Execute"}, receipt_reserve=32768)
        ledger.update(op_id, "dispatched", operation_classification=classification)
        call_item = {
            "call_id": call_id,
            "operation_id": op_id,
            "name": "ryn_shell_execute",
            "arguments": args,
            "effect": "write",
            "context_epoch": initial_row.get("context_epoch", 0),
        }

        def put_flight(data):
            row = data["tasks"][task_id]
            row["messages"].append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "ryn_shell_execute", "arguments": json.dumps(args)},
                        }
                    ],
                    "context_epoch": row.get("context_epoch", 0),
                }
            )
            row["status"] = "running"
            row["queue"] = [call_item]
            row["in_flight"] = {
                **call_item,
                "kind": "tool",
                "dispatched": True,
                "authorization": "read",
                "operation_classification": classification,
                "goal_revision": row["goal"]["revision"],
                "control_revision": row["control_revision"],
                "scope_revision": row["scope_revision"],
                "context_epoch": row.get("context_epoch", 0),
                "authority": model_retry_authority(data["settings"]),
                "preview": None,
            }
            verification.record(row, call_item, "intent", now(), flight=row["in_flight"])

        store.transaction(put_flight)
        return task_id, op_id, call_id

    process_alive = {"alive": True}

    def fake_recover(_tid, _oid=None):
        if process_alive["alive"]:
            return {
                "ok": False,
                "had_running": True,
                "still_running": True,
                "cleanup_error": False,
                "process_ids": ["proc_temp_stuck"],
                "stopped_ids": [],
            }
        return {
            "ok": True,
            "had_running": False,
            "still_running": False,
            "cleanup_error": False,
            "process_ids": [],
            "stopped_ids": ["proc_temp_stuck"],
        }

    monkeypatch.setattr(tools.local.processes, "recover_operation", fake_recover)

    # Initial restart reconcile while process is still alive: interrupts and keeps in_flight
    t_resume, op_resume, call_resume = create_stuck_read_task("via_resume")
    restarted = AgentRuntime(store, bridge, tools, catalog)
    restarted.reconcile()

    stuck_state = restarted.get(t_resume)
    assert stuck_state["status"] == "interrupted"
    assert "resume" in stuck_state["allowed_actions"]
    assert "reconcile" in stuck_state["allowed_actions"]
    assert store.read()["tasks"][t_resume]["in_flight"] is not None

    # Trying to resume or reconcile while process is still alive must NOT clear in_flight or queue
    with pytest.raises(AgentError, match="agent_operation_unresolved"):
        restarted.action(t_resume, {"action": "resume", "expected_revision": stuck_state["revision"]})
    assert store.read()["tasks"][t_resume]["in_flight"] is not None

    still_reconcile = restarted.action(t_resume, {"action": "reconcile", "expected_revision": stuck_state["revision"]})
    assert still_reconcile["status"] == "interrupted"
    assert store.read()["tasks"][t_resume]["in_flight"] is not None

    # Now the process exits; user clicks "resume" -> settles read, clears in_flight, and queues task
    process_alive["alive"] = False
    resumed = restarted.action(t_resume, {"action": "resume", "expected_revision": still_reconcile["revision"]})
    assert resumed["status"] == "queued"
    assert resumed["error"] is None
    raw_resumed = store.read()["tasks"][t_resume]
    assert raw_resumed["in_flight"] is None
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == call_resume for m in raw_resumed["messages"])
    ledger_resumed = tools.local.ledger_factory().get(op_resume)
    assert ledger_resumed["status"] == "failed"
    assert ledger_resumed["cleanup_confirmed"] is True
    assert "receipt_reserve" not in ledger_resumed

    fake_upstream(
        monkeypatch,
        [
            {"role": "assistant", "content": "Recovered after temporary cleanup failure resolved."},
            passed_checker,
        ],
    )
    restarted.run_once()
    assert restarted.get(t_resume)["status"] == "completed"

    # Also verify user action "reconcile" on a second task after temporary failure resolves
    process_alive["alive"] = True
    t_rec, op_rec, call_rec = create_stuck_read_task("via_reconcile")
    restarted.reconcile()
    stuck_rec = restarted.get(t_rec)
    assert stuck_rec["status"] == "interrupted"
    process_alive["alive"] = False
    reconciled = restarted.action(t_rec, {"action": "reconcile", "expected_revision": stuck_rec["revision"]})
    assert reconciled["status"] == "queued"
    assert reconciled["error"] is None
    assert store.read()["tasks"][t_rec]["in_flight"] is None
    assert "receipt_reserve" not in tools.local.ledger_factory().get(op_rec)


@pytest.mark.skipif(os.name != "nt", reason="Windows JobObject forced host exit test")
def test_isolated_windows_host_forced_exit_bypasses_atexit_and_cleans_up_child(tmp_path):
    import subprocess
    import sys
    import time
    from pathlib import Path
    from types import SimpleNamespace

    from rynmesh.agent_runtime.local_tools import LocalTools
    from rynmesh.agent_runtime.operation_ledger import OperationLedger
    from rynmesh.services import peer_box

    home = tmp_path / "host_home_forced"
    workspace = tmp_path / "workspace_forced"
    home.mkdir()
    workspace.mkdir()
    marker_file = tmp_path / "forced_spawned_info.json"
    atexit_canary = tmp_path / "atexit_ran.txt"
    repo_root = str(Path(__file__).resolve().parents[1])

    child_script = f"""
import atexit, json, os, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, {repo_root!r})
from rynmesh.agent_runtime import verification
from rynmesh.agent_runtime.execution_policy import classify_operation
from rynmesh.agent_runtime.local_processes import prepare_shell_command
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.protocol import digest, model_retry_authority, now
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.services import peer_box

atexit.register(lambda: Path({str(atexit_canary)!r}).write_text("atexit", encoding="utf-8"))

home = Path({str(home)!r})
workspace = Path({str(workspace)!r})
key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
store = AgentStore(home / "agent", key)
ledger = OperationLedger(home)
local_tools = LocalTools(SimpleNamespace(home=home), lambda: ledger)
local_tools.store = store

class StubCatalog:
    def list_models(self):
        return [{{"id": "local:test", "name": "Test", "source": "local_api", "available": True, "agent_supported": True}}]

class StubTools:
    def __init__(self, local):
        self.local = local
    def specs(self):
        return []
    def metadata(self, name):
        return {{"effect": "write", "scope_context_supported": True, "preview_precondition": True}}
    def recover_operation(self, task_id, operation_id=None, *, error="agent_read_interrupted", operation_classification=None):
        return self.local.recover_operation(task_id, operation_id, error=error, operation_classification=operation_classification)

runtime = AgentRuntime(store, SimpleNamespace(), StubTools(local_tools), StubCatalog())
runtime.update_settings({{"enabled": True, "model_id": "local:test", "workspace_root": str(workspace)}})
created = runtime.create({{"message": "Forced host exit read shell"}})
task_id = created["id"]
initial_row = store.read()["tasks"][task_id]
op_id = "op_forced_host_exit"
args = {{"command": "Get-Location"}}
argv = prepare_shell_command({{"command": "Start-Sleep 30"}})
ident = local_tools.processes.start(argv, workspace, task_id, 30, lambda: True, lambda: False, operation_id=op_id)
child_pid = local_tools.processes.items[ident]["process"].pid

classification = classify_operation(
    "ryn_shell_execute", args, {{"effect": "write"}},
    operation_id=op_id, task_id=task_id, workspace_root=initial_row["local_execution"]["workspace_root"],
    goal_revision=initial_row["goal"]["revision"],
    scope_revision=initial_row["scope_revision"],
    context_epoch=initial_row.get("context_epoch", 0),
    control_revision=initial_row["control_revision"],
)
binding = {{
    "operation_id": op_id, "tool": "ryn_shell_execute", "task_id": task_id,
    "arguments_sha256": digest(args),
    "local_execution": initial_row["local_execution"],
}}
ledger.begin(binding, {{"title": "Execute"}}, receipt_reserve=32768)
ledger.update(op_id, "dispatched", operation_classification=classification, host_pid=os.getpid())

def record_flight(data):
    row = data["tasks"][task_id]
    call_item = {{"call_id": "call_forced", "operation_id": op_id, "name": "ryn_shell_execute", "arguments": args, "effect": "write", "context_epoch": row.get("context_epoch", 0)}}
    row["status"] = "running"
    row["queue"] = [call_item]
    row["in_flight"] = {{
        **call_item, "kind": "tool", "dispatched": True, "authorization": "read",
        "operation_classification": classification,
        "goal_revision": row["goal"]["revision"], "control_revision": row["control_revision"],
        "scope_revision": row["scope_revision"], "context_epoch": row.get("context_epoch", 0),
        "authority": model_retry_authority(data["settings"]), "preview": None,
    }}
    verification.record(row, call_item, "intent", now(), flight=row["in_flight"])

store.transaction(record_flight)
Path({str(marker_file)!r}).write_text(json.dumps({{"task_id": task_id, "operation_id": op_id, "child_pid": child_pid, "host_pid": os.getpid()}}), encoding="utf-8")
os._exit(9)
"""
    proc = subprocess.run([sys.executable, "-c", child_script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 9, proc.stderr
    assert not atexit_canary.exists(), "os._exit(9) should bypass Python atexit handlers"
    info = json.loads(marker_file.read_text(encoding="utf-8"))
    child_pid = info["child_pid"]

    def pid_alive(pid):
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in out.stdout

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and pid_alive(child_pid):
        time.sleep(0.1)
    assert not pid_alive(child_pid), f"Child process {child_pid} survived forced host exit"

    key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    new_store = AgentStore(home / "agent", key)
    new_ledger = OperationLedger(home)
    new_local_tools = LocalTools(SimpleNamespace(home=home), lambda: new_ledger)
    new_local_tools.store = new_store

    class NewCatalog:
        def list_models(self):
            return [{"id": "local:test", "name": "Test", "source": "local_api", "available": True, "agent_supported": True}]

    class NewTools:
        def __init__(self, local):
            self.local = local

        def specs(self):
            return []

        def metadata(self, name):
            return {"effect": "write", "scope_context_supported": True, "preview_precondition": True}

        def recover_operation(self, task_id, operation_id=None, *, error="agent_read_interrupted", operation_classification=None):
            return self.local.recover_operation(task_id, operation_id, error=error, operation_classification=operation_classification)

    try:
        restarted_runtime = AgentRuntime(new_store, SimpleNamespace(), NewTools(new_local_tools), NewCatalog())
        restarted_runtime.reconcile()
        after = restarted_runtime.get(info["task_id"])
        assert after["status"] == "queued"
        assert after["error"] is None
        settled = new_ledger.get(info["operation_id"])
        assert settled["status"] == "failed"
        assert settled["no_mutation"] is True
        assert settled["cleanup_confirmed"] is True
        assert "receipt_reserve" not in settled
    finally:
        new_local_tools.processes.close()
