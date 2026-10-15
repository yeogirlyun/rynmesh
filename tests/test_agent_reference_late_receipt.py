"""Native R05 committed write, owner cancel, and the original late receipt."""
import copy
import json
import threading
import time
from types import SimpleNamespace

from test_agent_r05_controller import controller  # noqa: F401
from test_agent_reference_recovery import native  # noqa: F401

from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.agent_runtime.tools import RynTools
from rynmesh.nas import NasPlugin
from rynmesh.services import peer_box


def test_native_commit_cancel_original_late_receipt_and_same_session_readback(controller, native, tmp_path, record_property):  # noqa: F811
    from documents import execute
    from worker_host import run_worker

    home, root = tmp_path / "node", tmp_path / "dav"
    (root / "reports/acceptance").mkdir(parents=True)
    (root / ".ryn-acceptance-isolated").touch()
    with controller.CommitResponseDAV(root) as dav:
        nas = NasPlugin(home)
        nas.enable(True)
        source = nas.configure({"name": "docs-alpha", "system": "generic", "protocol": "webdav", "url": dav.url,
            "username": "fixture", "password": "fixture", "allow_ai": True, "writable": True})
        app, store = SimpleNamespace(state=SimpleNamespace(nas_plugin=nas)), SimpleNamespace(home=home)
        tools = RynTools(app, store, {}, None)
        calls = []

        class Planner:
            supports_destination_binding = True

            def complete(self, _model, messages, _specs, _cancelled, **_options):
                from importlib import import_module
                helper = import_module("docs.acceptance.ryn-agent-delivery.real-model.planner_discovery")
                discovery = helper.discover(_specs, ["ryn_list_resource_roots", "ryn_create_text_file"])
                if discovery:
                    return discovery
                messages = helper.business_history(messages)
                calls.append(messages)
                roots = next((json.loads(m["content"])["roots"] for m in messages
                              if m["role"] == "tool" and "roots" in json.loads(m["content"])), None)
                name, args = "ryn_list_resource_roots", {}
                if roots:
                    name, args = "ryn_create_text_file", {"directory_ref": roots[0]["resource_ref"],
                        "name": "late-receipt.md", "content": controller.CONTENT}
                return {"message": {"role": "assistant", "content": "", "tool_calls": [{"id": f"call-{len(calls)}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}]}, "usage": {"input_tokens": 10, "output_tokens": 10}}

        bridge = Planner()
        model = {"id": "fixture", "model_id": "fixture", "source": "local_api", "available": True,
                 "agent_supported": True, "peer_id": "local", "source_id": "fixture", "destination_revision": "rev1",
                 "context_window": 100000, "max_output_tokens": 4096}
        catalog = SimpleNamespace(list_models=lambda: [model], resolve=lambda _: model)
        runtime = AgentRuntime(AgentStore(home / "agent", peer_box.load_or_create_messaging_key(home / "messaging.x25519")),
                               bridge, tools, catalog, consumer_node_id="local")
        runtime.update_settings({"enabled": True, "model_id": "fixture", "max_context_chars": 100000,
                                 "max_steps": 24, "max_seconds": 600})
        prompt = next(s["prompt"] for s in controller.load_case()["setup"]["subscenarios"] if s["id"] == "late_write_receipt")
        task = runtime.create({"message": prompt, "scope": {"resources": [{"connection_id": source["id"],
            "root_path": "reports/acceptance", "recursive": True, "actions": ["list", "read_ai", "create_file", "create_directory"]}],
            "allow_model_data": True}, "expected_destination_revision": "rev1"})
        proposals, errors = [], []
        record = {"prompt": prompt, "approvals": []}

        def approve(request):
            context = runtime._execution_context(runtime.store.read()["tasks"][task["id"]])
            _, _, path, content = tools.resource_writes.target(request["name"], request["arguments"], context)
            return path == controller.TARGET and content == controller.CONTENT.encode()

        def work():
            try:
                execute(runtime=runtime, tools=tools, catalog=catalog, selected="fixture", task=task, bridge=bridge,
                        record=record, home=home, node=native[0], upstream=native[1], real_model=False,
                        native_business_operations=True, allow_write=approve,
                        before_approval=lambda op, request: proposals.append({"operation_id": op, **request}))
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        try:
            assert dav.committed.wait(15), errors
            current = runtime.get(task["id"])
            dispatched = runtime.store.read()["tasks"][task["id"]]["in_flight"]
            assert current["status"] == "running" and dispatched["dispatched"]
            operation_id = dispatched["operation_id"]
            cancelled = runtime.action(task["id"], {"action": "cancel", "expected_control_revision": current["control_revision"]})
            cancel_time = time.perf_counter_ns()
            assert cancelled["status"] == "cancelled" and not dav.release.is_set()
            dav.release.set()
            worker.join(15)
            assert not worker.is_alive() and not errors, errors
            row = runtime.store.read()["tasks"][task["id"]]
            operation = copy.deepcopy(row["reference_operations"][operation_id])
            assert operation["state"] == "returned" and operation["result"]["business_acknowledged"] is True
            assert row["status"] == "cancelled" and row["control_revision"] == cancelled["control_revision"]
            assert tools.resource_writes.ledger.get(operation_id)["status"] == "confirmed"
            assert (root / controller.TARGET).read_bytes() == controller.CONTENT.encode()
            assert len([e for e in dav.audit if e["method"] == "PUT"]) == 1 and len(calls) == 2
            assert dav.barrier["commit_counter_ns"] < cancel_time < dav.barrier["release_counter_ns"]
            assert dav.barrier["original_response_count"] == 1 and not dav.barrier.get("hold_expired")
            stopped = record["reference"]["result"]
            assert stopped["owner_control"] == {"action": "cancel", "result": stopped["owner_control"]["result"]}
            assert stopped["owner_control"]["result"]["kind"] == "stopped"
            ledger = copy.deepcopy(row["budget"])
            readback = run_worker({"session_id": stopped["session_id"], "session_db_path": str(home / "reference-session.sqlite"),
                "resume": True, "rpc_sequence_start": record["reference"]["last_rpc_id"], "provider_id": "ryn-source",
                "model_id": "fixture", "context_window": 100000, "max_model_calls": 24, "max_seconds": 60,
                "tools": [], "tool_metadata": {}}, lambda *_: (_ for _ in ()).throw(AssertionError("Readback must not dispatch")),
                node=native[0], upstream=native[1])
            assert readback["worker_pid"] != record["reference"]["worker_pid"]
            assert readback["result"]["persisted_history_unchanged"]
            assert all(value == 0 for value in readback["dispatch_counts"].values())
            native_write = next(item for item in readback["result"]["persisted_tools"] if item["tool"] == "ryn_create_text_file")
            assert operation_id == stopped["session_id"] + ":" + native_write["call_id"]
            assert native_write["status"] == "error" and readback["result"]["target_status"] == "paused"
            final = runtime.store.read()["tasks"][task["id"]]
            assert final["budget"] == ledger and final["reference_operations"][operation_id] == operation
            assert final["status"] == "cancelled"
            record_property("native_late_receipt", json.dumps({"record": record, "readback": readback,
                "operation": operation, "operation_id": operation_id, "response_barrier": dav.barrier,
                "cancel_counter_ns": cancel_time, "effective_writes": 1, "put_attempts": 1,
                "host_original_receipt_retained": True, "task_cancelled": True,
                "native_original_receipt_retained": False, "native_write_status": native_write["status"],
                "native_target_status": readback["result"]["target_status"],
                "case_pass": False, "capability_limit": "host preserves actual late receipt; native public stop saves tool error and paused target",
                "model_calls": len(calls), "new_dispatches_after_cancel": 0, "formal_pass": False}))
        finally:
            dav.release.set()
            worker.join(5)
            for source_id in list(nas.sessions):
                nas._close_session(source_id)


def test_native_stopped_stream_does_not_receive_late_normal_response(native, tmp_path, record_property):  # noqa: F811
    from worker_host import run_worker

    completed = []

    def dispatch(method, params, *, on_event=None):
        assert method == "model" and params["stream"] is True
        on_event({"type": "text_delta", "text": "actual-frame"})
        # A transport may finish normally after owner cancellation. Preserve
        # its audit/usage at dispatch, but never answer the native closed RPC.
        time.sleep(.05)
        response = {"message": {"role": "assistant", "content": "actual-frame"},
                    "usage": {"input_tokens": 10, "output_tokens": 2}}
        completed.append(response)
        return response

    config = {"session_id": "native-late-normal-stream", "session_db_path": str(tmp_path / "session.sqlite"),
        "provider_id": "fixture", "model_id": "fixture", "context_window": 100000,
        "max_model_calls": 4, "max_seconds": 60, "streaming": True, "prompt": "Read the synthetic marker.",
        "tools": [{"type": "function", "function": {"name": "ryn_fixture_read", "description": "Read marker",
                  "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}],
        "tool_metadata": {"ryn_fixture_read": {"effect": "read"}}}
    result = run_worker(config, dispatch, node=native[0], upstream=native[1], model_event=lambda _: "cancel")
    assert len(completed) == 1 and completed[0]["usage"] == {"input_tokens": 10, "output_tokens": 2}
    assert result["result"]["owner_control"]["result"]["kind"] == "stopped"
    assert result["dispatch_counts"] == {"model": 1, "tool": 0, "approval": 0}
    record_property("native_late_normal_stream", json.dumps({"result": result, "actual_response_audit": completed,
        "late_success_not_injected": True, "formal_pass": False}))
