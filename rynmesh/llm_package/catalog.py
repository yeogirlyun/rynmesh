"""Pinned four-profile model catalog: light / balanced / quality / agent.

Pure data module: no network access, no subprocess calls, nothing that can
fail at import time. Each profile pins an exact Hugging Face revision,
filename, and SHA-256 digest so a managed install always fetches the same
reviewed bytes; `lifecycle.install_managed` and `hardware.recommend` both
read from `PROFILES` instead of hardcoding model details themselves.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)
_APACHE_NOTICE = "Qwen2.5 and llama.cpp are Apache-2.0; review their notices before use."
_DEFAULT_HF_ORIGIN = "https://huggingface.co"
_HOST_PORT_RE = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*"
    r"|\[[0-9A-Fa-f:]+\])(?::([0-9]{1,5}))?"
)
_PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9._~-]+")
_SETTINGS_MIRROR_RE = re.compile(r'"llm_download_mirror"\s*:\s*"((?:\\.|[^"\\])*)"')
_CONFIGURED_HF_MIRROR: str | None = None
_CONFIGURED_DOWNLOAD_MIRROR: str | None = None


def set_hf_mirror(mirror: str | None) -> None:
    """Configure an in-process Hugging Face download mirror (shared with S01-I30)."""
    global _CONFIGURED_HF_MIRROR
    if mirror is not None and mirror.strip():
        _normalize_hf_mirror(mirror)
    _CONFIGURED_HF_MIRROR = mirror.strip() if isinstance(mirror, str) and mirror.strip() else None


def set_download_mirror(mirror: str | None) -> None:
    """Configure an in-process download mirror prefix (`https://host[/prefix]`)."""
    global _CONFIGURED_DOWNLOAD_MIRROR
    normalized = normalize_download_mirror(mirror or "")
    _CONFIGURED_DOWNLOAD_MIRROR = normalized or None


def normalize_download_mirror(mirror: str | None) -> str:
    """Validate an HTTPS mirror host with optional path prefix (`https://host[:port][/prefix]`)."""
    raw = (mirror or "").strip()
    if not raw:
        return ""
    if not raw.startswith("https://"):
        raise ValueError(f"download mirror must start with https://: {raw!r}")
    rest = raw[len("https://"):]
    if any(ch in rest for ch in ("?", "#", "@", "\\", " ", "\t", "\r", "\n")):
        raise ValueError(f"download mirror must not contain query, fragment, or credentials: {raw!r}")
    authority, _, path_part = rest.partition("/")
    if not authority:
        raise ValueError(f"download mirror is missing hostname: {raw!r}")
    match = _HOST_PORT_RE.fullmatch(authority)
    if not match:
        raise ValueError(f"invalid download mirror host: {raw!r}")
    port_text = match.group(1)
    if port_text is not None and not (1 <= int(port_text) <= 65535):
        raise ValueError(f"invalid download mirror port: {raw!r}")
    trimmed_path = path_part.rstrip("/")
    if not trimmed_path:
        return f"https://{authority}"
    segments = trimmed_path.split("/")
    for seg in segments:
        if not seg or seg in {".", ".."} or not _PATH_SEGMENT_RE.fullmatch(seg):
            raise ValueError(f"invalid download mirror path prefix: {raw!r}")
    return f"https://{authority}/{'/'.join(segments)}"


def _read_owner_download_mirror(home_dir: str | None = None) -> str:
    candidates: list[str] = []
    if home_dir:
        candidates.append(os.path.join(os.path.expanduser(home_dir), "settings.json"))
    env_home = os.environ.get("RYNMESH_HOME", "").strip()
    if env_home:
        candidates.append(os.path.join(os.path.expanduser(env_home), "settings.json"))
    else:
        env_llm_home = os.environ.get("RYNMESH_LLM_HOME", "").strip()
        if env_llm_home:
            candidates.append(os.path.join(os.path.dirname(os.path.expanduser(env_llm_home)), "settings.json"))
        elif not home_dir:
            candidates.append(os.path.join(os.path.expanduser("~"), ".rynmesh", "settings.json"))
    for settings_file in candidates:
        try:
            with open(settings_file, encoding="utf-8") as handle:
                text = handle.read(65536)
        except OSError:
            continue
        found = _SETTINGS_MIRROR_RE.search(text)
        if found:
            value = found.group(1).replace(r"\/", "/").strip()
            if value:
                return value
        return ""
    return ""


def resolve_download_mirror(*, mirror: str | None = None, home_dir: str | None = None) -> str:
    if mirror is not None:
        return normalize_download_mirror(mirror)
    env_dl = os.environ.get("RYNMESH_DOWNLOAD_MIRROR")
    if env_dl is not None and env_dl.strip():
        return normalize_download_mirror(env_dl)
    if _CONFIGURED_DOWNLOAD_MIRROR:
        return normalize_download_mirror(_CONFIGURED_DOWNLOAD_MIRROR)
    stored = _read_owner_download_mirror(home_dir)
    return normalize_download_mirror(stored) if stored else ""


def apply_download_mirror(url: str, *, mirror: str | None = None, home_dir: str | None = None) -> str:
    prefix = resolve_download_mirror(mirror=mirror, home_dir=home_dir)
    if not prefix:
        return url
    for origin in ("https://github.com", _DEFAULT_HF_ORIGIN):
        if url.startswith(origin + "/"):
            return prefix + url[len(origin):]
    return url


def _normalize_hf_mirror(mirror: str | None = None) -> str:
    if mirror is not None:
        raw = mirror.strip()
    else:
        env_hf = os.environ.get("RYNMESH_HF_MIRROR")
        if env_hf is not None and env_hf.strip() != "":
            raw = env_hf.strip()
        elif _CONFIGURED_HF_MIRROR:
            raw = _CONFIGURED_HF_MIRROR.strip()
        else:
            dl_prefix = resolve_download_mirror()
            return dl_prefix or _DEFAULT_HF_ORIGIN
    if not raw:
        return _DEFAULT_HF_ORIGIN
    if not raw.startswith("https://"):
        raise ValueError(f"Hugging Face mirror must be an https:// hostname: {raw!r}")
    rest = raw[len("https://"):]
    if rest.endswith("/"):
        rest = rest[:-1]
    if not rest or any(ch in rest for ch in ("/", "?", "#", "@", "\\", " ", "\t", "\r", "\n")):
        raise ValueError(f"Hugging Face mirror must specify host only without path or credentials: {raw!r}")
    match = _HOST_PORT_RE.fullmatch(rest)
    if not match:
        raise ValueError(f"invalid Hugging Face mirror host: {raw!r}")
    port_text = match.group(1)
    if port_text is not None and not (1 <= int(port_text) <= 65535):
        raise ValueError(f"invalid Hugging Face mirror port: {raw!r}")
    return f"https://{rest}"


def _apply_hf_mirror(url: str, *, mirror: str | None = None) -> str:
    origin = normalize_download_mirror(mirror) or _DEFAULT_HF_ORIGIN if mirror is not None else _normalize_hf_mirror()
    if url.startswith(_DEFAULT_HF_ORIGIN + "/"):
        return origin + url[len(_DEFAULT_HF_ORIGIN):]
    return url


@dataclass(frozen=True)
class ModelProfile:
    profile: str            # "light" | "balanced" | "quality" | "agent"
    model_alias: str        # public alias, e.g. "rynmesh-qwen2.5-0.5b-q4"
    display_name: str       # "Qwen2.5-0.5B-Instruct-Q4_K_M"
    parameter_millions: int
    quantization: str       # "Q4_K_M"
    url: str                # pinned HTTPS URL (revision in path)
    sha256: str
    size_bytes: int
    estimated_memory_mb: int
    context_window: int
    max_concurrent: int
    agent_capable: bool = False
    license_id: str = "Apache-2.0"
    license_notice: str = _APACHE_NOTICE

    def __getattribute__(self, name: str):
        value = object.__getattribute__(self, name)
        if name == "url" and isinstance(value, str):
            try:
                return _apply_hf_mirror(value)
            except ValueError:
                _LOGGER.warning("invalid_hf_mirror_ignored: falling back to default Hugging Face origin")
                return value
        return value


def _pinned_url(repo: str, revision: str, filename: str, *, mirror: str | None = None) -> str:
    origin = (normalize_download_mirror(mirror) or _DEFAULT_HF_ORIGIN) if mirror is not None else _normalize_hf_mirror()
    return f"{origin}/{repo}/resolve/{revision}/{filename}"


def _canonical_hf_url(repo: str, revision: str, filename: str) -> str:
    return f"{_DEFAULT_HF_ORIGIN}/{repo}/resolve/{revision}/{filename}"


PROFILES: tuple[ModelProfile, ...] = (
    ModelProfile(
        profile="light",
        model_alias="rynmesh-qwen2.5-0.5b-q4",
        display_name="Qwen2.5-0.5B-Instruct-Q4_K_M",
        parameter_millions=500,
        quantization="Q4_K_M",
        url=_canonical_hf_url(
            "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
            "872f8a96064a1242ac3a3359cad77c3042548405",
            "qwen2.5-0.5b-instruct-q4_k_m.gguf",
        ),
        sha256="74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db",
        size_bytes=491400032,
        estimated_memory_mb=900,
        context_window=32768,
        max_concurrent=2,
        agent_capable=False,
    ),
    ModelProfile(
        profile="balanced",
        model_alias="rynmesh-qwen2.5-1.5b-q4",
        display_name="Qwen2.5-1.5B-Instruct-Q4_K_M",
        parameter_millions=1500,
        quantization="Q4_K_M",
        url=_canonical_hf_url(
            "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
            "91cad51170dc346986eccefdc2dd33a9da36ead9",
            "qwen2.5-1.5b-instruct-q4_k_m.gguf",
        ),
        sha256="6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e",
        size_bytes=1117320736,
        estimated_memory_mb=2300,
        context_window=32768,
        max_concurrent=1,
        agent_capable=False,
    ),
    ModelProfile(
        profile="quality",
        model_alias="rynmesh-qwen2.5-3b-q4",
        display_name="Qwen2.5-3B-Instruct-Q4_K_M",
        parameter_millions=3000,
        quantization="Q4_K_M",
        url=_canonical_hf_url(
            "Qwen/Qwen2.5-3B-Instruct-GGUF",
            "7dabda4d13d513e3e842b20f0d435c732f172cbe",
            "qwen2.5-3b-instruct-q4_k_m.gguf",
        ),
        sha256="626b4a6678b86442240e33df819e00132d3ba7dddfe1cdc4fbb18e0a9615c62d",
        size_bytes=2104932768,
        estimated_memory_mb=4200,
        context_window=32768,
        max_concurrent=1,
        agent_capable=False,
    ),
    ModelProfile(
        profile="agent",
        model_alias="rynmesh-qwen2.5-coder-7b-q4",
        display_name="Qwen2.5-Coder-7B-Instruct-Q4_K_M",
        parameter_millions=7000,
        quantization="Q4_K_M",
        url=_canonical_hf_url(
            "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
            "13fb94bfda8c8cf22497dc57b78f391a9acb426a",
            "qwen2.5-coder-7b-instruct-q4_k_m.gguf",
        ),
        sha256="509287f78cb4d4cf6b3843734733b914b2c158e43e22a7f4bf5e963800894d3c",
        size_bytes=4683073536,
        estimated_memory_mb=8000,
        context_window=32768,
        max_concurrent=1,
        agent_capable=True,
    ),
)

_BY_NAME: dict[str, ModelProfile] = {profile.profile: profile for profile in PROFILES}


def profile_by_name(name: str) -> ModelProfile:
    """Look up a catalog profile by name.

    Raises a plain `ValueError` (not `LifecycleError`) so this stays a
    dependency-free data module; every call site already treats `ValueError`
    the same way it treats `LifecycleError`.
    """
    try:
        return _BY_NAME[str(name)]
    except KeyError:
        raise ValueError(
            f"unknown model profile: {name!r}; choose one of {', '.join(_BY_NAME)}"
        ) from None


def estimated_disk_mb(profile: ModelProfile) -> int:
    """Conservative disk headroom for a profile: model weights plus overhead."""
    return int(profile.estimated_memory_mb * 1.5 + 256)
