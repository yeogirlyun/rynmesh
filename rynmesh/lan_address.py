"""Shared, route-first IPv4 discovery for desktop and signed node records."""
from __future__ import annotations

import ipaddress
import os
import socket

VIRTUAL_ADAPTER_NAMES = ("vethernet", "wsl", "hyper-v", "vmware", "virtualbox")
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def usable_ipv4(value) -> bool:
    if not isinstance(value, str):
        return False  # ifaddr represents IPv6 addresses as tuples.
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return not (address.is_loopback or address.is_link_local or address.is_unspecified
                or address.is_multicast or address.is_reserved)


def private_lan_ip(value: str) -> bool:
    return usable_ipv4(value) and any(ipaddress.IPv4Address(value) in network for network in PRIVATE_NETWORKS)


def local_lan_ips() -> tuple[str, ...]:
    """Prefer local interface enumeration; hostname resolution is last resort."""
    import ifaddr

    addresses = []
    excluded = set()
    try:
        for adapter in ifaddr.get_adapters():
            name = f"{getattr(adapter, 'name', '')} {getattr(adapter, 'nice_name', '')}".casefold()
            if any(virtual in name for virtual in VIRTUAL_ADAPTER_NAMES):
                excluded.update(ip.ip for ip in adapter.ips if isinstance(ip.ip, str))
                continue
            addresses.extend(ip.ip for ip in adapter.ips if usable_ipv4(ip.ip))
    except OSError:
        addresses = []
    if not addresses:
        try:
            addresses = [item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                         if item[0] == socket.AF_INET and usable_ipv4(item[4][0]) and item[4][0] not in excluded]
        except OSError:
            return ()
    return tuple(dict.fromkeys(addresses))


def primary_lan_ip() -> str:
    if configured := os.environ.get("RYNMESH_MACHINE_IP", "").strip():
        return configured
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(.2)
            # UDP connect only asks the kernel for its route; no packet is sent.
            probe.connect(("1.1.1.1", 80))
            address = probe.getsockname()[0]
            if usable_ipv4(address):
                return address
    except OSError:
        pass  # Offline: enumerate local interfaces below.
    addresses = local_lan_ips()
    return addresses[0] if addresses else "127.0.0.1"
