"""Image studio backend: sizes, queue, failures, isolation, owner routes, and colleagues through two nodes."""

from __future__ import annotations

import io
import json
import threading
import time
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from rynmesh.image_studio import generator, install_image_studio, sizes
from rynmesh.image_studio.jobs import JobError, JobStore, Worker, compose_brief
from rynmesh.image_studio.site import StudioSite

RESOURCE = "https://provider.test/connect"


def png(w=941, h=1672, color=(30, 80, 160)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buffer, format="PNG")
    return buffer.getvalue()


def dims(raw: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(raw)) as img:
        return img.size


class FakeCodex:
    """Stands in for native Codex: records briefs/references; can block or fail on demand."""

    def __init__(self):
        self.calls = []
        self.fail: list[str] = []
        self.gate: threading.Event | None = None
        self.started = threading.Event()

    def __call__(self, brief, *, references=None, cancelled=None):
        self.calls.append({"brief": brief, "references": list(references or [])})
        self.started.set()
        if self.gate is not None:
            while not self.gate.wait(0.02):
                if cancelled and cancelled():
                    raise generator.GenerationError("cancelled")
        if self.fail:
            raise generator.GenerationError(self.fail.pop(0))
        return png(), {"model": "fake-image-model"}


def wait_for(predicate, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out")


def headers(member="m-1", name="小美", peer="peer-1"):
    return {"X-Ryn-Caller-Peer": peer, "X-Ryn-Caller-Member": quote(member), "X-Ryn-Caller-Name": quote(name),
            "X-Ryn-Caller-Resource": quote(RESOURCE, safe=""), "X-Ryn-Site-Token": "in-process"}


@pytest.fixture
def studio(tmp_path):
    store = JobStore(tmp_path)
    fake = FakeCodex()
    worker = Worker(store, generate=fake)
    site = StudioSite(store)
    yield store, worker, site, fake
    worker.stop()
    store.close()


def call(site, method, path, body=b"", who=None):
    status, hdrs, data = site(method, path, who if who is not None else headers(), body)
    return status, dict(hdrs), data


def submit(site, who=None, **body):
    body = {"purpose": "story", "brief": "冬季保温杯海报", **body}
    status, _, data = call(site, "POST", "/api/jobs", json.dumps(body).encode(), who)
    assert status == 200, data
    return json.loads(data)["jobs"]


def test_sizes_fit_exact_pixels_and_validate_uploads():
    assert sizes.resolve("story") == ("story", 1024, 1820)
    assert sizes.resolve("custom", 800, 600) == ("custom", 800, 600)
    for bad in (("custom", 100, 600), ("custom", "800", 600), ("nope", None, None)):
        with pytest.raises(sizes.SizeError):
            sizes.resolve(*bad)
    out = sizes.fit(png(941, 1672), 1024, 1820)
    assert dims(out) == (1024, 1820)
    assert dims(sizes.fit(png(941, 1672), 1820, 1024)) == (1820, 1024)  # wrong orientation still exact
    thumb = sizes.thumbnail(out)
    assert thumb[:3] == b"\xff\xd8\xff" and max(dims(thumb)) == sizes.THUMB_SIDE
    assert sizes.check_upload(png(64, 64), 1 << 20) == "image/png"
    for raw in (b"GIF89a....", b"<svg></svg>", png(64, 64)[:40], b""):
        with pytest.raises(sizes.SizeError):
            sizes.check_upload(raw, 1 << 20)
    with pytest.raises(sizes.SizeError):
        sizes.check_upload(png(64, 64), 10)
    assert "竖版" in sizes.ratio_phrase(1024, 1820) and "9:16" in sizes.ratio_phrase(1024, 1820)


def test_queue_runs_in_order_with_positions_exact_size_and_thumbnails(studio):
    store, worker, site, fake = studio
    fake.gate = threading.Event()
    first = submit(site, count=2, words="冬季新品")
    second = submit(site, who=headers("m-2", "阿九", "peer-2"), purpose="main")
    assert [j["position"] for j in first] == [1, 2] and second[0]["position"] == 3
    worker.start()
    wait_for(fake.started.is_set)
    status, _, data = call(site, "GET", "/api/jobs?state=active")
    jobs = {j["id"]: j for j in json.loads(data)["jobs"]}
    assert jobs[first[0]["id"]]["state"] == "running" and jobs[first[1]["id"]]["position"] == 1
    fake.gate.set()
    wait_for(lambda: store.job(None, second[0]["id"])["state"] == "succeeded")
    assert [c["brief"].splitlines()[0] for c in fake.calls][:2] == [compose_brief(store.job(None, first[0]["id"])).splitlines()[0]] * 2
    assert "「冬季新品」" in fake.calls[0]["brief"] and "9:16" in fake.calls[0]["brief"]
    status, hdrs, data = call(site, "GET", f"/api/jobs/{first[0]['id']}/image")
    assert status == 200 and dims(data) == (1024, 1820) and hdrs["content-disposition"].startswith("attachment")
    status, hdrs, data = call(site, "GET", f"/api/jobs/{first[0]['id']}/thumb")
    assert status == 200 and hdrs["content-type"] == "image/jpeg" and len(data) < 100_000
    other = call(site, "GET", f"/api/jobs/{second[0]['id']}/image", who=headers("m-2", "阿九", "peer-2"))
    assert other[0] == 200 and dims(other[2]) == (1024, 1024)


def test_failures_are_classified_and_retry_requeues(studio):
    store, worker, site, fake = studio
    fake.fail = ["quota_exhausted"]
    job = submit(site)[0]
    worker.start()
    row = wait_for(lambda: (r := store.job(None, job["id"]))["state"] == "failed" and r)
    status, _, data = call(site, "GET", f"/api/jobs/{job['id']}")
    view = json.loads(data)
    assert view["failure"] == "quota_exhausted" and "额度" in view["failure_text"]
    assert call(site, "GET", f"/api/jobs/{job['id']}/image")[0] == 404
    assert call(site, "POST", f"/api/jobs/{job['id']}/retry")[0] == 200
    wait_for(lambda: store.job(None, job["id"])["state"] == "succeeded")
    assert call(site, "POST", f"/api/jobs/{job['id']}/retry")[0] == 409
    assert generator._classify('{"error": "usage_limit_reached"}') == "quota_exhausted"
    assert generator._classify("blocked by safety system") == "content_rejected"
    assert generator._classify("something odd") == "generation_failed"


def test_cancel_queued_and_running(studio):
    store, worker, site, fake = studio
    fake.gate = threading.Event()
    running, queued = submit(site, count=2)
    worker.start()
    wait_for(fake.started.is_set)
    assert json.loads(call(site, "POST", f"/api/jobs/{queued['id']}/cancel")[2])["state"] == "cancelled"
    call(site, "POST", f"/api/jobs/{running['id']}/cancel")
    wait_for(lambda: store.job(None, running["id"])["state"] == "cancelled")
    assert len(fake.calls) == 1  # the cancelled queued job never ran


def test_colleagues_are_isolated(studio):
    store, worker, site, fake = studio
    a = headers("m-1", "小美", "peer-1")
    b = headers("m-2", "阿九", "peer-2")
    status, _, data = call(site, "POST", "/api/uploads", png(64, 64), a)
    upload = json.loads(data)["upload_id"]
    job = submit(site, a, references=[upload])[0]
    for path, method in ((f"/api/jobs/{job['id']}", "GET"), (f"/api/jobs/{job['id']}/cancel", "POST"),
                         (f"/api/jobs/{job['id']}/thumb", "GET")):
        assert call(site, method, path, who=b)[0] == 404
    assert json.loads(call(site, "GET", "/api/jobs", who=b)[2])["jobs"] == []
    status, _, data = call(site, "POST", "/api/jobs", json.dumps({"purpose": "main", "brief": "x", "references": [upload]}).encode(), b)
    assert status == 404 and json.loads(data)["code"] == "image_reference_missing"
    # The same person on a second device shares history.
    assert json.loads(call(site, "GET", "/api/jobs", who=headers("m-1", "小美", "peer-9"))[2])["jobs"][0]["id"] == job["id"]
    assert call(site, "GET", "/api/jobs", who={})[0] == 403


def test_references_and_edit_from_previous_result(studio):
    store, worker, site, fake = studio
    worker.start()
    status, _, data = call(site, "POST", "/api/uploads", png(80, 80, (200, 10, 10)))
    upload = json.loads(data)["upload_id"]
    first = submit(site, references=[upload])[0]
    wait_for(lambda: store.job(None, first["id"])["state"] == "succeeded")
    assert len(fake.calls[0]["references"]) == 1 and "参考附带的图片" in fake.calls[0]["brief"]
    edit = submit(site, brief="背景换成浅红色", source_job=first["id"])[0]
    wait_for(lambda: store.job(None, edit["id"])["state"] == "succeeded")
    assert dims(fake.calls[1]["references"][0]) == (1024, 1820)  # the earlier result is the reference
    for bad in ({"purpose": "main", "brief": ""}, {"purpose": "main", "brief": "x", "count": 3},
                {"purpose": "main", "brief": "x", "references": ["a", "b", "c", "d"]},
                {"purpose": "main", "brief": "x", "extra": 1}, {"purpose": "main", "brief": "x" * 3000}):
        assert call(site, "POST", "/api/jobs", json.dumps(bad).encode())[0] == 400
    assert call(site, "POST", "/api/uploads", b"not an image")[0] == 400


def test_restart_recovery_limits_and_prune(tmp_path):
    clock = [1_000_000.0]
    store = JobStore(tmp_path, clock=lambda: clock[0])
    store.submit("c", "n", "p", {"purpose": "main", "brief": "x", "count": 2})
    a = store.claim()
    store._exec("UPDATE jobs SET attempt=3 WHERE id=?", (a["id"],))
    b = store.claim()
    store.recover()
    assert store.job(None, a["id"])["failure"] == "interrupted"
    assert store.job(None, b["id"])["state"] == "queued"
    store.submit("c", "n", "p", {"purpose": "main", "brief": "x", "count": 4})
    for _ in range(3):
        store.submit("c", "n", "p", {"purpose": "main", "brief": "x", "count": 4})
    with pytest.raises(JobError, match="image_too_many_active"):
        store.submit("c", "n", "p", {"purpose": "main", "brief": "x", "count": 4})
    store.finish(b["id"], png=png())
    clock[0] += 91 * 24 * 3600
    assert store.prune() >= 2
    with pytest.raises(JobError):
        store.job(None, b["id"])
    assert not store.image_path({"caller": "c", "id": b["id"]}).exists()
    store.close()


def test_static_shell_and_traversal(studio, tmp_path, monkeypatch):
    store, worker, site, fake = studio
    from rynmesh.image_studio import site as site_mod
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><title>做图</title>", encoding="utf-8")
    (web / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr(site_mod, "WEB_ROOT", web)
    status, hdrs, data = call(site, "GET", "/")
    assert status == 200 and hdrs["cache-control"] == "no-store" and "做图" in data.decode()
    assert call(site, "GET", "/assets/app.js")[1]["content-type"].startswith("application/javascript") or \
        call(site, "GET", "/assets/app.js")[1]["content-type"].startswith("text/javascript")
    assert call(site, "GET", "/history")[0] == 200  # client route -> shell
    for path in ("/../secret.txt", "/assets/../../x.js", "/assets/missing.js"):
        assert call(site, "GET", path)[0] == 404


def test_owner_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(generator, "readiness", lambda: {"ready": True, "code": ""})

    class Sites:
        def __init__(self):
            from rynmesh.node_sites.store import SiteStore
            self.store = SiteStore(tmp_path)
            self.host = type("H", (), {"builtins": {}})()

    sites = Sites()
    fake = FakeCodex()
    fake.gate = threading.Event()
    app = FastAPI()
    studio = install_image_studio(app, home=tmp_path, sites=sites, local_control=lambda request: None, generate=fake)
    try:
        assert sites.store.site("image-studio")["enabled"] is False and sites.store.site("image-studio")["builtin"]
        assert "image-studio" in sites.host.builtins
        client = TestClient(app)
        a, b, c = submit(studio.site, count=1)[0], submit(studio.site, count=1)[0], submit(studio.site, count=1)[0]
        wait_for(fake.started.is_set)
        status = client.get("/api/local/image-studio/status").json()
        assert status["queue_length"] == 3 and status["running"] == a["id"] and status["model"]["ready"]
        assert client.post(f"/api/local/image-studio/jobs/{c['id']}/prioritize").json()["priority"] == 1
        queue = client.get("/api/local/image-studio/queue").json()["jobs"]
        assert [j["id"] for j in queue] == [a["id"], c["id"], b["id"]] and queue[0]["caller_name"] == "小美"
        assert client.post(f"/api/local/image-studio/jobs/{b['id']}/cancel").json()["state"] == "cancelled"
        assert client.post("/api/local/image-studio/jobs/" + "0" * 24 + "/cancel").status_code == 404
        fake.gate.set()
        wait_for(lambda: studio.store.job(None, c["id"])["state"] == "succeeded")
        usage = client.get("/api/local/image-studio/usage").json()["rows"]
        assert usage[0]["name"] == "小美" and usage[0]["today"] == 2
    finally:
        studio.close()
        studio.store.close()


def test_colleague_through_two_nodes(tmp_path, monkeypatch):
    """Opener -> gateway -> signed sealed request -> hosting node -> in-process studio -> back."""
    from test_node_sites import RESOURCE as SITE_RESOURCE, Authority, SiteNode

    monkeypatch.setattr(generator, "readiness", lambda: {"ready": True, "code": ""})
    opener, hosting = SiteNode(tmp_path, "opener").start(), SiteNode(tmp_path, "hosting").start()
    fake = FakeCodex()
    studio = install_image_studio(hosting.app, home=hosting.home, sites=hosting.sites,
                                  local_control=lambda request: None, generate=fake)
    try:
        authority = Authority([opener, hosting])
        for node in (opener, hosting):
            node.e._provider_peers = authority.rows
            node.e._provider_authorize = (lambda r, s, t, c, sid=None, node=node:
                                          authority.authorize(node.identity.peer_id, r, s, t, c, sid))
        hosting.sites.store.update("image-studio", {"enabled": True})
        opened = opener.sites.client.open(SITE_RESOURCE, hosting.identity.peer_id, "image-studio")
        assert opened["title"] == "做图"
        key, port = opened["key"], opener.sites.gateway.port
        import http.client

        def req(method, path, body=None, ctype=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
            try:
                conn.request(method, path, body=body, headers={"Host": f"{key}.site.localhost:{port}",
                                                               **({"Content-Type": ctype} if ctype else {})})
                r = conn.getresponse()
                return r.status, r.read()
            finally:
                conn.close()

        status, data = req("GET", "/api/me")
        assert status == 200 and json.loads(data)["name"] == "成员 0"
        status, data = req("POST", "/api/uploads", png(120, 120), "image/png")
        upload = json.loads(data)["upload_id"]
        status, data = req("POST", "/api/jobs", json.dumps({"purpose": "poster", "brief": "圣诞袜海报",
                                                            "references": [upload]}).encode(), "application/json")
        job = json.loads(data)["jobs"][0]
        wait_for(lambda: json.loads(req("GET", f"/api/jobs/{job['id']}")[1])["state"] == "succeeded")
        status, data = req("GET", f"/api/jobs/{job['id']}/image")
        assert status == 200 and dims(data) == (1024, 1365)
        rows = studio.store.usage(day_start=0)
        assert rows[0]["name"] == "成员 0" and rows[0]["caller"].startswith(SITE_RESOURCE)
    finally:
        studio.close()
        studio.store.close()
        opener.stop()
        hosting.stop()
