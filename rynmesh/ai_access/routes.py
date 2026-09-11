"""Owner-only controls for persisted, explicit friend AI grants."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException, Request

from .store import AIAccessError, AIAccessStore


@dataclass
class AIAccessState:
    store: AIAccessStore
    local_control: Callable
    recheck: Callable | None = None


def install_ai_access(app: Any, *, home: str | Path, local_control: Callable,
                      relationship: Callable) -> AIAccessStore:
    permissions = AIAccessStore(home, relationship=relationship)
    prior = getattr(app.state, "ai_access", None)
    app.state.ai_access = AIAccessState(permissions, local_control, getattr(prior, "recheck", None))
    if any(getattr(route, "name", "") == "ai_access_list" for route in app.routes):
        return permissions

    @app.get("/api/local/ai-access", name="ai_access_list")
    async def list_grants(request: Request):
        app.state.ai_access.local_control(request)
        try:
            return {"grants": await asyncio.to_thread(app.state.ai_access.store.list)}
        except (AIAccessError, OSError, ValueError):
            raise HTTPException(503, detail="ai_permissions_unavailable") from None

    @app.put("/api/local/ai-access/{service_id}/{relationship_id}")
    async def set_grant(service_id: str, relationship_id: str, request: Request):
        app.state.ai_access.local_control(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096:
                raise HTTPException(413, detail="ai_permission_request_too_large")
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError
            grant = await asyncio.to_thread(app.state.ai_access.store.set, service_id, relationship_id,
                                             allowed=body.get("allowed"), expected_revision=body.get("expected_revision"))
        except AIAccessError as exc:
            raise HTTPException(409, detail=str(exc)) from None
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, detail="ai_permission_request_invalid") from None
        except OSError:
            raise HTTPException(503, detail="ai_permissions_unavailable") from None
        # The grant is already durable. Cancellation is best effort and has
        # its own result; an unsuccessful notification must not undo revocation.
        cancellation = "not_requested"
        if not grant["allowed"] and app.state.ai_access.recheck:
            try:
                await asyncio.to_thread(app.state.ai_access.recheck)
                cancellation = "requested"
            except Exception:
                cancellation = "pending"
        return {"grant": grant, "cancellation": cancellation}

    return permissions
