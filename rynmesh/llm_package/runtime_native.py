"""Native llama.cpp runtime backend: `llama-server` as a loopback child process.

Mirrors the backend surface of `runtime_docker.py` (`RUNTIME_ID`, `available`,
`prepare`, `start`, `stop`, `remove`, `update`, `state`, `install_source`) so
`lifecycle._backend` can dispatch on `manifest.runtime`. This is the default
managed runtime on consumer desktops, which cannot run Docker (issue #34).

This module resolves and runs the server; `runtime_native_install` obtains it.

Privacy rule for everything here: no filesystem path, prompt, or model output
may appear in a raised message, in `state()`, or in anything the node itself
writes to a log.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from .adapters import AdapterError, adapter_from_manifest
from .errors import LifecycleError
from .manifest import LLMPackageManifest, fingerprint_file
from .runtime_native_install import (
    MARKER_NAME,
    RUNTIME_RELEASE,
    UNAVAILABLE_REASON,
    UNWRITABLE_STATE,
    asset,
    download,
    find_server,
    managed_root,
    report,
    server_filename,
    usable_server,
)

RUNTIME_ID = "native_llama_cpp"

STARTUP_GRACE_SECONDS = 2.0
STOP_GRACE_SECONDS = 10.0

# Child handles owned by this process, so `stop()`/`state()` can reap an exited
# server instead of reading a zombie pid as "still running".
_CHILDREN: dict[int, subprocess.Popen] = {}


# --------------------------------------------------------------------------
# Binary resolution
# --------------------------------------------------------------------------

def _default_root() -> Path:
    # Imported lazily: `lifecycle` imports this module at import time.
    from .lifecycle import default_root

    return default_root()


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
    return usable_server(base / parts)


def resolve_server(root: Path | str | None = None) -> Path | None:
    """First usable `llama-server`, in the documented preference order."""
    explicit = os.environ.get("RYNMESH_LLAMA_SERVER", "").strip()
    if explicit:
        found = usable_server(Path(explicit).expanduser())
        if found is not None:
            return found
    bundled = os.environ.get("RYNMESH_LLAMA_DIR", "").strip()
    if bundled:
        found = find_server(Path(bundled).expanduser())
        if found is not None:
            return found
    if getattr(sys, "frozen", False):
        found = find_server(Path(sys.executable).parent / "llama")
        if found is not None:
            return found
    base = managed_root(root if root is not None else _default_root())
    found = _marker_server(base) or find_server(base)
    if found is not None:
        return found
    on_path = shutil.which(server_filename())
    return usable_server(Path(on_path)) if on_path else None


def available(root: Path | str | None = None) -> tuple[bool, str]:
    """(True, "") when a server is resolvable or downloadable; else a safe reason."""
    if resolve_server(root) is not None or asset() is not None:
        return True, ""
    return False, UNAVAILABLE_REASON


def prepare(*, progress: Any = None, cancel_check: Any = None,
            root: Path | str | None = None) -> None:
    """Make a `llama-server` available, downloading the pinned release if needed."""
    base = Path(root).expanduser() if root is not None else _default_root()
    if resolve_server(base) is not None:
        report(progress, cancel_check, 72, "Local inference runtime already present")
        return
    download(base, progress=progress, cancel_check=cancel_check)


# --------------------------------------------------------------------------
# Process control
# --------------------------------------------------------------------------

def _runtime_root(manifest: LLMPackageManifest) -> Path:
    return Path(manifest.runtime_dir).expanduser() if manifest.runtime_dir else _default_root()


def _runtime_dir(root: Path) -> Path:
    """The private per-node runtime directory (pidfiles and server logs)."""
    directory = root / "runtime"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, 0o700)
    except OSError as exc:
        raise LifecycleError(UNWRITABLE_STATE) from exc
    return directory


def _pid_path(root: Path, package_id: str) -> Path:
    return root / "runtime" / f"{package_id}.pid"


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_pid(root: Path, package_id: str, pid: int) -> None:
    path = _runtime_dir(root) / f"{package_id}.pid"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(str(pid))
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError as exc:
        raise LifecycleError(UNWRITABLE_STATE) from exc


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


def _spawn(server: Path, command: list[str], root: Path, package_id: str,
           port: int) -> subprocess.Popen:
    detach: dict[str, Any] = {"start_new_session": True}
    if os.name == "nt":
        detach = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    log = _runtime_dir(root) / f"{package_id}.log"
    try:
        # Truncated on every start (so it stays bounded) and owner-only, since
        # a runtime log is node-private operational data.
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            if os.name != "nt":
                os.chmod(log, 0o600)
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                cwd=str(server.parent), **detach,
            )
    except OSError as exc:
        raise LifecycleError("unable to start llama-server (see the runtime log)") from exc
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
        _write_pid(root, manifest.package_id, 0)  # Adopted: owner-managed server.
        return
    command = [
        str(server), "-m", str(model), "--host", "127.0.0.1", "--port", str(port),
        "--alias", manifest.public_model_alias, "-c", str(manifest.context_window),
        "-np", str(manifest.max_concurrent), "--no-webui",
    ]
    process = _spawn(server, command, root, manifest.package_id, port)
    _CHILDREN[process.pid] = process
    _write_pid(root, manifest.package_id, process.pid)
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


def _forget(pid_path: Path, pid: int | None) -> None:
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass
    if pid:
        _CHILDREN.pop(pid, None)


def stop(manifest: LLMPackageManifest) -> bool:
    """True only when this node had a live server and it is now gone."""
    root = _runtime_root(manifest)
    pid_path = _pid_path(root, manifest.package_id)
    pid = _read_pid(pid_path)
    if pid == 0:
        return False  # Adopted server: owner-managed, never stopped by Rynmesh.
    if pid is None:
        return False  # No pidfile: this node never started a server for the package.
    if not _alive(pid):
        _forget(pid_path, pid)  # Stale pidfile: nothing of ours was running.
        return False
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
        _forget(pid_path, pid)
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
