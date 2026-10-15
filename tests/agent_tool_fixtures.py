"""Prepare schema exposure for boundary tests whose synthetic model is not a planner.

These fixtures still use the product discovery validator. They grant no tool
execution, data scope or approval. End-to-end discovery tests run the actual
model/tool exchange instead, and never use this precondition helper.
"""
from rynmesh.agent_runtime import tool_menu
from rynmesh.agent_runtime.prompt import INTERNAL_SPECS


def expose(runtime, task_id, names):
    def prepare(data):
        row = data["tasks"][task_id]
        (_, state), authority = runtime._menu(row)
        if state["mode"] == "directory":
            _, state = tool_menu.discover(runtime.tools.specs(), INTERNAL_SPECS, runtime._metadata, state,
                                         {"load": list(names)}, capacity_bytes=tool_menu.capacity(runtime._context_limit(row), authority))
        row["tool_menu"] = state
    runtime.store.transaction(prepare)


def expose_on_create(runtime, names):
    original = runtime.create
    def create(value):
        row = original(value)
        expose(runtime, row["id"], names)
        return runtime.get(row["id"])
    runtime.create = create
