"""Tests for S02-I12: Node reports real Agent task counts & titles to the desktop shell."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


class _StubModel:
    def complete(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}


class _StubCatalog:
    def list_models(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "local:test",
                "name": "Test",
                "source": "local_api",
                "available": True,
                "agent_supported": True,
            }
        ]


class _StubTools:
    def specs(self) -> list[dict[str, Any]]:
        return []

    def metadata(self, name: str) -> dict[str, Any]:
        return {"effect": "read", "title": name}


def _make_app_with_llm(tmp_path: Path) -> tuple[FastAPI, Path]:
    home = tmp_path / "node_home"
    net = tmp_path / "network"
    store = RynmeshStore(home=home, network_dir=net)
    app = FastAPI()
    install_llm_routes(
        app,
        store=store,
        home=home,
        messaging_key=peer_box.load_or_create_messaging_key(home / "messaging.x25519"),
        resolve_endpoint=lambda _: "",
        resolve_pubkey=lambda _: "",
    )
    return app, home


# T1: Running / queued / waiting_approval (plus waiting_input & retry-waiting & title truncation/capping)
def test_t1_desktop_status_and_activity_counts_with_running_queued_retrying_and_waiting(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("RYNMESH_DESKTOP_MODE", "1")
    app, home = _make_app_with_llm(tmp_path)

    with TestClient(app) as client:
        # Before agent_runtime is attached: fallback to 0 and []
        resp_no_agent = client.get("/api/local/desktop/status")
        assert resp_no_agent.status_code == 200
        body_no_agent = resp_no_agent.json()
        assert body_no_agent["agent_active_runs"] == 0
        assert body_no_agent["agent_waiting"] == 0
        assert body_no_agent["agent_task_titles"] == []

        key = X25519PrivateKey.generate()
        agent_store = AgentStore(home / "agent", key)
        runtime = AgentRuntime(agent_store, _StubModel(), _StubTools(), _StubCatalog())
        runtime.update_settings({"enabled": True, "model_id": "local:test"})
        app.state.agent_runtime = runtime

        long_title = "整理季度财务报表并核对所有海外渠道账单明细_" + ("超长后缀" * 10)
        assert len(long_title) > 40

        t_run = runtime.create({"message": "Running task", "title": long_title})
        t_queued = runtime.create({"message": "Queued task", "title": "排队中的代码检查任务"})
        t_approval = runtime.create({"message": "Approval task", "title": "等待主人审批写操作"})
        t_done = runtime.create({"message": "Completed task", "title": "已结束历史任务"})

        def _seed_states(data: dict[str, Any]) -> None:
            data["tasks"][t_run["id"]]["status"] = "running"
            data["tasks"][t_run["id"]]["title"] = long_title
            data["tasks"][t_queued["id"]]["status"] = "queued"
            data["tasks"][t_queued["id"]]["title"] = "排队中的代码检查任务"
            data["tasks"][t_approval["id"]]["status"] = "waiting_approval"
            data["tasks"][t_approval["id"]]["title"] = "等待主人审批写操作"
            data["tasks"][t_done["id"]]["status"] = "completed"

        runtime.store.transaction(_seed_states)
        with runtime._active_lock:
            runtime._active_runs[1001] = {
                "task_id": t_run["id"],
                "started": 1.0,
                "recorded_seconds": 0.0,
                "revision": 1,
            }

        # Verify lightweight inspect (no full store.read() transcript copy)
        def _forbid_full_read() -> dict[str, Any]:
            raise AssertionError("activity_counts must use store.inspect, not store.read")

        monkeypatch.setattr(runtime.store, "read", _forbid_full_read)
        counts = runtime.activity_counts()
        assert counts["running"] == 1
        assert counts["queued"] == 1
        assert counts["waiting_approval"] == 1
        assert counts["waiting_input"] == 0
        assert counts["agent_active_runs"] == 2
        assert counts["agent_waiting"] == 1
        assert len(counts["agent_task_titles"]) == 2
        assert counts["agent_task_titles"][0] == long_title[:40]
        assert len(counts["agent_task_titles"][0]) == 40
        assert counts["agent_task_titles"][1] == "排队中的代码检查任务"

        resp = client.get("/api/local/desktop/status")
        assert resp.status_code == 200
        status = resp.json()
        assert status["desktop_managed"] is True
        assert status["active_tasks"] == 0
        assert status["agent_active_runs"] == 2
        assert status["agent_waiting"] == 1
        assert status["agent_task_titles"] == [long_title[:40], "排队中的代码检查任务"]

        monkeypatch.undo()
        monkeypatch.setenv("RYNMESH_DESKTOP_MODE", "1")

        # Add retry-waiting task + waiting_input task + 2 more queued tasks -> >3 active runs, titles capped at 3
        t_retry = runtime.create({"message": "Retry wait task"})
        t_input = runtime.create({"message": "Input wait task"})
        t_q2 = runtime.create({"message": "Queued 2"})
        t_q3 = runtime.create({"message": "Queued 3"})

        def _seed_more(data: dict[str, Any]) -> None:
            data["tasks"][t_retry["id"]].update(
                status="queued",
                title="网络波动等待重试任务",
                connection_recovery={"state": "retrying", "retry_count": 1, "next_retry_at": 9999999999.0},
            )
            data["tasks"][t_input["id"]].update(status="waiting_input", title="等待补充参数")
            data["tasks"][t_q2["id"]].update(status="queued", title="排队任务二")
            data["tasks"][t_q3["id"]].update(status="queued", title="排队任务三")

        runtime.store.transaction(_seed_more)
        counts2 = runtime.activity_counts()
        assert counts2["running"] == 1
        assert counts2["retrying"] == 1
        assert counts2["agent_active_runs"] == 5
        assert counts2["agent_waiting"] == 2
        assert len(counts2["agent_task_titles"]) == 3
        assert all(len(t) <= 40 for t in counts2["agent_task_titles"])

        resp2 = client.get("/api/local/desktop/status").json()
        assert resp2["agent_active_runs"] == 5
        assert resp2["agent_waiting"] == 2
        assert len(resp2["agent_task_titles"]) == 3
