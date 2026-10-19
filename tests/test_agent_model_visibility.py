"""Sharing configuration and authenticated recipient observations stay distinct."""
import copy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from test_agent_integrations import add_source, node  # noqa: F401
from test_agent_peer_binding import paired  # noqa: F401
from test_agent_space_binding import nodes, space_pair  # noqa: F401

from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.agent_runtime.tools import RynTools
from rynmesh.ai_access.store import AIAccessStore
from rynmesh.llm_package import peer_binding
from rynmesh.llm_package.sources import SourceStore


def consumer_catalog(p, records=None):
    home = p.bob.store.root.parent
    sources = SourceStore(home, p.bob.identity_private, migrate=False)
    store = SimpleNamespace(home=home, peer_id=p.bob.peer_id)
    services = {"source_store": sources, "discover": lambda _: p.consumer.records() if records is None else records}
    return ModelCatalog(services, store), services, store


def remote_row(catalog):
    return next(row for row in catalog.list_models() if row["source"] == "peer")


def test_real_authenticated_friend_catalog_exposes_observation_not_provider_configuration(paired):  # noqa: F811
    p = paired
    record = p.consumer.refresh(p.alice.peer_id)["services"][0]
    assert record["access"] == "friend" and record.get("access_policy") is None
    catalog, _, _ = consumer_catalog(p)
    row = remote_row(catalog)
    bound = peer_binding.expected(record, p.bob.peer_id)
    assert row["sharing"] is True and row["share_scope"] == "unknown"
    assert row["sharing_basis"] == "remote_catalog_advertisement" and row["sharing_configuration_known"] is False
    assert row["recipient_access"] == {"kind": "friend", "consumer_peer_id": p.bob.peer_id,
        "basis": "authenticated_catalog_observation", "catalog_available": True, "current_access": "unknown",
        "invocation_verified": False, "ai_permission": bound["ai_permission"]}
    assert row["expected_model_binding"] == bound and row["destination_binding_supported"]
    assert row["destination_revision"] == peer_binding.revision(record, p.bob.peer_id)
    assert not p.traces  # No inference is needed to report a catalog observation.


@pytest.mark.parametrize("invalid", ["unverified", "wrong_consumer", "invalid_binding", "missing_permission", "zero_revision"])
def test_invalid_recipient_binding_never_projects_precise_permission(paired, invalid):  # noqa: F811
    p = paired
    record = p.consumer.refresh(p.alice.peer_id)["services"][0]
    record = copy.deepcopy(record)
    if invalid == "unverified":
        record["binding_verified"] = False
    elif invalid == "wrong_consumer":
        record["consumer_peer_id"] = "another-consumer"
    elif invalid == "invalid_binding":
        record["model_binding"]["model_generation"] = "invalid"
    elif invalid == "missing_permission":
        record.pop("ai_permission")
    else:
        record["ai_permission"]["revision"] = 0
    catalog, _, _ = consumer_catalog(p, [record])
    row = remote_row(catalog)
    assert row["recipient_access"] == {"kind": "unknown", "consumer_peer_id": p.bob.peer_id,
        "basis": "unknown", "catalog_available": True, "current_access": "unknown", "invocation_verified": False}
    assert row["expected_model_binding"] is None and not row["destination_binding_supported"]
    assert row["destination_revision"] == "" and row["share_scope"] == "unknown"


def test_production_catalog_expiry_preserves_only_historical_observation(paired):  # noqa: F811
    p = paired
    record = p.consumer.refresh(p.alice.peer_id)["services"][0]
    catalog, _, _ = consumer_catalog(p)
    initial = remote_row(catalog)
    checked = p.consumer.cache[p.alice.peer_id]["checked_at"]
    p.consumer.clock = lambda: checked + 121
    assert p.consumer.snapshots()[0]["status"] == "stale"
    assert p.consumer.records()[0]["online"] is False
    stale = remote_row(catalog)
    assert stale["available"] is False and stale["recipient_access"]["catalog_available"] is False
    assert stale["recipient_access"]["kind"] == "friend"
    assert stale["recipient_access"]["ai_permission"] == record["ai_permission"]
    assert stale["recipient_access"]["current_access"] == "unknown" and not stale["recipient_access"]["invocation_verified"]
    assert stale["expected_model_binding"] == initial["expected_model_binding"]
    assert stale["destination_revision"] == initial["destination_revision"]
    with pytest.raises(ValueError, match="agent_model_unavailable"):
        catalog.resolve(stale["id"])


def test_real_space_catalog_observation_cannot_become_friend_grant(space_pair):  # noqa: F811
    p = space_pair
    record = p.consumer.refresh(p.locator)[0]
    services = {"source_store": p.apps[1].state.llm_agent_services["source_store"], "discover": lambda _: p.consumer.records()}
    row = remote_row(ModelCatalog(services, p.child.store))
    bound = peer_binding.expected(record, p.child.store.peer_id)
    assert row["share_scope"] == "unknown" and not row["sharing_configuration_known"]
    assert row["recipient_access"]["kind"] == "personal_space"
    assert row["recipient_access"]["space_permission"] == bound["space_permission"]
    assert "ai_permission" not in row["recipient_access"]
    assert row["recipient_access"]["current_access"] == "unknown" and not row["recipient_access"]["invocation_verified"]
    assert row["expected_model_binding"] == bound and not p.traces


def test_actual_local_source_configuration_remains_known_and_unchanged(node):  # noqa: F811
    app, store = node
    source, _ = add_source(app)
    services = app.state.llm_agent_services
    before = services["source_store"].path.read_bytes()
    catalog = ModelCatalog(services, store)
    row = next(row for row in catalog.list_models() if row["source_id"] == source["id"])
    assert row["sharing_configuration_known"] is True and row["sharing_basis"] == "local_configuration"
    assert row["sharing"] is False and row["share_scope"] == "personal_space"
    assert "recipient_access" not in row and row["destination_binding_supported"]
    assert services["source_store"].path.read_bytes() == before


def test_local_service_advertisement_does_not_establish_full_configuration(paired):  # noqa: F811
    p = paired
    record = p.consumer.refresh(p.alice.peer_id)["services"][0]
    record["peer_id"] = p.bob.peer_id
    record["publication_enabled"] = True
    record["access_policy"] = "explicit_friends"
    catalog, _, _ = consumer_catalog(p, [record])
    row = catalog.list_models()[0]
    assert row["source"] == "local_service" and row["sharing"] and row["share_scope"] == "friends"
    assert row["sharing_basis"] == "local_service_advertisement" and row["sharing_configuration_known"] is False
    assert "recipient_access" not in row


@pytest.mark.parametrize("name", ["ryn_list_models", "ryn_inspect_services"])
def test_real_tool_results_keep_remote_observation_and_strict_receipt_fields(paired, name):  # noqa: F811
    p = paired
    p.consumer.refresh(p.alice.peer_id)
    catalog, services, store = consumer_catalog(p)
    outgoing = AIAccessStore(store.home, relationship=p.bob.store.relationship)
    app = FastAPI()
    app.state.ai_access = SimpleNamespace(store=outgoing)
    tools = RynTools(app, store, services, catalog)
    result = tools.execute(name, {}, operation_id="observe", cancelled=lambda: False)
    row = result["models"][0]
    assert row["recipient_access"]["current_access"] == "unknown"
    assert row["recipient_access"]["ai_permission"] == {"relationship_id": p.rid, "revision": 1}
    if name == "ryn_inspect_services":
        assert result["friend_grants"] == []
        assert result["friend_grants_scope"] == {"provider_peer_id": p.bob.peer_id, "direction": "local_outgoing",
            "remote_provider_grants": "unknown"}
        assert p.grants.list()  # Remote provider grants exist despite the empty local list.
    receipt = {"tool": name, "arguments": {}, "result": result, "status": "confirmed"}
    def verify(candidate):
        return tools.verify(candidate, [{"verifier": name + "_facts"}], context={}, cancelled=lambda: False)
    assert verify(receipt)["status"] == "passed"
    for field in ("sharing_basis", "sharing_configuration_known", "recipient_access"):
        old = copy.deepcopy(receipt)
        old["result"]["models"][0].pop(field)
        assert verify(old)["status"] == "failed"
        assert field not in old["result"]["models"][0]  # No retroactive receipt repair.
    if name == "ryn_inspect_services":
        old = copy.deepcopy(receipt)
        old["result"].pop("friend_grants_scope")
        assert verify(old)["status"] == "failed"
    reread = tools.execute(name, {}, operation_id="new-observation", cancelled=lambda: False)
    assert verify({**receipt, "result": reread})["status"] == "passed"
