"""Local-owner HTTP routes for Ryn Agent Extensions (Plugins, Skills, MCP servers)."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ..diagnostic_log import failure
from ..store import AgentError
from .adapter import ExtensionToolAdapter
from .views import query_view


def _error_status(code: str) -> int:
    if code.endswith("_not_found"):
        return 404
    if (
        code.endswith("_conflict")
        or code in ("agent_mcp_server_already_exists", "agent_stale_revision")
    ):
        return 409
    if code.endswith(("_too_large", "_limit")):
        return 413
    return 400


def _parse_expected_revision(value: Any) -> int:
    if type(value) is int and value >= 1:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        if parsed >= 1:
            return parsed
    raise AgentError("agent_invalid_revision")


def install_extension_routes(
    app: Any,
    *,
    runtime: Any,
    extensions: ExtensionToolAdapter,
    local_control: Callable,
) -> None:
    """Mount `/api/local/agent/extensions` management routes on the FastAPI app."""

    def _desktop_access() -> bool:
        try:
            return bool(
                runtime.store.inspect(
                    lambda data: bool((data.get("settings") or {}).get("desktop_access", False))
                )
            )
        except Exception:
            return False

    async def _run(name: str, fn: Callable[[], Any]) -> Any:
        started = time.monotonic()
        try:
            return await asyncio.to_thread(fn)
        except AgentError as exc:
            failure("api_ext_" + name, exc, elapsed_seconds=time.monotonic() - started)
            code = str(exc)
            raise HTTPException(_error_status(code), detail=code) from None
        except HTTPException:
            raise
        except Exception as exc:
            failure("api_ext_" + name, exc, elapsed_seconds=time.monotonic() - started)
            raise HTTPException(503, detail="agent_unavailable") from None

    async def _body(request: Request, max_bytes: int = 256 * 1024) -> dict[str, Any]:
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

    async def _delete_revision(request: Request) -> int:
        local_control(request)
        query_rev = request.query_params.get("expected_revision")
        if query_rev is not None:
            try:
                return _parse_expected_revision(query_rev)
            except AgentError as exc:
                raise HTTPException(400, detail=str(exc)) from None
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16 * 1024:
                raise HTTPException(413, detail="agent_request_limit")
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and "expected_revision" in data:
                    return _parse_expected_revision(data["expected_revision"])
            except AgentError as exc:
                raise HTTPException(400, detail=str(exc)) from None
            except Exception:
                pass
        raise HTTPException(400, detail="agent_invalid_revision")

    @app.get("/api/local/agent/extensions")
    async def get_extensions_overview(request: Request):
        local_control(request)
        return await _run(
            "overview",
            lambda: extensions.overview(desktop_access=_desktop_access()),
        )

    @app.post("/api/local/agent/extensions/mcp/servers")
    async def create_mcp_server(request: Request):
        payload = await _body(request)

        def _op():
            expected_rev = _parse_expected_revision(payload.get("expected_revision"))
            return extensions.store.upsert_mcp_server(
                payload,
                expected_revision=expected_rev,
                desktop_access=_desktop_access(),
            )

        return await _run("create_mcp_server", _op)

    @app.get("/api/local/agent/extensions/model-availability")
    async def extension_model_availability(request: Request):
        local_control(request)
        model_id = request.query_params.get("model_id", "")
        if not model_id or len(model_id) > 2048:
            raise HTTPException(400, detail="agent_invalid_request")
        return await _run("model_availability", lambda: runtime.extension_model_availability(model_id))

    @app.post("/api/local/agent/extensions/mcp/servers/{server_id}/apply-server-hints")
    async def apply_server_hints(server_id: str, request: Request):
        payload = await _body(request, 128 * 1024)
        def operation():
            result = extensions.store.apply_server_hints(server_id, payload, desktop_access=_desktop_access())
            extensions.manager.invalidate_server(server_id)
            return result
        return await _run("apply_server_hints", operation)

    @app.patch("/api/local/agent/extensions/mcp/servers/{server_id}")
    async def update_mcp_server(server_id: str, request: Request):
        payload = await _body(request)

        def _op():
            expected_rev = _parse_expected_revision(payload.get("expected_revision"))
            result = extensions.store.upsert_mcp_server(
                payload,
                server_id=server_id,
                expected_revision=expected_rev,
                desktop_access=_desktop_access(),
            )
            extensions.manager.invalidate_server(server_id)
            return result

        return await _run("update_mcp_server", _op)

    @app.delete("/api/local/agent/extensions/mcp/servers/{server_id}")
    async def delete_mcp_server(server_id: str, request: Request):
        expected_rev = await _delete_revision(request)

        def _op():
            previous = extensions.store.get_mcp_server_internal(server_id)
            result = extensions.store.delete_mcp_server(
                server_id,
                expected_revision=expected_rev,
            )
            extensions.manager.invalidate_server(server_id)
            oauth = getattr(extensions.manager, "oauth", None)
            if oauth is not None and previous:
                oauth.forget(previous["instance_id"])
            return result

        return await _run("delete_mcp_server", _op)

    @app.post("/api/local/agent/extensions/mcp/servers/{server_id}/oauth/login")
    async def start_mcp_login(server_id: str, request: Request):
        payload = await _body(request, 16384)
        return await _run("oauth_login", lambda: extensions.manager.oauth.start_login(
            server_id, _parse_expected_revision(payload.get("expected_revision")),
            language="zh" if str(payload.get("language", "zh")).startswith("zh") else "en"))

    @app.get("/api/local/agent/extensions/mcp/servers/{server_id}/oauth/login/{auth_id}")
    async def get_mcp_login(server_id: str, auth_id: str, request: Request):
        local_control(request)
        return await _run("oauth_status", lambda: extensions.manager.oauth.login_status(server_id, auth_id))

    @app.post("/api/local/agent/extensions/mcp/servers/{server_id}/oauth/login/{auth_id}/cancel")
    async def cancel_mcp_login(server_id: str, auth_id: str, request: Request):
        local_control(request)
        return await _run("oauth_cancel", lambda: extensions.manager.oauth.cancel_login(server_id, auth_id))

    @app.post("/api/local/agent/extensions/mcp/servers/{server_id}/oauth/logout")
    async def logout_mcp(server_id: str, request: Request):
        payload = await _body(request, 16384)
        def operation():
            result = extensions.manager.oauth.logout(server_id, _parse_expected_revision(payload.get("expected_revision")))
            extensions.manager.invalidate_server(server_id)
            return result
        return await _run("oauth_logout", operation)

    @app.post("/api/local/agent/extensions/mcp/oauth/clear-local-authorizations")
    async def clear_mcp_authorizations(request: Request):
        payload = await _body(request, 16384)
        if payload.get("confirm") is not True:
            raise HTTPException(400, "agent_confirmation_required")
        def operation():
            extensions.manager.oauth.clear_all()
            extensions.manager.close()
            return {"cleared": True}
        return await _run("oauth_clear", operation)

    @app.post("/api/local/agent/extensions/mcp/servers/{server_id}/test")
    async def test_mcp_server(server_id: str, request: Request):
        payload = await _body(request)

        def _op():
            expected_rev = _parse_expected_revision(payload.get("expected_revision"))
            return extensions.manager.test_server(
                server_id,
                expected_revision=expected_rev,
                restart=False,
                desktop_access=_desktop_access(),
            )

        return await _run("test_mcp_server", _op)

    @app.post("/api/local/agent/extensions/mcp/servers/{server_id}/restart")
    async def restart_mcp_server(server_id: str, request: Request):
        payload = await _body(request)

        def _op():
            expected_rev = _parse_expected_revision(payload.get("expected_revision"))
            return extensions.manager.test_server(
                server_id,
                expected_revision=expected_rev,
                restart=True,
                desktop_access=_desktop_access(),
            )

        return await _run("restart_mcp_server", _op)

    @app.post("/api/local/agent/extensions/mcp/restart")
    async def restart_all_mcp_servers():
        return await _run(
            "restart_all_mcp_servers",
            lambda: extensions.manager.restart_all(desktop_access=_desktop_access()),
        )

    @app.post("/api/local/agent/extensions/mcp/apps/clear-data")
    async def clear_mcp_app_data():
        return await _run(
            "clear_mcp_app_data",
            lambda: extensions.manager.clear_app_data(desktop_access=_desktop_access()),
        )

    @app.post("/api/local/agent/extensions/plugins/{plugin_id}/views/{view_id}/query")
    async def query_plugin_view(plugin_id: str, view_id: str, request: Request):
        payload = await _body(request, 16 * 1024)
        result = await _run("query_plugin_view", lambda: query_view(
            extensions.store, extensions.manager, plugin_id, view_id, payload))
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.patch("/api/local/agent/extensions/plugins/{plugin_id}")
    async def update_plugin(plugin_id: str, request: Request):
        payload = await _body(request)

        def _op():
            if type(payload.get("enabled")) is not bool:
                raise AgentError("agent_invalid_request")
            expected_rev = _parse_expected_revision(payload.get("expected_revision"))
            result = extensions.store.set_plugin_enabled(
                plugin_id,
                payload["enabled"],
                expected_revision=expected_rev,
                desktop_access=_desktop_access(),
            )
            for mid in result.get("mcp_server_ids") or []:
                extensions.manager.invalidate_server(mid)
            return result

        return await _run("update_plugin", _op)

    @app.delete("/api/local/agent/extensions/plugins/{plugin_id}")
    async def delete_plugin(plugin_id: str, request: Request):
        expected_rev = await _delete_revision(request)

        def _op():
            previous = [server for server in extensions.store.list_mcp_servers_internal()
                        if server.get("plugin_id") == plugin_id]
            result = extensions.store.delete_plugin(
                plugin_id,
                expected_revision=expected_rev,
            )
            for mid in result.get("removed_mcp_server_ids") or []:
                extensions.manager.invalidate_server(mid)
            oauth = getattr(extensions.manager, "oauth", None)
            if oauth is not None:
                for server in previous:
                    oauth.forget(server["instance_id"])
            return result

        return await _run("delete_plugin", _op)

    @app.get("/api/local/agent/extensions/skills/recommended")
    async def list_recommended_skills(request: Request):
        local_control(request)
        raw_refresh = str(request.query_params.get("refresh") or "").strip().lower()
        refresh = raw_refresh in ("1", "true", "yes")
        return await _run(
            "list_recommended_skills",
            lambda: extensions.store.list_recommended_skills(refresh=refresh),
        )

    @app.post("/api/local/agent/extensions/skills/recommended/install")
    async def install_recommended_skill(request: Request):
        payload = await _body(request)
        return await _run(
            "install_recommended_skill",
            lambda: extensions.store.install_recommended_skill(
                payload,
                desktop_access=_desktop_access(),
            ),
        )

    @app.post("/api/local/agent/extensions/skills/refresh")
    async def refresh_discovered_skills(request: Request):
        local_control(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16 * 1024:
                raise HTTPException(413, detail="agent_request_limit")
        payload: dict[str, Any] = {}
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                pass
        return await _run(
            "refresh_discovered_skills",
            lambda: extensions.store.sync_discovered_skills(
                desktop_access=_desktop_access(),
                reset_uninstalled=bool(payload.get("reset_uninstalled", False)),
            ),
        )

    @app.get("/api/local/agent/extensions/skills/{skill_id}/files")
    async def read_skill_package_file(skill_id: str, request: Request):
        local_control(request)
        rel_path = str(request.query_params.get("path") or "SKILL.md").strip() or "SKILL.md"
        return await _run(
            "read_skill_package_file",
            lambda: extensions.store.read_skill_file(
                skill_id,
                rel_path,
                allow_disabled=True,
            ),
        )

    @app.post("/api/local/agent/extensions/skills/{skill_id}/open")
    async def open_skill_path(skill_id: str, request: Request):
        payload = await _body(request)
        action = str(payload.get("action") or "open").strip().lower()
        return await _run(
            "open_skill_path",
            lambda: extensions.store.open_skill_path(skill_id, action=action),
        )

    @app.patch("/api/local/agent/extensions/skills/{skill_id}")
    async def update_skill(skill_id: str, request: Request):
        payload = await _body(request)

        def _op():
            if type(payload.get("enabled")) is not bool:
                raise AgentError("agent_invalid_request")
            expected_rev = _parse_expected_revision(payload.get("expected_revision"))
            return extensions.store.set_skill_enabled(
                skill_id,
                payload["enabled"],
                expected_revision=expected_rev,
            )

        return await _run("update_skill", _op)

    @app.delete("/api/local/agent/extensions/skills/{skill_id}")
    async def delete_skill(skill_id: str, request: Request):
        expected_rev = await _delete_revision(request)
        return await _run(
            "delete_skill",
            lambda: extensions.store.delete_skill(
                skill_id,
                expected_revision=expected_rev,
            ),
        )

    @app.post("/api/local/agent/extensions/import/preview")
    async def preview_extension_import(request: Request):
        payload = await _body(request, max_bytes=10 * 1024 * 1024)
        return await _run(
            "import_preview",
            lambda: extensions.imports.preview(payload),
        )

    @app.post("/api/local/agent/extensions/import/commit")
    async def commit_extension_import(request: Request):
        payload = await _body(request)

        def _op():
            preview_id = payload.get("preview_id")
            if not isinstance(preview_id, str) or not preview_id.strip():
                raise AgentError("agent_import_preview_not_found")
            expected_rev = _parse_expected_revision(payload.get("expected_revision"))
            overview = extensions.imports.commit(
                preview_id.strip(),
                expected_revision=expected_rev,
                desktop_access=_desktop_access(),
            )
            for srv in overview.get("mcp_servers") or []:
                if isinstance(srv, dict) and isinstance(srv.get("id"), str):
                    extensions.manager.invalidate_server(srv["id"])
            return overview

        return await _run("import_commit", _op)
