"""Split a recorded Agent task into model time and runtime gaps.

Usage: python step_gaps.py TASK.json [TASK.json ...]

TASK.json is a task as returned by GET /api/local/agent/tasks/{id} (or a file
with a top-level "task"). Prints, per transition, the count, median and max:
  model_started -> assistant      time inside one model call
  assistant -> tool_started       scheduling before a proposed tool runs
  tool_result -> model_started    scheduling before the next model call
Owner waits (approval, input, new messages) are excluded.
"""
import json
import statistics
import sys
from datetime import datetime


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


def gaps(events):
    found = {"model_started -> assistant": [], "assistant -> tool_started": [], "tool_result -> model_started": []}
    for first, second in zip(events, events[1:]):
        if first["type"] == "assistant" and second["type"] in {"tool_started", "automatically_authorized"}:
            found["assistant -> tool_started"].append(stamp(second["at"]) - stamp(first["at"]))
    last_result = last_model = None
    for item in events:
        if item["type"] == "tool_result":
            last_result = stamp(item["at"])
        elif item["type"] == "model_started":
            if last_result is not None:
                found["tool_result -> model_started"].append(stamp(item["at"]) - last_result)
                last_result = None
            last_model = stamp(item["at"])
        elif item["type"] == "assistant" and last_model is not None:
            found["model_started -> assistant"].append(stamp(item["at"]) - last_model)
            last_model = None
        if item["type"] in {"message_added", "information_added", "approval_requested", "input_requested"}:
            last_result = None
    return found


def main():
    for path in sys.argv[1:]:
        data = json.load(open(path, encoding="utf-8"))
        print(path)
        for name, values in gaps(data.get("task", data)["events"]).items():
            if values:
                print(f"  {name:30s} n={len(values):3d} median={statistics.median(values):6.2f}s max={max(values):6.2f}s")


if __name__ == "__main__":
    main()
