"""Default-deny transport guard for existing model tests with mocked HTTP."""
import socket

import pytest
import test_agent_extensions_oauth as network_guard


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setenv("RYNMESH_P2P_STUN", "off")
    numeric_dns = socket.getaddrinfo
    guard = network_guard.no_network.__wrapped__(monkeypatch)
    ports = next(guard)
    restricted_dns = socket.getaddrinfo
    def resolve(host, port, family=0, type=0, proto=0, flags=0):
        if host in {"127.0.0.1", "::1"}:
            # Adapter construction validates numeric loopback URLs even when
            # urllib is mocked. AI_NUMERICHOST does no DNS or other network I/O;
            # the connect guard still allows only this test's owned listeners.
            return numeric_dns(host, port, family, type, proto, flags | socket.AI_NUMERICHOST)
        return restricted_dns(host, port, family, type, proto, flags)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    try:
        yield ports
    finally:
        with pytest.raises(StopIteration):
            next(guard)
