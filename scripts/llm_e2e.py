"""One-command Docker two-node LLM E2E launcher and verifier."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

from rynmesh.transport import network_key_header

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "llm-e2e" / "docker-compose.yml"
TOKEN_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer rynmesh-e2e-browser-token",
}
TOKEN_HEADERS.update(network_key_header())


def _compose(*args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(["docker", "compose", "-f", str(COMPOSE), *args], cwd=ROOT,
                   env=env, check=True)


def _json(url: str, body: dict[str, Any] | None = None, timeout: float = 20) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers=TOKEN_HEADERS, method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _wait(url: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _json(url, timeout=2)
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"timed out waiting for {url}")


def up(mode: str) -> None:
    env = os.environ.copy()
    profile = "real" if mode == "real" else "test" if mode in {"test", "relay-test"} else ""
    if mode == "real":
        if not env.get("RYNMESH_REAL_MODEL_PATH"):
            raise RuntimeError("set RYNMESH_REAL_MODEL_PATH to a readable GGUF file")
        env["RYNMESH_LLM_MANIFEST"] = "/config/real-manifest.json"
    elif mode == "host-real":
        env["RYNMESH_LLM_MANIFEST"] = "/config/host-real-manifest.json"
    if mode == "relay-test":
        env["RYNMESH_LLM_FORCE_RELAY"] = "1"
    args = ["up", "-d", "--build"]
    if profile:
        args = ["--profile", profile, *args]
    _compose(*args, env=env)
    _wait("http://127.0.0.1:18890/health")
    _wait("http://127.0.0.1:18891/health")
    _wait("http://127.0.0.1:18892/health")


def verify(mode: str) -> dict[str, Any]:
    service_id = {"test": "e2e-test-service", "relay-test": "e2e-test-service", "real": "e2e-real-service",
                  "host-real": "e2e-host-real-service"}[mode]
    published = _json("http://127.0.0.1:18891/api/local/llm/services/publish", {
        "network_id": "rynmesh-llm-e2e", "benchmark": True,
    }, timeout=180)
    provider_id = str(published["record"]["peer_id"])
    deadline = time.monotonic() + 30
    discovered: dict[str, Any] = {}
    while time.monotonic() < deadline:
        discovered = _json("http://127.0.0.1:18892/api/local/llm/services?network_id=rynmesh-llm-e2e")
        if any(item.get("peer_id") == provider_id for item in discovered.get("services", [])):
            break
        time.sleep(1)
    result = _json("http://127.0.0.1:18892/api/local/llm/orders", {
        "network_id": "rynmesh-llm-e2e", "provider_peer_id": provider_id,
        "service_id": service_id,
        "prompt": "Reply with a short confirmation that the encrypted two-node path works.",
        "max_tokens": 32,
    }, timeout=240)
    consumer_balance = _json("http://127.0.0.1:18892/api/local/task-balance")
    provider_balance: dict[str, Any] = {}
    earning_id = "earning:" + str(result.get("task_id") or "")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        provider_balance = _json("http://127.0.0.1:18891/api/local/task-balance")
        if any(event.get("event_id") == earning_id for event in provider_balance.get("events", [])):
            break
        time.sleep(0.5)
    orders = _json("http://127.0.0.1:18892/api/local/llm/orders")
    provider_orders = _json("http://127.0.0.1:18891/api/local/llm/provider-orders")
    output = str(result.get("output") or "")
    consumer_record = next((item for item in orders.get("orders", [])
                            if item.get("task_id") == result.get("task_id")), {})
    provider_record = next((item for item in provider_orders.get("orders", [])
                            if item.get("task_id") == result.get("task_id")), {})
    report = {
        "profile": "deterministic-test" if mode == "test" else mode, "provider_peer_id": provider_id,
        "discovered_service_count": len(discovered.get("services", [])), "task_id": result.get("task_id"),
        "state": result.get("state"), "model_alias": result.get("model_alias"),
        "input_tokens": result.get("input_tokens"), "output_tokens": result.get("output_tokens"),
        "duration_ms": result.get("duration_ms"), "amount": result.get("amount"),
        "output_sha256": hashlib.sha256(output.encode()).hexdigest(), "output_preview": output[:160],
        "consumer_task_balance": {k: consumer_balance.get(k) for k in ("available", "held", "earned")},
        "provider_task_balance": {k: provider_balance.get(k) for k in ("available", "held", "earned")},
        "provider_earning_recorded_once": sum(
            event.get("event_id") == earning_id for event in provider_balance.get("events", [])
        ) == 1,
        "order_states": consumer_record.get("history", []),
        "provider_order_states": provider_record.get("history", []),
    }
    if result.get("state") != "succeeded" or not output:
        raise RuntimeError(f"E2E task failed: {report}")
    results_dir = ROOT / "deploy" / "llm-e2e" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / (mode + "-result.json")).write_text(
        json.dumps(report, indent=2), encoding="utf-8",
    )
    print(json.dumps(report, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=[
        "up", "verify", "run", "real-up", "real-verify", "real-run",
        "host-real-up", "host-real-verify", "host-real-run", "down", "clean",
        "relay-up", "relay-verify", "relay-run",
    ])
    args = parser.parse_args()
    mode = ("host-real" if args.command.startswith("host-real-") else "real"
            if args.command.startswith("real-") else "relay-test"
            if args.command.startswith("relay-") else "test")
    if args.command in {"up", "real-up", "host-real-up", "relay-up"}:
        up(mode)
    elif args.command in {"verify", "real-verify", "host-real-verify", "relay-verify"}:
        verify(mode)
    elif args.command in {"run", "real-run", "host-real-run", "relay-run"}:
        up(mode)
        verify(mode)
    elif args.command == "down":
        _compose("--profile", "test", "--profile", "real", "down")
    elif args.command == "clean":
        # Removes only named E2E containers, networks, and Docker volumes. It
        # never touches host GGUF files mounted read-only into the real profile.
        _compose("--profile", "test", "--profile", "real", "down", "--volumes", "--remove-orphans")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
