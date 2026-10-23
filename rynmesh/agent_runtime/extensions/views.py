"""Bounded, declarative tool views. No executable content or model dispatch."""
from __future__ import annotations

import copy
import json
import math
import re
from datetime import date
from typing import Any

from jsonschema.validators import validator_for
from referencing import Registry

from ..store import AgentError

INVALID = "agent_invalid_plugin_view"
_FORBIDDEN = {"__proto__", "prototype", "constructor"}
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
_PARAM = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_FORMATS = {"text", "number", "money"}
_SAFE_ERRORS = {
    INVALID, "agent_invalid_request", "agent_plugin_not_found", "agent_plugin_disabled",
    "agent_plugin_view_not_found", "agent_plugin_view_not_read_only", "agent_plugin_view_query_failed",
    "agent_plugin_view_unsupported_server",
    "agent_plugin_view_read_only_confirmation_required",
    "agent_plugin_view_result_invalid", "agent_plugin_view_result_limit", "agent_mcp_auth_required",
    "agent_mcp_server_not_found", "agent_mcp_server_disabled", "agent_mcp_tool_disabled",
    "agent_stale_revision", "agent_extensions_revision_conflict", "agent_mcp_timeout",
    "agent_mcp_output_limit", "agent_cancelled",
}


def _fail(code=INVALID):
    raise AgentError(code)


def _object(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        _fail()
    return value


def _text(value, limit=120, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or (not empty and not value.strip())
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        _fail()
    return value


def _name(value, *, parameter=False, limit=128):
    _text(value, limit)
    if not (_PARAM if parameter else _NAME).fullmatch(value) or any(x.casefold() in _FORBIDDEN for x in value.split(".")):
        _fail()
    return value


def safe_path(value):
    """Property traversal only; no expressions, indices, attributes or prototype keys."""
    _text(value, 256)
    parts = value.split(".")
    if len(parts) > 8 or any(not part or len(part) > 64 or part.casefold() in _FORBIDDEN
                            or not all(c.isalnum() or c in "_-" for c in part) for part in parts):
        _fail()
    return value


def _list(value, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        _fail()
    return value


def normalize_views(value: Any, server_ids: dict[str, str]) -> list[dict[str, Any]]:
    """Resolve package-local server keys; caller supplies only this installation's servers."""
    result, ids = [], set()
    for raw in _list(value, 8):
        _object(raw, {"id", "title", "mcpServer", "tool", "inputs", "columns", "rowsPath", "pagination"},
                {"description", "summaryFields"})
        view_id = _name(raw["id"], limit=64)
        server = _name(raw["mcpServer"])
        if view_id in ids or server not in server_ids:
            _fail()
        ids.add(view_id)
        item = {"id": view_id, "title": _text(raw["title"]),
                "description": _text(raw.get("description", ""), 500, empty=True),
                "mcpServer": _name(server_ids[server]), "tool": _name(raw["tool"]),
                "inputs": [], "columns": [], "rowsPath": safe_path(raw["rowsPath"]), "summaryFields": []}
        names = set()
        for field in _list(raw["inputs"], 16):
            _object(field, {"name", "label", "type"}, {"required", "default"})
            name = _name(field["name"], parameter=True)
            if name in names or field["type"] not in ("text", "date") or type(field.get("required", False)) is not bool:
                _fail()
            names.add(name)
            entry = {"name": name, "label": _text(field["label"]), "type": field["type"],
                     "required": field.get("required", False)}
            if "default" in field:
                if field["type"] != "date" or field["default"] not in ("today", "month_start"):
                    _fail()
                entry["default"] = field["default"]
            item["inputs"].append(entry)
        for key, maximum, path_key in (("columns", 32, "key"), ("summaryFields", 16, "path")):
            paths = set()
            for field in _list(raw.get(key, []), maximum):
                _object(field, {path_key, "label"}, {"format"})
                path = safe_path(field[path_key])
                if path in paths or not isinstance(field.get("format", "text"), str) or field.get("format", "text") not in _FORMATS:
                    _fail()
                paths.add(path)
                item[key].append({path_key: path, "label": _text(field["label"]), "format": field.get("format", "text")})
        if not item["columns"]:
            _fail()
        p = _object(raw["pagination"], {"pageArgument", "sizeArgument", "path", "pageSize"})
        page_arg, size_arg = _name(p["pageArgument"], parameter=True), _name(p["sizeArgument"], parameter=True)
        if page_arg == size_arg or {page_arg, size_arg} & names or type(p["pageSize"]) is not int or p["pageSize"] != 20:
            _fail()
        item["pagination"] = {"pageArgument": page_arg, "sizeArgument": size_arg, "path": safe_path(p["path"]), "pageSize": 20}
        result.append(item)
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 64 * 1024:
        _fail()
    return result


def _arguments(view, payload):
    if not isinstance(payload, dict) or set(payload) != {"arguments", "page", "expected_instance_id", "expected_revision"}:
        _fail("agent_invalid_request")
    raw, page = payload["arguments"], payload["page"]
    if not isinstance(raw, dict) or set(raw) - {x["name"] for x in view["inputs"]} or type(page) is not int or not 1 <= page <= 100000:
        _fail("agent_invalid_request")
    result = {}
    for field in view["inputs"]:
        val = raw.get(field["name"])
        if field["name"] not in raw and "default" in field:
            today = date.today()
            val = (today.replace(day=1) if field["default"] == "month_start" else today).isoformat()
        if val is None and field["name"] not in raw:
            if field["required"]:
                _fail("agent_invalid_request")
            continue
        if not isinstance(val, str) or len(val) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in val):
            _fail("agent_invalid_request")
        if not val.strip():
            if field["required"]:
                _fail("agent_invalid_request")
            continue
        if field["type"] == "date":
            try:
                if not _DATE.fullmatch(val):
                    raise ValueError
                date.fromisoformat(val)
            except ValueError:
                _fail("agent_invalid_request")
        result[field["name"]] = val
    result[view["pagination"]["pageArgument"]] = page
    result[view["pagination"]["sizeArgument"]] = 20
    return result


def _prepare_tool_arguments(view, schema, arguments):
    call_args = dict(arguments)
    if (
        isinstance(schema, dict)
        and schema.get("additionalProperties") is False
        and isinstance(schema.get("properties"), dict)
    ):
        props = schema["properties"]
        for key in (view["pagination"]["pageArgument"], view["pagination"]["sizeArgument"]):
            if key not in props:
                call_args.pop(key, None)
    return call_args


def _normalize_legacy_view_data(view, result, page: int):
    if not isinstance(result, dict):
        return result
    tool_name = view.get("tool")
    if tool_name not in {
        "journal_records",
        "pl_report",
        "cashFlow_dailyDetail",
        "balance_list",
        "balance_overview",
        "pl_financialDashboard",
    }:
        return result
    data = result.get("data")
    if isinstance(data, dict) and isinstance(data.get("pagination"), dict):
        return result
    if data is None and result.get("truncated") is True:
        ev = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
        total = ev.get("rowCount") if isinstance(ev.get("rowCount"), int) and ev.get("rowCount") >= 0 else 0
        result["data"] = {
            "count": total,
            "rows": [],
            "truncated": True,
            "summary": {"dayCount": total, "categoryCount": total},
            "reportSummary": {"rowCount": total, "totals": {}},
            "pagination": {"page": page, "pageSize": 20, "total": total, "totalPages": (total + 19) // 20},
        }
        return result
    if tool_name == "pl_report" and isinstance(data, list):
        total = len(data)
        result["data"] = {
            "rows": data[(page - 1) * 20 : page * 20],
            "pagination": {"page": page, "pageSize": 20, "total": total, "totalPages": (total + 19) // 20},
        }
        return result
    if not isinstance(data, dict):
        return result
    if tool_name in ("journal_records", "balance_list"):
        rows = data.get("rows") if isinstance(data.get("rows"), list) else []
        total = data.get("count") if isinstance(data.get("count"), int) else len(rows)
        data["rows"] = rows
        data["pagination"] = {"page": page, "pageSize": 20, "total": total, "totalPages": (total + 19) // 20}
    elif tool_name == "pl_report":
        rows = data.get("rows") if isinstance(data.get("rows"), list) else (data.get("profit") if isinstance(data.get("profit"), list) else [])
        summary_obj = data.get("reportSummary") if isinstance(data.get("reportSummary"), dict) else {}
        total = summary_obj.get("rowCount") if isinstance(summary_obj.get("rowCount"), int) else len(rows)
        data["rows"] = rows[(page - 1) * 20 : page * 20]
        data["pagination"] = {"page": page, "pageSize": 20, "total": total, "totalPages": (total + 19) // 20}
    elif tool_name == "cashFlow_dailyDetail":
        dept = "全部团队"
        mbd = data.get("metricsByDepartment")
        if isinstance(mbd, list) and mbd and isinstance(mbd[0], dict) and mbd[0].get("department"):
            dept = str(mbd[0]["department"])
        raw_rows = data.get("rows") if isinstance(data.get("rows"), list) else []
        norm_rows = []
        for r in raw_rows:
            if not isinstance(r, dict):
                continue
            by_day = r.get("byDay")
            day_count = r.get("dayCount") if isinstance(r.get("dayCount"), int) else (len(by_day) if isinstance(by_day, dict) else 0)
            norm_rows.append({
                "department": r.get("department") or dept,
                "categoryLabel": r.get("categoryLabel"),
                "category": r.get("category"),
                "total": r.get("total"),
                "dayCount": day_count,
            })
        metrics = data.get("metrics") if isinstance(data.get("metrics"), list) else []
        def _metric_val(k, fallback=0):
            for m in metrics:
                if isinstance(m, dict) and m.get("key") == k:
                    v = m.get("value")
                    if isinstance(v, (int, float, str)):
                        return v
            return fallback
        total = len(norm_rows)
        data["rows"] = norm_rows[(page - 1) * 20 : page * 20]
        if not isinstance(data.get("summary"), dict):
            data["summary"] = {
                "categoryCount": total,
                "cashFlowSummary": _metric_val("cashFlowSummary", data.get("total", 0)),
                "laborCost": _metric_val("laborCost", 0),
                "cashFlowProfit": _metric_val("cashFlowProfit", 0),
            }
        data["pagination"] = {"page": page, "pageSize": 20, "total": total, "totalPages": (total + 19) // 20}
    elif tool_name == "balance_overview":
        rows = data.get("rows") if isinstance(data.get("rows"), list) else (data.get("totalsByCurrency") if isinstance(data.get("totalsByCurrency"), list) else [])
        total = len(rows)
        data["rows"] = rows[(page - 1) * 20 : page * 20]
        data["pagination"] = {"page": page, "pageSize": 20, "total": total, "totalPages": (total + 19) // 20}
    elif tool_name == "pl_financialDashboard":
        platforms = ("Amazon", "Aliexpress", "Otto", "Kaufland", "Cdiscount", "Worten", "Coupang")
        day_keys = sorted(k for k, v in data.items() if isinstance(k, str) and _DATE.fullmatch(k) and isinstance(v, dict))
        rows = []
        for day in day_keys:
            entry = data[day]
            glob = entry.get("Global") if isinstance(entry.get("Global"), dict) else {}
            def _sum_plat(field):
                return round(sum(float((entry.get(p) or {}).get(field) or 0) for p in platforms if isinstance(entry.get(p), dict)), 2)
            rows.append({
                "date": day,
                "totalSales": entry.get("totalSales") if entry.get("totalSales") is not None else _sum_plat("销售金额"),
                "totalPayout": entry.get("totalPayout") if entry.get("totalPayout") is not None else _sum_plat("回款金额"),
                "totalPurchase": entry.get("totalPurchase") if entry.get("totalPurchase") is not None else _sum_plat("采购金额"),
                "totalCost": glob.get("总成本") if glob.get("总成本") is not None else (entry.get("totalCost") or 0),
                "profit": glob.get("利润") if glob.get("利润") is not None else (entry.get("profit") or 0),
                "availableFund": glob.get("可用资金") if glob.get("可用资金") is not None else (entry.get("availableFund") or 0),
                "inTransitFund": glob.get("在途资金") if glob.get("在途资金") is not None else (entry.get("inTransitFund") or 0),
                "thirdPartyFund": glob.get("三方资金") if glob.get("三方资金") is not None else (entry.get("thirdPartyFund") or 0),
                "frozenFund": glob.get("冻结资金") if glob.get("冻结资金") is not None else (entry.get("frozenFund") or 0),
            })
        total = len(rows)
        data["rows"] = rows[(page - 1) * 20 : page * 20]
        if not isinstance(data.get("summary"), dict):
            data["summary"] = {
                "dayCount": total,
                "totalSales": round(sum(float(r.get("totalSales") or 0) for r in rows), 2),
                "totalPayout": round(sum(float(r.get("totalPayout") or 0) for r in rows), 2),
                "totalPurchase": round(sum(float(r.get("totalPurchase") or 0) for r in rows), 2),
                "totalCost": round(sum(float(r.get("totalCost") or 0) for r in rows), 2),
                "totalProfit": round(sum(float(r.get("profit") or 0) for r in rows), 2),
                "latestAvailableFund": rows[-1]["availableFund"] if rows else 0,
            }
        data["pagination"] = {"page": page, "pageSize": 20, "total": total, "totalPages": (total + 19) // 20}
    return result


def _validate_arguments(schema, arguments):
    # An MCP-supplied schema must never cause network retrieval during validation.
    def no_resources(uri):
        raise ValueError("external_schema_disabled")
    try:
        validator = validator_for(schema)
        validator.check_schema(schema)
        validator(schema, registry=Registry(retrieve=no_resources)).validate(arguments)
    except Exception:
        _fail("agent_invalid_request")


def _result(raw):
    if not isinstance(raw, dict) or raw.get("isError") or not isinstance(raw.get("structuredContent"), dict):
        _fail("agent_plugin_view_query_failed")
    result = raw["structuredContent"]
    if result.get("success") is False:
        _fail("agent_plugin_view_query_failed")
    # Validate before serializing: bounded traversal also rejects non-JSON data.
    nodes = 0
    secret_keys = {"access_token", "refresh_token", "id_token", "client_secret", "authorization", "cookie", "secret", "password"}
    def visit(value, depth):
        nonlocal nodes
        nodes += 1
        if depth > 16 or nodes > 50000:
            _fail("agent_plugin_view_result_limit")
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > 256 or key.casefold() in _FORBIDDEN | secret_keys:
                    _fail("agent_plugin_view_result_invalid")
                visit(child, depth + 1)
        elif isinstance(value, list):
            if len(value) > 1000:
                _fail("agent_plugin_view_result_limit")
            for child in value:
                visit(child, depth + 1)
        elif isinstance(value, str):
            if len(value) > 65536:
                _fail("agent_plugin_view_result_limit")
        elif value is not None and type(value) not in (bool, int, float):
            _fail("agent_plugin_view_result_invalid")
        elif type(value) is float and not math.isfinite(value):
            _fail("agent_plugin_view_result_invalid")
    visit(result, 0)
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 2 * 1024 * 1024:
        _fail("agent_plugin_view_result_limit")
    return copy.deepcopy(result)


def query_view(store, manager, plugin_id: str, view_id: str, payload: Any):
    """Execute only the current declared read-only tool; never persist its result."""
    try:
        initial = store.get_plugin_view_internal(plugin_id, view_id)
        view, server, tool = initial["view"], initial["server"], initial["tool"]
        arguments = _arguments(view, payload)
        if (payload["expected_instance_id"] != initial["plugin_instance_id"]
                or type(payload["expected_revision"]) is not int
                or payload["expected_revision"] != initial["plugin_revision"]):
            _fail("agent_stale_revision")
        call_arguments = _prepare_tool_arguments(view, tool["input_schema"], arguments)
        _validate_arguments(tool["input_schema"], call_arguments)

        def session_generation():
            with manager._lock:
                return manager._manager_generation, manager._server_generations.get(server["id"], 0)

        generation = session_generation()

        def recheck():
            if store.get_plugin_view_internal(plugin_id, view_id) != initial or session_generation() != generation:
                _fail("agent_stale_revision")
            if manager.oauth.public_status(server["instance_id"])["state"] not in ("logged_in", "not_required"):
                _fail("agent_mcp_auth_required")

        def cancelled():
            try:
                recheck()
                return False
            except AgentError:
                return True

        recheck()
        raw = manager.call_tool(server["id"], view["tool"], call_arguments,
            expected_revision=server["revision"], expected_config_generation=server["config_generation"],
            expected_instance_id=server["instance_id"], expected_schema_hash=tool["schema_sha256"],
            before_dispatch=recheck, cancelled=cancelled)
        sc = raw.get("structuredContent") if isinstance(raw, dict) else None
        if (
            view.get("tool") == "pl_financialDashboard"
            and isinstance(sc, dict)
            and sc.get("data") is None
            and sc.get("truncated") is True
            and isinstance(call_arguments.get("startDate"), str)
            and isinstance(call_arguments.get("endDate"), str)
        ):
            end_dt = date.fromisoformat(call_arguments["endDate"])
            start_dt = date.fromisoformat(call_arguments["startDate"])
            narrow_start = date.fromordinal(max(start_dt.toordinal(), end_dt.toordinal() - 6)).isoformat()
            if narrow_start > call_arguments["startDate"]:
                narrow_args = {**call_arguments, "startDate": narrow_start}
                raw_narrow = manager.call_tool(server["id"], view["tool"], narrow_args,
                    expected_revision=server["revision"], expected_config_generation=server["config_generation"],
                    expected_instance_id=server["instance_id"], expected_schema_hash=tool["schema_sha256"],
                    before_dispatch=recheck, cancelled=cancelled)
                sc_narrow = raw_narrow.get("structuredContent") if isinstance(raw_narrow, dict) else None
                if isinstance(sc_narrow, dict) and isinstance(sc_narrow.get("data"), dict):
                    sc_narrow["truncated"] = True
                    raw = raw_narrow
        result = _normalize_legacy_view_data(view, _result(raw), payload["page"])
        recheck()
        return {"view_id": view_id, "result": result}
    except Exception as exc:
        code = str(exc) if isinstance(exc, AgentError) and str(exc) in _SAFE_ERRORS else "agent_plugin_view_query_failed"
        raise AgentError(code) from None
