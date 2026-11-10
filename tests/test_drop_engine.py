"""End-to-end tests for device drop: two real nodes over loopback HTTP.

These run the real engine, real streaming PUTs, real SQLite state and real
files on disk. Nothing here simulates a successful transfer.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI

from rynmesh.drop import codec
from rynmesh.drop import engine as engine_mod
from rynmesh.drop.engine import CHUNK_BYTES, DropEngine, DropError, safe_rel_path
from rynmesh.drop.routes import install_drop
from rynmesh.drop.store import DropStore

MiB = 1024 * 1024


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeIdentity:
    """Only what the engine needs from the node store: identity + discovery."""

    def __init__(self, home: Path, name: str) -> None:
        key = Ed25519PrivateKey.generate()
        self.private_key_bytes = key.private_bytes_raw()
        self.peer_id = base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")
        self.node_name = name
        self.home = home
        self.records: dict[str, dict[str, Any]] = {}

    def discover_peers(self, network_id: str, include_self: bool = False, cache_only: bool = True) -> dict:
        return {"peers": list(self.records.values())}


class Node:
    def __init__(self, root: Path, name: str) -> None:
        self.home = root / name
        self.home.mkdir(parents=True)
        self.identity = FakeIdentity(self.home, name)
        self.msg_key = X25519PrivateKey.generate()
        self.port = free_port()
        self.own: dict[str, str] = {}
        self.engine: DropEngine | None = None
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.hook: Callable[[DropEngine], None] | None = None
        self.receive_dir = root / f"{name}-received"

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "Node":
        os.environ["RYNMESH_DROP_RECEIVE_DIR"] = str(self.receive_dir)
        engine = DropEngine(node=self.identity, home=self.home, messaging_key=self.msg_key,
                            network_id=lambda: "test", own_peers=lambda: dict(self.own), friend_peers=dict)
        engine.db.set_setting("receive_dir", str(self.receive_dir))
        if self.hook:
            self.hook(engine)
        app = FastAPI()
        install_drop(app, engine=engine, local_control=lambda request: None)
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        assert self.server.started
        engine.start()
        self.engine = engine
        return self

    def stop(self) -> None:
        if self.engine:
            self.engine.close()
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=10)
        self.engine = None

    @property
    def e(self) -> DropEngine:
        assert self.engine is not None
        return self.engine


def link(a: Node, b: Node, *, own: bool = True, lan: bool = False) -> None:
    for x, y in ((a, b), (b, a)):
        record = {"peer_id": y.identity.peer_id, "node_name": y.identity.node_name, "endpoints": [y.endpoint]}
        if lan:
            record["lan_endpoints"] = [y.endpoint]
        x.identity.records[y.identity.peer_id] = record
        if own:
            x.own[y.identity.peer_id] = y.identity.node_name


def wait_for(predicate: Callable[[], Any], timeout: float = 30.0, message: str = "condition") -> Any:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {message}; last={last!r}")


def status(node: Node, tid: str) -> str:
    row = node.e.db.transfer(tid)
    return row["status"] if row else ""


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(MiB), b""):
            digest.update(block)
    return digest.hexdigest()


def write_random(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        left = size
        while left:
            n = min(left, 4 * MiB)
            fh.write(os.urandom(n))
            left -= n
    return path


@pytest.fixture
def pair(tmp_path: Path):
    a = Node(tmp_path, "alpha").start()
    b = Node(tmp_path, "beta").start()
    link(a, b)
    yield a, b
    a.stop()
    b.stop()


@pytest.fixture
def slow_frames(monkeypatch):
    """Throttle frame sealing so pause/restart can be exercised mid-stream."""

    original = codec.FrameSealer.seal
    delay = {"s": 0.03}

    def seal(self, offset, plaintext):
        time.sleep(delay["s"])
        return original(self, offset, plaintext)

    monkeypatch.setattr(codec.FrameSealer, "seal", seal)
    return delay


# ---- unit ----------------------------------------------------------------------


def test_safe_rel_path_rejects_traversal_and_cleans_names():
    for bad in ["../x", "a/../../b", "/etc/passwd", "C:/Windows/x", "a//b", "", "a/./b"]:
        with pytest.raises(codec.DropProtocolError):
            safe_rel_path(bad)
    assert safe_rel_path("dir\\sub\\file.txt") == "dir/sub/file.txt"
    assert safe_rel_path("CON.txt") == "_CON.txt"
    assert safe_rel_path('we<ird>:name?.txt ') == "we_ird__name_.txt"


def test_byte_values_and_frames_beyond_32_bits(tmp_path: Path):
    hundred_gb = 100 * 1000**3
    huge = 2**60
    assert codec.json_bytes(hundred_gb) == hundred_gb
    assert codec.json_bytes(huge) == str(huge)
    assert codec.parse_bytes(str(huge)) == huge
    key = os.urandom(32)
    salt = os.urandom(4)
    offset = 5 * 1024**3 + 12345  # past 4 GiB
    frame = codec.FrameSealer(key, "t", "f", salt).seal(offset, b"payload")
    assert codec.FrameOpener(key, "t", "f", salt).open(offset, frame[4:]) == b"payload"
    with pytest.raises(codec.DropProtocolError):
        codec.FrameOpener(key, "t", "f", salt).open(offset + 1, frame[4:])
    store = DropStore(tmp_path)
    store.insert_transfer({"transfer_id": "t1", "offer_id": "o", "peer_id": "p", "direction": "in", "kind": "file",
                           "name": "big.bin", "file_count": 1, "total_bytes": hundred_gb, "status": "transferring"},
                          [{"file_id": "f000000", "idx": 0, "rel_path": "big.bin", "size": hundred_gb}])
    store.commit_chunk("t1", "f000000", (hundred_gb // CHUNK_BYTES) - 1, "00" * 32, hundred_gb - CHUNK_BYTES)
    assert store.committed_total("t1") == hundred_gb - CHUNK_BYTES
    assert store.transfer("t1")["total_bytes"] == hundred_gb
    store.close()


# ---- messages ----------------------------------------------------------------------


def test_message_delivered_only_after_receiver_persisted(pair):
    a, b = pair
    msg = a.e.send_message({"peer_id": b.identity.peer_id, "text": "hello beta", "client_id": "c1"})
    assert msg["status"] == "sending"
    wait_for(lambda: a.e.db.message(msg["message_id"])["status"] == "delivered", message="delivered")
    items = b.e.conversation(a.identity.peer_id)["items"]
    assert [i["message"]["text"] for i in items] == ["hello beta"]
    assert b.e.peer_meta(a.identity.peer_id)["unread"] == 1
    b.e.mark_read(a.identity.peer_id)
    assert b.e.peer_meta(a.identity.peer_id)["unread"] == 0
    # idempotent on client_id
    again = a.e.send_message({"peer_id": b.identity.peer_id, "text": "hello beta", "client_id": "c1"})
    assert again["message_id"] == msg["message_id"]
    link_msg = a.e.send_message({"peer_id": b.identity.peer_id, "text": "https://example.com/a?b=1", "client_id": "c2"})
    assert link_msg["kind"] == "link"


def test_message_waits_for_offline_peer_then_delivers(tmp_path: Path):
    a = Node(tmp_path, "alpha").start()
    b = Node(tmp_path, "beta")
    try:
        link(a, b)
        msg = a.e.send_message({"peer_id": b.identity.peer_id, "text": "queued", "client_id": "q1"})
        wait_for(lambda: a.e.db.message(msg["message_id"])["status"] == "waiting_online", message="waiting")
        assert a.e.peer_meta(b.identity.peer_id)["send_mode"] == "queue"
        b.start()
        link(a, b)
        a.e.dir.forget(b.identity.peer_id)
        wait_for(lambda: a.e.db.message(msg["message_id"])["status"] == "delivered", timeout=30, message="flush")
        assert b.e.conversation(a.identity.peer_id)["items"][0]["message"]["text"] == "queued"
    finally:
        a.stop()
        b.stop()


def test_duplicate_message_envelope_is_stored_once(pair):
    a, b = pair
    probe = a.e.dir.probe(b.identity.peer_id)
    secret = b'{"text": "once", "kind": "text"}'
    payload = {"message_id": codec.new_id(), "from_pub": a.e.msg_pub,
               "sealed": codec.seal_for(a.msg_key, probe.messaging_pub, secret, codec.MESSAGE_INFO)}
    env = codec.make_envelope("message", payload, sender=a.identity.peer_id, to=b.identity.peer_id,
                              private_key_bytes=a.identity.private_key_bytes)
    b.e.handle_message(env, "127.0.0.1")
    b.e.handle_message(env, "127.0.0.1")
    assert len(b.e.db.messages(a.identity.peer_id)) == 1


def test_unpaired_device_is_refused(tmp_path: Path):
    a = Node(tmp_path, "alpha").start()
    b = Node(tmp_path, "beta").start()
    try:
        link(a, b, own=False)  # discovered via registry only, no relationship
        with pytest.raises(DropError) as err:
            a.e.send_message({"peer_id": b.identity.peer_id, "text": "hi"})
        assert err.value.status == 403
        env = codec.make_envelope("status", {"transfer_id": "x"}, sender=a.identity.peer_id,
                                  to=b.identity.peer_id, private_key_bytes=a.identity.private_key_bytes)
        with pytest.raises(DropError):
            b.e.handle_status(env, "127.0.0.1")
    finally:
        a.stop()
        b.stop()


def test_nearby_device_messages_are_held_until_verified(tmp_path: Path):
    a = Node(tmp_path, "alpha").start()
    b = Node(tmp_path, "beta").start()
    try:
        link(a, b, own=False, lan=True)
        meta = a.e.peer_meta(b.identity.peer_id)
        assert meta["relationship"] == "nearby" and meta["send_mode"] == "verify"
        assert meta["verification"]["code"] == b.e.peer_meta(a.identity.peer_id)["verification"]["code"]
        msg = a.e.send_message({"peer_id": b.identity.peer_id, "text": "nearby hello"})
        wait_for(lambda: a.e.db.message(msg["message_id"])["status"] == "delivered")
        assert b.e.conversation(a.identity.peer_id)["items"] == []
        assert b.e.peer_meta(a.identity.peer_id)["verification"]["required"] is True
        b.e.verify_peer(a.identity.peer_id, True)
        assert b.e.conversation(a.identity.peer_id)["items"][0]["message"]["text"] == "nearby hello"
    finally:
        a.stop()
        b.stop()


# ---- transfers ------------------------------------------------------------------------


def test_folder_transfer_verifies_and_never_overwrites(pair, tmp_path: Path):
    a, b = pair
    src = tmp_path / "src" / "Project"
    write_random(src / "big.bin", 20 * MiB + 123)
    write_random(src / "sub" / "deep" / "small.txt", 1000)
    (src / "empty.txt").write_bytes(b"")
    pre = a.e.preflight({"peer_id": b.identity.peer_id, "paths": [str(src)]})
    assert pre["ok"] and pre["file_count"] == 3 and pre["items"][0]["kind"] == "folder"

    def send_once() -> Path:
        t = a.e.create_transfer({"peer_id": b.identity.peer_id, "paths": [str(src)], "client_id": codec.new_id()})
        tid = t["transfer_id"]
        assert t["kind"] == "folder" and t["file_count"] == 3
        wait_for(lambda: status(b, tid) == "awaiting_accept", message="offer")
        assert status(a, tid) in ("awaiting_accept", "connecting")
        b.e.action(tid, "accept", {})
        wait_for(lambda: status(a, tid) == "completed", timeout=60, message="sender completed")
        assert status(b, tid) == "completed"
        final = Path(b.e.db.transfer(tid)["final_path"])
        assert a.e.db.transfer(tid)["confirmed_bytes"] == a.e.db.transfer(tid)["total_bytes"]
        assert not (b.receive_dir / ".ryn-drop" / tid).exists()
        return final

    first = send_once()
    assert first == b.receive_dir / "Project"
    for rel in ["big.bin", "sub/deep/small.txt", "empty.txt"]:
        assert sha(first / rel) == sha(src / rel)
    second = send_once()
    assert second == b.receive_dir / "Project (1)"
    assert sha(second / "big.bin") == sha(src / "big.bin")


def test_pause_resume_and_sender_restart_resume_from_committed(tmp_path: Path, slow_frames, monkeypatch):
    a = Node(tmp_path, "alpha").start()
    b = Node(tmp_path, "beta").start()
    offsets: list[int] = []
    original_auth = codec.put_auth

    def record(master, tid, fid, offset, salt):
        offsets.append(offset)
        return original_auth(master, tid, fid, offset, salt)

    monkeypatch.setattr(codec, "put_auth", record)
    original_check = codec.check_put_auth

    def check(master, tid, fid, offset, salt, value):
        import hmac as _hmac

        return _hmac.compare_digest(original_auth(master, tid, fid, offset, salt), str(value or ""))

    monkeypatch.setattr(codec, "check_put_auth", check)
    assert original_check is not check
    try:
        link(a, b)
        src = write_random(tmp_path / "src" / "movie.mkv", 48 * MiB)
        t = a.e.create_transfer({"peer_id": b.identity.peer_id, "paths": [str(src)], "client_id": "p1"})
        tid = t["transfer_id"]
        wait_for(lambda: status(b, tid) == "awaiting_accept")
        b.e.action(tid, "accept", {})
        wait_for(lambda: b.e.db.committed_total(tid) >= CHUNK_BYTES, message="first chunk")
        paused = a.e.action(tid, "pause")
        assert paused["status"] == "paused" and "resume" in paused["actions"]
        time.sleep(1.0)
        held = b.e.db.committed_total(tid)
        assert held % CHUNK_BYTES == 0 and held > 0
        part = Path(b.e.db.transfer(tid)["staging_dir"]) / "f000000.part"
        assert part.stat().st_size == held  # partial tail trimmed back to the last fsynced chunk
        time.sleep(0.5)
        assert b.e.db.committed_total(tid) == held
        a.e.action(tid, "resume")
        wait_for(lambda: b.e.db.committed_total(tid) >= held + CHUNK_BYTES, message="resumed progress")
        # Restart the sender process state mid-stream.
        a.stop()
        before = b.e.db.committed_total(tid)
        a.start()
        wait_for(lambda: status(a, tid) == "completed", timeout=90, message="completed after restart")
        assert sha(Path(b.e.db.transfer(tid)["final_path"])) == sha(src)
        resumed = [o for o in offsets if o > 0]
        assert resumed and min(resumed) >= CHUNK_BYTES
        assert any(o >= before for o in resumed)
        assert offsets.count(0) == 1  # never restarted from scratch
    finally:
        a.stop()
        b.stop()


def test_receiver_restart_truncates_to_committed_and_resumes(tmp_path: Path, slow_frames):
    a = Node(tmp_path, "alpha").start()
    b = Node(tmp_path, "beta").start()
    try:
        link(a, b)
        src = write_random(tmp_path / "src" / "disk.img", 40 * MiB)
        t = a.e.create_transfer({"peer_id": b.identity.peer_id, "paths": [str(src)], "client_id": "r1"})
        tid = t["transfer_id"]
        wait_for(lambda: status(b, tid) == "awaiting_accept")
        b.e.action(tid, "accept", {})
        wait_for(lambda: b.e.db.committed_total(tid) >= CHUNK_BYTES)
        b.stop()
        wait_for(lambda: status(a, tid) in ("reconnecting", "waiting_online"), message="sender notices")
        b.start()
        a.e.dir.forget(b.identity.peer_id)
        a.e.action(tid, "retry")
        wait_for(lambda: status(a, tid) == "completed", timeout=90, message="completed")
        assert sha(Path(b.e.db.transfer(tid)["final_path"])) == sha(src)
    finally:
        a.stop()
        b.stop()


def test_source_change_stops_the_original_transfer(tmp_path: Path, slow_frames):
    a = Node(tmp_path, "alpha").start()
    b = Node(tmp_path, "beta").start()
    try:
        link(a, b)
        src = write_random(tmp_path / "src" / "notes.bin", 30 * MiB)
        t = a.e.create_transfer({"peer_id": b.identity.peer_id, "paths": [str(src)], "client_id": "s1"})
        tid = t["transfer_id"]
        wait_for(lambda: status(b, tid) == "awaiting_accept")
        b.e.action(tid, "accept", {})
        wait_for(lambda: b.e.db.committed_total(tid) >= CHUNK_BYTES)
        a.e.action(tid, "pause")
        time.sleep(0.5)
        with open(src, "ab") as fh:
            fh.write(b"changed")
        a.e.action(tid, "resume")
        wait_for(lambda: status(a, tid) == "source_changed", message="source_changed")
        assert a.e.transfer_api(a.e.db.transfer(tid))["actions"] == ["reselect"]
    finally:
        a.stop()
        b.stop()


def test_reject_and_cancel_reach_the_other_side(pair, tmp_path: Path):
    a, b = pair
    src = write_random(tmp_path / "src" / "a.bin", 1000)
    t1 = a.e.create_transfer({"peer_id": b.identity.peer_id, "paths": [str(src)], "client_id": "x1"})
    wait_for(lambda: status(b, t1["transfer_id"]) == "awaiting_accept")
    assert b.e.incoming_count() == 1
    b.e.action(t1["transfer_id"], "reject")
    wait_for(lambda: status(a, t1["transfer_id"]) == "rejected")
    assert a.e.transfer_api(a.e.db.transfer(t1["transfer_id"]))["actions"] == ["send_again"]
    again = a.e.action(t1["transfer_id"], "send_again")
    assert again["transfer_id"] != t1["transfer_id"]
    wait_for(lambda: status(b, again["transfer_id"]) == "awaiting_accept")
    a.e.action(again["transfer_id"], "cancel")
    wait_for(lambda: status(b, again["transfer_id"]) == "cancelled", message="receiver cancelled")
    assert b.e.incoming_count() == 0


def test_insufficient_space_is_reported_with_need_and_free(pair, tmp_path: Path, monkeypatch):
    a, b = pair
    src = write_random(tmp_path / "src" / "big.bin", 2 * MiB)
    monkeypatch.setattr(engine_mod, "free_bytes", lambda path: 1024)
    t = a.e.create_transfer({"peer_id": b.identity.peer_id, "paths": [str(src)], "client_id": "i1"})
    tid = t["transfer_id"]
    wait_for(lambda: status(b, tid) == "awaiting_accept")
    b.e.action(tid, "accept", {})
    rb = b.e.transfer_api(b.e.db.transfer(tid))
    assert rb["status"] == "insufficient_space" and rb["error"]["free_bytes"] == 1024
    assert rb["error"]["need_bytes"] == 2 * MiB
    wait_for(lambda: status(a, tid) == "insufficient_space", message="sender sees space")
    ra = a.e.transfer_api(a.e.db.transfer(tid))
    assert ra["error"]["need_bytes"] == 2 * MiB and set(ra["actions"]) == {"retry", "cancel"}
    monkeypatch.setattr(engine_mod, "free_bytes", lambda path: 10**12)
    b.e.action(tid, "accept", {})
    wait_for(lambda: status(a, tid) == "completed", message="completed after space freed")


def test_corruption_is_caught_by_verification_and_retry_repairs(tmp_path: Path):
    a = Node(tmp_path, "alpha").start()
    b = Node(tmp_path, "beta")
    corrupted = {"done": False}

    def hook(engine: DropEngine) -> None:
        original = engine.handle_complete

        def corrupt_then_complete(envelope, source_ip):
            row = engine.db.transfers(limit=1)[0]
            part = Path(row["staging_dir"]) / "f000000.part"
            if not corrupted["done"] and part.exists():
                with open(part, "r+b") as fh:
                    first = fh.read(1)
                    fh.seek(0)
                    fh.write(bytes([first[0] ^ 0xFF]))
                corrupted["done"] = True
            return original(envelope, source_ip)

        engine.handle_complete = corrupt_then_complete  # type: ignore[method-assign]

    b.hook = hook
    b.start()
    try:
        link(a, b)
        src = write_random(tmp_path / "src" / "data.bin", 12 * MiB)
        t = a.e.create_transfer({"peer_id": b.identity.peer_id, "paths": [str(src)], "client_id": "v1"})
        tid = t["transfer_id"]
        wait_for(lambda: status(b, tid) == "awaiting_accept")
        b.e.action(tid, "accept", {})
        wait_for(lambda: status(a, tid) == "verify_failed", timeout=60, message="verify_failed")
        assert status(b, tid) == "verify_failed"
        assert not (b.receive_dir / "data.bin").exists()
        a.e.action(tid, "retry")
        wait_for(lambda: status(a, tid) == "completed", timeout=60, message="repaired")
        assert sha(b.receive_dir / "data.bin") == sha(src)
    finally:
        a.stop()
        b.stop()


def test_events_stream_versions_never_go_backwards(pair, tmp_path: Path):
    a, b = pair
    start = a.e.seq
    src = write_random(tmp_path / "src" / "e.bin", 3 * MiB)
    t = a.e.create_transfer({"peer_id": b.identity.peer_id, "paths": [str(src)], "client_id": "ev1"})
    tid = t["transfer_id"]
    wait_for(lambda: status(b, tid) == "awaiting_accept")
    b.e.action(tid, "accept", {})
    wait_for(lambda: status(a, tid) == "completed")
    events = a.e.events_since(start) or []
    versions = [e["transfer"]["version"] for e in events if e["type"] == "transfer" and
                e["transfer"]["transfer_id"] == tid]
    assert versions == sorted(versions) and versions[-1] == a.e.db.transfer(tid)["version"]
    assert events[-1]["seq"] == a.e.seq
    assert a.e.events_since(a.e.seq + 5) is None  # unknown future seq -> resync


def test_signed_response_is_bound_to_request_nonce(pair, monkeypatch):
    a, b = pair
    stale = codec.sign_response({"ok": True, "request_nonce": "old-request"}, me=b.identity.peer_id,
                               to=a.identity.peer_id, private_key_bytes=b.identity.private_key_bytes)
    monkeypatch.setattr(a.e.dir, "request", lambda *args, **kwargs: (200, stale))
    with pytest.raises(codec.DropProtocolError, match="acknowledge"):
        a.e._call(b.endpoint, "message", b.identity.peer_id, {"message_id": codec.new_id()})


def test_browser_upload_and_create_route_use_real_bytes(pair):
    import httpx
    a, b = pair
    batch = codec.new_id()
    content = os.urandom(2 * MiB + 31)
    with httpx.Client(timeout=20) as client:
        result = client.put(a.endpoint + "/api/local/drop/uploads", params={"batch": batch, "rel": "folder/browser.bin"}, content=content)
        assert result.status_code == 200, result.text
        created = client.post(a.endpoint + "/api/local/drop/transfers", json={"peer_id": b.identity.peer_id,
            "upload_ids": [result.json()["upload_id"]], "client_id": batch})
        assert created.status_code == 200, created.text
        tid = created.json()["transfer_id"]
        wait_for(lambda: status(b, tid) == "awaiting_accept")
        accepted = client.post(b.endpoint + f"/api/local/drop/transfers/{tid}/accept", json={})
        assert accepted.status_code == 200, accepted.text
        wait_for(lambda: status(a, tid) == "completed")
        assert (b.receive_dir / "folder" / "browser.bin").read_bytes() == content


def test_finalize_recovers_a_partially_published_renamed_folder(pair, monkeypatch):
    _, b = pair
    tid = codec.new_id()
    staging = b.receive_dir / ".ryn-drop" / tid
    staging.mkdir(parents=True)
    (b.receive_dir / "Project").mkdir()
    files = [{"file_id": f"f{i:06d}", "idx": i, "rel_path": f"Project/{i}.txt", "size": 3, "committed": 3} for i in range(2)]
    b.e.db.insert_transfer({"transfer_id": tid, "offer_id": tid, "peer_id": "test-peer", "direction": "in",
        "kind": "folder", "name": "Project", "file_count": 2, "total_bytes": 6, "status": "verifying",
        "save_dir": str(b.receive_dir), "staging_dir": str(staging)}, files)
    for f in files:
        (staging / (f["file_id"] + ".part")).write_bytes(b"abc")
        digest = hashlib.sha256(b"abc").hexdigest()
        b.e.db.set_chunks(tid, f["file_id"], [(0, digest)])
        b.e.db.update_file(tid, f["file_id"], root=codec.chunk_root([digest]))
    original = b.e.db.update_file
    def interrupt_after_first_move(transfer_id, file_id, **values):
        if values.get("status") == "completed":
            raise OSError("simulated interruption after publishing first file")
        return original(transfer_id, file_id, **values)
    with monkeypatch.context() as patch:
        patch.setattr(b.e.db, "update_file", interrupt_after_first_move)
        with pytest.raises(OSError, match="interruption"):
            b.e._finalize(tid)
    assert (b.receive_dir / "Project (1)" / "0.txt").read_bytes() == b"abc"
    assert not (staging / "f000000.part").exists()
    # Recovery must hash the published child and put all remaining children in
    # the same journaled destination, never the pre-existing Project folder.
    b.e._verify(tid)
    assert status(b, tid) == "completed"
    assert (b.receive_dir / "Project (1)" / "1.txt").read_bytes() == b"abc"
    assert list((b.receive_dir / "Project").iterdir()) == []


# ---- opening received items -----------------------------------------------------------


def _receive(a: Node, b: Node, paths: list[Path]) -> str:
    t = a.e.create_transfer({"peer_id": b.identity.peer_id, "paths": [str(p) for p in paths], "client_id": codec.new_id()})
    tid = t["transfer_id"]
    wait_for(lambda: status(b, tid) == "awaiting_accept", message="offer")
    b.e.action(tid, "accept", {})
    wait_for(lambda: status(b, tid) == "completed", timeout=60, message="receiver completed")
    return tid


def test_open_never_launches_received_programs_and_delegate_only_resolves(pair, tmp_path: Path, monkeypatch):
    a, b = pair
    launched: list[tuple[str, bool]] = []
    monkeypatch.setattr(engine_mod, "_open_path", lambda path, *, reveal: launched.append((path, reveal)))
    doc = tmp_path / "src" / "notes.txt"
    doc.parent.mkdir(parents=True)
    doc.write_text("hello")
    setup = tmp_path / "src" / "Setup.EXE"
    setup.write_bytes(b"MZ")

    doc_tid = _receive(a, b, [doc])
    assert b.e.action(doc_tid, "open", {}) == {"ok": True, "path": str(b.receive_dir / "notes.txt"), "how": "open"}
    exe_tid = _receive(a, b, [setup])
    result = b.e.action(exe_tid, "open", {})
    assert result["how"] == "reveal" and launched[-1] == (str(b.receive_dir / "Setup.EXE"), True)

    count = len(launched)
    assert b.e.action(doc_tid, "reveal", {"delegate": True})["how"] == "reveal"
    assert len(launched) == count  # the desktop shell opens it, not the daemon


def test_multi_file_receive_opens_folder_and_reveals_a_file(pair, tmp_path: Path, monkeypatch):
    a, b = pair
    monkeypatch.setattr(engine_mod, "_open_path", lambda path, *, reveal: None)
    one, two = tmp_path / "src" / "one.txt", tmp_path / "src" / "two.txt"
    one.parent.mkdir(parents=True)
    one.write_text("1")
    two.write_text("2")
    tid = _receive(a, b, [one, two])
    assert b.e.db.transfer(tid)["kind"] == "files"
    assert b.e.action(tid, "open", {"delegate": True})["path"] == str(b.receive_dir)
    revealed = b.e.action(tid, "reveal", {"delegate": True})
    assert revealed["path"] in (str(b.receive_dir / "one.txt"), str(b.receive_dir / "two.txt"))

    (b.receive_dir / "one.txt").unlink()
    (b.receive_dir / "two.txt").unlink()
    with pytest.raises(DropError) as missing:
        b.e.action(tid, "reveal", {})
    assert missing.value.status == 404
