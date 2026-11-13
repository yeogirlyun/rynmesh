"""Declared resource constraints, independent of any company's loan policy."""
import copy
import json
from datetime import date, timedelta

import pytest
import test_agent_local_tools as consumer_fixtures
from test_codex_object_protocol import assert_strict_subset

from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.schedule_work import summary, validate
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.llm_package.codex_tools import _response_schema

local = consumer_fixtures.local
RULES = {"opening_balance": "100.00", "unit": "resource units", "assumptions": ["A row pays out before its declared inflow"]}


def row(**extra):
    return {"id": "cycle-1", "date": "2026-11-13", "subject": "requested", "outflow": "50.00", "inflow": "40.00",
            "units": "40.00", "counts_as_operation": True, **extra}


def check(rows, rules=None, cancelled=lambda: False):
    return validate(json.dumps(rows).encode(), ".json", rules or RULES, cancelled)


def test_liquidity_checked_before_offsetting_inflow():
    result = check([row(outflow="105.00", inflow="100.00", reported_balance="95.00")])
    assert result["ending_balance"] == "95.00" and result["status"] == "failed"
    assert result["violation_counts"] == {"insufficient_balance": 1}
    assert result["rows"][0]["after_outflow"] == "-5.00"
    assert result["violations"][0]["shortfall"] == "5.00"
    assert "counterfactual" in result["coverage"]


def test_every_operation_reduces_the_actual_pool_not_only_the_last_batch():
    result = check([row(outflow="60.00", inflow="50.00", reported_balance="100.00"),
                    row(id="cycle-2", outflow="95.00", inflow="85.00", reported_balance="80.00")])
    assert result["rows"][1]["before"] == "90.00"
    assert result["ending_balance"] == "80.00"
    assert result["violation_counts"] == {"reported_balance_mismatch": 1, "insufficient_balance": 1}


def test_scope_quantity_deadline_and_cash_are_joint_checks():
    rules = {**RULES, "subjects": [{"id": "requested", "units": "100.00", "deadline": "2026-11-12"}]}
    result = check([row(), row(id="extra", subject="unrequested")], rules)
    assert result["violation_counts"] == {"deadline_missed": 1, "out_of_scope": 1, "scoped_units_mismatch": 1}


def test_fixed_32_operation_calendar_bound_is_not_an_optimization_claim():
    start = date(2026, 11, 13)
    rows = [row(id=f"op-{i}", date=(start + timedelta(days=i // 2)).isoformat(), outflow="0", inflow="0", units="0") for i in range(32)]
    rules = {**RULES, "working_weekdays": [0, 1, 2, 3, 4], "excluded_dates": [], "daily_capacity": 2,
             "period_start": "2026-11-13", "period_end": "2026-11-25"}
    result = check(rows, rules)
    assert result["violation_counts"]["non_working_day"] == 10
    assert result["capacity"]["working_days_in_period"] == 9
    assert result["capacity"]["available_slots"] == 18
    assert result["capacity"]["shortfall_slots"] == 14
    assert result["capacity"]["earliest_end_ignoring_cash_deadlines_and_changing_operation_count"] == "2026-12-04"
    assert result["capacity"]["fixed_count_only"] is True
    assert result["business_rules_verified"] is False


def test_delayed_credit_does_not_fund_an_earlier_payment_and_calendar_exclusions_apply():
    result = check([row(outflow="100", inflow="0"), row(id="second", outflow="1", inflow="0"),
                    row(id="arrival", date="2026-11-16", outflow="0", inflow="100", units="0", counts_as_operation=False)],
                   {**RULES, "working_weekdays": [0, 1, 2, 3, 4], "excluded_dates": ["2026-11-13"], "daily_capacity": 1})
    assert result["violation_counts"]["insufficient_balance"] == 1
    assert result["violation_counts"]["daily_capacity_exceeded"] == 1
    assert result["violation_counts"]["non_working_day"] == 2
    assert result["ending_balance"] == "99.00"


def test_exact_cents_scenario_pass_does_not_verify_business_rules_or_omitted_constraints():
    result = check([row(outflow="0.30", inflow="0.20", reported_balance="99.90")])
    assert result["status"] == "passed" and result["ending_balance"] == "99.90"
    assert result["business_rules_verified"] is False
    assert "working_calendar" in result["unchecked"] and "subject_scope_and_quantities" in result["unchecked"]
    assert result["input_sha256"] and result["rules_sha256"]


@pytest.mark.parametrize("damage", [
    {"outflow": ""}, {"outflow": None}, {"outflow": 0.1}, {"outflow": "NaN"}, {"outflow": "Infinity"},
    {"outflow": "1e99999999"}, {"outflow": "1.001"}, {"inflow": "-1"}, {"date": "20261113"},
    {"counts_as_operation": 0}, {"units": "-1"}, {"unknown": "ignored"},
])
def test_unknown_or_malformed_values_are_not_silently_zero(damage):
    with pytest.raises(AgentToolRejected, match="agent_schedule_input_invalid"):
        check([row(**damage)])


def test_duplicate_ids_unsorted_rows_and_duplicate_source_fields_rejected():
    for rows in [[row(), row()], [row(date="2026-11-16"), row(id="second")]]:
        with pytest.raises(AgentToolRejected):
            check(rows)
    with pytest.raises(AgentToolRejected):
        validate(b'[{"id":"a","id":"b"}]', ".json", RULES)


def test_rules_horizon_and_cancellation_are_bounded():
    for rules in [{**RULES, "daily_capacity": 0}, {**RULES, "working_weekdays": [[]]},
                  {**RULES, "period_start": "2026-11-14", "period_end": "2026-11-13"}, {**RULES, "assumptions": []}]:
        with pytest.raises(AgentToolRejected):
            check([row()], rules)
    with pytest.raises(AgentToolRejected, match="agent_cancelled"):
        check([row()], cancelled=lambda: True)
    result = check([row(date="9999-12-31")], {**RULES, "working_weekdays": [0, 1, 2, 3, 4], "daily_capacity": 1,
                                            "period_start": "9999-12-31", "period_end": "9999-12-31"})
    assert result["capacity"] is not None


def test_summary_retains_complete_counts_with_bounded_previews_and_failures():
    rows = [row(id=f"op-{i}", outflow="101.00", inflow="101.00") for i in range(100)]
    shown = summary(check(rows))
    assert len(shown["preview_rows"]) == 8 and shown["rows_truncated"]
    assert len(shown["violations"]) == 64 and shown["violations_truncated"]
    assert shown["violation_counts"]["insufficient_balance"] == 100


def test_read_only_workspace_tool_and_strict_model_schema(local):
    tools, context, workspace, _ = local
    source = workspace / "schedule.json"
    source.write_text(json.dumps([row(outflow="101", inflow="100")]))
    before = source.read_bytes()
    args = {"source_path": "schedule.json", "rules": copy.deepcopy(RULES)}
    result = tools.perform("ryn_schedule_validate", args, context, lambda: False)
    assert result["violation_counts"]["insufficient_balance"] == 1 and source.read_bytes() == before
    spec = SPECS["ryn_schedule_validate"]
    assert spec[1] == "read"
    assert_strict_subset(_response_schema({"ryn_schedule_validate": {"parameters": spec[3]}}, strict=True))
