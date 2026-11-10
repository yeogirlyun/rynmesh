"""Cancellation stops the response wait without cancelling another order or replaying."""
import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient
from test_llm_auto_connect import mesh  # noqa: F401

from rynmesh.llm_package import routes
from rynmesh.llm_package.consumer_cancellation import ConsumerOrderCancelled, cancellable_exchange
from rynmesh.llm_package.p2p_session import SessionPool
from rynmesh.llm_package.task_balance import TaskBalanceLedger
from rynmesh.llm_package.task_protocol import TaskOrderStore


def test_predispatch_cancellation_does_not_construct_transport():
    called = []
    async def exchange():
        called.append(True)
    with pytest.raises(ConsumerOrderCancelled):
        asyncio.run(cancellable_exchange(exchange, lambda: True))
    assert not called


def test_waiter_cancellation_drains_cleanup_and_keeps_original_failure():
    async def run():
        stopped, closed = False, False
        async def exchange():
            nonlocal stopped, closed
            stopped = True
            try:
                await asyncio.Event().wait()
            finally:
                closed = True
        with pytest.raises(ConsumerOrderCancelled):
            await asyncio.wait_for(cancellable_exchange(exchange, lambda: stopped), 2)
        assert closed
        async def failed():
            raise ValueError("original failure")
        with pytest.raises(ValueError, match="original failure"):
            await cancellable_exchange(failed, lambda: False)
    asyncio.run(run())


def test_cancelled_pool_exchange_is_evicted_and_next_order_runs_once():
    pool = SessionPool()
    started, closed = threading.Event(), threading.Event()
    calls, made = [], []
    class Channel:
        last_used = 0
        async def close(self):
            closed.set()
        async def probe(self):
            return True
    async def factory():
        channel = Channel()
        made.append(channel)
        return channel, {}
    async def exchange(channel, evidence):
        calls.append(channel)
        started.set()
        await asyncio.Event().wait()
    async def recovered(channel, evidence):
        calls.append(channel)
        return evidence
    try:
        with pytest.raises(ConsumerOrderCancelled):
            asyncio.run(cancellable_exchange(lambda: pool.run("peer", factory, exchange), started.is_set))
        assert closed.wait(2)
        result = asyncio.run(pool.run("peer", factory, recovered))
        assert result["connection_reused"] is False
        assert len(made) == len(calls) == 2 and made == calls
    finally:
        pool.close()


def test_queued_order_cancellation_does_not_close_active_session():
    pool = SessionPool()
    started, release, closed = threading.Event(), threading.Event(), threading.Event()
    calls = []
    class Channel:
        last_used = 0
        async def close(self):
            closed.set()
        async def probe(self):
            return True
    async def factory():
        return Channel(), {}
    async def active(*_):
        calls.append("active")
        started.set()
        while not release.is_set():
            await asyncio.sleep(.01)
        return "done"
    async def queued(*_):
        calls.append("queued")
    async def run():
        first = asyncio.create_task(pool.run("peer", factory, active))
        while not started.is_set():
            await asyncio.sleep(.01)
        polls = [0]
        def cancel_second():
            polls[0] += 1
            return polls[0] > 1
        with pytest.raises(ConsumerOrderCancelled):
            await cancellable_exchange(lambda: pool.run("peer", factory, queued), cancel_second)
        assert not closed.is_set()
        release.set()
        assert await first == "done"
    try:
        asyncio.run(asyncio.wait_for(run(), 3))
        assert calls == ["active"]
    finally:
        release.set()
        pool.close()


def test_real_ice_cancel_returns_before_slow_provider_and_reconnects(mesh, monkeypatch):  # noqa: F811
    started, release, cancelled, finished = (threading.Event() for _ in range(4))
    original = mesh.adapter.infer
    calls = []
    def slow(**kwargs):
        calls.append(True)
        if len(calls) == 1:
            started.set()
            assert release.wait(8)
            finished.set()
        return original(**kwargs)
    monkeypatch.setattr(mesh.adapter, "infer", slow)
    monkeypatch.setattr(mesh.adapter, "cancel", lambda _: cancelled.set(), raising=False)
    body = {"network_id": "test-space", "provider_peer_id": mesh.nodes[0].peer_id,
            "service_id": "test-model", "prompt": "private", "max_tokens": 16,
            "transport": "p2p", "task_id": "cancel-ice-one"}
    result = []
    with TestClient(mesh.apps[1]) as client:
        worker = threading.Thread(target=lambda: result.append(client.post("/api/local/llm/orders", json=body)))
        worker.start()
        try:
            assert started.wait(5)
            before = time.monotonic()
            response = client.post("/api/local/llm/orders/cancel-ice-one/cancel")
            assert response.status_code == 200
            worker.join(2)
            assert not worker.is_alive() and time.monotonic() - before < 2
            assert cancelled.is_set() and not finished.is_set()
            assert result[0].status_code == 200, result[0].text
            terminal = result[0].json()
            assert terminal["state"] == "cancelled" and terminal["error_code"] == "consumer_cancelled"
            assert not {"output", "message", "reported_usage", "input_tokens"} & terminal.keys()
            assert client.get("/api/local/llm/orders/cancel-ice-one").json()["state"] == "cancelled"
            release.set()
            assert finished.wait(2)
            again = client.post("/api/local/llm/orders", json={**body, "task_id": "cancel-ice-two"})
            assert again.status_code == 200, again.text
            assert again.json()["state"] == "succeeded"
            assert again.json()["transport_evidence"]["connection_reused"] is False
            assert len(calls) == 2
            assert client.get("/api/local/llm/orders/cancel-ice-one").json()["state"] == "cancelled"
        finally:
            release.set()
            worker.join(5)


def test_authenticated_late_success_does_not_revive_cancelled_order(mesh, monkeypatch):  # noqa: F811
    task_id = "late-after-local-cancel"
    services = mesh.apps[1].state.llm_agent_services
    provider = mesh.apps[0].state.llm_agent_services["provider_for"]("test-model")
    monkeypatch.setattr(mesh.adapter, "cancel", lambda _: True, raising=False)
    published = []
    original_publish = routes.StreamEventBroker.publish
    def publish(self, key, event):
        if key == task_id:
            published.append(event)
        return original_publish(self, key, event)
    monkeypatch.setattr(routes.StreamEventBroker, "publish", publish)
    async def completed_then_cancelled(**kwargs):
        response = provider.handle(kwargs["signed_request"])
        services["cancel_order"](task_id)
        return response, {"transport": "test_authenticated_response", "relay_used": False}
    monkeypatch.setattr(routes, "consumer_exchange", completed_then_cancelled)
    with TestClient(mesh.apps[1]) as client:
        assert client.put("/api/local/llm/privacy", json={"result_retention_seconds": 3600}).status_code == 200
        response = client.post("/api/local/llm/orders", json={
            "network_id": "test-space", "provider_peer_id": mesh.nodes[0].peer_id,
            "service_id": "test-model", "prompt": "private", "max_tokens": 16,
            "transport": "p2p", "task_id": task_id})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["state"] == "cancelled" and result["error_code"] == "consumer_cancelled"
        assert not {"output", "message"} & result.keys()
        assert client.get("/api/local/llm/orders/" + task_id).json()["state"] == "cancelled"
        assert mesh.adapter.calls == 1
        record = TaskOrderStore(mesh.nodes[1].home / "llm" / "consumer-orders").get(task_id)
        assert "encrypted_response" not in record
        assert not any(event.get("event") == "complete" for event in published)


@pytest.mark.parametrize("stage", ["before_checkpoint", "after_checkpoint", "after_settle"])
def test_cancel_during_terminal_commit_observes_committed_success(mesh, monkeypatch, stage):  # noqa: F811
    """Cancellation cannot split retained response, accounting, and final state."""
    task_id = "commit-race-" + stage
    services = mesh.apps[1].state.llm_agent_services
    provider = mesh.apps[0].state.llm_agent_services["provider_for"]("test-model")
    cancel_started, cancel_done = threading.Event(), threading.Event()
    receipts, workers, errors = [], [], []

    def cancel():
        cancel_started.set()
        try:
            receipts.append(services["cancel_order"](task_id))
        except Exception as exc:
            errors.append(exc)
        finally:
            cancel_done.set()

    def race():
        worker = threading.Thread(target=cancel, daemon=True)
        workers.append(worker)
        worker.start()
        assert cancel_started.wait(2)
        # The cancel call is already contending with the in-progress commit.
        # On the old implementation it returns cancelled during this pause.
        assert not cancel_done.wait(.2)

    async def authenticated_response(**kwargs):
        return provider.handle(kwargs["signed_request"]), {
            "transport": "test_authenticated_response", "relay_used": False}

    original_checkpoint = TaskOrderStore.checkpoint
    def checkpoint(self, **kwargs):
        target = (kwargs["task_id"] == task_id
                  and kwargs.get("metadata", {}).get("settlement_pending"))
        if target and stage == "before_checkpoint":
            race()
        result = original_checkpoint(self, **kwargs)
        if target and stage == "after_checkpoint":
            race()
        return result

    original_settle = TaskBalanceLedger.settle
    def settle(self, **kwargs):
        result = original_settle(self, **kwargs)
        if kwargs["task_id"] == task_id and stage == "after_settle":
            race()
        return result

    monkeypatch.setattr(routes, "consumer_exchange", authenticated_response)
    monkeypatch.setattr(TaskOrderStore, "checkpoint", checkpoint)
    monkeypatch.setattr(TaskBalanceLedger, "settle", settle)
    monkeypatch.setattr(mesh.adapter, "cancel", lambda _: True, raising=False)
    with TestClient(mesh.apps[1]) as client:
        assert client.put("/api/local/llm/privacy", json={"result_retention_seconds": 3600}).status_code == 200
        try:
            response = client.post("/api/local/llm/orders", json={
                "network_id": "test-space", "provider_peer_id": mesh.nodes[0].peer_id,
                "service_id": "test-model", "prompt": "private", "max_tokens": 16,
                "transport": "p2p", "task_id": task_id})
            assert response.status_code == 200, response.text
            assert response.json()["state"] == "succeeded"
            assert response.json()["output"] == "Private response"
            assert cancel_done.wait(2)
            assert not errors
            assert receipts == [{"task_id": task_id, "state": "succeeded"}]
            assert client.get("/api/local/llm/orders/" + task_id).json()["state"] == "succeeded"
            events = client.get("/api/local/task-balance").json()["events"]
            settles = [event for event in events if event["task_id"] == task_id
                       and event["kind"] == "settle"]
            assert len(settles) == 1
            assert settles[0]["input_tokens"] == 4 and settles[0]["output_tokens"] == 2
            assert not any(event["task_id"] == task_id and event["kind"] == "release" for event in events)
            assert mesh.adapter.calls == 1
        finally:
            for worker in workers:
                worker.join(3)


def test_cancelled_transport_failure_returns_cancelled_without_output(mesh, monkeypatch):  # noqa: F811
    task_id = "cancel-before-transport-failure"
    services = mesh.apps[1].state.llm_agent_services
    monkeypatch.setattr(mesh.adapter, "cancel", lambda _: True, raising=False)
    async def cancelled_failure(**kwargs):
        assert services["cancel_order"](task_id)["state"] == "cancelled"
        raise RuntimeError("transport closed after cancellation")
    monkeypatch.setattr(routes, "consumer_exchange", cancelled_failure)
    with TestClient(mesh.apps[1]) as client:
        response = client.post("/api/local/llm/orders", json={
            "network_id": "test-space", "provider_peer_id": mesh.nodes[0].peer_id,
            "service_id": "test-model", "prompt": "private", "max_tokens": 16,
            "transport": "p2p", "task_id": task_id})
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "cancelled"
        assert not {"output", "message"} & response.json().keys()
        assert client.get("/api/local/llm/orders/" + task_id).json()["state"] == "cancelled"
        assert mesh.adapter.calls == 0
