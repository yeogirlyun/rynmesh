"""Keep the active Agent store under its size and task ceilings.

When the active store nears its decrypted-size ceiling or task-count limit, the
least recently updated finished tasks move to the existing per-task archive
stores. Nothing is deleted and the on-disk format is unchanged: older Ryn
versions already list, open and restore these archives.
"""
from __future__ import annotations

from .lifecycle import eligible
from .store import MAX_PLAINTEXT, encoded

# Start archiving at two thirds of the ceiling and stop at half, so one model
# step (reserved output plus a bounded tool result) never meets the ceiling.
HIGH_WATER_BYTES = MAX_PLAINTEXT * 2 // 3
LOW_WATER_BYTES = MAX_PLAINTEXT // 2
# The store refuses a 201st active task.
HIGH_WATER_TASKS = 190
LOW_WATER_TASKS = 180
# The newest top-level conversations stay active whatever their size.
KEEP_RECENT = 10


def needed(usage: dict, *, need_bytes: int = 0, need_tasks: int = 0) -> bool:
    return (usage["total"] + need_bytes > HIGH_WATER_BYTES
            or usage["count"] + need_tasks > HIGH_WATER_TASKS)


def satisfied(total: int, count: int) -> bool:
    return total <= LOW_WATER_BYTES and count <= LOW_WATER_TASKS


def _protected(data: dict) -> set[str]:
    """Tasks an automation still writes to, or one whose run has not settled."""
    keep = set()
    for item in (data.get("automations") or {}).values():
        if isinstance(item, dict) and str(item.get("status") or "ACTIVE").upper() == "ACTIVE" and item.get("target_task_id"):
            keep.add(str(item["target_task_id"]))
    for run in (data.get("automation_runs") or {}).values():
        if isinstance(run, dict) and run.get("status") == "IN_PROGRESS" and run.get("task_id"):
            keep.add(str(run["task_id"]))
    return keep


def candidates(data: dict, sizes: dict, exclude: set[str]) -> list[dict]:
    """Archivable tasks, least recently updated first, with their stored size."""
    rows = data["tasks"]
    top_level = sorted((row for row in rows.values() if not row.get("parent_task_id")),
                       key=lambda row: row.get("updated_at") or "", reverse=True)
    keep = _protected(data) | set(exclude) | {row["id"] for row in top_level[:KEEP_RECENT]}
    found = []
    for row in sorted(rows.values(), key=lambda row: row.get("updated_at") or ""):
        if (row["id"] in keep or row.get("archived") or row.get("parent_task_id")
                or row.get("subagent_ids") or not eligible(row)):
            continue
        size = sizes.get(row["id"])
        found.append({"id": row["id"], "control_revision": row["control_revision"],
                      "bytes": size if type(size) is int else len(encoded(row))})
    return found
