"""Inert, bounded content previews for receipt-bound Office files.

OOXML is read locally without Office, an external viewer, formulas, macros or URLs.
The optional LibreOffice fallback handles old binary Office formats in an isolated profile.
"""
from __future__ import annotations

import asyncio
import base64
import csv
import datetime as dt
import io
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

MAX_PACKAGE_BYTES = 48 * 1024 * 1024
MAX_XML_BYTES = 12 * 1024 * 1024
MAX_ROWS = 200
MAX_COLUMNS = 40
MAX_SHEETS = 32
MAX_SLIDES = 100
MAX_TEXT = 250_000
MAX_BLOCKS = 2000
OFFICE_EXTENSIONS = {".xlsx", ".xlsm", ".xltx", ".docx", ".docm", ".pptx", ".pptm", ".xls", ".doc", ".ppt", ".odt", ".ods", ".odp", ".rtf"}
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


class PreviewError(ValueError):
    def __init__(self, code, *, details=None):
        super().__init__(code)
        self.details = details or {}


class Package:
    def __init__(self, raw, include_images=True, *, max_package_bytes=None, max_xml_bytes=None):
        self.max_package_bytes = MAX_PACKAGE_BYTES if max_package_bytes is None else max_package_bytes
        self.max_xml_bytes = MAX_XML_BYTES if max_xml_bytes is None else max_xml_bytes
        self.archive = zipfile.ZipFile(io.BytesIO(raw))
        entries = self.archive.infolist()
        if len(entries) > 5000 or sum(item.file_size for item in entries) > self.max_package_bytes:
            self.archive.close()
            raise PreviewError("agent_preview_limit")
        self.names = set(self.archive.namelist())
        self.text_left = MAX_TEXT
        self.image_bytes_left = 4 * 1024 * 1024
        self.include_images = include_images
        self.truncated = False

    def xml(self, name):
        info = self.archive.getinfo(name)
        if info.file_size > self.max_xml_bytes:
            raise PreviewError("agent_preview_limit")
        raw = self.archive.read(info)
        # ElementTree does not fetch external entities. Reject internal entities too,
        # including UTF-16 declarations, before handing bytes to the XML parser.
        if re.search(br"<!\s*(?:DOCTYPE|ENTITY)", raw.replace(b"\x00", b""), re.I):
            raise PreviewError("agent_preview_invalid")
        return ET.fromstring(raw)

    def text(self, value):
        value = value or ""
        result = value[:min(self.text_left, 8000)]
        self.text_left -= len(result)
        self.truncated |= result != value
        return result

    def rels(self, name):
        folder, filename = posixpath.split(name)
        path = posixpath.join(folder, "_rels", filename + ".rels")
        if path not in self.names:
            return {}
        result = {}
        for item in self.xml(path):
            target = item.get("Target", "")
            if item.get("TargetMode") == "External" or ":" in target or "\\" in target:
                continue
            resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(folder, target))
            if not resolved.startswith("../") and resolved in self.names:
                result[item.get("Id")] = resolved
        return result


def _column(reference):
    match = re.match(r"([A-Za-z]{1,4})", reference or "")
    result = 0
    for letter in match[1].upper() if match else "A":
        result = result * 26 + ord(letter) - 64
    return result


def _image(package, path):
    if not package.include_images:
        return None
    if not path or package.archive.getinfo(path).file_size > package.image_bytes_left:
        package.truncated = True
        return None
    from PIL import Image
    raw = package.archive.read(path)
    try:
        with Image.open(io.BytesIO(raw)) as picture:
            if picture.format not in {"PNG", "JPEG", "GIF", "WEBP"} or picture.width * picture.height > 20_000_000:
                return None
            kind = "jpeg" if picture.format == "JPEG" else picture.format.lower()
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    package.image_bytes_left -= len(raw)
    return "data:image/" + kind + ";base64," + base64.b64encode(raw).decode("ascii")


def _number(raw, style, styles, epoch, *, format_numbers=True):
    """Use cached values and familiar number/date formats; never evaluate formulas."""
    if not raw:
        return raw
    try:
        number = float(raw)
        code = styles[int(style)] if style and int(style) < len(styles) else ""
        if code.startswith("date:"):
            value = epoch + dt.timedelta(days=number)
            return value.strftime("%Y-%m-%d %H:%M" if number % 1 else "%Y-%m-%d")
        if format_numbers:
            if "%" in code:
                decimals = len(re.search(r"\.([0#]+)", code)[1]) if re.search(r"\.([0#]+)", code) else 0
                return f"{number * 100:,.{min(decimals, 6)}f}%"
            if re.search(r"[0#],[0#]|0\.0", code):
                decimals = len(re.search(r"\.([0#]+)", code)[1]) if re.search(r"\.([0#]+)", code) else 0
                return f"{number:,.{min(decimals, 6)}f}"
    except (ValueError, OverflowError, IndexError):
        pass
    return raw


def _workbook_parts(package):
    workbook = package.xml("xl/workbook.xml")
    relationships = package.rels("xl/workbook.xml")
    strings = []
    if "xl/sharedStrings.xml" in package.names:
        strings = ["".join(node.text or "" for node in item.iter(S + "t")) for item in package.xml("xl/sharedStrings.xml").findall(S + "si")]
    styles = []
    if "xl/styles.xml" in package.names:
        root = package.xml("xl/styles.xml")
        formats = {int(item.get("numFmtId", "0")): item.get("formatCode", "") for item in root.findall(f"{S}numFmts/{S}numFmt")}
        for item in root.findall(f"{S}cellXfs/{S}xf"):
            ident = int(item.get("numFmtId", "0"))
            code = formats.get(ident, "0%" if ident == 9 else "0.00%" if ident == 10 else "#,##0.00" if ident == 4 else "#,##0" if ident == 3 else "")
            # Ignore quoted labels, color/condition tokens and escaped literals.
            clean = re.sub(r'"[^"]*"|\[[^]]*\]|\\.', "", code)
            is_date = 14 <= ident <= 22 or 27 <= ident <= 36 or 45 <= ident <= 47 or 50 <= ident <= 58 or bool(re.search(r"[ydhs]", clean, re.I))
            styles.append("date:" + code if is_date else code)
    properties = workbook.find(S + "workbookPr")
    epoch = dt.datetime(1904, 1, 1) if properties is not None and properties.get("date1904") in {"1", "true"} else dt.datetime(1899, 12, 30)
    return relationships, strings, styles, epoch, workbook.findall(f"{S}sheets/{S}sheet")


def _cell_value(cell, strings, styles, epoch, *, format_numbers=True):
    kind = cell.get("t")
    v_node = cell.find(S + "v")
    raw = (v_node.text or "") if v_node is not None else ""
    formula = cell.find(S + "f")
    if kind == "s":
        value = strings[int(raw)] if raw.isdigit() and int(raw) < len(strings) else ""
    elif kind == "inlineStr":
        value = "".join(item.text or "" for item in cell.findall(f"{S}is//{S}t"))
    elif kind == "b":
        value = "TRUE" if raw == "1" else "FALSE"
    elif kind in {"str", "e"}:
        value = raw
    elif kind == "d":
        value = raw[:10] if re.match(r"^\d{4}-\d{2}-\d{2}T00:00:00", raw) else raw
    else:
        value = _number(raw, cell.get("s"), styles, epoch, format_numbers=format_numbers)
    formula_text = (formula.text or "") if formula is not None else None
    uncalculated = formula is not None and (v_node is None or (not raw and kind not in {"str", "inlineStr"}))
    return value, formula_text, uncalculated


def sheet_rows(raw, sheet=None, max_rows=5000, max_columns=64, *, _preview=False):
    owns_package = not isinstance(raw, Package)
    package = None
    try:
        package = (
            Package(
                raw,
                include_images=False,
                max_package_bytes=max(MAX_PACKAGE_BYTES, 256 * 1024 * 1024),
                max_xml_bytes=max(MAX_XML_BYTES, 256 * 1024 * 1024),
            )
            if owns_package
            else raw
        )
        relationships, strings, styles, epoch, sheets = _workbook_parts(package)
        if _preview:
            result = []
            formulas_uncalculated = False
            for item_sheet in sheets[:MAX_SHEETS]:
                path = relationships.get(item_sheet.get(R + "id"))
                if not path:
                    continue
                root = package.xml(path)
                rows, max_column, total_rows, truncated = [], 0, 0, False
                for ordinal, row in enumerate(root.findall(f"{S}sheetData/{S}row"), 1):
                    index = int(row.get("r", str(ordinal)))
                    total_rows = max(total_rows, index)
                    cells = []
                    previous_column = 0
                    for cell in row.findall(S + "c"):
                        column = _column(cell.get("r")) if cell.get("r") else previous_column + 1
                        previous_column = column
                        max_column = max(max_column, column)
                        if index > max_rows or column > max_columns:
                            truncated = True
                            continue
                        value, formula_text, uncalculated = _cell_value(cell, strings, styles, epoch, format_numbers=True)
                        if uncalculated:
                            formulas_uncalculated = True
                            value = "=" + (formula_text or "")
                        if value or formula_text is not None:
                            item = {"column": column, "value": package.text(value)}
                            if formula_text is not None:
                                item["formula"] = package.text("=" + formula_text)
                            cells.append(item)
                    if index <= max_rows:
                        rows.append({"index": index, "cells": cells})
                dimension = root.find(S + "dimension")
                if dimension is not None:
                    end = dimension.get("ref", "A1").split(":")[-1]
                    match = re.search(r"(\d+)$", end)
                    total_rows = max(total_rows, int(match[1]) if match else 0)
                    max_column = max(max_column, _column(end))
                result.append({"name": package.text(item_sheet.get("name", "Sheet")), "rows": rows, "total_rows": total_rows,
                               "total_columns": max_column, "columns": min(max_column, max_columns),
                               "hidden": item_sheet.get("state") in {"hidden", "veryHidden"},
                               "truncated": truncated or total_rows > max_rows or max_column > max_columns})
            return {"kind": "spreadsheet", "sheets": result, "formulas_uncalculated": formulas_uncalculated,
                    "truncated": len(sheets) > MAX_SHEETS or package.truncated}

        target_path = None
        if sheet is None:
            for item_sheet in sheets:
                target_path = relationships.get(item_sheet.get(R + "id"))
                if target_path:
                    break
            if not target_path:
                return [], []
        else:
            matched = next((item_sheet for item_sheet in sheets if item_sheet.get("name") == sheet), None)
            target_path = relationships.get(matched.get(R + "id")) if matched is not None else None
            if not target_path:
                raise PreviewError("agent_preview_sheet_not_found", details={"sheet": sheet})

        root = package.xml(target_path)
        xml_rows = root.findall(f"{S}sheetData/{S}row")
        if not xml_rows:
            return [], []

        header_cells = {}
        previous_column = 0
        for cell in xml_rows[0].findall(S + "c"):
            col = _column(cell.get("r")) if cell.get("r") else previous_column + 1
            previous_column = col
            if col > max_columns:
                raise PreviewError("agent_preview_column_limit", details={"max_columns": max_columns})
            value, _, uncalculated = _cell_value(cell, strings, styles, epoch, format_numbers=False)
            if uncalculated:
                raise PreviewError("agent_preview_formula_uncalculated", details={"row": 0, "column": cell.get("r") or str(col), "cell": cell.get("r") or ""})
            header_cells[col] = value
        width = max((col for col, val in header_cells.items() if val != ""), default=0)
        if width == 0:
            return [], []
        headers = [header_cells.get(col, "") for col in range(1, width + 1)]

        rows = []
        for xml_row in xml_rows[1:]:
            row_values = [""] * width
            previous_column = 0
            next_row_num = len(rows) + 1
            for cell in xml_row.findall(S + "c"):
                col = _column(cell.get("r")) if cell.get("r") else previous_column + 1
                previous_column = col
                if col > max_columns:
                    raise PreviewError("agent_preview_column_limit", details={"max_columns": max_columns})
                value, _, uncalculated = _cell_value(cell, strings, styles, epoch, format_numbers=False)
                if uncalculated:
                    col_label = headers[col - 1] if col <= width and headers[col - 1] else (cell.get("r") or str(col))
                    raise PreviewError("agent_preview_formula_uncalculated", details={"row": next_row_num, "column": col_label, "cell": cell.get("r") or ""})
                if col > width:
                    if value != "":
                        raise PreviewError("agent_preview_ragged_row", details={"row": next_row_num, "column": col})
                else:
                    row_values[col - 1] = value
            if not any(v != "" for v in row_values):
                continue
            if next_row_num > max_rows:
                raise PreviewError("agent_preview_row_limit", details={"max_rows": max_rows})
            rows.append(row_values)
        return headers, rows
    except (KeyError, ValueError, OverflowError, ET.ParseError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, PreviewError):
            raise
        raise PreviewError("agent_preview_invalid") from None
    finally:
        if owns_package and package is not None:
            package.archive.close()


def _spreadsheet(package):
    return sheet_rows(package, max_rows=MAX_ROWS, max_columns=MAX_COLUMNS, _preview=True)


def _word_paragraph(package, paragraph):
    style = paragraph.find(f"{W}pPr/{W}pStyle")
    runs = []
    for run in paragraph.iter(W + "r"):
        text = "".join("\n" if item.tag == W + "br" else "\t" if item.tag == W + "tab" else item.text or ""
                       for item in run if item.tag in {W + "t", W + "br", W + "tab"})
        if text:
            props = run.find(W + "rPr")
            runs.append({"text": package.text(text), "bold": props is not None and props.find(W + "b") is not None,
                         "italic": props is not None and props.find(W + "i") is not None})
    return {"type": "paragraph", "style": style.get(W + "val", "") if style is not None else "", "runs": runs}


def _document(package):
    root = package.xml("word/document.xml")
    blocks = []
    relationships = package.rels("word/document.xml")
    body = root.find(W + "body")
    if body is None:
        raise PreviewError("agent_preview_invalid")
    for item in body:
        if len(blocks) >= MAX_BLOCKS or not package.text_left:
            package.truncated = True
            break
        if item.tag == W + "p":
            blocks.append(_word_paragraph(package, item))
            for image in item.iter(A + "blip"):
                path = relationships.get(image.get(R + "embed"))
                if path and (url := _image(package, path)):
                    blocks.append({"type": "image", "url": url})
        elif item.tag == W + "tbl":
            rows = item.findall(W + "tr")
            blocks.append({"type": "table", "rows": [
                ["\n".join("".join(run["text"] for run in _word_paragraph(package, paragraph)["runs"])
                            for paragraph in cell.findall(W + "p")) for cell in row.findall(W + "tc")[:MAX_COLUMNS]]
                for row in rows[:MAX_ROWS]]})
            package.truncated |= len(rows) > MAX_ROWS
    return {"kind": "document", "blocks": blocks, "truncated": package.truncated}


def _presentation(package):
    presentation = package.xml("ppt/presentation.xml")
    relationships = package.rels("ppt/presentation.xml")
    identifiers = presentation.findall(f"{P}sldIdLst/{P}sldId")
    slides = []
    for identifier in identifiers[:MAX_SLIDES]:
        path = relationships.get(identifier.get(R + "id"))
        if not path:
            continue
        root = package.xml(path)
        slide_relationships = package.rels(path)
        paragraphs = []
        for paragraph in root.iter(A + "p"):
            text = "".join("\n" if item.tag == A + "br" else item.text or "" for item in paragraph.iter() if item.tag in {A + "t", A + "br"})
            if text and len(paragraphs) < MAX_BLOCKS:
                paragraphs.append(package.text(text))
        images = []
        for image in root.iter(A + "blip"):
            target = slide_relationships.get(image.get(R + "embed"))
            if target and len(images) < 16 and (url := _image(package, target)):
                images.append(url)
        slides.append({"number": len(slides) + 1, "paragraphs": paragraphs, "images": images})
        if not package.text_left:
            package.truncated = True
            break
    return {"kind": "presentation", "slides": slides, "total_slides": len(identifiers),
            "truncated": len(identifiers) > MAX_SLIDES or package.truncated}


def _libreoffice_preview(raw, suffix, include_images):
    executable = shutil.which("soffice") or shutil.which("libreoffice")
    if not executable:
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), "/Applications"):
            if base:
                candidate = Path(base) / ("LibreOffice.app/Contents/MacOS/soffice" if base == "/Applications" else "LibreOffice/program/soffice.exe")
                if candidate.is_file():
                    executable = str(candidate)
                    break
    if not executable:
        raise PreviewError("agent_preview_converter_missing")
    target = "xlsx" if suffix in {".xls", ".ods"} else "pptx" if suffix in {".ppt", ".odp"} else "docx"
    with tempfile.TemporaryDirectory(prefix="ryn-preview-") as directory:
        root = Path(directory)
        source = root / ("document" + suffix)
        source.write_bytes(raw)
        profile = root / "profile"
        profile.mkdir()
        # A fresh profile has no extensions or trusted locations, and no macro execution.
        (profile / "registrymodifications.xcu").write_text(
            '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>'
            '</item></oor:items>', encoding="utf-8")
        try:
            subprocess.run([executable, "-env:UserInstallation=" + profile.as_uri(), "--headless", "--nologo", "--nodefault",
                            "--norestore", "--convert-to", target, "--outdir", str(root), str(source)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=20, check=True, **({"creationflags": 0x08000000} if os.name == "nt" else {}))
            result = root / ("document." + target)
            if result.stat().st_size > MAX_PACKAGE_BYTES:
                raise PreviewError("agent_preview_limit")
            return preview_file(result.name, result.read_bytes(), include_images=include_images)
        except (OSError, subprocess.SubprocessError):
            raise PreviewError("agent_preview_conversion_failed") from None


def _delimited_rows(text, delimiter):
    try:
        yield from csv.reader(io.StringIO(text), delimiter=delimiter)
    except csv.Error:
        raise PreviewError("agent_preview_limit") from None


def preview_file(name: str, raw: bytes, *, include_images: bool = True) -> dict:
    suffix = Path(name).suffix.lower()
    if suffix in {".csv", ".tsv"}:
        text = None
        encodings = ("utf-16",) if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
        for encoding in encodings:
            try:
                text = raw.decode(encoding)
                break
            except UnicodeError:
                continue
        if text is None:
            raise PreviewError("agent_preview_invalid")
        rows = []
        columns = 0
        chars_left = MAX_TEXT
        truncated = False
        for index, row in enumerate(_delimited_rows(text, "\t" if suffix == ".tsv" else ","), 1):
            if index > MAX_ROWS or not chars_left:
                truncated = True
                break
            columns = max(columns, len(row))
            cells = []
            for column, value in enumerate(row[:MAX_COLUMNS], 1):
                visible = value[:min(chars_left, 8000)]
                chars_left -= len(visible)
                truncated |= value != visible
                cells.append({"column": column, "value": visible})
            rows.append({"index": index, "cells": cells})
        return {"kind": "spreadsheet", "sheets": [{"name": name, "rows": rows, "columns": min(columns, MAX_COLUMNS),
                "total_rows": len(rows), "total_columns": columns, "truncated": truncated or columns > MAX_COLUMNS}], "truncated": False}
    if suffix not in OFFICE_EXTENSIONS:
        raise PreviewError("agent_preview_unsupported")
    if suffix in {".xls", ".doc", ".ppt", ".odt", ".ods", ".odp", ".rtf"}:
        return _libreoffice_preview(raw, suffix, include_images)
    package = None
    try:
        package = Package(raw, include_images)
        if suffix in {".xlsx", ".xlsm", ".xltx"}:
            return _spreadsheet(package)
        if suffix in {".docx", ".docm"}:
            return _document(package)
        return _presentation(package)
    except (KeyError, ValueError, OverflowError, ET.ParseError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, PreviewError):
            raise
        raise PreviewError("agent_preview_invalid") from None
    finally:
        if package is not None:
            package.archive.close()


def install_file_preview_routes(app, call, local_control):
    @app.get("/api/local/agent/tasks/{task_id}/files/{file_id}/preview")
    async def file_preview(task_id: str, file_id: str, request: Request, v: str | None = None):
        local_control(request)
        item = await call("read_file_artifact", task_id, file_id)
        if isinstance(item, JSONResponse):
            return item
        if v and v != item["sha256"]:
            return JSONResponse(status_code=409, content={"detail": "agent_artifact_content_changed"})
        try:
            result = await asyncio.to_thread(preview_file, item["name"], item["bytes"])
        except PreviewError as exc:
            raise HTTPException(422, detail=str(exc)) from None
        return {**result, "sha256": item["sha256"]}
