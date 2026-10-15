"""Collector must observe diagnostic subreads without changing tool behavior."""
import copy

import pytest
from test_agent_role_dispatch_capture import capture  # noqa: F401
from test_agent_role_query_world import modules, node, runtime_for  # noqa: F401


def prepare(fixture, *, cohort="ryn", child="ryn_list_devices", child_context=None, fail=False):
    collector, controller, runtime, calls, _ = fixture
    collector.record["cohort"] = cohort

    def execute(name, arguments, **options):
        calls.append((name, arguments, options))
        if name == "ryn_get_system_diagnostics":
            return runtime.tools.execute(child, {}, operation_id="diagnostic-read-abc",
                **({"context": child_context} if child_context is not None else {}))
        if fail:
            raise ValueError("device_provider_failure")
        return {"devices": [{"peer_id": "self", "online": True}]}

    runtime.tools.execute = execute
    runtime.tools.metadata = lambda _: {"effect": "read"}
    collector.register(controller, runtime, {"id": "task-1"})
    return collector, runtime, calls


@pytest.mark.parametrize("cohort", ["ryn", "reference"])
def test_bound_diagnostic_can_observe_nested_device_read(capture, cohort):  # noqa: F811
    collector, runtime, calls = prepare(capture, cohort=cohort)
    context = {"task_id": "task-1"}
    def run():
        return runtime.tools.execute("ryn_get_system_diagnostics", {}, context=context, operation_id="top")
    if cohort == "reference":
        native = {"id": "native:rpc:1", "rpc_id": 1, "session_id": "native", "method": "tool",
            "sampling_binding": copy.deepcopy(collector.binding), "task_id": collector.task_id,
            "registration_sha256": collector.registration_hash, "emitted_at": "2026-10-11T00:00:00Z"}
        with collector.reference_dispatch("tool", {"name": "ryn_get_system_diagnostics", "tool_call_id": "top"}, native):
            result = run()
    else:
        result = run()
    assert result == {"devices": [{"peer_id": "self", "online": True}]}
    assert calls[-1] == ("ryn_list_devices", {}, {"operation_id": "diagnostic-read-abc"})
    assert len(collector.events) == 2
    parent, child = collector.events
    assert child["parent_dispatch_id"] == parent["id"]
    assert child["task_binding_basis"] == "nested_read_of_bound_tool"
    assert child["id"] != parent["id"]
    collector.finalize()
    assert collector.record["dispatch_audit"]["completed_collection"]


@pytest.mark.parametrize("child,context", [("ryn_set_model_sharing", None), ("ryn_list_devices", {"task_id": "other"})])
def test_nested_call_does_not_bypass_binding_or_expand_allowed_edges(capture, child, context):  # noqa: F811
    _, runtime, calls = prepare(capture, child=child, child_context=context)
    with pytest.raises(RuntimeError):
        runtime.tools.execute("ryn_get_system_diagnostics", {}, context={"task_id": "task-1"}, operation_id="top")
    assert len(calls) == 1


def test_nested_exception_clears_parent_and_does_not_authorize_later_contextless_call(capture):  # noqa: F811
    collector, runtime, calls = prepare(capture, fail=True)
    with pytest.raises(ValueError, match="device_provider_failure"):
        runtime.tools.execute("ryn_get_system_diagnostics", {}, context={"task_id": "task-1"}, operation_id="top")
    with pytest.raises(RuntimeError, match="durable_intent_mismatch"):
        runtime.tools.execute("ryn_list_devices", {}, operation_id="diagnostic-read-abc")
    assert len(calls) == 2 and len(collector.events) == 2


def test_actual_runtime_diagnostics_and_readback_survive_collection(node, tmp_path, modules):  # noqa: F811
    app, store = node
    module, runner = modules

    class Planner(runner.FixturePlanner):
        def complete(self, model_id, messages, specs, cancelled, on_event=None, **options):
            from importlib import import_module
            helper = import_module("docs.acceptance.ryn-agent-delivery.real-model.planner_discovery")
            discovery = helper.discover(specs, ["ryn_get_system_diagnostics"])
            if discovery:
                return discovery
            messages = helper.business_history(messages)
            if specs and not any(m["role"] == "tool" for m in messages):
                from test_agent_runtime import assistant
                return assistant(name="ryn_get_system_diagnostics")
            return super().complete(model_id, messages, [], cancelled, on_event, **options) if not specs else {
                "message": {"role": "assistant", "content": "Collector exercise only. Evidence retained."}}

    record = {"case_id": "Q01", "cohort": "ryn", "run_id": "c" * 32, "run_nonce": "d" * 32,
              "dataset_version": module.composer.DATASET_VERSION, "collector_profile": "nested-integration"}
    output = tmp_path / "capture"
    output.mkdir()
    collector = module.DraftDispatchCollector(output, record)
    with module.RoleQueryWorld(tmp_path / "world", "Q01") as world:
        world.configure(app, store)
        runtime = runtime_for(world, app, store, Planner("Q01"))
        world.on_task_created = lambda task: collector.register(world, runtime, task)
        result = world.execute(runtime)
        assert result["task"]["status"] == "completed"
        stored = runtime.store.read()["tasks"][world.task_id]
        receipt = next(r for r in stored["receipts"] if r["tool"] == "ryn_get_system_diagnostics")
        assert receipt["status"] == "confirmed" and "error" not in receipt["result"]
        assert len(receipt["result"]["models"]) == 4
        assert receipt["result"]["devices"]["devices"]
        assert all(e["status"] == "passed" for e in stored["verification_evidence"])
        children = [e for e in collector.events if e.get("parent_dispatch_id")]
        parents = [e for e in collector.events if e.get("tool_name") == "ryn_get_system_diagnostics"]
        assert len(parents) >= 2  # Original read and mechanical re-read, plus collection reads.
        assert {e["parent_dispatch_id"] for e in children} == {e["id"] for e in parents}
        assert len(children) == len(parents)
        assert all(e["tool_name"] == "ryn_list_devices" for e in children)
        collector.finalize()
        assert record["dispatch_audit"]["completed_collection"]
