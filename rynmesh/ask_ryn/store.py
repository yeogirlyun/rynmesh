"""Encrypted owner history, separate from task and settlement metadata.

The product explicitly retains conversations. Reuse the node's existing
messaging key and atomic storage; never write conversation plaintext to disk.
Revision checks protect parallel views, tombstones prevent late writes from
resurrecting deleted history, and migration acknowledgements are transactional.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag

from ..atomic_io import atomic_write_json, read_json
from ..file_transactions import file_transaction
from ..services import peer_box

VERSION = "ryn.ask-history.v1"
CHANNEL = b"rynmesh-ask-history-v1"
MAX_BYTES = 16 * 1024 * 1024
MAX_PLAINTEXT = 11 * 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
FIELDS = ("id", "title", "serviceKey", "serviceName", "providerPeerId", "networkId", "createdAt", "updatedAt", "messages", "revision", "draft")
MESSAGE_FIELDS = ("id", "role", "content", "createdAt", "status", "taskId", "inputTokens", "outputTokens", "cost")


class ConversationError(ValueError):
    pass


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ConversationError("ask_invalid_conversation")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except ValueError:
        raise ConversationError("ask_invalid_conversation") from None
    return value


def _identity(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ConversationError("ask_invalid_conversation")
    return value


def clean_conversation(value: Mapping[str, Any]) -> dict[str, Any]:
    """Only explicit history fields cross the owner API (no arbitrary secrets)."""
    if not isinstance(value, dict):
        raise ConversationError("ask_invalid_conversation")
    result = {key: value[key] for key in FIELDS if key in value and key != "revision"}
    result["id"] = _identity(result.get("id"))
    for key, limit in (("title", 512), ("serviceKey", 2048), ("serviceName", 512), ("providerPeerId", 1024), ("networkId", 256)):
        text = result.get(key)
        if not isinstance(text, str) or not text.strip() or len(text) > limit or any(ord(char) < 32 for char in text):
            raise ConversationError("ask_invalid_conversation")
    if not result["serviceKey"].startswith(result["providerPeerId"] + "::") or result["serviceKey"] == result["providerPeerId"] + "::":
        raise ConversationError("ask_service_binding_mismatch")
    result["createdAt"] = _timestamp(result.get("createdAt"))
    result["updatedAt"] = _timestamp(result.get("updatedAt"))
    messages = result.get("messages")
    if not isinstance(messages, list) or len(messages) > 2000:
        raise ConversationError("ask_history_limit")
    cleaned, seen = [], set()
    for message in messages:
        if not isinstance(message, dict):
            raise ConversationError("ask_invalid_conversation")
        row = {key: message[key] for key in MESSAGE_FIELDS if key in message}
        row["id"] = _identity(row.get("id"))
        if row["id"] in seen or row.get("role") not in {"user", "assistant"} or row.get("status") not in {"complete", "failed", "cancelled", "queued", "running", "cancel_requested", "interrupted"}:
            raise ConversationError("ask_invalid_conversation")
        seen.add(row["id"])
        if not isinstance(row.get("content"), str) or len(row["content"].encode()) > 256 * 1024:
            raise ConversationError("ask_history_limit")
        row["createdAt"] = _timestamp(row.get("createdAt"))
        if "taskId" in row:
            _identity(row["taskId"])
        for key in ("inputTokens", "outputTokens", "cost"):
            if key in row and (type(row[key]) not in {int, float} or not math.isfinite(row[key]) or row[key] < 0):
                raise ConversationError("ask_invalid_conversation")
        cleaned.append(row)
    result["messages"] = cleaned
    if "draft" in result and (not isinstance(result["draft"], str) or len(result["draft"].encode()) > 64 * 1024):
        raise ConversationError("ask_history_limit")
    if len(_json(result)) > 1024 * 1024:
        raise ConversationError("ask_history_limit")
    return result


def public_conversation(value: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: value[key] for key in FIELDS if key in value}
    result["messages"] = [{key: message[key] for key in MESSAGE_FIELDS if key in message} for message in value["messages"]]
    return result


class ConversationStore:
    def __init__(self, root: str | Path, messaging_key: Any):
        self.root = Path(root)
        self.path = self.root / "history.json"
        self.lock = self.root / ".history.lock"
        self.key = messaging_key
        self.pub = peer_box.public_key_b64(messaging_key)

    def _read(self) -> tuple[dict, dict]:
        if not self.path.exists():
            return {}, {"version": VERSION, "conversations": {}, "tombstones": {}, "migrations": {}}
        envelope = read_json(self.path, max_bytes=MAX_BYTES)
        if not isinstance(envelope, dict) or envelope.get("version") != VERSION:
            raise ConversationError("ask_history_version_unsupported")
        try:
            plaintext = peer_box.open_sealed(self.key, self.pub, envelope["nonce"], envelope["ciphertext"], info=CHANNEL)
            if len(plaintext) > MAX_PLAINTEXT:
                raise ValueError
            data = json.loads(plaintext)
        except (KeyError, TypeError, ValueError, InvalidTag):
            raise ConversationError("ask_history_unreadable") from None
        if not isinstance(data, dict) or data.get("version") != VERSION:
            raise ConversationError("ask_history_version_unsupported")
        if any(not isinstance(data.get(key), dict) for key in ("conversations", "tombstones", "migrations")):
            raise ConversationError("ask_history_unreadable")
        return envelope, data

    def _write(self, envelope: dict, data: dict) -> None:
        plaintext = _json(data)
        if len(plaintext) > MAX_PLAINTEXT:
            raise ConversationError("ask_history_limit")
        nonce, ciphertext = peer_box.seal(self.key, self.pub, plaintext, info=CHANNEL)
        atomic_write_json(self.path, {**envelope, "version": VERSION, "nonce": nonce, "ciphertext": ciphertext}, max_bytes=MAX_BYTES)

    def list(self, service_key: str | None = None) -> list[dict]:
        with file_transaction(self.lock):
            _, data = self._read()
            rows = [public_conversation(row) for row in data["conversations"].values() if service_key is None or row["serviceKey"] == service_key]
            return sorted(rows, key=lambda row: row["updatedAt"], reverse=True)

    def get(self, conversation_id: str) -> dict:
        _identity(conversation_id)
        with file_transaction(self.lock):
            _, data = self._read()
            if conversation_id not in data["conversations"]:
                raise ConversationError("ask_conversation_not_found")
            return public_conversation(data["conversations"][conversation_id])

    def draft(self) -> dict:
        with file_transaction(self.lock):
            _, data = self._read()
            record = data.get("draft", {"version": 1, "text": "", "revision": 0})
            if not isinstance(record, dict) or record.get("version") != 1:
                raise ConversationError("ask_history_version_unsupported")
            return {key: record[key] for key in ("text", "revision")}

    def save_draft(self, text: str, *, expected_revision: int) -> dict:
        if not isinstance(text, str) or len(text.encode()) > 64 * 1024:
            raise ConversationError("ask_history_limit")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ConversationError("ask_revision_required")
        with file_transaction(self.lock):
            envelope, data = self._read()
            prior = self.draft()
            if prior["text"] == text:
                return prior
            if prior["revision"] != expected_revision:
                raise ConversationError("ask_revision_conflict")
            data["draft"] = {**data.get("draft", {}), "version": 1, "text": text, "revision": expected_revision + 1}
            self._write(envelope, data)
            return {"text": text, "revision": expected_revision + 1}

    def save(self, value: Mapping[str, Any], *, expected_revision: int) -> dict:
        clean = clean_conversation(value)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ConversationError("ask_revision_required")
        with file_transaction(self.lock):
            envelope, data = self._read()
            identifier = clean["id"]
            if identifier in data["tombstones"]:
                raise ConversationError("ask_conversation_deleted")
            prior = data["conversations"].get(identifier)
            if prior:
                if any(prior[key] != clean[key] for key in ("serviceKey", "providerPeerId", "networkId", "createdAt")):
                    raise ConversationError("ask_service_binding_mismatch")
                if clean_conversation(prior) == clean:
                    return public_conversation(prior)  # Same write after a lost response.
            if (prior or {}).get("revision", 0) != expected_revision:
                raise ConversationError("ask_revision_conflict")
            if not prior and len(data["conversations"]) >= 1000:
                raise ConversationError("ask_history_limit")
            # Preserve unknown fields already on disk, including message fields.
            old_messages = {row["id"]: row for row in (prior or {}).get("messages", [])}
            clean["messages"] = [{**old_messages.get(row["id"], {}), **row} for row in clean["messages"]]
            saved = {**(prior or {}), **clean, "revision": expected_revision + 1}
            data["conversations"][identifier] = saved
            self._write(envelope, data)
            return public_conversation(saved)

    def remove(self, conversation_id: str, *, expected_revision: int) -> dict:
        _identity(conversation_id)
        if type(expected_revision) is not int or expected_revision < 1:
            raise ConversationError("ask_revision_required")
        with file_transaction(self.lock):
            envelope, data = self._read()
            prior = data["conversations"].get(conversation_id)
            if prior is None:
                if conversation_id in data["tombstones"]:
                    return {"removed": 0}
                raise ConversationError("ask_conversation_not_found")
            if prior["revision"] != expected_revision:
                raise ConversationError("ask_revision_conflict")
            if len(data["tombstones"]) >= 10000:
                raise ConversationError("ask_history_limit")
            data["tombstones"][conversation_id] = {"deletedAt": datetime.now(timezone.utc).isoformat(), "revision": expected_revision + 1}
            del data["conversations"][conversation_id]
            self._write(envelope, data)
            return {"removed": 1}

    def migrate(self, value: Mapping[str, Any]) -> dict:
        """Import one legacy browser record; never overwrite newer node history.

        The browser retains its encrypted original until this acknowledgement.
        Digest + original ID make response loss/repeated imports safe. A deleted
        conversation remains deleted even if another browser imports it later.
        """
        clean = clean_conversation(value)
        identifier = clean["id"]
        digest = hashlib.sha256(_json(clean)).hexdigest()
        with file_transaction(self.lock):
            envelope, data = self._read()
            if identifier in data["tombstones"]:
                return {"id": identifier, "status": "deleted", "digest": digest}
            receipt = data["migrations"].get(identifier)
            if receipt:
                if receipt["digest"] != digest:
                    raise ConversationError("ask_migration_conflict")
                return {"id": identifier, "status": "already_imported", "digest": digest}
            prior = data["conversations"].get(identifier)
            if prior and clean_conversation(prior) != clean:
                raise ConversationError("ask_migration_conflict")
            if len(data["migrations"]) >= 10000 or (not prior and len(data["conversations"]) >= 1000):
                raise ConversationError("ask_history_limit")
            data["conversations"].setdefault(identifier, {**clean, "revision": 1})
            data["migrations"][identifier] = {"digest": digest}
            self._write(envelope, data)
            return {"id": identifier, "status": "imported", "digest": digest}
