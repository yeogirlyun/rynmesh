"""The image studio as an in-process node site: what colleagues reach through Ryn.

Identity comes only from the headers the node-sites host sets after its own
checks (the caller cannot supply them). Every job and file lookup is scoped to
that caller; another colleague's job id answers 404, never 403.
"""

from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import generator, sizes
from .jobs import MAX_UPLOAD_BYTES, JobError, JobStore, caller_key

SITE_ID = "image-studio"
TITLE = "做图"
WEB_ROOT = Path(__file__).resolve().parent / "web"
JOB_PATH = re.compile(r"^/api/jobs/([0-9a-f]{24})(?:/(cancel|retry|image|thumb))?$")

Response = tuple[int, list[list[str]], bytes]


def _json(status: int, value: Any) -> Response:
    return status, [["content-type", "application/json; charset=utf-8"], ["cache-control", "no-store"]], \
        json.dumps(value, ensure_ascii=False).encode("utf-8")


def _error(code: str, status: int) -> Response:
    return _json(status, {"code": code})


def _header(headers: dict[str, str], name: str) -> str:
    lower = name.lower()
    return next((v for k, v in headers.items() if k.lower() == lower), "")


class StudioSite:
    def __init__(self, store: JobStore) -> None:
        self.store = store

    def identity(self, headers: dict[str, str]) -> tuple[str, str, str]:
        peer = _header(headers, "X-Ryn-Caller-Peer")
        member = unquote(_header(headers, "X-Ryn-Caller-Member"))
        name = unquote(_header(headers, "X-Ryn-Caller-Name")) or "同事"
        resource = unquote(_header(headers, "X-Ryn-Caller-Resource"))
        if not peer or not resource:
            raise JobError("image_identity_missing", 403)
        return caller_key(resource, member, peer), name, peer

    def __call__(self, method: str, path: str, headers: dict[str, str], body: bytes) -> Response:
        try:
            return self.handle(method, path, headers, body)
        except JobError as exc:
            return _error(exc.code, exc.status)

    def handle(self, method: str, target: str, headers: dict[str, str], body: bytes) -> Response:
        parts = urlsplit(target)
        path, query = parts.path, parse_qs(parts.query)
        if not path.startswith("/api/"):
            return self.static(method, path)
        caller, name, peer = self.identity(headers)
        if path == "/api/me" and method == "GET":
            queue = self.store.queue()
            return _json(200, {"name": name, "model": generator.readiness(), "queue_length": len(queue),
                               "average_seconds": round(self.store.average_seconds()),
                               "purposes": {k: {"label": v["label"], "ratio": v["ratio"], "width": v["size"][0],
                                                "height": v["size"][1]} for k, v in sizes.PURPOSES.items()}})
        if path == "/api/uploads" and method == "POST":
            if len(body) > MAX_UPLOAD_BYTES:
                return _error("image_upload_too_large", 413)
            return _json(200, self.store.add_upload(caller, body))
        if path == "/api/jobs" and method == "POST":
            try:
                value = json.loads(body or b"{}")
            except ValueError:
                return _error("image_bad_request", 400)
            return _json(200, {"jobs": self.store.submit(caller, name, peer, value)})
        if path == "/api/jobs" and method == "GET":
            state = (query.get("state") or ["all"])[0]
            try:
                limit = int((query.get("limit") or ["100"])[0])
            except ValueError:
                return _error("image_bad_request", 400)
            return _json(200, {"jobs": self.store.list(caller, state=state, limit=limit),
                               "queue_length": len(self.store.queue())})
        match = JOB_PATH.fullmatch(path)
        if match:
            job_id, action = match.groups()
            if action is None and method == "GET":
                return _json(200, self.store.view(self.store.job(caller, job_id)))
            if action == "cancel" and method == "POST":
                return _json(200, self.store.cancel(caller, job_id))
            if action == "retry" and method == "POST":
                return _json(200, self.store.retry(caller, job_id))
            if action in ("image", "thumb") and method in ("GET", "HEAD"):
                row = self.store.job(caller, job_id)
                if row["state"] != "succeeded":
                    return _error("image_not_ready", 404)
                file = self.store.image_path(row, "png" if action == "image" else "jpg")
                data = file.read_bytes() if method == "GET" else b""
                headers_out = [["content-type", "image/png" if action == "image" else "image/jpeg"],
                               ["cache-control", "private, max-age=86400, immutable"]]
                if action == "image":
                    headers_out.append(["content-disposition", f'attachment; filename="ryn-{job_id[:8]}.png"'])
                return 200, headers_out, data
        return _error("image_not_found", 404)

    def static(self, method: str, path: str) -> Response:
        """The page itself (built files only; inline scripts are blocked by the gateway CSP)."""
        if method not in ("GET", "HEAD"):
            return _error("image_not_found", 404)
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        if ".." in relative.split("/") or "\\" in relative:
            return _error("image_not_found", 404)
        file = (WEB_ROOT / relative).resolve()
        if not file.is_file() or WEB_ROOT.resolve() not in file.parents:
            if "." not in relative.rsplit("/", 1)[-1]:  # client-side route: serve the app shell
                file = WEB_ROOT / "index.html"
            if not file.is_file():
                return 404, [["content-type", "text/plain; charset=utf-8"]], b"not found"
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in ("application/javascript", "image/svg+xml"):
            mime += "; charset=utf-8"
        cache = "no-store" if file.name == "index.html" else "private, max-age=31536000, immutable"
        return 200, [["content-type", mime], ["cache-control", cache]], file.read_bytes() if method == "GET" else b""
