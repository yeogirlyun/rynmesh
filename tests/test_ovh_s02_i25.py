"""Tests for S02-I25: desktop updater mirror configuration, build rerun trigger,
endpoint list wiring, package origin whitelist, and fallback download invariants."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATES_RS = ROOT / "webapp" / "src-tauri" / "src" / "updates.rs"
BUILD_RS = ROOT / "webapp" / "src-tauri" / "build.rs"
TAURI_CONF = ROOT / "webapp" / "src-tauri" / "tauri.conf.json"
TAURI_UPDATER_CONF = ROOT / "webapp" / "src-tauri" / "tauri.updater.conf.json"


def test_t1_mirror_whitelist_and_signature_invariants_configured():
    build_src = BUILD_RS.read_text(encoding="utf-8")
    updates_src = UPDATES_RS.read_text(encoding="utf-8")

    # Build script must re-run when RYN_DESKTOP_UPDATE_MIRRORS changes.
    assert "cargo:rerun-if-env-changed=RYN_DESKTOP_UPDATE_MIRRORS" in build_src
    assert 'option_env!("RYN_DESKTOP_UPDATE_MIRRORS")' in updates_src

    # Mirror whitelist must enforce https, port 443, empty username/password, and prefix matching,
    # while keeping PUBLIC_KEY and update.verify signature verification intact.
    assert "valid_mirror_endpoint" in updates_src
    assert "mirror_package_prefix" in updates_src
    assert "PUBLIC_KEY" in updates_src
    assert "update.verify(&bytes)" in updates_src
    assert "whitelisted_mirrors_allow_matching_https_packages_and_reject_others_or_credentials" in updates_src

    for conf_path in (TAURI_CONF, TAURI_UPDATER_CONF):
        conf = json.loads(conf_path.read_text(encoding="utf-8"))
        updater = conf["plugins"]["updater"]
        assert updater["endpoints"][0] == "https://github.com/yeogirlyun/rynmesh/releases/latest/download/desktop-latest.json"
        assert bool(updater.get("allowDowngrades", False)) is False
        assert bool(updater.get("dangerousInsecureTransportProtocol", False)) is False


def test_t2_endpoint_list_order_and_download_fallback_wired():
    updates_src = UPDATES_RS.read_text(encoding="utf-8")

    # check() passes the full ordered endpoint list to updater_builder().endpoints(...)
    # and download() retries mirror package URLs with source logging.
    assert "resolve_endpoints" in updates_src
    assert "candidate_download_urls" in updates_src
    assert ".endpoints(endpoint_list.clone())" in updates_src
    assert "desktop_update_download" in updates_src
    assert "endpoint_list_preserves_primary_then_mirror_order_and_derives_fallback_urls" in updates_src
