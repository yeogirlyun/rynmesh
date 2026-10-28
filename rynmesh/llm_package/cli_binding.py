"""Bind shared CLI inference to a provider configuration and explicit upstream model."""
from __future__ import annotations

import hashlib
import json
import threading
import time
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
        self._cached_binding_row = None
        self._metadata_fingerprint = None
        self._metadata_lock = threading.Lock()
        self._display_refresh_lock = threading.Lock()
        self._display_refresh_running = False
        self._display_refresh_at = 0.0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _path_stamp(self):
        try:
            st = self.path.stat()
            return (st.st_size, st.st_mtime_ns)
        except OSError:
            return None

    def _configuration(self):
        command = Path(self.inner._command_path())
        try:
            stat = command.stat()
            executable = [str(command.resolve()), stat.st_size, stat.st_mtime_ns]
        except OSError:
            executable = [str(command), None, None]
        # No credentials, prompts, workspace contents or raw paths are published.
        fingerprint = hashlib.sha256(json.dumps({"executable": executable,
            "manifest": self.manifest.public_dict(), "kind": self.inner.kind}, sort_keys=True).encode()).hexdigest()
        return str(command), fingerprint

    def current_models(self):
        """Use the discovery TTL, never an indefinitely retained selected snapshot."""
        from .cli_models import list_models
        command, fingerprint = self._configuration()
        with self._metadata_lock:
            prior = self._metadata_fingerprint
            if prior is None and self.path.exists():
                with file_transaction(self.lock_path):
                    prior = read_json(self.path, max_bytes=256 * 1024).get(
                        self.manifest.package_id, {}).get("fingerprint")
            models = list_models(self.inner.kind, command, **({"refresh": True} if prior and prior != fingerprint else {}))
            if not models:
                raise AdapterError("cli_model_unavailable", code="model_configuration_changed")
            self._observe_models(models, fingerprint)
            self._metadata_fingerprint = fingerprint
            return models

    def display_models(self):
        """Passive UI metadata; admission always uses current_models instead.

        The last observed inventory survives restarts in the binding record.
        It is only a display fallback, never a model binding or dispatch grant.
        """
        from .cli_models import cached_models
        command, fingerprint = self._configuration()
        models = cached_models(self.inner.kind, command)
        if self._metadata_fingerprint == fingerprint and models:
            return models
        self._refresh_display_models()
        try:
            rows = read_json(self.path, max_bytes=256 * 1024) if self.path.exists() else {}
            row = rows.get(self.manifest.package_id, {})
            if row.get("fingerprint") != fingerprint:
                return []
            default = row.get("models", {}).get("", {}).get("signature", {}).get("model")
            return [{"id": model, "name": model or "CLI default", "default": model == default,
                     **{key: signature[key] for key in ("context_window", "context_window_known") if key in signature}}
                    for model, observation in row.get("models", {}).items()
                    if isinstance(signature := observation.get("signature"), dict)
                    and signature.get("available") is not False and model]
        except (OSError, ValueError, TypeError):
            return []

    def _refresh_display_models(self):
        with self._display_refresh_lock:
            if self._display_refresh_running or time.monotonic() - self._display_refresh_at < 30:
                return
            self._display_refresh_running = True
            self._display_refresh_at = time.monotonic()
        def refresh():
            try:
                self.current_models()
            except (ValueError, OSError, RuntimeError):
                pass
            finally:
                with self._display_refresh_lock:
                    self._display_refresh_running = False
        threading.Thread(target=refresh, name="ryn-cli-directory-refresh", daemon=True).start()

    def context_metadata_for_model(self, model=""):
        from .cli_models import context_metadata, validate_model
        validate_model(model)
        return context_metadata(self.current_models(), model)

    def binding_for_model(self, model=""):
        from .cli_models import validate_model
        validate_model(model)
        models = self.current_models()
        _, fingerprint = self._configuration()
        return self._binding_for_models(model, models, fingerprint)

    def cached_binding_for_model(self, model=""):
        """Passive signed catalogs never start discovery or publish stale authority."""
        from .cli_models import cached_models, validate_model
        validate_model(model)
        command, fingerprint = self._configuration()
        if self._metadata_fingerprint != fingerprint:
            return None
        models = cached_models(self.inner.kind, command)
        return self._binding_for_models(model, models, fingerprint) if models else None

    def _binding_for_models(self, model, models, fingerprint):
        if model and not any(item.get("id") == model for item in models):
            raise AdapterError("cli_model_unavailable", code="model_configuration_changed")
        row = self._observe_models(models, fingerprint)
        generation = hashlib.sha256((row["generation"] + ":" + model + ":" + row["models"][model]["generation"]).encode()).hexdigest()[:32]
        return {"version": VERSION, "service_id": self.manifest.package_id, "model_generation": generation,
                "rule_revision": row["rule_revision"]}

    def _observe_models(self, models, fingerprint):
        # Accepting metadata is itself an observation, even if nobody requests
        # an intermediate binding. Commit the whole snapshot in one transaction.
        from .cli_models import context_metadata
        selected = {"": next((item for item in models if item.get("default")), {})}
        for item in models:
            if isinstance(item.get("id"), str) and item["id"]:
                selected.setdefault(item["id"], item)
        observed = {model: {"model": item.get("id"), **{key: value for key, value in
                    context_metadata([item], model).items() if key != "context_window_basis"}}
                    for model, item in selected.items()}

        def signatures(row):
            # Disappearance from a successful inventory is durable too: a later
            # reappearance must not resurrect the old exact-model generation.
            return {**{model: {"model": model, "available": False}
                       for model in row.get("models", {}) if model not in observed}, **observed}

        def unchanged(row):
            return all(row.get("models", {}).get(model, {}).get("signature") == signature
                       for model, signature in signatures(row).items())

        sid = self.manifest.package_id
        stamp = self._path_stamp()
        cached = self._cached_binding_row
        if (cached is not None and stamp is not None and cached[:3] == (stamp, sid, fingerprint)
                and unchanged(cached[3])):
            row = cached[3]
        else:
            with file_transaction(self.lock_path):
                rows = read_json(self.path, max_bytes=256 * 1024) if self.path.exists() else {}
                row = rows.get(sid)
                if not row or row.get("fingerprint") != fingerprint:
                    row = {"fingerprint": fingerprint, "generation": uuid.uuid4().hex, "rule_revision": uuid.uuid4().hex}
                changed = False
                for model, signature in signatures(row).items():
                    if row.get("models", {}).get(model, {}).get("signature") != signature:
                        row.setdefault("models", {})[model] = {"signature": signature, "generation": uuid.uuid4().hex}
                        changed = True
                if changed:
                    rows[sid] = row
                    atomic_write_json(self.path, rows, max_bytes=256 * 1024)
                stamp = self._path_stamp()
                self._cached_binding_row = (stamp, sid, fingerprint, dict(row))
        return row

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
