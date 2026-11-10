"""Independent no-API review of draft RPC boundaries and default v1 collection."""
import copy
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "docs/acceptance/ryn-agent-delivery/reference"


@pytest.fixture
def rpc_host(monkeypatch):
    spec = importlib.util.spec_from_file_location("independent_role_rpc_host", REFERENCE / "worker_host.py")
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    config = {"session_id": "native-session", "max_seconds": 1, "max_model_calls": 2,
              "dispatch_provenance": {"sampling_binding": {"run_nonce": "fresh-nonce"},
                                      "task_id": "actual-task", "registration_sha256": "a" * 64}}
    context = {**copy.deepcopy(config["dispatch_provenance"]), "rpc_id": 1,
               "id": "native-session:rpc:1", "session_id": "native-session", "method": "model",
               "emitted_at": "2026-10-10T08:00:00Z"}
    calls = []

    def run(packet, selected_config):
        child = SimpleNamespace(
            pid=12345,
            stdin=io.StringIO(), stderr=io.StringIO(),
            stdout=io.StringIO(json.dumps(packet) + "\n" + json.dumps({"kind": "result", "result": {"ok": True}}) + "\n"),
            poll=lambda: 0, wait=lambda **_: 0)
        monkeypatch.setattr(host.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=host.PIN, returncode=0))
        monkeypatch.setattr(host.subprocess, "Popen", lambda *a, **k: child)
        def dispatch(*args, **kwargs):
            calls.append((args, kwargs))
            return {"synthetic": True}
        return host.run_worker(selected_config, dispatch, node=ROOT / "unused-node", upstream=ROOT / "unused-upstream")

    return config, context, calls, run


@pytest.mark.parametrize("field", ["rpc_id", "id", "session_id", "method", "sampling_binding", "task_id", "registration_sha256"])
def test_native_mismatched_binding_never_reaches_business_dispatch(rpc_host, field):
    config, context, calls, run = rpc_host
    context[field] = "wrong"
    packet = {"kind": "rpc", "id": 1, "method": "model", "params": {"messages": []}, "dispatch_provenance": context}
    with pytest.raises(ValueError, match="reference_dispatch_provenance_mismatch"):
        run(packet, config)
    assert calls == []


@pytest.mark.parametrize("draft", [False, True])
def test_host_preserves_v1_two_argument_dispatch_and_v2_separate_context(rpc_host, draft):
    config, context, calls, run = rpc_host
    if not draft:
        config.pop("dispatch_provenance")
    params = {"messages": [{"role": "user", "content": "synthetic business input"}], "tools": []}
    packet = {"kind": "rpc", "id": 1, "method": "model", "params": copy.deepcopy(params)}
    if draft:
        packet["dispatch_provenance"] = context
    assert run(packet, config)["result"] == {"ok": True}
    assert calls == [(("model", params), {"request_context": context} if draft else {})]


def test_default_q06_cli_still_executes_as_receiver_and_emits_no_draft_capture():
    command = [sys.executable, str(ROOT / "docs/acceptance/ryn-agent-delivery/real-model/run_queries.py"),
               "--case", "Q06", "--fixture-only", "--cohort", "ryn"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=150)
    assert result.returncode == 0, result.stdout + result.stderr
    outputs = [json.loads(line)["output"] for line in result.stdout.splitlines() if line.startswith('{"output":')]
    assert outputs
    output = Path(outputs[-1])
    record = json.loads((output / "run.json").read_text(encoding="utf-8"))
    assert "error_type" not in record, record.get("error_code", record.get("error_type"))
    assert record["kind"] == "local-query-collector-check" and not record["real_model"]
    assert record["consumer_peer_id"] == record["bindings"]["node-b"] != record["bindings"]["node-a"]
    assert record["task"]["status"] == "completed"
    assert record["agent_state_isolation"]["provider_task_ids"] == []
    assert record["agent_state_isolation"]["consumer_task_ids"] == [record["task"]["id"]]
    assert all("dispatch_provenance" not in call for call in record["model_calls"])
    assert all(field not in record for field in ("sampling_binding", "registration", "dispatch_audit", "model_requests"))
    assert not any((output / name).exists() for name in
                   ("registration.json", "task-binding.json", "dispatch-events.jsonl", "native-rpc-events.jsonl"))
