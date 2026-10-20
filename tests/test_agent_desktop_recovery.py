"""Regressions from the local desktop send and task-page acceptance run."""
import pytest

from rynmesh.agent_runtime.computer.protocol import NativeBridgeError
from rynmesh.agent_runtime.native_computer import NativeComputerFacade
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected
from rynmesh.agent_runtime.workspace_artifacts import history
from rynmesh.agent_runtime.image_capabilities import compact_native_observation_history
from test_agent_cli_models import cli_metadata_catalog


def blocked_click(**overrides):
    fields = dict(method="action.commit", commit_attempted=True, dispatched=True,
                  native_code="window_occluded", native_stage="validating",
                  native_mutation="possible", internal_mutation="focus_only",
                  reason="hit_test_unrelated_window")
    return NativeBridgeError("agent_computer_window_occluded", **(fields | overrides))


def test_validated_occlusion_is_correctable_without_replaying_send():
    with pytest.raises(AgentToolRejected) as caught:
        NativeComputerFacade._translate_bridge_exception(None, blocked_click(), is_write=True)
    assert caught.value.input_not_delivered
    assert caught.value.dispatched and not caught.value.pre_dispatch
    assert "fresh screenshot" in caught.value.correction


def test_focus_failure_before_input_is_correctable():
    error = blocked_click(native_code="focus_failed", native_stage="activating",
                          reason="foreground_verification_failed")
    with pytest.raises(AgentToolRejected) as caught:
        NativeComputerFacade._translate_bridge_exception(None, error, is_write=True)
    assert caught.value.input_not_delivered
    for change in (dict(native_stage="input_started"), dict(internal_mutation="unknown_or_partial"), dict(callback_uncertain=True)):
        fields = dict(native_code="focus_failed", native_stage="activating", reason="foreground_verification_failed") | change
        with pytest.raises(AgentError, match="agent_write_outcome_unknown") as failure:
            NativeComputerFacade._translate_bridge_exception(None, blocked_click(**fields), is_write=True)
        assert not isinstance(failure.value, AgentToolRejected)


@pytest.mark.parametrize("change", [dict(callback_uncertain=True), dict(native_stage="committing"),
    dict(internal_mutation=None), dict(reason=None), dict(method="observe")])
def test_uncertain_input_still_stops_without_replay(change):
    with pytest.raises(AgentError, match="agent_write_outcome_unknown") as caught:
        NativeComputerFacade._translate_bridge_exception(None, blocked_click(**change), is_write=True)
    assert not isinstance(caught.value, AgentToolRejected)


def test_selected_model_never_discovers_unrelated_providers(cli_metadata_catalog):
    f = cli_metadata_catalog
    selected = []
    f.catalog.services["discover_local_model"] = lambda network, service: selected.append(service) or [f.record]
    f.catalog.services["discover"] = lambda _: pytest.fail("unrelated provider scan")
    assert f.catalog.get_model("local/codex-cli/model/model-a")["id"].endswith("model-a")
    assert selected == ["codex-cli"]


def test_display_uses_passive_catalog(cli_metadata_catalog):
    f = cli_metadata_catalog
    f.catalog.services["discover_display"] = lambda _: [f.record]
    f.catalog.services["discover"] = lambda _: pytest.fail("blocking remote catalog")
    assert f.catalog.display_models()


def test_native_screenshot_is_openable_only_with_matching_task_receipt(tmp_path):
    item = {"path": ".ryn/computer/task/op/image_1.png", "sha256": "a" * 64,
            "native_descriptor": {"task_id": "task", "operation_id": "op", "source_sha256": "a" * 64}}
    receipt = {"status": "confirmed", "tool": "mcp__computer_use__computer_read_window__abc",
               "operation_id": "op", "extension_binding": {"server_id": "computer-use", "instance_id": "builtin-computer-use"},
               "result": {"native_observation_artifacts": [item]}}
    row = {"id": "task", "local_execution": {"workspace_root": str(tmp_path)}, "receipts": [receipt]}
    assert len(history(row)) == 1
    assert next(iter(history(row).values()))["source"] == "computer_observation"
    item["native_descriptor"]["task_id"] = "other-task"
    assert not history(row)
    item["native_descriptor"]["task_id"] = "task"
    item["path"] = "../escape.png"
    assert not history(row)
    item["path"] = ".ryn/computer/task/op/image_1.png"
    receipt["extension_binding"]["server_id"] = "external"
    assert not history(row)


def test_old_native_screens_are_compacted_but_current_evidence_and_unknown_writes_remain():
    import copy,json
    messages=[];receipts=[];events=[]
    for index, (tool,status,builtin) in enumerate([
        ("computer_read_window","confirmed",True),
        ("computer_act_window","confirmed",True),
        ("computer_act_window","unknown",True),
        ("computer_read_window","confirmed",False),
        ("computer_read_window","confirmed",True),
    ]):
        call,op=f"c{index}",f"op{index}"
        result={"tool":tool,"structuredContent":{"status":"committed","hwnd":123,"state_token":"expired_sensitive_geometry","elements":[{"name":"x"*5000}],"action":"key","mutation":"delivered"}}
        messages.extend([{"role":"assistant","content":"","tool_calls":[{"id":call,"function":{"name":tool,"arguments":"{}"}}]},
                         {"role":"tool","tool_call_id":call,"content":json.dumps(result)}])
        receipts.append({"operation_id":op,"status":status,"extension_binding":{"server_id":"computer-use" if builtin else "external","instance_id":"builtin-computer-use"},"result":result})
        events.append({"type":"tool_result","call_id":call,"operation_id":op})
    row={"receipts":receipts,"events":events}
    original=copy.deepcopy(messages)
    projected=compact_native_observation_history(row,messages)
    assert messages==original
    assert "state_token" not in projected[1]["content"] and len(projected[1]["content"])<600
    assert json.loads(projected[3]["content"])["mutation"]=="delivered"
    assert projected[5:]==messages[5:]
    assert projected[0]==messages[0]
