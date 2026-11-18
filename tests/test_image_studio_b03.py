"""Preset submissions and crop-safe briefs for extreme ratios."""

import pytest

from test_image_studio import dims, png, studio, submit
from ryn_image_studio import sizes, templates
from ryn_image_studio.jobs import compose_brief


@pytest.mark.parametrize(("scene", "width", "height"), [
    ("banner", 1920, 600), ("banner", 1200, 400), ("banner", 750, 390), ("banner", 1820, 1024),
    ("main", 1024, 1024), ("main", 800, 800), ("poster", 1024, 1365),
    ("social", 1080, 1080), ("social", 1080, 1350), ("social", 1080, 1920), ("detail", 750, 1000),
])
def test_scene_presets_can_be_submitted(studio, scene, width, height):
    _, _, site, _ = studio
    assert {"width": width, "height": height} in templates.SCENES[scene]["sizes"]
    job = submit(site, purpose="custom", scene=scene, width=width, height=height)[0]
    assert (job["scene"], job["width"], job["height"]) == (scene, width, height)


@pytest.mark.parametrize(("width", "height", "ratio", "safe_band"), [
    (1920, 600, "2:1", "画面中间高度 62.5% 的横条里，上下只放可以裁掉的背景"),
    (1200, 400, "2:1", "画面中间高度 66.67% 的横条里，上下只放可以裁掉的背景"),
    (600, 1920, "1:2", "画面中间宽度 62.5% 的竖条里，左右只放可以裁掉的背景"),
])
def test_extreme_ratios_include_the_center_crop_in_the_brief(studio, width, height, ratio, safe_band):
    store, _, site, _ = studio
    job = submit(site, purpose="custom", width=width, height=height)[0]
    brief = compose_brief(store.job(None, job["id"]))
    assert f"宽高比 {ratio}的图片。" in brief.splitlines()[0]
    assert f"成品会居中裁成 {width}×{height} 像素" in brief
    assert safe_band in brief


@pytest.mark.parametrize(("width", "height"), [(750, 390), (800, 400), (400, 800), (1024, 1024)])
def test_regular_ratios_need_no_safe_band(studio, width, height):
    store, _, site, _ = studio
    job = submit(site, purpose="custom", width=width, height=height)[0]
    assert "成品会居中裁成" not in compose_brief(store.job(None, job["id"]))


@pytest.mark.parametrize(("source", "target"), [((800, 400), (1920, 600)), ((400, 800), (600, 1920))])
def test_extreme_ratio_output_has_exact_pixels(source, target):
    assert dims(sizes.fit(png(*source), *target)) == target


def test_legacy_purposes_keep_their_defaults_and_custom_keeps_its_bounds():
    assert sizes.resolve("banner") == ("banner", 1820, 1024)
    assert sizes.resolve("story") == ("story", 1024, 1820)
    assert sizes.resolve("custom", 256, 2048) == ("custom", 256, 2048)
