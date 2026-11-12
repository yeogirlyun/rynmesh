"""Actual Decimal transforms, package readback and consumer tool delivery."""
import copy
import io
import json
import zipfile
from decimal import Decimal

import pytest
import test_agent_local_tools as consumer_fixtures
from test_agent_local_tools import execute

from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.agent_runtime.table_work import calculate, export_bytes, summary

# Reuse fixtures only in this module, without registering a global test plugin.
local = consumer_fixtures.local
node = consumer_fixtures.node

def columns(**types):
    return [{"name": name, "type": kind} for name, kind in types.items()]


RECIPE = {"columns": columns(name="text", amount="decimal", fee="decimal"),
          "column_order": ["name", "amount", "fee", "total"],
          "derived": [{"column": "total", "expression": "round(amount + fee, 2)"}],
          "rounding": "half_even", "checks": [
              {"name": "Nonnegative", "scope": "row", "expression": "total >= 0"},
              {"name": "Reconciles", "scope": "totals", "expression": "sum_total == 0.60"}]}
CSV = b'name,amount,fee\nFirst,0.1,0.2\nSecond,0.2,0.1\n'


def test_decimal_math_and_row_total_contract_are_exact():
    report = calculate(CSV, ".csv", RECIPE)
    assert report["status"] == "passed" and report["totals"]["total"] == "0.60"
    assert [row["total"] for row in report["rows"]] == ["0.30", "0.30"]
    assert report["checks"][0]["evaluated"] == 2 and report["checks"][0]["passed"]
    assert report["input_provenance"] == "caller_supplied"
    assert "business rules are not independently verified" in report["coverage"]


def test_missing_input_is_not_zero_and_cannot_be_exported():
    report = calculate(b'name,amount,fee\nFirst,0.1,\n', ".csv", RECIPE)
    assert report["status"] == "needs_input"
    assert report["missing_fields"] == [{"row": 1, "column": "fee"}]
    with pytest.raises(AgentToolRejected, match="agent_table_not_verified"):
        export_bytes(report, ".xlsx")


def test_failed_business_assertion_blocks_output():
    recipe = copy.deepcopy(RECIPE)
    recipe["checks"][-1]["expression"] = "sum_total == 100"
    report = calculate(CSV, ".csv", recipe)
    assert report["status"] == "failed" and report["failed_check_count"] == 1
    with pytest.raises(AgentToolRejected, match="agent_table_not_verified"):
        export_bytes(report, ".csv")


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "True", "not a number", "1e101"])
def test_invalid_numbers_are_rejected(bad):
    with pytest.raises(AgentToolRejected):
        calculate(f'name,amount,fee\nFirst,{bad},0\n'.encode(), ".csv", RECIPE)


@pytest.mark.parametrize("bad", ['__import__("os").getcwd()', '(1).__class__', '10 ** 100000', '[amount for x in []]'])
def test_expressions_never_execute_general_code(bad):
    recipe = copy.deepcopy(RECIPE)
    recipe["derived"][0]["expression"] = bad
    with pytest.raises(AgentToolRejected):
        calculate(CSV, ".csv", recipe)


def test_rounding_and_day_count_are_explicit_and_aliases_support_real_headers():
    recipe = {"columns": [{"name": "本金", "type": "decimal", "source": "Principal amount"},
                          {"name": "开始", "type": "date"}, {"name": "结束", "type": "date"}], "rounding": "half_up",
              "derived": [{"column": "利息", "expression": "round(本金 * 0.05 * days_between(开始, 结束) / 365, 2)"}]}
    report = calculate(json.dumps([{"Principal amount": "1000", "开始": "2026-01-01", "结束": "2026-01-31"}]).encode(), ".json", recipe)
    assert report["rows"][0]["利息"] == "4.11"
    recipe.pop("rounding")
    with pytest.raises(AgentToolRejected):
        calculate(json.dumps([{"Principal amount": "1000", "开始": "2026-01-01", "结束": "2026-01-31"}]).encode(), ".json", recipe)


def test_xlsx_reopens_with_independent_reader_and_has_no_formula_gaps():
    openpyxl = pytest.importorskip("openpyxl")
    report = calculate(CSV, ".csv", RECIPE)
    raw = export_bytes(report, ".xlsx")
    assert raw == export_bytes(report, ".xlsx")
    wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
    assert wb.sheetnames == ["Data", "Checks", "Provenance"]
    assert Decimal(str(wb["Data"]["D2"].value)) == Decimal("0.30")
    assert wb["Data"].max_row == 3 and wb["Data"].freeze_panes == "A2"
    wb.close()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert b"<f>" not in archive.read("xl/worksheets/sheet1.xml")


def test_csv_text_is_protected_without_changing_negative_numbers():
    report = calculate(b'name,amount\n=1+1,-2\n', ".csv", {"columns": columns(name="text", amount="decimal"), "column_order": ["name", "amount"]})
    raw = export_bytes(report, ".csv").decode("utf-8-sig")
    assert "'=1+1,-2" in raw


def test_precision_not_silently_lost_in_excel():
    report = calculate(b'amount\n1234567890123456\n', ".csv", {"columns": columns(amount="decimal")})
    with pytest.raises(AgentToolRejected, match="agent_table_xlsx_precision_limit"):
        export_bytes(report, ".xlsx")


def test_cancellation_and_row_limit():
    with pytest.raises(AgentToolRejected, match="agent_cancelled"):
        calculate(CSV, ".csv", RECIPE, lambda: True)
    with pytest.raises(AgentToolRejected, match="agent_table_input_invalid"):
        calculate(b'amount\n' + b'1\n' * 5001, ".csv", {"columns": columns(amount="decimal")})


def test_actual_tool_exports_then_verifies_and_never_overwrites(local):
    tools, context, workspace, _ = local
    (workspace / "source.csv").write_bytes(CSV)
    args = {"source_path": "source.csv", "recipe": RECIPE}
    report = tools.perform("ryn_table_calculate", args, context, lambda: False)
    export = {**args, "expected_calculation_sha256": report["calculation_sha256"], "path": "report.xlsx"}
    result = execute(tools, context, "ryn_table_export", export, "table-export")
    assert result["readback_verified"] and (workspace / "report.xlsx").is_file()
    receipt = {"tool": "ryn_table_export", "status": "confirmed", "result": result, "arguments": export}
    assert tools.verify(receipt, context, lambda: False)["status"] == "passed"
    with pytest.raises(AgentToolRejected, match="agent_table_new_output_required"):
        tools.preview("ryn_table_export", export, context)


def calculation_receipt(local, **override):
    tools, context, workspace, state = local
    (workspace / "source.csv").write_bytes(CSV)
    args = {"source_path": "source.csv", "recipe": RECIPE}
    report = tools.perform("ryn_table_calculate", args, context, lambda: False)
    row = state.read()["tasks"][context["task_id"]]
    receipt = {"id": "receipt_calc", "operation_id": "calc", "tool": "ryn_table_calculate", "status": "confirmed",
               "goal_revision": row["goal"]["revision"], "scope_revision": row["scope_revision"],
               "context_epoch": row.get("context_epoch", 0), "arguments": copy.deepcopy(args), "result": report, **override}
    state.transaction(lambda data: data["tasks"][context["task_id"]].setdefault("receipts", []).append(receipt))
    return report


def test_export_reuses_exact_calculation_receipt_without_model_repeating_recipe(local):
    tools, context, workspace, _ = local
    report = calculation_receipt(local)
    args = {"calculation_receipt_id": "receipt_calc", "path": "report.xlsx"}
    result = execute(tools, context, "ryn_table_export", args, "receipt-export")
    assert result["readback_verified"] and result["calculation"]["calculation_sha256"] == report["calculation_sha256"]
    assert (workspace / "report.xlsx").read_bytes() == export_bytes(calculate(CSV, ".csv", RECIPE), ".xlsx")
    (workspace / "source.csv").write_bytes(CSV.replace(b"0.1,0.2", b"0.1,0.3"))
    with pytest.raises(AgentToolRejected):
        tools.preview("ryn_table_export", {**args, "path": "changed.xlsx"}, context)
    assert not (workspace / "changed.xlsx").exists()


@pytest.mark.parametrize("override", [{"status": "failed"}, {"goal_revision": 99}, {"scope_revision": 99},
                                      {"context_epoch": 99}, {"result": {"status": "failed"}}])
def test_export_receipt_requires_success_and_current_task_binding(local, override):
    tools, context, _, _ = local
    calculation_receipt(local, **override)
    with pytest.raises(AgentToolRejected, match="agent_table_calculation_receipt_invalid"):
        tools.preview("ryn_table_export", {"calculation_receipt_id": "receipt_calc", "path": "report.xlsx"}, context)


def test_export_rejects_unknown_receipt_mixed_modes_and_missing_explicit_fields(local):
    tools, context, _, _ = local
    for args in [{"calculation_receipt_id": "someone-elses-receipt"}, {"calculation_receipt_id": "receipt_calc", "recipe": RECIPE}, {}]:
        with pytest.raises(AgentToolRejected):
            tools.preview("ryn_table_export", {**args, "path": "report.xlsx"}, context)


def test_changed_input_or_rules_invalidate_export_fingerprint(local):
    tools, context, workspace, _ = local
    source = workspace / "source.csv"
    source.write_bytes(CSV)
    report = tools.perform("ryn_table_calculate", {"source_path": "source.csv", "recipe": RECIPE}, context, lambda: False)
    args = {"source_path": "source.csv", "recipe": RECIPE, "path": "report.csv",
            "expected_calculation_sha256": report["calculation_sha256"]}
    source.write_bytes(CSV.replace(b"0.1", b"0.9"))
    with pytest.raises(AgentToolRejected, match="agent_table_input_changed"):
        tools.preview("ryn_table_export", args, context)
    assert not (workspace / "report.csv").exists()


def test_recipe_order_survives_canonical_storage_and_changed_rules_are_detected():
    canonical = json.loads(json.dumps(RECIPE, sort_keys=True))
    original = calculate(CSV, ".csv", RECIPE)
    restored = calculate(CSV, ".csv", canonical)
    assert export_bytes(original, ".xlsx") == export_bytes(restored, ".xlsx")
    canonical["rounding"] = "half_up"
    assert calculate(CSV, ".csv", canonical)["calculation_sha256"] != original["calculation_sha256"]


def test_table_recipe_uses_strict_object_protocol_and_restores_nested_omissions():
    from jsonschema import Draft202012Validator

    from rynmesh.agent_runtime.local_specs import SPECS
    from rynmesh.llm_package.codex_tools import _response_schema, _validated

    tools = {"ryn_table_calculate": {"name": "ryn_table_calculate", "parameters": SPECS["ryn_table_calculate"][3]}}
    wire = {"content": "", "tool_calls": [{"name": "ryn_table_calculate", "arguments": {
        "source_path": "source.csv", "recipe": {"columns": [{"name": "amount", "type": "decimal", "source": None}],
        "derived": None, "checks": None, "rounding": None, "column_order": None}}}]}
    Draft202012Validator(_response_schema(tools, strict=True)).validate(wire)
    message = _validated({"text": json.dumps(wire)}, tools, False, restore_omitted=True)
    restored = json.loads(message["tool_calls"][0]["function"]["arguments"])
    assert restored["recipe"] == {"columns": [{"name": "amount", "type": "decimal"}]}
    assert calculate(b'amount\n0.1\n', ".csv", restored["recipe"])["totals"] == {"amount": "0.1"}


def test_duplicate_column_names_are_rejected():
    with pytest.raises(AgentToolRejected):
        calculate(CSV, ".csv", {"columns": columns(amount="decimal") * 2})


@pytest.mark.parametrize("patch", [
    {"columns": [{"name": "amount", "type": [], "source": "amount"}]},
    {"rounding": []}, {"derived": [{"column": [], "expression": "1"}]},
    {"checks": [{"name": "invalid", "scope": [], "expression": "1 == 1"}]},
])
def test_malformed_recipe_is_a_clean_tool_rejection(patch):
    with pytest.raises(AgentToolRejected):
        calculate(CSV, ".csv", {**RECIPE, **patch})


def test_ragged_csv_and_extreme_excel_precision_are_rejected():
    with pytest.raises(AgentToolRejected, match="agent_table_input_invalid"):
        calculate(b'amount\n1,2\n', ".csv", {"columns": columns(amount="decimal")})
    report = calculate(b'amount\n10000000000000000000000000000000000000000000000001\n',
                       ".csv", {"columns": columns(amount="decimal")})
    with pytest.raises(AgentToolRejected, match="agent_table_xlsx_precision_limit"):
        export_bytes(report, ".xlsx")


def test_large_text_is_complete_in_delivery_but_bounded_in_model_preview():
    value = "example" * 10000
    report = calculate(json.dumps([{"name": value}]).encode(), ".json", {"columns": columns(name="text")})
    preview = summary(report)
    assert preview["preview_cells_truncated"] and len(preview["preview_rows"][0]["name"]) == 160
    assert len(json.dumps(preview)) < 24000
    assert json.loads(export_bytes(report, ".json"))["rows"][0]["name"] == value


def test_source_change_after_approval_and_workspace_escape_do_not_write(local, tmp_path):
    tools, context, workspace, _ = local
    source = workspace / "source.csv"
    source.write_bytes(CSV)
    args = {"source_path": "source.csv", "recipe": RECIPE, "path": "report.xlsx",
            "expected_calculation_sha256": calculate(CSV, ".csv", RECIPE)["calculation_sha256"]}
    preview = tools.preview("ryn_table_export", args, context)
    source.write_bytes(CSV.replace(b"First", b"Changed"))
    with pytest.raises(AgentToolRejected):
        tools.execute("ryn_table_export", args, operation_id="changed", context=context,
                      expected_preview=preview, cancelled=lambda: False)
    assert not (workspace / "report.xlsx").exists()
    outside = tmp_path / "outside.csv"
    outside.write_bytes(CSV)
    with pytest.raises(AgentToolRejected):
        tools.perform("ryn_table_calculate", {"source_path": str(outside), "recipe": RECIPE}, context, lambda: False)


def test_runtime_discovers_calculates_approves_delivers_and_verifies(node, monkeypatch, tmp_path):
    from test_agent_integrations import add_source, call, fake_upstream, passed_checker

    from rynmesh.agent_runtime.bridge import build_agent_integrations
    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.store import AgentStore
    from rynmesh.services import peer_box

    app, owner = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, owner, owner.home)
    runtime = AgentRuntime(AgentStore(owner.home / "table-agent", peer_box.load_or_create_messaging_key(owner.home / "messaging.x25519")), bridge, tools, catalog)
    runtime.update_settings({"enabled": True, "model_id": model_id, "workspace_root": str(tmp_path),
                             "permission_mode": "confirm_changes", "verify_text_responses": True})
    (tmp_path / "source.csv").write_bytes(CSV)
    args = {"source_path": "source.csv", "recipe": RECIPE}
    export = {**args, "expected_calculation_sha256": calculate(CSV, ".csv", RECIPE)["calculation_sha256"], "path": "report.xlsx"}
    fake_upstream(monkeypatch, [
        call("ryn_discover_tools", json.dumps({"load": ["ryn_table_calculate", "ryn_table_export"]}), "discover"),
        call("ryn_table_calculate", json.dumps(args), "calculate"),
        call("ryn_table_export", json.dumps(export), "export"),
        {"role": "assistant", "content": "Saved report.xlsx with totals 0.60 and passing declared checks."}, passed_checker])
    task = runtime.create({"message": "Calculate source.csv using these declared checks and deliver report.xlsx."})
    for _ in range(20):
        runtime.run_once()
        current = runtime.get(task["id"])
        if current["status"] == "waiting_approval":
            break
    assert current["status"] == "waiting_approval", current.get("error")
    assert not (tmp_path / "report.xlsx").exists()
    runtime.action(task["id"], {"action": "approve", "expected_revision": current["revision"],
                               "approval_id": current["pending_approval"]["id"]})
    for _ in range(20):
        runtime.run_once()
        current = runtime.get(task["id"])
        if current["status"] in {"completed", "failed"}:
            break
    assert current["status"] == "completed" and current["outcome"]["status"] == "verified", current.get("error")
    files = runtime.artifacts(task["id"])["files"]
    file = next(item for item in files if item["name"] == "report.xlsx")
    assert file["status"] == "current" and file["preview"] == "download"
    assert runtime.read_file_artifact(task["id"], file["id"])["status"] == "current"
    assert (tmp_path / "source.csv").read_bytes() == CSV
