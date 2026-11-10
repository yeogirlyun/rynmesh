"""Local library projection over the existing managed imports and reading stores."""
from typing import Any

from fastapi import HTTPException
from fastapi import Request as FastAPIRequest

from .digest import DigestError
from .library import LibraryError, LibraryService
from .library_imports import MAX_IMPORT_BYTES, LibraryImportError
from .library_metadata import LibraryMetadataError, LibraryMetadataStore


def install_library(app, *, home, imports, consumption, digest, local_control):
    app.state.library_imports = imports
    app.state.library_metadata = LibraryMetadataStore(home / "library" / "metadata.json")
    app.state.library_service = LibraryService(consumption=consumption, imports=imports, metadata=app.state.library_metadata)
    app.state.library_enabled = True
    def _library_service(): return app.state.library_service
    def _digest_service(): return digest()
    def _library_enabled() -> None:
        if not app.state.library_enabled:
            raise HTTPException(status_code=404, detail="library_workspace_disabled")

    @app.post("/api/local/library/contexts")
    async def library_contexts(request: FastAPIRequest):
        local_control(request)
        value = await request.json()
        ids = value.get("library_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 8 or any(not isinstance(i, str) for i in ids):
            raise HTTPException(400, detail="library_context_limit")
        try:
            sources = []
            for identifier in ids:
                if identifier.startswith("import:"):
                    sources.append(app.state.ask_ryn.context.describe(identifier))
                else:
                    record = _library_service()._consumption_record(identifier)
                    sources.append(app.state.ask_ryn.context.prepare(record['item_id']))
            return {"materials": [source['library_id'] for source in sources]}
        except (ValueError, KeyError):
            raise HTTPException(409, detail="ask_context_unavailable") from None

    @app.get("/api/local/library")
    def local_library(
        request: FastAPIRequest,
        view: str = "all",
        query: str = "",
        kind: str = "",
        source: str = "",
        cursor: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        try:
            return _library_service().list(
                view=view, query=query, kind=kind, source=source, cursor=cursor, limit=limit
            )
        except LibraryError as exc:
            raise HTTPException(status_code=400, detail=exc.code) from exc

    @app.post("/api/local/library/imports")
    async def local_library_import(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        try:
            content_length = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            content_length = 0
        if content_length > ((MAX_IMPORT_BYTES + 2) // 3) * 4 + 128 * 1024:
            raise HTTPException(status_code=413, detail="library_import_size_limit")
        try:
            imported = app.state.library_imports.import_bytes(dict(await request.json()))
            return _library_service().get(f"import:{imported['import_id']}")
        except LibraryImportError as exc:
            raise HTTPException(status_code=400, detail=exc.code) from exc

    @app.get("/api/local/library/imports/{import_id}/status")
    def local_library_import_status(import_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        try:
            return app.state.library_imports.get(import_id)
        except LibraryImportError as exc:
            raise HTTPException(status_code=404, detail=exc.code) from exc

    @app.get("/api/local/library/imports/{import_id}/preview")
    def local_library_import_preview(import_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        try:
            metadata = app.state.library_imports.get(import_id)
            text = app.state.library_imports.extracted_text(import_id)
            return {"import_id": import_id, "title": metadata["filename"], "text": text[:64 * 1024], "truncated": len(text) > 64 * 1024}
        except LibraryImportError as exc:
            raise HTTPException(status_code=404, detail=exc.code) from exc

    @app.delete("/api/local/library/imports/{import_id}")
    def local_library_import_delete(import_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        raise HTTPException(409, detail="library_cleanup_review_required")

    @app.post("/api/local/library/actions")
    async def local_library_actions(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        try:
            return _library_service().actions(dict(await request.json()))
        except LibraryError as exc:
            raise HTTPException(status_code=400, detail=exc.code) from exc

    @app.get("/api/local/library/sources")
    def local_library_sources(request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        health = {
            str(item.get("id", "")): item
            for item in _digest_service().discovery_status().get("source_health", [])
        }
        sources = []
        for source_item in _digest_service().list_sources():
            source_id = str(source_item.get("id", ""))
            sources.append({**source_item, "paused": bool(source_item.get("paused")), "health": health.get(source_id, {})})
        return {"sources": sources, "watchers": _digest_service().list_watchers()}

    @app.patch("/api/local/library/sources/{source_id}")
    async def local_library_source_patch(source_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        body = dict(await request.json())
        if set(body) != {"paused"}:
            raise HTTPException(status_code=400, detail="library_source_patch_invalid")
        try:
            return _digest_service().set_source_paused(source_id, bool(body["paused"]))
        except DigestError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.patch("/api/local/library/watchers/{watcher_id}")
    async def local_library_watcher_patch(watcher_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        body = dict(await request.json())
        if set(body) != {"paused"}:
            raise HTTPException(status_code=400, detail="library_watcher_patch_invalid")
        try:
            return _digest_service().set_watcher_paused(watcher_id, bool(body["paused"]))
        except DigestError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/local/library/{library_id:path}")
    def local_library_item(library_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        try:
            return _library_service().get(library_id)
        except LibraryError as exc:
            raise HTTPException(status_code=404, detail=exc.code) from exc

    @app.patch("/api/local/library/{library_id:path}")
    async def local_library_item_patch(library_id: str, request: FastAPIRequest) -> dict[str, Any]:
        local_control(request)
        _library_enabled()
        try:
            return _library_service().patch(library_id, dict(await request.json()))
        except (LibraryError, LibraryMetadataError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

