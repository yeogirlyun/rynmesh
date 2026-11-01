"""Focused host v1 integration tests for NativeComputerFacade, ExtensionToolAdapter, and LocalTools.

Uses the real NativeWorkerClient, NativeBridge, AgentStore, OperationLedger, ExtensionToolAdapter,
and LocalTools against an owned Python JSONL terminal worker subprocess without touching real GUI windows,
real native binaries, network, or model inference.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable

import pytest

from rynmesh.agent_runtime.computer.client import NativeWorkerClient
from rynmesh.agent_runtime.computer.coordinator import NativeBridge, bind_node_native_bridge
from rynmesh.agent_runtime.extensions.adapter import ExtensionToolAdapter
from rynmesh.agent_runtime.extensions.computer_use_server import ComputerUseProvider
from rynmesh.agent_runtime.extensions.store import (
    COMPUTER_USE_PLUGIN_ID,
    COMPUTER_USE_SERVER_ID,
    ExtensionStore,
    compute_model_tool_name,
)
from rynmesh.agent_runtime.local_desktop import Desktop
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.native_computer import bind_owner_native_computer
from rynmesh.agent_runtime.resource_writes import OperationLedger
from rynmesh.agent_runtime.state import initialize
from rynmesh.agent_runtime.store import AgentError, AgentStore, AgentToolRejected, encoded
from rynmesh.services import peer_box


def _build_owned_terminal_script() -> str:
    """Build the owned Python JSONL terminal worker script from `tests/test_agent_native_bridge.py`."""
    bridge_test_path = Path(__file__).with_name("test_agent_native_bridge.py")
    tree = ast.parse(bridge_test_path.read_text(encoding="utf-8"))
    assignment = next(
        n
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "FAKE_WORKER_SCRIPT" for t in n.targets)
    )
    source = textwrap.dedent(
        max(
            (n.value for n in ast.walk(assignment.value) if isinstance(n, ast.Constant) and isinstance(n.value, str)),
            key=len,
        )
    ).lstrip()

    rpc_needle = '    params = req.get("params") or {}'
    assert rpc_needle in source
    source = source.replace(
        rpc_needle,
        '    params = req.get("params") or {}\n    record_state("rpc_received", {"method": method, "postObservation": params.get("postObservation")})',
        1,
    )

    err_needle = "def make_error(req_id, code:"
    assert err_needle in source
    wrapper_code = (
        "_orig_make_window_entry = make_window_entry\n"
        "def make_window_entry(wref: dict) -> dict:\n"
        "    entry = _orig_make_window_entry(wref)\n"
        "    if MODE == 'negative_monitor':\n"
        "        entry['bounds'] = make_rect(-1920, -200, -1120, 400)\n"
        "        entry['clientBounds'] = make_rect(-1912, -170, -1128, 392)\n"
        "        entry['dpi'] = 144\n"
        "    return entry\n\n"
        "_orig_make_observation = make_observation\n"
        "def make_observation(task_id: str, wref: dict, st: str) -> dict:\n"
        "    obs = _orig_make_observation(task_id, wref, st)\n"
        "    obs['accessibility']['elements'].append({\n"
        "        'index': 1,\n"
        "        'runtimeId': '42.2',\n"
        "        'automationId': 'Pwd1',\n"
        "        'name': 'Password Input',\n"
        "        'controlType': 'Edit',\n"
        "        'className': 'Edit',\n"
        "        'password': True,\n"
        "        'enabled': True,\n"
        "        'offscreen': False,\n"
        "        'focusable': True,\n"
        "        'focused': False,\n"
        "        'bounds': make_rect(110, 200, 300, 240),\n"
        "        'depth': 1,\n"
        "        'patterns': ['ValuePattern'],\n"
        "    })\n"
        "    if obs.get('screenshots'):\n"
        "        obs['screenshots'][0]['originX'] = 120\n"
        "        obs['screenshots'][0]['originY'] = 130\n"
        "    if MODE == 'negative_monitor':\n"
        "        obs['geometry'] = {\n"
        "            'windowRect': make_rect(-1920, -200, -1120, 400),\n"
        "            'clientRect': make_rect(-1912, -170, -1128, 392),\n"
        "            'screenOrigin': [-1920, -200],\n"
        "            'dpi': 144,\n"
        "            'scaleFactor': 1.5,\n"
        "        }\n"
        "        if obs.get('screenshots'):\n"
        "            obs['screenshots'][0]['originX'] = -1900\n"
        "            obs['screenshots'][0]['originY'] = -180\n"
        "            obs['screenshots'][0]['dpi'] = 144\n"
        "            obs['screenshots'][0]['scaleFactor'] = 1.5\n"
        "    elif MODE == 'bad_screenshot_bounds':\n"
        "        if obs.get('screenshots'):\n"
        "            obs['screenshots'][0]['originX'] = 50\n"
        "            obs['screenshots'][0]['originY'] = 50\n"
        "    return obs\n\n"
    )
    source = source.replace(err_needle, wrapper_code + err_needle, 1)

    sha_needle = '        if MODE == "bad_screenshot_sha":'
    assert sha_needle in source
    source = source.replace(
        sha_needle,
        '        if params.get("includeScreenshot") is False:\n'
        '            obs["screenshots"] = []\n'
        '        if MODE == "bad_screenshot_sha":',
        1,
    )

    commit_obs_needle = '                "observation": make_observation(task_id, cur["window"], new_st),'
    assert commit_obs_needle in source
    source = source.replace(
        commit_obs_needle,
        '                "observation": {**make_observation(task_id, cur["window"], new_st), "screenshots": make_observation(task_id, cur["window"], new_st)["screenshots"] if (params.get("postObservation") or {}).get("enabled") else []},',
        1,
    )
    return source


OWNED_TERMINAL_SOURCE = _build_owned_terminal_script()


def test_computer_read_uses_one_control_payload_and_preserves_trusted_bindings(tmp_path):
    env = _create_owned_fixture(tmp_path / "compact_read")
    try:
        adapter = env["adapter"]
        observe = compute_model_tool_name(COMPUTER_USE_SERVER_ID, "computer_observe_windows")
        read = compute_model_tool_name(COMPUTER_USE_SERVER_ID, "computer_read_window")
        adapter.execute(observe, {}, operation_id="compact_windows", context=env["ctx"])
        result = adapter.execute(read, {"hwnd": 100100, "include_screenshot": False}, operation_id="compact_controls", context=env["ctx"])
        public = result["structuredContent"]["elements"][0]
        assert public["id"] == "el-0" and public["value"] == "hello"
        assert "runtime_id" not in public
        assert len(result["text"]) < 200
        assert result["content"][0]["text"] == result["text"]
        trusted = adapter.native_computer._tasks["task_owned"].observations_by_hwnd[100100]
        assert trusted.elements_by_sky_id[public["id"]]["runtime_id"]
        assert trusted.elements_by_sky_id[public["id"]]["bounds"] == public["bounds"]
        assert trusted.state_token == result["structuredContent"]["state_token"]
        from rynmesh.agent_runtime.protocol import sanitized
        act = compute_model_tool_name(COMPUTER_USE_SERVER_ID, "computer_act_window")
        args = {"hwnd": 100100, "action": "invoke", "element_id": "el-0"}
        preview = adapter.preview(act, args, context=env["ctx"])
        assert preview["element"]["is_password"] is False
        assert sanitized(preview) == preview
        result = adapter.execute(act, args, operation_id="compact_invoke", context=env["ctx"], expected_preview=sanitized(preview))
        assert result["structuredContent"]["status"] == "committed"
    finally:
        _cleanup_owned_fixture(env)


def test_focus_and_following_actions_return_current_state_without_model_read_roundtrip(tmp_path):
    env = _create_owned_fixture(tmp_path / "post_observation")
    try:
        adapter, ctx = env["adapter"], env["ctx"]
        observe = compute_model_tool_name(COMPUTER_USE_SERVER_ID, "computer_observe_windows")
        act = compute_model_tool_name(COMPUTER_USE_SERVER_ID, "computer_act_window")
        adapter.execute(observe, {}, operation_id="list", context=ctx)
        args = {"hwnd": 100100, "action": "focus"}
        preview = adapter.preview(act, args, context=ctx)
        focused = adapter.execute(act, args, operation_id="focus", context=ctx, expected_preview=preview)
        fresh = focused["structuredContent"]["observation"]
        assert fresh["state_token"] != preview["expected_state_token"]
        assert focused["native_observation_artifacts"]
        args = {"hwnd": 100100, "action": "type", "text": "owned test"}
        preview2 = adapter.preview(act, args, context=ctx)
        assert preview2["expected_state_token"] == fresh["state_token"]
        typed = adapter.execute(act, args, operation_id="type", context=ctx, expected_preview=preview2)
        assert typed["structuredContent"]["observation"]["state_token"] != fresh["state_token"]
        repeated = adapter.execute(act, args, operation_id="type", context=ctx, expected_preview=preview2)
        assert repeated == typed
        records = env["events"]()
        rpc = [record for record in records if record["event"] == "rpc_received"]
        assert sum(record["method"] == "state.get" for record in rpc) == 1
        commits = [record for record in rpc if record["method"] == "action.commit"]
        assert len(commits) == 2
        assert all(record["postObservation"]["enabled"] for record in commits)
    finally:
        _cleanup_owned_fixture(env)


def test_committed_action_with_failed_post_capture_remains_committed(tmp_path, monkeypatch):
    env = _create_owned_fixture(tmp_path / "post_capture_failure")
    try:
        adapter, ctx = env["adapter"], env["ctx"]
        observe = compute_model_tool_name(COMPUTER_USE_SERVER_ID, "computer_observe_windows")
        act = compute_model_tool_name(COMPUTER_USE_SERVER_ID, "computer_act_window")
        adapter.execute(observe, {}, operation_id="list", context=ctx)
        args = {"hwnd": 100100, "action": "focus"}
        preview = adapter.preview(act, args, context=ctx)
        def fail_capture(*args, **kwargs):
            raise OSError("owned screenshot write failure")
        monkeypatch.setattr(env["local"].native_computer, "_ingest_observation", fail_capture)
        result = adapter.execute(act, args, operation_id="focus", context=ctx, expected_preview=preview)
        assert result["structuredContent"]["status"] == "committed"
        assert "do not replay" in result["structuredContent"]["next_step"]
        assert "observation" not in result["structuredContent"]
        assert env["ledger"].get("focus")["status"] == "confirmed"
    finally:
        _cleanup_owned_fixture(env)


def _make_task_row(
    task_id: str,
    workspace: Path,
    *,
    permission_mode: str = "full_access",
    allowed_write_tools: list[str] | None = None,
    desktop_access: bool = True,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": task_id,
        "status": "running",
        "messages": [],
        "events": [],
        "receipts": [],
        "queue": [],
        "permission_mode": permission_mode,
        "allowed_write_tools": list(allowed_write_tools or []),
        "local_execution": {
            "workspace_root": str(workspace),
            "desktop_access": desktop_access,
        },
        "context_epoch": 1,
    }
    initialize(row)
    row["context_epoch"] = 1
    return row


def _create_owned_fixture(
    base_dir: Path,
    *,
    mode: str = "normal",
) -> dict[str, Any]:
    base_dir.mkdir(parents=True, exist_ok=True)
    script_path = base_dir / "owned_terminal.py"
    if not script_path.exists():
        script_path.write_text(OWNED_TERMINAL_SOURCE, encoding="utf-8")

    home = base_dir / "home"
    home.mkdir(parents=True, exist_ok=True)
    workspace = home / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    store = AgentStore(home / "agent", key)
    store.home = home  # type: ignore[attr-defined]

    row_1 = _make_task_row("task_owned", workspace)
    row_2 = _make_task_row("task_other", workspace)

    def _setup(data: dict[str, Any]) -> bool:
        data["settings"].update(
            enabled=True,
            desktop_access=True,
            workspace_root=str(workspace),
            permission_mode="full_access",
        )
        data["tasks"]["task_owned"] = copy.deepcopy(row_1)
        data["tasks"]["task_other"] = copy.deepcopy(row_2)
        return True

    store.transaction(_setup)

    state_file = home / "terminal-events.jsonl"
    base_py = Path(getattr(sys, "_base_executable", None) or sys.executable).resolve()
    bridge = NativeBridge(
        dev_executable=[str(base_py), str(script_path), mode, str(state_file)],
        handshake_timeout=10,
    )
    bind_node_native_bridge(store, bridge=bridge)

    ledger = OperationLedger(home)
    local = LocalTools(store, lambda: ledger)
    ext = ExtensionStore(home / "agent", key)
    ext.set_plugin_enabled(COMPUTER_USE_PLUGIN_ID, True, desktop_access=True)
    adapter = ExtensionToolAdapter(
        local,
        lambda: ledger,
        node_store=store,
        extension_store=ext,
        native_computer=local.native_computer,
    )
    ctx = {
        "task_id": "task_owned",
        "local_execution": copy.deepcopy(row_1["local_execution"]),
        "context_epoch": 1,
        "scope_revision": 1,
        "goal_revision": 1,
        "control_revision": 0,
        "operation_id": "op_write",
    }

    def read_events() -> list[dict[str, Any]]:
        if not state_file.exists():
            return []
        return [
            json.loads(line)
            for line in state_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def record_event(event: str, **fields: Any) -> None:
        with state_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event": event, **fields}) + "\n")
            f.flush()

    real_update = ledger.update

    def wrapped_update(op: str, status: str, *a: Any, **kw: Any) -> Any:
        val = real_update(op, status, *a, **kw)
        record_event(
            "host_ledger_update",
            status=status,
            persisted_status=(ledger.get(op) or {}).get("status"),
        )
        return val

    ledger.update = wrapped_update  # type: ignore[method-assign]

    return {
        "home": home,
        "workspace": workspace,
        "store": store,
        "state_file": state_file,
        "bridge": bridge,
        "ledger": ledger,
        "local": local,
        "ext": ext,
        "adapter": adapter,
        "ctx": ctx,
        "events": read_events,
        "record": record_event,
    }


def _cleanup_owned_fixture(env: dict[str, Any]) -> None:
    bridge: NativeBridge = env["bridge"]
    adapter: ExtensionToolAdapter = env["adapter"]
    local: LocalTools = env["local"]

    sessions = [bridge.client._session] if bridge.client._session is not None else []
    owned_procs = [s.process for s in sessions if s.process is not None]

    adapter.close()
    local.native_computer.close()
    bridge.close()

    assert all(p.poll() is not None for p in owned_procs), "owned worker process did not exit"
    assert all(
        all(v is None or v.closed for v in (p.stdin, p.stdout, p.stderr))
        for p in owned_procs
    ), "owned worker pipes not closed"
    assert all(not getattr(p, "_ryn_job", None) for p in owned_procs), "owned worker Job handle not closed"
    assert all(
        not any(t and t.is_alive() for t in (s._reader_thread, s._writer_thread, s._stderr_thread))
        for s in sessions
    ), "owned worker reader/writer threads still alive"


def test_shared_facade_builtin_mcp_read_write_dispatch_order_and_artifact_retirement(tmp_path: Path):
    env = _create_owned_fixture(tmp_path / "case_read_write")
    try:
        adapter: ExtensionToolAdapter = env["adapter"]
        local: LocalTools = env["local"]
        bridge: NativeBridge = env["bridge"]
        ledger: OperationLedger = env["ledger"]
        ctx = env["ctx"]

        assert isinstance(bridge.client, NativeWorkerClient)
        assert adapter._should_route_builtin_to_native_computer() is True
        assert adapter.native_computer is local.native_computer
        assert adapter.native_computer.bridge is bridge
        assert bridge._ever_used is False

        observe_name = compute_model_tool_name(COMPUTER_USE_SERVER_ID, "computer_observe_windows")
        read_name = compute_model_tool_name(COMPUTER_USE_SERVER_ID, "computer_read_window")
        act_name = compute_model_tool_name(COMPUTER_USE_SERVER_ID, "computer_act_window")

        # Unobserved HWND fails closed before any state.get RPC
        with pytest.raises(AgentToolRejected, match="agent_computer_window_changed_read_again"):
            adapter.execute(read_name, {"hwnd": 100100}, operation_id="op_unobserved", context=ctx)

        obs_res = adapter.execute(observe_name, {}, operation_id="op_obs_1", context=ctx)
        assert obs_res["is_error"] is False
        windows = obs_res["structuredContent"]["windows"]
        assert any(w["hwnd"] == 100100 for w in windows)

        read_res = adapter.execute(
            read_name,
            {"hwnd": 100100, "include_screenshot": True},
            operation_id="op_read_1",
            context=ctx,
        )
        assert read_res["is_error"] is False
        sc = read_res["structuredContent"]
        assert sc["state_token"].startswith("st_")
        els = sc["elements"]
        assert len(els) == 2
        assert els[0]["id"] == "el-0"
        assert els[0]["value"] == "hello"
        assert els[1]["id"] == "el-1"
        assert els[1]["password"] is True
        assert els[1]["value"] == ""

        # Password element is rejected for set_value / type / invoke
        with pytest.raises(AgentToolRejected, match="agent_computer_password_field"):
            adapter.preview(
                act_name,
                {"hwnd": 100100, "action": "set_value", "element_id": "el-1", "text": "secret"},
                context=ctx,
            )

        # Screenshot artifact created with non-zero window-relative physical_origin [20, 30]
        assert len(read_res["native_observation_artifacts"]) == 1
        art = read_res["native_observation_artifacts"][0]
        assert art["path"] == ".ryn/computer/task_owned/op_read_1/image_1.png"
        assert art["origin_x"] == 120
        assert art["origin_y"] == 130
        assert art["native_descriptor"]["physical_origin"] == [20, 30]
        assert art["native_descriptor"]["screen_origin"] == [120, 130]
        shot_file = env["workspace"] / art["path"]
        assert shot_file.is_file()

        verified_art = adapter.native_computer.verify_and_load_registered_artifact(
            task_id="task_owned",
            artifact_id=art["id"],
            expected_operation_id="op_read_1",
            expected_state_token=sc["state_token"],
        )
        assert verified_art["width"] == 4
        assert verified_art["height"] == 3

        # Preview and execute click at screen (150, 160) -> window_physical_px (50, 60)
        preview = adapter.preview(
            act_name,
            {"hwnd": 100100, "action": "click", "x": 150, "y": 160},
            context=ctx,
        )
        assert preview["native_action"] == {
            "type": "click",
            "button": "left",
            "target": {
                "coordinateSpace": "window_physical_px",
                "x": 50,
                "y": 60,
            },
            "element": None,
        }

        act_res = adapter.execute(
            act_name,
            {"hwnd": 100100, "action": "click", "x": 150, "y": 160},
            operation_id="op_write",
            context=ctx,
            expected_preview=preview,
        )
        assert act_res["is_error"] is False
        assert act_res["structuredContent"]["status"] == "committed"
        assert ledger.get("op_write")["status"] == "confirmed"

        ev = env["events"]()
        order = [
            x.get("event") + (":" + str(x.get("status")) if x.get("event") == "host_ledger_update" else "")
            for x in ev
            if x.get("event") != "rpc_received"
        ]
        assert order.index("host_ledger_update:dispatched") < order.index("commit_received")
        commit_ev = next(x for x in ev if x.get("event") == "commit_received")
        assert commit_ev["paramsKeys"] == ["operationId", "postObservation", "prepareTicket", "taskSessionId"]

        # Prior state token and screenshot artifact are retired after action commit
        with pytest.raises(ValueError, match="native_descriptor_stale_state_token"):
            adapter.native_computer.verify_and_load_registered_artifact(
                task_id="task_owned",
                artifact_id=art["id"],
            )
    finally:
        _cleanup_owned_fixture(env)


def test_confirmed_dedup_and_post_commit_unknown_no_replay(tmp_path: Path):
    observe_name = compute_model_tool_name(COMPUTER_USE_SERVER_ID, "computer_observe_windows")
    read_name = compute_model_tool_name(COMPUTER_USE_SERVER_ID, "computer_read_window")
    act_name = compute_model_tool_name(COMPUTER_USE_SERVER_ID, "computer_act_window")

    # 1. Confirmed MCP & Local writes deduplicate without touching terminal or recomputing preview
    for sub_name, entry in (("mcp_confirmed", "mcp"), ("local_confirmed", "local")):
        env = _create_owned_fixture(tmp_path / sub_name, mode="normal")
        try:
            adapter: ExtensionToolAdapter = env["adapter"]
            local: LocalTools = env["local"]
            ctx = env["ctx"]

            adapter.execute(observe_name, {}, operation_id="op_obs", context=ctx)
            adapter.execute(read_name, {"hwnd": 100100, "include_screenshot": False}, operation_id="op_read", context=ctx)

            if entry == "mcp":
                args = {"hwnd": 100100, "action": "focus"}
                preview = adapter.preview(act_name, args, context=ctx)
                res_1 = adapter.execute(act_name, args, operation_id="op_write", context=ctx, expected_preview=preview)
                before_count = len(env["events"]())
                adapter.preview = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("duplicate attempted preview"))  # type: ignore[method-assign]
                res_2 = adapter.execute(act_name, args, operation_id="op_write", context=ctx, expected_preview=preview)
            else:
                args = {"window_id": 100100, "action": "focus"}
                preview = local.preview("ryn_computer_action", args, ctx)
                res_1 = local.execute(
                    "ryn_computer_action",
                    args,
                    operation_id="op_write",
                    context=ctx,
                    expected_preview=preview,
                    cancelled=lambda: False,
                )
                before_count = len(env["events"]())
                local.preview = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("duplicate attempted preview"))  # type: ignore[method-assign]
                res_2 = local.execute(
                    "ryn_computer_action",
                    args,
                    operation_id="op_write",
                    context=ctx,
                    expected_preview=preview,
                    cancelled=lambda: False,
                )

            assert res_2 == res_1
            assert len(env["events"]()) == before_count
            assert env["ledger"].get("op_write")["status"] == "confirmed"
        finally:
            _cleanup_owned_fixture(env)

    # 2. Post-ready commit EOF records dispatched -> unknown and never replays
    env_unk = _create_owned_fixture(tmp_path / "mcp_unknown", mode="commit_eof_no_reply")
    try:
        adapter = env_unk["adapter"]
        ctx = env_unk["ctx"]
        adapter.execute(observe_name, {}, operation_id="op_obs", context=ctx)
        adapter.execute(read_name, {"hwnd": 100100, "include_screenshot": False}, operation_id="op_read", context=ctx)
        args = {"hwnd": 100100, "action": "focus"}
        preview = adapter.preview(act_name, args, context=ctx)

        with pytest.raises(AgentError, match="agent_write_outcome_unknown") as exc_info:
            adapter.execute(act_name, args, operation_id="op_write", context=ctx, expected_preview=preview)
        assert exc_info.value.diagnostics["error"] == "agent_computer_process_disconnected"
        assert env_unk["ledger"].get("op_write")["status"] == "dispatched"

        rec = adapter.reconcile(
            "op_write",
            {
                "task_id": "task_owned",
                "operation": {
                    "tool": act_name,
                    "arguments_sha256": hashlib.sha256(encoded(args)).hexdigest(),
                    "extension_binding": preview["extension_binding"],
                },
            },
        )
        assert rec["outcome"] == "unknown"
        assert rec["error"] == "agent_computer_process_disconnected"

        before_count = len(env_unk["events"]())
        adapter.preview = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("duplicate attempted preview"))  # type: ignore[method-assign]
        with pytest.raises(AgentError, match="agent_write_outcome_unknown"):
            adapter.execute(act_name, args, operation_id="op_write", context=ctx, expected_preview=preview)
        assert len(env_unk["events"]()) == before_count

        # Spoofed builtin binding is rejected
        spoofed = copy.deepcopy(preview["extension_binding"])
        spoofed["instance_id"] = "spoofed-instance"
        with pytest.raises(AgentToolRejected, match="agent_tool_definition_changed"):
            adapter.verify_binding(spoofed)
    finally:
        _cleanup_owned_fixture(env_unk)


def test_ready_time_authority_and_approval_guards_yield_zero_commit(tmp_path: Path):
    observe_name = compute_model_tool_name(COMPUTER_USE_SERVER_ID, "computer_observe_windows")
    read_name = compute_model_tool_name(COMPUTER_USE_SERVER_ID, "computer_read_window")
    act_name = compute_model_tool_name(COMPUTER_USE_SERVER_ID, "computer_act_window")

    scenarios = [
        ("mcp_cancel", "mcp", "cancel"),
        ("mcp_desktop", "mcp", "desktop"),
        ("mcp_task_pause", "mcp", "task"),
        ("mcp_control_revision", "mcp", "control"),
        ("mcp_binding_disable", "mcp", "binding"),
        ("mcp_preview_changed", "mcp", "preview"),
        ("local_task_pause", "local", "task"),
        ("local_control_revision", "local", "control"),
    ]

    for name, entry, change in scenarios:
        env = _create_owned_fixture(tmp_path / name, mode="normal")
        try:
            adapter: ExtensionToolAdapter = env["adapter"]
            local: LocalTools = env["local"]
            store: AgentStore = env["store"]
            ext: ExtensionStore = env["ext"]
            bridge: NativeBridge = env["bridge"]
            ledger: OperationLedger = env["ledger"]
            ctx = env["ctx"]
            flag = {"cancelled": False}
            ready_calls: list[str] = []

            adapter.execute(observe_name, {}, operation_id="op_observe", context=ctx)
            adapter.execute(read_name, {"hwnd": 100100, "include_screenshot": False}, operation_id="op_read", context=ctx)

            args = {"hwnd": 100100, "action": "focus"} if entry == "mcp" else {"window_id": 100100, "action": "focus"}
            preview = (
                adapter.preview(act_name, args, context=ctx)
                if entry == "mcp"
                else local.preview("ryn_computer_action", args, ctx)
            )

            def _mut(data: dict[str, Any]) -> bool:
                target = data["tasks"]["task_owned"]
                if change == "desktop":  # noqa: B023 - this callback finishes within the current case.
                    data["settings"]["desktop_access"] = False
                elif change == "task":  # noqa: B023 - this callback finishes within the current case.
                    target["status"] = "paused"
                elif change == "control":  # noqa: B023 - this callback finishes within the current case.
                    target["control_revision"] += 1
                return True

            real_execute = bridge.execute_action

            def _instrument(*a: Any, on_ready: Callable[[dict[str, Any]], Any], **kw: Any) -> Any:
                def _ready(payload: dict[str, Any]) -> Any:
                    ready_calls.append("ready")  # noqa: B023 - this callback finishes within the current case.
                    if change == "cancel":  # noqa: B023 - this callback finishes within the current case.
                        flag["cancelled"] = True  # noqa: B023 - this callback finishes within the current case.
                    elif change in {"desktop", "task", "control"}:  # noqa: B023 - this callback finishes within the current case.
                        store.transaction(_mut)  # noqa: B023 - this callback finishes within the current case.
                    elif change == "binding":  # noqa: B023 - this callback finishes within the current case.
                        ext.set_plugin_enabled(COMPUTER_USE_PLUGIN_ID, False, desktop_access=True)  # noqa: B023 - this callback finishes within the current case.
                    elif change == "preview":  # noqa: B023 - this callback finishes within the current case.
                        adapter.execute(  # noqa: B023 - this callback finishes within the current case.
                            read_name,
                            {"hwnd": 100100, "include_screenshot": False},
                            operation_id="op_changed_read",
                            context=ctx,  # noqa: B023 - this callback finishes within the current case.
                        )
                    return on_ready(payload)

                return real_execute(*a, on_ready=_ready, **kw)  # noqa: B023 - this callback finishes within the current case.

            bridge.execute_action = _instrument  # type: ignore[method-assign]

            with pytest.raises(AgentToolRejected):
                if entry == "mcp":
                    adapter.execute(
                        act_name,
                        args,
                        operation_id="op_write",
                        context=ctx,
                        expected_preview=preview,
                        cancelled=lambda: flag["cancelled"],  # noqa: B023 - this callback finishes within the current case.
                    )
                else:
                    local.execute(
                        "ryn_computer_action",
                        args,
                        operation_id="op_write",
                        context=ctx,
                        expected_preview=preview,
                        cancelled=lambda: flag["cancelled"],  # noqa: B023 - this callback finishes within the current case.
                    )

            ev = env["events"]()
            commits = sum(x.get("event") == "commit_received" for x in ev)
            assert len(ready_calls) == 1
            assert commits == 0
            assert (ledger.get("op_write") or {}).get("status") == "not_executed"
        finally:
            _cleanup_owned_fixture(env)


def test_negative_screen_coordinates_and_local_approval_preview_guard(tmp_path: Path):
    env = _create_owned_fixture(tmp_path / "case_negative_coords", mode="negative_monitor")
    try:
        local: LocalTools = env["local"]
        ledger: OperationLedger = env["ledger"]
        ctx = env["ctx"]

        # Whole-screen screenshot_path is rejected with zero ImageGrab fallback
        with pytest.raises(AgentToolRejected, match="agent_computer_action_unsupported"):
            local.execute(
                "ryn_computer_read",
                {"screenshot_path": "screen.png"},
                operation_id="op_local_shot",
                context=ctx,
                expected_preview=None,
                cancelled=lambda: False,
            )

        # Legacy ryn_computer_read delegates to the shared NativeComputerFacade
        read_res = local.execute(
            "ryn_computer_read",
            {"window_id": 100100},
            operation_id="op_local_read_1",
            context=ctx,
            expected_preview=None,
            cancelled=lambda: False,
        )
        assert any(w["window_id"] == 100100 for w in read_res["windows"])
        assert len(read_res["elements"]) == 2
        assert read_res["state_token"].startswith("st_")

        # Negative monitor coordinates (-1800, -100) inside window [-1920..-1120, -200..400]
        # translate in a single pass to (120, 100) without double DPI scaling
        local_args = {"window_id": 100100, "action": "click", "x": -1800, "y": -100}
        preview = local.preview("ryn_computer_action", local_args, ctx)
        assert preview["native_action"]["target"] == {
            "coordinateSpace": "window_physical_px",
            "x": 120,
            "y": 100,
        }

        # Changing arguments after preview is rejected before action.prepare
        with pytest.raises(AgentToolRejected, match="agent_approval_preview_changed"):
            local.execute(
                "ryn_computer_action",
                {"window_id": 100100, "action": "click", "x": -1799, "y": -100},
                operation_id="op_local_mismatch",
                context=ctx,
                expected_preview=preview,
                cancelled=lambda: False,
            )
        # Argument drift is rejected before creating a dispatch intent.
        assert ledger.get("op_local_mismatch") is None

        # Another task cannot use task_owned's observed window or state token
        ctx_other = {**ctx, "task_id": "task_other"}
        with pytest.raises(AgentToolRejected, match="agent_computer_window_changed_read_again"):
            local.preview("ryn_computer_action", local_args, ctx_other)

        act_res = local.execute(
            "ryn_computer_action",
            local_args,
            operation_id="op_write",
            context=ctx,
            expected_preview=preview,
            cancelled=lambda: False,
        )
        assert act_res["action_delivered"] == "click"
        assert ledger.get("op_write")["status"] == "confirmed"
    finally:
        _cleanup_owned_fixture(env)


def test_screenshot_bounds_rejection_exclusive_create_and_verified_cleanup(tmp_path: Path):
    observe_name = compute_model_tool_name(COMPUTER_USE_SERVER_ID, "computer_observe_windows")
    read_name = compute_model_tool_name(COMPUTER_USE_SERVER_ID, "computer_read_window")

    # 1. Out-of-bounds screenshot originX/originY fails closed without writing any file
    env_bad = _create_owned_fixture(tmp_path / "case_bad_bounds", mode="bad_screenshot_bounds")
    try:
        adapter_bad: ExtensionToolAdapter = env_bad["adapter"]
        ctx_bad = env_bad["ctx"]
        adapter_bad.execute(observe_name, {}, operation_id="op_obs", context=ctx_bad)
        with pytest.raises(AgentError, match="agent_computer_screenshot_invalid"):
            adapter_bad.execute(
                read_name,
                {"hwnd": 100100, "include_screenshot": True},
                operation_id="op_bad_bounds",
                context=ctx_bad,
            )
        assert not (env_bad["workspace"] / ".ryn" / "computer" / "task_owned" / "op_bad_bounds").exists()
    finally:
        _cleanup_owned_fixture(env_bad)

    # 2. Valid negative monitor screenshot, exclusive CREATE_NEW collision guard, and bounded forget_task cleanup
    env_good = _create_owned_fixture(tmp_path / "case_good_bounds", mode="negative_monitor")
    try:
        adapter_good: ExtensionToolAdapter = env_good["adapter"]
        ctx_good = env_good["ctx"]
        adapter_good.execute(observe_name, {}, operation_id="op_obs", context=ctx_good)

        read_res = adapter_good.execute(
            read_name,
            {"hwnd": 100100, "include_screenshot": True},
            operation_id="op_good_neg",
            context=ctx_good,
        )
        art = read_res["native_observation_artifacts"][0]
        assert art["origin_x"] == -1900
        assert art["origin_y"] == -180
        assert art["native_descriptor"]["physical_origin"] == [20, 20]
        assert art["native_descriptor"]["screen_origin"] == [-1900, -180]
        shot_file = env_good["workspace"] / art["path"]
        assert shot_file.is_file()

        # Pre-existing destination file in operation directory causes CREATE_NEW to fail closed
        collision_dir = env_good["workspace"] / ".ryn" / "computer" / "task_owned" / "op_collision"
        collision_dir.mkdir(parents=True, exist_ok=True)
        collision_file = collision_dir / "image_1.png"
        collision_file.write_bytes(b"untrusted_preexisting")
        with pytest.raises(AgentError, match="agent_computer_screenshot_invalid"):
            adapter_good.execute(
                read_name,
                {"hwnd": 100100, "include_screenshot": True},
                operation_id="op_collision",
                context=ctx_good,
            )
        assert collision_file.read_bytes() == b"untrusted_preexisting"

        # forget_task unlinks tracked verified PNGs and calls session.end, leaving untracked files untouched
        adapter_good.forget_task("task_owned")
        assert not shot_file.exists()
        assert collision_file.read_bytes() == b"untrusted_preexisting"
        ev = env_good["events"]()
        assert any(
            x.get("event") == "session_end_received" and x.get("taskSessionId") == "task_owned"
            for x in ev
        )
    finally:
        _cleanup_owned_fixture(env_good)


def test_owner_isolation_and_per_task_computer_access_session_scope(tmp_path: Path):
    env_a = _create_owned_fixture(tmp_path / "owner_a")
    env_b = _create_owned_fixture(tmp_path / "owner_b")
    try:
        store_a: AgentStore = env_a["store"]
        store_b: AgentStore = env_b["store"]
        adapter_a: ExtensionToolAdapter = env_a["adapter"]
        local_a: LocalTools = env_a["local"]

        facade_a = adapter_a.native_computer
        assert facade_a._owner is store_a
        assert facade_a.bridge is env_a["bridge"]

        # Binding store_b while passing local_a must not reuse store_a's facade or bridge
        facade_b = bind_owner_native_computer(store_b, local_tools=local_a)
        assert facade_b is not facade_a
        assert facade_b._owner is store_b
        assert facade_b.bridge is env_b["bridge"]

        # The authoritative local store owns the facade. An unrelated mutable
        # attribute must not redirect a live adapter into a different owner.
        adapter_a.node_store = store_b
        assert adapter_a.native_computer is facade_a
        adapter_a.node_store = store_a
        from rynmesh.agent_runtime.extensions.mcp_transport import McpSessionManager
        assert McpSessionManager._session_key("third-party-computer", "task_owned") != McpSessionManager._session_key("third-party-computer", "task_other")
        assert McpSessionManager._session_key("third-party-normal") == ("third-party-normal", None)
    finally:
        _cleanup_owned_fixture(env_a)
        _cleanup_owned_fixture(env_b)


def test_h1b_standalone_computer_use_provider_and_desktop_fail_closed():
    provider = ComputerUseProvider()
    with pytest.raises(AgentToolRejected, match="agent_computer_host_required"):
        provider.observe_windows({})
    with pytest.raises(AgentToolRejected, match="agent_computer_host_required"):
        provider.read_window({"hwnd": 100100})
    with pytest.raises(AgentToolRejected, match="agent_computer_host_required"):
        provider.act_window({"hwnd": 100100, "action": "focus"})

    with pytest.raises(AgentToolRejected, match="agent_computer_host_required"):
        Desktop(None)
