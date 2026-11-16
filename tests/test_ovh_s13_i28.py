"""S13-I28: hung visibility, cancellation, isolation and safe diagnostics."""
import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI
from test_agent_runtime import Catalog, Model, Tools

from rynmesh.ai_access.store import AIAccessError
from rynmesh.background_workers import BackgroundWorkerRegistry, BackgroundWorkerSpec, BackoffPolicy, WorkerRunResult


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


def spec(name, body, **kwargs):
    return BackgroundWorkerSpec(name, body, BackoffPolicy.fixed(60), **kwargs)


def test_t1_hung_state_and_body_free_diagnostic(caplog):
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            release.wait(3)
        registry = BackgroundWorkerRegistry()
        registry.register(spec("ryn-agent-test", blocked, run_timeout_s=0.02))
        try:
            await registry.start()
            await until(entered.is_set)
            await until(lambda: registry.status()["ryn-agent-test"].get("hung"))
            row = await asyncio.to_thread(lambda: registry.status()["ryn-agent-test"])
            assert row["in_run"] and row["running_for_s"] >= 0.02
            await until(lambda: any("background_worker_hung:ryn-agent-test" in r.getMessage() for r in caplog.records))
            assert any("elapsed_seconds=" in r.getMessage() for r in caplog.records)
        finally:
            release.set()
            await registry.stop()
    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())


def test_t2_hung_disappears_after_return():
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            release.wait(3)
        registry = BackgroundWorkerRegistry()
        registry.register(spec("returns", blocked, run_timeout_s=0.01))
        try:
            await registry.start()
            await until(entered.is_set)
            await until(lambda: registry.status()["returns"].get("hung"))
            release.set()
            await until(lambda: bool(registry.status()["returns"]["last_success_at"]))
            row = registry.status()["returns"]
            assert not row["hung"] and not row["in_run"]
        finally:
            release.set()
            await registry.stop()
    asyncio.run(scenario())


def test_t3_no_timeout_does_not_report_hung():
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            release.wait(3)
        registry = BackgroundWorkerRegistry()
        registry.register(spec("no-timeout", blocked))
        try:
            await registry.start()
            await until(entered.is_set)
            await asyncio.sleep(0.03)
            row = registry.status()["no-timeout"]
            assert row.get("in_run") is True
            assert row.get("hung", False) is False
        finally:
            release.set()
            await registry.stop()
    asyncio.run(scenario())


def test_t4_failure_log_omits_exception_message(caplog):
    async def scenario():
        def failing():
            raise RuntimeError("PRIVATE_EXCEPTION_MESSAGE")
        registry = BackgroundWorkerRegistry()
        registry.register(spec("friend-check", failing))
        await registry.start()
        await until(lambda: registry.status()["friend-check"]["consecutive_failures"] == 1)
        await registry.stop()
    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    messages = [r.getMessage() for r in caplog.records]
    assert any("background worker friend-check failed: RuntimeError" in m for m in messages)
    assert all("PRIVATE_EXCEPTION_MESSAGE" not in m for m in messages)


def test_t5_repeated_failure_logging_is_limited_to_five_minutes(caplog):
    now, calls = [0.0], [0]
    async def sleep(_):
        calls[0] += 1
        now[0] = [1, 299, 300, 301][calls[0] - 1]
        if calls[0] == 4:
            raise asyncio.CancelledError
    def failing():
        raise RuntimeError("PRIVATE_MESSAGE")
    registry = BackgroundWorkerRegistry(sleep=sleep, monotonic=lambda: now[0])
    item = spec("deduplicated", failing)
    registry.register(item)
    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await registry._run(item)
    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    messages = [r.getMessage() for r in caplog.records if "background worker deduplicated failed" in r.getMessage()]
    assert len(messages) == 2


def test_t5_stop_signals_sync_worker_and_waits_for_actual_exit():
    async def scenario():
        entered, exited = threading.Event(), threading.Event()
        def cooperative(cancelled):
            entered.set()
            assert isinstance(cancelled, threading.Event)
            cancelled.wait(3)
            exited.set()
        registry = BackgroundWorkerRegistry(stop_timeout_s=1)
        registry.register(spec("cooperative", cooperative))
        await registry.start()
        await until(entered.is_set)
        result = await registry.stop()
        assert exited.is_set()
        assert result == {"stopped": ["cooperative"], "abandoned": []}
    asyncio.run(scenario())


def test_t6_replace_does_not_overlap_even_after_timeout():
    async def scenario():
        entered, release, exited, new = (threading.Event() for _ in range(4))
        cancelled_seen = []
        def old(cancelled):
            entered.set()
            cancelled.wait(2)
            cancelled_seen.append(cancelled.is_set())
            release.wait(3)
            exited.set()
        def replacement():
            assert exited.is_set()
            new.set()
        registry = BackgroundWorkerRegistry(stop_timeout_s=0.03)
        registry.register(spec("replace", old))
        try:
            await registry.start()
            await until(entered.is_set)
            registry.register(spec("replace", replacement), replace=True)
            await asyncio.sleep(0.08)
            assert not new.is_set()
            assert registry.status()["replace"].get("abandoned") is True
            release.set()
            await until(new.is_set)
            assert cancelled_seen == [True]
        finally:
            release.set()
            await registry.stop()
    asyncio.run(scenario())


def test_stop_reports_noncooperative_thread_as_abandoned():
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        def old():
            entered.set()
            release.wait(3)
        registry = BackgroundWorkerRegistry(stop_timeout_s=0.02)
        registry.register(spec("abandoned", old))
        try:
            await registry.start()
            await until(entered.is_set)
            result = await registry.stop()
            assert result == {"stopped": [], "abandoned": ["abandoned"]}
        finally:
            release.set()
            await asyncio.sleep(0.03)
    asyncio.run(scenario())


def test_t7_eight_blocked_workers_leave_default_executor_available():
    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        release = threading.Event()
        entered = [threading.Event() for _ in range(8)]
        names = []
        def blocked(index):
            names.append(threading.current_thread().name)
            entered[index].set()
            release.wait(3)
        registry = BackgroundWorkerRegistry()
        for index in range(8):
            registry.register(spec(str(index), lambda index=index: blocked(index)))
        try:
            await registry.start()
            await until(lambda: all(e.is_set() for e in entered))
            assert await asyncio.wait_for(asyncio.to_thread(lambda: "route-ok"), 0.5) == "route-ok"
            assert len(names) == 8 and all(n.startswith("ryn-bg") for n in names)
        finally:
            release.set()
            await registry.stop()
    asyncio.run(scenario())


def test_t8_project_error_code_is_preserved_in_status():
    async def scenario():
        def failing():
            raise AIAccessError("ai_friend_service_unreachable")
        registry = BackgroundWorkerRegistry()
        registry.register(spec("coded", failing))
        await registry.start()
        await until(lambda: registry.status()["coded"]["consecutive_failures"] == 1)
        row = registry.status()["coded"]
        await registry.stop()
        assert row["error"] == "AIAccessError: ai_friend_service_unreachable"
    asyncio.run(scenario())


@pytest.mark.parametrize("exception", [RuntimeError("private path /tmp/hidden"), RuntimeError("looks_like_a_code"), AIAccessError("private /tmp/hidden")])
def test_t9_arbitrary_error_text_remains_redacted(exception):
    assert BackgroundWorkerRegistry._sanitize_error(exception) == type(exception).__name__ + ": background worker invocation failed"


def test_agent_registered_workers_have_real_timeout_budget(tmp_path):
    from rynmesh.agent_runtime.routes import install_agent
    registry = BackgroundWorkerRegistry()
    runtime = install_agent(FastAPI(), store=object(), home=tmp_path, workers=registry, local_control=lambda _: None,
                            messaging_key=X25519PrivateKey.generate(), integrations=(Model(), Tools(), Catalog()))
    settings_max = runtime.store.inspect(lambda data: data["settings"]["max_seconds"])
    workers = [s for s in registry.specs() if s.name in {"ryn-agent", *(f"ryn-agent-{n}" for n in range(2, 7))}]
    assert len(workers) == 6
    assert all(s.run_timeout_s >= settings_max + 120 for s in workers)


def test_real_pollers_respect_pre_cancelled_event():
    from rynmesh.ai_access.catalog import FriendAICatalog
    from rynmesh.ask_ryn.runs import AskRunService
    cancelled = threading.Event()
    cancelled.set()
    friends, commands = Mock(), Mock()
    catalog = FriendAICatalog(friends=friends, grants=Mock(), provider=Mock())
    assert catalog.run_once(cancelled=cancelled) is False
    friends.assert_not_called()
    ask = AskRunService(Mock(), Mock(), commands)
    assert ask.run_once(cancelled=cancelled) == WorkerRunResult()
    commands.assert_not_called()


def test_agent_loop_checks_cancelled_between_steps():
    from rynmesh.agent_runtime.runtime import AgentRuntime
    cancelled = threading.Event()
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._step = SimpleNamespace(clean=True)
    def one_step():
        cancelled.set()
        return WorkerRunResult(activity=True)
    runtime.run_once = Mock(side_effect=one_step)
    assert runtime.run_continuously(cancelled=cancelled) == WorkerRunResult(activity=True)
    assert runtime.run_once.call_count == 1


def test_registered_discovery_and_ask_pollers_forward_cancelled(tmp_path):
    from rynmesh.ai_access.routes import install_ai_access
    from rynmesh.ask_ryn.routes import install_ask_ryn
    app, registry = FastAPI(), BackgroundWorkerRegistry()
    key = X25519PrivateKey.generate()
    install_ai_access(app, home=tmp_path, local_control=lambda _: None, relationship=lambda _: None,
                      friends=Mock(), workers=registry)
    install_ask_ryn(app, store=SimpleNamespace(home=tmp_path), home=tmp_path, workers=registry,
                    local_control=lambda _: None, messaging_key=key)
    cancelled = threading.Event()
    cancelled.set()
    # Invoke production-registered wrappers, then the real cancellation-aware services.
    for name in ("ai-access.discovery", "ask-ryn-runs"):
        worker = next(s for s in registry.specs() if s.name == name)
        outcome = worker.run_once(cancelled=cancelled)
        assert outcome is False or outcome == WorkerRunResult()


def test_registered_friend_delivery_checks_cancelled(tmp_path):
    from rynmesh.friends.routes import install_friends
    from rynmesh.store import RynmeshStore
    app, registry = FastAPI(), BackgroundWorkerRegistry()
    store = RynmeshStore(home=tmp_path / "home", network_dir=tmp_path / "network")
    service = install_friends(app, store=store, home=store.home, workers=registry,
                              local_control=lambda _: None, messaging_key=X25519PrivateKey.generate())
    service.list_friends = Mock(side_effect=AssertionError("cancelled tick must not read friends"))
    cancelled = threading.Event()
    cancelled.set()
    worker = next(s for s in registry.specs() if s.name == "friends.delivery")
    assert worker.run_once(cancelled=cancelled) == WorkerRunResult()
    app.state.friend_diagnostics.close()
