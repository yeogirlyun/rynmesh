#!/usr/bin/env python3
"""Bind a completed soak audit to independently observed process shutdown."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.audit_peer_transit import AuditError, audit_soak_report
except ModuleNotFoundError:  # direct ``python scripts/...`` execution
    from audit_peer_transit import AuditError, audit_soak_report


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        raise AuditError("process ID must be positive")
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            process_query_limited_information,
            False,
            pid,
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        error = ctypes.windll.kernel32.GetLastError()  # type: ignore[attr-defined]
        if error == 87:  # ERROR_INVALID_PARAMETER: no process owns this PID.
            return False
        if error == 5:  # ERROR_ACCESS_DENIED still proves that the process exists.
            return True
        raise AuditError(f"unable to inspect process {pid}: Windows error {error}")

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def finalize_soak(
    progress_path: Path,
    *,
    require_duration_s: float,
    min_sessions: int,
    launcher_pid: int | None = None,
) -> dict[str, Any]:
    progress_path = progress_path.expanduser().resolve()
    progress_bytes = progress_path.read_bytes()
    value = json.loads(progress_bytes)
    if not isinstance(value, dict):
        raise AuditError("soak progress root must be a JSON object")
    try:
        worker_pid = int(value["pid"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditError("soak progress is missing a valid worker PID") from exc

    if _process_exists(worker_pid):
        raise AuditError("soak worker process is still running")
    if launcher_pid is not None and _process_exists(launcher_pid):
        raise AuditError("soak launcher process is still running")

    audit = audit_soak_report(
        value,
        require_duration_s=require_duration_s,
        min_sessions=min_sessions,
        artifact_root=progress_path.parent,
    )

    # Check again after the artifact scan so a live/reused PID cannot pass a
    # time-of-check/time-of-use window. A process that does not exist cannot
    # own a UDP endpoint, which is stronger than a best-effort socket listing.
    if _process_exists(worker_pid):
        raise AuditError("soak worker process appeared during final audit")
    if launcher_pid is not None and _process_exists(launcher_pid):
        raise AuditError("soak launcher process appeared during final audit")

    return {
        "ok": True,
        "kind": "peer_transit_soak_final_audit",
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "progress_path": str(progress_path),
        "progress_sha256": hashlib.sha256(progress_bytes).hexdigest(),
        "worker_pid": worker_pid,
        "worker_process_alive": False,
        "launcher_pid": launcher_pid,
        "launcher_process_alive": False if launcher_pid is not None else None,
        "owned_udp_endpoints": 0,
        "udp_endpoint_proof": "owner_process_absent",
        "soak_audit": audit,
    }


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("progress", help="completed soak progress JSON")
    parser.add_argument("--require-duration-seconds", type=float, default=86400.0)
    parser.add_argument("--min-sessions", type=int, default=3)
    parser.add_argument("--launcher-pid", type=int)
    parser.add_argument("--output", default="", help="final shutdown audit JSON")
    args = parser.parse_args()
    if args.require_duration_seconds < 0 or args.min_sessions < 0:
        raise AuditError("final soak audit requirements cannot be negative")

    progress_path = Path(args.progress)
    report = finalize_soak(
        progress_path,
        require_duration_s=args.require_duration_seconds,
        min_sessions=args.min_sessions,
        launcher_pid=args.launcher_pid,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    output = Path(args.output) if args.output else progress_path.parent / "final-audit.json"
    _write_json_atomic(output, report)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
