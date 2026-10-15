"""Per-task encrypted storage: one root commits each task file by digest."""
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import store as storage
from rynmesh.agent_runtime.store import AgentError, AgentStore, encoded
from rynmesh.atomic_io import atomic_write_json
from rynmesh.services import peer_box


def blobs(root):
    directory = root / storage.TASKS_DIR
    return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []


def row(task_id, text="hello"):
    return {"id": task_id, "status": "completed", "events": [], "messages": [{"role": "user", "content": text}]}


def seed(store, *task_ids):
    def apply(data):
        data["tasks"].update({task_id: row(task_id, "private " + task_id) for task_id in task_ids})
    store.transaction(apply)


def test_legacy_single_file_is_read_then_converted(tmp_path):
    key = X25519PrivateKey.generate()
    legacy = {"version": storage.VERSION, "schema_version": 2, "settings": dict(storage.DEFAULT_SETTINGS),
              "tasks": {"a": row("a", "legacy secret")}, "context_budget_version": 1}
    nonce, ciphertext = peer_box.seal(key, peer_box.public_key_b64(key), encoded(legacy), info=storage.CHANNEL)
    tmp_path.mkdir(exist_ok=True)
    atomic_write_json(tmp_path / "sessions.json", {"version": storage.VERSION, "nonce": nonce, "ciphertext": ciphertext})
    store = AgentStore(tmp_path, key)
    assert store.read()["tasks"]["a"]["messages"][0]["content"] == "legacy secret"
    store.transaction(lambda data: data["settings"].update(enabled=True))
    assert json.loads((tmp_path / "sessions.json").read_text())["version"] == storage.ROOT_VERSION
    assert len(blobs(tmp_path)) == 1 and "legacy secret" not in (tmp_path / storage.TASKS_DIR / blobs(tmp_path)[0]).read_text()
    assert AgentStore(tmp_path, key).read()["tasks"]["a"]["messages"][0]["content"] == "legacy secret"


def test_a_transaction_rewrites_only_the_task_it_changed(tmp_path):
    store = AgentStore(tmp_path, X25519PrivateKey.generate())
    seed(store, "a", "b", "c")
    before = blobs(tmp_path)
    store.transaction(lambda data: None)
    assert blobs(tmp_path) == before  # unchanged rows write nothing
    store.transaction(lambda data: data["tasks"]["b"]["messages"].append({"role": "assistant", "content": "x"}))
    after = blobs(tmp_path)
    assert len(after) == 3 and len(set(before) & set(after)) == 2
    assert [name for name in after if name not in before] == [store._index["b"]["file"]]
    reopened = AgentStore(tmp_path, store.key).read()
    assert reopened["tasks"]["b"]["messages"][-1]["content"] == "x" and set(reopened["tasks"]) == {"a", "b", "c"}


def test_removed_tasks_lose_their_file_and_orphans_are_swept(tmp_path):
    store = AgentStore(tmp_path, X25519PrivateKey.generate())
    seed(store, "a", "b")
    store.transaction(lambda data: data["tasks"].pop("a"))
    assert blobs(tmp_path) == [store._index["b"]["file"]]
    (tmp_path / storage.TASKS_DIR / ("0" * 24 + ".json")).write_text("{}")
    (tmp_path / storage.TASKS_DIR / (".b.json." + "a" * 32 + ".tmp")).write_text("{}")
    store.purge_orphan_snapshots()
    assert blobs(tmp_path) == [store._index["b"]["file"]]


def test_a_swapped_task_file_is_refused(tmp_path):
    store = AgentStore(tmp_path, X25519PrivateKey.generate())
    seed(store, "a", "b")
    names = blobs(tmp_path)
    directory = tmp_path / storage.TASKS_DIR
    a, b = (directory / names[0]).read_bytes(), (directory / names[1]).read_bytes()
    (directory / names[0]).write_bytes(b)
    (directory / names[1]).write_bytes(a)
    with pytest.raises(AgentError, match="agent_state_unreadable"):
        AgentStore(tmp_path, store.key).read()


def test_total_size_limit_still_applies(tmp_path, monkeypatch):
    store = AgentStore(tmp_path, X25519PrivateKey.generate())
    seed(store, "a")
    monkeypatch.setattr(storage, "MAX_PLAINTEXT", len(encoded(store.read())) + 1000)
    with pytest.raises(AgentError, match="agent_storage_limit"):
        store.transaction(lambda data: data["tasks"].update(b=row("b", "x" * 5000)))
    assert set(store.read()["tasks"]) == {"a"}


def test_inspect_detaches_its_result_without_copying_tasks(tmp_path):
    store = AgentStore(tmp_path, X25519PrivateKey.generate())
    seed(store, "a")
    view = store.inspect(lambda data: data["tasks"]["a"]["messages"])
    view.append("changed")
    assert len(store.read()["tasks"]["a"]["messages"]) == 1


def test_unchanged_task_poll_returns_only_live_fields(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("Done."))
    task = runtime.create({"message": "Say done."})
    advance(runtime)
    full = runtime.get(task["id"])
    same = runtime.get(task["id"], full["revision"])
    assert same["unchanged"] is True and "messages" not in same and set(same) <= {
        "id", "revision", "unchanged", "run_timing", "budget", "allowed_actions", "run_state"}
    assert runtime.get(task["id"], full["revision"] - 1) == full
    summaries = runtime.list_tasks()
    assert summaries[0]["id"] == task["id"] and "messages" not in summaries[0]
