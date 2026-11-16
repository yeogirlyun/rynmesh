"""Docker-backed llama.cpp runtime backend.

Moved out of `lifecycle.py` verbatim (no behavior change) so `lifecycle.py`
can dispatch on `manifest.runtime` and Task 2 can add a native backend beside
this one. Callers reach this module only through `lifecycle._backend`, except
where a manifest does not exist yet (`install_managed`/`import_gguf` call
`available()`/`prepare()` directly, matching the pre-extraction order).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import LifecycleError
from .manifest import LLMPackageManifest, cached_fingerprint

# The llama.cpp server flags are the same binary's either way, so the hardening
# constants are defined once, next to the flag verification, in the native
# backend, and imported here rather than copied.
from .runtime_native import API_KEY_BYTES, CORS_ORIGINS, LOG_VERBOSITY

RUNTIME_ID = "docker_llama_cpp"
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _quiet() -> dict[str, Any]:
    return {"creationflags": _CREATE_NO_WINDOW} if os.name == "nt" else {}


def _docker_cpus(manifest: LLMPackageManifest) -> str:
    override = (manifest.hardware_requirements or {}).get("cpus")
    if override is not None and not isinstance(override, bool):
        try:
            value = float(override)
            if value > 0:
                return str(int(value) if value.is_integer() else value)
        except (TypeError, ValueError):
            pass
    return str(max(1, (os.cpu_count() or 1) - 1))

DEFAULT_IMAGE = (
    "ghcr.io/ggml-org/llama.cpp:server@"
    "sha256:db8e923e6edc9241ad788979af79543a1e1ba55dbb7d41e62490ef0d0ad3c8e7"
)
_HOST_PORT_RE = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*"
    r"|\[[0-9A-Fa-f:]+\])(?::([0-9]{1,5}))?"
)


def _normalize_docker_registry_mirror(mirror: str | None = None) -> str:
    raw = (mirror if mirror is not None else os.environ.get("RYNMESH_DOCKER_REGISTRY_MIRROR", "")).strip()
    if not raw:
        return ""
    if raw.lower().startswith("http://"):
        raise LifecycleError("Docker registry mirror must not use plain HTTP")
    if raw.lower().startswith("https://"):
        raw = raw[len("https://"):]
    if raw.endswith("/"):
        raw = raw[:-1]
    if not raw or any(ch in raw for ch in ("/", "?", "#", "@", "\\", " ", "\t", "\r", "\n")):
        raise LifecycleError("Docker registry mirror must specify registry host only")
    match = _HOST_PORT_RE.fullmatch(raw)
    if not match:
        raise LifecycleError("invalid Docker registry mirror host")
    port_text = match.group(1)
    if port_text is not None and not (1 <= int(port_text) <= 65535):
        raise LifecycleError("invalid Docker registry mirror port")
    return raw


def _docker_pull(
    image: str,
    *,
    progress: Any = None,
    cancel_check: Any = None,
    timeout_s: float = 600,
) -> None:
    docker = _docker()
    if progress:
        progress("pull_runtime", 72, "Preparing the local inference runtime")
    process = subprocess.Popen(
        [docker, "pull", image],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **_quiet(),
    )
    deadline = time.monotonic() + timeout_s
    try:
        while process.poll() is None:
            if cancel_check and cancel_check():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise LifecycleError("setup cancelled")
            if time.monotonic() >= deadline:
                process.terminate()
                raise LifecycleError("downloading the llama.cpp runtime timed out")
            if progress:
                progress("pull_runtime", 72, "Preparing the local inference runtime")
            time.sleep(0.25)
    finally:
        if process.poll() is None:
            process.kill()
    if process.returncode:
        raise LifecycleError("unable to download the llama.cpp runtime image")


def _docker() -> str:
    executable = shutil.which("docker")
    if not executable:
        raise LifecycleError("Docker is not installed; connect an existing local API or install Docker first")
    try:
        subprocess.run([executable, "info", "--format", "{{.ServerVersion}}"], check=True,
                       capture_output=True, text=True, timeout=15, **_quiet())
    except (OSError, subprocess.SubprocessError) as exc:
        raise LifecycleError("Docker is installed but its engine is not running") from exc
    return executable


def _container_name(package_id: str) -> str:
    return "rynmesh-llm-" + re.sub(r"[^a-z0-9_.-]", "-", package_id.lower())


def _pinned_runtime_image(
    manifest: LLMPackageManifest | None = None,
    *,
    apply_mirror: bool = True,
) -> str:
    image = str((manifest.install_source if manifest else {}).get("runtime_image") or DEFAULT_IMAGE)
    if image == "ghcr.io/ggml-org/llama.cpp:server":
        # Migrate manifests created by the pre-pinning preview to the reviewed
        # digest without ever pulling or running the mutable legacy tag.
        image = DEFAULT_IMAGE
    if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image):
        raise LifecycleError("managed runtime image must be pinned by SHA-256 digest")
    if apply_mirror:
        mirror_host = _normalize_docker_registry_mirror()
        if mirror_host and image.startswith("ghcr.io/"):
            image = f"{mirror_host}/{image[len('ghcr.io/'):]}"
    return image


def _docker_state(package_id: str) -> dict[str, Any]:
    docker = shutil.which("docker")
    if not docker:
        return {"installed": False, "running": False, "status": "docker unavailable"}
    result = subprocess.run(
        [docker, "inspect", "--format", "{{json .State}}", _container_name(package_id)],
        capture_output=True, text=True, timeout=15, **_quiet(),
    )
    if result.returncode:
        return {"installed": False, "running": False, "status": "not created"}
    try:
        state = json.loads(result.stdout)
    except json.JSONDecodeError:
        state = {}
    return {"installed": True, "running": bool(state.get("Running")),
            "status": str(state.get("Status") or "unknown"), "exit_code": state.get("ExitCode")}


def _image_present(docker: str, image: str) -> bool:
    result = subprocess.run(
        [docker, "image", "inspect", image],
        capture_output=True,
        text=True,
        timeout=15,
        **_quiet(),
    )
    return result.returncode == 0


def _run_container(
    manifest: LLMPackageManifest,
    *,
    progress: Any = None,
    cancel_check: Any = None,
) -> None:
    docker = _docker()
    model = Path(manifest.model_path).resolve()
    if not model.is_file():
        raise LifecycleError("configured model file is missing")
    if manifest.checksum and cached_fingerprint(model) != manifest.checksum:
        raise LifecycleError("configured model checksum no longer matches; refusing to start")
    runtime_image = _pinned_runtime_image(manifest)
    if not _image_present(docker, runtime_image):
        _docker_pull(runtime_image, progress=progress, cancel_check=cancel_check, timeout_s=600)
    name = _container_name(manifest.package_id)
    subprocess.run([docker, "rm", "-f", name], capture_output=True, timeout=30, **_quiet())
    port = int(urlparse(manifest.base_url).port or 8080)
    if not manifest.runtime_api_key:
        manifest.runtime_api_key = secrets.token_urlsafe(API_KEY_BYTES)
    command = [
        docker, "run", "-d", "--name", name, "--read-only", "--tmpfs", "/tmp:size=128m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "256",
        "--cpus", _docker_cpus(manifest),
        "--memory", str(max(1024, int(manifest.hardware_requirements.get("estimated_memory_mb", 2048))) * 1024**2),
        "--memory-swap", str(max(1024, int(manifest.hardware_requirements.get("estimated_memory_mb", 2048))) * 1024**2),
        "--log-driver", "none", "--ulimit", "core=0",
        "-p", f"127.0.0.1:{port}:8080", "-v", f"{model.parent}:/models:ro", runtime_image,
        "-m", f"/models/{model.name}", "--host", "0.0.0.0", "--port", "8080",
        "--alias", manifest.public_model_alias, "-c", str(manifest.context_window),
        "-np", str(manifest.max_concurrent),
        # Same dangerous llama.cpp defaults as the native backend: the
        # published port is loopback-only, but without these any page the
        # owner visits could still drive the model through it — and the
        # server's own log stays at error level either way.
        "--api-key", manifest.runtime_api_key, "--cors-origins", CORS_ORIGINS,
        "-lv", LOG_VERBOSITY,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60, **_quiet())
    if result.returncode:
        raise LifecycleError("llama.cpp container failed to start (inspect Docker logs for details)")


def available() -> tuple[bool, str]:
    """(True, "") when the Docker engine answers; else (False, safe reason)."""
    try:
        _docker()
    except LifecycleError as exc:
        return False, str(exc)
    return True, ""


def prepare(
    manifest: LLMPackageManifest | None = None,
    *,
    progress: Any = None,
    cancel_check: Any = None,
    root: Any = None,
) -> None:
    """Pull the pinned image declared by `manifest` (or `DEFAULT_IMAGE`).

    `root` exists only so both backends share one `prepare` signature; the
    Docker image lives in the engine's own store, not under the Rynmesh root.
    """
    _docker_pull(_pinned_runtime_image(manifest), progress=progress, cancel_check=cancel_check)


def start(
    manifest: LLMPackageManifest,
    *,
    progress: Any = None,
    cancel_check: Any = None,
) -> None:
    _run_container(manifest, progress=progress, cancel_check=cancel_check)


def stop(manifest: LLMPackageManifest) -> bool:
    result = subprocess.run([_docker(), "stop", _container_name(manifest.package_id)],
                            capture_output=True, text=True, timeout=60, **_quiet())
    return result.returncode == 0


def remove(manifest: LLMPackageManifest) -> None:
    subprocess.run([_docker(), "rm", "-f", _container_name(manifest.package_id)],
                   capture_output=True, timeout=60, **_quiet())


def update(manifest: LLMPackageManifest) -> None:
    subprocess.run([_docker(), "pull", _pinned_runtime_image(manifest)], check=True, timeout=600, **_quiet())


def state(manifest: LLMPackageManifest) -> dict[str, Any]:
    return _docker_state(manifest.package_id)


def install_source(manifest_or_none: LLMPackageManifest | None) -> dict[str, str]:
    return {"runtime_image": _pinned_runtime_image(manifest_or_none, apply_mirror=False)}
