"""Native policy API and actual dispatch gates with a local synthetic upstream."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_agent_integrations import node as integration_node
from test_native_model import tiny_model

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.store import AgentError
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.native_model import NativeModel, validate_model
from rynmesh.llm_package.peer_binding import local_revision

node = integration_node


@pytest.fixture
def native(node, tmp_path):
    app, store = node
    model = app.state.native_model
    original = tiny_model(tmp_path)
    model.model_path.parent.mkdir(parents=True)
    model.model_path.write_bytes(original.read_bytes())
    model.metadata = validate_model(original)
    model.config["installed"] = True
    model._save()
    model._update_manifest()
    model.state = "running"
    model.process = SimpleNamespace(poll=lambda: None)
    traces = []
    class Upstream:
        def chat(self, body, **kwargs):
            traces.append(copy.deepcopy(body))
            return {"text": "native only", "message": {"role": "assistant", "content": "native only"},
                    "finish_reason": "stop", "input_tokens": 2, "output_tokens": 2, "duration_ms": 1}
        def health(self):
            return {"ok": True}
        def shutdown(self):
            pass
    model.upstream = Upstream()
    store.llm_tool_capabilities.save(model.manifest, "supported")
    return app, store, model, traces


def configure(model, **configuration):
    return model.configure_model({"expected_rule_revision": model.status()["model_settings"]["rule_revision"],
                                  "configuration": configuration})


def test_native_actual_owner_api_cas_and_http_output_cap(native):
    app, store, model, traces = native
    with TestClient(app) as client:
        before = client.get("/api/local/llm/native").json()["model_settings"]
        body = {"expected_rule_revision": before["rule_revision"], "configuration": {"max_output_tokens": 13, "requests_per_day": 2}}
        saved = client.put("/api/local/llm/native/model-config", json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()["model_settings"]["configuration"]["max_output_tokens"] == 13
        assert client.put("/api/local/llm/native/model-config", json=body).status_code == 409
        response = client.post("/api/local/llm/orders", json={"provider_peer_id": store.peer_id,
            "service_id": model.manifest.package_id, "chat": {"messages": [{"role": "user", "content": "native current only"}]}, "max_tokens": 99})
        assert response.status_code == 200 and response.json()["state"] == "succeeded", response.text
        assert traces[0]["max_tokens"] == 13


def test_native_bridge_pins_actual_dispatch_generation(native):
    app, store, model, traces = native
    bridge, _, catalog = build_agent_integrations(app, store, store.home)
    row = next(row for row in catalog.list_models() if row["runtime"] == "native_llama_cpp")
    destination = catalog.destination(row)
    assert destination["destination_revision"] == local_revision(model.adapter.model_binding, store.peer_id)
    result = bridge.complete(row["id"], [{"role": "user", "content": "current native"}], [], lambda: False,
                             expected_destination=destination)
    assert result["message"]["content"] == "native only"
    original = model.adapter.call_bound
    def race(*args, **kwargs):
        model.rotate_model_binding()
        return original(*args, **kwargs)
    model.adapter.call_bound = race
    with pytest.raises(AgentError, match="agent_model_destination_changed"):
        bridge.complete(row["id"], [{"role": "user", "content": "never"}], [], lambda: False, expected_destination=destination)
    assert len(traces) == 1


def test_native_quota_persists_unknown_dispatch_and_old_adapters_cannot_restart(native):
    _, store, model, traces = native
    old = model.adapter
    configure(model, requests_per_day=1)
    with pytest.raises(AdapterError, match="model_configuration_changed"):
        old.chat({"messages": [], "max_tokens": 1})
    def fail(*args, **kwargs):
        traces.append("unknown")
        raise OSError("isolated simulated lost response")
    model.upstream.chat = fail
    with pytest.raises(OSError):
        model.adapter.chat({"messages": [], "max_tokens": 1})
    assert model.active == 0
    restored = NativeModel(store.home)
    restored.state, restored.process, restored.upstream = "running", model.process, model.upstream
    with pytest.raises(AdapterError, match="model_daily_quota_exhausted"):
        restored.adapter.chat({"messages": [], "max_tokens": 1})
    assert traces == ["unknown"]


def test_native_inflight_count_survives_rule_rebuild(native):
    _, _, model, traces = native
    entered, release = threading.Event(), threading.Event()
    real = model.upstream.chat
    def block(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real(*args, **kwargs)
    model.upstream.chat = block
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(model.adapter.chat, {"messages": [], "max_tokens": 1})
        assert entered.wait(5)
        try:
            configure(model, requests_per_day=2)
            with pytest.raises(AdapterError, match="runtime_busy"):
                model.adapter.chat({"messages": [], "max_tokens": 1})
        finally:
            release.set()
        future.result()
    assert len(traces) == 1 and model.active == 0


def test_native_sharing_and_runtime_restart_aba_never_restore_old_binding(native):
    _, _, model, _ = native
    old = model.adapter.model_binding
    model.preferences({"sharing": True})
    model.preferences({"sharing": False})
    assert model.adapter.model_binding != old
    before = model.adapter.model_binding
    model.rotate_model_binding()  # Called by each newly installed real upstream.
    assert model.adapter.model_binding["model_generation"] != before["model_generation"]
