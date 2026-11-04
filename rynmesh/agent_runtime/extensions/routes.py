"""Local-owner HTTP routes for Ryn Agent Extensions (Plugins, Skills, MCP servers)."""
from __future__ import annotations

import asyncio
import hmac
import json
import time
from typing import Any, Callable

from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ..diagnostic_log import failure
from ..store import AgentError
from .adapter import ExtensionToolAdapter
from .local_services import LocalServiceCredentials
from .views import query_plugin_tool, query_view


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
    local_services = LocalServiceCredentials(extensions.store, extensions.manager)

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

    @app.post("/api/local/agent/extensions/local-service/prepare")
    async def prepare_local_service(request: Request):
        payload = await _body(request, max_bytes=8192)
        if set(payload) != {"service_id", "plugin_ids", "server_ids", "port"}:
            raise HTTPException(400, detail="agent_local_service_invalid")
        # Only the authenticated native owner channel receives this secret.
        return await _run("prepare_local_service", lambda: local_services.prepare(**payload))

    @app.post("/api/local/agent/extensions/local-service/activate")
    async def activate_local_service(request: Request, background_tasks: BackgroundTasks):
        payload = await _body(request, max_bytes=1024)
        if set(payload) != {"service_id", "generation"}:
            raise HTTPException(400, detail="agent_local_service_invalid")
        result = await _run("activate_local_service", lambda: local_services.activate(**payload))
        async def rediscover():
            for sid in result["server_ids"]:
                try:
                    server = extensions.store.get_mcp_server_internal(sid, require_enabled=True)
                    await asyncio.to_thread(extensions.manager.test_server, sid, expected_revision=server["revision"])
                except AgentError:
                    # Auth-required/disabled states remain visible; never enable or borrow credentials.
                    pass
        background_tasks.add_task(rediscover)
        return result

    async def service_payload(request):
        if (not request.client or request.client.host not in {"127.0.0.1", "::1"}
                or request.headers.get("origin") or request.headers.get("referer")):
            raise HTTPException(403, detail="agent_local_service_denied")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8192:
                raise HTTPException(413, detail="agent_request_limit")
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError()
            return payload
        except (ValueError, TypeError):
            raise HTTPException(400, detail="agent_invalid_request") from None

    def operation_allowed(request, payload):
        try:
            server = extensions.store.get_mcp_server_internal(payload["server_id"], require_enabled=True)
            headers = server.get("secret_headers") or {}
            for header in ("X-Ryn-Service-Token", "X-Ryn-Service-Generation"):
                expected = headers.get(header, "")
                if not expected or not hmac.compare_digest(expected.encode(), request.headers.get(header, "").encode()):
                    raise ValueError
            receipt = extensions.ledger_factory().get(payload["operation_id"])
            binding = receipt["binding"]
            if (receipt.get("trusted_operation") != payload
                    or receipt["status"] not in {"dispatched", "confirmed"}
                    or binding["task_id"] != payload["task_id"]
                    or binding["arguments_sha256"] != payload["arguments_sha256"]
                    or binding["extension_binding"]["server_id"] != server["id"]
                    or payload["expires_at"] < time.time()):
                raise ValueError
            extensions.verify_binding(binding["extension_binding"])
            def current(data):
                row = data["tasks"].get(binding["task_id"])
                if (not row or not data["settings"].get("enabled")
                        or data["settings"].get("permission_mode") == "read_only"
                        or row["status"] in {"cancelled", "paused", "interrupted", "failed"}
                        or row.get("pending_control")):
                    return False
                actual = {"goal_revision": row["goal"]["revision"], "control_revision": row["control_revision"],
                          "scope_revision": row["scope_revision"], "settings_revision": data.get("settings_revision", 0)}
                return actual == binding.get("authority_revision") == payload.get("authority_revision")
            return runtime.store.inspect(current)
        except (ValueError, KeyError, TypeError, AgentError):
            return False

    @app.post("/api/local/agent/extensions/local-service/authorize-operation")
    async def authorize_local_operation(request: Request):
        return {"allowed": operation_allowed(request, await service_payload(request))}

    @app.post("/api/local/agent/extensions/local-service/artifact")
    async def local_operation_artifact(request: Request):
        payload = await service_payload(request)
        if not operation_allowed(request, payload) or not isinstance(payload.get("artifact"), dict):
            raise HTTPException(403, detail="agent_local_service_denied")
        from ..artifact_transfers import ArtifactTransfers
        receipt = extensions.ledger_factory().get(payload["operation_id"])
        binding = receipt["binding"]
        context = {"task_id": binding["task_id"], "owner_id": binding["owner_id"],
                   "scope_revision": binding["authority_revision"]["scope_revision"]}
        item = ArtifactTransfers(runtime.store).resolve(context, payload["artifact"]["artifact_ref"], content=True)
        if {k: v for k, v in item.items() if k != "bytes"} != payload["artifact"]:
            raise HTTPException(409, detail="agent_artifact_content_changed")
        if not operation_allowed(request, payload):
            raise HTTPException(403, detail="agent_local_service_denied")
        return Response(item["bytes"], media_type="application/octet-stream", headers={"Cache-Control": "no-store"})

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

    async def read_company_account():
        import httpx
        from urllib.parse import urlsplit
        server = await _run("company_workspace", lambda: extensions.store.get_mcp_server_internal(
            "company-workspace-mcp", require_enabled=False))
        url = urlsplit(server.get("url") or "")
        if (server.get("plugin_id") != "company-workspace" or url.scheme != "http"
                or url.hostname != "127.0.0.1" or not url.port or url.path != "/ryn/agent-mcp"):
            raise HTTPException(503, detail="company_account_unavailable")
        try:
            async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
                response = await client.get(f"http://127.0.0.1:{url.port}/api/company/account")
                response.raise_for_status()
                account = response.json()
                return {key: account[key] for key in ("memberId", "displayName", "state", "version", "plugins", "connections")}
        except (httpx.HTTPError, ValueError, KeyError):
            raise HTTPException(503, detail="company_account_unavailable") from None

    app.state.read_company_account = read_company_account

    @app.get("/api/local/agent/extensions/company/account")
    async def company_account(request: Request):
        local_control(request)
        return await read_company_account()

    @app.get("/api/local/agent/extensions/company/workspace-member")
    async def company_workspace_member(request: Request):
        local_control(request)
        return await _run("company_workspace_member", extensions.manager.oauth.workspace_member)

    @app.get("/api/local/agent/extensions/company/connections")
    async def company_connections(request: Request):
        local_control(request)
        return await _run("company_connections", extensions.manager.oauth.company_connections)

    @app.get("/api/local/agent/extensions/company/account-action")
    async def company_account_action(request: Request):
        local_control(request)
        return await _run("company_account_action", extensions.manager.oauth.company_action)

    @app.post("/api/local/agent/extensions/company/logout")
    async def logout_company(request: Request):
        local_control(request)
        def operation():
            from .mcp_oauth import COMPANY_PLUGIN_IDS
            providers = getattr(app.state, "peer_providers", None)
            if providers:
                providers.clear_local_company_sessions(COMPANY_PLUGIN_IDS)
            result = extensions.manager.oauth.logout_company()
            for server_id in result["servers"]:
                extensions.manager.invalidate_server(server_id)
            return result
        return await _run("company_logout", operation)

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

    @app.post("/api/local/agent/extensions/plugins/{plugin_id}/tool-query")
    async def run_plugin_tool_query(plugin_id: str, request: Request):
        payload = await _body(request, 32 * 1024)
        result = await _run("query_plugin_tool", lambda: query_plugin_tool(
            extensions.store, extensions.manager, plugin_id, payload))
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

    @app.patch("/api/local/agent/extensions/plugins/{plugin_id}/web-capabilities")
    async def configure_plugin_web_capabilities(plugin_id: str, request: Request):
        payload = await _body(request, 4096)

        def _op():
            if set(payload) != {"downloads", "dialogs", "expected_instance_id", "expected_revision"}:
                raise AgentError("agent_invalid_request")
            return extensions.store.configure_plugin_web_capabilities(
                plugin_id,
                downloads=payload["downloads"], dialogs=payload["dialogs"],
                expected_instance_id=payload["expected_instance_id"],
                expected_revision=_parse_expected_revision(payload["expected_revision"]),
                desktop_access=_desktop_access(),
            )

        # Browser permissions do not replace MCP instances or invalidate OAuth grants.
        return await _run("configure_plugin_web_capabilities", _op)

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
