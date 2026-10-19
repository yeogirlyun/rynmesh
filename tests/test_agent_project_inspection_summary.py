"""Composite project observations remain compactable without changing archived evidence."""
import copy
import json

import pytest

from rynmesh.agent_runtime import projects
from rynmesh.agent_runtime.loop import AgentLoop
from rynmesh.agent_runtime.store import encoded


def exchange(name, result):
    return [{"role": "user", "content": "Inspect the project; preserve my existing changes."},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "observe", "type": "function", "function": {"name": name, "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "observe", "content": encoded(result).decode()}]


def large_snapshot():
    snapshot = {"name": "project", "path": "D:/workspace/project", "entry_snapshot_version": 1,
                "snapshot_depth": 1, "observed_at": 1.0, "snapshot_incomplete": True,
                "root_listing": {"path": ".", "entries": [
                    {"name": f"file{i:02}" + "x" * 75, "directory": False, "symlink": False} for i in range(20)],
                    "incomplete": True, "next_offset": 20,
                    "continuation": {"tool": "ryn_file_list", "arguments": {"path": ".", "offset": 20}}},
                "directory_listings": [
                    {"path": f"package{i}", "entries": [
                        {"name": f"module{j:02}" + "x" * 30, "directory": False, "symlink": False} for j in range(12)],
                        "incomplete": True, "continuation": {"tool": "ryn_file_list", "arguments": {"path": f"package{i}", "offset": 12}}}
                    for i in range(3)],
                "directory_listings_incomplete": False, "directory_listings_continuation": None,
                "entry_documents": [{"path": "AGENTS.md", "status": "absent", "incomplete": False, "continuation": None}],
                "git": {"branch": "main", "changes": [" M source.py"], "changes_incomplete": False}}
    used = len(encoded(snapshot["entry_documents"][0]))
    for name in projects.ENTRY_FILES[1:]:
        text = ("Guidance " + "x" * 70 + "\n") * 80
        page = projects._inspect_page({"text": text, "sha256": "a" * 64, "bytes": len(text),
                                       "start_line": 1, "end_line": 80, "total_lines": 80,
                                       "next_start_line": None, "next_offset": None},
                                      name, max(400, min(3000, 8000 - used)))
        snapshot["entry_documents"].append(page)
        used += len(encoded(page))
    projects._inspect_fit(snapshot)
    return snapshot


def test_12k_composite_observation_reaches_real_compaction_admission_without_model_calls():
    class SummaryAdmitted(Exception):
        pass

    class MemoryStore:
        def transaction(self, _mutate):
            # Stop before changing durable state or dispatching any model.
            raise SummaryAdmitted

    class Context(AgentLoop):
        store = MemoryStore()

        def _validate_context(self, _row):
            pass

        def _history(self, row):
            return row["messages"]

        def _context_limit(self, _row):
            return 12000

        def _context(self, row):
            return [{"role": "system", "content": "Host policy. " * 400}, row["messages"][0]]

        def _specs(self, _row):
            return []

        def _complete(self, *_args, **_kwargs):
            pytest.fail("This test must never dispatch inference")

    snapshot = large_snapshot()
    messages = exchange("ryn_project_inspect", snapshot)
    original = copy.deepcopy(messages)
    assert 12000 < len(encoded(snapshot)) < 20000  # No large-result reference hides the regression.
    selected = {"messages": messages, "project": {"path": "D:/workspace/project"},
                "limits": {"max_output_chars": 20000}}
    with pytest.raises(SummaryAdmitted):
        Context()._compact(selected, lambda: False, 0)
    assert messages == original


def test_inspection_summary_bounds_unicode_and_layout_and_preserves_page_provenance():
    snapshot = large_snapshot()
    snapshot["root_listing"]["entries"] *= 3
    for item in snapshot["root_listing"]["entries"]:
        item["name"] = "测" * 200 + '\\"' * 20
    for page in snapshot["entry_documents"]:
        if page.get("text"):
            page["text"] = ("测\\\"\n" * 2000)
    original = copy.deepcopy(snapshot)
    messages = exchange("ryn_project_inspect", snapshot)
    summary = json.loads(projects.summary_history(messages)[-1]["content"])
    assert len(encoded(summary)) <= 4000 and summary["summary_excerpt_only"] is True
    assert "original observation" in summary["summary_note"]
    for before, after in zip(original["entry_documents"], summary["entry_documents"], strict=True):
        assert {key: after[key] for key in before if key != "text"} == {key: value for key, value in before.items() if key != "text"}
        if before.get("text"):
            assert after["summary_text_omitted"] is True
    assert snapshot == original and sum(len(page.get("text", "").encode()) for page in summary["entry_documents"]) <= 1000


def test_structure_only_inspection_is_bounded_and_unavailable_git_stays_unknown():
    snapshot = large_snapshot()
    snapshot["entry_documents"] = []
    snapshot["git"] = {"branch": "unavailable", "changes": "unavailable"}
    snapshot["root_listing"]["entries"] *= 5
    messages = exchange("ryn_project_inspect", snapshot)
    summary = json.loads(projects.summary_history(messages)[-1]["content"])
    assert len(encoded(summary)) <= 4000
    assert summary.get("summary_git_omitted") or summary["git"]["changes_status"] == "unavailable"
    assert summary["summary_layout_omitted"] is True


def test_small_inspections_other_tools_and_owner_text_keep_their_existing_semantics():
    small = {"entry_documents": [{"path": "AGENTS.md", "status": "absent", "incomplete": False}]}
    small_messages = exchange("ryn_project_inspect", small)
    assert projects.summary_history(small_messages) == small_messages
    resource = exchange("ryn_read_resource", {"text": "business evidence" * 1000, "cursor": "opaque"})
    assert projects.summary_history(resource) == resource
    file_read = exchange("ryn_file_read", {"text": "业务" * 1000, "sha256": "b" * 64})
    projected = projects.summary_history(file_read)
    result = json.loads(projected[-1]["content"])
    assert len(result["text"].encode()) <= 2000 and result["summary_excerpt_only"] is True
    assert result["sha256"] == "b" * 64 and projected[0] == file_read[0]
    assert "summary_excerpt_only" not in file_read[-1]["content"]
