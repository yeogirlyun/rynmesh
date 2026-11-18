"""Scene and copy presets, including jobs written by the old schema."""

import json
import sqlite3

import pytest

from test_image_studio import call, studio, submit
from ryn_image_studio import generator, templates
from ryn_image_studio.jobs import JobStore, compose_brief


@pytest.mark.parametrize("scene", list(templates.SCENES))
def test_scene_adds_composition_and_uses_default_size(studio, scene):
    store, _, site, _ = studio
    job = submit(site, purpose=None, scene=scene)[0]
    preset = templates.SCENES[scene]
    assert job["scene"] == scene
    assert (job["width"], job["height"]) == preset["size"]
    assert preset["composition"] in compose_brief(store.job(None, job["id"]))


@pytest.mark.parametrize(("purpose", "scene"), [
    ("main", "main"), ("poster", "poster"), ("story", "social"),
    ("banner", "banner"), ("custom", None),
])
def test_omitted_scene_follows_existing_purpose(studio, purpose, scene):
    store, _, site, _ = studio
    job = submit(site, purpose=purpose, width=640, height=480)[0]
    assert job["scene"] == scene
    brief = compose_brief(store.job(None, job["id"]))
    if scene:
        assert templates.SCENES[scene]["composition"] in brief
    else:
        assert "场景构图" not in brief


def test_compose_copy_and_style_and_keep_legacy_words(studio):
    store, _, site, _ = studio
    copy = {"title": "冬季新品", "subtitle": "暖手也暖心", "badge": "限时特惠"}
    job = submit(site, style="promo", copy=copy, words="旧标题")[0]
    assert job["style"] == "promo"
    assert job["copy"] == copy
    brief = compose_brief(store.job(None, job["id"]))
    assert templates.STYLES["promo"]["description"] in brief
    assert "主标题：「冬季新品」；副标题：「暖手也暖心」；角标：「限时特惠」。这些文字要一字不差，画面里不要出现其它文字。" in brief
    assert "旧标题" not in brief

    words = "旧" * 41
    legacy = submit(site, words=words, copy={"badge": "限时"})[0]
    assert legacy["words"] == words
    assert legacy["copy"] == {"title": words, "badge": "限时"}
    assert f"主标题：「{words}」" in compose_brief(store.job(None, legacy["id"]))


@pytest.mark.parametrize(("fields", "error"), [
    ({"copy": {"title": "字" * 41}}, "image_bad_copy"),
    ({"copy": {"subtitle": "字" * 61}}, "image_bad_copy"),
    ({"copy": {"badge": "字" * 21}}, "image_bad_copy"),
    ({"copy": {"badge": 1}}, "image_bad_copy"),
    ({"copy": {"caption": "未定义字段"}}, "image_bad_copy"),
    ({"scene": "missing"}, "image_bad_scene"),
    ({"style": "missing"}, "image_bad_style"),
])
def test_invalid_template_fields_rejected(studio, fields, error):
    _, _, site, _ = studio
    status, _, data = call(site, "POST", "/api/jobs", json.dumps({
        "purpose": "main", "brief": "画一个杯子", **fields,
    }).encode())
    assert status == 400
    assert json.loads(data)["code"] == error


def test_me_lists_scenes_with_size_and_styles(studio, monkeypatch):
    _, _, site, _ = studio
    monkeypatch.setattr(generator, "readiness", lambda: {"ready": True, "code": ""})
    status, _, data = call(site, "GET", "/api/me")
    assert status == 200
    me = json.loads(data)
    assert set(me["scenes"]) == set(templates.SCENES)
    assert me["scenes"]["detail"] == {
        "label": "详情页头图", "purpose": "banner", "width": 1820, "height": 1024,
        "sizes": templates.SCENES["detail"]["sizes"],
    }
    assert me["styles"] == {key: {"label": style["label"]} for key, style in templates.STYLES.items()}


def test_open_existing_database_migrates_in_place(tmp_path):
    database = tmp_path / "image-studio" / "studio.db"
    database.parent.mkdir()
    legacy_id = "a" * 24
    with sqlite3.connect(database) as old:
        old.executescript("""
            CREATE TABLE jobs (
              id TEXT PRIMARY KEY, caller TEXT NOT NULL, caller_name TEXT NOT NULL, caller_peer TEXT NOT NULL,
              batch TEXT NOT NULL, purpose TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
              brief TEXT NOT NULL, words TEXT NOT NULL, refs TEXT NOT NULL, source_job TEXT,
              state TEXT NOT NULL, failure TEXT, priority INTEGER NOT NULL DEFAULT 0, attempt INTEGER NOT NULL DEFAULT 0,
              created_at REAL NOT NULL, started_at REAL, finished_at REAL, seconds REAL, model TEXT
            );
        """)
        old.execute("""INSERT INTO jobs(id, caller, caller_name, caller_peer, batch, purpose, width, height,
                       brief, words, refs, state, created_at)
                       VALUES (?, 'caller', 'name', 'peer', 'batch', 'story', 1024, 1820,
                               '原需求', '旧标题', '[]', 'queued', 1)""", (legacy_id,))
    old.close()

    store = JobStore(tmp_path)
    try:
        existing = store.job("caller", legacy_id)
        assert store.view(existing)["scene"] == "social"
        assert store.view(existing)["copy"] == {"title": "旧标题"}
        assert "主标题：「旧标题」" in compose_brief(existing)
        new = store.submit("caller", "name", "peer", {
            "scene": "detail", "style": "fresh", "brief": "新需求", "copy": {"title": "新标题"},
        })[0]
    finally:
        store.close()

    reopened = JobStore(tmp_path)
    try:
        assert len(reopened.list("caller")) == 2
        new_view = reopened.view(reopened.job("caller", new["id"]))
        assert (new_view["scene"], new_view["style"], new_view["copy"]) == (
            "detail", "fresh", {"title": "新标题"},
        )
    finally:
        reopened.close()
