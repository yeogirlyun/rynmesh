"""Rynmesh HTTP registry, polling mailbox, and relay service.

The registry is a coordination plane: it stores self-signed peer records,
signed job-control messages, and optional relay blobs for nodes that cannot
accept inbound connections. Content still moves directly between peers when
both endpoints are reachable.
"""

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, Optional

from .crypto import SignedPayload
from .jobs import JobError
from .registry import FilePeerRegistry, PeerRegistry, RegistryError
from .relay import FileRelayStore, RelayError

MAX_REGISTRY_REQUEST_BYTES = 1024 * 1024


def create_app(
    registry: Optional[PeerRegistry] = None,
    relay_store: Optional[FileRelayStore] = None,
):
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import FileResponse, JSONResponse
    except ImportError as exc:  # pragma: no cover
        raise ImportError("Rynmesh registry HTTP server requires `fastapi`") from exc

    active_registry = registry or FilePeerRegistry(default_registry_root())
    active_relay = relay_store or FileRelayStore(default_registry_root() / "relay")
    app = FastAPI(title="Rynmesh Registry", version="0.1")

    @app.middleware("http")
    async def _guard_registry_api(request: Request, call_next):
        """Hide the public coordination/relay surface behind the mesh key."""
        key = os.environ.get("RYNMESH_NETWORK_KEY", "").strip()
        path = request.url.path
        if key and (path == "/health" or path.startswith("/api/v1")):
            expected = hashlib.sha256(("rynmesh-net-key:" + key).encode("utf-8")).hexdigest()
            if not hmac.compare_digest(request.headers.get("x-ryn-auth", ""), expected):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
        return await call_next(request)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "kind": "rynmesh-registry"}

    @app.post("/api/v1/peers/register")
    async def register_peer(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > MAX_REGISTRY_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="registry_request_too_large")
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("signed_record_not_object")
            signed = SignedPayload.from_dict(payload)
            return active_registry.publish(signed)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, RegistryError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/peers")
    def list_peers(network_id: str = "rynmesh-main", max_age_hours: float = 0.0) -> dict[str, Any]:
        try:
            peers = [
                signed.to_dict()
                for signed in active_registry.list_peers(
                    network_id=network_id,
                    max_age_hours=max_age_hours if max_age_hours > 0 else None,
                )
            ]
        except RegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"network_id": network_id, "peers": peers}

    @app.post("/api/v1/jobs/capacity/register")
    async def register_job_capacity(request: Request) -> dict[str, Any]:
        signed = await _signed_request(request)
        try:
            return active_registry.publish_job_capacity(signed)
        except (RegistryError, JobError, KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/jobs/capacity")
    def list_job_capacity(
        network_id: str = "rynmesh-main",
        capability: str = "",
        max_age_hours: float = 0.0,
    ) -> dict[str, Any]:
        try:
            capacities = [
                signed.to_dict()
                for signed in active_registry.list_job_capacities(
                    network_id=network_id,
                    capability=capability,
                    max_age_hours=max_age_hours if max_age_hours > 0 else None,
                )
            ]
        except (RegistryError, JobError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"network_id": network_id, "capacities": capacities}

    @app.post("/api/v1/jobs/work-orders")
    async def submit_work_order(request: Request) -> dict[str, Any]:
        signed = await _signed_request(request)
        try:
            return active_registry.submit_work_order(signed)
        except (RegistryError, JobError, KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/jobs/work-orders")
    def list_work_orders(
        network_id: str = "rynmesh-main",
        provider_peer_id: str = "",
        requester_peer_id: str = "",
        capability: str = "",
        status: str = "open",
        max_age_hours: float = 0.0,
    ) -> dict[str, Any]:
        try:
            work_orders = [
                signed.to_dict()
                for signed in active_registry.list_work_orders(
                    network_id=network_id,
                    provider_peer_id=provider_peer_id,
                    requester_peer_id=requester_peer_id,
                    capability=capability,
                    status=status,
                    max_age_hours=max_age_hours if max_age_hours > 0 else None,
                )
            ]
        except (RegistryError, JobError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"network_id": network_id, "work_orders": work_orders}

    @app.post("/api/v1/jobs/work-results")
    async def publish_work_result(request: Request) -> dict[str, Any]:
        signed = await _signed_request(request)
        try:
            return active_registry.publish_work_result(signed)
        except (RegistryError, JobError, KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/jobs/work-results")
    def list_work_results(
        work_order_id: str = "",
        network_id: str = "rynmesh-main",
        requester_peer_id: str = "",
        provider_peer_id: str = "",
        status: str = "",
    ) -> dict[str, Any]:
        try:
            work_results = [
                signed.to_dict()
                for signed in active_registry.list_work_results(
                    work_order_id=work_order_id,
                    network_id=network_id,
                    requester_peer_id=requester_peer_id,
                    provider_peer_id=provider_peer_id,
                    status=status,
                )
            ]
        except (RegistryError, JobError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"network_id": network_id, "work_results": work_results}

    @app.post("/api/v1/relay/blobs")
    async def upload_relay_blob(request: Request) -> dict[str, Any]:
        content_length = request.headers.get("content-length", "")
        if content_length:
            try:
                if int(content_length) > active_relay.max_blob_bytes:
                    raise HTTPException(status_code=413, detail="relay_blob_too_large")
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail="relay_content_length_invalid",
                ) from exc
        try:
            record = await active_relay.put_async_chunks(
                request.stream(),
                media_type=request.headers.get("content-type", "application/octet-stream"),
                filename=request.headers.get("x-rynmesh-filename", ""),
                uploader_peer_id=request.headers.get("x-rynmesh-uploader-peer-id", ""),
                expected_hash=request.headers.get("x-rynmesh-expected-hash", ""),
            )
        except RelayError as exc:
            raise _relay_http_exception(exc, HTTPException) from exc
        return {"status": "stored", "blob": record.to_dict()}

    @app.post("/api/v1/relay/blobs/assemble")
    async def assemble_relay_blob(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > MAX_REGISTRY_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="relay_assemble_request_too_large")
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("relay_assemble_request_not_object")
            chunks = payload.get("chunks", [])
            if not isinstance(chunks, list):
                raise ValueError("relay_chunks_not_list")
            metadata = payload.get("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            record = active_relay.put_chunk_refs(
                chunks,
                media_type=str(payload.get("media_type") or "application/octet-stream"),
                filename=str(payload.get("filename") or ""),
                uploader_peer_id=str(payload.get("uploader_peer_id") or ""),
                metadata=metadata,
                expected_hash=str(payload.get("expected_hash") or ""),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, RelayError) as exc:
            if isinstance(exc, RelayError):
                raise _relay_http_exception(exc, HTTPException) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "stored", "blob": record.to_dict()}

    @app.get("/api/v1/relay/meta/{content_hash}")
    def relay_blob_meta(content_hash: str) -> dict[str, Any]:
        try:
            return active_relay.info(content_hash).to_dict()
        except RelayError as exc:
            raise _relay_http_exception(exc, HTTPException) from exc

    @app.get("/api/v1/relay/blobs/{content_hash}")
    def download_relay_blob(content_hash: str):
        try:
            record = active_relay.info(content_hash)
            path = active_relay.path_for(content_hash)
        except RelayError as exc:
            raise _relay_http_exception(exc, HTTPException) from exc
        kwargs: dict[str, Any] = {"media_type": record.media_type}
        if record.filename:
            kwargs["filename"] = record.filename
        return FileResponse(path, **kwargs)

    def _release_path():
        from pathlib import Path
        home = Path(os.environ.get("RYNMESH_HOME", str(Path.home() / ".rynmesh")))
        d = home / "releases"
        d.mkdir(parents=True, exist_ok=True)
        return d / "latest.json"

    @app.post("/api/v1/releases")
    async def post_release(request: Request) -> dict[str, Any]:
        body = await request.json()
        if not isinstance(body, dict) or "payload" not in body or "signature" not in body:
            raise HTTPException(status_code=400, detail="signed manifest required")
        _release_path().write_text(json.dumps(body, indent=2))
        return {"ok": True, "version": body.get("payload", {}).get("version", "")}

    @app.get("/api/v1/releases/latest")
    def get_latest_release() -> dict[str, Any]:
        p = _release_path()
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return {}

    return app


def _relay_http_exception(exc: RelayError, http_exception_type: Any) -> Any:
    detail = str(exc)
    status_code = 400
    if detail == "relay_blob_not_found":
        status_code = 404
    elif detail == "relay_blob_too_large":
        status_code = 413
    return http_exception_type(status_code=status_code, detail=detail)


async def _signed_request(request: Any) -> SignedPayload:
    raw = await request.body()
    if len(raw) > MAX_REGISTRY_REQUEST_BYTES:
        try:
            from fastapi import HTTPException
        except ImportError:  # pragma: no cover
            raise ValueError("registry_request_too_large") from None
        raise HTTPException(status_code=413, detail="registry_request_too_large")
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            raise ValueError("signed_record_not_object")
        return SignedPayload.from_dict(payload)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        try:
            from fastapi import HTTPException
        except ImportError:  # pragma: no cover
            raise
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def default_registry_root() -> Path:
    return Path(os.environ.get("RYNMESH_REGISTRY_DIR", Path.home() / ".rynmesh" / "registry"))


def main() -> int:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise ImportError("Rynmesh registry HTTP server requires `uvicorn`") from exc

    host = os.environ.get("RYNMESH_REGISTRY_HOST", "127.0.0.1")
    port = int(os.environ.get("RYNMESH_REGISTRY_PORT", "8790") or 8790)
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
