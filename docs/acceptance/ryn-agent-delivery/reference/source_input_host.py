"""Native source-credential input port using the existing owner HTTP control.

Only the exact input lifecycle may advance the accounting binding. This does
not run the candidate planner or accept a general owner-control revision jump.
"""
import copy
import uuid

from jsonschema import ValidationError, validate

from rynmesh.agent_runtime.default_model import binding
from rynmesh.agent_runtime.prompt import INTERNAL_SPECS
from rynmesh.agent_runtime.runtime import event, now
from rynmesh.agent_runtime.store import AgentToolRejected

NAME = "ryn_request_input"
SPEC = next(spec for spec in INTERNAL_SPECS if spec["function"]["name"] == NAME)


class ReferenceSourceInput:
    def __init__(self, tools, runtime, context, owner_input, record):
        self.tools, self.runtime, self.context = tools, runtime, context
        self.owner_input, self.record = owner_input, record

    def __getattr__(self, name):
        return getattr(self.tools, name)

    def specs(self):
        return [*self.tools.specs(), copy.deepcopy(SPEC)]

    def metadata(self, name):
        if name == NAME:
            return {"effect": "read", "internal": True, "scope_context_supported": True, "owner_input": True}
        return self.tools.metadata(name)

    def _validate(self, name, args):
        if name != NAME:
            return self.tools._validate(name, args)
        try:
            validate(args, SPEC["function"]["parameters"])
        except ValidationError:
            raise AgentToolRejected("agent_tool_arguments_invalid") from None
        if (args["kind"] != "credential_ref" or len({f["id"] for f in args["fields"]}) != len(args["fields"])
                or any(f["type"] != "credential_ref" or f.get("options") for f in args["fields"])):
            raise AgentToolRejected("agent_invalid_control_proposal")

    def bind(self, meter, admission):
        self.meter, self.admission = meter, admission

    def execute(self, name, args, **options):
        if name != NAME:
            return self.tools.execute(name, args, **options)
        self._validate(name, args)
        with self.meter.owner_wait():
            original = self.runtime.store.read()
            before = self.admission._row(original)
            if args["goal_revision"] != before["goal"]["revision"]:
                raise AgentToolRejected("agent_goal_revision_conflict")
            pending = {"id": "input_" + uuid.uuid4().hex, "kind": args["kind"], "prompt": args["prompt"],
                       "fields": copy.deepcopy(args["fields"]), "goal_revision": before["goal"]["revision"],
                       "scope_revision": before["scope_revision"], "business_revision": None, "created_at": now()}
            request = {"operation_id": options["operation_id"], "tool_call_id": options["operation_id"].split(":", 1)[1],
                       "arguments": copy.deepcopy(args), "binding": copy.deepcopy(self.admission.bound),
                       "pending_input_id": pending["id"], "state": "waiting_input"}

            def stage(data):
                row = self.admission._row(data)
                if (not self.admission.current(data, row) or self.admission.active is not None
                        or row.get("pending_input") or row["event_cursor"] != before["event_cursor"]
                        or row.get("reference_source_inputs")):
                    raise AgentToolRejected("agent_input_mismatch")
                row.update(status="waiting_input", pending_input=copy.deepcopy(pending))
                event(row, "input_requested", pending_input_id=pending["id"])
                row["pending_input"]["control_revision"] = row["control_revision"]
                row["reference_source_inputs"] = {options["operation_id"]: copy.deepcopy(request)}
            self.runtime.store.transaction(stage)
            # Callback is the same authenticated source-credential HTTP form
            # used by the candidate controller. Raw credentials never return.
            answer = self.owner_input(self.runtime.get(before["id"]))
            metadata = answer.get("credential", {}) if isinstance(answer, dict) else {}
            safe_keys = {"has_credential", "kind", "base_url", "source_id", "credential_version", "expires_at"}
            if set(metadata) - safe_keys or metadata.get("has_credential") is not True:
                raise AgentToolRejected("agent_input_mismatch")

            def accept(data):
                row = data["tasks"].get(before["id"])
                expected = {**self.admission.bound, "control_revision": before["control_revision"] + 3}
                if (row is None or binding(self.runtime, row) != expected or row["model_id"] != before["model_id"]
                        or row["status"] != "queued" or row.get("pending_input") or row.get("in_flight")
                        or row["scope"] != before["scope"] or data["settings"] != original["settings"]
                        or data.get("settings_revision") != original.get("settings_revision")
                        or self.admission._permissions(data, row) != self.admission._permissions(original, before)
                        or row["event_cursor"] != before["event_cursor"] + 3):
                    raise AgentToolRejected("agent_input_mismatch")
                events = row["events"][-3:]
                if ([e["type"] for e in events] != ["input_requested", "source_credential_saved", "input_answered"]
                        or events[0].get("pending_input_id") != pending["id"]
                        or events[2].get("pending_input_id") != pending["id"] or events[1].get("metadata") != metadata):
                    raise AgentToolRejected("agent_input_mismatch")
                saved = row.get("reference_source_inputs", {}).get(options["operation_id"])
                if saved != request:
                    raise AgentToolRejected("agent_input_mismatch")
                saved.update(state="answered", control_revision_after=row["control_revision"], credential=copy.deepcopy(metadata))
                return copy.deepcopy(row)
            row = self.runtime.store.transaction(accept)
            self.admission.bound = binding(self.runtime, row)
            self.meter.control_revision = row["control_revision"]
            # Preserve trusted callables and only refresh server-produced facts.
            refreshed = self.runtime._execution_context(row)
            refreshed["resource_origins"] = copy.deepcopy(self.context.get("resource_origins", []))
            self.context.update(refreshed)
            self.tools.validate_context(self.context)
            self.record["reference_source_inputs"] = copy.deepcopy(row["reference_source_inputs"])
        return {"accepted": True, "pending_input_id": pending["id"], "waiting_for_owner": False,
                "credential": copy.deepcopy(metadata), "grants_permission": False,
                "goal_revision": row["goal"]["revision"], "control_revision": row["control_revision"]}

    def after_tool(self, params, result):
        if params["name"] != "ryn_commit_source_draft" or not isinstance(result, dict) or result.get("ok") is not True:
            return result
        receipt = {"tool": params["name"], "arguments": copy.deepcopy(params["arguments"]), "result": copy.deepcopy(result)}
        checked = self.meter.tool(lambda: self.tools.verify(receipt, [{"verifier": "saved_source_state"}],
                                                          context=self.context, cancelled=self.meter.cancelled))
        self.record.setdefault("reference_source_verifications", []).append({"tool_call_id": params["tool_call_id"],
            "receipt": copy.deepcopy(receipt), "verification": copy.deepcopy(checked)})
        # This is observed business evidence in the original tool response,
        # not a new owner message, planner hint or substituted native checker.
        return {**result, "post_save_verification": checked}
