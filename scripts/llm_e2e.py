"""One-command Docker two-node LLM E2E launcher and verifier."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
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


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
        capture_output=True, text=True,
    )
    return result.stdout.strip()


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


def _parse_sse_lines(lines: Any, *, now_ns: Any = time.monotonic_ns) -> list[dict[str, Any]]:
    """Parse an SSE byte-line iterator and timestamp each complete event."""
    events: list[dict[str, Any]] = []
    event_name = "message"
    data_lines: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n") if isinstance(raw, bytes) else str(raw).rstrip("\r\n")
        if not line:
            if data_lines:
                value = json.loads("\n".join(data_lines))
                if not isinstance(value, dict):
                    raise RuntimeError("E2E SSE data must be a JSON object")
                events.append({"event": event_name, "received_ns": int(now_ns()), "data": value})
                if event_name in {"complete", "error"}:
                    break
            event_name = "message"
            data_lines = []
        elif line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    return events


def _sse_events(url: str, *, timeout: float = 300) -> list[dict[str, Any]]:
    request = urllib.request.Request(url, headers=TOKEN_HEADERS, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return _parse_sse_lines(response)


def _mode_order(mode: str, *, provider_id: str, service_id: str, task_id: str) -> dict[str, Any]:
    requested_transport = {
        "test": "p2p",
        "relay-test": "relay",
        "stream-test": "direct",
    }.get(mode, "auto")
    order = {
        "network_id": "rynmesh-llm-e2e",
        "provider_peer_id": provider_id,
        "service_id": service_id,
        "prompt": "Reply with a short confirmation that the encrypted two-node path works.",
        "max_tokens": 32,
        "transport": requested_transport,
        "task_id": task_id,
    }
    if mode in {"test", "relay-test", "stream-test"}:
        # Explicit P2P/Relay must still produce complete-only delivery; direct
        # is the one deterministic profile expected to emit delta events.
        order["response_mode"] = "stream-v1"
    return order


def up(mode: str) -> None:
    env = os.environ.copy()
    profile = "real" if mode == "real" else "test" if mode in {"test", "relay-test", "stream-test"} else ""
    if mode == "real":
        if not env.get("RYNMESH_REAL_MODEL_PATH"):
            raise RuntimeError("set RYNMESH_REAL_MODEL_PATH to a readable GGUF file")
        env["RYNMESH_LLM_MANIFEST"] = "/config/real-manifest.json"
    elif mode == "host-real":
        env["RYNMESH_LLM_MANIFEST"] = "/config/host-real-manifest.json"
    elif mode in {"test", "relay-test", "stream-test"}:
        env["RYNMESH_LLM_MANIFEST"] = "/config/test-manifest.json"
    # Never inherit an operator's route override into deterministic evidence.
    env["RYNMESH_LLM_FORCE_RELAY"] = "1" if mode == "relay-test" else "0"
    args = ["up", "-d", "--build"]
    if profile:
        args = ["--profile", profile, *args]
    _compose(*args, env=env)
    _wait("http://127.0.0.1:18890/health")
    _wait("http://127.0.0.1:18891/health")
    _wait("http://127.0.0.1:18892/health")


def verify(mode: str) -> dict[str, Any]:
    service_id = {"test": "e2e-test-service", "relay-test": "e2e-test-service",
                  "stream-test": "e2e-test-service", "real": "e2e-real-service",
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
    expected_transport = {
        "relay-test": "encrypted_relay",
        "test": "ice_udp_direct",
        "stream-test": "peer_http_direct",
    }.get(mode, "")
    task_id = f"e2e-{mode}-{time.time_ns()}"
    order = _mode_order(mode, provider_id=provider_id, service_id=service_id, task_id=task_id)
    submitted_ns = time.monotonic_ns()
    queued = _json(
        "http://127.0.0.1:18892/api/local/llm/orders/async", order, timeout=30,
    )
    submit_returned_ns = time.monotonic_ns()
    events = _sse_events(
        f"http://127.0.0.1:18892/api/local/llm/orders/{task_id}/events?after_sequence=-1",
        timeout=300,
    )
    terminal = next((event for event in events if event["event"] in {"complete", "error"}), None)
    if terminal is None:
        raise RuntimeError("E2E event stream ended without a terminal event")
    result = dict(terminal["data"])
    result.setdefault("task_id", task_id)
    consumer_balance = _json("http://127.0.0.1:18892/api/local/task-balance")
    provider_balance: dict[str, Any] = {}
    earning_id = "earning:" + task_id
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
    delta_events = [event for event in events if event["event"] == "delta"]
    first_delta_ns = int(delta_events[0]["received_ns"]) if delta_events else 0
    terminal_ns = int(terminal["received_ns"])
    consumer_events = list(consumer_balance.get("events", []))
    consumer_holds = [
        event for event in consumer_events
        if event.get("kind") == "hold" and event.get("task_id") == task_id
    ]
    consumer_settlements = [
        event for event in consumer_events
        if event.get("kind") == "settle" and event.get("task_id") == task_id
    ]
    event_evidence = [
        {
            "event": event["event"],
            "after_submit_ms": round((int(event["received_ns"]) - submitted_ns) / 1_000_000, 3),
            "sequence": event["data"].get("sequence"),
            "state": event["data"].get("state"),
            "delta_bytes": len(str(event["data"].get("delta") or "").encode("utf-8")),
        }
        for event in events
    ]
    report = {
        "commit": _git_commit(), "platform": platform.platform(),
        "python": sys.version.split()[0],
        "profile": "deterministic-test" if mode == "test" else mode, "provider_peer_id": provider_id,
        "discovered_service_count": len(discovered.get("services", [])), "task_id": task_id,
        "queued_state": queued.get("state"), "requested_transport": order["transport"],
        "requested_response_mode": order.get("response_mode", "complete-v1"),
        "state": result.get("state"), "model_alias": result.get("model_alias"),
        "input_tokens": result.get("input_tokens"), "output_tokens": result.get("output_tokens"),
        "duration_ms": result.get("duration_ms"), "amount": result.get("amount"),
        "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
        "consumer_task_balance": {k: consumer_balance.get(k) for k in ("available", "held", "earned")},
        "provider_task_balance": {k: provider_balance.get(k) for k in ("available", "held", "earned")},
        "consumer_hold_recorded_once": len(consumer_holds) == 1,
        "consumer_settlement_recorded_once": len(consumer_settlements) == 1,
        "provider_earning_recorded_once": sum(
            event.get("event_id") == earning_id for event in provider_balance.get("events", [])
        ) == 1,
        "stream_event_count": len(delta_events),
        "timing": {
            "submit_returned_ms": round((submit_returned_ns - submitted_ns) / 1_000_000, 3),
            "first_delta_ms": round((first_delta_ns - submitted_ns) / 1_000_000, 3) if first_delta_ns else None,
            "terminal_ms": round((terminal_ns - submitted_ns) / 1_000_000, 3),
            "total_ms": round((terminal_ns - submitted_ns) / 1_000_000, 3),
            "first_delta_before_terminal": bool(first_delta_ns and first_delta_ns < terminal_ns),
        },
        "events": event_evidence,
        "order_states": consumer_record.get("history", []),
        "provider_order_states": provider_record.get("history", []),
    }
    if result.get("state") != "succeeded" or not output:
        raise RuntimeError(f"E2E task failed: {report}")
    if result.get("task_id") != task_id:
        raise RuntimeError("E2E terminal task id does not match the submitted task")
    if not report["consumer_hold_recorded_once"] \
            or not report["consumer_settlement_recorded_once"] \
            or not report["provider_earning_recorded_once"]:
        raise RuntimeError("E2E exactly-once settlement evidence failed")
    if expected_transport and result.get("transport") != expected_transport:
        raise RuntimeError(
            f"E2E transport mismatch: expected {expected_transport}, got {result.get('transport')}"
        )
    if expected_transport and bool(dict(result.get("transport_evidence") or {}).get("relay_used")) \
            != (expected_transport == "encrypted_relay"):
        raise RuntimeError("E2E relay evidence does not match the requested transport")
    if mode == "stream-test":
        if not delta_events or not report["timing"]["first_delta_before_terminal"]:
            raise RuntimeError("direct stream did not deliver a delta before terminal")
        if dict(result.get("transport_evidence") or {}).get("stream_protocol") != "rynmesh.llm.stream.v1":
            raise RuntimeError("direct stream result lacks stream-v1 transport evidence")
    elif mode in {"test", "relay-test"} and delta_events:
        raise RuntimeError("explicit P2P/Relay must keep complete-response delivery")
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
        "stream-up", "stream-verify", "stream-run",
    ])
    args = parser.parse_args()
    mode = ("host-real" if args.command.startswith("host-real-") else "real"
            if args.command.startswith("real-") else "relay-test"
            if args.command.startswith("relay-") else "test")
    if args.command.startswith("stream-"):
        mode = "stream-test"
    if args.command in {"up", "real-up", "host-real-up", "relay-up", "stream-up"}:
        up(mode)
    elif args.command in {"verify", "real-verify", "host-real-verify", "relay-verify", "stream-verify"}:
        verify(mode)
    elif args.command in {"run", "real-run", "host-real-run", "relay-run", "stream-run"}:
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
