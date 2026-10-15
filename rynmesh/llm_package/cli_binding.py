"""Bind shared CLI inference to a provider configuration and explicit upstream model."""
from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import nullcontext
from pathlib import Path

from ..atomic_io import atomic_write_json, read_json
from ..file_transactions import file_transaction
from .adapters import AdapterError
from .peer_binding import VERSION


class CLIModelDispatchAdapter:
    def __init__(self, inner, manifest, home):
        self.inner, self.manifest = inner, manifest
        self.path = Path(home) / "cli-model-bindings.json"
        self.lock_path = Path(home) / ".cli-model-bindings.lock"

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def binding_for_model(self, model=""):
        from .cli_models import validate_model
        validate_model(model)
        command = Path(self.inner._command_path())
        try:
            stat = command.stat()
            executable = [str(command.resolve()), stat.st_size, stat.st_mtime_ns]
        except OSError:
            executable = [str(command), None, None]
        # No credentials, prompts, workspace contents or raw paths are published.
        fingerprint = hashlib.sha256(json.dumps({"executable": executable,
            "manifest": self.manifest.public_dict(), "kind": self.inner.kind}, sort_keys=True).encode()).hexdigest()
        with file_transaction(self.lock_path):
            rows = read_json(self.path, max_bytes=256 * 1024) if self.path.exists() else {}
            sid = self.manifest.package_id
            row = rows.get(sid)
            if not row or row.get("fingerprint") != fingerprint:
                row = {"fingerprint": fingerprint, "generation": uuid.uuid4().hex, "rule_revision": uuid.uuid4().hex}
                rows[sid] = row
                atomic_write_json(self.path, rows, max_bytes=256 * 1024)
        generation = hashlib.sha256((row["generation"] + ":" + model).encode()).hexdigest()[:32]
        return {"version": VERSION, "service_id": sid, "model_generation": generation, "rule_revision": row["rule_revision"]}

    @property
    def model_binding(self):
        try:
            return self.binding_for_model()
        except (AdapterError, OSError):
            return None

    def call_bound(self, method, expected, authorize, *args, authority_lock=None, **kwargs):
        # Admission follows the same authority-lock order as source/native models.
        # Never retain it across a CLI generation. Revocations after admission are
        # handled as in-flight requests by the provider's cancellation machinery.
        with authority_lock if authority_lock is not None else nullcontext():
            if expected != self.binding_for_model(kwargs.get("model", "")):
                raise AdapterError("model_configuration_changed", code="model_configuration_changed")
            try:
                authorize()
            except (ValueError, OSError):
                raise AdapterError("ai_permission_denied", code="ai_permission_denied") from None
        return getattr(self.inner, method)(*args, **kwargs)
