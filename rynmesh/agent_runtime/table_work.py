"""Deterministic, bounded table calculations and independently readable exports.

Input data and rules remain caller supplied. A passed calculation verifies the
declared arithmetic/checks, never the truth of a source or a business policy.
No expressions run as Python or spreadsheet formulas.
"""
from __future__ import annotations

import ast
import copy
import csv
import hashlib
import io
import json
import operator
import re
import zipfile
from datetime import date, datetime
from decimal import (
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Decimal,
    DecimalException,
    localcontext,
)
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from .store import AgentToolRejected, encoded

VERSION = "ryn.table.v1"
ROUNDING = {"half_even": ROUND_HALF_EVEN, "half_up": ROUND_HALF_UP, "down": ROUND_DOWN}
DECIMAL_FORMATS = frozenset({"thousands", "currency", "percent", "paren_negative"})
NAME = re.compile(r"^[^\W\d]\w{0,63}$", re.UNICODE)
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
       ast.Gt: operator.gt, ast.GtE: operator.ge}

DEFAULT_CORRECTION = (
    "Use declared columns, finite decimal inputs and bounded arithmetic expressions. "
    "Missing values are not zero."
)
CORRECTIONS = {
    "agent_table_csv_or_json_required": "只支持 .csv/.json/.xlsx，其他格式请先转换",
    "agent_table_duplicate_headers": "表头有重复列名：{names}",
    "agent_table_date_invalid": "第 {row} 行 {column} 列的日期需为 YYYY-MM-DD，或在列定义里声明 date_format",
    "agent_table_text_invalid": "第 {row} 行 {column} 列的文本值无效，需为字符串",
    "agent_table_numeric_input_required": "该值不是数字，可在列定义里声明 format",
    "agent_table_input_invalid": "表格输入无效：{reason}",
    "agent_table_missing_value": "第 {row} 行 {column} 列为空值，缺值不当 0，请声明 default 或使用 if_else",
    "agent_table_calculation_failed": "第 {row} 行 {column} 列计算失败，请检查除以 0（可用 div）或数值范围",
}


def reject(code="agent_table_recipe_invalid", **details):
    template = CORRECTIONS.get(code)
    correction = DEFAULT_CORRECTION
    if template is not None:
        try:
            correction = template.format(**details)
        except (KeyError, IndexError, ValueError):
            correction = DEFAULT_CORRECTION
    raise AgentToolRejected(code, details=details, correction=correction)


def number(value):
    if isinstance(value, bool) or value is None:
        reject("agent_table_numeric_input_required")
    try:
        result = Decimal(str(value))
    except (DecimalException, ValueError):
        reject("agent_table_numeric_input_required")
    if not result.is_finite() or len(result.as_tuple().digits) > 50 or abs(result.as_tuple().exponent) > 100:
        reject("agent_table_number_out_of_range")
    return result


def _parse_decimal_formats(fmt) -> frozenset[str]:
    if isinstance(fmt, str) and fmt in DECIMAL_FORMATS:
        return frozenset({fmt})
    if isinstance(fmt, list) and 1 <= len(fmt) <= len(DECIMAL_FORMATS) and all(isinstance(x, str) and x in DECIMAL_FORMATS for x in fmt):
        return frozenset(fmt)
    reject()


def _parse_date_format(fmt) -> str:
    if not isinstance(fmt, str) or not 1 <= len(fmt) <= 64 or not any(tok in fmt for tok in ("%Y", "%y", "%G")):
        reject()
    return fmt


def _clean_decimal(value, fmt_set: frozenset[str], *, row=None, column=None) -> Decimal:
    ctx = {}
    if row is not None:
        ctx["row"] = row
    if column is not None:
        ctx["column"] = column
    if not fmt_set or not isinstance(value, str):
        try:
            return number(value)
        except AgentToolRejected as exc:
            if ctx:
                reject(exc.args[0], **ctx)
            raise
    s = value.strip()
    negate = False
    divide_100 = False
    if "paren_negative" in fmt_set and ((s.startswith("(") and s.endswith(")")) or (s.startswith("（") and s.endswith("）"))):
        inner = s[1:-1].strip()
        if not inner or inner[0] in "+-":
            reject("agent_table_numeric_input_required", **ctx)
        negate = True
        s = inner
    if "currency" in fmt_set:
        if s and s[0] in "+-":
            if s[0] == "-":
                negate = not negate
            s = s[1:].lstrip()
        if s and s[0] in "¥￥$€£":
            s = s[1:].lstrip()
        if s.endswith("元"):
            s = s[:-1].rstrip()
        for ch in (",", "，", " ", "\u00a0", "\u3000"):
            s = s.replace(ch, "")
    if "percent" in fmt_set:
        if not s.endswith(("%", "％")):
            reject("agent_table_numeric_input_required", **ctx)
        s = s[:-1].strip()
        divide_100 = True
    if "thousands" in fmt_set:
        for ch in (",", "，", " ", "\u00a0", "\u3000"):
            s = s.replace(ch, "")
    try:
        parsed = number(s)
        if negate:
            parsed = -parsed
        if divide_100:
            parsed = parsed / Decimal(100)
        return number(parsed)
    except AgentToolRejected as exc:
        if ctx:
            reject(exc.args[0], **ctx)
        raise


def _clean_date(value, date_fmt: str | None, *, row=None, column=None) -> str:
    ctx = {}
    if row is not None:
        ctx["row"] = row
    if column is not None:
        ctx["column"] = column
    try:
        if not isinstance(value, str):
            raise ValueError
        if date_fmt is not None:
            return datetime.strptime(value, date_fmt).date().isoformat()
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
        return value
    except ValueError:
        reject("agent_table_date_invalid", **ctx)


def expression(text):
    if not isinstance(text, str) or len(text) > 512:
        reject()
    try:
        node = ast.parse(text, mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        reject()
    if sum(1 for _ in ast.walk(node)) > 100:
        reject()
    return text, node.body


def evaluate(compiled, values, rounding, *, row=None, column=None):
    text, root = compiled
    ctx = {}
    if row is not None:
        ctx["row"] = row
    if column is not None:
        ctx["column"] = column

    def as_num(val):
        if val is None:
            reject("agent_table_missing_value", **ctx)
        try:
            return number(val)
        except AgentToolRejected as exc:
            if ctx:
                reject(exc.args[0], **ctx)
            raise

    def run(node):
        if isinstance(node, ast.Name):
            if node.id not in values:
                reject("agent_table_unknown_column", **({} if row is None else {"row": row}), column=node.id)
            val = values[node.id]
            if val is None:
                reject(
                    "agent_table_missing_value",
                    **({} if row is None else {"row": row}),
                    column=node.id,
                    **({"target": column} if column and column != node.id else {}),
                )
            return val
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return node.value
            return as_num(ast.get_source_segment(text, node))
        if isinstance(node, ast.BinOp) and type(node.op) in OPS:
            return OPS[type(node.op)](as_num(run(node.left)), as_num(run(node.right)))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            result = as_num(run(node.operand))
            return -result if isinstance(node.op, ast.USub) else result
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in CMP:
            return CMP[type(node.ops[0])](run(node.left), run(node.comparators[0]))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            name = node.func.id
            if name == "if_else":
                if len(node.args) != 3:
                    reject("agent_table_expression_not_supported", **ctx)
                cond = run(node.args[0])
                if type(cond) is not bool:
                    reject("agent_table_boolean_check_required", **ctx)
                return run(node.args[1]) if cond else run(node.args[2])
            args = [run(arg) for arg in node.args]
            if name == "div":
                if len(args) != 3:
                    reject("agent_table_expression_not_supported", **ctx)
                num_a, num_b, num_default = as_num(args[0]), as_num(args[1]), as_num(args[2])
                return num_default if num_b == 0 else (num_a / num_b)
            if name in {"min", "max", "sum", "abs"} and args:
                args = [as_num(v) for v in args]
                if name == "abs" and len(args) == 1:
                    return abs(args[0])
                if name == "sum":
                    return sum(args, Decimal(0))
                if name in {"min", "max"}:
                    return (min if name == "min" else max)(args)
            if name == "days_between" and len(args) == 2 and all(isinstance(v, str) for v in args):
                return Decimal((date.fromisoformat(args[1]) - date.fromisoformat(args[0])).days)
            if name == "round" and len(args) == 2 and rounding:
                scale = as_num(args[1])
                if scale != int(scale) or not 0 <= scale <= 12:
                    reject(**ctx)
                return as_num(args[0]).quantize(Decimal(1).scaleb(-int(scale)), rounding=rounding)
        reject("agent_table_expression_not_supported", **ctx)

    try:
        return run(root)
    except AgentToolRejected:
        raise
    except (DecimalException, ValueError, TypeError, OverflowError, ZeroDivisionError):
        reject("agent_table_calculation_failed", **ctx)


def calculate(raw, suffix, recipe, cancelled=lambda: False):
    if not isinstance(recipe, dict) or set(recipe) - {"columns", "derived", "checks", "rounding", "column_order"}:
        reject()
    if len(encoded(recipe)) > 24000:
        reject("agent_table_recipe_too_large")
    recipe = copy.deepcopy(recipe)
    definitions, derived, checks = recipe.get("columns"), recipe.get("derived", []), recipe.get("checks", [])
    if not isinstance(definitions, list) or not 1 <= len(definitions) <= 32 or not isinstance(derived, list) or len(derived) > 16 or not isinstance(checks, list) or len(checks) > 32:
        reject()
    allowed_col_keys = {"name", "type", "source", "optional", "default", "format", "date_format"}
    columns = {}
    types, sources, optional_cols, defaults, decimal_formats, date_formats = {}, {}, {}, {}, {}, {}
    for item in definitions:
        if (not isinstance(item, dict) or set(item) - allowed_col_keys
                or not {"name", "type"} <= set(item) or not isinstance(item["name"], str)
                or item["name"] in columns):
            reject()
        key = item["name"]
        kind = item["type"]
        source = item.get("source", key)
        if not NAME.fullmatch(key) or not isinstance(kind, str) or kind not in {"text", "decimal", "date"} or not isinstance(source, str) or not 1 <= len(source) <= 128:
            reject()
        opt = item.get("optional", False)
        if not isinstance(opt, bool):
            reject()
        dec_fmt = frozenset()
        dt_fmt = None
        if kind == "decimal":
            if "date_format" in item:
                reject()
            if "format" in item:
                dec_fmt = _parse_decimal_formats(item["format"])
        elif kind == "date":
            if "date_format" in item and "format" in item and item["date_format"] != item["format"]:
                reject()
            raw_dt_fmt = item.get("date_format", item.get("format"))
            if raw_dt_fmt is not None:
                dt_fmt = _parse_date_format(raw_dt_fmt)
        else:
            if "format" in item or "date_format" in item:
                reject()
        if "default" in item:
            raw_def = item["default"]
            if raw_def is None:
                reject()
            if kind == "decimal":
                defaults[key] = _clean_decimal(raw_def, dec_fmt)
            elif kind == "date":
                defaults[key] = _clean_date(raw_def, dt_fmt)
            else:
                if not isinstance(raw_def, str):
                    reject("agent_table_text_invalid", row=0, column=key)
                defaults[key] = raw_def
        columns[key] = {"type": kind, "source": source}
        types[key], sources[key], optional_cols[key] = kind, source, opt
        decimal_formats[key], date_formats[key] = dec_fmt, dt_fmt
    if "rounding" in recipe and not isinstance(recipe["rounding"], str):
        reject()
    rounding = ROUNDING.get(recipe.get("rounding"))
    if "rounding" in recipe and rounding is None:
        reject()
    names, formulas = list(columns), []
    for item in derived:
        if not isinstance(item, dict) or set(item) != {"column", "expression"} or not isinstance(item["column"], str) or not NAME.fullmatch(item["column"]) or item["column"] in names:
            reject()
        names.append(item["column"])
        formulas.append((item["column"], expression(item["expression"])))
    if "column_order" in recipe:
        order = recipe["column_order"]
        if not isinstance(order, list) or any(not isinstance(name, str) for name in order) or len(order) != len(names) or set(order) != set(names):
            reject()
        names = list(order)
    assertions = []
    for check in checks:
        if (not isinstance(check, dict) or set(check) != {"name", "scope", "expression"}
                or not isinstance(check["name"], str) or not 1 <= len(check["name"]) <= 120
                or not isinstance(check["scope"], str) or check["scope"] not in {"row", "totals"}):
            reject()
        assertions.append((check, expression(check["expression"])))
    ext = suffix.lower()
    if ext not in {".csv", ".json"}:
        reject("agent_table_csv_or_json_required")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        reject("agent_table_input_invalid", reason="无法解码")
    try:
        if ext == ".csv":
            reader = csv.DictReader(io.StringIO(text, newline=""))
            if not reader.fieldnames:
                reject("agent_table_input_invalid", reason="缺少表头")
            seen_headers, dup_headers = set(), []
            for header in reader.fieldnames:
                if header in seen_headers and header not in dup_headers:
                    dup_headers.append(str(header))
                seen_headers.add(header)
            if dup_headers:
                reject("agent_table_duplicate_headers", names=", ".join(dup_headers))
            source = list(reader)
            if any(None in row for row in source):
                reject("agent_table_input_invalid", reason="某行列数与表头不一致")
        else:
            source = json.loads(text, parse_float=Decimal)
    except AgentToolRejected:
        raise
    except (ValueError, csv.Error):
        reject("agent_table_input_invalid", reason="无法解码或解析表格内容")
    if not isinstance(source, list) or not source or any(not isinstance(row, dict) for row in source):
        reject("agent_table_input_invalid", reason="输入需为非空行对象列表")
    if len(source) > 5000:
        reject("agent_table_input_invalid", reason="行数超限（最多 5000 行）")
    missing = [
        {"row": i + 1, "column": name}
        for i, row in enumerate(source)
        for name in columns
        if not optional_cols[name] and name not in defaults and row.get(sources[name]) in (None, "")
    ]
    fingerprint = hashlib.sha256(raw + b"\x00" + encoded(recipe)).hexdigest()
    base = {"engine": VERSION, "recipe": recipe, "calculation_sha256": fingerprint, "input_sha256": hashlib.sha256(raw).hexdigest(),
            "recipe_sha256": hashlib.sha256(encoded(recipe)).hexdigest(), "input_provenance": "caller_supplied",
            "coverage": "Declared arithmetic and checks only; source truth and business rules are not independently verified."}
    if missing:
        return {**base, "status": "needs_input", "missing_fields": missing[:64], "missing_count": len(missing), "rows": [], "columns": names, "checks": []}
    rows = []
    outcomes = [{**check, "evaluated": 0, "failures": 0, "failed_rows": []} for check in checks]
    with localcontext() as ctx:
        ctx.prec = 50
        for index, source_row in enumerate(source):
            if cancelled():
                raise AgentToolRejected("agent_cancelled")
            values = {}
            for name, kind in types.items():
                value = source_row.get(sources[name])
                if value in (None, ""):
                    values[name] = defaults[name] if name in defaults else None
                    continue
                if kind == "decimal":
                    value = _clean_decimal(value, decimal_formats[name], row=index + 1, column=name)
                elif kind == "date":
                    value = _clean_date(value, date_formats[name], row=index + 1, column=name)
                elif not isinstance(value, str):
                    reject("agent_table_text_invalid", row=index + 1, column=name)
                values[name] = value
            for name, compiled in formulas:
                evaluated = evaluate(compiled, values, rounding, row=index + 1, column=name)
                if evaluated is None:
                    reject("agent_table_missing_value", row=index + 1, column=name)
                values[name] = _clean_decimal(evaluated, frozenset(), row=index + 1, column=name)
            for check_index, (check, compiled) in enumerate(assertions):
                if check["scope"] == "row":
                    result = evaluate(compiled, values, rounding, row=index + 1, column=check["name"])
                    if type(result) is not bool:
                        reject("agent_table_boolean_check_required", row=index + 1, column=check["name"])
                    outcome = outcomes[check_index]
                    outcome["evaluated"] += 1
                    outcome["failures"] += int(not result)
                    if not result and len(outcome["failed_rows"]) < 8:
                        outcome["failed_rows"].append(index + 1)
            rows.append(values)
        decimal_names = [name for name in names if types.get(name) == "decimal" or any(isinstance(r[name], Decimal) for r in rows)]
        totals = {name: sum((row[name] for row in rows if row[name] is not None), Decimal(0)) for name in decimal_names}
        for check_index, (check, compiled) in enumerate(assertions):
            if check["scope"] == "totals":
                result = evaluate(compiled, {"sum_" + k: v for k, v in totals.items()}, rounding, column=check["name"])
                if type(result) is not bool:
                    reject("agent_table_boolean_check_required", column=check["name"])
                outcomes[check_index].update(evaluated=1, failures=int(not result))
    for outcome in outcomes:
        outcome["passed"] = outcome["failures"] == 0
    def serialize(value):
        return format(value, "f") if isinstance(value, Decimal) else value
    return {**base, "status": "failed" if any(not c["passed"] for c in outcomes) else "passed",
            "columns": names, "decimal_columns": list(totals), "rows": [{k: serialize(v) for k, v in row.items()} for row in rows],
            "row_count": len(rows), "totals": {k: serialize(v) for k, v in totals.items()}, "checks": outcomes[:64],
            "failed_check_count": sum(not c["passed"] for c in outcomes), "declared_check_count": len(checks),
            "rounding": recipe.get("rounding", "not_requested")}


def summary(report):
    # Keep the durable operation result below its reserved receipt capacity.
    # Full values/rules remain in the export; previews must not flood context.
    result = {k: v for k, v in report.items() if k not in {"rows", "recipe", "decimal_columns", "checks"}}
    result["totals_semantics"] = ("Unweighted arithmetic sums of every decimal column, for declared checks only. "
                                  "Percentages, rates and repeated source summaries are not necessarily additive business metrics.")
    result["export_layout"] = ("XLSX Data and CSV contain headers plus data rows only: no automatic totals row. "
                               "The totals metadata is not rendered as company metrics. JSON retains arithmetic metadata.")
    result["checks"] = [{k: v for k, v in check.items() if k != "expression"} for check in report["checks"]]
    result.update(preview_rows=[], rows_truncated=len(report["rows"]) > 8, preview_cells_truncated=False)
    for row in report["rows"][:8]:
        preview = {key: value[:160] if isinstance(value, str) else value for key, value in row.items()}
        if preview != row:
            result["preview_cells_truncated"] = True
        result["preview_rows"].append(preview)
        if len(encoded(result)) > 24000:
            result["preview_rows"].pop()
            result["rows_truncated"] = True
            break
    if len(encoded(result)) > 24000:
        reject("agent_table_summary_limit")
    return result


def _cell_name(col, row):
    name = ""
    while col:
        col, digit = divmod(col - 1, 26)
        name = chr(65 + digit) + name
    return name + str(row)


def _sheet(rows, numeric_columns=()):
    rendered = []
    for r, values in enumerate(rows, 1):
        cells = []
        for c, value in enumerate(values, 1):
            ref = _cell_name(c, r)
            if r > 1 and value is None:
                cells.append(f'<c r="{ref}"/>')
                continue
            if r > 1 and c - 1 in numeric_columns:
                # Excel cannot preserve more than 15 significant digits as numbers.
                if len("".join(map(str, number(value).as_tuple().digits)).rstrip("0")) > 15:
                    reject("agent_table_xlsx_precision_limit", cell=ref)
                cells.append(f'<c r="{ref}"><v>{escape(str(value))}</v></c>')
            else:
                text = str(value)
                if len(text) > 32767:
                    reject("agent_table_xlsx_text_limit", cell=ref)
                if any((ord(ch) < 32 and ch not in "\t\r\n") or 0xD800 <= ord(ch) <= 0xDFFF or ord(ch) in {0xFFFE, 0xFFFF} for ch in text):
                    reject("agent_table_export_invalid_text", cell=ref)
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>')
        rendered.append(f'<row r="{r}">{"".join(cells)}</row>')
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews>'
            '<sheetData>' + "".join(rendered) + '</sheetData></worksheet>')


def export_bytes(report, suffix):
    if report["status"] != "passed":
        reject("agent_table_not_verified", status=report["status"])
    names = report["columns"]
    data = [names, *[[row[name] for name in names] for row in report["rows"]]]
    if suffix == ".json":
        raw = encoded(report)
        if json.loads(raw) != report:
            reject("agent_table_export_verification_failed")
        return raw
    if suffix == ".csv":
        safe = [[("" if value is None else ("'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
                  and (r == 0 or names[c] not in report["decimal_columns"]) else value))
                 for c, value in enumerate(row)] for r, row in enumerate(data)]
        buffer = io.StringIO(newline="")
        csv.writer(buffer).writerows(safe)
        raw = buffer.getvalue().encode("utf-8-sig")
        if list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""))) != safe:
            reject("agent_table_export_verification_failed")
        return raw
    if suffix != ".xlsx":
        reject("agent_table_export_format_invalid")
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    sheets = {
        "Data": _sheet(data, {i for i, name in enumerate(names) if name in report["decimal_columns"]}),
        "Checks": _sheet([["Name", "Result"], ["Calculation", report["status"]],
                           ["Declared checks", str(report["declared_check_count"])],
                           ["Coverage", report["coverage"]], *[[c["name"], encoded(c).decode()] for c in report["checks"]]]),
        "Provenance": _sheet([["Field", "Value"], *[[key, report[key]] for key in
                             ("engine", "input_sha256", "recipe_sha256", "calculation_sha256", "input_provenance", "rounding")],
                             ["recipe", encoded(report["recipe"]).decode()]])}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        entries = {
            "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' + ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, 4)) + '</Types>',
            "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
            "xl/workbook.xml": f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' + ''.join(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>' for i, name in enumerate(sheets, 1)) + '</sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + ''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, 4)) + '</Relationships>',
            **{f"xl/worksheets/sheet{i}.xml": sheet for i, sheet in enumerate(sheets.values(), 1)}}
        for name, text in entries.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text.encode("utf-8"))
    raw = buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        actual = [[cell.findtext(f"{{{ns}}}v") if cell.get("t") != "inlineStr"
                   else cell.findtext(f"{{{ns}}}is/{{{ns}}}t", "") for cell in row]
                  for row in root.findall(f"{{{ns}}}sheetData/{{{ns}}}row")]
        if archive.testzip() or actual != data:
            reject("agent_table_export_verification_failed")
    return raw
