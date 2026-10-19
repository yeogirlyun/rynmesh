"""Local-owner Agent API and supervised worker installation."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ..background_workers import BackgroundWorkerSpec, BackoffPolicy, WorkerRunResult
from .diagnostic_log import failure
from .runtime import AgentRuntime
from .store import AgentError, AgentStore


def install_agent(app: Any, *, store: Any, home: str | Path, workers: Any,
                  local_control: Callable, messaging_key: Any,
                  integrations: tuple | None = None) -> AgentRuntime:
    if integrations is None:
        from .bridge import build_agent_integrations
        integrations = build_agent_integrations(app, store, home)
    model, tools, catalog = integrations
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(instance):
        try:
            async with previous_lifespan(instance) as state:
                yield state
        finally:
            craft = getattr(tools, "craft", None)
            if craft is not None:
                await asyncio.to_thread(craft.close)

    app.router.lifespan_context = lifespan
    runtime = AgentRuntime(AgentStore(Path(home) / "agent", messaging_key), model, tools, catalog, consumer_node_id=getattr(store, "peer_id", None))
    startup_error = None
    try:
        runtime.reconcile()
    except (AgentError, OSError) as exc:
        # The optional Agent's damaged encrypted history must not stop model
        # sharing, friends, or the rest of the node from starting.
        startup_error = "agent_state_unreadable"
        failure("startup_reconcile", exc)
    app.state.agent_runtime = runtime
    app.state.agent_startup_error = startup_error
    workers.register(BackgroundWorkerSpec(name="ryn-agent", run_once=(lambda: WorkerRunResult()) if startup_error else runtime.run_continuously, policy=BackoffPolicy.fixed(0.5)), replace=True)
    for slot in (2, 3):
        workers.register(BackgroundWorkerSpec(name=f"ryn-agent-{slot}", run_once=(lambda: WorkerRunResult()) if startup_error else runtime.run_continuously, policy=BackoffPolicy.fixed(0.5)), replace=True)

    @app.middleware("http")
    async def prevent_agent_caching(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/local/agent/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    async def call(method: str, *args):
        started = time.monotonic()
        try:
            return await asyncio.to_thread(getattr(runtime, method), *args)
        except AgentError as exc:
            failure("api_" + method, exc, elapsed_seconds=time.monotonic() - started)
            code = str(exc)
            status = 410 if code == "agent_task_deleted" else 404 if code == "agent_task_not_found" else 400 if code.startswith("agent_invalid_") else 409
            if status == 409 and args and isinstance(args[0], str):
                try:
                    current = await asyncio.to_thread(runtime.get, args[0])
                except Exception as current_error:
                    failure("api_conflict_snapshot", current_error)
                    pass
                else:
                    return JSONResponse({"detail": code, "current": current}, status_code=409)
            raise HTTPException(status, detail=code) from None
        except Exception as exc:
            failure("api_" + method, exc, elapsed_seconds=time.monotonic() - started)
            raise HTTPException(503, detail="agent_unavailable") from None

    async def body(request: Request, max_bytes: int = 64 * 1024) -> dict:
        local_control(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > max_bytes:
                raise HTTPException(413, detail="agent_request_limit")
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (TypeError, ValueError):
            raise HTTPException(400, detail="agent_invalid_request") from None

    @app.get("/api/local/agent/settings")
    async def settings(request: Request):
        local_control(request)
        return await call("settings")

    @app.put("/api/local/agent/settings")
    async def update_settings(request: Request):
        return await call("update_settings", await body(request))

    @app.post("/api/local/agent/tasks/{task_id}/workspace")
    async def bind_workspace(task_id: str, request: Request):
        return await call("bind_workspace", task_id, await body(request))

    @app.get("/api/local/agent/models")
    async def models(request: Request):
        local_control(request)
        return {"models": await call("models")}

    @app.get("/api/local/agent/projects")
    async def list_projects(request: Request):
        local_control(request)
        return {"projects": await call("list_projects"), "selected_project_id": await call("project_selection")}

    @app.post("/api/local/agent/projects")
    async def load_project(request: Request):
        return await call("load_project", await body(request))

    @app.post("/api/local/agent/projects/selection")
    async def select_project(request: Request):
        return await call("select_project", await body(request))

    @app.delete("/api/local/agent/projects/{project_id}")
    async def remove_project(project_id: str, request: Request):
        local_control(request)
        return await call("remove_project", project_id)

    @app.patch("/api/local/agent/projects/{project_id}")
    async def rename_project(project_id: str, request: Request):
        return await call("rename_project", project_id, await body(request))

    @app.get("/api/local/agent/skills")
    async def skills(request: Request):
        local_control(request)
        return await call("skills")

    @app.get("/api/local/agent/preferences")
    async def preferences(request: Request):
        local_control(request)
        return await call("preferences")

    @app.put("/api/local/agent/preferences")
    async def update_preferences(request: Request):
        return await call("update_preferences", await body(request))

    @app.put("/api/local/agent/tasks/{task_id}/budget")
    async def update_budget(task_id: str, request: Request):
        return await call("update_budget", task_id, await body(request))

    @app.post("/api/local/agent/tasks/{task_id}/source-credential")
    async def source_credential(task_id: str, request: Request):
        return await call("source_credential", task_id, await body(request), False)

    @app.delete("/api/local/agent/tasks/{task_id}/source-credential")
    async def revoke_source_credential(task_id: str, request: Request):
        return await call("source_credential", task_id, await body(request), True)

    @app.get("/api/local/agent/tools")
    async def tool_catalog(request: Request):
        local_control(request)
        return {"tools": await call("tool_catalog")}

    @app.get("/api/local/agent/tasks")
    async def tasks(request: Request, archived: bool = False):
        local_control(request)
        return {"tasks": await call("list_tasks", archived)}

    @app.post("/api/local/agent/tasks/{task_id}/archive")
    async def archive_task(task_id: str, request: Request):
        return await call("lifecycle", task_id, await body(request), "archive")

    @app.post("/api/local/agent/tasks/{task_id}/restore")
    async def restore_task(task_id: str, request: Request):
        return await call("lifecycle", task_id, await body(request), "restore")

    @app.delete("/api/local/agent/tasks/{task_id}")
    async def delete_task(task_id: str, request: Request):
        return await call("lifecycle", task_id, await body(request), "delete")

    @app.patch("/api/local/agent/tasks/{task_id}")
    async def rename_task(task_id: str, request: Request):
        return await call("rename_task", task_id, await body(request))

    @app.get("/api/local/agent/resources")
    async def scope_catalog(request: Request):
        local_control(request)
        return await call("scope_catalog")

    @app.get("/api/local/agent/tasks/{task_id}/scope")
    async def get_scope(task_id: str, request: Request):
        local_control(request)
        return await call("get_scope", task_id)

    @app.get("/api/local/agent/tasks/{task_id}/model")
    async def preview_model_switch(task_id: str, request: Request, model_id: str):
        local_control(request)
        return await call("preview_model_switch", task_id, model_id)

    @app.put("/api/local/agent/tasks/{task_id}/model")
    async def switch_model(task_id: str, request: Request):
        return await call("switch_model", task_id, await body(request))

    @app.put("/api/local/agent/tasks/{task_id}/scope")
    async def update_scope(task_id: str, request: Request):
        return await call("update_scope", task_id, await body(request))

    @app.post("/api/local/agent/tasks")
    async def create_task(request: Request):
        return await call("create", await body(request, 28 * 1024 * 1024))

    @app.get("/api/local/agent/tasks/{task_id}")
    async def get_task(task_id: str, request: Request, since_revision: int | None = None):
        local_control(request)
        return await call("get", task_id, since_revision)

    @app.post("/api/local/agent/tasks/{task_id}/messages")
    async def add_message(task_id: str, request: Request):
        return await call("add_message", task_id, await body(request, 28 * 1024 * 1024))

    @app.post("/api/local/agent/tasks/{task_id}/actions")
    async def action(task_id: str, request: Request):
        return await call("action", task_id, await body(request))

    @app.get("/api/local/agent/tasks/{task_id}/events")
    async def events(task_id: str, request: Request, after: int = 0):
        local_control(request)
        return await call("events", task_id, after)

    @app.get("/api/local/agent/tasks/{task_id}/artifacts")
    async def artifacts(task_id: str, request: Request):
        local_control(request)
        return await call("artifacts", task_id)

    @app.get("/api/local/agent/tasks/{task_id}/artifacts/{operation_id}")
    async def read_artifact(task_id: str, operation_id: str, request: Request):
        local_control(request)
        return await call("read_artifact", task_id, operation_id)

    @app.post("/api/local/agent/tasks/{task_id}/files/{file_id}/actions")
    async def open_file_artifact(task_id: str, file_id: str, request: Request):
        local_control(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="agent_artifact_action_invalid")
        return await call("open_file_artifact", task_id, file_id, body.get("action"))

    @app.get("/api/local/agent/tasks/{task_id}/files/{file_id}")
    async def read_file_artifact(task_id: str, file_id: str, request: Request, v: str | None = None):
        local_control(request)
        from urllib.parse import quote

        from fastapi.responses import Response

        from .workspace_artifacts import CONTENT_SECURITY_POLICY
        item = await call("read_file_artifact", task_id, file_id)
        if isinstance(item, JSONResponse):
            return item  # a refusal carrying the current task, as for every other task route
        if v and v != item["sha256"]:
            return JSONResponse(status_code=409, content={"detail": "agent_artifact_content_changed"})
        # Bytes for a sandboxed preview or download, never an active document of this origin.
        return Response(item["bytes"], media_type=item["media_type"], headers={
            "Content-Security-Policy": CONTENT_SECURITY_POLICY, "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline; filename*=UTF-8''" + quote(item["name"]), "ETag": '"' + item["sha256"] + '"',
            "X-Ryn-Artifact-Sha256": item["sha256"], "X-Ryn-Artifact-Status": item["status"]})

    @app.get("/api/local/agent/tasks/{task_id}/results/{result_id}")
    async def get_result(task_id: str, result_id: str, request: Request, offset: int = 0, limit: int = 8192):
        local_control(request)
        return await call("get_result", task_id, result_id, offset, limit)

    from .file_preview import install_file_preview_routes
    install_file_preview_routes(app, call, local_control)

    return runtime
