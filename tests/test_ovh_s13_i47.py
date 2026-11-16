"""Acceptance tests for S13-I47: /api/local/desktop/open-roots read-only endpoint and live refresh on settings updates."""
from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from rynmesh.agent_runtime.routes import install_agent
from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.drop.engine import DropEngine
from rynmesh.drop.routes import install_drop
from rynmesh.store import RynmeshStore


def test_s13_i47_open_roots_endpoint_enforces_local_control_and_refreshes_on_settings_change(tmp_path: Path):
    """Step 2: /api/local/desktop/open-roots requires local_control and returns drop receive_dir, Agent workspace, and Agent output dir, updating when settings change."""
    from test_agent_runtime import Catalog, Model, Tools

    home = tmp_path / "home"
    initial_drop_dir = tmp_path / "drop_initial"
    initial_drop_dir.mkdir()
    updated_drop_dir = tmp_path / "drop_updated"
    updated_drop_dir.mkdir()
    ws_first = tmp_path / "workspace_first"
    ws_first.mkdir()
    ws_second = tmp_path / "workspace_second"
    ws_second.mkdir()

    def local_control(request: Request) -> None:
        if request.headers.get("x-local-ok") != "1":
            raise HTTPException(status_code=403, detail="local_control_required")

    app = FastAPI()
    node_store = RynmeshStore(home=home, network_dir=home / "network", node_name="test-node")
    msg_key = X25519PrivateKey.generate()
    drop_engine = DropEngine(
        node=node_store,
        home=home,
        messaging_key=msg_key,
        network_id=lambda: "local",
        own_peers=dict,
        friend_peers=dict,
        peer_endpoints=lambda _pid: [],
    )
    drop_engine.db.set_setting("receive_dir", str(initial_drop_dir))
    install_drop(app, engine=drop_engine, local_control=local_control)
    runtime = install_agent(
        app,
        store=node_store,
        home=home,
        workers=BackgroundWorkerRegistry(),
        local_control=local_control,
        messaging_key=msg_key,
        integrations=(Model(), Tools(), Catalog()),
    )

    try:
        headers = {"x-local-ok": "1"}
        with TestClient(app) as client:
            assert client.get("/api/local/desktop/open-roots").status_code == 403

            r1 = client.get("/api/local/desktop/open-roots", headers=headers)
            assert r1.status_code == 200, r1.text
            roots_1 = {Path(p).resolve() for p in r1.json()["roots"]}
            output_dir = Path(runtime.store.default_workspace()).resolve()
            assert initial_drop_dir.resolve() in roots_1
            assert output_dir in roots_1

            # Update Agent workspace_root and Drop receive_dir -> open-roots reflects new directories and removes replaced workspace.
            assert client.put(
                "/api/local/agent/settings",
                headers=headers,
                json={"enabled": True, "model_id": "local:test", "workspace_root": str(ws_first)},
            ).status_code == 200
            assert client.put(
                "/api/local/drop/prefs",
                headers=headers,
                json={"receive_dir": str(updated_drop_dir)},
            ).status_code == 200

            r2 = client.get("/api/local/desktop/open-roots", headers=headers)
            assert r2.status_code == 200
            roots_2 = {Path(p).resolve() for p in r2.json()["roots"]}
            assert updated_drop_dir.resolve() in roots_2
            assert ws_first.resolve() in roots_2
            assert output_dir in roots_2
            assert initial_drop_dir.resolve() not in roots_2

            # Change workspace_root to ws_second -> ws_first is replaced by ws_second.
            assert client.put(
                "/api/local/agent/settings",
                headers=headers,
                json={"workspace_root": str(ws_second)},
            ).status_code == 200
            r3 = client.get("/api/local/desktop/open-roots", headers=headers)
            assert r3.status_code == 200
            roots_3 = {Path(p).resolve() for p in r3.json()["roots"]}
            assert ws_second.resolve() in roots_3
            assert ws_first.resolve() not in roots_3
    finally:
        drop_engine.stop()


def test_s13_i47_inbound_custom_save_dir_included_in_open_roots(tmp_path: Path):
    """Rework: completed inbound transfers saved to a user-chosen save_dir are deduplicated into /api/local/desktop/open-roots."""
    from test_agent_runtime import Catalog, Model, Tools

    home = tmp_path / "home"
    default_recv = tmp_path / "default_recv"
    default_recv.mkdir()
    custom_save = tmp_path / "custom_save_dir"
    custom_save.mkdir()
    outbound_only = tmp_path / "outbound_dir"
    outbound_only.mkdir()

    app = FastAPI()
    node_store = RynmeshStore(home=home, network_dir=home / "network", node_name="test-node")
    msg_key = X25519PrivateKey.generate()
    drop_engine = DropEngine(
        node=node_store,
        home=home,
        messaging_key=msg_key,
        network_id=lambda: "local",
        own_peers=dict,
        friend_peers=dict,
        peer_endpoints=lambda _pid: [],
    )
    drop_engine.db.set_setting("receive_dir", str(default_recv))
    drop_engine.db.insert_transfer(
        {
            "transfer_id": "tr_in_1",
            "offer_id": "off_1",
            "peer_id": "peer_a",
            "direction": "in",
            "kind": "file",
            "name": "report.pdf",
            "file_count": 1,
            "total_bytes": 128,
            "status": "completed",
            "save_dir": str(custom_save),
            "final_path": str(custom_save / "report.pdf"),
        },
        [{"file_id": "f1", "idx": 0, "rel_path": "report.pdf", "size": 128, "committed": 128, "status": "completed"}],
    )
    drop_engine.db.insert_transfer(
        {
            "transfer_id": "tr_in_2_dup",
            "offer_id": "off_2",
            "peer_id": "peer_a",
            "direction": "in",
            "kind": "file",
            "name": "notes.txt",
            "file_count": 1,
            "total_bytes": 64,
            "status": "completed",
            "save_dir": str(custom_save),
            "final_path": str(custom_save / "notes.txt"),
        },
        [{"file_id": "f2", "idx": 0, "rel_path": "notes.txt", "size": 64, "committed": 64, "status": "completed"}],
    )
    drop_engine.db.insert_transfer(
        {
            "transfer_id": "tr_out_1",
            "offer_id": "off_3",
            "peer_id": "peer_a",
            "direction": "out",
            "kind": "file",
            "name": "sent.pdf",
            "file_count": 1,
            "total_bytes": 64,
            "status": "completed",
            "save_dir": str(outbound_only),
        },
        [],
    )
    install_drop(app, engine=drop_engine, local_control=lambda _req: None)
    install_agent(
        app,
        store=node_store,
        home=home,
        workers=BackgroundWorkerRegistry(),
        local_control=lambda _req: None,
        messaging_key=msg_key,
        integrations=(Model(), Tools(), Catalog()),
    )

    try:
        with TestClient(app) as client:
            resp = client.get("/api/local/desktop/open-roots")
            assert resp.status_code == 200
            raw_roots = resp.json()["roots"]
            resolved = [Path(p).resolve() for p in raw_roots]
            assert custom_save.resolve() in resolved
            assert resolved.count(custom_save.resolve()) == 1
            assert outbound_only.resolve() not in resolved
    finally:
        drop_engine.stop()
