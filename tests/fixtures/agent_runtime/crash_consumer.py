"""Deterministic consumer process for actual crash-boundary integration tests.

The DAV server lives in the parent process. No fixture audit is available to the
product or this consumer; recovery must use its encrypted business receipt.
"""
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from rynmesh.agent_runtime.runtime import AgentRuntime  # noqa: E402
from rynmesh.agent_runtime.store import AgentStore  # noqa: E402
from rynmesh.agent_runtime.tools import RynTools  # noqa: E402
from rynmesh.nas import NasPlugin  # noqa: E402
from rynmesh.services import peer_box  # noqa: E402


def answer(text="", name=None, arguments=None):
    message = {"role": "assistant", "content": text}
    if name:
        message["tool_calls"] = [{"id": "call-" + name, "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments or {})}}]
    return {"message": message}


class SyntheticModel:
    supports_destination_binding = True

    def complete(self, model_id, messages, specs, cancelled, on_event=None, **kwargs):
        if not specs:
            evidence = json.loads(messages[-1]["content"])
            return answer(json.dumps({"status": "passed", "summary": "Synthetic protocol check only.",
                "uncovered_requirements": [], "evidence_refs": [e["id"] for e in evidence["evidence"]]}))
        calls = {call["id"]: call["function"]["name"] for m in messages for call in m.get("tool_calls", [])}
        receipts = [(calls.get(m.get("tool_call_id")), json.loads(m["content"])) for m in messages if m["role"] == "tool"]
        roots = next((value for name, value in receipts if name == "ryn_list_resource_roots"), None)
        if roots is None:
            return answer(name="ryn_list_resource_roots")
        if any(name == "ryn_create_text_file" for name, _ in receipts):
            return answer("The original confirmed operation created recovery.md once.")
        return answer(name="ryn_create_text_file", arguments={"directory_ref": roots["roots"][0]["resource_ref"],
                      "name": "recovery.md", "content": "RECOVERY_ONCE_731"})


def main():
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    home = Path(config["home"])
    nas = NasPlugin(home)
    nas.enable(True)
    connection = next(iter(nas.state["sources"]), None)
    if connection is None:
        connection = nas.configure({"name": "docs-alpha", "system": "generic", "protocol": "webdav", "url": config["dav_url"],
                                   "username": "fixture", "password": "fixture", "allow_ai": True, "writable": True})
    store = SimpleNamespace(home=home, peer_id="isolated-consumer")
    app = SimpleNamespace(state=SimpleNamespace(nas_plugin=nas))
    catalog = SimpleNamespace(list_models=lambda: [{"id": "fixture", "source": "local_api", "available": True, "agent_supported": True,
        "peer_id": "isolated-consumer", "source_id": "fixture", "destination_revision": "fixture-revision"}])
    tools = RynTools(app, store, {}, catalog)
    key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    runtime = AgentRuntime(AgentStore(home / "agent", key), SyntheticModel(), tools, catalog, consumer_node_id=store.peer_id)
    app.state.agent_runtime = runtime
    runtime.update_settings({"enabled": True, "model_id": "fixture", "max_seconds": 60})
    tasks = runtime.store.read()["tasks"]
    if not tasks:
        current = runtime.create({"message": "Create recovery.md with RECOVERY_ONCE_731 once, without overwriting.",
            "scope": {"resources": [{"connection_id": connection["id"], "root_path": "reports/acceptance",
                "actions": ["list", "read_ai", "create_file"]}], "allow_model_data": True},
            "expected_destination_revision": "fixture-revision"})
    else:
        current = runtime.get(next(iter(tasks)))
        runtime.reconcile()
        if config.get("boundary") == "approval":
            (home / "approval-reloaded.json").write_text(json.dumps(runtime.get(current["id"])), encoding="utf-8")
    if config["phase"] == "crash" and config.get("boundary") != "approval":
        original = tools.execute

        def stop_after_business_ack(name, arguments, **kwargs):
            result = original(name, arguments, **kwargs)
            if name == "ryn_create_text_file":
                (home / "crash-ready.json").write_text(json.dumps({"operation_id": kwargs["operation_id"]}), encoding="utf-8")
                # Parent terminates this real process while the Agent transaction
                # has not yet received the returned business result.
                threading.Event().wait(60)
                raise RuntimeError("Parent failed to terminate crash fixture")
            return result

        tools.execute = stop_after_business_ack
    try:
        for _ in range(100):
            current = runtime.get(current["id"])
            recovering = current.get("reconciliation", {}).get("state") in {"pending", "running"}
            if not recovering and current["status"] in {"completed", "failed", "interrupted", "paused", "cancelled"}:
                break
            if current["status"] == "waiting_approval":
                pending = current["pending_approval"]
                if config["phase"] == "crash" and config.get("boundary") == "approval":
                    # Persisted waiting approval must survive a real process kill.
                    # The parent inspects the original identity and zero writes.
                    (home / "approval-ready.json").write_text(json.dumps(current), encoding="utf-8")
                    threading.Event().wait(60)
                    raise RuntimeError("Parent failed to terminate approval fixture")
                allowed = (pending["tool"] == "ryn_create_text_file" and pending["arguments"].get("name") == "recovery.md"
                           and pending["arguments"].get("content") == "RECOVERY_ONCE_731")
                runtime.action(current["id"], {"action": "approve" if allowed else "deny",
                    "approval_id": pending["id"], "expected_control_revision": current["control_revision"]})
            else:
                runtime.run_once()
        (home / "consumer-result.json").write_text(json.dumps(runtime.get(current["id"])), encoding="utf-8")
    finally:
        for source_id in list(nas.sessions):
            nas._close_session(source_id)


if __name__ == "__main__":
    main()
