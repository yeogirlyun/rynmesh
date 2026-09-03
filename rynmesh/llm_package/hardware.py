"""Cross-platform hardware detection and conservative LLM recommendations."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


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


def _parse_vm_stat(text: str, page_size_default: int = 4096) -> int:
    """Parse macOS `vm_stat` output into available memory in MiB.

    Available memory is the sum of free, inactive, and speculative pages,
    using the page size reported in the `vm_stat` header (falling back to
    `page_size_default` if it cannot be parsed).
    """
    page_size = page_size_default
    header_match = re.search(r"page size of (\d+) bytes", text)
    if header_match:
        page_size = int(header_match.group(1))
    wanted = ("Pages free", "Pages inactive", "Pages speculative")
    pages = 0
    for line in text.splitlines():
        for label in wanted:
            if line.startswith(label):
                value = line.split(":", 1)[1].strip().rstrip(".")
                pages += int(value)
                break
    return pages * page_size // 2**20


def _darwin_memory() -> tuple[int, int]:
    total_result = subprocess.run(
        ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5, check=True,
    )
    total_mb = int(total_result.stdout.strip()) // 2**20
    vm_result = subprocess.run(
        ["vm_stat"], capture_output=True, text=True, timeout=5, check=True,
    )
    available_mb = _parse_vm_stat(vm_result.stdout)
    return total_mb, available_mb


def _memory() -> tuple[int, int]:
    if platform.system() == "Darwin":
        try:
            return _darwin_memory()
        except (OSError, subprocess.SubprocessError, ValueError):
            return 0, 0
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
        warnings.append(
            "A running Docker engine was not detected; the bundled native runtime or an existing "
            "local API remains available."
        )
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


def report_json(path: str | Path | None = None) -> str:
    report = detect_hardware(path)
    return json.dumps({"hardware": report.to_dict(), "recommendations": recommend(report)}, indent=2)
