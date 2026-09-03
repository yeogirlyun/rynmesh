"""Tests for cross-platform hardware detection, focused on macOS memory."""

from __future__ import annotations

import subprocess

import rynmesh.llm_package.hardware as llm_hardware

VM_STAT_SAMPLE_16K_PAGES = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                              10000.
Pages active:                            50000.
Pages inactive:                           5000.
Pages speculative:                         500.
Pages throttled:                             0.
Pages wired down:                        20000.
Pages purgeable:                           100.
"Translation faults":                 12345678.
Pages copy-on-write:                    123456.
Pages zero filled:                     1234567.
Pages reactivated:                        1000.
Pages purged:                              500.
File-backed pages:                        3000.
Anonymous pages:                          4000.
Pages stored in compressor:               2000.
Pages occupied by compressor:             1000.
Decompressions:                            300.
Compressions:                              600.
Pageins:                                 70000.
Pageouts:                                    0.
Swapins:                                     0.
Swapouts:                                    0.
"""


def test_parse_vm_stat_uses_reported_page_size():
    # (10000 + 5000 + 500) pages * 16384 bytes / 2**20 MiB-per-byte-divisor
    expected_mb = (10000 + 5000 + 500) * 16384 // 2**20
    assert llm_hardware._parse_vm_stat(VM_STAT_SAMPLE_16K_PAGES) == expected_mb


def test_parse_vm_stat_falls_back_to_default_page_size_when_header_missing():
    text = "Pages free:                              256.\nPages inactive:                            0.\n"
    assert llm_hardware._parse_vm_stat(text, page_size_default=4096) == (256 * 4096) // 2**20


def test_darwin_memory_branch_reads_sysctl_and_vm_stat(monkeypatch):
    monkeypatch.setattr(llm_hardware.platform, "system", lambda: "Darwin")

    def fake_run(command, **_kwargs):
        if command[:2] == ["sysctl", "-n"]:
            return subprocess.CompletedProcess(command, 0, stdout=str(16 * 2**30) + "\n", stderr="")
        if command == ["vm_stat"]:
            return subprocess.CompletedProcess(command, 0, stdout=VM_STAT_SAMPLE_16K_PAGES, stderr="")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(llm_hardware.subprocess, "run", fake_run)
    total_mb, available_mb = llm_hardware._memory()
    assert total_mb == 16 * 1024
    assert available_mb == (10000 + 5000 + 500) * 16384 // 2**20


def test_darwin_memory_branch_falls_back_to_zero_on_failure(monkeypatch):
    monkeypatch.setattr(llm_hardware.platform, "system", lambda: "Darwin")

    def fake_run(_command, **_kwargs):
        raise FileNotFoundError("sysctl not found")

    monkeypatch.setattr(llm_hardware.subprocess, "run", fake_run)
    assert llm_hardware._memory() == (0, 0)


def test_parse_vm_stat_tolerates_malformed_lines_without_raising():
    # A label line missing its colon, and a label line with a non-numeric
    # page count, must both be skipped rather than raising IndexError/ValueError.
    text = "Pages free\nPages inactive:   10.\nPages speculative:   not-a-number.\n"
    # Only "Pages inactive: 10" is parseable; default page size is 4096.
    assert llm_hardware._parse_vm_stat(text) == (10 * 4096) // 2**20


def test_darwin_memory_branch_returns_zero_pair_on_garbage_vm_stat_output(monkeypatch):
    monkeypatch.setattr(llm_hardware.platform, "system", lambda: "Darwin")

    def fake_run(command, **_kwargs):
        if command[:2] == ["sysctl", "-n"]:
            return subprocess.CompletedProcess(command, 0, stdout=str(16 * 2**30) + "\n", stderr="")
        if command == ["vm_stat"]:
            return subprocess.CompletedProcess(command, 0, stdout="not vm_stat output at all\n\xff\xfe", stderr="")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(llm_hardware.subprocess, "run", fake_run)
    total_mb, available_mb = llm_hardware._memory()
    # _parse_vm_stat must not raise on garbage input; with no parseable page
    # lines, available memory comes back as 0 while total is still read.
    assert total_mb == 16 * 1024
    assert available_mb == 0


def test_docker_warning_names_native_runtime_and_local_api(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_hardware.shutil, "which", lambda _name: None)
    report = llm_hardware.detect_hardware(tmp_path)
    assert any(
        "the bundled native runtime or an existing local API remains available" in warning
        for warning in report.warnings
    )
