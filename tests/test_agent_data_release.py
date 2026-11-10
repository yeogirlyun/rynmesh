"""Destination policy at actual adapter admission and owned HTTP endpoints."""
import copy
import json
import threading
import urllib.error
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from test_agent_extensions_oauth import no_network as no_network  # noqa: F401
from test_agent_integrations import add_source, fake_upstream  # noqa: F401
from test_agent_integrations import node as node

from rynmesh.agent_runtime import data_policy
from rynmesh.agent_runtime.bridge import AgentModelBridge, build_agent_integrations
from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.agent_runtime.store import AgentError
from rynmesh.llm_package import peer_binding
from rynmesh.llm_package.adapters import AdapterError, OpenAICompatibleAdapter
from rynmesh.llm_package.data_release import DataReleaseError, model_request_scope, urlopen
from rynmesh.llm_package.protocol_adapter import NativeProtocolAdapter


def state(*rules, memory="deny"):
    return data_policy.merge_state(None, {"id": "restricted", "instance_id": "instance-a",
        "data_policy": {"models": {"mode": "allowlist", "allow": list(rules)}, "memory": memory}})


@pytest.fixture(autouse=True)
def disable_background_probes(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")


@contextmanager
def endpoint(*, redirect=None, status=302):
    requests = []
    config = {"redirect": redirect, "status": status}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_GET(self):
            self.serve()
        def do_POST(self):
            self.serve()
        def serve(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append({"method": self.command, "path": self.path, "body": raw,
                             "headers": dict(self.headers)})
            payload = json.dumps({"choices": [{"message": {"role": "assistant", "content": "accepted"},
                                               "finish_reason": "stop"}]}).encode()
            self.send_response(config["status"] if config["redirect"] else 200)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Type", "application/json")
            if config["redirect"]:
                self.send_header("Location", config["redirect"])
            self.end_headers()
            self.wfile.write(payload)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, config
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


def test_source_context_uses_real_record_and_actual_manager(node):  # noqa: F811 - imported pytest fixture
    app, store = node
    record, model_id = add_source(app)
    bridge, _, catalog = build_agent_integrations(app, store, store.home)
    context = bridge.data_policy_context(model_id)
    assert context["consumer_node_id"] == store.peer_id
    assert context["model_destination_verified"] is True
    assert context["api_source"] == {"base_url": record["base_url"],
        "destination_revision": context["model"]["destination_revision"]}
    assert "secret-do-not-export" not in json.dumps(context)
    assert data_policy.allows_task(state({"api_base_url_prefix": "https://vendor.test/v1/"}), context)
    assert not data_policy.allows_task(state({"api_base_url_prefix": "https://vendor.test/v10/"}), context)
    manager = bridge.services["provider_for"](context["model"]["service_id"])
    manager._source_revision = "stale-adapter"
    with pytest.raises(AgentError, match="^agent_model_destination_changed$"):
        bridge._data_policy_context(catalog.resolve(model_id), context["model_destination"], manager)


@pytest.mark.parametrize("expected", [None, {}])
def test_allowlist_requires_explicit_valid_destination_before_dispatch(node, monkeypatch, expected):  # noqa: F811 - imported pytest fixture
    app, store = node
    _, model_id = add_source(app)
    bridge, _, _ = build_agent_integrations(app, store, store.home)
    requests = fake_upstream(monkeypatch, [])
    with pytest.raises(AgentError, match="^agent_(extension_model_not_allowed|model_destination_changed)$"):
        bridge.complete(model_id, [{"role": "user", "content": "private-body"}], [], lambda: False,
                        expected_destination=expected, data_policy_state=state({"catalog_model_id": model_id}))
    assert requests == []


def test_memory_only_policy_preserves_unbound_adapter_compatibility(node, monkeypatch):  # noqa: F811 - imported pytest fixture
    app, store = node
    _, model_id = add_source(app)
    bridge, _, _ = build_agent_integrations(app, store, store.home)
    requests = fake_upstream(monkeypatch, [{"role": "assistant", "content": "ok"}], json_only=True)
    unrestricted = data_policy.merge_state(None, {"id": "memory", "instance_id": "a",
        "data_policy": {"models": {"mode": "any"}, "memory": "deny"}})
    assert bridge.complete(model_id, [{"role": "user", "content": "private-body"}], [], lambda: False,
                           data_policy_state=unrestricted)["message"]["content"] == "ok"
    assert len(requests) == 1


@pytest.mark.parametrize("policy_state", [state(), {"version": 500, "sources": []}])
def test_disallowed_or_malformed_policy_never_dispatches(node, monkeypatch, policy_state):  # noqa: F811 - imported pytest fixture
    app, store = node
    _, model_id = add_source(app)
    bridge, _, catalog = build_agent_integrations(app, store, store.home)
    requests = fake_upstream(monkeypatch, [])
    with pytest.raises(AgentError, match="^agent_extension_model_not_allowed$"):
        bridge.complete(model_id, [{"role": "user", "content": "private-body"}], [], lambda: False,
            expected_destination=catalog.destination(catalog.resolve(model_id)), data_policy_state=policy_state)
    assert requests == []


@pytest.mark.parametrize("change_at", ["request_callback", "http_boundary"])
@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic_messages"])
def test_source_change_after_admission_is_rechecked_before_body_leaves(node, monkeypatch, change_at, protocol):  # noqa: F811 - imported pytest fixture
    app, store = node
    record, model_id = add_source(app)
    bridge, _, catalog = build_agent_integrations(app, store, store.home)
    sources = bridge.services["source_store"]
    record = sources.prepare({"protocol": protocol}, record["id"])
    record.update(status="ready", tool_support={"model-a": "supported"})
    sources.save(record)
    destination = catalog.destination(catalog.resolve(model_id))
    manager = bridge.services["provider_for"](catalog.resolve(model_id)["service_id"])
    def mutate(*_args):
        changed = sources.prepare({"base_url": "https://replacement.test/v1"}, record["id"])
        changed.update(status="ready", tool_support={"model-a": "supported"})
        sources.save(changed)
    callback = mutate if change_at == "request_callback" else None
    if change_at == "http_boundary":
        inner = manager.adapter.inner
        original = inner._endpoint
        def changed_endpoint(path):
            mutate()
            return original(path)
        monkeypatch.setattr(inner, "_endpoint", changed_endpoint)
    requests = fake_upstream(monkeypatch, [])
    with pytest.raises(AgentError, match="^agent_model_destination_changed$"):
        bridge.complete(model_id, [{"role": "user", "content": "private-body"}], [], lambda: False,
            expected_destination=destination, data_policy_state=state({"catalog_model_id": model_id}),
            on_request_policy=callback)
    assert requests == []


def test_real_bridge_delivers_to_bound_endpoint_and_refuses_redirect(node):  # noqa: F811 - imported pytest fixture
    app, store = node
    with endpoint() as (sink, received, _), endpoint() as (origin, requests, config):
        sources = app.state.llm_agent_services["source_store"]
        record = sources.prepare({"name": "Owned fixture", "kind": "local", "base_url": origin + "/v1",
                                  "models": ["fixture"]})
        record.update(status="ready", tool_support={"fixture": "supported"})
        sources.save(record)
        bridge, _, catalog = build_agent_integrations(app, store, store.home)
        model_id = "local/" + sources.model_service_id(record, "fixture")
        destination = catalog.destination(catalog.resolve(model_id))
        kwargs = {"expected_destination": destination, "data_policy_state": state({"catalog_model_id": model_id})}
        messages = [{"role": "user", "content": "private-tool-result"}]
        assert bridge.complete(model_id, messages, [], lambda: False, **kwargs)["message"]["content"] == "accepted"
        config["redirect"] = sink + "/secret-redirect"
        with pytest.raises(AgentError, match="^agent_model_upstream_failed$"):
            bridge.complete(model_id, messages, [], lambda: False, **kwargs)
        assert len(requests) == 2 and received == []
        assert all(b"private-tool-result" in item["body"] for item in requests)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_scope_refuses_redirect_statuses_and_resets(status):
    with endpoint() as (sink, received, _), endpoint(redirect=sink + "/other", status=status) as (origin, sent, _):
        with model_request_scope(restricted=True):
            with pytest.raises(urllib.error.HTTPError) as error:
                urlopen(origin + "/initial", timeout=2)
            error.value.close()
        assert received == [] and len(sent) == 1
        with urlopen(origin + "/ordinary", timeout=2) as response:
            assert response.status == 200
        assert len(received) == 1 and len(sent) == 2


@pytest.mark.parametrize("method", ["infer", "chat", "stream_chat", "infer_stream", "capabilities",
                                   "openai_responses", "anthropic_messages"])
def test_every_adapter_http_exit_refuses_redirect_without_payload_leak(method):
    with endpoint() as (sink, received, _), endpoint(redirect=sink + "/private-location") as (origin, sent, _):
        options = {"base_url": origin, "model": "fixture", "api_key": "private-api-key"}
        adapter = NativeProtocolAdapter(protocol=method, **options) if method in {"openai_responses", "anthropic_messages"} else OpenAICompatibleAdapter(**options)
        calls = []
        with model_request_scope(restricted=True, before_dispatch=lambda: calls.append(True)):
            if method == "capabilities":
                assert adapter.capabilities()["streaming"] is False
            else:
                with pytest.raises(AdapterError) as error:
                    options = {"task_id": "task", "timeout_s": 2}
                    if method == "infer":
                        adapter.infer(prompt="private-body", max_tokens=10, **options)
                    elif method == "infer_stream":
                        adapter.infer_stream(prompt="private-body", max_tokens=10, on_delta=lambda _: None, **options)
                    else:
                        adapter.chat({"messages": [{"role": "user", "content": "private-body"}],
                                      "stream": method == "stream_chat"}, **options)
                assert error.value.code == ("inference_failed" if method in {"infer", "infer_stream"} else "upstream_http_failed")
                assert all(secret not in str(error.value) for secret in ("private-body", "private-api-key", "private-location"))
        assert len(sent) == 1 and received == [] and calls == [True]


def bound_bridge(*, source="local_service", runtime="native_llama_cpp", adapter="native", consumer="local"):
    binding = {"version": peer_binding.VERSION, "service_id": "fixture", "model_generation": "a" * 32,
               "rule_revision": "b" * 32}
    row = {"id": "local/fixture" if source != "peer" else "peer/fixture", "source": source,
           "service_id": "fixture", "peer_id": consumer if source != "peer" else "remote", "source_id": "",
           "destination_binding_supported": True, "runtime": runtime, "adapter": adapter,
           "local_model_binding": binding, "consumer_peer_id": consumer}
    if source == "peer":
        record = {"binding_verified": True, "consumer_peer_id": consumer, "peer_id": "remote",
                  "model_binding": binding, "service_id": "fixture",
                  "ai_permission": {"relationship_id": "c" * 32, "revision": 1}}
        row["expected_model_binding"] = peer_binding.expected(record, consumer)
        row["destination_revision"] = peer_binding.revision(record, consumer)
    else:
        row["destination_revision"] = peer_binding.local_revision(binding, consumer)
    manager = SimpleNamespace(adapter=SimpleNamespace(model_binding=binding, binding_for_model=lambda _: binding),
                              manifest=SimpleNamespace(package_id="fixture", runtime=runtime))
    catalog = SimpleNamespace(store=SimpleNamespace(peer_id=consumer), resolve=lambda _id: copy.deepcopy(row),
                              destination=ModelCatalog.destination)
    return AgentModelBridge({"provider_for": lambda _: manager}, catalog), row, manager


def test_only_bound_native_local_runtime_satisfies_on_device():
    bridge, row, manager = bound_bridge()
    assert data_policy.allows_task(state({"on_device": True}), bridge.data_policy_context(row["id"]))
    manager.adapter.model_binding = {**manager.adapter.model_binding, "model_generation": "d" * 32}
    with pytest.raises(AgentError, match="^agent_model_destination_changed$"):
        bridge.data_policy_context(row["id"])
    bridge, row, _ = bound_bridge(runtime="cli", adapter="codex_cli")
    assert not data_policy.allows_task(state({"on_device": True}), bridge.data_policy_context(row["id"]))
    assert data_policy.allows_task(state({"catalog_model_id": row["id"]}), bridge.data_policy_context(row["id"]))


def test_native_display_claim_must_match_actual_runtime():
    bridge, row, manager = bound_bridge()
    manager.manifest.runtime = "cli"
    with pytest.raises(AgentError, match="^agent_model_destination_changed$"):
        bridge.data_policy_context(row["id"])


def test_peer_data_release_checks_current_real_catalog_before_execute(tmp_path):
    from test_peer_native_protocol import peer_catalog
    catalog, services = peer_catalog(tmp_path, "anthropic_messages")
    row = catalog.list_models()[0]
    requests = []
    async def execute(request):
        requests.append(request)
        return {"state": "succeeded", "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    services.update(execute_order=execute, cancel_order=lambda _: None)
    bridge = AgentModelBridge(services, catalog)
    kwargs = {"expected_destination": catalog.destination(row),
              "data_policy_state": state({"catalog_model_id": row["id"]})}
    messages = [{"role": "user", "content": "private-tool-result"}]
    bridge.complete(row["id"], messages, [], lambda: False, **kwargs)
    assert len(requests) == 1 and requests[0]["expected_model_binding"] == row["expected_model_binding"]
    def replace(_policy):
        changed = services["discover"](catalog.network)[0]
        changed["model_binding"]["model_generation"] = "f" * 32
        services["discover"] = lambda _: [copy.deepcopy(changed)]
    with pytest.raises(AgentError, match="^agent_model_destination_changed$"):
        bridge.complete(row["id"], messages, [], lambda: False, on_request_policy=replace, **kwargs)
    assert len(requests) == 1


@pytest.mark.parametrize("mutation", ["recipient", "model", "revision", "unsupported", "resource"])
def test_peer_context_rejects_changed_or_unverified_binding(mutation):
    bridge, row, _ = bound_bridge(source="peer")
    assert data_policy.allows_task(state({"catalog_model_id": row["id"]}), bridge.data_policy_context(row["id"]))
    if mutation == "recipient":
        row["expected_model_binding"]["consumer_peer_id"] = "other"
    elif mutation == "model":
        row["expected_model_binding"]["model_binding"]["model_generation"] = "d" * 32
    elif mutation == "revision":
        row["destination_revision"] = "wrong"
    elif mutation == "unsupported":
        row["destination_binding_supported"] = False
    else:
        row["provider_resource"] = "https://directory.test/connect"
    with pytest.raises(AgentError, match="^agent_model_destination_changed$"):
        bridge.data_policy_context(row["id"])


def test_http_scope_does_not_leak_to_other_thread_or_after_exception(monkeypatch):
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: calls.append("ordinary"))
    def reject():
        raise AgentError("agent_extension_model_not_allowed")
    with model_request_scope(restricted=True, before_dispatch=reject):
        with pytest.raises(DataReleaseError, match="agent_extension_model_not_allowed"):
            urlopen("https://never.test", timeout=1)
        with model_request_scope(restricted=False):
            urlopen("https://never.test", timeout=1)
        with pytest.raises(DataReleaseError, match="agent_extension_model_not_allowed"):
            urlopen("https://never.test", timeout=1)
        worker = threading.Thread(target=lambda: urlopen("https://never.test", timeout=1))
        worker.start()
        worker.join(2)
        assert not worker.is_alive()
    urlopen("https://never.test", timeout=1)
    assert calls == ["ordinary", "ordinary", "ordinary"]
