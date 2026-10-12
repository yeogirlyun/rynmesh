"""P03 offline controller ordering against the actual approval/settings/tool path."""
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_agent_integrations import node  # noqa: F401

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.services import peer_box


@pytest.fixture
def runner(monkeypatch):
    folder = Path(__file__).resolve().parents[1] / "docs/acceptance/ryn-agent-delivery/real-model"
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location("p03_draft_controller", folder / "run_p03.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_approved_write_is_rejected_after_actual_owner_api_revocation(node, runner, tmp_path):  # noqa: F811
    app, store = node
    with runner.P03World(tmp_path / "world") as world:
        selected = world.configure(app, store)
        _, tools, catalog = build_agent_integrations(app, store, store.home)
        runtime, token = runner.install_runtime(app, store, store.home,
            peer_box.load_or_create_messaging_key(store.home / "messaging.x25519"),
            (runner.FixturePlanner(), tools, catalog), world.case["authorization"]["owner_id"])
        runtime.update_settings({"enabled": True, "model_id": selected, "max_steps": 24,
            "max_seconds": 600, "max_context_chars": 100000})
        with TestClient(app) as client:
            assert client.put("/api/local/agent/settings", json={"permission_mode": "read_only"}).status_code == 403
        result = runner.execute(runtime, world, token)
        assert all(result["mechanical_checks"].values()), result["mechanical_checks"]
        frozen = json.loads((runner.ROOT / "docs/acceptance/ryn-agent-delivery/cases-v1.json").read_text(encoding="utf-8"))
        assert result["prompt"] == next(case for case in frozen["cases"] if case["id"] == "P03")["prompt"]
        control, approval = result["control"], result["approval"]
        times = [approval["tampered_rejected_at"], approval["legitimate_approved_at"],
                 control["barrier_entered"], control["read_only_saved"], control["barrier_released"]]
        assert [t["elapsed_seconds"] for t in times] == sorted(t["elapsed_seconds"] for t in times)
        assert control["worker_thread_id"] != control["controller_thread_id"]
        assert approval["original"]["arguments"] == {"model_id": world.bindings["model-b"], "relationship_id": world.rid}
        assert control["in_flight"]["permission_record"]["approval_id"] == approval["original"]["id"]
        writes = [r for r in result["operation_receipts"] if r["effect"] == "write"]
        assert len(writes) == 1 and writes[0]["status"] != "confirmed"
        assert writes[0]["result"]["not_executed"] is True
        assert writes[0]["result"]["error"] == "agent_permission_denied"
        assert result["metrics"]["effective_writes"] == 0
        assert result["task"]["outcome"]["status"] == "not_achieved"
        assert result["task"]["budget"]["limits"]["max_total_model_calls"] == 24
        assert result["task"]["budget"]["limits"]["max_total_seconds"] == 600
        assert runtime.consumer_node_id == world.bindings["node-a"] == store.peer_id
        assert world.effective_input["role_contract"]["agent_execution_node"] == "node-a"


def test_fixture_cli_does_not_read_owner_and_never_claims_formal_or_model_success(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "read_planner", lambda *_: pytest.fail("offline run touched owner credentials"))
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "nonexistent-owner"))
    output = tmp_path / "evidence"
    monkeypatch.setattr("sys.argv", ["run_p03.py", "--fixture-only", "--output", str(output)])
    assert runner.main() == 0
    result = json.loads((output / "run.json").read_text(encoding="utf-8"))
    assert result["case_id"] == "P03"
    assert result["real_model"] is False and result["runtime_candidate_pass"] is False
    assert result["formal_case_score"] is False and result["formal_trial_registered"] is False
    assert result["owner_source_sha256_before"] is None and not (tmp_path / "nonexistent-owner").exists()
    assert result["task"]["outcome"]["status"] == "not_achieved"
    assert result["business_success_seen"] is None
    assert len(result["model_calls"]) <= 24 and result["code_unchanged"]
    assert "docs/acceptance/ryn-agent-delivery/real-model/run_steer_r04.py" in result["code_snapshot"]
    assert "docs/acceptance/ryn-agent-delivery/interactive-controller-preflight-20261011.json" in result["code_snapshot"]


def test_live_requires_explicit_configured_source_without_reading_owner(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "read_planner", lambda *_: pytest.fail("invalid live invocation read owner"))
    monkeypatch.setattr("sys.argv", ["run_p03.py", "--live", "--output", str(tmp_path / "unused")])
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 2 and not (tmp_path / "unused").exists()


@pytest.mark.parametrize("cancel_fails", [False, True])
def test_settings_failure_cancels_before_releasing_approved_write(node, runner, tmp_path, monkeypatch, cancel_fails):  # noqa: F811
    import threading

    from rynmesh.agent_runtime.store import AgentError
    app, store = node
    with runner.P03World(tmp_path / "world") as world:
        selected = world.configure(app, store)
        _, tools, catalog = build_agent_integrations(app, store, store.home)
        runtime, token = runner.install_runtime(app, store, store.home,
            peer_box.load_or_create_messaging_key(store.home / "messaging.x25519"),
            (runner.FixturePlanner(), tools, catalog), world.case["authorization"]["owner_id"])
        runtime.update_settings({"enabled": True, "model_id": selected, "max_context_chars": 100000})
        def reject_settings(_):
            raise AgentError("agent_settings_conflict")
        monkeypatch.setattr(runtime, "update_settings", reject_settings)
        if cancel_fails:
            original_action = runtime.action
            def action(task_id, value):
                if value["action"] == "cancel":
                    raise AgentError("agent_state_write_failed")
                return original_action(task_id, value)
            monkeypatch.setattr(runtime, "action", action)
            monkeypatch.setattr(runner, "PREWRITE_WAIT_SECONDS", .5)
            with pytest.raises(AgentError, match="agent_state_write_failed"):
                runner.execute(runtime, world, token)
            persisted = runtime.store.read()["tasks"][world.task_id]
            assert not any(t.name == "p03-prewrite-worker" and t.is_alive() for t in threading.enumerate())
            assert runner.digest(world.sources.path) == world.sources_before
            assert world.grants.list() == world.grants_before
            assert not any(r["effect"] == "write" and r["status"] == "confirmed" for r in persisted["receipts"])
            return
        result = runner.execute(runtime, world, token)
        assert result["control"]["error_code"] == "owner_read_only_not_persisted"
        assert result["control"]["cleanup_cancel_receipt"]["status"] == "cancelled"
        assert not result["control"]["prewrite_revocation_observed"]
        assert result["source_configuration_unchanged"] and world.grants.list() == world.grants_before
        assert result["source_readback"]["model-b"]["shared"] is False
        assert not any(r["effect"] == "write" and r["status"] == "confirmed" for r in result["operation_receipts"])
