"""Local address discovery must not wait for macOS hostname resolution."""
from types import SimpleNamespace

import ifaddr

from rynmesh import peer_http, store


def no_dns(*args, **kwargs):
    raise AssertionError("hostname resolution must not run on the startup path")


def test_macos_store_reads_interfaces_without_dns(monkeypatch):
    monkeypatch.setattr(store.sys, "platform", "darwin")
    monkeypatch.delenv("RYNMESH_MACHINE_IP", raising=False)
    monkeypatch.setattr(store.socket, "getaddrinfo", no_dns)
    monkeypatch.setattr(ifaddr, "get_adapters", lambda: [SimpleNamespace(ips=[
        SimpleNamespace(ip="127.0.0.1"), SimpleNamespace(ip="192.168.1.8"),
        SimpleNamespace(ip=("::1", 0, 0)),
    ])])

    def no_route(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(store.socket, "socket", no_route)
    assert store._local_ip_addresses() == ("192.168.1.8",)


def test_macos_desktop_fallback_uses_local_interfaces(monkeypatch):
    monkeypatch.setattr(peer_http.sys, "platform", "darwin")
    monkeypatch.delenv("RYNMESH_MACHINE_IP", raising=False)
    monkeypatch.setattr(peer_http.socket, "getaddrinfo", no_dns)

    def missing_command(*args, **kwargs):
        raise FileNotFoundError("no route command")

    monkeypatch.setattr(peer_http.subprocess, "run", missing_command)
    monkeypatch.setattr(store, "_primary_lan_ip", lambda: "192.168.1.8")
    assert peer_http._desktop_lan_ip() == "192.168.1.8"
