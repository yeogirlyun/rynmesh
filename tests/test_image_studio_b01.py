"""Role-aware uploads and the attachment order sent to the image generator."""

import json

import pytest

from test_image_studio import call, headers, png, studio, submit
from ryn_image_studio.jobs import UPLOAD_TTL_S


def upload(site, color, who=None):
    status, _, data = call(site, "POST", "/api/uploads", png(64, 64, color), who)
    assert status == 200
    return json.loads(data)["upload_id"]


def test_mixed_reference_roles_follow_attachment_order(studio):
    store, worker, site, fake = studio
    style = upload(site, (100, 15, 15))
    material = upload(site, (20, 100, 15))
    source = submit(site)[0]
    worker.run_one(store.claim())

    edit = submit(site, source_job=source["id"],
                  references=[{"id": style, "role": "style"}, material],
                  words="冬季新品")[0]
    assert edit["references"] == 3
    assert edit["reference_roles"] == ["source", "style", "material"]
    assert json.loads(store.job(None, edit["id"])["refs"]) == [
        {"id": style, "role": "style"},
        {"id": material, "role": "material"},
    ]

    worker.run_one(store.claim())
    generation = fake.calls[-1]
    source_row = store.job(None, source["id"])
    assert generation["references"] == [
        store.image_path(source_row).read_bytes(),
        store.upload_bytes(source_row["caller"], style),
        store.upload_bytes(source_row["caller"], material),
    ]
    assert "「冬季新品」" in generation["brief"]
    assert generation["brief"].splitlines()[-3:] == [
        "第 1 张图是上一版成品，在它的基础上修改，没提到的部分保持不变。",
        "第 2 张图是风格参考：只参考配色、构图和氛围；里面的物体、文字、logo 不要出现在成品里。",
        "第 3 张图是素材：画面里要出现它，外形、颜色、logo、上面的文字和结构保持一致，不要重新设计；可以调整角度、大小、位置和光影。",
    ]


def test_legacy_and_current_refs_survive_prune_and_generate(studio):
    store, worker, site, fake = studio
    legacy_id = upload(site, (100, 15, 15))
    style_id = upload(site, (20, 100, 15))
    unused_id = upload(site, (15, 20, 100))
    legacy = submit(site, references=[legacy_id])[0]
    submit(site, references=[{"id": style_id, "role": "style"}])
    store._exec("UPDATE jobs SET refs=? WHERE id=?", (json.dumps([legacy_id]), legacy["id"]))
    store._exec("UPDATE uploads SET created_at=?", (store.clock() - UPLOAD_TTL_S - 1,))
    store.prune()

    assert {row["id"] for row in store._rows("SELECT id FROM uploads")} == {legacy_id, style_id}
    row = store.job(None, legacy["id"])
    assert store.view(row)["reference_roles"] == ["material"]
    worker.run_one(store.claim())
    assert "第 1 张图是素材" in fake.calls[-1]["brief"]
    assert fake.calls[-1]["references"] == [store.upload_bytes(row["caller"], legacy_id)]
    assert not store.upload_path(row["caller"], unused_id).exists()


def test_five_attachment_limit_includes_source(studio):
    store, worker, site, _ = studio
    source = submit(site)[0]
    worker.run_one(store.claim())
    refs = [upload(site, (10 * i, 15, 15)) for i in range(6)]
    assert submit(site, references=refs[:5])[0]["references"] == 5
    assert submit(site, references=refs[:4], source_job=source["id"])[0]["references"] == 5

    for args in ({"references": refs}, {"references": refs[:5], "source_job": source["id"]}):
        status, _, data = call(site, "POST", "/api/jobs",
                               json.dumps({"purpose": "main", "brief": "x", **args}).encode())
        assert status == 400
        assert json.loads(data)["code"] == "image_bad_references"


@pytest.mark.parametrize("role", ["material", "style"])
def test_reference_objects_cannot_use_someone_elses_upload(studio, role):
    _, _, site, _ = studio
    other = upload(site, (20, 100, 15), headers(member="other"))
    status, _, data = call(site, "POST", "/api/jobs", json.dumps({
        "purpose": "main", "brief": "x", "references": [{"id": other, "role": role}],
    }).encode())
    assert status == 404
    assert json.loads(data)["code"] == "image_reference_missing"


def test_invalid_reference_role_is_rejected(studio):
    _, _, site, _ = studio
    ref = upload(site, (20, 100, 15))
    status, _, data = call(site, "POST", "/api/jobs", json.dumps({
        "purpose": "main", "brief": "x", "references": [{"id": ref, "role": "source"}],
    }).encode())
    assert status == 400
    assert json.loads(data)["code"] == "image_bad_references"
