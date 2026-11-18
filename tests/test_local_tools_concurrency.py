"""Tests for S05-I29 (T1..T4): write-tool perform outside process-wide lock."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent_runtime import make_runtime

from rynmesh.agent_runtime.execution_policy import classify_operation
from rynmesh.agent_runtime.local_processes import shell_command
from rynmesh.agent_runtime.local_tools import LocalTools, sha
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected, encoded


def _setup_two_task_tools(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "node"
    home.mkdir()
    runtime, _, _, _ = make_runtime(home / "agent")
    store = runtime.store
    configuration = {"workspace_root": str(workspace), "desktop_access": False}
    runtime.update_settings({"enabled": True, **configuration})
    t_a = runtime.create({"message": "Task A"})
    t_b = runtime.create({"message": "Task B"})
    ledger = OperationLedger(home)
    tools = LocalTools(SimpleNamespace(home=home), lambda: ledger)
    tools.store = store
    ctx_a = {"task_id": t_a["id"], "local_execution": dict(configuration)}
    ctx_b = {"task_id": t_b["id"], "local_execution": dict(configuration)}
    return tools, ledger, workspace, ctx_a, ctx_b


def _run_tool(tools: LocalTools, ctx: dict, name: str, args: dict, op_id: str, *, preview: dict | None = None):
    expected = preview if preview is not None else tools.preview(name, args, ctx)
    return tools.execute(
        name,
        args,
        operation_id=op_id,
        context=ctx,
        expected_preview=expected,
        cancelled=lambda: False,
    )


def test_t1_image_generate_does_not_block_concurrent_apply_patch(tmp_path, monkeypatch):
    from rynmesh.llm_package import cli_adapter, codex_image

    tools, _, workspace, ctx_a, ctx_b = _setup_two_task_tools(tmp_path)
    (workspace / "target.txt").write_text("before\n", encoding="utf-8")

    monkeypatch.setattr(cli_adapter.CLIAgentAdapter, "health", lambda self: {"ok": True})
    gen_started = threading.Event()
    held_logo_lock_during_gen = {"locked": False}

    def slow_generate(prompt, *, transparent_background=False, cancelled=None):
        del prompt, transparent_background, cancelled
        logo_key = os.path.normcase(os.path.abspath(str((workspace / "logo.png").resolve())))
        path_locks = getattr(tools, "_path_locks", {})
        held_logo_lock_during_gen["locked"] = logo_key in path_locks
        gen_started.set()
        time.sleep(5.0)
        return b"\x89PNG\r\n\x1a\nfakepngbytes", {"provider": "codex_native", "mime_type": "image/png"}

    monkeypatch.setattr(codex_image, "generate", slow_generate)

    img_args = {"prompt": "draw a geometric emblem", "path": "logo.png"}
    img_preview = tools.preview("ryn_image_generate", img_args, ctx_a)

    patch_text = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: target.txt",
            "@@",
            "-before",
            "+after_from_task_b",
            "*** Add File: created_by_b.txt",
            "+parallel_ok",
            "*** End Patch",
        ]
    )
    patch_args = {"patch": patch_text}

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            gen_future = pool.submit(_run_tool, tools, ctx_a, "ryn_image_generate", img_args, "op_img_a", preview=img_preview)
            assert gen_started.wait(2.0), "image generation did not enter perform"

            # Task B's apply_patch on unrelated files must complete immediately while Task A blocks 5s.
            t0 = time.monotonic()
            patch_res = _run_tool(tools, ctx_b, "ryn_apply_patch", patch_args, "op_patch_b")
            patch_elapsed = time.monotonic() - t0
            assert patch_res["applied"] is True
            assert patch_elapsed < 1.5, f"apply_patch was blocked for {patch_elapsed:.2f}s by ryn_image_generate"
            assert not gen_future.done(), "ryn_image_generate finished prematurely instead of blocking 5s"
            assert (workspace / "target.txt").read_text(encoding="utf-8") == "after_from_task_b\n"
            assert (workspace / "created_by_b.txt").read_text(encoding="utf-8") == "parallel_ok\n"

            # Short critical sections on `self.lock` (:435 _command_artifacts, :498 _repeat_note)
            # remain unblocked during slow perform.
            r0 = time.monotonic()
            tools.perform("ryn_file_read", {"path": "created_by_b.txt"}, ctx_b, lambda: False)
            second_read = tools.perform("ryn_file_read", {"path": "created_by_b.txt"}, ctx_b, lambda: False)
            fast_shell = {"command": "echo cmd_ok", "shell": "cmd"} if os.name == "nt" else {"command": "echo cmd_ok", "shell": "sh"}
            shell_res = _run_tool(tools, ctx_b, "ryn_shell_execute", fast_shell, "op_shell_during_img")
            assert time.monotonic() - r0 < 2.0
            assert second_read.get("repeat_read") == 1
            assert shell_res.get("exit_code") == 0
            assert "workspace_files" in shell_res
            assert not gen_future.done()

            # Output-path fine lock (`_path_lock`) is held for `logo.png` while Task A generates.
            assert held_logo_lock_during_gen["locked"] is True

            img_res = gen_future.result(timeout=8.0)
            assert img_res["written"] is True
            assert (workspace / "logo.png").read_bytes().startswith(b"\x89PNG")

        assert not getattr(tools, "_path_locks", {"unexpected": 1})
        assert not tools._operation_locks
    finally:
        tools.processes.close()


def test_t2_concurrent_writes_same_path_one_succeeds_one_gets_file_changed_read_again(tmp_path, monkeypatch):
    tools, ledger, workspace, ctx_a, ctx_b = _setup_two_task_tools(tmp_path)
    try:
        # 1. Two tasks concurrently writing the same file via `ryn_file_write`:
        #    both enter `perform` concurrently (outside `self.lock`), serialize on `_path_lock`,
        #    one succeeds and one fails with `agent_local_file_changed_read_again`.
        shared = workspace / "shared.txt"
        shared.write_text("v0\n", encoding="utf-8")
        tools.perform("ryn_file_read", {"path": "shared.txt"}, ctx_a, lambda: False)
        tools.perform("ryn_file_read", {"path": "shared.txt"}, ctx_b, lambda: False)

        args_a = {"path": "shared.txt", "content": "from_task_a\n"}
        args_b = {"path": "shared.txt", "content": "from_task_b\n"}
        prev_a = tools.preview("ryn_file_write", args_a, ctx_a)
        prev_b = tools.preview("ryn_file_write", args_b, ctx_b)

        orig_perform = tools.perform
        write_barrier = threading.Barrier(2, timeout=2.0)

        def synced_perform(name, args, context, cancelled, **kwargs):
            if name == "ryn_file_write" and args.get("path") == "shared.txt":
                write_barrier.wait()
            return orig_perform(name, args, context, cancelled, **kwargs)

        monkeypatch.setattr(tools, "perform", synced_perform)

        outcomes: list[tuple[str, str, object]] = []
        outcomes_lock = threading.Lock()

        def worker(tag: str, ctx: dict, args: dict, op_id: str, prev: dict) -> None:
            try:
                res = _run_tool(tools, ctx, "ryn_file_write", args, op_id, preview=prev)
                with outcomes_lock:
                    outcomes.append((tag, "ok", res))
            except BaseException as exc:
                with outcomes_lock:
                    outcomes.append((tag, "err", exc))

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(worker, "a", ctx_a, args_a, "op_same_path_a", prev_a)
            f2 = pool.submit(worker, "b", ctx_b, args_b, "op_same_path_b", prev_b)
            f1.result(timeout=5.0)
            f2.result(timeout=5.0)

        oks = [item for item in outcomes if item[1] == "ok"]
        errs = [item for item in outcomes if item[1] == "err"]
        assert len(oks) == 1, f"expected 1 success, got {outcomes}"
        assert len(errs) == 1, f"expected 1 rejection, got {outcomes}"
        loser_tag, _, loser_exc = errs[0]
        assert isinstance(loser_exc, AgentToolRejected)
        assert str(loser_exc) == "agent_local_file_changed_read_again"
        winner_tag = oks[0][0]
        assert shared.read_text(encoding="utf-8") == (f"from_task_{winner_tag}\n")
        loser_op = "op_same_path_a" if loser_tag == "a" else "op_same_path_b"
        loser_row = ledger.get(loser_op)
        assert loser_row["status"] == "not_executed"
        assert loser_row["error"] == "agent_local_file_changed_read_again"

        # 2. Same-path contention via `ryn_apply_patch` (even touching different lines of the same file):
        shared_patch = workspace / "shared_patch.txt"
        shared_patch.write_text("top\nmiddle\nbottom\n", encoding="utf-8")
        patch_a = {"patch": "*** Begin Patch\n*** Update File: shared_patch.txt\n@@\n-top\n+top_a\n middle\n*** End Patch"}
        patch_b = {"patch": "*** Begin Patch\n*** Update File: shared_patch.txt\n@@\n middle\n-bottom\n+bottom_b\n*** End Patch"}
        pp_a = tools.preview("ryn_apply_patch", patch_a, ctx_a)
        pp_b = tools.preview("ryn_apply_patch", patch_b, ctx_b)

        patch_barrier = threading.Barrier(2, timeout=2.0)

        def synced_patch_perform(name, args, context, cancelled, **kwargs):
            if name == "ryn_apply_patch":
                patch_barrier.wait()
            return orig_perform(name, args, context, cancelled, **kwargs)

        monkeypatch.setattr(tools, "perform", synced_patch_perform)
        patch_outcomes: list[tuple[str, object]] = []

        def patch_worker(ctx: dict, args: dict, op_id: str, prev: dict) -> None:
            try:
                res = _run_tool(tools, ctx, "ryn_apply_patch", args, op_id, preview=prev)
                with outcomes_lock:
                    patch_outcomes.append(("ok", res))
            except BaseException as exc:
                with outcomes_lock:
                    patch_outcomes.append(("err", exc))

        with ThreadPoolExecutor(max_workers=2) as pool:
            pf1 = pool.submit(patch_worker, ctx_a, patch_a, "op_patch_same_a", pp_a)
            pf2 = pool.submit(patch_worker, ctx_b, patch_b, "op_patch_same_b", pp_b)
            pf1.result(timeout=5.0)
            pf2.result(timeout=5.0)

        patch_oks = [x for x in patch_outcomes if x[0] == "ok"]
        patch_errs = [x for x in patch_outcomes if x[0] == "err"]
        assert len(patch_oks) == 1
        assert len(patch_errs) == 1
        assert isinstance(patch_errs[0][1], AgentToolRejected)
        assert str(patch_errs[0][1]) == "agent_local_file_changed_read_again"

        # 3. Concurrent new-file creation on the SAME path (`brand_new.txt`): one succeeds, one gets `agent_local_file_changed_read_again`.
        new_args_a = {"path": "brand_new.txt", "content": "created_a\n"}
        new_args_b = {"path": "brand_new.txt", "content": "created_b\n"}
        np_a = tools.preview("ryn_file_write", new_args_a, ctx_a)
        np_b = tools.preview("ryn_file_write", new_args_b, ctx_b)
        new_barrier = threading.Barrier(2, timeout=2.0)

        def synced_new_perform(name, args, context, cancelled, **kwargs):
            if name == "ryn_file_write" and args.get("path") == "brand_new.txt":
                new_barrier.wait()
            return orig_perform(name, args, context, cancelled, **kwargs)

        monkeypatch.setattr(tools, "perform", synced_new_perform)
        new_outcomes: list[tuple[str, object]] = []

        def new_worker(ctx: dict, args: dict, op_id: str, prev: dict) -> None:
            try:
                res = _run_tool(tools, ctx, "ryn_file_write", args, op_id, preview=prev)
                with outcomes_lock:
                    new_outcomes.append(("ok", res))
            except BaseException as exc:
                with outcomes_lock:
                    new_outcomes.append(("err", exc))

        with ThreadPoolExecutor(max_workers=2) as pool:
            nf1 = pool.submit(new_worker, ctx_a, new_args_a, "op_new_same_a", np_a)
            nf2 = pool.submit(new_worker, ctx_b, new_args_b, "op_new_same_b", np_b)
            nf1.result(timeout=5.0)
            nf2.result(timeout=5.0)

        assert len([x for x in new_outcomes if x[0] == "ok"]) == 1
        new_errs = [x for x in new_outcomes if x[0] == "err"]
        assert len(new_errs) == 1
        assert isinstance(new_errs[0][1], AgentToolRejected)
        assert str(new_errs[0][1]) == "agent_local_file_changed_read_again"

        # 4. Different paths (`diff_a.txt` vs `diff_b.txt`) acquire separate `_path_locks` in parallel and both succeed:
        diff_barrier = threading.Barrier(2, timeout=2.0)
        orig_file_review = tools.file_review

        def parallel_file_review(task_id_arg, ws_root_arg):
            # Reached INSIDE `with self._path_lock(path):` — proves two distinct path locks are held simultaneously!
            diff_barrier.wait()
            return orig_file_review(task_id_arg, ws_root_arg)

        monkeypatch.setattr(tools, "perform", orig_perform)
        monkeypatch.setattr(tools, "file_review", parallel_file_review)
        da = {"path": "diff_a.txt", "content": "alpha\n"}
        db = {"path": "diff_b.txt", "content": "beta\n"}
        dpa = tools.preview("ryn_file_write", da, ctx_a)
        dpb = tools.preview("ryn_file_write", db, ctx_b)
        with ThreadPoolExecutor(max_workers=2) as pool:
            df1 = pool.submit(_run_tool, tools, ctx_a, "ryn_file_write", da, "op_diff_a", preview=dpa)
            df2 = pool.submit(_run_tool, tools, ctx_b, "ryn_file_write", db, "op_diff_b", preview=dpb)
            assert df1.result(timeout=5.0)["written"] is True
            assert df2.result(timeout=5.0)["written"] is True

        assert (workspace / "diff_a.txt").read_text(encoding="utf-8") == "alpha\n"
        assert (workspace / "diff_b.txt").read_text(encoding="utf-8") == "beta\n"
        assert not getattr(tools, "_path_locks", {"unexpected": 1})
    finally:
        tools.processes.close()


def test_t3_same_operation_id_reentry_rejected(tmp_path, monkeypatch):
    from rynmesh.llm_package import cli_adapter, codex_image

    tools, _, workspace, ctx_a, ctx_b = _setup_two_task_tools(tmp_path)
    monkeypatch.setattr(cli_adapter.CLIAgentAdapter, "health", lambda self: {"ok": True})

    try:
        # 1. Same-thread recursive re-entry with the same `operation_id` while `perform` is active:
        #    must NOT deadlock and must be rejected immediately.
        reentry_outcome: dict[str, object] = {}

        def reentering_generate(prompt, *, transparent_background=False, cancelled=None):
            del prompt, transparent_background, cancelled
            nested_args = {"path": "nested.txt", "content": "nested\n"}
            nested_prev = tools.preview("ryn_file_write", nested_args, ctx_a)
            try:
                tools.execute(
                    "ryn_file_write",
                    nested_args,
                    operation_id="op_recursive",
                    context=ctx_a,
                    expected_preview=nested_prev,
                    cancelled=lambda: False,
                )
                reentry_outcome["err"] = None
            except BaseException as exc:
                reentry_outcome["err"] = exc
            return b"\x89PNG\r\n\x1a\nok", {"provider": "codex_native", "mime_type": "image/png"}

        monkeypatch.setattr(codex_image, "generate", reentering_generate)
        img_args = {"prompt": "icon", "path": "recursive_icon.png"}
        img_prev = tools.preview("ryn_image_generate", img_args, ctx_a)

        rec_holder: dict[str, object] = {}

        def run_recursive_outer() -> None:
            try:
                rec_holder["res"] = _run_tool(tools, ctx_a, "ryn_image_generate", img_args, "op_recursive", preview=img_prev)
            except BaseException as exc:
                rec_holder["outer_err"] = exc

        rec_thread = threading.Thread(target=run_recursive_outer, daemon=True)
        rec_thread.start()
        rec_thread.join(timeout=1.5)
        assert not rec_thread.is_alive(), "same-thread recursive operation_id re-entry deadlocked"
        assert "outer_err" not in rec_holder, f"unexpected outer error: {rec_holder.get('outer_err')}"
        rec_res = rec_holder["res"]
        assert rec_res["written"] is True
        assert isinstance(reentry_outcome.get("err"), AgentToolRejected)
        assert str(reentry_outcome["err"]) == "agent_operation_id_conflict"
        assert not (workspace / "nested.txt").exists()

        # 2. Cross-thread concurrent re-entry with the same `operation_id` while `perform` is running:
        #    second caller waits on `_operation_lock(operation_id)` (never runs `perform` concurrently)
        #    and is rejected with `agent_operation_id_conflict` when binding differs.
        in_gen = threading.Event()
        release_gen = threading.Event()
        active_in_perform = {"count": 0, "max_concurrent": 0}
        count_lock = threading.Lock()

        def held_generate(prompt, *, transparent_background=False, cancelled=None):
            del prompt, transparent_background, cancelled
            with count_lock:
                active_in_perform["count"] += 1
                active_in_perform["max_concurrent"] = max(active_in_perform["max_concurrent"], active_in_perform["count"])
            in_gen.set()
            release_gen.wait(timeout=4.0)
            with count_lock:
                active_in_perform["count"] -= 1
            return b"\x89PNG\r\n\x1a\nok2", {"provider": "codex_native", "mime_type": "image/png"}

        monkeypatch.setattr(codex_image, "generate", held_generate)
        args_first = {"prompt": "first", "path": "first_icon.png"}
        prev_first = tools.preview("ryn_image_generate", args_first, ctx_a)
        args_second = {"path": "conflict.txt", "content": "should_not_write\n"}
        prev_second = tools.preview("ryn_file_write", args_second, ctx_b)

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut1 = pool.submit(_run_tool, tools, ctx_a, "ryn_image_generate", args_first, "op_dup_cross", preview=prev_first)
            assert in_gen.wait(2.0)
            fut2 = pool.submit(_run_tool, tools, ctx_b, "ryn_file_write", args_second, "op_dup_cross", preview=prev_second)
            time.sleep(0.15)
            assert not fut2.done(), "second caller bypassed _operation_lock while first was in perform"
            release_gen.set()
            assert fut1.result(timeout=4.0)["written"] is True
            with pytest.raises(AgentToolRejected, match="agent_operation_id_conflict"):
                fut2.result(timeout=4.0)

        assert active_in_perform["max_concurrent"] == 1
        assert not (workspace / "conflict.txt").exists()

        # 3. Re-entry with identical binding after an unconfirmed failure in `perform`:
        #    rejected with `agent_write_outcome_unknown` without re-running `perform`.
        boom_calls = {"n": 0}

        def failing_generate(prompt, *, transparent_background=False, cancelled=None):
            del prompt, transparent_background, cancelled
            boom_calls["n"] += 1
            raise RuntimeError("simulated mid-flight provider crash")

        monkeypatch.setattr(codex_image, "generate", failing_generate)
        args_boom = {"prompt": "boom", "path": "boom.png"}
        prev_boom = tools.preview("ryn_image_generate", args_boom, ctx_a)
        with pytest.raises(AgentError, match="agent_local_operation_unconfirmed"):
            _run_tool(tools, ctx_a, "ryn_image_generate", args_boom, "op_boom", preview=prev_boom)
        with pytest.raises(AgentError, match="agent_write_outcome_unknown"):
            _run_tool(tools, ctx_a, "ryn_image_generate", args_boom, "op_boom", preview=prev_boom)
        assert boom_calls["n"] == 1
        assert not tools._operation_locks
        assert not getattr(tools, "_path_locks", {"unexpected": 1})
    finally:
        tools.processes.close()


def test_t4_recover_operation_restart_recovery_unchanged(tmp_path, monkeypatch):
    from rynmesh.llm_package import cli_adapter, codex_image

    tools, ledger, workspace, ctx_a, ctx_b = _setup_two_task_tools(tmp_path)
    monkeypatch.setattr(cli_adapter.CLIAgentAdapter, "health", lambda self: {"ok": True})

    gen_started = threading.Event()
    release_gen = threading.Event()

    def blocking_generate(prompt, *, transparent_background=False, cancelled=None):
        del prompt, transparent_background, cancelled
        gen_started.set()
        release_gen.wait(timeout=5.0)
        return b"\x89PNG\r\n\x1a\nrec", {"provider": "codex_native", "mime_type": "image/png"}

    monkeypatch.setattr(codex_image, "generate", blocking_generate)

    try:
        # 1. Start a real background process tied to `op_read_running` and a `"dispatched"` read-shell ledger entry.
        read_args = {"command": "Get-Location" if os.name == "nt" else "pwd"}
        read_cls = classify_operation(
            "ryn_shell_execute",
            read_args,
            {"effect": "write"},
            operation_id="op_read_running",
            task_id=ctx_b["task_id"],
            workspace_root=str(workspace),
        )
        read_binding = {
            "operation_id": "op_read_running",
            "tool": "ryn_shell_execute",
            "task_id": ctx_b["task_id"],
            "arguments_sha256": sha(encoded(read_args)),
            "local_execution": ctx_b["local_execution"],
        }
        ledger.begin_dispatched(
            read_binding,
            {"title": "Read shell"},
            operation_classification=read_cls,
            host_pid=os.getpid(),
            receipt_reserve=32768,
        )

        slow_shell = "powershell" if os.name == "nt" else "sh"
        slow_argv = shell_command(slow_shell, "Start-Sleep 30" if os.name == "nt" else "sleep 30")
        proc_id = tools.processes.start(
            slow_argv,
            workspace,
            ctx_b["task_id"],
            30,
            lambda: True,
            lambda: False,
            operation_id="op_read_running",
        )
        assert tools.processes.running_for_operation(ctx_b["task_id"], "op_read_running")

        # 2. Also record an `"intent"` read-shell entry and an unconfirmed `"dispatched"` write entry (`ryn_file_write`).
        intent_cls = classify_operation(
            "ryn_shell_execute",
            read_args,
            {"effect": "write"},
            operation_id="op_read_intent",
            task_id=ctx_b["task_id"],
            workspace_root=str(workspace),
        )
        intent_binding = {**read_binding, "operation_id": "op_read_intent"}
        ledger.begin(intent_binding, {"title": "Intent read"}, receipt_reserve=32768)

        write_args = {"path": "unconfirmed.txt", "content": "maybe\n"}
        write_cls = classify_operation(
            "ryn_file_write",
            write_args,
            {"effect": "write"},
            operation_id="op_write_dispatched",
            task_id=ctx_b["task_id"],
            workspace_root=str(workspace),
        )
        write_binding = {
            "operation_id": "op_write_dispatched",
            "tool": "ryn_file_write",
            "task_id": ctx_b["task_id"],
            "arguments_sha256": sha(encoded(write_args)),
            "local_execution": ctx_b["local_execution"],
        }
        ledger.begin(write_binding, {"title": "Write"}, receipt_reserve=32768)
        ledger.update("op_write_dispatched", "dispatched", operation_classification=write_cls, host_pid=os.getpid())

        # 3. Run `recover_operation` and a follow-up file write while Task A is blocked inside `ryn_image_generate`.
        img_args = {"prompt": "bg", "path": "bg.png"}
        img_prev = tools.preview("ryn_image_generate", img_args, ctx_a)

        with ThreadPoolExecutor(max_workers=2) as pool:
            gen_fut = pool.submit(_run_tool, tools, ctx_a, "ryn_image_generate", img_args, "op_img_rec", preview=img_prev)
            assert gen_started.wait(2.0)

            t0 = time.monotonic()
            rec_dispatched = tools.recover_operation(
                ctx_b["task_id"],
                "op_read_running",
                error="agent_node_restarted",
                operation_classification=read_cls,
            )
            rec_intent = tools.recover_operation(
                ctx_b["task_id"],
                "op_read_intent",
                error="agent_node_restarted",
                operation_classification=intent_cls,
            )
            rec_write = tools.recover_operation(
                ctx_b["task_id"],
                "op_write_dispatched",
                error="agent_node_restarted",
                operation_classification=write_cls,
            )
            followup_res = _run_tool(
                tools,
                ctx_b,
                "ryn_file_write",
                {"path": "after_recovery.txt", "content": "recovered_ok\n"},
                "op_after_recovery",
            )
            elapsed = time.monotonic() - t0
            assert elapsed < 2.0, f"recovery + file write blocked for {elapsed:.2f}s behind slow perform"
            assert not gen_fut.done()

            release_gen.set()
            assert gen_fut.result(timeout=4.0)["written"] is True

        # Verify `recover_operation` semantics are completely unchanged:
        assert rec_dispatched["ok"] is True
        assert rec_dispatched["had_running"] is True
        assert rec_dispatched["still_running"] is False
        assert proc_id in rec_dispatched["stopped_ids"]
        assert rec_dispatched["ledger_settled"] is True
        assert rec_dispatched["ledger_outcome"] == "failed"
        settled_read = ledger.get("op_read_running")
        assert settled_read["status"] == "failed"
        assert settled_read["no_mutation"] is True
        assert settled_read["cleanup_confirmed"] is True
        assert "receipt_reserve" not in settled_read

        assert rec_intent["ok"] is True
        assert rec_intent["ledger_outcome"] == "not_executed"
        assert ledger.get("op_read_intent")["status"] == "not_executed"

        # The write stays dispatched until its file-review journal is consulted.
        assert rec_write["ok"] is False
        assert rec_write["ledger_settled"] is False
        assert ledger.get("op_write_dispatched")["status"] == "dispatched"
        reconciled = tools.reconcile(
            "op_write_dispatched",
            {
                **ctx_b,
                "operation": {
                    "tool": "ryn_file_write",
                    "arguments_sha256": write_binding["arguments_sha256"],
                },
            },
        )
        assert reconciled["outcome"] == "not_executed"
        assert reconciled["receipt"]["result"] == {"not_executed": True, "no_mutation": True, "files": []}
        assert followup_res["written"] is True
        assert (workspace / "after_recovery.txt").read_text(encoding="utf-8") == "recovered_ok\n"
    finally:
        release_gen.set()
        tools.processes.close()
