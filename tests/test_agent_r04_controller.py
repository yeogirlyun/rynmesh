"""R04 isolated controller barriers; no model calls or semantic pass claims."""
import importlib.util
from pathlib import Path

import pytest
from test_agent_integrations import node  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.services import peer_box


@pytest.fixture
def runner(monkeypatch):
    folder = Path(__file__).resolve().parents[1] / "docs/acceptance/ryn-agent-delivery/real-model"
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location("r04_draft_controller", folder / "run_steer_r04.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_r04_actual_pending_write_is_invalidated_with_fresh_control_revision(node, runner, tmp_path):  # noqa: F811
    app, store = node
    with runner.R04World(tmp_path / "world") as world:
        selected = world.configure(app, store)
        _, tools, catalog = build_agent_integrations(app, store, store.home)
        runtime = AgentRuntime(AgentStore(store.home / "agent", peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")),
            runner.FixturePlanner(), tools, catalog, consumer_node_id=store.peer_id,
            owner_id=world.case["authorization"]["owner_id"])
        app.state.agent_runtime = runtime
        runtime.update_settings({"enabled": True, "model_id": selected, "max_steps": 24, "max_context_chars": 100000})
        result = runner.execute(runtime, world)
        assert all(result["mechanical_checks"].values())
        assert result["prompt"] == "检查 model-b 后分享给 node-b；执行过程中以我更新的要求为准。"
        assert result["control"]["correction"] == "不要分享，只检查 model-b 的状态。"
        assert result["control"]["effective_writes_at_barrier"] == 0
        assert result["control"]["stale_approval_error"] == "agent_approval_mismatch"
        assert result["control"]["after_goal_revision"] == result["control"]["before_goal_revision"] + 1
        assert result["control"]["stale_approval_request_control_revision"] > result["control"]["original_approval"]["control_revision"]
        assert result["task"]["outcome"]["status"] == "unverified"
        assert not [e for e in result["task"]["events"] if e["type"] == "approved"]
        grant = world.grants.grant(selected.removeprefix("local/"), world.rid)
        assert grant["effective"] and grant["peer_id"] == world.receiver.peer_id
        assert result["source_readback"]["model-a"]["shared"]
        assert world.bindings["node-a"] == store.peer_id
        assert runtime.owner_id == world.case["authorization"]["owner_id"] == "owner-test"
        assert world.effective_input["role_contract"]["agent_execution_node"] == "node-a"


def test_fixture_cli_never_reads_owner_source_and_exports_no_quality_pass(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "read_planner", lambda *_: pytest.fail("fixture mode read owner source"))
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "nonexistent-owner"))
    output = tmp_path / "evidence"
    monkeypatch.setattr("sys.argv", ["run_steer_r04.py", "--fixture-only", "--output", str(output)])
    assert runner.main() == 0
    import json
    result = json.loads((output / "run.json").read_text(encoding="utf-8"))
    assert result["real_model"] is False and result["formal_trial_registered"] is False
    assert result["owner_source_sha256_before"] is None
    assert result["task"]["outcome"]["status"] == "unverified"
    assert result["business_success_seen"] is None and result["runtime_candidate_pass"] is False
    assert "docs/acceptance/ryn-agent-delivery/real-model/role_query_world.py" in result["code_snapshot"]
    assert Path(runner.composer.__file__).relative_to(runner.ROOT).as_posix() in result["code_snapshot"]
    assert result["code_unchanged"]
    assert not (tmp_path / "nonexistent-owner").exists()
