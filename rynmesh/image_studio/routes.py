"""Wire the image studio into the node: in-process node site + owner-only control routes."""

from __future__ import annotations

import asyncio
import datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import generator
from .jobs import JobError, JobStore, Worker
from .site import SITE_ID, TITLE, StudioSite


class ImageStudio:
    def __init__(self, home: Path, sites: Any, *, generate: Callable[..., Any] | None = None) -> None:
        self.store = JobStore(home)
        self.worker = Worker(self.store, **({"generate": generate} if generate else {}))
        self.site = StudioSite(self.store)
        self.sites = sites
        # Registered closed on first run; the owner opens it from Shared pages.
        sites.store.register(sid=SITE_ID, title=TITLE, builtin=True, entry="/")
        sites.host.builtins[SITE_ID] = self.site

    def start(self) -> "ImageStudio":
        self.worker.start()
        return self

    def close(self) -> None:
        self.worker.stop()

    @staticmethod
    def day_start() -> float:
        today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return today.timestamp()

    def status(self) -> dict[str, Any]:
        queue = self.store.queue()
        site = self.sites.store.site(SITE_ID) or {}
        usage = self.store.usage(day_start=self.day_start())
        return {"model": generator.readiness(), "enabled": bool(site.get("enabled")),
                "audience": site.get("audience") or {"mode": "all", "members": []},
                "queue_length": len(queue), "running": next((q["id"] for q in queue if q["state"] == "running"), None),
                "today": sum(row["today"] for row in usage), "average_seconds": round(self.store.average_seconds())}

    def queue(self) -> list[dict[str, Any]]:
        queue = self.store.queue()
        average = self.store.average_seconds()
        return [self.store.view(row, queue=queue, average=average, owner=True) for row in queue]


def install_image_studio(app: FastAPI, *, home: Path, sites: Any, local_control: Callable[[Request], None],
                         start: bool = True, generate: Callable[..., Any] | None = None) -> ImageStudio:
    studio = ImageStudio(home, sites, generate=generate)
    app.state.image_studio = studio

    async def run(request: Request, fn: Callable[[], Any]) -> Any:
        local_control(request)
        try:
            return await asyncio.to_thread(fn)
        except JobError as exc:
            return JSONResponse({"code": exc.code}, status_code=exc.status)

    @app.get("/api/local/image-studio/status")
    async def image_studio_status(request: Request):
        return await run(request, studio.status)

    @app.get("/api/local/image-studio/queue")
    async def image_studio_queue(request: Request):
        return await run(request, lambda: {"jobs": studio.queue()})

    @app.get("/api/local/image-studio/usage")
    async def image_studio_usage(request: Request):
        return await run(request, lambda: {"rows": studio.store.usage(day_start=studio.day_start())})

    @app.post("/api/local/image-studio/jobs/{job_id}/cancel")
    async def image_studio_cancel(job_id: str, request: Request):
        def cancel():
            studio.store.cancel(None, job_id)
            return studio.store.view(studio.store.job(None, job_id), owner=True)
        return await run(request, cancel)

    @app.post("/api/local/image-studio/jobs/{job_id}/prioritize")
    async def image_studio_prioritize(job_id: str, request: Request):
        return await run(request, lambda: studio.store.prioritize(job_id))

    if start:
        studio.start()
        if hasattr(app, "router"):
            app.router.on_shutdown.append(studio.close)
    return studio
