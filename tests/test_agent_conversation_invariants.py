"""Pinned rules: a conversation that is not working can always continue (I1), and an
unconfirmed write is never resumed automatically (I3).

Anyone adding a new way to lock a conversation must change this test first and
write the reason next to the new exception.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from rynmesh.agent_runtime.control import allowed_actions, ambiguous

SETTINGS = {"enabled": True, "permission_mode": "confirm_changes"}
IDLE = ("paused", "completed", "failed", "interrupted", "cancelled")
ERRORS = (None, "agent_write_outcome_unknown", "agent_node_restarted", "agent_image_input_unsupported",
          "agent_task_limit", "agent_model_unavailable", "agent_cancelled")
FLIGHTS = {
    "none": None,
    "dead_write": {"kind": "tool", "name": "share_service", "effect": "write", "dispatched": True,
                   "call_id": "call_w", "operation_id": "op_w", "arguments": {"service_id": "svc"}},
    "undispatched_write": {"kind": "tool", "name": "share_service", "effect": "write", "dispatched": False,
                           "call_id": "call_u", "operation_id": "op_u", "arguments": {"service_id": "svc"}},
}
RECONCILIATIONS = {
    "none": None,
    "running_fresh": {"state": "running", "attempts": 1, "started_at": datetime.now(timezone.utc).isoformat()},
    "finished_unknown": {"state": "unknown", "attempts": 3},
    "stale_pending": {"state": "pending", "attempts": 0,
                      "requested_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()},
}


def _row(status, error, flight, reconciliation):
    row = {"id": "agent_" + "a" * 32, "status": status, "error": error, "in_flight": flight, "queue": [],
           "pending_approval": None, "pending_input": None, "permission_mode": "confirm_changes",
           "goal": {"revision": 1}, "archived": False, "local_execution": {"workspace_root": "C:/ryn-test"}}
    if reconciliation:
        row["reconciliation"] = reconciliation
    return row


def _not_working(row):
    """Nothing runs: idle status and no live operation (a dead unconfirmed write is not live)."""
    if row["status"] not in IDLE:
        return False
    return row["in_flight"] is None or ambiguous(row)


CASES = list(itertools.product(IDLE, ERRORS, FLIGHTS, RECONCILIATIONS))


@pytest.mark.parametrize("status,error,flight,reconciliation", CASES)
def test_idle_conversations_can_continue_switch_model_and_be_removed(status, error, flight, reconciliation):
    row = _row(status, error, FLIGHTS[flight], RECONCILIATIONS[reconciliation])
    actions = allowed_actions(row, SETTINGS)
    uncertain = ambiguous(row)
    if uncertain:
        assert not actions["resume"]["enabled"]  # I3: never resumed automatically.
    if not _not_working(row):
        return
    if uncertain and reconciliation == "running_fresh":
        # Documented exception: a reconciliation that can still finish holds input for at most
        # two minutes (control.RECONCILIATION_RUNNING_SECONDS); the reason is shown to the owner.
        assert actions["information"]["reason"] == "agent_reconciliation_running"
        return
    for action in ("information", "model", "delete", "archive"):
        assert actions[action]["enabled"], (action, status, error, flight, reconciliation)


def test_an_interrupted_read_can_be_continued_and_removed():
    flight = {"kind": "tool", "name": "ryn_file_read", "effect": "read", "dispatched": True,
              "call_id": "call_r", "operation_id": "op_r", "arguments": {"path": "a.txt"}}
    actions = allowed_actions(_row("interrupted", "agent_node_restarted", flight, None), SETTINGS)
    assert actions["information"]["enabled"] and actions["delete"]["enabled"]
    # Documented exception: switching model first needs the read settled, which sending a
    # message or Resume does; the switch button stays off until then.
    assert not actions["model"]["enabled"]


def test_disabled_agent_and_archived_tasks_are_the_only_global_exceptions():
    row = _row("completed", None, None, None)
    assert not allowed_actions(row, {**SETTINGS, "enabled": False})["information"]["enabled"]
    archived = {**row, "archived": True}
    assert allowed_actions(archived, SETTINGS)["restore"]["enabled"]
    assert allowed_actions(archived, SETTINGS)["delete"]["enabled"]
