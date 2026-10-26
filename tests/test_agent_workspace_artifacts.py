"""Task files come only from confirmed file-tool receipts, keep one link per path and are served inert."""
import hashlib
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_agent_runtime import Catalog, Model, Tools, make_runtime

from rynmesh.agent_runtime import results, workspace_artifacts
from rynmesh.agent_runtime.routes import install_agent
from rynmesh.agent_runtime.store import AgentError
from rynmesh.background_workers import BackgroundWorkerRegistry

V1 = b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="5"/></svg>'
V2 = b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="9"/><script>alert(1)</script></svg>'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def receipt(op, tool, arguments, result, *, goal=1, preview=None, status="confirmed", effect="write"):
    return {"id": "receipt_" + op, "operation_id": op, "tool": tool, "arguments": arguments, "effect": effect,
            "goal_revision": goal, "scope_revision": 0, "context_epoch": 0, "status": status, "result": result,
            "preview": preview or {}, "started_at": f"2026-10-13T0{goal}:00:00+00:00", "confirmed_at": f"2026-10-13T0{goal}:01:{op[-1]}0+00:00"}


def written(path, raw, op, *, goal=1, created=True, tool="ryn_file_write"):
    return receipt(op, tool, {"path": path.name}, {"path": str(path), "sha256": sha(raw), "bytes": len(raw), "written": True},
                   goal=goal, preview={"before_sha256": None if created else "previous"})


def scenario(tmp_path):
    """Two turns of real work: a revised deliverable, a helper, a moved draft, a deleted scratch file."""
    workspace = tmp_path / "workspace"
    (workspace / "final").mkdir(parents=True)
    pelican, helper, scratch = workspace / "pelican.svg", workspace / "helper.txt", workspace / "render_check.py"
    pelican.write_bytes(V2)
    helper.write_bytes(b"notes")
    (workspace / "final" / "draft.svg").write_bytes(V1)
    (workspace / "secret.txt").write_bytes(b"never produced by the task")
    outside = tmp_path / "outside.svg"
    outside.write_bytes(V1)
    receipts = [
        written(pelican, V1, "op1"),
        written(scratch, b"print(1)", "op2"),
        written(helper, b"notes", "op3"),
        written(workspace / "draft.svg", V1, "op4"),
        receipt("op5", "ryn_file_manage", {"action": "move", "path": "draft.svg", "destination": "final/draft.svg"},
                {"path": str(workspace / "draft.svg"), "destination": str(workspace / "final" / "draft.svg"), "sha256": sha(V1), "action": "move"}),
        receipt("op6", "ryn_file_manage", {"action": "delete", "path": "render_check.py"}, {"path": str(scratch), "deleted": True}),
        written(outside, V1, "op7"),  # a receipt cannot make a file outside the task's workspace an artifact
        receipt("op8", "ryn_file_write", {"path": "lost.svg"}, {"error": "agent_local_file_changed_read_again"}, status="failed"),
        receipt("op9", "ryn_render_inspect", {"path": "pelican.svg"}, {"sha256": sha(V1), "findings": ["x"]}, effect="read"),
        written(pelican, V2, "op1b", goal=2, created=False, tool="ryn_file_edit"),
    ]
    row = {"id": "task", "local_execution": {"workspace_root": str(workspace), "desktop_access": False}, "receipts": receipts,
           "result": "Updated **pelican.svg** with a white background, as in node.js demos. See also ghost.svg for the old version.",
           "messages": []}
    return row, workspace


def test_listing_comes_from_receipts_and_ranks_files_the_answer_names(tmp_path):
    row, workspace = scenario(tmp_path)
    listing = workspace_artifacts.listing(row)
    names = [(item["path"], item["role"], item["status"]) for item in listing["files"]]
    assert names == [("pelican.svg", "deliverable", "current"),
                     ("final/draft.svg", "supporting", "current"),
                     ("helper.txt", "supporting", "current")]
    pelican = listing["files"][0]
    assert pelican["media_type"] == "image/svg+xml" and pelican["preview"] == "image"
    assert pelican["revisions"] == 2 and pelican["goal_revisions"] == [1, 2] and pelican["sha256"] == sha(V2)
    assert pelican["created_by_task"] is True and pelican["mentioned_in_answer"] is True
    # The inspection covered version 1 only, so the current version is not marked as checked.
    assert "render_inspected" not in pelican
    # Named in prose without any receipt: reported, never presented as a file.
    assert listing["unbacked_mentions"] == []
    assert all(item["name"] not in {"secret.txt", "outside.svg", "render_check.py", "lost.svg"} for item in listing["files"])


def test_one_path_keeps_one_link_across_revisions_and_outside_changes_are_flagged(tmp_path):
    row, workspace = scenario(tmp_path)
    first = workspace_artifacts.listing({**row, "receipts": row["receipts"][:1]})["files"][0]
    latest = workspace_artifacts.listing(row)["files"][0]
    assert first["id"] == latest["id"] == workspace_artifacts.artifact_id(workspace / "pelican.svg")
    row["receipts"].append(receipt("op9b", "ryn_render_inspect", {"path": "pelican.svg"}, {"sha256": sha(V2), "findings": []}, goal=2, effect="read"))
    assert workspace_artifacts.listing(row)["files"][0]["render_inspected"]["findings"] == 0
    (workspace / "pelican.svg").write_bytes(b"<svg/>")
    changed = workspace_artifacts.listing(row)["files"][0]
    assert changed["status"] == "changed_outside_task" and "render_inspected" not in changed
    assert workspace_artifacts.read(row, changed["id"])["status"] == "changed_outside_task"
    (workspace / "pelican.svg").unlink()
    missing = next(item for item in workspace_artifacts.listing(row)["files"] if item["name"] == "pelican.svg")
    assert missing["status"] == "missing" and missing["role"] == "supporting"


def test_without_a_named_file_every_present_task_file_is_a_result(tmp_path):
    row, _ = scenario(tmp_path)
    row["result"] = "Done."
    roles = {item["path"]: item["role"] for item in workspace_artifacts.listing(row)["files"]}
    assert set(roles.values()) == {"deliverable"}


def test_file_route_serves_the_current_version_as_inert_bytes(tmp_path):
    row, workspace = scenario(tmp_path)
    app = FastAPI()
    runtime = install_agent(app, store=object(), home=tmp_path / "home", workers=BackgroundWorkerRegistry(),
                            local_control=lambda _: None, messaging_key=X25519PrivateKey.generate(),
                            integrations=(Model(), Tools(), Catalog()))
    runtime.update_settings({"enabled": True, "model_id": "local:test"})
    task = runtime.create({"message": "Draw a pelican"})
    runtime.store.transaction(lambda data: data["tasks"][task["id"]].update(
        receipts=row["receipts"], local_execution=row["local_execution"], result=row["result"]))
    with TestClient(app) as client:
        listing = client.get(f"/api/local/agent/tasks/{task['id']}/artifacts").json()
        assert listing["artifacts"] == [] and listing["files"][0]["name"] == "pelican.svg"
        file_id = listing["files"][0]["id"]
        response = client.get(f"/api/local/agent/tasks/{task['id']}/files/{file_id}?v={sha(V2)}")
        assert response.status_code == 200 and response.content == V2
        assert response.headers["content-type"].startswith("image/svg+xml")
        policy = response.headers["content-security-policy"]
        assert "default-src 'none'" in policy and "sandbox" in policy and "script" not in policy
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["etag"] == f'"{sha(V2)}"' and response.headers["cache-control"] == "no-store"
        (workspace / "pelican.svg").write_bytes(V1)
        stale = client.get(f"/api/local/agent/tasks/{task['id']}/files/{file_id}?v={sha(V2)}")
        assert stale.status_code == 409 and stale.json()["detail"] == "agent_artifact_content_changed"
        # An id the receipts never produced, even for a real workspace file, is not readable.
        forged = workspace_artifacts.artifact_id(workspace / "secret.txt")
        refused = client.get(f"/api/local/agent/tasks/{task['id']}/files/{forged}")
        assert refused.status_code in {404, 409} and refused.json()["detail"] == "agent_artifact_not_found"


def test_completion_review_flags_visual_files_not_inspected_in_their_current_version(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "Draw"})
    root = str(tmp_path)
    tools.change_review = lambda task_id, workspace_root: {"files": [
        {"path": "art.svg", "status": "created", "sha256": "current"}, {"path": "tool.py", "status": "created", "sha256": "x"}],
        "files_total": 2, "digest": "d"}
    write = receipt("op1", "ryn_file_write", {"path": "art.svg"}, {"path": "art.svg", "sha256": "current", "written": True})
    stale = receipt("op2", "ryn_render_inspect", {"path": "art.svg"}, {"sha256": "older"}, effect="read")
    row = {**runtime.store.read()["tasks"][task["id"]], "receipts": [write, stale], "local_execution": {"workspace_root": root}}
    review = runtime._change_review(row)
    assert review["files"][0]["render_inspected"] is False and "render_inspected" not in review["files"][1]
    fresh = receipt("op3", "ryn_render_inspect", {"path": "art.svg"}, {"sha256": "current"}, effect="read")
    assert runtime._change_review({**row, "receipts": [write, stale, fresh]})["files"][0]["render_inspected"] is True


def test_named_existing_file_is_openable_without_claiming_creation(tmp_path):
    path = tmp_path / "验收 记录.md"
    path.write_bytes(b"existing record")
    read = receipt("op1", "ryn_file_read", {"path": path.name}, {"path": str(path), "sha256": sha(path.read_bytes())}, effect="read")
    row = {"local_execution": {"workspace_root": str(tmp_path)}, "receipts": [read],
           "result": f"Confirmed [{path.name}](<{path}>)", "messages": []}
    listing = workspace_artifacts.listing(row)
    item = listing["files"][0]
    assert item["source"] == "read" and item["created_by_task"] is False and item["revisions"] == 0
    assert item["absolute_path"] == str(path) and item["status"] == "current"
    assert listing["unbacked_mentions"] == []
    assert workspace_artifacts.read(row, item["id"])["bytes"] == b"existing record"
    row["result"] = "Done."
    assert workspace_artifacts.listing(row)["files"] == []


def test_command_manifest_tracks_binary_deliverables_and_survives_repeated_process_reads(tmp_path):
    before = workspace_artifacts.snapshot(tmp_path)
    path = tmp_path / "机械板块（预估）-合并 汇总.xlsx"
    path.write_bytes(b"PK\x03\x04binary workbook")
    files = workspace_artifacts.command_files(tmp_path, before)
    assert len(files) == 1 and files[0]["created"] and files[0]["sha256"] == sha(path.read_bytes())
    command = receipt("op1", "ryn_shell_execute", {}, {"running": False, "exit_code": 0, "workspace_files": files})
    repeat = receipt("op2", "ryn_process_read", {}, {"running": False, "workspace_files": files}, effect="read")
    row = {"local_execution": {"workspace_root": str(tmp_path)}, "receipts": [command, repeat], "result": path.name, "messages": []}
    listing = workspace_artifacts.listing(row)
    item = listing["files"][0]
    assert item["source"] == "command" and item["created_by_task"] is False and item["revisions"] == 1
    assert item["attribution"] == "observed_during_command"
    assert item["media_type"].endswith("spreadsheetml.sheet") and listing["unbacked_mentions"] == []
    assert workspace_artifacts.read(row, item["id"])["bytes"] == path.read_bytes()
    assert workspace_artifacts.command_files(tmp_path, {"files": {}, "complete": False}) == []


def test_artifacts_survive_encrypted_large_command_output(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "Create a report"})
    row = runtime.store.read()["tasks"][task["id"]]
    path = Path(row["local_execution"]["workspace_root"]) / "report.xlsx"
    path.write_bytes(b"binary report")
    value = {"workspace_files": [{"path": str(path), "sha256": sha(path.read_bytes()), "created": True}], "output": "x" * 30000}
    reference = results.save(runtime.store, row, {"name": "ryn_shell_execute", "operation_id": "op1"}, value)
    command = receipt("op1", "ryn_shell_execute", {}, {"result_ref": reference["id"]})
    runtime.store.transaction(lambda data: data["tasks"][task["id"]].update(
        receipts=[command], result_refs=[reference], result="Saved report.xlsx"))
    item = runtime.artifacts(task["id"])["files"][0]
    assert item["name"] == "report.xlsx" and item["source"] == "command"
    assert runtime.read_file_artifact(task["id"], item["id"])["bytes"] == b"binary report"
    assert runtime.store.read()["tasks"][task["id"]]["receipts"][0]["result"] == {"result_ref": reference["id"]}


def test_concurrent_project_file_change_is_not_claimed_as_a_task_deliverable(tmp_path):
    path = tmp_path / "other-editor.ts"
    path.write_text("before", encoding="utf-8")
    before = workspace_artifacts.snapshot(tmp_path)
    path.write_text("changed by another editor", encoding="utf-8")
    files = workspace_artifacts.command_files(tmp_path, before)
    row = {"project": {"id": "project"}, "local_execution": {"workspace_root": str(tmp_path)},
           "receipts": [receipt("op1", "ryn_shell_execute", {}, {"workspace_files": files})],
           "result": "Tests finished.", "messages": []}
    assert files and workspace_artifacts.listing(row)["files"] == []
    row["result"] = "Observed other-editor.ts"
    observed = workspace_artifacts.listing(row)["files"][0]
    assert observed["created_by_task"] is False and observed["attribution"] == "observed_during_command"


def test_owner_open_and_reveal_resolve_bound_file_and_reject_forged_ids(tmp_path, monkeypatch):
    row, workspace = scenario(tmp_path)
    opened = []
    monkeypatch.setattr(os, "startfile", lambda path: opened.append(path), raising=False)
    monkeypatch.setattr("subprocess.Popen", lambda args, **kwargs: opened.append(args))
    ident = workspace_artifacts.artifact_id(workspace / "pelican.svg")
    assert workspace_artifacts.open_file(row, ident, "open")["opened"]
    assert workspace_artifacts.open_file(row, ident, "reveal")["opened"]
    assert len(opened) == 2 and all(str(workspace / "pelican.svg") in (" ".join(value) if isinstance(value, list) else value) for value in opened)
    with pytest.raises(AgentError, match="agent_artifact_not_found"):
        workspace_artifacts.open_file(row, workspace_artifacts.artifact_id(workspace / "secret.txt"), "open")
    with pytest.raises(AgentError, match="agent_artifact_action_invalid"):
        workspace_artifacts.open_file(row, ident, "delete")


def test_confirmed_reference_survives_followup_and_compacted_messages(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    task = runtime.create({"message": "Check an existing report"})
    row = runtime.store.read()["tasks"][task["id"]]
    path = Path(row["local_execution"]["workspace_root"]) / "existing.svg"
    path.write_bytes(V1)
    observed = receipt("op1", "ryn_file_read", {}, {"path": str(path), "sha256": sha(V1)}, effect="read")
    runtime.store.transaction(lambda data: data["tasks"][task["id"]].update(receipts=[observed], result="Confirmed existing.svg"))
    file = runtime.artifacts(task["id"])["files"][0]
    runtime.store.transaction(lambda data: data["tasks"][task["id"]].update(result="You are welcome.", messages=[]))
    assert runtime.artifacts(task["id"])["files"][0]["id"] == file["id"]
    assert runtime.read_file_artifact(task["id"], file["id"])["bytes"] == V1


def test_command_output_over_preview_limit_is_listed_instead_of_dropped(tmp_path):
    before = workspace_artifacts.snapshot(tmp_path)
    path = tmp_path / "large.xlsx"
    with path.open("wb") as stream:
        stream.seek(workspace_artifacts.MAX_ARTIFACT_BYTES)
        stream.write(b"x")
    files = workspace_artifacts.command_files(tmp_path, before)
    assert files[0]["path"] == str(path) and files[0]["sha256"] is None
    row = {"local_execution": {"workspace_root": str(tmp_path)}, "receipts": [
        receipt("op1", "ryn_shell_execute", {}, {"workspace_files": files})], "result": "Saved large.xlsx", "messages": []}
    item = workspace_artifacts.listing(row)["files"][0]
    assert item["status"] == "too_large" and item["role"] == "deliverable"
    assert workspace_artifacts.location(row, item["id"]) == path


def test_temporarily_locked_output_is_retained_then_becomes_openable(tmp_path, monkeypatch):
    before = workspace_artifacts.snapshot(tmp_path)
    path = tmp_path / "held.xlsx"
    path.write_bytes(b"workbook")
    original = workspace_artifacts._digest
    monkeypatch.setattr(workspace_artifacts, "_digest", lambda _: (_ for _ in ()).throw(PermissionError("file held by another app")))
    files = workspace_artifacts.command_files(tmp_path, before)
    assert files[0]["path"] == str(path)
    row = {"local_execution": {"workspace_root": str(tmp_path)}, "receipts": [
        receipt("op1", "ryn_shell_execute", {}, {"workspace_files": files})], "result": "Saved held.xlsx", "messages": []}
    item = workspace_artifacts.listing(row)["files"][0]
    assert item["status"] == "unavailable"
    monkeypatch.setattr(workspace_artifacts, "_digest", original)
    available = workspace_artifacts.listing(row)["files"][0]
    assert available["id"] == item["id"] and available["status"] == "unverified"
    assert workspace_artifacts.read(row, item["id"])["bytes"] == b"workbook"


def test_unmentioned_shell_scratch_files_are_supporting_until_named_in_answer(tmp_path):
    before = workspace_artifacts.snapshot(tmp_path)
    brief = tmp_path / "skills_brief.txt"
    listing_json = tmp_path / "skills_list.json"
    brief.write_text("brief", encoding="utf-8")
    listing_json.write_text("[]", encoding="utf-8")
    files = workspace_artifacts.command_files(tmp_path, before)
    row = {"local_execution": {"workspace_root": str(tmp_path)},
           "receipts": [receipt("op1", "ryn_shell_execute", {}, {"workspace_files": files})],
           "result": "", "messages": []}
    roles = {item["name"]: item["role"] for item in workspace_artifacts.listing(row)["files"]}
    assert roles == {"skills_brief.txt": "supporting", "skills_list.json": "supporting"}
    row["result"] = "See skills_brief.txt for the summary."
    roles_after = {item["name"]: item["role"] for item in workspace_artifacts.listing(row)["files"]}
    assert roles_after == {"skills_brief.txt": "deliverable", "skills_list.json": "supporting"}


def test_helper_scripts_stay_supporting_and_turn_deliverables_survive_multi_turn(tmp_path):
    attach_dir = tmp_path / ".ryn" / "attachments" / "task-1"
    attach_dir.mkdir(parents=True)
    (attach_dir / "Payoneer.pdf").write_bytes(b"%PDF-1.4 input")
    before = workspace_artifacts.snapshot(tmp_path)
    probe_py = tmp_path / "geom_probe2.py"
    probe_json = tmp_path / "verify_payoneer.json"
    out_pdf = tmp_path / "Payoneer_fixed.pdf"
    probe_py.write_text("import fitz", encoding="utf-8")
    probe_json.write_text('{"ok": true}', encoding="utf-8")
    out_pdf.write_bytes(b"%PDF-1.4 fixed")
    files = workspace_artifacts.command_files(tmp_path, before)
    row = {
        "local_execution": {"workspace_root": str(tmp_path)},
        "receipts": [
            receipt("op_py", "ryn_file_write", {"path": str(probe_py)}, {"path": str(probe_py), "sha256": sha(probe_py.read_bytes())}),
            receipt("op_cmd", "ryn_shell_execute", {}, {"running": False, "exit_code": 0, "workspace_files": files}),
        ],
        "messages": [
            {"role": "user", "content": "Fix Payoneer.pdf"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "ryn_file_write"}}, {"id": "c2", "function": {"name": "ryn_shell_execute"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "{}"},
            {"role": "tool", "tool_call_id": "c2", "content": "{}"},
            {"role": "assistant", "content": "Generated Payoneer_fixed.pdf (verified via geom_probe2.py and verify_payoneer.json)."},
            {"role": "user", "content": "Why did the other files show up?"},
            {"role": "assistant", "content": "Those were helper scripts used during verification."},
        ],
        "events": [
            {"seq": 1, "type": "tool_result", "call_id": "c1", "operation_id": "op_py"},
            {"seq": 2, "type": "tool_result", "call_id": "c2", "operation_id": "op_cmd"},
        ],
        "result": "Those were helper scripts used during verification.",
    }
    listed = {item["name"]: item for item in workspace_artifacts.listing(row)["files"]}
    assert "Payoneer.pdf" not in listed
    assert listed["Payoneer_fixed.pdf"]["role"] == "deliverable"
    assert listed["Payoneer_fixed.pdf"]["mentioned_in_answer"] is True
    assert listed["Payoneer_fixed.pdf"]["turn_index"] == 0
    assert listed["geom_probe2.py"]["role"] == "supporting"
    assert listed["verify_payoneer.json"]["role"] == "supporting"


def test_paused_followup_turn_does_not_leak_previous_turn_final_text_or_unbacked_mentions(tmp_path):
    budget_xlsx = tmp_path / "qa_budget.xlsx"
    budget_xlsx.write_bytes(b"PK\x03\x04workbook")
    row = {
        "local_execution": {"workspace_root": str(tmp_path)},
        "receipts": [
            receipt("op_xlsx", "ryn_shell_execute", {}, {
                "running": False, "exit_code": 0,
                "workspace_files": [{"path": str(budget_xlsx), "sha256": sha(budget_xlsx.read_bytes()), "created": True}],
            }),
        ],
        "messages": [
            {"role": "user", "content": "Create qa_budget.xlsx or qa_memo.docx"},
            {"role": "assistant", "content": "仅完成了 Excel 分支 qa_budget.xlsx，未额外创建 qa_memo.docx（任务中为“或”关系）"},
            {"role": "user", "content": "这个表格，能存到我的百度网盘吗？创建一个ryn文件夹，存进去"},
        ],
        "events": [],
        "result": "",
    }
    assert workspace_artifacts.final_text(row) == ""
    assert workspace_artifacts._turn_answers(row) == {
        0: "仅完成了 Excel 分支 qa_budget.xlsx，未额外创建 qa_memo.docx（任务中为“或”关系）",
    }
    listed = workspace_artifacts.listing(row)
    assert listed["unbacked_mentions"] == []
    assert [f["name"] for f in listed["files"]] == ["qa_budget.xlsx"]
    assert listed["files"][0]["turn_index"] == 0



@pytest.mark.skipif(os.name != "nt", reason="Windows Explorer command-line parsing")
def test_reveal_quotes_only_the_path_with_spaces_commas_and_unicode(tmp_path, monkeypatch):
    target = tmp_path / "中文 report, final.pdf"
    target.write_bytes(b"pdf")
    monkeypatch.setattr(workspace_artifacts, "location", lambda row, file_id: target)
    calls = []
    monkeypatch.setattr("subprocess.Popen", lambda args, **kwargs: calls.append((args, kwargs)))
    workspace_artifacts.open_file({}, "bound-file", "reveal")
    assert calls == [(f'explorer.exe /select,"{target}"', {"creationflags": 0x08000000})]
