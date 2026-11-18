import io
import json

import pytest
from PIL import Image

from test_image_studio import call, dims, headers, studio, submit
from ryn_image_studio import generator
from ryn_image_studio.jobs import RETENTION_S, Worker, compose_brief


def derive(site, job, sizes, who=None):
    body = json.dumps({"sizes": sizes}).encode()
    return call(site, "POST", f"/api/jobs/{job['id']}/derive", body, who)


def test_derive_queues_one_batch_and_uses_original_first(studio):
    store, worker, site, fake = studio
    source = submit(site)[0]
    worker.run_one(store.claim())
    original = store.image_path(store.job(None, source["id"])).read_bytes()
    status, _, body = derive(site, source, [{"width": 1200, "height": 400}, {"purpose": "main"}])
    assert status == 200
    jobs = json.loads(body)["jobs"]
    assert [(job["width"], job["height"]) for job in jobs] == [(1200, 400), (1024, 1024)]
    assert len({job["batch"] for job in jobs}) == 1
    for job in jobs:
        assert job["derived_from"] == job["source_job"] == source["id"]
        assert job["reference_roles"] == ["source"]
        worker.run_one(store.claim())
        assert fake.calls[-1]["references"] == [original]
        assert "同样的商品、文案、配色和风格" in fake.calls[-1]["brief"]
        assert "不要增加或减少文字" in fake.calls[-1]["brief"]
        assert dims(store.image_path(store.job(None, job["id"])).read_bytes()) == (job["width"], job["height"])


def test_derive_rejects_foreign_jobs_and_invalid_batches(studio):
    store, worker, site, _ = studio
    source = submit(site)[0]
    requested = [{"purpose": "main"}]
    assert derive(site, source, requested)[0] == 409
    worker.run_one(store.claim())
    assert derive(site, source, requested, headers(member="someone-else"))[0] == 404
    for invalid in ([], requested * 7, [*requested, {"width": 200, "height": 600}], [{"purpose": ["main"]}]):
        assert derive(site, source, invalid)[0] == 400
    assert store.queue() == []


@pytest.mark.parametrize(("edit", "phrase"), [
    ("background", "只换背景，商品和文字保持不变"),
    ("text", "只改文字为新文案，其他内容保持不变"),
    ("angle", "换一个角度展示商品，文案和风格保持不变"),
])
def test_edit_combines_fixed_instruction_and_user_description(studio, edit, phrase):
    store, worker, site, _ = studio
    source = submit(site)[0]
    worker.run_one(store.claim())
    job = submit(site, edit=edit, source_job=source["id"], brief="附加要求")[0]
    assert job["edit"] == edit and job["derived_from"] is None
    brief = compose_brief(store.job(None, job["id"]))
    assert phrase in brief and "附加要求" in brief


@pytest.mark.parametrize(("extra", "code"), [
    ({"edit": "background"}, "image_bad_edit"),
    ({"edit": "unknown", "source_job": "a" * 24}, "image_bad_edit"),
    ({"transparent": "true"}, "image_bad_transparent"),
    ({"transparent": 1}, "image_bad_transparent"),
])
def test_invalid_edit_or_transparent_is_rejected(studio, extra, code):
    _, _, site, _ = studio
    body = {"purpose": "main", "brief": "x", **extra}
    status, _, data = call(site, "POST", "/api/jobs", json.dumps(body).encode())
    assert status == 400 and json.loads(data)["code"] == code


def test_transparent_job_preserves_alpha_and_uses_white_thumbnail(studio):
    store, _, site, _ = studio
    calls = []
    image = Image.new("RGBA", (256, 256), (200, 20, 30, 0))
    image.paste((50, 100, 150, 255), (64, 64, 192, 192))
    with io.BytesIO() as buffer:
        image.save(buffer, "PNG")
        data = buffer.getvalue()

    def generate(brief, *, references, cancelled, transparent=False):
        calls.append(transparent)
        return data, {"model": "fake"}

    worker = Worker(store, generate=generate)
    source = submit(site, purpose="custom", width=256, height=256, transparent=True)[0]
    worker.run_one(store.claim())
    assert calls == [True] and source["transparent"] is True
    row = store.job(None, source["id"])
    with Image.open(store.image_path(row)) as output:
        assert output.mode == "RGBA" and output.getpixel((0, 0))[3] == 0
        assert output.getpixel((128, 128))[3] == 255
    with Image.open(store.image_path(row, "jpg")) as thumb:
        assert thumb.mode == "RGB" and thumb.getpixel((0, 0)) == (255, 255, 255)
    assert "transparent_background to true" in generator.instructions(0, transparent=True)
    assert "transparent_background to false" in generator.instructions(0)
    status, _, body = derive(site, source, [{"purpose": "main"}])
    assert status == 200 and json.loads(body)["jobs"][0]["transparent"] is True


def test_prune_preserves_old_source_while_derived_job_waits(studio):
    store, worker, site, _ = studio
    source = submit(site)[0]
    worker.run_one(store.claim())
    store._exec("UPDATE jobs SET created_at=? WHERE id=?", (store.clock() - RETENTION_S - 1, source["id"]))
    assert derive(site, source, [{"purpose": "main"}])[0] == 200
    assert store.prune() == 0
    worker.run_one(store.claim())
    assert store.prune() == 1
