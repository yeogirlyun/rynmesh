"""Two exact native interruption points and public cold-resume readback.

This does not implement a replacement recovery planner. An interrupted native
tool has no restored executor promise; its missing capability stays a failure.
"""
from __future__ import annotations

import copy

from worker_host import ReferenceProcessInterrupted


class ReferenceRecovery:
    def __init__(self, runtime, task_id, phase):
        if phase not in {"approval", "receipt", "resume"}:
            raise ValueError("reference_recovery_phase_invalid")
        self.runtime, self.task_id, self.phase = runtime, task_id, phase
        self.saved = copy.deepcopy(runtime.store.read()["tasks"][task_id].get("reference_session"))
        if (phase == "resume") != bool(self.saved):
            raise ValueError("reference_recovery_session_state_invalid")
        self.session_id = self.saved["session_id"] if self.saved else None
        self.process = None
        self.gate = self.menu = self.admission = None

    def bind(self, gate, menu, admission, config):
        if admission is None:
            raise ValueError("reference_recovery_admission_required")
        self.gate, self.menu, self.admission = gate, menu, admission
        if self.saved:
            if (self.saved["binding"] != admission.bound or self.saved["model_id"] != config["model_id"]
                    or self.saved["session_db_path"] != config["session_db_path"]):
                raise ValueError("reference_recovery_binding_changed")
            config.update(resume=True, rpc_sequence_start=self.saved["last_rpc_id"])
            # Recovered admission does not adopt the old in-flight operation.
            # No model/tool RPC is allowed in this readback-only phase.
        else:
            self.saved = {"session_id": gate.session, "session_db_path": config["session_db_path"],
                "model_id": config["model_id"], "binding": copy.deepcopy(admission.bound), "last_rpc_id": 0}
            self._save()

    def _save(self):
        saved = copy.deepcopy(self.saved)
        self.runtime.store.transaction(lambda data: data["tasks"][self.task_id].update(reference_session=saved))

    def attach(self, process):
        self.process = process

    def before_approval(self, operation_id, request):
        if self.phase == "approval":
            self.admission.stage(operation_id, request)
            self.interrupt(operation_id)

    def after_business(self, operation_id, request):
        if self.phase == "receipt":
            self.interrupt(operation_id)

    def interrupt(self, operation_id):
        self.saved.update(interrupted_operation_id=operation_id, interrupted_phase=self.phase,
                          worker_pid=self.process.pid)
        self._save()
        self.process.kill()
        self.process.wait(timeout=5)
        raise ReferenceProcessInterrupted()

    def finish(self, result):
        if result.get("interrupted"):
            self.saved["last_rpc_id"] = result["last_rpc_id"]
            self._save()
        return {"phase": self.phase, "session_id": self.session_id or self.gate.session,
                "worker_pid": self.process.pid, "interrupted_operation_id": self.saved.get("interrupted_operation_id"),
                "native_execution_resumed": False, "formal_pass": False,
                "case_status": "failed" if self.phase == "resume" else "interrupted",
                "reason": "native_pending_execution_not_restored" if self.phase == "resume" else None}
