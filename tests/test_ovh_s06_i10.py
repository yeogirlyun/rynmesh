"""S06-I10 visible result loss across direct, batch and real Node child paths."""
import copy
import json
import re
from datetime import datetime
from decimal import Decimal

import pytest

from test_agent_runtime import assistant, make_runtime
from test_agent_tool_dispatch import BatchTools, raw, run_until
from rynmesh.agent_runtime.protocol import sanitized
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected
from rynmesh.agent_runtime.tools import bounded_result


def nested(depth=25):
    value = {"leaf": "value"}
    for _ in range(depth):
        value = {"nested": value}
    return {"keep": 1, "deep": value}


def test_t1_head_tail_preview_preserves_terminal_error():
    value = {f"part_{i}": "x" * 2000 for i in range(16)}
    value["error"] = "x"
    result = bounded_result(value)
    assert result["truncated"] is True
    assert '"error": "x"' in result["preview"]
    assert len(result["preview"]) <= 6000
    omitted = re.search(r"\[(\d+) chars truncated\]", result["preview"])
    assert omitted and result["omitted_characters"] == int(omitted[1]) > 0


@pytest.mark.parametrize("strict", [False, True])
def test_t1_truncated_strings_are_counted_in_both_modes(strict):
    result = bounded_result({"text": "x" * 3000}, strict=strict)
    assert result["text"] == "x" * 2048 + "…[+952 chars]"
    assert result["bounded_truncation"] == {"strings": 1, "lists": 0, "objects": 0, "depth": 0}


def test_t2_list_object_and_depth_loss_is_visible():
    result = bounded_result({"rows": list(range(150)), "object": {str(i): i for i in range(150)}, **nested()})
    assert len(result["rows"]) == len(result["object"]) == 100
    assert result["bounded_truncation"]["lists"] == 1
    assert result["bounded_truncation"]["objects"] == 1
    assert result["bounded_truncation"]["depth"] == 1
    assert "[bounded]" in json.dumps(result)


def test_t3_opaque_refs_have_no_suffix_and_strict_overflow_still_rejects():
    reference = "opaque-" + "x" * 2993
    result = bounded_result({"resource_ref": reference, "text": "x" * 3000}, opaque_keys=("resource_ref",), strict=True)
    assert result["resource_ref"] == reference
    assert result["bounded_truncation"]["strings"] == 1
    with pytest.raises(AgentToolRejected, match="agent_resource_output_limit"):
        bounded_result({str(i): "x" * 2000 for i in range(20)}, strict=True)


def test_t3_depth_marks_are_opt_in_and_other_fields_survive():
    with pytest.raises(AgentError, match="agent_output_limit"):
        sanitized(nested())
    marks = []
    result = sanitized(nested(), on_depth="mark", marks=marks)
    assert result["keep"] == 1
    assert "[depth limit: nested value omitted]" in json.dumps(result)
    assert marks == ["depth_limit"]


def test_t4_tuples_are_json_lists():
    assert sanitized({"items": (1, (2, 3))}) == {"items": [1, [2, 3]]}


@pytest.mark.parametrize("value, kind", [(b"PRIVATE_BINARY", "bytes"), (Decimal("1.5"), "Decimal"),
                                       (datetime(2026, 1, 1), "datetime"), ({1, 2}, "set")])
def test_t5_t6_unsupported_values_mark_only_at_explicit_boundary(value, kind):
    with pytest.raises(AgentError, match="agent_invalid_output"):
        sanitized({"a": value})
    marks = []
    result = sanitized({"a": value, "b": Decimal("1.5"), "c": 1, "password": b"SECRET"},
                       on_unsupported="mark", marks=marks)
    assert result == {"a": f"[unsupported:{kind}]", "b": "[unsupported:Decimal]", "c": 1, "password": "[redacted]"}
    assert set(marks) == {"unsupported_type"}
    assert "PRIVATE_BINARY" not in json.dumps(result)


@pytest.mark.parametrize("route", ["single", "batch", "code", "standalone"])
@pytest.mark.parametrize("kind", ["depth", "bytes"])
def test_t4_t7_partial_tool_results_reach_model_and_receipts(tmp_path, route, kind):
    runtime, model, _, _ = make_runtime(tmp_path)
    tools = BatchTools()
    runtime.tools = tools
    value = nested() if kind == "depth" else {"safe": 1, "binary": b"PRIVATE_BINARY", "decimal": Decimal("1.5")}
    original = tools.execute
    def execute(name, arguments, **options):
        return original(name, arguments, **options) if name == "ryn_code_exec" else copy.deepcopy(value)
    tools.execute = execute
    try:
        code = {"code": 'text(await tools.ryn_file_read({path: "a"}));', "yield_time_ms": 60000}
        if route == "standalone":
            result = tools.runner.execute(code, operation_id="standalone", cancelled=lambda: False,
                                          context={"task_id": "standalone"})
            serialized = json.dumps(result)
        else:
            calls = [raw(0)] if route == "single" else [raw(0), raw(1)]
            first = assistant(name="ryn_code_exec", args=code) if route == "code" else {"message": {"content": "", "tool_calls": calls}}
            model.responses = [first, assistant("Done")]
            row = runtime.create({"message": "Read the result"})
            done = run_until(runtime, row["id"], {"completed", "failed"})
            assert done["status"] == "completed", done.get("error")
            tool_messages = [m for m in model.calls[-1][1] if m["role"] == "tool"]
            serialized = json.dumps(tool_messages)
            receipts = runtime.store.read()["tasks"][row["id"]]["receipts"]
            assert all(r["status"] == "confirmed" for r in receipts)
            assert "sanitized_partial" in json.dumps(receipts)
        assert "sanitized_partial" in serialized
        assert ("depth_limit" if kind == "depth" else "unsupported_type") in serialized
        assert "agent_invalid_output" not in serialized and "agent_output_limit" not in serialized
        assert "PRIVATE_BINARY" not in serialized
    finally:
        tools.runner.close()
