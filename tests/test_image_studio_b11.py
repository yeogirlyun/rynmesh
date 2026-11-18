"""Owner-only Codex failure details and B11 banner presets."""

import json
import sqlite3
from collections import deque
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from test_image_studio import call, dims, png, studio, submit
from ryn_image_studio import generator, install_image_studio, sizes, templates
from ryn_image_studio.jobs import JobStore, compose_brief
from rynmesh.llm_package import codex_session
from rynmesh.node_sites.store import SiteStore


def test_failure_detail_is_persisted_and_visible_only_to_the_local_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(generator, "readiness", lambda: {"ready": True, "code": ""})
    original = "Original generation failure. "
    image_data = "data:image/png;base64," + "A" * 200
    raw = original + image_data + " " + "z " * 250

    def fail(*args, **kwargs):
        raise generator.GenerationError("generation_failed", raw)

    def authorize(request):
        if request.headers.get("X-Owner-Test") != "local-control":
            raise HTTPException(status_code=403)

    app = FastAPI()
    sites = SimpleNamespace(store=SiteStore(tmp_path), host=SimpleNamespace(builtins={}))
    studio = install_image_studio(app, home=tmp_path, sites=sites, local_control=authorize,
                                  start=False, generate=fail)
    try:
        job = submit(studio.site)[0]
        studio.worker.run_one(studio.store.claim())
        job_id = job["id"]
        path = f"/api/jobs/{job_id}"
        colleague = json.loads(call(studio.site, "GET", path)[2])
        assert colleague["failure"] == "generation_failed"
        assert "failure_detail" not in colleague
        assert "failure_detail" not in json.loads(call(studio.site, "GET", "/api/jobs")[2])["jobs"][0]

        client = TestClient(app)
        local_path = f"/api/local/image-studio/jobs/{job_id}"
        assert client.get(local_path).status_code == 403
        result = client.get(local_path, headers={"X-Owner-Test": "local-control"})
        assert result.status_code == 200
        detail = result.json()["failure_detail"]
        assert detail.startswith(original + "[image data omitted]")
        assert "base64" not in detail and "A" * 50 not in detail and len(detail) == 300

        packaged = studio.owner("GET", f"/jobs/{job_id}", b"")
        assert packaged[0] == 200 and json.loads(packaged[2])["failure_detail"] == detail
        with sqlite3.connect(tmp_path / "image-studio" / "studio.db") as db:
            assert db.execute("SELECT failure_detail FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == detail
        call(studio.site, "POST", f"{path}/retry")
        assert studio.store.job(None, job_id)["failure_detail"] is None
    finally:
        studio.close()


def test_open_existing_jobs_adds_failure_detail_column(tmp_path):
    store = JobStore(tmp_path)
    store.close()
    with sqlite3.connect(tmp_path / "image-studio" / "studio.db") as db:
        db.execute("ALTER TABLE jobs DROP COLUMN failure_detail")
    store = JobStore(tmp_path)
    try:
        assert "failure_detail" in {row["name"] for row in store._rows("PRAGMA table_info(jobs)")}
    finally:
        store.close()


def test_codex_failure_preserves_its_message_without_the_image_result(monkeypatch):
    monkeypatch.setattr(generator, "readiness", lambda: {"ready": True, "code": ""})
    monkeypatch.setattr(codex_session, "_start_thread", lambda *args, **kwargs: "thread")
    item = {"type": "imageGeneration", "status": "failed",
            "error": {"message": "Provider could not render this image", "image": "private image data"},
            "result": "a private image result"}
    event = {"method": "item/completed", "params": {"threadId": "thread", "turnId": "turn", "item": item}}

    class RPC:
        def __init__(self, *args, **kwargs):
            self.pending = deque([event])

        def initialize(self):
            pass

        def call(self, method, params):
            if method == "model/list":
                return {"data": [{"id": "stub", "inputModalities": ["image"]}]}
            return {"turn": {"id": "turn"}}

        def close(self):
            pass

    monkeypatch.setattr(codex_session, "CodexRPC", RPC)
    with pytest.raises(generator.GenerationError) as error:
        generator.generate("Test banner")
    assert "Provider could not render this image" in error.value.detail
    assert "private image" not in error.value.detail


@pytest.mark.parametrize(("width", "height", "band"), [
    (1920, 600, "16:5 的横条"),
    (600, 1920, "5:16 的竖条"),
])
def test_extreme_brief_requests_a_centered_band_and_exact_output(studio, width, height, band):
    store, _, site, _ = studio
    job = submit(site, purpose="custom", width=width, height=height)[0]
    brief = compose_brief(store.job(None, job["id"]))
    assert band in brief
    assert "纯背景色的空白，不放任何东西" in brief
    assert "80% 以内" in brief
    assert dims(sizes.fit(png(800, 400), width, height)) == (width, height)


def test_me_lists_all_scene_sizes(studio, monkeypatch):
    _, _, site, _ = studio
    monkeypatch.setattr(generator, "readiness", lambda: {"ready": True, "code": ""})
    status, _, body = call(site, "GET", "/api/me")
    assert status == 200
    scenes = json.loads(body)["scenes"]
    assert {key: scene["sizes"] for key, scene in scenes.items()} == {
        key: scene["sizes"] for key, scene in templates.SCENES.items()
    }
    assert {"width": 1920, "height": 600} in scenes["banner"]["sizes"]
