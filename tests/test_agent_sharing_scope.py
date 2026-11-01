"""Private source -> friend scope -> exact grant -> sharing, with real stores."""
import copy
import threading

import pytest
from test_agent_integrations import add_source
from test_agent_integrations import node as integration_node

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.ai_access.routes import AIAccessState
from rynmesh.ai_access.store import AIAccessStore
from rynmesh.llm_package.model_settings import effective

node = integration_node


def setup(app, store):
    source, model_id = add_source(app, models=["model-a", "model-b"])
    relations = {name * 32: {"relationship_id": name * 32, "peer_id": "node-" + name,
        "node_name": "Node " + name, "status": "active"} for name in ("a", "b")}
    permissions = AIAccessStore(store.home, relationship=relations.get)
    app.state.ai_access = AIAccessState(permissions, lambda _: None, recheck=lambda: None)
    _, tools, _ = build_agent_integrations(app, store, store.home)
    sources = app.state.llm_agent_services["source_store"]
    return source, model_id, sources, tools, permissions, relations


def execute(tools, args, op, name="ryn_set_model_sharing"):
    return tools.execute(name, args, operation_id=op, cancelled=lambda: False, expected_preview=tools.preview(name, args))


def test_private_scope_grant_enable_preserves_sibling_and_requires_both_layers(node):
    app, store = node
    source, model_id, sources, tools, permissions, _ = setup(app, store)
    original = sources.read()[source["id"]]
    sibling = copy.deepcopy(original["model_settings"]["model-b"])
    args = {"model_id": model_id, "enabled": False, "scope": "friends"}
    preview = tools.preview("ryn_set_model_sharing", args)
    assert preview["before"] == {"sharing": False, "scope": "personal_space"}
    assert preview["after"] == {"sharing": False, "scope": "friends"} and preview["allowed_friend_grants"] == []
    assert sources.read()[source["id"]] == original
    execute(tools, args, "prepare-private")
    sid = sources.model_service_id(original, "model-a")
    provider = app.state.llm_agent_services["provider_for"](sid)
    assert not provider.accepting_orders and "explicit-friends" in provider.manifest.capabilities
    grant = {"service_id": sid, "relationship_id": "b" * 32, "allowed": True, "expected_revision": 0}
    execute(tools, grant, "grant-one", "ryn_set_friend_model_access")
    assert permissions.grant(sid, "b" * 32)["effective"]
    assert not app.state.llm_agent_services["provider_for"](sid).accepting_orders
    enabled = {**args, "enabled": True}
    preview = tools.preview("ryn_set_model_sharing", enabled)
    assert [r["peer_id"] for r in preview["allowed_friend_grants"]] == ["node-b"]
    execute(tools, enabled, "enable-one")
    saved = sources.read()[source["id"]]
    assert saved["shared_models"] == ["model-a"] and effective(saved, "model-a")["share_scope"] == "friends"
    assert saved["model_settings"]["model-b"] == sibling
    assert app.state.llm_agent_services["provider_for"](sid).accepting_orders
    assert permissions.grant(sid, "a" * 32) is None
    result = tools.verify({"tool": "ryn_set_model_sharing", "arguments": enabled},
        [{"verifier": "model_sharing_state"}], context={}, cancelled=lambda: False)
    assert result["status"] == "passed" and result["evidence"]["scope_matches"]
    # A concurrent owner scope change must fail even if sharing stays enabled.
    sources.configure_model(source["id"], {"model_id": "model-a",
        "expected_rule_revision": saved["model_settings"]["model-a"]["rule_revision"], "configuration": {"share_scope": "personal_space"}})
    assert tools.verify({"tool": "ryn_set_model_sharing", "arguments": enabled},
        [{"verifier": "model_sharing_state"}], context={}, cancelled=lambda: False)["status"] == "failed"


def test_added_recipient_invalidates_approval_including_inactive_grants(node):
    app, store = node
    source, model_id, sources, tools, permissions, relations = setup(app, store)
    args = {"model_id": model_id, "enabled": True, "scope": "friends"}
    preview = tools.preview("ryn_set_model_sharing", args)
    sid = sources.model_service_id(source, "model-a")
    permissions.set(sid, "a" * 32, allowed=True, expected_revision=0)
    relations["a" * 32]["status"] = "inactive"
    current = tools.preview("ryn_set_model_sharing", args)
    assert len(current["allowed_friend_grants"]) == 1 and not current["allowed_friend_grants"][0]["effective"]
    before = sources.read()
    with pytest.raises(AgentToolRejected, match="agent_approval_preview_changed"):
        tools.execute("ryn_set_model_sharing", args, operation_id="stale", cancelled=lambda: False, expected_preview=preview)
    assert sources.read() == before  # Scope and sharing are both unchanged.


def test_scope_and_sharing_publish_in_one_source_save_and_no_credential_change(node, monkeypatch):
    app, store = node
    source, model_id, sources, tools, _, _ = setup(app, store)
    before = sources.read()[source["id"]]
    saves = []
    save = sources.save
    def observed(row):
        saves.append(copy.deepcopy(row))
        return save(row)
    monkeypatch.setattr(sources, "save", observed)
    execute(tools, {"model_id": model_id, "enabled": True, "scope": "friends"}, "atomic")
    assert len(saves) == 1
    assert saves[0]["shared_models"] == ["model-a"] and effective(saves[0], "model-a")["share_scope"] == "friends"
    after = sources.read()[source["id"]]
    for field in ("base_url", "api_key", "models", "enabled", "tool_support", "destination_revision"):
        assert after[field] == before[field]


def test_recipient_store_stays_locked_until_approved_source_save(node, monkeypatch):
    app, store = node
    source, model_id, sources, tools, permissions, _ = setup(app, store)
    sid = sources.model_service_id(source, "model-a")
    started, finished = threading.Event(), threading.Event()
    errors = []
    def add_recipient():
        started.set()
        try:
            permissions.set(sid, "a" * 32, allowed=True, expected_revision=0)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()
    worker = threading.Thread(target=add_recipient)
    original_save = sources.save
    def save_while_contended(record):
        worker.start()
        assert started.wait(2)
        assert not finished.wait(.1)  # Preview/commit cannot admit an unseen recipient in between.
        assert permissions.grant(sid, "a" * 32) is None
        return original_save(record)
    monkeypatch.setattr(sources, "save", save_while_contended)
    try:
        execute(tools, {"model_id": model_id, "enabled": True, "scope": "friends"}, "locked-preview")
    finally:
        if worker.ident is not None:
            worker.join(timeout=3)
    assert finished.is_set() and not errors
    # An independent later owner edit is allowed; it was serialized after the commit.
    assert permissions.grant(sid, "a" * 32)["allowed"]
