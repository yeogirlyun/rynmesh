"""Reference traffic uses durable candidate admission, including nested probes."""
import importlib.util
from pathlib import Path

import pytest
from test_agent_runtime import make_runtime

from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentModelResponseError, AgentStore

_spec = importlib.util.spec_from_file_location("reference_budget_host", Path(__file__).resolve().parents[1] /
    "docs/acceptance/ryn-agent-delivery/reference/budget_host.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
ReferenceBudget = _module.ReferenceBudget


def test_worker_bootstrap_can_be_excluded_without_extending_active_execution(tmp_path):
    runtime, _, _, _, task_id = setup(tmp_path, max_seconds=10)
    now = [100.0]
    meter = ReferenceBudget(runtime, task_id, guard=lambda: None, clock=lambda: now[0], defer_start=True)
    now[0] += 600  # Interpreter/package startup precedes the first dispatch.
    meter.model(lambda _: {"usage": {"input_tokens": 1, "output_tokens": 1}}, input_bytes=8, max_output_tokens=20)
    assert meter.owner_wait_seconds() == 0
    assert meter.deadline == 710
    now[0] = 710
    with pytest.raises(AgentError, match="agent_time_budget_exhausted"):
        meter.guard()


def test_owner_wait_excludes_only_approval_and_active_deadline_still_expires(tmp_path):
    runtime, _, _, _, task_id = setup(tmp_path, max_seconds=10)
    now = [100.0]
    meter = ReferenceBudget(runtime, task_id, guard=lambda: None, clock=lambda: now[0])
    with meter.owner_wait():
        now[0] += 600
        meter.guard()
        with pytest.raises(AgentError, match="reference_dispatch_during_owner_wait"):
            meter.model(lambda _: pytest.fail("dispatch while awaiting owner"), input_bytes=8, max_output_tokens=20)
    assert meter.owner_wait_seconds() == 600
    assert meter.snapshot()["lifetime"]["model_calls"] == 0
    def request(allowed, remaining, guard):
        assert remaining == 10
        now[0] += 3
        guard()
        return {"usage": {"input_tokens": 1, "output_tokens": 1}}
    meter.tool(lambda: meter.probe(request, input_bytes=8, max_output_tokens=20, model_id="m", source_id="s"))
    assert meter.snapshot()["lifetime"]["active_seconds"] == 3
    now[0] = 710
    with pytest.raises(AgentError, match="agent_time_budget_exhausted"):
        meter.guard()


@pytest.mark.parametrize("stop", ["pause", "disable", "permission"])
def test_owner_wait_cannot_revive_stopped_or_changed_permissions(tmp_path, stop):
    runtime, _, _, _, task_id = setup(tmp_path, max_seconds=10)
    now = [100.0]
    meter = ReferenceBudget(runtime, task_id, guard=lambda: None, clock=lambda: now[0])
    with pytest.raises(AgentError, match="agent_cancelled"):
        with meter.owner_wait():
            now[0] += 600
            if stop == "pause":
                runtime.action(task_id, {"action": "pause"})
            else:
                runtime.update_settings({"enabled": False} if stop == "disable" else {"permission_mode": "read_only"})
    assert meter.owner_wait_seconds() == 600
    assert meter.snapshot()["lifetime"]["model_calls"] == 0
    assert meter.snapshot()["lifetime"]["tool_calls"] == 0


def test_active_dispatch_cannot_hide_its_time_as_owner_wait(tmp_path):
    runtime, _, _, _, task_id = setup(tmp_path, max_seconds=10)
    now = [100.0]
    meter = ReferenceBudget(runtime, task_id, guard=lambda: None, clock=lambda: now[0])
    def invoke(_):
        now[0] += 2
        with meter.owner_wait():
            pytest.fail("active inference excluded from budget")
    with pytest.raises(AgentError, match="reference_owner_wait_during_dispatch"):
        meter.model(invoke, input_bytes=8, max_output_tokens=20)
    assert meter.owner_wait_seconds() == 0
    assert meter.snapshot()["lifetime"]["active_seconds"] == 2


def test_failed_owner_callback_restores_active_deadline(tmp_path):
    runtime, _, _, _, task_id = setup(tmp_path, max_seconds=10)
    now = [100.0]
    meter = ReferenceBudget(runtime, task_id, guard=lambda: None, clock=lambda: now[0])
    with pytest.raises(ValueError, match="owner callback failed"):
        with meter.owner_wait():
            now[0] += 600
            raise ValueError("owner callback failed")
    meter.guard()
    now[0] += 10
    with pytest.raises(AgentError, match="agent_time_budget_exhausted"):
        meter.guard()

_host_spec = importlib.util.spec_from_file_location("reference_budget_tool_host", Path(__file__).resolve().parents[1] /
    "docs/acceptance/ryn-agent-delivery/reference/tool_host.py")
_host_module = importlib.util.module_from_spec(_host_spec)
_host_spec.loader.exec_module(_host_module)


def setup(tmp_path, **limits):
    runtime, model, tools, key = make_runtime(tmp_path)
    if "max_seconds" in limits:
        runtime.update_settings({"max_seconds": limits.pop("max_seconds")})
    task = runtime.create({"message": "Reference budget acceptance", "budget_limits": limits})
    return runtime, model, tools, key, task["id"]


def test_main_and_probe_share_encrypted_call_ledger_across_reopen(tmp_path):
    runtime, model, tools, key, task_id = setup(tmp_path, max_total_model_calls=2)
    meter = ReferenceBudget(runtime, task_id, guard=lambda: None)
    meter.model(lambda allowed: {"usage": {"input_tokens": 1, "output_tokens": 2}}, input_bytes=8, max_output_tokens=20)
    meter.tool(lambda: meter.probe(lambda allowed, remaining, guard: {}, input_bytes=8,
                                  max_output_tokens=20, model_id="new", source_id="draft"))
    reopened = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    again = ReferenceBudget(reopened, task_id, guard=lambda: None)
    sent = []
    with pytest.raises(AgentError, match="agent_model_call_budget_exhausted"):
        again.model(lambda _: sent.append(True), input_bytes=8, max_output_tokens=20)
    usage = again.snapshot()["lifetime"]
    assert sent == [] and usage["model_calls"] == 2 and usage["tool_calls"] == 1
    assert usage["total_tokens"] is None and usage["charged_tokens"] == 543
    assert runtime.get(task_id)["status"] == "queued"


def test_nested_time_not_double_charged_and_timeout_guard_stops_dispatch(tmp_path):
    runtime, _, _, _, task_id = setup(tmp_path, max_seconds=10)
    clock = [100.0]
    meter = ReferenceBudget(runtime, task_id, guard=lambda: None, clock=lambda: clock[0])
    def probe(allowed, remaining, guard):
        assert remaining == 10
        clock[0] += 3
        guard()
        return {"usage": {"input_tokens": 1, "output_tokens": 1}}
    meter.tool(lambda: meter.probe(probe, input_bytes=8, max_output_tokens=20, model_id="m", source_id="s"))
    assert meter.snapshot()["lifetime"]["active_seconds"] == 3
    clock[0] = 110
    with pytest.raises(AgentError, match="agent_time_budget_exhausted"):
        meter.model(lambda _: pytest.fail("request sent after deadline"), input_bytes=8, max_output_tokens=20)
    assert meter.snapshot()["lifetime"]["model_calls"] == 1


def test_owner_pause_or_disable_prevents_reference_probe(tmp_path):
    runtime, _, _, _, task_id = setup(tmp_path)
    meter = ReferenceBudget(runtime, task_id, guard=lambda: None)
    runtime.action(task_id, {"action": "pause"})
    with pytest.raises(AgentError, match="agent_cancelled"):
        meter.probe(lambda *_: pytest.fail("request sent after pause"), input_bytes=8,
                    max_output_tokens=20, model_id="m", source_id="s")
    assert meter.snapshot()["lifetime"]["model_calls"] == 0


def test_reference_output_cap_matches_actual_remaining_reservation(tmp_path):
    runtime, _, _, _, task_id = setup(tmp_path, max_total_tokens=1000)
    meter = ReferenceBudget(runtime, task_id, guard=lambda: None)
    caps = []
    meter.model(lambda allowed: caps.append(allowed), input_bytes=100, max_output_tokens=4096)
    assert caps == [388]
    assert meter.snapshot()["lifetime"]["charged_tokens"] == 1000
    with pytest.raises(AgentError, match="agent_token_budget_exhausted"):
        meter.model(lambda _: pytest.fail("request exceeded total tokens"), input_bytes=1, max_output_tokens=1)


@pytest.mark.parametrize("stop", ["pause", "deadline", "revoke", "read_only"])
def test_inflight_model_stop_discards_late_response_and_settles(tmp_path, stop):
    runtime, _, _, _, task_id = setup(tmp_path, max_seconds=10)
    now, revoked = [100.0], [False]
    def permissions():
        if revoked[0]:
            raise AgentError("agent_resource_permission_changed")
    meter = ReferenceBudget(runtime, task_id, guard=permissions, clock=lambda: now[0])
    def request(_):
        assert not meter.cancelled()
        if stop == "pause":
            runtime.action(task_id, {"action": "pause"})
        elif stop == "deadline":
            now[0] = 111
        elif stop == "read_only":
            runtime.update_settings({"permission_mode": "read_only"})
        else:
            revoked[0] = True
        assert meter.cancelled()
        return {"usage": {"input_tokens": 2, "output_tokens": 3}, "message": "late output"}
    with pytest.raises(AgentError):
        meter.model(request, input_bytes=8, max_output_tokens=20)
    assert meter.snapshot()["lifetime"]["model_calls"] == 1
    assert meter.snapshot()["lifetime"]["charged_tokens"] == 5


def test_nested_budget_error_stops_reference_tool_host_and_keeps_audit(tmp_path):
    from test_agent_reference_host import Tools, call
    runtime, _, _, _, task_id = setup(tmp_path, max_total_model_calls=1)
    meter = ReferenceBudget(runtime, task_id, guard=lambda: None)
    meter.model(lambda _: {}, input_bytes=8, max_output_tokens=20)
    class ProbeTools(Tools):
        def execute(self, *args, **kwargs):
            return meter.probe(lambda *_: pytest.fail("probe sent without budget"), input_bytes=8,
                               max_output_tokens=20, model_id="new", source_id="draft")
    host = _host_module.ReferenceToolHost(ProbeTools(), policy=lambda _: True, cancelled=meter.cancelled)
    with pytest.raises(AgentError, match="agent_model_call_budget_exhausted"):
        meter.tool(lambda: host.execute(call("read")))
    assert host.events[-1]["result"]["error"] == "agent_model_call_budget_exhausted"
    assert host.events[-1]["state"] == "failed"
    assert meter.snapshot()["lifetime"]["model_calls"] == 1
    assert meter.snapshot()["lifetime"]["tool_calls"] == 1


def test_inflight_guard_only_uses_cheap_permissions_without_recursion(tmp_path):
    from test_agent_reference_host import Tools
    runtime, _, _, _, task_id = setup(tmp_path)
    checks = []
    class ScopedTools(Tools):
        def validate_context(self, context, *, cheap_only=False):
            checks.append(cheap_only)
    host = _host_module.ReferenceToolHost(ScopedTools(), policy=lambda _: True, cancelled=lambda: False, context={})
    meter = ReferenceBudget(runtime, task_id, guard=host.check_current_permissions)
    host.cancelled = meter.cancelled
    host.before_inference()
    assert checks == [True, False]
    checks.clear()
    assert not meter.cancelled()
    assert checks == [True]


@pytest.mark.parametrize("stop", [False, True])
def test_rejected_model_response_keeps_actual_usage_even_after_owner_stop(tmp_path, stop):
    runtime, model, tools, key, task_id = setup(tmp_path)
    meter = ReferenceBudget(runtime, task_id, guard=lambda: None)
    def rejected(_):
        if stop:
            runtime.action(task_id, {"action": "pause"})
        raise AgentModelResponseError({"input_tokens": 1123, "output_tokens": 4096}, "length")
    with pytest.raises(AgentError, match="agent_cancelled" if stop else "agent_invalid_model_response"):
        meter.model(rejected, input_bytes=8000, max_output_tokens=4096)
    reopened = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    usage = ReferenceBudget(reopened, task_id, guard=lambda: None).snapshot()["lifetime"]
    assert usage["model_calls"] == 1 and usage["charged_tokens"] == 5219
    assert usage["total_tokens"] == 5219 and usage["usage_complete"]


def test_rejected_probe_keeps_unknown_usage_conservative_and_no_double_time(tmp_path):
    runtime, _, _, _, task_id = setup(tmp_path)
    now = [100.0]
    meter = ReferenceBudget(runtime, task_id, guard=lambda: None, clock=lambda: now[0])
    def rejected(*_):
        now[0] += 2
        raise AgentModelResponseError({"input_tokens": 7, "output_tokens": True}, "length")
    with pytest.raises(AgentModelResponseError):
        meter.tool(lambda: meter.probe(rejected, input_bytes=8, max_output_tokens=20, model_id="m", source_id="s"))
    usage = meter.snapshot()["lifetime"]
    assert usage["model_calls"] == 1 and usage["tool_calls"] == 1
    assert usage["charged_tokens"] == 27 and usage["total_tokens"] is None
    assert usage["active_seconds"] == 2
