"""Bundled desktop operations inherit workstation grants, not MCP annotations."""
import pytest

from rynmesh.agent_runtime.computer_overlay import ComputerOverlay
from rynmesh.agent_runtime.execution_policy import decide


def policy(mode="workspace", desktop=True, grants=()):
    return {"permission_mode": mode, "desktop_access": desktop,
            "local_execution": {"desktop_access": desktop}, "allowed_write_tools": list(grants)}


CALL = {"name": "mcp__computer_use__computer_act_window__9ae0ccc8", "arguments": {"action": "focus"}}
META = {"effect": "write", "computer_access": True, "workstation_permission_tool": "ryn_computer_action"}


def test_builtin_desktop_inherits_full_access_without_dialog():
    assert decide(CALL, META, policy(), policy()).action == "execute"


@pytest.mark.parametrize("mode,expected", [("read_only", "deny"), ("confirm_changes", "ask")])
def test_restrictive_permission_still_wins(mode, expected):
    assert decide(CALL, META, policy(), policy(mode)).action == expected
    assert decide(CALL, META, policy(mode), policy()).action == expected


def test_external_mcp_cannot_claim_builtin_permission_by_name_or_annotation():
    assert decide(CALL, {"effect": "write", "computer_access": True}, policy(), policy()).action == "ask"


def test_both_desktop_grants_are_required():
    assert decide(CALL, META, policy(desktop=False), policy()).action == "ask"
    assert decide(CALL, META, policy(), policy(desktop=False)).action == "ask"


def test_automatic_allowlist_inherits_only_an_explicit_desktop_grant():
    assert decide(CALL, META, policy("automatic"), policy()).action == "ask"
    assert decide(CALL, META, policy("automatic", grants=["ryn_computer_action"]), policy()).action == "execute"
    assert decide(CALL, META, policy("automatic", grants=["ryn_file_write"]), policy()).action == "ask"


def test_explicit_approval_metadata_is_preserved():
    assert decide(CALL, {**META, "requires_explicit_approval": True}, policy(), policy()).action == "ask"


def test_automatic_builtin_grant_survives_dispatch_and_is_revoked_with_desktop_access():
    from rynmesh.agent_runtime.runtime import write_permission_revoked
    row = policy()
    row['in_flight'] = {**CALL, **META, 'authorization':'automatic'}
    assert not write_permission_revoked(row, policy())
    assert write_permission_revoked(row, policy(desktop=False))
    assert write_permission_revoked(row, policy('confirm_changes'))
    row['in_flight'].pop('workstation_permission_tool')
    assert write_permission_revoked(row, policy())


def test_native_ready_gate_retains_task_desktop_authority(tmp_path):
    from types import SimpleNamespace

    from rynmesh.agent_runtime.local_tools import LocalTools
    local = LocalTools(SimpleNamespace(home=tmp_path), lambda: None)
    workspace = str(tmp_path.resolve())
    row = {**policy(), 'status':'running', 'local_execution':{'workspace_root':workspace,'desktop_access':True},
           'in_flight':{**CALL, **META, 'operation_id':'op-focus','authorization':'automatic'}}
    data = {'settings':{**policy(), 'enabled':True, 'workspace_root':workspace}, 'tasks':{'task':row}}
    local.store = SimpleNamespace(inspect=lambda view:view(data))
    check = local.allowed({'task_id':'task','operation_id':'op-focus','local_execution':row['local_execution'].copy()})
    assert check()
    row['local_execution']['desktop_access'] = False
    assert not check()


def test_idle_approval_refresh_does_not_copy_transcripts():
    from types import SimpleNamespace

    from rynmesh.agent_runtime.loop import AgentLoop
    class NotCopyable:
        def __deepcopy__(self, memo):
            raise AssertionError('Historical transcript reached idle polling projection')
    import copy
    data = {'settings':policy(), 'tasks':{
        'waiting':{**policy(),'id':'waiting','status':'waiting_approval','queue':[CALL], 'messages':NotCopyable()},
        'done':{'status':'completed','messages':NotCopyable()}}}
    loop = AgentLoop()
    loop.store = SimpleNamespace(inspect=lambda view:copy.deepcopy(view(data)))
    snapshot = loop._waiting_policy_snapshot()
    assert list(snapshot['tasks']) == ['waiting']
    assert snapshot['tasks']['waiting']['local_execution']['desktop_access']


def test_alias_is_issued_only_for_verified_enabled_builtin(tmp_path):
    from types import SimpleNamespace

    from rynmesh.agent_runtime.extensions import (
        COMPUTER_USE_PLUGIN_ID,
        ExtensionStore,
        ExtensionToolAdapter,
    )
    from rynmesh.services import peer_box
    store = ExtensionStore(tmp_path / "agent", peer_box.load_or_create_messaging_key(tmp_path / "messaging.key"))
    adapter = ExtensionToolAdapter(SimpleNamespace(), lambda: None, extension_store=store)
    store.set_plugin_enabled(COMPUTER_USE_PLUGIN_ID, True)
    metadata = adapter.metadata(CALL["name"])
    assert metadata["workstation_permission_tool"] == "ryn_computer_action"
    assert decide(CALL, metadata, policy(), policy()).action == "execute"
    store.set_plugin_enabled(COMPUTER_USE_PLUGIN_ID, False)
    assert not adapter.metadata(CALL["name"]).get("workstation_permission_tool")


def test_overlay_hides_on_pause_failure_completion_and_keeps_other_running_task():
    overlay = ComputerOverlay()
    for terminal in ("paused", "waiting_approval", "completed", "failed", "cancelled"):
        overlay._tasks = {"a": "正在查看屏幕", "b": "正在操作窗口"}
        overlay.sync({"a": {"status": terminal}, "b": {"status": "running", "kind": "model"}})
        assert set(overlay._tasks) == {"b"}
        assert "思考下一步" in overlay._text()
    overlay.sync({"b": {"status": "running", "pending_control": True}})
    assert overlay._text() == ""
    overlay.close()


def test_overlay_renders_feathered_halo_and_three_phase_hud_capsules():
    from rynmesh.agent_runtime.computer_overlay import (
        HALO_BREADTH,
        render_hud_capsule,
        render_overlay_frame,
    )

    for phase in ("正在查看屏幕", "正在操作窗口", "正在思考下一步"):
        capsule = render_hud_capsule(phase, pulse_step=1)
        assert capsule.mode == "RGBA"
        assert capsule.width >= 420 and capsule.height >= 60

    frame = render_overlay_frame(640, 360, "正在操作窗口", pulse_step=2)
    assert frame.size == (640, 360)
    # Screen edge has soft glow (alpha > 0) that feathers smoothly inward to 0
    edge_alpha = frame.getpixel((4, 180))[3]
    mid_halo_alpha = frame.getpixel((HALO_BREADTH // 2, 180))[3]
    center_alpha = frame.getpixel((320, 180))[3]
    assert edge_alpha > mid_halo_alpha > 0
    assert center_alpha == 0


def test_overlay_escape_stops_active_task_and_blocks_turn_until_resumed(tmp_path):
    from types import SimpleNamespace

    from rynmesh.agent_runtime.computer_overlay import ESCAPE_STOP_MESSAGE
    from rynmesh.agent_runtime.native_computer import NativeComputerFacade
    from rynmesh.agent_runtime.store import AgentToolRejected

    data = {
        "settings": {**policy(), "enabled": True, "workspace_root": str(tmp_path)},
        "tasks": {
            "task-esc": {
                "id": "task-esc",
                "status": "running",
                "control_revision": 3,
                "revision": 3,
                "event_cursor": 0,
                "events": [],
                "messages": [],
                "queue": [],
                "in_flight": {"kind": "tool", "name": "computer_observe_windows"},
                "local_execution": {"desktop_access": True, "workspace_root": str(tmp_path)},
            }
        },
    }

    def _inspect(fn):
        return fn(data)

    def _transaction(fn):
        return fn(data)

    owner_store = SimpleNamespace(
        home=tmp_path,
        root=tmp_path,
        inspect=_inspect,
        transaction=_transaction,
    )
    facade = NativeComputerFacade(owner_store)
    try:
        facade.overlay._tasks["task-esc"] = "正在操作窗口"
        assert "Esc" in facade.overlay._text()
        assert "退出控制" in facade.overlay._text()

        # Self-injected Escape action suppresses false-positive user Esc detection
        facade._maybe_suppress_self_escape({"type": "key_press", "key": "Escape"})
        assert facade.overlay._suppress_escape_until > 0.0

        # Physical Esc press hides overlay, pauses task in store, and blocks further turn calls
        escaped = facade.overlay.trigger_escape()
        assert escaped == ["task-esc"]
        assert facade.overlay._text() == ""
        row = data["tasks"]["task-esc"]
        assert row["status"] == "paused"
        assert row["turn_stopped"] is True
        assert any(e.get("reason") == "user_escape_stopped" for e in row["events"])

        with pytest.raises(AgentToolRejected) as exc_info:
            facade.execute_local_read({}, "task-esc", context={"control_revision": 3})
        assert str(exc_info.value) == "agent_cancelled"
        assert exc_info.value.correction == ESCAPE_STOP_MESSAGE

        # Once the owner resumes the task in a new turn, the escape latch clears automatically
        row["status"] = "running"
        row.pop("turn_stopped", None)
        row.pop("pending_control", None)
        row["control_revision"] += 1
        assert not facade._is_task_escaped("task-esc", context={"control_revision": row["control_revision"]})
    finally:
        facade.close()
