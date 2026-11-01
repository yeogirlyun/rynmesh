"""Explicit pinned Craft CLI installation and bounded, private MCP stdio sessions.

The executable is a reviewed portable-release member, never a model command.
This module grants no file authority: callers must validate every tool and path
against their task workspace before dispatch. CLI root flags are extra defence,
not an operating-system sandbox. No constructor downloads or installs software.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import queue
import re
import shutil
import stat
import tempfile
import threading
import time
import zipfile
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from ..file_transactions import file_transaction
from ..llm_package import process_guard
from . import craft_host
from .craft_catalog import CRAFT

CraftError = craft_host.CraftError
PLATFORM = "windows-x64"
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXPANDED = 768 * 1024 * 1024
MAX_FILES = 8192
MAX_RECEIPT = 2 * 1024 * 1024
MAX_REQUEST = 1024 * 1024
MAX_OUTPUT = 8 * 1024 * 1024
PROTOCOL_VERSION = "2025-06-18"
_VERSIONS = {PROTOCOL_VERSION, "2025-03-26", "2024-11-05"}
_WINDOWS_RESERVED = re.compile(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.I)
_STATUS_CACHE = OrderedDict()
_STATUS_LOCK = threading.RLock()
MAX_STATUS_CACHE = 64


def platform_supported():
    return os.name == "nt" and platform.machine().lower() in {"amd64", "x86_64"}


def _specification(slug):
    from .craft_automation_catalog import AUTOMATION

    spec = AUTOMATION.get(slug, {}).get(PLATFORM)
    if slug not in CRAFT or not isinstance(spec, dict):
        raise CraftError("craft_agent_runtime_unsupported")
    if (not isinstance(spec.get("asset"), str)
            or PurePosixPath(spec["asset"]).name != spec["asset"]
            or any(c in spec["asset"] for c in "\\:\x00")
            or not re.fullmatch(r"[0-9a-f]{64}", str(spec.get("sha256", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(spec.get("payload_manifest_sha256", "")))
            or spec.get("executable") != f"{slug}-cli.exe"):
        raise CraftError("craft_agent_runtime_spec_invalid")
    return spec


def _is_link(path):
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _location(cache, slug):
    _specification(slug)
    base = Path(cache).expanduser().resolve()
    root = base / slug / CRAFT[slug]["tag"] / "agent" / PLATFORM
    # The configured cache itself may resolve through an owner-selected link;
    # package components below it must never redirect extraction or verification.
    current = base
    for part in root.relative_to(base).parts:
        current = current / part
        if _is_link(current):
            raise CraftError("craft_agent_runtime_unsafe_path")
    if not root.resolve().is_relative_to(base):
        raise CraftError("craft_agent_runtime_unsafe_path")
    return root


def _member_path(name):
    path = PurePosixPath(name)
    if (not name or "\\" in name or ":" in name or "\x00" in name
            or path.is_absolute() or not path.parts
            or any(part in {".", ".."} or part.endswith((".", " "))
                   or _WINDOWS_RESERVED.match(part) for part in path.parts)
            or path.as_posix() != name.rstrip("/")):
        raise CraftError("craft_agent_runtime_unsafe_archive")
    return path


def _extract(archive_path, destination):
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if (not members or len(members) > MAX_FILES
                or sum(row.file_size for row in members) > MAX_EXPANDED):
            raise CraftError("craft_agent_runtime_archive_limit")
        seen, files = set(), set()
        for row in members:
            path = _member_path(row.orig_filename)
            name = path.as_posix().casefold()
            mode = row.external_attr >> 16
            if (name in seen or row.flag_bits & 1 or stat.S_ISLNK(mode)
                    or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}):
                raise CraftError("craft_agent_runtime_unsafe_archive")
            seen.add(name)
            if not row.is_dir():
                files.add(name)
        if any(any(parent.as_posix().casefold() in files for parent in PurePosixPath(name).parents
                       if parent != PurePosixPath(".")) for name in seen):
            raise CraftError("craft_agent_runtime_unsafe_archive")
        archive.extractall(destination)


def _inventory(payload):
    if not payload.is_dir() or _is_link(payload):
        raise CraftError("craft_agent_runtime_not_verified")
    files, size, entries = {}, 0, 0
    for directory, dirs, names in os.walk(payload, followlinks=False):
        current = Path(directory)
        entries += len(dirs) + len(names)
        if entries > MAX_FILES:
            raise CraftError("craft_agent_runtime_archive_limit")
        for name in dirs + names:
            target = current / name
            if _is_link(target) or not target.resolve().is_relative_to(payload.resolve()):
                raise CraftError("craft_agent_runtime_not_verified")
        for name in names:
            target = current / name
            if not stat.S_ISREG(target.stat().st_mode):
                raise CraftError("craft_agent_runtime_not_verified")
            relative = target.relative_to(payload).as_posix()
            _member_path(relative)
            size += target.stat().st_size
            if len(files) >= MAX_FILES or size > MAX_EXPANDED:
                raise CraftError("craft_agent_runtime_archive_limit")
            files[relative] = craft_host.digest(target)
    return files


def _executable(payload, files, slug):
    wanted = f"{slug}-cli.exe"
    candidates = [name for name in files if PurePosixPath(name).name.casefold() == wanted]
    if len(candidates) != 1 or PurePosixPath(candidates[0]).name != wanted:
        raise CraftError("craft_agent_runtime_executable_invalid")
    return payload / candidates[0]


def _manifest_digest(files):
    """Canonical digest pinned in code, independent of the writable receipt."""
    raw = json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _verified_runtime(root, slug):
    spec = _specification(slug)
    try:
        receipt = json.loads(_receipt_bytes(root))
        if (not isinstance(receipt, dict) or receipt.get("schema") != 1
                or receipt.get("app") != slug or receipt.get("version") != CRAFT[slug]["tag"]
                or receipt.get("platform") != PLATFORM
                or receipt.get("asset") != spec["asset"]
                or receipt.get("archive_sha256") != spec["sha256"]):
            raise ValueError
        files = receipt.get("files")
        if (not isinstance(files, dict) or not files or len(files) > MAX_FILES
                or any(not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{64}", str(checksum))
                       for name, checksum in files.items())):
            raise ValueError
        payload = root / "payload"
        actual = _inventory(payload)
        if actual != files or _manifest_digest(actual) != spec["payload_manifest_sha256"]:
            raise ValueError
        executable = _executable(payload, files, slug)
        if receipt.get("executable") != executable.relative_to(payload).as_posix():
            raise ValueError
        return executable
    except (OSError, ValueError, KeyError, TypeError):
        raise CraftError("craft_agent_runtime_not_verified") from None


def installed_runtime(cache, slug):
    """Verify the complete pinned payload, including DLLs, fonts and notices."""
    return _verified_runtime(_location(cache, slug), slug)


def _receipt_bytes(root):
    receipt = root / "receipt.json"
    if _is_link(receipt) or receipt.stat().st_size > MAX_RECEIPT:
        raise CraftError("craft_agent_runtime_not_verified")
    with receipt.open("rb") as stream:
        raw = stream.read(MAX_RECEIPT + 1)
    if len(raw) > MAX_RECEIPT:
        raise CraftError("craft_agent_runtime_not_verified")
    return raw


def _status_stamp(root):
    """Cheap menu evidence only. Never used to authorize process startup."""
    payload = root / "payload"
    if not payload.is_dir() or _is_link(payload):
        raise CraftError("craft_agent_runtime_not_verified")
    digest = hashlib.sha256(_receipt_bytes(root))
    entries, size = 0, 0

    def add(path, relative):
        if _is_link(path) or not path.resolve().is_relative_to(root):
            raise CraftError("craft_agent_runtime_not_verified")
        value = path.stat()
        if not (stat.S_ISREG(value.st_mode) or stat.S_ISDIR(value.st_mode)):
            raise CraftError("craft_agent_runtime_not_verified")
        fields = (relative, value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_mode)
        digest.update(json.dumps(fields, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
        digest.update(b"\n")
        return value

    add(payload, "payload")
    add(root / "receipt.json", "receipt.json")
    for directory, dirs, names in os.walk(payload, followlinks=False):
        dirs.sort()
        names.sort()
        entries += len(dirs) + len(names)
        if entries > MAX_FILES:
            raise CraftError("craft_agent_runtime_archive_limit")
        current = Path(directory)
        for name in sorted([*dirs, *names]):
            path = current / name
            relative = path.relative_to(payload).as_posix()
            _member_path(relative)
            value = add(path, relative)
            if stat.S_ISREG(value.st_mode):
                size += value.st_size
                if size > MAX_EXPANDED:
                    raise CraftError("craft_agent_runtime_archive_limit")
    return digest.hexdigest()


def _invalidate_status(root):
    with _STATUS_LOCK:
        for key in [key for key in _STATUS_CACHE if key[0] == os.path.normcase(str(root))]:
            _STATUS_CACHE.pop(key, None)


def _menu_runtime(cache, slug, root):
    spec = _specification(slug)
    key = (os.path.normcase(str(root)), slug, spec["sha256"], spec["payload_manifest_sha256"])
    with _STATUS_LOCK:
        try:
            stamp = _status_stamp(root)
            previous = _STATUS_CACHE.get(key)
            if previous is not None and previous["stamp"] == stamp:
                _STATUS_CACHE.move_to_end(key)
                return Path(previous["executable"]), True
            _STATUS_CACHE.pop(key, None)
            executable = installed_runtime(cache, slug)
            # Do not label later metadata as verified if a file changed while
            # the complete payload hashes were being checked.
            if stamp != _status_stamp(root):
                raise CraftError("craft_agent_runtime_not_verified")
            _STATUS_CACHE[key] = {"stamp": stamp, "executable": str(executable)}
            _STATUS_CACHE.move_to_end(key)
            while len(_STATUS_CACHE) > MAX_STATUS_CACHE:
                _STATUS_CACHE.popitem(last=False)
            return executable, False
        except BaseException:
            _STATUS_CACHE.pop(key, None)
            raise


def runtime_status(cache, slug):
    """No writes or network access; a damaged installation is never executable."""
    result = {"app": slug, "platform": PLATFORM, "platform_supported": platform_supported(),
              "supported": False, "installed": False, "verified": False, "state": "unsupported",
              "verification_cached": False}
    try:
        _specification(slug)
        result.update(supported=True, version=CRAFT[slug]["tag"], state="missing")
        root = _location(cache, slug)
        result["installed"] = root.exists()
        if not result["installed"]:
            _invalidate_status(root)
            return result
        executable, cached = _menu_runtime(cache, slug, root)
        return {**result, "verified": True, "state": "ready", "executable": str(executable),
                "verification_cached": cached}
    except (CraftError, OSError) as exc:
        result.update(state="unverified" if result["supported"] else "unsupported",
                      error=str(exc) if isinstance(exc, CraftError) else "craft_agent_runtime_not_verified")
        return result


def install_runtime(cache, slug, *, download=craft_host.download_to):
    """Only an explicit owner installation calls this bounded download path."""
    spec = _specification(slug)
    root = _location(cache, slug)
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_transaction(root.parent / ".install.lock"):
        root = _location(cache, slug)
        existing = False
        if root.exists():
            try:
                executable = installed_runtime(cache, slug)
            except CraftError:
                # Explicit owner repair only; discovery never removes a package.
                existing = True
            else:
                return {"app": slug, "version": CRAFT[slug]["tag"], "platform": PLATFORM,
                        "state": "ready", "executable": str(executable)}
        backup = None
        stage = None
        published_ready = False
        try:
            stage = Path(tempfile.mkdtemp(prefix=".install-agent-", dir=root.parent))
            os.chmod(stage, 0o700)
            archive = stage / "release.zip"
            url = f"https://github.com/storytold/{slug}/releases/download/{CRAFT[slug]['tag']}/{spec['asset']}"
            download(url, archive)
            if (archive.stat().st_size > MAX_ARCHIVE
                    or craft_host.digest(archive) != spec["sha256"]):
                raise CraftError("craft_agent_runtime_checksum_mismatch")
            payload = stage / "payload"
            try:
                _extract(archive, payload)
            except (zipfile.BadZipFile, OSError, RuntimeError):
                raise CraftError("craft_agent_runtime_unsafe_archive") from None
            files = _inventory(payload)
            if _manifest_digest(files) != spec["payload_manifest_sha256"]:
                raise CraftError("craft_agent_runtime_payload_mismatch")
            executable = _executable(payload, files, slug)
            receipt = {"schema": 1, "app": slug, "version": CRAFT[slug]["tag"],
                       "platform": PLATFORM, "asset": spec["asset"], "archive_sha256": spec["sha256"],
                       "release_url": url, "executable": executable.relative_to(payload).as_posix(),
                       "files": files}
            raw = json.dumps(receipt, indent=2).encode("utf-8")
            if len(raw) > MAX_RECEIPT:
                raise CraftError("craft_agent_runtime_archive_limit")
            (stage / "receipt.json").write_bytes(raw)
            archive.unlink()
            _verified_runtime(stage, slug)
            if _location(cache, slug) != root:
                raise CraftError("craft_agent_runtime_unsafe_path")
            if existing:
                backup = Path(tempfile.mkdtemp(prefix=".replaced-agent-", dir=root.parent))
                root.rename(backup / "previous")
            stage.rename(root)
            executable = installed_runtime(cache, slug)
            published_ready = True
        except BaseException:
            if backup is not None and (backup / "previous").exists():
                # Restore only our exact checked package path. A failed new
                # installation is moved back to our own disposable stage.
                _location(cache, slug)
                if root.exists() and stage is not None and not stage.exists():
                    root.rename(stage)
                if not root.exists():
                    (backup / "previous").rename(root)
            raise
        finally:
            _invalidate_status(root)
            for disposable in (stage, backup):
                # The absolute target must stay directly below the agent root;
                # never recursively remove a computed redirected directory.
                if (disposable is not None and disposable.exists() and not _is_link(disposable)
                        and disposable.resolve().parent == root.parent.resolve()):
                    # Preserve a previous package if an unusual restoration
                    # failure left it here; it is recoverable by the owner.
                    if disposable == backup and (disposable / "previous").exists():
                        if not published_ready:
                            continue
                    shutil.rmtree(disposable)
        return {"app": slug, "version": CRAFT[slug]["tag"], "platform": PLATFORM,
                "state": "ready", "executable": str(executable)}


def _environment(root):
    keep = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL"}
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    private = str(root)
    env.update(HOME=private, USERPROFILE=private, APPDATA=private, LOCALAPPDATA=private,
               TMP=private, TEMP=private, TMPDIR=private, XDG_CONFIG_HOME=private,
               XDG_CACHE_HOME=private, RUST_LOG="off", OTEL_SDK_DISABLED="true")
    return env


class McpProcess:
    """One serial MCP session, with finite bytes, time and process-tree lifetime."""

    def __init__(self, executable, workspace, app, *, cancelled=lambda: False,
                 timeout=30.0, max_output=MAX_OUTPUT, lifetime=300.0):
        _specification(app)
        self.executable, self.workspace = Path(executable).resolve(), Path(workspace).resolve()
        if (self.executable.name != f"{app}-cli.exe" or not self.executable.is_file()
                or not self.workspace.is_dir()):
            raise CraftError("craft_mcp_path_invalid")
        if (not isinstance(timeout, (int, float)) or not 0 < timeout <= 300
                or not isinstance(lifetime, (int, float)) or not 0 < lifetime <= 600
                or type(max_output) is not int or not 1024 <= max_output <= 32 * 1024 * 1024):
            raise CraftError("craft_mcp_limits_invalid")
        self.timeout, self.max_output, self.cancelled = float(timeout), max_output, cancelled
        self.deadline = time.monotonic() + lifetime
        self.sequence, self.output_bytes, self.notifications = 0, 0, 0
        self.initialized, self.initialization = False, None
        self._request_cancelled = None
        self.failure = None
        self.closed = threading.Event()
        self.lock = threading.RLock()
        self.termination_lock = threading.Lock()
        self.messages = queue.Queue(maxsize=32)
        if cancelled():
            raise CraftError("agent_cancelled")
        self.private = tempfile.TemporaryDirectory(prefix=".ryn-craft-", dir=self.workspace,
                                                   ignore_cleanup_errors=True)
        args = [str(self.executable), "mcp"]
        if app == "wordcraft":
            args += ["--automation-read-root", str(self.workspace), "--automation-write-root", str(self.workspace)]
        elif app == "pdfcraft":
            args += ["--root", str(self.workspace)]
        try:
            self.process = process_guard.spawn(args, cwd=str(self.workspace),
                                               env=_environment(self.private.name), timeout=lifetime)
        except (OSError, ValueError):
            self.private.cleanup()
            raise CraftError("craft_mcp_start_failed") from None
        self.reader = threading.Thread(target=self._read, daemon=True, name="ryn-craft-mcp-output")
        self.watcher = threading.Thread(target=self._watch, daemon=True, name="ryn-craft-mcp-lifetime")
        self.reader.start()
        self.watcher.start()

    def _read(self):
        try:
            while not self.closed.is_set():
                line = self.process.stdout.readline(self.max_output + 1)
                if not line:
                    break
                self.output_bytes += len(line)
                if self.output_bytes > self.max_output or not line.endswith(b"\n"):
                    self.failure = "craft_mcp_output_limit"
                    break
                value = json.loads(line)
                if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                    self.failure = "craft_mcp_protocol_invalid"
                    break
                self.messages.put(value, timeout=0.1)
        except (ValueError, UnicodeError, OSError, queue.Full):
            self.failure = "craft_mcp_protocol_invalid"
        finally:
            try:
                self.messages.put_nowait(None)
            except queue.Full:
                pass

    def _watch(self):
        while not self.closed.wait(0.05):
            cancelled = self._is_cancelled()
            if cancelled or time.monotonic() >= self.deadline or self.failure:
                self.failure = self.failure or ("agent_cancelled" if cancelled else "craft_mcp_timeout")
                self._terminate()
                return

    def _is_cancelled(self):
        try:
            if self.cancelled():
                return True
            active = self._request_cancelled
            return bool(active and active() and self._request_cancelled is active)
        except Exception:
            return True

    @contextmanager
    def _cancel_scope(self, cancelled):
        self._request_cancelled = cancelled
        try:
            yield
        finally:
            # Per-run control tokens expire as soon as a runtime step ends.
            # Never keep one in the idle persistent-session watcher.
            self._request_cancelled = None

    def _terminate(self):
        with self.termination_lock:
            process_guard.terminate(self.process)

    def _check(self, deadline):
        if self._is_cancelled():
            self.failure = "agent_cancelled"
        if self.failure:
            raise CraftError(self.failure)
        if self.closed.is_set():
            raise CraftError("craft_mcp_closed")
        if time.monotonic() >= min(deadline, self.deadline):
            raise CraftError("craft_mcp_timeout")

    def _send(self, value, deadline):
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(raw) > MAX_REQUEST:
            raise CraftError("craft_mcp_input_limit")
        errors, done = [], threading.Event()

        def write():
            try:
                self.process.stdin.write(raw)
                self.process.stdin.flush()
            except (OSError, ValueError):
                errors.append(True)
            finally:
                done.set()

        threading.Thread(target=write, daemon=True, name="ryn-craft-mcp-input").start()
        while not done.wait(0.05):
            self._check(deadline)
        self._check(deadline)
        if errors:
            raise CraftError("craft_mcp_input_unconfirmed")

    def _exchange(self, method, params, timeout=None):
        duration = self.timeout if timeout is None else timeout
        if not isinstance(duration, (int, float)) or not 0 < duration <= 300:
            raise CraftError("craft_mcp_limits_invalid")
        deadline = time.monotonic() + duration
        self._check(deadline)
        self.sequence += 1
        ident = self.sequence
        try:
            self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}, deadline)
            while True:
                self._check(deadline)
                try:
                    message = self.messages.get(timeout=0.05)
                except queue.Empty:
                    continue
                if message is None:
                    raise CraftError(self.failure or "craft_mcp_process_ended")
                if "id" not in message and str(message.get("method", "")).startswith("notifications/"):
                    self.notifications += 1
                    if self.notifications > 64:
                        raise CraftError("craft_mcp_notification_limit")
                    continue
                if type(message.get("id")) is not int or message["id"] != ident:
                    raise CraftError("craft_mcp_response_id_mismatch")
                if "error" in message:
                    raise CraftError("craft_mcp_rpc_error")
                if "result" not in message or not isinstance(message["result"], dict):
                    raise CraftError("craft_mcp_protocol_invalid")
                return message["result"]
        except BaseException:
            self.close()
            raise

    def initialize(self, *, cancelled=None):
        with self.lock, self._cancel_scope(cancelled):
            if self.initialized:
                self._check(self.deadline)
                return self.initialization
            result = self._exchange("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                                   "clientInfo": {"name": "Ryn", "version": "1.0"}})
            if result.get("protocolVersion") not in _VERSIONS or not isinstance(result.get("capabilities"), dict):
                self.close()
                raise CraftError("craft_mcp_initialization_invalid")
            try:
                self._send({"jsonrpc": "2.0", "method": "notifications/initialized"},
                           time.monotonic() + self.timeout)
            except BaseException:
                self.close()
                raise
            self.initialized, self.initialization = True, result
            return result

    def request(self, method, params=None, *, timeout=None, cancelled=None):
        with self.lock, self._cancel_scope(cancelled):
            if not self.initialized:
                raise CraftError("craft_mcp_not_initialized")
            if method not in {"tools/list", "tools/call"} or (params is not None and not isinstance(params, dict)):
                raise CraftError("craft_mcp_method_invalid")
            return self._exchange(method, params or {}, timeout)

    def tools_list(self, *, timeout=None, cancelled=None):
        return self.request("tools/list", timeout=timeout, cancelled=cancelled)

    def tools_call(self, name, arguments, *, timeout=None, cancelled=None):
        if not isinstance(name, str) or not name or not isinstance(arguments, dict):
            raise CraftError("craft_mcp_arguments_invalid")
        return self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout, cancelled=cancelled)

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        self._terminate()
        self.reader.join(timeout=1)
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        self.private.cleanup()

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, *_):
        self.close()
