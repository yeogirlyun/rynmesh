"""Loopback-only inference API with separate, revocable project credentials."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from rynmesh.node_auth import is_browser_cross_site, is_forwarded, is_loopback_addr
from rynmesh.private_permissions import ensure_private_file_acl, restrict_windows_acl

from . import codex_threads
from .adapters import AdapterError, RUNTIME_ERROR_CODES, actual_usage
from .api_formats import StreamFormat, convert_response, normalize_with_ignored, sse
from .chat import completion
from .context_safety import context_overflow_detail, estimate_context_safety_tokens
from .safety import MAX_OUTPUT, MAX_REQUEST, install_body_limit
from .service_view import is_usable
from .streaming import events


_logger = logging.getLogger(__name__)

DEFAULT_KEY_MAX_CONCURRENT = 2
MAX_KEY_MAX_CONCURRENT = 16
LOCAL_SLOT_WAIT_SECONDS = 10.0
LOCAL_SLOT_POLL_SECONDS = 0.05
LOCAL_SLOT_RETRY_AFTER = "5"
CATALOG_CACHE_TTL_SECONDS = 5.0
HEALTH_CACHE_TTL_SECONDS = 10.0

_ADAPTER_ERROR_STATUS: dict[str, int] = {
    "inference_timeout": 504,
    "runtime_busy": 429,
    "model_daily_quota_exhausted": 429,
    "upstream_image_rejected": 422,
    "upstream_context_overflow": 400,
    "source_protocol_parameters_unsupported": 400,
}

_ADAPTER_ERROR_MESSAGES: dict[str, str] = {
    "inference_timeout": "Model inference timed out.",
    "runtime_busy": "Model runtime is busy; retry shortly.",
    "model_daily_quota_exhausted": "Model daily quota is exhausted.",
    "upstream_image_rejected": "Upstream model rejected the image input.",
    "upstream_context_overflow": "Input exceeds the model context window.",
    "source_protocol_parameters_unsupported": "Requested protocol parameters are not supported by this source.",
    "model_not_ready": "Model runtime is not ready.",
    "model_not_found": "Configured upstream model was not found.",
    "runtime_unavailable": "Model runtime is unavailable.",
    "runtime_connection_failed": "Failed to connect to the model runtime.",
    "model_configuration_changed": "Model configuration changed during inference.",
    "ai_permission_denied": "Model access permission was denied.",
    "stream_delivery_failed": "Model stream delivery failed.",
    "upstream_http_failed": "Upstream model service returned an HTTP error.",
    "upstream_response_invalid": "Upstream model returned an invalid response.",
    "upstream_response_too_large": "Upstream model response exceeded size limit.",
    "upstream_output_limit_reached": "Model output token limit was reached.",
    "upstream_stream_truncated": "Upstream model stream ended prematurely.",
    "source_protocol_response_invalid": "Upstream protocol response was invalid.",
    "upstream_stream_error": "Upstream model reported a stream error.",
    "upstream_request_rejected": "Upstream model rejected the request.",
    "inference_failed": "Model inference failed.",
}


class _GuardedStream:
    """Ensure stream cleanup runs on close/abort even if Starlette skips BackgroundTask."""

    def __init__(self, gen: Any, on_close: Any) -> None:
        self._gen = gen
        self._on_close = on_close

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self._gen.__anext__()
        except BaseException:
            self._on_close()
            raise

    async def aclose(self) -> None:
        try:
            await self._gen.aclose()
        finally:
            self._on_close()

    def __del__(self) -> None:
        self._on_close()


class InferenceKeys:
    def __init__(self, home: Path):
        self.path = home / "llm" / "inference-keys.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.active: dict[str, int] = {}
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS keys ("
                "id TEXT PRIMARY KEY, digest TEXT UNIQUE, name TEXT, created REAL, "
                "revoked INTEGER DEFAULT 0, token_limit INTEGER, used INTEGER DEFAULT 0, "
                "max_concurrent INTEGER DEFAULT 2)"
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(keys)")}
            if "max_concurrent" not in columns:
                db.execute("ALTER TABLE keys ADD COLUMN max_concurrent INTEGER DEFAULT 2")
            db.execute("UPDATE keys SET max_concurrent=2 WHERE max_concurrent IS NULL")
            db.execute("CREATE TABLE IF NOT EXISTS model_aliases (name TEXT PRIMARY KEY, target TEXT NOT NULL)")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        if sys.platform == "win32":
            ensure_private_file_acl(
                self.path,
                track_key_status=False,
                acl_fn=restrict_windows_acl,
                logger=_logger,
            )

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _validate_token_limit(token_limit: Any) -> int:
        if type(token_limit) is not int or not 1 <= token_limit <= 10**9:
            raise ValueError("output_token_limit (1–1000000000) is required")
        return token_limit

    @staticmethod
    def _validate_max_concurrent(max_concurrent: Any) -> int:
        if type(max_concurrent) is not int or not 1 <= max_concurrent <= MAX_KEY_MAX_CONCURRENT:
            raise ValueError(f"max_concurrent (1–{MAX_KEY_MAX_CONCURRENT}) must be an integer")
        return max_concurrent

    def create(self, name: str, token_limit: int, max_concurrent: int = DEFAULT_KEY_MAX_CONCURRENT) -> dict[str, Any]:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError("name (1–80 characters) and output_token_limit (1–1000000000) are required")
        self._validate_token_limit(token_limit)
        self._validate_max_concurrent(max_concurrent)
        key = "ryn_" + secrets.token_urlsafe(32)
        key_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO keys(id,digest,name,created,token_limit,max_concurrent) VALUES(?,?,?,?,?,?)",
                (key_id, hashlib.sha256(key.encode()).hexdigest(), name.strip(), time.time(), token_limit, max_concurrent),
            )
        return {
            "id": key_id,
            "key": key,
            "name": name.strip(),
            "output_token_limit": token_limit,
            "max_concurrent": max_concurrent,
        }

    def list(self):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,name,created,revoked,token_limit AS output_token_limit,"
                    "used AS used_output_tokens,COALESCE(max_concurrent,2) AS max_concurrent "
                    "FROM keys ORDER BY created DESC"
                )
            ]

    def update(self, key_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {"output_token_limit", "reset_used", "max_concurrent"}
        if not isinstance(changes, dict) or not changes or (set(changes) - allowed):
            raise ValueError("supported fields: output_token_limit, reset_used, max_concurrent")
        assignments: list[str] = []
        params: list[Any] = []
        if "output_token_limit" in changes:
            assignments.append("token_limit=?")
            params.append(self._validate_token_limit(changes["output_token_limit"]))
        if "max_concurrent" in changes:
            assignments.append("max_concurrent=?")
            params.append(self._validate_max_concurrent(changes["max_concurrent"]))
        if "reset_used" in changes:
            if type(changes["reset_used"]) is not bool:
                raise ValueError("reset_used must be a boolean")
            if changes["reset_used"]:
                assignments.append("used=0")
        with self.lock:
            with self.connect() as db:
                existing = db.execute("SELECT id FROM keys WHERE id=?", (key_id,)).fetchone()
                if not existing:
                    raise HTTPException(404, "api_key_not_found")
                if assignments:
                    db.execute(f"UPDATE keys SET {', '.join(assignments)} WHERE id=?", (*params, key_id))
                row = db.execute(
                    "SELECT id,name,created,revoked,token_limit AS output_token_limit,"
                    "used AS used_output_tokens,COALESCE(max_concurrent,2) AS max_concurrent "
                    "FROM keys WHERE id=?",
                    (key_id,),
                ).fetchone()
                return dict(row)

    def revoke(self, key_id: str):
        with self.connect() as db:
            changed = db.execute("UPDATE keys SET revoked=1 WHERE id=?", (key_id,)).rowcount
        if not changed:
            raise HTTPException(404, "api_key_not_found")

    def purge(self, key_id: str):
        with self.lock:
            with self.connect() as db:
                row = db.execute("SELECT revoked FROM keys WHERE id=?", (key_id,)).fetchone()
                if not row:
                    raise HTTPException(404, "api_key_not_found")
                if not int(row["revoked"]):
                    raise HTTPException(409, "api_key_not_revoked")
                db.execute("DELETE FROM keys WHERE id=? AND revoked=1", (key_id,))
            self.active.pop(key_id, None)

    def aliases(self) -> dict[str, str]:
        with self.connect() as db:
            return dict(db.execute("SELECT name,target FROM model_aliases ORDER BY name"))

    def set_alias(self, name: str, target: str):
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}", name):
            raise ValueError("alias must be 1–64 letters, digits, dots, underscores or hyphens")
        with self.connect() as db:
            db.execute(
                "INSERT INTO model_aliases(name,target) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET target=excluded.target",
                (name, target),
            )

    def authenticate(self, token: str) -> str:
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM keys WHERE digest=? AND revoked=0",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
        if not row or not token:
            raise HTTPException(401, "invalid_api_key")
        return row["id"]

    def reserve(self, key_id: str, tokens: int, *, cap_to_remaining: bool = False) -> int:
        with self.lock:
            with self.connect() as db:
                key_row = db.execute(
                    "SELECT token_limit - used AS remaining, COALESCE(max_concurrent, 2) AS max_concurrent "
                    "FROM keys WHERE id=? AND revoked=0",
                    (key_id,),
                ).fetchone()
                if not key_row:
                    raise HTTPException(401, "invalid_api_key")
                max_conc = max(1, min(MAX_KEY_MAX_CONCURRENT, int(key_row["max_concurrent"])))
                if self.active.get(key_id, 0) >= max_conc:
                    raise HTTPException(429, "project_concurrency_limit")
                if cap_to_remaining:
                    if int(key_row["remaining"]) < 1:
                        raise HTTPException(429, "project_output_token_limit")
                    tokens = min(tokens, int(key_row["remaining"]))
                changed = db.execute(
                    "UPDATE keys SET used=used+? WHERE id=? AND revoked=0 AND used+?<=token_limit",
                    (tokens, key_id, tokens),
                ).rowcount
                if not changed:
                    raise HTTPException(429, "project_output_token_limit")
            self.active[key_id] = self.active.get(key_id, 0) + 1
            return tokens

    def finish(self, key_id: str, reserved: int, used: int):
        with self.lock:
            with self.connect() as db:
                db.execute("UPDATE keys SET used=MAX(0,used-?+?) WHERE id=?", (reserved, used, key_id))
            self.active[key_id] = max(0, self.active.get(key_id, 1) - 1)


def install_inference_api(app: Any, *, home: Path, store: Any, active_manager: Any,
                          local_manager_for: Any = None, local_service_ids: Any = None, discover: Any,
                          execute_order: Any, cancel_order: Any):
    install_body_limit(app)
    keys = InferenceKeys(home)
    network = os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main")
    cache_lock = threading.Lock()
    catalog_cache: dict[str, Any] = {"expires_at": -1.0, "topology": None, "values": None}
    health_cache: dict[tuple[str, int], dict[str, Any]] = {}

    def _probe_health(manager: Any) -> dict[str, Any]:
        try:
            res = manager.adapter.health()
            return dict(res) if isinstance(res, dict) else {"ok": False}
        except Exception:
            return {"ok": False}

    def cached_health(service_id: str, manager: Any) -> dict[str, Any]:
        cache_key = (service_id, id(manager.adapter))
        now = time.monotonic()
        with cache_lock:
            entry = health_cache.get(cache_key)
            if entry is not None:
                if now - entry["checked_at"] < HEALTH_CACHE_TTL_SECONDS:
                    return dict(entry["result"])
                if not entry["refreshing"]:
                    entry["refreshing"] = True

                    def _refresh_bg() -> None:
                        fresh = _probe_health(manager)
                        with cache_lock:
                            health_cache[cache_key] = {
                                "checked_at": time.monotonic(),
                                "result": fresh,
                                "refreshing": False,
                            }
                            catalog_cache["expires_at"] = -1.0

                    threading.Thread(
                        target=_refresh_bg,
                        name=f"ryn-api-health-{service_id}",
                        daemon=True,
                    ).start()
                return dict(entry["result"])
        fresh = _probe_health(manager)
        with cache_lock:
            health_cache[cache_key] = {
                "checked_at": time.monotonic(),
                "result": fresh,
                "refreshing": False,
            }
        return dict(fresh)

    def authorize(request: Request) -> str:
        host = urlparse("http://" + request.headers.get("host", "")).hostname
        if not is_loopback_addr(request.client.host if request.client else "") or is_forwarded(request.headers) or is_browser_cross_site(request.headers) or host not in {"127.0.0.1", "::1", "localhost"}:
            raise HTTPException(403, "inference_api_is_loopback_only")
        bearer = request.headers.get("authorization", "")
        token = bearer[7:] if bearer.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
        return keys.authenticate(token)

    def model_catalog():
        manager = active_manager()
        extra_entries: list[tuple[str, Any]] = []
        if local_manager_for:
            seen_services = {manager.manifest.package_id} if manager else set()
            for service_id in ["codex-cli", "claude-cli", *(local_service_ids() if local_service_ids else [])]:
                if service_id in seen_services:
                    continue
                seen_services.add(service_id)
                extra = local_manager_for(service_id)
                if extra:
                    extra_entries.append((service_id, extra))
        topology = (
            (manager.manifest.package_id, id(manager.adapter)) if manager else None,
            tuple((sid, id(ext.adapter)) for sid, ext in extra_entries),
        )
        now = time.monotonic()
        with cache_lock:
            if (
                catalog_cache["values"] is not None
                and catalog_cache["topology"] == topology
                and now < catalog_cache["expires_at"]
            ):
                return copy.deepcopy(catalog_cache["values"])

        def local_window(mgr):
            if mgr.manifest.adapter in {"codex_cli", "antigravity_cli"}:
                return mgr.context_window_for_model()
            return mgr.manifest.context_window

        values = []
        if manager and cached_health(manager.manifest.package_id, manager).get("ok"):
            values.append({"id": "local/" + manager.manifest.package_id, "object": "model", "created": 0, "owned_by": "local", "rynmesh": {"source": "local", "service_id": manager.manifest.package_id, "adapter": manager.manifest.adapter, "capabilities": manager.manifest.capabilities, "model_alias": manager.manifest.public_model_alias, "max_output_tokens": manager.manifest.max_output_tokens, "context_window": local_window(manager)}})
        for service_id, extra in extra_entries:
            if cached_health(service_id, extra).get("ok"):
                values.append({"id": "local/" + service_id, "object": "model", "created": 0,
                               "owned_by": "local", "rynmesh": {
                                   "source": "local", "service_id": service_id,
                                   "adapter": extra.manifest.adapter,
                                   "capabilities": extra.manifest.capabilities,
                                   "model_alias": extra.manifest.public_model_alias,
                                   "max_output_tokens": extra.manifest.max_output_tokens,
                                   "context_window": local_window(extra),
                               }})
        try:
            discovered = discover(network)
        except Exception:
            discovered = []  # local inference remains usable when registry is unavailable
        for item in discovered:
            # An advertisement is not entitlement: offer only what this node can actually use.
            if not is_usable(item) or item.get("peer_id") == store.peer_id or item.get("chat_protocol") != "rynmesh.chat.v1":
                continue
            service = item["service"]
            window = service["context_window"]
            if service.get("adapter") in {"codex_cli", "antigravity_cli"}:
                from .cli_models import context_metadata
                window = context_metadata(item.get("cli_models", []))["context_window"]
            # A full stable digest avoids ambiguity when models share aliases.
            digest = hashlib.sha256(item["peer_id"].encode()).hexdigest()
            values.append({"id": "peer/" + digest + "/" + service["package_id"], "object": "model", "created": 0, "owned_by": "rynmesh", "rynmesh": {"source": "peer", "provider_peer_id": item["peer_id"], "service_id": service["package_id"], "adapter": service.get("adapter", "openai_compatible"), "capabilities": service.get("capabilities", []), "model_alias": service["model_alias"], "max_output_tokens": service["max_output_tokens"], "context_window": window}})
        from .model_usage import ModelUsageStore
        usage_store = ModelUsageStore(home / "llm" / "model-usage.json")
        usage_snapshot = usage_store.read_cached()
        for entry in values:
            usage = usage_store.public_usage(entry["id"], snapshot=usage_snapshot)
            entry["usage"] = usage
            if isinstance(entry.get("rynmesh"), dict):
                entry["rynmesh"]["usage"] = usage
        with cache_lock:
            catalog_cache["topology"] = topology
            catalog_cache["values"] = copy.deepcopy(values)
            catalog_cache["expires_at"] = time.monotonic() + CATALOG_CACHE_TTL_SECONDS
        return values

    def public_models(catalog, aliases):
        targets = {item["id"]: item for item in catalog}
        short = [{**targets[target], "id": name} for name, target in aliases.items() if target in targets]
        return short + [item for item in catalog if item["id"] not in aliases.values()]

    @app.get("/api/local/llm/api-access")
    def access():
        port = int(os.environ.get("RYNMESH_PEER_PORT", "8791"))
        catalog = model_catalog()
        aliases = keys.aliases()
        return {"base_url": f"http://127.0.0.1:{port}/v1", "keys": keys.list(), "models": public_models(catalog, aliases), "targets": catalog, "aliases": aliases, "protocols": ["chat_completions", "responses_stateless", "anthropic_messages"], "loopback_only": True}

    @app.put("/api/local/llm/model-aliases/{name}")
    async def set_model_alias(name: str, request: Request):
        try:
            value = await request.json()
            if not isinstance(value, dict):
                raise ValueError("request must be a JSON object")
            target = value.get("target")
            catalog = await asyncio.to_thread(model_catalog)
            if not isinstance(target, str) or not any(item["id"] == target for item in catalog):
                raise HTTPException(400, "choose an available model target")
            keys.set_alias(name, target)
            return {"name": name, "target": target}
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/local/llm/api-keys")
    async def create_key(request: Request):
        try:
            value = await request.json()
            if not isinstance(value, dict):
                raise ValueError("request must be a JSON object")
            return keys.create(
                value.get("name", ""),
                value.get("output_token_limit", 100000),
                value.get("max_concurrent", DEFAULT_KEY_MAX_CONCURRENT),
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.patch("/api/local/llm/api-keys/{key_id}")
    async def patch_key(key_id: str, request: Request):
        try:
            value = await request.json()
            if not isinstance(value, dict):
                raise ValueError("request must be a JSON object")
            return keys.update(key_id, value)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/local/llm/api-keys/{key_id}")
    def revoke_key(key_id: str, purge: bool = False):
        if purge:
            keys.purge(key_id)
            return {"purged": True, "deleted": True}
        keys.revoke(key_id)
        return {"revoked": True}

    def error(exc: Exception, protocol: str) -> JSONResponse:
        closed_code: str | None = None
        resp_headers: dict[str, str] = {}
        if isinstance(exc, HTTPException):
            status, message = exc.status_code, str(exc.detail)
            if exc.headers:
                resp_headers.update(exc.headers)
        elif isinstance(exc, AdapterError):
            closed_code = exc.code if exc.code in RUNTIME_ERROR_CODES else "inference_failed"
            status = _ADAPTER_ERROR_STATUS.get(closed_code, 502)
            message = _ADAPTER_ERROR_MESSAGES.get(closed_code, "Model inference failed.")
        elif isinstance(exc, (ValueError, TypeError)):
            status, message = 400, str(exc)
        elif isinstance(exc, (KeyError, AttributeError, IndexError)):
            status, message = 500, "internal_error"
            closed_code = "internal_error"
        else:
            status, message = 502, "Model inference failed."
            closed_code = "model_inference_failed"
        kind = "authentication_error" if status == 401 else "permission_error" if status == 403 else "rate_limit_error" if status == 429 else "invalid_request_error" if status < 500 else "api_error"
        body: dict[str, Any] = {"error": {"type": kind, "message": message}}
        if status == 413:
            body["detail"] = "request_too_large"
            body["limit_bytes"] = getattr(exc, "limit_bytes", MAX_REQUEST)
        if protocol == "messages":
            body["type"] = "error"
            if closed_code is not None:
                body["error"]["code"] = closed_code
        else:
            body["error"].update(
                code="request_too_large" if status == 413 else (closed_code or message),
                param=None,
            )
        return JSONResponse(body, status_code=status, headers=resp_headers or None)

    @app.get("/v1/models")
    async def models(request: Request):
        try:
            authorize(request)
            return {"object": "list", "data": public_models(await asyncio.to_thread(model_catalog), keys.aliases())}
        except Exception as exc:
            return error(exc, "chat")

    async def handle(request: Request, protocol: str):
        try:
            key_id = authorize(request)
            raw = bytearray()
            async for part in request.stream():
                raw.extend(part)
                if len(raw) > MAX_REQUEST:
                    raise HTTPException(
                        413,
                        f"request exceeds byte limit ({MAX_REQUEST} bytes)；多张图片时请先压缩或减少张数",
                    )
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("request must be a JSON object")
            chat, ignored_list = normalize_with_ignored(body, protocol)
            ignored_params = set(ignored_list)
            catalog = await asyncio.to_thread(model_catalog)
            requested = body.get("model")
            if not isinstance(requested, str):
                raise ValueError("model must be a string")
            aliases = keys.aliases()
            target_id = aliases.get(requested, requested)
            model = next((m for m in catalog if m["id"] == target_id), None)
            if model is None:
                if requested in aliases:
                    raise HTTPException(503, "model_alias_target_unavailable")
                raise HTTPException(404, "model_not_found; choose an id from /v1/models")
            model = {**model, "id": requested}
            target = model["rynmesh"]
            if target.get("adapter") in {"codex_cli", "claude_cli", "antigravity_cli"}:
                if chat.get("stream") and target["adapter"] == "claude_cli":
                    raise HTTPException(400, "CLI API mode does not support streaming yet")
                if target["adapter"] == "claude_cli" and (chat.get("tools") or chat.get("tool_choice", "auto") not in ("auto", "none")):
                    raise HTTPException(400, "CLI API mode does not support client tool calls")
                for cli_ignored in ("temperature", "top_p"):
                    if cli_ignored in chat:
                        chat.pop(cli_ignored, None)
                        ignored_params.add(cli_ignored)
                unsupported = set(chat) & {"stop", "enable_thinking"}
                if unsupported:
                    raise HTTPException(400, "CLI API mode does not support " + ", ".join(sorted(unsupported)))
            # Client defaults often describe a larger model (e.g. 128k output).
            # A requested maximum is a ceiling, so fit it to the selected model
            # and estimated remaining context before reserving quota or routing.
            estimated_input = estimate_context_safety_tokens(json.dumps(chat, ensure_ascii=False))
            available = target["context_window"] - estimated_input
            if available < 1:
                raise HTTPException(400, context_overflow_detail())
            requested_max = chat.get("max_tokens")
            chat["max_tokens"] = min(
                requested_max if requested_max is not None else target["max_output_tokens"],
                target["max_output_tokens"],
                available,
                393216,
            )
            task_id = "api_" + uuid.uuid4().hex
            manager = (local_manager_for(target["service_id"]) if local_manager_for
                       else active_manager()) if target["source"] == "local" else None
            if target["source"] == "local" and manager is None:
                raise HTTPException(503, "local_model_unavailable")
            local_binding = (manager.adapter.binding_for_model() if manager
                and callable(getattr(manager.adapter, "binding_for_model", None)) else None)
            if manager:
                acquired = manager._slots.acquire(blocking=False)
                if not acquired:
                    wait_deadline = time.monotonic() + LOCAL_SLOT_WAIT_SECONDS
                    while True:
                        if await request.is_disconnected():
                            raise HTTPException(499, "client_disconnected")
                        now = time.monotonic()
                        if now >= wait_deadline:
                            break
                        await asyncio.sleep(min(LOCAL_SLOT_POLL_SECONDS, wait_deadline - now))
                        acquired = manager._slots.acquire(blocking=False)
                        if acquired:
                            break
                if not acquired:
                    raise HTTPException(
                        429,
                        "local_model_busy",
                        headers={"Retry-After": LOCAL_SLOT_RETRY_AFTER},
                    )
            try:
                chat["max_tokens"] = keys.reserve(
                    key_id,
                    chat["max_tokens"],
                    cap_to_remaining=requested_max is None,
                )
            except Exception:
                if manager:
                    manager._slots.release()
                raise
        except Exception as exc:
            return error(exc, protocol)

        started = False
        release_lock = threading.Lock()
        released = False

        def release_resources(used_tokens: int = 0) -> None:
            nonlocal released
            with release_lock:
                if released:
                    return
                released = True
            keys.finish(key_id, chat["max_tokens"], used_tokens)
            if manager:
                manager._slots.release()

        def release_unused() -> None:
            if not started:
                release_resources(0)

        async def run(emit):
            nonlocal started
            started = True
            used = 0
            stream_reported_output: int | None = None
            running_marked = False

            def tracked_emit(chunk: Any) -> None:
                nonlocal stream_reported_output
                if isinstance(chunk, dict) and isinstance(chunk.get("usage"), dict):
                    out_val = actual_usage(chunk)["output_tokens"]
                    if type(out_val) is int and out_val >= 0:
                        stream_reported_output = out_val
                if emit is not None:
                    emit(chunk)

            emit_arg = tracked_emit if emit is not None else None
            try:
                if manager:
                    with manager._lock:
                        manager._running += 1
                    running_marked = True
                    # Each local API key is its own requester; these clients send no window key.
                    scope = ({"conversation_scope": codex_threads.scope("local-api:" + str(key_id))}
                             if getattr(manager.adapter, "supports_conversation_scope", False) is True else {})

                    def authorize_context():
                        current_window = (manager.context_window_for_model() if target["adapter"] in
                                          {"codex_cli", "antigravity_cli"} else manager.manifest.context_window)
                        if estimated_input + chat["max_tokens"] > current_window:
                            raise ValueError(context_overflow_detail())

                    if local_binding is not None:
                        result = await asyncio.to_thread(manager.adapter.call_bound, "chat", local_binding,
                            authorize_context, chat, task_id=task_id, timeout_s=manager.manifest.timeout_seconds,
                            on_event=emit_arg, **scope)
                    else:
                        authorize_context()
                        result = await asyncio.to_thread(manager.adapter.chat, chat, task_id=task_id,
                                                         timeout_s=manager.manifest.timeout_seconds, on_event=emit_arg, **scope)
                else:
                    result = await execute_order({"network_id": network, "provider_peer_id": target["provider_peer_id"],
                                                  "service_id": target["service_id"], "chat": chat,
                                                  "_no_retention": True,
                                                  "transport": "p2p",
                                                  "max_tokens": chat["max_tokens"], "task_id": task_id}, emit_arg)
                    if result.get("state") != "succeeded":
                        peer_out = actual_usage(result)["output_tokens"]
                        if type(peer_out) is int and peer_out >= 0:
                            used = peer_out
                        elif stream_reported_output is not None:
                            used = stream_reported_output
                        err_code = str(result.get("error_code") or "inference_failed")
                        if err_code in RUNTIME_ERROR_CODES:
                            raise AdapterError("peer inference failed", code=err_code,
                                               reported_usage={"output_tokens": used} if used > 0 else None)
                        raise HTTPException(502, err_code)
                actual_output = actual_usage(result)["output_tokens"]
                if len(json.dumps(result).encode()) > MAX_OUTPUT:
                    if type(actual_output) is int and actual_output >= 0:
                        used = actual_output
                    elif stream_reported_output is not None:
                        used = stream_reported_output
                    raise HTTPException(502, "provider_output_limit_exceeded")
                used = (
                    actual_output
                    if (type(actual_output) is int and actual_output >= 0)
                    else (stream_reported_output if stream_reported_output is not None else chat["max_tokens"])
                )
                return completion(result, model["id"], task_id)
            except AdapterError as exc:
                rep = exc.reported_usage
                if isinstance(rep, dict) and type(rep.get("output_tokens")) is int and rep["output_tokens"] >= 0:
                    used = rep["output_tokens"]
                elif stream_reported_output is not None:
                    used = stream_reported_output
                else:
                    used = 0
                raise
            except BaseException:
                if used == 0 and stream_reported_output is not None:
                    used = stream_reported_output
                raise
            finally:
                if running_marked and manager:
                    with manager._lock:
                        manager._running -= 1
                release_resources(used)

        def cancel():
            if manager:
                manager.adapter.cancel(task_id)
            else:
                try:
                    cancel_order(task_id)
                except HTTPException:
                    pass

        ignored_headers = (
            {"X-Ryn-Ignored-Parameters": ", ".join(sorted(ignored_params))}
            if ignored_params
            else {}
        )

        if not chat.get("stream"):
            if await request.is_disconnected():
                release_unused()
                return error(HTTPException(499, "client_disconnected"), protocol)
            # Watch a disconnected client while keeping the worker alive for cleanup.
            task = asyncio.create_task(run(None))
            task.add_done_callback(lambda _fut: release_unused())
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=0.2)
                    if await request.is_disconnected():
                        await asyncio.to_thread(cancel)
                        break
                payload = convert_response(await asyncio.shield(task), protocol, request_body=body)
                return JSONResponse(payload, headers=ignored_headers)
            except asyncio.CancelledError:
                cleanup = asyncio.create_task(asyncio.to_thread(cancel))
                cleanup.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
                raise
            except Exception as exc:
                return error(exc, protocol)

        async def generate():
            formatter = StreamFormat(protocol, model["id"], task_id, request_body=body)
            try:
                for value in formatter.start():
                    yield value
                async for kind, value in events(run, cancel):
                    if protocol == "chat":
                        if kind == "event":
                            # Publish one final accounting frame from the same
                            # canonical result used for quota, never a raw
                            # upstream event that could disagree with it.
                            value = {key: item for key, item in value.items() if key != "usage"}
                            if not value.get("choices"):
                                continue
                        elif (body.get("stream_options") or {}).get("include_usage"):
                            for data in formatter.delta({"choices": [], "usage": value["usage"]}):
                                yield data
                    for data in formatter.delta(value) if kind == "event" else formatter.finish(value):
                        yield data
            except Exception as exc:
                payload = json.loads(error(exc, protocol).body)
                err_code = payload.get("error", {}).get("code") or "inference_failed"
                yield sse(
                    payload
                    if protocol != "responses"
                    else {"type": "error", "code": err_code, "message": payload["error"]["message"]},
                    "error" if protocol != "chat" else "",
                )
            finally:
                release_unused()

        from starlette.background import BackgroundTask

        return StreamingResponse(
            _GuardedStream(generate(), release_unused),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", **ignored_headers},
            background=BackgroundTask(release_unused),
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        return await handle(request, "chat")

    @app.post("/v1/responses")
    async def responses(request: Request):
        return await handle(request, "responses")

    @app.post("/v1/messages")
    async def messages(request: Request):
        return await handle(request, "messages")
