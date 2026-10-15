"""Run two isolated real Drop nodes for UI acceptance. Never uses the user's node home.

Pairing membership is a test seed, not pairing acceptance. File/message routes,
encryption, streaming and persistence are the production implementations.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18876)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="rynmesh-drop-acceptance-"))
    repo = Path(__file__).resolve().parents[1]
    os.environ.update(RYNMESH_HOME=str(root / "alpha"), RYNMESH_AUTO_REGISTER="0",
        RYNMESH_DISABLE_DISCOVERY="1", RYNMESH_MODEL_PROVIDER="none", RYNMESH_REGISTRY_URL="",
        RYNMESH_REGISTRY_DIR=str(root / "registry"), RYNMESH_RELAY_URL="", RYNMESH_LLM_RELAY_URL="",
        RYNMESH_LLM_SERVICE_MANIFEST="", RYNMESH_FRIEND_ALLOW_LOOPBACK="1",
        RYNMESH_WEBUI_DIR=str(repo / "webapp" / "dist"))
    import uvicorn
    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore
    from rynmesh.settings_store import SettingsStore

    stores, apps, servers, threads = [], [], [], []
    for i, name in enumerate(["工作电脑", "家里的 NAS"]):
        home = root / ("alpha" if i == 0 else "beta")
        os.environ["RYNMESH_HOME"] = str(home)
        os.environ["RYNMESH_FRIEND_ENDPOINT"] = f"http://127.0.0.1:{args.port + i}"
        store = RynmeshStore(home=home, network_dir=root / "network", node_name=name)
        SettingsStore(home / "settings.json").patch({"onboarding_version": 1})
        store.register_node(endpoints=[f"http://127.0.0.1:{args.port + i}"])
        app = create_app(store)
        app.state.first_run.store.dismiss()
        app.state.drop_engine.db.set_setting("receive_dir", str(home / "received"))
        stores.append(store)
        apps.append(app)
    for i, app in enumerate(apps):
        stores[i].discover_peers(network_id="rynmesh-main", cache_only=False)
        other = stores[1 - i]
        app.state.drop_engine._own_peers = lambda other=other: {other.peer_id: other.node_name}
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port + i, log_level="warning", lifespan="off"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        servers.append(server)
        threads.append(thread)
    for server in servers:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started
    for app in apps:
        app.state.drop_engine.start()
    details = {"home": str(root), "url": f"http://127.0.0.1:{args.port}/devices",
               "receiver_url": f"http://127.0.0.1:{args.port + 1}", "peers": [store.peer_id for store in stores],
               "scope": "Two loopback nodes; seeded own-device trust; real HTTP data and messages"}
    (root / "preview.json").write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(details, ensure_ascii=True), flush=True)
    try:
        while all(thread.is_alive() for thread in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for app in apps:
            app.state.drop_engine.stop()
        for server in servers:
            server.should_exit = True
        for thread in threads:
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
