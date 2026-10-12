"""Verified Clawpad workflow subset; no synthetic screening or sample records."""

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from jsonschema import ValidationError, validate

from .catalog_data import CATALOG
from .resume import EngineError, ResumeEngine


class AppError(ValueError):
    pass


def contract(app, action, args):
    try:
        item = CATALOG[app]["operations"][action]
        validate(args, item["schema"])
    except (KeyError, TypeError, ValidationError):
        raise AppError("apps_invalid_operation") from None
    return item


def run(app, action, args, state):
    contract(app, action, args)
    if app == "resume":
        engine = ResumeEngine(state or None)
        try:
            if action == "export_resume_text":
                resume = engine._require_resume(args["resume_id"])
                result = {"text": "\n".join(engine._resume_to_md(resume)) + "\n"}
            else:
                result = getattr(engine, "_handle_" + action)(deepcopy(args))
        except EngineError:
            raise AppError("apps_resume_operation_rejected") from None
        return result, engine.state
    state = deepcopy(state)
    records = state.setdefault("records", {})

    def rows(bucket):
        return records.setdefault(bucket, [])

    def find(bucket, identifier):
        for row in rows(bucket):
            if row["id"] == identifier:
                return row
        raise AppError("apps_record_unavailable")

    def create(bucket, values):
        if len(rows(bucket)) >= 64:
            raise AppError("apps_workspace_capacity")
        row = {
            **deepcopy(values),
            "id": uuid4().hex,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        rows(bucket).append(row)
        return {"record": deepcopy(row)}

    if action.startswith(("list_", "get_")):
        if action == "get_pipeline":
            candidates = rows("candidates")
            if args.get("job_id"):
                find("jobs", args["job_id"])
                candidates = [c for c in candidates if c["job_id"] == args["job_id"]]
            return {
                "stages": {
                    s: [deepcopy(c) for c in candidates if c["stage"] == s]
                    for s in ("applied", "screening", "interview", "offer", "hired", "rejected")
                }
            }, state
        noun = action.split("_", 1)[1]
        bucket = noun if action.startswith("list_") else noun + "s"
        if action.startswith("list_"):
            items = rows(bucket)
            if args.get("job_id"):
                find("jobs", args["job_id"])
            for field in ("status", "priority", "job_id", "stage"):
                if args.get(field):
                    items = [r for r in items if r.get(field) == args[field]]
            if args.get("tag"):
                items = [r for r in items if args["tag"] in r.get("tags", [])]
            if args.get("query"):
                items = [r for r in items if args["query"].casefold() in str(r).casefold()]
            if args.get("include_closed") is False:
                items = [r for r in items if r.get("status") != "closed"]
            offset = args.get("offset", 0)
            limit = args.get("limit", 64)
            return {"items": deepcopy(items[offset : offset + limit]), "total": len(items)}, state
        return {"record": deepcopy(find(bucket, args[noun + "_id"]))}, state
    if action == "create_job":
        if not args["name"].strip():
            raise AppError("apps_invalid_operation")
        return create("jobs", {**args, "status": "open"}), state
    if action == "create_candidate":
        find("jobs", args["job_id"])
        if not args["name"].strip():
            raise AppError("apps_invalid_operation")
        return create("candidates", {**args, "stage": "applied"}), state
    if action in {"advance_candidate", "reject_candidate"}:
        row = find("candidates", args["candidate_id"])
        target = "rejected" if action == "reject_candidate" else args["stage"]
        allowed = {
            "applied": {"screening", "rejected"},
            "screening": {"interview", "rejected"},
            "interview": {"offer", "rejected"},
            "offer": {"hired", "rejected"},
        }
        if target not in allowed.get(row["stage"], set()):
            raise AppError("apps_candidate_transition_rejected")
        row.update(stage=target)
        if args.get("notes"):
            row["notes"] = args["notes"]
        if args.get("reason"):
            row["reason"] = args["reason"]
        return {"record": deepcopy(row)}, state
    if action == "create_ticket":
        if not args["name"].strip():
            raise AppError("apps_invalid_operation")
        if args.get("priority", "normal") not in {"low", "normal", "medium", "high", "urgent"}:
            raise AppError("apps_invalid_operation")
        return create("tickets", {**args, "status": "open", "comments": []}), state
    row = find("tickets", args["ticket_id"])
    if action == "add_comment":
        if not args["body"].strip():
            raise AppError("apps_invalid_operation")
        if len(row["comments"]) >= 64:
            raise AppError("apps_workspace_capacity")
        comment = {
            "id": uuid4().hex,
            "body": args["body"],
            "author_name": args.get("author_name", ""),
        }
        row["comments"].append(comment)
        return {"comment": deepcopy(comment)}, state
    if action in {"close_ticket", "reopen_ticket"}:
        target = "closed" if action == "close_ticket" else "open"
        if row["status"] == target:
            raise AppError("apps_ticket_transition_rejected")
        row.update(status=target, resolution_note=args.get("resolution_note", ""))
        return {"record": deepcopy(row)}, state
    raise AppError("apps_invalid_operation")
