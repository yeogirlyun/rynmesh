"""Release-facing package versions must stay aligned."""

from __future__ import annotations

import json
from pathlib import Path

import tomllib

import rynmesh

ROOT = Path(__file__).resolve().parents[1]


def test_python_web_and_desktop_versions_match():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    web = json.loads((ROOT / "webapp/package.json").read_text(encoding="utf-8"))
    tauri = json.loads((ROOT / "webapp/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    cargo = tomllib.loads((ROOT / "webapp/src-tauri/Cargo.toml").read_text(encoding="utf-8"))

    versions = {
        project["project"]["version"],
        rynmesh.__version__,
        web["version"],
        tauri["version"],
        cargo["package"]["version"],
    }
    assert versions == {"0.6.2"}


def test_intel_macos_uses_last_supported_cryptography_wheel_line():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]

    assert any(
        dependency.startswith("cryptography>=48.0.1,<49.0.0")
        and "platform_machine == 'x86_64'" in dependency
        for dependency in dependencies
    )
