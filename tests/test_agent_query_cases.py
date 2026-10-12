"""Local query-world assembly; no real planner or external endpoint is used."""
import importlib.util
import json
from pathlib import Path

import pytest
from test_agent_integrations import node  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations

_spec = importlib.util.spec_from_file_location("development_query_cases", Path(__file__).resolve().parents[1] /
    "docs/acceptance/ryn-agent-delivery/real-model/query_cases.py")
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)


@pytest.mark.parametrize("case_id", ["Q01", "Q02", "Q03"])
def test_real_encrypted_world_and_actual_presence_classifier(node, tmp_path, case_id):  # noqa: F811
    app, store = node
    with module.QueryWorld(tmp_path / "query", case_id) as controller:
        selected = controller.configure(app, store)
        _, tools, catalog = build_agent_integrations(app, store, store.home)
        original_models = {row["id"]: row for row in tools.execute("ryn_list_models", {}, operation_id="before-labels", cancelled=lambda: False)["models"]}
        controller.attach(tools)
        models = catalog.list_models()
        rows = {logical: next(row for row in models if row["id"] == controller.bindings[logical]) for logical in controller.world["models"]}
        assert selected == rows["model-a"]["id"]
        for logical, expected in controller.world["models"].items():
            row = rows[logical]
            assert (row["available"], row["agent_supported"], row["sharing"]) == (expected["available"], expected["agent_supported"], expected["sharing"])
        result = tools.execute("ryn_list_models", {}, operation_id="identity-read", cancelled=lambda: False)
        identities = {row["fixture_identity"]: row for row in result["models"]}
        assert set(identities) == set(controller.world["models"])
        for label, row in identities.items():
            assert {key: value for key, value in row.items() if key != "fixture_identity"} == original_models[row["id"]]
            assert row["id"] == controller.bindings[label]
        controller.attach(tools)  # Reattaching never changes the mapping or facts.
        assert tools.execute("ryn_list_models", {}, operation_id="identity-repeat", cancelled=lambda: False) == result
        devices = tools.execute("ryn_list_devices", {}, operation_id="fixture-read", cancelled=lambda: False)
        by_id = {row["peer_id"]: row for row in devices["devices"]}
        expected = {"node-a": (True, "http"), "node-c": (False, "heartbeat_expired"), "node-d": (None, "unknown")}
        for logical, value in expected.items():
            row = by_id[controller.bindings[logical]]
            assert (row["online"], row["evidence_source"]) == value
        evidence = controller.collect()
        assert evidence["source_configuration_unchanged"]
        assert evidence["setup_probes"][-1]["error_code"] == "connection_refused"
        assert len(evidence["signed_registry_evidence"]) == 3 and evidence["http_health_audit"]
        assert controller.upstream.secret not in json.dumps([models, devices, evidence])
        assert rows["model-d"]["evidence"]["connection"]["error_code"] == "connection_refused"


def test_query_loader_does_not_accept_heldout_or_other_cases():
    with pytest.raises(ValueError, match="development_query_case_unsupported"):
        module.load_case("H01")
