"""Local CLI model discovery with bounded processes and a short-lived cache."""
from __future__ import annotations

import re
import subprocess
import threading
import time

from .adapters import AdapterError
from .codex_session import private_workspace
from .process_guard import communicate_bounded, spawn, terminate

_cache = {}
_lock = threading.Lock()
_cache_lock = threading.Lock()


def context_metadata(models, model="", *, fallback=32768):
    """Read only the selected/default model's declared window, never a caller hint."""
    selected = next((row for row in models if row.get("id") == model), {}) if model else next(
        (row for row in models if row.get("default")), {})
    window = selected.get("context_window")
    known = type(window) is int and 256 <= window <= 2000000
    return {"context_window": window if known else fallback,
            "context_window_known": known,
            "context_window_basis": "installed_cli_model_cache" if known else "unknown_fallback"}


def cached_models(kind, executable):
    """Metadata reads must not wait behind a CLI discovery subprocess."""
    key = (kind, executable) if kind == "codex_cli" else executable
    with _cache_lock:
        cached = _cache.get(key)
        if cached and 0 <= time.monotonic() - cached[0] < (60 if kind == "codex_cli" else 30):
            return [dict(row) for row in cached[1]]
    return []


def validate_model(model):
    if not isinstance(model, str) or (model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model)):
        raise AdapterError("invalid cli_model")


def parse_antigravity_models(output):
    models, seen = [], set()
    for line in output.splitlines():
        identifier, separator, name = line.partition("\t")
        if not separator:
            continue  # CLI progress/status lines are not models.
        identifier, name = identifier.strip(), name.strip()
        validate_model(identifier)
        if not identifier or not name:
            raise AdapterError("Antigravity returned an invalid model list")
        if identifier not in seen:
            models.append({"id": identifier, "name": name, "default": False})
            seen.add(identifier)
    if not models:
        raise AdapterError("Antigravity returned no models; run agy locally to check login and model access")
    # No default model is advertised by `agy models`; preserve the CLI's choice.
    return [{"id": "", "name": "CLI default", "default": True}, *models]


def list_models(kind, executable, *, refresh=False):
    if kind == "codex_cli":
        from .codex_session import list_models as codex_models
        with _lock:
            cached = _cache.get((kind, executable))
            if not refresh and cached and 0 <= time.monotonic() - cached[0] < 60:
                return [dict(row) for row in cached[1]]
            models = codex_models(executable)
            with _cache_lock:
                _cache[(kind, executable)] = (time.monotonic(), models)
            return [dict(row) for row in models]
    if kind != "antigravity_cli":
        raise AdapterError("model discovery is unavailable for this CLI")
    with _lock:
        cached = _cache.get(executable)
        if not refresh and cached and 0 <= time.monotonic() - cached[0] < 30:
            return [dict(row) for row in cached[1]]
        with private_workspace(copy_auth=False) as (root, env):
            from .antigravity_session import prepare_workspace
            prepare_workspace(root, env)
            process = None
            try:
                process = spawn([executable, "models"], cwd=str(root), env=env, timeout=15)
                output = communicate_bounded(process, b"", timeout=15, root=root)
                if process.returncode:
                    raise AdapterError("Antigravity model discovery failed; check local login and CLI version")
                if isinstance(output, bytes):
                    output = output.decode("utf-8")
                models = parse_antigravity_models(output)
            except (OSError, UnicodeError, subprocess.TimeoutExpired, RuntimeError) as exc:
                raise AdapterError("could not read Antigravity models; check local login and CLI version") from exc
            finally:
                if process is not None:
                    terminate(process)
        with _cache_lock:
            _cache[executable] = (time.monotonic(), models)
        return [dict(row) for row in models]
