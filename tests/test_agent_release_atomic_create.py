"""Actual scheduler exclusion at formal creation; synthetic collector, no model IO."""
import importlib
import threading
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent_integrations import add_source, node  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.services import peer_box

ROOT = Path(__file__).resolve().parents[1]


def test_p05_fixture_mapping_uses_actual_local_read_only_nas(node, tmp_path, monkeypatch):  # noqa: F811
    from rynmesh.nas import NasPlugin

    monkeypatch.syspath_prepend(str(ROOT / "docs/acceptance/ryn-agent-delivery/two-host"))
    module = importlib.import_module("release_tasks")
    _app, store = node
    plugin = NasPlugin(store.home)
    plugin.enable(True)
    identities = {}
    monkeypatch.syspath_prepend(str(ROOT / "tests/fixtures/agent_acceptance"))
    dav = importlib.import_module("webdav_fixture")
    with ExitStack() as stack:
        for label in ("docs-alpha", "docs-beta"):
            root = tmp_path / label
            root.mkdir()
            (root / ".ryn-acceptance-isolated").touch()
            server = stack.enter_context(dav.FixtureDAV(root))
            row = plugin.configure({"name": "P05-" + label, "system": "generic", "protocol": "webdav",
                              "url": server.url, "username": "fixture", "password": "test",
                              "writable": False, "allow_ai": True})
            identities[label] = row["id"]
    body = {"case_id": "P05", "fixture_bindings": identities}
    def resolve(value=body, selected=plugin, execution="node-b", configured=None):
        return module.fixture_bindings(value, configured or {}, plugin=selected, store=store, execution_label=execution)
    assert resolve() == identities
    assert NasPlugin(store.home).state == plugin.state  # Actual encrypted persistence, no request-label substitute.
    with pytest.raises(ValueError, match="connector_mismatch"):
        resolve({"case_id": "P05", "fixture_bindings": {**identities, "docs-alpha": "missing"}})
    with pytest.raises(ValueError, match="binding_invalid"):
        resolve(selected=NasPlugin(tmp_path / "other-node"))
    with pytest.raises(ValueError, match="binding_invalid"):
        resolve(execution="node-a")
    with pytest.raises(ValueError, match="binding_changed"):
        resolve(configured={"docs-alpha": "prior-id"})
    plugin.state["sources"][0]["writable"] = True
    with pytest.raises(ValueError, match="connector_mismatch"):
        resolve()
    plugin.state["sources"][0]["writable"] = False
    plugin.enable(False)
    with pytest.raises(ValueError, match="connector_disabled"):
        resolve()


@pytest.mark.parametrize("fail", [False, True])
def test_original_scheduler_cannot_claim_until_registered_or_cancelled(node, tmp_path, monkeypatch, fail):  # noqa: F811
    monkeypatch.syspath_prepend(str(ROOT / "docs/acceptance/ryn-agent-delivery/two-host"))
    module = importlib.import_module("release_tasks")
    app, store = node
    add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, store, store.home)
    selected = next(row["id"] for row in catalog.list_models() if row["available"] and row["agent_supported"])
    runtime = AgentRuntime(AgentStore(store.home / "agent", peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")),
        bridge, tools, catalog, consumer_node_id=store.peer_id)
    runtime.update_settings({"enabled": True, "model_id": selected})
    entered, release = threading.Event(), threading.Event()
    seen, errors = [], []
    fake_registration = tmp_path / "fake-registration.json"
    fake_registration.write_text('{"binding":{"synthetic":true},"observation":{"task_id":"synthetic"}}', encoding="utf-8")

    def factory(body, task):
        assert task["owner_id"] == "owner-test" if "owner_id" in task else runtime.owner_id == "owner-test"
        entered.set()
        assert release.wait(5)
        if fail:
            raise ValueError("synthetic_observer_refused")
        return SimpleNamespace(register=lambda: seen.append("register"), attach_candidate=lambda: seen.append("attach"),
                               registration_path=fake_registration)

    manager = module.RegisteredTasks(runtime, factory)
    def create():
        try:
            manager.create({"task": {"message": "Synthetic atomic creation test", "model_id": selected}})
        except ValueError as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=create)
    thread.start()
    try:
        assert entered.wait(5)
        before = runtime.store.read()["tasks"]
        assert len(before) == 1 and next(iter(before.values()))["status"] == "queued"
        runtime.run_once()
        assert runtime.store.read()["tasks"] == before
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    if fail:
        assert errors == ["synthetic_observer_refused"] and seen == []
        runtime.run_once()
        assert next(iter(runtime.store.read()["tasks"].values()))["status"] == "cancelled"
    else:
        assert not errors and seen == ["register", "attach"]
    with pytest.raises(ValueError, match="registered_create_required"):
        runtime.create({"message": "bypass", "model_id": selected})
    with pytest.raises(ValueError, match="fresh_task_slot_required"):
        manager.create({"task": {"message": "reuse", "model_id": selected}})
