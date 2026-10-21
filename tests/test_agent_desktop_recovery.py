"""Regressions from the local desktop send and task-page acceptance run."""
import pytest
from test_agent_cli_models import cli_metadata_catalog as cli_metadata_catalog

from rynmesh.agent_runtime.computer.protocol import NativeBridgeError
from rynmesh.agent_runtime.image_capabilities import compact_native_observation_history
from rynmesh.agent_runtime.native_computer import NativeComputerFacade
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected
from rynmesh.agent_runtime.workspace_artifacts import history


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
    import copy
    import json
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


def test_stale_state_token_includes_read_window_correction():
    from rynmesh.agent_runtime.computer.protocol import NativeBridgeRejected

    err = NativeBridgeRejected("agent_computer_state_token_stale")
    with pytest.raises(AgentToolRejected) as caught:
        NativeComputerFacade._translate_bridge_exception(None, err, is_write=True)
    assert caught.value.pre_dispatch and not caught.value.dispatched
    assert caught.value.correction is not None
    assert "computer_read_window" in caught.value.correction
    assert "computer_observe_windows" in caught.value.correction


def test_materialized_screenshot_projection_triggers_compaction_before_complete(tmp_path, monkeypatch):
    import base64
    import io
    import json

    from PIL import Image
    from test_agent_runtime import advance, assistant, make_runtime

    from rynmesh.agent_runtime import image_capabilities
    from rynmesh.agent_runtime.store import encoded

    runtime, model, tools, _ = make_runtime(tmp_path)
    buf = io.BytesIO()
    Image.new("RGB", (1076, 917), color=(32, 64, 128)).save(buf, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    row = runtime.create({"message": "Open WeCom and send message."})
    task_id = row["id"]
    history_pad = [
        assistant(name="inspect_service", args={"service_id": "step_a"}, call_id="c1")["message"],
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"note": "a" * 35000})},
        assistant(name="inspect_service", args={"service_id": "step_b"}, call_id="c2")["message"],
        {"role": "tool", "tool_call_id": "c2", "content": json.dumps({"note": "b" * 24000})},
        assistant(name="inspect_service", args={"service_id": "step_c"}, call_id="c3")["message"],
        {"role": "tool", "tool_call_id": "c3", "content": json.dumps({"screen": "latest_window"})},
    ]
    runtime.store.transaction(lambda data: data["tasks"][task_id]["messages"].extend(history_pad))

    def fake_materialize(task_row, msgs, **kwargs):
        has_trailing_c3 = any(m.get("role") == "tool" and m.get("tool_call_id") == "c3" for m in msgs)
        if not has_trailing_c3:
            return msgs, {"image_prepared": False, "has_candidate_artifacts": False}
        materialized = [
            *msgs,
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Host-verified screenshot for c3"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
        return materialized, {
            "image_prepared": True,
            "has_candidate_artifacts": True,
            "route_capability": {"supported": True},
            "materialized_artifacts": [],
            "status": "materialized",
            "rejected_artifacts": [],
        }

    monkeypatch.setattr(image_capabilities, "materialize_operation_images", fake_materialize)
    monkeypatch.setattr(image_capabilities, "require_route_image_support", lambda *a, **k: None)

    stored = runtime.store.read()["tasks"][task_id]
    context_limit = runtime._context_limit(stored)
    raw_msgs = runtime._context(stored)
    specs = runtime._specs(stored)
    # Raw text bytes fit under context_limit (84480), but materialized image projection exceeds it!
    assert len(encoded(raw_msgs)) + len(encoded(specs)) < context_limit
    assert runtime._request_context_bytes(stored, raw_msgs, specs) > context_limit

    summaries = []

    def complete(model_id, messages, tool_specs, cancelled, on_event):
        if not tool_specs:
            summaries.append(messages)
            return assistant("Compacted step_a and step_b history.")
        # Trailing screenshot must still be present on the main model call after compaction
        assert any(isinstance(m.get("content"), list) for m in messages)
        return assistant("Completed desktop send after viewing screenshot.")

    model.complete = complete
    advance(runtime, 2)
    done = runtime.get(task_id)
    assert done["status"] == "completed", done.get("error")
    assert len(summaries) == 1


def test_full_history_checkpoint_preserves_reasoning_content_and_oversized_read_window_artifacts(tmp_path):
    from rynmesh.agent_runtime import results

    # 1. Oversized computer_read_window result preserves native_observation_artifacts & scalar structuredContent
    ref = {"id": "result_" + "a" * 32, "sha256": "b" * 64, "bytes": 22000}
    artifact = {
        "id": "native_img_1234",
        "path": ".ryn/computer/obs_1.png",
        "sha256": "c" * 64,
        "bytes": 42000,
        "mime_type": "image/png",
        "width": 1280,
        "height": 720,
        "observation_id": "obs_1",
        "state_token": "st_chrome_1",
        "provenance": "native_computer_observation",
        "coordinate_authority": True,
        "window_ref": {"hwnd": 267326, "pid": 1000, "create_time_100ns": 1},
    }
    read_payload = {
        "server_id": "computer-use",
        "tool": "computer_read_window",
        "model_tool_name": "mcp__f37855bd16e7__computer_read_window",
        "is_error": False,
        "text": "Window Chrome hwnd=267326 state_token=st_chrome_1",
        "native_observation_artifacts": [artifact],
        "structuredContent": {
            "status": "ok",
            "hwnd": 267326,
            "title": "Gshopper - Google Chrome",
            "state_token": "st_chrome_1",
            "observation_id": "obs_1",
            "elements": [{"element_id": f"el_{i}", "name": "item" * 50} for i in range(120)],
        },
    }
    formatted = results.format_reference_result(
        "mcp__f37855bd16e7__computer_read_window",
        read_payload,
        ref,
        max_bytes=6000,
    )
    assert formatted.get("truncated_inline") is True
    assert formatted.get("native_observation_artifacts") == [artifact]
    assert formatted.get("structuredContent", {}).get("state_token") == "st_chrome_1"
    assert formatted.get("structuredContent", {}).get("hwnd") == 267326
    assert "elements" not in formatted.get("structuredContent", {})

    # 2. Full-history checkpoint retains reasoning_content="" on synthetic checkpoint assistant message
    #    and compacts historical computer_act_window receipts inside checkpoint_note.
    from test_agent_runtime import make_runtime

    runtime, _, _, _ = make_runtime(tmp_path)
    created = runtime.create({"message": "Turn 1 and Turn 2"})
    task_id = created["id"]

    def seed_checkpoint(data):
        row = data["tasks"][task_id]
        row["messages"] = [
            {"role": "user", "content": "Send WeCom message", "context_epoch": 0},
            {
                "role": "assistant",
                "content": "Clicking send",
                "reasoning_content": "thinking step 1",
                "tool_calls": [
                    {
                        "id": "call_act_1",
                        "type": "function",
                        "function": {"name": "mcp__f37855bd16e7__computer_act_window", "arguments": "{}"},
                    }
                ],
                "context_epoch": 0,
            },
            {"role": "tool", "tool_call_id": "call_act_1", "content": "{}", "context_epoch": 0},
        ]
        row["events"].extend(
            [
                {"type": "tool_started", "tool": "mcp__f37855bd16e7__computer_act_window", "call_id": "call_act_1", "effect": "write", "context_epoch": 0},
                {
                    "type": "tool_result",
                    "tool": "mcp__f37855bd16e7__computer_act_window",
                    "call_id": "call_act_1",
                    "context_epoch": 0,
                    "result": {
                        "server_id": "computer-use",
                        "tool": "computer_act_window",
                        "is_error": False,
                        "text": "Clicked send",
                        "native_observation_artifacts": [artifact],
                        "structuredContent": {
                            "status": "committed",
                            "hwnd": 329444,
                            "title": "企业微信",
                            "action": "click",
                            "action_delivered": True,
                            "mutation": True,
                            "state_token": "st_old",
                            "elements": [{"element_id": f"wecom_{i}", "name": "pad" * 40} for i in range(80)],
                        },
                    },
                },
            ]
        )
        row["context_checkpoint"] = {
            "through": len(row["messages"]),
            "content": "Completed Turn 1 and read Chrome window.",
            "references": {},
        }

    runtime.store.transaction(seed_checkpoint)
    stored = runtime.store.read()["tasks"][task_id]
    built = runtime._context(stored)
    assistants = [m for m in built if m.get("role") == "assistant"]
    assert len(assistants) == 1
    assert assistants[0]["content"].startswith("Historical checkpoint")
    assert assistants[0].get("reasoning_content") == ""
    assert "wecom_0" not in assistants[0]["content"]
    assert '"status":"committed"' in assistants[0]["content"]
