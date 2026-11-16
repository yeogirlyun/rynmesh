"""Unit tests for S01-I19: SharedSlots local_compute, rejection reasons, macOS memory, and async GPU temperature."""
from __future__ import annotations

import platform
import subprocess
import threading
from collections import namedtuple
from types import SimpleNamespace

from fastapi import FastAPI

from rynmesh.llm_package import hardware, safety
from rynmesh.llm_package.manifest import LLMPackageManifest
from rynmesh.llm_package.routes import ProviderService, install_llm_routes
from rynmesh.llm_package.safety import NODE_SLOTS, SharedSlots
from rynmesh.llm_package.task_balance import TaskBalanceLedger
from rynmesh.llm_package.task_protocol import TaskOrderStore
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


def _healthy_env(monkeypatch):
    monkeypatch.setattr(safety.shutil, "disk_usage", lambda _: DiskUsage(10 * 1024**3, 1024**3, 9 * 1024**3))
    monkeypatch.setattr(safety, "_available_memory", lambda: 4 * 1024**3)
    monkeypatch.setattr(safety, "_gpu_temperature", None, raising=False)
    monkeypatch.setattr(safety, "_pressure_checked", 0.0)
    monkeypatch.setattr(safety, "_pressure_ok", True)
    monkeypatch.setattr(safety, "_ensure_gpu_sampler", lambda: None, raising=False)


def test_t3_api_and_remote_sources_bypass_node_slots_and_pressure(tmp_path, monkeypatch):
    """T3 (Changes 1, 6, 8): local_compute=False (API/external) does not consume NODE_SLOTS and skips memory/GPU checks."""
    _healthy_env(monkeypatch)
    # Simulate severe local memory pressure and overheated GPU: API source must still be admitted.
    monkeypatch.setattr(safety, "_available_memory", lambda: 32 * 1024**2)
    monkeypatch.setattr(safety, "_gpu_temperature", 95, raising=False)

    # Drain all 4 process-wide NODE_SLOTS first to prove API slots do not touch NODE_SLOTS.
    held_node_slots = 0
    for _ in range(4):
        assert NODE_SLOTS.acquire(blocking=False)
        held_node_slots += 1
    try:
        api_slots = SharedSlots(6, tmp_path, local_compute=False)
        assert api_slots.local_compute is False
        for _ in range(6):
            assert api_slots.acquire_reason() is None
        # 7th exceeds its own capacity=6 -> service_slots
        assert api_slots.acquire_reason() == "service_slots"
        assert api_slots.acquire() is False
        for _ in range(6):
            api_slots.release()
    finally:
        for _ in range(held_node_slots):
            NODE_SLOTS.release()

    # Also verify routes.ProviderService passes local_compute from manifest.runtime:
    # external API manifest -> local_compute=False; native_llama_cpp manifest -> local_compute=True.
    monkeypatch.setattr("rynmesh.store._primary_lan_ip", lambda: "192.0.2.10")
    monkeypatch.setattr("rynmesh.store._local_ip_addresses", lambda: ("192.0.2.10",))
    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    app = FastAPI()
    install_llm_routes(
        app,
        store=store,
        home=home,
        messaging_key=key,
        resolve_endpoint=lambda _: "",
        resolve_pubkey=lambda _: "",
    )
    try:
        sources = app.state.llm_agent_services["source_store"]
        rec = sources.prepare({
            "name": "Cloud API",
            "kind": "api",
            "base_url": "https://vendor.example/v1",
            "api_key": "sk-test-fixture",
            "models": ["cloud-x"],
        })
        rec["status"] = "ready"
        sources.save(rec)
        sid = sources.model_service_id(rec, "cloud-x")
        api_mgr = app.state.llm_agent_services["provider_for"](sid)
        assert api_mgr is not None
        assert api_mgr._slots.local_compute is False

        local_manifest = LLMPackageManifest(
            package_id="local-native",
            mode="managed",
            public_model_alias="Local Native",
            runtime="native_llama_cpp",
            max_concurrent=2,
        )
        local_mgr = ProviderService(
            manifest=local_manifest,
            adapter=SimpleNamespace(health=lambda: {"ok": True}, shutdown=lambda: None),
            store=store,
            task_store=TaskOrderStore(tmp_path / "orders"),
            balance=TaskBalanceLedger(tmp_path / "balance.json"),
            messaging_key=key,
        )
        assert local_mgr._slots.local_compute is True
    finally:
        app.state.llm_shutdown()


def test_t4_local_compute_capacity_8_still_bounded_by_node_slots(tmp_path, monkeypatch):
    """T4 (Changes 1, 6): local_compute=True with capacity=8 still shares NODE_SLOTS=4."""
    _healthy_env(monkeypatch)
    slots = SharedSlots(8, tmp_path, local_compute=True)
    acquired = 0
    try:
        for _ in range(4):
            assert slots.acquire() is True
            acquired += 1
        # Even though local capacity is 8, the 5th request hits process-wide NODE_SLOTS=4.
        assert slots.acquire_reason() == "node_slots"
        assert slots.acquire() is False
    finally:
        for _ in range(acquired):
            slots.release()


def test_t5_shared_slots_rejection_reasons(tmp_path, monkeypatch):
    """T5 (Changes 1, 7, 8): acquire_reason distinguishes disk, memory_pressure, gpu_temperature, service_slots, node_slots."""
    _healthy_env(monkeypatch)
    slots = SharedSlots(1, tmp_path, local_compute=True)

    # 1. disk
    monkeypatch.setattr(safety.shutil, "disk_usage", lambda _: DiskUsage(10 * 1024**3, 10 * 1024**3, 10 * 1024**2))
    assert slots.acquire_reason() == "disk"
    assert slots.acquire() is False

    # Restore disk; 2. memory_pressure (< 256 MiB)
    monkeypatch.setattr(safety.shutil, "disk_usage", lambda _: DiskUsage(10 * 1024**3, 1024**3, 9 * 1024**3))
    monkeypatch.setattr(safety, "_pressure_checked", 0.0)
    monkeypatch.setattr(safety, "_available_memory", lambda: 100 * 1024**2)
    assert slots.acquire_reason() == "memory_pressure"
    assert slots.acquire() is False

    # Restore memory; 3. gpu_temperature (>= 85 C)
    monkeypatch.setattr(safety, "_pressure_checked", 0.0)
    monkeypatch.setattr(safety, "_available_memory", lambda: 2 * 1024**3)
    monkeypatch.setattr(safety, "_gpu_temperature", 86)
    assert slots.acquire_reason() == "gpu_temperature"
    assert slots.acquire() is False

    # Restore GPU temperature; 4. service_slots (capacity=1 occupied)
    monkeypatch.setattr(safety, "_pressure_checked", 0.0)
    monkeypatch.setattr(safety, "_gpu_temperature", 60)
    assert slots.acquire_reason() is None
    try:
        assert slots.acquire_reason() == "service_slots"
    finally:
        slots.release()

    # 5. node_slots (4 NODE_SLOTS occupied by another local service)
    other = SharedSlots(4, tmp_path, local_compute=True)
    for _ in range(4):
        assert other.acquire_reason() is None
    try:
        assert slots.acquire_reason() == "node_slots"
    finally:
        for _ in range(4):
            other.release()


def test_t6_macos_available_memory_uses_darwin_sysctl_and_vm_stat(tmp_path, monkeypatch):
    """T6 (Change 7): _available_memory on macOS calls hardware._darwin_memory (sysctl + vm_stat)."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0] == "sysctl":
            return subprocess.CompletedProcess(cmd, 0, stdout=str(16 * 1024**3) + "\n", stderr="")
        if cmd[0] == "vm_stat":
            # 16384-byte pages: (5000 + 4000 + 1000) * 16384 = 163,840,000 bytes = 156 MiB (< 256 MiB)
            vm_out = (
                "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
                "Pages free:                                5000.\n"
                "Pages active:                            200000.\n"
                "Pages inactive:                            4000.\n"
                "Pages speculative:                         1000.\n"
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=vm_out, stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(hardware.subprocess, "run", fake_run)
    monkeypatch.setattr(safety.shutil, "disk_usage", lambda _: DiskUsage(10 * 1024**3, 1024**3, 9 * 1024**3))
    monkeypatch.setattr(safety, "_gpu_temperature", None, raising=False)
    monkeypatch.setattr(safety, "_pressure_checked", 0.0)
    monkeypatch.setattr(safety, "_ensure_gpu_sampler", lambda: None, raising=False)

    avail = safety._available_memory()
    assert avail == 156 * 1024**2
    assert ["sysctl", "-n", "hw.memsize"] in calls
    assert ["vm_stat"] in calls

    slots = SharedSlots(1, tmp_path, local_compute=True)
    assert slots.acquire_reason() == "memory_pressure"


def test_t7_pressure_ok_never_spawns_nvidia_smi_on_caller_thread(monkeypatch):
    """T7 (Change 7): pressure_ok reads cached GPU temperature and never runs nvidia-smi on the caller thread."""
    caller_ident = threading.get_ident()
    sampled = threading.Event()
    subproc_threads = []

    def fake_which(name):
        return "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None

    def fake_run(cmd, **kwargs):
        subproc_threads.append(threading.get_ident())
        sampled.set()
        return subprocess.CompletedProcess(cmd, 0, stdout=b"88\n", stderr=b"")

    monkeypatch.setattr(safety.shutil, "which", fake_which)
    monkeypatch.setattr(safety.subprocess, "run", fake_run)
    monkeypatch.setattr(safety, "_available_memory", lambda: 4 * 1024**3)
    monkeypatch.setattr(safety, "_pressure_checked", 0.0)
    monkeypatch.setattr(safety, "_pressure_ok", True)
    monkeypatch.setattr(safety, "_gpu_temperature", None, raising=False)
    monkeypatch.setattr(safety, "_gpu_sampler_thread", None, raising=False)
    monkeypatch.setattr(safety, "_GPU_SAMPLE_INTERVAL_SECONDS", 0.05, raising=False)

    # First call starts background sampler if needed and returns immediately without running subprocess on caller thread.
    _ = safety.pressure_ok()
    assert sampled.wait(timeout=2.0), "background GPU sampler did not sample nvidia-smi"
    assert caller_ident not in subproc_threads

    # Once background sampler records 88 C (>= 85 C), pressure_ok immediately reflects thermal pressure.
    monkeypatch.setattr(safety, "_pressure_checked", 0.0)
    assert safety.pressure_ok() is False

    # Stop background loop cleanly when monkeypatch tears down by pointing which() to None.
    monkeypatch.setattr(safety.shutil, "which", lambda _: None)
