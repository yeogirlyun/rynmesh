"""S12-I22: factual tool contracts, schema bounds and real dispatch paths."""
from pathlib import Path

import pytest
from jsonschema import ValidationError, validate
from test_agent_local_tools import execute, local  # noqa: F401
from test_agent_runtime import advance, assistant
from tests.test_agent_automations import _build_test_env

from rynmesh.agent_runtime import automations, local_browser, memory_runtime
from rynmesh.agent_runtime.file_review import FileReview
from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.loop import ALL_INTERNAL_SPECS
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected, encoded
from rynmesh.agent_runtime.web_search import WebSearchEngine


def auto_spec():
    return automations.AUTOMATION_TOOL_SPECS[0]["function"]


def memory_spec():
    return next(s["function"] for s in memory_runtime.MEMORY_TOOL_SPECS
                if s["function"]["name"] == "ryn_memory_propose")


def test_T1_browser_description_tracks_actual_idle_timeout_and_profile():
    description = SPECS["ryn_browser_open"][0]
    assert "signed-out" in description
    assert "owner cookies" in description and "logins" in description
    assert f"{local_browser.BROWSER_IDLE_SECONDS / 60:g} minutes idle" in description
    assert "page_id" in description and "invalid" in description and "open again" in description


def test_T2_model_web_schema_excludes_disabled_and_explains_find_and_filtering():
    description, _, _, schema = SPECS["ryn_web_search"]
    assert schema["properties"]["mode"]["enum"] == ["live", "cached"]
    assert "find without open" in description and "filtered" in description
    assert "private" in description and "unresolvable" in description
    # S12-I8's search guidance survives the shared description change.
    assert "never when told not to" in description and "primary sources" in description
    with pytest.raises(ValidationError):
        validate({"query": "article", "mode": "disabled"}, schema)
    for mode in ("live", "cached"):
        validate({"find": "article", "mode": mode}, schema)


def test_internal_disabled_web_policy_still_blocks_without_fetch(tmp_path):
    calls = []
    engine = WebSearchEngine(tmp_path / "web", http_fetcher=lambda *args: calls.append(args))
    with pytest.raises(AgentToolRejected, match="agent_web_search_disabled"):
        engine.execute({"query": "article", "mode": "disabled"})
    assert calls == []


def test_T3_patch_description_matches_real_midway_failure(local, monkeypatch):
    description = SPECS["ryn_apply_patch"][0]
    assert "or nothing is written" not in description
    assert "Matching is checked" in description and "midway" in description
    assert "files" in description and "before retrying" in description
    tools, context, workspace, _ = local
    original = FileReview.replace
    def replace(review, step, expected, target, **kwargs):
        if Path(step["path"]).name == "b.txt":
            raise AgentToolRejected("agent_local_write_failed")
        return original(review, step, expected, target, **kwargs)
    monkeypatch.setattr(FileReview, "replace", replace)
    patch = "*** Begin Patch\n*** Add File: a.txt\n+first\n*** Add File: b.txt\n+second\n*** End Patch"
    with pytest.raises(AgentError) as failure:
        execute(tools, context, "ryn_apply_patch", {"patch": patch})
    assert not isinstance(failure.value, AgentToolRejected)
    assert (workspace / "a.txt").read_text() == "first\n"
    assert not (workspace / "b.txt").exists()


def test_T4_automation_schema_describes_real_fields_and_stays_under_existing_cap():
    spec = auto_spec()
    properties = spec["parameters"]["properties"]
    assert properties["notification_policy"]["enum"] == ["all_runs", "failed_runs_only", "never"]
    assert properties["weekdays"] == {"type": "array", "items": properties["weekday"], "maxItems": 7}
    assert properties["status"]["enum"] == ["ACTIVE", "PAUSED"]
    description = spec["description"].lower()
    for fact in ("local", "hh:mm", "heartbeat", "prompt", "this chat", "cron", "new chat", "repeat=once", "delay_minutes", "today", "next day", "delete", "firing"):
        assert fact in description
    # S06-I20 raised the shared internal-spec cap to 14240 (test_ovh_s10_i3).
    assert len(encoded(ALL_INTERNAL_SPECS)) <= 14240
    for bad in ({"action": "create", "weekdays": ["XX"]},
                {"action": "create", "weekdays": ["MO"] * 8},
                {"action": "create", "status": "UNKNOWN"}):
        with pytest.raises(ValidationError):
            validate(bad, spec["parameters"])


def test_automation_weekdays_and_status_reach_real_model_tool_dispatch(tmp_path):
    _, runtime, model, client = _build_test_env(tmp_path)
    try:
        model.responses = [assistant(name="ryn_automation_update", args={"action": "create", "name": "Test schedule",
            "prompt": "Inspect test data.", "kind": "cron", "repeat": "weekly", "time": "09:15",
            "weekdays": ["TU", "TH"], "status": "PAUSED", "notification_policy": "never", "desktop_access": False}),
            assistant("Created.")]
        task = runtime.create({"message": "Create the specified paused test schedule."})
        advance(runtime, 3)
        assert runtime.get(task["id"])["status"] == "completed"
        [item] = runtime.list_automations()["items"]
        assert item["weekdays"] == ["TU", "TH"] and item["status"] == "PAUSED"
        assert item["notification_policy"] == "never"
        assert "BYDAY=TU,TH" in item["rrule"]
        model.responses = [assistant(name="ryn_automation_update", args={"action": "update", "id": item["id"],
            "weekdays": ["WE", "FR"], "status": "PAUSED"}), assistant("Updated.")]
        task = runtime.create({"message": "Update the test schedule to Wednesday and Friday only."})
        advance(runtime, 3)
        assert runtime.get(task["id"])["status"] == "completed"
        [updated] = runtime.list_automations()["items"]
        assert updated["weekdays"] == ["WE", "FR"] and updated["status"] == "PAUSED"
    finally:
        client.close()


def test_once_delay_and_clock_time_have_one_occurrence():
    for arguments in ({"repeat": "once", "delay_minutes": 5}, {"repeat": "once", "time_str": "09:15"}):
        rule = automations.build_rrule(**arguments)
        assert "COUNT=1" in rule
        assert automations.format_schedule_labels(rule)["repeat"] == "once"


def test_T5_memory_description_limits_text_to_owner_words():
    description = memory_spec()["description"]
    assert "(in any language)" not in description
    assert "owner" in description.lower() and "language" in description
    assert "text must reuse" in description and "current message words" in description
    assert "do not translate, paraphrase or normalize" in description
    owner = "记住我的编辑器是VSCode"
    assert memory_runtime.verify_proposal_grounding_in_owner_message("我的编辑器是VSCode", owner)
    assert not memory_runtime.verify_proposal_grounding_in_owner_message("My editor is VSCode", owner)


@pytest.mark.parametrize("field,bad", [("operation", None), ("operation", True), ("operation", {}),
    ("operation", "invent"), ("scope_kind", None), ("scope_kind", False), ("scope_kind", "global")])
def test_compact_memory_enum_schemas_preserve_type_and_value_constraints(field, bad):
    arguments = {"operation": "remember", "scope_kind": "personal"}
    arguments[field] = bad
    with pytest.raises(ValidationError):
        validate(arguments, memory_spec()["parameters"])
