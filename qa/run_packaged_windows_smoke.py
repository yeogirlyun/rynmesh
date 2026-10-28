"""Cold-start a packaged Windows desktop build and verify its frozen node."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path
from typing import Any


def get_json(url: str, *, timeout: float = 2.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def window_titles(process_id: int) -> list[str]:
    titles: list[str] = []
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(window: int, _parameter: int) -> bool:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value != process_id:
            return True
        length = user32.GetWindowTextLengthW(window)
        if length:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(window, buffer, len(buffer))
            titles.append(buffer.value)
        return True

    user32.EnumWindows(visit, 0)
    return titles


def wait_json(url: str, process: subprocess.Popen[bytes], timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error = "not attempted"
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"packaged app exited early with code {code}")
        try:
            return get_json(url)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = type(exc).__name__
            time.sleep(0.5)
    raise RuntimeError(f"timed out waiting for {url}: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    app = args.app.resolve(strict=True)
    work_root = args.work_root.resolve()
    if work_root.exists():
        raise SystemExit(f"work root must not exist: {work_root}")
    if port_is_open(args.port):
        raise SystemExit(f"port is already in use: {args.port}")
    work_root.mkdir(parents=True)

    env = os.environ.copy()
    env.update(
        {
            "RYNMESH_HOME": str(work_root / "node"),
            "RYNMESH_NETWORK_DIR": str(work_root / "network"),
            "RYNMESH_PEER_HOST": "127.0.0.1",
            "RYNMESH_PEER_PUBLIC_HOST": "127.0.0.1",
            "RYNMESH_PEER_ENDPOINT": f"http://127.0.0.1:{args.port}",
            "RYNMESH_PEER_PORT": str(args.port),
            "RYNMESH_AUTO_REGISTER": "0",
        }
    )
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    process = subprocess.Popen(
        [str(app)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        startupinfo=startup,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    result: dict[str, Any] = {}
    try:
        base = f"http://127.0.0.1:{args.port}"
        health = wait_json(f"{base}/health", process, min(args.timeout, 60.0))
        deadline = time.monotonic() + args.timeout
        first: dict[str, Any] = {}
        while time.monotonic() < deadline:
            first = get_json(f"{base}/api/local/first-success")
            if first.get("phase") == "ready":
                break
            time.sleep(1)
        if first.get("phase") != "ready":
            raise RuntimeError(f"first success did not reach ready: {first.get('phase')}")
        titles = window_titles(process.pid)
        if "Ryn" not in titles:
            raise RuntimeError(f"packaged app did not create the Ryn window: {titles}")
        result = {
            "version": "ryn.packaged-windows-smoke.v1",
            "result": "passed",
            "health": health.get("status"),
            "desktop_managed": health.get("desktop_managed"),
            "first_success_phase": first.get("phase"),
            "content_ready": first.get("content_ready"),
            "item_count": first.get("item_count"),
            "source_count": first.get("source_count"),
            "healthy_sources": first.get("healthy_sources"),
            "failed_sources": first.get("failed_sources"),
            "using_cache": first.get("using_cache"),
            "app_name": app.name,
            "window_title": "Ryn",
        }
        if result["health"] != "ok" or result["desktop_managed"] is not True:
            raise RuntimeError(f"unexpected packaged health: {result}")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and port_is_open(args.port):
            time.sleep(0.25)
        if port_is_open(args.port):
            raise RuntimeError(f"packaged sidecar leaked on port {args.port}")
        shutil.rmtree(work_root)
    print(json.dumps({"shutdown": "clean", "port_released": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
