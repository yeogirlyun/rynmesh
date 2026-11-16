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
        calculate(b"a\n" + b"1\n" * 5001, ".csv", {"columns": [{"name": "a", "type": "decimal"}]})
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
