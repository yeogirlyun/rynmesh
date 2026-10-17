"""Body-free Agent failure diagnostics; never log prompts, paths or exception text."""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

_log = logging.getLogger(__name__)
_CODE = re.compile(r"agent_[a-z0-9_]{1,100}\Z")


def failure(stage, exc, *, elapsed_seconds=None):
    chain, seen, current = [], set(), exc
    while current is not None and id(current) not in seen and len(chain) < 5:
        seen.add(id(current))
        name = re.sub(r"[^A-Za-z0-9_]", "", type(current).__name__)[:80]
        code = str(current) if type(current).__module__ == "rynmesh.agent_runtime.store" else ""
        chain.append({"type": name, **({"code": code} if _CODE.fullmatch(code) else {}),
                      **{key: value for key in ("errno", "winerror")
                         if type(value := getattr(current, key, None)) is int}})
        current = current.__cause__ or current.__context__
    # stage is a host-authored route/tool phase, never a request argument.
    _log.warning("agent_failure at=%s pid=%s stage=%s elapsed_seconds=%s causes=%s",
                 datetime.now(timezone.utc).isoformat(), os.getpid(), stage,
                 round(elapsed_seconds, 3) if elapsed_seconds is not None else None, chain)


def performance(stage, **phases):
    if sum(phases.values()) >= 1.0:
        _log.warning("agent_slow at=%s pid=%s stage=%s phases=%s",
                     datetime.now(timezone.utc).isoformat(), os.getpid(), stage,
                     {key: round(value, 3) for key, value in phases.items()})
