"""No-API tests for the explicit extra scenario preconditions and request shape."""
from __future__ import annotations

import copy

import pytest
from extra_scenarios import agent_off, manual_off, provider_history


class FakeControl:
    def __init__(self, role="consumer", enabled=False):
        self.calls = []
        self.evidence = {"identity": {"role": role, "peer_id": role, "network_id": "test-network"},
                         "tasks": [], "agent_settings": {"enabled": enabled}}
        self.model = {"id": "selected-model", "available": True, "agent_supported": True,
            "source": "peer" if role == "consumer" else "local_api", "model_id": "glm-5.3-flash",
            "peer_id": "provider", "service_id": "actual-service",
            "expected_model_binding": {"consumer_peer_id": "consumer", "model_binding": {"service_id": "actual-service"}}}

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        response = {}
        if path == "/api/local/two-host/evidence":
            response = copy.deepcopy(self.evidence)
        elif path == "/api/local/agent/models":
            response = {"models": [copy.deepcopy(self.model)]}
        elif path == "/api/local/agent/settings":
            self.evidence["agent_settings"].update(body)
            response = copy.deepcopy(self.evidence["agent_settings"])
        elif path == "/api/local/agent/tasks":
            response = {"id": "real-api-created-task", "status": "queued"}
        elif path == "/api/local/llm/orders":
            response = {"state": "succeeded", "task_id": body["task_id"]}
        return {"status_code": 200, "response": response}


def test_provider_history_is_actual_create_with_unique_private_marker():
    client = FakeControl("provider")
    result = provider_history(client, "selected-model")
    create = next(body for method, path, body in client.calls if method == "POST" and path.endswith("/tasks"))
    assert result["marker"] in create["message"]
    assert create["model_id"] == "selected-model"
    assert result["create"]["response"]["status"] == "queued"
    assert "wait for actual completed" in result["claim"]


def test_provider_history_rejects_consumer_without_mutating():
    client = FakeControl()
    with pytest.raises(ValueError, match="provider_role_required"):
        provider_history(client, "selected-model")
    assert all(method == "GET" for method, _, _ in client.calls)


def test_manual_off_retains_exact_catalog_binding_and_production_p2p():
    client = FakeControl()
    result = manual_off(client, "selected-model", 2048)
    order = next(body for method, path, body in client.calls if path == "/api/local/llm/orders")
    assert order["transport"] == "p2p"
    assert order["expected_model_binding"] == client.model["expected_model_binding"]
    assert result["agent_stayed_off"] and result["agent_task_ids_unchanged"]
    assert not any(path == "/api/local/agent/tasks" for _, path, _ in client.calls)


@pytest.mark.parametrize("failure", ["enabled", "binding", "active"])
def test_manual_preconditions_do_not_send_inference(failure):
    client = FakeControl(enabled=failure == "enabled")
    if failure == "binding":
        client.model["expected_model_binding"]["consumer_peer_id"] = "somebody-else"
    if failure == "active":
        client.evidence["tasks"] = [{"id": "busy", "status": "running"}]
    with pytest.raises(ValueError):
        manual_off(client, "selected-model", 2048)
    assert all(method == "GET" for method, _, _ in client.calls)


def test_agent_off_refuses_to_interrupt_active_tasks():
    client = FakeControl(enabled=True)
    client.evidence["tasks"] = [{"id": "busy", "status": "queued"}]
    with pytest.raises(ValueError, match="active_tasks_present"):
        agent_off(client)
    assert all(method == "GET" for method, _, _ in client.calls)
