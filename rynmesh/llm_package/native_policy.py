"""Persistent native-model admission policy; no credentials or task bodies."""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone

from ..atomic_io import atomic_write_json, read_json
from ..file_transactions import file_transaction
from .adapters import AdapterError
from .model_settings import validate
from .peer_binding import VERSION

FIELDS = {"max_output_tokens", "max_concurrent", "requests_per_day"}


class NativeModelPolicy:
    def __init__(self, root):
        self.path = root / "model-policies.json"
        self.lock = root / ".model-policies.lock"

    def _read(self):
        return read_json(self.path, max_bytes=256 * 1024) if self.path.exists() else {}

    def _save(self, rows):
        atomic_write_json(self.path, rows, max_bytes=256 * 1024)

    def ensure(self, manifest, sharing, *, rotate=False):
        with file_transaction(self.lock):
            rows = self._read()
            sid = manifest.package_id
            previous = rows.get(sid)
            if previous is None and len(rows) >= 128:
                raise ValueError("native_model_policy_capacity_exhausted")
            row = copy.deepcopy(previous) if previous else {"configuration": {
                "max_output_tokens": manifest.max_output_tokens, "max_concurrent": 1, "requests_per_day": None},
                "model_generation": uuid.uuid4().hex, "rule_revision": uuid.uuid4().hex,
                "sharing": sharing, "day": "", "requests": 0}
            if rotate:
                row["model_generation"] = uuid.uuid4().hex
            if rotate or row["sharing"] != sharing:
                row["rule_revision"] = uuid.uuid4().hex
            row["sharing"] = sharing
            if row != previous:
                rows[sid] = row
                self._save(rows)
            return row

    def current(self, service_id):
        with file_transaction(self.lock):
            return self._read().get(service_id)

    @staticmethod
    def binding(service_id, row):
        return {"version": VERSION, "service_id": service_id,
                "model_generation": row["model_generation"], "rule_revision": row["rule_revision"]}

    def configure(self, manifest, body):
        if (not isinstance(body, dict) or set(body) != {"expected_rule_revision", "configuration"}
                or not isinstance(body["configuration"], dict) or set(body["configuration"]) - FIELDS):
            raise ValueError("native_model_configuration_invalid")
        with file_transaction(self.lock):
            rows = self._read()
            row = rows.get(manifest.package_id)
            if row is None or row["rule_revision"] != body["expected_rule_revision"]:
                raise ValueError("native_model_rule_revision_conflict")
            config = {**row["configuration"], **body["configuration"]}
            validate({**config, "context_window": manifest.context_window})
            if config != row["configuration"]:
                if config["max_output_tokens"] != row["configuration"]["max_output_tokens"]:
                    row["model_generation"] = uuid.uuid4().hex
                row.update(configuration=config, rule_revision=uuid.uuid4().hex)
                self._save(rows)
            return copy.deepcopy(row)

    def admit(self, service_id, expected, active):
        with file_transaction(self.lock):
            rows = self._read()
            row = rows.get(service_id)
            if row is None or self.binding(service_id, row) != expected:
                raise AdapterError("model_configuration_changed", code="model_configuration_changed")
            config = row["configuration"]
            if active >= config["max_concurrent"]:
                raise AdapterError("runtime_busy", code="runtime_busy")
            day = datetime.now(timezone.utc).date().isoformat()
            count = row["requests"] if row["day"] == day else 0
            if config["requests_per_day"] is not None and count >= config["requests_per_day"]:
                raise AdapterError("model_daily_quota_exhausted", code="model_daily_quota_exhausted")
            row.update(day=day, requests=count + 1)
            self._save(rows)
            return copy.deepcopy(config)
