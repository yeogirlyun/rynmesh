"""S13-I61: keep blocking node/source work off the ASGI loop."""
import ast
import asyncio
import inspect
import json
import logging
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from rynmesh import peer_http
from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.services import peer_box
from rynmesh.services.digest import DigestService
from rynmesh.services.egress_control import EgressController
from rynmesh.store import RynmeshStore


@pytest.fixture
def node(tmp_path, monkeypatch):
    home = tmp_path / "node"
    monkeypatch.setenv("RYNMESH_HOME", str(home))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DEFAULT_DISCOVERY", "0")
    monkeypatch.setenv("RYNMESH_REGISTRY_URL", "")
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    app = peer_http.create_app(store)
    yield app, store, home
    app.state.llm_shutdown()


async def _probe_during_blocking_call(app, method, path, body, install_pause):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    started = []

    def pause():
        started.append(time.perf_counter())
        loop.call_soon_threadsafe(entered.set)
        time.sleep(2)

    install_pause(pause)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        assert (await client.get("/health")).status_code == 200
        operation = asyncio.create_task(client.request(method, path, json=body))
        await asyncio.wait_for(entered.wait(), timeout=15)
        health = await client.get("/health")
        latency = time.perf_counter() - started[0]
        result = await operation
    return health, result, latency


def test_t1_digest_build_two_seconds_does_not_block_health(node, monkeypatch):
    app, _, _ = node

    def install(pause):
        def slow_build(*args, **kwargs):
            pause()
            return {"items": []}
        monkeypatch.setattr(DigestService, "build", slow_build)

    health, result, latency = asyncio.run(_probe_during_blocking_call(
        app, "PATCH", "/api/local/recommendations/profile", {"direction": "gardens"}, install))
    assert result.status_code == 200, result.text
    assert result.json()["direction"] == "gardens"
    assert health.json()["status"] == "ok"
    assert latency < 0.100, f"health delayed {latency:.3f}s by digest build"


def _pool_call(node):
    return isinstance(node, ast.Call) and (
        isinstance(node.func, ast.Name) and node.func.id == "run_in_threadpool"
        or isinstance(node.func, ast.Attribute) and node.func.attr == "to_thread"
    )


def _blocking_call(node):
    if not isinstance(node, ast.Call):
        return False
    value = node.func
    while isinstance(value, (ast.Attribute, ast.Call)):
        if isinstance(value, ast.Attribute) and value.attr == "OllamaProvider":
            return True
        value = value.value if isinstance(value, ast.Attribute) else value.func
    return isinstance(value, ast.Name) and value.id in {
        "active_store", "_digest_service", "_recommendation_profile", "OllamaProvider", "egress_controller"
    }


def _blocking_violations(route):
    definitions = {item.name: item for item in route.body if isinstance(item, ast.FunctionDef)}
    queued = {
        arg.id for call in ast.walk(route) if _pool_call(call)
        for arg in call.args if isinstance(arg, ast.Name)
    }
    direct = set()

    class DirectCalls(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            self.visit(node.args)
            for decorator in node.decorator_list:
                self.visit(decorator)

        def visit_Lambda(self, node):
            self.visit(node.args)

        def visit_Call(self, node):
            if isinstance(node.func, ast.Name):
                direct.add(node.func.id)
            self.generic_visit(node)

    collector = DirectCalls()
    for statement in route.body:
        collector.visit(statement)
    queued -= direct
    violations = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            self.visit(node.args)
            for decorator in node.decorator_list:
                self.visit(decorator)
            if definitions.get(node.name) is not node or node.name not in queued:
                for statement in node.body:
                    self.visit(statement)

        def visit_Call(self, node):
            if _blocking_call(node):
                violations.append((route.name, node.lineno, ast.unparse(node)))
            self.visit(node.func)
            for index, arg in enumerate(node.args):
                if _pool_call(node) and isinstance(arg, ast.Lambda):
                    self.visit(arg.args)
                else:
                    self.visit(arg)
            for keyword in node.keywords:
                if _pool_call(node) and isinstance(keyword.value, ast.Lambda):
                    self.visit(keyword.value.args)
                else:
                    self.visit(keyword.value)

    visitor = Visitor()
    for statement in route.body:
        visitor.visit(statement)
    return violations


def _node_factory():
    module = ast.parse(Path(peer_http.__file__).read_text(encoding="utf-8"))
    return next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "create_app")


def test_t2_async_route_ast_has_no_direct_blocking_calls():
    factory = _node_factory()
    routes = [node for node in factory.body if isinstance(node, ast.AsyncFunctionDef) and any(
        isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
        and isinstance(decorator.func.value, ast.Name) and decorator.func.value.id == "app"
        for decorator in node.decorator_list)]
    assert len(routes) >= 20
    violations = [item for route in routes for item in _blocking_violations(route)]
    assert violations == [], "Direct loop I/O: " + repr(violations)


@pytest.mark.parametrize("body,safe", [
    ("active_store.register_node()", False),
    ("await run_in_threadpool(active_store.register_node)", True),
    ("await run_in_threadpool(_digest_service().build)", False),
    ("await run_in_threadpool(lambda: _digest_service().build())", True),
    ("def work():\n    _recommendation_profile.patch({})\nwork()", False),
    ("def work():\n    _recommendation_profile.patch({})\nawait asyncio.to_thread(work)", True),
    ("def work():\n    _recommendation_profile.patch({})\nawait run_in_threadpool(_recommendation_call, work)", True),
    ("def work():\n    _recommendation_profile.patch({})\nawait run_in_threadpool(_recommendation_call, work())", False),
    ("def work():\n    _recommendation_profile.patch({})\nwork()\nawait asyncio.to_thread(work)", False),
    ("await asyncio.to_thread(lambda value=active_store.node_info(): value)", False),
    ("def work(value=active_store.node_info()):\n    return value\nawait asyncio.to_thread(work)", False),
    ("OllamaProvider().installed()", False),
    ("egress_controller.connect(region='unit')", False),
])
def test_t2_guard_distinguishes_queued_callbacks_from_eager_calls(body, safe):
    source = "async def endpoint():\n" + "\n".join("    " + line for line in body.splitlines())
    assert (not _blocking_violations(ast.parse(source).body[0])) is safe


def test_lifespan_registration_is_awaited_in_a_worker():
    lifespan = next(node for node in _node_factory().body
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan")
    direct = [node for node in ast.walk(lifespan) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute) and node.func.attr == "register_node"
              and isinstance(node.func.value, ast.Name) and node.func.value.id == "active_store"]
    assert direct == []
    assert any(isinstance(node, ast.Await) and _pool_call(node.value) and node.value.args
               and isinstance(node.value.args[0], ast.Attribute)
               and node.value.args[0].attr == "register_node" for node in ast.walk(lifespan))


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/local/recommendations/profile", None),
    ("PATCH", "/api/local/recommendations/profile", {"direction": "gardens"}),
    ("POST", "/api/local/recommendations/feedback", {"contentId": "sample", "action": "more"}),
])
def test_t3_corrupt_profile_is_503_and_records_diagnostics(node, monkeypatch, method, path, body):
    app, _, home = node
    (home / "recommendation-profile.json").write_bytes(b"{broken json")
    monkeypatch.setattr(DigestService, "recommendation_items", lambda self: [{"content_id": "sample", "title": "Sample"}])

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url="http://127.0.0.1") as client:
            return await client.request(method, path, json=body)

    response = asyncio.run(request())
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "recommendation_profile_unavailable"}
    from rynmesh import diagnostic_events
    assert any(event["component"] == "recommendation" and event["code"] == "recommendation_profile_unavailable"
               and event["exception_type"] == "AtomicIOError" for event in diagnostic_events.recent())


def test_profile_value_error_remains_400_with_code(node):
    app, _, home = node
    (home / "recommendation-profile.json").write_text(json.dumps({"version": 99}), encoding="utf-8")

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url="http://127.0.0.1") as client:
            return await client.get("/api/local/recommendations/profile")

    response = asyncio.run(request())
    assert response.status_code == 400
    assert response.json() == {"detail": "recommendation_profile_version_unsupported"}


@pytest.mark.parametrize("owner,operation,path,body,expected", [
    (RynmeshStore, "register_job_capacity", "/api/local/jobs/capacity/register",
     {"capabilities": ["unit"], "capacity_units": 2, "network_id": "unit-network"}, {"capacity_units": 2, "network_id": "unit-network"}),
    (RynmeshStore, "submit_work_order", "/api/local/jobs/work-orders",
     {"provider_peer_id": "unit-provider", "operation": "unit"}, {"provider_peer_id": "unit-provider", "operation": "unit"}),
    (EgressController, "connect", "/api/local/egress/connect",
     {"region": "unit-region", "provider_peer_id": "unit-provider"}, {"region": "unit-region", "provider_peer_id": "unit-provider"}),
    (EgressController, "disconnect", "/api/local/egress/disconnect",
     {"region": "unit-region"}, {"region": "unit-region"}),
])
def test_registry_and_egress_work_runs_on_worker(node, monkeypatch, owner, operation, path, body, expected):
    app, _, _ = node
    observed = []

    def perform(self, **kwargs):
        observed.append((threading.get_ident(), kwargs))
        return {"ok": True}

    monkeypatch.setattr(owner, operation, perform)

    async def request():
        loop_thread = threading.get_ident()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = await client.post(path, json=body)
        return loop_thread, response

    loop_thread, response = asyncio.run(request())
    assert response.status_code == 200, response.text
    assert len(observed) == 1 and observed[0][0] != loop_thread
    assert {key: observed[0][1][key] for key in expected} == expected


def test_source_save_holds_both_locks_on_worker_and_keeps_health_responsive(tmp_path, monkeypatch):
    home = tmp_path / "node"
    monkeypatch.setenv("RYNMESH_HOME", str(home))
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    app = FastAPI()
    install_llm_routes(app, store=store, home=home,
                       messaging_key=peer_box.load_or_create_messaging_key(home / "messaging.x25519"),
                       resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    endpoint = next(route.endpoint for route in app.routes
                    if getattr(route, "path", "") == "/api/local/llm/sources/{source_id}"
                    and "PUT" in getattr(route, "methods", set()))
    sources = inspect.getclosurevars(endpoint).nonlocals["source_store"]
    lock_threads = {"space": [], "source": []}

    class TrackedLock:
        def __init__(self, lock, name):
            self.lock, self.name = lock, name

        def __enter__(self):
            lock_threads[self.name].append(threading.get_ident())
            return self.lock.__enter__()

        def __exit__(self, *args):
            return self.lock.__exit__(*args)

    monkeypatch.setattr(store.personal_space, "lock", TrackedLock(store.personal_space.lock, "space"))
    source_lock = type(sources).lock.fget
    monkeypatch.setattr(type(sources), "lock", property(lambda current: TrackedLock(source_lock(current), "source")))
    prepare = sources.prepare

    def install(pause):
        def slow_prepare(*args, **kwargs):
            pause()
            return prepare(*args, **kwargs)
        monkeypatch.setattr(sources, "prepare", slow_prepare)

    async def exercise():
        loop_thread = threading.get_ident()
        result = await _probe_during_blocking_call(app, "PUT", "/api/local/llm/sources/new", {
            "name": "Example", "kind": "api", "provider": "openai", "base_url": "https://api.example.test/v1",
            "models": ["example-model"],
        }, install)
        return loop_thread, result

    try:
        loop_thread, (health_response, saved, latency) = asyncio.run(exercise())
        assert saved.status_code == 200, saved.text
        assert health_response.json() == {"status": "ok"}
        assert latency < 0.100, f"health delayed {latency:.3f}s by source save"
        assert all(lock_threads.values())
        assert all(thread != loop_thread for threads in lock_threads.values() for thread in threads)
        assert sources.read()[saved.json()["id"]]["base_url"] == "https://api.example.test/v1"
    finally:
        app.state.llm_shutdown()


def test_diagnostic_record_is_rate_limited_bounded_and_redacted(monkeypatch, caplog):
    from rynmesh import diagnostic_events
    clock = [1000.0]
    monkeypatch.setattr(diagnostic_events, "monotonic", lambda: clock[0])
    caplog.set_level(logging.WARNING, logger="rynmesh.diagnostic_events")
    try:
        raise ValueError("PRIVATE_MESSAGE_CANARY_S13_I61")
    except ValueError as exc:
        diagnostic_events.record("i61_test", "profile_unavailable", exc)
        diagnostic_events.record("i61_test", "profile_unavailable", exc)
        clock[0] += 301
        diagnostic_events.record("i61_test", "profile_unavailable", exc)
    events = [event for event in diagnostic_events.recent() if event["component"] == "i61_test"]
    assert len(events) == 2
    assert all(event["exception_type"] == "ValueError" and event["file"] == Path(__file__).name
               and isinstance(event["line"], int) and event["line"] > 0 for event in events)
    assert "PRIVATE_MESSAGE_CANARY_S13_I61" not in json.dumps(events) + caplog.text
    for index in range(55):
        diagnostic_events.record("i61_ring", f"event_{index}")
    recent = diagnostic_events.recent()
    assert len(recent) == 50
    assert [event["code"] for event in recent] == [f"event_{index}" for index in range(5, 55)]
