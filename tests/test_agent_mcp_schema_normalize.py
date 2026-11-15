from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

from rynmesh.agent_runtime.extensions.adapter import ExtensionToolAdapter
from rynmesh.agent_runtime.extensions.store import ExtensionStore, _canon_sha256
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.agent_runtime.tools import RynTools
from rynmesh.llm_package import chat as llm_chat
from rynmesh.llm_package.chat import validate_chat, validate_tool_schema


def _load_schema_normalize() -> ModuleType:
    return importlib.import_module("rynmesh.agent_runtime.extensions.schema_normalize")


def _utf8_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


class _StubLocalTools:
    store = None


class _StubSessionManager:
    oauth = None

    @staticmethod
    def refresh_tool_catalogs() -> list[object]:
        return []


def _make_store_and_adapter(tmp_path: Path) -> tuple[ExtensionStore, ExtensionToolAdapter]:
    store = ExtensionStore(tmp_path)
    adapter = ExtensionToolAdapter(
        _StubLocalTools(),
        lambda: None,
        extension_store=store,
        session_manager=_StubSessionManager(),
    )
    return store, adapter


def test_sanitize_handles_boolean_const_missing_type_unknown_type_and_bad_defs() -> None:
    schema_normalize = _load_schema_normalize()
    # Boolean schemas -> JSON Schema objects
    assert schema_normalize.sanitize(True) == {}
    false_sanitized = schema_normalize.sanitize(False)
    assert isinstance(false_sanitized, dict)
    assert false_sanitized == {"not": {}}

    # const -> enum + type clue from enum
    const_str = schema_normalize.sanitize({"const": "fixed_mode"})
    assert const_str["enum"] == ["fixed_mode"]
    assert const_str["type"] == "string"

    const_bool = schema_normalize.sanitize({"const": True})
    assert const_bool["enum"] == [True]
    assert const_bool["type"] == "boolean"

    # Missing type inferred from properties / items / enum clues
    inferred_obj = schema_normalize.sanitize(
        {
            "properties": {
                "tags": {"items": {"enum": ["alpha", "beta"]}},
                "flag": True,
                "blocked": False,
            }
        }
    )
    assert inferred_obj["type"] == "object"
    assert inferred_obj["properties"]["tags"]["type"] == "array"
    assert inferred_obj["properties"]["tags"]["items"]["type"] == "string"
    assert inferred_obj["properties"]["flag"] == {}
    assert inferred_obj["properties"]["blocked"] == {"not": {}}

    # object missing properties -> {}, array missing items -> {"type": "string"}
    assert schema_normalize.sanitize({"type": "object"}) == {
        "type": "object",
        "properties": {},
    }
    assert schema_normalize.sanitize({"type": "array"}) == {
        "type": "array",
        "items": {"type": "string"},
    }

    # Only 7 JSON Schema type names recognized; unknown type or unknown structure -> {}
    assert schema_normalize.sanitize({"type": "custom_datetime", "format": "iso"}) == {}
    assert schema_normalize.sanitize({"arbitraryKeyword": 42}) == {}
    assert schema_normalize.sanitize("not-a-schema") == {}

    # Non-object $defs / definitions deleted whole-table; external $ref stripped
    bad_defs_schema = {
        "type": "object",
        "properties": {
            "ok": {"type": "string"},
            "ext": {"$ref": "https://example.com/remote.json"},
        },
        "$defs": "corrupted_defs_table",
        "definitions": ["not", "an", "object"],
    }
    cleaned = schema_normalize.sanitize(bad_defs_schema)
    assert "$defs" not in cleaned
    assert "definitions" not in cleaned
    assert cleaned["properties"]["ext"] == {}
    validate_tool_schema(cleaned)


def test_prune_unreferenced_defs_and_four_stage_compaction_to_5000_bytes() -> None:
    schema_normalize = _load_schema_normalize()
    # Transitive reference kept (Root -> UsedA -> UsedB), UnusedC / UnusedLegacy pruned
    with_defs = {
        "type": "object",
        "properties": {
            "item": {"$ref": "#/$defs/UsedA"},
            "legacy": {"$ref": "#/definitions/UsedLegacy"},
        },
        "$defs": {
            "UsedA": {
                "type": "object",
                "properties": {"nested": {"$ref": "#/$defs/UsedB"}},
            },
            "UsedB": {"type": "string"},
            "UnusedC": {
                "type": "object",
                "properties": {"cycle": {"$ref": "#/$defs/UnusedC"}},
            },
        },
        "definitions": {
            "UsedLegacy": {"type": "integer"},
            "UnusedLegacy": {"type": "boolean"},
        },
    }
    pruned = schema_normalize.prune_unreferenced_defs(with_defs)
    assert set(pruned["$defs"].keys()) == {"UsedA", "UsedB"}
    assert set(pruned["definitions"].keys()) == {"UsedLegacy"}

    # Top-level type != "object" returns None
    assert schema_normalize.compact({"type": "string"}) is None
    assert schema_normalize.compact(False) is None
    assert schema_normalize.compact("invalid") is None

    # Stage 1: strips schema descriptions while keeping property named "description"
    stage1_input = {
        "type": "object",
        "description": "R" * 5200,
        "properties": {
            "description": {"type": "string", "description": "P" * 600},
        },
    }
    stage1_out = schema_normalize.compact(stage1_input, max_bytes=5000)
    assert stage1_out is not None
    assert "description" not in stage1_out
    assert "description" in stage1_out["properties"]
    assert "description" not in stage1_out["properties"]["description"]
    assert _utf8_size(stage1_out) <= 5000

    # Stage 2: replaces local $ref with {} and drops $defs/definitions
    stage2_input = {
        "type": "object",
        "properties": {
            "payload": {"$ref": "#/$defs/Heavy"},
        },
        "$defs": {
            "Heavy": {
                "type": "object",
                "properties": {f"field_{i:03d}": {"type": "string"} for i in range(180)},
            }
        },
    }
    assert _utf8_size(stage2_input) > 5000
    stage2_out = schema_normalize.compact(stage2_input, max_bytes=5000)
    assert stage2_out is not None
    assert stage2_out["properties"]["payload"] == {}
    assert "$defs" not in stage2_out
    assert _utf8_size(stage2_out) <= 5000

    # Stage 3 & Stage 4 on a > 20 KB schema: compresses to <= 5000 bytes and passes validate_tool_schema
    deep_leaf = {
        "type": "object",
        "properties": {
            f"deep_prop_{i:02d}": {
                "type": "object",
                "description": "D" * 120,
                "properties": {"val": {"type": "string", "enum": [f"opt_{j}" for j in range(15)]}},
            }
            for i in range(25)
        },
    }
    huge_20kb_schema = {
        "type": "object",
        "description": "Top level tool schema " + ("X" * 1500),
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "description": {"type": "string", "description": "User description field"},
            "filter_tree": {
                "type": "object",
                "properties": {
                    "level2": {
                        "type": "object",
                        "properties": {
                            f"branch_{k:02d}": copy.deepcopy(deep_leaf) for k in range(6)
                        },
                    }
                },
            },
            "union_mode": {
                "anyOf": [
                    {"type": "string", "enum": [f"mode_{m:03d}" for m in range(120)]},
                    {"type": "integer"},
                ]
            },
        },
        "$defs": {
            "DeadDef": {
                "type": "object",
                "properties": {f"unused_{u}": {"type": "string"} for u in range(60)},
            }
        },
    }
    normalized_pruned = schema_normalize.prune_unreferenced_defs(
        schema_normalize.sanitize(huge_20kb_schema)
    )
    assert _utf8_size(huge_20kb_schema) >= 20 * 1024
    compacted_20kb = schema_normalize.compact(normalized_pruned, max_bytes=5000)
    assert compacted_20kb is not None
    assert _utf8_size(compacted_20kb) <= 5000
    assert compacted_20kb["type"] == "object"
    # Depth-3 branches inside root(0)->filter_tree(1)->level2(2)->branch_XX(3) collapsed to {}
    assert compacted_20kb["properties"]["filter_tree"]["properties"]["level2"]["properties"]["branch_00"] == {}
    validate_tool_schema(compacted_20kb)

    # Stage 4 explicitly strips anyOf / oneOf / allOf when still above max_bytes after stage 3
    stage4_input = {
        "type": "object",
        "properties": {
            "mode": {
                "anyOf": [
                    {"type": "string", "enum": [f"long_variant_value_{i:03d}" for i in range(220)]},
                    {"type": "integer"},
                ]
            }
        },
    }
    assert _utf8_size(stage4_input) > 5000
    stage4_out = schema_normalize.compact(stage4_input, max_bytes=5000)
    assert stage4_out is not None
    assert "anyOf" not in stage4_out["properties"]["mode"]
    assert _utf8_size(stage4_out) <= 5000
    validate_tool_schema(stage4_out)


def test_bad_schema_tool_isolated_and_other_tools_work_in_chat_request(tmp_path: Path) -> None:
    store, adapter = _make_store_and_adapter(tmp_path)
    created = store.upsert_mcp_server(
        {
            "id": "third-party",
            "name": "Third Party MCP",
            "transport": "stdio",
            "command": "python",
            "args": ["-V"],
            "enabled": True,
        }
    )

    raw_quirky_and_deep = {
        "type": "object",
        "properties": {
            "query": {"const": "status"},
            "extra_meta": True,
            "external_hint": {"$ref": "https://schemas.example.com/hint.json"},
            "when": {"type": "custom_timestamp"},
            "envelope": {
                "type": "object",
                "properties": {
                    "inner": {
                        "type": "object",
                        "properties": {
                            "strict_leaf": {
                                "type": "object",
                                "properties": {
                                    "count": {"type": "integer"},
                                    "pad": {
                                        "type": "string",
                                        "description": "Z" * 21000,
                                    },
                                },
                                "required": ["count"],
                                "additionalProperties": False,
                            },
                            **{
                                f"deep_extra_{i:02d}": {
                                    "type": "object",
                                    "properties": {
                                        f"k_{j:02d}": {"type": "string"} for j in range(18)
                                    },
                                }
                                for i in range(20)
                            },
                        },
                    }
                },
            },
        },
        "required": ["query"],
        "$defs": "broken_defs",
    }
    raw_output_schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "string"}}},
    }

    public_srv = store.record_mcp_discovery(
        "third-party",
        expected_revision=created["revision"],
        discovered_tools=[
            {
                "name": "broken_top_string",
                "description": "Top-level type is string instead of object",
                "inputSchema": {"type": "string"},
            },
            {
                "name": "broken_top_bool",
                "description": "Top-level boolean false schema",
                "inputSchema": False,
            },
            {
                "name": "quirky_deep_tool",
                "description": "Has const, boolean schema, external ref, bad $defs, and >20KB deep object",
                "inputSchema": raw_quirky_and_deep,
                "outputSchema": raw_output_schema,
            },
            {
                "name": "clean_echo",
                "description": "Simple valid tool",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
        ],
        status="ready",
    )

    # Verify public view exposes schema_invalid for UI rendering
    public_by_name = {t["name"]: t for t in public_srv["tools"]}
    assert public_by_name["broken_top_string"]["schema_invalid"] is True
    assert public_by_name["broken_top_bool"]["schema_invalid"] is True
    assert public_by_name["quirky_deep_tool"]["schema_invalid"] is False
    assert public_by_name["clean_echo"]["schema_invalid"] is False

    # Verify internal persisted fields: raw input_schema, unchanged schema_sha256, model_schema, output_schema
    internal_srv = store.get_mcp_server_internal("third-party")
    stored_by_name = {t["name"]: t for t in internal_srv["discovered_tools"]}
    assert stored_by_name["broken_top_string"]["schema_invalid"] is True
    assert stored_by_name["broken_top_string"]["model_schema"] is None
    assert stored_by_name["broken_top_string"]["schema_sha256"] == _canon_sha256({"type": "string"})

    quirky_stored = stored_by_name["quirky_deep_tool"]
    assert quirky_stored["input_schema"] == raw_quirky_and_deep
    assert quirky_stored["schema_sha256"] == _canon_sha256(raw_quirky_and_deep)
    assert isinstance(quirky_stored["model_schema"], dict)
    assert _utf8_size(quirky_stored["model_schema"]) <= 5000
    assert quirky_stored["output_schema"] == raw_output_schema

    # _active_mcp_tools skips schema_invalid tools; specs emits model_schema and whole chat request succeeds
    active_orig_names = {tool["name"] for _srv, tool, _bind in adapter._active_mcp_tools().values()}
    assert active_orig_names == {"quirky_deep_tool", "clean_echo"}

    specs = adapter.specs()
    spec_by_name = {s["function"]["name"]: s for s in specs}
    quirky_model_name = quirky_stored["model_tool_name"]
    clean_model_name = stored_by_name["clean_echo"]["model_tool_name"]
    assert quirky_model_name in spec_by_name
    assert clean_model_name in spec_by_name
    assert stored_by_name["broken_top_string"]["model_tool_name"] not in spec_by_name
    assert stored_by_name["broken_top_bool"]["model_tool_name"] not in spec_by_name

    # Full validate_chat succeeds without failing on any tool schema
    validated_body = validate_chat(
        {
            "messages": [{"role": "user", "content": "check status"}],
            "tools": specs,
        }
    )
    assert len(validated_body["tools"]) == len(specs)

    # Legacy record missing model_schema computes once on the fly in specs(); corrupted model_schema is skipped
    raw_state = store._read()
    for item in raw_state["mcp_servers"]["third-party"]["discovered_tools"]:
        if item["name"] == "clean_echo":
            item.pop("model_schema", None)
    raw_state["mcp_servers"]["third-party"]["discovered_tools"].append(
        {
            "name": "corrupted_model_schema_tool",
            "title": "Corrupted",
            "description": "Injected bad model_schema",
            "input_schema": {"type": "object", "properties": {}},
            "model_schema": {"$ref": "https://evil.example/external.json"},
            "schema_sha256": _canon_sha256({"type": "object", "properties": {}}),
            "annotations": {},
            "model_tool_name": "mcp__third_party__corrupted__00000000",
        }
    )
    store._write(raw_state)
    adapter._active_mcp_tools_cache = None

    specs_after_legacy = adapter.specs()
    names_after_legacy = {s["function"]["name"] for s in specs_after_legacy}
    assert clean_model_name in names_after_legacy
    assert "mcp__third_party__corrupted__00000000" not in names_after_legacy

    # Step 4: adapter.validate still enforces raw input_schema even when model_schema compacted strict_leaf to {}
    assert (
        spec_by_name[quirky_model_name]["function"]["parameters"]["properties"]["envelope"][
            "properties"
        ]["inner"]["properties"]["strict_leaf"]
        == {}
    )
    # Valid deep argument passes
    adapter.validate(
        quirky_model_name,
        {"query": "status", "envelope": {"inner": {"strict_leaf": {"count": 3}}}},
    )
    # Invalid deep argument (count is string instead of integer) is rejected by raw input_schema!
    with pytest.raises(ValueError):
        adapter.validate(
            quirky_model_name,
            {"query": "status", "envelope": {"inner": {"strict_leaf": {"count": "not_int"}}}},
        )


def test_argument_validation_runs_once_with_unchanged_error_code(tmp_path: Path) -> None:
    store, adapter = _make_store_and_adapter(tmp_path)
    created = store.upsert_mcp_server(
        {
            "id": "calc",
            "name": "Calc Server",
            "transport": "stdio",
            "command": "python",
            "args": ["-V"],
            "enabled": True,
        }
    )
    store.record_mcp_discovery(
        "calc",
        expected_revision=created["revision"],
        discovered_tools=[
            {
                "name": "add_numbers",
                "description": "Add two integers",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "integer"},
                        "b": {"type": "integer"},
                    },
                    "required": ["a", "b"],
                    "additionalProperties": False,
                },
            }
        ],
        status="ready",
    )
    internal_srv = store.get_mcp_server_internal("calc")
    model_tool_name = internal_srv["discovered_tools"][0]["model_tool_name"]

    # Build a minimal RynTools wrapper delegating extension validation to adapter
    ryn_tools = RynTools.__new__(RynTools)
    ryn_tools.extensions = adapter
    ryn_tools.craft = None
    ryn_tools.diagnostics = None

    with (
        patch(
            "rynmesh.agent_runtime.extensions.adapter.validate_tool_arguments",
            wraps=llm_chat.validate_tool_arguments,
        ) as spy_validate_args,
        patch(
            "rynmesh.agent_runtime.extensions.adapter.jsonschema_validate",
            side_effect=AssertionError("duplicate jsonschema_validate must not be called"),
        ) as spy_legacy_jsonschema,
    ):
        # Valid call executes validate_tool_arguments once and never calls jsonschema_validate
        ryn_tools._validate(model_tool_name, {"a": 10, "b": 20})
        assert spy_validate_args.call_count == 1
        assert spy_legacy_jsonschema.call_count == 0

        # Invalid call executes validate_tool_arguments once and raises agent_tool_arguments_invalid
        with pytest.raises(AgentToolRejected) as exc_info:
            ryn_tools._validate(model_tool_name, {"a": "wrong_type", "b": 20})
        assert str(exc_info.value) == "agent_tool_arguments_invalid"
        assert exc_info.value.details is not None
        assert exc_info.value.details["field_path"] == "a"
        assert spy_validate_args.call_count == 2
        assert spy_legacy_jsonschema.call_count == 0
