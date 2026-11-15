"""One-shot POSIX login shell environment capture with bounded warmup wait."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
from collections.abc import Mapping

from .git_safety import strip_rynmesh_env

LOGGER = logging.getLogger(__name__)

CAPTURE_TIMEOUT_SECONDS = 10
WARMUP_WAIT_SECONDS = 2.0
IGNORED_KEYS = frozenset({"PWD", "OLDPWD", "SHLVL", "_"})
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_LOCK = threading.Lock()
_CACHE: dict[tuple[str], dict[str, str]] = {}
_INFLIGHT: dict[tuple[str], threading.Event] = {}


def default_posix_shell(env: Mapping[str, str] | None = None) -> str | None:
    """Choose the POSIX shell executable used for login environment capture."""
    source = os.environ if env is None else env
    configured = str(source.get("SHELL") or "").strip()
    if configured and os.path.isfile(configured) and os.access(configured, os.X_OK):
        return configured
    candidates = ("/bin/zsh", "/bin/bash") if sys.platform == "darwin" else ("/bin/bash", "/bin/zsh")
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def parse_nul_env(raw: bytes | str) -> dict[str, str]:
    """Parse NUL-delimited `env -0` output while discarding volatile/node-private keys."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    parsed: dict[str, str] = {}
    for entry in text.split("\0"):
        if not entry or "=" not in entry:
            continue
        raw_key, value = entry.split("=", 1)
        if "\n" in raw_key or "\r" in raw_key:
            raw_key = raw_key.replace("\r\n", "\n").rsplit("\n", 1)[-1]
        if not _ENV_NAME_RE.match(raw_key):
            continue
        if raw_key in IGNORED_KEYS or raw_key.upper().startswith("RYNMESH_"):
            continue
        parsed[raw_key] = value
    return parsed


def capture(shell_path: str | None = None) -> dict[str, str]:
    """Capture POSIX login environment once per *shell_path*; never retry after failure."""
    if os.name == "nt":
        return {}
    resolved = shell_path or default_posix_shell()
    if not resolved:
        return {}
    cache_key = (resolved,)
    with _LOCK:
        if cache_key in _CACHE:
            return dict(_CACHE[cache_key])
        inflight = _INFLIGHT.get(cache_key)
        if inflight is None:
            inflight = threading.Event()
            _INFLIGHT[cache_key] = inflight
            leader = True
        else:
            leader = False

    if not leader:
        inflight.wait()
        with _LOCK:
            return dict(_CACHE.get(cache_key, {}))

    result: dict[str, str] = {}
    try:
        base_env = strip_rynmesh_env(os.environ)
        completed = subprocess.run(
            [resolved, "-lc", "env -0"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            start_new_session=True,
            env=base_env,
            timeout=CAPTURE_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode == 0:
            result = parse_nul_env(completed.stdout)
        else:
            LOGGER.warning(
                "POSIX login shell environment capture exited %s for %s; falling back to inherited env",
                completed.returncode,
                resolved,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.warning(
            "POSIX login shell environment capture failed for %s (%s); falling back to inherited env",
            resolved,
            exc,
        )
    finally:
        with _LOCK:
            _CACHE[cache_key] = result
            _INFLIGHT.pop(cache_key, None)
            inflight.set()
    return dict(result)


def start_warmup(shell_path: str | None = None) -> threading.Thread | None:
    """Start a background daemon capture if not already cached or in flight."""
    if os.name == "nt":
        return None
    resolved = shell_path or default_posix_shell()
    if not resolved:
        return None
    cache_key = (resolved,)
    with _LOCK:
        if cache_key in _CACHE or cache_key in _INFLIGHT:
            return None
        inflight = threading.Event()
        _INFLIGHT[cache_key] = inflight

    def _worker() -> None:
        result: dict[str, str] = {}
        try:
            base_env = strip_rynmesh_env(os.environ)
            completed = subprocess.run(
                [resolved, "-lc", "env -0"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                start_new_session=True,
                env=base_env,
                timeout=CAPTURE_TIMEOUT_SECONDS,
                check=False,
            )
            if completed.returncode == 0:
                result = parse_nul_env(completed.stdout)
            else:
                LOGGER.warning(
                    "POSIX login shell environment capture exited %s for %s; falling back to inherited env",
                    completed.returncode,
                    resolved,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.warning(
                "POSIX login shell environment capture failed for %s (%s); falling back to inherited env",
                resolved,
                exc,
            )
        finally:
            with _LOCK:
                _CACHE[cache_key] = result
                _INFLIGHT.pop(cache_key, None)
                inflight.set()

    thread = threading.Thread(target=_worker, daemon=True, name="ryn-login-env-warmup")
    thread.start()
    return thread


def captured_for_child(shell_path: str | None = None, *, wait_seconds: float | None = None) -> dict[str, str]:
    """Return cached login environment, waiting at most *wait_seconds* for background warmup."""
    if os.name == "nt":
        return {}
    resolved = shell_path or default_posix_shell()
    if not resolved:
        return {}
    cache_key = (resolved,)
    budget = WARMUP_WAIT_SECONDS if wait_seconds is None else max(0.0, float(wait_seconds))
    with _LOCK:
        if cache_key in _CACHE:
            return dict(_CACHE[cache_key])
        inflight = _INFLIGHT.get(cache_key)
    if inflight is None:
        start_warmup(resolved)
        with _LOCK:
            if cache_key in _CACHE:
                return dict(_CACHE[cache_key])
            inflight = _INFLIGHT.get(cache_key)
    if inflight is not None and not inflight.wait(budget):
        return {}
    with _LOCK:
        return dict(_CACHE.get(cache_key, {}))


def reset_cache() -> None:
    """Clear cached login environments (used by isolated unit tests)."""
    with _LOCK:
        _CACHE.clear()
        _INFLIGHT.clear()
