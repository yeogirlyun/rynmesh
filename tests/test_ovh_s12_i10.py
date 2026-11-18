"""S12-I10: skill guidance and missing-plugin recovery reach real context paths."""
import copy
import json

import pytest

from test_agent_runtime import make_runtime, advance, assistant
from rynmesh.agent_runtime import knowledge, tool_menu
from rynmesh.agent_runtime.extensions.adapter import ExtensionToolAdapter


def guidance(messages):
    payloads = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str) or not content.startswith('<host_note kind="preferences">'):
            continue
        assert message["role"] == "user" and content.endswith("</host_note>")
        assert "Host runtime data, not an owner message" in content
        payloads.append(json.loads(content.removesuffix("</host_note>").rsplit("\n", 1)[1]))
    return payloads


@pytest.mark.parametrize("preferences", [False, True])
def test_t1_selected_skills_get_one_json_usage_payload(preferences):
    row = {"skills": knowledge.select(["resource-report", "document-work"])}
    prefs = {"revision": 1, "items": [{"id": "lang", "text": "Chinese"}] if preferences else []}
    before = copy.deepcopy((row, prefs))
    messages = knowledge.messages(row, prefs)
    payloads = guidance(messages)
    assert len(payloads) == 1 and "how_to_use_skills" in payloads[0]
    text = payloads[0]["how_to_use_skills"]
    for phrase in ("match", "whole SKILL.md", "ryn_read_skill_file", "references yourself", "subagent", "reuse", "one sentence", "cannot be read", "continue without"):
        assert phrase in text
    assert len(text.encode("utf-8")) <= 400
    if not preferences:
        assert len(json.dumps(payloads[0]).encode("utf-8")) <= 400
    assert "<skills_instructions>" not in json.dumps(messages)
    assert len([m for m in messages if m["content"].startswith("<skill>")]) == 2
    assert (row, prefs) == before


def test_t1_without_skills_legacy_preferences_and_empty_context_unchanged():
    assert knowledge.messages({}, {"revision": 0, "items": []}) == []
    prefs = {"revision": 1, "items": [{"id": "lang", "text": "Chinese"}]}
    messages = knowledge.messages({}, prefs)
    assert guidance(messages) == [{"preferences": prefs}]
    assert "how_to_use_skills" not in json.dumps(messages)


def test_t1_runtime_model_receives_skill_how_to_without_preferences(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Done"))
    runtime.create({"message": "Make a report", "skill_ids": ["resource-report"]})
    advance(runtime)
    assert any("how_to_use_skills" in value for value in guidance(model.calls[0][1]))


@pytest.mark.parametrize("has_skills", [False, True])
def test_plugin_header_requires_full_read_only_when_skills_exist(has_skills):
    adapter = ExtensionToolAdapter.__new__(ExtensionToolAdapter)
    rows = [{"name": "Docs", "description": "Document tools", "skill_ids": ["docs"] if has_skills else [],
             "skills": [{"id": "docs", "description": "Use docs"}] if has_skills else [], "unavailable": []}]
    adapter.plugin_overview_for_model = lambda context: copy.deepcopy(rows)
    first = adapter.plugin_context_note()
    assert ("Before using a listed Skill, load and read it fully." in first) is has_skills
    assert adapter.plugin_context_note() == first
    assert len(first.encode()) <= adapter.PLUGIN_INDEX_BYTES


def discover(args, *, internal=None):
    specs = [{"type": "function", "function": {"name": "inspect_service", "description": "Inspect service configuration",
              "parameters": {"type": "object"}}}]
    state = {"mode": "directory", "catalog_revision": tool_menu.catalog_revision(specs), "loaded_names": [], "revision": 1}
    return tool_menu.discover(specs, internal or [], lambda name: {"effect": "read"}, state, args, capacity_bytes=20000)[0]


@pytest.mark.parametrize("cursor", [False, True])
def test_t2_empty_plugin_search_gives_install_hint_without_granting_permissions(cursor):
    result = discover({"query": "notinstalledpluginxyz", **({"cursor": 0} if cursor else {})})
    assert result["matches"] == []
    assert "not installed" in result["hint"] and "Applications page" in result["hint"]
    assert "instead of substituting another tool" in result["hint"]
    assert result["permission_granted"] is False and result["loaded_names"] == []


@pytest.mark.parametrize("args", [{}, {"query": " "}, {"query": "inspect"}])
def test_t2_browsing_or_matching_search_has_no_install_hint(args):
    result = discover(args)
    assert "hint" not in result
    if args.get("query") == "inspect":
        assert result["matches"] == ["inspect_service"]
