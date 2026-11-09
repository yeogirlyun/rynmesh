"""A write tool that answers with an error is an ordinary tool error, not a locked task."""
from __future__ import annotations

import json

from test_agent_runtime import approve, assistant, make_runtime


def run_until(runtime, task_id, statuses, limit=12):
    for _ in range(limit):
        row = runtime.get(task_id)
        if row["status"] in statuses:
            return row
        runtime.run_once()
    return runtime.get(task_id)


def test_identical_failed_write_is_refused_until_the_owner_speaks_again(tmp_path):
    write = assistant(name="share_service", args={"service_id": "svc"})
    runtime, model, tools, _ = make_runtime(
        tmp_path, write, assistant(name="share_service", args={"service_id": "svc"}),
        assistant("The tool failed; I will check the state first."),
        assistant(name="share_service", args={"service_id": "svc"}), assistant("Shared on the second try."))
    dispatched = []

    def answered_with_error(name, arguments, *, operation_id, cancelled):
        dispatched.append(operation_id)
        if len(dispatched) == 1:
            return {"is_error": True, "error": "agent_mcp_tool_returned_error", "side_effects": "unknown"}
        return {"service_id": arguments["service_id"], "shared": True}

    tools.execute = answered_with_error
    row = runtime.create({"message": "Share the service."})
    run_until(runtime, row["id"], {"waiting_approval"})
    approve(runtime, row["id"])
    row = run_until(runtime, row["id"], {"completed", "failed", "interrupted"})
    assert row["status"] == "completed" and row["error"] is None
    assert len(dispatched) == 1  # The blind identical retry never reached the tool.
    stored = runtime.store.read()["tasks"][row["id"]]
    assert any(m["role"] == "tool" and "agent_repeated_failed_write" in m["content"] for m in stored["messages"])

    # The owner asks again: the same write may now be proposed (and still needs approval).
    current = runtime.get(row["id"])
    runtime.add_message(row["id"], {"message": "Please try sharing again.", "intent": "information",
                                    "expected_control_revision": current["control_revision"],
                                    "expected_goal_revision": current["goal"]["revision"]})
    run_until(runtime, row["id"], {"waiting_approval"})
    approve(runtime, row["id"])
    row = run_until(runtime, row["id"], {"completed", "failed", "interrupted"})
    assert row["result"] == "Shared on the second try."
    assert len(dispatched) == 2
    assert "agent_repeated_failed_write" in json.dumps(model.calls[2][1])
