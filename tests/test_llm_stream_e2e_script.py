from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import scripts.llm_stream_e2e as llm_e2e
from rynmesh.llm_package.test_adapter import app as deterministic_adapter
from scripts.llm_stream_e2e import _mode_order, _parse_sse_lines


def test_sse_evidence_records_delta_before_terminal() -> None:
    ticks = iter((1_000_000, 2_000_000, 3_000_000))
    events = _parse_sse_lines(
        [
            b"event: state\n", b'data: {"state":"running"}\n', b"\n",
            b"event: delta\n", b'data: {"sequence":0,"delta":"ok"}\n', b"\n",
            b"event: complete\n", b'data: {"state":"succeeded","output":"ok"}\n', b"\n",
        ],
        now_ns=lambda: next(ticks),
    )
    assert [event["event"] for event in events] == ["state", "delta", "complete"]
    assert events[1]["received_ns"] < events[2]["received_ns"]


@pytest.mark.parametrize(
    ("mode", "transport"),
    [
        ("stream-test", "direct"),
        ("test", "p2p"),
        ("relay-test", "relay"),
        ("local-stream", "direct"),
        ("local-fallback", "direct"),
    ],
)
def test_deterministic_modes_request_stream_but_preserve_transport_boundary(
    mode: str, transport: str,
) -> None:
    order = _mode_order(mode, provider_id="provider", service_id="service", task_id="task")
    assert order["response_mode"] == "stream-v1"
    assert order["transport"] == transport


def test_deterministic_adapter_emits_real_sse_chunks_and_final_usage() -> None:
    with TestClient(deterministic_adapter) as client:
        response = client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "stream evidence"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        })
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = response.text.split("\n\n")
    assert sum('"delta":{"content":' in frame for frame in frames) == 3
    assert any('"usage":' in frame for frame in frames)
    assert "data: [DONE]" in response.text


def test_deterministic_adapter_can_expose_complete_only_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RYNMESH_TEST_ADAPTER_DISABLE_STREAM", "1")
    with TestClient(deterministic_adapter) as client:
        response = client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "fallback evidence"}],
            "stream": True,
        })
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["choices"][0]["message"]["content"]


@pytest.mark.parametrize("result_pending", [False, True])
@pytest.mark.parametrize(("provider_base", "consumer_base"), [
    ("http://127.0.0.1:18891", "http://127.0.0.1:18892"),
    ("http://127.0.0.1:51301", "http://127.0.0.1:51302"),
])
def test_stream_verifier_writes_timing_route_and_exactly_once_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path, provider_base: str, consumer_base: str,
    result_pending: bool,
) -> None:
    provider_id = "provider-peer"
    async_calls = 0
    result_reads = 0
    authorized = False
    permission = {"relationship_id": "fixture-friend", "revision": 4}

    def authorize(peer, service, **endpoints):
        nonlocal authorized
        assert peer == provider_id and service == "e2e-test-service"
        assert endpoints == {"provider_base": provider_base, "consumer_base": consumer_base}
        authorized = True
        return permission

    def fake_json(url, body=None, timeout=20):
        nonlocal async_calls, result_reads
        del timeout
        if url.endswith("/services/publish"):
            return {"record": {"peer_id": provider_id}}
        if "/llm/services?" in url:
            assert authorized
            return {"services": [{
                "peer_id": provider_id,
                "service": {"package_id": "e2e-test-service"},
            }]}
        if url.endswith("/llm/orders/async"):
            assert authorized and body["ai_permission"] == permission
            async_calls += 1
            return {
                "task_id": task_id,
                "state": "queued" if async_calls == 1 else "succeeded",
            }
        if url == consumer_base + "/api/local/llm/orders/" + task_id:
            assert async_calls == 1
            result_reads += 1
            if result_pending and result_reads == 1:
                return {"task_id": task_id, "state": "running"}
            return {
                "task_id": task_id, "state": "succeeded", "output": "private",
                "transport": "peer_http_direct",
                "transport_evidence": {"relay_used": False,
                                       "stream_protocol": "rynmesh.llm.stream.v1"},
            }
        if url == consumer_base + "/api/local/task-balance":
            return {"available": 99, "held": 0, "earned": 0, "events": [
                {"kind": "hold", "task_id": task_id},
                {"kind": "settle", "task_id": task_id},
            ]}
        if url == provider_base + "/api/local/task-balance":
            return {"available": 100, "held": 0, "earned": 1, "events": [
                {"event_id": "earning:" + task_id, "kind": "earning", "task_id": task_id},
            ]}
        if url == consumer_base + "/api/local/llm/orders":
            return {"orders": [{"task_id": task_id, "history": [{"state": "succeeded"}]}]}
        if url == provider_base + "/api/local/llm/provider-orders":
            return {"orders": [{"task_id": task_id, "history": [{"state": "succeeded"}]}]}
        raise AssertionError(f"unexpected URL: {url}")

    def fake_sse(_url, timeout=300):
        del timeout
        base = llm_e2e.time.monotonic_ns()
        return [
            {"event": "delta", "received_ns": base + 1_000_000,
             "data": {"sequence": 0, "delta": "private"}},
            {"event": "complete", "received_ns": base + 2_000_000, "data": {
                "task_id": task_id, "state": "succeeded",
            }},
        ]

    monkeypatch.setattr(llm_e2e.time, "time_ns", lambda: 1)
    task_id = "e2e-stream-test-1"
    monkeypatch.setattr(llm_e2e, "_json", fake_json)
    monkeypatch.setattr(llm_e2e, "_authorize_consumer", authorize)
    monkeypatch.setattr(llm_e2e, "_sse_events", fake_sse)
    monkeypatch.setattr(llm_e2e, "_git_commit", lambda: "abc123")
    monkeypatch.setattr(llm_e2e, "ROOT", tmp_path)

    report = llm_e2e.verify("stream-test", provider_base=provider_base, consumer_base=consumer_base)
    assert report["commit"] == "abc123"
    assert report["timing"]["first_delta_before_terminal"] is True
    assert report["consumer_hold_recorded_once"] is True
    assert report["consumer_settlement_recorded_once"] is True
    assert report["provider_earning_recorded_once"] is True
    assert report["duplicate_submission_reused_task"] is True
    assert report["consumer_task_recorded_once"] is True
    assert report["provider_task_recorded_once"] is True
    assert report["effective_response_mode"] == "stream-v1"
    assert report["stream_event_count"] == 1
    assert result_reads == (2 if result_pending else 1) and async_calls == 2
    assert "output_preview" not in report
    assert "provider_peer_id" not in report
    assert report["provider_peer_id_sha256"]
    assert (tmp_path / "deploy/llm-e2e/results/stream-test-result.json").is_file()


def test_stream_verifier_stops_before_order_submission_when_sharing_is_denied(monkeypatch):
    calls = []

    def fake_json(url, body=None, timeout=20):
        calls.append(url)
        assert url.endswith("/services/publish")
        return {"record": {"peer_id": "provider-peer"}}

    def deny(*args, **kwargs):
        raise RuntimeError("E2E consumer did not receive explicit AI authorization")

    monkeypatch.setattr(llm_e2e, "_json", fake_json)
    monkeypatch.setattr(llm_e2e, "_authorize_consumer", deny)
    with pytest.raises(RuntimeError, match="explicit AI authorization"):
        llm_e2e.verify("local-stream")
    assert len(calls) == 1


def test_stream_verifier_stops_before_order_submission_when_discovery_times_out(monkeypatch):
    calls = []
    ticks = iter((0, 1, 31))

    def fake_json(url, body=None, timeout=20):
        calls.append(url)
        if url.endswith("/services/publish"):
            return {"record": {"peer_id": "provider-peer"}}
        assert "/llm/services?" in url
        return {"services": []}

    monkeypatch.setattr(llm_e2e, "_json", fake_json)
    monkeypatch.setattr(llm_e2e, "_authorize_consumer", lambda *args, **kwargs: {"revision": 1})
    monkeypatch.setattr(llm_e2e.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(llm_e2e.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="authorized service was not discovered"):
        llm_e2e.verify("local-stream")
    assert len(calls) == 2


def test_local_environment_removes_host_transport_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    monkeypatch.setenv("RYNMESH_NETWORK_KEY", "do-not-inherit")
    monkeypatch.setenv("RYNMESH_LLM_FORCE_RELAY", "1")
    monkeypatch.setenv("RYNMESH_REGISTRY_URLS", "https://external.invalid")
    env = llm_e2e._local_environment(tmp_path)
    assert "RYNMESH_NETWORK_KEY" not in env
    assert "RYNMESH_LLM_FORCE_RELAY" not in env
    assert "RYNMESH_REGISTRY_URLS" not in env
    assert env["RYNMESH_NETWORK_ID"] == "rynmesh-llm-e2e"
    assert env["RYNMESH_LOCAL_TOKEN"] == "rynmesh-e2e-browser-token"
    assert env["RYNMESH_FRIEND_ALLOW_LOOPBACK"] == "1"


def test_ci_runs_deterministic_stream_verifier() -> None:
    workflow = (llm_e2e.ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "python scripts/llm_stream_e2e.py local-run" in workflow
    assert "python scripts/llm_stream_e2e.py stream-run" in workflow


@pytest.mark.parametrize(("mode", "forced_relay"), [("stream-test", "0"), ("relay-test", "1")])
def test_deterministic_up_does_not_inherit_route_or_manifest_overrides(
    monkeypatch: pytest.MonkeyPatch, mode: str, forced_relay: str,
) -> None:
    captured = {}

    def fake_compose(*args, env=None):
        captured["args"] = args
        captured["env"] = env

    monkeypatch.setenv("RYNMESH_LLM_MANIFEST", "/untrusted/host-value.json")
    monkeypatch.setenv("RYNMESH_LLM_FORCE_RELAY", "unexpected")
    monkeypatch.setattr(llm_e2e, "_compose", fake_compose)
    monkeypatch.setattr(llm_e2e, "_wait", lambda *_args, **_kwargs: None)

    llm_e2e.up(mode)
    assert captured["args"][:2] == ("--profile", "test")
    assert captured["env"]["RYNMESH_LLM_MANIFEST"] == (
        "/config/stream-manifest.json" if mode == "stream-test" else "/config/test-manifest.json"
    )
    assert captured["env"]["RYNMESH_LLM_FORCE_RELAY"] == forced_relay
