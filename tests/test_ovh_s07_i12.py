"""S07-I12: encrypted, multi-generation task lifecycle on real runtime paths."""
import copy

import pytest

from test_agent_runtime import advance, assistant, make_runtime
from test_agent_runtime_lifecycle import change
from test_agent_storage_relief import finished_history
from test_agent_subagents import _build_env
from rynmesh.agent_runtime import lifecycle, results, storage_relief, subagents
from rynmesh.agent_runtime.runtime import event
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected


def tree(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("done"))
    root = runtime.create({"message": "coordinate"})["id"]
    advance(runtime)
    def spawn(data):
        parent = data["tasks"][root]
        child = subagents.spawn_agent_in_tx(data, parent, {
            "task_name": "child", "prompt": "inspect", "role": "explorer"})["agent_id"]
        leaf = subagents.spawn_agent_in_tx(data, data["tasks"][child], {
            "task_name": "leaf", "prompt": "inspect leaf", "role": "explorer"})["agent_id"]
        for task_id in (child, leaf):
            data["tasks"][task_id].update(status="completed", queue=[], in_flight=None)
        return root, child, leaf
    return runtime, runtime.store.transaction(spawn)


def result_files(runtime, ids):
    paths = []
    for task_id in ids:
        row = runtime.store.read()["tasks"][task_id]
        ref = results.seal(runtime.store, row, {"name": "inspect", "operation_id": "op_" + task_id}, {"text": "private result"})
        paths.append(results.path(runtime.store, task_id, ref["id"]))
    return paths


def test_t1_archive_two_generations_in_postorder_with_independent_encrypted_stores(tmp_path):
    runtime, (root, child, leaf) = tree(tmp_path)
    change(runtime, root, "archive")
    data = runtime.store.read()
    assert not set((root, child, leaf)) & data["tasks"].keys()
    for task_id in (root, child, leaf):
        saved = lifecycle.archive_store(runtime.store, task_id)
        assert saved.path.exists()
        assert saved.read()["tasks"][task_id]["archived"]
        assert "private" not in saved.path.read_text()
    assert runtime.get(child)["archive_reason"] == "parent_archived"
    assert runtime.get(leaf)["archive_reason"] == "parent_archived"
    rows = {key: runtime.get(key) for key in (root, child, leaf)}
    assert lifecycle._descendants({"tasks": rows}, rows[root]) == [leaf, child]


@pytest.mark.parametrize("archived", [False, True])
def test_t2_delete_tree_tombstones_purges_results_and_forgets_each_child(tmp_path, archived):
    runtime, ids = tree(tmp_path)
    files = result_files(runtime, ids)
    forgotten = []
    runtime.tools.forget_task = lambda task_id, **kw: forgotten.append(task_id)
    if archived:
        change(runtime, ids[0], "archive")
    reply = change(runtime, ids[0], "delete")
    assert reply["descendant_ids"] == [ids[2], ids[1]]
    data = runtime.store.read()
    assert all(key in data["deleted_tasks"] for key in ids)
    assert not set(ids) & (data["tasks"].keys() | data.get("archives", {}).keys())
    assert set(forgotten) == set(ids)
    assert not any(path.exists() for path in files)
    assert all(not lifecycle.archive_store(runtime.store, key).path.exists() for key in ids)
    assert all(not data["deleted_tasks"][key]["cleanup_pending"] for key in ids)


def test_t3_restore_tree_and_reserve_entire_capacity_before_restore(tmp_path, monkeypatch):
    runtime, ids = tree(tmp_path)
    change(runtime, ids[0], "archive")
    calls = []
    real = runtime._relieve_storage
    def record(trigger, **kw):
        calls.append((trigger, kw))
        return real(trigger, **kw)
    monkeypatch.setattr(runtime, "_relieve_storage", record)
    change(runtime, ids[0], "restore")
    assert calls[0][1]["need_tasks"] == 3
    assert all(key in runtime.store.read()["tasks"] for key in ids)
    assert all(not runtime.get(key)["archived"] for key in ids)
    assert not runtime.store.read()["archives"]


def test_t3_restore_limit_counts_all_descendants_atomically(tmp_path, monkeypatch):
    runtime, ids = tree(tmp_path)
    template = copy.deepcopy(runtime.store.read()["tasks"][ids[0]])
    change(runtime, ids[0], "archive")
    def fill(data):
        for index in range(198):
            row = copy.deepcopy(template)
            row.update(id=f"agent_full_{index}", subagent_ids=[])
            data["tasks"][row["id"]] = row
    runtime.store.transaction(fill)
    monkeypatch.setattr(storage_relief, "needed", lambda *a, **kw: False)
    with pytest.raises(AgentError, match="agent_task_limit"):
        change(runtime, ids[0], "restore")
    assert len(runtime.store.read()["tasks"]) == 198
    assert set(ids) <= runtime.store.read()["archives"].keys()


@pytest.mark.parametrize("busy", ["in_flight", "ambiguous"])
def test_t4_busy_descendant_rejects_archive_before_cascade_changes_state(tmp_path, busy):
    runtime, ids = tree(tmp_path)
    def mark(data):
        row = data["tasks"][ids[2]]
        if busy == "in_flight":
            row.update(status="running", in_flight={"effect": "read"})
        else:
            row.update(status="interrupted", error="agent_write_outcome_unknown")
    runtime.store.transaction(mark)
    before = runtime.store.read()
    with pytest.raises(AgentError, match="agent_task_busy"):
        change(runtime, ids[0], "archive")
    assert runtime.store.read() == before


def test_t1_archive_failure_of_one_child_emits_skip_and_preserves_parent(tmp_path, monkeypatch):
    runtime, ids = tree(tmp_path)
    original = lifecycle._archive_in_tx
    def fail_one(store, data, row, *args, **kwargs):
        if row["id"] == ids[2]:
            raise OSError("archive disk failure")
        return original(store, data, row, *args, **kwargs)
    monkeypatch.setattr(lifecycle, "_archive_in_tx", fail_one)
    change(runtime, ids[0], "archive")
    assert runtime.get(ids[0])["archived"]
    assert ids[2] in runtime.store.read()["tasks"]
    assert any(e["type"] == "archive_child_skipped" and e["task_id"] == ids[2]
               for e in runtime.get(ids[0])["events"])


def test_t5_storage_relief_counts_whole_trees_with_one_hundred_children(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("done"))
    ids = finished_history(runtime, 190)
    def link(data):
        for index in range(50):
            children = ids[90 + index * 2:92 + index * 2]
            data["tasks"][ids[index]]["subagent_ids"] = children
            for child in children:
                data["tasks"][child]["parent_task_id"] = ids[index]
    runtime.store.transaction(link)
    plan = storage_relief.candidates(runtime.store.read(), runtime.store.usage()["tasks"], set())
    assert plan[0]["tree_ids"] == [ids[90], ids[91], ids[0]]
    sizes = runtime.store.usage()["tasks"]
    assert plan[0]["bytes"] == sum(sizes[key] for key in plan[0]["tree_ids"])
    archived = runtime._relieve_storage("test", need_tasks=1)
    data = runtime.store.read()
    assert archived == 12  # Four three-task trees meet the watermark; do not over-archive eleven trees.
    assert len(data["tasks"]) <= 180
    assert archived == 190 - len(data["tasks"]) == len(data["archives"])
    assert all(row.get("parent_task_id") in data["tasks"] for row in data["tasks"].values() if row.get("parent_task_id"))


@pytest.mark.parametrize("parent_state", ["archived", "deleted", "missing"])
def test_t6_reconcile_legacy_multilevel_orphans_without_removing_top_level(tmp_path, parent_state):
    runtime, ids = tree(tmp_path)
    files = result_files(runtime, ids[1:])
    def legacy(data):
        root = data["tasks"].pop(ids[0])
        top = copy.deepcopy(root)
        top.update(id="agent_unrelated_top", subagent_ids=[])
        data["tasks"][top["id"]] = top
        if parent_state == "archived":
            data["tasks"][ids[0]] = root
            lifecycle._archive_in_tx(runtime.store, data, root, event)
        elif parent_state == "deleted":
            data.setdefault("deleted_tasks", {})[ids[0]] = {"id": ids[0]}
    runtime.store.transaction(legacy)
    runtime.reconcile()
    data = runtime.store.read()
    assert set(data["tasks"]) == {"agent_unrelated_top"}
    if parent_state == "deleted":
        assert set(ids) <= data["deleted_tasks"].keys()
        assert not any(path.exists() for path in files)
    else:
        assert set(ids[1:]) <= data["archives"].keys()
        assert runtime.get(ids[1])["archive_reason"] == ("parent_archived" if parent_state == "archived" else "orphan")
    runtime.reconcile()
    assert set(runtime.store.read()["tasks"]) == {"agent_unrelated_top"}


def test_t7_spawn_at_two_hundred_tasks_is_rejected_with_correction(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("done"))
    ids = finished_history(runtime, 200)
    def spawn(data):
        return subagents.spawn_agent_in_tx(data, data["tasks"][ids[0]], {
            "task_name": "over_limit", "prompt": "inspect", "role": "explorer"})
    with pytest.raises(AgentToolRejected, match="agent_task_limit") as error:
        runtime.store.transaction(spawn)
    assert "archive" in error.value.correction.lower()
    assert len(runtime.store.read()["tasks"]) == 200


def test_t7_real_model_spawn_path_relieve_storage_before_transaction(tmp_path, monkeypatch):
    _, runtime, model, _ = _build_env(tmp_path)
    model.parent_responses.append(assistant(name="ryn_spawn_agent", args={
        "task_name": "new_worker", "prompt": "inspect", "role": "explorer"}))
    parent = runtime.create({"message": "delegate", "model_id": "local/gpt-4o"})["id"]
    runtime.run_once()
    calls = []
    real = runtime._relieve_storage
    def record(trigger, **kw):
        calls.append((trigger, kw))
        return real(trigger, **kw)
    monkeypatch.setattr(runtime, "_relieve_storage", record)
    runtime.run_once()
    assert any(trigger == "spawn" and kw["need_tasks"] == 1 for trigger, kw in calls)
    assert len(runtime.store.read()["tasks"][parent]["subagent_ids"]) == 1


def test_restore_does_not_restore_a_child_archived_independently(tmp_path):
    runtime, ids = tree(tmp_path)
    change(runtime, ids[2], "archive")
    change(runtime, ids[0], "archive")
    change(runtime, ids[0], "restore")
    data = runtime.store.read()
    assert set(ids[:2]) <= data["tasks"].keys()
    assert ids[2] in data["archives"]


def test_storage_tree_revalidates_child_revision_and_protects_any_member(tmp_path):
    runtime, ids = tree(tmp_path)
    # Recent top-level conversations are protected; add ten newer roots.
    def newer(data):
        template = data["tasks"][ids[0]]
        for index in range(10):
            row = copy.deepcopy(template)
            row.update(id=f"agent_recent_{index}", subagent_ids=[], updated_at="2099")
            data["tasks"][row["id"]] = row
    runtime.store.transaction(newer)
    data = runtime.store.read()
    sizes = runtime.store.usage()["tasks"]
    assert not storage_relief.candidates(data, sizes, {ids[2]})
    data["automations"] = {"auto": {"status": "ACTIVE", "target_task_id": ids[1]}}
    assert not storage_relief.candidates(data, sizes, set())
    plan = storage_relief.candidates(runtime.store.read(), sizes, set())
    runtime.store.transaction(lambda current: current["tasks"][ids[2]].update(control_revision=999))
    assert lifecycle.archive_many(runtime.store, plan, event, reason="storage") == []
    assert set(ids) <= runtime.store.read()["tasks"].keys()


def test_delete_retry_retries_failed_child_cleanup_from_durable_tree_tombstone(tmp_path, monkeypatch):
    runtime, ids = tree(tmp_path)
    files = result_files(runtime, ids)
    real = results.purge
    def fail_child(store, task_id):
        if task_id == ids[2]:
            raise OSError("temporary cleanup failure")
        return real(store, task_id)
    monkeypatch.setattr(results, "purge", fail_child)
    reply = change(runtime, ids[0], "delete")
    assert reply["cleanup_pending"]
    assert files[2].exists() and not files[0].exists() and not files[1].exists()
    monkeypatch.setattr(results, "purge", real)
    tombstone = runtime.store.read()["deleted_tasks"][ids[0]]
    reply = runtime.lifecycle(ids[0], {"expected_control_revision": tombstone["control_revision"]}, "delete")
    assert not reply["cleanup_pending"]
    assert not any(path.exists() for path in files)


def test_archive_cancels_queued_descendants_before_they_reach_archive(tmp_path):
    runtime, ids = tree(tmp_path)
    runtime.store.transaction(lambda data: data["tasks"][ids[2]].update(status="queued"))
    change(runtime, ids[0], "archive")
    saved = runtime.get(ids[2])
    assert saved["status"] == "cancelled"
    assert lifecycle.archive_store(runtime.store, ids[2]).read()["tasks"][ids[2]]["subagent_meta"]["closed"]
