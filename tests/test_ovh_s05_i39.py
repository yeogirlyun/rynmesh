"""Acceptance tests for S05-I39: pure-Python .xls reading, legacy Office conversion timeout & cache, 202 polling route, attachment excerpts, and table_work .xls support."""
from __future__ import annotations

import base64
import io
import os
import struct
import subprocess
import threading
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.agent_runtime import file_preview, table_work
from rynmesh.agent_runtime.file_preview import PreviewError, preview_file
from rynmesh.agent_runtime.input_attachments import attach_to_message, parse_attachments
from rynmesh.agent_runtime.routes import install_agent
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.agent_runtime.table_work import calculate
from rynmesh.background_workers import BackgroundWorkerRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_XLS = Path(__file__).resolve().parent / "data" / "sample_legacy.xls"

S_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R_NS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _ooxml_archive(entries: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _sample_equivalent_xlsx() -> bytes:
    styles = (
        f"<styleSheet {S_NS}>"
        '<cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs>'
        "</styleSheet>"
    )
    sheet1 = (
        f'<worksheet {S_NS}><dimension ref="A1:D3"/><sheetData>'
        '<row r="1">'
        '<c r="A1" t="inlineStr"><is><t>门店</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>销量</t></is></c>'
        '<c r="C1" t="inlineStr"><is><t>日期</t></is></c>'
        '<c r="D1" t="inlineStr"><is><t>小计</t></is></c>'
        "</row>"
        '<row r="2">'
        '<c r="A2" t="inlineStr"><is><t>上海门店</t></is></c>'
        '<c r="B2"><v>120</v></c>'
        '<c r="C2" s="1"><v>45292</v></c>'
        '<c r="D2"><v>240</v></c>'
        "</row>"
        '<row r="3">'
        '<c r="A3" t="inlineStr"><is><t>北京门店</t></is></c>'
        '<c r="B3"><v>85.5</v></c>'
        '<c r="C3" s="1"><v>45306</v></c>'
        '<c r="D3"><v>171</v></c>'
        "</row>"
        "</sheetData></worksheet>"
    )
    sheet2 = (
        f'<worksheet {S_NS}><dimension ref="A1:D2"/><sheetData>'
        '<row r="1">'
        '<c r="A1" t="inlineStr"><is><t>门店</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>销量</t></is></c>'
        '<c r="C1" t="inlineStr"><is><t>日期</t></is></c>'
        '<c r="D1" t="inlineStr"><is><t>小计</t></is></c>'
        "</row>"
        '<row r="2">'
        '<c r="A2" t="inlineStr"><is><t>深圳门店</t></is></c>'
        '<c r="B2"><v>200</v></c>'
        '<c r="C2" s="1"><v>45383</v></c>'
        '<c r="D2"><v>400</v></c>'
        "</row>"
        "</sheetData></worksheet>"
    )
    return _ooxml_archive({
        "xl/workbook.xml": (
            f"<workbook {S_NS} {R_NS}><sheets>"
            '<sheet name="销售" sheetId="1" r:id="r1"/>'
            '<sheet name="二季度" sheetId="2" state="hidden" r:id="r2"/>'
            "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            "<Relationships>"
            '<Relationship Id="r1" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="r2" Target="worksheets/sheet2.xml"/>'
            "</Relationships>"
        ),
        "xl/styles.xml": styles,
        "xl/worksheets/sheet1.xml": sheet1,
        "xl/worksheets/sheet2.xml": sheet2,
    })


def _simple_docx(text: str) -> bytes:
    return _ooxml_archive({
        "word/document.xml": (
            f"<w:document {W_NS}><w:body>"
            f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
            "</w:body></w:document>"
        )
    })


def _make_cfb(workbook_stream: bytes) -> bytes:
    sector_size = 512
    stream_len = len(workbook_stream)
    padded_len = max(4096, ((stream_len + sector_size - 1) // sector_size) * sector_size)
    data_bytes = workbook_stream.ljust(padded_len, b"\x00")
    num_data_sectors = len(data_bytes) // sector_size
    fat_sec_id = 1 + num_data_sectors
    header = bytearray(512)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<H", header, 24, 0x003E)
    struct.pack_into("<H", header, 26, 0x0003)
    struct.pack_into("<H", header, 28, 0xFFFE)
    struct.pack_into("<H", header, 30, 9)
    struct.pack_into("<H", header, 32, 6)
    struct.pack_into("<I", header, 44, 1)
    struct.pack_into("<I", header, 48, 0)
    struct.pack_into("<I", header, 56, 0x1000)
    struct.pack_into("<i", header, 60, -2)
    struct.pack_into("<I", header, 64, 0)
    struct.pack_into("<i", header, 68, -2)
    struct.pack_into("<I", header, 72, 0)
    struct.pack_into("<i", header, 76, fat_sec_id)
    for i in range(1, 109):
        struct.pack_into("<i", header, 76 + i * 4, -1)

    def dir_entry(name: str, obj_type: int, child_id: int, start_sec: int, size: int) -> bytes:
        ent = bytearray(128)
        if name:
            nb = (name + "\x00").encode("utf-16le")
            ent[0 : len(nb)] = nb
            struct.pack_into("<H", ent, 64, len(nb))
        ent[66] = obj_type
        ent[67] = 1
        struct.pack_into("<i", ent, 68, -1)
        struct.pack_into("<i", ent, 72, -1)
        struct.pack_into("<i", ent, 76, child_id)
        struct.pack_into("<i", ent, 116, start_sec)
        struct.pack_into("<I", ent, 120, size)
        return bytes(ent)

    dir_sec = (
        dir_entry("Root Entry", 5, 1, -2, 0)
        + dir_entry("Workbook", 2, -1, 1, max(4096, stream_len))
        + dir_entry("", 0, -1, -2, 0)
        + dir_entry("", 0, -1, -2, 0)
    )
    fat = [-1] * 128
    fat[0] = -2
    for i in range(num_data_sectors):
        sec = 1 + i
        fat[sec] = (sec + 1) if i + 1 < num_data_sectors else -2
    fat[fat_sec_id] = -3
    return bytes(header) + dir_sec + data_bytes + struct.pack("<128i", *fat)


def _encrypted_biff8_xls() -> bytes:
    def rec(rid: int, payload: bytes = b"") -> bytes:
        return struct.pack("<HH", rid, len(payload)) + payload

    bof = rec(0x0809, struct.pack("<HHHHII", 0x0600, 0x0005, 0x0DBB, 1996, 0, 6))
    filepass = rec(0x002F, b"\x01\x00\x01\x00" + b"\x00" * 48)
    eof = rec(0x000A)
    return _make_cfb(bof + filepass + eof)


def _dup_header_xls() -> bytes:
    def rec(rid: int, payload: bytes = b"") -> bytes:
        return struct.pack("<HH", rid, len(payload)) + payload

    def ustr16(s: str) -> bytes:
        return struct.pack("<HB", len(s), 1) + s.encode("utf-16le")

    def short_ustr(s: str) -> bytes:
        return struct.pack("<BB", len(s), 1) + s.encode("utf-16le")

    def xf_rec(fmt_idx: int, is_style: bool) -> bytes:
        flags = 0xFFF5 if is_style else 0x0001
        return rec(0x00E0, struct.pack("<HHHBBBBBBBBHI", 0, fmt_idx, flags, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0))

    bof_g = rec(0x0809, struct.pack("<HHHHII", 0x0600, 0x0005, 0x0DBB, 1996, 0, 6))
    cp = rec(0x0042, struct.pack("<H", 1200))
    dm = rec(0x0022, struct.pack("<H", 0))
    xfs = b"".join(xf_rec(0, True) for _ in range(15)) + xf_rec(0, False)
    sst = rec(0x00FC, struct.pack("<II", 1, 1) + ustr16("amount"))
    eof = rec(0x000A)
    sh = (
        rec(0x0809, struct.pack("<HHHHII", 0x0600, 0x0010, 0x0DBB, 1996, 0, 6))
        + rec(0x0200, struct.pack("<IIHHH", 0, 2, 0, 2, 0))
        + rec(0x00FD, struct.pack("<HHHI", 0, 0, 15, 0))
        + rec(0x00FD, struct.pack("<HHHI", 0, 1, 15, 0))
        + rec(0x0203, struct.pack("<HHHd", 1, 0, 15, 10.0))
        + rec(0x0203, struct.pack("<HHHd", 1, 1, 15, 20.0))
        + eof
    )
    bs_tail = struct.pack("<BB", 0, 0) + short_ustr("Dup")
    globals_len = len(bof_g + cp + dm + xfs) + 4 + 4 + len(bs_tail) + len(sst) + len(eof)
    bs = rec(0x0085, struct.pack("<I", globals_len) + bs_tail)
    return _make_cfb(bof_g + cp + dm + xfs + bs + sst + eof + sh)


def test_t1_t2_sample_xls_fixture_and_pyproject_dependency_match_xlsx_structure(monkeypatch):
    """T1 & T2 (Steps 1, 2): pyproject.toml bundles xlrd>=2.0.1,<3, and tests/data/sample_legacy.xls previews to the exact same structure as .xlsx without LibreOffice."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = pyproject["project"]["dependencies"]
    assert any(dep.replace(" ", "") == "xlrd>=2.0.1,<3" for dep in deps), deps

    assert SAMPLE_XLS.is_file()
    raw_xls = SAMPLE_XLS.read_bytes()

    # Forbid LibreOffice so .xls must be parsed in pure Python via _xls_spreadsheet.
    monkeypatch.setattr("rynmesh.agent_runtime.file_preview.shutil.which", lambda _: None)
    monkeypatch.setattr(
        "rynmesh.agent_runtime.file_preview.subprocess.run",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("subprocess.run must not be called for valid .xls")),
    )

    xls_direct = file_preview._xls_spreadsheet(raw_xls)
    xls_preview = preview_file("sample_legacy.xls", raw_xls)
    xlsx_preview = preview_file("sample_equivalent.xlsx", _sample_equivalent_xlsx())

    assert xls_direct == xls_preview
    assert xls_preview == xlsx_preview
    assert xls_preview["kind"] == "spreadsheet"
    assert xls_preview["formulas_uncalculated"] is False
    assert xls_preview["truncated"] is False
    assert [s["name"] for s in xls_preview["sheets"]] == ["销售", "二季度"]
    assert xls_preview["sheets"][0]["hidden"] is False
    assert xls_preview["sheets"][1]["hidden"] is True
    assert xls_preview["sheets"][0]["rows"][1]["cells"] == [
        {"column": 1, "value": "上海门店"},
        {"column": 2, "value": "120"},
        {"column": 3, "value": "2024-01-01"},
        {"column": 4, "value": "240"},
    ]


def test_t3_encrypted_or_corrupt_xls_falls_back_to_libreoffice_and_reports_conversion_failed(monkeypatch):
    """T3 (Step 2): encrypted or corrupt .xls raises XLRDError, falls back to LibreOffice, and reports agent_preview_conversion_failed on failure."""
    enc_raw = _encrypted_biff8_xls()
    corrupt_raw = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1corrupt-xls-payload"

    # Case 1: LibreOffice present, fallback invoked and fails -> agent_preview_conversion_failed
    fallback_calls: list[list[str]] = []

    def failing_soffice(cmd, **_kwargs):
        fallback_calls.append(cmd)
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("rynmesh.agent_runtime.file_preview.shutil.which", lambda _: "soffice")
    monkeypatch.setattr("rynmesh.agent_runtime.file_preview.subprocess.run", failing_soffice)

    for payload in (enc_raw, corrupt_raw):
        with pytest.raises(PreviewError) as exc_info:
            preview_file("broken.xls", payload)
        assert str(exc_info.value) == "agent_preview_conversion_failed"
    assert len(fallback_calls) == 2

    # Case 2: LibreOffice missing on host -> encrypted/corrupt .xls still reports agent_preview_conversion_failed,
    # while .doc continues to report agent_preview_converter_missing.
    monkeypatch.setattr("rynmesh.agent_runtime.file_preview.shutil.which", lambda _: None)
    monkeypatch.setattr("rynmesh.agent_runtime.file_preview.Path.is_file", lambda _: False)
    with pytest.raises(PreviewError) as enc_missing_exc:
        preview_file("encrypted.xls", enc_raw)
    assert str(enc_missing_exc.value) == "agent_preview_conversion_failed"

    with pytest.raises(PreviewError) as doc_missing_exc:
        preview_file("legacy.doc", b"legacy-doc-bytes")
    assert str(doc_missing_exc.value) == "agent_preview_converter_missing"


def test_t4_conversion_timeout_scales_by_file_size_and_caps_at_120s(monkeypatch):
    """T4 (Step 3): conversion timeout is max(20, min(120, 20 + len(raw) // (512 * 1024) * 5)) -> 1 MiB = 30s, large file = 120s."""
    assert file_preview.conversion_timeout(0) == 20
    assert file_preview.conversion_timeout(511 * 1024) == 20
    assert file_preview.conversion_timeout(1024 * 1024) == 30
    assert file_preview.conversion_timeout(5 * 1024 * 1024) == 70
    assert file_preview.conversion_timeout(20 * 1024 * 1024) == 120

    recorded_timeouts: list[int] = []
    docx_out = _simple_docx("timeout-verified")

    def fake_run(arguments, **options):
        recorded_timeouts.append(options["timeout"])
        outdir = Path(arguments[arguments.index("--outdir") + 1])
        (outdir / "document.docx").write_bytes(docx_out)

    monkeypatch.setattr("rynmesh.agent_runtime.file_preview.shutil.which", lambda _: "soffice")
    monkeypatch.setattr("rynmesh.agent_runtime.file_preview.subprocess.run", fake_run)

    preview_file("one_mib.doc", b"d" * (1024 * 1024))
    preview_file("fifteen_mib.doc", b"e" * (15 * 1024 * 1024))
    assert recorded_timeouts == [30, 120]


def test_t5_preview_cache_hits_on_identical_content_and_enforces_lru_limits(tmp_path: Path, monkeypatch):
    """T5 (Step 3): second preview of identical raw + suffix hits store.root/preview-cache/ (subprocess.run called once) and enforces 32-file / 200 MiB LRU outside workspace."""
    store_root = tmp_path / "agent_store"
    workspace = tmp_path / "user_workspace"
    workspace.mkdir()
    cache_dir = store_root / "preview-cache"

    calls: list[list[str]] = []

    def fake_run(arguments, **_options):
        calls.append(arguments)
        outdir = Path(arguments[arguments.index("--outdir") + 1])
        src = Path(arguments[-1]).read_bytes()
        (outdir / "document.docx").write_bytes(_simple_docx("缓存内容:" + src.decode("ascii", errors="ignore")[:16]))

    monkeypatch.setattr("rynmesh.agent_runtime.file_preview.shutil.which", lambda _: "soffice")
    monkeypatch.setattr("rynmesh.agent_runtime.file_preview.subprocess.run", fake_run)

    doc_bytes = b"quarter-summary-legacy-doc"
    res1 = preview_file("first_name.doc", doc_bytes, cache_dir=cache_dir)
    res2 = preview_file("renamed_copy.doc", doc_bytes, cache_dir=cache_dir)

    assert len(calls) == 1
    assert res1 == res2
    assert res1["blocks"][0]["runs"][0]["text"] == "缓存内容:quarter-summary-"
    assert cache_dir.is_dir()
    assert not cache_dir.resolve().is_relative_to(workspace.resolve())
    cached_files = [p for p in cache_dir.iterdir() if p.is_file()]
    assert len(cached_files) == 1 and cached_files[0].suffix == ".docx"

    # Verify LRU count cap (32 items): inserting 35 distinct .doc conversions retains at most 32 files.
    for idx in range(35):
        preview_file(f"batch_{idx}.doc", f"doc-payload-{idx:03d}".encode("ascii"), cache_dir=cache_dir)
        # Ensure strictly increasing mtime order for deterministic LRU pruning on fast filesystems
        newest = max(cache_dir.glob("*.docx"), key=lambda p: p.stat().st_mtime_ns)
        os.utime(newest, ns=(1_800_000_000_000_000_000 + idx * 10_000_000, 1_800_000_000_000_000_000 + idx * 10_000_000))

    remaining = [p for p in cache_dir.iterdir() if p.is_file()]
    assert len(remaining) <= 32


def test_t6_preview_route_returns_202_while_converting_and_200_once_cached(tmp_path: Path, monkeypatch):
    """T6 (Step 4): preview route immediately returns 202 {'status': 'converting'} while background LibreOffice conversion runs (deduplicated per sha), then 200 once cached."""
    from test_agent_runtime import Catalog, Model, Tools
    from test_agent_workspace_artifacts import written

    started = threading.Event()
    release = threading.Event()
    run_count = 0
    count_lock = threading.Lock()

    def slow_soffice(arguments, **_options):
        nonlocal run_count
        with count_lock:
            run_count += 1
        started.set()
        assert release.wait(timeout=10), "conversion release timed out"
        outdir = Path(arguments[arguments.index("--outdir") + 1])
        (outdir / "document.docx").write_bytes(_simple_docx("异步转换完成正文"))

    monkeypatch.setattr("rynmesh.agent_runtime.file_preview.shutil.which", lambda _: "soffice")
    monkeypatch.setattr("rynmesh.agent_runtime.file_preview.subprocess.run", slow_soffice)

    app = FastAPI()
    runtime = install_agent(
        app,
        store=object(),
        home=tmp_path / "home",
        workers=BackgroundWorkerRegistry(),
        local_control=lambda _: None,
        messaging_key=X25519PrivateKey.generate(),
        integrations=(Model(), Tools(), Catalog()),
    )
    runtime.update_settings({"enabled": True, "model_id": "local:test"})
    task = runtime.create({"message": "Preview legacy files"})
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    doc_raw = b"legacy-word-binary-content"
    doc_path = workspace / "contract.doc"
    doc_path.write_bytes(doc_raw)

    xls_raw = SAMPLE_XLS.read_bytes()
    xls_path = workspace / "sales.xls"
    xls_path.write_bytes(xls_raw)

    runtime.store.transaction(
        lambda data: data["tasks"][task["id"]].update(
            receipts=[written(doc_path, doc_raw, "op_doc"), written(xls_path, xls_raw, "op_xls")],
            local_execution={"workspace_root": str(workspace)},
            result="contract.doc sales.xls",
        )
    )

    with TestClient(app) as client:
        files = {f["name"]: f for f in client.get(f"/api/local/agent/tasks/{task['id']}/artifacts").json()["files"]}

        # Valid .xls previews synchronously via pure-Python xlrd with 200 right away.
        xls_resp = client.get(f"/api/local/agent/tasks/{task['id']}/files/{files['sales.xls']['id']}/preview?v={files['sales.xls']['sha256']}")
        assert xls_resp.status_code == 200
        assert xls_resp.json()["sheets"][0]["name"] == "销售"

        # Legacy .doc returns 202 immediately and deduplicates concurrent requests for the same sha.
        doc_url = f"/api/local/agent/tasks/{task['id']}/files/{files['contract.doc']['id']}/preview?v={files['contract.doc']['sha256']}"
        r1 = client.get(doc_url)
        assert r1.status_code == 202
        assert r1.json() == {"status": "converting"}
        assert started.wait(timeout=5)

        r2 = client.get(doc_url)
        assert r2.status_code == 202
        assert r2.json() == {"status": "converting"}
        assert run_count == 1

        release.set()
        # Poll until background thread finishes writing to runtime.store.root / "preview-cache"
        final_resp = None
        for _ in range(40):
            resp = client.get(doc_url)
            if resp.status_code == 200:
                final_resp = resp
                break
            threading.Event().wait(0.05)

        assert final_resp is not None and final_resp.status_code == 200
        body = final_resp.json()
        assert body["kind"] == "document"
        assert body["sha256"] == files["contract.doc"]["sha256"]
        assert body["blocks"][0]["runs"][0]["text"] == "异步转换完成正文"
        assert run_count == 1
        assert (Path(runtime.store.root) / "preview-cache").is_dir()


def test_t7_xls_attachment_excerpt_includes_sheet_name_and_cell_values(tmp_path: Path, monkeypatch):
    """T7 (Step 5): .xls attachment excerpt uses _xls_spreadsheet + _flatten_preview and contains sheet names and cell values."""
    monkeypatch.setattr(
        "rynmesh.agent_runtime.file_preview.subprocess.run",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("subprocess.run must not run for .xls attachment")),
    )
    xls_bytes = SAMPLE_XLS.read_bytes()
    prepared = parse_attachments([{
        "name": "季度报表.xls",
        "media_type": "application/vnd.ms-excel",
        "data_base64": base64.b64encode(xls_bytes).decode("ascii"),
    }])
    store = SimpleNamespace(root=tmp_path / "store", default_workspace=lambda: str(tmp_path / "ws"))
    msg = attach_to_message(store, {"id": "task_xls_att"}, {"role": "user", "content": "请汇总"}, prepared)

    from rynmesh.agent_runtime.input_attachments import provider_content
    content = provider_content(msg)
    assert "Bounded content excerpt:" in content
    assert "Sheet: 销售" in content
    assert "上海门店" in content
    assert "120" in content
    assert "2024-01-01" in content
    assert "240" in content
    assert "Sheet: 销售" not in msg["content"]


def test_t8_doc_attachment_without_cache_does_not_block_send_and_uses_cache_when_present(tmp_path: Path, monkeypatch):
    """T8 (Steps 3, 5): uncached .doc/.ppt attachment never starts LibreOffice on send and emits the new hint; cached conversion is reused."""
    soffice_called = False

    def forbidden_soffice(*_args, **_kwargs):
        nonlocal soffice_called
        soffice_called = True
        raise AssertionError("LibreOffice must not run synchronously during message send")

    monkeypatch.setattr("rynmesh.agent_runtime.file_preview.subprocess.run", forbidden_soffice)

    store_root = tmp_path / "store"
    ws_root = tmp_path / "ws"
    store = SimpleNamespace(root=store_root, default_workspace=lambda: str(ws_root))

    doc_raw = b"legacy-binary-doc-not-yet-converted"
    prepared = parse_attachments([{
        "name": "旧合同.doc",
        "media_type": "application/msword",
        "data_base64": base64.b64encode(doc_raw).decode("ascii"),
    }])
    msg_uncached = attach_to_message(store, {"id": "task_doc_uncached"}, {"role": "user", "content": "看下合同"}, prepared)
    assert soffice_called is False
    assert "旧格式文件，尚未转换；稍后可在文件预览中查看，或让主人另存为 .xlsx/.docx" in msg_uncached["content"]
    assert "Content was not extracted here; use the local file/command tools to inspect the original." not in msg_uncached["content"]

    # Now populate store.root/preview-cache/ via preview_file and verify _excerpt reuses the cached .docx without re-running LibreOffice.
    monkeypatch.setattr("rynmesh.agent_runtime.file_preview.shutil.which", lambda _: "soffice")
    monkeypatch.setattr(
        "rynmesh.agent_runtime.file_preview.subprocess.run",
        lambda arguments, **_kw: (Path(arguments[arguments.index("--outdir") + 1]) / "document.docx").write_bytes(
            _simple_docx("已缓存的旧版合同条款正文")
        ),
    )
    preview_file("旧合同.doc", doc_raw, cache_dir=store_root / "preview-cache")

    # Forbid subprocess.run again to prove attach_to_message reads strictly from preview-cache.
    monkeypatch.setattr("rynmesh.agent_runtime.file_preview.subprocess.run", forbidden_soffice)
    msg_cached = attach_to_message(store, {"id": "task_doc_cached"}, {"role": "user", "content": "再看下合同"}, prepared)
    assert soffice_called is False
    from rynmesh.agent_runtime.input_attachments import provider_content
    content = provider_content(msg_cached)
    assert "Bounded content excerpt:" in content
    assert "已缓存的旧版合同条款正文" in content
    assert "已缓存的旧版合同条款正文" not in msg_cached["content"]


def test_t9_table_work_calculates_xls_matching_csv_and_validates_headers(tmp_path: Path):
    """T9 (Step 6): table_work.calculate on .xls (first and named sheet) matches identical CSV, enforces duplicate header/missing sheet checks, and drops '不支持 .xls' wording."""
    xls_raw = SAMPLE_XLS.read_bytes()
    csv_sheet1 = "门店,销量,日期,小计\n上海门店,120,2024-01-01,240\n北京门店,85.5,2024-01-15,171\n".encode("utf-8")
    csv_sheet2 = "门店,销量,日期,小计\n深圳门店,200,2024-04-01,400\n".encode("utf-8")

    recipe = {
        "columns": [
            {"name": "shop", "type": "text", "source": "门店"},
            {"name": "qty", "type": "decimal", "source": "销量"},
            {"name": "sold_on", "type": "date", "source": "日期"},
            {"name": "subtotal", "type": "decimal", "source": "小计"},
        ],
        "derived": [{"column": "expected_subtotal", "expression": "qty * 2"}],
        "checks": [{"name": "subtotal_matches", "scope": "row", "expression": "subtotal == expected_subtotal"}],
    }

    rep_xls_1 = calculate(xls_raw, ".xls", recipe)
    rep_csv_1 = calculate(csv_sheet1, ".csv", recipe)
    assert rep_xls_1["status"] == rep_csv_1["status"] == "passed"
    assert rep_xls_1["rows"] == rep_csv_1["rows"]
    assert rep_xls_1["totals"] == rep_csv_1["totals"]
    assert rep_xls_1["checks"] == rep_csv_1["checks"]

    rep_xls_2 = calculate(xls_raw, ".xls", {**recipe, "sheet": "二季度"})
    rep_csv_2 = calculate(csv_sheet2, ".csv", recipe)
    assert rep_xls_2["status"] == "passed"
    assert rep_xls_2["rows"] == rep_csv_2["rows"]
    assert rep_xls_2["totals"] == rep_csv_2["totals"]

    # Duplicate headers in .xls reuse S05-I15 validation.
    with pytest.raises(AgentToolRejected) as dup_exc:
        calculate(_dup_header_xls(), ".xls", {"columns": [{"name": "amount", "type": "decimal"}]})
    assert dup_exc.value.args[0] == "agent_table_duplicate_headers"

    # Missing sheet in .xls raises agent_table_input_invalid with sheet name.
    with pytest.raises(AgentToolRejected) as missing_sheet_exc:
        calculate(xls_raw, ".xls", {**recipe, "sheet": "不存在的工作表"})
    assert missing_sheet_exc.value.args[0] == "agent_table_input_invalid"
    assert "不存在的工作表" in missing_sheet_exc.value.correction

    # Unsupported extension guidance mentions .xls as supported rather than unsupported.
    assert ".xls" in table_work.CORRECTIONS["agent_table_csv_or_json_required"]
    assert "不支持 .xls" not in table_work.CORRECTIONS["agent_table_csv_or_json_required"]
