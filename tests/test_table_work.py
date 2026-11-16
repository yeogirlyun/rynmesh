"""Acceptance tests for S05-I16: optional columns/defaults, row/col error details, if_else & div, formats, and per-code corrections."""
from __future__ import annotations

import copy
import json

import pytest

from rynmesh.agent_runtime import table_work
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.agent_runtime.table_work import calculate, export_bytes


def test_t1_optional_columns_with_and_without_default_do_not_block_table():
    """T1 (Changes 1, 5): optional column empty values use explicit default or None without triggering needs_input; required column still triggers needs_input."""
    csv_data = b"item,amount,discount,note,paid_on\nA,100,,first,2026-11-01\nB,200,15,,\n"
    recipe = {
        "columns": [
            {"name": "item", "type": "text"},
            {"name": "amount", "type": "decimal"},
            {"name": "discount", "type": "decimal", "optional": True, "default": "0"},
            {"name": "note", "type": "text", "optional": True},
            {"name": "paid_on", "type": "date", "optional": True},
        ],
        "derived": [{"column": "net", "expression": "amount - discount"}],
        "checks": [{"name": "positive_net", "scope": "row", "expression": "net >= 0"}],
    }
    report = calculate(csv_data, ".csv", recipe)
    assert report["status"] == "passed"
    assert report["rows"][0]["discount"] == "0"
    assert report["rows"][0]["net"] == "100"
    assert report["rows"][0]["note"] == "first"
    assert report["rows"][0]["paid_on"] == "2026-11-01"
    assert report["rows"][1]["discount"] == "15"
    assert report["rows"][1]["net"] == "185"
    assert report["rows"][1]["note"] is None
    assert report["rows"][1]["paid_on"] is None
    assert report["totals"]["net"] == "285"
    assert export_bytes(report, ".xlsx")

    # Unused optional decimal column without default stays None and does not break totals or treats missing as 0 in row values.
    unused_opt_recipe = {
        "columns": [
            {"name": "amount", "type": "decimal"},
            {"name": "bonus", "type": "decimal", "optional": True},
        ],
        "derived": [{"column": "doubled", "expression": "amount * 2"}],
    }
    report_opt = calculate(b"amount,bonus\n10,\n20,5\n", ".csv", unused_opt_recipe)
    assert report_opt["status"] == "passed"
    assert report_opt["rows"][0]["bonus"] is None
    assert report_opt["rows"][1]["bonus"] == "5"
    assert report_opt["totals"]["bonus"] == "5"

    # Change 5: non-optional column without default still returns needs_input (missing is never implicitly 0).
    strict_recipe = {
        "columns": [
            {"name": "amount", "type": "decimal"},
            {"name": "discount", "type": "decimal"},
        ]
    }
    missing_report = calculate(b"amount,discount\n100,\n", ".csv", strict_recipe)
    assert missing_report["status"] == "needs_input"
    assert missing_report["missing_fields"] == [{"row": 1, "column": "discount"}]

    # Invalid default type is rejected during column parsing.
    with pytest.raises(AgentToolRejected) as bad_def:
        calculate(b"amount\n10\n", ".csv", {"columns": [{"name": "amount", "type": "decimal", "optional": True, "default": "not-a-decimal"}]})
    assert bad_def.value.args[0] == "agent_table_numeric_input_required"


def test_t2_optional_column_without_default_in_arithmetic_reports_row_and_column():
    """T2 (Changes 1, 2, 4, 5): using an optional column without default in arithmetic when cell is empty raises agent_table_missing_value with row and column."""
    csv_data = b"amount,bonus\n100,10\n200,\n"
    recipe = {
        "columns": [
            {"name": "amount", "type": "decimal"},
            {"name": "bonus", "type": "decimal", "optional": True},
        ],
        "derived": [{"column": "total", "expression": "amount + bonus"}],
    }
    with pytest.raises(AgentToolRejected) as exc_info:
        calculate(csv_data, ".csv", recipe)
    err = exc_info.value
    assert err.args[0] == "agent_table_missing_value"
    assert err.details["row"] == 2
    assert err.details["column"] == "bonus"


def test_t3_division_by_zero_reports_exact_row_and_column():
    """T3 (Changes 2, 4): division by zero raises agent_table_calculation_failed with exact row number and derived/check name."""
    csv_data = b"revenue,units\n100,4\n250,0\n"
    recipe = {
        "columns": [
            {"name": "revenue", "type": "decimal"},
            {"name": "units", "type": "decimal"},
        ],
        "derived": [{"column": "unit_price", "expression": "revenue / units"}],
    }
    with pytest.raises(AgentToolRejected) as exc_info:
        calculate(csv_data, ".csv", recipe)
    err = exc_info.value
    assert err.args[0] == "agent_table_calculation_failed"
    assert err.details["row"] == 2
    assert err.details["column"] == "unit_price"

    # Also verify row check expression failure reports row and check name.
    check_recipe = {
        "columns": [
            {"name": "revenue", "type": "decimal"},
            {"name": "units", "type": "decimal"},
        ],
        "checks": [{"name": "avg_above_ten", "scope": "row", "expression": "(revenue / units) > 10"}],
    }
    with pytest.raises(AgentToolRejected) as check_exc:
        calculate(csv_data, ".csv", check_recipe)
    assert check_exc.value.args[0] == "agent_table_calculation_failed"
    assert check_exc.value.details == {"row": 2, "column": "avg_above_ten"}


def test_t4_div_and_lazy_if_else_results():
    """T4 (Change 3): div(a, b, default) returns default on b==0; if_else(cond, a, b) requires bool cond and evaluates branches lazily."""
    csv_data = b"tier,revenue,units,bonus\nVIP,100,4,20\nSTD,250,0,\n"
    recipe = {
        "columns": [
            {"name": "tier", "type": "text"},
            {"name": "revenue", "type": "decimal"},
            {"name": "units", "type": "decimal"},
            {"name": "bonus", "type": "decimal", "optional": True},
        ],
        "derived": [
            {"column": "unit_price", "expression": "div(revenue, units, 0)"},
            # Lazy evaluation: on row 2 tier != 'VIP', so 'revenue + bonus' (where bonus is None) and 'revenue / units' (0 divisor) are NOT evaluated!
            {"column": "adjusted", "expression": 'if_else(tier == "VIP", revenue + bonus + (revenue / units), revenue)'},
        ],
    }
    report = calculate(csv_data, ".csv", recipe)
    assert report["status"] == "passed"
    assert report["rows"][0]["unit_price"] == "25"
    assert report["rows"][1]["unit_price"] == "0"
    assert report["rows"][0]["adjusted"] == "145"
    assert report["rows"][1]["adjusted"] == "250"

    # Non-boolean condition in if_else must be rejected.
    bad_cond_recipe = {
        "columns": [{"name": "revenue", "type": "decimal"}],
        "derived": [{"column": "out", "expression": "if_else(revenue, 1, 0)"}],
    }
    with pytest.raises(AgentToolRejected) as exc_info:
        calculate(b"revenue\n10\n", ".csv", bad_cond_recipe)
    assert exc_info.value.args[0] == "agent_table_boolean_check_required"


def test_t5_legacy_recipe_output_and_calculation_sha256_unchanged():
    """T5 (Compat): legacy recipe produces the exact same rows, totals, recipe_sha256, and calculation_sha256."""
    import hashlib
    from rynmesh.agent_runtime.store import encoded

    raw_csv = b"name,amount,fee\nFirst,0.1,0.2\nSecond,0.2,0.1\n"
    legacy_recipe = {
        "columns": [
            {"name": "name", "type": "text"},
            {"name": "amount", "type": "decimal"},
            {"name": "fee", "type": "decimal"},
        ],
        "column_order": ["name", "amount", "fee", "total"],
        "derived": [{"column": "total", "expression": "round(amount + fee, 2)"}],
        "rounding": "half_even",
        "checks": [
            {"name": "Nonnegative", "scope": "row", "expression": "total >= 0"},
            {"name": "Reconciles", "scope": "totals", "expression": "sum_total == 0.60"},
        ],
    }
    recipe_snapshot = copy.deepcopy(legacy_recipe)
    expected_calc_sha = hashlib.sha256(raw_csv + b"\x00" + encoded(recipe_snapshot)).hexdigest()
    expected_recipe_sha = hashlib.sha256(encoded(recipe_snapshot)).hexdigest()

    report = calculate(raw_csv, ".csv", legacy_recipe)
    assert legacy_recipe == recipe_snapshot
    assert report["status"] == "passed"
    assert report["calculation_sha256"] == expected_calc_sha
    assert report["recipe_sha256"] == expected_recipe_sha
    assert [row["total"] for row in report["rows"]] == ["0.30", "0.30"]
    assert report["totals"] == {"amount": "0.3", "fee": "0.3", "total": "0.60"}


def test_t6_declared_decimal_and_date_formats_parse_correctly():
    """T6 (Change 6): '1,234.50', '¥1,000', '12.5%', '(300)', and '2026/11/15' (plus Chinese date) parse accurately when format is declared."""
    rows_json = json.dumps([
        {
            "gross": "1,234.50",
            "price": "¥1,000",
            "tax_rate": "12.5%",
            "adjustment": "(300)",
            "trade_date": "2026/11/15",
            "settle_date": "2026年11月20日",
        }
    ]).encode("utf-8")
    recipe = {
        "columns": [
            {"name": "gross", "type": "decimal", "format": "thousands"},
            {"name": "price", "type": "decimal", "format": "currency"},
            {"name": "tax_rate", "type": "decimal", "format": "percent"},
            {"name": "adjustment", "type": "decimal", "format": "paren_negative"},
            {"name": "trade_date", "type": "date", "date_format": "%Y/%m/%d"},
            {"name": "settle_date", "type": "date", "format": "%Y年%m月%d日"},
        ],
        "rounding": "half_up",
        "derived": [
            {"column": "net", "expression": "round(gross + price + adjustment, 2)"},
            {"column": "tax", "expression": "round(price * tax_rate, 2)"},
            {"column": "settle_days", "expression": "days_between(trade_date, settle_date)"},
        ],
    }
    report = calculate(rows_json, ".json", recipe)
    assert report["status"] == "passed"
    row = report["rows"][0]
    assert row["gross"] == "1234.50"
    assert row["price"] == "1000"
    assert row["tax_rate"] == "0.125"
    assert row["adjustment"] == "-300"
    assert row["trade_date"] == "2026-11-15"
    assert row["settle_date"] == "2026-11-20"
    assert row["net"] == "1934.50"
    assert row["tax"] == "125.00"
    assert row["settle_days"] == "5"


@pytest.mark.parametrize(
    "col_type,bad_value,expected_code",
    [
        ("decimal", "1,234.50", "agent_table_numeric_input_required"),
        ("decimal", "¥1,000", "agent_table_numeric_input_required"),
        ("decimal", "12.5%", "agent_table_numeric_input_required"),
        ("decimal", "(300)", "agent_table_numeric_input_required"),
        ("date", "2026/11/15", "agent_table_date_invalid"),
    ],
)
def test_t7_unformatted_columns_reject_formatted_strings_without_guessing(col_type, bad_value, expected_code):
    """T7 (Change 6): without declared format/date_format, formatted strings still raise original errors (never guessed)."""
    payload = json.dumps([{"val": bad_value}]).encode("utf-8")
    recipe = {"columns": [{"name": "val", "type": col_type}]}
    with pytest.raises(AgentToolRejected) as exc_info:
        calculate(payload, ".json", recipe)
    assert exc_info.value.args[0] == expected_code


def test_t8_distinct_corrections_include_row_column_header_and_reason():
    """T8 (Change 7): each error code produces a distinct correction containing row/column/header/reason."""
    seen_corrections = {}

    # 1. agent_table_csv_or_json_required
    with pytest.raises(AgentToolRejected) as e_ext:
        calculate(b"a\n1\n", ".txt", {"columns": [{"name": "a", "type": "text"}]})
    assert e_ext.value.args[0] == "agent_table_csv_or_json_required"
    assert ".csv/.json/.xlsx" in e_ext.value.correction
    seen_corrections["ext"] = e_ext.value.correction

    # 2. agent_table_duplicate_headers (includes duplicate header name)
    with pytest.raises(AgentToolRejected) as e_dup:
        calculate(b"amount,amount\n1,2\n", ".csv", {"columns": [{"name": "amount", "type": "decimal"}]})
    assert e_dup.value.args[0] == "agent_table_duplicate_headers"
    assert "amount" in e_dup.value.correction
    seen_corrections["dup"] = e_dup.value.correction

    # 3. agent_table_date_invalid (includes row and column)
    with pytest.raises(AgentToolRejected) as e_date:
        calculate(b"dt\n2026-01-01\n2026/02/03\n", ".csv", {"columns": [{"name": "dt", "type": "date"}]})
    assert e_date.value.args[0] == "agent_table_date_invalid"
    assert "2" in e_date.value.correction and "dt" in e_date.value.correction and "date_format" in e_date.value.correction
    seen_corrections["date"] = e_date.value.correction

    # 4. agent_table_text_invalid (includes row and column)
    with pytest.raises(AgentToolRejected) as e_txt:
        calculate(json.dumps([{"label": "ok"}, {"label": 123}]).encode(), ".json", {"columns": [{"name": "label", "type": "text"}]})
    assert e_txt.value.args[0] == "agent_table_text_invalid"
    assert "2" in e_txt.value.correction and "label" in e_txt.value.correction
    seen_corrections["txt"] = e_txt.value.correction

    # 5. agent_table_numeric_input_required
    with pytest.raises(AgentToolRejected) as e_num:
        calculate(b"price\n$100\n", ".csv", {"columns": [{"name": "price", "type": "decimal"}]})
    assert e_num.value.args[0] == "agent_table_numeric_input_required"
    assert "format" in e_num.value.correction
    seen_corrections["num"] = e_num.value.correction

    # 6a. agent_table_input_invalid: ragged CSV columns
    with pytest.raises(AgentToolRejected) as e_ragged:
        calculate(b"a,b\n1,2,3\n", ".csv", {"columns": [{"name": "a", "type": "decimal"}]})
    assert e_ragged.value.args[0] == "agent_table_input_invalid"
    assert "表头不一致" in e_ragged.value.correction
    seen_corrections["in_ragged"] = e_ragged.value.correction

    # 6b. agent_table_input_invalid: decode failure
    with pytest.raises(AgentToolRejected) as e_decode:
        calculate(b"\xff\xfe\x00\x80invalid", ".csv", {"columns": [{"name": "a", "type": "text"}]})
    assert e_decode.value.args[0] == "agent_table_input_invalid"
    assert "解码" in e_decode.value.correction
    seen_corrections["in_decode"] = e_decode.value.correction

    # 6c. agent_table_input_invalid: row limit exceeded
    with pytest.raises(AgentToolRejected) as e_rows:
        calculate(b"a\n" + b"1\n" * 100001, ".csv", {"columns": [{"name": "a", "type": "decimal"}]})
    assert e_rows.value.args[0] == "agent_table_input_invalid"
    assert "行数超限" in e_rows.value.correction
    seen_corrections["in_rows"] = e_rows.value.correction

    assert len(set(seen_corrections.values())) == len(seen_corrections)


def test_t9_unknown_error_code_and_missing_detail_keys_fall_back_to_generic_correction():
    """T9 (Change 7): unknown error codes and templates missing format keys fall back to generic correction."""
    generic = "Use declared columns, finite decimal inputs and bounded arithmetic expressions. Missing values are not zero."

    with pytest.raises(AgentToolRejected) as e_unknown:
        table_work.reject("agent_table_custom_unknown_code", foo="bar")
    assert e_unknown.value.args[0] == "agent_table_custom_unknown_code"
    assert e_unknown.value.correction == generic

    # Calling a parameterized code without its required detail key safely falls back instead of KeyError.
    with pytest.raises(AgentToolRejected) as e_missing_key:
        table_work.reject("agent_table_date_invalid")
    assert e_missing_key.value.args[0] == "agent_table_date_invalid"
    assert e_missing_key.value.correction == generic


def _build_ooxml_xlsx(sheets: list[tuple[str, str]], *, shared_strings: list[str] | None = None, styles_xml: str | None = None) -> bytes:
    import io
    import zipfile

    ns_s = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    ns_r = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    sheet_tags = "".join(
        f'<sheet name="{name}" sheetId="{idx}" r:id="rId{idx}"/>'
        for idx, (name, _) in enumerate(sheets, 1)
    )
    rel_tags = "".join(
        f'<Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>'
        for idx in range(1, len(sheets) + 1)
    )
    files = {
        "xl/workbook.xml": f'<workbook {ns_s} {ns_r}><sheets>{sheet_tags}</sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rel_tags}</Relationships>',
    }
    for idx, (_, body_xml) in enumerate(sheets, 1):
        files[f"xl/worksheets/sheet{idx}.xml"] = f'<worksheet {ns_s}><sheetData>{body_xml}</sheetData></worksheet>'
    if shared_strings is not None:
        si_xml = "".join(f"<si><t>{s}</t></si>" for s in shared_strings)
        files["xl/sharedStrings.xml"] = f"<sst {ns_s}>{si_xml}</sst>"
    if styles_xml is not None:
        files["xl/styles.xml"] = styles_xml
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buf.getvalue()


def _make_local_tools(tmp_path):
    from types import SimpleNamespace
    from rynmesh.agent_runtime.local_tools import LocalTools
    from rynmesh.agent_runtime.operation_ledger import OperationLedger

    ws = tmp_path / "ws"
    ws.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    state = {"settings": {"enabled": True, "workspace_root": str(ws), "desktop_access": False, "permission_mode": "workspace"}, "tasks": {}}
    tools = LocalTools(SimpleNamespace(home=home), lambda: OperationLedger(home))
    tools.store = SimpleNamespace(inspect=lambda fn: fn(state), read=lambda: state, default_workspace=lambda: str(ws))
    ctx = {"task_id": "t_local", "local_execution": {"workspace_root": str(ws), "desktop_access": False}}
    return tools, ctx, ws


def test_s05_i15_t1_xlsx_first_and_named_sheet_match_csv_and_export(tmp_path):
    from rynmesh.agent_runtime import file_preview
    from rynmesh.agent_runtime.local_specs import SPECS

    assert hasattr(file_preview, "sheet_rows")
    spec_desc = SPECS["ryn_table_calculate"][0].lower()
    assert "xlsx" in spec_desc and ("cached" in spec_desc or "缓存" in SPECS["ryn_table_calculate"][0])

    sheet1_xml = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr"><is><t>amount</t></is></c><c r="C1" t="inlineStr"><is><t>fee</t></is></c></row>'
        '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2"><v>100.50</v></c><c r="C2"><f>B2*0.1</f><v>10.05</v></c></row>'
        '<row r="3"><c r="A3" t="s"><v>2</v></c><c r="B3"><v>200.00</v></c><c r="C3"><v>20.00</v></c></row>'
    )
    sheet2_xml = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr"><is><t>amount</t></is></c><c r="C1" t="inlineStr"><is><t>fee</t></is></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>Q2-Item</t></is></c><c r="B2"><v>500.25</v></c><c r="C2"><v>12.75</v></c></row>'
    )
    xlsx_raw = _build_ooxml_xlsx(
        [("一季度", sheet1_xml), ("二季度", sheet2_xml)],
        shared_strings=["item", "Alpha", "Beta"],
    )
    csv_q1 = b"item,amount,fee\nAlpha,100.50,10.05\nBeta,200.00,20.00\n"
    csv_q2 = b"item,amount,fee\nQ2-Item,500.25,12.75\n"

    recipe = {
        "columns": [
            {"name": "item", "type": "text"},
            {"name": "amount", "type": "decimal"},
            {"name": "fee", "type": "decimal"},
        ],
        "derived": [{"column": "total", "expression": "amount + fee"}],
    }
    rep_xlsx_1 = calculate(xlsx_raw, ".xlsx", recipe)
    rep_csv_1 = calculate(csv_q1, ".csv", recipe)
    assert rep_xlsx_1["status"] == rep_csv_1["status"] == "passed"
    assert rep_xlsx_1["rows"] == rep_csv_1["rows"]
    assert rep_xlsx_1["totals"] == rep_csv_1["totals"]

    rep_xlsx_2 = calculate(xlsx_raw, ".xlsm", {**recipe, "sheet": "二季度"})
    rep_csv_2 = calculate(csv_q2, ".csv", recipe)
    assert rep_xlsx_2["rows"] == rep_csv_2["rows"]
    assert rep_xlsx_2["totals"] == rep_csv_2["totals"]

    # Duplicate headers in .xlsx raise agent_table_duplicate_headers.
    dup_sheet_xml = (
        '<row r="1"><c r="A1" t="inlineStr"><is><t>amount</t></is></c><c r="B1" t="inlineStr"><is><t>amount</t></is></c></row>'
        '<row r="2"><c r="A2"><v>1</v></c><c r="B2"><v>2</v></c></row>'
    )
    with pytest.raises(AgentToolRejected) as dup_exc:
        calculate(_build_ooxml_xlsx([("Dup", dup_sheet_xml)]), ".xlsx", {"columns": [{"name": "amount", "type": "decimal"}]})
    assert dup_exc.value.args[0] == "agent_table_duplicate_headers"

    # ryn_table_export preview and perform support .xlsx source directly via LocalTools.
    tools, ctx, ws = _make_local_tools(tmp_path)
    (ws / "source.xlsx").write_bytes(xlsx_raw)
    named_recipe = {**recipe, "sheet": "二季度"}
    calc_summary = tools.perform("ryn_table_calculate", {"source_path": "source.xlsx", "recipe": named_recipe}, ctx, lambda: False)
    export_args = {
        "source_path": "source.xlsx",
        "recipe": named_recipe,
        "expected_calculation_sha256": calc_summary["calculation_sha256"],
        "path": "out.csv",
    }
    prev = tools.preview("ryn_table_export", export_args, ctx)
    assert prev["calculation"]["calculation_sha256"] == calc_summary["calculation_sha256"]
    exported = tools.perform("ryn_table_export", export_args, ctx, lambda: False, operation_id="op_xlsx_export")
    assert exported["readback_verified"] and (ws / "out.csv").is_file()

    # Legacy binary .xls remains unsupported with agent_table_csv_or_json_required.
    with pytest.raises(AgentToolRejected) as xls_exc:
        calculate(b"\xd0\xcf\x11\xe0binary", ".xls", recipe)
    assert xls_exc.value.args[0] == "agent_table_csv_or_json_required"


def test_s05_i15_t2_xlsx_date_cells_read_as_iso_dates():
    ns_s = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    styles = (
        f'<styleSheet {ns_s}>'
        '<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy&quot;年&quot;m&quot;月&quot;d&quot;日&quot;"/></numFmts>'
        '<cellXfs><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="164"/></cellXfs>'
        '</styleSheet>'
    )
    sheet_xml = (
        '<row r="1"><c r="A1" t="inlineStr"><is><t>start_on</t></is></c><c r="B1" t="inlineStr"><is><t>end_on</t></is></c></row>'
        '<row r="2"><c r="A2" s="1"><v>45292</v></c><c r="B2" s="2"><v>45322</v></c></row>'
    )
    xlsx_raw = _build_ooxml_xlsx([("Dates", sheet_xml)], styles_xml=styles)
    recipe = {
        "columns": [
            {"name": "start_on", "type": "date"},
            {"name": "end_on", "type": "date"},
        ],
        "derived": [{"column": "days", "expression": "days_between(start_on, end_on)"}],
    }
    report = calculate(xlsx_raw, ".xlsx", recipe)
    assert report["status"] == "passed"
    assert report["rows"][0] == {"start_on": "2024-01-01", "end_on": "2024-01-31", "days": "30"}


def test_s05_i15_t3_uncalculated_formula_raises_missing_value_with_row_and_column():
    sheet_xml = (
        '<row r="1"><c r="A1" t="inlineStr"><is><t>price</t></is></c><c r="B1" t="inlineStr"><is><t>subtotal</t></is></c></row>'
        '<row r="2"><c r="A2"><v>10</v></c><c r="B2"><f>A2*2</f><v>20</v></c></row>'
        '<row r="3"><c r="A3"><v>15</v></c><c r="B3"><f>A3*2</f></c></row>'
    )
    xlsx_raw = _build_ooxml_xlsx([("Sheet1", sheet_xml)])
    recipe = {
        "columns": [
            {"name": "price", "type": "decimal"},
            {"name": "subtotal", "type": "decimal"},
        ]
    }
    with pytest.raises(AgentToolRejected) as exc_info:
        calculate(xlsx_raw, ".xlsx", recipe)
    assert exc_info.value.args[0] == "agent_table_formula_value_missing"
    assert exc_info.value.details["row"] == 2
    assert exc_info.value.details["column"] == "subtotal"


def test_s05_i15_t4_gbk_csv_matches_utf8_and_sets_input_encoding_gb18030():
    csv_text = "门店,销售额,费用\n上海静安,1200.50,200.25\n北京朝阳,800.00,100.75\n"
    gbk_bytes = csv_text.encode("gbk")
    utf8_bytes = csv_text.encode("utf-8")
    utf16_bytes = csv_text.encode("utf-16")
    recipe = {
        "columns": [
            {"name": "shop", "type": "text", "source": "门店"},
            {"name": "sales", "type": "decimal", "source": "销售额"},
            {"name": "cost", "type": "decimal", "source": "费用"},
        ],
        "derived": [{"column": "profit", "expression": "sales - cost"}],
    }
    rep_gbk = calculate(gbk_bytes, ".csv", recipe)
    rep_utf8 = calculate(utf8_bytes, ".csv", recipe)
    rep_utf16 = calculate(utf16_bytes, ".csv", recipe)

    assert rep_gbk["input_encoding"] == "gb18030"
    assert rep_utf8["input_encoding"] == "utf-8"
    assert rep_utf16["input_encoding"] in {"utf-16-le", "utf-16-be"}
    assert rep_gbk["rows"] == rep_utf8["rows"] == rep_utf16["rows"]
    assert rep_gbk["totals"] == rep_utf8["totals"] == rep_utf16["totals"]


def test_s05_i15_t5_60000_row_csv_passes_with_200_preview_rows_and_streams_export(tmp_path):
    lines = ["id,amount,fee\n"] + [f"row_{i},10.25,1.75\n" for i in range(1, 60001)]
    big_csv = "".join(lines).encode("utf-8")
    assert len(big_csv) > 1024 * 1024  # > 1 MiB, verifying 64 MiB source read too

    recipe = {
        "columns": [
            {"name": "id", "type": "text"},
            {"name": "amount", "type": "decimal"},
            {"name": "fee", "type": "decimal"},
        ],
        "derived": [{"column": "total", "expression": "amount + fee"}],
    }
    report = calculate(big_csv, ".csv", recipe)
    assert report["status"] == "passed"
    assert report["row_count"] == 60000
    assert len(report["rows"]) == 200
    assert report["rows"][0]["id"] == "row_1"
    assert report["rows"][199]["id"] == "row_200"
    assert report["totals"]["total"] == "720000.00"

    # Full 60,000 rows remain exportable via export_bytes AND LocalTools (>1 MiB source via 64 MiB read_window).
    exported_csv = export_bytes(report, ".csv")
    exported_lines = exported_csv.decode("utf-8-sig").splitlines()
    assert len(exported_lines) == 60001
    assert exported_lines[1].startswith("row_1,")
    assert exported_lines[-1].startswith("row_60000,")

    tools, ctx, ws = _make_local_tools(tmp_path)
    (ws / "big.csv").write_bytes(big_csv)
    calc_res = tools.perform("ryn_table_calculate", {"source_path": "big.csv", "recipe": recipe}, ctx, lambda: False)
    assert calc_res["row_count"] == 60000 and calc_res["rows_truncated"] is True
    exp_res = tools.perform(
        "ryn_table_export",
        {"source_path": "big.csv", "recipe": recipe, "expected_calculation_sha256": calc_res["calculation_sha256"], "path": "big_out.csv"},
        ctx,
        lambda: False,
        operation_id="op_big_csv",
    )
    assert exp_res["readback_verified"] and (ws / "big_out.csv").read_bytes() == exported_csv

    # Oversized .xlsx (>8 MiB) continues to raise agent_table_output_limit.
    import base64
    import os

    incompressible_rows = [base64.a85encode(os.urandom(18000)).decode("ascii").replace(",", "_").replace('"', "_") for _ in range(480)]
    huge_xlsx_src = ("payload\n" + "\n".join(incompressible_rows) + "\n").encode("utf-8")
    (ws / "huge.csv").write_bytes(huge_xlsx_src)
    huge_recipe = {"columns": [{"name": "payload", "type": "text"}]}
    huge_calc = tools.perform("ryn_table_calculate", {"source_path": "huge.csv", "recipe": huge_recipe}, ctx, lambda: False)
    with pytest.raises(AgentToolRejected, match="agent_table_output_limit"):
        tools.preview(
            "ryn_table_export",
            {"source_path": "huge.csv", "recipe": huge_recipe, "expected_calculation_sha256": huge_calc["calculation_sha256"], "path": "huge.xlsx"},
            ctx,
        )


def test_s05_i15_t6_100001_rows_and_wide_recipe_limits_report_cap_and_guidance():
    over_csv = b"amount\n" + b"1\n" * 100001
    with pytest.raises(AgentToolRejected) as row_exc:
        calculate(over_csv, ".csv", {"columns": [{"name": "amount", "type": "decimal"}]})
    assert row_exc.value.args[0] == "agent_table_input_invalid"
    assert "100000" in row_exc.value.correction
    assert "先筛选或分表" in row_exc.value.correction

    # 128 columns + 64 derived columns succeed; 129 columns and 65 derived columns reject with cap & guidance.
    wide_headers = ",".join(f"c{i}" for i in range(128))
    wide_row = ",".join("1" for _ in range(128))
    wide_csv = f"{wide_headers}\n{wide_row}\n".encode("utf-8")
    wide_recipe = {
        "columns": [{"name": f"c{i}", "type": "decimal"} for i in range(128)],
        "derived": [{"column": f"d{j}", "expression": f"c{j} + 1"} for j in range(64)],
    }
    wide_report = calculate(wide_csv, ".csv", wide_recipe)
    assert wide_report["status"] == "passed"
    assert wide_report["totals"]["d0"] == "2"

    with pytest.raises(AgentToolRejected) as col_exc:
        calculate(
            b"c0\n1\n",
            ".csv",
            {"columns": [{"name": f"c{i}", "type": "decimal"} for i in range(129)]},
        )
    assert "128" in col_exc.value.correction and "先筛选或分表" in col_exc.value.correction

    with pytest.raises(AgentToolRejected) as der_exc:
        calculate(
            b"c0\n1\n",
            ".csv",
            {
                "columns": [{"name": "c0", "type": "decimal"}],
                "derived": [{"column": f"d{j}", "expression": "c0 + 1"} for j in range(65)],
            },
        )
    assert "64" in der_exc.value.correction and "先筛选或分表" in der_exc.value.correction

