"""Destination decisions use verified identities and monotonic source snapshots."""
import copy
import socket

import pytest

from rynmesh.agent_runtime.data_policy import (
    allows,
    allows_task,
    memory_blocked,
    memory_denied,
    merge_state,
    normalize_policy,
    snapshot_source,
    task_requires_model_restriction,
)
from rynmesh.agent_runtime.store import AgentError


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Network is forbidden in policy tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def restricted(rule):
    return {"models": {"mode": "allowlist", "allow": [rule]}, "memory": "allow"}


def context(**changes):
    model = {"id": "local/model", "source": "local_service", "runtime": "native_llama_cpp",
             "peer_id": "self", "source_id": "", "destination_revision": "r1", **changes}
    return {"consumer_node_id": "self", "model": model, "model_destination_verified": True,
            "model_destination": {"catalog_model_id": model["id"], "provider_peer_id": model["peer_id"],
                                  "source_id": model["source_id"], "destination_revision": model["destination_revision"]}}


@pytest.mark.parametrize("value", [False, [], {"memory": []}, {"models": {"mode": []}},
    {"unexpected": True}, {"models": {"mode": "any", "allow": []}},
    {"models": {"mode": "allowlist"}}, restricted({"on_device": 1}),
    restricted({"catalog_model_id": ""}), restricted({"on_device": True, "catalog_model_id": "a"}),
    restricted({"api_base_url_prefix": "http://localhost/"}),
    restricted({"api_base_url_prefix": "https://api.example/v1"}),
    restricted({"api_base_url_prefix": "https://user@api.example/"}),
    restricted({"api_base_url_prefix": "https://api.example/?query=/"}),
    restricted({"api_base_url_prefix": "https://api.example/%2e%2e/"}),
    restricted({"api_base_url_prefix": "https://api.example/%zz/"}),
    restricted({"api_base_url_prefix": "https://api.example:0/"})])
def test_invalid_manifest_policies_have_fixed_error(value):
    with pytest.raises(AgentError, match="^agent_invalid_data_policy$"):
        normalize_policy(value)
    assert not allows(value, context())


def test_default_and_missing_context_fail_closed_for_allowlist():
    assert normalize_policy(None) == {"models": {"mode": "any"}, "memory": "allow"}
    assert allows(None) and allows({"memory": "deny"})
    assert not allows(restricted({"on_device": True}))
    assert not allows({"models": {"mode": "allowlist", "allow": []}}, context())


@pytest.mark.parametrize("changes,expected", [({}, True), ({"runtime": "codex_cli"}, False),
    ({"runtime": "antigravity_cli"}, False), ({"source": "peer"}, False),
    ({"source": "local_api"}, False), ({"peer_id": "other"}, False)])
def test_on_device_never_trusts_remote_cli_or_api(changes, expected):
    assert allows(restricted({"on_device": True}), context(**changes)) is expected


@pytest.mark.parametrize("field", ["catalog_model_id", "provider_peer_id", "source_id", "destination_revision"])
def test_exact_catalog_rule_still_requires_current_destination_binding(field):
    ctx = context()
    assert allows(restricted({"catalog_model_id": "local/model"}), ctx)
    ctx["model_destination"][field] = "stale"
    assert not allows(restricted({"catalog_model_id": "local/model"}), ctx)
    ctx = context(); ctx["model_destination_verified"] = False
    assert not allows(restricted({"catalog_model_id": "local/model"}), ctx)


@pytest.mark.parametrize("url,expected", [("https://api.example/v1", True), ("https://API.EXAMPLE:443/v1/", True),
    ("https://api.example/v1/models", True), ("https://api.example/v11/", False),
    ("https://api.example.evil/v1/", False), ("https://api.example:444/v1/", False),
    ("http://api.example/v1/", False), ("https://api.example/v1/../other", False),
    ("https://api.example/v1/%2fother", False), ("https://api.example/v1/%252e", False),
    ("https://api.example/v1/#fragment", False), ("https://name@api.example/v1/", False)])
def test_api_prefix_uses_origin_path_and_current_source_revision(url, expected):
    ctx = context(source="local_api", source_id="source-a")
    ctx["api_source"] = {"base_url": url, "destination_revision": "r1"}
    policy = restricted({"api_base_url_prefix": "https://api.example/v1/"})
    assert allows(policy, ctx) is expected
    ctx["api_source"]["destination_revision"] = "old"
    assert not allows(policy, ctx)


def test_snapshots_intersect_deduplicate_and_survive_server_reimport():
    first = snapshot_source({"id": "remote", "instance_id": "old", "data_policy": restricted({"on_device": True})})
    state = merge_state(None, first)
    frozen = copy.deepcopy(state)
    assert merge_state(state, first) == state
    state = merge_state(state, snapshot_source({"id": "remote", "instance_id": "new", "data_policy": {"memory": "deny"}}))
    assert len(state["sources"]) == 2 and state["revision"] == 2
    assert allows_task(state, context())
    assert not allows_task(state, context(source="peer"))
    assert memory_denied(state) and memory_denied({"data_policy_state": state})
    assert memory_blocked(frozen) and not memory_denied(frozen)
    assert state != frozen and frozen["revision"] == 1
    assert merge_state(state, None) == state
    assert merge_state(None, state)["sources"] == state["sources"]
    assert not memory_blocked({"id": "fresh"})


def test_allowlist_rules_are_alternatives_when_api_prefix_cannot_match():
    ctx = context(source="local_api", source_id="source-a")
    ctx["api_source"] = {"base_url": "http://localhost:3210/v1/", "destination_revision": "r1"}
    policy = {"models": {"mode": "allowlist", "allow": [
        {"api_base_url_prefix": "https://api.example/"}, {"catalog_model_id": "local/model"}]}}
    assert allows(policy, ctx)


def test_corrupt_persisted_policy_never_grants_memory_or_model_access():
    bad = {"version": 1, "sources": [{"instance_id": "broken"}], "memory_deny": False}
    assert not allows_task(bad, context())
    assert task_requires_model_restriction(bad)
    assert memory_denied(bad) and memory_blocked(bad)
