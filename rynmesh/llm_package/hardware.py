"""Cross-platform hardware detection and conservative LLM recommendations."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

LOCAL_AI_HARDWARE_VERSION = "ryn.local-ai-hardware.v1"


def platform_id() -> str:
    system = platform.system().lower()
    architecture = platform.machine().lower().replace("amd64", "x86_64")
    return f"{system}-{architecture}"


@dataclass
class GPUInfo:
    name: str
    memory_total_mb: int
    memory_free_mb: int
    driver_version: str


@dataclass
class HardwareReport:
    os: str
    architecture: str
    cpu: str
    logical_cpus: int
    ram_total_mb: int
    ram_available_mb: int
    disk_free_mb: int
    nvidia_gpus: list[GPUInfo]
    nvidia_probe: str
    container_runtime: str
    container_available: bool
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _memory() -> tuple[int, int]:
    if os.name == "nt":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
                ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                ("avail_extended", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.total_phys // 2**20, status.avail_phys // 2**20
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0])
        return values["MemTotal"] // 1024, values.get("MemAvailable", values["MemFree"]) // 1024
    except (OSError, KeyError, ValueError):
        return 0, 0


def _nvidia() -> tuple[list[GPUInfo], str]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return [], "nvidia-smi not found; NVIDIA GPU/driver unavailable or not installed"
    try:
        result = subprocess.run(
            [executable, "--query-gpu=name,memory.total,memory.free,driver_version", "--format=csv,noheader,nounits"],
            check=True, capture_output=True, text=True, timeout=8,
        )
        gpus = []
        for line in result.stdout.splitlines():
            name, total, free, driver = (part.strip() for part in line.split(",", 3))
            gpus.append(GPUInfo(name, int(float(total)), int(float(free)), driver))
        return gpus, "ok" if gpus else "nvidia-smi returned no GPUs"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return [], f"NVIDIA probe failed: {exc}"


def detect_hardware(path: str | Path | None = None) -> HardwareReport:
    ram_total, ram_available = _memory()
    disk_target = Path(path or Path.cwd()).expanduser().resolve()
    while not disk_target.exists() and disk_target.parent != disk_target:
        disk_target = disk_target.parent
    disk_free = shutil.disk_usage(disk_target).free // 2**20
    gpus, gpu_status = _nvidia()
    docker = shutil.which("docker")
    container_ok = False
    if docker:
        try:
            subprocess.run([docker, "info", "--format", "{{json .ServerVersion}}"], check=True,
                           capture_output=True, timeout=8)
            container_ok = True
        except (OSError, subprocess.SubprocessError):
            pass
    warnings = []
    if not ram_total:
        warnings.append("Could not determine total RAM; automatic managed installation is disabled.")
    if not gpus:
        warnings.append("No usable NVIDIA GPU was detected; CPU inference or an existing local API remains available.")
    if not container_ok:
        warnings.append("A running Docker engine was not detected; native runtime mode remains available.")
    return HardwareReport(
        os=platform.system(), architecture=platform.machine(), cpu=platform.processor() or "unknown",
        logical_cpus=os.cpu_count() or 1, ram_total_mb=ram_total, ram_available_mb=ram_available,
        disk_free_mb=disk_free, nvidia_gpus=gpus, nvidia_probe=gpu_status,
        container_runtime="docker" if docker else "", container_available=container_ok, warnings=warnings,
    )


def recommend(report: HardwareReport) -> list[dict[str, Any]]:
    """Return only profiles that fit conservative available-memory/disk caps."""
    candidates = [
        ("Qwen2.5-0.5B-Instruct-Q4_K_M", 500, 900, 4096, 2),
        ("Qwen2.5-1.5B-Instruct-Q4_K_M", 1500, 2300, 4096, 1),
        ("Qwen2.5-3B-Instruct-Q4_K_M", 3000, 4200, 4096, 1),
    ]
    usable_ram = int(report.ram_available_mb * 0.75)
    usable_vram = max((gpu.memory_free_mb for gpu in report.nvidia_gpus), default=0)
    usable_memory = max(usable_ram, int(usable_vram * 0.85))
    recommendations = []
    for alias, params_m, estimated_mb, context, concurrency in candidates:
        disk_need = int(estimated_mb * 1.5 + 256)
        if estimated_mb <= usable_memory and disk_need <= report.disk_free_mb:
            recommendations.append({
                "model_alias": alias, "parameter_millions": params_m, "quantization": "Q4_K_M",
                "estimated_memory_mb": estimated_mb, "estimated_disk_mb": disk_need,
                "context_window": context, "max_concurrent": concurrency,
                # The managed llama.cpp image is the portable CPU build. GPU
                # data is still reported so an advanced owner can override it,
                # but the safe default must describe what we actually launch.
                "device": "cpu",
                "can_run": True,
            })
    if not recommendations:
        recommendations.append({
            "can_run": False, "reason": "No bundled profile safely fits detected available RAM/disk.",
            "alternatives": ["Connect an existing loopback OpenAI-compatible service", "Free memory/disk and retry"],
        })
    return recommendations


def setup_recommendations(report: HardwareReport) -> dict[str, Any]:
    """Versioned, conservative product profiles for the setup wizard."""
    usable_ram = int(report.ram_available_mb * 0.75)
    runtime_platform = platform_id()
    # v1 only advertises platforms that have completed a physical clean-install
    # acceptance run.  Additional reviewed runtime artifacts may be pinned in the
    # lifecycle module without being exposed to ordinary users yet.
    native_platforms = {"windows-x86_64"}
    managed_available = runtime_platform in native_platforms
    definitions = [
        {
            "id": "light",
            "model_id": "Qwen2.5-0.5B-Instruct-Q4_K_M",
            "alias": "rynmesh-qwen2.5-0.5b-q4",
            "version": "872f8a96064a1242ac3a3359cad77c3042548405",
            "download_mib": 491,
            "estimated_memory_mib": 900,
            "context_window": 4096,
            "backend": "native-llama.cpp-cpu",
            "published": True,
        },
        {
            "id": "balanced",
            "model_id": "Qwen2.5-1.5B-Instruct-Q4_K_M",
            "alias": "rynmesh-qwen2.5-1.5b-q4",
            "version": "not-published",
            "download_mib": 1100,
            "estimated_memory_mib": 2300,
            "context_window": 4096,
            "backend": "native-llama.cpp-cpu",
            "published": False,
        },
        {
            "id": "quality",
            "model_id": "Qwen2.5-3B-Instruct-Q4_K_M",
            "alias": "rynmesh-qwen2.5-3b-q4",
            "version": "not-published",
            "download_mib": 2200,
            "estimated_memory_mib": 4200,
            "context_window": 4096,
            "backend": "native-llama.cpp-cpu",
            "published": False,
        },
    ]
    profiles: list[dict[str, Any]] = []
    for profile in definitions:
        disk_need = int(profile["download_mib"] * 2 + 384)
        reason = ""
        if not managed_available:
            reason = "A reviewed native runtime is not published for this platform."
        elif not profile["published"]:
            reason = "This model profile is not published in the reviewed manifest yet."
        elif report.ram_available_mb <= 0:
            reason = "Available memory could not be measured safely."
        elif int(profile["estimated_memory_mib"]) > usable_ram:
            reason = "The conservative available-memory limit is too small for this profile."
        elif disk_need > report.disk_free_mb:
            reason = "There is not enough free disk space for download and verification."
        profiles.append({
            **profile,
            "estimated_disk_mib": disk_need,
            "supported": not reason,
            "unsupported_reason": reason or None,
            "recommendation_reason": (
                "Small reviewed model with the fastest safe first answer on this device."
                if profile["id"] == "light" else
                "Larger profiles remain visible but disabled until their manifests are reviewed."
            ),
        })
    recommended = next((item["id"] for item in profiles if item["supported"]), None)
    return {
        "version": LOCAL_AI_HARDWARE_VERSION,
        "platform": runtime_platform,
        "memory_available_mib": report.ram_available_mb,
        "disk_available_mib": report.disk_free_mb,
        "accelerators": [
            {"kind": "cuda", "name": gpu.name, "memory_mib": gpu.memory_free_mb}
            for gpu in report.nvidia_gpus
        ],
        "runtime_support": {
            "managed": managed_available,
            "ollama": bool(shutil.which("ollama")),
            "docker_preview": report.container_available,
        },
        "recommended_profile": recommended,
        "profiles": profiles,
        "warnings": list(report.warnings),
    }


def report_json(path: str | Path | None = None) -> str:
    report = detect_hardware(path)
    return json.dumps({"hardware": report.to_dict(), "recommendations": recommend(report)}, indent=2)
