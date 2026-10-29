"""The project entry tool combines fresh bounded observations without bypassing file guards."""
import hashlib
import json
import os
from types import SimpleNamespace

import pytest
from test_agent_local_tools import local as local

from rynmesh.agent_runtime import projects
from rynmesh.agent_runtime.store import AgentToolRejected, encoded


def observe(local, cancelled=lambda: False):  # noqa: F811 - imported pytest fixture parameter
    tools, context, _, _ = local
    return tools.perform("ryn_project_inspect", {}, context, cancelled)


def test_entry_snapshot_includes_structure_existing_guidance_and_git_without_arbitrary_reads(local, monkeypatch):  # noqa: F811 - imported pytest fixture
    tools, _, root, _ = local
    (root / "engine").mkdir()
    (root / "engine" / "implementation.py").write_text("PRIVATE SOURCE BODY", encoding="utf-8")
    (root / "engine" / "AGENTS.md").write_text("NESTED GUIDANCE BODY", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".env").write_text("PRIVATE CONFIG BODY", encoding="utf-8")
    (root / "README.md").write_bytes(b"Entry description.\n")
    (root / "AI_HANDOFF.md").write_text("Required handoff.\n", encoding="utf-8")
    monkeypatch.setattr(projects.subprocess, "run", lambda arguments, **kwargs: SimpleNamespace(
        returncode=0, stdout="feature\n" if "branch" in arguments else " M engine/implementation.py\n"))
    calls = []
    original = tools.perform

    def perform(name, args, *rest, **kwargs):
        if name == "ryn_file_read":
            calls.append(args["path"])
        return original(name, args, *rest, **kwargs)

    monkeypatch.setattr(tools, "perform", perform)
    result = observe(local)
    documents = {item["path"]: item for item in result["entry_documents"]}
    assert result["git"]["branch"] == "feature" and result["git"]["changes"] == [" M engine/implementation.py"]
    assert result["entry_files"] == ["README.md", "AI_HANDOFF.md"]
    assert documents["AGENTS.md"]["status"] == "absent"
    assert documents["README.md"]["text"] == "Entry description.\n"
    assert documents["README.md"]["incomplete"] is False and documents["README.md"]["continuation"] is None
    assert calls == ["README.md", "AI_HANDOFF.md"]
    assert {item["name"] for item in result["root_listing"]["entries"]} >= {"engine", "README.md"}
    engine = next(item for item in result["directory_listings"] if item["path"] == "engine")
    assert {item["name"] for item in engine["entries"]} == {"implementation.py", "AGENTS.md"}
    serialized = json.dumps(result)
    assert all(text not in serialized for text in ("PRIVATE SOURCE BODY", "PRIVATE CONFIG BODY", "NESTED GUIDANCE BODY"))
    assert len(encoded(result)) <= projects.INSPECT_BYTES


def test_snapshot_pages_end_on_lines_and_continuation_returns_exact_remainder(local):  # noqa: F811 - imported pytest fixture
    tools, context, root, _ = local
    body = "".join(f"line {number}: {'测' * 35} \\\"\n" for number in range(100))
    (root / "README.md").write_bytes(body.encode())
    page = next(item for item in observe(local)["entry_documents"] if item["path"] == "README.md")
    assert page["text"] and page["text"].endswith("\n") and body.startswith(page["text"])
    assert page["start_line"] == 1 and page["end_line"] == len(page["text"].splitlines())
    assert page["sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert page["incomplete"] is True and page["next_start_line"] == page["end_line"] + 1
    continuation = page["continuation"]
    remaining = tools.perform(continuation["tool"], continuation["arguments"], context, lambda: False)
    assert page["text"] + remaining["text"] == body


def test_large_directory_and_first_line_are_explicitly_incomplete_and_bounded(local):  # noqa: F811 - imported pytest fixture
    _, _, root, _ = local
    for number in range(120):
        (root / f"file-{number:03}-{'x' * 60}.txt").touch()
    (root / "README.md").write_text("\\" * 50000, encoding="utf-8")
    result = observe(local)
    listing = result["root_listing"]
    assert listing["incomplete"] is True and listing["next_offset"] == len(listing["entries"])
    assert listing["continuation"]["arguments"]["offset"] == len(listing["entries"])
    page = next(item for item in result["entry_documents"] if item["path"] == "README.md")
    assert page["status"] == "deferred" and page["incomplete"] is True and not page["text"]
    assert "end_line" not in page and page["continuation"]["arguments"] == {"path": "README.md", "offset": 0}
    assert result["snapshot_incomplete"] is True and len(encoded(result)) <= projects.INSPECT_BYTES


def test_child_directory_limit_does_not_claim_all_subdirectories_were_listed(local):  # noqa: F811 - imported pytest fixture
    _, _, root, _ = local
    for number in range(12):
        (root / f"package{number:02}").mkdir()
    result = observe(local)
    assert len(result["directory_listings"]) <= 8
    assert result["directory_listings_incomplete"] is True
    assert result["directory_listings_continuation"]["tool"] == "ryn_file_list"
    assert result["snapshot_incomplete"] is True


def test_budget_counts_json_escaping_and_unicode_without_claiming_omitted_pages(local):  # noqa: F811 - imported pytest fixture
    _, _, root, _ = local
    (root / "package.json").write_text(json.dumps({"scripts": {
        f"{number:02}" + "测" * 78: "private command" for number in range(32)}}, ensure_ascii=False), encoding="utf-8")
    for name in projects.ENTRY_FILES:
        (root / name).write_bytes((('测' * 30 + '\\"' * 30 + '\n') * 100).encode())
    for number in range(60):
        (root / f"file{number:02}-{'x' * 70}").touch()
    result = observe(local)
    assert len(encoded(result)) <= projects.INSPECT_BYTES and result["snapshot_incomplete"] is True
    for page in result["entry_documents"]:
        assert page["incomplete"] is True and page["continuation"] is not None
        if page.get("text"):
            assert page["end_line"] == len(page["text"].splitlines())
            assert page["next_start_line"] == page["end_line"] + 1
    assert "private command" not in json.dumps(result)


def test_time_bounded_directory_sample_restarts_exact_listing_instead_of_skipping_names(tmp_path, monkeypatch):
    for name in ("z.txt", "a.txt", "middle.txt"):
        (tmp_path / name).touch()
    ticks = iter((0, 0, 1))
    monkeypatch.setattr(projects.time, "monotonic", lambda: next(ticks))
    listing, _, complete_scan = projects._inspect_listing(
        tmp_path, tmp_path, lambda: False, byte_limit=3000, max_entries=60)
    assert complete_scan is False and listing["incomplete"] is True
    assert listing["next_offset"] == 0 and listing["continuation"]["arguments"]["offset"] == 0
    assert "not an exact sorted prefix" in listing["note"]


def test_snapshot_does_not_follow_guidance_or_directory_symlinks(local, tmp_path):  # noqa: F811 - imported pytest fixture
    _, _, root, _ = local
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("OUTSIDE CONTENT", encoding="utf-8")
    try:
        os.symlink(outside / "secret.txt", root / "AGENTS.md")
        os.symlink(outside, root / "external", target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this Windows account")
    result = observe(local)
    guidance = next(item for item in result["entry_documents"] if item["path"] == "AGENTS.md")
    assert guidance["status"] == "skipped_symlink" and guidance["incomplete"] is True
    assert not any(item["path"] == "external" for item in result["directory_listings"])
    assert "OUTSIDE CONTENT" not in json.dumps(result) and "secret.txt" not in json.dumps(result)


def test_snapshot_records_real_file_version_and_external_changes_still_block_edits(local):  # noqa: F811 - imported pytest fixture
    tools, context, root, _ = local
    path = root / "AGENTS.md"
    path.write_text("Original instructions.\n", encoding="utf-8")
    page = next(item for item in observe(local)["entry_documents"] if item["path"] == "AGENTS.md")
    assert tools._get_observed_sha256(context, path) == page["sha256"]
    tools.preview("ryn_file_edit", {"path": "AGENTS.md", "old_text": "Original", "new_text": "Updated"}, context)
    path.write_text("External update.\n", encoding="utf-8")
    with pytest.raises(AgentToolRejected, match="agent_local_file_changed_read_again"):
        tools.preview("ryn_file_edit", {"path": "AGENTS.md", "old_text": "External", "new_text": "Updated"}, context)


def test_cancellation_stops_snapshot_between_entry_reads(local, monkeypatch):  # noqa: F811 - imported pytest fixture
    tools, _, root, _ = local
    (root / "AGENTS.md").write_text("Instructions.\n", encoding="utf-8")
    (root / "README.md").write_text("Must not read after cancellation.\n", encoding="utf-8")
    original = tools.perform
    reads, stopped = [], [False]

    def perform(name, args, *rest, **kwargs):
        result = original(name, args, *rest, **kwargs)
        if name == "ryn_file_read":
            reads.append(args["path"])
            stopped[0] = True
        return result

    monkeypatch.setattr(tools, "perform", perform)
    with pytest.raises(AgentToolRejected, match="agent_cancelled"):
        observe(local, lambda: stopped[0])
    assert reads == ["AGENTS.md"]


def test_permission_revocation_during_snapshot_is_rechecked_before_next_read(local, monkeypatch):  # noqa: F811 - imported pytest fixture
    tools, _, root, state = local
    (root / "AGENTS.md").write_text("Instructions.\n", encoding="utf-8")
    (root / "README.md").write_text("Must not read after revocation.\n", encoding="utf-8")
    original = tools.perform
    reads = []

    def perform(name, args, *rest, **kwargs):
        result = original(name, args, *rest, **kwargs)
        if name == "ryn_file_read":
            reads.append(args["path"])
            state.transaction(lambda data: data["settings"].update(enabled=False))
        return result

    monkeypatch.setattr(tools, "perform", perform)
    with pytest.raises(AgentToolRejected, match="agent_disabled"):
        observe(local)
    assert reads == ["AGENTS.md"]


def test_entry_snapshot_reports_nested_instructions_and_batches_observed_files(local, monkeypatch):  # noqa: F811 - imported pytest fixture
    tools, context, root, state = local
    (root / "AGENTS.md").write_text("Root guidance.\n", encoding="utf-8")
    (root / "README.md").write_text("Root readme.\n", encoding="utf-8")
    (root / "AI_HANDOFF.md").write_text("Handoff.\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "AGENTS.md").write_text("Nested test guidance.\n", encoding="utf-8")

    tx_count = [0]
    orig_tx = state.transaction

    def count_tx(mutate):
        tx_count[0] += 1
        return orig_tx(mutate)

    monkeypatch.setattr(state, "transaction", count_tx)
    result = observe(local)
    assert result["nested_instructions"] == {"found": ["tests/AGENTS.md"], "complete": True}
    # All three entry files are recorded in a single batched transaction rather than 3 separate commits.
    assert tx_count[0] == 1
    assert tools._get_observed_sha256(context, root / "AGENTS.md") is not None
    assert tools._get_observed_sha256(context, root / "README.md") is not None
    assert tools._get_observed_sha256(context, root / "AI_HANDOFF.md") is not None

    # Adding a deeper subdirectory marks nested_instructions.complete False so deeper paths are not assumed absent.
    (root / "src" / "subpkg").mkdir()
    second = observe(local)
    assert second["nested_instructions"] == {"found": ["tests/AGENTS.md"], "complete": False}
