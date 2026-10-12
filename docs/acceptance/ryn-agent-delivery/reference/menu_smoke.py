"""Pinned native reference loop with shared discovery; deterministic, no score."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_agent_tool_menu import ManyTools  # noqa: E402
from tool_host import ReferenceToolHost  # noqa: E402
from tool_menu_host import ReferenceMenuTools  # noqa: E402
from worker_host import run_worker  # noqa: E402

from rynmesh.agent_runtime import tool_menu  # noqa: E402
from rynmesh.agent_runtime.store import encoded  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--upstream", type=Path, required=True)
    args = parser.parse_args()
    backend = ManyTools()
    menu = ReferenceMenuTools(backend, max_context_bytes=12000)
    gate = ReferenceToolHost(menu, policy=lambda _: False, cancelled=lambda: False)
    trace = []

    def dispatch(method, params):
        if method == "tool":
            return gate.execute(params)
        if method == "approval":
            return gate.approval(params)
        specs = menu.project(params["messages"], params["tools"])
        size = len(encoded(params["messages"])) + len(encoded(specs))
        if size > 12000:
            raise ValueError("reference_context_limit")
        names = [s["function"]["name"] for s in specs]
        trace.append({"context_bytes": size, "exposed": names})
        if len(trace) == 1:
            name, body = tool_menu.NAME, {"load": ["inspect_service"]}
        elif len(trace) == 2:
            name, body = "inspect_service", {"service_id": "svc1"}
        else:
            assert any(m["role"] == "tool" and "svc1" in m.get("content", "") for m in params["messages"])
            return {"message": {"role": "assistant", "content": "Synthetic service read confirmed."}}
        assert name in names
        return {"message": {"role": "assistant", "content": "", "tool_calls": [{"id": f"menu-{len(trace)}",
            "type": "function", "function": {"name": name, "arguments": json.dumps(body)}}]}}

    record = {"kind": "pinned-reference-menu-fixture", "formal_case_score": False, "real_model": False}
    try:
        record["reference"] = run_worker({"session_id": gate.session, "provider_id": "fixture", "model_id": "fixture",
            "context_window": 100000, "max_model_calls": 8, "max_seconds": 30,
            "prompt": "Read the one synthetic service svc1 and report its state.", "tools": menu.specs(),
            "tool_metadata": {name: menu.metadata(name) for name in gate.allowed}},
            dispatch, node=args.node.resolve(), upstream=args.upstream.resolve())
        assert len(trace) == 3 and [call[0] for call in backend.calls] == ["inspect_service"]
        record["checks_passed"] = True
    except Exception as exc:
        record.update(checks_passed=False, error_type=type(exc).__name__)
    record.update(model_requests=trace, tool_events=gate.events, menu_state=menu.state)
    output = Path(__file__).parent / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-menu-fixture")
    output.mkdir()
    (output / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "checks_passed": record["checks_passed"]}))


if __name__ == "__main__":
    main()
