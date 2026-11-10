"""Offline checks for C04's exact approval and shared paid-dispatch budget."""
import copy
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

HARNESS = Path(__file__).resolve().parents[1] / "docs/acceptance/ryn-agent-delivery/real-model"
sys.path.insert(0, str(HARNESS))
spec = importlib.util.spec_from_file_location("c04_development_controller", HARNESS / "run_access_c04.py")
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


def approval_fixture():
    world = controller.C04World.__new__(controller.C04World)
    world.sid, world.rid = "model-a-service", "a" * 32
    world.receiver = SimpleNamespace(peer_id="signed-node-b")
    world.bindings = {"model-a": "local/model-a-service"}
    grant = {"service_id": world.sid, "relationship_id": world.rid,
             "peer_id": world.receiver.peer_id, "allowed": True, "effective": True, "revision": 1}
    world.grants = SimpleNamespace(grant=lambda sid, rid: copy.deepcopy(grant))
    approval = {"tool": "ryn_set_friend_model_access", "arguments": {
        "service_id": world.sid, "relationship_id": world.rid, "allowed": False, "expected_revision": 1},
        "preview": {"service_id": world.sid, "relationship_id": world.rid,
                    "peer_id": world.receiver.peer_id, "before": grant, "after": {"allowed": False}}}
    return world, approval


def test_revoke_requires_both_probe_phase_and_exact_operation():
    world, approval = approval_fixture()
    assert world.permitted(approval, revoke=True)
    assert not world.permitted(approval, revoke=False)


@pytest.mark.parametrize("change", [
    lambda a: a["arguments"].update(service_id="model-b-service"),
    lambda a: a["arguments"].update(relationship_id="c" * 32),
    lambda a: a["arguments"].update(expected_revision=0),
    lambda a: a["preview"].update(peer_id="signed-node-c"),
    lambda a: a["preview"]["after"].update(allowed=True),
    lambda a: a.update(tool="ryn_set_model_sharing"),
])
def test_rejects_wrong_model_receiver_revision_or_effect(change):
    world, approval = approval_fixture()
    change(approval)
    assert not world.permitted(approval, revoke=True)


def test_receiver_probe_consumes_same_attempt_cap_as_planner():
    requests = []
    def chat(body, **options):
        requests.append((body, options))
        return {"usage": {"input_tokens": 8, "output_tokens": 1}, "finish_reason": "stop"}
    record = {"real_model_dispatches": []}
    world = SimpleNamespace(deadline=time.monotonic() + 10, phase="initial_task")
    adapter = controller.CountedAdapter(SimpleNamespace(chat=chat), world, record, 1)
    adapter.chat({"messages": [], "max_tokens": 10}, timeout_s=600)
    assert 0 < requests[0][1]["timeout_s"] <= 10
    world.phase = "before_revoke_probes"
    with pytest.raises(controller.AdapterError):
        adapter.chat({"messages": [], "max_tokens": 10}, timeout_s=600)
    assert len(requests) == len(record["real_model_dispatches"]) == 1


def test_expired_attempt_never_dispatches_or_claims_usage():
    record = {"real_model_dispatches": []}
    adapter = controller.CountedAdapter(None, SimpleNamespace(deadline=time.monotonic()-1,
        phase="after_revoke_probe"), record, 24)
    with pytest.raises(controller.AdapterError):
        adapter.chat({"messages": []}, timeout_s=600)
    assert record["real_model_dispatches"] == []


def test_live_probe_counts_actual_adapter_not_loopback_setup(monkeypatch):
    world = controller.C04World.__new__(controller.C04World)
    world.real_dispatches = []
    def inherited_probe(self, logical, sender, row):
        self.real_dispatches.append({"phase": "before_revoke_probes"})
        return {"result": "allowed", "upstream_requests": 0}
    monkeypatch.setattr(controller.SharingWorld, "probe_receiver", inherited_probe)
    result = world.probe_receiver("model-a", object())
    assert result["upstream_requests"] == 1
    assert result["loopback_http_requests"] == 0
    assert result["upstream_evidence_basis"] == "counted_actual_GLM_adapter_dispatches"


def test_adapter_failure_retains_safe_known_usage_not_exception_text():
    def chat(body, **options):
        raise controller.AdapterError("MUST_NOT_EXPORT_RAW_PROVIDER_TEXT", code="inference_timeout",
            reported_usage={"input_tokens": 7, "output_tokens": 2}, finish_reason="length")
    record = {"real_model_dispatches": []}
    adapter = controller.CountedAdapter(SimpleNamespace(chat=chat), SimpleNamespace(
        deadline=time.monotonic()+10, phase="before_revoke_probes"), record, 24)
    with pytest.raises(controller.AdapterError):
        adapter.chat({"messages": []}, timeout_s=600)
    entry = record["real_model_dispatches"][0]
    assert entry["usage"] == {"input_tokens": 7, "output_tokens": 2}
    assert entry["finish_reason"] == "length" and entry["error_code"] == "inference_timeout"
    assert "MUST_NOT_EXPORT_RAW_PROVIDER_TEXT" not in repr(entry)
