"""S05-I14 tests for attachment limits, reference-only large paths, and directory handling."""
from __future__ import annotations

import base64
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from test_agent_runtime import Catalog, Model, Tools, assistant

from rynmesh.agent_runtime.input_attachments import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_IMAGES,
    MAX_TOTAL_BYTES,
    attach_to_message,
    parse_attachments,
)
from rynmesh.agent_runtime.routes import install_agent
from rynmesh.agent_runtime.store import AgentError
from rynmesh.background_workers import BackgroundWorkerRegistry


def _b64_upload(name: str, data: bytes, media_type: str = "application/octet-stream") -> dict:
    return {
        "name": name,
        "media_type": media_type,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }


def _png_upload(name: str) -> dict:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 120, 220)).save(buf, "PNG")
    return _b64_upload(name, buf.getvalue(), "image/png")


def test_t1_large_path_attachment_is_reference_only_and_not_copied(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    large = outside / "server-50mb.log"
    size_50mb = 50 * 1024 * 1024
    with large.open("wb") as handle:
        handle.seek(size_50mb - 1)
        handle.write(b"\n")
    assert large.stat().st_size == size_50mb

    # 50 MiB path attachment becomes reference_only, is not read into memory,
    # and does not count toward MAX_TOTAL_BYTES alongside two 8 MiB uploads.
    chunk_8mb = b"x" * (8 * 1024 * 1024)
    prepared = parse_attachments([
        {"name": large.name, "path": str(large)},
        _b64_upload("part1.bin", chunk_8mb),
        _b64_upload("part2.bin", chunk_8mb),
    ])
    assert prepared is not None and len(prepared) == 3
    ref_item = prepared[0]
    assert ref_item["reference_only"] is True
    assert "raw" not in ref_item
    assert ref_item["size"] == size_50mb
    assert ref_item["path"] == str(large.resolve())

    # Attach only the 50 MiB reference_only file to verify .ryn/attachments is untouched.
    store = SimpleNamespace(default_workspace=lambda: str(workspace))
    row_non_auto = {
        "id": "task_ref_non_auto",
        "permission_mode": "workspace",
        "local_execution": {"workspace_root": str(workspace)},
    }
    msg_non_auto = {"role": "user", "content": "请分析这份大日志"}
    attach_to_message(store, row_non_auto, msg_non_auto, [ref_item])

    attachments_dir = workspace / ".ryn" / "attachments" / "task_ref_non_auto"
    assert not attachments_dir.exists() or list(attachments_dir.iterdir()) == []
    saved = msg_non_auto["attachments"][0]
    assert saved["reference_only"] is True
    assert saved["path"] == str(large.resolve())
    assert saved["size"] == size_50mb
    assert f"大文件未复制，原路径: {large.resolve()}，请用文件/命令工具按需读取" in msg_non_auto["content"]
    assert "自动处理" in msg_non_auto["content"]
    assert "额外" in msg_non_auto["content"]

    # In automatic mode (or when path is inside workspace / extra_roots), no mode warning is added.
    row_auto = {
        "id": "task_ref_auto",
        "permission_mode": "automatic",
        "local_execution": {"workspace_root": str(workspace)},
    }
    msg_auto = {"role": "user", "content": "请分析这份大日志"}
    attach_to_message(store, row_auto, msg_auto, [ref_item])
    assert f"大文件未复制，原路径: {large.resolve()}，请用文件/命令工具按需读取" in msg_auto["content"]
    assert "非自动" not in msg_auto["content"]

    row_extra = {
        "id": "task_ref_extra",
        "permission_mode": "workspace",
        "local_execution": {
            "workspace_root": str(workspace),
            "extra_roots": [{"path": str(outside), "access": "read"}],
        },
    }
    msg_extra = {"role": "user", "content": "请分析这份大日志"}
    attach_to_message(store, row_extra, msg_extra, [ref_item])
    assert "非自动" not in msg_extra["content"]


def test_t2_parse_attachments_limit_details_and_settings_endpoint(tmp_path: Path):
    # 1. Exceeding count (9 files)
    nine_files = [_b64_upload(f"f{i}.txt", b"ok", "text/plain") for i in range(8)] + [
        _b64_upload("ninth.txt", b"ok", "text/plain")
    ]
    with pytest.raises(AgentError) as exc_count:
        parse_attachments(nine_files)
    assert str(exc_count.value) == "agent_attachment_limit"
    assert getattr(exc_count.value, "details", None) == {
        "limit": "count",
        "name": "ninth.txt",
        "max": MAX_FILES,
    }

    # 2. Exceeding images (5 images)
    five_images = [_png_upload(f"img{i}.png") for i in range(4)] + [_png_upload("fifth.png")]
    with pytest.raises(AgentError) as exc_images:
        parse_attachments(five_images)
    assert str(exc_images.value) == "agent_attachment_limit"
    assert getattr(exc_images.value, "details", None) == {
        "limit": "images",
        "name": "fifth.png",
        "max": MAX_IMAGES,
    }

    # 3. Exceeding single-file bytes on base64 upload
    oversized = _b64_upload("oversized.bin", b"a" * (MAX_FILE_BYTES + 1))
    with pytest.raises(AgentError) as exc_file:
        parse_attachments([oversized])
    assert str(exc_file.value) == "agent_attachment_limit"
    assert getattr(exc_file.value, "details", None) == {
        "limit": "file_bytes",
        "name": "oversized.bin",
        "max": MAX_FILE_BYTES,
    }

    # 4. Exceeding total bytes across multiple files
    chunk_8mb = b"b" * (8 * 1024 * 1024)
    with pytest.raises(AgentError) as exc_total:
        parse_attachments([
            _b64_upload("a.bin", chunk_8mb),
            _b64_upload("b.bin", chunk_8mb),
            _b64_upload("c.bin", chunk_8mb),
        ])
    assert str(exc_total.value) == "agent_attachment_limit"
    assert getattr(exc_total.value, "details", None) == {
        "limit": "total_bytes",
        "name": "c.bin",
        "max": MAX_TOTAL_BYTES,
    }

    # 5. Settings endpoint exposes attachment_limits and task route surfaces details
    app = FastAPI()
    install_agent(
        app,
        store=object(),
        home=tmp_path,
        workers=BackgroundWorkerRegistry(),
        local_control=lambda _req: True,
        messaging_key=X25519PrivateKey.generate(),
        integrations=(Model(assistant("ok")), Tools(), Catalog()),
    )
    with TestClient(app) as client:
        settings_resp = client.get("/api/local/agent/settings")
        assert settings_resp.status_code == 200
        assert settings_resp.json().get("attachment_limits") == {
            "count": MAX_FILES,
            "images": MAX_IMAGES,
            "file_bytes": MAX_FILE_BYTES,
            "total_bytes": MAX_TOTAL_BYTES,
        }
        client.put("/api/local/agent/settings", json={"enabled": True, "model_id": "local:test"})
        err_resp = client.post(
            "/api/local/agent/tasks",
            json={"message": "check", "attachments": nine_files},
        )
        assert err_resp.status_code == 409
        body = err_resp.json()
        assert body["detail"] == "agent_attachment_limit"
        assert body.get("details") == {
            "limit": "count",
            "name": "ninth.txt",
            "max": MAX_FILES,
        }


def test_t7_directory_path_raises_agent_attachment_directory(tmp_path: Path):
    sub_dir = tmp_path / "reports_folder"
    sub_dir.mkdir()
    with pytest.raises(AgentError) as exc_dir:
        parse_attachments([{"name": "reports_folder", "path": str(sub_dir)}])
    assert str(exc_dir.value) == "agent_attachment_directory"

    missing = tmp_path / "does_not_exist.txt"
    with pytest.raises(AgentError) as exc_missing:
        parse_attachments([{"name": "does_not_exist.txt", "path": str(missing)}])
    assert str(exc_missing.value) == "agent_attachment_unavailable"


def test_fork_task_with_reference_only_attachment_preserves_original_path(tmp_path: Path):
    import uuid

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    large = outside / "dataset-50mb.csv"
    inside_large = workspace / "inside-50mb.log"
    size_50mb = 50 * 1024 * 1024
    for target in (large, inside_large):
        with target.open("wb") as handle:
            handle.seek(size_50mb - 1)
            handle.write(b"\n")

    app = FastAPI()
    runtime = install_agent(
        app,
        store=object(),
        home=tmp_path / "home",
        workers=BackgroundWorkerRegistry(),
        local_control=lambda _req: True,
        messaging_key=X25519PrivateKey.generate(),
        integrations=(Model(assistant("已分析")), Tools(), Catalog()),
    )
    with TestClient(app) as client:
        assert client.put(
            "/api/local/agent/settings",
            json={"enabled": True, "model_id": "local:test", "workspace_root": str(workspace)},
        ).status_code == 200

        created = client.post(
            "/api/local/agent/tasks",
            json={
                "message": "分析大文件与小附件",
                "attachments": [
                    {"name": inside_large.name, "path": str(inside_large)},
                    {"name": large.name, "path": str(large)},
                    _b64_upload("small.txt", b"small content", "text/plain"),
                ],
            },
        )
        assert created.status_code == 200, created.text
        tid = created.json()["id"]
        runtime.run_once()

        boundaries_resp = client.get(f"/api/local/agent/tasks/{tid}/fork-boundaries")
        assert boundaries_resp.status_code == 200, boundaries_resp.text
        boundary = boundaries_resp.json()["boundaries"][-1]

        fork_resp = client.post(
            f"/api/local/agent/tasks/{tid}/fork",
            json={
                "client_request_id": str(uuid.uuid4()),
                "title": "Forked with large reference",
                "message_count": boundary["message_count"],
                "token": boundary["token"],
                "shared_workspace_confirmed": True,
            },
        )
        assert fork_resp.status_code == 200, fork_resp.text
        child = fork_resp.json()
        child_attachments = child["messages"][0]["attachments"]
        assert len(child_attachments) == 3

        assert child_attachments[0]["reference_only"] is True
        assert child_attachments[0]["path"] == str(inside_large.resolve())
        assert child_attachments[0]["size"] == size_50mb

        ref_att = child_attachments[1]
        assert ref_att["reference_only"] is True
        assert ref_att["path"] == str(large.resolve())
        assert ref_att["size"] == size_50mb

        copied_att = child_attachments[2]
        assert copied_att["name"] == "small.txt"
        assert Path(copied_att["path"]).parent == (workspace / ".ryn" / "attachments" / child["id"])
        assert Path(copied_att["path"]).read_bytes() == b"small content"


def _detailed_rgb_png(width: int, height: int, seed: int = 7) -> bytes:
    """Deterministic detailed image whose 2048px JPEG exceeds 128 KiB (and >384 KiB at q=90 when noisy)."""
    import random

    rng = random.Random(seed)
    # Build a small noisy tile and resize with BILINEAR/NEAREST to create rich high-frequency detail fast.
    tile_w, tile_h = 320, 240
    tile_bytes = rng.randbytes(tile_w * tile_h * 3)
    tile = Image.frombytes("RGB", (tile_w, tile_h), tile_bytes)
    img = tile.resize((width, height), Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_s05_i8_t1_2560x1440_screenshot_sent_at_2048_and_within_512kib(tmp_path: Path):
    from rynmesh.agent_runtime import image_capabilities, input_attachments
    from rynmesh.agent_runtime.input_attachments import provider_content

    assert input_attachments.MAX_IMAGE_BYTES == 512 * 1024

    raw_png = _detailed_rgb_png(2560, 1440, seed=11)
    prepared = parse_attachments([_b64_upload("screenshot.png", raw_png, "image/png")])
    assert prepared is not None and len(prepared) == 1
    item = prepared[0]
    header, b64 = item["image_url"].split(",", 1)
    assert header == "data:image/jpeg;base64"
    sent_bytes = base64.b64decode(b64)
    assert 96 * 1024 < len(sent_bytes) <= 512 * 1024
    with Image.open(io.BytesIO(sent_bytes)) as sent_img:
        assert sent_img.size == (2048, 1152)

    store = SimpleNamespace(default_workspace=lambda: str(tmp_path))
    msg = attach_to_message(store, {"id": "task_s05_i8_t1"}, {"role": "user", "content": "看图"}, prepared)
    att = msg["attachments"][0]
    assert (att["image_width"], att["image_height"]) == (2560, 1440)
    assert (att["sent_width"], att["sent_height"]) == (2048, 1152)

    # Step 6 verification: legacy shared route compresses >128 KiB image and reports it.
    fitted, report = image_capabilities.fit_images_for_shared_route(
        [{"role": "user", "content": provider_content(msg)}],
        "inline-v1",
    )
    assert report["resized"] == 1
    img_parts = [p for p in fitted[0]["content"] if p.get("type") == "image_url"]
    assert len(base64.b64decode(img_parts[0]["image_url"]["url"].split(",", 1)[1])) <= 128 * 1024


def test_s05_i8_t2_transparent_png_preserves_png_and_alpha():
    img = Image.new("RGBA", (320, 200), (0, 0, 0, 0))
    for x in range(80, 240):
        for y in range(40, 160):
            img.putpixel((x, y), (30, 140, 250, 128))
    buf = io.BytesIO()
    img.save(buf, "PNG")

    prepared = parse_attachments([_b64_upload("badge.png", buf.getvalue(), "image/png")])
    assert prepared is not None
    url = prepared[0]["image_url"]
    assert url.startswith("data:image/png;base64,")
    decoded = base64.b64decode(url.split(",", 1)[1])
    assert len(decoded) <= 512 * 1024
    with Image.open(io.BytesIO(decoded)) as out:
        assert out.format == "PNG"
        rgba = out.convert("RGBA")
        assert rgba.getpixel((0, 0))[3] == 0
        assert rgba.getpixel((120, 100))[3] == 128


def test_s05_i8_t3_manifest_line_includes_original_and_sent_dimensions_and_image_tag(tmp_path: Path):
    raw_png = _detailed_rgb_png(2560, 1440, seed=21)
    prepared = parse_attachments([
        _b64_upload("readme.txt", b"hello", "text/plain"),
        _b64_upload("dashboard.png", raw_png, "image/png"),
    ])
    store = SimpleNamespace(default_workspace=lambda: str(tmp_path))
    msg = attach_to_message(store, {"id": "task_s05_i8_t3"}, {"role": "user", "content": "请检查截图"}, prepared)
    saved_img = msg["attachments"][1]
    expected_fragment = (
        f'2. dashboard.png (image/png, {saved_img["size"]} bytes) [Image #1]，'
        f'原图 2560x1440，已缩到 2048x1152，原件 local path: {saved_img["path"]}，要看细节用 ryn_image_inspect'
    )
    assert expected_fragment in msg["content"]


def test_s05_i8_t4_provider_content_labels_match_manifest_order(tmp_path: Path):
    from rynmesh.agent_runtime.input_attachments import provider_content

    prepared = parse_attachments([
        _b64_upload("notes.txt", b"first doc", "text/plain"),
        _png_upload("first.png"),
        _b64_upload("table.csv", b"a,b\n1,2\n", "text/csv"),
        _png_upload("second.png"),
    ])
    store = SimpleNamespace(default_workspace=lambda: str(tmp_path))
    msg = attach_to_message(store, {"id": "task_s05_i8_t4"}, {"role": "user", "content": "对比两张图"}, prepared)
    assert "2. first.png" in msg["content"] and "[Image #1]" in msg["content"]
    assert "4. second.png" in msg["content"] and "[Image #2]" in msg["content"]

    parts = provider_content(msg)
    assert isinstance(parts, list) and len(parts) == 5
    assert parts[0] == {"type": "text", "text": msg["content"]}
    assert parts[1] == {"type": "text", "text": "[Image #1]"}
    assert parts[2] == {"type": "image_url", "image_url": {"url": msg["attachments"][1]["image_url"], "detail": "auto"}}
    assert parts[3] == {"type": "text", "text": "[Image #2]"}
    assert parts[4] == {"type": "image_url", "image_url": {"url": msg["attachments"][3]["image_url"], "detail": "auto"}}


def test_s05_i8_t5_four_large_images_stay_within_total_budget():
    import random

    # High-entropy 2048x2048 images so each unbudgeted q=90 JPEG would exceed 384 KiB.
    uploads = []
    for idx in range(4):
        rng = random.Random(100 + idx)
        tile = Image.frombytes("RGB", (640, 640), rng.randbytes(640 * 640 * 3))
        big = tile.resize((2400, 2000), Image.Resampling.NEAREST)
        buf = io.BytesIO()
        big.save(buf, "JPEG", quality=95)
        uploads.append(_b64_upload(f"photo_{idx}.jpg", buf.getvalue(), "image/jpeg"))

    prepared = parse_attachments(uploads)
    assert prepared is not None and len(prepared) == 4
    decoded_lengths = [len(base64.b64decode(item["image_url"].split(",", 1)[1])) for item in prepared]
    assert all(size <= 512 * 1024 for size in decoded_lengths)
    assert sum(decoded_lengths) <= int(1.5 * 1024 * 1024)


def test_s05_i8_t6_legacy_attachment_record_without_meta_still_renders_manifest(tmp_path: Path):
    from rynmesh.agent_runtime.input_attachments import provider_content

    store = SimpleNamespace(default_workspace=lambda: str(tmp_path))
    legacy_path = str(tmp_path / "legacy.png")
    legacy_msg = {
        "role": "user",
        "content": "编辑旧消息",
        "attachments": [
            {
                "id": "attachment_legacy_1",
                "name": "legacy.png",
                "media_type": "image/png",
                "size": 4096,
                "path": legacy_path,
                "sha256": "0" * 64,
                "image_url": "data:image/jpeg;base64," + base64.b64encode(b"jpeg").decode("ascii"),
            }
        ],
    }
    updated = attach_to_message(store, {"id": "task_s05_i8_t6"}, legacy_msg, None)
    assert f"1. legacy.png (image/png, 4096 bytes) [Image #1]" in updated["content"]
    assert f"local path: {legacy_path}" in updated["content"]
    assert "ryn_image_inspect" in updated["content"]
    parts = provider_content(updated)
    assert parts[1] == {"type": "text", "text": "[Image #1]"}


