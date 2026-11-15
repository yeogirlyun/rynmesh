"""S01-I3: Unlimited connection retry, exponential backoff, and per-source stream_max_retries."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from test_agent_runtime import advance, assistant, make_runtime
from test_agent_runtime_retry import failing_model
from test_agent_integrations import node  # noqa: F401
from rynmesh.agent_runtime import budget, retry_policy
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.diagnostic_log import safe_diagnostics
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.llm_package import model_settings
from rynmesh.services import peer_box


@pytest.fixture
def fake_clock(monkeypatch):
    wall = [2000.0]
    monkeypatch.setattr(retry_policy, "now", lambda: wall[0])
    monkeypatch.setattr(retry_policy.random, "uniform", lambda *_: 1.0)
    return wall


def _err(code: str = "agent_model_connection_failed", **diagnostics) -> AgentError:
    exc = AgentError(code)
    exc.diagnostics = diagnostics
    return exc


def _act(runtime: AgentRuntime, task_id: str, action: str):
    return runtime.action(task_id, {"action": action, "expected_revision": runtime.get(task_id)["revision"]})


@contextmanager
def _anthropic_status_server(responses: list[tuple[int, dict[str, str], bytes]]):
    requests: list[str] = []
    remaining = list(responses)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            requests.append(self.path)
            status, headers, payload = remaining.pop(0) if remaining else responses[-1]
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()

        def log_message(self, format, *args):  # noqa: A002
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "code,diagnostics,expected_limit",
    [
        # Connection failures WITH explicit network_reason and without http_status -> None (unlimited)
        ("agent_model_connection_failed", {"network_reason": "reset"}, None),
        ("agent_model_connection_failed", {"network_reason": "refused"}, None),
        ("agent_model_connection_failed", {"network_reason": "connection"}, None),
        ("agent_model_connection_failed", {"network_reason": "dns_temporary"}, None),
        ("agent_model_connection_failed", {"network_reason": "timeout"}, None),
        # Connection failures WITHOUT network_reason (unknown reason) -> finite stream_max_retries (default 5)
        ("agent_model_connection_failed", {}, 5),
        ("agent_model_connection_failed", {"stream_max_retries": 4}, 4),
        # Connection code WITH http_status falls back to finite HTTP rules, not unlimited
        ("agent_model_connection_failed", {"network_reason": "http", "http_status": 503}, 5),
        # TLS / DNS not found / quota remain 0
        ("agent_model_connection_failed", {"network_reason": "tls_certificate"}, 0),
        ("agent_model_connection_failed", {"network_reason": "tls"}, 0),
        ("agent_model_connection_failed", {"network_reason": "dns_not_found"}, 0),
        ("agent_model_upstream_failed", {"network_reason": "quota", "http_status": 429}, 0),
        # Non-retryable HTTP statuses remain 0
        ("agent_model_upstream_failed", {"http_status": 400}, 0),
        ("agent_model_upstream_failed", {"http_status": 401}, 0),
        ("agent_model_upstream_failed", {"http_status": 403}, 0),
        ("agent_model_upstream_failed", {"http_status": 404}, 0),
        # Stream interrupted, idle timeout, and retryable HTTP statuses (including 529) use stream_max_retries (default 5, cap 20)
        ("agent_model_stream_interrupted", {}, 5),
        ("agent_model_stream_interrupted", {"stream_max_retries": 0}, 0),
        ("agent_model_stream_interrupted", {"stream_max_retries": 9}, 9),
        ("agent_model_stream_interrupted", {"stream_max_retries": 25}, 20),
        ("agent_inference_timeout", {"network_reason": "idle_timeout"}, 5),
        ("agent_inference_timeout", {"network_reason": "idle_timeout", "stream_max_retries": 12}, 12),
        ("agent_model_upstream_failed", {"http_status": 408}, 5),
        ("agent_model_upstream_failed", {"http_status": 429}, 5),
        ("agent_model_upstream_failed", {"http_status": 500}, 5),
        ("agent_model_upstream_failed", {"http_status": 502}, 5),
        ("agent_model_upstream_failed", {"http_status": 503}, 5),
        ("agent_model_upstream_failed", {"http_status": 504}, 5),
        ("agent_model_upstream_failed", {"http_status": 529}, 5),
        ("agent_model_upstream_failed", {"http_status": 529, "stream_max_retries": 8}, 8),
        ("agent_model_upstream_failed", {"http_status": 529, "stream_max_retries": 99}, 20),
        # Busy -> 5
        ("agent_model_busy", {}, 5),
        # Non-idle inference timeout (task budget / ambiguous slow header) -> 1
        ("agent_inference_timeout", {}, 1),
        ("agent_inference_timeout", {"network_reason": "timeout"}, 1),
        # Non-network retryable codes (malformed response / compaction) -> 1
        ("agent_invalid_model_response", {}, 1),
        ("agent_compaction_size_limit", {}, 1),
        ("agent_compaction_incomplete", {}, 1),
    ],
)
def test_t1_limit_table_driven(code, diagnostics, expected_limit):
    assert retry_policy.limit(code, diagnostics) == expected_limit


@pytest.mark.parametrize(
    "code,diagnostics,counts_and_expected",
    [
        # Unlimited class: 5s doubling up to 60s cap
        (
            "agent_model_connection_failed",
            {"network_reason": "refused"},
            [(1, 5), (2, 10), (3, 20), (4, 40), (5, 60), (6, 60), (8, 60)],
        ),
        # Unlimited class with overloaded=True: doubled start (10s) up to 60s cap
        (
            "agent_model_connection_failed",
            {"network_reason": "reset", "overloaded": True},
            [(1, 10), (2, 20), (3, 40), (4, 60), (5, 60)],
        ),
        # Finite class: 2/4/8/16/32s
        (
            "agent_model_upstream_failed",
            {"http_status": 503},
            [(1, 2), (2, 4), (3, 8), (4, 16), (5, 32), (6, 32)],
        ),
        (
            "agent_model_stream_interrupted",
            {},
            [(1, 2), (2, 4), (3, 8), (4, 16), (5, 32)],
        ),
        # Finite class with overloaded=True: doubled start (4s)
        (
            "agent_model_upstream_failed",
            {"http_status": 529, "overloaded": True},
            [(1, 4), (2, 8), (3, 16), (4, 32), (5, 32)],
        ),
        # retry_after_seconds takes max over computed backoff
        (
            "agent_model_upstream_failed",
            {"http_status": 429, "retry_after_seconds": 25},
            [(1, 25), (2, 25), (5, 32)],
        ),
    ],
)
def test_t1_delay_table_driven_and_resume_policy(monkeypatch, code, diagnostics, counts_and_expected):
    monkeypatch.setattr(retry_policy.random, "uniform", lambda *_: 1.0)
    for count, expected in counts_and_expected:
        assert retry_policy.delay(count, diagnostics, code) == pytest.approx(expected)


def test_t1_delay_jitter_bounds_and_resume_writes_new_max_retries():
    for _ in range(30):
        d_unlim = retry_policy.delay(1, {"network_reason": "reset"}, "agent_model_connection_failed")
        assert 4.5 <= d_unlim <= 5.5
        d_fin = retry_policy.delay(3, {"http_status": 502}, "agent_model_upstream_failed")
        assert 7.2 <= d_fin <= 8.8

    # Change 3: resume writes new rule's max_retries (None for unlimited connection failure)
    unlimited_row = {
        "elapsed_seconds": 0.0,
        "connection_recovery": {
            "state": "stopped",
            "retry_count": 3,
            "max_retries": 3,
            "error_code": "agent_model_connection_failed",
            "diagnostics": {"network_reason": "refused"},
        },
    }
    retry_policy.resume(unlimited_row)
    assert unlimited_row["connection_recovery"] == {
        "state": "retrying",
        "retry_count": 0,
        "max_retries": None,
    }

    configured_stream_row = {
        "elapsed_seconds": 0.0,
        "connection_recovery": {
            "state": "disconnected",
            "retry_count": 5,
            "max_retries": 5,
            "error_code": "agent_model_stream_interrupted",
            "diagnostics": {"stream_max_retries": 9},
        },
    }
    retry_policy.resume(configured_stream_row)
    assert configured_stream_row["connection_recovery"] == {
        "state": "retrying",
        "retry_count": 0,
        "max_retries": 9,
    }


def test_t1_source_settings_and_bridge_diagnostics_carry_stream_max_retries(node):  # noqa: F811
    assert "stream_max_retries" in model_settings.FIELDS
    assert model_settings.defaults({"models": ["m"]})["stream_max_retries"] == 5
    assert model_settings.validate({"stream_max_retries": 0})["stream_max_retries"] == 0
    assert model_settings.validate({"stream_max_retries": 20})["stream_max_retries"] == 20
    for bad in (-1, 21, True, "5"):
        with pytest.raises(ValueError, match="model_stream_max_retries_invalid"):
            model_settings.validate({"stream_max_retries": bad})

    assert safe_diagnostics({"stream_max_retries": 7, "overloaded": True}) == {
        "stream_max_retries": 7,
        "overloaded": True,
    }
    assert safe_diagnostics({"stream_max_retries": 0}) == {"stream_max_retries": 0}
    assert safe_diagnostics({"stream_max_retries": 21}) == {}
    assert safe_diagnostics({"stream_max_retries": -1}) == {}
    assert safe_diagnostics({"stream_max_retries": True}) == {}

    app, store = node
    with _anthropic_status_server([(503, {"Content-Type": "application/json"}, b'{"error":{"message":"busy"}}')]) as (
        base_url,
        _,
    ):
        sources = app.state.llm_agent_services["source_store"]
        rec = sources.prepare(
            {
                "name": "Custom Retry Source",
                "kind": "local",
                "protocol": "anthropic_messages",
                "base_url": base_url,
                "models": ["claude-retry-test"],
            }
        )
        rec.update(status="ready", tool_support={"claude-retry-test": "supported"})
        sources.save(rec)
        sources.configure_model(
            rec["id"],
            {
                "model_id": "claude-retry-test",
                "expected_rule_revision": rec["model_settings"]["claude-retry-test"]["rule_revision"],
                "configuration": {"stream_max_retries": 7},
            },
        )
        model_id = "local/" + sources.model_service_id(rec, "claude-retry-test")

        bridge, _, _ = build_agent_integrations(app, store, store.home)
        with pytest.raises(AgentError) as caught:
            bridge.complete(
                model_id,
                [{"role": "user", "content": "ping"}],
                [],
                lambda: False,
            )
        assert str(caught.value) == "agent_model_upstream_failed"
        assert caught.value.diagnostics["stream_max_retries"] == 7
        assert retry_policy.limit(str(caught.value), caught.value.diagnostics) == 7


def test_t2_eight_consecutive_connection_failures_stay_queued_and_recover(tmp_path, fake_clock):
    runtime, model, _, _ = make_runtime(tmp_path)
    failing_model(
        model,
        *[_err("agent_model_connection_failed", network_reason="refused") for _ in range(8)],
        assistant("Recovered on attempt 9"),
    )
    created = runtime.create({"message": "Keep working across offline outage"})
    expected_waits = (5, 10, 20, 40, 60, 60, 60, 60)

    advance(runtime)
    for idx, expected_wait in enumerate(expected_waits, start=1):
        snap = runtime.get(created["id"])
        assert snap["status"] == "queued"
        assert snap["error"] is None
        rec = snap["connection_recovery"]
        assert rec["state"] == "retrying"
        assert rec["retry_count"] == idx
        assert rec["max_retries"] is None
        assert rec["next_retry_at"] == pytest.approx(fake_clock[0] + expected_wait)
        assert not any(ev["type"] == "connection_paused" for ev in snap["events"])
        fake_clock[0] += expected_wait
        advance(runtime)

    done = runtime.get(created["id"])
    assert done["status"] == "completed"
    assert done["result"] == "Recovered on attempt 9"
    assert len(model.calls) == 9
    assert done["connection_recovery"]["state"] == "recovered"
    assert done["connection_recovery"]["max_retries"] is None
    stored = runtime.store.read()["tasks"][created["id"]]
    assert stored["retry_wait_seconds"] == pytest.approx(sum(expected_waits))


def test_t3_hard_time_limit_pauses_while_default_soft_limit_allows_long_wait(tmp_path, fake_clock):
    # 1) Owner hard per-run max_seconds pauses when next retry wait >= remaining
    runtime, model, _, _ = make_runtime(tmp_path)
    runtime.update_settings({"max_seconds": 30})
    failing_model(
        model,
        *[_err("agent_model_connection_failed", network_reason="reset") for _ in range(5)],
    )
    hard_task = runtime.create({"message": "Bounded by owner max_seconds"})
    advance(runtime)  # fail 1 -> wait 5s (remaining 30s > 5s)
    assert runtime.get(hard_task["id"])["status"] == "queued"
    fake_clock[0] += 5
    advance(runtime)  # fail 2 -> wait 10s (remaining ~25s > 10s)
    assert runtime.get(hard_task["id"])["status"] == "queued"
    fake_clock[0] += 10
    advance(runtime)  # fail 3 -> wait 20s >= remaining (~15s) -> connection_paused!

    paused = runtime.get(hard_task["id"])
    assert paused["status"] == "paused"
    assert paused["error"] is None
    assert paused["connection_recovery"]["state"] == "disconnected"
    assert paused["connection_recovery"]["max_retries"] is None
    assert paused["events"][-1]["type"] == "connection_paused"
    stored_hard = runtime.store.read()["tasks"][hard_task["id"]]
    assert stored_hard["connection_recovery"]["reason"] == "retry_wait_exceeds_budget"

    # 2) Default soft max_seconds (900s default segment) does NOT prematurely pause a 1200s wait
    soft_runtime, soft_model, _, _ = make_runtime(tmp_path / "soft")
    failing_model(
        soft_model,
        _err("agent_model_connection_failed", network_reason="timeout", retry_after_seconds=1200),
        assistant("Resumed after 20 min outage"),
    )
    soft_task = soft_runtime.create({"message": "Default soft segment should grow/continue"})
    advance(soft_runtime)
    soft_snap = soft_runtime.get(soft_task["id"])
    assert soft_snap["status"] == "queued"
    assert soft_snap["connection_recovery"]["next_retry_at"] == pytest.approx(fake_clock[0] + 1200)

    # 3) Owner hard lifetime max_total_seconds also triggers connection_paused when wait >= remaining
    def set_hard_lifetime(data):
        budget.set_owner_limits(data["tasks"][soft_task["id"]], {"max_total_seconds": 60})

    soft_runtime.store.transaction(set_hard_lifetime)
    fake_clock[0] += 1200
    advance(soft_runtime)
    # Lifetime 60s was exhausted by the 1200s charged wait
    assert soft_runtime.get(soft_task["id"])["status"] == "failed"
    assert soft_runtime.get(soft_task["id"])["error"] == "agent_time_budget_exhausted"


def test_t4_user_stop_retrying_calls_control_stop_and_manual_resume_restores_none(tmp_path, fake_clock):
    runtime, model, _, _ = make_runtime(tmp_path)
    failing_model(
        model,
        _err("agent_model_connection_failed", network_reason="refused"),
        _err("agent_model_connection_failed", network_reason="refused"),
        assistant("Done after manual resume"),
    )
    row = runtime.create({"message": "Allow stopping infinite retry"})
    advance(runtime)
    fake_clock[0] += 5
    advance(runtime)
    retrying = runtime.get(row["id"])
    assert retrying["status"] == "queued"
    assert retrying["connection_recovery"]["retry_count"] == 2
    assert retrying["connection_recovery"]["max_retries"] is None

    # User clicks "停止重试" -> action("pause") -> control.py settle -> retry_policy.stop(row)
    fake_clock[0] += 3
    stopped = _act(runtime, row["id"], "pause")
    assert stopped["status"] == "paused"
    assert stopped["connection_recovery"]["state"] == "stopped"

    # Advancing past scheduled backoff must NOT fire another model call while stopped
    fake_clock[0] += 300
    advance(runtime, 4)
    assert len(model.calls) == 2
    saved = runtime.store.read()["tasks"][row["id"]]
    assert saved["retry_wait_seconds"] == pytest.approx(8)

    # User clicks "重试并继续" -> action("resume") -> retry_policy.resume writes max_retries=None
    resumed = _act(runtime, row["id"], "resume")
    assert resumed["status"] == "queued"
    assert resumed["connection_recovery"] == {
        "state": "retrying",
        "retry_count": 0,
        "max_retries": None,
    }
    advance(runtime)
    done = runtime.get(row["id"])
    assert done["status"] == "completed"
    assert done["result"] == "Done after manual resume"
    assert len(model.calls) == 3


def test_t5_http_529_overloaded_retries_and_respects_stream_max_retries(node, fake_clock):  # noqa: F811
    app, store = node
    overloaded_resp = (
        529,
        {"Content-Type": "application/json"},
        b'{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
    )
    ok_resp = (
        200,
        {"Content-Type": "application/json"},
        (
            b'{"id":"msg_1","type":"message","role":"assistant","model":"claude-529",'
            b'"content":[{"type":"text","text":"529 recovered"}],'
            b'"stop_reason":"end_turn","usage":{"input_tokens":10,"output_tokens":4}}'
        ),
    )

    with _anthropic_status_server([overloaded_resp, overloaded_resp, ok_resp]) as (base_url, requests):
        sources = app.state.llm_agent_services["source_store"]
        rec = sources.prepare(
            {
                "name": "Anthropic 529 Fixture",
                "kind": "local",
                "protocol": "anthropic_messages",
                "base_url": base_url,
                "models": ["claude-529"],
            }
        )
        rec.update(status="ready", tool_support={"claude-529": "supported"})
        sources.save(rec)
        sources.configure_model(
            rec["id"],
            {
                "model_id": "claude-529",
                "expected_rule_revision": rec["model_settings"]["claude-529"]["rule_revision"],
                "configuration": {"stream_max_retries": 2},
            },
        )
        model_id = "local/" + sources.model_service_id(rec, "claude-529")

        bridge, tools, catalog = build_agent_integrations(app, store, store.home)
        runtime = AgentRuntime(
            AgentStore(
                store.home / "s01-i3-529-agent",
                peer_box.load_or_create_messaging_key(store.home / "messaging.x25519"),
            ),
            bridge,
            tools,
            catalog,
        )
        runtime.update_settings({"enabled": True, "model_id": model_id})

        task = runtime.create({"message": "Test 529 retry"})
        runtime.run_once()
        first = runtime.get(task["id"])
        assert first["status"] == "queued"
        assert first["connection_recovery"]["retry_count"] == 1
        assert first["connection_recovery"]["max_retries"] == 2
        assert first["connection_recovery"]["diagnostics"]["http_status"] == 529

        fake_clock[0] = first["connection_recovery"]["next_retry_at"]
        runtime.run_once()
        second = runtime.get(task["id"])
        assert second["status"] == "queued"
        assert second["connection_recovery"]["retry_count"] == 2
        assert second["connection_recovery"]["max_retries"] == 2

        fake_clock[0] = second["connection_recovery"]["next_retry_at"]
        runtime.run_once()
        done = runtime.get(task["id"])
        assert done["status"] == "completed"
        assert done["result"] == "529 recovered"
        assert len(requests) == 3


def test_t6_first_retry_emits_only_internal_scheduled_event_with_waiting_for_network(tmp_path, fake_clock):
    runtime, model, _, _ = make_runtime(tmp_path)
    failing_model(
        model,
        _err("agent_model_connection_failed", network_reason="dns_temporary"),
        _err("agent_model_connection_failed", network_reason="dns_temporary"),
        assistant("Done"),
    )
    created = runtime.create({"message": "Verify first retry event contract"})
    advance(runtime)

    after_first = runtime.get(created["id"])
    assert after_first["status"] == "queued"
    assert after_first["connection_recovery"]["retry_count"] == 1
    assert after_first["connection_recovery"]["max_retries"] is None
    assert after_first["connection_recovery"].get("waiting_for_network") is True

    # Only internal model_retry_scheduled is emitted for the retry; no connection_paused or UI alert event
    retry_or_pause_events = [
        ev["type"]
        for ev in after_first["events"]
        if "retry" in ev["type"] or "connection" in ev["type"] or ev["type"] in {"paused", "failed"}
    ]
    assert retry_or_pause_events == ["model_retry_scheduled"]
    scheduled_ev = after_first["events"][-1]
    assert scheduled_ev["type"] == "model_retry_scheduled"
    assert scheduled_ev["retry_count"] == 1
    assert scheduled_ev["waiting_for_network"] is True


def test_rework_unknown_reason_connection_failure_is_finite_and_delay_requires_code(tmp_path, fake_clock):
    # Point 1: code is required on retry_policy.delay
    with pytest.raises(TypeError):
        retry_policy.delay(1, {})  # type: ignore[call-arg]

    # Point 2: unknown reason (no network_reason) -> finite limit & finite delay; network_reason="reset" -> None & 5s delay
    assert retry_policy.limit("agent_model_connection_failed", {}) == 5
    assert retry_policy.limit("agent_model_connection_failed", {"stream_max_retries": 2}) == 2
    assert retry_policy.delay(1, {}, "agent_model_connection_failed") == pytest.approx(2)
    assert retry_policy.limit("agent_model_connection_failed", {"network_reason": "reset"}) is None
    assert retry_policy.delay(1, {"network_reason": "reset"}, "agent_model_connection_failed") == pytest.approx(5)

    # Runtime integration: unknown-reason connection failure pauses after finite retries instead of looping forever
    runtime, model, _, _ = make_runtime(tmp_path)
    failing_model(
        model,
        *[_err("agent_model_connection_failed", stream_max_retries=2) for _ in range(3)],
    )
    unknown_task = runtime.create({"message": "Unknown connection error must not loop forever"})
    advance(runtime)
    for idx, wait in enumerate((2, 4), start=1):
        snap = runtime.get(unknown_task["id"])
        assert snap["status"] == "queued"
        assert snap["connection_recovery"]["max_retries"] == 2
        assert snap["connection_recovery"]["retry_count"] == idx
        assert snap["connection_recovery"]["next_retry_at"] == pytest.approx(fake_clock[0] + wait)
        fake_clock[0] += wait
        advance(runtime)
    paused = runtime.get(unknown_task["id"])
    assert paused["status"] == "paused"
    assert paused["connection_recovery"]["state"] == "disconnected"
    assert paused["connection_recovery"]["max_retries"] == 2

    # Whereas explicit network_reason="reset" remains unlimited (max_retries is None)
    reset_runtime, reset_model, _, _ = make_runtime(tmp_path / "reset")
    failing_model(reset_model, _err("agent_model_connection_failed", network_reason="reset"))
    reset_task = reset_runtime.create({"message": "Reset connection error stays unlimited"})
    advance(reset_runtime)
    reset_snap = reset_runtime.get(reset_task["id"])
    assert reset_snap["status"] == "queued"
    assert reset_snap["connection_recovery"]["max_retries"] is None
    assert reset_snap["connection_recovery"]["next_retry_at"] == pytest.approx(fake_clock[0] + 5)

