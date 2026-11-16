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
