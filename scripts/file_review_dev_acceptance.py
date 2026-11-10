"""Disposable dev UI backend, real installed Agent routes and file tools, no model.

Run with --home <fresh disposable directory> --port 18791. Point Vite's
VITE_RYN_NODE_BASE_URL at http://127.0.0.1:18791/api/local and open /agent?client=fixture.
Only the surrounding node shell uses fixture data. Agent APIs and disk writes are real.
Restart with the same --home to verify persisted task snapshots and undo history.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from test_agent_file_changes import environment
from test_agent_local_tools import execute


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--port", type=int, default=18791)
    args = parser.parse_args()
    base = Path(args.home).resolve()
    app, runtime, local, workspace, model = environment(base, guard=False)
    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:15173", "http://localhost:15173"],
                       allow_credentials=True, allow_methods=["GET", "POST", "PUT"], allow_headers=["Content-Type"])
    state_file = base / "acceptance.json"
    if not state_file.exists():
        task = runtime.create({"message": "文件审阅验收：查看 diff，撤销 safe.txt；再验证 conflict.txt 的用户修改不会被覆盖。"})
        runtime.store.transaction(lambda d: d["tasks"][task["id"]].update(status="paused", title="文件审阅与撤销验收"))
        context = {"task_id": task["id"], "local_execution": runtime.store.read()["tasks"][task["id"]]["local_execution"]}
        for filename in ["safe.txt", "conflict.txt"]:
            (workspace / filename).write_bytes(b"original content\n")
            execute(local, context, "ryn_file_write", {"path": filename, "content": "Agent changed content\n"}, filename)
        state_file.write_text(json.dumps({"task_id": task["id"], "workspace": str(workspace)}), encoding="utf-8")
    print(state_file.read_text(encoding="utf-8"), flush=True)
    # Workers are deliberately not started. A model call would fail and is never needed.
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    assert not model.calls


if __name__ == "__main__":
    main()
