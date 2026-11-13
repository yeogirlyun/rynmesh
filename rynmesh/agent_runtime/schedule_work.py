"""Deterministic checks of a declared resource-flow schedule, never a solver.

Each input row is an ordered outflow followed by an inflow. Delayed receipts
must be separate later rows. Business rules and actual settlement are not inferred.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, DecimalException, localcontext

from .store import AgentToolRejected, encoded

FIELDS = {"id", "date", "subject", "outflow", "inflow", "units", "counts_as_operation", "reported_balance"}
REQUIRED = FIELDS - {"reported_balance"}
RULES = {"opening_balance", "unit", "assumptions", "working_weekdays", "excluded_dates", "daily_capacity",
         "period_start", "period_end", "subjects"}


def reject(message):
    raise AgentToolRejected("agent_schedule_input_invalid", correction=message)


def amount(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or len(str(value)) > 256:
        reject("Supply exact decimal strings (or integers), never missing values or floating-point approximations.")
    try:
        number = Decimal(value)
        if not number.is_finite() or abs(number) > Decimal("1e18") or number != number.quantize(Decimal("0.01")):
            reject("Amounts must be finite, at most 1e18 and exactly representable to two decimal places.")
    except DecimalException:
        reject("Supply valid decimal amounts; unknown amounts are not zero.")
    return number


def day(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        reject("Dates must be explicit ISO YYYY-MM-DD; do not infer a year or timezone from the workstation date.")


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            reject("Duplicate source fields are not allowed.")
        result[key] = value
    return result


def load(raw, suffix):
    if len(raw) > 1024 * 1024:
        reject("Schedule source exceeds 1 MiB.")
    try:
        text = raw.decode("utf-8-sig")
        if suffix.lower() == ".json":
            rows = json.loads(text, object_pairs_hook=unique)
        elif suffix.lower() == ".csv":
            reader = csv.DictReader(io.StringIO(text, newline=""))
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                reject("CSV needs unique named columns.")
            rows = list(reader)
        else:
            reject("Use a UTF-8 CSV or JSON array of ordered schedule rows.")
    except (UnicodeError, ValueError, csv.Error):
        reject("The source is not valid UTF-8 CSV or JSON.")
    if not isinstance(rows, list) or len(rows) > 5000:
        reject("Use an array containing at most 5,000 rows.")
    return rows


def validate(raw, suffix, rules, cancelled=lambda: False):
    with localcontext() as context:
        context.prec = 50
        return _validate(raw, suffix, rules, cancelled)


def _validate(raw, suffix, rules, cancelled):
    if (not isinstance(rules, dict) or set(rules) - RULES
            or not {"opening_balance", "unit", "assumptions"} <= rules.keys()):
        reject("Supply opening_balance, a unit label and explicit assumptions. Unknown rules must not be invented.")
    if (not isinstance(rules["unit"], str) or not 1 <= len(rules["unit"]) <= 80
            or not isinstance(rules["assumptions"], list) or not 1 <= len(rules["assumptions"]) <= 16
            or any(not isinstance(s, str) or not 1 <= len(s) <= 400 for s in rules["assumptions"])):
        reject("Use a unit label and 1–16 explicit assumption strings, each at most 400 characters.")
    balance = amount(rules["opening_balance"])
    if balance < 0:
        reject("Opening balance cannot be negative.")
    weekdays = rules.get("working_weekdays")
    if weekdays is not None and (not isinstance(weekdays, list) or not weekdays or len(weekdays) > 7
            or any(type(x) is not int or not 0 <= x <= 6 for x in weekdays) or len(set(weekdays)) != len(weekdays)):
        reject("working_weekdays must contain unique integers 0 (Monday) through 6 (Sunday).")
    exclusions = rules.get("excluded_dates", [])
    if not isinstance(exclusions, list) or len(exclusions) > 3660:
        reject("excluded_dates must be an explicit list of at most 3,660 dates.")
    excluded = {day(value) for value in exclusions}
    capacity = rules.get("daily_capacity")
    if capacity is not None and (type(capacity) is not int or not 1 <= capacity <= 5000):
        reject("daily_capacity must be an integer from 1 to 5,000.")
    start = day(rules["period_start"]) if "period_start" in rules else None
    end = day(rules["period_end"]) if "period_end" in rules else None
    if start and end and not 0 <= (end - start).days <= 3660:
        reject("The stated period must be ordered and at most 3,660 days long.")
    expected = rules.get("subjects")
    subjects = {}
    if expected is not None:
        if not isinstance(expected, list) or not 1 <= len(expected) <= 128:
            reject("subjects must contain 1–128 explicit scoped quantities and optional deadlines.")
        for subject in expected:
            if (not isinstance(subject, dict) or not {"id", "units"} <= subject.keys()
                    or set(subject) - {"id", "units", "deadline"} or not isinstance(subject["id"], str)
                    or not 1 <= len(subject["id"]) <= 160 or subject["id"] in subjects):
                reject("Scoped subject IDs must be unique; supply exact units and optional deadline.")
            units = amount(subject["units"])
            if units < 0:
                reject("Scoped units cannot be negative.")
            subjects[subject["id"]] = (units, day(subject["deadline"]) if "deadline" in subject else None)
    rows = load(raw, suffix)
    counts, progress, daily = Counter(), Counter(), Counter()
    violations, audit, seen = [], [], set()
    total_out, total_in = Decimal(0), Decimal(0)
    prior_date = None

    def fail(code, index=None, **details):
        counts[code] += 1
        if len(violations) < 64:
            violations.append({"code": code, **({"row": index} if index is not None else {}), **details})

    for index, row in enumerate(rows, 1):
        if cancelled():
            raise AgentToolRejected("agent_cancelled")
        if (not isinstance(row, dict) or not REQUIRED <= row.keys() or set(row) - FIELDS
                or any(not isinstance(row[key], str) or not 1 <= len(row[key]) <= 160 for key in ("id", "subject"))
                or row["id"] in seen):
            reject("Each row needs unique id, date, subject, outflow, inflow, units and counts_as_operation; no unknown fields.")
        seen.add(row["id"])
        when = day(row["date"])
        if prior_date and when < prior_date:
            reject("Rows must already be chronological; the validator never silently sorts cash movements.")
        prior_date = when
        counted = row["counts_as_operation"]
        if isinstance(counted, str) and counted.lower() in {"true", "false"}:
            counted = counted.lower() == "true"
        if type(counted) is not bool:
            reject("counts_as_operation must be an explicit boolean matching the declared operation definition.")
        outgoing, incoming, units = (amount(row[key]) for key in ("outflow", "inflow", "units"))
        if min(outgoing, incoming, units) < 0:
            reject("Outflows, inflows and completed units cannot be negative.")
        if counted:
            daily[when] += 1
            if (weekdays is not None and when.weekday() not in weekdays) or when in excluded:
                fail("non_working_day", index, date=when.isoformat())
            if capacity is not None and daily[when] > capacity:
                fail("daily_capacity_exceeded", index, date=when.isoformat())
        if (start and when < start) or (end and when > end):
            fail("outside_period", index, date=when.isoformat())
        if expected is not None:
            if row["subject"] not in subjects:
                fail("out_of_scope", index, subject=row["subject"])
            elif units and subjects[row["subject"]][1] and when > subjects[row["subject"]][1]:
                fail("deadline_missed", index, subject=row["subject"], deadline=subjects[row["subject"]][1].isoformat())
        progress[row["subject"]] += units
        before = balance
        after_out = balance - outgoing
        if outgoing and after_out < 0:
            fail("insufficient_balance", index, available=format(before, ".2f"), required=format(outgoing, ".2f"),
                 shortfall=format(-after_out, ".2f"))
        balance = after_out + incoming
        total_out += outgoing
        total_in += incoming
        if balance < 0:
            fail("negative_closing_balance", index)
        if "reported_balance" in row and amount(row["reported_balance"]) != balance:
            fail("reported_balance_mismatch", index, reported=str(row["reported_balance"]), computed=format(balance, ".2f"))
        audit.append({"id": row["id"], "date": when.isoformat(), "subject": row["subject"],
                      "before": format(before, ".2f"), "outflow": format(outgoing, ".2f"),
                      "after_outflow": format(after_out, ".2f"), "inflow": format(incoming, ".2f"),
                      "after_inflow": format(balance, ".2f"), "units": format(units, ".2f")})
    for sid, (required, _) in subjects.items():
        if progress[sid] != required:
            fail("scoped_units_mismatch", subject=sid, required=format(required, ".2f"), observed=format(progress[sid], ".2f"))
    capacity_report = None
    if start and end and weekdays is not None and capacity is not None:
        def working(d):
            return d.weekday() in weekdays and d not in excluded
        available_days = sum(working(start + timedelta(days=i)) for i in range((end - start).days + 1))
        operations = sum(daily.values())
        capacity_report = {"operation_count": operations, "working_days_in_period": available_days,
                           "available_slots": available_days * capacity, "shortfall_slots": max(0, operations - available_days * capacity),
                           "fixed_count_only": True, "earliest_end_ignoring_cash_deadlines_and_changing_operation_count": None}
        if operations > available_days * capacity:
            fail("period_capacity_shortfall", required=operations, available=available_days * capacity)
        remaining = operations
        for offset in range(3661) if remaining else ():
            try:
                candidate = start + timedelta(days=offset)
            except OverflowError:
                break
            if working(candidate):
                remaining -= capacity
                if remaining <= 0:
                    capacity_report["earliest_end_ignoring_cash_deadlines_and_changing_operation_count"] = candidate.isoformat()
                    break
    return {"engine": "ryn.schedule.v1", "status": "failed" if counts else "passed", "row_count": len(rows),
            "input_sha256": hashlib.sha256(raw).hexdigest(), "rules_sha256": hashlib.sha256(encoded(rules)).hexdigest(),
            "unit": rules["unit"], "opening_balance": format(amount(rules["opening_balance"]), ".2f"),
            "ending_balance": format(balance, ".2f"), "total_outflow": format(total_out, ".2f"), "total_inflow": format(total_in, ".2f"),
            "violation_counts": dict(counts), "violations": violations, "violations_truncated": sum(counts.values()) > len(violations),
            "capacity": capacity_report, "rows": audit, "assumptions": rules["assumptions"],
            "unchecked": [name for name, present in [("working_calendar", weekdays is not None), ("daily_capacity", capacity is not None),
                          ("subject_scope_and_quantities", expected is not None),
                          ("subject_deadlines", bool(subjects) and all(deadline is not None for _, deadline in subjects.values())),
                          ("period", start is not None and end is not None)] if not present],
            "business_rules_verified": False,
            "coverage": "Declared inputs only; each row pays out before receiving inflow. After an invalid payment, later balances are counterfactual diagnostics, not an executable path. Inflow truth, interest rules, bank calendars, optimality and business authorization are not verified."}


def summary(report):
    return {**{key: value for key, value in report.items() if key != "rows"},
            "preview_rows": report["rows"][:8], "rows_truncated": report["row_count"] > 8}
