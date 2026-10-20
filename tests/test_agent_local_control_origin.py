"""Only the executing node's declared port can become local API context."""
import json

from test_agent_runtime import make_runtime

from rynmesh.agent_runtime.prompt import authority


def test_local_api_context_uses_validated_host_port(tmp_path, monkeypatch):
    runtime, _, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "Inspect local configuration"})
    row = runtime.store.read()["tasks"][task["id"]]

    def context():
        return json.JSONDecoder().raw_decode(authority(row).split("Server control state: ", 1)[1])[0]

    monkeypatch.delenv("RYNMESH_PEER_PORT", raising=False)
    assert "local_control_api" not in context()
    for port in ("0", "65536", "-1", "8791/remote", "https://example.test", "9" * 5000):
        monkeypatch.setenv("RYNMESH_PEER_PORT", port)
        assert "local_control_api" not in context()
    for port in ("8791", "18877"):
        monkeypatch.setenv("RYNMESH_PEER_PORT", port)
        assert context()["local_control_api"] == {
            "origin": "http://127.0.0.1:" + port, "schema_path": "/openapi.json",
        }
