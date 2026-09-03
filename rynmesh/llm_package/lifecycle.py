"""Installation, import, self-test, and split runtime/model lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .adapters import AdapterError, adapter_from_manifest
from .hardware import HardwareReport, detect_hardware, platform_id, recommend, setup_recommendations
from .manifest import (
    LLMPackageManifest,
    fingerprint_file,
    load_manifest,
    save_manifest,
)

DEFAULT_IMAGE = (
    "ghcr.io/ggml-org/llama.cpp:server@"
    "sha256:db8e923e6edc9241ad788979af79543a1e1ba55dbb7d41e62490ef0d0ad3c8e7"
)
DEFAULT_MODEL_REVISION = "872f8a96064a1242ac3a3359cad77c3042548405"
DEFAULT_MODEL_SHA256 = "74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db"
DEFAULT_MODEL_URL = (
    "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/"
    f"{DEFAULT_MODEL_REVISION}/"
    "qwen2.5-0.5b-instruct-q4_k_m.gguf"
)
NATIVE_RUNTIME_VERSION = "b10516"
NATIVE_RUNTIME_SPECS = {
    "windows-x86_64": {
        "archive": "llama-b10516-bin-win-cpu-x64.zip",
        "sha256": "fbbbc55e0eb2e1b07f9dcb9488616c98ed47d9003b90e15e7c8c7812c4307cd3",
        "executable": "llama-server.exe",
    },
    "darwin-arm64": {
        "archive": "llama-b10516-bin-macos-arm64.tar.gz",
        "sha256": "ee3324327d621026ae80c24031670e65fa62a0b23a3a027dbe2f65f240affd30",
        "executable": "llama-server",
    },
    "darwin-aarch64": {
        "archive": "llama-b10516-bin-macos-arm64.tar.gz",
        "sha256": "ee3324327d621026ae80c24031670e65fa62a0b23a3a027dbe2f65f240affd30",
        "executable": "llama-server",
    },
    "darwin-x86_64": {
        "archive": "llama-b10516-bin-macos-x64.tar.gz",
        "sha256": "b7adecf7bd2cde577ddabee8357a72409165d8104f43b4acee9f1b98cc9c447a",
        "executable": "llama-server",
    },
}
GGUF_MAGIC = b"GGUF"
_NATIVE_PROCESSES: dict[str, subprocess.Popen] = {}


class LifecycleError(RuntimeError):
    pass


ProgressCallback = Callable[[str, int, str], None]
CancelCheck = Callable[[], bool]


def _progress(
    callback: ProgressCallback | None,
    cancel_check: CancelCheck | None,
    stage: str,
    percent: int,
    message: str,
) -> None:
    if cancel_check and cancel_check():
        raise LifecycleError("setup cancelled")
    if callback:
        callback(stage, max(0, min(100, percent)), message)


def _validate_package_id(package_id: str) -> str:
    if not package_id or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_." for ch in package_id):
        raise LifecycleError("package_id must be a non-empty lowercase slug")
    return package_id


def default_root() -> Path:
    return Path(os.environ.get("RYNMESH_LLM_HOME", Path.home() / ".rynmesh" / "llm")).expanduser()


def manifest_path(package_id: str, root: str | Path | None = None) -> Path:
    return Path(root or default_root()).expanduser() / "packages" / package_id / "manifest.json"


def validate_gguf(path: str | Path, *, report: HardwareReport | None = None,
                  allow_risk: bool = False) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise LifecycleError("GGUF path is not a readable file")
    try:
        with source.open("rb") as handle:
            magic = handle.read(4)
    except OSError as exc:
        raise LifecycleError(f"GGUF file is not readable: {exc}") from exc
    if magic != GGUF_MAGIC:
        raise LifecycleError("unsupported model format: expected GGUF magic")
    size = source.stat().st_size
    if size < 32:
        raise LifecycleError("GGUF file is truncated")
    hardware = report or detect_hardware(source.parent)
    estimated_memory = int(size / 2**20 * 1.35 + 384)
    safe_memory = max(int(hardware.ram_available_mb * 0.75), max(
        (int(gpu.memory_free_mb * 0.85) for gpu in hardware.nvidia_gpus), default=0
    ))
    fits = bool(safe_memory and estimated_memory <= safe_memory)
    if not fits and not allow_risk:
        raise LifecycleError(
            f"model needs about {estimated_memory} MiB but the conservative available limit is "
            f"{safe_memory} MiB; pass --accept-risk to override"
        )
    return {"path": str(source), "format": "GGUF", "size_bytes": size,
            "estimated_memory_mb": estimated_memory, "fits": fits,
            "fingerprint": fingerprint_file(source)}


def _download(
    url: str,
    destination: Path,
    expected_sha256: str,
    *,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    max_bytes: int = 8 * 1024**3,
) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise LifecycleError("install downloads require an HTTPS URL")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    metadata_path = temporary.with_suffix(temporary.suffix + ".json")
    metadata = {"url": url, "sha256": expected_sha256.lower()}
    if metadata_path.exists():
        try:
            stored_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            stored_metadata = {}
        if stored_metadata.get("url") != url or stored_metadata.get("sha256") != expected_sha256.lower():
            temporary.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
    elif temporary.exists():
        temporary.unlink(missing_ok=True)

    existing = temporary.stat().st_size if temporary.exists() else 0
    if existing > max_bytes:
        temporary.unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)
        raise LifecycleError("partial download exceeds the configured size limit")
    digest = hashlib.sha256()
    if existing:
        with temporary.open("rb") as partial:
            while chunk := partial.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() == expected_sha256.lower():
            temporary.replace(destination)
            metadata_path.unlink(missing_ok=True)
            return expected_sha256.lower()
    try:
        headers = {"User-Agent": "Rynmesh/0.6"}
        if existing:
            headers["Range"] = f"bytes={existing}-"
            try:
                prior = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                prior = {}
            if prior.get("validator"):
                headers["If-Range"] = str(prior["validator"])
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=300) as response:
            response_status = int(getattr(response, "status", response.getcode()) or 200)
            content_range = str(response.headers.get("content-range") or "")
            resumed = bool(existing and response_status == 206 and content_range.startswith(f"bytes {existing}-"))
            if existing and not resumed:
                existing = 0
                digest = hashlib.sha256()
            remaining = int(response.headers.get("content-length") or 0)
            total = existing + remaining if remaining else 0
            if total and total > max_bytes:
                raise LifecycleError("download exceeds the configured size limit")
            disk_required = max(0, total - existing) + 64 * 1024**2 if total else 64 * 1024**2
            if shutil.disk_usage(destination.parent).free < disk_required:
                raise LifecycleError("not enough free disk space for the verified download")
            validator = response.headers.get("etag") or response.headers.get("last-modified") or ""
            metadata_path.write_text(
                json.dumps({**metadata, "validator": validator}, sort_keys=True), encoding="utf-8",
            )
            downloaded = existing
            last_reported_percent = -1
            last_reported_at = 0.0
            with temporary.open("ab" if resumed else "wb") as handle:
                while chunk := response.read(1024 * 1024):
                    if cancel_check and cancel_check():
                        raise LifecycleError("setup cancelled")
                    digest.update(chunk)
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        raise LifecycleError("download exceeds the configured size limit")
                    percent = 15 + int((downloaded / total) * 45) if total else 35
                    if progress:
                        bounded_percent = min(60, percent)
                        now = time.monotonic()
                        if bounded_percent != last_reported_percent or now - last_reported_at >= 1.0:
                            progress(
                                "download_model", bounded_percent,
                                "Downloading verified model data", downloaded, total,
                            )
                            last_reported_percent = bounded_percent
                            last_reported_at = now
    except LifecycleError:
        raise
    except OSError as exc:
        raise LifecycleError("download interrupted; retry will resume when supported") from exc
    actual = digest.hexdigest()
    if actual != expected_sha256.lower():
        temporary.unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)
        raise LifecycleError("download checksum mismatch; the partial file was discarded")
    temporary.replace(destination)
    metadata_path.unlink(missing_ok=True)
    return actual


def _native_runtime_spec() -> dict[str, str]:
    spec = NATIVE_RUNTIME_SPECS.get(platform_id())
    if not spec:
        raise LifecycleError("a reviewed native runtime is not available for this platform")
    return dict(spec)


def _safe_extract_runtime(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()

    def safe_target(name: str) -> Path:
        target = (root / name).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise LifecycleError("runtime archive contains an unsafe path") from exc
        return target

    try:
        if archive.suffix.lower() == ".zip":
            with zipfile.ZipFile(archive) as bundle:
                for entry in bundle.infolist():
                    safe_target(entry.filename)
                bundle.extractall(root)
        elif archive.name.endswith(".tar.gz"):
            with tarfile.open(archive, "r:gz") as bundle:
                for entry in bundle.getmembers():
                    safe_target(entry.name)
                    if entry.issym() or entry.islnk():
                        raise LifecycleError("runtime archive contains an unsupported link")
                bundle.extractall(root, filter="data")
        else:
            raise LifecycleError("runtime archive format is not supported")
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise LifecycleError("the verified runtime archive could not be extracted") from exc
    executable_name = _native_runtime_spec()["executable"]
    executable = next((item for item in root.rglob(executable_name) if item.is_file()), None)
    if executable is None:
        raise LifecycleError("the verified runtime archive is missing llama-server")
    if os.name != "nt":
        executable.chmod(executable.stat().st_mode | 0o700)
    return executable


def _prepare_native_runtime(
    base: Path,
    *,
    progress: ProgressCallback | None,
    cancel_check: CancelCheck | None,
) -> Path:
    spec = _native_runtime_spec()
    archive_name = spec["archive"]
    runtime_root = base / "runtimes" / NATIVE_RUNTIME_VERSION / platform_id()
    executable = next(
        (item for item in runtime_root.rglob(spec["executable"]) if item.is_file()), None,
    ) if runtime_root.exists() else None
    if executable:
        _progress(progress, cancel_check, "download_runtime", 68, "Verified runtime already present")
        return executable
    archive = base / "downloads" / archive_name
    runtime_url = (
        f"https://github.com/ggml-org/llama.cpp/releases/download/"
        f"{NATIVE_RUNTIME_VERSION}/{archive_name}"
    )

    def runtime_progress(_stage: str, percent: int, _message: str, *counts: int) -> None:
        mapped = 61 + max(0, min(12, int((percent - 15) * 12 / 45)))
        if progress:
            progress("download_runtime", mapped, "Downloading verified native runtime", *counts)

    _download(
        runtime_url,
        archive,
        spec["sha256"],
        progress=runtime_progress,
        cancel_check=cancel_check,
        max_bytes=512 * 1024**2,
    )
    _progress(progress, cancel_check, "extract_runtime", 75, "Preparing verified native runtime")
    temporary_root = runtime_root.with_name(runtime_root.name + ".preparing")
    if temporary_root.exists():
        shutil.rmtree(temporary_root)
    executable = _safe_extract_runtime(archive, temporary_root)
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    temporary_root.replace(runtime_root)
    relative = executable.relative_to(temporary_root)
    executable = runtime_root / relative
    manifest = {
        "version": "ryn.local-ai-runtime.v1",
        "runtime": "llama.cpp",
        "release": NATIVE_RUNTIME_VERSION,
        "platform": platform_id(),
        "archive": archive_name,
        "sha256": spec["sha256"],
        "license": "MIT",
    }
    (runtime_root / "ryn-runtime.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8",
    )
    return executable


def _native_state(manifest: LLMPackageManifest) -> dict[str, Any]:
    process = _NATIVE_PROCESSES.get(manifest.package_id)
    running = bool(process and process.poll() is None) or _owned_native_pid(manifest) is not None
    health: dict[str, Any] = {}
    try:
        health = adapter_from_manifest(manifest).health()
        running = running or bool(health.get("ok"))
    except Exception:  # noqa: BLE001
        health = {"ok": False}
    return {
        "managed": True,
        "kind": "native_llama_cpp",
        "installed": bool(manifest.runtime_command and Path(manifest.runtime_command[0]).is_file()),
        "running": running,
        "status": "running" if running else "stopped",
        "health": {"ok": bool(health.get("ok"))},
    }


def _native_pid_path(manifest: LLMPackageManifest) -> Path:
    return Path(manifest.runtime_dir) / "ryn-runtime.pid.json"


def _process_executable(pid: int) -> str:
    if pid <= 0:
        return ""
    if os.name == "nt":
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return ""
        try:
            capacity = ctypes.c_ulong(32768)
            buffer = ctypes.create_unicode_buffer(capacity.value)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(
                process, 0, buffer, ctypes.byref(capacity),
            ):
                return buffer.value
            return ""
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _owned_native_pid(manifest: LLMPackageManifest) -> int | None:
    pid_path = _native_pid_path(manifest)
    try:
        record = json.loads(pid_path.read_text(encoding="utf-8"))
        pid = int(record.get("pid") or 0)
        expected = str(Path(manifest.runtime_command[0]).resolve())
        if str(record.get("executable") or "") != expected:
            return None
        actual = _process_executable(pid)
        if actual and Path(actual).resolve() == Path(expected).resolve():
            return pid
    except (OSError, ValueError, TypeError, json.JSONDecodeError, IndexError):
        return None
    return None


def _terminate_owned_native_pid(manifest: LLMPackageManifest, pid: int) -> None:
    if _owned_native_pid(manifest) != pid:
        raise LifecycleError("refusing to stop a process that is not the verified Ryn runtime")
    if os.name == "nt":
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x0001 | 0x00100000, False, pid)
        if not process:
            return
        try:
            if not ctypes.windll.kernel32.TerminateProcess(process, 0):
                raise LifecycleError("the verified native runtime could not be stopped")
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    else:
        import signal

        os.kill(pid, signal.SIGTERM)


def _run_native(manifest: LLMPackageManifest) -> None:
    if not manifest.runtime_command:
        raise LifecycleError("native runtime command is missing")
    executable = Path(manifest.runtime_command[0]).resolve()
    runtime_root = Path(manifest.runtime_dir).resolve()
    try:
        executable.relative_to(runtime_root)
    except ValueError as exc:
        raise LifecycleError("native runtime executable escaped its verified directory") from exc
    if not executable.is_file():
        raise LifecycleError("verified native runtime executable is missing")
    model = Path(manifest.model_path).resolve()
    if not model.is_file() or fingerprint_file(model) != manifest.checksum:
        raise LifecycleError("configured model checksum no longer matches; refusing to start")
    existing = _NATIVE_PROCESSES.get(manifest.package_id)
    if existing and existing.poll() is None:
        return
    if _owned_native_pid(manifest) is not None:
        return
    try:
        if adapter_from_manifest(manifest).health().get("ok"):
            raise LifecycleError("the configured loopback port is already in use by another model service")
    except AdapterError:
        pass
    log_path = runtime_root / "ryn-runtime.log"
    if log_path.exists() and log_path.stat().st_size > 1024 * 1024:
        log_path.replace(log_path.with_suffix(".log.1"))
    command = list(manifest.runtime_command)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    with log_path.open("ab") as log_handle:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=runtime_root,
            creationflags=creationflags,
        )
    _NATIVE_PROCESSES[manifest.package_id] = process
    pid_path = _native_pid_path(manifest)
    temporary_pid = pid_path.with_suffix(".tmp")
    temporary_pid.write_text(
        json.dumps({
            "version": "ryn.local-ai-runtime-pid.v1",
            "pid": process.pid,
            "executable": str(executable),
        }, sort_keys=True),
        encoding="utf-8",
    )
    temporary_pid.replace(pid_path)


def _stop_native(manifest: LLMPackageManifest) -> dict[str, Any]:
    process = _NATIVE_PROCESSES.pop(manifest.package_id, None)
    stopped = False
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        stopped = True
    else:
        pid = _owned_native_pid(manifest)
        if pid is not None:
            _terminate_owned_native_pid(manifest, pid)
            stopped = True
    _native_pid_path(manifest).unlink(missing_ok=True)
    return {"managed": True, "stopped": stopped,
            "message": "runtime stopped" if stopped else "verified runtime is not running"}


def _docker_pull(
    image: str,
    *,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    timeout_s: float = 600,
) -> None:
    docker = _docker()
    process = subprocess.Popen(
        [docker, "pull", image],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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
                       capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LifecycleError("Docker is installed but its engine is not running") from exc
    return executable


def _container_name(package_id: str) -> str:
    return "rynmesh-llm-" + re.sub(r"[^a-z0-9_.-]", "-", package_id.lower())


def _pinned_runtime_image(manifest: LLMPackageManifest | None = None) -> str:
    image = str((manifest.install_source if manifest else {}).get("runtime_image") or DEFAULT_IMAGE)
    if image == "ghcr.io/ggml-org/llama.cpp:server":
        # Migrate manifests created by the pre-pinning preview to the reviewed
        # digest without ever pulling or running the mutable legacy tag.
        return DEFAULT_IMAGE
    if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image):
        raise LifecycleError("managed runtime image must be pinned by SHA-256 digest")
    return image


def _docker_state(package_id: str) -> dict[str, Any]:
    docker = shutil.which("docker")
    if not docker:
        return {"installed": False, "running": False, "status": "docker unavailable"}
    result = subprocess.run(
        [docker, "inspect", "--format", "{{json .State}}", _container_name(package_id)],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode:
        return {"installed": False, "running": False, "status": "not created"}
    try:
        state = json.loads(result.stdout)
    except json.JSONDecodeError:
        state = {}
    return {"installed": True, "running": bool(state.get("Running")),
            "status": str(state.get("Status") or "unknown"), "exit_code": state.get("ExitCode")}


def _run_container(manifest: LLMPackageManifest) -> None:
    docker = _docker()
    model = Path(manifest.model_path).resolve()
    if not model.is_file():
        raise LifecycleError("configured model file is missing")
    if manifest.checksum and fingerprint_file(model) != manifest.checksum:
        raise LifecycleError("configured model checksum no longer matches; refusing to start")
    name = _container_name(manifest.package_id)
    subprocess.run([docker, "rm", "-f", name], capture_output=True, timeout=30)
    port = int(urlparse(manifest.base_url).port or 8080)
    runtime_image = _pinned_runtime_image(manifest)
    command = [
        docker, "run", "-d", "--name", name, "--read-only", "--tmpfs", "/tmp:size=128m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "256",
        "-p", f"127.0.0.1:{port}:8080", "-v", f"{model.parent}:/models:ro", runtime_image,
        "-m", f"/models/{model.name}", "--host", "0.0.0.0", "--port", "8080",
        "--alias", manifest.public_model_alias, "-c", str(manifest.context_window),
        "-np", str(manifest.max_concurrent),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise LifecycleError("llama.cpp container failed to start (inspect Docker logs for details)")


def wait_healthy(manifest: LLMPackageManifest, *, timeout_s: float = 120) -> dict[str, Any]:
    adapter = adapter_from_manifest(manifest)
    deadline = time.monotonic() + timeout_s
    latest: dict[str, Any] = {"ok": False, "error": "not started"}
    while time.monotonic() < deadline:
        latest = adapter.health()
        if latest.get("ok"):
            return latest
        time.sleep(1)
    raise LifecycleError(f"runtime did not become healthy: {latest.get('error', 'unknown error')}")


def self_test(manifest: LLMPackageManifest) -> dict[str, Any]:
    adapter = adapter_from_manifest(manifest)
    health = adapter.health()
    if not health.get("ok"):
        raise LifecycleError(f"health check failed: {health.get('error', 'unknown error')}")
    result = adapter.infer(
        prompt="Reply with exactly: RYNMESH SELF TEST OK", max_tokens=64,
        task_id="selftest", timeout_s=manifest.timeout_seconds,
    )
    text = str(result.pop("text", ""))
    if not text.strip():
        raise LifecycleError("real inference self-test returned empty output")
    return {"ok": True, "output_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "output_preview": text[:120], **result}


def install_managed(*, package_id: str = "local-small", root: str | Path | None = None,
                    port: int = 18080, model_url: str = DEFAULT_MODEL_URL,
                    expected_sha256: str = DEFAULT_MODEL_SHA256, accept_risk: bool = False,
                    progress: ProgressCallback | None = None,
                    cancel_check: CancelCheck | None = None) -> dict[str, Any]:
    package_id = _validate_package_id(package_id)
    base = Path(root or default_root()).expanduser()
    _progress(progress, cancel_check, "hardware", 5, "Checking available hardware")
    report = detect_hardware(base)
    choices = recommend(report)
    if not choices[0].get("can_run") and not accept_risk:
        raise LifecycleError(str(choices[0].get("reason")))
    _progress(progress, cancel_check, "runtime_check", 10, "Checking Docker runtime")
    _docker()
    _progress(progress, cancel_check, "checksum", 12, "Verifying model source metadata")
    digest = expected_sha256.lower()
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise LifecycleError("managed model install requires a pinned SHA-256 digest")
    model_dir = base / "models" / package_id
    model_path = model_dir / "model.gguf"
    if not model_path.exists() or fingerprint_file(model_path) != "sha256:" + digest:
        _download(
            model_url, model_path, digest, progress=progress, cancel_check=cancel_check,
        )
    else:
        _progress(progress, cancel_check, "download_model", 60, "Verified model already present")
    selected = choices[0] if choices[0].get("can_run") else {
        "context_window": 2048, "max_concurrent": 1, "estimated_memory_mb": 1024,
    }
    _progress(progress, cancel_check, "pull_runtime", 65, "Preparing the local inference runtime")
    _docker_pull(DEFAULT_IMAGE, progress=progress, cancel_check=cancel_check)
    manifest = LLMPackageManifest(
        package_id=package_id, mode="managed", public_model_alias="rynmesh-qwen2.5-0.5b-q4",
        adapter="openai_compatible", runtime="docker_llama_cpp", model="rynmesh-qwen2.5-0.5b-q4",
        model_path=str(model_path), model_owned=True, base_url=f"http://127.0.0.1:{port}",
        checksum="sha256:" + digest, model_fingerprint="sha256:" + digest,
        context_window=int(selected["context_window"]), max_output_tokens=256,
        max_concurrent=int(selected["max_concurrent"]),
        hardware_requirements={"estimated_memory_mb": int(selected["estimated_memory_mb"])},
        install_source={"model_url": model_url, "runtime_image": DEFAULT_IMAGE},
        license_notice="Qwen2.5 0.5B and llama.cpp are Apache-2.0; review their notices before use.",
        license_id="Apache-2.0",
        lifecycle={"start": "rynmesh-llm start", "stop": "rynmesh-llm stop",
                   "restart": "rynmesh-llm restart", "status": "rynmesh-llm status"},
    )
    _progress(progress, cancel_check, "start_runtime", 82, "Starting the local model runtime")
    _run_container(manifest)
    _progress(progress, cancel_check, "health_check", 90, "Waiting for the model health check")
    wait_healthy(manifest)
    _progress(progress, cancel_check, "self_test", 96, "Running a real private inference self-test")
    result = self_test(manifest)
    save_manifest(manifest, manifest_path(package_id, base))
    _progress(progress, cancel_check, "completed", 100, "Local model is ready")
    return {"manifest": str(manifest_path(package_id, base)), "hardware": report.to_dict(),
            "recommendation": selected, "self_test": result}


def install_native_managed(
    *,
    profile: str = "light",
    package_id: str = "local-small",
    root: str | Path | None = None,
    port: int = 18080,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, Any]:
    """Install a reviewed llama.cpp binary and model without Docker or a shell."""
    package_id = _validate_package_id(package_id)
    if profile != "light":
        raise LifecycleError("the selected model profile is not published in this build")
    base = Path(root or default_root()).expanduser().resolve()
    _progress(progress, cancel_check, "hardware", 4, "Checking available memory and disk")
    report = detect_hardware(base)
    catalog = setup_recommendations(report)
    selected = next((item for item in catalog["profiles"] if item["id"] == profile), None)
    if not selected or not selected["supported"]:
        raise LifecycleError(
            str((selected or {}).get("unsupported_reason") or "the selected profile is not supported")
        )
    digest = DEFAULT_MODEL_SHA256.lower()
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise LifecycleError("managed model install requires a pinned SHA-256 digest")
    model_dir = base / "models" / package_id
    model_path = model_dir / "model.gguf"
    _progress(progress, cancel_check, "verify_manifest", 9, "Checking reviewed model and runtime manifests")
    if not model_path.exists() or fingerprint_file(model_path) != "sha256:" + digest:
        _download(
            DEFAULT_MODEL_URL,
            model_path,
            digest,
            progress=progress,
            cancel_check=cancel_check,
            max_bytes=2 * 1024**3,
        )
    else:
        _progress(progress, cancel_check, "download_model", 60, "Verified model already present")
    executable = _prepare_native_runtime(
        base, progress=progress, cancel_check=cancel_check,
    )
    runtime_root = executable.parent
    alias = str(selected["alias"])
    command = [
        str(executable),
        "--model", str(model_path),
        "--host", "127.0.0.1",
        "--port", str(port),
        "--alias", alias,
        "--ctx-size", str(selected["context_window"]),
        "--parallel", "1",
        "--no-webui",
    ]
    manifest = LLMPackageManifest(
        package_id=package_id,
        mode="managed",
        public_model_alias=alias,
        adapter="openai_compatible",
        runtime="native_llama_cpp",
        model=alias,
        model_path=str(model_path),
        model_owned=True,
        base_url=f"http://127.0.0.1:{port}",
        checksum="sha256:" + digest,
        model_fingerprint="sha256:" + digest,
        context_window=int(selected["context_window"]),
        max_output_tokens=256,
        max_concurrent=1,
        hardware_requirements={
            "profile": profile,
            "estimated_memory_mb": int(selected["estimated_memory_mib"]),
        },
        install_source={
            "model_url": DEFAULT_MODEL_URL,
            "model_revision": DEFAULT_MODEL_REVISION,
            "runtime_release": NATIVE_RUNTIME_VERSION,
            "runtime_archive_sha256": _native_runtime_spec()["sha256"],
        },
        license_notice="Qwen2.5 0.5B is Apache-2.0; llama.cpp is MIT.",
        license_id="Apache-2.0 AND MIT",
        lifecycle={"manager": "rynmesh-native-runtime-v1"},
        runtime_command=command,
        runtime_dir=str(runtime_root),
    )
    _progress(progress, cancel_check, "start_runtime", 82, "Starting private local AI on loopback")
    _run_native(manifest)
    try:
        _progress(progress, cancel_check, "health_check", 90, "Waiting for local AI readiness")
        wait_healthy(manifest)
        _progress(progress, cancel_check, "self_test", 96, "Running a private first-answer test")
        result = self_test(manifest)
    except Exception:
        _stop_native(manifest)
        raise
    target = manifest_path(package_id, base)
    save_manifest(manifest, target)
    _progress(progress, cancel_check, "completed", 100, "Private local AI is ready")
    return {
        "manifest": str(target),
        "hardware": catalog,
        "recommendation": selected,
        "self_test": result,
    }


def import_gguf(*, source: str | Path, package_id: str, alias: str,
                root: str | Path | None = None, port: int = 18080,
                accept_risk: bool = False, progress: ProgressCallback | None = None,
                cancel_check: CancelCheck | None = None) -> dict[str, Any]:
    package_id = _validate_package_id(package_id)
    _progress(progress, cancel_check, "validate_model", 10, "Validating the GGUF file in place")
    details = validate_gguf(source, allow_risk=accept_risk)
    _progress(progress, cancel_check, "runtime_check", 25, "Checking Docker runtime")
    _docker()
    _progress(progress, cancel_check, "pull_runtime", 40, "Preparing the local inference runtime")
    _docker_pull(DEFAULT_IMAGE, progress=progress, cancel_check=cancel_check)
    manifest = LLMPackageManifest(
        package_id=package_id, mode="import_gguf", public_model_alias=alias,
        adapter="openai_compatible", runtime="docker_llama_cpp", model=alias,
        model_path=details["path"], model_owned=False, base_url=f"http://127.0.0.1:{port}",
        checksum=details["fingerprint"], model_fingerprint=details["fingerprint"],
        hardware_requirements={"estimated_memory_mb": details["estimated_memory_mb"]},
        install_source={"kind": "user_owned_read_only_gguf", "runtime_image": DEFAULT_IMAGE},
    )
    path = manifest_path(package_id, root)
    _progress(progress, cancel_check, "start_runtime", 80, "Starting the read-only GGUF runtime")
    _run_container(manifest)
    _progress(progress, cancel_check, "health_check", 90, "Waiting for the model health check")
    wait_healthy(manifest)
    _progress(progress, cancel_check, "self_test", 96, "Running a real private inference self-test")
    result = self_test(manifest)
    save_manifest(manifest, path)
    _progress(progress, cancel_check, "completed", 100, "Imported model is ready")
    return {"manifest": str(path), "import": {k: v for k, v in details.items() if k != "path"},
            "self_test": result}


def connect_local_api(*, base_url: str, package_id: str, alias: str, model: str = "",
                      api_key_env: str = "", adapter: str = "openai_compatible",
                      root: str | Path | None = None, allow_non_loopback: bool = False,
                      progress: ProgressCallback | None = None,
                      cancel_check: CancelCheck | None = None) -> dict[str, Any]:
    package_id = _validate_package_id(package_id)
    if api_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
        raise LifecycleError(
            "API key setting must be an environment-variable name, never the secret value"
        )
    _progress(progress, cancel_check, "connect", 20, "Checking the local model API")
    manifest = LLMPackageManifest(
        package_id=package_id, mode="ollama" if adapter == "ollama" else "openai_compatible",
        public_model_alias=alias, adapter=adapter, runtime="external", base_url=base_url,
        api_key_env=api_key_env, model=model, allow_non_loopback=allow_non_loopback,
        install_source={"kind": "existing_local_service"},
    )
    active = adapter_from_manifest(manifest)
    health = active.health()
    if not health.get("ok"):
        raise LifecycleError(f"local API health check failed: {health.get('error', 'unknown error')}")
    if not manifest.model:
        manifest.model = str(health.get("model") or "")
    capabilities = active.capabilities()
    if capabilities.get("streaming") and "streaming" not in manifest.capabilities:
        manifest.capabilities.append("streaming")
    _progress(progress, cancel_check, "self_test", 75, "Running a real private inference self-test")
    result = self_test(manifest)
    path = manifest_path(package_id, root)
    save_manifest(manifest, path)
    _progress(progress, cancel_check, "completed", 100, "Local API connection is ready")
    return {"manifest": str(path), "health": health, "capabilities": capabilities,
            "self_test": result}


def start(path: str | Path) -> dict[str, Any]:
    manifest = load_manifest(path)
    if manifest.runtime == "external":
        return {"managed": False, "health": adapter_from_manifest(manifest).health()}
    if manifest.runtime == "native_llama_cpp":
        _run_native(manifest)
        return {"managed": True, "kind": "native_llama_cpp", "health": wait_healthy(manifest)}
    _run_container(manifest)
    return {"managed": True, "health": wait_healthy(manifest)}


def stop(path: str | Path) -> dict[str, Any]:
    manifest = load_manifest(path)
    if manifest.runtime == "external":
        return {"managed": False, "stopped": False, "message": "external service is owner-managed"}
    if manifest.runtime == "native_llama_cpp":
        return _stop_native(manifest)
    result = subprocess.run([_docker(), "stop", _container_name(manifest.package_id)],
                            capture_output=True, text=True, timeout=60)
    return {"managed": True, "stopped": result.returncode == 0}


def restart(path: str | Path) -> dict[str, Any]:
    stop(path)
    return start(path)


def update(path: str | Path) -> dict[str, Any]:
    """Update the managed runtime without silently replacing/deleting models."""
    manifest = load_manifest(path)
    if manifest.runtime == "external":
        return {"managed": False, "updated": False, "message": "external runtime is owner-managed",
                "self_test": self_test(manifest)}
    if manifest.runtime == "native_llama_cpp":
        restarted = restart(path)
        return {"managed": True, "updated": False, "message": "Pinned runtime is current",
                "runtime": restarted, "model_preserved": True, "self_test": self_test(manifest)}
    subprocess.run([_docker(), "pull", _pinned_runtime_image(manifest)], check=True, timeout=600)
    restarted = restart(path)
    return {"managed": True, "updated": True, "runtime": restarted,
            "model_preserved": True, "self_test": self_test(manifest)}


def status(path: str | Path) -> dict[str, Any]:
    manifest = load_manifest(path)
    return {"package_id": manifest.package_id, "mode": manifest.mode,
            "runtime": _native_state(manifest) if manifest.runtime == "native_llama_cpp"
            else _docker_state(manifest.package_id) if manifest.runtime != "external"
            else {"managed": False},
            "health": adapter_from_manifest(manifest).health(), "public": manifest.public_dict()}


def uninstall(path: str | Path, *, delete_environment: bool = True,
              delete_model: bool = False, confirm_model_delete: bool = False) -> dict[str, Any]:
    manifest_file = Path(path).expanduser()
    manifest = load_manifest(manifest_file)
    removed = []
    if delete_environment and manifest.runtime != "external":
        if manifest.runtime == "native_llama_cpp":
            _stop_native(manifest)
            runtime_dir = Path(manifest.runtime_dir).resolve()
            owned_runtime_root = manifest_file.resolve().parents[2] / "runtimes"
            try:
                runtime_dir.relative_to(owned_runtime_root.resolve())
            except ValueError as exc:
                raise LifecycleError("native runtime path escaped the owned runtime directory") from exc
            shutil.rmtree(runtime_dir, ignore_errors=True)
            removed.append("native_runtime")
        else:
            subprocess.run([_docker(), "rm", "-f", _container_name(manifest.package_id)],
                           capture_output=True, timeout=60)
            removed.append("runtime_container")
    if delete_model:
        if not confirm_model_delete:
            raise LifecycleError("model deletion needs separate explicit confirmation")
        if not manifest.model_owned:
            raise LifecycleError("Rynmesh will not delete an imported/user-owned model")
        model = Path(manifest.model_path).resolve()
        owned_root = manifest_file.resolve().parents[2] / "models"
        try:
            model.relative_to(owned_root)
        except ValueError as exc:
            raise LifecycleError("managed model path escaped the owned model directory") from exc
        model.unlink(missing_ok=True)
        removed.append("managed_model")
    return {"removed": removed, "model_preserved": "managed_model" not in removed,
            "user_owned_model": not manifest.model_owned,
            "private_manifest_preserved": manifest_file.exists()}
