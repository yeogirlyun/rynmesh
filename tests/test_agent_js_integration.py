"""Phase A unit and encrypted-store transaction tests for Ryn JS integration modules.

Scope note:
- Tests exercise the pure/transaction-local Phase A modules (`js_state`,
  `tool_delivery`, `js_integration`) and real encrypted `AgentStore` persistence.
- `ryn_js_execute` is NOT mounted into `AgentRuntime` / `AgentLoop` in Phase A.
- `FakeCoordinatorPort` proves exact session/generation/execution/callback
  addressing and no re-evaluation; it is NOT proof of QuickJS original-Promise
  resumption (which belongs to the Phase B real-worker smoke harness).
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime import budget, control, js_integration, js_state, results, tool_delivery, verification
from rynmesh.agent_runtime.js_integration import (
    DeliveryPlan,
    capture_capability_catalog,
    complete_delivery,
    deliver_committed_token,
    derive_child_identities,
    dispatch_delivery_plan,
    plan_child_call,
    select_delivery,
)
from rynmesh.agent_runtime.js_runtime.protocol import (
    MAX_OUTPUT_BYTES,
    MAX_RESULT_BYTES,
    JsRuntimeError,
    PendingHostCallEnvelope,
    canonical_arguments_sha256,
    validate_sky_host_call,
)
from rynmesh.agent_runtime.js_state import (
    CONTAINER_TOOL_NAME,
    JS_STATE_VERSION,
    MAX_ADMITTED_CALLBACKS,
    MAX_JS_STATE_BYTES,
    MAX_PARENT_CODE_BYTES,
    MAX_STATE_DEPTH,
    JsStateValidationError,
    build_authority_stamp,
    create_js_execution_state,
    plan_cancellation_js_state,
    plan_continuation_lost_js_state,
    plan_fatal_fence_js_state,
    plan_terminal_js_state,
    project_js_control,
    project_public_js_summary,
    validate_callback_status_transition,
    validate_delivery_transition,
    validate_js_parent_stamp,
    validate_js_state,
    validate_phase_transition,
    validate_task_js_state,
)
from rynmesh.agent_runtime.protocol import digest, now
from rynmesh.agent_runtime.state import initialize
from rynmesh.agent_runtime.store import AgentError, AgentStore, encoded
from rynmesh.agent_runtime.tool_delivery import (
    DeliveryToken,
    derive_outbox_id,
    is_js_child_call,
    stage_child_completion,
    stage_tool_completion,
)
from rynmesh.agent_runtime.tool_router import normalize_tool_calls


HOST_METHOD_MAPPING: dict[str, str] = {
    "list_apps": "ryn_computer_observe_windows",
    "list_windows": "ryn_computer_observe_windows",
    "get_window": "ryn_computer_observe_windows",
    "get_window_state": "ryn_computer_read_window",
    "click": "ryn_computer_act_window",
    "type_text": "ryn_computer_act_window",
}


def _registered_specs() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "ryn_computer_observe_windows",
                "description": "Observe desktop windows.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "method": {"type": "string"},
                        "id": {"type": "integer"},
                        "app": {"type": "string"},
                    },
                    "required": ["method"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ryn_computer_read_window",
                "description": "Read a specific window state.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "method": {"type": "string"},
                        "window": {
                            "type": "object",
                            "properties": {
                                "app": {"type": "string"},
                                "id": {"type": "integer"},
                                "title": {"type": "string"},
                            },
                            "required": ["app", "id"],
                            "additionalProperties": False,
                        },
                        "include_screenshot": {"type": "boolean"},
                        "include_text": {"type": "boolean"},
                    },
                    "required": ["method", "window"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ryn_computer_act_window",
                "description": "Perform a desktop action on a window.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "method": {"type": "string"},
                        "window": {
                            "type": "object",
                            "properties": {
                                "app": {"type": "string"},
                                "id": {"type": "integer"},
                                "title": {"type": "string"},
                            },
                            "required": ["app", "id"],
                            "additionalProperties": False,
                        },
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "click_count": {"type": "integer"},
                        "mouse_button": {"type": "string"},
                        "screenshotId": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["method", "window"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def _registered_metadata(name: str) -> dict[str, Any]:
    table = {
        "ryn_computer_observe_windows": {
            "effect": "read",
            "computer_access": True,
            "scope_context_supported": True,
            "title": "Observe Windows",
        },
        "ryn_computer_read_window": {
            "effect": "read",
            "computer_access": True,
            "scope_context_supported": True,
            "title": "Read Window",
        },
        "ryn_computer_act_window": {
            "effect": "write",
            "computer_access": True,
            "scope_context_supported": True,
            "preview_precondition": True,
            "title": "Act on Window",
        },
    }
    if name not in table:
        raise ValueError(f"Unknown tool {name}")
    return copy.deepcopy(table[name])


def _make_parent_call(
    code: str = "const wins = await sky.list_windows(); return wins.length;",
    *,
    call_id: str = "call_parent_js_1",
    operation_id: str = "op_parent_js_1",
) -> dict[str, Any]:
    args = {"code": code}
    args_json = encoded(args).decode("utf-8")
    schema_snapshot = {
        "name": CONTAINER_TOOL_NAME,
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "timeout_ms": {"type": "integer"},
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    }
    schema_sha = hashlib.sha256(
        encoded({"name": schema_snapshot["name"], "parameters": schema_snapshot["parameters"]})
    ).hexdigest()
    return {
        "call_id": call_id,
        "operation_id": operation_id,
        "name": CONTAINER_TOOL_NAME,
        "arguments": args,
        "arguments_json": args_json,
        "raw_arguments_json": args_json,
        "arguments_sha256": hashlib.sha256(args_json.encode("utf-8")).hexdigest(),
        "schema_snapshot": schema_snapshot,
        "schema_sha256": schema_sha,
        "effect": "read",
        "error_result": None,
        "context_epoch": 0,
        "goal_revision": 1,
        "scope_revision": 1,
        "execution_container": True,
    }


def _make_sibling_call(
    *,
    call_id: str = "call_sibling_1",
    operation_id: str = "op_sibling_1",
) -> dict[str, Any]:
    args = {"service_id": "svc_tail"}
    args_json = encoded(args).decode("utf-8")
    return {
        "call_id": call_id,
        "operation_id": operation_id,
        "name": "inspect_service",
        "arguments": args,
        "arguments_json": args_json,
        "raw_arguments_json": args_json,
        "arguments_sha256": hashlib.sha256(args_json.encode("utf-8")).hexdigest(),
        "effect": "read",
        "error_result": None,
        "context_epoch": 0,
        "goal_revision": 1,
        "scope_revision": 1,
    }


def _seed_task_row(
    store: AgentStore,
    *,
    task_id: str = "task_js_phase_a",
    code: str = "const wins = await sky.list_windows(); return wins.length;",
    with_js_state: bool = True,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    catalog = capture_capability_catalog(_registered_specs(), _registered_metadata, HOST_METHOD_MAPPING)
    parent_call = _make_parent_call(code=code)
    sibling_call = _make_sibling_call()

    def _init(data: dict[str, Any]) -> dict[str, Any]:
        data["settings"]["enabled"] = True
        data["settings"]["desktop_access"] = True
        row: dict[str, Any] = {
            "id": task_id,
            "title": "JS Integration Phase A Test",
            "model_id": "local:test",
            "status": "running",
            "revision": 1,
            "created_at": now(),
            "updated_at": now(),
            "elapsed_seconds": 0.25,
            "steps": 1,
            "context_epoch": 0,
            "permission_mode": "confirm_changes",
            "allowed_write_tools": [],
            "local_execution": {
                "workspace_root": store.default_workspace(),
                "desktop_access": True,
            },
            "model_destination": {
                "catalog_model_id": "local:test",
                "destination_revision": 1,
            },
            "limits": {
                "max_steps": 64,
                "max_seconds": 900,
                "max_context_chars": None,
                "max_output_chars": 20000,
            },
            "messages": [
                {"role": "user", "message_id": f"{task_id}:owner:0", "content": "Inspect windows via JS"},
                {
                    "role": "assistant",
                    "content": "",
                    "context_epoch": 0,
                    "tool_calls": [
                        {
                            "id": parent_call["call_id"],
                            "type": "function",
                            "function": {
                                "name": parent_call["name"],
                                "arguments": parent_call["arguments_json"],
                            },
                        },
                        {
                            "id": sibling_call["call_id"],
                            "type": "function",
                            "function": {
                                "name": sibling_call["name"],
                                "arguments": sibling_call["arguments_json"],
                            },
                        },
                    ],
                },
            ],
            "events": [],
            "receipts": [],
            "result_refs": [],
            "queue": [sibling_call],
            "in_flight": None,
            "pending_approval": None,
            "approved": None,
            "result": None,
            "error": None,
        }
        initialize(row)
        budget.initialize(row)
        if with_js_state:
            js_exec = create_js_execution_state(
                row,
                parent_call,
                execution_id="exec_001",
                session_id="sess_001",
                generation=1,
                capability_catalog=catalog,
            )
            js_exec["phase"] = validate_phase_transition(js_exec["phase"], "running")
            row["js_execution"] = validate_js_state(js_exec, task_row=row)
        data["tasks"][task_id] = row
        return row

    seeded = store.transaction(_init)
    return seeded, catalog


def _assert_durable_message_pairing(row: dict[str, Any]) -> None:
    """Universal durable message pairing checker on raw task history."""
    epoch = row.get("context_epoch", 0)
    assistant_call_ids: list[str] = []
    tool_result_ids: list[str] = []
    seen_assistant: set[str] = set()
    seen_tool: set[str] = set()

    for msg in row.get("messages", []):
        if msg.get("context_epoch", 0) != epoch:
            continue
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                cid = tc.get("id") or tc.get("call_id")
                assert isinstance(cid, str) and cid not in seen_assistant
                seen_assistant.add(cid)
                assistant_call_ids.append(cid)
        elif msg.get("role") == "tool":
            cid = msg.get("tool_call_id")
            assert isinstance(cid, str)
            assert cid not in seen_tool, f"Duplicate tool result for {cid}"
            assert cid in seen_assistant, f"Orphan tool result {cid} without preceding assistant call"
            seen_tool.add(cid)
            tool_result_ids.append(cid)

    js_exec = row.get("js_execution")
    if isinstance(js_exec, dict):
        for cb in (js_exec.get("admitted_callbacks") or {}).values():
            child_cid = cb["call_id"]
            assert child_cid not in seen_assistant, "JS child call_id must never appear in assistant tool_calls"
            assert child_cid not in seen_tool, "JS child call_id must never appear in role:tool messages"


class FakeCoordinatorPort:
    """Injectable coordinator port test double for proving addressing and no re-evaluation.

    Explicitly NOT proof of QuickJS original Promise resumption.
    """

    def __init__(self, *, fail_send: bool = False) -> None:
        self.fail_send = fail_send
        self.resolved: list[dict[str, Any]] = []
        self.rejected: list[dict[str, Any]] = []
        self.started: list[Any] = []

    def start(self, *args: Any, **kwargs: Any) -> Any:
        self.started.append((args, kwargs))
        raise AssertionError("Phase A delivery port must never invoke coordinator.start / evaluate")

    def resolve_host_call(
        self,
        task_id: str,
        *,
        execution_id: str,
        generation: int,
        callback_id: str,
        result: Any,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if self.fail_send:
            raise JsRuntimeError("ryn_js_worker_crashed", "Simulated IPC transport failure during resolve")
        entry = {
            "task_id": task_id,
            "session_id": session_id,
            "execution_id": execution_id,
            "generation": generation,
            "callback_id": callback_id,
            "result": copy.deepcopy(result),
        }
        self.resolved.append(entry)
        return entry

    def reject_host_call(
        self,
        task_id: str,
        *,
        execution_id: str,
        generation: int,
        callback_id: str,
        code: str,
        message: str = "",
        not_executed: bool = True,
        outcome: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if self.fail_send:
            raise JsRuntimeError("ryn_js_worker_crashed", "Simulated IPC transport failure during reject")
        entry = {
            "task_id": task_id,
            "session_id": session_id,
            "execution_id": execution_id,
            "generation": generation,
            "callback_id": callback_id,
            "code": code,
            "message": message,
            "not_executed": not_executed,
            "outcome": outcome,
        }
        self.rejected.append(entry)
        return entry


def test_pure_modules_have_no_import_side_effects_or_forbidden_dependencies():
    """Verify js_state is pure and none of the Phase A modules create managers or call execute."""
    js_state_path = Path(js_state.__file__)
    tree = ast.parse(js_state_path.read_text(encoding="utf-8"))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            imported_names.add(node.module or "")

    forbidden_js_state_imports = {
        "os",
        "subprocess",
        "socket",
        "urllib",
        "http",
        "store",
        "rynmesh.agent_runtime.store",
        "manager",
        "coordinator",
        "runtime",
        "loop",
    }
    assert not (imported_names & forbidden_js_state_imports)

    for mod in (js_state, tool_delivery, js_integration):
        assert not hasattr(mod, "DEFAULT_MANAGER")
        assert not hasattr(mod, "DEFAULT_COORDINATOR")
        mod_src = Path(mod.__file__).read_text(encoding="utf-8")
        mod_tree = ast.parse(mod_src)
        called_attrs = {
            f"{getattr(n.func.value, 'id', '')}.{n.func.attr}"
            for n in ast.walk(mod_tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert "tools.execute" not in called_attrs
        assert "local.perform" not in called_attrs
        assert "LocalTools.perform" not in called_attrs
        assert "extensions.execute" not in called_attrs
        assert "coordinator.run_to_completion" not in called_attrs


def test_encrypted_store_roundtrip_legacy_null_and_safe_projections(tmp_path: Path):
    """Verify real encrypted AgentStore roundtrip, legacy missing->None, and safe projections."""
    key = X25519PrivateKey.generate()
    store = AgentStore(tmp_path, key)

    legacy_row, _ = _seed_task_row(store, task_id="task_legacy", with_js_state=False)
    assert validate_task_js_state(legacy_row) is None
    assert validate_js_state(None) is None
    assert project_js_control(legacy_row) is None
    assert project_public_js_summary(legacy_row) is None

    secret_marker = "const secretMarker = 'do_not_expose_plaintext_js_source_99182';"
    seeded_row, _ = _seed_task_row(
        store,
        task_id="task_encrypted_js",
        code=f"{secret_marker}\nreturn await sky.list_windows();",
        with_js_state=True,
    )
    validated = validate_task_js_state(seeded_row)
    assert validated is not None
    assert validated["version"] == JS_STATE_VERSION
    assert validated["phase"] == "running"
    assert validated["parent_call"]["name"] == CONTAINER_TOOL_NAME

    # Verify detached copy semantics.
    validated["counters"]["host_calls"] = 99
    assert seeded_row["js_execution"]["counters"]["host_calls"] == 0

    # Verify encrypted on-disk task blobs never expose source code in plaintext.
    for file_path in tmp_path.rglob("*.json"):
        raw_disk = file_path.read_text(encoding="utf-8")
        assert secret_marker not in raw_disk
        assert "do_not_expose_plaintext_js_source_99182" not in raw_disk

    # Reopen store from disk with same key.
    reopened = AgentStore(tmp_path, key).read()
    reopened_row = reopened["tasks"]["task_encrypted_js"]
    reopened_js = validate_task_js_state(reopened_row)
    assert reopened_js == seeded_row["js_execution"]

    # Control and public projections omit source code, outbox, and admitted callbacks.
    ctrl = project_js_control(reopened_row)
    assert ctrl is not None
    assert ctrl["active"] is True
    assert ctrl["phase"] == "running"
    assert "secretMarker" not in json.dumps(ctrl)
    assert "delivery_outbox" not in ctrl

    pub = project_public_js_summary(reopened_row)
    assert pub is not None
    assert pub["active"] is True
    assert "secretMarker" not in json.dumps(pub)
    assert "parent_call" not in pub


def test_js_state_strict_closed_validation_and_malformed_rejection(tmp_path: Path):
    """Verify malformed, oversized, deep, base64/data-URL, and dangling states fail closed."""
    key = X25519PrivateKey.generate()
    store = AgentStore(tmp_path, key)
    row, _ = _seed_task_row(store)
    base_state = validate_task_js_state(row)
    assert base_state is not None

    # 1. Wrong version or boolean version.
    bad_ver = copy.deepcopy(base_state)
    bad_ver["version"] = 2
    with pytest.raises(JsStateValidationError, match="agent_js_state_version_unsupported"):
        validate_js_state(bad_ver)

    bool_ver = copy.deepcopy(base_state)
    bool_ver["version"] = True
    with pytest.raises(JsStateValidationError, match="agent_js_state_version_unsupported"):
        validate_js_state(bool_ver)

    # 2. Extra unknown top-level key or authority key.
    extra_key = copy.deepcopy(base_state)
    extra_key["unexpected_guest_field"] = "evil"
    with pytest.raises(JsStateValidationError, match="agent_invalid_js_state"):
        validate_js_state(extra_key)

    extra_auth = copy.deepcopy(base_state)
    extra_auth["authority"]["elevated"] = True
    with pytest.raises(JsStateValidationError, match="agent_invalid_js_state"):
        validate_js_state(extra_auth)

    # 3. Source sha256 mismatch or oversized code (> 96 KiB router cap).
    bad_sha = copy.deepcopy(base_state)
    bad_sha["source_sha256"] = "0" * 64
    with pytest.raises(JsStateValidationError, match="agent_invalid_js_state"):
        validate_js_state(bad_sha)

    huge_code = "a" * (MAX_PARENT_CODE_BYTES + 1)
    with pytest.raises(JsStateValidationError, match="agent_js_state_limit"):
        create_js_execution_state(
            row,
            _make_parent_call(code=huge_code),
            execution_id="exec_huge",
            session_id="sess_001",
            generation=1,
        )

    # 4. Inline data: URL or long base64 blob or forbidden key anywhere in state.
    inline_data_url = copy.deepcopy(base_state)
    inline_data_url["phase"] = "completed"
    inline_data_url["terminal_result"] = {
        "status": "completed",
        "value": {"shot": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA"},
        "value_type": "object",
        "stdout": "",
        "images": [],
        "error": None,
        "child_receipt_refs": [],
    }
    with pytest.raises(JsStateValidationError, match="agent_js_inline_media_forbidden"):
        validate_js_state(inline_data_url)

    raw_b64 = copy.deepcopy(base_state)
    raw_b64["phase"] = "completed"
    raw_b64["terminal_result"] = {
        "status": "completed",
        "value": "A" * 256,
        "value_type": "string",
        "stdout": "",
        "images": [],
        "error": None,
        "child_receipt_refs": [],
    }
    with pytest.raises(JsStateValidationError, match="agent_js_inline_media_forbidden"):
        validate_js_state(raw_b64)

    forbidden_key_state = copy.deepcopy(base_state)
    forbidden_key_state["phase"] = "completed"
    forbidden_key_state["terminal_result"] = {
        "status": "completed",
        "value": {"promise": "serialized_continuation"},
        "value_type": "object",
        "stdout": "",
        "images": [],
        "error": None,
        "child_receipt_refs": [],
    }
    with pytest.raises(JsStateValidationError, match="agent_js_inline_media_forbidden"):
        validate_js_state(forbidden_key_state)

    # 5. Excessive nesting depth (> MAX_STATE_DEPTH).
    nested: Any = "leaf"
    for _ in range(MAX_STATE_DEPTH + 2):
        nested = {"n": nested}
    deep_state = copy.deepcopy(base_state)
    deep_state["phase"] = "completed"
    deep_state["terminal_result"] = {
        "status": "completed",
        "value": nested,
        "value_type": "object",
        "stdout": "",
        "images": [],
        "error": None,
        "child_receipt_refs": [],
    }
    with pytest.raises(JsStateValidationError, match="agent_js_state_depth_limit"):
        validate_js_state(deep_state)

    # 6. Opaque image placeholder with path/url authority fails closed.
    bad_img_state = copy.deepcopy(base_state)
    bad_img_state["phase"] = "completed"
    bad_img_state["terminal_result"] = {
        "status": "completed",
        "value": None,
        "value_type": "undefined",
        "stdout": "",
        "images": [
            {
                "token_id": "imgtok_1",
                "source_child_operation_id": "jsop_1",
                "source_receipt_id": "receipt_jsop_1",
                "source_sha256": "a" * 64,
                "mime_type": "image/png",
                "width": 100,
                "height": 100,
                "path": "C:/Windows/System32/cmd.exe",
            }
        ],
        "error": None,
        "child_receipt_refs": [],
    }
    with pytest.raises(JsStateValidationError, match="agent_invalid_js_state"):
        validate_js_state(bad_img_state)

    # 7. Dangling parent_call without matching assistant tool_call in task row.
    dangling_row = copy.deepcopy(row)
    dangling_row["messages"] = [{"role": "user", "message_id": "u1", "content": "hi"}]
    with pytest.raises(JsStateValidationError, match="agent_js_dangling_parent"):
        validate_task_js_state(dangling_row)


def test_monotonic_phase_callback_and_delivery_transitions(tmp_path: Path):
    """Verify phase, callback status, and outbox delivery state transitions are strictly monotonic."""
    assert validate_phase_transition("starting", "running") == "running"
    assert validate_phase_transition("running", "awaiting_child") == "awaiting_child"
    assert validate_phase_transition("awaiting_child", "awaiting_approval") == "awaiting_approval"
    assert validate_phase_transition("awaiting_approval", "delivering") == "delivering"
    assert validate_phase_transition("delivering", "running") == "running"
    assert validate_phase_transition("running", "completed") == "completed"

    for terminal in ("completed", "continuation_lost", "cancelled"):
        for target in ("starting", "running", "awaiting_child", "delivering"):
            with pytest.raises(JsStateValidationError, match="agent_invalid_js_phase_transition"):
                validate_phase_transition(terminal, target)

    # fenced_unknown cannot transition to running or completed, or to delivering without trusted_reconcile.
    fence = {"operation_id": "jsop_1", "reason": "agent_write_outcome_unknown"}
    with pytest.raises(JsStateValidationError, match="agent_invalid_js_phase_transition"):
        validate_phase_transition("fenced_unknown", "running", fatal_fence=fence)
    with pytest.raises(JsStateValidationError, match="agent_js_fenced_unknown"):
        validate_phase_transition("fenced_unknown", "delivering", fatal_fence=fence, trusted_reconcile=False)
    assert (
        validate_phase_transition("fenced_unknown", "delivering", fatal_fence=fence, trusted_reconcile=True)
        == "delivering"
    )

    # Delivery state monotonicity: staged -> sent -> consumed; never regresses to staged or from lost.
    assert validate_delivery_transition("staged", "sent") == "sent"
    assert validate_delivery_transition("sent", "consumed") == "consumed"
    assert validate_delivery_transition("staged", "lost") == "lost"
    assert validate_delivery_transition("sent", "lost") == "lost"
    for invalid_pair in (("sent", "staged"), ("consumed", "staged"), ("consumed", "lost"), ("lost", "staged"), ("lost", "sent"), ("lost", "consumed")):
        with pytest.raises(JsStateValidationError, match="agent_invalid_js_delivery_transition"):
            validate_delivery_transition(*invalid_pair)

    # Callback status monotonicity.
    assert validate_callback_status_transition("admitted", "awaiting_approval") == "awaiting_approval"
    assert validate_callback_status_transition("awaiting_approval", "dispatched") == "dispatched"
    assert validate_callback_status_transition("dispatched", "completed") == "completed"
    with pytest.raises(JsStateValidationError, match="agent_invalid_js_callback_transition"):
        validate_callback_status_transition("completed", "dispatched")


def test_plan_child_call_stable_ids_host_recomputation_and_catalog_checks(tmp_path: Path):
    """Verify callback-to-child normalization, stable IDs, host effect recomputation, and catalog checks."""
    key = X25519PrivateKey.generate()
    store = AgentStore(tmp_path, key)
    _, catalog = _seed_task_row(store)

    win_args = {"window": {"app": "Notepad", "id": 1}, "x": 40, "y": 50, "click_count": 1, "mouse_button": "left"}
    win_args_sha = canonical_arguments_sha256(win_args)
    envelope = PendingHostCallEnvelope(
        task_id="task_js_phase_a",
        session_id="sess_001",
        generation=1,
        execution_id="exec_001",
        callback_id="cb_001",
        ordinal=0,
        method="click",
        effect="write",
        target_tool="computer_act_window",  # Untrusted logical target from guest/core
        arguments=win_args,
        arguments_sha256=win_args_sha,
    )

    # 1. Guest effect forgery (claiming a write method 'click' is 'read') fails closed.
    forged_read_env = PendingHostCallEnvelope(
        **{**envelope.to_dict(), "effect": "read"}
    )
    with pytest.raises(AgentError, match="agent_js_effect_mismatch"):
        store.transaction(lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], forged_read_env, catalog, HOST_METHOD_MAPPING))

    # 2. Logical target names never authorize MCP aliases or unmapped tools.
    mcp_specs = [
        {
            "type": "function",
            "function": {
                "name": "mcp__desktop__computer_observe_windows",
                "description": "MCP alias",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    with pytest.raises(AgentError, match="agent_js_method_unavailable"):
        capture_capability_catalog(
            mcp_specs,
            {"mcp__desktop__computer_observe_windows": {"effect": "read", "computer_access": True}},
            {"list_windows": "mcp__desktop__computer_observe_windows"},
        )

    # Unmapped Sky method fails closed.
    unmapped_env = PendingHostCallEnvelope(
        task_id="task_js_phase_a",
        session_id="sess_001",
        generation=1,
        execution_id="exec_001",
        callback_id="cb_unmapped",
        ordinal=0,
        method="drag",
        effect="write",
        target_tool="computer_act_window",
        arguments={"window": {"app": "Notepad", "id": 1}, "from_x": 1, "from_y": 1, "to_x": 2, "to_y": 2},
        arguments_sha256=canonical_arguments_sha256(
            {"window": {"app": "Notepad", "id": 1}, "from_x": 1, "from_y": 1, "to_x": 2, "to_y": 2}
        ),
    )
    with pytest.raises(AgentError, match="agent_js_method_unavailable"):
        store.transaction(lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], unmapped_env, catalog, HOST_METHOD_MAPPING))

    # 3. Valid callback admission produces deterministic IDs and queues child ahead of sibling.
    planned_1 = store.transaction(
        lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], envelope, catalog, HOST_METHOD_MAPPING)
    )
    assert planned_1["name"] == "ryn_computer_act_window"
    assert planned_1["effect"] == "write"
    assert planned_1["computer_access"] is True
    assert planned_1.get("internal") is None
    assert planned_1["js_parent"]["callback_id"] == "cb_001"
    assert planned_1["js_parent"]["parent_call_id"] == "call_parent_js_1"

    # Re-polling the exact same callback + canonical args is idempotent and returns identical IDs.
    planned_2 = store.transaction(
        lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], envelope, catalog, HOST_METHOD_MAPPING)
    )
    assert planned_2["call_id"] == planned_1["call_id"]
    assert planned_2["operation_id"] == planned_1["operation_id"]

    persisted_row = store.read()["tasks"]["task_js_phase_a"]
    assert persisted_row["js_execution"]["counters"]["host_calls"] == 1
    assert len(persisted_row["queue"]) == 2
    assert persisted_row["queue"][0]["call_id"] == planned_1["call_id"]
    assert persisted_row["queue"][1]["call_id"] == "call_sibling_1"

    # 4. Same callback_id with different arguments fails closed.
    altered_args = {**win_args, "x": 999}
    altered_env = PendingHostCallEnvelope(
        **{
            **envelope.to_dict(),
            "arguments": altered_args,
            "arguments_sha256": canonical_arguments_sha256(altered_args),
        }
    )
    with pytest.raises(AgentError, match="agent_js_callback_conflict"):
        store.transaction(lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], altered_env, catalog, HOST_METHOD_MAPPING))

    # 5. Wrong generation / session / execution / task fails closed.
    for bad_field, expected_err in (
        ({"task_id": "other_task"}, "agent_js_task_mismatch"),
        ({"session_id": "sess_other"}, "agent_js_session_mismatch"),
        ({"generation": 2}, "agent_js_stale_generation"),
        ({"execution_id": "exec_other"}, "agent_js_execution_mismatch"),
    ):
        bad_env = PendingHostCallEnvelope(**{**envelope.to_dict(), **bad_field})
        with pytest.raises(AgentError, match=expected_err):
            store.transaction(lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], bad_env, catalog, HOST_METHOD_MAPPING))

    # 6. Changed catalog after container creation fails closed.
    changed_catalog = copy.deepcopy(catalog)
    changed_catalog["click"]["title"] = "Mutated Title"
    with pytest.raises(AgentError, match="agent_tool_definition_changed"):
        store.transaction(lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], envelope, changed_catalog, HOST_METHOD_MAPPING))

    # 7. Serial admission: admitting a second callback while cb_001 is still active fails closed.
    second_env = PendingHostCallEnvelope(
        task_id="task_js_phase_a",
        session_id="sess_001",
        generation=1,
        execution_id="exec_001",
        callback_id="cb_002",
        ordinal=1,
        method="list_windows",
        effect="read",
        target_tool="computer_observe_windows",
        arguments={},
        arguments_sha256=canonical_arguments_sha256({}),
    )
    with pytest.raises(AgentError, match="agent_js_child_already_active"):
        store.transaction(lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], second_env, catalog, HOST_METHOD_MAPPING))


def test_stage_child_completion_atomicity_idempotency_no_orphan_messages_and_delivery_loss(tmp_path: Path):
    """Verify transaction-local completion staging, no child messages/notice consumption, and confirmed-vs-lost separation."""
    key = X25519PrivateKey.generate()
    store = AgentStore(tmp_path, key)
    _, catalog = _seed_task_row(store)

    env = PendingHostCallEnvelope(
        task_id="task_js_phase_a",
        session_id="sess_001",
        generation=1,
        execution_id="exec_001",
        callback_id="cb_write_1",
        ordinal=0,
        method="click",
        effect="write",
        target_tool="computer_act_window",
        arguments={"window": {"app": "Notepad", "id": 1}, "x": 10, "y": 20, "click_count": 1, "mouse_button": "left"},
        arguments_sha256=canonical_arguments_sha256(
            {"window": {"app": "Notepad", "id": 1}, "x": 10, "y": 20, "click_count": 1, "mouse_button": "left"}
        ),
    )

    child_call = store.transaction(
        lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], env, catalog, HOST_METHOD_MAPPING)
    )

    # Simulate validation failure inside transaction (inline base64 data URL in result):
    # must roll back atomically without committing receipt, queue pop, or outbox item.
    with pytest.raises(AgentError, match="agent_js_inline_media_forbidden"):
        store.transaction(
            lambda data: stage_child_completion(
                data["tasks"]["task_js_phase_a"],
                child_call,
                receipt_status="confirmed",
                result={"screenshot": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA"},
            )
        )

    after_failed_tx = AgentStore(tmp_path, key).read()["tasks"]["task_js_phase_a"]
    assert after_failed_tx["receipts"] == []
    assert after_failed_tx["js_execution"]["delivery_outbox"] == []
    assert after_failed_tx["queue"][0]["call_id"] == child_call["call_id"]

    # Uncommitted fake token must be rejected by select_delivery / deliver_committed_token.
    fake_uncommitted_token = DeliveryToken(
        task_id="task_js_phase_a",
        outbox_id="outbox_uncommitted",
        parent_operation_id="op_parent_js_1",
        parent_call_id="call_parent_js_1",
        session_id="sess_001",
        generation=1,
        execution_id="exec_001",
        callback_id="cb_write_1",
        ordinal=0,
        method="click",
        tool_name="ryn_computer_act_window",
        call_id=child_call["call_id"],
        operation_id=child_call["operation_id"],
        arguments_sha256=env.arguments_sha256,
        receipt_id="receipt_" + child_call["operation_id"],
        receipt_status="confirmed",
        outcome="succeeded",
        not_executed=False,
        result_ref=None,
        payload={"ok": True},
        payload_sha256=hashlib.sha256(encoded({"ok": True})).hexdigest(),
        delivery_state="staged",
        deliverable=True,
        recorded_at=now(),
    )
    with pytest.raises(AgentError, match="agent_js_uncommitted_delivery_token"):
        select_delivery(
            after_failed_tx,
            fake_uncommitted_token,
            {"task_id": "task_js_phase_a", "session_id": "sess_001", "generation": 1, "execution_id": "exec_001", "alive": True},
        )

    # Now commit valid confirmed child completion (including a server_note that must be stripped).
    flight_record = {
        **child_call,
        "kind": "tool",
        "dispatched": True,
        "authorization": "approved",
        "preview": {"target": "Notepad#1"},
    }
    token = store.transaction(
        lambda data: stage_child_completion(
            data["tasks"]["task_js_phase_a"],
            child_call,
            receipt_status="confirmed",
            result={"clicked": True, "window": {"app": "Notepad", "id": 1}, "server_note": "should_not_persist"},
            flight=flight_record,
        )
    )
    assert isinstance(token, DeliveryToken)
    assert token.deliverable is True
    assert token.receipt_status == "confirmed"
    assert token.outcome == "succeeded"
    assert "server_note" not in token.payload

    # Duplicate stage_child_completion with identical callback + args + outcome is idempotent.
    dup_token = store.transaction(
        lambda data: stage_child_completion(
            data["tasks"]["task_js_phase_a"],
            child_call,
            receipt_status="confirmed",
            result={"clicked": True, "window": {"app": "Notepad", "id": 1}},
            flight=flight_record,
        )
    )
    assert dup_token == token

    # Conflicting stage_child_completion (trying to turn confirmed into not_executed) fails closed.
    with pytest.raises(AgentError, match="agent_js_outbox_conflict"):
        store.transaction(
            lambda data: stage_child_completion(
                data["tasks"]["task_js_phase_a"],
                child_call,
                receipt_status="not_executed",
                result={"error": "owner_denied_operation", "not_executed": True, "no_mutation": True},
            )
        )

    reopened_row = AgentStore(tmp_path, key).read()["tasks"]["task_js_phase_a"]
    _assert_durable_message_pairing(reopened_row)
    assert len(reopened_row["receipts"]) == 1
    receipt = reopened_row["receipts"][0]
    assert receipt["status"] == "confirmed"
    assert receipt["operation_id"] == child_call["operation_id"]
    assert receipt["js_parent"]["callback_id"] == "cb_write_1"
    assert receipt["preview"] == {"target": "Notepad#1"}
    # Child was removed from queue, while tail sibling remains intact at queue[0]!
    assert [item["call_id"] for item in reopened_row["queue"]] == ["call_sibling_1"]
    assert len(reopened_row["js_execution"]["delivery_outbox"]) == 1
    assert reopened_row["js_execution"]["phase"] == "delivering"

    # Simulate transport failure during coordinator send -> continuation_lost,
    # while confirmed business receipt remains confirmed!
    failing_port = FakeCoordinatorPort(fail_send=True)
    live_id = {"task_id": "task_js_phase_a", "session_id": "sess_001", "generation": 1, "execution_id": "exec_001", "alive": True}
    disposition = deliver_committed_token(reopened_row, token, failing_port, live_identity=live_id)
    assert disposition == "continuation_lost"
    assert failing_port.started == []

    store.transaction(
        lambda data: complete_delivery(data["tasks"]["task_js_phase_a"], token, disposition)
    )
    after_loss = AgentStore(tmp_path, key).read()["tasks"]["task_js_phase_a"]
    assert after_loss["js_execution"]["phase"] == "continuation_lost"
    assert after_loss["js_execution"]["delivery_outbox"][0]["delivery_state"] == "lost"
    # Business receipt stays confirmed even though continuation delivery was lost!
    assert len(after_loss["receipts"]) == 1
    assert after_loss["receipts"][0]["status"] == "confirmed"

    # Attempting to redeliver a lost outbox record to a replacement worker returns continuation_lost without calling port.
    healthy_port = FakeCoordinatorPort(fail_send=False)
    replacement_live = {"task_id": "task_js_phase_a", "session_id": "sess_002", "generation": 2, "execution_id": "exec_001", "alive": True}
    redeliver_disp = deliver_committed_token(after_loss, token, healthy_port, live_identity=replacement_live)
    assert redeliver_disp == "continuation_lost"
    assert healthy_port.resolved == []
    assert healthy_port.rejected == []


def test_router_rejection_without_receipt_and_successful_coordinator_delivery(tmp_path: Path):
    """Verify record_receipt=False for router rejections and successful post-commit coordinator resolve/reject."""
    key = X25519PrivateKey.generate()
    store = AgentStore(tmp_path, key)
    _, catalog = _seed_task_row(store)

    # First callback: read window list -> confirmed -> resolved via injected coordinator port.
    read_env = PendingHostCallEnvelope(
        task_id="task_js_phase_a",
        session_id="sess_001",
        generation=1,
        execution_id="exec_001",
        callback_id="cb_read_1",
        ordinal=0,
        method="list_windows",
        effect="read",
        target_tool="computer_observe_windows",
        arguments={},
        arguments_sha256=canonical_arguments_sha256({}),
    )
    read_call = store.transaction(
        lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], read_env, catalog, HOST_METHOD_MAPPING)
    )
    read_token = store.transaction(
        lambda data: stage_tool_completion(
            data["tasks"]["task_js_phase_a"],
            read_call,
            receipt_status="confirmed",
            result={"windows": [{"app": "Notepad", "id": 1, "title": "Untitled"}]},
        )
    )
    snap = store.read()["tasks"]["task_js_phase_a"]
    port = FakeCoordinatorPort()
    live_id = {"task_id": "task_js_phase_a", "session_id": "sess_001", "generation": 1, "execution_id": "exec_001", "alive": True}
    disp = deliver_committed_token(snap, read_token, port, live_identity=live_id)
    assert disp == "sent"
    assert len(port.resolved) == 1
    assert port.resolved[0]["callback_id"] == "cb_read_1"
    assert port.resolved[0]["session_id"] == "sess_001"
    assert port.resolved[0]["generation"] == 1

    store.transaction(lambda data: complete_delivery(data["tasks"]["task_js_phase_a"], read_token, disp))
    after_sent = store.read()["tasks"]["task_js_phase_a"]
    assert after_sent["js_execution"]["phase"] == "running"
    assert after_sent["js_execution"]["delivery_outbox"][0]["delivery_state"] == "sent"

    # Cannot regress delivery_state from 'sent' back to 'sent' or 'staged'.
    with pytest.raises(AgentError, match="agent_invalid_js_delivery_transition"):
        store.transaction(lambda data: complete_delivery(data["tasks"]["task_js_phase_a"], read_token, "sent"))

    # Second callback: pre-dispatch router rejection with record_receipt=False.
    reject_env = PendingHostCallEnvelope(
        task_id="task_js_phase_a",
        session_id="sess_001",
        generation=1,
        execution_id="exec_001",
        callback_id="cb_reject_2",
        ordinal=1,
        method="list_apps",
        effect="read",
        target_tool="computer_observe_windows",
        arguments={},
        arguments_sha256=canonical_arguments_sha256({}),
    )
    reject_call = store.transaction(
        lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], reject_env, catalog, HOST_METHOD_MAPPING)
    )
    reject_call["error_result"] = {
        "error": "agent_tool_arguments_invalid",
        "message": "Simulated router schema rejection.",
        "not_executed": True,
        "no_mutation": True,
    }
    reject_token = store.transaction(
        lambda data: stage_child_completion(
            data["tasks"]["task_js_phase_a"],
            reject_call,
            receipt_status="not_executed",
            result=reject_call["error_result"],
            record_receipt=False,
        )
    )
    assert reject_token.receipt_id is None
    assert reject_token.not_executed is True

    snap_2 = store.read()["tasks"]["task_js_phase_a"]
    # No second receipt was fabricated for the unexecuted router rejection.
    assert len(snap_2["receipts"]) == 1
    disp_2 = deliver_committed_token(snap_2, reject_token, port, live_identity=live_id)
    assert disp_2 == "sent"
    assert len(port.rejected) == 1
    assert port.rejected[0]["callback_id"] == "cb_reject_2"
    assert port.rejected[0]["code"] == "agent_tool_arguments_invalid"
    assert port.rejected[0]["not_executed"] is True


def test_unknown_write_creates_fatal_fence_blocks_new_admission_and_integrates_with_ambiguous(tmp_path: Path):
    """Verify unknown child outcome sets fatal_fence, triggers control.ambiguous(row), and blocks new callbacks."""
    key = X25519PrivateKey.generate()
    store = AgentStore(tmp_path, key)
    _, catalog = _seed_task_row(store)

    write_env = PendingHostCallEnvelope(
        task_id="task_js_phase_a",
        session_id="sess_001",
        generation=1,
        execution_id="exec_001",
        callback_id="cb_unknown_1",
        ordinal=0,
        method="click",
        effect="write",
        target_tool="computer_act_window",
        arguments={"window": {"app": "Notepad", "id": 1}, "x": 5, "y": 5, "click_count": 1, "mouse_button": "left"},
        arguments_sha256=canonical_arguments_sha256(
            {"window": {"app": "Notepad", "id": 1}, "x": 5, "y": 5, "click_count": 1, "mouse_button": "left"}
        ),
    )
    write_call = store.transaction(
        lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], write_env, catalog, HOST_METHOD_MAPPING)
    )

    unknown_token = store.transaction(
        lambda data: stage_child_completion(
            data["tasks"]["task_js_phase_a"],
            write_call,
            receipt_status="unknown",
            flight={**write_call, "kind": "tool", "dispatched": True, "authorization": "approved"},
        )
    )
    assert unknown_token.deliverable is False
    assert unknown_token.receipt_status == "unknown"

    fenced_row = AgentStore(tmp_path, key).read()["tasks"]["task_js_phase_a"]
    _assert_durable_message_pairing(fenced_row)
    assert control.ambiguous(fenced_row) is True
    assert fenced_row["status"] == "interrupted"
    assert fenced_row["error"] == "agent_write_outcome_unknown"
    assert fenced_row["js_execution"]["phase"] == "fenced_unknown"
    assert fenced_row["js_execution"]["fatal_fence"]["operation_id"] == write_call["operation_id"]
    assert fenced_row["receipts"][0]["status"] == "unknown"

    # Neither guest catch nor a second Promise.all write can admit a new callback while fenced.
    second_write_env = PendingHostCallEnvelope(
        task_id="task_js_phase_a",
        session_id="sess_001",
        generation=1,
        execution_id="exec_001",
        callback_id="cb_unknown_2",
        ordinal=1,
        method="type_text",
        effect="write",
        target_tool="computer_act_window",
        arguments={"window": {"app": "Notepad", "id": 1}, "text": "hello"},
        arguments_sha256=canonical_arguments_sha256({"window": {"app": "Notepad", "id": 1}, "text": "hello"}),
    )
    with pytest.raises(AgentError, match="agent_js_fenced_unknown"):
        store.transaction(
            lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], second_write_env, catalog, HOST_METHOD_MAPPING)
        )

    # Delivery port refuses to resolve/reject an unknown fenced token so guest catch cannot unlock it.
    port = FakeCoordinatorPort()
    live_id = {"task_id": "task_js_phase_a", "session_id": "sess_001", "generation": 1, "execution_id": "exec_001", "alive": True}
    assert deliver_committed_token(fenced_row, unknown_token, port, live_identity=live_id) == "fenced_unknown"
    assert port.resolved == []
    assert port.rejected == []

    # Cancellation retains fatal_fence and does not erase the unknown receipt.
    cancelled_js = plan_cancellation_js_state(fenced_row["js_execution"])
    assert cancelled_js["phase"] == "cancelled"
    assert cancelled_js["fatal_fence"]["operation_id"] == write_call["operation_id"]

    # Trusted reconciliation proof for the exact operation_id can clear fatal_fence and stage the proven result.
    reconciled_token = store.transaction(
        lambda data: stage_child_completion(
            data["tasks"]["task_js_phase_a"],
            write_call,
            receipt_status="confirmed",
            result={"clicked": True, "reconciled": True},
            trusted_reconcile=True,
        )
    )
    assert reconciled_token.deliverable is True
    reconciled_row = AgentStore(tmp_path, key).read()["tasks"]["task_js_phase_a"]
    assert reconciled_row["js_execution"]["fatal_fence"] is None
    assert reconciled_row["js_execution"]["phase"] == "delivering"
    assert reconciled_row["receipts"][0]["status"] == "confirmed"


def test_x1_completion_sink_rejects_argument_and_parent_tampering_preserving_durable_state(tmp_path: Path):
    """X1 regression: stage_child_completion rejects dict-only and dict+JSON+hash argument tampering."""
    key = X25519PrivateKey.generate()
    store = AgentStore(tmp_path, key)
    _, catalog = _seed_task_row(store)

    sky_args = {"id": 7}
    env = PendingHostCallEnvelope(
        task_id="task_js_phase_a",
        session_id="sess_001",
        generation=1,
        execution_id="exec_001",
        callback_id="cb_get_win_7",
        ordinal=0,
        method="get_window",
        effect="read",
        target_tool="computer_observe_windows",
        arguments=sky_args,
        arguments_sha256=canonical_arguments_sha256(sky_args),
    )

    child_call = store.transaction(
        lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], env, catalog, HOST_METHOD_MAPPING)
    )
    # Guest Sky arg hash and translated registered-tool arg hash legitimately differ.
    assert child_call["arguments"] == {"id": 7, "method": "get_window"}
    assert child_call["js_parent"]["arguments_sha256"] == canonical_arguments_sha256(sky_args)
    assert child_call["arguments_sha256"] != child_call["js_parent"]["arguments_sha256"]

    # 1. Dictionary-only tamper: change arguments.id to 999 while keeping original arguments_json/hash/js_parent.
    dict_tampered = copy.deepcopy(child_call)
    dict_tampered["arguments"]["id"] = 999
    with pytest.raises(AgentError, match="agent_js_callback_conflict"):
        store.transaction(
            lambda data: stage_child_completion(
                data["tasks"]["task_js_phase_a"],
                dict_tampered,
                receipt_status="confirmed",
                result={"window": {"app": "Notepad", "id": 999}},
            )
        )

    after_dict_tamper = AgentStore(tmp_path, key).read()["tasks"]["task_js_phase_a"]
    assert after_dict_tamper["receipts"] == []
    assert after_dict_tamper["js_execution"]["delivery_outbox"] == []
    assert after_dict_tamper["queue"][0]["arguments"]["id"] == 7

    # 2. Dictionary + JSON + hash tamper: recompute arguments_json and arguments_sha256 for id=999.
    full_tampered = copy.deepcopy(child_call)
    full_tampered["arguments"] = {"id": 999, "method": "get_window"}
    full_tampered["arguments_json"] = encoded(full_tampered["arguments"]).decode("utf-8")
    full_tampered["raw_arguments_json"] = full_tampered["arguments_json"]
    full_tampered["arguments_sha256"] = hashlib.sha256(full_tampered["arguments_json"].encode("utf-8")).hexdigest()
    with pytest.raises(AgentError, match="agent_js_callback_conflict"):
        store.transaction(
            lambda data: stage_child_completion(
                data["tasks"]["task_js_phase_a"],
                full_tampered,
                receipt_status="confirmed",
                result={"window": {"app": "Notepad", "id": 999}},
            )
        )

    after_full_tamper = AgentStore(tmp_path, key).read()["tasks"]["task_js_phase_a"]
    assert after_full_tamper["receipts"] == []
    assert after_full_tamper["js_execution"]["delivery_outbox"] == []
    assert after_full_tamper["queue"][0]["arguments"]["id"] == 7

    # 3. Also test when original call has moved from queue to in_flight.
    def _move_to_flight(data: dict[str, Any]) -> None:
        row = data["tasks"]["task_js_phase_a"]
        popped = row["queue"].pop(0)
        row["in_flight"] = {**popped, "kind": "tool", "dispatched": True}

    store.transaction(_move_to_flight)
    with pytest.raises(AgentError, match="agent_js_callback_conflict"):
        store.transaction(
            lambda data: stage_child_completion(
                data["tasks"]["task_js_phase_a"],
                full_tampered,
                receipt_status="confirmed",
                result={"window": {"app": "Notepad", "id": 999}},
            )
        )

    after_flight_tamper = AgentStore(tmp_path, key).read()["tasks"]["task_js_phase_a"]
    assert after_flight_tamper["receipts"] == []
    assert after_flight_tamper["in_flight"]["arguments"]["id"] == 7

    # 4. Parent call arguments vs arguments_json/assistant tool_call mismatch fails closed.
    bad_parent_state = copy.deepcopy(after_flight_tamper["js_execution"])
    bad_parent_state["parent_call"]["arguments"]["code"] = "return 999;"
    with pytest.raises(JsStateValidationError, match="agent_invalid_js_state"):
        validate_js_state(bad_parent_state, task_row=after_flight_tamper)

    # 5. Valid untampered completion succeeds and records real receipt with id=7.
    token = store.transaction(
        lambda data: stage_child_completion(
            data["tasks"]["task_js_phase_a"],
            child_call,
            receipt_status="confirmed",
            result={"window": {"app": "Notepad", "id": 7}},
        )
    )
    assert token.receipt_status == "confirmed"
    after_valid = AgentStore(tmp_path, key).read()["tasks"]["task_js_phase_a"]
    assert len(after_valid["receipts"]) == 1
    assert after_valid["receipts"][0]["arguments"]["id"] == 7
    assert after_valid["receipts"][0]["status"] == "confirmed"


def test_x2_post_send_completion_and_selection_reject_conflicting_delivery_tokens(tmp_path: Path):
    """X2 regression: select_delivery and complete_delivery reject tokens with conflicting fields before mutation."""
    key = X25519PrivateKey.generate()
    store = AgentStore(tmp_path, key)
    _, catalog = _seed_task_row(store)

    env = PendingHostCallEnvelope(
        task_id="task_js_phase_a",
        session_id="sess_001",
        generation=1,
        execution_id="exec_001",
        callback_id="cb_x2_1",
        ordinal=0,
        method="get_window",
        effect="read",
        target_tool="computer_observe_windows",
        arguments={"id": 7},
        arguments_sha256=canonical_arguments_sha256({"id": 7}),
    )
    child_call = store.transaction(
        lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], env, catalog, HOST_METHOD_MAPPING)
    )
    token = store.transaction(
        lambda data: stage_child_completion(
            data["tasks"]["task_js_phase_a"],
            child_call,
            receipt_status="confirmed",
            result={"window": {"app": "Notepad", "id": 7}},
        )
    )

    live_id = {"task_id": "task_js_phase_a", "session_id": "sess_001", "generation": 1, "execution_id": "exec_001", "alive": True}
    base_dict = token.to_dict()
    conflict_mutations = [
        {"operation_id": "op_foreign"},
        {"call_id": "jsc_foreign"},
        {"arguments_sha256": "f" * 64},
        {"payload_sha256": "e" * 64},
        {"payload": {"window": {"app": "Notepad", "id": 999}}},
        {"receipt_id": "receipt_op_foreign"},
        {"outcome": "failed"},
        {"not_executed": True},
        {"ordinal": 3},
        {"tool_name": "ryn_computer_act_window"},
    ]

    for mutation in conflict_mutations:
        bad_tok = DeliveryToken(**{**base_dict, **mutation})
        snap = store.read()["tasks"]["task_js_phase_a"]
        with pytest.raises(AgentError, match="agent_js_outbox_conflict"):
            select_delivery(snap, bad_tok, live_id)
        with pytest.raises(AgentError, match="agent_js_outbox_conflict"):
            store.transaction(
                lambda data, bt=bad_tok: complete_delivery(data["tasks"]["task_js_phase_a"], bt, "sent")
            )

    unchanged = AgentStore(tmp_path, key).read()["tasks"]["task_js_phase_a"]
    assert unchanged["js_execution"]["delivery_outbox"][0]["delivery_state"] == "staged"
    assert not any(ev.get("operation_id") == "op_foreign" for ev in unchanged["events"])

    # Valid delivery and completion uses durable outbox operation_id.
    port = FakeCoordinatorPort()
    disp = deliver_committed_token(unchanged, token, port, live_identity=live_id)
    assert disp == "sent"
    store.transaction(lambda data: complete_delivery(data["tasks"]["task_js_phase_a"], token, disp))

    after_sent = AgentStore(tmp_path, key).read()["tasks"]["task_js_phase_a"]
    assert after_sent["js_execution"]["delivery_outbox"][0]["delivery_state"] == "sent"
    delivered_events = [ev for ev in after_sent["events"] if ev.get("type") == "js_child_delivered"]
    assert len(delivered_events) == 1
    assert delivered_events[0]["operation_id"] == child_call["operation_id"]

    # Explicit idempotent acknowledgement does not emit a second event or resend.
    store.transaction(
        lambda data: complete_delivery(data["tasks"]["task_js_phase_a"], token, "sent", allow_idempotent=True)
    )
    after_idem = AgentStore(tmp_path, key).read()["tasks"]["task_js_phase_a"]
    assert len([ev for ev in after_idem["events"] if ev.get("type") == "js_child_delivered"]) == 1
    assert deliver_committed_token(after_idem, token, port, live_identity=live_id) == "continuation_lost"
    assert len(port.resolved) == 1


def test_x3_terminal_payload_immutability_and_idempotent_duplicate_notification(tmp_path: Path):
    """X3 regression: once terminal_result exists, only exact identical terminal notifications are idempotent."""
    key = X25519PrivateKey.generate()
    store = AgentStore(tmp_path, key)
    row, _ = _seed_task_row(store)
    running_state = validate_task_js_state(row)
    assert running_state is not None

    done = plan_terminal_js_state(
        running_state,
        status="completed",
        value="first",
        value_type="string",
        stdout="hello\n",
        active_cpu_ms=15,
    )
    assert done["phase"] == "completed"
    assert done["terminal_result"]["status"] == "completed"
    assert done["terminal_result"]["value"] == "first"

    # Exact duplicate terminal notification is idempotent.
    done_dup = plan_terminal_js_state(
        done,
        status="completed",
        value="first",
        value_type="string",
        stdout="hello\n",
        active_cpu_ms=15,
    )
    assert done_dup == done

    # Attempting to rewrite status, value, value_type, stdout, error, or counters fails closed.
    with pytest.raises(JsStateValidationError, match="agent_js_terminal_immutable"):
        plan_terminal_js_state(
            done,
            status="failed",
            value="changed",
            value_type="string",
            error={"code": "changed", "message": "changed"},
        )
    with pytest.raises(JsStateValidationError, match="agent_js_terminal_immutable"):
        plan_terminal_js_state(
            done,
            status="completed",
            value="second",
            value_type="string",
            stdout="hello\n",
            active_cpu_ms=15,
        )
    with pytest.raises(JsStateValidationError, match="agent_js_terminal_immutable"):
        plan_terminal_js_state(
            done,
            status="completed",
            value="first",
            value_type="string",
            stdout="changed stdout\n",
            active_cpu_ms=15,
        )
    with pytest.raises(JsStateValidationError, match="agent_js_terminal_immutable"):
        plan_terminal_js_state(
            done,
            status="completed",
            value="first",
            value_type="string",
            stdout="hello\n",
            active_cpu_ms=99,
        )


def test_x4_live_capability_url_and_raw_hex_token_rejected_across_durable_js_state(tmp_path: Path):
    """X4 regression: live ryn-image:cap URLs and raw hex tokens are rejected in terminal images, values, dict keys, stdout, result_refs, and outbox."""
    key = X25519PrivateKey.generate()
    store = AgentStore(tmp_path, key)
    _, catalog = _seed_task_row(store)

    sky_spec, sky_args, sky_args_sha = validate_sky_host_call(
        "get_window_state",
        {"window": {"app": "Notepad", "id": 1}, "include_screenshot": True},
    )
    env = PendingHostCallEnvelope(
        task_id="task_js_phase_a",
        session_id="sess_001",
        generation=1,
        execution_id="exec_001",
        callback_id="cb_x4_1",
        ordinal=0,
        method="get_window_state",
        effect=sky_spec["effect"],
        target_tool=sky_spec["target_tool"],
        arguments=sky_args,
        arguments_sha256=sky_args_sha,
    )
    child_call = store.transaction(
        lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], env, catalog, HOST_METHOD_MAPPING)
    )

    live_cap_url = "ryn-image:cap:" + ("a1" * 32)
    raw_hex_token = "b2" * 32

    # 1. Outbox completion payload containing a literal capability URL in a value OR dict key fails closed.
    with pytest.raises(AgentError, match="agent_js_live_capability_forbidden"):
        store.transaction(
            lambda data: stage_child_completion(
                data["tasks"]["task_js_phase_a"],
                child_call,
                receipt_status="confirmed",
                result={"screenshot_url": live_cap_url},
            )
        )
    with pytest.raises(AgentError, match="agent_js_live_capability_forbidden"):
        store.transaction(
            lambda data: stage_child_completion(
                data["tasks"]["task_js_phase_a"],
                child_call,
                receipt_status="confirmed",
                result={live_cap_url: "leaked_in_dict_key"},
            )
        )

    # 1b. result_ref containing a literal capability URL in string, dict value, or dict key fails closed before copying.
    valid_ref_meta = results.save(
        store,
        store.read()["tasks"]["task_js_phase_a"],
        child_call,
        {"window": {"app": "Notepad", "id": 1}, "artifact_ref": "imgref_notepad_1"},
    )
    for bad_ref in (
        live_cap_url,
        {**valid_ref_meta, "tool": live_cap_url},
        {**valid_ref_meta, live_cap_url: "bad"},
    ):
        with pytest.raises(AgentError, match="agent_js_live_capability_forbidden"):
            store.transaction(
                lambda data, br=bad_ref: stage_child_completion(
                    data["tasks"]["task_js_phase_a"],
                    child_call,
                    receipt_status="confirmed",
                    result={"window": {"app": "Notepad", "id": 1}},
                    result_ref=br,
                )
            )
    assert store.read()["tasks"]["task_js_phase_a"].get("result_refs", []) == []

    # Complete child with clean metadata and real results.save result_ref so receipt_id exists for terminal image provenance.
    token = store.transaction(
        lambda data: stage_child_completion(
            data["tasks"]["task_js_phase_a"],
            child_call,
            receipt_status="confirmed",
            result={"window": {"app": "Notepad", "id": 1}, "artifact_ref": "imgref_notepad_1"},
            result_ref=valid_ref_meta,
        )
    )
    store.transaction(lambda data: complete_delivery(data["tasks"]["task_js_phase_a"], token, "sent"))
    state = store.read()["tasks"]["task_js_phase_a"]["js_execution"]
    receipt_id = "receipt_" + child_call["operation_id"]

    # 2. Live capability URL or raw hex token as image.token_id fails closed.
    for bad_token_id in (live_cap_url, raw_hex_token, "ryn-image:cap:short", "cap_secret"):
        bad_image = {
            "token_id": bad_token_id,
            "source_child_operation_id": child_call["operation_id"],
            "source_receipt_id": receipt_id,
            "source_sha256": "c" * 64,
            "mime_type": "image/png",
            "width": 800,
            "height": 600,
        }
        with pytest.raises(JsStateValidationError, match="agent_js_live_capability_forbidden"):
            plan_terminal_js_state(state, status="completed", images=[bad_image])

    # 3. Live capability URL in terminal value, terminal dict key, or stdout fails closed.
    with pytest.raises(JsStateValidationError, match="agent_js_live_capability_forbidden"):
        plan_terminal_js_state(state, status="completed", value={"leaked": live_cap_url}, value_type="object")
    with pytest.raises(JsStateValidationError, match="agent_js_live_capability_forbidden"):
        plan_terminal_js_state(state, status="completed", value={live_cap_url: 1}, value_type="object")
    with pytest.raises(JsStateValidationError, match="agent_js_live_capability_forbidden"):
        plan_terminal_js_state(state, status="completed", stdout=f"saw {live_cap_url}\n")

    # 4. Valid inert artifact reference ID round-trips cleanly through encrypted store.
    valid_image = {
        "token_id": "imgref_notepad_1",
        "source_child_operation_id": child_call["operation_id"],
        "source_receipt_id": receipt_id,
        "source_sha256": "c" * 64,
        "mime_type": "image/png",
        "width": 800,
        "height": 600,
    }
    completed_state = plan_terminal_js_state(
        state,
        status="completed",
        value={"ok": True},
        value_type="object",
        images=[valid_image],
    )
    store.transaction(lambda data: data["tasks"]["task_js_phase_a"].update(js_execution=completed_state))
    reopened = AgentStore(tmp_path, key).read()["tasks"]["task_js_phase_a"]
    validated_reopened = validate_task_js_state(reopened)
    assert validated_reopened is not None
    assert validated_reopened["terminal_result"]["images"][0]["token_id"] == "imgref_notepad_1"


def test_a02_router_raw_vs_semantic_contract_real_receipts_and_22_field_token_integrity(tmp_path: Path):
    """A02 regression: real normalize_tool_calls raw-vs-semantic contract, real verification.record receipts without call_id, router schema rejection, and 22-field token sweep."""
    key = X25519PrivateKey.generate()
    store = AgentStore(tmp_path, key)

    # 1. Real normalize_tool_calls parent call with pretty-printed / non-canonical key order JSON.
    parent_specs = [
        {
            "type": "function",
            "function": {
                "name": CONTAINER_TOOL_NAME,
                "description": "Execute bounded JS.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "timeout_ms": {"type": "integer"},
                    },
                    "required": ["code"],
                    "additionalProperties": False,
                },
            },
        },
        *_registered_specs(),
    ]
    indented_parent_json = '{\n  "timeout_ms": 5000,\n  "code": "return await sky.get_window({id: 7});"\n}'
    normalized_parents = normalize_tool_calls(
        [
            {
                "id": "call_parent_indented_1",
                "type": "function",
                "function": {
                    "name": CONTAINER_TOOL_NAME,
                    "arguments": indented_parent_json,
                },
            }
        ],
        parent_specs,
        metadata=lambda name: {"effect": "read"}
        if name == CONTAINER_TOOL_NAME
        else _registered_metadata(name),
    )
    assert len(normalized_parents) == 1
    real_parent_call = normalized_parents[0]
    real_parent_call.update(operation_id="op_parent_indented_1", execution_container=True)
    assert real_parent_call.get("error_result") is None
    # Confirm raw string hash differs from canonical encoded(arguments) hash, as produced by real tool_router.
    assert real_parent_call["arguments_sha256"] == hashlib.sha256(indented_parent_json.encode("utf-8")).hexdigest()
    assert real_parent_call["arguments_sha256"] != hashlib.sha256(encoded(real_parent_call["arguments"])).hexdigest()

    row, catalog = _seed_task_row(store, task_id="task_indented_parent", with_js_state=False)
    row["messages"].append(
        {
            "role": "assistant",
            "message_id": "msg_asst_indented",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_parent_indented_1",
                    "type": "function",
                    "function": {
                        "name": CONTAINER_TOOL_NAME,
                        "arguments": indented_parent_json,
                    },
                }
            ],
        }
    )

    # Parent container call with direct computer_access=True or extension_binding must fail closed.
    bad_parent_comp = copy.deepcopy(real_parent_call)
    bad_parent_comp["computer_access"] = True
    with pytest.raises(JsStateValidationError, match="agent_invalid_js_state"):
        create_js_execution_state(
            row,
            bad_parent_comp,
            execution_id="exec_indented_1",
            session_id="sess_indented_1",
            generation=1,
            capability_catalog=catalog,
        )
    bad_parent_ext = copy.deepcopy(real_parent_call)
    bad_parent_ext["extension_binding"] = {"extension_id": "ext.bad", "version": "1.0.0"}
    with pytest.raises(JsStateValidationError, match="agent_invalid_js_state"):
        create_js_execution_state(
            row,
            bad_parent_ext,
            execution_id="exec_indented_1",
            session_id="sess_indented_1",
            generation=1,
            capability_catalog=catalog,
        )

    js_exec = create_js_execution_state(
        row,
        real_parent_call,
        execution_id="exec_indented_1",
        session_id="sess_indented_1",
        generation=1,
        capability_catalog=catalog,
    )
    js_exec["phase"] = "running"
    row["js_execution"] = js_exec
    store.transaction(lambda data: data["tasks"].update(task_indented_parent=copy.deepcopy(row)))

    persisted_row = store.read()["tasks"]["task_indented_parent"]
    validated_indented = validate_task_js_state(persisted_row)
    assert validated_indented is not None
    assert validated_indented["parent_call"]["arguments"]["code"] == "return await sky.get_window({id: 7});"

    # Negative parent checks: changed assistant code or duplicate assistant tool_call with conflicting code fails closed.
    tampered_asst_code = copy.deepcopy(persisted_row)
    tampered_asst_code["messages"][-1]["tool_calls"][0]["function"]["arguments"] = json.dumps(
        {"timeout_ms": 5000, "code": "return 'different';"}
    )
    with pytest.raises(JsStateValidationError, match="agent_invalid_js_state"):
        validate_task_js_state(tampered_asst_code)

    dup_asst_conflict = copy.deepcopy(persisted_row)
    dup_asst_conflict["messages"].append(
        {
            "role": "assistant",
            "message_id": "msg_asst_dup",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_parent_indented_1",
                    "type": "function",
                    "function": {
                        "name": CONTAINER_TOOL_NAME,
                        "arguments": json.dumps({"timeout_ms": 5000, "code": "return 'conflict';"}),
                    },
                }
            ],
        }
    )
    with pytest.raises(JsStateValidationError, match="agent_invalid_js_state"):
        validate_task_js_state(dup_asst_conflict)

    # 2. Child call with non-canonical key order / indentation in arguments_json completes and delivers cleanly,
    #    and real verification.record receipt (which has no call_id) is accepted by duplicate, select, and complete.
    env = PendingHostCallEnvelope(
        task_id="task_indented_parent",
        session_id="sess_indented_1",
        generation=1,
        execution_id="exec_indented_1",
        callback_id="cb_indented_child_1",
        ordinal=0,
        method="get_window",
        effect="read",
        target_tool="computer_observe_windows",
        arguments={"id": 7},
        arguments_sha256=canonical_arguments_sha256({"id": 7}),
    )
    child_call = store.transaction(
        lambda data: plan_child_call(data["tasks"]["task_indented_parent"], env, catalog, HOST_METHOD_MAPPING)
    )
    # Simulate non-canonical whitespace/key order preserved by normalize_tool_calls in durable queue & in_flight.
    non_canonical_child_json = '{\n  "id": 7,\n  "method": "get_window"\n}'
    non_canonical_child_sha = hashlib.sha256(non_canonical_child_json.encode("utf-8")).hexdigest()
    assert non_canonical_child_sha != hashlib.sha256(encoded(child_call["arguments"])).hexdigest()

    def _apply_non_canonical_raw(data: dict[str, Any]) -> dict[str, Any]:
        t = data["tasks"]["task_indented_parent"]
        t["queue"][0]["arguments_json"] = non_canonical_child_json
        t["queue"][0]["raw_arguments_json"] = non_canonical_child_json
        t["queue"][0]["arguments_sha256"] = non_canonical_child_sha
        t["in_flight"] = copy.deepcopy(t["queue"][0])
        return copy.deepcopy(t["queue"][0])

    non_canonical_call = store.transaction(_apply_non_canonical_raw)
    tok = store.transaction(
        lambda data: stage_child_completion(
            data["tasks"]["task_indented_parent"],
            non_canonical_call,
            receipt_status="confirmed",
            result={"window": {"app": "Notepad", "id": 7}},
        )
    )
    after_stage = store.read()["tasks"]["task_indented_parent"]
    assert len(after_stage["receipts"]) == 1
    assert "call_id" not in after_stage["receipts"][0]
    assert after_stage["receipts"][0]["arguments"] == {"id": 7, "method": "get_window"}

    # Exact idempotent duplicate stage_child_completion succeeds without calling verification.record again.
    tok_dup = store.transaction(
        lambda data: stage_child_completion(
            data["tasks"]["task_indented_parent"],
            non_canonical_call,
            receipt_status="confirmed",
            result={"window": {"app": "Notepad", "id": 7}},
        )
    )
    assert tok_dup == tok
    assert len(store.read()["tasks"]["task_indented_parent"]["receipts"]) == 1

    # 22-field DeliveryToken single-field conflict sweep before delivery.
    live_id = {
        "task_id": "task_indented_parent",
        "session_id": "sess_indented_1",
        "generation": 1,
        "execution_id": "exec_indented_1",
        "alive": True,
    }
    base_tok = tok.to_dict()
    all_22_field_mutations: list[tuple[str, Any]] = [
        ("task_id", "task_other"),
        ("outbox_id", "outbox_other"),
        ("parent_operation_id", "op_parent_other"),
        ("parent_call_id", "call_parent_other"),
        ("session_id", "sess_other"),
        ("generation", 99),
        ("execution_id", "exec_other"),
        ("callback_id", "cb_other"),
        ("ordinal", 9),
        ("method", "list_windows"),
        ("tool_name", "ryn_computer_act_window"),
        ("call_id", "jsc_other"),
        ("operation_id", "jsop_other"),
        ("arguments_sha256", "1" * 64),
        ("receipt_id", "receipt_other"),
        ("receipt_status", "failed"),
        ("outcome", "failed"),
        ("not_executed", True),
        ("result_ref", "ref_unexpected"),
        ("payload", {"window": {"app": "Notepad", "id": 999}}),
        ("payload_sha256", "2" * 64),
        ("recorded_at", "1999-01-01T00:00:00Z"),
    ]
    for field_name, bad_val in all_22_field_mutations:
        mutated_tok = DeliveryToken(**{**base_tok, field_name: bad_val})
        snap = store.read()["tasks"]["task_indented_parent"]
        with pytest.raises(AgentError):
            select_delivery(snap, mutated_tok, live_id)
        with pytest.raises(AgentError):
            store.transaction(
                lambda data, mt=mutated_tok: complete_delivery(data["tasks"]["task_indented_parent"], mt, "sent")
            )

    # Positive delivery and completion after 22-field sweep.
    port = FakeCoordinatorPort()
    snap = store.read()["tasks"]["task_indented_parent"]
    disp = deliver_committed_token(snap, tok, port, live_identity=live_id)
    assert disp == "sent"
    store.transaction(lambda data: complete_delivery(data["tasks"]["task_indented_parent"], tok, disp))
    assert store.read()["tasks"]["task_indented_parent"]["js_execution"]["phase"] == "running"

    # 3. Genuine router schema rejection (error_result is not None, arguments == {}, record_receipt=False).
    env_rej = PendingHostCallEnvelope(
        task_id="task_indented_parent",
        session_id="sess_indented_1",
        generation=1,
        execution_id="exec_indented_1",
        callback_id="cb_router_rej_2",
        ordinal=1,
        method="get_window",
        effect="read",
        target_tool="computer_observe_windows",
        arguments={"id": 7},
        arguments_sha256=canonical_arguments_sha256({"id": 7}),
    )
    rejected_call = store.transaction(
        lambda data: plan_child_call(
            data["tasks"]["task_indented_parent"],
            env_rej,
            catalog,
            HOST_METHOD_MAPPING,
            argument_translator=lambda method, _args: {"method": method, "id": "not_an_integer"},
        )
    )
    assert rejected_call.get("error_result") is not None
    assert rejected_call["arguments"] == {}

    # Attempting to complete a router-rejected call as confirmed fails closed.
    with pytest.raises(AgentError):
        store.transaction(
            lambda data: stage_child_completion(
                data["tasks"]["task_indented_parent"],
                rejected_call,
                receipt_status="confirmed",
                record_receipt=True,
            )
        )

    # Completing as not_executed with record_receipt=False succeeds and preserves known-not-executed semantics.
    rej_tok = store.transaction(
        lambda data: stage_child_completion(
            data["tasks"]["task_indented_parent"],
            rejected_call,
            receipt_status="not_executed",
            result=rejected_call["error_result"],
            record_receipt=False,
        )
    )
    assert rej_tok.not_executed is True
    assert rej_tok.outcome == "not_executed"
    assert rej_tok.receipt_id is None
    assert len(store.read()["tasks"]["task_indented_parent"]["receipts"]) == 1


def test_c1_c2_synchronized_queue_flight_tamper_and_result_ref_content_immutability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """C1 & C2 regression: synchronized queue+in_flight+call substitution is refused via frozen admitted tool_arguments_sha256, and mutated result_ref sha256/bytes are refused before delivery."""
    key = X25519PrivateKey.generate()
    store = AgentStore(tmp_path, key)
    _, catalog = _seed_task_row(store)

    env = PendingHostCallEnvelope(
        task_id="task_js_phase_a",
        session_id="sess_001",
        generation=1,
        execution_id="exec_001",
        callback_id="cb_c1_sync_1",
        ordinal=0,
        method="get_window",
        effect="read",
        target_tool="computer_observe_windows",
        arguments={"id": 7},
        arguments_sha256=canonical_arguments_sha256({"id": 7}),
    )
    admitted_call = store.transaction(
        lambda data: plan_child_call(data["tasks"]["task_js_phase_a"], env, catalog, HOST_METHOD_MAPPING)
    )
    admitted_entry = store.read()["tasks"]["task_js_phase_a"]["js_execution"]["admitted_callbacks"]["cb_c1_sync_1"]
    # Sky arguments hash and translated registered-tool canonical hash are distinct and both frozen at admission.
    assert admitted_entry["arguments_sha256"] == canonical_arguments_sha256({"id": 7})
    assert admitted_entry["tool_arguments_sha256"] == hashlib.sha256(
        encoded({"method": "get_window", "id": 7})
    ).hexdigest()
    assert admitted_entry["arguments_sha256"] != admitted_entry["tool_arguments_sha256"]
    assert admitted_entry["router_rejected"] is False

    # C1 attack: synchronously replace queue[0], in_flight, and completion call from id=7 to id=999 while leaving admitted_callbacks unchanged.
    record_calls: list[tuple[Any, ...]] = []
    real_record = verification.record

    def _spy_record(*args: Any, **kwargs: Any) -> Any:
        record_calls.append((args, kwargs))
        return real_record(*args, **kwargs)

    monkeypatch.setattr(verification, "record", _spy_record)

    forged_args = {"method": "get_window", "id": 999}
    forged_json = json.dumps(forged_args, sort_keys=True, separators=(",", ":"))
    forged_sha = hashlib.sha256(forged_json.encode("utf-8")).hexdigest()

    def _synchronized_tamper_stage(data: dict[str, Any]) -> DeliveryToken:
        t = data["tasks"]["task_js_phase_a"]
        forged_call = copy.deepcopy(admitted_call)
        forged_call["arguments"] = copy.deepcopy(forged_args)
        forged_call["arguments_json"] = forged_json
        forged_call["raw_arguments_json"] = forged_json
        forged_call["arguments_sha256"] = forged_sha
        t["queue"][0] = copy.deepcopy(forged_call)
        t["in_flight"] = copy.deepcopy(forged_call)
        return stage_child_completion(
            t,
            forged_call,
            receipt_status="confirmed",
            result={"window": {"app": "Notepad", "id": 999}},
        )

    with pytest.raises(AgentError, match="agent_js_callback_conflict") as exc_c1:
        store.transaction(_synchronized_tamper_stage)
    assert str(exc_c1.value) == "agent_js_callback_conflict"

    assert len(record_calls) == 0
    untouched = store.read()["tasks"]["task_js_phase_a"]
    assert untouched["receipts"] == []
    assert untouched["js_execution"]["delivery_outbox"] == []
    assert untouched["queue"][0]["arguments"] == {"method": "get_window", "id": 7}

    # C2: real results.save reference metadata is frozen in outbox and checked on select_delivery / complete_delivery.
    real_ref = results.save(
        store,
        untouched,
        admitted_call,
        {"window": {"app": "Notepad", "id": 7}},
    )
    tok = store.transaction(
        lambda data: stage_child_completion(
            data["tasks"]["task_js_phase_a"],
            admitted_call,
            receipt_status="confirmed",
            result={"window": {"app": "Notepad", "id": 7}},
            result_ref=real_ref,
        )
    )
    assert len(record_calls) == 1
    staged_task = store.read()["tasks"]["task_js_phase_a"]
    assert staged_task["js_execution"]["delivery_outbox"][0]["result_ref"] == real_ref["id"]
    assert staged_task["js_execution"]["delivery_outbox"][0]["result_ref_sha256"] == hashlib.sha256(
        encoded(real_ref)
    ).hexdigest()

    live_id = {
        "task_id": "task_js_phase_a",
        "session_id": "sess_001",
        "generation": 1,
        "execution_id": "exec_001",
        "alive": True,
    }

    # Mutating sha256 or bytes on row["result_refs"][0] for the same result_<32hex> id is refused before delivery.
    tampered_sha_task = copy.deepcopy(staged_task)
    tampered_sha_task["result_refs"][0]["sha256"] = "f" * 64
    with pytest.raises(AgentError, match="agent_js_outbox_conflict") as exc_sha:
        select_delivery(tampered_sha_task, tok, live_id)
    assert str(exc_sha.value) == "agent_js_outbox_conflict"

    tampered_bytes_task = copy.deepcopy(staged_task)
    tampered_bytes_task["result_refs"][0]["bytes"] = real_ref["bytes"] + 100
    with pytest.raises(AgentError, match="agent_js_outbox_conflict") as exc_bytes:
        select_delivery(tampered_bytes_task, tok, live_id)
    assert str(exc_bytes.value) == "agent_js_outbox_conflict"

    # Unmodified real result_ref selects and matches durable outbox and results.read cleanly.
    plan = select_delivery(staged_task, tok, live_id)
    assert plan.action == "resolve"
    assert plan.result == {"window": {"app": "Notepad", "id": 7}}
    assert staged_task["js_execution"]["delivery_outbox"][0]["result_ref"] == real_ref["id"]
    stored_val, stored_ref = results.read(store, staged_task, real_ref["id"])
    assert stored_val == {"window": {"app": "Notepad", "id": 7}}
    assert stored_ref == real_ref
