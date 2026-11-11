"""Per-task encrypted storage: one root commits each task file by digest."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

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


def test_noop_keeps_committed_bytes_and_detaches_return_value(tmp_path, monkeypatch):
    store = AgentStore(tmp_path, X25519PrivateKey.generate())
    seed(store, "a")
    before = store.path.read_bytes()
    writes = []
    original = storage.atomic_write_json
    monkeypatch.setattr(storage, "atomic_write_json", lambda *a, **kw: (writes.append(a[0]), original(*a, **kw))[1])
    returned = store.transaction(lambda data: data["tasks"]["a"]["messages"])
    returned.append({"role": "assistant", "content": "detached"})
    assert writes == [] and store.path.read_bytes() == before
    assert len(store.read()["tasks"]["a"]["messages"]) == 1
    store.transaction(lambda data: data["tasks"]["a"]["messages"].append({"role": "assistant", "content": "persisted"}))
    assert len(writes) == 2  # changed task blob, then the committing root
    assert AgentStore(tmp_path, store.key).read()["tasks"]["a"]["messages"][-1]["content"] == "persisted"


def test_noop_refreshes_another_store_commit(tmp_path):
    store = AgentStore(tmp_path, X25519PrivateKey.generate())
    seed(store, "a")
    other = AgentStore(tmp_path, store.key)
    other.transaction(lambda data: data["tasks"]["a"].update(result="external"))
    before = store.path.read_bytes()
    assert store.transaction(lambda data: data["tasks"]["a"]["result"]) == "external"
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("layout", [storage.VERSION, storage.ROOT_VERSION])
def test_noop_persists_layout_and_payload_migration(tmp_path, layout):
    store = AgentStore(tmp_path, X25519PrivateKey.generate())
    seed(store, "a")
    envelope = json.loads(store.path.read_bytes())
    payload = json.loads(peer_box.open_sealed(store.key, store.pub, envelope["nonce"], envelope["ciphertext"], info=storage.CHANNEL))
    if layout == storage.VERSION:
        payload.pop("task_blobs")
        payload["tasks"] = store.read()["tasks"]
    payload.pop("context_budget_version", None)
    payload.pop("settings_context_budget_provenance", None)
    payload["settings"]["max_context_chars"] = 120000
    nonce, ciphertext = peer_box.seal(store.key, store.pub, encoded(payload), info=storage.CHANNEL)
    atomic_write_json(store.path, {"version": layout, "nonce": nonce, "ciphertext": ciphertext})
    reopened = AgentStore(tmp_path, store.key)
    reopened.transaction(lambda data: None)
    committed = json.loads(store.path.read_bytes())
    raw = json.loads(peer_box.open_sealed(store.key, store.pub, committed["nonce"], committed["ciphertext"], info=storage.CHANNEL))
    assert committed["version"] == storage.ROOT_VERSION
    assert raw["context_budget_version"] == 2 and raw["settings"]["max_context_chars"] == 120000
    assert raw["settings_context_budget_provenance"] == "legacy_ambiguous_manual"


def test_noop_releases_stale_storage_reservations(tmp_path):
    store = AgentStore(tmp_path, X25519PrivateKey.generate())
    seed(store, "a")
    def reserve(data):
        data["tasks"]["a"]["in_flight"] = {"kind": "model"}
        data["storage_reservations"] = {"a": {"bytes": 100, "baseline": 0}}
    store.transaction(reserve)
    store.transaction(lambda data: data["tasks"]["a"].update(in_flight=None))
    assert not AgentStore(tmp_path, store.key).read()["storage_reservations"]


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


def test_history_above_old_quota_roundtrips_and_can_grow(tmp_path):
    store = AgentStore(tmp_path, X25519PrivateKey.generate())
    # One task also exceeds the former encrypted-file limit.
    text = "x" * (26 * 1024 * 1024)
    store.transaction(lambda data: data["tasks"].update(a=row("a", text)))
    reopened = AgentStore(tmp_path, store.key)
    assert reopened.read()["tasks"]["a"]["messages"][0]["content"] == text
    reopened.transaction(lambda data: data["tasks"].update(b=row("b")))
    assert set(AgentStore(tmp_path, store.key).read()["tasks"]) == {"a", "b"}
    assert reopened.usage()["limit"] is None


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
        "id", "revision", "unchanged", "run_timing", "budget", "allowed_actions", "run_state", "streaming"}
    assert runtime.get(task["id"], full["revision"] - 1) == full
    summaries = runtime.list_tasks()
    assert summaries[0]["id"] == task["id"] and "messages" not in summaries[0]


def test_task_poll_reads_last_commit_during_slow_write(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("Done."))
    task_id = runtime.create({"message": "Say done."})["id"]
    previous = runtime.get(task_id)
    entered, release = threading.Event(), threading.Event()

    def mutate(data):
        data["tasks"][task_id]["title"] = "committed later"
        entered.set()
        assert release.wait(10)

    with ThreadPoolExecutor(2) as pool:
        writer = pool.submit(runtime.store.transaction, mutate)
        try:
            assert entered.wait(5)
            assert pool.submit(runtime.list_tasks).result(2)[0]["title"] == previous["title"]
            assert pool.submit(runtime.get, task_id).result(2)["title"] == previous["title"]
            control = pool.submit(runtime.store.read_control, task_id).result(2)
            assert control["tasks"][task_id]["title"] == previous["title"]
        finally:
            release.set()
        writer.result(5)
    assert runtime.get(task_id)["title"] == "committed later"


def test_slow_result_copy_does_not_block_next_writer(tmp_path):
    store = AgentStore(tmp_path, X25519PrivateKey.generate())
    seed(store, "a")
    copying, release = threading.Event(), threading.Event()

    class SlowResult:
        def __deepcopy__(self, memo):
            copying.set()
            assert release.wait(10)
            return "copied"

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(store.transaction, lambda data: SlowResult())
        try:
            assert copying.wait(5)
            second = pool.submit(store.transaction, lambda data: data["tasks"]["a"].update(title="next"))
            second.result(2)
        finally:
            release.set()
        assert first.result(5) == "copied"
    assert store.read()["tasks"]["a"]["title"] == "next"


def test_competing_prepared_writes_apply_each_callback_once(tmp_path, monkeypatch):
    store = AgentStore(tmp_path, X25519PrivateKey.generate())
    seed(store, "a")
    other = AgentStore(tmp_path, store.key)
    barrier = threading.Barrier(2)
    for instance in (store, other):
        original = instance._snapshot

        def same_base(original=original):
            snapshot = original()
            barrier.wait(timeout=5)
            return snapshot
        monkeypatch.setattr(instance, "_snapshot", same_base)
    calls = []

    def append(data, label):
        calls.append(label)
        data["tasks"]["a"]["events"].append({"type": label})

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(instance.transaction, lambda data, label=label: append(data, label))
                   for instance, label in ((store, "left"), (other, "right"))]
        for future in futures:
            future.result(10)
    assert sorted(calls) == ["left", "right"]
    final = AgentStore(tmp_path, store.key).read()
    assert sorted(event["type"] for event in final["tasks"]["a"]["events"]) == ["left", "right"]


def test_dispatch_does_not_reserve_a_second_copy_of_unrelated_history(tmp_path, monkeypatch):
    from types import SimpleNamespace
    store = AgentStore(tmp_path, X25519PrivateKey.generate())
    store.transaction(lambda data: data["tasks"].update(a=row("a", "x" * 2_000_000), b=row("b")))
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda path: SimpleNamespace(free=512_000))
    def dispatch(data):
        data["tasks"]["b"]["in_flight"] = {"kind": "model"}
        store.reserve(data, "b", 32_000)
    store.transaction(dispatch)
    assert store.read()["storage_reservations"]["b"]["bytes"] == 32_000
