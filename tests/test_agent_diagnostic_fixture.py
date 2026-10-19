"""Q04 setup uses real failed I/O, not an invented successful task record."""
import hashlib
import importlib.util
import json
from pathlib import Path

from test_agent_integrations import add_source
from test_agent_integrations import node as integration_node
from test_agent_webdav_fixture import FixtureDAV

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore, encoded
from rynmesh.nas import NasPlugin
from rynmesh.services import peer_box

node = integration_node
path = Path(__file__).parents[1] / "docs/acceptance/ryn-agent-delivery/real-model/diagnostic_fixture.py"
spec = importlib.util.spec_from_file_location("diagnostic_fixture", path)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def test_q04_actual_failed_read_and_immutable_safe_diagnostics(node, tmp_path):
    app, store = node
    root = tmp_path / "dav"
    root.mkdir()
    (root / ".ryn-acceptance-isolated").touch()
    (root / "failure.md").write_text("PRIVATE_RESOURCE_CANARY_491", encoding="utf-8")
    _, selected = add_source(app)
    nas = app.state.nas_plugin = NasPlugin(store.home)
    nas.enable(True)
    with FixtureDAV(root) as dav:
        connection = nas.configure({"name": "docs-offline", "system": "generic", "protocol": "webdav",
            "url": dav.url, "username": "fixture", "password": "fixture", "allow_ai": True, "writable": False})
        bridge, tools, catalog = build_agent_integrations(app, store, store.home)
        runtime = AgentRuntime(AgentStore(store.home / "agent", peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")),
                               bridge, tools, catalog, consumer_node_id=store.peer_id)
        app.state.agent_runtime = runtime
        runtime.update_settings({"enabled": True, "model_id": selected})
        evidence = fixture.prepare_failed_task(runtime, catalog, nas, connection, dav, selected, canary="PRIVATE_TASK_CANARY_712")
        assert evidence["status"] == "failed" and evidence["write_dispatched"] is False
        assert runtime.model is bridge
        context = {"task_id": "diagnosis-only", "owner_id": runtime.owner_id, "consumer_node_id": runtime.consumer_node_id}
        projection = tools.execute("ryn_diagnose_task", {"task_id": fixture.TARGET_ID}, operation_id="diagnose-only", context=context, cancelled=lambda: False)
        assert projection["task_id"] == fixture.TARGET_ID and projection["task_id"] != context["task_id"]
        assert projection["error"] == "resource_read_timeout" and projection["write_dispatched"] is False
        assert any(op.get("connection_name") == "docs-offline" for op in projection["operations"])
        assert "PRIVATE_TASK_CANARY" not in json.dumps(projection) and "PRIVATE_RESOURCE_CANARY" not in json.dumps(projection)
        assert hashlib.sha256(encoded(runtime.store.read()["tasks"][fixture.TARGET_ID])).hexdigest() == evidence["task_snapshot_sha256"]
        assert not any(entry["mutation"] for entry in dav.audit)
        for source_id in list(nas.sessions):
            nas._close_session(source_id)
