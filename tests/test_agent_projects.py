"""Project grants, restart/revocation and real file tools through the Agent loop."""
import json
import os
import shutil
import threading
import uuid
from types import SimpleNamespace

import pytest
from test_agent_integrations import add_source, call, fake_upstream, passed_checker
from test_agent_integrations import node as node
from test_agent_runtime import assistant, make_runtime

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore, AgentToolRejected
from rynmesh.services import peer_box


def project_runtime(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path)
    model.supports_destination_binding = True
    runtime.catalog.list_models = lambda: [{"id": "local:test", "source": "local_api", "source_id": "fixture",
        "destination_revision": "fixture-v1", "available": True, "agent_supported": True}]
    return runtime, model, tools, key


def project_folder(tmp_path):
    root = tmp_path / "sample-backend"
    (root / "src").mkdir(parents=True)
    (root / "test/support").mkdir(parents=True)
    (root / "AGENTS.md").write_text("Preserve existing changes. Run isolated tests.", encoding="utf-8")
    (root / "README.md").write_text("A backend project.", encoding="utf-8")
    (root / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}, "dependencies": {"@midwayjs/core": "3"}}), encoding="utf-8")
    (root / "test/support/runIsolated.cjs").write_text("// isolation entry", encoding="utf-8")
    return root


def test_project_selection_is_task_local_restart_safe_and_does_not_change_other_workspaces(tmp_path):
    root = project_folder(tmp_path)
    runtime, model, tools, key = project_runtime(tmp_path / "state")
    ordinary = runtime.create({"message": "Ordinary task"})
    selected = runtime.load_project({"path": str(root)})
    assert runtime.load_project({"path": str(root)})["id"] == selected["id"]
    task = runtime.create({"message": "Inspect the project", "project_id": selected["id"]})
    assert task["local_execution"]["workspace_root"] == str(root)
    assert task["skills"] == []
    assert runtime.get(ordinary["id"])["local_execution"]["workspace_root"] != str(root)
    assert runtime.settings()["workspace_root"] == ""
    restored = AgentRuntime(AgentStore(runtime.store.root, key), model, tools, runtime.catalog)
    assert restored.list_projects()[0]["id"] == selected["id"]
    assert restored.project_selection() == selected["id"]
    restored.select_project({"project_id": ""})
    assert restored.project_selection() == ""
    assert restored.get(task["id"])["project"]["id"] == selected["id"]
    assert "test_isolation_entry" not in restored.get(task["id"])["project"]
    context = restored._context(restored.store.read()["tasks"][task["id"]])
    assert any(message["role"] == "user" and "AGENTS.md" in message["content"] for message in context)
    assert not any(message["role"] in {"developer", "system"} and "Run isolated tests." in message["content"] for message in context)


def test_project_revocation_and_regrant_do_not_revive_old_access(tmp_path):
    root = project_folder(tmp_path)
    runtime, _, _, _ = project_runtime(tmp_path / "state")
    project = runtime.load_project({"path": str(root)})
    row = runtime.create({"message": "Read project", "project_id": project["id"]})
    local = LocalTools(SimpleNamespace(home=tmp_path), lambda: OperationLedger(tmp_path))
    local.store = runtime.store
    context = runtime._execution_context(runtime.store.read()["tasks"][row["id"]])
    assert local.perform("ryn_project_inspect", {}, context, lambda: False)["frameworks"] == ["@midwayjs/core"]
    runtime.remove_project(project["id"])
    assert runtime.get(row["id"])["status"] == "paused"
    reloaded = runtime.load_project({"path": str(root)})
    assert reloaded["generation"] == project["generation"] + 1
    with pytest.raises(AgentToolRejected, match="permission_revoked"):
        local.path("AGENTS.md", context)
    with pytest.raises(AgentError, match="permission_revoked"):
        runtime._validate_context(runtime.store.read()["tasks"][row["id"]])
    local.processes.close()


def test_multiple_saved_projects_keep_explicit_task_binding_after_selection_and_restart(tmp_path):
    runtime, model, tools, key = project_runtime(tmp_path / "state")
    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        root.mkdir()
    first, second = [runtime.load_project({"path": str(root)}) for root in roots]
    assert {row["id"] for row in runtime.list_projects()} == {first["id"], second["id"]}
    assert runtime.project_selection() == second["id"]
    task = runtime.create({"message": "Work in first", "project_id": first["id"]})
    runtime.select_project({"project_id": first["id"]})
    other = runtime.create({"message": "Work in second", "project_id": second["id"]})
    runtime.select_project({"project_id": second["id"]})
    restored = AgentRuntime(AgentStore(runtime.store.root, key), model, tools, runtime.catalog)
    assert len(restored.list_projects()) == 2
    assert restored.project_selection() == second["id"]
    assert restored.get(task["id"])["project"]["id"] == first["id"]
    assert restored.get(other["id"])["local_execution"]["workspace_root"] == str(roots[1])
    restored.remove_project(first["id"])
    assert [row["id"] for row in restored.list_projects()] == [second["id"]]
    assert restored.get(other["id"])["status"] == "queued"


def test_actual_project_loop_preloads_tools_reads_instructions_and_creates_verified_output(node, monkeypatch, tmp_path):
    app, owner = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, owner, owner.home)
    runtime = AgentRuntime(AgentStore(owner.home / "project-agent", peer_box.load_or_create_messaging_key(owner.home / "messaging.x25519")), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": model_id, "verify_text_responses": True})
    root = project_folder(tmp_path)
    selected = runtime.load_project({"path": str(root)})
    requests = fake_upstream(monkeypatch, [call("ryn_project_inspect", call_id="inspect"),
        call("ryn_file_read", json.dumps({"path": "AGENTS.md"}), "instructions"),
        call("ryn_file_write", json.dumps({"path": "report.txt", "content": "Project inspected; existing changes preserved."}), "write"),
        {"role": "assistant", "content": "Created report.txt."}, passed_checker])
    body = {"message": "Inspect project and save report.txt", "project_id": selected["id"], "client_request_id": str(uuid.uuid4())}
    task = runtime.create(body)
    assert runtime.create(body)["id"] == task["id"]
    for _ in range(20):
        runtime.run_once()
        current = runtime.get(task["id"])
        if current["status"] == "waiting_approval":
            assert not (root / "report.txt").exists()
            runtime.action(task["id"], {"action": "approve", "expected_control_revision": current["control_revision"], "approval_id": current["pending_approval"]["id"]})
        if current["status"] in {"completed", "failed"}:
            break
    assert current["status"] == "completed", current.get("error")
    assert current["outcome"]["status"] == "verified", current["outcome"]
    assert (root / "report.txt").read_text() == "Project inspected; existing changes preserved."
    assert not any(event["type"] == "change_review_requested" for event in current["events"])
    assert "ryn_project_inspect" in {spec["function"]["name"] for spec in requests[0]["tools"]}
    assert len(requests) == 5


def test_slow_task_does_not_block_other_task_or_duplicate_dispatch(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    first = runtime.create({"message": "slow"})
    second = runtime.create({"message": "fast"})
    entered, release = threading.Event(), threading.Event()
    calls = []
    def complete(_id, messages, *_args):
        content = next(m["content"] for m in messages if m["role"] == "user" and m["content"] in {"slow", "fast"})
        calls.append(content)
        if content == "slow":
            entered.set()
            assert release.wait(5)
        return assistant(content + " done")
    model.complete = complete
    worker = threading.Thread(target=runtime.run_once)
    worker.start()
    try:
        assert entered.wait(3)
        assert runtime.get(first["id"])["run_timing"]["measurement"] == "live"
        runtime.run_once()
        assert runtime.get(second["id"])["status"] == "completed"
        runtime.run_once()
        assert calls.count("slow") == calls.count("fast") == 1
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and runtime.get(first["id"])["status"] == "completed"


def test_store_read_cache_is_isolated_and_invalidates_on_another_writer(tmp_path, monkeypatch):
    runtime, _, _, key = make_runtime(tmp_path)
    other = AgentStore(tmp_path, key)
    original = peer_box.open_sealed
    decrypted = []
    def open_sealed(*args, **kwargs):
        decrypted.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(peer_box, "open_sealed", open_sealed)
    runtime.store.read()["settings"]["enabled"] = False
    assert runtime.store.read()["settings"]["enabled"] is True
    assert decrypted == []
    other.transaction(lambda data: data["settings"].update(enabled=False))
    assert runtime.store.read()["settings"]["enabled"] is False
    assert len(decrypted) == 2


def test_project_destination_change_stops_before_sending_any_project_text(tmp_path):
    runtime, model, _, _ = project_runtime(tmp_path / "state")
    selected = runtime.load_project({"path": str(project_folder(tmp_path))})
    task = runtime.create({"message": "Inspect project", "project_id": selected["id"]})
    changed = {**runtime.catalog.list_models()[0], "destination_revision": "other-endpoint"}
    runtime.catalog.list_models = lambda: [changed]
    runtime.run_once()
    assert not model.calls
    assert runtime.get(task["id"])["error"] == "agent_model_destination_changed"


@pytest.mark.parametrize("transfer", [False, True])
def test_project_model_switch_requires_explicit_transfer_and_rebinds(tmp_path, transfer):
    from test_agent_runtime_model_switch import body
    runtime, _, _, _ = project_runtime(tmp_path / "state")
    selected = runtime.load_project({"path": str(project_folder(tmp_path))})
    task = runtime.create({"message": "Inspect project", "project_id": selected["id"]})
    runtime.action(task["id"], {"action": "pause"})
    previous = runtime.catalog.list_models()[0]
    runtime.catalog.list_models = lambda: [previous, {**previous, "id": "local:other", "source_id": "other", "destination_revision": "other-v1"}]
    preview = runtime.preview_model_switch(task["id"], "local:other")
    assert preview["current_project"]["path"] == selected["path"]
    assert preview["current_scope_authorized"]
    switched = runtime.switch_model(task["id"], body(runtime, task["id"], transfer_resource_scope=transfer))
    stored = runtime.store.read()["tasks"][task["id"]]
    assert switched["status"] == "paused"
    assert stored["model_destination"]["source_id"] == "other"
    if transfer:
        assert switched["project"]["id"] == selected["id"]
        assert stored["local_execution"]["project_id"] == selected["id"]
        runtime._validate_context(stored)
    else:
        assert not switched.get("project")
        assert stored["local_execution"]["workspace_root"] != selected["path"]
        assert "Preserve existing changes. Run isolated tests." not in json.dumps(runtime._context(stored))


def test_project_creation_rejects_unverifiable_model_destination(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path / "state")
    selected = runtime.load_project({"path": str(project_folder(tmp_path))})
    with pytest.raises(AgentError, match="agent_model_destination_unverifiable"):
        runtime.create({"message": "Read project", "project_id": selected["id"]})
    assert not runtime.list_tasks()


def test_checkpoint_file_coverage_survives_restart_and_excludes_old_scope(tmp_path):
    from rynmesh.agent_runtime import projects
    runtime, _, _, _ = project_runtime(tmp_path / "state")
    root = project_folder(tmp_path)
    selected = runtime.load_project({"path": str(root)})
    created = runtime.create({"message": "Inspect project", "project_id": selected["id"]})
    row = runtime.store.read()["tasks"][created["id"]]
    row["context_checkpoint"] = {"through": 1, "content": "Read guidance."}
    def page(first, last, digest="a" * 64, epoch=0, path=None):
        return {"type": "tool_result", "tool": "ryn_file_read", "context_epoch": epoch,
                "result": {"path": str(path or root / "AGENTS.md"), "sha256": digest,
                    "start_line": first, "end_line": last, "total_lines": 10, "text": "injected prose"}}
    row["events"] += [page(1, 3), page(5, 10), page(1, 10, epoch=1), page(1, 10, path=tmp_path / "outside.md")]
    note = projects.read_progress(row)
    assert '"ranges":[[1,3],[5,10]]' in note and '"complete_at_read":false' in note
    assert "injected prose" not in note and "outside.md" not in note
    row["events"].append(page(4, 4))
    assert '"complete_at_read":true' in projects.read_progress(row)
    row["events"].append(page(1, 2, digest="b" * 64))
    assert '"ranges":[[1,2]]' in projects.read_progress(row)
    assert '"complete_at_read":false' in projects.read_progress(row)
    row["context_epoch"] = 2
    assert projects.read_progress(row) == ""
    row["context_checkpoint"] = None
    assert projects.read_progress(row) == ""


def test_only_project_source_is_abbreviated_for_checkpoint_not_business_data():
    import copy

    from rynmesh.agent_runtime import projects
    source = "中文代码\n" * 4000
    history = [{"role": "user", "content": source}, {"role": "assistant", "tool_calls": [
        {"id": "source", "function": {"name": "ryn_file_read"}},
        {"id": "business", "function": {"name": "ryn_read_resource"}}]},
        {"role": "tool", "tool_call_id": "source", "content": json.dumps({"text": source, "sha256": "a" * 64})},
        {"role": "tool", "tool_call_id": "business", "content": json.dumps({"text": source})}]
    original = copy.deepcopy(history)
    projected = projects.summary_history(history)
    excerpt = json.loads(projected[2]["content"])
    assert excerpt["summary_excerpt_only"] and len(excerpt["text"].encode()) <= 2000
    assert "�" not in excerpt["text"] and source.startswith(excerpt["text"])
    assert projected[0] == original[0] and projected[3] == original[3]
    assert history == original


def test_oversized_closed_source_batch_can_be_checkpointed_without_losing_archive(tmp_path):
    import copy

    from test_agent_runtime import advance

    from rynmesh.agent_runtime.store import encoded
    runtime, model, _, _ = project_runtime(tmp_path / "state")
    selected = runtime.load_project({"path": str(project_folder(tmp_path))})
    runtime.update_settings({"max_context_chars": 60000})
    created = runtime.create({"message": "Read guidance then repair the project", "project_id": selected["id"]})
    calls = [{"id": f"read{i}", "type": "function", "function": {"name": "ryn_file_read",
              "arguments": json.dumps({"path": f"src/file{i}.ts"})}} for i in range(8)]
    def seed(data):
        row = data["tasks"][created["id"]]
        row["messages"].append({"role": "assistant", "content": "Located files; read this batch.", "tool_calls": calls})
        for i in range(8):
            row["messages"].append({"role": "tool", "tool_call_id": f"read{i}",
                "content": json.dumps({"text": "中文源码\n" * 3000, "path": str(selected["path"])+f"/src/file{i}.ts",
                                       "start_line": 1, "end_line": 3000, "total_lines": 3000, "sha256": "a" * 64}, ensure_ascii=False)})
    runtime.store.transaction(seed)
    before = copy.deepcopy(runtime.store.read()["tasks"][created["id"]]["messages"])
    assert len(encoded(runtime._context(runtime.store.read()["tasks"][created["id"]]))) > 60000
    model.responses = [assistant("Read eight source files; next implement the located repairs."), assistant("Finished inspection.")]
    original_complete = model.complete
    model.complete = lambda *args, **kwargs: original_complete(*args)
    advance(runtime)
    row = runtime.store.read()["tasks"][created["id"]]
    assert row.get("context_checkpoint"), row.get("error")
    assert row["messages"][:len(before)] == before
    assert not model.calls[0][2] and len(encoded(model.calls[0][1])) < 60000
    payload = json.loads(model.calls[0][1][-1]["content"])
    assert all(m["content"]["summary_excerpt_only"] for m in payload["history"] if m["role"] == "tool")


def test_control_projection_keeps_authority_origins_and_refreshes_other_writer(tmp_path, monkeypatch):
    import copy

    from rynmesh.agent_runtime.store import AgentStore
    runtime, _, _, key = project_runtime(tmp_path)
    task = runtime.create({"message": "Read project"})
    def seed(data):
        row = data["tasks"][task["id"]]
        row["messages"].append({"role": "tool", "context_epoch": 0, "resource_origin": {"grant_id": "exact"}, "content": "source" * 100000})
        row["events"].append({"type": "fixture", "data": "large" * 100000})
    runtime.store.transaction(seed)
    whole = runtime.store.read()
    original_deepcopy = copy.deepcopy
    copied_whole = []
    def tracked(value, *args, **kwargs):
        if value is runtime.store._cached_data: copied_whole.append(True)
        return original_deepcopy(value, *args, **kwargs)
    monkeypatch.setattr(copy, "deepcopy", tracked)
    control = runtime.store.read_control(task["id"])
    assert not copied_whole
    assert runtime._execution_context(control["tasks"][task["id"]]) == runtime._execution_context(whole["tasks"][task["id"]])
    assert "events" not in control["tasks"][task["id"]]
    assert "content" not in control["tasks"][task["id"]]["messages"][0]
    control["settings"]["enabled"] = False
    assert runtime.store.read_control(task["id"])["settings"]["enabled"]
    other = AgentStore(tmp_path, key)
    other.transaction(lambda data: data["settings"].update(enabled=False))
    assert not runtime.store.read_control(task["id"])["settings"]["enabled"]


def test_larger_owner_context_budget_still_respects_selected_model_window(tmp_path):
    runtime, _, _, _ = project_runtime(tmp_path)
    runtime.update_settings({"max_context_chars": 384000})
    created = runtime.create({"message": "Inspect project"})
    row = runtime.store.read()["tasks"][created["id"]]
    base = runtime.catalog.list_models()[0]
    runtime.catalog.list_models = lambda: [{**base, "context_window": 500000, "max_output_tokens": 4096}]
    assert runtime._context_limit(row) == 384000
    runtime.catalog.list_models = lambda: [{**base, "context_window": 32768, "max_output_tokens": 4096}]
    assert runtime._context_limit(row) < 98000
    assert runtime.get(created["id"])["limits"]["max_context_chars"] == 384000


def test_project_destination_change_rebinds_same_or_new_model_and_resumes_explicitly(tmp_path):
    from test_agent_runtime_model_switch import body
    runtime, model, _, _ = project_runtime(tmp_path / "state")
    selected = runtime.load_project({"path": str(project_folder(tmp_path))})
    task = runtime.create({"message": "Inspect project", "project_id": selected["id"]})
    task_id = task["id"]

    # Record prior epoch assistant/tool history before source destination changes
    def seed_old_epoch(data):
        row = data["tasks"][task_id]
        row["messages"].extend([
            {"role": "assistant", "content": "OLD_EPOCH_SECRET", "context_epoch": 0},
        ])

    runtime.store.transaction(seed_old_epoch)
    changed = {**runtime.catalog.list_models()[0], "destination_revision": "other-endpoint"}
    runtime.catalog.list_models = lambda: [changed]
    runtime.run_once()
    assert not model.calls
    failed = runtime.get(task_id)
    assert failed["status"] == "failed"
    assert failed["error"] == "agent_model_destination_changed"

    # Previewing the same model_id with its new destination revision allows project scope transfer
    preview = runtime.preview_model_switch(task_id, "local:test")
    assert preview["current_project"]["id"] == selected["id"]
    assert preview["current_scope_authorized"] is True
    assert preview["destination"]["destination_revision"] == "other-endpoint"

    # Stale expected_destination is still rejected
    req = body(runtime, task_id, "local:test", transfer_resource_scope=True)
    with pytest.raises(AgentError, match="agent_model_destination_changed"):
        runtime.switch_model(task_id, {**req, "expected_destination": {**req["expected_destination"], "destination_revision": "fixture-v1"}})

    # Explicit owner confirmation rebinds destination, retains project, retires old epoch projection, and stays paused
    switched = runtime.switch_model(task_id, req)
    assert switched["status"] == "paused"
    assert switched["error"] is None
    assert switched["project"]["id"] == selected["id"]
    stored = runtime.store.read()["tasks"][task_id]
    assert stored["model_destination"]["destination_revision"] == "other-endpoint"
    assert stored["model_destination_confirmed"] is True
    assert stored["context_epoch"] == 1
    assert stored["local_execution"]["project_id"] == selected["id"]
    runtime._validate_context(stored)
    projected = json.dumps(runtime._context(stored))
    assert "OLD_EPOCH_SECRET" not in projected
    assert "Preserve existing changes. Run isolated tests." in projected

    # Does not run until explicit owner resume
    runtime.run_once()
    assert not model.calls
    model.responses.append(assistant("Project inspected after explicit destination rebind."))
    original_complete = model.complete
    model.complete = lambda *args, **kwargs: original_complete(*args)
    runtime.action(task_id, {"action": "resume", "expected_control_revision": switched["control_revision"]})
    runtime.run_once()
    completed = runtime.get(task_id)
    assert completed["status"] == "completed"
    assert completed["result"] == "Project inspected after explicit destination rebind."
    assert len(model.calls) == 1

    # If the project itself is removed, scope transfer is no longer authorized
    runtime.remove_project(selected["id"])
    assert runtime.preview_model_switch(task_id, "local:test")["current_scope_authorized"] is False
    with pytest.raises(AgentError, match="agent_project_unavailable"):
        runtime.switch_model(task_id, body(runtime, task_id, "local:test", transfer_resource_scope=True))


def _make_agents_md_bytes(target_bytes: int) -> bytes:
    lines = []
    size = 0
    idx = 1
    while size < target_bytes:
        remaining = target_bytes - size
        if remaining <= 32:
            line = ("rule-" + ("x" * max(0, remaining - 6)) + "\n")[:remaining]
            if not line.endswith("\n") and remaining >= 1:
                line = line[:-1] + "\n"
        else:
            line = f"Rule {idx:04d}: keep deterministic module boundaries and tests.\n"
            if len(line) > remaining:
                line = ("x" * (remaining - 1)) + "\n"
        raw_line = line.encode("utf-8")
        lines.append(raw_line)
        size += len(raw_line)
        idx += 1
    result = b"".join(lines)
    assert len(result) == target_bytes
    return result


def _legacy_untruncated_guidance(row: dict, full_text: str) -> str:
    project = row["project"]
    path = project.get("path") or ""
    shell = ("pwsh" if shutil.which("pwsh") else "powershell") if os.name == "nt" else "sh"
    info = {key: project[key] for key in ("name", "path", "entry_files", "manifests", "directories") if key in project}
    env_block = (
        "<environment_context>\n"
        f"  <cwd>{path}</cwd>\n"
        f"  <shell>{shell}</shell>\n"
        f"  <project>{json.dumps(info, ensure_ascii=False, separators=(',', ':'))}</project>\n"
        "</environment_context>"
    )
    rules = []
    if project.get("is_git"):
        rules.append(
            "Git repository: do not commit, amend, create or switch branches, or discard changes "
            "(reset --hard, checkout --, clean) unless the owner asks. Changes you did not make "
            "belong to the owner; keep them."
        )
    rules.append(
        "Fix the cause, not the symptom; follow the repository's existing patterns; keep the change "
        "to what was asked; mention unrelated problems instead of fixing them."
    )
    rules.append(
        "Batch independent final checks in one response; repeat only after changes, failure or unresolved uncertainty."
    )
    rules_block = "\n".join(rules)
    scope_rule = (
        "AGENTS.md rules apply to their directory tree; a deeper file wins over a higher one, "
        "and the owner's direct instructions win over both."
    )
    stop_rule = "If you stop because of a rule in AGENTS.md, name the file and quote the rule."
    return (
        f"{env_block}\n\n"
        f"{rules_block}\n\n"
        f"# AGENTS.md instructions for {path}\n\n"
        f"{scope_rule}\n\n"
        "<INSTRUCTIONS>\n"
        f"{full_text.rstrip(chr(10))}\n"
        "</INSTRUCTIONS>\n"
        f"{stop_rule}"
    )


@pytest.mark.parametrize("size_kb,expect_truncated,expect_incomplete", [
    (4, False, False),
    (20, False, False),
    (40, True, False),
    (70, True, True),
])
def test_agents_md_32kib_budget_truncation_marker_and_task_event(tmp_path, size_kb, expect_truncated, expect_incomplete):
    import hashlib
    import re

    from rynmesh.agent_runtime import projects

    target_bytes = size_kb * 1024
    raw_bytes = _make_agents_md_bytes(target_bytes)
    root = project_folder(tmp_path / f"proj_{size_kb}k")
    (root / "AGENTS.md").write_bytes(raw_bytes)

    runtime, _, _, _ = project_runtime(tmp_path / f"state_{size_kb}k")
    selected = runtime.load_project({"path": str(root)})
    created = runtime.create({"message": "Check instructions", "project_id": selected["id"]})
    row = runtime.store.read()["tasks"][created["id"]]

    instructions = row["project"]["instructions"]
    assert instructions["bytes"] == target_bytes
    assert instructions["sha256"] == hashlib.sha256(raw_bytes[:64 * 1024]).hexdigest()
    assert instructions["sha256_scope"] == ("read_prefix" if expect_incomplete else "full_file")
    assert instructions["incomplete"] is expect_incomplete

    rendered = projects.guidance(row)
    trunc_events = [e for e in row["events"] if e["type"] == "project_instructions_truncated"]

    if not expect_truncated:
        assert "[AGENTS.md truncated:" not in rendered
        assert rendered == _legacy_untruncated_guidance(row, raw_bytes.decode("utf-8"))
        assert trunc_events == []
    else:
        inner = rendered.split("<INSTRUCTIONS>\n", 1)[1].rsplit("\n</INSTRUCTIONS>", 1)[0]
        injected_text, marker_tail = inner.rsplit("\n[AGENTS.md truncated: ", 1)
        marker = "[AGENTS.md truncated: " + marker_tail
        injected_bytes = len(injected_text.encode("utf-8"))
        expected_start_line = len(injected_text.splitlines()) + 1
        assert 32000 < injected_bytes <= 32768

        m = re.fullmatch(
            r"\[AGENTS\.md truncated: showing (\d+) of (>64 KiB|\d+ bytes); "
            r"read the rest with ryn_file_read path=AGENTS\.md start_line=(\d+)\]",
            marker,
        )
        assert m is not None, marker
        assert int(m.group(1)) == injected_bytes
        assert m.group(2) == (">64 KiB" if expect_incomplete else f"{target_bytes} bytes")
        assert int(m.group(3)) == expected_start_line

        assert len(trunc_events) == 1
        ev = trunc_events[0]
        assert ev["injected_bytes"] == injected_bytes
        assert ev["total_bytes"] == target_bytes
        assert ev["incomplete"] is expect_incomplete
        assert "Rule 0001" not in json.dumps(ev)


def test_agents_md_small_context_window_respects_quarter_limit(tmp_path):
    from rynmesh.agent_runtime import projects

    raw_bytes = _make_agents_md_bytes(4 * 1024)
    root = project_folder(tmp_path / "proj_small_win")
    (root / "AGENTS.md").write_bytes(raw_bytes)

    runtime, _, _, _ = project_runtime(tmp_path / "state_small_win")
    runtime.update_settings({"max_context_chars": 12000})
    selected = runtime.load_project({"path": str(root)})
    created = runtime.create({"message": "Check small window", "project_id": selected["id"]})
    row = runtime.store.read()["tasks"][created["id"]]

    rendered = projects.guidance(row)
    inner = rendered.split("<INSTRUCTIONS>\n", 1)[1].rsplit("\n</INSTRUCTIONS>", 1)[0]
    injected_text, marker_tail = inner.rsplit("\n[AGENTS.md truncated: ", 1)
    injected_bytes = len(injected_text.encode("utf-8"))
    expected_start_line = len(injected_text.splitlines()) + 1
    assert injected_bytes <= 3000
    assert (
        "[AGENTS.md truncated: " + marker_tail
        == f"[AGENTS.md truncated: showing {injected_bytes} of 4096 bytes; read the rest with ryn_file_read path=AGENTS.md start_line={expected_start_line}]"
    )
    trunc_events = [e for e in row["events"] if e["type"] == "project_instructions_truncated"]
    assert len(trunc_events) == 1
    assert trunc_events[0]["injected_bytes"] == injected_bytes
    assert trunc_events[0]["total_bytes"] == 4096



def test_s03_i4_project_guidance_git_non_git_and_no_project(tmp_path):
    from rynmesh.agent_runtime import projects

    git_rule = (
        "Git repository: do not commit, amend, create or switch branches, or discard changes "
        "(reset --hard, checkout --, clean) unless the owner asks. Changes you did not make "
        "belong to the owner; keep them."
    )
    eng_rule = (
        "Fix the cause, not the symptom; follow the repository's existing patterns; keep the change "
        "to what was asked; mention unrelated problems instead of fixing them."
    )
    scope_rule = (
        "AGENTS.md rules apply to their directory tree; a deeper file wins over a higher one, "
        "and the owner's direct instructions win over both."
    )

    runtime, _, _, _ = project_runtime(tmp_path / "state")

    # 1. No project -> environment only; project rules are never injected.
    no_proj_task = runtime.create({"message": "Plain task"})
    no_proj_row = runtime.store.read()["tasks"][no_proj_task["id"]]
    plain_guidance = projects.guidance(no_proj_row)
    assert "<environment_context>" in plain_guidance and "<shell>" in plain_guidance
    assert f'<cwd>{no_proj_row["local_execution"]["workspace_root"]}</cwd>' in plain_guidance
    no_proj_ctx = json.dumps(runtime._context(no_proj_row))
    assert git_rule not in no_proj_ctx
    assert eng_rule not in no_proj_ctx
    assert scope_rule not in no_proj_ctx

    # 2. Non-git directory with AGENTS.md -> engineering + scope rules, no git rule
    non_git_root = project_folder(tmp_path / "non_git")
    non_git_sel = runtime.load_project({"path": str(non_git_root)})
    non_git_task = runtime.create({"message": "Inspect non-git", "project_id": non_git_sel["id"]})
    non_git_row = runtime.store.read()["tasks"][non_git_task["id"]]
    assert non_git_row["project"]["is_git"] is False
    non_git_guidance = projects.guidance(non_git_row)
    assert git_rule not in non_git_guidance
    assert eng_rule in non_git_guidance
    assert scope_rule in non_git_guidance
    assert (
        non_git_guidance.index("</environment_context>")
        < non_git_guidance.index(eng_rule)
        < non_git_guidance.index("# AGENTS.md instructions for ")
        < non_git_guidance.index(scope_rule)
        < non_git_guidance.index("<INSTRUCTIONS>")
    )

    # 3. Git repository (.git dir) with AGENTS.md -> git + engineering + scope rules in order
    git_root = project_folder(tmp_path / "git_repo")
    (git_root / ".git").mkdir()
    git_sel = runtime.load_project({"path": str(git_root)})
    git_task = runtime.create({"message": "Inspect git repo", "project_id": git_sel["id"]})
    git_row = runtime.store.read()["tasks"][git_task["id"]]
    assert git_row["project"]["is_git"] is True
    git_guidance = projects.guidance(git_row)
    assert git_rule in git_guidance
    assert eng_rule in git_guidance
    assert scope_rule in git_guidance
    assert (
        git_guidance.index("</environment_context>")
        < git_guidance.index(git_rule)
        < git_guidance.index(eng_rule)
        < git_guidance.index("# AGENTS.md instructions for ")
        < git_guidance.index(scope_rule)
        < git_guidance.index("<INSTRUCTIONS>")
    )

    # 4. Git worktree (.git file) without AGENTS.md -> git + engineering rules, no AGENTS.md block
    wt_root = tmp_path / "git_worktree_no_agents"
    wt_root.mkdir()
    (wt_root / ".git").write_text("gitdir: ../.git/worktrees/wt\n", encoding="utf-8")
    wt_sel = runtime.load_project({"path": str(wt_root)})
    wt_task = runtime.create({"message": "Inspect worktree", "project_id": wt_sel["id"]})
    wt_row = runtime.store.read()["tasks"][wt_task["id"]]
    assert wt_row["project"]["is_git"] is True
    wt_guidance = projects.guidance(wt_row)
    assert git_rule in wt_guidance
    assert eng_rule in wt_guidance
    assert scope_rule not in wt_guidance
    assert "<INSTRUCTIONS>" not in wt_guidance
