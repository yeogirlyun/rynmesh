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
from .automation_worker import AUTOMATION_WORKER_NAME, AutomationSchedulerWorker
from .computer_status import get_computer_status_response
from .diagnostic_log import failure
from .memory_worker import MEMORY_WORKER_NAME, MemoryExtractionWorker
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
            extensions = getattr(tools, "extensions", None)
            if extensions is not None:
                await asyncio.to_thread(extensions.close)
            native_computer = getattr(tools, "native_computer", None) or getattr(
                getattr(tools, "local", None), "native_computer", None
            )
            if native_computer is not None:
                await asyncio.to_thread(native_computer.close)

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
    memory_worker = MemoryExtractionWorker(runtime)
    app.state.agent_memory_worker = memory_worker
    automation_worker = AutomationSchedulerWorker(runtime)
    app.state.agent_automation_worker = automation_worker
    workers.register(BackgroundWorkerSpec(name="ryn-agent", run_once=(lambda: WorkerRunResult()) if startup_error else runtime.run_continuously, policy=BackoffPolicy.fixed(0.5)), replace=True)
    for slot in (2, 3):
        workers.register(BackgroundWorkerSpec(name=f"ryn-agent-{slot}", run_once=(lambda: WorkerRunResult()) if startup_error else runtime.run_continuously, policy=BackoffPolicy.fixed(0.5)), replace=True)
    workers.register(BackgroundWorkerSpec(name=MEMORY_WORKER_NAME, run_once=(lambda: WorkerRunResult()) if startup_error else memory_worker.run_once, policy=BackoffPolicy.fixed(1.0)), replace=True)
    workers.register(BackgroundWorkerSpec(name=AUTOMATION_WORKER_NAME, run_once=(lambda: WorkerRunResult()) if startup_error else automation_worker.run_once, policy=BackoffPolicy.fixed(2.0)), replace=True)

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
            if code == "agent_task_deleted":
                status = 410
            elif code in {"agent_task_not_found", "agent_memory_not_found", "agent_memory_candidate_not_found", "agent_preference_not_found", "agent_automation_not_found"}:
                status = 404
            elif code in {"agent_memory_revision_conflict", "agent_memory_generation_conflict", "agent_memory_idempotency_conflict", "agent_memory_candidate_not_pending", "agent_memory_candidate_stale"} or code.startswith("agent_memory_candidate_source_stale"):
                status = 409
            elif code.startswith("agent_invalid_") or code.startswith("agent_memory_"):
                status = 400
            else:
                status = 409
            if (
                status == 409
                and not code.startswith("agent_memory_")
                and not method.endswith(("_memory", "_memory_settings", "_memory_entry", "_memory_candidate", "_automation", "_automations", "_automation_now"))
                and args
                and isinstance(args[0], str)
            ):
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

    @app.get("/api/local/agent/computer/status")
    async def computer_status(request: Request):
        local_control(request)
        return await get_computer_status_response(app, runtime, request)

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

    @app.get("/api/local/agent/memory")
    async def memory_snapshot(request: Request, scope_kind: str | None = None, project_id: str | None = None, status: str = "active"):
        local_control(request)
        return await call("memory_snapshot", scope_kind, project_id, status)

    @app.get("/api/local/agent/memory/destinations")
    async def memory_destinations(request: Request):
        local_control(request)
        return await call("memory_destinations")

    @app.put("/api/local/agent/memory/settings")
    async def update_memory_settings(request: Request):
        return await call("update_memory_settings", await body(request))

    @app.post("/api/local/agent/memory/entries")
    async def create_memory_entry(request: Request):
        return await call("create_memory_entry", await body(request))

    @app.put("/api/local/agent/memory/entries/{entry_id}")
    async def update_memory_entry(entry_id: str, request: Request):
        return await call("update_memory_entry", entry_id, await body(request))

    @app.delete("/api/local/agent/memory/entries/{entry_id}")
    async def delete_memory_entry(entry_id: str, request: Request):
        return await call("delete_memory_entry", entry_id, await body(request))

    @app.post("/api/local/agent/memory/candidates/{candidate_id}/decision")
    async def decide_memory_candidate(candidate_id: str, request: Request):
        return await call("decide_memory_candidate", candidate_id, await body(request))

    @app.get("/api/local/agent/memory/search")
    async def search_memory(request: Request, q: str = "", scope_kind: str | None = None, project_id: str | None = None, category: str | None = None, limit: int = 20):
        local_control(request)
        return await call("search_memory", q, scope_kind, project_id, category, limit)

    @app.post("/api/local/agent/memory/reset")
    async def reset_memory(request: Request):
        return await call("reset_memory", await body(request))

    @app.get("/api/local/agent/automations")
    async def list_automations(request: Request):
        local_control(request)
        return await call("list_automations")

    @app.post("/api/local/agent/automations")
    async def create_automation(request: Request):
        return await call("create_automation", await body(request))

    @app.patch("/api/local/agent/automations/{automation_id}")
    async def update_automation(automation_id: str, request: Request):
        return await call("update_automation", automation_id, await body(request))

    @app.delete("/api/local/agent/automations/{automation_id}")
    async def delete_automation(automation_id: str, request: Request):
        local_control(request)
        return await call("delete_automation", automation_id)

    @app.post("/api/local/agent/automations/{automation_id}/run-now")
    async def run_automation_now(automation_id: str, request: Request):
        local_control(request)
        return await call("run_automation_now", automation_id)

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

    extensions = getattr(tools, "extensions", None)
    if extensions is not None:
        from .extensions import install_extension_routes
        install_extension_routes(app, runtime=runtime, extensions=extensions, local_control=local_control)

    return runtime
