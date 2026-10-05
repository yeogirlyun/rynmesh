"""Link-local IPv4 discovery, with nonce-bound, identity-signed replies.

Discovery supplies addresses, never trust or resource permissions. UDP multicast
is confined to one hop; only replies from an attached subnet are accepted. A
bounded HTTP probe of attached private /24s handles networks that drop multicast.
Each HTTP reply must be signed and bound to its fresh challenge nonce.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import select
import socket
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from .crypto import SignedPayload, canonical_json, public_key_from_private, sign_payload
from .registry import PeerRecord, RegistryError, verify_peer_record

GROUP = "239.255.87.91"
PORT = 48791
INTERVAL = 15.0
TTL = 45.0
HTTP_TTL = 100.0
MAX_PACKET = 4096
MAX_PEERS = 256
PROTOCOL = "rynmesh-lan-v1"
log = logging.getLogger(__name__)


def local_interfaces() -> tuple[ipaddress.IPv4Interface, ...]:
    import ifaddr

    result = set()
    for adapter in ifaddr.get_adapters():
        for address in adapter.ips:
            if not isinstance(address.ip, str):
                continue
            interface = ipaddress.IPv4Interface(f"{address.ip}/{address.network_prefix}")
            if not (interface.ip.is_loopback or interface.ip.is_unspecified
                    or interface.ip.is_multicast or interface.ip.is_link_local):
                result.add(interface)
    return tuple(sorted(result, key=str))


class LanDiscovery:
    def __init__(self, *, private_key: bytes, node_name: str, network_id: str,
                 peer_port: int, bind_host: str = "0.0.0.0", network_key: str = "",
                 interfaces: Callable = local_interfaces, port: int = PORT):
        if not 1 <= peer_port <= 65535 or not 1 <= port <= 65535:
            raise ValueError("invalid LAN discovery port")
        self.private_key = private_key
        self.peer_id = public_key_from_private(private_key)
        self.node_name = node_name[:128]
        self.network_id = network_id
        self.peer_port = peer_port
        self.bind_host = bind_host
        self.network_key = network_key.encode()
        self.interfaces = interfaces
        self.port = port
        self.error = ""
        self._stop = threading.Event()
        self._scan = threading.Event()
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._http_thread: threading.Thread | None = None
        self._nonces: dict[str, float] = {}
        self._records: dict[tuple[str, str], tuple[float, SignedPayload]] = {}
        self._replies: dict[str, float] = {}

    @classmethod
    def from_environment(cls, store):
        enabled = os.environ.get("RYNMESH_LAN_DISCOVERY", "1").strip().lower()
        host = os.environ.get("RYNMESH_PEER_HOST", "127.0.0.1").strip()
        if enabled in {"0", "false", "off", "no"} or host in {"127.0.0.1", "localhost", "::1"}:
            return None
        # IPv6-only/hostname listeners must not advertise an unbound IPv4 port.
        try:
            ipaddress.IPv4Address(host)
            return cls(
                private_key=store.private_key_bytes, node_name=store.node_name,
                network_id=os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main").strip() or "rynmesh-main",
                peer_port=int(os.environ.get("RYNMESH_PEER_PORT", "8791") or 8791),
                bind_host=host, network_key=os.environ.get("RYNMESH_NETWORK_KEY", "").strip(),
            )
        except ValueError:
            return None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="rynmesh-lan", daemon=True)
            self._thread.start()
            self._http_thread = threading.Thread(target=self._scan_http_loop, name="rynmesh-lan-http", daemon=True)
            self._http_thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._http_thread:
            self._http_thread.join(timeout=2)
        with self._condition:
            self._records.clear()
            self._condition.notify_all()

    def records(self, network_id: str) -> list[SignedPayload]:
        if network_id != self.network_id:
            return []
        with self._condition:
            now = time.monotonic()
            self._records = {key: value for key, value in self._records.items() if value[0] > now}
            return [record for _, record in self._records.values()]

    def endpoints(self, peer_id: str, network_id: str, *, wait_s: float = 0.4) -> list[str]:
        if network_id != self.network_id or self._stop.is_set():
            return []
        deadline = time.monotonic() + wait_s
        with self._condition:
            while True:
                result = [record.payload["endpoints"][0] for record in self.records(network_id)
                          if record.public_key == peer_id]
                if result or not self._thread or not self._thread.is_alive():
                    return result
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._scan.set()
                self._condition.wait(remaining)

    def _encode(self, body: dict) -> bytes:
        mac = hmac.new(self.network_key, canonical_json(body), hashlib.sha256).hexdigest()
        return canonical_json({"body": body, "mac": mac if self.network_key else ""})

    def _probe_http(self, address: str, nonce: str) -> SignedPayload | None:
        url = f"http://{address}:{self.peer_port}/api/peer/lan-record?nonce={nonce}"
        headers = {}
        if self.network_key:
            key = self.network_key.decode()
            headers["x-ryn-auth"] = hashlib.sha256(("rynmesh-net-key:" + key).encode()).hexdigest()
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=0.35) as response:
                raw = response.read(MAX_PACKET + 1)
            if len(raw) > MAX_PACKET:
                return None
            signed = SignedPayload.from_dict(json.loads(raw))
            record = verify_peer_record(signed)
            if (record.peer_id == self.peer_id or record.network_id != self.network_id
                    or signed.payload.get("lan_protocol") != PROTOCOL
                    or signed.payload.get("lan_nonce") != nonce
                    or list(record.endpoints) != [f"http://{address}:{self.peer_port}"]):
                return None
            return signed
        except (OSError, ValueError, KeyError, TypeError, RegistryError):
            return None

    def _scan_http_loop(self):
        # Some Wi-Fi networks drop multicast but allow direct host-to-host TCP.
        # Scan only attached private /24s, with a strict time and worker bound.
        last_full_scan = 0.0
        while not self._stop.is_set():
            try:
                interfaces = [item for item in self.interfaces()
                              if item.ip.is_private and (self.bind_host == "0.0.0.0" or str(item.ip) == self.bind_host)]
                candidates = set()
                now = time.monotonic()
                if now - last_full_scan >= 120:
                    last_full_scan = now
                    for interface in interfaces[:6]:
                        subnet = ipaddress.IPv4Network(f"{interface.ip}/24", strict=False)
                        candidates.update(str(ip) for ip in subnet.hosts() if ip != interface.ip)
                else:
                    from urllib.parse import urlsplit
                    with self._condition:
                        candidates.update(urlsplit(endpoint).hostname for (_, endpoint) in self._records)
                    candidates.discard(None)
                if candidates:
                    nonce = secrets.token_hex(16)
                    with ThreadPoolExecutor(max_workers=32) as pool:
                        futures = [pool.submit(self._probe_http, address, nonce) for address in candidates]
                        for future in as_completed(futures):
                            signed = future.result()
                            if signed:
                                record = verify_peer_record(signed)
                                with self._condition:
                                    self._records[(record.peer_id, record.endpoints[0])] = (time.monotonic() + HTTP_TTL, signed)
                                    self._condition.notify_all()
            except Exception as exc:
                log.debug("LAN HTTP discovery scan failed: %s", exc)
            self._stop.wait(30)

    def _decode(self, data: bytes) -> dict:
        if len(data) > MAX_PACKET:
            raise ValueError("oversized LAN packet")
        packet = json.loads(data)
        body = packet["body"]
        if body["protocol"] != PROTOCOL or body["network_id"] != self.network_id:
            raise ValueError("foreign LAN packet")
        expected = hmac.new(self.network_key, canonical_json(body), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(packet["mac"], expected if self.network_key else ""):
            raise ValueError("LAN network key mismatch")
        return body

    def _query(self) -> bytes:
        nonce = secrets.token_hex(16)
        now = time.monotonic()
        self._nonces = {key: value for key, value in self._nonces.items() if now - value < 2}
        self._nonces[nonce] = now
        return self._encode({"protocol": PROTOCOL, "network_id": self.network_id,
                             "kind": "query", "nonce": nonce, "peer_id": self.peer_id})

    def _receive(self, data: bytes, source: tuple[str, int], interface: ipaddress.IPv4Interface):
        """Return a unicast reply, or store a verified response. Never trusts a URL."""
        try:
            address = ipaddress.IPv4Address(source[0])
            if (address not in interface.network or address.is_loopback or address.is_multicast
                    or address.is_unspecified or address.is_link_local
                    or address in {interface.network.network_address, interface.network.broadcast_address}):
                return None
            body = self._decode(data)
            nonce = body["nonce"]
            if not isinstance(nonce, str) or len(nonce) != 32:
                return None
            now = time.monotonic()
            if body["kind"] == "query":
                if body.get("peer_id") == self.peer_id:
                    return None
                # Bound both amplification and memory, even on a hostile LAN.
                self._replies = {key: value for key, value in self._replies.items() if now - value < 1}
                if source[0] in self._replies or len(self._replies) >= MAX_PEERS:
                    return None
                self._replies[source[0]] = now
                payload = PeerRecord(
                    peer_id=self.peer_id, node_name=self.node_name,
                    endpoints=(f"http://{interface.ip}:{self.peer_port}",),
                    capabilities=(), safety_packs=(), network_id=self.network_id,
                ).to_dict()
                payload.update(lan_protocol=PROTOCOL, lan_nonce=nonce)
                signed = sign_payload(payload, private_key_bytes=self.private_key)
                return self._encode({"protocol": PROTOCOL, "network_id": self.network_id,
                                     "kind": "reply", "nonce": nonce, "record": signed.to_dict()})
            issued = self._nonces.get(nonce, -TTL)
            if body["kind"] != "reply" or not 0 <= now - issued < 2:
                return None
            signed = SignedPayload.from_dict(body["record"])
            record = verify_peer_record(signed)
            if (record.peer_id == self.peer_id or record.network_id != self.network_id
                    or signed.payload.get("lan_nonce") != nonce
                    or signed.payload.get("lan_protocol") != PROTOCOL or len(record.endpoints) != 1):
                return None
            from urllib.parse import urlsplit

            url = urlsplit(record.endpoints[0])
            if (url.scheme != "http" or url.hostname != source[0] or not url.port
                    or url.username or url.password or url.path or url.query or url.fragment):
                return None
            with self._condition:
                self.records(self.network_id)
                key = (record.peer_id, record.endpoints[0])
                if len(self._records) >= MAX_PEERS and key not in self._records:
                    return None
                self._records[key] = (issued + TTL, signed)
                self._condition.notify_all()
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, RecursionError, RegistryError):
            pass  # Untrusted UDP must never kill the discovery worker.
        return None

    def _run(self):
        listener = None
        sockets: dict[socket.socket, ipaddress.IPv4Interface] = {}
        current = ()
        next_refresh = next_scan = last_scan = 0.0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_refresh:
                    next_refresh = now + INTERVAL
                    try:
                        interfaces = tuple(item for item in self.interfaces()
                                           if self.bind_host == "0.0.0.0" or str(item.ip) == self.bind_host)
                        if interfaces != current or listener is None:
                            had_network = bool(current)
                            for sock in sockets:
                                sock.close()
                            sockets.clear()
                            if listener:
                                listener.close()
                            listener = None
                            current = ()
                            if had_network:
                                # A real interface change invalidates old addresses.
                                # On first boot, the concurrent HTTP scan may have
                                # already found a peer; keep that signed result.
                                with self._condition:
                                    self._records.clear()
                            if interfaces:
                                listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                                listener.bind(("", self.port))
                                for interface in interfaces:
                                    member = socket.inet_aton(GROUP) + socket.inet_aton(str(interface.ip))
                                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                                    try:
                                        listener.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, member)
                                        sock.bind((str(interface.ip), 0))
                                        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                                        socket.inet_aton(str(interface.ip)))
                                        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                                    except OSError:
                                        sock.close()
                                        continue  # A VPN/virtual adapter must not disable physical LANs.
                                    sockets[sock] = interface
                                current = interfaces
                                next_scan = 0
                                if not sockets:
                                    raise OSError("no multicast-capable IPv4 interface")
                        self.error = ""
                    except OSError as exc:
                        self.error = str(exc)
                        for sock in sockets:
                            sock.close()
                        sockets.clear()
                        if listener:
                            listener.close()
                        listener = None
                        current = ()
                if now >= next_scan or (self._scan.is_set() and now - last_scan >= 1):
                    self._scan.clear()
                    next_scan, last_scan = now + INTERVAL, now
                    query = self._query()
                    for sock in sockets:
                        try:
                            sock.sendto(query, (GROUP, self.port))
                        except OSError as exc:
                            self.error = str(exc)
                if listener is None:
                    self._stop.wait(0.1)
                    continue
                ready, _, _ = select.select([listener, *sockets], [], [], 0.1)
                for sock in ready:
                    try:
                        data, source = sock.recvfrom(MAX_PACKET + 1)
                        # Replies use an ephemeral per-interface socket so multiple
                        # local processes sharing the multicast port remain distinct.
                        receiver = sock if sock in sockets else next(
                            (item for item, interface in sockets.items()
                             if ipaddress.IPv4Address(source[0]) in interface.network), None)
                        if receiver is not None:
                            reply = self._receive(data, source, sockets[receiver])
                            if reply:
                                receiver.sendto(reply, source)
                    except OSError as exc:
                        self.error = str(exc)
        except Exception as exc:
            self.error = str(exc)
            log.warning("LAN discovery unavailable: %s", exc)
        finally:
            for sock in sockets:
                sock.close()
            if listener:
                listener.close()
