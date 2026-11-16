"""Tests for S02-I26: streaming resumable desktop update download, 60s per-chunk idle timeout,
64 KiB buffer + signature check cleanup, .verified marker, and SHA-256 precheck before install."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CARGO_TOML = ROOT / "webapp" / "src-tauri" / "Cargo.toml"
CARGO_LOCK = ROOT / "webapp" / "src-tauri" / "Cargo.lock"
UPDATES_RS = ROOT / "webapp" / "src-tauri" / "src" / "updates.rs"


def test_t1_verified_marker_skips_check_read_and_ignores_part():
    cargo_toml = CARGO_TOML.read_text(encoding="utf-8")
    updates_src = UPDATES_RS.read_text(encoding="utf-8")

    assert "sha2" in cargo_toml
    assert "VerifiedMarker" in updates_src
    assert "check_cached_package" in updates_src
    assert '.strip_suffix(".update.verified")' in updates_src
    assert "verified_marker_skips_package_read_when_matching_and_discards_on_mismatch_or_part_only" in updates_src


def test_t2_install_streams_sha256_before_memory_read_and_reports_corrupt():
    updates_src = UPDATES_RS.read_text(encoding="utf-8")

    assert "sha256_hex_streaming" in updates_src
    assert "load_verified_package_bytes" in updates_src
    assert '"update_cache_corrupt"' in updates_src
    assert "install_precheck_rejects_sha256_mismatch_without_loading_package_into_memory" in updates_src


def test_t3_t4_streaming_range_resume_and_200_truncate_wired():
    cargo_toml = CARGO_TOML.read_text(encoding="utf-8")
    cargo_lock = CARGO_LOCK.read_text(encoding="utf-8")
    updates_src = UPDATES_RS.read_text(encoding="utf-8")

    assert "reqwest" in cargo_toml
    assert 'name = "aws-lc-rs"' not in cargo_lock
    assert "attempt.download(" not in updates_src
    assert "update.download(" not in updates_src
    assert "bytes=" in updates_src
    assert "StatusCode::PARTIAL_CONTENT" in updates_src
    assert "interrupted_download_resumes_with_range_header_for_remaining_bytes_only" in updates_src
    assert "server_ignoring_range_returns_200_and_truncates_part_from_scratch" in updates_src


def test_t5_t6_sixty_second_chunk_idle_timeout_and_bad_signature_part_cleanup():
    updates_src = UPDATES_RS.read_text(encoding="utf-8")

    assert "DOWNLOAD_TIMEOUT" not in updates_src
    assert "update.timeout" not in updates_src
    assert "CHUNK_IDLE_TIMEOUT: Duration = Duration::from_secs(60)" in updates_src
    assert "STREAM_BUF_BYTES: usize = 64 * 1024" in updates_src
    assert "download_aborts_when_no_bytes_arrive_within_idle_timeout" in updates_src
    assert "invalid_signature_removes_part_file_and_does_not_create_cache_or_marker" in updates_src
