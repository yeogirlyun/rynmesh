"""Use the candidate's bounded business-tool menu in the reference transport.

The upstream registry retains every business implementation. The actual model
request sees the shared projection, and dispatch accepts only that projection.
State is per reference session; this host does not claim restart support.
"""
from __future__ import annotations

import copy

from jsonschema import ValidationError, validate

from rynmesh.agent_runtime import tool_menu
from rynmesh.agent_runtime.store import AgentToolRejected, encoded


class ReferenceMenuTools:
    def __init__(self, tools, *, max_context_bytes, epoch=0, goal_revision=1):
        self.tools = tools
        self.max_context_bytes = max_context_bytes
        self.epoch, self.goal_revision = epoch, goal_revision
        self.state, self.exposed, self.capacity_bytes = {}, set(), 0

    def __getattr__(self, name):
        return getattr(self.tools, name)

    def specs(self):
        return [*self.tools.specs(), copy.deepcopy(tool_menu.SPEC)]

    def metadata(self, name):
        if name == tool_menu.NAME:
            return {"effect": "read", "title": "Discover available tools", "internal": True}
        return self.tools.metadata(name)

    def project(self, messages, supplied_specs):
        if not supplied_specs:  # Native completion checker deliberately has no tools.
            return []
        canonical = {item["function"]["name"]: item for item in self.specs()}
        if (len(supplied_specs) != len(canonical)
                or len({item["function"]["name"] for item in supplied_specs}) != len(canonical)
                or any(item != canonical.get(item["function"]["name"]) for item in supplied_specs)):
            raise ValueError("reference_tool_schema_changed")
        authority_bytes = len(encoded([m for m in messages if m["role"] in {"system", "user"}]))
        exposed, self.state = tool_menu.project(self.tools.specs(), [], self.state,
            max_context_bytes=self.max_context_bytes, authority_bytes=authority_bytes,
            epoch=self.epoch, goal_revision=self.goal_revision)
        if (len(encoded(messages)) + len(encoded(exposed)) > self.max_context_bytes
                and self.state.get("mode") == "directory" and self.state.get("loaded_names")):
            self.state = tool_menu.release(self.state)
            exposed, self.state = tool_menu.project(self.tools.specs(), [], self.state,
                max_context_bytes=self.max_context_bytes, authority_bytes=authority_bytes,
                epoch=self.epoch, goal_revision=self.goal_revision)
        self.capacity_bytes = tool_menu.capacity(self.max_context_bytes, authority_bytes)
        self.exposed = {item["function"]["name"] for item in exposed}
        return exposed

    def _validate(self, name, arguments):
        if name not in self.exposed:
            raise AgentToolRejected("agent_tool_not_exposed")
        if name == tool_menu.NAME:
            try:
                validate(arguments, tool_menu.SPEC["function"]["parameters"])
            except ValidationError:
                raise AgentToolRejected("agent_tool_arguments_invalid") from None
        else:
            self.tools._validate(name, arguments)

    def execute(self, name, arguments, **options):
        self._validate(name, arguments)
        if name != tool_menu.NAME:
            return self.tools.execute(name, arguments, **options)
        if options["cancelled"]():
            raise AgentToolRejected("agent_cancelled")
        result, self.state = tool_menu.discover(self.tools.specs(), [], self.tools.metadata,
            self.state, arguments, capacity_bytes=self.capacity_bytes)
        # Loading affects the next inference, never other calls in this batch.
        return result
