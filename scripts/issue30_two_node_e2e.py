#!/usr/bin/env python3
"""Run Issue #30 against two real local node processes without Docker.

This is a development-acceptance harness, not a substitute for cross-host or
installed-package release acceptance.  It uses two independent homes and TCP
ports, a local Registry process, and a deterministic in-process OpenAI-compatible
stub.  All Friend Mesh and Private AI actions go through the nodes' HTTP APIs.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rynmesh.friends import verify_invite  # noqa: E402

NETWORK_ID = "rynmesh-issue30-local-e2e"
SERVICE_ID = "issue30-e2e-model"
MODEL_ID = "issue30-deterministic-stub"
MODEL_OUTPUT = "RYNMESH ISSUE 30 PRIVATE AI OK"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha_ref(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", 0))
        return int(sock.getsockname()[1])


def choose_lan_address(configured: str = "") -> str:
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    else:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))
            candidates.append(str(probe.getsockname()[0]))
        except OSError:
            pass
        finally:
            probe.close()
        try:
            candidates.extend(
                item[4][0]
                for item in socket.getaddrinfo(
                    socket.gethostname(), None, family=socket.AF_INET, type=socket.SOCK_STREAM
                )
            )
        except OSError:
            pass
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.version == 4 and address.is_private and not (
            address.is_loopback or address.is_link_local or address.is_unspecified
        ):
            return str(address)
    raise RuntimeError(
        "No usable private LAN IPv4 address found; pass --host-address with an address "
        "bound on this machine. Loopback is intentionally rejected by the invite policy."
    )


def request_json(
    base: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    expected: tuple[int, ...] = (200,),
    timeout: float = 20,
) -> tuple[int, dict[str, Any] | list[Any]]:
    encoded = None
    headers = {"Accept": "application/json"}
    if body is not None:
        encoded = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(base.rstrip("/") + path, data=encoded, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            raw = response.read()
    except HTTPError as exc:
        status = int(exc.code)
        raw = exc.read()
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"HTTP request failed for {path}: {type(exc).__name__}") from exc
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Non-JSON response for {path} (HTTP {status})") from exc
    if status not in expected:
        detail = payload.get("detail", "") if isinstance(payload, dict) else ""
        raise RuntimeError(f"Unexpected HTTP {status} for {path}: {str(detail)[:160]}")
    if not isinstance(payload, (dict, list)):
        raise RuntimeError(f"Unexpected JSON root for {path}")
    return status, payload


def wait_ready(base: str, process: "ManagedProcess", *, timeout: float = 20) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error = "not started"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{process.name} exited before readiness; inspect its local log")
        try:
            _, payload = request_json(base, "/health", timeout=1)
            if isinstance(payload, dict) and payload.get("status") == "ok":
                return payload
        except RuntimeError as exc:
            last_error = str(exc)
        time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for {process.name}: {last_error}")


class ManagedProcess:
    def __init__(
        self,
        name: str,
        module: str,
        env: dict[str, str],
        log_path: Path,
    ) -> None:
        self.name = name
        self.module = module
        self.env = env
        self.log_path = log_path
        self.process: subprocess.Popen[bytes] | None = None
        self._log_handle: Any = None
        self.pids: list[int] = []

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError(f"{self.name} is already running")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_path.open("ab")
        self.process = subprocess.Popen(
            [sys.executable, "-m", self.module],
            cwd=ROOT,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
        )
        self.pids.append(int(self.process.pid))

    def poll(self) -> int | None:
        return None if self.process is None else self.process.poll()

    def stop(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.process = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None


class DeterministicModel:
    def __init__(self, port: int) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/v1/models":
                    self.send_error(404)
                    return
                self._send_json({"data": [{"id": MODEL_ID}]})

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0") or 0)
                try:
                    request_body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.send_error(400)
                    return
                with owner._lock:
                    owner.completion_calls += 1
                if bool(request_body.get("stream")):
                    body = (
                        'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                        "data: [DONE]\n\n"
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self._send_json(
                    {
                        "choices": [{"message": {"content": MODEL_OUTPUT}}],
                        "usage": {"prompt_tokens": 7, "completion_tokens": 7},
                    }
                )

            def _send_json(self, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                return

        self._lock = threading.Lock()
        self.completion_calls = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def calls(self) -> int:
        with self._lock:
            return self.completion_calls


def clean_environment(overrides: dict[str, str]) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("RYNMESH_") and key not in {"HTTP_PROXY", "HTTPS_PROXY"}
    }
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + current_pythonpath if current_pythonpath else "")
    env["PYTHONUTF8"] = "1"
    env.update(overrides)
    return env


def endpoint_probe(endpoint: str, *, node_label: str) -> dict[str, Any]:
    parsed = urlparse(endpoint)
    host = parsed.hostname or ""
    port = int(parsed.port or 80)
    answers = sorted(
        {
            str(item[4][0])
            for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        }
    )
    with socket.create_connection((host, port), timeout=5) as connection:
        peer_host, peer_port = connection.getpeername()[:2]
    return {
        "endpoint": f"http://<private-lan-ip>:<{node_label}-port>",
        "address_class": "private-lan",
        "dns_or_literal_answers": ["<private-lan-ip>" for _ in answers],
        "answer_count": len(answers),
        "socket_peer": f"<private-lan-ip>:<{node_label}-port>",
        "socket_port_matches": int(peer_port) == port,
        "socket_address_was_reviewed": str(peer_host) in answers,
        "url_host_matches_socket_address": str(peer_host) == host,
    }


def friend_record(base: str, peer_id: str) -> dict[str, Any]:
    _, payload = request_json(base, "/api/local/friends")
    if not isinstance(payload, list):
        raise RuntimeError("Friend list was not an array")
    found = next((dict(item) for item in payload if item.get("peer_id") == peer_id), None)
    if found is None:
        raise RuntimeError("Expected friendship record was not found")
    return found


def wait_for_service(base: str, provider_peer_id: str, *, timeout: float = 40) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    path = "/api/local/llm/services?" + urlencode({"network_id": NETWORK_ID})
    while time.monotonic() < deadline:
        _, payload = request_json(base, path)
        services = payload.get("services", []) if isinstance(payload, dict) else []
        found = next(
            (
                dict(item)
                for item in services
                if item.get("peer_id") == provider_peer_id
                and dict(item.get("service") or {}).get("package_id") == SERVICE_ID
            ),
            None,
        )
        capacity = dict(found.get("capacity") or {}) if found is not None else {}
        if (
            found is not None
            and found.get("online")
            and int(capacity.get("available") or 0) >= 1
        ):
            return found
        time.sleep(0.2)
    raise RuntimeError(
        "Published friends-only Private AI service did not become online with capacity"
    )


def sensitive_occurrences(paths: list[Path], needles: list[str]) -> dict[str, int]:
    encoded = [value.encode("utf-8") for value in needles if value]
    counts = {"invite_links": 0, "invite_secrets": 0}
    for root in paths:
        candidates = [root] if root.is_file() else list(root.rglob("*"))
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                data = candidate.read_bytes()
            except OSError:
                continue
            for index, needle in enumerate(encoded):
                key = "invite_links" if index % 2 == 0 else "invite_secrets"
                counts[key] += data.count(needle)
    return counts


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    started_at = utc_now()
    lan_address = choose_lan_address(args.host_address)
    ports: set[int] = set()
    while len(ports) < 4:
        ports.add(free_port())
    registry_port, node_a_port, node_b_port, model_port = sorted(ports)
    registry_base = f"http://127.0.0.1:{registry_port}"
    node_a_local = f"http://127.0.0.1:{node_a_port}"
    node_b_local = f"http://127.0.0.1:{node_b_port}"
    node_a_peer = f"http://{lan_address}:{node_a_port}"
    node_b_peer = f"http://{lan_address}:{node_b_port}"

    workspace = Path(tempfile.mkdtemp(prefix="rynmesh-issue30-e2e-"))
    node_a_home = workspace / "node-a"
    node_b_home = workspace / "node-b"
    log_dir = workspace / "logs"
    registry_env = clean_environment(
        {
            "RYNMESH_REGISTRY_HOST": "127.0.0.1",
            "RYNMESH_REGISTRY_PORT": str(registry_port),
            "RYNMESH_REGISTRY_DIR": str(workspace / "registry"),
        }
    )

    def node_env(home: Path, name: str, port: int, peer_endpoint: str) -> dict[str, str]:
        return clean_environment(
            {
                "RYNMESH_HOME": str(home),
                "RYNMESH_NETWORK_DIR": str(home / "network"),
                "RYNMESH_NODE_NAME": name,
                "RYNMESH_NETWORK_ID": NETWORK_ID,
                "RYNMESH_PEER_HOST": "0.0.0.0",
                "RYNMESH_PEER_PORT": str(port),
                "RYNMESH_PEER_ENDPOINT": peer_endpoint,
                "RYNMESH_REGISTRY_URL": registry_base,
                "RYNMESH_AUTO_REGISTER": "1",
                "RYNMESH_LLM_TRANSPORT": "direct",
            }
        )

    registry = ManagedProcess(
        "registry", "rynmesh.registry_http", registry_env, log_dir / "registry.log"
    )
    node_a = ManagedProcess(
        "node-a",
        "rynmesh.peer_http",
        node_env(node_a_home, "Issue30 Provider", node_a_port, node_a_peer),
        log_dir / "node-a.log",
    )
    node_b = ManagedProcess(
        "node-b",
        "rynmesh.peer_http",
        node_env(node_b_home, "Issue30 Friend", node_b_port, node_b_peer),
        log_dir / "node-b.log",
    )
    model = DeterministicModel(model_port)
    sensitive_values: list[str] = []
    processes_stopped = False

    try:
        model.start()
        registry.start()
        wait_ready(registry_base, registry)
        node_a.start()
        node_b.start()
        health_a = wait_ready(node_a_local, node_a)
        health_b = wait_ready(node_b_local, node_b)
        peer_a = str(health_a["peer_id"])
        peer_b = str(health_b["peer_id"])
        if peer_a == peer_b or node_a.pids[-1] == node_b.pids[-1]:
            raise RuntimeError("Node homes/processes did not produce distinct identities")

        probes = {
            "node_a": endpoint_probe(node_a_peer, node_label="node-a"),
            "node_b": endpoint_probe(node_b_peer, node_label="node-b"),
        }
        if not all(
            probe["socket_port_matches"] and probe["socket_address_was_reviewed"]
            for probe in probes.values()
        ):
            raise RuntimeError("Endpoint DNS/socket preflight did not match")

        _, setup = request_json(
            node_a_local,
            "/api/local/llm/setup",
            method="POST",
            body={
                "mode": "openai-compatible",
                "package_id": SERVICE_ID,
                "alias": "Issue 30 deterministic model",
                "base_url": f"http://127.0.0.1:{model_port}",
                "model": MODEL_ID,
            },
            timeout=30,
        )
        if not isinstance(setup, dict) or not setup.get("configured"):
            raise RuntimeError("Provider model setup did not complete")
        _, published = request_json(
            node_a_local,
            "/api/local/llm/services/publish",
            method="POST",
            body={
                "network_id": NETWORK_ID,
                "access_policy": "friends",
                "benchmark": False,
            },
            timeout=30,
        )
        published_record = dict(published.get("record") or {}) if isinstance(published, dict) else {}
        access_policy = str(
            dict(dict(published_record.get("metadata") or {}).get("llm_service") or {}).get(
                "access_policy", ""
            )
        )
        if access_policy != "friends":
            raise RuntimeError("Provider did not publish friends-only access")

        def establish_relationship(label: str) -> dict[str, Any]:
            _, created = request_json(
                node_a_local,
                "/api/local/friends/invites",
                method="POST",
                body={
                    "network_id": NETWORK_ID,
                    "endpoints": [node_a_peer],
                    "permissions": ["private-ai.use"],
                    "ttl_seconds": 900,
                    "allow_private_endpoints": True,
                },
            )
            if not isinstance(created, dict):
                raise RuntimeError("Invite creation response was not an object")
            link = str(created["link"])
            decoded = verify_invite(link, allow_private_endpoints=True)
            sensitive_values.extend([link, str(decoded["one_time_secret"])])
            _, reviewed = request_json(
                node_b_local,
                "/api/local/friends/invites/review",
                method="POST",
                body={"link": link, "allow_private_endpoints": True},
            )
            if not isinstance(reviewed, dict) or "one_time_secret" in reviewed:
                raise RuntimeError("Offline review was invalid or exposed its bearer")
            _, before_join = request_json(node_a_local, "/api/local/friends/invites")
            invite_id = str(decoded["invite_id"])
            persisted = next(
                (dict(item) for item in before_join if item.get("invite_id") == invite_id), None
            )
            if persisted is None or persisted.get("used_at") is not None:
                raise RuntimeError("Offline review contacted or consumed the invitation")
            _, joined = request_json(
                node_b_local,
                "/api/local/friends/join",
                method="POST",
                body={
                    "link": link,
                    "acceptor_endpoints": [node_b_peer],
                    "allow_private_endpoints": True,
                },
                timeout=30,
            )
            if not isinstance(joined, dict) or joined.get("status") != "active":
                raise RuntimeError("Join did not establish an active relationship")
            if friend_record(node_a_local, peer_b).get("state") != "active":
                raise RuntimeError("Provider did not persist an active friendship")
            if friend_record(node_b_local, peer_a).get("state") != "active":
                raise RuntimeError("Friend did not persist an active friendship")
            return {
                "phase": label,
                "invite_ref": sha_ref(invite_id),
                "offline_review_before_contact": True,
                "reviewed_network": reviewed.get("network_id"),
                "reviewed_permission": list(reviewed.get("permissions") or []),
                "reviewed_endpoint_count": len(reviewed.get("endpoints") or []),
                "both_relationships_active": True,
            }

        relationship_online = establish_relationship("online-revocation")
        discovered = wait_for_service(node_b_local, peer_a)
        if discovered.get("access_policy") != "friends":
            raise RuntimeError("Consumer discovery did not preserve friends-only policy")

        def private_ai_order(
            task_id: str,
            prompt: str,
            *,
            response_mode: str = "complete-v1",
        ) -> tuple[int, dict[str, Any]]:
            status, payload = request_json(
                node_b_local,
                "/api/local/llm/orders",
                method="POST",
                body={
                    "network_id": NETWORK_ID,
                    "provider_peer_id": peer_a,
                    "service_id": SERVICE_ID,
                    "prompt": prompt,
                    "max_tokens": 16,
                    "transport": "direct",
                    "task_id": task_id,
                    "response_mode": response_mode,
                },
                expected=(200, 400, 404, 409, 500, 502, 503),
                timeout=30,
            )
            return status, dict(payload) if isinstance(payload, dict) else {}

        success_status, success = private_ai_order(
            "issue30-online-authorized",
            "Confirm this invited friend can stream Private AI",
            response_mode="stream-v1",
        )
        if success_status != 200 or success.get("state") != "succeeded":
            raise RuntimeError(
                "Authorized friend Private AI order did not succeed "
                f"(HTTP {success_status}, state={success.get('state')}, "
                f"detail={str(success.get('detail', ''))[:120]})"
            )
        if success.get("output") != "ok":
            raise RuntimeError("Authorized streaming output was not the deterministic result")
        stream_evidence = dict(success.get("transport_evidence") or {})
        if stream_evidence.get("stream_protocol") != "rynmesh.llm.stream.v1":
            raise RuntimeError("Authorized friend order did not use the stream-v1 peer route")
        model_after_success = model.calls()

        _, online_revoke = request_json(
            node_a_local,
            "/api/local/friends/revoke",
            method="POST",
            body={"peer_id": peer_b, "reason_code": "issue30_online_e2e"},
            timeout=20,
        )
        if not isinstance(online_revoke, dict) or online_revoke.get("delivery") != "delivered":
            raise RuntimeError("Online revocation did not converge")
        if friend_record(node_a_local, peer_b).get("state") != "revoked":
            raise RuntimeError("Provider did not revoke locally")
        if friend_record(node_b_local, peer_a).get("state") != "revoked":
            raise RuntimeError("Friend did not apply online revocation")
        denied_status, _denied = private_ai_order(
            "issue30-online-denied",
            "This stream must not reach inference after revoke",
            response_mode="stream-v1",
        )
        online_denied_before_inference = model.calls() == model_after_success
        if denied_status < 400 or not online_denied_before_inference:
            raise RuntimeError("Post-revoke order was not denied before inference")

        relationship_offline = establish_relationship("offline-revocation")
        # The provider publishes a point-in-time capacity snapshot every 30s. A
        # refresh can legitimately occur while the first task is running and
        # briefly advertise available=0 after that task has finished. Wait for
        # the next real Registry publication rather than treating normal busy
        # back-pressure as a Friend Mesh failure.
        wait_for_service(node_b_local, peer_a)
        second_success_status, second_success = private_ai_order(
            "issue30-offline-authorized", "Confirm access before the offline revoke"
        )
        if second_success_status != 200 or second_success.get("state") != "succeeded":
            raise RuntimeError(
                "Re-established friend access did not succeed "
                f"(HTTP {second_success_status}, state={second_success.get('state')}, "
                f"detail={str(second_success.get('detail', ''))[:120]})"
            )
        model_before_offline_denial = model.calls()

        first_node_b_pid = node_b.pids[-1]
        node_b.stop()
        _, offline_revoke = request_json(
            node_a_local,
            "/api/local/friends/revoke",
            method="POST",
            body={"peer_id": peer_b, "reason_code": "issue30_offline_e2e"},
            timeout=20,
        )
        if not isinstance(offline_revoke, dict) or offline_revoke.get("delivery") != "remote_unreachable":
            raise RuntimeError("Offline revocation did not record pending delivery")
        if friend_record(node_a_local, peer_b).get("state") != "revoked":
            raise RuntimeError("Offline revocation did not deny locally")

        node_b.start()
        restarted_health = wait_ready(node_b_local, node_b)
        if str(restarted_health.get("peer_id")) != peer_b or node_b.pids[-1] == first_node_b_pid:
            raise RuntimeError("Restart did not preserve identity in a new process")
        if friend_record(node_b_local, peer_a).get("state") != "active":
            raise RuntimeError("Offline friend should remain active until signed retry arrives")
        _, retried = request_json(
            node_a_local,
            "/api/local/friends/revocations/retry",
            method="POST",
            body={"peer_id": peer_b},
            timeout=20,
        )
        if not isinstance(retried, dict) or retried.get("delivery") != "delivered":
            raise RuntimeError("Revocation retry did not deliver")
        if friend_record(node_b_local, peer_a).get("state") != "revoked":
            raise RuntimeError("Restarted friend did not converge to revoked")
        offline_denied_status, _offline_denied = private_ai_order(
            "issue30-offline-denied", "This must not infer after retry convergence"
        )
        offline_denied_before_inference = model.calls() == model_before_offline_denial
        if offline_denied_status < 400 or not offline_denied_before_inference:
            raise RuntimeError("Converged offline revocation did not deny before inference")

        _, privacy_export = request_json(node_b_local, "/api/local/privacy/export")
        export_text = json.dumps(privacy_export, separators=(",", ":"), sort_keys=True)
        if any(value in export_text for value in sensitive_values):
            raise RuntimeError("Privacy export exposed an invitation bearer")

        node_a.stop()
        node_b.stop()
        registry.stop()
        model.stop()
        processes_stopped = True

        occurrence_counts = sensitive_occurrences(
            [node_a_home, node_b_home, log_dir], sensitive_values
        )
        if occurrence_counts != {"invite_links": 0, "invite_secrets": 0}:
            raise RuntimeError("Sensitive invitation material was persisted or logged")
        secret_states: dict[str, list[str]] = {}
        for label, home in (("node_a", node_a_home), ("node_b", node_b_home)):
            secret_path = home / "friends.secrets.json"
            content = json.loads(secret_path.read_text(encoding="utf-8"))
            secret_states[label] = sorted(str(key) for key in dict(content.get("relationships") or {}))
        if any(secret_states.values()):
            raise RuntimeError("Relationship credentials remained after convergence")

        git_base = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        harness_status = subprocess.run(
            [
                "git",
                "status",
                "--short",
                "--",
                str(Path(__file__).relative_to(ROOT)),
                "rynmesh/transport.py",
                "rynmesh/llm_package/routes.py",
                "tests/test_transport.py",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        evidence = {
            "schema": "rynmesh.issue30.local-two-node-e2e.v2",
            "decision": "development_acceptance_passed",
            "release_acceptance": "not_claimed",
            "started_at": started_at,
            "completed_at": utc_now(),
            "duration_seconds": round(time.monotonic() - started, 3),
            "source": {
                "git_base_commit": git_base,
                "harness_sources_clean": not bool(harness_status),
                "harness_sha256": "sha256:"
                + hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "command": (
                    "python scripts/issue30_two_node_e2e.py "
                    "--output docs/evidence/issue30-integration-two-node-e2e.json"
                ),
                "docker_used": False,
            },
            "topology": {
                "node_process_count": 2,
                "node_processes_distinct": True,
                "node_b_restart_new_process": len(node_b.pids) == 2
                and node_b.pids[0] != node_b.pids[1],
                "independent_homes": True,
                "independent_ports": True,
                "registry": "local-http-process",
                "model": "deterministic-local-http-stub",
                "advertised_address": "<private-lan-ip>",
            },
            "identities": {"node_a": sha_ref(peer_a), "node_b": sha_ref(peer_b)},
            "endpoint_dns_socket": probes,
            "flows": {
                "online": {
                    "relationship": relationship_online,
                    "private_ai_stream": {
                        "status": "succeeded",
                        "output_sha256": hashlib.sha256(b"ok").hexdigest(),
                        "transport": success.get("transport"),
                        "stream_protocol": stream_evidence.get("stream_protocol"),
                    },
                    "revoke_delivery": online_revoke.get("delivery"),
                    "next_order_http_status": denied_status,
                    "next_order_denied_before_inference": online_denied_before_inference,
                },
                "offline_restart": {
                    "relationship": relationship_offline,
                    "revoke_initial_delivery": offline_revoke.get("delivery"),
                    "identity_preserved": True,
                    "new_process": True,
                    "retry_delivery": retried.get("delivery"),
                    "remote_state": "revoked",
                    "next_order_http_status": offline_denied_status,
                    "next_order_denied_before_inference": offline_denied_before_inference,
                },
            },
            "secret_scan": {
                "scanned": ["node-a-home", "node-b-home", "sanitized-process-logs"],
                "invite_link_occurrences": occurrence_counts["invite_links"],
                "invite_secret_occurrences": occurrence_counts["invite_secrets"],
                "relationship_keys_after_convergence": secret_states,
                "privacy_export_sensitive_occurrences": 0,
                "secret_values_recorded": False,
            },
            "cleanup": {"all_child_processes_stopped": processes_stopped},
            "limitations": [
                "Both node processes ran on one host over its private LAN interface.",
                "This proves source-build development acceptance, not installed desktop deep-link dispatch.",
                "The integrated branch exercises stream-v1 online and complete-v1 after reconnect.",
            ],
        }
        return evidence
    finally:
        node_a.stop()
        node_b.stop()
        registry.stop()
        if model.thread.is_alive():
            model.stop()
        if not args.keep_work_dir:
            shutil.rmtree(workspace, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs" / "evidence" / "issue30-local-two-node-e2e.json",
        help="Path for sanitized JSON evidence.",
    )
    parser.add_argument(
        "--host-address",
        default="",
        help="Private LAN IPv4 address bound on this host (auto-detected by default).",
    )
    parser.add_argument(
        "--keep-work-dir",
        action="store_true",
        help="Keep temporary homes and logs for local debugging; never attach them as evidence.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        evidence = run(args)
    except Exception as exc:  # noqa: BLE001 - CLI must clean up before returning a bounded failure
        print(f"Issue #30 local two-node E2E failed: {exc}", file=sys.stderr)
        return 1
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Issue #30 local two-node E2E passed; sanitized evidence: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
