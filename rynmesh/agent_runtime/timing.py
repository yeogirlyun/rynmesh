"""Read-only projection of the runtime deadline clock, separate from call usage."""
from __future__ import annotations

import math


def _seconds(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def snapshot(row, *, active_run=None, monotonic_now=None):
    """Never reconstruct missing time or persist a process-local monotonic clock.

    elapsed_seconds is the runtime's recorded deadline counter. A live tick
    extends that counter using its ORIGINAL start, not the narrower dispatch
    meter. The persisted baseline must still match to avoid double-counting a
    tick that settled between the store read and this projection.
    """
    recorded = _seconds(row.get("elapsed_seconds"))
    limit = _seconds(row.get("limits", {}).get("max_seconds"))
    pending = bool(row.get("in_flight") and (
        row.get("status") in {"running", "pausing", "cancelled"}
        or row.get("run_state") == "pausing"
        or row.get("reconciliation", {}).get("state") == "running"))
    result = {"recorded_seconds": recorded, "elapsed_seconds": recorded,
              "max_seconds": limit, "measurement": "unknown" if recorded is None else "pending" if pending else "recorded"}
    if (recorded is not None and pending and active_run is not None
            and active_run["task_id"] == row.get("id")
            and active_run["recorded_seconds"] == recorded
            and row.get("revision", -1) >= active_run["revision"]
            and _seconds(monotonic_now) is not None
            and monotonic_now >= active_run["started"]):
        result.update(elapsed_seconds=recorded + monotonic_now - active_run["started"], measurement="live")
    return result
