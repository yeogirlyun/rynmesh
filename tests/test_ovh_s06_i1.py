"""S06-I1: shared host read eligibility reaches queue, stream and dispatch gate."""
import copy
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agent_tool_fixtures import expose
from test_agent_runtime import assistant, make_runtime
from test_agent_integrations import node  # noqa: F401
from test_agent_extensions_runtime import ext_env, _write_fake_mcp_server  # noqa: F401
from test_agent_tool_dispatch import host, cell  # noqa: F401

from rynmesh.agent_runtime import code_mode
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.execution_policy import shell_read
from rynmesh.agent_runtime.protocol import model_retry_authority
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore, AgentToolRejected
from rynmesh.agent_runtime.streamed_reads import StreamedReads
from rynmesh.agent_runtime.tool_dispatch import PARALLEL_READS, supports_parallel
from rynmesh.agent_runtime.extensions import compute_model_tool_name


SHELL_META = {"effect": "write", "scope_context_supported": True}


def raw(index, name, arguments):
    return assistant(name=name, args=arguments, call_id=f"call_{index}")["message"]["tool_calls"][0]


def response(calls):
    return {"message": {"role": "assistant", "content": "", "tool_calls": calls}}


def test_t1_literal_shell_shares_gate():
    args = {"command": "git status"}
    assert shell_read(args) is True
    assert supports_parallel("ryn_shell_execute", SHELL_META, args) is True


@pytest.mark.parametrize("args", [
    {"command": "git status", "background": True}, {"command": "git status", "yield_seconds": 0},
    {"command": "git status", "tty": True}, {"command": "rm target.txt"},
    {"command": "git commit -m change"}, {"command": "git status > saved.txt"},
])
def test_t2_nonliteral_or_interactive_shell_stays_exclusive(args):
    assert supports_parallel("ryn_shell_execute", SHELL_META, args) is False


@pytest.mark.parametrize("meta", [{**SHELL_META, "requires_explicit_approval": True}, {**SHELL_META, "computer_access": True}])
def test_approval_and_desktop_never_get_read_parallelism(meta):
    assert supports_parallel("ryn_shell_execute", meta, {"command": "git status"}) is False


class ConcurrentTools:
    def __init__(self, groups=(), builtin=None):
        self.groups = {}
        for group in groups:
            barrier = threading.Barrier(len(group), timeout=30)
            self.groups.update(dict.fromkeys(group, barrier))
        self.builtin, self.calls, self.log = builtin, [], []
        self.lock = threading.Lock()
        self.active = self.peak = 0

    def specs(self):
        if self.builtin is not None:
            return [s for s in self.builtin.specs() if s["function"]["name"] == "ryn_read_resource"]
        return [{"type": "function", "function": {"name": "ryn_shell_execute", "description": "host shell",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                           "required": ["command"], "additionalProperties": False}}}]

    def metadata(self, name):
        return self.builtin.metadata(name) if self.builtin else dict(SHELL_META)

    def preview(self, name, arguments, **kwargs):
        return arguments

    def execute(self, name, arguments, **kwargs):
        value = arguments.get("command", arguments.get("resource_ref"))
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append(value)
            self.log.append(("start", value, time.monotonic()))
        try:
            if value in self.groups:
                self.groups[value].wait()
            time.sleep(0.04)
            return {"text": value}
        finally:
            with self.lock:
                self.log.append(("end", value, time.monotonic()))
                self.active -= 1


def scheduled_runtime(tmp_path, tools, calls):
    runtime, model, _, key = make_runtime(tmp_path, response(calls), assistant("Done"))
    runtime.tools = tools
    runtime.update_settings({"permission_mode": "automatic", "max_seconds": 600})
    task = runtime.create({"message": "Inspect with the listed tools"})
    expose(runtime, task["id"], [call["function"]["name"] for call in calls])
    return runtime, model, task, key


def test_t3_shell_batch_overlaps_then_write_is_exclusive_and_next_reads_batch(tmp_path, monkeypatch):
    from rynmesh.agent_runtime import read_reliability
    checked = []
    original = read_reliability.check_shell_retry
    def check(row, call):
        checked.append(call["arguments"]["command"])
        return original(row, call)
    monkeypatch.setattr(read_reliability, "check_shell_retry", check)
    first = ["git status", "git diff", "rg needle ."]
    later = ["git log -1", "git show HEAD"]
    write = "git commit -m requested"
    tools = ConcurrentTools([first, later])
    commands = first + [write] + later
    calls = [raw(i, "ryn_shell_execute", {"command": command}) for i, command in enumerate(commands)]
    runtime, model, task, key = scheduled_runtime(tmp_path, tools, calls)
    runtime.run_once()
    runtime.run_once()
    assert sorted(tools.calls) == sorted(first)
    assert tools.peak == 3
    assert set(checked) >= set(first)
    queued = runtime.store.read()["tasks"][task["id"]]["queue"]
    assert queued[0]["arguments"]["command"] == write
    runtime.run_once()
    assert tools.calls[-1] == write
    runtime.run_once()
    assert sorted(tools.calls) == sorted(commands)
    ends = {value: stamp for event, value, stamp in tools.log if event == "end"}
    starts = {value: stamp for event, value, stamp in tools.log if event == "start"}
    assert min(starts[value] for value in later) >= ends[write]
    assert starts[write] >= max(ends[value] for value in first)
    events = runtime.get(task["id"])["events"]
    assert [e["effect"] for e in events if e["type"] == "tool_started"] == ["read"] * 3 + ["write"] + ["read"] * 2


def test_t4_restart_marks_shell_batch_as_interrupted_reads(tmp_path, monkeypatch):
    calls = [raw(i, "ryn_shell_execute", {"command": command}) for i, command in enumerate(["git status", "git diff"])]
    tools = ConcurrentTools()
    runtime, model, task, key = scheduled_runtime(tmp_path, tools, calls)
    runtime.run_once()
    class Crash(BaseException):
        """Stop after durable batch admission, before executor creation."""
    def crash(**kwargs):
        raise Crash()
    monkeypatch.setattr("rynmesh.agent_runtime.loop.ThreadPoolExecutor", crash)
    with pytest.raises(Crash):
        runtime.run_once()
    stored = runtime.store.read()["tasks"][task["id"]]
    assert len(stored["in_flight"]["batch_call_ids"]) == 2
    assert stored["in_flight"]["operation_classification"]["basis"] == "literal_shell_read"
    assert tools.calls == []
    from rynmesh.agent_runtime.local_tools import LocalTools
    from rynmesh.agent_runtime.operation_ledger import OperationLedger
    home = tmp_path / "local-node"
    home.mkdir()
    local = LocalTools(SimpleNamespace(home=home), lambda: OperationLedger(home))
    local.store = runtime.store
    tools.local = local
    restored = AgentRuntime(AgentStore(tmp_path, key), model, tools, runtime.catalog)
    try:
        restored.reconcile()
    finally:
        local.processes.close()
    saved = restored.store.read()["tasks"][task["id"]]
    assert saved["error"] != "agent_write_outcome_unknown"
    assert saved["in_flight"] is None
    assert all(r["status"] != "unknown" for r in saved["receipts"])


def stream_host(tmp_path, tools):
    runtime, model, _, key = make_runtime(tmp_path)
    runtime.tools = tools
    runtime.update_settings({"permission_mode": "automatic", "max_seconds": 600})
    task_id = runtime.create({"message": "Inspect streamed commands"})["id"]
    expose(runtime, task_id, [s["function"]["name"] for s in tools.specs()])
    def claim(data):
        row = data["tasks"][task_id]
        row.update(status="running", in_flight={"kind": "model", "dispatched": True, "request_id": "stream-test"})
        return copy.deepcopy(row)
    selected = runtime.store.transaction(claim)
    selected.update(_run_control_revision=selected["control_revision"], _request_id="stream-test",
                    _model_retry_authority=model_retry_authority(runtime.store.read()["settings"]))
    return runtime, selected, StreamedReads(runtime, selected, tools.specs(), lambda: False, PARALLEL_READS)


def test_t5_stream_shell_starts_early_but_write_blocks_later_reads(tmp_path):
    tools = ConcurrentTools()
    runtime, selected, reads = stream_host(tmp_path, tools)
    calls = [raw(i, "ryn_shell_execute", {"command": command}) for i, command in enumerate(["git status", "git commit -m change", "git diff"])]
    for i, call in enumerate(calls):
        reads.on_event({"type": "tool_call_complete", "index": i, "call": call})
    records = reads.drain(calls)
    assert tools.calls == ["git status"]
    assert len(records) == 1 and records[0]["call"]["call_id"] == "call_0"
    assert reads.coordinator.blocked is True
    assert runtime.store.read()["tasks"][selected["id"]]["receipts"][0]["operation_classification"]["basis"] == "literal_shell_read"


def test_t8_t9_builtin_metadata_declares_safe_resources_but_not_browser(node):  # noqa: F811
    app, store = node
    _, tools, _ = build_agent_integrations(app, store, store.home)
    for name in ("ryn_read_resource", "ryn_list_resources", "ryn_image_inspect"):
        meta = tools.metadata(name)
        assert meta["supports_parallel"] is True
        assert supports_parallel(name, meta) is True
    for name in ("ryn_browser_action", "ryn_browser_read"):
        assert supports_parallel(name, tools.metadata(name)) is False


def test_t10_three_resource_reads_batch_using_actual_registered_metadata(node, tmp_path):  # noqa: F811
    app, store = node
    _, builtin, _ = build_agent_integrations(app, store, store.home)
    refs = ["test-resource-0", "test-resource-1", "test-resource-2"]
    tools = ConcurrentTools([refs], builtin=builtin)
    runtime, model, task, key = scheduled_runtime(tmp_path / "resource-task", tools,
        [raw(i, "ryn_read_resource", {"resource_ref": ref}) for i, ref in enumerate(refs)])
    runtime.run_once()
    start = time.monotonic()
    runtime.run_once()
    elapsed = time.monotonic() - start
    assert tools.peak == 3 and sorted(tools.calls) == refs
    stamps = [stamp for event, _, stamp in tools.log if event == "start"]
    assert max(stamps) < min(stamp for event, _, stamp in tools.log if event == "end")
    assert elapsed < 30


def test_code_mode_child_gate_receives_real_arguments(host, monkeypatch):  # noqa: F811
    runtime, model, tools = host
    observed = []
    original = code_mode.supports_parallel
    def spy(name, meta, arguments=None, **kwargs):
        observed.append(copy.deepcopy(arguments))
        return original(name, meta, arguments, **kwargs)
    monkeypatch.setattr(code_mode, "supports_parallel", spy)
    tools.runner.execute(cell(2), operation_id="standalone", cancelled=lambda: False, context={"task_id": "standalone"})
    assert sorted(observed, key=lambda item: item["path"]) == [{"path": "0"}, {"path": "1"}]


def test_serial_read_in_code_mode_remains_allowed_without_parallel_capability(host, monkeypatch):  # noqa: F811
    runtime, model, tools = host
    original = tools.specs
    spec = {"type": "function", "function": {"name": "serial_read", "description": "Read through a serial handle",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}}
    monkeypatch.setattr(tools, "specs", lambda: [*original(), spec])
    model.responses = [assistant(name="ryn_code_exec", args={"code": "text(await tools.serial_read({path:'one'}));", "yield_time_ms": 60000}), assistant("Done")]
    task = runtime.create({"message": "Use the serial read"})
    for _ in range(3):
        runtime.run_once()
    assert [(name, path) for name, path, _ in tools.calls] == [("serial_read", "one")]


def test_parallel_shell_uses_the_same_workspace_boundary_as_classification(tmp_path):
    from rynmesh.agent_runtime.execution_policy import parallel_read
    inside = {"command": "git status", "cwd": str(tmp_path)}
    outside = {"command": "git status", "cwd": str(tmp_path.parent)}
    assert parallel_read("ryn_shell_execute", inside, SHELL_META, workspace_root=tmp_path) is True
    assert parallel_read("ryn_shell_execute", outside, SHELL_META, workspace_root=tmp_path) is False


def mcp_setup(ext_env, tmp_path, *, read=True):
    runtime, model, tools, workspace, home = ext_env
    runtime.update_settings({"permission_mode": "automatic", "max_seconds": 600})
    script = tmp_path / "mcp_fixture.py"
    _write_fake_mcp_server(script, tmp_path / "calls.json")
    store = tools.extensions.store
    config = {"id": "parallel-test", "name": "Parallel test", "transport": "stdio", "command": sys.executable,
              "args": ["-u", str(script)], "enabled": True, "read_only_tools": ["ping_info"] if read else []}
    server = store.upsert_mcp_server(config, expected_revision=store.overview()["revision"])
    tested = tools.extensions.manager.test_server(server["id"], expected_revision=server["revision"])
    assert tested["test_result"]["status"] == "connected"
    name = compute_model_tool_name(server["id"], "ping_info" if read else "mutate_record")
    return runtime, model, tools, config, name


def fake_rpc(tools, monkeypatch, *, parties=1):
    lock, barrier = threading.Lock(), threading.Barrier(parties, timeout=30)
    state = {"active": 0, "peak": 0, "calls": [], "entered": threading.Event()}
    def call(server, tool, arguments, **options):
        options["before_dispatch"]()
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            state["calls"].append((server, tool, copy.deepcopy(arguments)))
            if len(state["calls"]) == parties:
                state["entered"].set()
        try:
            barrier.wait()
            return {"content": [{"type": "text", "text": json.dumps(arguments)}]}
        finally:
            with lock:
                state["active"] -= 1
    monkeypatch.setattr(tools.extensions.manager, "call_tool", call)
    return state


@pytest.mark.parametrize("streaming", [False, True])
def test_t6_owner_confirmed_mcp_reads_overlap_and_carry_exact_binding(ext_env, tmp_path, monkeypatch, streaming):  # noqa: F811
    runtime, model, tools, config, name = mcp_setup(ext_env, tmp_path)
    rpc = fake_rpc(tools, monkeypatch, parties=2)
    contexts = []
    execute = tools.execute
    def capture(tool, args, **kwargs):
        contexts.append(copy.deepcopy(kwargs["context"]["extension_binding"]))
        return execute(tool, args, **kwargs)
    monkeypatch.setattr(tools, "execute", capture)
    calls = [raw(i, name, {"label": str(i)}) for i in range(2)]
    def streamed(_model, _messages, _specs, cancelled, on_event, **options):
        for index, call in enumerate(calls):
            on_event({"type": "tool_call_complete", "index": index, "call": call})
        assert rpc["entered"].wait(30), "both MCP reads must start before the model finishes its response"
        return response(calls)
    model.responses = [streamed if streaming else response(calls), assistant("Read both")]
    task = runtime.create({"message": "Read both status values"})
    expose(runtime, task["id"], [name])
    expected = copy.deepcopy(tools.metadata(name)["extension_binding"])
    runtime.run_once()
    runtime.run_once()
    assert rpc["peak"] == 2 and len(rpc["calls"]) == 2
    assert contexts == [expected, expected]
    assert not runtime.store.read()["tasks"][task["id"]]["queue"]


@pytest.mark.parametrize("when", ["before_batch", "at_authorize"])
def test_t6_binding_revocation_falls_back_without_dispatch(ext_env, tmp_path, monkeypatch, when):  # noqa: F811
    runtime, model, tools, config, name = mcp_setup(ext_env, tmp_path)
    rpc = fake_rpc(tools, monkeypatch)
    model.responses = [response([raw(i, name, {"label": str(i)}) for i in range(2)])]
    task = runtime.create({"message": "Read status"})
    expose(runtime, task["id"], [name])
    runtime.run_once()
    changed = []
    def revoke():
        store = tools.extensions.store
        current = next(server for server in store.overview()["mcp_servers"] if server["id"] == config["id"])
        store.upsert_mcp_server({"read_only_tools": []}, server_id=config["id"], expected_revision=current["revision"])
        changed.append(True)
    if when == "before_batch":
        revoke()
    else:
        transaction = runtime.store.transaction
        def race(mutate):
            if mutate.__qualname__.endswith("_tool_batch.<locals>.authorize") and not changed:
                revoke()
            return transaction(mutate)
        monkeypatch.setattr(runtime.store, "transaction", race)
    runtime.run_once()
    row = runtime.store.read()["tasks"][task["id"]]
    assert changed == [True]
    assert rpc["calls"] == []
    assert len(row["queue"]) == 1
    result = next(e["result"] for e in reversed(row["events"]) if e["type"] == "tool_result")
    assert result["error"] == "agent_tool_definition_changed" and result["not_executed"] is True
    assert not any(e["type"] == "tool_started" for e in row["events"])


def test_t7_unmarked_mcp_mutations_stay_serial(ext_env, tmp_path, monkeypatch):  # noqa: F811
    runtime, model, tools, config, name = mcp_setup(ext_env, tmp_path, read=False)
    rpc = fake_rpc(tools, monkeypatch)
    model.responses = [response([raw(i, name, {"value": str(i)}) for i in range(2)])]
    task = runtime.create({"message": "Apply both changes"})
    expose(runtime, task["id"], [name])
    assert supports_parallel(name, tools.metadata(name)) is False
    runtime.run_once()
    runtime.run_once()
    assert len(runtime.store.read()["tasks"][task["id"]]["queue"]) == 1
    runtime.run_once()
    assert rpc["peak"] == 1 and len(rpc["calls"]) == 2
