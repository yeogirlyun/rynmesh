"""C03 controller plumbing and exact permission checks, never model quality."""
import copy
import importlib.util
from pathlib import Path

import pytest
from test_agent_integrations import node  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.services import peer_box


@pytest.fixture
def module(monkeypatch):
    folder = Path(__file__).resolve().parents[1] / "docs/acceptance/ryn-agent-delivery/real-model"
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location("development_sharing_cases", folder / "sharing_cases.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_c03_frozen_prompt_exact_approvals_and_independent_receiver_readback(node, module, tmp_path):  # noqa: F811
    app, store = node
    with module.SharingWorld(tmp_path / "world", "C03") as world:
        selected = world.configure(app, store)
        _, tools, catalog = build_agent_integrations(app, store, store.home)
        runtime = AgentRuntime(AgentStore(store.home / "agent", peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")),
                               module.SharingFixturePlanner(), tools, catalog, consumer_node_id=store.peer_id)
        app.state.agent_runtime = runtime
        runtime.update_settings({"enabled": True, "model_id": world.bindings["model-c"], "max_steps": 24,
                                 "max_seconds": 600, "max_context_chars": 100000})
        result = world.execute(runtime)
        assert result["prompt"] == world.case["prompt"] == result["task"]["messages"][0]["content"]
        assert result["initial_all_models_private"] and result["settings_before"]["model_id"] != selected
        assert result["settings_after"]["model_id"] == selected and result["settings_after"]["enabled"]
        assert result["settings_except_default_unchanged"] and result["sibling_and_source_configuration_unchanged"]
        assert result["receiver_visible_models"] == ["model-b"] and not result["ungranted_receiver_visible_models"]
        assert result["source_readback"]["model-b"]["share_scope"] == "friends"
        assert result["source_readback"]["model-b"]["receivers"] == ["node-b"]
        assert [p["result"] for p in result["receiver_probes"]] == ["allowed", "denied", "denied"]
        assert [p["upstream_requests"] for p in result["receiver_probes"]] == [1, 0, 0]
        assert len(result["approvals"]) == 4 and all(a["approved"] for a in result["approvals"])
        assert len([e for e in result["task"]["events"] if e["type"] == "tool_started" and e.get("effect") == "write"]) == 4
        # The scripted checker deliberately refuses to supply a quality verdict.
        assert result["task"]["status"] == "completed" and result["task"]["outcome"]["status"] == "unverified"
        assert all(c["status"] == "passed" for c in result["task"]["outcome"]["criteria_results"] if c["kind"] == "mechanical")
        valid = next(a["approval"] for a in result["approvals"] if a["approval"]["tool"] == "ryn_set_friend_model_access")
        for key, bad in (("relationship_id", "f" * 32), ("service_id", selected.removeprefix("local/")), ("allowed", False)):
            changed = copy.deepcopy(valid)
            changed["arguments"][key] = bad
            assert not world.permitted(changed)
        enabled = copy.deepcopy(result["approvals"][-1]["approval"])
        enabled["preview"]["allowed_friend_grants"].append({"relationship_id": "f" * 32, "peer_id": world.other.peer_id})
        assert not world.permitted(enabled)
        assert result["physical_hosts"] == 1 and result["formal_case_score"] is False


def test_sharing_controller_cannot_claim_single_write_recovery_or_heldout(module, tmp_path):
    for case in ("H01", "Q01"):
        with pytest.raises(ValueError, match="development_sharing_case_unsupported"):
            module.SharingWorld(tmp_path, case)
    world = module.SharingWorld(tmp_path, "R02")
    assert world.case["id"] == "R02" and world.case["setup"]["world_overrides"] == {}
    with pytest.raises(ValueError, match="sharing_recovery_requires_process_controller"):
        world.execute(None)
