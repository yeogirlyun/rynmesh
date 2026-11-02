"""Execution concurrency is independent of the number of proposed tool calls.

Both direct calls and code-mode children use this host-owned admission gate.
Only known parallel-safe tools share the gate; other tools run exclusively.
The worker ceiling protects this desktop host, not the model response format.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

from .store import AgentToolRejected

MAX_PARALLEL_TOOLS = 8
PARALLEL_READS = frozenset({"ryn_file_read", "ryn_file_list", "ryn_file_search", "ryn_process_read",
                          "ryn_project_inspect", "ryn_local_environment", "ryn_list_plugins",
                          "ryn_plugin_info", "ryn_list_craft_plugins", "ryn_craft_editor_info",
                          "ryn_list_node_apps", "ryn_craft_tools", "ryn_craft_read", "ryn_web_search"})


def supports_parallel(name, metadata):
    """Use host capabilities, never a model's claimed effect or annotations."""
    return (metadata.get("effect") == "read"
            and not metadata.get("requires_explicit_approval")
            and not metadata.get("computer_access")
            and (name in PARALLEL_READS or metadata.get("supports_parallel") is True))


class ToolDispatchGate:
    """Per-task shared/exclusive admission, preserving barriers and cancellation."""

    def __init__(self, capacity=MAX_PARALLEL_TOOLS):
        self.capacity = capacity
        self.changed = threading.Condition()
        self.tasks = {}

    @contextmanager
    def enter(self, task_id, *, parallel, cancelled):
        ticket = object()
        with self.changed:
            state = self.tasks.setdefault(task_id, {"queue": [], "active": 0, "exclusive": False})
            state["queue"].append((ticket, parallel))
        admitted = False
        try:
            while not admitted:
                # Cancellation can acquire the store lock; never call under our lock.
                if cancelled():
                    raise AgentToolRejected(getattr(cancelled, "reason", None) or "agent_cancelled")
                with self.changed:
                    position = next(i for i, item in enumerate(state["queue"]) if item[0] is ticket)
                    ahead = state["queue"][:position]
                    ready = (not state["exclusive"] and state["active"] < self.capacity
                             and (all(shared for _, shared in ahead) if parallel
                                  else position == 0 and state["active"] == 0))
                    if ready:
                        state["queue"].pop(position)
                        state["active"] += 1
                        state["exclusive"] = not parallel
                        admitted = True
                    else:
                        self.changed.wait(0.05)
            yield
        finally:
            with self.changed:
                if admitted:
                    state["active"] -= 1
                    if not parallel:
                        state["exclusive"] = False
                else:
                    state["queue"] = [item for item in state["queue"] if item[0] is not ticket]
                if not state["queue"] and not state["active"]:
                    self.tasks.pop(task_id, None)
                self.changed.notify_all()
