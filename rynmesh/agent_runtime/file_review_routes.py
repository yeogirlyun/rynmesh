"""Owner-only task file review routes. No model tool can invoke undo."""
import asyncio

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .store import AgentError


def install_file_review_routes(app, runtime, local_control, body):
    def execute(task_id, action, value=None):
        local = getattr(runtime.tools, "local", None)
        if local is None:
            raise AgentError("agent_review_unavailable")
        with local.lock:
            data = runtime.store.read()
            row = data.get("tasks", {}).get(task_id)
            if not row or row.get("deleted"):
                raise AgentError("agent_task_not_found")
            context = {"task_id": task_id, "local_execution": row.get("local_execution", {})}
            root = local.policy(context, data)["workspace_root"]
            review = local.file_review(task_id, root)
            if action == "review":
                result = local.change_review(task_id, root)
                conflicts = review.preview()["conflicts"] if review.manifest.exists() else []
                by_path = {c["path"].replace("\\", "/"): c["reason"] for c in conflicts}
                for item in result["files"]:
                    item["conflict"] = by_path.get(item["path"])
                observed = []
                for receipt in [*row.get("receipts", []), *(row.get("workspace_file_receipts") or {}).values()]:
                    for item in (receipt.get("result") or {}).get("workspace_files", []):
                        observed.append({"path": item.get("path"), "status": item.get("status", item.get("action", "observed")),
                                         "source": "command_observation", "can_undo": False})
                result["observed"] = observed[-100:]
                result["task_busy"] = row.get("status") in {"running", "queued", "pausing", "reconciling"} or bool(row.get("in_flight"))
                return result
            if data["settings"].get("permission_mode") == "read_only":
                raise AgentError("agent_review_read_only")
            if row.get("status") in {"running", "queued", "pausing", "reconciling"} or row.get("in_flight") or local.processes.running_for_operation(task_id):
                raise AgentError("agent_review_pause_task_first")
            value = value or {}
            if set(value) - ({"ids"} if action == "preview" else {"ids", "token", "request_id", "confirm"}):
                raise AgentError("agent_invalid_review_request")
            if action == "preview":
                return review.preview(value.get("ids"))
            if value.get("confirm") is not True:
                raise AgentError("agent_review_confirmation_required")
            return review.undo(value.get("ids"), value.get("token"), value.get("request_id"))

    async def call(task_id, action, value=None):
        try:
            result = await asyncio.to_thread(execute, task_id, action, value)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except AgentError as exc:
            raise HTTPException(404 if str(exc) == "agent_task_not_found" else 409, detail=str(exc)) from None
        except OSError:
            raise HTTPException(503, detail="agent_review_storage_unavailable") from None

    @app.get("/api/local/agent/tasks/{task_id}/changes")
    async def changes(task_id: str, request: Request):
        local_control(request)
        return await call(task_id, "review")

    @app.post("/api/local/agent/tasks/{task_id}/changes/preview")
    async def preview(task_id: str, request: Request):
        return await call(task_id, "preview", await body(request))

    @app.post("/api/local/agent/tasks/{task_id}/changes/undo")
    async def undo(task_id: str, request: Request):
        return await call(task_id, "undo", await body(request))
