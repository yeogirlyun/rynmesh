"""Static release-contract checks for the supported Linux desktop artifact.

The installed-package smoke still has to run on Ubuntu.  These tests make the
workflow/configuration wiring reviewable on every development platform, so a
renamed artifact or accidentally removed smoke step fails before release.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TAURI = REPO / "webapp" / "src-tauri"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_linux_bundle_uses_one_target_named_sidecar() -> None:
    config = json.loads(_text(TAURI / "tauri.conf.json"))
    assert config["bundle"]["externalBin"] == ["binaries/rynmesh-peer"]
    assert config["bundle"]["targets"] == "all"

    build = _text(TAURI / "scripts" / "build-sidecar.sh")
    assert 'RUSTC="$(command -v rustc' in build
    assert 'TRIPLE="$("$RUSTC" -Vv' in build
    assert 'rynmesh-peer-$TRIPLE' in build
    assert "production webapp is missing" in build


def test_pull_request_linux_job_builds_inspects_installs_and_smokes_deb() -> None:
    workflow = _text(REPO / ".github" / "workflows" / "ci.yml")
    required = (
        "linux-desktop-package:",
        "runs-on: ubuntu-24.04",
        "cargo test --manifest-path src-tauri/Cargo.toml --lib",
        "npm run tauri build -- --bundles deb",
        "verify-linux-deb.sh",
        "apt-get install -y \"$PWD/Ryn-linux-x86_64.deb\"",
        "dbus-run-session -- ./src-tauri/scripts/smoke-linux-desktop.sh",
        "Ryn-linux-x86_64.deb.sha256",
    )
    for value in required:
        assert value in workflow, f"Linux CI lost required gate: {value}"


def test_release_uploads_verified_deb_and_checksum_without_dropping_macos() -> None:
    workflow = _text(REPO / ".github" / "workflows" / "release.yml")
    required = (
        "linux-desktop:",
        "runs-on: ubuntu-24.04",
        "npm run tauri build -- --bundles deb",
        "verify-linux-deb.sh",
        "sha256sum \"$artifact\" > \"$artifact.sha256\"",
        "gh release upload \"$GITHUB_REF_NAME\" \"$artifact\" \"$artifact.sha256\"",
        "macos-desktop:",
        "runner: macos-15-intel",
        "runner: macos-15",
        "codesign --verify --deep --strict",
    )
    for value in required:
        assert value in workflow, f"release workflow lost required gate: {value}"


def test_installed_smoke_covers_health_ui_recovery_xdg_and_orphan_cleanup() -> None:
    smoke = _text(TAURI / "scripts" / "smoke-linux-desktop.sh")
    required = (
        "XDG_STATE_HOME=\"$SMOKE_ROOT/state\"",
        'curl -fsS "http://127.0.0.1:$PORT/health"',
        'curl -fsS "http://127.0.0.1:$PORT/"',
        'http://127.0.0.1:$PORT/digest',
        'kill -KILL "$OLD_NODE"',
        '"$SMOKE_ROOT/state/rynmesh/ryn-node.log"',
        'kill -TERM "$APP_PID"',
        "managed sidecar remained after desktop exit",
    )
    for value in required:
        assert value in smoke, f"installed smoke lost assertion: {value}"


def test_linux_verifiers_remain_executable_shell_entrypoints() -> None:
    for name in (
        "build-sidecar.sh",
        "verify-sidecar.sh",
        "verify-linux-deb.sh",
        "smoke-linux-desktop.sh",
    ):
        path = TAURI / "scripts" / name
        assert path.is_file()
        assert _text(path).startswith(("#!/bin/sh", "#!/usr/bin/env bash"))
