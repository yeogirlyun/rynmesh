"""User-selected local GGUF, verified native runtime and application-start policy."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .adapters import AdapterError, OpenAICompatibleAdapter
from .gguf_import import NativeModelError, inspect_model
from .hardware import _memory, _nvidia
from .manifest import LLMPackageManifest

MODEL_ID = "ryn-local-model"
ENGINE_VERSION = "b11153"
ENGINE_BASE = f"https://github.com/ggml-org/llama.cpp/releases/download/{ENGINE_VERSION}/"
# GitHub release asset digests, verified against the official release API.
ENGINES = {
    "windows-cpu": [("llama-b11153-bin-win-cpu-x64.zip", 18559858,
                     "569d19826f3fb00a3fc2df7bd68ab9ad33e5c0d6d69ce022b24372700cee7931")],
    "windows-cuda": [("llama-b11153-bin-win-cuda-12.4-x64.zip", 253869597,
                      "0d5c737b6c5ba61f971515e1e008a52073e606914b4ac4ac8d6f27389b84935a"),
                     ("cudart-llama-bin-win-cuda-12.4-x64.zip", 391443627,
                      "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6")],
    "macos-metal": [("llama-b11153-bin-macos-arm64.tar.gz", 11189602,
                     "9aa63c493ee501b10d2c51a4f6e7923843e5a75ede176a521df3cc55a1b83db0")],
}


def digest_file(path: Path, cancel=lambda: False) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024**2):
            if cancel():
                raise NativeModelError("cancelled")
            digest.update(chunk)
    return digest.hexdigest()


def platform_key() -> str:
    system, arch = platform.system(), platform.machine().lower()
    if system == "Darwin" and arch in {"arm64", "aarch64"}:
        return "macos-metal"
    if system == "Windows" and arch in {"amd64", "x86_64"}:
        return "windows-cpu"
    raise NativeModelError("unsupported_platform")


def validate_model(path: Path, cancel=lambda: False) -> dict:
    metadata = inspect_model(path, cancel)
    metadata["sha256"] = digest_file(path, cancel)
    return metadata


def download(url: str, target: Path, size: int, sha: str, report, cancel) -> None:
    for attempt in range(3):
        if cancel():
            raise NativeModelError("cancelled")
        try:
            return _download_once(url, target, size, sha, report, cancel)
        except OSError as exc:
            if attempt == 2:
                raise NativeModelError("download_incomplete") from exc
            for _ in range(10 * (attempt + 1)):
                if cancel():
                    raise NativeModelError("cancelled") from exc
                time.sleep(0.1)


def _download_once(url: str, target: Path, size: int, sha: str, report, cancel) -> None:
    """Resume only exact byte ranges; always hash the entire completed file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size == size and digest_file(target, cancel) == sha:
        return
    part = target.with_name(target.name + ".part")
    offset = part.stat().st_size if part.exists() else 0
    if offset > size:
        part.unlink()
        offset = 0
    if offset < size:
        headers = {"User-Agent": "Rynmesh/0.6", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            if offset and response.status != 206:
                offset = 0
            if response.status == 206 and response.headers.get("Content-Range") != f"bytes {offset}-{size - 1}/{size}":
                raise NativeModelError("download_range_invalid")
            with part.open("ab" if offset else "wb") as output:
                while chunk := response.read(64 * 1024):
                    if cancel():
                        raise NativeModelError("cancelled")
                    offset += len(chunk)
                    if offset > size:
                        raise NativeModelError("download_size_invalid")
                    output.write(chunk)
                    report(offset, size)
    if part.stat().st_size != size:
        raise NativeModelError("download_incomplete")
    if digest_file(part, cancel) != sha:
        part.unlink()
        raise NativeModelError("download_checksum_invalid")
    part.replace(target)


def extract_runtime(archive: Path, destination: Path) -> None:
    """Extract bounded regular files only. Materialize internal tar links as files."""
    destination.mkdir(parents=True, exist_ok=True)
    total = 0

    def target(name, size):
        nonlocal total
        clean = PurePosixPath(name)
        if clean.is_absolute() or ".." in clean.parts or "\\" in name or ":" in name:
            raise NativeModelError("runtime_archive_invalid")
        path = destination.joinpath(*clean.parts)
        if not path.resolve().is_relative_to(destination.resolve()):
            raise NativeModelError("runtime_archive_invalid")
        total += size
        if size > 2 * 1024**3 or total > 4 * 1024**3:
            raise NativeModelError("runtime_archive_too_large")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            if len(bundle.infolist()) > 1024:
                raise NativeModelError("runtime_archive_too_large")
            for item in bundle.infolist():
                if item.is_dir():
                    continue
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise NativeModelError("runtime_archive_invalid")
                path = target(item.filename, item.file_size)
                with bundle.open(item) as source, path.open("wb") as output:
                    shutil.copyfileobj(source, output)
    else:
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            if len(members) > 1024:
                raise NativeModelError("runtime_archive_too_large")
            for item in members:
                if item.isdir():
                    continue
                if not (item.isfile() or item.issym() or item.islnk()):
                    raise NativeModelError("runtime_archive_invalid")
                if item.issym() or item.islnk():
                    linked = PurePosixPath(item.linkname)
                    if linked.is_absolute() or ".." in linked.parts:
                        raise NativeModelError("runtime_archive_invalid")
                source = bundle.extractfile(item)
                if source is None:
                    raise NativeModelError("runtime_archive_invalid")
                with source:
                    data = source.read(256 * 1024**2 + 1)
                if len(data) > 256 * 1024**2:
                    raise NativeModelError("runtime_archive_too_large")
                path = target(item.name, len(data))
                path.write_bytes(data)
                path.chmod(0o755 if item.mode & 0o111 else 0o644)


class ManagedAdapter:
    """Admission and drain apply equally to chat, local API and peer requests."""
    def __init__(self, owner):
        self.owner = owner

    def health(self):
        with self.owner.lock:
            ready = self.owner.state == "running" and self.owner.process is not None and self.owner.process.poll() is None
            return {"ok": ready, "model": self.owner.manifest.model}

    @contextmanager
    def request(self):
        with self.owner.lock:
            if not self.health()["ok"]:
                raise AdapterError("local_model_stopped")
            adapter = self.owner.upstream
            self.owner.active += 1
        try:
            yield adapter
        finally:
            with self.owner.lock:
                self.owner.active -= 1
                self.owner.lock.notify_all()

    def infer(self, **kwargs):
        with self.request() as adapter:
            return adapter.infer(**kwargs)

    def chat(self, *args, **kwargs):
        with self.request() as adapter:
            return adapter.chat(*args, **kwargs)

    def capabilities(self):
        return {"chat_completions": True, "streaming": True, "cancel": "best_effort"}

    def models(self):
        return [{"id": self.owner.manifest.model}] if self.health()["ok"] else []

    def metrics(self):
        return self.owner.upstream.metrics() if self.owner.upstream else {}

    def cancel(self, task_id):
        return self.owner.upstream.cancel(task_id) if self.owner.upstream else False

    def shutdown(self):
        if self.owner.upstream:
            self.owner.upstream.shutdown()


class NativeModel:
    def __init__(self, home: Path):
        self.root = home.resolve() / "llm" / "native"
        self.config_path = self.root / "settings.json"
        self.model_path = self.root / "models" / "model.gguf"
        self.lock = threading.Condition(threading.RLock())
        self.cancel_event = threading.Event()
        self.closed = False
        self.worker = None
        self.process = None
        self.upstream = None
        self.active = 0
        self.progress = None
        self.stage = ""
        self.error = ""
        self.backend = ""
        self.uploading = False
        self.upload_progress = 0
        self.metadata = {}
        self.config = {"autostart": False, "sharing": False, "installed": False}
        try:
            saved = json.loads(self.config_path.read_text(encoding="utf-8"))
            self.config.update({k: v for k, v in saved.items() if k in self.config and type(v) is bool})
            if isinstance(saved.get("model"), dict):
                self.metadata = saved["model"]
        except (OSError, ValueError, AttributeError):
            pass
        self.config["installed"] = bool(self.config["installed"] and self.metadata and self.model_path.is_file())
        self.state = "stopped" if self.config["installed"] else "not_installed"
        self.adapter = ManagedAdapter(self)
        self._update_manifest()

    def _update_manifest(self):
        sha = self.metadata.get("sha256", "")
        name = self.metadata.get("name", "Local model")
        context = self.metadata.get("context_window", 4096)
        self.manifest = LLMPackageManifest(
            package_id=MODEL_ID + ("-" + sha[:16] if sha else ""), mode="managed", public_model_alias=name,
            runtime="native_llama_cpp", model=name, context_window=context,
            max_output_tokens=min(1024, context // 2), timeout_seconds=300,
            model_fingerprint="sha256:" + sha if sha else "", license_id=self.metadata.get("license_id", "unknown"),
            capabilities=["text-generation", "streaming", "personal-space-only"],
        )

    def _save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.config_path.with_suffix(".tmp")
        temp.write_text(json.dumps({**self.config, "model": self.metadata}), encoding="utf-8")
        temp.replace(self.config_path)

    def preferences(self, values):
        if not isinstance(values, dict) or any(k not in {"autostart", "sharing"} or type(v) is not bool for k, v in values.items()):
            raise NativeModelError("invalid_preferences")
        with self.lock:
            self.config.update(values)
            self._save()
        return self.status()

    def status(self):
        with self.lock:
            if self.state == "running" and self.process and self.process.poll() is not None:
                self.state, self.error = "error", "runtime_exited"
            try:
                supported = bool(platform_key())
            except NativeModelError:
                supported = False
            return {"id": self.manifest.package_id, "name": self.metadata.get("name", ""), "file_name": self.metadata.get("file_name", ""),
                    "size_bytes": self.metadata.get("size_bytes", 0), "state": "installing" if self.uploading else self.state,
                    "stage": "uploading" if self.uploading else self.stage,
                    "progress": self.upload_progress if self.uploading else self.progress, "error": self.error, "backend": self.backend,
                    "supported": supported, "active_requests": self.active, **self.config}

    def _report(self, stage, progress=None):
        if self.cancel_event.is_set() or self.closed:
            raise NativeModelError("cancelled")
        with self.lock:
            self.stage, self.progress = stage, progress

    def action(self, action, *, source="", uploaded=False, file_name=""):
        if action not in {"import", "start", "stop", "cancel", "uninstall"}:
            raise NativeModelError("unsupported_action")
        with self.lock:
            if self.closed:
                raise NativeModelError("node_stopping")
            if action == "cancel":
                if self.state == "installing" or self.uploading:
                    self.cancel_event.set()
                return self.status()
            if (self.worker and self.worker.is_alive()) or (self.uploading and not uploaded):
                raise NativeModelError("model_busy")
            if action == "import" and self.config["installed"]:
                raise NativeModelError("model_already_installed")
            if action == "start" and self.state == "running":
                return self.status()
            if action == "stop" and not self.process:
                self.state = "stopped" if self.config["installed"] else "not_installed"
                return self.status()
            if action == "start" and not self.config["installed"]:
                raise NativeModelError("model_not_installed")
            if action == "import" and not source:
                raise NativeModelError("select_model_file")
            self.cancel_event.clear()
            self.error, self.progress = "", None
            self.state = {"start": "starting", "stop": "stopping", "uninstall": "stopping"}.get(action, "installing")
            self.stage = "stopping" if action in {"stop", "uninstall"} else "checking"
            self.worker = threading.Thread(target=self._run, args=(action, source, uploaded, file_name), daemon=True,
                                           name="ryn-native-model")
            self.worker.start()
            return self.status()

    def _run(self, action, source, uploaded=False, file_name=""):
        try:
            if action in {"stop", "uninstall"}:
                with self.lock:
                    while self.active and not self.closed:
                        self.stage = "draining"
                        self.lock.wait(timeout=0.5)
                self._kill()
                with self.lock:
                    if action == "uninstall":
                        self.model_path.unlink(missing_ok=True)
                        self.config.update(installed=False, autostart=False, sharing=False)
                        self.metadata = {}
                        self._update_manifest()
                        self._save()
                    self.state = "stopped" if self.config["installed"] else "not_installed"
                return
            self.root.mkdir(parents=True, exist_ok=True)
            backend = platform_key()
            self._report("validating")
            original = Path(source).expanduser().resolve() if action == "import" else self.model_path
            metadata = validate_model(original, self.cancel_event.is_set)
            if action == "start" and metadata["sha256"] != self.metadata.get("sha256"):
                raise NativeModelError("model_changed")
            size = metadata["size_bytes"]
            # Conservative estimate includes weights plus context/runtime overhead.
            required_mb = int(size / 1024**2 * 1.2) + 1024
            _, available = _memory()
            if available < required_mb:
                raise NativeModelError("insufficient_memory")
            if backend == "windows-cpu":
                gpus, _ = _nvidia()
                if any(g.memory_free_mb >= required_mb and tuple(int(p) for p in g.driver_version.split(".")[:2]) >= (551, 61) for g in gpus):
                    backend = "windows-cuda"
            self.backend = backend
            needed = (size if action == "import" else 0) + sum(a[1] for a in ENGINES[backend]) * 4 + 512 * 1024**2
            if shutil.disk_usage(self.root).free < needed:
                raise NativeModelError("insufficient_disk")
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            if action == "import":
                if original != self.model_path.resolve():
                    temp = self.model_path.with_suffix(".importing")
                    try:
                        with original.open("rb") as src, temp.open("wb") as dst:
                            count = 0
                            while chunk := src.read(4 * 1024**2):
                                if count + len(chunk) > size:
                                    raise NativeModelError("model_changed")
                                self._report("copying", min(100, int(count / size * 100)))
                                dst.write(chunk)
                                count += len(chunk)
                        if digest_file(temp, self.cancel_event.is_set) != metadata["sha256"]:
                            raise NativeModelError("model_changed")
                        temp.replace(self.model_path)
                    finally:
                        temp.unlink(missing_ok=True)
            with self.lock:
                if action == "import":
                    self.metadata = metadata
                    if uploaded:
                        self.metadata["file_name"] = file_name
                        if self.metadata["name"] == original.stem:
                            self.metadata["name"] = Path(file_name).stem[:160]
                    self._update_manifest()
                self.config["installed"] = True
                self._save()
            engine = self._engine(backend)
            self._start(engine, backend)
            with self.lock:
                self.state, self.stage, self.progress = "running", "ready", None
        except Exception as exc:
            self._kill()
            with self.lock:
                cancelled = self.cancel_event.is_set() or self.closed
                self.state = ("stopped" if self.config["installed"] else "not_installed") if cancelled else "error"
                self.error = "" if cancelled else (str(exc) if isinstance(exc, NativeModelError) else "operation_failed")
                self.stage, self.progress = "", None
        finally:
            if uploaded:
                Path(source).unlink(missing_ok=True)

    def _engine(self, backend):
        directory = self.root / "runtime" / ENGINE_VERSION / backend
        index = directory / "verified.json"
        self._report("runtime")
        if index.exists():
            try:
                files = json.loads(index.read_text())
                if files and all((directory / name).resolve().is_relative_to(directory.resolve()) and
                                 digest_file(directory / name, self.cancel_event.is_set) == sha for name, sha in files.items()):
                    return self._executable(directory)
            except (OSError, ValueError, AttributeError):
                pass
        for filename, size, sha in ENGINES[backend]:
            archive = self.root / "downloads" / filename
            download(ENGINE_BASE + filename, archive, size, sha,
                     lambda n, total: self._report("runtime", int(n / total * 100)), self.cancel_event.is_set)
            extract_runtime(archive, directory)
        executable = self._executable(directory)
        files = {p.relative_to(directory).as_posix(): digest_file(p, self.cancel_event.is_set)
                 for p in directory.rglob("*") if p.is_file() and p != index}
        index.write_text(json.dumps(files), encoding="utf-8")
        return executable

    @staticmethod
    def _executable(directory):
        name = "llama-server.exe" if os.name == "nt" else "llama-server"
        matches = list(directory.rglob(name))
        if len(matches) != 1:
            raise NativeModelError("runtime_missing")
        return matches[0]

    def _start(self, executable, backend):
        self._report("starting")
        self._kill()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        key = secrets.token_urlsafe(32)
        command = [str(executable), "--model", str(self.model_path), "--alias", self.manifest.model,
                   "--host", "127.0.0.1", "--port", str(port), "--api-key", key,
                   "--ctx-size", str(self.manifest.context_window), "--parallel", "1", "--n-gpu-layers",
                   "0" if backend.endswith("cpu") else "99", "--no-webui",
                   "--jinja"]
        if self.metadata.get("architecture", "").startswith("qwen"):
            command += ["--chat-template-kwargs", '{"enable_thinking":false}']
        options = {"cwd": str(executable.parent), "stdin": subprocess.DEVNULL,
                   "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            from .process_guard import _windows_job
            process = subprocess.Popen(command, creationflags=0x4 | 0x08000000, **options)
            try:
                process._ryn_job = _windows_job(process, memory_limit=0, cpu_rate=0)
            except BaseException:
                process.kill()
                process.wait(timeout=5)
                raise
        else:
            entry = [sys.executable, "--ryn-native-worker"] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).with_name("native_worker.py"))]
            process = subprocess.Popen([*entry, str(os.getpid()), *command], start_new_session=True, **options)
        with self.lock:
            self.process = process
            cancelled = self.closed or self.cancel_event.is_set()
        if cancelled:
            self._kill()
            raise NativeModelError("cancelled")
        upstream = OpenAICompatibleAdapter(base_url=f"http://127.0.0.1:{port}", model=self.manifest.model,
                                            api_key=key, timeout_s=300)
        deadline = time.monotonic() + 180
        while not upstream.health().get("ok"):
            self._report("starting")
            if process.poll() is not None:
                raise NativeModelError("runtime_exited")
            if time.monotonic() > deadline:
                raise NativeModelError("startup_timeout")
            self.cancel_event.wait(0.5)
        self._report("testing")
        result = upstream.infer(prompt="Reply with exactly: OK", max_tokens=32, task_id="native-self-test", timeout_s=90)
        if not result.get("text", "").strip():
            raise NativeModelError("self_test_failed")
        self._report("ready")
        with self.lock:
            self.upstream = upstream

    def _kill(self):
        from .process_guard import terminate
        with self.lock:
            process, self.process = self.process, None
            self.upstream = None
        if process:
            terminate(process)

    def on_startup(self):
        if self.config["autostart"] and self.config["installed"]:
            self.action("start")

    def close(self):
        with self.lock:
            self.closed = True
            self.cancel_event.set()
            self.lock.notify_all()
        self._kill()
        if self.worker:
            self.worker.join(timeout=2)
