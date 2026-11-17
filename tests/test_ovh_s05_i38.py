"""S05-I38: honest bounded excerpts, fair allocation and task-owned cleanup."""
import base64
import io
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import zipfile

import pytest
from test_agent_integrations import node
from test_agent_runtime import advance, assistant, make_runtime
from test_agent_runtime_lifecycle import change

from rynmesh.agent_runtime import input_attachments as attachments, lifecycle
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime


def upload(name, raw):
    return {"name": name, "data_base64": base64.b64encode(raw).decode()}


def attach(tmp_path, uploaded):
    store = SimpleNamespace(root=tmp_path / "store", default_workspace=lambda: str(tmp_path / "workspace"))
    return attachments.attach_to_message(store, {"id": "agent_fixture"},
        {"role": "user", "content": "Read the files"}, attachments.parse_attachments(uploaded))


def test_t1_real_model_receives_truncation_notice_and_owner_text_stays_clean(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Read the excerpt"))
    tid = runtime.create({"message": "Read fixture", "attachments": [upload("large.txt", b"a" * 30000)]})["id"]
    message = runtime.store.read()["tasks"][tid]["messages"][0]
    assert "Excerpt truncated: showing 12000 of 30000 characters" in message["attachment_excerpts"][0]["text"]
    assert "Excerpt truncated" not in message["content"]
    advance(runtime)
    assert any("truncated: showing 12000 of 30000" in str(item["content"]) for item in model.calls[0][1])


def excerpt_body(entry):
    return entry["text"].split("\nExcerpt truncated:", 1)[0]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "gb18030"])
def test_t1_text_counts_entire_decoded_file_and_marks_truncation(tmp_path, encoding):
    text = "中文a" * 10000
    raw = text.encode(encoding)
    result = attachments._excerpt("large.txt", "text/plain", raw)
    assert isinstance(result, tuple) and len(result) == 2
    excerpt, meta = result
    assert excerpt == text[:12000]
    assert meta == {"truncated": True, "shown_chars": 12000, "total_chars": 30000,
                    "pages_shown": None, "total_pages": None}
    message = attach(tmp_path, [upload("large.txt", raw)])
    entry = message["attachment_excerpts"][0]
    assert "truncated: showing 12000 of 30000 characters" in entry["text"]
    assert entry["text"].endswith("Read the rest with the local file tools using the path above.")
    assert text[:100] not in message["content"]
    assert entry["path"] in attachments.provider_content(message)


@pytest.mark.parametrize("lengths,expected", [([20000, 20000], [6000, 6000]),
    ([100, 20000, 20000], [100, 7900, 4000]), ([20000] * 8, [1500] * 8)])
def test_t2_each_excerpt_gets_a_floor_then_leftovers_in_order(tmp_path, lengths, expected):
    message = attach(tmp_path, [upload(f"{i}.txt", chr(65 + i).encode() * size)
                                for i, size in enumerate(lengths)])
    entries = message["attachment_excerpts"]
    assert [len(excerpt_body(entry)) for entry in entries] == expected
    assert sum(map(len, map(excerpt_body, entries))) == attachments.MAX_EXCERPT
    for i, (entry, size) in enumerate(zip(entries, lengths)):
        assert excerpt_body(entry) == chr(65 + i) * expected[i]
        if size > expected[i]:
            assert f"showing {expected[i]} of {size} characters" in entry["text"]


def test_t2_unsupported_attachments_do_not_starve_text_or_claim_truncation(tmp_path):
    message = attach(tmp_path, [upload("opaque.bin", b"\x00\xff"), upload("short.txt", b"whole text")])
    assert len(message["attachment_excerpts"]) == 1
    assert message["attachment_excerpts"][0]["text"] == "whole text"


def pdf_bytes():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    for index in range(20):
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
            DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 10 10 Td (Page_{index + 1:02d}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_t3_pdf_reports_actual_page_coverage(tmp_path):
    raw = pdf_bytes()
    result = attachments._excerpt("pages.pdf", "application/pdf", raw)
    assert isinstance(result, tuple)
    text, meta = result
    assert meta["truncated"] and meta["pages_shown"] == 12 and meta["total_pages"] == 20
    assert "Page_01" in text and "Page_12" in text and "Page_13" not in text
    message = attach(tmp_path, [upload("pages.pdf", raw)])
    shown = message["attachment_excerpts"][0]["text"]
    assert "pages 1-12 of 20" in shown and "Excerpt truncated:" in shown


def workbook_bytes():
    from xml.sax.saxutils import escape
    sheets = [f'<row r="{i}"><c r="A{i}" t="inlineStr"><is><t>{escape("value" + str(i))}</t></is></c></row>'
              for i in range(1, 206)]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            '<sheet name="Visible" sheetId="1" r:id="rId1"/><sheet name="Hidden" state="hidden" sheetId="2" r:id="rId2"/>'
            '</sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        for n in (1, 2):
            archive.writestr(f"xl/worksheets/sheet{n}.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<dimension ref="A1:AR205"/><sheetData>' + "".join(sheets) + '</sheetData></worksheet>')
    return out.getvalue()


def test_t4_workbook_excerpt_retains_totals_hidden_and_truncated(tmp_path):
    message = attach(tmp_path, [upload("book.xlsx", workbook_bytes())])
    text = message["attachment_excerpts"][0]["text"]
    assert "Sheet: Visible (共 205 行 × 44 列，显示前 200 行 40 列；已截断)" in text
    assert "Sheet: Hidden (共 205 行 × 44 列，显示前 200 行 40 列；隐藏表；已截断)" in text


def test_t4_other_preview_kinds_and_optional_metadata():
    assert attachments._flatten_preview({"kind": "presentation", "total_slides": 20,
        "slides": [{"number": 1, "paragraphs": ["First slide"]}]}) == "Presentation: 20 slides\nSlide 1\nFirst slide"
    assert attachments._flatten_preview({"kind": "document", "truncated": True,
        "blocks": [{"type": "paragraph", "runs": [{"text": "Document text"}]}]}).endswith("文档已截断")
    assert attachments._flatten_preview({"kind": "spreadsheet", "sheets": [{"name": "Old", "rows": []}]}) == "Sheet: Old (共 0 行 × 0 列，显示前 0 行 0 列)"


def test_t5_delete_real_runtime_cleans_own_files_and_keeps_other_task(node, tmp_path):
    app, server_store = node
    tools = build_agent_integrations(app, server_store, server_store.home)[1]
    runtime, _, _, _ = make_runtime(tmp_path / "runtime", assistant("Done"), assistant("Done"))
    ids = [runtime.create({"message": "Read fixture", "attachments": [upload("note.txt", b"fixture")]})["id"] for _ in range(2)]
    advance(runtime, 2)
    before = runtime.store.read()
    paths = [Path(before["tasks"][tid]["messages"][0]["attachments"][0]["path"]) for tid in ids]
    runtime = AgentRuntime(runtime.store, runtime.model, tools, runtime.catalog)
    deleted = change(runtime, ids[0], "delete")
    assert deleted["deleted"] and not deleted["cleanup_pending"]
    tombstone = runtime.store.read()["deleted_tasks"][ids[0]]
    assert tombstone["attachment_dirs"] == [str(paths[0].parent)]
    assert not paths[0].parent.exists() and paths[1].is_file()
    assert change(runtime, ids[1], "delete")["deleted"]
    assert not paths[1].parent.parent.exists()


def test_t5_tombstone_deduplicates_message_directories_and_caps_eight(tmp_path):
    dirs = [tmp_path / str(i) / ".ryn" / "attachments" / "agent_fixture" for i in range(10)]
    row = {"id": "agent_fixture", "control_revision": 1, "messages": [
        {"attachments": [{"path": str(d / "a.txt")}, {"path": str(d / "b.txt")}]} for d in dirs]}
    data = {"tasks": {row["id"]: row}}
    lifecycle.delete_rows_in_tx(data, [row], lambda: "now")
    assert data["deleted_tasks"][row["id"]]["attachment_dirs"] == list(map(str, dirs[:8]))


def make_dir(path):
    path.mkdir(parents=True)
    (path / "keep.txt").write_text("fixture")
    return path


def link_dir(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        assert os.name == "nt" and exc.winerror == 1314
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


@pytest.mark.parametrize("link_at", ["task", "attachments", "ryn", "workspace"])
def test_t5_cleanup_never_follows_directory_or_parent_links(tmp_path, link_at):
    target = make_dir(tmp_path / "target")
    parts = ["workspace", ".ryn", "attachments", "agent_fixture"]
    position = {"workspace": 0, "ryn": 1, "attachments": 2, "task": 3}[link_at]
    link = tmp_path.joinpath(*parts[:position + 1])
    link.parent.mkdir(parents=True, exist_ok=True)
    link_dir(link, target)
    directory = tmp_path.joinpath(*parts)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "a.txt").write_text("attached")
    attachments.forget_dirs([str(directory)])
    assert (target / "keep.txt").is_file() and (directory / "a.txt").is_file()


def test_t5_cleanup_ignores_nonmatching_paths_and_preserves_link_targets(tmp_path):
    wrong = make_dir(tmp_path / "attachments" / "agent_fixture")
    ordinary = make_dir(tmp_path / "ordinary")
    target = make_dir(tmp_path / "target")
    owned = make_dir(tmp_path / "workspace" / ".ryn" / "attachments" / "agent_fixture")
    link_dir(owned / "child-link", target)
    attachments.forget_dirs([str(wrong), str(ordinary), str(owned)])
    assert wrong.is_dir() and ordinary.is_dir() and (target / "keep.txt").is_file()
    assert not owned.exists()


def test_t5_cleanup_failure_does_not_block_other_directories(tmp_path, monkeypatch):
    first = make_dir(tmp_path / "a" / ".ryn" / "attachments" / "agent_fixture")
    second = make_dir(tmp_path / "b" / ".ryn" / "attachments" / "agent_fixture")
    import shutil
    real = shutil.rmtree
    def remove(path, *args, **kwargs):
        if Path(path) == first:
            raise PermissionError("fixture locked file")
        return real(path, *args, **kwargs)
    monkeypatch.setattr(shutil, "rmtree", remove)
    attachments.forget_dirs([str(first), str(second)])
    assert first.is_dir() and not second.exists()


def test_t5_registry_refuses_other_task_directory(node, tmp_path):
    app, store = node
    tools = build_agent_integrations(app, store, store.home)[1]
    runtime, model, _, _ = make_runtime(tmp_path / "agent-store")
    AgentRuntime(runtime.store, model, tools, runtime.catalog)
    own = make_dir(tmp_path / ".ryn" / "attachments" / "agent_fixture")
    other = make_dir(own.parent / "agent_other")
    tools.forget_task("agent_fixture", operation_ids=[], attachment_dirs=[str(other), str(own)])
    assert not own.exists() and other.is_dir()


def test_t5_old_tombstone_without_attachment_dirs_uses_legacy_cleanup(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    tid = "agent_" + "a" * 32
    runtime.store.transaction(lambda data: data.setdefault("deleted_tasks", {}).update({tid: {
        "id": tid, "operation_ids": ["old-operation"], "cleanup_pending": True}}))
    calls = []
    def forget(task_id, *, operation_ids):
        calls.append((task_id, operation_ids))
    runtime.tools.forget_task = forget
    assert runtime._cleanup_deleted_tasks([tid]) is False
    assert calls == [(tid, ["old-operation"])]
    assert "attachment_dirs" not in runtime.store.read()["deleted_tasks"][tid]
