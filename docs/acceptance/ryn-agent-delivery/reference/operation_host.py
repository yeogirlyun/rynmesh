"""Native-call admission to existing business authority; never a planner.

This single-process port stores approvals and receipts durably, but does not
claim recovery orchestration. Only its exact active flight can relax the
accounting host's queued-state guard.

Status transitions here are dispatch bookkeeping, not Ryn owner control actions:
the native approval callback is bound in a separate immutable operation record.
The existing goal/control/scope revisions are CAS preconditions, never rewritten
or advanced to conceal an owner action. A genuine owner change invalidates them.
"""
import copy

from rynmesh.agent_runtime.default_model import binding
from rynmesh.agent_runtime.store import AgentToolRejected


class ReferenceOperationAdmission:
    def __init__(self, runtime, task_id, context):
        self.runtime, self.task_id, self.context = runtime, task_id, context
        self.bound = copy.deepcopy(binding(runtime, runtime.store.read()["tasks"][task_id]))
        self.model_id = runtime.store.read()["tasks"][task_id]["model_id"]
        self.active = None

    def _row(self, data):
        row = data["tasks"].get(self.task_id)
        if (row is None or binding(self.runtime, row) != self.bound or row["model_id"] != self.model_id
                or any(self.context.get(k) != v for k, v in self.bound.items())
                or not data["settings"]["enabled"]):
            raise AgentToolRejected("agent_configuration_context_changed")
        return row

    def current(self, data, row):
        self._row(data)
        if self.active is None:
            return row["status"] == "queued" and row.get("in_flight") is None
        record = row.get("reference_operations", {}).get(self.active["operation_id"], {})
        return (row["status"] == "running" and row.get("in_flight") == self.active
                and record.get("state") == "dispatched"
                and record.get("permissions") == self._permissions(data, row))

    @staticmethod
    def _permissions(data, row):
        return {"task": [row["permission_mode"], sorted(row["allowed_write_tools"])],
                "settings": [data["settings"]["permission_mode"], sorted(data["settings"]["allowed_write_tools"])]}

    def approve(self, operation_id, request, approved):
        def save(data):
            row = self._row(data)
            if not self.current(data, row) or self.active is not None:
                raise AgentToolRejected("agent_configuration_authority_changed")
            records = row.setdefault("reference_operations", {})
            if operation_id in records:
                raise AgentToolRejected("agent_operation_already_confirmed")
            records[operation_id] = {"operation_id": operation_id, **copy.deepcopy(request),
                "approved": approved, "state": "approved" if approved else "denied",
                "binding": copy.deepcopy(self.bound), "permissions": self._permissions(data, row)}
        self.runtime.store.transaction(save)

    def stage(self, operation_id, request):
        """Durable exact pending request for a real approval-wait interruption."""
        def save(data):
            row = self._row(data)
            if not self.current(data, row) or self.active is not None:
                raise AgentToolRejected("agent_configuration_authority_changed")
            records = row.setdefault("reference_operations", {})
            if operation_id in records:
                raise AgentToolRejected("agent_operation_already_confirmed")
            records[operation_id] = {"operation_id": operation_id, **copy.deepcopy(request),
                "approved": None, "state": "waiting_approval", "binding": copy.deepcopy(self.bound),
                "permissions": self._permissions(data, row)}
        self.runtime.store.transaction(save)

    def admit(self, operation_id, request):
        def apply(data):
            row = self._row(data)
            record = row.get("reference_operations", {}).get(operation_id, {})
            if (not self.current(data, row) or self.active is not None
                    or record.get("state") != "approved" or record.get("approved") is not True
                    or any(record.get(k) != v for k, v in request.items())
                    or record.get("binding") != self.bound
                    or record.get("permissions") != self._permissions(data, row)
                    or "read_only" in {row["permission_mode"], data["settings"]["permission_mode"]}):
                raise AgentToolRejected("agent_configuration_authority_changed")
            flight = {**copy.deepcopy(request), "operation_id": operation_id, "kind": "tool",
                      "effect": "write", "dispatched": True, "authorization": "approved"}
            row.update(status="running", in_flight=flight)
            record["state"] = "dispatched"
            return copy.deepcopy(flight)
        self.active = self.runtime.store.transaction(apply)

    def settle(self, operation_id, state, result):
        def apply(data):
            row = data["tasks"].get(self.task_id)
            if row is None:
                raise AgentToolRejected("agent_configuration_context_changed")
            record = row.get("reference_operations", {}).get(operation_id)
            if record is None or record["state"] != "dispatched":
                raise AgentToolRejected("agent_configuration_authority_changed")
            record.update(state=state, result=copy.deepcopy(result))
            # Owner cancellation/revision changes must never be resurrected.
            if (state != "unknown" and binding(self.runtime, row) == self.bound and row["status"] == "running"
                    and row.get("in_flight") == self.active):
                row.update(status="queued", in_flight=None)
        try:
            self.runtime.store.transaction(apply)
        finally:
            self.active = None
