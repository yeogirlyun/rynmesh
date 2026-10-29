"""Real runtime, fake clock: backoff never blocks workers or replays tool writes."""
import errno
import socket
import ssl
from urllib.error import HTTPError, URLError

import pytest
from test_agent_runtime import Catalog, advance, approve, assistant, make_runtime
from test_agent_runtime_retry import failing_model

from rynmesh.agent_runtime import retry_policy
from rynmesh.agent_runtime.diagnostic_log import safe_diagnostics
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.llm_package.transport_failure import describe


@pytest.fixture
def clock(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(retry_policy, "now", lambda: clock[0])
    monkeypatch.setattr(retry_policy.random, "uniform", lambda *_: 1)
    return clock


def fail(code="agent_model_connection_failed", **diagnostics):
    exc = AgentError(code)
    exc.diagnostics = diagnostics
    return exc


def act(runtime, task_id, action):
    return runtime.action(task_id, {"action": action, "expected_revision": runtime.get(task_id)["revision"]})


def test_backoff_persists_and_other_tasks_can_run(tmp_path, clock):
    runtime, model, tools, key = make_runtime(tmp_path)
    failing_model(model, fail(), assistant("Other task done"), assistant("Recovered"))
    row = runtime.create({"message": "Inspect"})
    advance(runtime)
    recovery = runtime.get(row["id"])["connection_recovery"]
    assert recovery["next_retry_at"] == 1002
    advance(runtime, 4)
    assert len(model.calls) == 1
    other = runtime.create({"message": "Other task"})
    advance(runtime)
    assert runtime.get(other["id"])["status"] == "completed"
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, Catalog())
    restarted.reconcile()
    advance(restarted)
    assert len(model.calls) == 2
    clock[0] = 1002
    advance(restarted)
    done = restarted.get(row["id"])
    assert done["status"] == "completed"
    assert done["connection_recovery"]["state"] == "recovered"
    assert restarted.store.read()["tasks"][row["id"]]["retry_wait_seconds"] == 2


def test_four_attempts_then_pause_and_manual_continue(tmp_path, clock):
    runtime, model, _, _ = make_runtime(tmp_path)
    failing_model(model, *[fail() for _ in range(4)], assistant("Done"))
    row = runtime.create({"message": "Inspect"})
    advance(runtime)
    for count, wait in enumerate((2, 5, 10), 1):
        recovery = runtime.get(row["id"])["connection_recovery"]
        assert recovery["retry_count"] == count
        assert recovery["next_retry_at"] == clock[0] + wait
        clock[0] += wait
        advance(runtime)
    done = runtime.get(row["id"])
    assert done["status"] == "paused" and done["error"] is None
    assert done["connection_recovery"]["retry_count"] == 3
    clock[0] += 100
    advance(runtime, 4)
    assert len(model.calls) == 4
    act(runtime, row["id"], "resume")
    with pytest.raises(AgentError):
        act(runtime, row["id"], "resume")
    advance(runtime)
    assert runtime.get(row["id"])["status"] == "completed"
    assert len(model.calls) == 5


def test_stop_survives_restart_and_late_failure(tmp_path, clock):
    runtime, model, tools, key = make_runtime(tmp_path)
    row = runtime.create({"message": "Inspect"})
    def stop_during_request():
        act(runtime, row["id"], "pause")
        raise fail()
    failing_model(model, fail(), stop_during_request, assistant("Done"))
    advance(runtime)
    clock[0] += 2
    advance(runtime)
    assert runtime.get(row["id"])["connection_recovery"]["state"] == "stopped"
    restarted = AgentRuntime(AgentStore(tmp_path, key), model, tools, Catalog())
    restarted.reconcile()
    clock[0] += 100
    advance(restarted, 3)
    assert len(model.calls) == 2
    act(restarted, row["id"], "resume")
    advance(restarted)
    assert restarted.get(row["id"])["status"] == "completed"


def test_stop_during_wait_prevents_scheduled_request(tmp_path, clock):
    runtime, model, _, _ = make_runtime(tmp_path)
    failing_model(model, fail())
    row = runtime.create({"message": "Inspect"})
    advance(runtime)
    clock[0] += 1
    act(runtime, row["id"], "pause")
    clock[0] += 100
    advance(runtime, 3)
    saved = runtime.store.read()["tasks"][row["id"]]
    assert saved["connection_recovery"]["state"] == "stopped"
    assert saved["retry_wait_seconds"] == 1
    assert len(model.calls) == 1


def test_duplicate_manual_resume_request_is_idempotent(tmp_path, clock):
    runtime, model, _, _ = make_runtime(tmp_path)
    failing_model(model, fail(), assistant("Done"))
    row = runtime.create({"message": "Inspect"})
    advance(runtime)
    stopped = act(runtime, row["id"], "pause")
    body = {"action": "resume", "expected_control_revision": stopped["control_revision"], "client_request_id": "9a5473c5-7b2e-4f05-a5ae-cfcd5a86afe8"}
    runtime.action(row["id"], body)
    runtime.action(row["id"], body)
    advance(runtime, 2)
    assert runtime.get(row["id"])["status"] == "completed"
    assert len(model.calls) == 2


def test_retry_wait_is_charged_to_lifetime_budget(tmp_path, clock):
    from rynmesh.agent_runtime import budget
    runtime, model, _, _ = make_runtime(tmp_path)
    failing_model(model, fail(), assistant("Done"))
    row = runtime.create({"message": "Inspect"})
    advance(runtime)
    before = runtime.store.read()["tasks"][row["id"]]
    remaining = budget.time_remaining(before)
    clock[0] += 2
    act(runtime, row["id"], "pause")
    after = runtime.store.read()["tasks"][row["id"]]
    assert budget.time_remaining(after) == remaining - 2
    assert budget.public(after)["lifetime"]["charged_seconds"] == budget.public(before)["lifetime"]["charged_seconds"] + 2


def test_completed_write_not_replayed_on_connection_resume(tmp_path, clock):
    runtime, model, tools, _ = make_runtime(tmp_path)
    failing_model(model, assistant(name="share_service", args={"service_id": "svc"}),
                  fail("agent_inference_timeout"), fail("agent_inference_timeout"), assistant("Shared"))
    row = runtime.create({"message": "Share"})
    advance(runtime, 2)
    approve(runtime, row["id"])
    advance(runtime, 2)
    clock[0] += 2
    advance(runtime)
    assert runtime.get(row["id"])["status"] == "paused"
    assert len(tools.calls) == 1
    act(runtime, row["id"], "resume")
    advance(runtime, 3)
    assert runtime.get(row["id"])["status"] == "completed"
    assert len(tools.calls) == 1


@pytest.mark.parametrize("diag", [{"network_reason": "quota"}, {"network_reason": "tls_certificate"}, {"network_reason": "dns_not_found"}, {"http_status": 401}, {"http_status": 403}, {"http_status": 404}])
def test_permanent_failure_pauses_without_automatic_retry(tmp_path, clock, diag):
    runtime, model, _, _ = make_runtime(tmp_path)
    failing_model(model, fail(**diag))
    row = runtime.create({"message": "Inspect"})
    advance(runtime, 3)
    done = runtime.get(row["id"])
    assert done["status"] == "paused" and done["error"] is None
    assert done["connection_recovery"]["max_retries"] == 0
    assert len(model.calls) == 1


@pytest.mark.parametrize("wait", [8, 500])
def test_retry_after_is_not_ignored_or_allowed_to_extend_budget(tmp_path, clock, wait):
    runtime, model, _, _ = make_runtime(tmp_path)
    runtime.update_settings({"max_seconds": 20})
    failing_model(model, fail("agent_model_upstream_failed", http_status=429, retry_after_seconds=wait))
    row = runtime.create({"message": "Inspect"})
    advance(runtime)
    result = runtime.get(row["id"])
    if wait == 8:
        assert result["connection_recovery"]["next_retry_at"] == 1008
    else:
        assert result["status"] == "paused"
    advance(runtime, 3)
    assert len(model.calls) == 1


@pytest.mark.parametrize("exc,reason", [
    (URLError(socket.gaierror(socket.EAI_AGAIN, "secret")), "dns_temporary"),
    (URLError(ssl.SSLCertVerificationError(1, "secret")), "tls_certificate"),
    (ConnectionResetError(errno.ECONNRESET, "secret"), "reset"),
    (ConnectionRefusedError(errno.ECONNREFUSED, "secret"), "refused"),
    (TimeoutError("secret"), "timeout"),
])
def test_safe_transport_classification(exc, reason):
    assert describe(exc)["network_reason"] == reason
    assert "secret" not in str(describe(exc))


def test_http_diagnostics_do_not_retain_url_or_body():
    exc = HTTPError("https://secret@host", 503, "secret", {"Retry-After": "7"}, None)
    assert describe(exc) == {"network_reason": "http", "network_phase": "request", "http_status": 503, "retry_after_seconds": 7}
    assert safe_diagnostics({"network_reason": "secret", "network_phase": "secret", "http_status": 900, "retry_after_seconds": float("inf"), "body": "secret"}) == {}


@pytest.mark.parametrize("protocol", ["chat_json", "chat_stream", "openai_responses", "anthropic_messages"])
def test_adapter_retains_quota_reason_without_provider_message(monkeypatch, protocol):
    import io
    import json

    from rynmesh.llm_package.adapters import AdapterError, OpenAICompatibleAdapter
    from rynmesh.llm_package.protocol_adapter import NativeProtocolAdapter
    def reject(*args, **kwargs):
        raise HTTPError("https://secret@host", 429, "secret", {"Retry-After": "60"},
                        io.BytesIO(json.dumps({"error": {"code": "insufficient_quota", "message": "secret"}}).encode()))
    module = "adapters" if protocol.startswith("chat_") else "protocol_adapter"
    monkeypatch.setattr(f"rynmesh.llm_package.{module}.model_urlopen", reject)
    adapter = (OpenAICompatibleAdapter(base_url="http://127.0.0.1:1", model="fixture") if module == "adapters" else
               NativeProtocolAdapter(protocol=protocol, base_url="http://127.0.0.1:1", model="fixture"))
    with pytest.raises(AdapterError) as caught:
        adapter.chat({"messages": [{"role": "user", "content": "test"}], "stream": protocol != "chat_json", "max_tokens": 8}, task_id="quota", timeout_s=1)
    assert caught.value.diagnostics["network_reason"] == "quota"
    assert caught.value.diagnostics["http_status"] == 429
    assert retry_policy.limit("agent_model_upstream_failed", caught.value.diagnostics) == 0
    assert "secret" not in str(vars(caught.value))
