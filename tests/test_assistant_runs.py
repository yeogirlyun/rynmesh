from __future__ import annotations

import json
import threading
import time

from fastapi.testclient import TestClient

from rynmesh.peer_http import create_app
from rynmesh.services.assistant_context import AssistantContextStore
from rynmesh.services.assistant_runs import AssistantRunService
from rynmesh.store import RynmeshStore


class Provider:
    id = "fixture"
    model = "fixture-model"

    def __init__(self, output="A grounded answer."):
        self.output = output
        self.prompts = []

    def generate(self, prompt, **_kwargs):
        self.prompts.append(prompt)
        return self.output


def _service(tmp_path, provider, *, searches=None):
    contexts = AssistantContextStore(tmp_path / "contexts")
    target = {
        "key": "local:fixture:fixture-model", "label": "This device",
        "privacy_boundary": "device", "online": True,
        "route_modes": ["local_chat", "local_search", "local_context"],
    }
    service = AssistantRunService(
        tmp_path / "runs", contexts=contexts, targets=lambda: [target],
        provider=lambda _key: provider, search_contexts=lambda _query: searches or [],
    )
    return service, contexts


def _request(**changes):
    value = {
        "version": "ryn.assistant-run.v1",
        "conversation_id": "conversation-a",
        "idempotency_key": "send-1",
        "target_key": "local:fixture:fixture-model",
        "privacy_boundary": "device",
        "mode": "local_chat",
        "messages": [{"role": "user", "content": "PRIVATE_PROMPT", "status": "complete"}],
        "contexts": [],
        "max_output_tokens": 128,
    }
    value.update(changes)
    return value


def _terminal(service, run_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = service.get(run_id)
        if result["state"] in {"completed", "failed", "cancelled"}:
            return result
        time.sleep(0.01)
    raise AssertionError("run did not finish")


def test_run_is_idempotent_persistent_and_metadata_has_no_plaintext(tmp_path):
    provider = Provider()
    service, _contexts = _service(tmp_path, provider)
    queued = service.create(_request())
    duplicate = service.create(_request())
    assert duplicate["run_id"] == queued["run_id"]
    completed = _terminal(service, queued["run_id"])
    assert completed["state"] == "completed"
    assert completed["output"] == "A grounded answer."
    metadata = (tmp_path / "runs" / f"{queued['run_id']}.json").read_text(encoding="utf-8")
    assert "PRIVATE_PROMPT" not in metadata
    assert "A grounded answer" not in metadata
    assert "output_sha256" in metadata


def test_grounded_run_uses_bounded_context_and_valid_citation(tmp_path):
    provider = Provider()
    service, contexts = _service(tmp_path, provider)
    prepared = contexts.prepare({
        "kind": "text", "title": "local.txt", "text": "Trusted fact from local material.",
        "conversation_id": "conversation-a", "source_ref": "item_123",
    })
    queued = service.create(_request(
        idempotency_key="grounded", mode="local_context",
        contexts=[{"local_ref": prepared["local_ref"]}],
    ))
    completed = _terminal(service, queued["run_id"])
    assert completed["context_budget"]["included_bytes"] > 0
    assert completed["citations"][0]["source_ref"] == "item_123"
    assert completed["citations"][0]["locator"]["end"] <= len("Trusted fact from local material.")
    assert "untrusted_text" in provider.prompts[0]


def test_cancelled_late_provider_result_never_completes(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class SlowProvider(Provider):
        def generate(self, prompt, **kwargs):
            entered.set()
            release.wait(timeout=2)
            return super().generate(prompt, **kwargs)

    service, _contexts = _service(tmp_path, SlowProvider())
    queued = service.create(_request(idempotency_key="cancel"))
    assert entered.wait(timeout=1)
    cancelling = service.cancel(queued["run_id"])
    assert cancelling["state"] in {"cancelling", "cancelled"}
    release.set()
    final = _terminal(service, queued["run_id"])
    assert final["state"] == "cancelled"
    assert "output" not in final


def test_restart_marks_active_run_retryable_without_prompt(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    run_id = "run_1234abcd"
    (runs / f"{run_id}.json").write_text(json.dumps({
        "version": "ryn.assistant-run.v1", "run_id": run_id,
        "conversation_id": "owner", "idempotency_key": "secret-key",
        "state": "running", "updated_at": "old",
    }), encoding="utf-8")
    service, _contexts = _service(tmp_path, Provider())
    recovered = service.get(run_id)
    assert recovered["state"] == "failed"
    assert recovered["error_code"] == "assistant_interrupted"
    assert recovered["retryable"] is True
    assert "idempotency_key" not in recovered


def test_privacy_boundary_mismatch_is_rejected(tmp_path):
    service, _contexts = _service(tmp_path, Provider())
    try:
        service.create(_request(privacy_boundary="friend"))
    except ValueError as exc:
        assert getattr(exc, "code", "") == "assistant_permission_required"
    else:
        raise AssertionError("privacy boundary mismatch must fail")


def test_peer_http_assistant_facade_runs_only_through_local_node(tmp_path):
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    app = create_app(store)
    app.state.model_provider = Provider("Answer from the local node facade.")
    app.state.model_provider_checked_at = time.time()
    with TestClient(app) as client:
        targets = client.get("/api/local/assistant/targets").json()["targets"]
        assert targets[0]["privacy_boundary"] == "device"
        request = _request(target_key=targets[0]["key"], idempotency_key="facade")
        queued = client.post("/api/local/assistant/runs", json=request)
        assert queued.status_code == 200
        run_id = queued.json()["run_id"]
        deadline = time.monotonic() + 3
        result = {}
        while time.monotonic() < deadline:
            result = client.get(f"/api/local/assistant/runs/{run_id}").json()
            if result.get("state") == "completed":
                break
            time.sleep(0.01)
        assert result["output"] == "Answer from the local node facade."
        assert "PRIVATE_PROMPT" not in (
            store.home / "assistant" / "runs" / f"{run_id}.json"
        ).read_text(encoding="utf-8")


def test_peer_http_assistant_can_use_explicit_friend_boundary(tmp_path):
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    app = create_app(store)
    captured = {}
    app.state.llm_discover_services = lambda _network: [{
        "peer_id": "peer:friend", "node_name": "Alice", "online": True,
        "service": {
            "package_id": "friend-model", "model_alias": "alice-private",
            "pricing": {"currency": "DEV_TASK_BALANCE"},
        },
    }]

    async def execute(body):
        captured.update(body)
        return {"state": "succeeded", "output": "Friend-boundary answer"}

    app.state.llm_execute_order = execute
    with TestClient(app) as client:
        targets = client.get("/api/local/assistant/targets").json()["targets"]
        friend = next(item for item in targets if item["privacy_boundary"] == "friend")
        queued = client.post("/api/local/assistant/runs", json=_request(
            target_key=friend["key"], privacy_boundary="friend", mode="remote_provider",
            idempotency_key="friend-run",
        )).json()
        deadline = time.monotonic() + 3
        result = {}
        while time.monotonic() < deadline:
            result = client.get(f"/api/local/assistant/runs/{queued['run_id']}").json()
            if result.get("state") == "completed":
                break
            time.sleep(0.01)
        assert result["output"] == "Friend-boundary answer"
        assert captured["provider_peer_id"] == "peer:friend"
        assert captured["service_id"] == "friend-model"
        assert captured["transport"] == "auto"


def test_assistant_privacy_export_excludes_content_and_erase_removes_records(tmp_path):
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    app = create_app(store)
    app.state.model_provider = Provider("PRIVATE_OUTPUT")
    app.state.model_provider_checked_at = time.time()
    with TestClient(app) as client:
        context = client.post("/api/local/assistant/context/prepare", json={
            "kind": "text",
            "title": "private.txt",
            "text": "PRIVATE_CONTEXT",
            "conversation_id": "conversation-a",
        }).json()
        target = client.get("/api/local/assistant/targets").json()["targets"][0]
        queued = client.post("/api/local/assistant/runs", json=_request(
            target_key=target["key"],
            idempotency_key="privacy-export",
            contexts=[{"local_ref": context["local_ref"]}],
        )).json()
        _terminal(app.state.assistant_runs, queued["run_id"])

        status = client.get("/api/local/privacy/status").json()
        assert status["assistant_run_records"] == 1
        assert status["assistant_context_records"] == 1
        exported = client.get("/api/local/privacy/export").json()
        serialized = json.dumps(exported)
        assert "PRIVATE_PROMPT" not in serialized
        assert "PRIVATE_CONTEXT" not in serialized
        assert "PRIVATE_OUTPUT" not in serialized
        assert exported["assistant_workspace"]["note"].startswith("Prompt")

        erased = client.post("/api/local/privacy/erase", json={"scopes": ["assistant"]})
        assert erased.status_code == 200
        after = client.get("/api/local/privacy/status").json()
        assert after["assistant_run_records"] == 0
        assert after["assistant_context_records"] == 0
