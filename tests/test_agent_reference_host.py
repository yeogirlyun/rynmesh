"""Security properties of the pinned reference's acceptance-only business gate."""
import importlib.util
from pathlib import Path

import pytest

from rynmesh.agent_runtime.store import AgentReadFailure, AgentToolRejected

_spec = importlib.util.spec_from_file_location("reference_tool_host", Path(__file__).resolve().parents[1] /
    "docs/acceptance/ryn-agent-delivery/reference/tool_host.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
ReferenceToolHost = _module.ReferenceToolHost


class Tools:
    def __init__(self):
        self.revision = 1
        self.calls = []
        self.fail_after_write = False

    def specs(self):
        return [{"function": {"name": name}} for name in ("write", "read")]

    def metadata(self, name):
        return {"effect": name, "preview_precondition": name == "write", "scope_context_supported": name == "read"}

    def _validate(self, name, arguments):
        assert name in {"read", "write"} and arguments == {"enabled": True}

    def preview(self, name, arguments, **_):
        return {"revision": self.revision, "after": arguments}

    def execute(self, name, arguments, **kwargs):
        self.calls.append((name, arguments, kwargs))
        if self.fail_after_write:
            raise OSError("private transport data")
        return {"ok": True}


def call(name="write", call_id="a"):
    return {"name": name, "arguments": {"enabled": True}, "tool_call_id": call_id}


def test_reference_gate_binds_exact_approval_and_current_preview():
    tools = Tools()
    host = ReferenceToolHost(tools, policy=lambda _: True, cancelled=lambda: False)
    with pytest.raises(AgentToolRejected, match="approval_required"):
        host.execute(call())
    assert host.approval(call()) == {"approved": True}
    with pytest.raises(AgentToolRejected, match="approval_required"):
        host.execute(call(call_id="b"))
    tools.revision += 1
    with pytest.raises(AgentToolRejected, match="approval_stale"):
        host.execute(call())
    assert tools.calls == []


def test_reference_gate_deny_and_cancel_do_not_dispatch():
    tools = Tools()
    stopped = False
    host = ReferenceToolHost(tools, policy=lambda _: False, cancelled=lambda: stopped)
    assert not host.approval(call())["approved"]
    with pytest.raises(AgentToolRejected, match="approval_required"):
        host.execute(call())
    stopped = True
    with pytest.raises(AgentToolRejected, match="cancelled"):
        host.execute(call("read"))
    assert tools.calls == []


@pytest.mark.parametrize("uncertain", [False, True])
def test_reference_gate_never_replays_dispatched_write(uncertain):
    tools = Tools()
    tools.fail_after_write = uncertain
    host = ReferenceToolHost(tools, policy=lambda _: True, cancelled=lambda: False)
    host.approval(call())
    result = host.execute(call())
    assert result == ({"error": "agent_tool_result_unknown"} if uncertain else {"ok": True})
    with pytest.raises(ValueError, match="duplicate_dispatch"):
        host.execute(call())
    assert len(tools.calls) == 1
    assert "private transport" not in str(host.events)
    assert tools.calls[0][2]["expected_preview"]["revision"] == 1


def test_reference_gate_resource_body_has_no_replay_cache():
    tools = Tools()
    context = object()
    host = ReferenceToolHost(tools, policy=lambda _: False, cancelled=lambda: False, context=context)
    host.execute(call("read"))
    assert tools.calls[0][2]["context"] is context
    with pytest.raises(ValueError, match="duplicate_dispatch"):
        host.execute(call("read"))


def test_reference_preserves_same_safe_timeout_evidence_without_claiming_write_failure():
    class TimeoutTools(Tools):
        def execute(self, *args, **kwargs):
            raise AgentReadFailure("resource_read_timeout", connection_id="docs-offline",
                                   connection_name="docs-offline token=PRIVATE_TEST_KEY")
    host = ReferenceToolHost(TimeoutTools(), policy=lambda _: True, cancelled=lambda: False)
    result = host.execute(call("read"))
    assert result == {"error": "resource_read_timeout", "not_executed": False, "no_mutation": True,
                      "connection_id": "docs-offline", "connection_name": "docs-offline [redacted]"}
    assert host.events[-1]["state"] == "failed" and "PRIVATE_TEST_KEY" not in str(host.events)
    host.approval(call("write", "write-call"))
    result = host.execute(call("write", "write-call"))
    assert result == {"error": "agent_tool_result_unknown"} and host.events[-1]["state"] == "unknown"


def test_reference_model_gate_tracks_origins_and_rechecks_revocation():
    class ResourceTools(Tools):
        revoked = False

        def execute(self, *args, **kwargs):
            super().execute(*args, **kwargs)
            return {"text": "synthetic data", "origin": {"grant_id": "fixture", "version": "v1"}}

        def validate_context(self, context):
            assert context["resource_origins"] == [{"grant_id": "fixture", "version": "v1"}]
            if self.revoked:
                raise AgentToolRejected("agent_resource_permission_changed")

    tools = ResourceTools()
    host = ReferenceToolHost(tools, policy=lambda _: False, cancelled=lambda: False, context={"resource_origins": []})
    returned = host.execute(call("read"))
    host.before_inference()
    returned["origin"]["version"] = "caller changed copy"
    host.before_inference()
    tools.revoked = True
    with pytest.raises(AgentToolRejected, match="permission_changed"):
        host.before_inference()
