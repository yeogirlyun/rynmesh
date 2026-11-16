"""Short-lived plugin-transfer grants over existing receipt-backed task files."""
from __future__ import annotations

import hashlib
import re
import secrets
import shutil
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag

from ..atomic_io import atomic_write_json, read_json
from ..services import peer_box
from . import workspace_artifacts
from .store import AgentError

NAME = "ryn_list_task_artifacts"
PURPOSE = "plugin_attachment"
CHANNEL = b"rynmesh-artifact-transfer-v1"
MAX_ENVELOPE_BYTES = 3 * workspace_artifacts.MAX_ARTIFACT_BYTES
REF = re.compile(r"^artifact_[a-f0-9]{48}$")


class ArtifactTransfers:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        self.root = Path(store.root) / "artifact-transfers"

    def _row(self, context):
        if not isinstance(context, dict) or not context.get("owner_id") or not context.get("task_id"):
            raise AgentError("agent_scope_context_required")
        row = self.store.inspect(lambda data: data["tasks"].get(context["task_id"]))
        if not row or row["scope_revision"] != context.get("scope_revision"):
            raise AgentError("agent_artifact_access_changed")
        return row

    def _task_dir(self, task_id):
        directory = self.root / hashlib.sha256(task_id.encode()).hexdigest()[:24]
        if directory.is_symlink() or directory.resolve().parent != self.root.resolve():
            raise AgentError("agent_artifact_access_changed")
        return directory

    def _path(self, task_id, ref):
        if not isinstance(ref, str) or not REF.fullmatch(ref):
            raise AgentError("agent_artifact_not_found")
        directory = self._task_dir(task_id)
        path = directory / ref
        if path.is_symlink() or path.resolve().parent != directory.resolve():
            raise AgentError("agent_artifact_access_changed")
        return path

    def forget_task(self, task_id):
        directory = self._task_dir(task_id)
        if directory.exists():
            shutil.rmtree(directory)

    def sweep(self):
        """Run after startup reconciliation, before any workers can issue copies."""
        tasks = self.store.inspect(lambda data: data["tasks"])
        live = {hashlib.sha256(task_id.encode()).hexdigest()[:24]: row
                for task_id, row in tasks.items()}
        if not self.root.exists():
            return
        now = self.clock()
        for directory in self.root.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                directory.unlink()  # Includes legacy plaintext copies at the root.
                continue
            row = live.get(directory.name)
            if row is None:
                shutil.rmtree(directory)
                continue
            refs = row.get("artifact_transfers", {})
            for path in directory.iterdir():
                record = refs.get(path.name)
                if path.is_symlink() or not record or record["expires_at"] <= now:
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
            if not any(directory.iterdir()):
                directory.rmdir()

    def prune(self, context):
        expired = []
        def prune(data):
            row = data["tasks"][context["task_id"]]
            refs = row.setdefault("artifact_transfers", {})
            for ref, record in list(refs.items()):
                if record["expires_at"] <= self.clock() or not self._path(context["task_id"], ref).is_file():
                    expired.append(ref)
                    del refs[ref]
        self.store.transaction(prune)
        for ref in expired:
            self._path(context["task_id"], ref).unlink(missing_ok=True)

    def issue(self, context):
        row = self._row(context)
        self.prune(context)
        row = self._row(context)
        existing = row.get("artifact_transfers", {})
        items = []
        for file_id, state in list(workspace_artifacts.history(row).items())[-32:]:
            if state["deleted"] or not state.get("sha256"):
                continue
            try:
                content = workspace_artifacts.read(row, file_id)
            except (AgentError, OSError):
                continue
            if content["status"] != "current":
                continue
            match = next((value for value in existing.values() if value["file_id"] == file_id
                and value["sha256"] == content["sha256"] and value["owner_id"] == context["owner_id"]
                and value["scope_revision"] == context["scope_revision"]), None)
            if match:
                items.append(self.public(match))
                continue
            ref = "artifact_" + secrets.token_hex(24)
            record = {"artifact_ref": ref, "file_id": file_id, "owner_id": context["owner_id"],
                "task_id": context["task_id"], "scope_revision": context["scope_revision"], "purpose": PURPOSE,
                "expires_at": int(self.clock()) + 900, "name": content["name"], "size": len(content["bytes"]),
                "sha256": content["sha256"], "media_type": content["media_type"]}
            nonce, ciphertext = peer_box.seal(self.store.key, self.store.pub, content["bytes"], info=CHANNEL)
            atomic_write_json(self._path(context["task_id"], ref), {"nonce": nonce, "ciphertext": ciphertext},
                              max_bytes=MAX_ENVELOPE_BYTES)
            def save(data, ref=ref, record=record):
                current = data["tasks"][context["task_id"]]
                refs = current.setdefault("artifact_transfers", {})
                if current["scope_revision"] != context["scope_revision"] or len(refs) >= 32:
                    raise AgentError("agent_artifact_transfer_limit")
                refs[ref] = record
            try:
                self.store.transaction(save)
            except BaseException:
                self._path(context["task_id"], ref).unlink(missing_ok=True)
                raise
            items.append(self.public(record))
        return {"artifacts": items, "max_file_bytes": workspace_artifacts.MAX_ARTIFACT_BYTES,
                "purpose": PURPOSE, "note": "Only receipt-backed files in this task; each transfer still requires approval."}

    @staticmethod
    def public(record):
        return {key: record[key] for key in ("artifact_ref", "name", "size", "sha256", "media_type", "purpose", "expires_at")}

    def resolve(self, context, ref, *, content=False):
        row = self._row(context)
        record = row.get("artifact_transfers", {}).get(ref)
        if (not record or record["owner_id"] != context["owner_id"] or record["task_id"] != context["task_id"]
                or record["scope_revision"] != context["scope_revision"] or record["expires_at"] <= self.clock()
                or record["purpose"] != PURPOSE):
            raise AgentError("agent_artifact_access_changed")
        path = self._path(context["task_id"], ref)
        try:
            envelope = read_json(path, max_bytes=MAX_ENVELOPE_BYTES)
            raw = peer_box.open_sealed(self.store.key, self.store.pub, envelope["nonce"], envelope["ciphertext"], info=CHANNEL)
        except (OSError, ValueError, KeyError, TypeError, InvalidTag):
            raise AgentError("agent_artifact_content_changed") from None
        if len(raw) != record["size"] or hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise AgentError("agent_artifact_content_changed")
        return {**self.public(record), **({"bytes": raw} if content else {})}
