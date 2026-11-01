"""Shared candidate accounting for reference model, verifier and probe traffic.

Uses the isolated encrypted task ledger. It does not start the Ryn executor or
claim reference process-recovery support. Nested probe time belongs to its tool.
"""
from __future__ import annotations

import time
from contextlib import contextmanager

from rynmesh.agent_runtime import budget
from rynmesh.agent_runtime.output_policy import budgeted
from rynmesh.agent_runtime.store import AgentError, AgentModelResponseError, AgentToolRejected


class ReferenceBudget:
    def __init__(self, runtime, task_id, *, guard, clock=time.monotonic, defer_start=False, admission=None):
        self.runtime, self.task_id, self.external_guard, self.clock = runtime, task_id, guard, clock
        self.admission = admission
        initial = runtime.store.read()
        row = initial["tasks"][task_id]
        self.control_revision = row["control_revision"]
        self.permission_authority = self._authority(initial["settings"])
        self.time_limit = min(row["limits"]["max_seconds"] - row["elapsed_seconds"], budget.time_remaining(row))
        self.deadline = None if defer_start else clock() + self.time_limit
        self._waiting_since, self._excluded_seconds, self._in_flight = None, 0.0, 0

    def owner_wait_seconds(self):
        return self._excluded_seconds + (self.clock() - self._waiting_since if self._waiting_since is not None else 0)

    @contextmanager
    def owner_wait(self):
        """Only a trusted owner-policy callback may suspend active time."""
        self.guard()
        if self._in_flight or self._waiting_since is not None:
            raise AgentError("reference_owner_wait_during_dispatch")
        self._waiting_since = self.clock()
        try:
            yield
        finally:
            self._excluded_seconds += self.clock() - self._waiting_since
            self._waiting_since = None
            self.guard()  # A late approval never revives cancelled/revoked work.

    @staticmethod
    def _authority(settings):
        return settings["permission_mode"], tuple(sorted(settings.get("allowed_write_tools", [])))

    def guard(self):
        if self.deadline is None:
            self.deadline = self.clock() + self.time_limit
        data = self.runtime.store.read()
        row = data["tasks"].get(self.task_id)
        if (row is None or row["control_revision"] != self.control_revision
                or not (self.admission.current(data, row) if self.admission else row["status"] == "queued")
                or not data["settings"]["enabled"]
                or self._authority(data["settings"]) != self.permission_authority):
            raise AgentToolRejected("agent_cancelled")
        self.external_guard()
        if self.clock() - self.owner_wait_seconds() >= self.deadline:
            raise AgentError("agent_time_budget_exhausted")

    def snapshot(self):
        return budget.public(self.runtime.store.read()["tasks"][self.task_id])

    def request_deadline(self):
        """Translate remaining active budget into a local transport deadline."""
        self.guard()
        row = self.runtime.store.read()["tasks"][self.task_id]
        remaining = min(self.deadline - self.clock() + self.owner_wait_seconds(), budget.time_remaining(row))
        if remaining <= 0:
            raise AgentError("agent_time_budget_exhausted")
        return time.monotonic() + remaining

    def record_request_policy(self, policy):
        self.runtime.store.transaction(lambda data: budget.record_request_policy(data["tasks"][self.task_id], policy))

    def cancelled(self):
        """Cheap in-flight checks; the full egress gate runs before dispatch."""
        try:
            self.guard()
        except AgentError:
            return True
        return False

    def _run(self, kind, stage, invoke, *, input_bound=0, output_bound=0, nested=False, output_policy=None):
        if self._waiting_since is not None:
            raise AgentError("reference_dispatch_during_owner_wait")
        self.guard()

        def admit(data):
            row = data["tasks"][self.task_id]
            if (row["control_revision"] != self.control_revision or row["status"] != "queued" or not data["settings"]["enabled"]
                    or self._authority(data["settings"]) != self.permission_authority):
                raise AgentToolRejected("agent_cancelled")
            state = budget.public(row)
            available = state["limits"]["max_total_tokens"] - state["lifetime"]["charged_tokens"] - input_bound
            if kind == "model" and available <= 0:
                raise AgentError("agent_token_budget_exhausted")
            reserved = budget.reserve(row, kind, stage, input_bound=input_bound,
                                  output_bound=min(output_bound, available) if kind == "model" else 0,
                                  seconds_bound=0 if nested else None)
            if output_policy:
                row["budget"]["records"][-1]["output_policy"] = budgeted(output_policy, reserved["output_reservation"])
            return reserved

        reservation = self.runtime.store.transaction(admit)
        self._in_flight += 1
        start, response = self.clock(), None
        try:
            self.guard()
            response = invoke(reservation["output_reservation"])
            self.guard()
            return response
        except Exception as exc:
            # A rejected response still consumed inference. Use the bridge's
            # sanitized usage just like the candidate; never infer zero usage
            # or retain an estimate when the actual counters are available.
            if kind == "model" and isinstance(exc, AgentModelResponseError):
                response = {"usage": exc.usage}
            # Preserve the actual owner/deadline stop when transport reports a
            # generic cancellation while the in-flight watcher is stopping it.
            self.guard()
            raise
        finally:
            usage = response.get("reported_usage", response.get("usage")) if kind == "model" and isinstance(response, dict) else None
            def settle(data):
                row = data["tasks"].get(self.task_id)
                if row is not None:
                    budget.settle(row, reservation["id"], usage=usage,
                                  elapsed_seconds=0 if nested else self.clock() - start)
            try:
                self.runtime.store.transaction(settle)
            finally:
                self._in_flight -= 1

    def model(self, invoke, *, input_bytes, max_output_tokens, output_policy=None):
        return self._run("model", "reference_inference", invoke,
                         input_bound=input_bytes + 512, output_bound=max_output_tokens, output_policy=output_policy)

    def tool(self, invoke):
        return self._run("tool", "reference_tool", lambda _: invoke())

    def probe(self, invoke, *, max_output_tokens, input_bytes, model_id, source_id, purpose="source_draft_probe"):
        def bounded(allowed):
            self.guard()
            return invoke(allowed, min(15.0, self.deadline - self.clock() + self.owner_wait_seconds()), self.guard)
        return self._run("model", purpose, bounded, input_bound=input_bytes + 512,
                         output_bound=max_output_tokens, nested=True)
