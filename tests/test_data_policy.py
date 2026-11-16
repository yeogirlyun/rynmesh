"""Tests for S13-I35: loopback HTTP data policy, source compaction, and data release check errors."""
from __future__ import annotations

import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rynmesh.agent_runtime import diagnostic_log
from rynmesh.agent_runtime.data_policy import (
    allows,
    allows_task,
    memory_denied,
    merge_state,
    normalize_policy,
    snapshot_source,
)
from rynmesh.agent_runtime.store import AgentError
from rynmesh.llm_package.data_release import DataReleaseError, model_request_scope, urlopen


def _restricted(*rules, memory: str = "allow") -> dict:
    return {"models": {"mode": "allowlist", "allow": list(rules)}, "memory": memory}


def _api_context(base_url: str, *, model_id: str = "local/ollama-qwen", rev: str = "rev-1", peer_id: str = "node-self") -> dict:
    model = {
        "id": model_id,
        "source": "local_api",
        "source_id": "src-local",
        "peer_id": peer_id,
        "destination_revision": rev,
    }
    return {
        "consumer_node_id": "node-self",
        "model": model,
        "model_destination_verified": True,
        "model_destination": {
            "catalog_model_id": model_id,
            "provider_peer_id": peer_id,
            "source_id": "src-local",
            "destination_revision": rev,
        },
        "api_source": {
            "base_url": base_url,
            "destination_revision": rev,
        },
    }


def test_s13_i35_t1_loopback_http_prefix_valid_and_matches():
    policy = _restricted({"api_base_url_prefix": "http://127.0.0.1:11434/v1/"})
    normalized = normalize_policy(policy)
    assert normalized["models"]["allow"] == [{"api_base_url_prefix": "http://127.0.0.1:11434/v1/"}]

    for valid_prefix in (
        "http://localhost:11434/v1/",
        "http://127.0.0.2:8080/api/",
        "http://[::1]:11434/v1/",
    ):
        assert normalize_policy(_restricted({"api_base_url_prefix": valid_prefix}))

    assert allows(policy, _api_context("http://127.0.0.1:11434/v1")) is True
    assert allows(policy, _api_context("http://127.0.0.1:11434/v1/chat/completions")) is True
    assert allows(policy, _api_context("http://127.0.0.1:9999/v1/")) is False
    assert allows(policy, _api_context("http://localhost:11434/v1/")) is False

    # Loopback HTTP without an explicit port remains invalid.
    for missing_port in ("http://127.0.0.1/v1/", "http://localhost/v1/", "http://[::1]/v1/"):
        with pytest.raises(AgentError, match="^agent_invalid_data_policy$"):
            normalize_policy(_restricted({"api_base_url_prefix": missing_port}))


def test_s13_i35_t2_lan_http_rejected_while_lan_https_allowed():
    for lan_http in (
        "http://192.168.1.5/",
        "http://192.168.1.5:11434/v1/",
        "http://10.0.0.8:8000/v1/",
    ):
        with pytest.raises(AgentError, match="^agent_invalid_data_policy$"):
            normalize_policy(_restricted({"api_base_url_prefix": lan_http}))
        assert allows(_restricted({"api_base_url_prefix": lan_http}), _api_context(lan_http)) is False

    lan_https = _restricted({"api_base_url_prefix": "https://192.168.1.5/v1/"})
    assert normalize_policy(lan_https)
    assert allows(lan_https, _api_context("https://192.168.1.5/v1/models")) is True


def test_s13_i35_t3_on_device_loopback_local_api_and_merge_state_dedupes_and_caps():
    on_dev = _restricted({"on_device": True})
    for loopback_url in (
        "http://127.0.0.1:11434/v1",
        "http://127.25.0.9:8080/v1/",
        "http://localhost:11434/v1",
        "http://[::1]:11434/v1",
        "https://127.0.0.1:8443/v1",
    ):
        assert allows(on_dev, _api_context(loopback_url)) is True, loopback_url

    for non_loopback_url in (
        "https://192.168.1.5/v1",
        "http://192.168.1.5:11434/v1",
        "https://api.example.com/v1",
    ):
        assert allows(on_dev, _api_context(non_loopback_url)) is False, non_loopback_url

    # Reinstalling the same server_id 3 times keeps 1 source entry and never loosens restrictions.
    s1 = snapshot_source({
        "id": "company-mcp",
        "instance_id": "inst-1",
        "data_policy": _restricted({"catalog_model_id": "m1"}, {"catalog_model_id": "m2"}, memory="allow"),
    })
    s2 = snapshot_source({
        "id": "company-mcp",
        "instance_id": "inst-2",
        "data_policy": _restricted({"catalog_model_id": "m2"}, {"catalog_model_id": "m3"}, memory="deny"),
    })
    s3 = snapshot_source({
        "id": "company-mcp",
        "instance_id": "inst-3",
        "data_policy": {"models": {"mode": "any"}, "memory": "allow"},
    })
    state = merge_state(None, s1)
    state = merge_state(state, s2)
    state = merge_state(state, s3)
    assert len(state["sources"]) == 1
    assert memory_denied(state) is True
    assert allows_task(state, _api_context("https://vendor.test/v1", model_id="m2")) is True
    assert allows_task(state, _api_context("https://vendor.test/v1", model_id="m1")) is False
    assert allows_task(state, _api_context("https://vendor.test/v1", model_id="m3")) is False

    # Exceeding 64 distinct servers compacts oldest entries into one strictest record while keeping all constraints.
    many_state = None
    for idx in range(70):
        pol = {"models": {"mode": "any"}, "memory": "deny" if idx == 0 else "allow"}
        if idx == 1:
            pol = _restricted({"on_device": True})
        snap = snapshot_source({"id": f"srv-{idx}", "instance_id": f"i-{idx}", "data_policy": pol})
        many_state = merge_state(many_state, snap)
    assert len(many_state["sources"]) == 64
    assert memory_denied(many_state) is True
    assert allows_task(many_state, _api_context("http://127.0.0.1:11434/v1")) is True
    assert allows_task(many_state, _api_context("https://vendor.test/v1")) is False


def test_s13_i35_t4_data_release_callback_oserror_reports_check_failed_without_sending(monkeypatch):
    received = []

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return None

        def do_GET(self):
            received.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logged = []
    orig_failure = diagnostic_log.failure
    monkeypatch.setattr(
        diagnostic_log,
        "failure",
        lambda stage, exc, **kw: (logged.append((stage, type(exc).__name__)), orig_failure(stage, exc, **kw)),
    )
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1/models"

        def fail_io():
            raise OSError("simulated store read error")

        with model_request_scope(restricted=True, before_dispatch=fail_io):
            with pytest.raises(DataReleaseError) as exc_info:
                urlopen(urllib.request.Request(url), timeout=2)
        assert str(exc_info.value) == "agent_data_release_check_failed"
        assert received == []
        assert any(name == "OSError" for _, name in logged)

        for passthrough_code in (
            "agent_model_destination_changed",
            "agent_extension_model_not_allowed",
            "agent_model_unavailable",
        ):
            with model_request_scope(restricted=True, before_dispatch=lambda c=passthrough_code: (_ for _ in ()).throw(AgentError(c))):
                with pytest.raises(DataReleaseError) as pass_exc:
                    urlopen(urllib.request.Request(url), timeout=2)
            assert str(pass_exc.value) == passthrough_code
        assert received == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
