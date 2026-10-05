"""Polling must not observe successful metadata before the private answer."""
import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.llm_package import routes
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.manifest import LLMPackageManifest, save_manifest
from rynmesh.llm_package.task_protocol import TaskOrderStore
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def test_async_answer_is_not_finished_until_deliverable(tmp_path, monkeypatch):
    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    app = FastAPI()
    routes.install_llm_routes(app, store=store, home=home,
        messaging_key=peer_box.load_or_create_messaging_key(home / "messaging.x25519"),
        resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    path = save_manifest(LLMPackageManifest(package_id="codex-cli", mode="codex_cli",
        adapter="codex_cli", runtime="external", public_model_alias="Codex CLI"),
        home / "llm/codex-cli/manifest.json")
    monkeypatch.setattr(routes, "connect_cli", lambda **_: {"manifest": str(path)})
    monkeypatch.setattr(CLIAgentAdapter, "health", lambda _: {"ok": True})
    monkeypatch.setattr(CLIAgentAdapter, "infer", lambda *_, **__: {
        "text": "private answer", "input_tokens": 4, "output_tokens": 3, "duration_ms": 1})
    committed, release = threading.Event(), threading.Event()
    original = TaskOrderStore.transition

    def pause_after_commit(self, **kwargs):
        result = original(self, **kwargs)
        if kwargs.get("state") == "succeeded":
            committed.set()
            assert release.wait(5), "test did not release the worker"
        return result

    monkeypatch.setattr(TaskOrderStore, "transition", pause_after_commit)
    with TestClient(app) as client:
        assert client.post("/api/local/llm/cli-services/codex_cli/setup").status_code == 200
        assert client.put("/api/local/llm/privacy", json={"result_retention_seconds": 0}).status_code == 200
        task = client.post("/api/local/llm/orders/async", json={
            "provider_peer_id": store.peer_id, "service_id": "codex-cli",
            "prompt": "synthetic test", "max_tokens": 32}).json()["task_id"]
        try:
            assert committed.wait(5)
            status = client.get("/api/local/llm/orders/" + task).json()
            assert status["state"] == "running", status
            assert "output" not in status
        finally:
            release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = client.get("/api/local/llm/orders/" + task).json()
            if status["state"] != "running":
                break
            time.sleep(0.01)
        assert status["state"] == "succeeded", status
        assert status["output"] == "private answer"
        consumed = client.get("/api/local/llm/orders/" + task).json()
        assert consumed["state"] == "failed"
        assert consumed["error_code"] == "response_no_longer_available"
        persisted = (home / "llm/consumer-orders" / (task + ".json")).read_text()
        assert "private answer" not in persisted
