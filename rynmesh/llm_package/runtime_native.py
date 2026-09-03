"""Native llama.cpp runtime backend: `llama-server` as a loopback child process.

Mirrors the backend surface of `runtime_docker.py` (`RUNTIME_ID`, `available`,
`prepare`, `start`, `stop`, `remove`, `update`, `state`, `install_source`) so
`lifecycle._backend` can dispatch on `manifest.runtime`. This is the default
managed runtime on consumer desktops, which cannot run Docker (issue #34).

Privacy rule for everything in this module: no filesystem path, prompt, or
model output may appear in a raised message, in `state()`, or in anything the
node itself writes to a log.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from .adapters import AdapterError, adapter_from_manifest
from .errors import LifecycleError
from .manifest import LLMPackageManifest, fingerprint_file

RUNTIME_ID = "native_llama_cpp"

RUNTIME_RELEASE = "b10774"  # ggml-org/llama.cpp, 2026-09-03
RUNTIME_BASE_URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{RUNTIME_RELEASE}/"
# (platform.system(), normalized machine) -> (asset, sha256, size_bytes)
RUNTIME_ASSETS: dict[tuple[str, str], tuple[str, str, int]] = {
    ("Darwin", "arm64"): ("llama-b10774-bin-macos-arm64.tar.gz",
        "aeb59ccd60191bdf96ddb57f352286430d9e0b6dc29281460e1bd217556c3c78", 11088473),
    ("Darwin", "x86_64"): ("llama-b10774-bin-macos-x64.tar.gz",
        "9acda4c44584970622ecc06086b5b8cd3e06b6dc0d039f8c95a63f1186f80e5e", 11146110),
    ("Linux", "x86_64"): ("llama-b10774-bin-ubuntu-x64.tar.gz",
        "68caa9c0e6dcdf32283fc4d8a0008fe389cb191bb79f5fa34c255beec388046d", 16718077),
    ("Linux", "arm64"): ("llama-b10774-bin-ubuntu-arm64.tar.gz",
        "eeb67bd32e163d09687e7a7a8bc25119bb2cbc637d5ec2b45c493c7df1675452", 13363523),
    ("Windows", "x86_64"): ("llama-b10774-bin-win-cpu-x64.zip",
        "04f25dd148fda9d66efd91e96c637126220273fec62cdf5007367722c11bc744", 18380733),
    ("Windows", "arm64"): ("llama-b10774-bin-win-cpu-arm64.zip",
        "a26b288a4a9b1d9163a171e6b834b4f2956f72fcb51d8b75560565939b4a755f", 11949368),
}

MARKER_NAME = "runtime.json"
MAX_EXTRACTED_BYTES = 200 * 2**20
STARTUP_GRACE_SECONDS = 2.0
STOP_GRACE_SECONDS = 10.0

# Child handles owned by this process, so `stop()`/`state()` can reap an exited
# server instead of reading a zombie pid as "still running".
_CHILDREN: dict[int, subprocess.Popen] = {}


# --------------------------------------------------------------------------
# Platform + binary resolution
# --------------------------------------------------------------------------

def _default_root() -> Path:
    # Imported lazily: `lifecycle` imports this module at import time.
    from .lifecycle import default_root

    return default_root()


def _machine() -> str:
    machine = platform.machine().strip().lower()
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    if machine in {"amd64", "x86_64"}:
        return "x86_64"
    return machine


def _asset() -> tuple[str, str, int] | None:
    return RUNTIME_ASSETS.get((platform.system(), _machine()))


def server_filename() -> str:
    return "llama-server.exe" if platform.system() == "Windows" else "llama-server"


def _regular_file(path: Path) -> Path | None:
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def _in_directory(directory: Path) -> Path | None:
    """Find the server in `directory`, allowing the release `build/bin` layout."""
    name = server_filename()
    for candidate in (directory / name, directory / "build" / "bin" / name):
        found = _regular_file(candidate)
        if found is not None:
            return found
    return None


def _managed_root(root: Path | str) -> Path:
    return Path(root).expanduser() / "runtime" / f"llama-{RUNTIME_RELEASE}"


def _marker_server(base: Path) -> Path | None:
    """Server recorded by a completed managed download, if the marker is sane."""
    try:
        marker = json.loads((base / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    relative = str(marker.get("server") or "") if isinstance(marker, dict) else ""
    if not relative:
        return None
    parts = PurePosixPath(relative)
    if parts.is_absolute() or ".." in parts.parts:
        return None
    return _regular_file(base / parts)


def resolve_server(root: Path | str | None = None) -> Path | None:
    """First usable `llama-server`, in the documented preference order."""
    explicit = os.environ.get("RYNMESH_LLAMA_SERVER", "").strip()
    if explicit:
        found = _regular_file(Path(explicit).expanduser())
        if found is not None:
            return found
    bundled = os.environ.get("RYNMESH_LLAMA_DIR", "").strip()
    if bundled:
        found = _in_directory(Path(bundled).expanduser())
        if found is not None:
            return found
    if getattr(sys, "frozen", False):
        found = _in_directory(Path(sys.executable).parent / "llama")
        if found is not None:
            return found
    base = _managed_root(root if root is not None else _default_root())
    found = _marker_server(base) or _in_directory(base)
    if found is not None:
        return found
    on_path = shutil.which(server_filename())
    return Path(on_path) if on_path else None


def available() -> tuple[bool, str]:
    """(True, "") when a server is resolvable or downloadable; else a safe reason."""
    if resolve_server() is not None or _asset() is not None:
        return True, ""
    return False, (
        "no bundled inference runtime is available for this platform; "
        "connect an existing local API or install Docker instead"
    )


# --------------------------------------------------------------------------
# Managed runtime download
# --------------------------------------------------------------------------

def _report(progress: Any, cancel_check: Any, percent: int, message: str) -> None:
    if cancel_check and cancel_check():
        raise LifecycleError("setup cancelled")
    if progress:
        progress("pull_runtime", percent, message)


def _fetch(url: str, destination: Path, expected_sha256: str, size_bytes: int, *,
           progress: Any = None, cancel_check: Any = None) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise LifecycleError("runtime downloads require an HTTPS URL")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.sha256()
    downloaded = 0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Rynmesh/0.6"})
        with urllib.request.urlopen(request, timeout=300) as response, temporary.open("wb") as handle:
            while chunk := response.read(1024 * 1024):
                if cancel_check and cancel_check():
                    raise LifecycleError("setup cancelled")
                downloaded += len(chunk)
                if downloaded > size_bytes:
                    raise LifecycleError("runtime archive is larger than its pinned size")
                digest.update(chunk)
                handle.write(chunk)
                percent = min(80, 65 + int(downloaded / size_bytes * 15))
                _report(progress, None, percent, "Downloading the local inference runtime")
    except LifecycleError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise LifecycleError("downloading the local inference runtime failed") from exc
    if digest.hexdigest() != expected_sha256.lower():
        temporary.unlink(missing_ok=True)
        raise LifecycleError("runtime archive checksum mismatch")
    temporary.replace(destination)


def _check_member(name: str) -> None:
    if not name or name.startswith(("/", "\\")) or "\\" in name or (len(name) > 1 and name[1] == ":"):
        raise LifecycleError("runtime archive contains an unsafe member path")
    if ".." in PurePosixPath(name).parts:
        raise LifecycleError("runtime archive contains an unsafe member path")


def _check_total(total: int) -> int:
    if total > MAX_EXTRACTED_BYTES:
        raise LifecycleError("runtime archive expands past the extraction size limit")
    return total


def _extract_tar(archive: Path, target: Path) -> None:
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        total = 0
        for member in members:
            _check_member(member.name)
            if not (member.isfile() or member.isdir()):
                raise LifecycleError("runtime archive contains an unsafe member type")
            total = _check_total(total + max(0, int(member.size)))
        bundle.extractall(target, members=members, filter="data")


def _extract_zip(archive: Path, target: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        total = 0
        for entry in entries:
            _check_member(entry.filename)
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise LifecycleError("runtime archive contains an unsafe member type")
            total = _check_total(total + max(0, int(entry.file_size)))
        bundle.extractall(target, members=[entry.filename for entry in entries])


def _extract(archive: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(target, 0o700)
    if archive.name.endswith(".zip"):
        _extract_zip(archive, target)
    else:
        _extract_tar(archive, target)


def _write_marker(target: Path, server: Path, expected_sha256: str) -> None:
    payload = {"release": RUNTIME_RELEASE, "server": server.relative_to(target).as_posix(),
               "sha256": expected_sha256.lower()}
    temporary = target / (MARKER_NAME + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, target / MARKER_NAME)


def prepare(*, progress: Any = None, cancel_check: Any = None,
            root: Path | str | None = None) -> None:
    """Make a `llama-server` available, downloading the pinned release if needed."""
    base = Path(root).expanduser() if root is not None else _default_root()
    if resolve_server(base) is not None:
        _report(progress, cancel_check, 72, "Local inference runtime already present")
        return
    asset = _asset()
    if asset is None:
        raise LifecycleError(available()[1])
    name, expected_sha256, size_bytes = asset
    _report(progress, cancel_check, 65, "Downloading the local inference runtime")
    archive = base / "runtime" / name
    _fetch(RUNTIME_BASE_URL + name, archive, expected_sha256, size_bytes,
           progress=progress, cancel_check=cancel_check)
    target = _managed_root(base)
    try:
        _extract(archive, target)
    finally:
        archive.unlink(missing_ok=True)
    server = _in_directory(target)
    if server is None:
        raise LifecycleError("the runtime archive did not contain an inference server")
    if os.name != "nt":
        server.chmod(0o755)
    _write_marker(target, server, expected_sha256)
    _report(progress, cancel_check, 80, "Local inference runtime installed")


# --------------------------------------------------------------------------
# Process control
# --------------------------------------------------------------------------

def _runtime_root(manifest: LLMPackageManifest) -> Path:
    return Path(manifest.runtime_dir).expanduser() if manifest.runtime_dir else _default_root()


def _pid_path(root: Path, package_id: str) -> Path:
    return root / "runtime" / f"{package_id}.pid"


def _log_path(root: Path, package_id: str) -> Path:
    return root / "runtime" / f"{package_id}.log"


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(str(pid))


def _alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    child = _CHILDREN.get(pid)
    if child is not None and child.poll() is not None:
        _CHILDREN.pop(pid, None)
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                    capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return True  # Unknown: assume alive rather than spawn a second server.
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def _endpoint_serves_alias(manifest: LLMPackageManifest) -> bool:
    """True when something already answers on the port with this model alias."""
    try:
        health = adapter_from_manifest(manifest).health()
    except AdapterError:
        return False
    expected = manifest.model or manifest.public_model_alias
    return bool(health.get("ok")) and str(health.get("model") or "") == expected


def _spawn(server: Path, command: list[str], log: Path, port: int) -> subprocess.Popen:
    log.parent.mkdir(parents=True, exist_ok=True)
    detach: dict[str, Any] = {"start_new_session": True}
    if os.name == "nt":
        detach = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    # Truncate mode: the log starts empty on every start, so it stays bounded.
    with log.open("wb") as handle:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
            cwd=str(server.parent), **detach,
        )
    deadline = time.monotonic() + STARTUP_GRACE_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LifecycleError("llama-server exited during startup (see the runtime log)")
        if _port_open(port):
            break
        time.sleep(0.05)
    return process


def start(manifest: LLMPackageManifest) -> None:
    root = _runtime_root(manifest)
    server = resolve_server(root)
    if server is None:
        raise LifecycleError("the local inference runtime is not installed")
    model = Path(manifest.model_path).resolve()
    if not model.is_file():
        raise LifecycleError("configured model file is missing")
    if manifest.checksum and fingerprint_file(model) != manifest.checksum:
        raise LifecycleError("configured model checksum no longer matches; refusing to start")
    port = int(urlparse(manifest.base_url).port or 8080)
    manifest.runtime_dir = str(root)
    pid_path = _pid_path(root, manifest.package_id)
    if _endpoint_serves_alias(manifest):
        if _alive(_read_pid(pid_path)):
            return  # Already running under this pidfile; never spawn a second server.
        _write_pid(pid_path, 0)  # Adopted: an owner-managed server holds the port.
        return
    command = [
        str(server), "-m", str(model), "--host", "127.0.0.1", "--port", str(port),
        "--alias", manifest.public_model_alias, "-c", str(manifest.context_window),
        "-np", str(manifest.max_concurrent), "--no-webui",
    ]
    process = _spawn(server, command, _log_path(root, manifest.package_id), port)
    _CHILDREN[process.pid] = process
    _write_pid(pid_path, process.pid)
    manifest.runtime_command = command


def _terminate(pid: int, *, force: bool) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", *(["/F"] if force else []), "/PID", str(pid)],
                           capture_output=True, timeout=30)
        else:
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except OSError:
        pass


def stop(manifest: LLMPackageManifest) -> bool:
    root = _runtime_root(manifest)
    pid_path = _pid_path(root, manifest.package_id)
    pid = _read_pid(pid_path)
    if pid == 0:
        return False  # Adopted server: owner-managed, never stopped by Rynmesh.
    if pid is None:
        return False  # No pidfile: this node never started a server for the package.
    if not _alive(pid):
        pid_path.unlink(missing_ok=True)
        _CHILDREN.pop(pid, None)
        return True
    _terminate(pid, force=False)
    deadline = time.monotonic() + STOP_GRACE_SECONDS
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.1)
    if _alive(pid):
        _terminate(pid, force=True)
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        while time.monotonic() < deadline and _alive(pid):
            time.sleep(0.1)
    stopped = not _alive(pid)
    if stopped:
        pid_path.unlink(missing_ok=True)
        _CHILDREN.pop(pid, None)
    return stopped


def remove(manifest: LLMPackageManifest) -> None:
    """Stop this package's server; the runtime dir is shared and stays."""
    stop(manifest)


def update(manifest: LLMPackageManifest) -> None:
    """The release is pinned, so updating only re-verifies what is installed."""
    prepare(root=_runtime_root(manifest))


def state(manifest: LLMPackageManifest) -> dict[str, Any]:
    root = _runtime_root(manifest)
    installed = resolve_server(root) is not None
    pid = _read_pid(_pid_path(root, manifest.package_id))
    running = _alive(pid)
    if pid == 0:
        status = "adopted"
    elif running:
        status = "running"
    else:
        status = "stopped" if installed else "not installed"
    return {"installed": installed, "running": running, "status": status,
            "release": RUNTIME_RELEASE}


def install_source(manifest_or_none: LLMPackageManifest | None = None) -> dict[str, str]:
    return {"kind": RUNTIME_ID, "runtime_release": RUNTIME_RELEASE}
