"""Device drop engine: files, folders and node messages between devices.

Direct routes (LAN, public direct) carry no artificial size limit. Files are
read and written as streams of 1 MiB encrypted frames; the receiver commits
8 MiB chunks (fsync + SHA-256 recorded in SQLite) and can resume from the last
committed chunk after a network drop, pause or a restart of either side.
``completed`` is only reported after the receiver re-read every byte from disk,
matched the chunk hashes and the sender's chunk-list root and moved the files
into place without overwriting anything.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import http.client
import ipaddress
import json
import os
import re
import shutil
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

from . import codec
from .codec import CHUNK_BYTES, FRAME_PLAINTEXT_BYTES, DropProtocolError, json_bytes, new_id
from .p2p_link import DropP2P, is_p2p_endpoint, peer_of
from .peers import (
    PeerDirectory,
    make_hello,
    network_headers,
    platform_label,
)
from .store import ACTIVE_OUT, FINAL, DropStore, now_iso

SPACE_MARGIN = 16 * 1024 * 1024
OFFER_TTL = timedelta(days=7)
MAX_MESSAGE_CHARS = 20000
MAX_FILES = 200_000
PROGRESS_EMIT_S = 0.25
EVENT_RING = 2000
# "Open" on a received file of these types only shows it in its folder. A peer
# chooses the name, so one click must never run code it sent.
LAUNCHABLE_SUFFIXES = frozenset({
    ".exe", ".com", ".scr", ".pif", ".msi", ".msp", ".msix", ".appx", ".appxbundle", ".bat", ".cmd",
    ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".lnk", ".url", ".reg",
    ".cpl", ".msc", ".jar", ".application", ".appref-ms", ".gadget", ".inf", ".sct", ".chm", ".scf",
    ".sh", ".command", ".tool", ".desktop", ".run", ".appimage", ".app", ".pkg", ".py", ".pyw",
})

OUT_ACTIONS: dict[str, list[str]] = {
    "connecting": ["cancel"],
    "waiting_online": ["retry", "cancel"],
    "awaiting_accept": ["cancel"],
    "transferring": ["pause", "cancel"],
    "paused": ["resume", "cancel"],
    "reconnecting": ["retry", "cancel"],
    "verifying": [],
    "completed": [],
    "insufficient_space": ["retry", "cancel"],
    "rejected": ["send_again"],
    "cancelled": ["send_again"],
    "source_changed": ["reselect"],
    "verify_failed": ["retry"],
    "failed": ["retry", "cancel"],
    "relay_confirmation": ["cancel"],
}
IN_ACTIONS: dict[str, list[str]] = {
    "awaiting_accept": ["accept", "reject"],
    "transferring": ["cancel"],
    "paused": ["cancel"],
    "reconnecting": ["cancel"],
    "completed": ["open", "reveal"],
    "insufficient_space": ["accept", "reject"],
}
URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


class DropError(Exception):
    """A request the engine refuses; ``status`` maps to the HTTP status."""

    def __init__(self, code: str, message: str = "", status: int = 400, **extra: Any) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.status = status
        self.extra = extra


class _PeerRefused(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class _SourceChanged(Exception):
    pass


class _Stopped(Exception):
    pass


# ---- helpers ---------------------------------------------------------------


def free_bytes(path: str | Path) -> int | None:
    current = Path(path)
    for candidate in [current, *current.parents]:
        try:
            if candidate.exists():
                return int(shutil.disk_usage(candidate).free)
        except OSError:
            return None
    return None


def safe_rel_path(rel: Any) -> str:
    """Normalise a sender-supplied relative path; reject traversal outright."""

    if not isinstance(rel, str) or not rel or len(rel) > 4096:
        raise DropProtocolError("invalid file path")
    rel = rel.replace("\\", "/")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise DropProtocolError("absolute paths are not accepted")
    parts = rel.split("/")
    if len(parts) > 64:
        raise DropProtocolError("path is too deep")
    out: list[str] = []
    for part in parts:
        if part in ("", ".", ".."):
            raise DropProtocolError("path traversal is not accepted")
        cleaned = _BAD_CHARS.sub("_", part).rstrip(" .")
        if not cleaned:
            cleaned = "_"
        if cleaned.split(".")[0].upper() in _RESERVED:
            cleaned = "_" + cleaned
        out.append(cleaned[:255])
    return "/".join(out)


def unique_path(parent: Path, name: str, taken: set[str] | None = None) -> Path:
    candidate = parent / name
    taken = taken or set()
    if not candidate.exists() and not candidate.is_symlink() and name.casefold() not in {s.casefold() for s in taken}:
        return candidate
    stem, ext = (name, "")
    if "." in name and not name.startswith("."):
        stem, ext = name.rsplit(".", 1)
        ext = "." + ext
    for n in range(1, 100000):
        alt = f"{stem} ({n}){ext}"
        candidate = parent / alt
        if not candidate.exists() and not candidate.is_symlink() and alt.casefold() not in {s.casefold() for s in taken}:
            return candidate
    raise DropError("name_exhausted", "Could not find a free file name.", 500)


def private_source(host: str | None) -> bool:
    try:
        address = ipaddress.ip_address(str(host or ""))
    except ValueError:
        return False
    return bool(address.is_private or address.is_loopback or address.is_link_local)


def _is_disk_full(exc: OSError) -> bool:
    return exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in (112, 39)


def default_receive_dir() -> str:
    configured = os.environ.get("RYNMESH_DROP_RECEIVE_DIR", "").strip()
    if configured:
        return configured
    return str(Path.home() / "Downloads" / "Rynmesh")


@dataclass
class _Rate:
    total: int | None = None
    at: float = 0.0
    bps: float | None = None

    def update(self, total: int) -> None:
        now = time.monotonic()
        if self.total is None or total < self.total:
            self.total, self.at = total, now
            return
        elapsed = now - self.at
        if elapsed < 0.5:
            return
        inst = (total - self.total) / elapsed
        self.bps = inst if self.bps is None else 0.6 * self.bps + 0.4 * inst
        self.total, self.at = total, now

    def reset(self) -> None:
        self.total, self.bps = None, None


@dataclass
class _Ctl:
    stop: threading.Event = field(default_factory=threading.Event)
    wake: threading.Event = field(default_factory=threading.Event)

    def wait(self, seconds: float) -> bool:
        """Sleep up to ``seconds``; return True when the worker must stop."""

        self.wake.wait(seconds)
        self.wake.clear()
        return self.stop.is_set()


@dataclass
class _WriteSlot:
    lock: threading.Lock = field(default_factory=threading.Lock)
    gen: int = 0


# ---- engine ----------------------------------------------------------------


class DropEngine:
    def __init__(
        self,
        *,
        node: Any,
        home: Path,
        messaging_key: Any,
        network_id: Callable[[], str],
        own_peers: Callable[[], dict[str, str]],
        friend_peers: Callable[[], dict[str, str]],
        peer_endpoints: Callable[[str], list[str]] | None = None,
    ) -> None:
        self.node = node
        self.home = Path(home)
        self.db = DropStore(self.home)
        self.msg_priv = messaging_key
        self.msg_pub = base64.b64encode(messaging_key.public_key().public_bytes_raw()).decode("ascii")
        self._own_peers = own_peers
        self._friend_peers = friend_peers
        self.p2p = DropP2P(store=node, network_id=network_id,
                           allowed=lambda peer_id: self.relationship(peer_id) in ("own", "friend")
                           ) if DropP2P.supported(node) else None
        self.dir = PeerDirectory(store=node, network_id=network_id, relationship=self.relationship,
                                 extra_endpoints=peer_endpoints, p2p=self.p2p)
        self.boot_id = new_id()
        self.uploads_root = self.home / "drop" / "uploads"
        # events
        self._ev_lock = threading.Lock()
        self._seq = 0
        self._ring: deque[dict[str, Any]] = deque(maxlen=EVENT_RING)
        self._subscribers: list[tuple[Any, Any]] = []
        self._last_emit: dict[str, float] = {}
        self._peer_sig: dict[str, tuple] = {}
        # workers
        self._lock = threading.RLock()
        self._workers: dict[str, tuple[threading.Thread, _Ctl]] = {}
        self._retry_receiver: set[str] = set()
        self._rates: dict[str, _Rate] = {}
        self._slots: dict[tuple[str, str], _WriteSlot] = {}
        self._verifying: set[str] = set()
        self._delivering: set[str] = set()
        self._pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="drop")
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None

    # ---- identity -------------------------------------------------------
    @property
    def me(self) -> str:
        return self.node.peer_id

    def node_name(self) -> str:
        return str(getattr(self.node, "node_name", "") or "")

    def hello(self, nonce: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-fA-F]{16,64}", nonce or ""):
            raise DropError("bad_nonce", "nonce must be hex", 400)
        return make_hello(peer_id=self.me, node_name=self.node_name(), messaging_pub=self.msg_pub, nonce=nonce,
                          private_key_bytes=self.node.private_key_bytes)

    # ---- trust ----------------------------------------------------------
    def _safe_call(self, fn: Callable[[], dict[str, str]]) -> dict[str, str]:
        try:
            return dict(fn() or {})
        except Exception:
            return {}

    def relationship(self, peer_id: str) -> str:
        if not peer_id or peer_id == self.me:
            return "other"
        if peer_id in self._safe_call(self._own_peers):
            return "own"
        if peer_id in self._safe_call(self._friend_peers):
            return "friend"
        if self.dir.is_lan_discovered(peer_id):
            return "nearby"
        return "other"

    def _authorize(self, peer_id: str, source_ip: str | None) -> str:
        if self.db.prefs(peer_id).get("blocked"):
            raise DropError("blocked", "This device does not accept requests from you.", 403)
        relation = self.relationship(peer_id)
        if relation == "nearby" and not private_source(source_ip):
            relation = "other"
        if relation == "other":
            raise DropError("not_paired", "Devices are not paired.", 403)
        return relation

    def _needs_verification(self, peer_id: str, relation: str | None = None) -> bool:
        relation = relation or self.relationship(peer_id)
        return relation == "nearby" and not self.db.prefs(peer_id).get("verified")

    # ---- events ---------------------------------------------------------
    def subscribe(self, loop: Any, wake: Any) -> None:
        with self._ev_lock:
            self._subscribers.append((loop, wake))

    def unsubscribe(self, loop: Any, wake: Any) -> None:
        with self._ev_lock:
            try:
                self._subscribers.remove((loop, wake))
            except ValueError:
                pass

    @property
    def seq(self) -> int:
        return self._seq

    def events_since(self, since: int) -> list[dict[str, Any]] | None:
        """Events after ``since``; ``None`` when the client must resync."""

        with self._ev_lock:
            if since > self._seq:
                return None
            if since == self._seq:
                return []
            if not self._ring or self._ring[0]["seq"] > since + 1:
                return None
            return [event for event in self._ring if event["seq"] > since]

    def _publish(self, event: dict[str, Any]) -> None:
        with self._ev_lock:
            self._seq += 1
            event = {"seq": self._seq, **event}
            self._ring.append(event)
            subscribers = list(self._subscribers)
        for loop, wake in subscribers:
            try:
                loop.call_soon_threadsafe(wake.set)
            except Exception:
                pass

    def emit_transfer(self, row: dict[str, Any] | str | None, *, force: bool = True) -> None:
        if isinstance(row, str):
            row = self.db.transfer(row)
        if not row:
            return
        tid = row["transfer_id"]
        now = time.monotonic()
        if not force and now - self._last_emit.get(tid, 0.0) < PROGRESS_EMIT_S:
            return
        self._last_emit[tid] = now
        self._publish({"type": "transfer", "transfer": self.transfer_api(row)})

    def emit_message(self, row: dict[str, Any]) -> None:
        if row.get("held"):
            return
        self._publish({"type": "message", "message": self.message_api(row)})

    def emit_peer(self, peer_id: str, *, force: bool = False) -> None:
        meta = self.peer_meta(peer_id)
        signature = (meta["online"], meta["route"], meta["unread"], json.dumps(meta["last"]), meta["send_mode"],
                     meta["verification"]["verified"], meta["auto_accept"], meta["name"], meta["relationship"])
        if not force and self._peer_sig.get(peer_id) == signature:
            return
        self._peer_sig[peer_id] = signature
        self._publish({"type": "peer", "peer": meta})

    # ---- serialisation --------------------------------------------------
    def _rate(self, tid: str) -> _Rate:
        rate = self._rates.get(tid)
        if rate is None:
            rate = self._rates[tid] = _Rate()
        return rate

    def transfer_api(self, row: dict[str, Any], *, with_files: bool = False) -> dict[str, Any]:
        tid = row["transfer_id"]
        status = row["status"]
        direction = row["direction"]
        if direction == "out":
            actions = list(OUT_ACTIONS.get(status, []))
            if "send_again" in actions and row["source_kind"] != "paths":
                actions.remove("send_again")
        else:
            actions = list(IN_ACTIONS.get(status, []))
        files = self.db.files(tid) if with_files else self.db.files_head(tid, 50)
        speed = eta = None
        if status == "transferring":
            rate = self._rates.get(tid)
            if rate and rate.bps is not None:
                speed = max(0.0, rate.bps)
                remaining = max(0, int(row["total_bytes"]) - int(row["confirmed_bytes"]))
                eta = int(remaining / speed) if speed > 1 else None
        error = None
        if row["error_code"]:
            error = {"code": row["error_code"], "message": row["error_message"]}
            if row["error_need"] is not None:
                error["need_bytes"] = json_bytes(row["error_need"])
            if row["error_free"] is not None:
                error["free_bytes"] = json_bytes(row["error_free"])
        code = None
        if status in ("awaiting_accept", "insufficient_space", "connecting", "waiting_online") and (
            (direction == "in" and row["needs_verification"])
            or (direction == "out" and self._needs_verification(row["peer_id"]))
        ):
            code = codec.verification_code(self.me, row["peer_id"])
        out = {
            "transfer_id": tid,
            "offer_id": row["offer_id"],
            "peer_id": row["peer_id"],
            "direction": direction,
            "kind": row["kind"],
            "name": row["name"],
            "file_count": row["file_count"],
            "total_bytes": json_bytes(row["total_bytes"]),
            "confirmed_bytes": json_bytes(row["confirmed_bytes"]),
            "status": status,
            "route": row["route"],
            "speed_bps": speed,
            "eta_s": eta,
            "error": error,
            "note": row["note"],
            "save_dir": row["save_dir"],
            "final_path": row["final_path"],
            "verification_code": code,
            "actions": actions,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
            "version": row["version"],
            "files_preview": [{"rel_path": f["rel_path"], "size": json_bytes(f["size"])} for f in files[:50]],
        }
        if with_files:
            out["files"] = [
                {"file_id": f["file_id"], "rel_path": f["rel_path"], "size": json_bytes(f["size"]),
                 "committed": json_bytes(f["committed"]), "status": f["status"], "final_path": f["final_path"]}
                for f in files
            ]
        return out

    @staticmethod
    def message_api(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "message_id": row["message_id"],
            "peer_id": row["peer_id"],
            "direction": row["direction"],
            "kind": row["kind"],
            "text": row["text"],
            "created_at": row["created_at"],
            "status": row["status"],
            "delivered_at": row["delivered_at"],
            "error": row["error"],
            "version": row["version"],
            "client_id": row.get("client_id"),
        }

    def _set(self, tid: str, *, emit: bool = True, force: bool = True, **values: Any) -> dict[str, Any]:
        row = self.db.update_transfer(tid, **values)
        if emit:
            self.emit_transfer(row, force=force)
        return row

    def _status(self, tid: str, status: str, **values: Any) -> dict[str, Any]:
        """Set a status, clearing a stale error unless one is supplied."""

        values.setdefault("error_code", "")
        values.setdefault("error_message", "")
        values.setdefault("error_need", None)
        values.setdefault("error_free", None)
        current = self.db.transfer(tid)
        if current and current["status"] == status and all(current.get(k) == v for k, v in values.items()):
            return current
        if status != "transferring":
            self._rate(tid).reset()
        return self._set(tid, status=status, **values)

    # ---- peers ------------------------------------------------------------
    def receive_dir(self) -> str:
        return self.db.setting("receive_dir", "") or default_receive_dir()

    def capabilities(self) -> dict[str, Any]:
        receive = self.receive_dir()
        return {
            "direct_size_limit": None,
            "routes": ["lan", "public"],
            # ICE hole punching + QUIC (no TURN): see p2p_link.py.
            "nat_traversal": bool(self.p2p and self.p2p.running),
            "relay": {"available": False, "reason": "not_implemented", "max_bytes": None, "quota_bytes": None},
            "folders": True,
            "path_send": True,
            "staged_upload": True,
            "receive_dir": receive,
            "receive_free_bytes": json_bytes(free_bytes(receive)),
        }

    def known_peers(self) -> list[str]:
        own = self._safe_call(self._own_peers)
        friends = self._safe_call(self._friend_peers)
        ids: list[str] = []
        for peer_id in [*own, *friends]:
            if peer_id != self.me and peer_id not in ids:
                ids.append(peer_id)
        for peer_id, record in self.dir.records().items():
            if record.get("lan_endpoints") and peer_id not in ids:
                ids.append(peer_id)
        for peer_id in self.db.conversation_peers():
            if peer_id != self.me and peer_id not in ids:
                ids.append(peer_id)
        return ids

    def peer_meta(self, peer_id: str) -> dict[str, Any]:
        relation = self.relationship(peer_id)
        prefs = self.db.prefs(peer_id)
        probe = self.dir.cached(peer_id)
        names = {**self._safe_call(self._friend_peers), **self._safe_call(self._own_peers)}
        name = names.get(peer_id) or self.dir.name(peer_id) or f"device-{peer_id[:6]}"
        online: bool | None = None if probe is None else probe.online
        verified = bool(prefs.get("verified"))
        required = relation == "nearby" and not verified
        if prefs.get("blocked"):
            sendable, mode, reason = False, "unavailable", "blocked"
        elif relation == "other":
            sendable, mode, reason = False, "unavailable", "not_paired"
        elif required:
            sendable, mode, reason = True, "verify", "verification_required"
        elif online is False:
            sendable, mode, reason = True, "queue", "offline"
        else:
            sendable, mode, reason = True, "send", ""
        candidates = self.dir.candidates(peer_id)
        address = (probe.endpoint if probe and probe.online else "") or (candidates[0] if candidates else "")
        if is_p2p_endpoint(address):
            info = self.p2p.link_info(peer_id) if self.p2p else None
            address = f"P2P · {info['address']}" if info else "P2P"
        return {
            "peer_id": peer_id,
            "relationship": relation,
            "name": name,
            "online": online,
            "route": probe.route if probe and probe.online else None,
            "address": address,
            "latency_ms": probe.latency_ms if probe and probe.online else None,
            "sendable": sendable,
            "send_mode": mode,
            "reason": reason,
            "verification": {
                "required": required,
                "code": codec.verification_code(self.me, peer_id) if relation == "nearby" else None,
                "verified": verified,
            },
            "auto_accept": bool(prefs.get("auto_accept")),
            "unread": self.db.unread(peer_id, str(prefs.get("last_read_at") or "")),
            "last": self.db.last_activity(peer_id),
            "last_seen": self.dir.last_seen(peer_id),
            "platform": (probe.platform if probe else "") or "",
        }

    def peers(self) -> list[dict[str, Any]]:
        out = []
        history = set(self.db.conversation_peers())
        for peer_id in self.known_peers():
            meta = self.peer_meta(peer_id)
            if meta["relationship"] == "other" and peer_id not in history:
                continue
            out.append(meta)
        return out

    def incoming_count(self) -> int:
        return len(self.db.transfers_in(("awaiting_accept", "insufficient_space"), "in"))

    def snapshot(self) -> dict[str, Any]:
        seq = self._seq
        return {
            "seq": seq,
            "boot_id": self.boot_id,
            "transfers": [self.transfer_api(row) for row in self.db.transfers(limit=500)],
            "peers": self.peers(),
            "incoming": self.incoming_count(),
        }

    def verify_peer(self, peer_id: str, accept: bool) -> dict[str, Any]:
        if accept:
            self.db.set_prefs(peer_id, verified=1, blocked=0)
            self.db.release_held(peer_id)
            for row in self.db.transfers(peer_id=peer_id):
                if row["direction"] == "in" and row["needs_verification"]:
                    self._set(row["transfer_id"], needs_verification=0)
        else:
            self.db.set_prefs(peer_id, verified=0, blocked=1)
            self.db.delete_held(peer_id)
            for row in self.db.transfers(peer_id=peer_id):
                if row["direction"] == "in" and row["status"] in ("awaiting_accept", "insufficient_space"):
                    self._status(row["transfer_id"], "rejected")
        self.emit_peer(peer_id, force=True)
        return self.peer_meta(peer_id)

    def mark_read(self, peer_id: str) -> None:
        self.db.set_prefs(peer_id, last_read_at=now_iso())
        self.emit_peer(peer_id)

    def conversation(self, peer_id: str, limit: int = 300) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for row in self.db.messages(peer_id, limit=limit):
            if row["held"]:
                continue
            items.append({"type": "message", "at": row["created_at"], "message": self.message_api(row)})
        for row in self.db.transfers(peer_id=peer_id, limit=limit):
            items.append({"type": "transfer", "at": row["created_at"], "transfer": self.transfer_api(row)})
        items.sort(key=lambda item: item["at"])
        return {"peer_id": peer_id, "items": items[-limit:]}

    def prefs_api(self) -> dict[str, Any]:
        receive = self.receive_dir()
        return {
            "receive_dir": receive,
            "receive_free_bytes": json_bytes(free_bytes(receive)),
            "peers": {pid: {"auto_accept": bool(p["auto_accept"])} for pid, p in self.db.all_prefs().items()},
        }

    def set_prefs_api(self, body: dict[str, Any]) -> dict[str, Any]:
        receive = body.get("receive_dir")
        if receive is not None:
            receive = str(receive).strip()
            if not receive or not Path(receive).is_absolute():
                raise DropError("invalid_dir", "Choose an absolute folder path.", 400)
            Path(receive).mkdir(parents=True, exist_ok=True)
            self.db.set_setting("receive_dir", receive)
        for peer_id, values in (body.get("peers") or {}).items():
            if isinstance(values, dict) and "auto_accept" in values:
                self.db.set_prefs(str(peer_id), auto_accept=int(bool(values["auto_accept"])))
                self.emit_peer(str(peer_id), force=True)
        return self.prefs_api()

    # ---- lifecycle -------------------------------------------------------
    def attach_app(self, app: Any) -> None:
        """Peer requests arriving over a P2P link are dispatched into this app."""
        if self.p2p is not None:
            self.p2p.app = app

    def attach_loop(self, loop: Any) -> None:
        """The server's event loop; P2P requests run there like HTTP ones."""
        if self.p2p is not None:
            self.p2p.app_loop = loop

    def start(self) -> None:
        if self._scheduler and self._scheduler.is_alive():
            return
        self._stop.clear()
        if self.p2p is not None:
            self.p2p.start()
        if getattr(self._pool, "_shutdown", False):
            self._pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="drop")
        self._recover()
        self._scheduler = threading.Thread(target=self._schedule, name="drop-scheduler", daemon=True)
        self._scheduler.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            workers = list(self._workers.values())
        for _thread, ctl in workers:
            ctl.stop.set()
            ctl.wake.set()
        for thread, _ctl in workers:
            thread.join(timeout=5)
        if self._scheduler:
            self._scheduler.join(timeout=5)
        if self.p2p is not None:
            self.p2p.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def close(self) -> None:
        self.stop()
        self.db.close()

    def _recover(self) -> None:
        """Bring persisted state back to a consistent, resumable point."""

        for row in self.db.transfers_in(("transferring", "paused", "reconnecting", "verifying", "insufficient_space",
                                         "awaiting_accept"), "in"):
            staging = Path(row["staging_dir"]) if row["staging_dir"] else None
            if staging is None:
                continue
            for f in self.db.files(row["transfer_id"]):
                part = staging / f"{f['file_id']}.part"
                try:
                    if part.exists() and part.stat().st_size > int(f["committed"]):
                        with open(part, "r+b") as fh:
                            fh.truncate(int(f["committed"]))
                except OSError:
                    pass
            if row["status"] == "verifying":
                self._start_verify(row["transfer_id"])
            elif row["status"] in ("paused", "reconnecting"):
                self._status(row["transfer_id"], "transferring")

    def _schedule(self) -> None:
        tick = 0
        while not self._stop.wait(1.0):
            tick += 1
            try:
                self._ensure_workers()
                if tick % 3 == 0:
                    self._flush_outbox()
                    self._retry_cancel_notices()
                if tick % 10 == 1:
                    self._probe_peers()
                if tick % 60 == 5:
                    self._expire_offers()
            except Exception:
                pass

    def _ensure_workers(self) -> None:
        for row in self.db.transfers_in(ACTIVE_OUT, "out"):
            if not row["paused"]:
                self._start_worker(row["transfer_id"])

    def _probe_peers(self) -> None:
        def probe(peer_id: str) -> None:
            try:
                self.dir.probe(peer_id)
                self.emit_peer(peer_id)
            except Exception:
                pass

        for peer_id in self.known_peers():
            if self.relationship(peer_id) != "other":
                self._pool.submit(probe, peer_id)

    def _expire_offers(self) -> None:
        limit = (datetime.now(UTC) - OFFER_TTL).isoformat(timespec="milliseconds")
        for row in self.db.transfers_in(("awaiting_accept",), "in"):
            if row["created_at"] < limit:
                self._status(row["transfer_id"], "cancelled", error_code="expired",
                             error_message="The offer expired before it was accepted.")

    # ---- sources -----------------------------------------------------------
    def _scan_paths(self, paths: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Return (files, items, issues) for absolute local paths."""

        files: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        taken: set[str] = set()

        def add(rel: str, source: Path, st: os.stat_result) -> None:
            base = rel
            n = 1
            while rel.lower() in taken:
                head, _, tail = base.partition("/")
                rel = f"{head} ({n})" + (f"/{tail}" if tail else "")
                n += 1
            taken.add(rel.lower())
            files.append({"rel_path": rel, "source_path": str(source), "size": int(st.st_size),
                          "mtime_ns": int(st.st_mtime_ns)})

        for raw in paths:
            path = Path(str(raw))
            if not path.is_absolute():
                issues.append({"code": "invalid_path", "message": "Path must be absolute.", "path": str(raw)})
                continue
            try:
                if path.is_symlink():
                    issues.append({"code": "warn_symlink_skipped", "message": "Symbolic link skipped.",
                                   "path": str(path)})
                    continue
                if path.is_file():
                    st = path.stat()
                    add(path.name, path, st)
                    items.append({"name": path.name, "kind": "file", "file_count": 1, "size": json_bytes(st.st_size)})
                elif path.is_dir():
                    count = 0
                    size = 0
                    top = path.name or path.anchor.strip(":\\/") or "folder"
                    for root, dirs, names in os.walk(path, followlinks=False):
                        dirs.sort()
                        kept = []
                        for d in dirs:
                            if Path(root, d).is_symlink():
                                issues.append({"code": "warn_symlink_skipped", "message": "Symbolic link skipped.",
                                               "path": str(Path(root, d))})
                            else:
                                kept.append(d)
                        dirs[:] = kept
                        for name in sorted(names):
                            full = Path(root, name)
                            try:
                                if full.is_symlink():
                                    issues.append({"code": "warn_symlink_skipped",
                                                   "message": "Symbolic link skipped.", "path": str(full)})
                                    continue
                                st = full.stat()
                            except OSError:
                                issues.append({"code": "unreadable", "message": "File cannot be read.",
                                               "path": str(full)})
                                continue
                            rel = top + "/" + full.relative_to(path).as_posix()
                            add(rel, full, st)
                            count += 1
                            size += int(st.st_size)
                            if len(files) > MAX_FILES:
                                raise DropError("too_many_files", "Too many files in one transfer.", 400)
                    items.append({"name": top, "kind": "folder", "file_count": count, "size": json_bytes(size)})
                else:
                    issues.append({"code": "missing", "message": "File not found.", "path": str(path)})
            except PermissionError:
                issues.append({"code": "unreadable", "message": "File cannot be read.", "path": str(path)})
        return files, items, issues

    def _scan_uploads(self, upload_ids: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                                                            list[dict[str, Any]], str]:
        files: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        batch = ""
        groups: dict[str, list[int]] = {}
        for upload_id in upload_ids:
            b, _, rel = str(upload_id).partition(":")
            if not re.fullmatch(r"[0-9a-f]{8,64}", b) or not rel:
                issues.append({"code": "missing", "message": "Upload not found.", "path": str(upload_id)})
                continue
            if batch and b != batch:
                raise DropError("mixed_batches", "Uploads must come from one selection.", 400)
            batch = b
            try:
                rel = safe_rel_path(rel)
            except DropProtocolError:
                issues.append({"code": "invalid_path", "message": "Invalid upload path.", "path": rel})
                continue
            source = self.uploads_root / b / rel
            if not source.is_file():
                issues.append({"code": "missing", "message": "Upload not found.", "path": rel})
                continue
            st = source.stat()
            files.append({"rel_path": rel, "source_path": str(source), "size": int(st.st_size),
                          "mtime_ns": int(st.st_mtime_ns)})
            groups.setdefault(rel.split("/")[0] if "/" in rel else rel, []).append(len(files) - 1)
        items = []
        for top, idxs in groups.items():
            folder = any("/" in files[i]["rel_path"] for i in idxs)
            items.append({"name": top, "kind": "folder" if folder else "file", "file_count": len(idxs),
                          "size": json_bytes(sum(files[i]["size"] for i in idxs))})
        return files, items, issues, batch

    def _gather(self, body: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                                                     list[dict[str, Any]], str, str]:
        paths = [str(p) for p in (body.get("paths") or []) if str(p).strip()]
        upload_ids = [str(u) for u in (body.get("upload_ids") or []) if str(u).strip()]
        if paths and upload_ids:
            raise DropError("mixed_sources", "Send local paths or uploads, not both.", 400)
        if upload_ids:
            files, items, issues, batch = self._scan_uploads(upload_ids)
            return files, items, issues, "uploads", batch
        files, items, issues = self._scan_paths(paths)
        return files, items, issues, "paths", ""

    def preflight(self, body: dict[str, Any]) -> dict[str, Any]:
        peer_id = str(body.get("peer_id") or "")
        files, items, issues, _kind, _batch = self._gather(body)
        meta = self.peer_meta(peer_id)
        if not meta["sendable"]:
            issues.append({"code": meta["reason"] or "not_paired", "message": "This device cannot receive files."})
        if not files and not any(i["code"] == "missing" for i in issues):
            issues.append({"code": "empty", "message": "Nothing to send."})
        probe = self.dir.cached(peer_id)
        blocking = [i for i in issues if not i["code"].startswith("warn_")]
        return {
            "ok": not blocking and bool(files),
            "route": probe.route if probe and probe.online else None,
            "send_mode": meta["send_mode"],
            "file_count": len(files),
            "total_bytes": json_bytes(sum(f["size"] for f in files)),
            "receiver_free_bytes": None,
            "issues": issues,
            "items": items,
        }

    # ---- outgoing transfers ------------------------------------------------
    def create_transfer(self, body: dict[str, Any]) -> dict[str, Any]:
        client_id = str(body.get("client_id") or "").strip() or None
        if client_id:
            existing = self.db.transfer_by_client(client_id)
            if existing:
                return self.transfer_api(existing)
        peer_id = str(body.get("peer_id") or "")
        meta = self.peer_meta(peer_id)
        if not meta["sendable"]:
            raise DropError(meta["reason"] or "not_paired", "This device cannot receive files.", 403)
        files, items, issues, source_kind, batch = self._gather(body)
        blocking = [i for i in issues if not i["code"].startswith("warn_")]
        if blocking:
            raise DropError(blocking[0]["code"], blocking[0]["message"], 400, issues=issues)
        if not files:
            raise DropError("empty", "Nothing to send.", 400)
        if len(items) == 1 and items[0]["kind"] == "folder":
            kind = "folder"
        elif len(files) == 1:
            kind = "file"
        else:
            kind = "files"
        tid = new_id()
        eph_private, eph_pub = codec.new_ephemeral()
        record = {
            "transfer_id": tid,
            "offer_id": new_id(),
            "peer_id": peer_id,
            "direction": "out",
            "kind": kind,
            "name": items[0]["name"] if items else files[0]["rel_path"],
            "file_count": len(files),
            "total_bytes": sum(f["size"] for f in files),
            "status": "connecting",
            "note": str(body.get("note") or "")[:2000],
            "client_id": client_id,
            "eph_private": eph_private,
            "eph_pub": eph_pub,
            "source_kind": source_kind,
            "upload_batch": batch,
        }
        rows = [{"file_id": f"f{i:06d}", "idx": i, **f} for i, f in enumerate(files)]
        self.db.insert_transfer(record, rows)
        row = self.db.transfer(tid)
        self.emit_transfer(row)
        self.emit_peer(peer_id)
        self._start_worker(tid)
        return self.transfer_api(row)  # type: ignore[arg-type]

    def _start_worker(self, tid: str) -> None:
        with self._lock:
            current = self._workers.get(tid)
            if current and current[0].is_alive() and not current[1].stop.is_set():
                current[1].wake.set()
                return
            previous = current[0] if current else None
            ctl = _Ctl()
            thread = threading.Thread(target=self._sender_entry, args=(tid, ctl, previous), daemon=True,
                                      name=f"drop-send-{tid[:8]}")
            self._workers[tid] = (thread, ctl)
        thread.start()

    def _stop_worker(self, tid: str) -> None:
        with self._lock:
            current = self._workers.get(tid)
        if current:
            current[1].stop.set()
            current[1].wake.set()

    def _sender_entry(self, tid: str, ctl: _Ctl, previous: threading.Thread | None) -> None:
        if previous is not None and previous is not threading.current_thread():
            previous.join(timeout=30)
        try:
            self._sender(tid, ctl)
        except Exception as exc:  # pragma: no cover - defensive
            try:
                self._status(tid, "failed", error_code="internal", error_message=str(exc)[:300])
            except Exception:
                pass
        finally:
            with self._lock:
                current = self._workers.get(tid)
                if current and current[1] is ctl:
                    self._workers.pop(tid, None)

    def _sender(self, tid: str, ctl: _Ctl) -> None:
        backoff = 2.0
        while not ctl.stop.is_set() and not self._stop.is_set():
            row = self.db.transfer(tid)
            if not row or row["direction"] != "out" or row["paused"] or row["status"] not in ACTIVE_OUT:
                return
            peer_id = row["peer_id"]
            probe = self.dir.probe(peer_id, fresh=row["status"] in ("connecting", "reconnecting", "waiting_online"))
            self.emit_peer(peer_id)
            if not probe.online:
                self._status(tid, "waiting_online", route=None)
                if ctl.wait(10):
                    return
                continue
            if row["receiver_pub"] and row["receiver_pub"] != probe.messaging_pub:
                self._status(tid, "failed", error_code="key_changed",
                             error_message="The receiving device presented a different key.")
                return
            if not row["master_key"] or row["route"] != probe.route:
                master = row["master_key"] or codec.sender_master_key(row["eph_private"], probe.messaging_pub, tid)
                row = self._set(tid, receiver_pub=probe.messaging_pub, route=probe.route, master_key=master)
            try:
                if self._drive(row, probe.endpoint, ctl):
                    return
                backoff = 2.0
            except _Stopped:
                return
            except _SourceChanged:
                self._status(tid, "source_changed", error_code="source_changed",
                             error_message="The source file changed; the original transfer cannot continue.")
                self._cleanup_uploads(tid)
                return
            except _PeerRefused as exc:
                code = "not_paired" if exc.status == 403 else "refused"
                self._status(tid, "failed", error_code=code, error_message=exc.detail[:300])
                return
            except (ConnectionError, OSError, TimeoutError, http.client.HTTPException, DropProtocolError) as exc:
                if ctl.stop.is_set():
                    return
                self.dir.forget(peer_id)
                current = self.db.transfer(tid)
                if current and current["status"] in ("transferring", "verifying", "awaiting_accept", "connecting"):
                    self._status(tid, "reconnecting", error_code="network", error_message=str(exc)[:200])
                if ctl.wait(backoff):
                    return
                backoff = min(backoff * 2, 30.0)

    # peer calls
    def _call(self, endpoint: str, kind: str, peer_id: str, payload: dict[str, Any], timeout: float = 20.0
              ) -> dict[str, Any]:
        envelope = codec.make_envelope(kind, payload, sender=self.me, to=peer_id,
                                       private_key_bytes=self.node.private_key_bytes)
        status, body = self.dir.request(endpoint, "POST", f"/api/peer/drop/{kind}", envelope, timeout=timeout)
        if status >= 500 or status == 0:
            raise ConnectionError(f"peer returned {status}")
        if status >= 400:
            detail = ""
            if isinstance(body, dict):
                detail = str(body.get("detail") or body.get("code") or "")
            raise _PeerRefused(status, detail or f"http {status}")
        response = codec.open_response(body, me=self.me, expect_from=peer_id)
        if response.get("request_nonce") != envelope["payload"]["nonce"]:
            raise DropProtocolError("response does not acknowledge this request")
        return response

    def _status_call(self, endpoint: str, row: dict[str, Any], state: str) -> dict[str, Any]:
        tid = row["transfer_id"]
        payload = {"transfer_id": tid, "sender_state": state, "retry": tid in self._retry_receiver}
        return self._call(endpoint, "status", row["peer_id"], payload, timeout=15)

    def _offer_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        files = self.db.files(row["transfer_id"])
        manifest = {
            "kind": row["kind"],
            "name": row["name"],
            "total_bytes": json_bytes(row["total_bytes"]),
            "files": [{"file_id": f["file_id"], "rel_path": f["rel_path"], "size": json_bytes(f["size"])}
                      for f in files],
        }
        secret = json.dumps({"manifest": manifest, "eph_pub": row["eph_pub"], "note": row["note"],
                             "sender_name": self.node_name(), "platform": platform_label()}).encode("utf-8")
        return {
            "transfer_id": row["transfer_id"],
            "offer_id": row["offer_id"],
            "from_pub": self.msg_pub,
            "sealed": codec.seal_for(self.msg_priv, row["receiver_pub"], secret, codec.OFFER_INFO),
        }

    def _drive(self, row: dict[str, Any], endpoint: str, ctl: _Ctl) -> bool:
        """Run one connected session. Return True when the worker is finished."""

        tid = row["transfer_id"]
        peer_id = row["peer_id"]
        if not row["offer_delivered"]:
            self._status(tid, "connecting", route=row["route"])
            self._call(endpoint, "offer", peer_id, self._offer_payload(row), timeout=60)
            row = self._set(tid, offer_delivered=1)
        while not ctl.stop.is_set():
            st = self._status_call(endpoint, row, "transferring")
            state = str(st.get("status") or "")
            if state not in ("verify_failed", "failed"):
                self._retry_receiver.discard(tid)
            self._apply_committed(tid, st)
            if state == "unknown":
                self._set(tid, offer_delivered=0)
                return False
            if state == "awaiting_accept":
                self._status(tid, "awaiting_accept")
                if ctl.wait(2):
                    raise _Stopped
                continue
            if state == "insufficient_space":
                self._status(tid, "insufficient_space", error_code="insufficient_space",
                             error_message="The receiving device does not have enough space.",
                             error_need=codec.parse_bytes(st.get("need_bytes") or 0),
                             error_free=codec.parse_bytes(st.get("free_bytes") or 0))
                if ctl.wait(5):
                    raise _Stopped
                continue
            if state in ("rejected", "cancelled"):
                self._status(tid, state)
                self._cleanup_uploads(tid)
                return True
            if state == "completed":
                total = int(row["total_bytes"])
                self._status(tid, "completed", confirmed_bytes=total, completed_at=now_iso())
                self._cleanup_uploads(tid)
                self.emit_peer(peer_id)
                return True
            if state in ("verify_failed", "failed"):
                if tid in self._retry_receiver:
                    if ctl.wait(1):
                        raise _Stopped
                    continue
                err = st.get("error") or {}
                self._status(tid, state, error_code=str(err.get("code") or state),
                             error_message=str(err.get("message") or "")[:300])
                return True
            if state == "verifying":
                self._status(tid, "verifying")
                if ctl.wait(1):
                    raise _Stopped
                continue
            if state not in ("transferring", "paused"):
                raise DropProtocolError(f"unexpected receiver status {state!r}")
            remote = st.get("pending") or {}
            pending = None
            if remote:
                first = min(remote, key=lambda fid: fid)
                f = self.db.file(tid, first)
                if f is None:
                    raise DropProtocolError("receiver reported an unknown file")
                pending = (f, codec.parse_bytes((remote.get(first) or {}).get("committed", 0)))
            if pending is None:
                self._status(tid, "verifying")
                roots = {f["file_id"]: self._root(row, f) for f in self.db.files(tid)}
                self._call(endpoint, "complete", peer_id, {"transfer_id": tid, "roots": roots}, timeout=30)
                continue
            self._status(tid, "transferring")
            self._stream_file(row, pending[0], pending[1], endpoint, ctl)
        raise _Stopped

    def _apply_committed(self, tid: str, st: dict[str, Any]) -> None:
        if "committed_total" not in st:
            return
        try:
            total = codec.parse_bytes(st.get("committed_total"))
        except DropProtocolError:
            return
        row = self.db.transfer(tid)
        if not row:
            return
        self._rate(tid).update(total)
        if total != int(row["confirmed_bytes"]):
            self._set(tid, confirmed_bytes=total, force=False)

    def _root(self, row: dict[str, Any], f: dict[str, Any]) -> str:
        tid, fid, size = row["transfer_id"], f["file_id"], int(f["size"])
        expected = (size + CHUNK_BYTES - 1) // CHUNK_BYTES
        hashes = self.db.chunks(tid, fid)[:expected]
        if len(hashes) < expected:
            missing: list[tuple[int, str]] = []
            with open(f["source_path"], "rb") as fh:
                fh.seek(len(hashes) * CHUNK_BYTES)
                for idx in range(len(hashes), expected):
                    digest = hashlib.sha256()
                    left = min(CHUNK_BYTES, size - idx * CHUNK_BYTES)
                    while left > 0:
                        data = fh.read(min(FRAME_PLAINTEXT_BYTES, left))
                        if not data:
                            raise _SourceChanged()
                        digest.update(data)
                        left -= len(data)
                    missing.append((idx, digest.hexdigest()))
            self.db.set_chunks(tid, fid, missing)
            hashes = self.db.chunks(tid, fid)[:expected]
        return codec.chunk_root(hashes)

    def _stream_file(self, row: dict[str, Any], f: dict[str, Any], offset: int, endpoint: str, ctl: _Ctl) -> None:
        tid, fid, size = row["transfer_id"], f["file_id"], int(f["size"])
        source = f["source_path"]
        try:
            st = os.stat(source)
        except OSError as exc:
            raise _SourceChanged() from exc
        if int(st.st_size) != size or int(st.st_mtime_ns) != int(f["mtime_ns"]):
            raise _SourceChanged()
        if offset % CHUNK_BYTES and offset != size:
            raise DropProtocolError("receiver offset is not chunk aligned")
        master = row["master_key"]
        key = codec.file_key(master, fid)
        salt = os.urandom(4)
        sealer = codec.FrameSealer(key, tid, fid, salt)
        auth = codec.put_auth(master, tid, fid, offset, salt)

        def frames() -> Iterator[bytes]:
            with open(source, "rb") as fh:
                fh.seek(offset)
                pos = offset
                idx = offset // CHUNK_BYTES
                digest = hashlib.sha256()
                while pos < size:
                    if ctl.stop.is_set() or self._stop.is_set():
                        raise _Stopped()
                    n = min(FRAME_PLAINTEXT_BYTES, size - pos)
                    data = fh.read(n)
                    if len(data) != n:
                        raise _SourceChanged()
                    digest.update(data)
                    frame = sealer.seal(pos, data)
                    pos += n
                    if pos % CHUNK_BYTES == 0 or pos == size:
                        self.db.set_chunks(tid, fid, [(idx, digest.hexdigest())])
                        idx += 1
                        digest = hashlib.sha256()
                    yield frame

        target = f"/api/peer/drop/data/{tid}/{fid}?offset={offset}&salt={salt.hex()}"
        headers = {"Content-Type": "application/octet-stream", "X-Ryn-Drop-Auth": auth,
                   "X-Ryn-Drop-From": self.me, **network_headers()}
        poll_stop = threading.Event()

        def poll() -> None:
            while not poll_stop.wait(1.0):
                try:
                    current = self.db.transfer(tid)
                    if not current:
                        return
                    self._apply_committed(tid, self._status_call(endpoint, current, "transferring"))
                except Exception:
                    pass

        poller = threading.Thread(target=poll, daemon=True, name=f"drop-poll-{tid[:8]}")
        poller.start()
        conn: http.client.HTTPConnection | None = None
        try:
            if is_p2p_endpoint(endpoint):
                if self.p2p is None:
                    raise ConnectionError("p2p is not available")
                # Same frames over a QUIC stream; congestion control paces them.
                try:
                    status, _body = self.p2p.stream_put(peer_of(endpoint), target, frames(), headers=headers)
                except TimeoutError as exc:
                    raise ConnectionError("p2p upload timed out") from exc
            else:
                url = urlsplit(endpoint)
                cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
                conn = cls(url.hostname, url.port or (443 if url.scheme == "https" else 80), timeout=60)
                conn.request("PUT", target, body=frames(), headers=headers, encode_chunked=True)
                resp = conn.getresponse()
                resp.read(1024 * 1024)
                status = resp.status
            if status >= 500 and status != 507:
                raise ConnectionError(f"receiver returned {status}")
            # 409 (offset moved), 410 (cancelled), 507 (space) are resolved by the next status poll.
        finally:
            poll_stop.set()
            if conn is not None:
                conn.close()
            poller.join(timeout=3)

    def _cleanup_uploads(self, tid: str) -> None:
        row = self.db.transfer(tid)
        if row and row["source_kind"] == "uploads" and row["upload_batch"]:
            shutil.rmtree(self.uploads_root / row["upload_batch"], ignore_errors=True)

    # local actions (sender + receiver)
    def action(self, tid: str, action: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        row = self.db.transfer(tid)
        if not row:
            raise DropError("not_found", "Transfer not found.", 404)
        api = self.transfer_api(row)
        if action not in api["actions"] and not (action == "accept" and row["direction"] == "in"
                                                  and row["status"] in ("awaiting_accept", "insufficient_space")):
            # Idempotent: repeating an action that already took effect returns the current state.
            already = {"pause": "paused", "resume": "connecting", "cancel": "cancelled", "reject": "rejected"}
            if already.get(action) == row["status"] or (action == "resume" and row["status"] in ACTIVE_OUT):
                return api
            raise DropError("invalid_action", f"Cannot {action} a transfer that is {row['status']}.", 409)
        if action in ("open", "reveal"):
            return self.open_received(row, action, launch=not body.get("delegate"))
        handler = getattr(self, f"_act_{action}")
        result = handler(row, body)
        return result if isinstance(result, dict) and "transfer_id" in result else self.transfer_api(
            self.db.transfer(tid))  # type: ignore[arg-type]

    def _act_pause(self, row: dict[str, Any], body: dict[str, Any]) -> None:
        tid = row["transfer_id"]
        self._status(tid, "paused", paused=1)
        self._stop_worker(tid)
        self._pool.submit(self._notify_state, tid, "paused")

    def _notify_state(self, tid: str, state: str) -> None:
        row = self.db.transfer(tid)
        probe = self.dir.cached(row["peer_id"]) if row else None
        if not row or not probe or not probe.online:
            return
        try:
            self._status_call(probe.endpoint, row, state)
        except Exception:
            pass

    def _act_resume(self, row: dict[str, Any], body: dict[str, Any]) -> None:
        tid = row["transfer_id"]
        self._status(tid, "connecting", paused=0)
        self._start_worker(tid)

    def _act_retry(self, row: dict[str, Any], body: dict[str, Any]) -> None:
        tid = row["transfer_id"]
        if row["status"] in ("verify_failed", "failed"):
            self._retry_receiver.add(tid)
        self.dir.forget(row["peer_id"])
        self._status(tid, "connecting", paused=0)
        self._start_worker(tid)

    def _act_cancel(self, row: dict[str, Any], body: dict[str, Any]) -> None:
        tid = row["transfer_id"]
        self._status(tid, "cancelled", paused=0)
        if row["direction"] == "out":
            self._stop_worker(tid)
            self._cleanup_uploads(tid)
        else:
            self._discard_staging(row)
        self.db.set_setting(f"cancel_notice:{tid}", "1")
        self._pool.submit(self._send_cancel_notice, tid)

    def _send_cancel_notice(self, tid: str) -> None:
        row = self.db.transfer(tid)
        if not row:
            return
        if row["direction"] == "out" and not row["offer_delivered"]:
            self.db.set_setting(f"cancel_notice:{tid}", "")
            return
        probe = self.dir.probe(row["peer_id"])
        if not probe.online:
            return
        try:
            self._call(probe.endpoint, "cancel", row["peer_id"], {"transfer_id": tid}, timeout=10)
            self.db.set_setting(f"cancel_notice:{tid}", "")
        except _PeerRefused:
            self.db.set_setting(f"cancel_notice:{tid}", "")
        except Exception:
            pass

    def _retry_cancel_notices(self) -> None:
        for row in self.db.transfers_in(("cancelled",)):
            if self.db.setting(f"cancel_notice:{row['transfer_id']}") == "1":
                self._pool.submit(self._send_cancel_notice, row["transfer_id"])

    def _act_send_again(self, row: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        files = self.db.files(row["transfer_id"])
        tops: list[str] = []
        for f in files:
            source = Path(f["source_path"])
            parts = f["rel_path"].split("/")
            top = source.parents[len(parts) - 2] if len(parts) > 1 else source
            if str(top) not in tops:
                tops.append(str(top))
        return self.create_transfer({"peer_id": row["peer_id"], "paths": tops, "note": row["note"]})

    # receiver local actions
    def _act_accept(self, row: dict[str, Any], body: dict[str, Any]) -> None:
        tid = row["transfer_id"]
        save_dir = str(body.get("save_dir") or row["save_dir"] or self.receive_dir()).strip()
        if not Path(save_dir).is_absolute():
            raise DropError("invalid_dir", "Choose an absolute folder path.", 400)
        files = self.db.files(tid)
        committed = sum(int(f["committed"]) for f in files)
        if committed and row["save_dir"] and os.path.normcase(save_dir) != os.path.normcase(row["save_dir"]):
            raise DropError("location_locked", "Received data already lives in the original folder.", 409)
        try:
            Path(save_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DropError("invalid_dir", f"Folder cannot be created: {exc}", 400) from exc
        if body.get("auto_accept") is not None:
            self.db.set_prefs(row["peer_id"], auto_accept=int(bool(body.get("auto_accept"))))
        if row["needs_verification"]:
            self.verify_peer(row["peer_id"], True)
        need = int(row["total_bytes"]) - committed
        free = free_bytes(save_dir)
        if free is not None and free < need + SPACE_MARGIN:
            self._status(tid, "insufficient_space", save_dir=save_dir, error_code="insufficient_space",
                         error_message="Not enough free space in the chosen folder.", error_need=need, error_free=free)
            return
        staging = Path(save_dir) / ".ryn-drop" / tid
        staging.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.kernel32.SetFileAttributesW(str(staging.parent), 0x2)  # hidden
            except Exception:
                pass
        for f in files:
            part = staging / f"{f['file_id']}.part"
            if not part.exists():
                part.touch()
        self._status(tid, "transferring", save_dir=save_dir, staging_dir=str(staging))
        self.emit_peer(row["peer_id"])

    def _act_reject(self, row: dict[str, Any], body: dict[str, Any]) -> None:
        self._status(row["transfer_id"], "rejected")
        self._discard_staging(row)
        self.emit_peer(row["peer_id"])

    def open_received(self, row: dict[str, Any], how: str, *, launch: bool = True) -> dict[str, Any]:
        """Resolves what open/reveal should show; launches it unless the desktop shell will.

        A multi-file transfer lands in the receive folder itself: "open" shows that
        folder and "reveal" selects the first received file instead of the folder.
        """
        target = Path(row["final_path"]) if row["final_path"] else None
        if row["kind"] == "files":
            received = [Path(f["final_path"]) for f in self.db.files(row["transfer_id"]) if f["final_path"]]
            existing = [p for p in received if p.exists()]
            if not existing:
                raise DropError("not_found", "The received files were moved or deleted.", 404)
            if how == "reveal" or target is None or not target.is_dir():
                target, how = existing[0], "reveal"
        if target is None or not target.exists():
            raise DropError("not_found", "The received file was moved or deleted.", 404)
        if how == "open" and _launchable(target):
            how = "reveal"
        if launch:
            _open_path(str(target), reveal=how == "reveal")
        return {"ok": True, "path": str(target), "how": how}

    def _discard_staging(self, row: dict[str, Any]) -> None:
        if row["staging_dir"]:
            shutil.rmtree(row["staging_dir"], ignore_errors=True)
            try:
                Path(row["staging_dir"]).parent.rmdir()
            except OSError:
                pass

    # ---- incoming peer requests ---------------------------------------------
    def _respond(self, peer_id: str, payload: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        return codec.sign_response({**payload, "request_nonce": request["nonce"]}, me=self.me, to=peer_id,
                                   private_key_bytes=self.node.private_key_bytes)

    def _remote_state(self, row: dict[str, Any]) -> dict[str, Any]:
        tid = row["transfer_id"]
        files = {f["file_id"]: {"committed": json_bytes(f["committed"]), "status": f["status"]}
                 for f in self.db.pending_files(tid, 50)}
        out: dict[str, Any] = {"transfer_id": tid, "status": row["status"], "pending": files,
                               "committed_total": json_bytes(self.db.committed_total(tid))}
        if row["error_code"]:
            out["error"] = {"code": row["error_code"], "message": row["error_message"]}
        if row["status"] == "insufficient_space":
            out["need_bytes"] = json_bytes(row["error_need"] or 0)
            out["free_bytes"] = json_bytes(row["error_free"] or 0)
        return out

    def handle_offer(self, envelope: Any, source_ip: str | None) -> dict[str, Any]:
        payload = codec.open_envelope(envelope, kind="offer", me=self.me)
        sender = payload["from"]
        relation = self._authorize(sender, source_ip)
        tid = str(payload.get("transfer_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", tid):
            raise DropError("bad_request", "invalid transfer id", 400)
        existing = self.db.transfer(tid)
        if existing:
            if existing["peer_id"] != sender or existing["direction"] != "in":
                raise DropError("conflict", "transfer id in use", 409)
            return self._respond(sender, self._remote_state(existing), payload)
        secret = json.loads(codec.open_from(self.msg_priv, str(payload.get("from_pub") or ""), payload["sealed"],
                                            codec.OFFER_INFO))
        manifest = secret["manifest"]
        kind = manifest.get("kind")
        if kind not in ("file", "files", "folder"):
            raise DropError("bad_request", "invalid kind", 400)
        files = []
        seen: set[str] = set()
        seen_ids: set[str] = set()
        for i, item in enumerate(manifest.get("files") or []):
            rel = safe_rel_path(item.get("rel_path"))
            if rel.lower() in seen:
                raise DropError("bad_request", "duplicate file path", 400)
            seen.add(rel.lower())
            fid = str(item.get("file_id") or "")
            if not re.fullmatch(r"f\d{6}", fid) or fid in seen_ids:
                raise DropError("bad_request", "invalid file id", 400)
            seen_ids.add(fid)
            files.append({"file_id": fid, "idx": i, "rel_path": rel, "size": codec.parse_bytes(item.get("size"))})
        if not files or len(files) > MAX_FILES:
            raise DropError("bad_request", "invalid file list", 400)
        for rel in seen:
            parts = rel.split("/")
            if any("/".join(parts[:i]) in seen for i in range(1, len(parts))):
                raise DropError("bad_request", "file conflicts with a parent folder", 400)
        total = sum(f["size"] for f in files)
        if total != codec.parse_bytes(manifest.get("total_bytes")):
            raise DropError("bad_request", "size mismatch", 400)
        needs_verification = self._needs_verification(sender, relation)
        record = {
            "transfer_id": tid,
            "offer_id": str(payload.get("offer_id") or new_id())[:64],
            "peer_id": sender,
            "direction": "in",
            "kind": kind,
            "name": safe_rel_path(str(manifest.get("name") or files[0]["rel_path"]).split("/")[0]),
            "file_count": len(files),
            "total_bytes": total,
            "status": "awaiting_accept",
            "route": "lan" if private_source(source_ip) else "public",
            "note": str(secret.get("note") or "")[:2000],
            "save_dir": self.receive_dir(),
            "eph_pub": str(secret["eph_pub"]),
            "master_key": codec.receiver_master_key(self.msg_priv, str(secret["eph_pub"]), tid),
            "needs_verification": int(needs_verification),
        }
        self.db.insert_transfer(record, files)
        row = self.db.transfer(tid)
        self.emit_transfer(row)
        if not needs_verification and self.db.prefs(sender).get("auto_accept"):
            try:
                self._act_accept(row, {})  # type: ignore[arg-type]
            except DropError:
                pass
            row = self.db.transfer(tid)
        self.emit_peer(sender, force=True)
        return self._respond(sender, self._remote_state(row), payload)  # type: ignore[arg-type]

    def _incoming(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = self.db.transfer(str(payload.get("transfer_id") or ""))
        if not row or row["direction"] != "in" or row["peer_id"] != payload["from"]:
            return {}
        return row

    def handle_status(self, envelope: Any, source_ip: str | None) -> dict[str, Any]:
        payload = codec.open_envelope(envelope, kind="status", me=self.me)
        sender = payload["from"]
        self._authorize(sender, source_ip)
        row = self._incoming(payload)
        if not row:
            return self._respond(sender, {"transfer_id": payload.get("transfer_id"), "status": "unknown"}, payload)
        tid = row["transfer_id"]
        state = payload.get("sender_state")
        if payload.get("retry") and row["status"] in ("verify_failed", "failed"):
            row = self._status(tid, "transferring")
        elif state == "paused" and row["status"] == "transferring":
            row = self._status(tid, "paused")
        elif state == "transferring" and row["status"] == "paused":
            row = self._status(tid, "transferring")
        elif row["status"] == "insufficient_space" and row["staging_dir"]:
            # Space may have been freed since; re-check without the user.
            committed = sum(int(f["committed"]) for f in self.db.files(tid))
            need = int(row["total_bytes"]) - committed
            free = free_bytes(row["save_dir"])
            if free is not None and free >= need + SPACE_MARGIN:
                row = self._status(tid, "transferring")
        return self._respond(sender, self._remote_state(row), payload)

    def handle_cancel(self, envelope: Any, source_ip: str | None) -> dict[str, Any]:
        payload = codec.open_envelope(envelope, kind="cancel", me=self.me)
        sender = payload["from"]
        self._authorize(sender, source_ip)
        row = self.db.transfer(str(payload.get("transfer_id") or ""))
        if row and row["peer_id"] == sender and row["status"] not in FINAL:
            self._status(row["transfer_id"], "cancelled")
            if row["direction"] == "in":
                self._discard_staging(row)
            else:
                self._stop_worker(row["transfer_id"])
                self._cleanup_uploads(row["transfer_id"])
            self.emit_peer(sender)
        return self._respond(sender, {"ok": True}, payload)

    def handle_complete(self, envelope: Any, source_ip: str | None) -> dict[str, Any]:
        payload = codec.open_envelope(envelope, kind="complete", me=self.me)
        sender = payload["from"]
        self._authorize(sender, source_ip)
        row = self._incoming(payload)
        if not row:
            return self._respond(sender, {"transfer_id": payload.get("transfer_id"), "status": "unknown"}, payload)
        roots = payload.get("roots") or {}
        if row["status"] in ("transferring", "paused"):
            files = self.db.files(row["transfer_id"])
            if all(int(f["committed"]) == int(f["size"]) for f in files):
                for f in files:
                    root = str(roots.get(f["file_id"]) or "")
                    if not re.fullmatch(r"[0-9a-f]{64}", root):
                        raise DropError("bad_request", "missing root", 400)
                    self.db.update_file(row["transfer_id"], f["file_id"], root=root)
                row = self._status(row["transfer_id"], "verifying")
                self._start_verify(row["transfer_id"])
        return self._respond(sender, self._remote_state(row), payload)

    # data stream
    def begin_put(self, tid: str, fid: str, offset: int, salt_hex: str, auth: str, sender: str,
                  source_ip: str | None) -> "_Writer":
        self._authorize(sender, source_ip)
        row = self.db.transfer(tid)
        if not row or row["direction"] != "in" or row["peer_id"] != sender:
            raise DropError("not_found", "unknown transfer", 404)
        if row["status"] not in ("transferring", "paused"):
            raise DropError("gone", f"transfer is {row['status']}", 410)
        f = self.db.file(tid, fid)
        if not f:
            raise DropError("not_found", "unknown file", 404)
        try:
            salt = bytes.fromhex(salt_hex)
        except ValueError as exc:
            raise DropError("bad_request", "invalid salt", 400) from exc
        if len(salt) != 4 or not codec.check_put_auth(row["master_key"], tid, fid, offset, salt, auth):
            raise DropError("forbidden", "invalid stream authorisation", 403)
        if row["status"] == "paused":
            row = self._status(tid, "transferring")
        slot = self._slots.setdefault((tid, fid), _WriteSlot())
        return _Writer(self, row, f, offset, salt, slot)

    def _on_commit(self, tid: str) -> None:
        total = self.db.committed_total(tid)
        self._rate(tid).update(total)
        self._set(tid, confirmed_bytes=total, force=False)

    def _on_disk_full(self, tid: str, save_dir: str) -> None:
        row = self.db.transfer(tid)
        if not row:
            return
        committed = sum(int(f["committed"]) for f in self.db.files(tid))
        self._status(tid, "insufficient_space", error_code="insufficient_space",
                     error_message="The disk filled up while receiving.",
                     error_need=int(row["total_bytes"]) - committed, error_free=free_bytes(save_dir) or 0)

    # verification
    def _start_verify(self, tid: str) -> None:
        with self._lock:
            if tid in self._verifying:
                return
            self._verifying.add(tid)
        threading.Thread(target=self._verify, args=(tid,), daemon=True, name=f"drop-verify-{tid[:8]}").start()

    def _verify(self, tid: str) -> None:
        try:
            row = self.db.transfer(tid)
            if not row or row["status"] != "verifying":
                return
            staging = Path(row["staging_dir"])
            files = self.db.files(tid)
            for f in files:
                fid, size = f["file_id"], int(f["size"])
                hashes = self.db.chunks(tid, fid)
                expected = (size + CHUNK_BYTES - 1) // CHUNK_BYTES
                part = staging / f"{fid}.part"
                # Publication may have succeeded just before the process died.
                # Its journaled destination must be verified again on recovery.
                if not part.exists() and f["final_path"]:
                    part = Path(f["final_path"])
                bad: int | None = None
                if len(hashes) < expected or not part.exists() or part.stat().st_size != size:
                    bad = min(len(hashes), expected)
                else:
                    with open(part, "rb") as fh:
                        for idx in range(expected):
                            digest = hashlib.sha256()
                            left = min(CHUNK_BYTES, size - idx * CHUNK_BYTES)
                            while left > 0:
                                data = fh.read(min(4 * FRAME_PLAINTEXT_BYTES, left))
                                if not data:
                                    break
                                digest.update(data)
                                left -= len(data)
                            if left or digest.hexdigest() != hashes[idx]:
                                bad = idx
                                break
                            if self._stop.is_set():
                                return
                    if bad is None and codec.chunk_root(hashes[:expected]) != f["root"]:
                        bad = 0
                if bad is not None:
                    if part.parent != staging:
                        self._status(tid, "failed", error_code="published_file_changed",
                                     error_message="A published file changed during recovery; it was left untouched.")
                        return
                    keep = bad * CHUNK_BYTES
                    self.db.drop_chunks_from(tid, fid, bad)
                    self.db.update_file(tid, fid, committed=keep, status="pending")
                    try:
                        with open(part, "r+b") as fh:
                            fh.truncate(keep)
                    except OSError:
                        pass
                    self._on_commit(tid)
                    self._status(tid, "verify_failed", error_code="verify_failed",
                                 error_message=f"Verification failed for {f['rel_path']}; damaged part will be resent.")
                    return
                self.db.update_file(tid, fid, status="verified")
            self._finalize(tid)
        except OSError as exc:
            self._status(tid, "failed", error_code="io_error", error_message=str(exc)[:300])
        finally:
            with self._lock:
                self._verifying.discard(tid)

    def _finalize(self, tid: str) -> None:
        row = self.db.transfer(tid)
        if not row:
            return
        save_dir = Path(row["save_dir"])
        staging = Path(row["staging_dir"])
        files = self.db.files(tid)
        tops: dict[str, Path] = {}
        taken: set[str] = set()
        for f in files:
            top = f["rel_path"].split("/")[0]
            if top not in tops:
                if f["final_path"]:
                    dest = Path(f["final_path"])
                    depth = len(f["rel_path"].split("/")) - 1
                    tops[top] = dest.parents[depth - 1] if depth else dest
                else:
                    dest = unique_path(save_dir, top, taken)
                    if any(g["rel_path"].startswith(top + "/") for g in files):
                        # Reserve this new directory exclusively. Existing folders
                        # (including symlinks) are never merged into.
                        while True:
                            try:
                                dest.mkdir()
                                break
                            except FileExistsError:
                                taken.add(dest.name)
                                dest = unique_path(save_dir, top, taken)
                    tops[top] = dest
                    taken.add(dest.name)
        # Journal every destination before moving the first byte. Otherwise a
        # restart after publishing one child can split a folder across two roots.
        for f in files:
            if not f["final_path"]:
                parts = f["rel_path"].split("/")
                dest = tops[parts[0]].joinpath(*parts[1:]) if len(parts) > 1 else tops[parts[0]]
                self.db.update_file(tid, f["file_id"], final_path=str(dest))
        files = self.db.files(tid)
        for f in files:
            dest = Path(f["final_path"])
            root = save_dir.resolve()
            if dest.is_symlink() or not dest.resolve().is_relative_to(root):
                raise OSError("The destination escaped the selected folder.")
            parent = dest.parent
            while parent != save_dir and parent != parent.parent:
                if parent.is_symlink():
                    raise OSError("A destination folder was replaced by a symbolic link.")
                parent = parent.parent
            part = staging / f"{f['file_id']}.part"
            if not part.exists() and dest.exists():
                # _verify has checked the published file on process recovery.
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                if os.name == "nt":
                    os.rename(part, dest)  # Windows rename refuses an existing target.
                else:
                    # Same filesystem: publish without overwriting, no extra copy.
                    if dest.exists() and os.path.samefile(part, dest):
                        pass  # crash after link but before unlink
                    else:
                        os.link(part, dest)
                    part.unlink()
            except FileExistsError as exc:
                raise OSError("The destination was created by another process; no file was overwritten.") from exc
            self.db.update_file(tid, f["file_id"], final_path=str(dest), status="completed")
        if row["kind"] == "folder" or row["kind"] == "file":
            final = str(next(iter(tops.values())))
        else:
            final = str(save_dir)
        self._discard_staging(row)
        self._status(tid, "completed", final_path=final, completed_at=now_iso(),
                     confirmed_bytes=int(row["total_bytes"]))
        self.emit_peer(row["peer_id"])

    # ---- messages ---------------------------------------------------------
    def send_message(self, body: dict[str, Any]) -> dict[str, Any]:
        client_id = str(body.get("client_id") or "").strip() or None
        if client_id:
            existing = self.db.message_by_client(client_id)
            if existing:
                return self.message_api(existing)
        peer_id = str(body.get("peer_id") or "")
        text = str(body.get("text") or "")
        if not text.strip():
            raise DropError("empty", "Message is empty.", 400)
        if len(text) > MAX_MESSAGE_CHARS:
            raise DropError("too_long", "Message is too long.", 400)
        meta = self.peer_meta(peer_id)
        if not meta["sendable"]:
            raise DropError(meta["reason"] or "not_paired", "This device cannot receive messages.", 403)
        row = self.db.insert_message({
            "message_id": new_id(),
            "peer_id": peer_id,
            "direction": "out",
            "kind": "link" if URL_RE.match(text.strip()) else "text",
            "text": text,
            "created_at": now_iso(),
            "status": "sending",
            "client_id": client_id,
        })
        self.emit_message(row)
        self.emit_peer(peer_id)
        self._pool.submit(self._deliver, row["message_id"])
        return self.message_api(row)

    def retry_message(self, message_id: str) -> dict[str, Any]:
        row = self.db.message(message_id)
        if not row or row["direction"] != "out":
            raise DropError("not_found", "Message not found.", 404)
        if row["status"] in ("failed", "waiting_online"):
            row = self.db.update_message(message_id, status="sending", error="")
            self.emit_message(row)
            self.dir.forget(row["peer_id"])
            self._pool.submit(self._deliver, message_id)
        return self.message_api(row)

    def _flush_outbox(self) -> None:
        for row in self.db.outbox():
            self._pool.submit(self._deliver, row["message_id"])

    def _deliver(self, message_id: str) -> None:
        with self._lock:
            if message_id in self._delivering:
                return
            self._delivering.add(message_id)
        try:
            row = self.db.message(message_id)
            if not row or row["status"] not in ("sending", "waiting_online"):
                return
            peer_id = row["peer_id"]
            probe = self.dir.probe(peer_id)
            if not probe.online:
                if row["status"] != "waiting_online":
                    self.emit_message(self.db.update_message(message_id, status="waiting_online"))
                return
            secret = json.dumps({"text": row["text"], "kind": row["kind"], "created_at": row["created_at"],
                                 "sender_name": self.node_name()}).encode("utf-8")
            payload = {"message_id": message_id, "from_pub": self.msg_pub,
                       "sealed": codec.seal_for(self.msg_priv, probe.messaging_pub, secret, codec.MESSAGE_INFO)}
            try:
                self._call(probe.endpoint, "message", peer_id, payload, timeout=15)
            except _PeerRefused as exc:
                self.emit_message(self.db.update_message(message_id, status="failed", error=exc.detail[:200],
                                                         attempts=int(row["attempts"]) + 1))
                return
            except (ConnectionError, DropProtocolError, OSError) as exc:
                self.dir.forget(peer_id)
                self.emit_message(self.db.update_message(message_id, status="waiting_online", error=str(exc)[:200],
                                                         attempts=int(row["attempts"]) + 1))
                return
            self.emit_message(self.db.update_message(message_id, status="delivered", delivered_at=now_iso(),
                                                     error=""))
        finally:
            with self._lock:
                self._delivering.discard(message_id)

    def handle_message(self, envelope: Any, source_ip: str | None) -> dict[str, Any]:
        payload = codec.open_envelope(envelope, kind="message", me=self.me)
        sender = payload["from"]
        relation = self._authorize(sender, source_ip)
        message_id = str(payload.get("message_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", message_id):
            raise DropError("bad_request", "invalid message id", 400)
        existing = self.db.message(message_id)
        if existing:
            if existing["peer_id"] != sender:
                raise DropError("conflict", "message id in use", 409)
            return self._respond(sender, {"ok": True, "message_id": message_id}, payload)
        secret = json.loads(codec.open_from(self.msg_priv, str(payload.get("from_pub") or ""), payload["sealed"],
                                            codec.MESSAGE_INFO))
        text = str(secret.get("text") or "")[:MAX_MESSAGE_CHARS]
        if not text:
            raise DropError("bad_request", "empty message", 400)
        kind = "link" if URL_RE.match(text.strip()) else "text"
        row = self.db.insert_message({
            "message_id": message_id,
            "peer_id": sender,
            "direction": "in",
            "kind": kind,
            "text": text,
            "created_at": now_iso(),
            "status": "delivered",
            "delivered_at": now_iso(),
            "held": int(self._needs_verification(sender, relation)),
        })
        self.emit_message(row)
        self.emit_peer(sender, force=True)
        return self._respond(sender, {"ok": True, "message_id": message_id}, payload)

    # ---- browser uploads ---------------------------------------------------
    def upload_target(self, batch: str, rel: str) -> tuple[Path, str]:
        if not re.fullmatch(r"[0-9a-f]{8,64}", batch or ""):
            raise DropError("bad_request", "batch must be hex", 400)
        try:
            rel = safe_rel_path(rel)
        except DropProtocolError as exc:
            raise DropError("bad_request", str(exc), 400) from exc
        target = self.uploads_root / batch / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        return target, rel


class _Writer:
    """Receiver side of one PUT: decrypt, write at the committed offset, commit chunks.

    A completed 8 MiB chunk is fsynced and committed on a dedicated thread while
    the next chunk is already being written, so the disk is never idle between
    chunks. "Committed" still means fsynced: the offset is recorded only after
    the fsync, at most one chunk is in flight, and finish/drain wait for it.
    """

    def __init__(self, engine: DropEngine, row: dict[str, Any], f: dict[str, Any], offset: int, salt: bytes,
                 slot: _WriteSlot) -> None:
        self.engine = engine
        self.tid = row["transfer_id"]
        self.fid = f["file_id"]
        self.size = int(f["size"])
        self.save_dir = row["save_dir"]
        self.slot = slot
        self.opener = codec.FrameOpener(codec.file_key(row["master_key"], self.fid), self.tid, self.fid, salt)
        part = Path(row["staging_dir"]) / f"{self.fid}.part"
        with slot.lock:
            slot.gen += 1
            self.gen = slot.gen
            current = engine.db.file(self.tid, self.fid)
            self.committed = int(current["committed"]) if current else 0
            if offset != self.committed:
                raise DropError("offset_mismatch", "offset does not match committed bytes", 409,
                                committed=json_bytes(self.committed))
            part.parent.mkdir(parents=True, exist_ok=True)
            if not part.exists():
                part.touch()
            self.fh = open(part, "r+b")
            self.fh.truncate(self.committed)
            self.fh.seek(self.committed)
        self.pos = self.committed
        self.idx = self.committed // CHUNK_BYTES
        self.digest = hashlib.sha256()
        self.closed = False
        self._syncer: ThreadPoolExecutor | None = None
        self._pending: Any = None

    def _durable(self, idx: int, digest: str, pos: int) -> None:
        try:
            os.fsync(self.fh.fileno())
        except OSError as exc:
            if _is_disk_full(exc):
                self.engine._on_disk_full(self.tid, self.save_dir)
                raise DropError("insufficient_space", "disk full", 507) from exc
            raise
        with self.slot.lock:
            # A newer stream for this file may have taken over (and truncated
            # to the old committed offset) meanwhile; never commit behind it.
            if self.slot.gen != self.gen:
                return
            self.engine.db.commit_chunk(self.tid, self.fid, idx, digest, pos)
            self.committed = pos
        row = self.engine.db.transfer(self.tid)
        if not row or row["status"] not in ("transferring", "paused"):
            raise DropError("gone", "transfer is no longer active", 410)
        self.engine._on_commit(self.tid)

    def _wait_pending(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None:
            pending.result()

    def drain(self) -> None:
        """Waits until every completed chunk is durable; raises its error."""
        self._wait_pending()

    def feed(self, frames: list[bytes]) -> None:
        with self.slot.lock:
            if self.slot.gen != self.gen:
                raise DropError("preempted", "a newer stream took over", 409)
            for ciphertext in frames:
                data = self.opener.open(self.pos, ciphertext)
                if self.pos + len(data) > self.size:
                    raise DropError("bad_request", "stream exceeds file size", 400)
                try:
                    self.fh.write(data)
                except OSError as exc:
                    if _is_disk_full(exc):
                        self.engine._on_disk_full(self.tid, self.save_dir)
                        raise DropError("insufficient_space", "disk full", 507) from exc
                    raise
                self.digest.update(data)
                self.pos += len(data)
                if self.pos % CHUNK_BYTES == 0 or self.pos == self.size:
                    try:
                        self.fh.flush()  # into the OS; the fsync happens on the syncer thread
                    except OSError as exc:
                        if _is_disk_full(exc):
                            self.engine._on_disk_full(self.tid, self.save_dir)
                            raise DropError("insufficient_space", "disk full", 507) from exc
                        raise
                    # Previous chunk first: commits stay in order, errors surface here.
                    self.slot.lock.release()
                    try:
                        self._wait_pending()
                    finally:
                        self.slot.lock.acquire()
                    if self.slot.gen != self.gen:
                        raise DropError("preempted", "a newer stream took over", 409)
                    if self._syncer is None:
                        self._syncer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="drop-fsync")
                    self._pending = self._syncer.submit(self._durable, self.idx, self.digest.hexdigest(), self.pos)
                    self.idx += 1
                    self.digest = hashlib.sha256()

    def finish(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._wait_pending()
        except Exception:
            pass  # the PUT already failed or ended; only the durable prefix counts
        finally:
            if self._syncer is not None:
                self._syncer.shutdown(wait=True)
        with self.slot.lock:
            try:
                if self.slot.gen == self.gen and self.pos != self.committed:
                    self.fh.truncate(self.committed)
            finally:
                self.fh.close()


def _launchable(path: Path) -> bool:
    if path.suffix.lower() in LAUNCHABLE_SUFFIXES:
        return True
    return os.name != "nt" and path.is_file() and os.access(path, os.X_OK)


def _open_path(path: str, *, reveal: bool) -> None:
    if not path or not Path(path).exists():
        raise DropError("not_found", "The received file was moved or deleted.", 404)
    import subprocess

    if sys.platform == "win32":
        if reveal:
            # explorer parses its own command line: the path must be quoted after
            # "/select," or names containing commas select the wrong item.
            subprocess.Popen(f'explorer.exe /select,"{path}"')
        else:
            os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path] if reveal else ["open", path])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent) if reveal else path])
