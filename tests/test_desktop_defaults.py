import os

from rynmesh import peer_http

_DESKTOP_KEYS = (
    "RYNMESH_HOME",
    "RYNMESH_NODE_NAME",
    "RYNMESH_MACHINE_NAME",
    "RYNMESH_MACHINE_IP",
    "RYNMESH_NETWORK_ID",
    "RYNMESH_PEER_HOST",
    "RYNMESH_PEER_PORT",
    "RYNMESH_PEER_PUBLIC_HOST",
    "RYNMESH_PEER_ENDPOINT",
    "RYNMESH_AUTO_REGISTER",
    "RYNMESH_REGISTRY_URL",
    "RYNMESH_RELAY_URL",
)


def _restore(values):
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_desktop_defaults_form_a_complete_zero_config_node(monkeypatch, tmp_path):
    original = {key: os.environ.get(key) for key in _DESKTOP_KEYS}
    for key in _DESKTOP_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RYNMESH_DESKTOP_MODE", "1")
    monkeypatch.setattr(peer_http, "_desktop_lan_ip", lambda: "192.0.2.44")
    monkeypatch.setattr(peer_http, "_desktop_node_name", lambda: "Ryn Laptop")
    monkeypatch.setattr(peer_http.Path, "home", lambda: tmp_path)

    try:
        peer_http.apply_desktop_defaults()

        assert peer_http.os.environ["RYNMESH_HOME"] == str(tmp_path / ".rynmesh")
        assert peer_http.os.environ["RYNMESH_NODE_NAME"] == "Ryn Laptop"
        assert peer_http.os.environ["RYNMESH_NETWORK_ID"] == "rynmesh-main"
        assert peer_http.os.environ["RYNMESH_PEER_HOST"] == "0.0.0.0"
        assert peer_http.os.environ["RYNMESH_PEER_ENDPOINT"] == "http://192.0.2.44:8791"
        assert peer_http.os.environ["RYNMESH_AUTO_REGISTER"] == "1"
        assert peer_http.os.environ["RYNMESH_REGISTRY_URL"] == "https://registry.rynmesh.ai"
    finally:
        _restore(original)


def test_desktop_defaults_preserve_operator_overrides(monkeypatch):
    original = {key: os.environ.get(key) for key in _DESKTOP_KEYS}
    monkeypatch.setenv("RYNMESH_DESKTOP_MODE", "1")
    monkeypatch.setenv("RYNMESH_NETWORK_ID", "private-mesh")
    monkeypatch.setenv("RYNMESH_REGISTRY_URL", "https://registry.example.test")
    try:
        peer_http.apply_desktop_defaults()
        assert peer_http.os.environ["RYNMESH_NETWORK_ID"] == "private-mesh"
        assert peer_http.os.environ["RYNMESH_REGISTRY_URL"] == "https://registry.example.test"
    finally:
        _restore(original)
