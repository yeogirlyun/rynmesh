"""Safe configuration and environment overrides for host-initiated and literal read-only Git commands."""
from __future__ import annotations

import os
from collections.abc import Mapping

GIT_NULL_HOOKS_PATH = "NUL" if os.name == "nt" else "/dev/null"

# Static default overrides without fsmonitor capability probing: Ryn prioritizes
# deterministic safety over watchman/fsmonitor acceleration on untrusted workspaces.
SAFE_GIT_CONFIG: tuple[str, ...] = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    f"core.hooksPath={GIT_NULL_HOOKS_PATH}",
    "-c",
    "safe.bareRepository=explicit",
)

GIT_READONLY_DIFF_SUBCOMMANDS = frozenset({"diff", "show", "log"})
GIT_READONLY_DIFF_FLAGS: tuple[str, ...] = ("--no-ext-diff", "--no-textconv")


def strip_rynmesh_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of *env* without node-private RYNMESH_* variables."""
    return {
        str(key): str(value)
        for key, value in env.items()
        if not str(key).upper().startswith("RYNMESH_")
    }


def safe_git_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a RYNMESH_*-stripped copy of *env* with GIT_OPTIONAL_LOCKS=0."""
    source = os.environ if env is None else env
    cleaned = strip_rynmesh_env(source)
    cleaned["GIT_OPTIONAL_LOCKS"] = "0"
    return cleaned
