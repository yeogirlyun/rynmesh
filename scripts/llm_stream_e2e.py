"""One-command local or Docker two-node LLM E2E launcher and verifier."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
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
E2E_PROMPT = "Reply with a short confirmation that the encrypted two-node path works."
LOCAL_PROCESS_NAMES = ("registry", "adapter", "provider", "consumer")


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


def _free_loopback_port() -> int:
    """Reserve a currently free loopback port long enough to choose test endpoints."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _result_path(mode: str) -> Path:
    results_dir = ROOT / "deploy" / "llm-e2e" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    return results_dir / (mode + "-result.json")


def _write_report(mode: str, report: dict[str, Any]) -> None:
    _result_path(mode).write_text(json.dumps(report, indent=2), encoding="utf-8")


def _discovered_service_id(record: dict[str, Any]) -> str:
    return str(dict(record.get("service") or {}).get("package_id") or record.get("package_id") or "")


def _sanitized_history(history: Any) -> list[dict[str, Any]]:
    allowed = {
        "amount", "at", "checkpoint", "duration_ms", "error_code", "input_tokens",
        "network_id", "output_tokens", "relay_used", "response_expires_at", "service_id",
        "settlement_dispatched", "settlement_pending", "state", "stream_events",
        "stream_protocol", "transport", "transport_evidence",
    }
    return [
        {key: value for key, value in dict(item).items() if key in allowed}
        for item in list(history or [])
        if isinstance(item, dict)
    ]


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
        # Explicit readline avoids buffered iterator/readlines implementations
        # coalescing the full SSE response and destroying first-delta timing.
        return _parse_sse_lines(iter(response.readline, b""))


def _event_monotonic_ns(event: dict[str, Any]) -> int:
    """Prefer Consumer emission time so HTTP buffering cannot falsify timing."""
    emitted = event.get("data", {}).get("emitted_monotonic_ns")
    return int(emitted) if emitted is not None else int(event["received_ns"])


def _mode_order(mode: str, *, provider_id: str, service_id: str, task_id: str) -> dict[str, Any]:
    requested_transport = {
        "test": "p2p",
        "relay-test": "relay",
        "stream-test": "direct",
        "local-stream": "direct",
        "local-fallback": "direct",
    }.get(mode, "auto")
    order = {
        "network_id": "rynmesh-llm-e2e",
        "provider_peer_id": provider_id,
        "service_id": service_id,
        "prompt": E2E_PROMPT,
        "max_tokens": 32,
        "transport": requested_transport,
        "task_id": task_id,
    }
    if mode in {"test", "relay-test", "stream-test", "local-stream", "local-fallback"}:
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


def verify(
    mode: str,
    *,
    provider_base: str = "http://127.0.0.1:18891",
    consumer_base: str = "http://127.0.0.1:18892",
    emit: bool = True,
) -> dict[str, Any]:
    service_id = {"test": "e2e-test-service", "relay-test": "e2e-test-service",
                  "stream-test": "e2e-test-service", "real": "e2e-real-service",
                  "host-real": "e2e-host-real-service",
                  "local-stream": "e2e-local-stream-service",
                  "local-fallback": "e2e-local-fallback-service"}[mode]
    published = _json(provider_base + "/api/local/llm/services/publish", {
        "network_id": "rynmesh-llm-e2e", "benchmark": True,
    }, timeout=180)
    provider_id = str(published["record"]["peer_id"])
    deadline = time.monotonic() + 30
    discovered: dict[str, Any] = {}
    while time.monotonic() < deadline:
        discovered = _json(consumer_base + "/api/local/llm/services?network_id=rynmesh-llm-e2e")
        if any(
            item.get("peer_id") == provider_id and _discovered_service_id(item) == service_id
            for item in discovered.get("services", [])
        ):
            break
        time.sleep(1)
    expected_transport = {
        "relay-test": "encrypted_relay",
        "test": "ice_udp_direct",
        "stream-test": "peer_http_direct",
        "local-stream": "peer_http_direct",
        "local-fallback": "peer_http_direct",
    }.get(mode, "")
    task_id = f"e2e-{mode}-{time.time_ns()}"
    order = _mode_order(mode, provider_id=provider_id, service_id=service_id, task_id=task_id)
    submitted_ns = time.monotonic_ns()
    queued = _json(
        consumer_base + "/api/local/llm/orders/async", order, timeout=30,
    )
    submit_returned_ns = time.monotonic_ns()
    events = _sse_events(
        f"{consumer_base}/api/local/llm/orders/{task_id}/events?after_sequence=-1",
        timeout=300,
    )
    terminal = next((event for event in events if event["event"] in {"complete", "error"}), None)
    if terminal is None:
        raise RuntimeError("E2E event stream ended without a terminal event")
    result = dict(terminal["data"])
    result.setdefault("task_id", task_id)
    duplicate = _json(
        consumer_base + "/api/local/llm/orders/async", order, timeout=30,
    )
    consumer_balance = _json(consumer_base + "/api/local/task-balance")
    provider_balance: dict[str, Any] = {}
    earning_id = "earning:" + task_id
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        provider_balance = _json(provider_base + "/api/local/task-balance")
        if any(event.get("event_id") == earning_id for event in provider_balance.get("events", [])):
            break
        time.sleep(0.5)
    orders = _json(consumer_base + "/api/local/llm/orders")
    provider_orders = _json(provider_base + "/api/local/llm/provider-orders")
    output = str(result.get("output") or "")
    consumer_record = next((item for item in orders.get("orders", [])
                            if item.get("task_id") == result.get("task_id")), {})
    provider_record = next((item for item in provider_orders.get("orders", [])
                            if item.get("task_id") == result.get("task_id")), {})
    consumer_task_records = [
        item for item in orders.get("orders", []) if item.get("task_id") == task_id
    ]
    provider_task_records = [
        item for item in provider_orders.get("orders", []) if item.get("task_id") == task_id
    ]
    delta_events = [event for event in events if event["event"] == "delta"]
    first_delta_ns = _event_monotonic_ns(delta_events[0]) if delta_events else 0
    terminal_ns = _event_monotonic_ns(terminal)
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
            "after_submit_ms": round((_event_monotonic_ns(event) - submitted_ns) / 1_000_000, 3),
            "sequence": event["data"].get("sequence"),
            "state": event["data"].get("state"),
            "delta_bytes": len(str(event["data"].get("delta") or "").encode("utf-8")),
        }
        for event in events
    ]
    discovered_record = next(
        (
            item for item in discovered.get("services", [])
            if item.get("peer_id") == provider_id and _discovered_service_id(item) == service_id
        ),
        {},
    )
    report = {
        "commit": _git_commit(), "platform": platform.platform(),
        "python": sys.version.split()[0],
        "profile": "deterministic-test" if mode == "test" else mode,
        "provider_peer_id_sha256": hashlib.sha256(provider_id.encode()).hexdigest(),
        "discovered_service_count": len(discovered.get("services", [])), "task_id": task_id,
        "discovered_delivery_protocols": list(discovered_record.get("delivery_protocols") or []),
        "queued_state": queued.get("state"), "requested_transport": order["transport"],
        "requested_response_mode": order.get("response_mode", "complete-v1"),
        "effective_response_mode": (
            "stream-v1"
            if dict(result.get("transport_evidence") or {}).get("stream_protocol")
            == "rynmesh.llm.stream.v1"
            else "complete-v1"
        ),
        "duplicate_submission_state": duplicate.get("state"),
        "duplicate_submission_reused_task": (
            duplicate.get("task_id") == task_id and duplicate.get("state") == result.get("state")
        ),
        "consumer_task_recorded_once": len(consumer_task_records) == 1,
        "provider_task_recorded_once": len(provider_task_records) == 1,
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
        "order_states": _sanitized_history(consumer_record.get("history", [])),
        "provider_order_states": _sanitized_history(provider_record.get("history", [])),
    }
    if result.get("state") != "succeeded" or not output:
        raise RuntimeError(f"E2E task failed: {report}")
    if result.get("task_id") != task_id:
        raise RuntimeError("E2E terminal task id does not match the submitted task")
    if not report["duplicate_submission_reused_task"]:
        raise RuntimeError("duplicate submission did not reuse the original terminal task")
    if not report["consumer_task_recorded_once"] or not report["provider_task_recorded_once"]:
        raise RuntimeError("duplicate submission created an additional task record")
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
    if mode in {"stream-test", "local-stream"}:
        if not delta_events or not report["timing"]["first_delta_before_terminal"]:
            raise RuntimeError(
                "direct stream did not deliver a delta before terminal: "
                + json.dumps({
                    "delivery_protocols": report["discovered_delivery_protocols"],
                    "events": event_evidence,
                    "timing": report["timing"],
                    "transport_evidence": result.get("transport_evidence"),
                })
            )
        if dict(result.get("transport_evidence") or {}).get("stream_protocol") != "rynmesh.llm.stream.v1":
            raise RuntimeError("direct stream result lacks stream-v1 transport evidence")
    elif mode in {"test", "relay-test", "local-fallback"} and delta_events:
        raise RuntimeError("complete-response fallback unexpectedly emitted delta events")
    if mode == "local-stream" and "stream-v1" not in report["discovered_delivery_protocols"]:
        raise RuntimeError("local streaming Provider did not advertise stream-v1")
    if mode == "local-fallback" and "stream-v1" in report["discovered_delivery_protocols"]:
        raise RuntimeError("local fallback Provider unexpectedly advertised stream-v1")
    _write_report(mode, report)
    if emit:
        print(json.dumps(report, indent=2))
    return report


def _local_environment(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "RYNMESH_DESKTOP_MODE",
        "RYNMESH_LLM_FORCE_RELAY",
        "RYNMESH_LLM_MANIFEST",
        "RYNMESH_LLM_SERVICE_MANIFEST",
        "RYNMESH_LLM_TRANSPORT",
        "RYNMESH_NETWORK_KEY",
        "RYNMESH_NETWORK_KEY_ID",
        "RYNMESH_REGISTRY_URLS",
        "RYNMESH_RELAY_URL",
    ):
        env.pop(key, None)
    env.update({
        "PYTHONUNBUFFERED": "1",
        "RYNMESH_NETWORK_ID": "rynmesh-llm-e2e",
        "RYNMESH_REGISTRY_URL": "",
        "RYNMESH_AUTO_REGISTER": "1",
        "RYNMESH_HOME": str(root / "base-home"),
        "RYNMESH_NETWORK_DIR": str(root / "base-network"),
    })
    return env


def _start_local_process(
    name: str,
    argv: list[str],
    *,
    env: dict[str, str],
    log_dir: Path,
) -> tuple[subprocess.Popen[bytes], Any, Path]:
    log_path = log_dir / f"{name}.log"
    log_handle = log_path.open("wb")
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        process = subprocess.Popen(
            argv,
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
    except Exception:
        log_handle.close()
        raise
    return process, log_handle, log_path


def _stop_local_processes(
    processes: list[tuple[str, subprocess.Popen[bytes], Any, Path]],
) -> dict[str, int]:
    for _name, process, _handle, _path in reversed(processes):
        if process.poll() is None:
            process.terminate()
    for _name, process, _handle, _path in reversed(processes):
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=8)
    exit_codes = {name: int(process.returncode or 0) for name, process, _handle, _path in processes}
    for _name, _process, handle, _path in processes:
        handle.close()
    return exit_codes


def _local_log_evidence(
    processes: list[tuple[str, subprocess.Popen[bytes], Any, Path]],
) -> dict[str, Any]:
    markers = _private_body_markers()
    logs: dict[str, Any] = {}
    for name, _process, _handle, path in processes:
        content = path.read_bytes()
        logs[name] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
            "prompt_or_output_body_absent": not any(marker in content for marker in markers),
        }
    return {
        "all_process_logs_body_free": all(
            item["prompt_or_output_body_absent"] for item in logs.values()
        ),
        "logs": logs,
    }


def _private_body_markers() -> tuple[bytes, bytes]:
    return (
        E2E_PROMPT.encode(),
        hashlib.sha256(E2E_PROMPT.encode()).hexdigest()[:12].encode(),
    )


def _local_storage_evidence(root: Path) -> dict[str, Any]:
    markers = _private_body_markers()
    scanned = 0
    body_free = True
    for path in root.rglob("*"):
        if not path.is_file() or path.parent.name == "logs":
            continue
        scanned += 1
        content = path.read_bytes()
        body_free = body_free and not any(marker in content for marker in markers)
    return {"scanned_file_count": scanned, "all_persistent_files_body_free": body_free}


def local_run(mode: str) -> dict[str, Any]:
    """Run a real four-process loopback E2E without Docker."""
    if mode not in {"local-stream", "local-fallback"}:
        raise ValueError(f"unsupported local mode: {mode}")
    ports: list[int] = []
    while len(ports) < len(LOCAL_PROCESS_NAMES):
        port = _free_loopback_port()
        if port not in ports:
            ports.append(port)
    registry_port, adapter_port, provider_port, consumer_port = ports
    with tempfile.TemporaryDirectory(prefix="rynmesh-llm-local-e2e-") as raw_root:
        temp_root = Path(raw_root)
        log_dir = temp_root / "logs"
        log_dir.mkdir()
        manifest = json.loads(
            (ROOT / "deploy" / "llm-e2e" / "config" / "test-manifest.json").read_text(
                encoding="utf-8",
            )
        )
        manifest["package_id"] = (
            "e2e-local-stream-service" if mode == "local-stream"
            else "e2e-local-fallback-service"
        )
        manifest["base_url"] = f"http://127.0.0.1:{adapter_port}"
        manifest_path = temp_root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        base_env = _local_environment(temp_root)
        registry_env = base_env | {
            "RYNMESH_REGISTRY_HOST": "127.0.0.1",
            "RYNMESH_REGISTRY_PORT": str(registry_port),
            "RYNMESH_REGISTRY_DIR": str(temp_root / "registry"),
        }
        adapter_env = base_env | {
            "RYNMESH_TEST_ADAPTER_DISABLE_STREAM": (
                "0" if mode == "local-stream" else "1"
            ),
        }
        peer_common = base_env | {
            "RYNMESH_REGISTRY_URL": f"http://127.0.0.1:{registry_port}",
        }
        provider_env = peer_common | {
            "RYNMESH_HOME": str(temp_root / "provider-home"),
            "RYNMESH_NETWORK_DIR": str(temp_root / "provider-network"),
            "RYNMESH_NODE_NAME": "LLM Local Provider",
            "RYNMESH_PEER_HOST": "127.0.0.1",
            "RYNMESH_PEER_PORT": str(provider_port),
            "RYNMESH_PEER_ENDPOINT": f"http://127.0.0.1:{provider_port}",
            "RYNMESH_LLM_SERVICE_MANIFEST": str(manifest_path),
        }
        consumer_env = peer_common | {
            "RYNMESH_HOME": str(temp_root / "consumer-home"),
            "RYNMESH_NETWORK_DIR": str(temp_root / "consumer-network"),
            "RYNMESH_NODE_NAME": "LLM Local Consumer",
            "RYNMESH_PEER_HOST": "127.0.0.1",
            "RYNMESH_PEER_PORT": str(consumer_port),
            "RYNMESH_PEER_ENDPOINT": f"http://127.0.0.1:{consumer_port}",
        }
        commands = [
            ("registry", [sys.executable, "-m", "rynmesh.registry_http"], registry_env),
            (
                "adapter",
                [
                    sys.executable, "-m", "uvicorn", "rynmesh.llm_package.test_adapter:app",
                    "--host", "127.0.0.1", "--port", str(adapter_port),
                    "--log-level", "warning",
                ],
                adapter_env,
            ),
            ("provider", [sys.executable, "-m", "rynmesh.peer_http"], provider_env),
            ("consumer", [sys.executable, "-m", "rynmesh.peer_http"], consumer_env),
        ]
        health_urls = {
            "registry": f"http://127.0.0.1:{registry_port}/health",
            "adapter": f"http://127.0.0.1:{adapter_port}/health",
            "provider": f"http://127.0.0.1:{provider_port}/health",
            "consumer": f"http://127.0.0.1:{consumer_port}/health",
        }
        processes: list[tuple[str, subprocess.Popen[bytes], Any, Path]] = []
        report: dict[str, Any] | None = None
        exit_codes: dict[str, int] = {}
        try:
            for name, argv, env in commands:
                process, handle, log_path = _start_local_process(
                    name, argv, env=env, log_dir=log_dir,
                )
                processes.append((name, process, handle, log_path))
                _wait(health_urls[name], timeout=60)
            report = verify(
                mode,
                provider_base=f"http://127.0.0.1:{provider_port}",
                consumer_base=f"http://127.0.0.1:{consumer_port}",
                emit=False,
            )
        finally:
            exit_codes = _stop_local_processes(processes)
        log_evidence = _local_log_evidence(processes)
        storage_evidence = _local_storage_evidence(temp_root)
        if report is None:
            raise RuntimeError("local multiprocess verification did not produce a report")
        report.update({
            "runtime": "local-multiprocess",
            "process_count": len(processes),
            "ephemeral_loopback_ports": dict(
                zip(LOCAL_PROCESS_NAMES, ports, strict=True),
            ),
            "process_exit_codes_after_harness_shutdown": exit_codes,
            "all_processes_stopped": all(process.poll() is not None for _, process, _, _ in processes),
            "log_privacy": log_evidence,
            "storage_privacy": storage_evidence,
        })
        if len(processes) != len(LOCAL_PROCESS_NAMES):
            raise RuntimeError("local E2E did not start all four required HTTP processes")
        if not log_evidence["all_process_logs_body_free"]:
            raise RuntimeError("local E2E process logs contained a prompt or output body")
        if not storage_evidence["all_persistent_files_body_free"]:
            raise RuntimeError("local E2E persistent files contained a prompt or output body")
        _write_report(mode, report)
        print(json.dumps(report, indent=2))
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=[
        "up", "verify", "run", "real-up", "real-verify", "real-run",
        "host-real-up", "host-real-verify", "host-real-run", "down", "clean",
        "relay-up", "relay-verify", "relay-run",
        "stream-up", "stream-verify", "stream-run",
        "local-stream-run", "local-fallback-run", "local-run",
    ])
    args = parser.parse_args()
    mode = ("host-real" if args.command.startswith("host-real-") else "real"
            if args.command.startswith("real-") else "relay-test"
            if args.command.startswith("relay-") else "test")
    if args.command.startswith("stream-"):
        mode = "stream-test"
    if args.command == "local-run":
        local_run("local-stream")
        local_run("local-fallback")
    elif args.command == "local-stream-run":
        local_run("local-stream")
    elif args.command == "local-fallback-run":
        local_run("local-fallback")
    elif args.command in {"up", "real-up", "host-real-up", "relay-up", "stream-up"}:
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
