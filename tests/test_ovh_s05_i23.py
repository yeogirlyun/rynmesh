"""S05-I23: real routing, bounded approvals and large file operations."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from test_agent_apply_patch import patch, specs
from test_agent_local_tools import execute, local  # noqa: F401
from test_agent_integrations import node  # noqa: F401
from test_agent_runtime import advance, approve, assistant, make_runtime

from rynmesh.agent_runtime import local_patch, output_policy
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.local_specs import SPECS, TEXT
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.store import AgentToolRejected, REJECTION_CORRECTIONS, encoded
from rynmesh.agent_runtime.tool_router import ToolCallProtocolError, normalize_tool_calls


def call(name, args, identifier="large"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_t1_shared_schema_and_transport_limits_leave_small_text_unchanged():
    assert output_policy.MAX_TOOL_CALL_ARGUMENT_BYTES == 256 * 1024
    assert output_policy.MAX_RAW_RESPONSE_BYTES == 640 * 1024
    maximum = output_policy.MAX_TOOL_CALL_ARGUMENT_BYTES - 8 * 1024
    for name, field in [("ryn_file_write", "content"), ("ryn_file_edit", "new_text"), ("ryn_apply_patch", "patch")]:
        assert SPECS[name][3]["properties"][field]["maxLength"] == maximum
    assert TEXT["maxLength"] == 16000
    assert SPECS["ryn_file_edit"][3]["properties"]["old_text"]["maxLength"] == 16000
    assert "ryn_apply_patch" in SPECS["ryn_file_write"][0]
    assert "ryn_apply_patch" in SPECS["ryn_file_edit"][0]


def test_t2_hundred_file_patch_executes_and_review_degrades_instead_of_failing(local):
    tools, context, workspace, _ = local
    text = patch(*(line for i in range(100) for line in (f"*** Add File: file{i}.txt", "+hello")))
    result = execute(tools, context, "ryn_apply_patch", {"patch": text})
    assert result["applied"] and len(result["files"]) == 100
    assert all((workspace / f"file{i}.txt").read_text() == "hello\n" for i in range(100))
    review = tools.change_review(context["task_id"], str(workspace))
    assert review["snapshot_skipped"] == 36
    assert len(review["files"]) == 100
    with pytest.raises(AgentToolRejected, match="agent_patch_too_many_files"):
        local_patch.parse(patch(*(line for i in range(129) for line in (f"*** Add File: x{i}", "+x"))))


def test_t3_real_router_accepts_200kb_and_keeps_argument_and_response_bounds():
    schema = specs(["ryn_file_write"])
    args = {"path": "large.txt", "content": "x" * 200000}
    routed = normalize_tool_calls([call("ryn_file_write", args)], schema)
    assert routed[0]["error_result"] is None and routed[0]["arguments"] == args
    too_big = {**args, "content": "x" * (256 * 1024)}
    rejected = normalize_tool_calls([call("ryn_file_write", too_big)], schema)[0]
    assert rejected["oversize"] is True
    assert rejected["error_result"]["error"] == "agent_model_output_limit"
    assert rejected["arguments"] == {} and rejected["error_result"]["not_executed"]
    # Three legal calls exceed the old response ceiling; four exceed the new one.
    batch = [call("ryn_file_write", args, f"c{i}") for i in range(4)]
    assert all(item["error_result"] is None for item in normalize_tool_calls(batch[:3], schema))
    with pytest.raises(ToolCallProtocolError) as error:
        normalize_tool_calls(batch, schema)
    assert error.value.reason == "response_too_large"


@pytest.mark.parametrize("name,field", [("ryn_file_write", "content"), ("ryn_file_edit", "new_text")])
@pytest.mark.parametrize("content", ["x" * 200000, "汉" * 70000], ids=["ascii_200kb", "utf8_210kb"])
def test_t4_large_preview_binds_full_content_hash_and_executes(local, name, field, content):
    tools, context, workspace, _ = local
    args = {"path": "large.txt", field: content}
    if name == "ryn_file_edit":
        (workspace / "large.txt").write_text("before", encoding="utf-8")
        args.update(old_text="before", expected_sha256=hashlib.sha256(b"before").hexdigest())
    original = copy.deepcopy(args)
    preview = tools.preview(name, args, context)
    assert len(encoded(preview)) < 40000
    operation = preview["operation"]
    assert operation[field] == content[:4000]
    assert operation["content_truncated"] is True
    assert operation["content_sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert operation["content_bytes"] == len(content.encode())
    assert args == original
    result = tools.execute(name, args, operation_id="large", context=context, expected_preview=preview, cancelled=lambda: False)
    assert (workspace / "large.txt").read_text(encoding="utf-8") == content
    assert result["sha256"] == preview["after_sha256"]


def test_t4_patch_preview_doubles_diff_budget_and_retains_400_minimum(local):
    tools, context, _, _ = local
    for count, budget in [(5, 4000), (100, 400)]:
        text = patch(*(line for i in range(count) for line in (f"*** Add File: f{i}.txt", "+" + "x" * (budget + 1000))))
        preview = tools.preview("ryn_apply_patch", {"patch": text}, context)
        assert all(len(item["diff"]) == budget for item in preview["files"])


@pytest.mark.parametrize("name,field", [("ryn_file_write", "content"), ("ryn_file_edit", "new_text")])
def test_t5_changed_tail_cannot_reuse_an_approved_truncated_preview(local, name, field):
    tools, context, workspace, _ = local
    args = {"path": "changed.txt", field: "x" * 200000}
    if name == "ryn_file_edit":
        (workspace / "changed.txt").write_bytes(b"before")
        args.update(old_text="before", expected_sha256=hashlib.sha256(b"before").hexdigest())
    preview = tools.preview(name, args, context)
    changed = {**args, field: args[field][:-1] + "y"}
    with pytest.raises(AgentToolRejected, match="agent_approval_preview_changed"):
        tools.execute(name, changed, operation_id="tampered", context=context, expected_preview=preview, cancelled=lambda: False)
    if name == "ryn_file_edit":
        assert (workspace / "changed.txt").read_bytes() == b"before"
    else:
        assert not (workspace / "changed.txt").exists()


def test_t6_real_loop_reports_preview_limit_with_actionable_correction(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path, assistant(name="share_service", args={"service_id": "one"}))
    tools.preview = lambda name, args: {"content": "x" * (300 * 1024)}
    row = runtime.create({"message": "Share the service"})
    advance(runtime, 2)
    result = runtime.get(row["id"])["events"][-1]["result"]
    assert result["error"] == "agent_preview_limit"
    assert result["correction"] == REJECTION_CORRECTIONS["agent_preview_limit"]
    assert "ryn_apply_patch" in result["correction"] and not tools.calls


def test_t4_real_loop_approves_and_executes_200kb_write(tmp_path):
    content = "x" * 200000
    runtime, _, _, _ = make_runtime(tmp_path / "agent", assistant(name="ryn_file_write", args={"path": "large.txt", "content": content}))
    ledger = OperationLedger(tmp_path)
    local_tools = LocalTools(SimpleNamespace(home=tmp_path), lambda: ledger)
    local_tools.store = runtime.store
    class Tools:
        def specs(self):
            return specs(["ryn_file_write"])
        def metadata(self, name):
            return {"effect": "write", "scope_context_supported": True, "preview_precondition": True}
        def preview(self, name, args, *, context):
            return local_tools.preview(name, args, context)
        def execute(self, name, args, **kwargs):
            return local_tools.execute(name, args, **kwargs)
    runtime.tools = Tools()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime.update_settings({"workspace_root": str(workspace), "permission_mode": "confirm_changes"})
    row = runtime.create({"message": "Create the requested large text file"})
    try:
        advance(runtime, 2)
        waiting = runtime.get(row["id"])
        assert waiting["status"] == "waiting_approval"
        assert waiting["pending_approval"]["preview"]["operation"]["content_truncated"] is True
        approve(runtime, row["id"])
        advance(runtime)
        assert (workspace / "large.txt").read_text() == content
    finally:
        local_tools.processes.close()


@pytest.mark.parametrize("name", ["ryn_file_write", "ryn_file_edit", "ryn_apply_patch"])
def test_rework_real_registry_previews_and_executes_200kb(node, tmp_path, name):
    app, store = node
    workspace = tmp_path / "registry-workspace"
    workspace.mkdir()
    runtime, _, _, _ = make_runtime(tmp_path / "registry-agent")
    configuration = {"workspace_root": str(workspace), "desktop_access": False}
    runtime.update_settings(configuration)
    task = runtime.create({"message": "Registry large write"})
    context = {"task_id": task["id"], "local_execution": configuration}
    _, registry, _ = build_agent_integrations(app, store, store.home)
    registry.local.store = runtime.store
    content = "x" * 200000
    if name == "ryn_apply_patch":
        args = {"patch": patch("*** Add File: registry.txt", "+" + content)}
        expected = content + "\n"
    elif name == "ryn_file_edit":
        (workspace / "registry.txt").write_bytes(b"before")
        args = {"path": "registry.txt", "old_text": "before", "new_text": content,
                "expected_sha256": hashlib.sha256(b"before").hexdigest()}
        expected = content
    else:
        args = {"path": "registry.txt", "content": content}
        expected = content
    try:
        preview = registry.preview(name, args, context=context)
        result = registry.execute(name, args, operation_id="registry-large", cancelled=lambda: False,
                                  expected_preview=preview, context=context)
        assert (workspace / "registry.txt").read_text() == expected
        assert result.get("applied", True)
        with pytest.raises(AgentToolRejected) as error:
            registry._validate("ryn_file_read", {"path": "x" * 100000})
        assert error.value.details["max_bytes"] == 96 * 1024
    finally:
        registry.close()


def test_rework_code_mode_child_delivery_uses_router_cap_but_parent_code_stays_96kb():
    from rynmesh.agent_runtime import js_state, tool_delivery
    from rynmesh.agent_runtime.js_runtime.protocol import MAX_CODE_BYTES
    args = {"path": "code-mode.txt", "content": "x" * 200000}
    routed = normalize_tool_calls([call("ryn_file_write", args)], specs(["ryn_file_write"]))[0]
    validated, raw, digest = tool_delivery._validate_router_raw_and_semantic_args(routed)
    assert validated == args and len(raw) > 96 * 1024
    assert digest == hashlib.sha256(raw).hexdigest()
    assert js_state.ROUTER_MAX_ARGUMENT_BYTES == output_policy.MAX_TOOL_CALL_ARGUMENT_BYTES
    assert js_state.MAX_PARENT_CODE_BYTES == min(MAX_CODE_BYTES, 96 * 1024)


def test_rework_preview_limit_has_specific_diagnostic_guidance():
    from rynmesh.agent_runtime.diagnostics import _classify_error
    code, guidance = _classify_error("agent_preview_limit")
    assert code == "agent_preview_limit"
    assert guidance[0] == "narrow_request" and "ryn_apply_patch" in guidance[1]
