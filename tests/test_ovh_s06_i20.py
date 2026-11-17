"""S06-I20: bounded cached result pages and literal/numbered line reads."""
import copy
import json

import pytest
from jsonschema import ValidationError, validate
from test_agent_runtime import advance, assistant, make_runtime
from rynmesh.agent_runtime import prompt, results
from rynmesh.agent_runtime.store import AgentError, encoded


def _result(tmp_path, value):
    runtime, model, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "Read the stored result"})
    row = runtime.store.read()["tasks"][task["id"]]
    ref = results.seal(runtime.store, row, {"name": "inspect_service", "operation_id": "result-op"}, value)
    def register(data):
        data["tasks"][task["id"]].setdefault("result_refs", []).append(ref)
        data["tasks"][task["id"]]["limits"]["max_output_chars"] = 20000
    runtime.store.transaction(register)
    return runtime, model, runtime.store.read()["tasks"][task["id"]], ref


def _tool(runtime, model, row, ref, **args):
    model.responses = [assistant(name="ryn_read_result", args={"result_id": ref["id"], **args})]
    advance(runtime, 2)
    saved = runtime.store.read()["tasks"][row["id"]]
    return next(e["result"] for e in reversed(saved["events"]) if e["type"] == "tool_result")


def test_t1_half_megabyte_real_tool_pages_are_large_and_byte_bounded(tmp_path, monkeypatch):
    runtime, model, row, ref = _result(tmp_path, {"text": "a" * (500 * 1024)})
    original_read, legacy_reads = results.read, []
    def counted_read(*args, **kwargs):
        legacy_reads.append(1)
        return original_read(*args, **kwargs)
    monkeypatch.setattr(results, "read", counted_read)
    out = _tool(runtime, model, row, ref)
    assert not legacy_reads  # The dispatcher must not decode the whole result a second time.
    assert len(out["content"]) >= 12000
    assert len(encoded(out)) <= 20000 - 512
    assert out["next_offset"] == len(out["content"])
    assert "reference" not in out


@pytest.mark.parametrize("body", ["中文" * 100000, ('"\\\n\t' * 50000)], ids=["cjk", "escaped"])
def test_t2_encoded_page_shrinks_for_multibyte_and_escaped_content(tmp_path, body):
    runtime, _, row, ref = _result(tmp_path, {"text": body})
    out, returned_ref = results.page(runtime.store, row, ref["id"], limit=65536, max_bytes=19488, provider=True, return_reference=True)
    assert 0 < len(out["content"]) < 12000
    assert len(encoded(out)) <= 19488
    assert out["content"] == encoded({"text": body}).decode()[:out["next_offset"]]
    assert returned_ref == ref


def test_t3_large_requested_limit_is_capped_in_real_small_output_budget(tmp_path):
    runtime, model, row, ref = _result(tmp_path, {"text": "x" * 500000})
    runtime.store.transaction(lambda data: data["tasks"][row["id"]]["limits"].update(max_output_chars=4000))
    out = _tool(runtime, model, row, ref, limit=65536)
    assert 1000 <= len(out["content"]) < 4000
    assert len(encoded(out)) <= 4000 - 512


def test_t4_literal_pattern_caps_hits_and_keeps_numbered_context_in_real_tool(tmp_path):
    body = "\n".join(f"line {i}: " + ("error" if i % 4 == 2 else "normal") for i in range(1, 401))
    runtime, model, row, ref = _result(tmp_path, {"output": body})
    out = _tool(runtime, model, row, ref, pattern="error", context_lines=1)
    matched = [item for item in out["lines"] if item["match"]]
    assert len(matched) == 40
    assert matched[0] == {"line": 2, "text": "line 2: error", "match": True}
    assert out["lines"][0] == {"line": 1, "text": "line 1: normal", "match": False}
    assert out["total_lines"] == 400 and out["next_line"] == 160
    assert len(encoded(out)) <= 19488
    assert "reference" not in out


def test_pattern_is_literal_not_a_regex_and_context_is_deduplicated(tmp_path):
    runtime, _, row, ref = _result(tmp_path, "before\nerror[.*]\nerror[.*]\nafter\nlast")
    out, returned_ref = results.lines(runtime.store, row, ref["id"], pattern="error[.*]", context_lines=1, max_bytes=19488, provider=True, return_reference=True)
    assert [item["line"] for item in out["lines"]] == [1, 2, 3, 4]
    assert sum(item["match"] for item in out["lines"]) == 2
    assert out["next_line"] is None and returned_ref == ref


def test_t5_line_range_reads_exact_requested_lines_in_real_tool(tmp_path):
    body = "\n".join(f"line-{i}" for i in range(1, 2001))
    runtime, model, row, ref = _result(tmp_path, {"text": body})
    out = _tool(runtime, model, row, ref, line_start=1000, line_count=50)
    assert out["lines"] == [{"line": i, "text": f"line-{i}"} for i in range(1000, 1050)]
    assert out["total_lines"] == 2000 and out["next_line"] == 1050


def test_t6_ten_pages_decrypt_once_and_purge_evicts_task_cache(tmp_path, monkeypatch):
    runtime, _, row, ref = _result(tmp_path, {"text": "x" * 500000})
    original, calls = results.peer_box.open_sealed, []
    def counted(*args, **kwargs):
        if kwargs.get("info") == results.CHANNEL:
            calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(results.peer_box, "open_sealed", counted)
    offset = 0
    for _ in range(10):
        out = results.page(runtime.store, row, ref["id"], offset, 16384, max_bytes=19488, provider=True)
        offset = out["next_offset"]
    assert len(calls) == 1
    assert (row["id"], ref["id"], ref["sha256"]) in results._TEXT_CACHE
    results.purge(runtime.store, row["id"])
    assert not any(key[0] == row["id"] for key in results._TEXT_CACHE)


def test_t7_cache_hit_still_checks_provider_epoch_expiration_and_task(tmp_path):
    runtime, _, row, ref = _result(tmp_path, {"text": "private body"})
    results.page(runtime.store, row, ref["id"], max_bytes=19488, provider=True)
    changed = {**row, "context_epoch": row.get("context_epoch", 0) + 1}
    with pytest.raises(AgentError, match="agent_result_scope_changed"):
        results.page(runtime.store, changed, ref["id"], max_bytes=19488, provider=True)
    assert "private body" in results.read(runtime.store, changed, ref["id"])[0]["text"]
    other = {**row, "id": "other-task", "result_refs": []}
    with pytest.raises(AgentError, match="agent_result_not_found"):
        results.page(runtime.store, other, ref["id"], max_bytes=19488, provider=True)
    expired = copy.deepcopy(row)
    expired["result_refs"][0]["expired"] = True
    with pytest.raises(AgentError, match="agent_result_expired"):
        results.page(runtime.store, expired, ref["id"], max_bytes=19488, provider=True)


def test_t8_mutually_exclusive_modes_rejected_by_real_tool_handler(tmp_path):
    runtime, model, row, ref = _result(tmp_path, {"text": "error\nnext"})
    out = _tool(runtime, model, row, ref, pattern="error", line_start=1)
    schema = next(s["function"]["parameters"] for s in prompt.INTERNAL_SPECS if s["function"]["name"] == "ryn_read_result")
    validate({"result_id": ref["id"], "pattern": "error"}, schema)
    validate({"result_id": ref["id"], "line_start": 1, "line_count": 400}, schema)
    # Cross-field exclusivity stays in the handler: strict transport schemas do not support `not`.
    validate({"result_id": ref["id"], "pattern": "error", "line_start": 1}, schema)
    with pytest.raises(AgentError, match="agent_invalid_cursor"):
        results.lines(runtime.store, row, ref["id"], pattern="error", line_start=1)
    for invalid in ({"limit": 999}, {"limit": 65537},
                    {"pattern": "x" * 201}, {"context_lines": 6}, {"line_start": 0}, {"line_count": 401}):
        with pytest.raises(ValidationError):
            validate({"result_id": ref["id"], **invalid}, schema)
    assert out["error"] == "agent_invalid_cursor"
    assert out["no_mutation"] is True


def test_cache_is_lru_bounded_and_independent_of_read_value_mutation(tmp_path):
    runtime, _, row, first = _result(tmp_path, {"text": "body-0"})
    references = [first]
    for i in range(1, 5):
        ref = results.seal(runtime.store, row, {"name": "inspect_service", "operation_id": f"op-{i}"}, {"text": f"body-{i}"})
        row["result_refs"].append(ref)
        references.append(ref)
    for ref in references[:4]:
        results.read(runtime.store, row, ref["id"])
    results.read(runtime.store, row, first["id"])[0]["text"] = "not immutable"
    results.read(runtime.store, row, references[4]["id"])
    assert (row["id"], references[1]["id"], references[1]["sha256"]) not in results._TEXT_CACHE
    assert results.read(runtime.store, row, first["id"])[0] == {"text": "body-0"}
    assert len(results._TEXT_CACHE) <= 4
    assert sum(len(text) for text in results._TEXT_CACHE.values()) <= 8 * 1024 * 1024


def test_line_output_is_byte_bounded_even_for_one_long_unicode_line(tmp_path):
    runtime, _, row, ref = _result(tmp_path, {"text": "文" * 50000 + "\nnext"})
    out = results.lines(runtime.store, row, ref["id"], line_start=1, line_count=2, max_bytes=2000, provider=True)
    assert len(encoded(out)) <= 2000
    assert out["lines"][0]["line"] == 1 and out["lines"][0]["truncated"] is True
    assert out["next_line"] == 2


def test_read_result_strict_parameters_and_whole_response_schema_keep_constraints():
    from rynmesh.llm_package import codex_tools

    tool = next(s["function"] for s in prompt.INTERNAL_SPECS if s["function"]["name"] == "ryn_read_result")
    converted = codex_tools._strict_parameters(tool["parameters"])
    assert converted["additionalProperties"] is False
    assert set(converted["required"]) == set(tool["parameters"]["properties"])
    assert converted["properties"]["limit"]["anyOf"][0]["maximum"] == 65536
    response = codex_tools._response_schema({tool["name"]: tool}, strict=True)
    variant = response["properties"]["tool_calls"]["items"]["anyOf"][0]
    assert variant["properties"]["arguments"] == converted
    assert variant["properties"]["name"]["enum"] == ["ryn_read_result"]
