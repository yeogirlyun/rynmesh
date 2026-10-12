"""Immutable encrypted large tool outputs; references confer no resource access."""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from ..atomic_io import atomic_write_json, read_json
from ..services import peer_box
from .store import AgentError, encoded

MAX_RESULT_BYTES = 2 * 1024 * 1024
CHANNEL = b"rynmesh-agent-result-v1"


def path(store, task_id, result_id):
    if not isinstance(result_id, str) or not re.fullmatch(r"result_[a-f0-9]{32}", result_id):
        raise AgentError("agent_result_not_found")
    return store.root / "results" / hashlib.sha256(task_id.encode()).hexdigest() / (result_id + ".json")


def save(store, row, call, value):
    raw = encoded(value)
    if len(raw) > MAX_RESULT_BYTES:
        raise AgentError("agent_result_storage_limit")
    result_id = "result_" + uuid.uuid4().hex
    reference = {"id": result_id, "task_id": row["id"], "tool": call["name"], "operation_id": call["operation_id"],
                 "goal_revision": row["goal"]["revision"], "scope_revision": row["scope_revision"], "context_epoch": row.get("context_epoch", 0),
                 "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    nonce, ciphertext = peer_box.seal(store.key, store.pub, raw, info=CHANNEL)
    atomic_write_json(path(store, row["id"], result_id), {"nonce": nonce, "ciphertext": ciphertext}, max_bytes=3 * MAX_RESULT_BYTES)
    return reference


def read(store, row, result_id, *, provider=False):
    reference = next((ref for ref in row.get("result_refs", []) if ref["id"] == result_id), None)
    if reference is None:
        raise AgentError("agent_result_not_found")
    if provider and reference["context_epoch"] != row.get("context_epoch", 0):
        raise AgentError("agent_result_scope_changed")
    try:
        envelope = read_json(path(store, row["id"], result_id), max_bytes=3 * MAX_RESULT_BYTES)
        raw = peer_box.open_sealed(store.key, store.pub, envelope["nonce"], envelope["ciphertext"], info=CHANNEL)
        if len(raw) > MAX_RESULT_BYTES or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            raise ValueError
        return json.loads(raw), reference
    except Exception:
        raise AgentError("agent_result_unreadable") from None


def page(store, row, result_id, offset=0, limit=8192, *, provider=False):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 64 <= limit <= 16384:
        raise AgentError("agent_invalid_cursor")
    value, reference = read(store, row, result_id, provider=provider)
    text = encoded(value).decode()
    if offset > len(text):
        raise AgentError("agent_invalid_cursor")
    end = min(len(text), offset + limit)
    return {"id": result_id, "sha256": reference["sha256"], "bytes": reference["bytes"], "characters": len(text),
            "offset": offset, "next_offset": end if end < len(text) else None, "content": text[offset:end]}


def purge(store, task_id):
    root = (store.root / "results").resolve()
    directory = root / hashlib.sha256(task_id.encode()).hexdigest()
    if not directory.exists():
        return
    if not directory.resolve().is_relative_to(root):
        raise AgentError("agent_result_cleanup_failed")
    # Includes orphan ciphertext from a crash before its reference was committed.
    for file in directory.iterdir():
        if re.fullmatch(r"result_[a-f0-9]{32}\.json|\.result_[a-f0-9]{32}\.json\.[a-f0-9]{32}\.tmp", file.name):
            file.unlink()
