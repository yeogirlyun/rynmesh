"""Owner routes with the real encrypted source vault and runtime store."""
import json
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_agent_integrations import add_source
from test_agent_integrations import node as integration_node

from rynmesh.agent_runtime.routes import install_agent
from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.services import peer_box

node = integration_node


@pytest.fixture
def owner(node):
    app, store = node
    _, model = add_source(app)
    def guard(request):
        if request.headers.get("x-fixture-owner") != "yes":
            raise HTTPException(401)
    runtime = install_agent(app, store=store, home=store.home, workers=BackgroundWorkerRegistry(),
        local_control=guard, messaging_key=peer_box.load_or_create_messaging_key(store.home / "messaging.x25519"))
    runtime.update_settings({"enabled": True, "model_id": model})
    with TestClient(app) as client:
        yield client, runtime, store, app.state.llm_agent_services["source_store"]


def test_real_vault_http_owner_binding_exact_retry_and_no_task_secret(owner):
    client, runtime, store, sources = owner
    task = runtime.create({"message": "Configure a model source"})
    task = runtime.action(task["id"], {"action": "pause"})
    body = {"client_request_id": str(uuid.uuid4()), "kind": "api", "base_url": "https://new-vendor.test/v1",
            "source_id": "", "api_key": "SYNTHETIC-CREDENTIAL-731", "expected_control_revision": task["control_revision"],
            "expected_goal_revision": task["goal"]["revision"]}
    url = f"/api/local/agent/tasks/{task['id']}/source-credential"
    assert client.post(url, json=body).status_code == 401
    assert not (store.home / "agent/source-drafts.enc").exists()
    headers = {"x-fixture-owner": "yes"}
    response = client.post(url, json=body, headers=headers)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["task"]["status"] == "paused"
    assert "SYNTHETIC-CREDENTIAL-731" not in response.text
    assert "SYNTHETIC-CREDENTIAL-731" not in json.dumps(runtime.store.read())
    ciphertext = (store.home / "agent/source-drafts.enc").read_bytes()
    assert b"SYNTHETIC-CREDENTIAL-731" not in ciphertext
    vault = json.loads(sources.cipher.decrypt(ciphertext))
    assert next(iter(vault["credentials"].values()))["api_key"] == "SYNTHETIC-CREDENTIAL-731"
    retried = client.post(url, json=body, headers=headers)
    assert retried.json()["credential"] == response.json()["credential"]
    changed = client.post(url, json={**body, "api_key": "OTHER-SYNTHETIC"}, headers=headers)
    assert changed.status_code == 409
    assert "OTHER-SYNTHETIC" not in changed.text
    assert len(sources.read()) == 1  # Saving a credential did not create an active source.


def test_http_skill_selection_preferences_and_budget_preserve_controls(owner):
    client, runtime, _, _ = owner
    headers = {"x-fixture-owner": "yes"}
    assert client.get("/api/local/agent/skills").status_code == 401
    assert client.put("/api/local/agent/preferences", json={"expected_revision": 0, "items": []}).status_code == 401
    catalog = client.get("/api/local/agent/skills", headers=headers).json()["skills"]
    assert any(row["id"] == "resource-report" for row in catalog)
    created = client.post("/api/local/agent/tasks", headers=headers, json={"message": "Summarize later", "skill_ids": ["resource-report"], "budget_limits": {"max_total_model_calls": 7}})
    assert created.status_code == 200
    task = created.json()
    assert task["skills"][0]["id"] == "resource-report"
    assert task["budget"]["limits"]["max_total_model_calls"] == 7
    preference = {"expected_revision": 0, "items": [{"id": "tone", "text": "Concise paragraphs"}]}
    assert client.put("/api/local/agent/preferences", headers=headers, json=preference).status_code == 200
    assert client.put("/api/local/agent/preferences", headers=headers, json=preference).status_code == 409
    task = runtime.action(task["id"], {"action": "pause"})
    body = {"expected_control_revision": task["control_revision"], "client_request_id": str(uuid.uuid4()), "limits": {"max_total_model_calls": 9}}
    path = f"/api/local/agent/tasks/{task['id']}/budget"
    changed = client.put(path, headers=headers, json=body)
    assert changed.status_code == 200
    assert changed.json()["status"] == "paused" and changed.json()["steps"] == 0
    assert changed.json()["budget"]["limits"]["max_total_model_calls"] == 9
    assert client.put(path, headers=headers, json=body).json()["control_revision"] == changed.json()["control_revision"]
    assert client.put(path, headers=headers, json={**body, "limits": {"max_total_model_calls": 10}}).status_code == 409
