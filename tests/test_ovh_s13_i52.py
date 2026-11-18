"""S13-I52: disk gates prove that production paths release the store lock."""
import copy
import hashlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from test_agent_runtime import advance, assistant, make_runtime
from test_agent_streamed_reads import Reads, setup as streamed_setup, event as streamed_event, raw as streamed_raw
from test_agent_task_forks import env, parent, request, stored
from rynmesh.agent_runtime import results, task_forks
from rynmesh.agent_runtime.store import AgentError


class DiskGate:
    def __init__(self, original):
        self.original = original
        self.entered, self.release = threading.Event(), threading.Event()

    def __call__(self, *args, **kwargs):
        self.entered.set()
        assert self.release.wait(10), "test must release the disk writer"
        return self.original(*args, **kwargs)


def other_task_can_commit(store, other_id, work, gate):
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(work)
        try:
            assert gate.entered.wait(5), "production path must reach the actual disk writer"
            probe = pool.submit(store.transaction, lambda d: d["tasks"][other_id].update(title="committed during disk IO"))
            probe.result(timeout=1.5)
        finally:
            gate.release.set()
        outcome = writer.result(timeout=10)
    assert store.read()["tasks"][other_id]["title"] == "committed during disk IO"
    return outcome


@pytest.mark.parametrize("mode", ["single", "batch", "stream"])
def test_T1_s13_i52_large_results_do_not_hold_store_lock(tmp_path, monkeypatch, mode):
    payload = {"text": "x" * (results.MAX_RESULT_BYTES - 16)}
    if mode == "stream":
        runtime, adapter, selected, _ = streamed_setup(tmp_path, lambda *args: payload)
        row_id = selected["id"]
        def work():
            adapter.on_event(streamed_event())
            return adapter.drain([streamed_raw()])
    else:
        response = assistant(name="inspect_service", args={"service_id": "A"})
        if mode == "batch":
            response = assistant(name="ryn_file_read", args={"path": "A.txt"})
            response["message"]["tool_calls"].extend(assistant(name="ryn_file_read", args={"path": "B.txt"})["message"]["tool_calls"])
        runtime, _, tools, _ = make_runtime(tmp_path, response)
        tools.execute = lambda *args, **kwargs: payload
        if mode == "batch":
            runtime.tools = Reads(lambda *args: payload)
        row_id = runtime.create({"message": "Inspect a large result"})["id"]
        advance(runtime)
        work = runtime.run_once
    other_id = runtime.create({"message": "Independent task"})["id"]
    gate = DiskGate(results.atomic_write_json)
    monkeypatch.setattr(results, "atomic_write_json", gate)
    other_task_can_commit(runtime.store, other_id, work, gate)
    row = runtime.store.read()["tasks"][row_id]
    assert len(row["result_refs"]) == (2 if mode == "batch" else 1)
    for reference in row["result_refs"]:
        assert results.read(runtime.store, row, reference["id"])[0] == payload


def test_T2_s13_i52_failed_transaction_orphan_is_purged(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    row = runtime.create({"message": "Keep reference registration atomic"})
    selected = runtime.store.read()["tasks"][row["id"]]
    reference = results.seal(runtime.store, selected, {"name": "inspect_service", "operation_id": "orphan"}, {"text": "x"*2_000_000})
    def fail(data):
        data["tasks"][row["id"]].setdefault("result_refs", []).append(reference)
        raise AgentError("agent_task_changed")
    with pytest.raises(AgentError, match="agent_task_changed"):
        runtime.store.transaction(fail)
    target = results.path(runtime.store, row["id"], reference["id"])
    assert target.exists() and not runtime.store.read()["tasks"][row["id"]].get("result_refs")
    results.purge(runtime.store, row["id"])
    assert not target.exists()


def attach(e, tid, count=1, size=10):
    directory = e.workspace / ".ryn" / "attachments" / tid
    directory.mkdir(parents=True, exist_ok=True)
    attachments = []
    raw = b"a"*size
    for i in range(count):
        target = directory / f"{i}.txt"
        target.write_bytes(raw)
        attachments.append({"path": str(target), "name": target.name, "size": size, "sha256": hashlib.sha256(raw).hexdigest()})
    e.runtime.store.transaction(lambda d: d["tasks"][tid]["messages"][0].update(attachments=attachments))


def test_T3_s13_i52_forty_mib_fork_does_not_block_other_task(env, monkeypatch):
    e = env
    tid = parent(e)
    other_id = parent(e)
    attach(e, tid, count=4, size=10*1024*1024)
    body = request(e, tid)
    gate = DiskGate(task_forks.atomic_write_bytes)
    monkeypatch.setattr(task_forks, "atomic_write_bytes", gate)
    child = other_task_can_commit(e.runtime.store, other_id, lambda: task_forks.create(e.runtime, tid, body), gate)
    attachments = stored(e, child["id"])["messages"][0]["attachments"]
    assert sum(Path(a["path"]).stat().st_size for a in attachments) == 40*1024*1024
    assert all(hashlib.sha256(Path(a["path"]).read_bytes()).hexdigest() == a["sha256"] for a in attachments)


@pytest.mark.parametrize("change,code", [("boundary", "agent_fork_boundary_changed"), ("copy", "agent_fork_staging_changed")])
def test_T4_s13_i52_validate_after_prewriting_and_remove_copies(env, monkeypatch, change, code):
    e = env
    tid = parent(e)
    attach(e, tid)
    body = request(e, tid)
    original = task_forks._write_copy
    written = []
    def alter(store, target, *args):
        result = original(store, target, *args)
        written.append(Path(target))
        if change == "boundary":
            store.transaction(lambda d: d["tasks"][tid]["messages"][0].update(content="edited during copy"))
        else:
            Path(target).write_bytes(b"tampered")
        return result
    monkeypatch.setattr(task_forks, "_write_copy", alter)
    with pytest.raises(AgentError, match=code):
        task_forks.create(e.runtime, tid, body)
    assert written and all(not path.exists() for path in written)
    assert len(e.runtime.store.read()["tasks"]) == 1


def test_T3_s13_i52_concurrent_same_request_reuses_copies(env):
    e = env
    tid = parent(e)
    attach(e, tid)
    body = request(e, tid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda _: task_forks.create(e.runtime, tid, body), range(2)))
    assert rows[0]["id"] == rows[1]["id"]
    assert len(e.runtime.store.read()["tasks"]) == 2


def test_T2_s13_i52_completed_stream_discards_prewritten_reference(tmp_path, monkeypatch):
    runtime, adapter, selected, _ = streamed_setup(tmp_path)
    adapter.raw[0] = streamed_raw()
    prepared = adapter._admit(0, adapter._normalize(streamed_raw()))
    original = results.atomic_write_json
    def completed(*args, **kwargs):
        original(*args, **kwargs)
        runtime.store.transaction(lambda d: d["tasks"][selected["id"]]["stream_read_journal"][0].update(state="complete"))
    monkeypatch.setattr(results, "atomic_write_json", completed)
    adapter._complete_record(prepared, {"result": {"text": "x"*60000}, "failed": False}, 0)
    row = runtime.store.read()["tasks"][selected["id"]]
    assert not row.get("result_refs")
    blobs = list((runtime.store.root / "results").rglob("result_*.json"))
    assert len(blobs) == 1
    results.purge(runtime.store, selected["id"])
    assert not blobs[0].exists()
