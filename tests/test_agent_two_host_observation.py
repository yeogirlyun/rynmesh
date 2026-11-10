"""Real local encrypted stores, signed snapshots; no model or remote calls."""
import copy
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from rynmesh.agent_runtime.store import AgentStore
from rynmesh.crypto import SignatureError
from rynmesh.llm_package.sources import SourceStore
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore

PATH = Path(__file__).parents[1] / "docs/acceptance/ryn-agent-delivery/two-host/release_observation.py"
SPEC = importlib.util.spec_from_file_location("two_host_release_observation", PATH)
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)


@pytest.fixture
def live_node(tmp_path):
    node = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    sources = SourceStore(node.home, node.private_key_bytes)
    row = sources.prepare({"name": "test source", "kind": "api", "base_url": "https://example.invalid",
                           "api_key": "PRIVATE_TEST_CREDENTIAL_NEVER_EXPORT", "models": ["glm-5.3-flash"]})
    row["status"] = "ready"
    sources.save(row)
    history = AgentStore(node.home / "agent", peer_box.load_or_create_messaging_key(node.home / "messaging.x25519"))
    app = FastAPI()
    app.state.llm_agent_services = {"source_store": sources}
    app.state.agent_runtime = SimpleNamespace(store=history)
    def local_control(request):
        if request.headers.get("authorization") != "Bearer synthetic-control":
            raise HTTPException(status_code=403)
    app.state.friends = SimpleNamespace(local_control=local_control)
    def code():
        return {"python_files": {"example.py": "1" * 64}, "sha256": "2" * 64, "launcher_sha256": "3" * 64}
    return app, node, sources, code


def challenge():
    return {"challenge": "a" * 32, "task_id": "original-task", "request": {"kind": "model", "id": "original-request"},
            "run_nonce": "b" * 32, "manifest_sha256": "c" * 64}


def test_actual_source_change_and_secret_exclusion(live_node):
    app, node, sources, code = live_node
    before = sources.path.read_bytes()
    wire = observer.capture_observation(app, node, challenge(), code)
    body = observer.verify_observation(wire, challenge(), node.peer_id)
    source = body["state"]["source_store"]["records"][0]
    assert source["id"] in sources.read() and source["models"][0]["ready"]
    assert source["models"][0]["model_binding"]["service_id"] == sources.model_service_id(sources.read()[source["id"]], "glm-5.3-flash")
    assert body["state"]["code"]["python_files"] == {"rynmesh/example.py": "1" * 64}
    assert "PRIVATE_TEST_CREDENTIAL" not in json.dumps(wire)
    assert "example.invalid" not in json.dumps(wire)
    assert sources.path.read_bytes() == before
    sources.delete(source["id"])
    current = observer.capture_observation(app, node, challenge(), code)
    assert observer.verify_observation(current, challenge(), node.peer_id)["state"]["source_store"]["records"] == []


def test_signature_identity_dispatch_binding_and_freshness(live_node):
    app, node, _sources, code = live_node
    wire = observer.capture_observation(app, node, challenge(), code)
    edited = copy.deepcopy(wire)
    edited["payload"]["state"]["source_store"]["records"] = []
    with pytest.raises(SignatureError):
        observer.verify_observation(edited, challenge(), node.peer_id)
    with pytest.raises(ValueError, match="envelope_invalid"):
        observer.verify_observation(wire, challenge(), "untrusted-peer")
    for key, new in (("challenge", "d" * 32), ("run_nonce", "e" * 32), ("manifest_sha256", "f" * 64),
                     ("task_id", "another-task"), ("request", {"kind": "tool", "id": "another-operation"})):
        expected = {**challenge(), key: new}
        with pytest.raises(ValueError, match="binding_mismatch"):
            observer.verify_observation(wire, expected, node.peer_id)
    later = datetime.now(timezone.utc) + timedelta(seconds=60)
    with pytest.raises(ValueError, match="not_fresh"):
        observer.verify_observation(wire, challenge(), node.peer_id, now=later)


def test_authenticated_route_returns_fresh_signed_observation(live_node):
    app, node, _sources, code = live_node
    observer.install_observation_route(app, node, code)
    with TestClient(app) as client:
        assert client.post("/api/local/two-host/observation", json=challenge()).status_code == 403
        headers = {"authorization": "Bearer synthetic-control"}
        assert client.post("/api/local/two-host/observation", headers=headers, json={}).status_code == 400
        response = client.post("/api/local/two-host/observation", headers=headers, json=challenge())
        assert response.status_code == 200
        assert observer.verify_observation(response.json(), challenge(), node.peer_id)["state"]["node"]["pid"] > 0
