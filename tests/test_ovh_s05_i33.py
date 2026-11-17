"""S05-I33 checked-write sharing violations and durable, read-only crash recovery."""
import ctypes
import os
import uuid
from pathlib import Path

import pytest
from test_agent_local_tools import execute, local  # noqa: F401

from rynmesh.agent_runtime import file_review
from rynmesh.agent_runtime.file_review import FileReview, digest, fingerprint
from rynmesh.agent_runtime.file_review_io import replace_checked
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected


def in_use(path):
    error = AgentToolRejected("agent_local_file_in_use",
        correction="The file is open in another program (for example Excel or Word). Ask the owner to close it, then retry.",
        details={"path": str(path)})
    error.nothing_written = True
    return error


def fake_windows_failure(monkeypatch, target, error, *, target_open=True):
    from ctypes import wintypes
    win_dll = ctypes.WinDLL
    kernel = win_dll("kernel32", use_last_error=True)
    real = kernel.CreateFileW
    real.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    real.restype = wintypes.HANDLE
    def create(path, access, share, security, creation, flags, template):
        if os.path.normcase(str(path)) == os.path.normcase(str(target)) and bool(access == 0xC0010000) == target_open:
            ctypes.set_last_error(error)
            return wintypes.HANDLE(-1).value
        return real(path, access, share, security, creation, flags, template)
    kernel.CreateFileW = create
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: kernel if name == "kernel32" else win_dll(name, **kw))


def review_for(tools, context, workspace):
    return tools.file_review(context["task_id"], workspace)


def reconcile(tools, context, operation_id):
    binding = tools.ledger_factory().get(operation_id)["binding"]
    return tools.reconcile(operation_id, {**context, "operation": {
        "tool": binding["tool"], "arguments_sha256": binding["arguments_sha256"]}})


@pytest.mark.skipif(os.name != "nt", reason="Windows CreateFileW sharing/lock errors")
@pytest.mark.parametrize("error", [32, 33])
def test_t1_target_handle_in_use_is_not_executed_and_can_retry(local, monkeypatch, error):
    tools, context, workspace, _ = local
    path = workspace / "office.txt"
    path.write_bytes(b"before\n")
    with monkeypatch.context() as m:
        fake_windows_failure(m, path, error)
        with pytest.raises(AgentToolRejected, match="^agent_local_file_in_use$") as caught:
            execute(tools, context, "ryn_file_write", {"path": path.name, "content": "after\n"}, "busy")
    assert caught.value.nothing_written and not getattr(caught.value, "after_side_effect", False)
    assert "Excel or Word" in caught.value.correction and caught.value.details["path"] == str(path)
    assert tools.ledger_factory().get("busy")["status"] == "not_executed"
    assert review_for(tools, context, workspace).load()["records"] == []
    assert path.read_bytes() == b"before\n"
    execute(tools, context, "ryn_file_write", {"path": path.name, "content": "after\n"}, "retry")
    assert path.read_bytes() == b"after\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows directory handles keep their existing error contract")
def test_parent_handle_error_keeps_oserror(local, monkeypatch):
    _, _, workspace, _ = local
    path = workspace / "document.txt"
    path.write_bytes(b"before")
    fake_windows_failure(monkeypatch, workspace, 32, target_open=False)
    with pytest.raises(OSError) as caught:
        replace_checked(path, workspace, digest(b"before"), b"after", 0o644, fingerprint(path))
    assert caught.value.errno == 32 and path.read_bytes() == b"before"


def test_t2_second_file_in_use_keeps_first_write_and_uncertainty(local, monkeypatch):
    tools, context, workspace, _ = local
    for name in ("a.txt", "b.txt"):
        (workspace / name).write_bytes(b"old\n")
    original = FileReview.replace
    def replace(self, step, *a, **kw):
        if Path(step["path"]).name == "b.txt":
            raise in_use(step["path"])
        return original(self, step, *a, **kw)
    patch = "*** Begin Patch\n*** Update File: a.txt\n@@\n-old\n+new\n*** Update File: b.txt\n@@\n-old\n+new\n*** End Patch"
    monkeypatch.setattr(FileReview, "replace", replace)
    with pytest.raises(AgentError, match="^agent_local_operation_unconfirmed$") as caught:
        execute(tools, context, "ryn_apply_patch", {"patch": patch}, "partial-busy")
    assert caught.value.__cause__.after_side_effect
    assert str(caught.value.__cause__) == "agent_local_file_in_use"
    assert (workspace / "a.txt").read_bytes() == b"new\n" and (workspace / "b.txt").read_bytes() == b"old\n"
    assert tools.ledger_factory().get("partial-busy")["status"] == "dispatched"
    result = reconcile(tools, context, "partial-busy")
    assert result["outcome"] == "unknown"
    assert {f["path"]: f["state"] for f in result["diagnostic"]["files"]} == {"a.txt": "applied", "b.txt": "not_applied"}


def interrupted_write(local, monkeypatch, *, stop=0, effect="before", count=1, operation_id="crash"):
    tools, context, workspace, _ = local
    names = [f"file-{i}.txt" for i in range(count)]
    for name in names:
        (workspace / name).write_bytes(b"before\n")
    original = FileReview.replace
    def replace(self, step, *a, **kw):
        if Path(step["path"]).name == names[stop]:
            if effect == "after":
                original(self, step, *a, **kw)
            elif effect == "partial":
                Path(step["path"]).write_bytes(b"part")
            raise KeyboardInterrupt("simulated process loss")
        return original(self, step, *a, **kw)
    patch = "*** Begin Patch\n" + "".join(f"*** Update File: {name}\n@@\n-before\n+after\n" for name in names) + "*** End Patch"
    with monkeypatch.context() as m:
        m.setattr(FileReview, "replace", replace)
        with pytest.raises(KeyboardInterrupt):
            execute(tools, context, "ryn_apply_patch", {"patch": patch}, operation_id)
    return review_for(tools, context, workspace), names


@pytest.mark.parametrize("effect,expected", [("after", "applied"), ("before", "not_applied"), ("partial", "partial_write")])
def test_t3_recover_hash_states_without_writing_user_file_and_restore_partial(local, monkeypatch, effect, expected):
    review, names = interrupted_write(local, monkeypatch, effect=effect)
    path = Path(review.root) / names[0]
    before = path.read_bytes()
    state = review.load()
    review.recover(state)
    step = state["records"][0]["steps"][0]
    assert step["state"] == expected and path.read_bytes() == before
    if expected == "applied":
        assert step["after_stamp"] == fingerprint(path)
    if expected == "partial_write":
        assert review.content(step["after"]) == b"part"
        file = review.review()["files"][0]
        assert file["problem"] == "partial_write" and file["restore_available"] and file["can_undo"]
        preview = review.preview()
        assert preview["can_undo"]
        assert review.undo(None, preview["token"], "restore")["status"] == "completed"
        assert path.read_bytes() == b"before\n"


@pytest.mark.parametrize("blocked_index", [0, 1])
def test_t4_undo_in_use_is_pending_not_interrupted(local, monkeypatch, blocked_index):
    tools, context, workspace, _ = local
    review = review_for(tools, context, workspace)
    changes = []
    for name in ("a.txt", "b.txt"):
        path = workspace / name
        path.write_bytes(b"before")
        changes.append((path, b"before", b"after", None))
    review.apply(changes, operation_id="write-two")
    preview = review.preview()
    target = changes[blocked_index][0]
    original = FileReview.replace
    def replace(self, step, *a, **kw):
        if step["path"] == str(target):
            raise in_use(target)
        return original(self, step, *a, **kw)
    with monkeypatch.context() as m:
        m.setattr(FileReview, "replace", replace)
        result = review.undo(None, preview["token"], "blocked")
    assert result["status"] == "blocked_file_in_use"
    assert result["remaining"][0] == {"path": target.name, "state": "file_in_use"}
    assert review.load()["undos"][-1]["items"][blocked_index]["state"] == "pending"
    assert review.review()["undo_operations"][-1]["status"] == "blocked_file_in_use"
    assert target.read_bytes() == b"after"
    preview = review.preview()
    assert review.undo(None, preview["token"], "retry-undo")["status"] == "completed"
    assert all(path.read_bytes() == b"before" for path, *_ in changes)


@pytest.mark.parametrize("stop,effect,outcome", [(2, "after", "executed"), (0, "before", "not_executed"), (1, "partial", "unknown")])
def test_t5_t6_t7_reconcile_all_none_and_partial_file_receipts(local, monkeypatch, stop, effect, outcome):
    tools, context, _, _ = local
    review, _ = interrupted_write(local, monkeypatch, stop=stop, effect=effect, count=3)
    result = reconcile(tools, context, "crash")
    assert result["outcome"] == outcome
    summary = review.record_outcome("crash")
    assert len(summary["files"]) == 3
    if outcome == "executed":
        assert summary["all_applied"] and result["receipt"]["result"]["recovered_from_review_log"]
    elif outcome == "not_executed":
        assert summary["none_applied"]
        assert result["receipt"]["result"]["not_executed"] is True and result["receipt"]["result"]["no_mutation"] is True
    else:
        assert [f["state"] for f in result["diagnostic"]["files"]] == ["applied", "partial_write", "not_applied"]


@pytest.mark.parametrize("effect,second", [("before", "not_applied"), ("after", "applied"), ("partial", "partial_write")])
def test_t8_second_file_interruption_keeps_unstarted_file_visible_and_applied_part_undoable(local, monkeypatch, effect, second):
    tools, context, workspace, _ = local
    review, names = interrupted_write(local, monkeypatch, stop=1, effect=effect, count=3)
    assert reconcile(tools, context, "crash")["outcome"] == "unknown"
    files = {f["path"]: f for f in review.review()["files"]}
    assert files[names[2]]["state"] == "not_applied" and not files[names[2]]["can_undo"]
    assert files[names[0]]["can_undo"] and files[names[1]]["state"] == second
    preview = review.preview()
    assert preview["can_undo"]
    assert review.undo(None, preview["token"], "undo-applied-part")["status"] == "completed"
    assert all((workspace / name).read_bytes() == b"before\n" for name in names)


def test_t9_confirmed_ledger_result_is_unchanged(local, monkeypatch):
    tools, context, _, _ = local
    original = execute(tools, context, "ryn_file_write", {"path": "done.txt", "content": "done"}, "confirmed")
    monkeypatch.setattr(FileReview, "record_outcome", lambda *_: pytest.fail("confirmed receipts do not inspect review"), raising=False)
    result = reconcile(tools, context, "confirmed")
    assert result["outcome"] == "executed" and result["receipt"]["result"] == original


def test_dispatched_without_review_intent_is_not_executed(local, monkeypatch):
    tools, context, _, _ = local
    with monkeypatch.context() as m:
        m.setattr(FileReview, "apply", lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            execute(tools, context, "ryn_file_write", {"path": "never.txt", "content": "never"}, "no-intent")
    assert reconcile(tools, context, "no-intent")["outcome"] == "not_executed"


@pytest.mark.parametrize("action", ["mkdir", "delete"])
def test_unreviewed_directory_mutation_never_claims_no_mutation(local, action):
    tools, context, workspace, _ = local
    if action == "delete":
        (workspace / "directory").mkdir()
    execute(tools, context, "ryn_file_manage", {"path": "directory", "action": action}, "directory")
    tools.ledger_factory().update("directory", "dispatched", result=None)
    assert reconcile(tools, context, "directory")["outcome"] == "unknown"


def test_overflow_summary_cannot_prove_absent_operation_did_nothing(local, monkeypatch):
    tools, context, _, _ = local
    monkeypatch.setattr(file_review, "MAX_OPERATIONS", 0)
    execute(tools, context, "ryn_file_write", {"path": "overflow.txt", "content": "written"}, "overflow")
    tools.ledger_factory().update("overflow", "dispatched", result=None)
    assert reconcile(tools, context, "overflow")["outcome"] == "unknown"


def test_busy_first_step_preserves_prior_overflow_summary(local, monkeypatch):
    tools, context, workspace, _ = local
    monkeypatch.setattr(file_review, "MAX_OPERATIONS", 0)
    execute(tools, context, "ryn_file_write", {"path": "prior.txt", "content": "prior"}, "prior")
    review = review_for(tools, context, workspace)
    records = review.load()["records"]
    with monkeypatch.context() as m:
        m.setattr(FileReview, "replace", lambda self, step, *a, **kw: (_ for _ in ()).throw(in_use(step["path"])))
        with pytest.raises(AgentToolRejected, match="^agent_local_file_in_use$"):
            execute(tools, context, "ryn_file_write", {"path": "blocked.txt", "content": "blocked"}, "blocked")
    assert review.load()["records"] == records
    assert tools.ledger_factory().get("blocked")["status"] == "not_executed"


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows file sharing handle")
def test_t1_real_shared_reader_blocks_in_place_write_without_mutation(local):
    from ctypes import wintypes
    tools, context, workspace, _ = local
    path = workspace / "opened.xlsx"
    path.write_bytes(b"document")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0, None)
    assert handle != wintypes.HANDLE(-1).value
    try:
        with pytest.raises(AgentToolRejected, match="^agent_local_file_in_use$"):
            execute(tools, context, "ryn_file_write", {"path": path.name, "content": "replacement"}, "real-busy")
        assert path.read_bytes() == b"document"
        assert tools.ledger_factory().get("real-busy")["status"] == "not_executed"
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.parametrize("tool,args", [
    ("ryn_file_edit", {"path": "source.txt", "old_text": "before", "new_text": "after"}),
    ("ryn_file_manage", {"path": "source.txt", "action": "copy", "destination": "copy.txt"}),
    ("ryn_file_manage", {"path": "source.txt", "action": "move", "destination": "moved.txt"}),
    ("ryn_file_manage", {"path": "source.txt", "action": "delete"}),
])
def test_t7_other_file_tools_reconcile_from_real_review_intent(local, monkeypatch, tool, args):
    tools, context, workspace, _ = local
    (workspace / "source.txt").write_bytes(b"before")
    original = FileReview.apply
    def lost_response(self, *a, **kw):
        original(self, *a, **kw)
        raise KeyboardInterrupt()
    with monkeypatch.context() as m:
        m.setattr(FileReview, "apply", lost_response)
        with pytest.raises(KeyboardInterrupt):
            execute(tools, context, tool, args, "lost-response")
    result = reconcile(tools, context, "lost-response")
    assert result["outcome"] == "executed"
    assert result["receipt"]["result"]["recovered_from_review_log"] is True


def test_t3_partial_snapshot_still_checks_subsequent_user_edit(local, monkeypatch):
    review, names = interrupted_write(local, monkeypatch, effect="partial")
    assert review.review()["files"][0]["restore_available"]
    path = Path(review.root) / names[0]
    path.write_bytes(b"user work")
    preview = review.preview()
    assert not preview["can_undo"] and preview["conflicts"][0]["reason"] == "file_changed"
    assert review.undo(None, preview["token"], "keep-user-work")["status"] == "conflict"
    assert path.read_bytes() == b"user work"


def test_t3_hash_only_partial_never_promises_snapshot_restore(local, monkeypatch):
    monkeypatch.setattr(file_review, "MAX_FILES", 0)
    review, _ = interrupted_write(local, monkeypatch, effect="partial")
    entry = review.review()["files"][0]
    assert entry["state"] == "partial_write" and not entry["restore_available"] and not entry["can_undo"]
    assert entry["problem"] == "snapshot_skipped_limit"
    assert not review.preview()["can_undo"]
