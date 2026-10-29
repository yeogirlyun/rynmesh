"""Office previews read inert content from the same receipt-bound bytes as downloads."""
import hashlib
import io
import zipfile

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from rynmesh.agent_runtime.file_preview import (
    PreviewError,
    install_file_preview_routes,
    preview_file,
)

S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as package:
        for name, value in files.items():
            package.writestr(name, value)
    return output.getvalue()


def workbook(sheet, *, extra=None, sheets=None):
    return archive({"xl/workbook.xml": f'<workbook {S} {R}><sheets>{sheets or """<sheet name="销售" sheetId="1" r:id="r1"/>"""}</sheets></workbook>',
                    "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>',
                    "xl/worksheets/sheet1.xml": f'<worksheet {S}>{sheet}</worksheet>', **(extra or {})})


def test_workbook_keeps_sparse_cells_formulas_dates_shared_strings_and_unicode():
    raw = workbook('''<dimension ref="A1:D3"/><sheetData>
      <row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="inlineStr"><is><t>销量</t></is></c></row>
      <row r="3"><c r="A3" s="1"><v>0.25</v></c><c r="B3" s="2"><v>45292</v></c>
      <c r="C3"><f>SUM(C1:C2)</f><v>120</v></c><c r="D3"><f>SUM(A1:A2)</f><v/></c></row>
      </sheetData>''', extra={"xl/sharedStrings.xml": f'<sst {S}><si><r><t>上海</t></r><r><t>门店</t></r></si></sst>',
                             "xl/styles.xml": f'<styleSheet {S}><cellXfs><xf numFmtId="0"/><xf numFmtId="10"/><xf numFmtId="14"/></cellXfs></styleSheet>'})
    result = preview_file("季度销售.xlsx", raw)
    assert result["kind"] == "spreadsheet" and result["sheets"][0]["name"] == "销售"
    sheet = result["sheets"][0]
    assert sheet["total_rows"] == 3 and sheet["columns"] == 4
    assert sheet["rows"][0]["cells"] == [{"column": 1, "value": "上海门店"}, {"column": 3, "value": "销量"}]
    assert sheet["rows"][1]["cells"] == [{"column": 1, "value": "25.00%"}, {"column": 2, "value": "2024-01-01"},
                                          {"column": 3, "value": "120", "formula": "=SUM(C1:C2)"},
                                          {"column": 4, "value": "=SUM(A1:A2)", "formula": "=SUM(A1:A2)"}]
    assert result["formulas_uncalculated"] is True


def test_workbook_bounds_huge_dimensions_and_preserves_explicit_sheet_order():
    raw = workbook('<dimension ref="A1:XFD1048576"/><sheetData><row r="50000"><c r="XFD50000"><v>1</v></c></row></sheetData>')
    sheet = preview_file("wide.xlsx", raw)["sheets"][0]
    assert sheet["rows"] == [] and sheet["truncated"] is True
    assert sheet["columns"] == 40 and sheet["total_columns"] == 16384
    assert sheet["total_rows"] == 1048576
    raw = workbook('<sheetData/>', sheets='<sheet name="Summary" state="hidden" r:id="r1"/><sheet name="Details" r:id="r1"/>')
    result = preview_file("tabs.xlsx", raw)
    assert [item["name"] for item in result["sheets"]] == ["Summary", "Details"]
    assert result["sheets"][0]["hidden"] is True


def test_document_preserves_paragraph_table_order_formatting_and_inert_text():
    raw = archive({"word/document.xml": f'''<w:document {W}><w:body>
      <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:rPr><w:b/></w:rPr><w:t>销售报告 &lt;script&gt;</w:t></w:r></w:p>
      <w:tbl><w:tr><w:tc><w:p><w:r><w:t>城市</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>收入</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
      <w:p><w:r><w:rPr><w:i/></w:rPr><w:t>第一行</w:t><w:br/><w:t>第二行</w:t></w:r></w:p>
      </w:body></w:document>'''})
    result = preview_file("report.docx", raw)
    assert result["kind"] == "document" and [block["type"] for block in result["blocks"]] == ["paragraph", "table", "paragraph"]
    assert result["blocks"][0]["runs"] == [{"text": "销售报告 <script>", "bold": True, "italic": False}]
    assert result["blocks"][1]["rows"] == [["城市", "收入"]]
    assert result["blocks"][2]["runs"][0]["text"] == "第一行\n第二行"


def test_presentation_uses_manifest_order_and_ignores_external_relationships():
    raw = archive({"ppt/presentation.xml": f'<p:presentation {P} {R}><p:sldIdLst><p:sldId r:id="second"/><p:sldId r:id="first"/><p:sldId r:id="external"/></p:sldIdLst></p:presentation>',
      "ppt/_rels/presentation.xml.rels": '<Relationships><Relationship Id="first" Target="slides/slide1.xml"/><Relationship Id="second" Target="/ppt/slides/slide2.xml"/><Relationship Id="external" TargetMode="External" Target="https://example.com/secret"/></Relationships>',
      "ppt/slides/slide1.xml": f'<p:sld {P} {A}><a:p><a:r><a:t>第一页</a:t></a:r></a:p></p:sld>',
      "ppt/slides/slide2.xml": f'<p:sld {P} {A}><a:p><a:r><a:t>第二页</a:t></a:r></a:p></p:sld>'})
    result = preview_file("deck.pptx", raw)
    assert result["kind"] == "presentation"
    assert [slide["paragraphs"] for slide in result["slides"]] == [["第二页"], ["第一页"]]


@pytest.mark.parametrize("raw", [b"not a zip", archive({"word/document.xml": f'<!DOCTYPE x [<!ENTITY e "bad">]><w:document {W}><w:body><w:p>&e;</w:p></w:body></w:document>'}),
                               archive({"word/document.xml": ('<?xml version="1.0" encoding="utf-16"?><!DOCTYPE x [<!ENTITY e "bad">]><x/>').encode("utf-16")})])
def test_rejects_corrupt_packages_and_xml_entities(raw):
    with pytest.raises(PreviewError, match="agent_preview_invalid"):
        preview_file("document.docx", raw)


def test_rejects_compressed_package_bombs_before_reading_xml(monkeypatch):
    monkeypatch.setattr("rynmesh.agent_runtime.file_preview.MAX_PACKAGE_BYTES", 500)
    with pytest.raises(PreviewError, match="agent_preview_limit"):
        preview_file("workbook.xlsx", archive({"unused": "a" * 501}))


def test_tsv_and_gbk_csv_are_tables_and_keep_empty_cells():
    result = preview_file("数据.csv", '城市,收入\n上海,123\n北京,"1,234"'.encode("gb18030"))
    assert result["sheets"][0]["rows"][2]["cells"][1]["value"] == "1,234"
    result = preview_file("data.tsv", b"name\t\tamount\nshop\t\t1")
    assert result["sheets"][0]["columns"] == 3
    assert result["sheets"][0]["rows"][0]["cells"][1]["value"] == ""
    result = preview_file("even.csv", '城市,收入\n上海,12'.encode("gb18030"))
    assert result["sheets"][0]["rows"][1]["cells"][0]["value"] == "上海"
    result = preview_file("utf16.csv", '城市,收入\n上海,12'.encode("utf-16"))
    assert result["sheets"][0]["rows"][1]["cells"][0]["value"] == "上海"


def test_csv_field_limits_return_preview_errors_instead_of_server_errors():
    with pytest.raises(PreviewError, match="agent_preview_limit"):
        preview_file("large.csv", b"x" * 140000)


def test_word_embedded_images_are_bounded_and_optional():
    from PIL import Image
    image = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(image, format="PNG")
    raw = archive({"word/document.xml": f'<w:document {W} {A} {R}><w:body><w:p><a:blip r:embed="image"/><a:blip r:link="remote"/></w:p></w:body></w:document>',
                   "word/_rels/document.xml.rels": '<Relationships><Relationship Id="image" Target="media/image.png"/><Relationship Id="remote" TargetMode="External" Target="https://example.com/tracker.png"/></Relationships>',
                   "word/media/image.png": image.getvalue()})
    result = preview_file("image.docx", raw)
    assert result["blocks"][1]["type"] == "image" and result["blocks"][1]["url"].startswith("data:image/png;base64,")
    assert len(result["blocks"]) == 2
    assert preview_file("image.docx", raw, include_images=False)["blocks"] == [{"type": "paragraph", "style": "", "runs": []}]


def test_missing_legacy_converter_is_an_actionable_error(monkeypatch):
    monkeypatch.setattr("rynmesh.agent_runtime.file_preview.shutil.which", lambda _: None)
    monkeypatch.setattr("rynmesh.agent_runtime.file_preview.Path.is_file", lambda _: False)
    with pytest.raises(PreviewError, match="agent_preview_converter_missing"):
        preview_file("old.xls", b"binary")


def test_legacy_conversion_uses_an_isolated_profile_and_the_same_bounded_parser(monkeypatch):
    monkeypatch.setattr("rynmesh.agent_runtime.file_preview.shutil.which", lambda _: "soffice")
    from pathlib import Path
    output = workbook('<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Converted data</t></is></c></row></sheetData>')
    invoked = []

    def run(arguments, **options):
        invoked.append((arguments, options))
        root = Path(arguments[arguments.index("--outdir") + 1])
        profile = root / "profile" / "registrymodifications.xcu"
        assert "DisableMacrosExecution" in profile.read_text("utf-8")
        assert (root / "document.xls").read_bytes() == b"legacy binary"
        (root / "document.xlsx").write_bytes(output)

    monkeypatch.setattr("rynmesh.agent_runtime.file_preview.subprocess.run", run)
    result = preview_file("legacy.xls", b"legacy binary")
    assert result["sheets"][0]["rows"][0]["cells"][0]["value"] == "Converted data"
    assert invoked[0][1]["timeout"] == 20 and "--headless" in invoked[0][0]


def test_preview_route_is_installed_in_the_real_agent_api(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from test_agent_runtime import Catalog, Model, Tools
    from test_agent_workspace_artifacts import written

    from rynmesh.agent_runtime.routes import install_agent
    from rynmesh.background_workers import BackgroundWorkerRegistry

    app = FastAPI()
    runtime = install_agent(app, store=object(), home=tmp_path / "home", workers=BackgroundWorkerRegistry(),
                            local_control=lambda _: None, messaging_key=X25519PrivateKey.generate(),
                            integrations=(Model(), Tools(), Catalog()))
    runtime.update_settings({"enabled": True, "model_id": "local:test"})
    task = runtime.create({"message": "Make a spreadsheet"})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    raw = workbook('<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Receipt-bound preview</t></is></c></row></sheetData>')
    path = workspace / "report.xlsx"
    path.write_bytes(raw)
    runtime.store.transaction(lambda data: data["tasks"][task["id"]].update(
        receipts=[written(path, raw, "op1")], local_execution={"workspace_root": str(workspace)}, result="report.xlsx"))
    with TestClient(app) as client:
        files = client.get(f"/api/local/agent/tasks/{task['id']}/artifacts").json()["files"]
        response = client.get(f"/api/local/agent/tasks/{task['id']}/files/{files[0]['id']}/preview?v={files[0]['sha256']}")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json()["sheets"][0]["rows"][0]["cells"][0]["value"] == "Receipt-bound preview"


def test_preview_route_checks_local_access_receipt_id_and_file_version():
    raw = workbook('<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>已确认内容</t></is></c></row></sheetData>')
    digest = hashlib.sha256(raw).hexdigest()
    app = FastAPI()
    checked = []

    async def call(method, task_id, file_id):
        checked.append((method, task_id, file_id))
        if file_id != "confirmed":
            raise HTTPException(409, "agent_artifact_not_found")
        return {"name": "report.xlsx", "bytes": raw, "sha256": digest}

    def local_control(request):
        if request.headers.get("x-allowed") != "yes":
            raise HTTPException(403, "local_control_required")

    install_file_preview_routes(app, call, local_control)
    with TestClient(app) as client:
        path = "/api/local/agent/tasks/task/files/confirmed/preview"
        assert client.get(path).status_code == 403 and not checked
        stale = client.get(path + "?v=old", headers={"x-allowed": "yes"})
        assert stale.status_code == 409 and stale.json()["detail"] == "agent_artifact_content_changed"
        response = client.get(path + "?v=" + digest, headers={"x-allowed": "yes"})
        assert response.status_code == 200 and response.json()["sha256"] == digest
        assert response.json()["sheets"][0]["rows"][0]["cells"][0]["value"] == "已确认内容"
        assert client.get(path.replace("confirmed", "forged"), headers={"x-allowed": "yes"}).status_code == 409
        assert checked[-1] == ("read_file_artifact", "task", "forged")
