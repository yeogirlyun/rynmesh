"""Offline R06 mechanism evidence, never a real-model or frozen business pass."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent_runtime import assistant, make_runtime

from rynmesh.agent_runtime.tools import RynTools
from rynmesh.nas import NasPlugin


@pytest.fixture
def controller(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "docs/acceptance/ryn-agent-delivery/real-model/run_r06.py"
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location("r06_controller", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_compaction_never_fabricates_correction_or_restart(controller, tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("No compaction needed."))
    task = runtime.create({"message": "Read only", "budget_limits": {"max_total_model_calls": 4}})
    barrier = controller.CorrectionBarrier(runtime, task["id"], controller.load_case()["interventions"][0]["message"])
    result = controller.drive(runtime, task["id"], barrier=barrier)
    assert result["unmet_trigger"] == "first_compaction_committed_then_safe_correction_checkpoint"
    assert barrier.observation is None and len(model.calls) == 1
    assert not any(e["type"] == "goal_changed" for e in result["task"]["events"])


def test_frozen_document_scope_uses_actual_production_grant_schema(controller, tmp_path):
    fixture_spec = importlib.util.spec_from_file_location("r06_test_dav", controller.FIXTURES / "webdav_fixture.py")
    fixture = importlib.util.module_from_spec(fixture_spec)
    fixture_spec.loader.exec_module(fixture)
    root = tmp_path / "documents"
    (root / "projects/orion/week-41").mkdir(parents=True)
    (root / "reports/acceptance").mkdir(parents=True)
    (root / ".ryn-acceptance-isolated").touch()
    with fixture.FixtureDAV(root) as dav:
        home = tmp_path / "home"
        nas = NasPlugin(home)
        nas.enable(True)
        connection = nas.configure({"name": "docs-alpha", "system": "generic", "protocol": "webdav", "url": dav.url,
            "username": "fixture", "password": "fixture", "allow_ai": True, "writable": True})
        tools = RynTools(SimpleNamespace(state=SimpleNamespace(nas_plugin=nas)), SimpleNamespace(home=home), {}, None)
        try:
            scope = tools.prepare_scope(controller.scope_selection(connection["id"]), owner_id="owner-test",
                consumer_node_id="node-b", model_destination={"catalog_model_id": "fixture", "destination_revision": "rev1"})
            assert len(scope["resource_grants"]) == 2
            assert scope["resource_grants"][0]["actions"] == ["list", "read_ai"]
            assert set(scope["resource_grants"][1]["actions"]) == {"list", "read_ai", "create_directory", "create_file"}
        finally:
            for connection_id in list(nas.sessions):
                nas._close_session(connection_id)


WORKER = r'''
import json, os, sys, threading
from pathlib import Path
from types import SimpleNamespace
from test_agent_context_serialization import DocumentTools
from test_agent_runtime import Model, Catalog, assistant
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.services import peer_box
import run_r06 as controller

home, phase = Path(sys.argv[1]), sys.argv[2]
controller.write(home / ("worker-" + phase + ".json"), {"worker_pid": os.getpid()})
key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
tools = DocumentTools(home)
model = Model()
runtime = AgentRuntime(AgentStore(home / "agent", key), model, tools, Catalog())
if phase == "before":
    runtime.update_settings({"enabled": True, "model_id": "local:test", "max_context_chars": 12000,
                             "max_steps": 24, "max_seconds": 600})
    task = runtime.create({"message": controller.load_case()["prompt"],
                           "budget_limits": {"max_total_model_calls": 4}})
else:
    task = runtime.get(next(iter(runtime.store.read()["tasks"])))
    before = controller.checkpoint_state(runtime, task["id"])
    runtime.reconcile()
    after = controller.checkpoint_state(runtime, task["id"])
    assert before == after
    controller.write(home / "readback.json", after)

def complete(model_id, messages, specs, cancelled, on_event):
    model.calls.append((model_id, messages, specs))
    charged = runtime.get(task["id"])["budget"]["lifetime"]["charged_model_calls"]
    if not specs:
        return assistant("Observed fixture records 1 through 48; no writes. Continue reading.")
    if phase == "before":
        answer = assistant(name="ryn_search_resource", args={"page": 0}, call_id="first")
        answer["message"]["tool_calls"] += assistant(name="ryn_search_resource", args={"page": 1}, call_id="second")["message"]["tool_calls"]
        return answer
    assert controller.load_case()["interventions"][0]["message"] in [m["content"] for m in messages if m["role"] == "user"]
    if charged <= 4:
        return assistant(name="ryn_search_resource", args={"page": 2})
    return assistant("Offline mechanism complete; no real business result asserted.")
model.complete = complete
barrier = controller.CorrectionBarrier(runtime, task["id"], controller.load_case()["interventions"][0]["message"]) if phase == "before" else None
result = controller.drive(runtime, task["id"], barrier=barrier)
result["model_calls"] = len(model.calls)
controller.write(home / ("result-" + phase + ".json"), result)
threading.Event().wait(60)
'''


def test_real_worker_safe_restart_preserves_correction_and_lifetime_4_plus_8(controller, tmp_path):
    script = tmp_path / "offline-worker.py"
    script.write_text(WORKER, encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root), str(root / "tests"),
           str(Path(controller.__file__).parent)])}
    observations, proofs = {}, []
    for phase in ("before", "after"):
        process = subprocess.Popen([sys.executable, str(script), str(tmp_path), phase], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        owned = controller.OwnedConsumerProcess(process)
        try:
            marker = controller.wait_file(tmp_path / f"worker-{phase}.json", process, 10)
            assert marker, process.communicate(timeout=5)
            owned.bind_worker(marker["worker_pid"])
            result = controller.wait_file(tmp_path / f"result-{phase}.json", process, 20)
            assert result, process.communicate(timeout=5)
            observations[phase] = result
        finally:
            proofs.append(owned.terminate())
            owned.close()
            process.communicate(timeout=5)
        assert proofs[-1]["confirmed_terminal"]
        if phase == "before":
            assert "safe_checkpoint" in result, result
            assert result["safe_checkpoint"]["checkpoint"]["safe"]
            assert result["safe_checkpoint"]["checkpoint"]["status"] == "queued"
            assert result["model_calls"] == 2  # Main plus actual production summary.
    readback = json.loads((tmp_path / "readback.json").read_text(encoding="utf-8"))
    assert readback == observations["before"]["safe_checkpoint"]["checkpoint"]
    assert len(readback["compactions"]) == 1
    resumed = observations["after"]["owner_continue"]
    assert resumed["stopped"]["error"] == "agent_model_call_budget_exhausted"
    assert resumed["stopped"]["budget"]["lifetime"]["charged_model_calls"] == 4
    assert resumed["before_explicit_continue"]["status"] == "failed"
    assert resumed["calls_after_budget_before_owner_continue"] == 0
    assert resumed["budget_request"]["limits"]["max_total_model_calls"] == 12
    assert resumed["continue_request"]["message"] == "按最新要求继续。"
    assert observations["after"]["task"]["status"] == "completed"
    assert observations["after"]["task"]["budget"]["lifetime"]["charged_model_calls"] == 5
    assert proofs[0]["actual_worker_pid"] != proofs[1]["actual_worker_pid"]
