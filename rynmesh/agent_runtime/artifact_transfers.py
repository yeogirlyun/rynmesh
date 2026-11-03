"""Short-lived plugin-transfer grants over existing receipt-backed task files."""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from pathlib import Path

from . import workspace_artifacts
from .store import AgentError

NAME = "ryn_list_task_artifacts"
PURPOSE = "plugin_attachment"
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

    def _path(self, ref):
        if not isinstance(ref, str) or not REF.fullmatch(ref):
            raise AgentError("agent_artifact_not_found")
        path = self.root / ref
        if path.exists() and (path.is_symlink() or path.resolve().parent != self.root.resolve()):
            raise AgentError("agent_artifact_access_changed")
        return path

    def prune(self, context):
        expired = []
        def prune(data):
            row = data["tasks"][context["task_id"]]
            refs = row.setdefault("artifact_transfers", {})
            for ref, record in list(refs.items()):
                if record["expires_at"] <= self.clock():
                    expired.append(ref)
                    del refs[ref]
        self.store.transaction(prune)
        for ref in expired:
            self._path(ref).unlink(missing_ok=True)

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
            self.root.mkdir(parents=True, exist_ok=True)
            self._path(ref).write_bytes(content["bytes"])
            def save(data, ref=ref, record=record):
                current = data["tasks"][context["task_id"]]
                refs = current.setdefault("artifact_transfers", {})
                if current["scope_revision"] != context["scope_revision"] or len(refs) >= 32:
                    raise AgentError("agent_artifact_transfer_limit")
                refs[ref] = record
            try:
                self.store.transaction(save)
            except BaseException:
                self._path(ref).unlink(missing_ok=True)
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
        path = self._path(ref)
        if not path.is_file() or path.stat().st_size != record["size"]:
            raise AgentError("agent_artifact_content_changed")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise AgentError("agent_artifact_content_changed")
        return {**self.public(record), **({"bytes": raw} if content else {})}
