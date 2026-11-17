"""Framework-only host aliases belong to tests, never production trust sets."""
import pytest


@pytest.fixture(autouse=True)
def local_test_transport_hosts(monkeypatch):
    from rynmesh import node_auth
    monkeypatch.setattr(node_auth, "_LOOPBACK", node_auth._LOOPBACK | {"testclient"})
    monkeypatch.setattr(node_auth, "_LOCAL_HOST_NAMES", node_auth._LOCAL_HOST_NAMES | {"testserver"})
