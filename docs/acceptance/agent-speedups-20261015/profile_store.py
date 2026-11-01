"""Cost of one Agent state transaction as encrypted history grows.

Usage: python profile_store.py [TASKS]   (from the repository root, default 20)

Builds synthetic completed tasks (600 small events and 120 tool messages each)
plus one live task in a temporary directory, then times a one-event
transaction on the live task. Synthetic data only; no Agent home is read.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime.protocol import event
from rynmesh.agent_runtime.store import AgentStore, encoded


def task(task_id, count):
    return {"id": task_id, "status": "completed", "updated_at": "x", "event_cursor": count, "control_revision": 0, "revision": count,
            "events": [{"seq": k, "type": "text_delta", "at": "2026-10-15T00:00:00+00:00", "context_epoch": 0, "step": k, "text": "word " * 30}
                       for k in range(count)],
            "messages": [{"role": "tool", "tool_call_id": f"c{k}", "content": json.dumps({"path": "x", "text": "line\n" * 200}), "context_epoch": 0}
                         for k in range(count // 5)],
            "receipts": [{"id": f"r{k}", "result": {"a": k, "b": [1, 2, 3]}} for k in range(count // 5)], "queue": []}


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    root = Path(tempfile.mkdtemp())
    store = AgentStore(root, X25519PrivateKey.generate())
    store.transaction(lambda data: data["tasks"].update({f"agent_{i}": task(f"agent_{i}", 600) for i in range(count)}, live=task("live", 4)))
    state_mb = len(encoded(store.read())) / 1e6
    samples = []
    for _ in range(10):
        started = time.perf_counter()
        store.transaction(lambda data: event(data["tasks"]["live"], "text_delta", text="hi"))
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    print(f"state {state_mb:.1f} MB plaintext; one-event transaction median {samples[5]:.1f} ms, max {samples[-1]:.1f} ms")


if __name__ == "__main__":
    main()
