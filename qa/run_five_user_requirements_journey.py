"""Run the approved 12-step user journey against real local node processes.

This acceptance runner uses only public HTTP surfaces, clean RYNMESH_HOME
directories, the reviewed managed-native model installer, and a second real
node process. It never edits a database or private configuration file.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class JourneyError(RuntimeError):
    pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _pdf_bytes(text: str) -> bytes:
    from reportlab.pdfgen import canvas

    output = io.BytesIO()
    document = canvas.Canvas(output)
    document.drawString(72, 720, text)
    document.save()
    return output.getvalue()


@dataclass
class NodeProcess:
    name: str
    home: Path
    port: int
    runtime_port: int
    discovery: bool
    process: subprocess.Popen[bytes] | None = None
    log_handle: Any = field(default=None, repr=False)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def token(self) -> str:
        return f"journey-{self.name.lower()}-token"

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            raise JourneyError(f"{self.name} is already running")
        self.home.mkdir(parents=True, exist_ok=True)
        log_path = self.home / "acceptance-node.log"
        self.log_handle = log_path.open("ab")
        env = os.environ.copy()
        env.update(
            {
                "RYNMESH_HOME": str(self.home),
                "RYNMESH_NETWORK_DIR": str(self.home / "network"),
                "RYNMESH_NODE_NAME": self.name,
                "RYNMESH_MACHINE_NAME": self.name,
                "RYNMESH_MACHINE_IP": "127.0.0.1",
                "RYNMESH_PEER_HOST": "127.0.0.1",
                "RYNMESH_PEER_PORT": str(self.port),
                "RYNMESH_PEER_ENDPOINT": self.base_url,
                "RYNMESH_PEER_PUBLIC_HOST": "127.0.0.1",
                "RYNMESH_LOCAL_TOKEN": self.token,
                "RYNMESH_AUTO_REGISTER": "0",
                "RYNMESH_DESKTOP_MODE": "1",
                "RYNMESH_DEFAULT_DISCOVERY": "1" if self.discovery else "0",
                "RYNMESH_FRIEND_ALLOW_LOOPBACK": "1",
            }
        )
        self.process = subprocess.Popen(
            [sys.executable, "-m", "rynmesh.peer_http"],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
        )
        self.wait_ready()

    def wait_ready(self, timeout_s: float = 45) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.process and self.process.poll() is not None:
                raise JourneyError(f"{self.name} exited during startup")
            try:
                if self.request("GET", "/health", local=False).get("status") == "ok":
                    return
            except JourneyError:
                pass
            time.sleep(0.2)
        raise JourneyError(f"{self.name} did not become healthy")

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        local: bool = True,
        timeout_s: float = 30,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if local:
            headers["X-Ryn-Local-Token"] = self.token
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            payload = exc.read(64 * 1024).decode("utf-8", errors="replace")
            raise JourneyError(f"{method} {path} -> {exc.code}: {payload}") from exc
        except OSError as exc:
            raise JourneyError(f"{method} {path} failed: {exc}") from exc
        if len(raw) > 16 * 1024 * 1024:
            raise JourneyError(f"{method} {path} response exceeded acceptance limit")
        try:
            value = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JourneyError(f"{method} {path} returned invalid JSON") from exc
        return value

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None


def _wait_for(
    node: NodeProcess,
    path: str,
    predicate,
    *,
    timeout_s: float,
    interval_s: float = 0.5,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = node.request("GET", path, timeout_s=min(30, timeout_s))
        if predicate(latest):
            return latest
        time.sleep(interval_s)
    raise JourneyError(f"timed out waiting for {path}: {latest}")


def _assistant_run(
    node: NodeProcess,
    *,
    conversation_id: str,
    idempotency_key: str,
    prompt: str,
    contexts: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    targets = node.request("GET", "/api/local/assistant/targets").get("targets", [])
    target = next(
        (
            value
            for value in targets
            if value.get("privacy_boundary") == "device" and value.get("online")
        ),
        None,
    )
    if not isinstance(target, dict):
        raise JourneyError("installed local model was not offered as an Assistant target")
    queued = node.request(
        "POST",
        "/api/local/assistant/runs",
        {
            "version": "ryn.assistant-run.v1",
            "conversation_id": conversation_id,
            "idempotency_key": idempotency_key,
            "target_key": target["key"],
            "privacy_boundary": "device",
            "mode": "local_context" if contexts else "local_chat",
            "messages": [{"role": "user", "content": prompt, "status": "complete"}],
            "contexts": contexts or [],
            "max_output_tokens": 96,
        },
    )
    run_id = str(queued.get("run_id", ""))
    return _wait_for(
        node,
        f"/api/local/assistant/runs/{run_id}",
        lambda value: value.get("state") in {"completed", "failed", "cancelled"},
        timeout_s=180,
        interval_s=0.2,
    )


def _assert_secret_free(value: dict[str, Any], markers: list[str]) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    forbidden_names = (
        "relationship_secret",
        "invite_secret",
        "secret_hash",
        "api_key",
        "model_path",
        "runtime_command",
    )
    for marker in [*markers, *forbidden_names]:
        if marker and marker in serialized:
            raise JourneyError(f"privacy export leaked forbidden marker: {marker}")
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def run_journey(root: Path, *, run_number: int) -> dict[str, Any]:
    if root.exists() and any(root.iterdir()):
        raise JourneyError(f"journey root must be empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    marker = f"PRIVATE_E2E_{run_number}_{uuid.uuid4().hex}"
    model_port = _free_port()
    node_a = NodeProcess("Journey A", root / "node-a", _free_port(), model_port, True)
    node_b = NodeProcess("Journey B", root / "node-b", _free_port(), _free_port(), False)
    report: dict[str, Any] = {
        "version": "ryn.five-user-requirements-journey.v1",
        "run": run_number,
        "started_at_unix": time.time(),
        "steps": [],
    }
    try:
        node_a.start()
        report["steps"].append({"step": 1, "status": "passed", "health": "ok"})

        first = _wait_for(
            node_a,
            "/api/local/first-success",
            lambda value: value.get("phase") in {"ready", "completed"},
            timeout_s=240,
            interval_s=1,
        )
        digest = node_a.request("GET", "/api/local/digest")
        items = digest.get("items", [])
        if not items:
            raise JourneyError("default discovery produced no recommendation")
        item = dict(items[0])
        report["steps"].append(
            {
                "step": 2,
                "status": "passed",
                "item_count": first.get("item_count"),
                "healthy_sources": first.get("healthy_sources"),
            }
        )

        node_a.request("POST", "/api/local/consumption", {"item": item, "action": "opened"})
        node_a.request("POST", "/api/local/consumption", {"item": item, "action": "bookmark"})
        node_a.request(
            "POST",
            "/api/local/consumption",
            {"item": item, "action": "progress", "progress": 0.42},
        )
        completed = node_a.request("GET", "/api/local/first-success")
        if not completed.get("completed"):
            raise JourneyError("first-success milestone did not complete")
        report["steps"].append({"step": 3, "status": "passed", "progress": 0.42})

        setup = node_a.request(
            "POST",
            "/api/local/llm/setup/async",
            {
                "profile": "light",
                "package_id": "local-light",
                "port": model_port,
                "accept_license": True,
            },
        )
        setup_job = _wait_for(
            node_a,
            "/api/local/llm/setup/status",
            lambda value: value.get("state") in {"succeeded", "failed", "cancelled"},
            timeout_s=1800,
            interval_s=1,
        )
        if setup_job.get("state") != "succeeded":
            raise JourneyError(f"managed model setup failed: {setup_job}")
        first_answer = node_a.request(
            "POST", "/api/local/llm/readiness/test", {}, timeout_s=180
        )
        if not first_answer.get("ok") or not str(first_answer.get("answer", "")).strip():
            raise JourneyError("managed model returned no first answer")
        report["steps"].append(
            {
                "step": 4,
                "status": "passed",
                "job_id_suffix": str(setup.get("job_id", ""))[-8:],
                "model_alias": first_answer.get("model_alias"),
                "answer_sha256": hashlib.sha256(
                    str(first_answer["answer"]).encode("utf-8")
                ).hexdigest(),
            }
        )

        conversation_id = f"journey-{run_number}"
        item_context = node_a.request(
            "POST",
            "/api/local/assistant/context/prepare",
            {
                "kind": "text",
                "title": str(item.get("title", "Recommendation")),
                "text": str(item.get("summary") or item.get("title") or "Recommendation"),
                "source_url": str(item.get("link", "")),
                "source_ref": str(item.get("item_id", "")),
                "conversation_id": conversation_id,
            },
        )
        answer = _assistant_run(
            node_a,
            conversation_id=conversation_id,
            idempotency_key="recommendation-question",
            prompt=f"Summarize the selected recommendation. {marker}",
            contexts=[{"local_ref": str(item_context["local_ref"])}],
        )
        if answer.get("state") != "completed" or not answer.get("citations"):
            raise JourneyError(f"grounded recommendation answer failed: {answer}")
        citation = dict(answer["citations"][0])
        if not (citation.get("source_url") or citation.get("source_ref")):
            raise JourneyError("recommendation answer citation is not openable")
        report["steps"].append(
            {
                "step": 5,
                "status": "passed",
                "run_id": answer["run_id"],
                "citation_count": len(answer["citations"]),
                "output_sha256": answer["output_sha256"],
            }
        )

        node_a.request("POST", "/api/local/llm/service/actions/stop", {})
        node_a.stop()
        node_a.start()
        readiness = _wait_for(
            node_a,
            "/api/local/llm/readiness",
            lambda value: bool(value.get("ready")),
            timeout_s=180,
            interval_s=0.5,
        )
        prior = node_a.request("GET", f"/api/local/assistant/runs/{answer['run_id']}")
        consumption = node_a.request("GET", "/api/local/consumption")
        selected_progress = next(
            (row.get("progress") for row in consumption if row.get("item_id") == item.get("item_id")),
            None,
        ) if isinstance(consumption, list) else None
        continued = _assistant_run(
            node_a,
            conversation_id=conversation_id,
            idempotency_key="continued-after-restart",
            prompt="Continue with one practical takeaway.",
        )
        if prior.get("state") != "completed" or continued.get("state") != "completed":
            raise JourneyError("assistant conversation did not survive restart")
        if float(selected_progress or 0) < 0.42:
            raise JourneyError("reading progress did not survive restart")
        report["steps"].append(
            {
                "step": 6,
                "status": "passed",
                "runtime": readiness.get("runtime"),
                "progress": selected_progress,
            }
        )

        pdf = _pdf_bytes(f"Ryn acceptance document {marker}")
        imported_pdf = node_a.request(
            "POST",
            "/api/local/library/imports",
            {
                "filename": "acceptance.pdf",
                "mime": "application/pdf",
                "data_base64": base64.b64encode(pdf).decode("ascii"),
            },
        )
        pdf_context = node_a.request(
            "POST",
            "/api/local/assistant/context/prepare",
            {
                "source_kind": "library-item",
                "library_id": imported_pdf["library_id"],
                "conversation_id": conversation_id,
            },
        )
        pdf_answer = _assistant_run(
            node_a,
            conversation_id=conversation_id,
            idempotency_key="pdf-question",
            prompt="What is this imported document for?",
            contexts=[{"local_ref": str(pdf_context["local_ref"])}],
        )
        if pdf_answer.get("state") != "completed" or not pdf_answer.get("citations"):
            raise JourneyError("PDF question did not return a grounded answer")
        report["steps"].append(
            {
                "step": 7,
                "status": "passed",
                "library_id_suffix": str(imported_pdf["library_id"])[-8:],
                "citation_count": len(pdf_answer["citations"]),
            }
        )

        node_b.start()
        invite = node_a.request("POST", "/api/local/friends/invites", {"ttl_minutes": 15})
        preview = node_b.request(
            "POST",
            "/api/local/friends/invites/inspect",
            {"invite_uri": invite["invite_uri"]},
        )
        relationship_b = node_b.request(
            "POST",
            "/api/local/friends/join",
            {"invite_uri": invite["invite_uri"]},
        )
        report["steps"].append(
            {
                "step": 8,
                "status": "passed",
                "reviewed_node": preview.get("node_name"),
                "relationship": relationship_b.get("status"),
            }
        )

        friends_a = node_a.request("GET", "/api/local/friends").get("friends", [])
        relationship_a = next(row for row in friends_a if row.get("status") == "active")
        peer_b = str(relationship_a["peer_id"])
        message = node_a.request(
            "POST",
            "/api/local/friends/messages/send",
            {"peer_id": peer_b, "text": "Acceptance hello"},
        )
        attachment = node_a.request(
            "POST",
            "/api/local/friends/messages/send",
            {
                "peer_id": peer_b,
                "text": "Small attachment",
                "attachment": {
                    "filename": "acceptance.txt",
                    "mime": "text/plain",
                    "bytes_b64": base64.b64encode(b"bounded attachment").decode("ascii"),
                },
            },
        )
        card = node_a.request(
            "POST",
            "/api/local/friends/content-cards",
            {"peer_id": peer_b, "card": {"library_id": imported_pdf["library_id"]}},
        )
        if not all(value.get("delivered") for value in (message, attachment, card)):
            raise JourneyError("friend message, attachment, or card was not delivered")
        report["steps"].append({"step": 9, "status": "passed", "delivered": 3})

        cards_b = node_b.request("GET", "/api/local/friends/content-cards").get("cards", [])
        received = next(row for row in cards_b if row.get("card_id") == card.get("card_id"))
        if received.get("fetch_state") != "available":
            raise JourneyError("received content card was not reviewable before fetch")
        files_before = node_b.request("GET", "/api/local/library?view=files").get("items", [])
        fetched = node_b.request(
            "POST", f"/api/local/friends/content-cards/{card['card_id']}/fetch", {}
        )
        files_after = node_b.request("GET", "/api/local/library?view=files").get("items", [])
        if files_before or len(files_after) != 1 or not fetched.get("sha256_verified"):
            raise JourneyError("content card was not fetched, verified, and imported exactly once")
        duplicate = node_b.request(
            "POST", f"/api/local/friends/content-cards/{card['card_id']}/fetch", {}
        )
        if not duplicate.get("already_fetched") or len(
            node_b.request("GET", "/api/local/library?view=files").get("items", [])
        ) != 1:
            raise JourneyError("content card fetch was not idempotent")
        report["steps"].append(
            {
                "step": 10,
                "status": "passed",
                "sha256_verified": True,
                "idempotent": True,
            }
        )

        revoked = node_a.request(
            "DELETE", f"/api/local/friends/{relationship_a['relationship_id']}"
        )
        if revoked.get("status") != "revoked":
            raise JourneyError("local friend revoke did not complete")
        rejected = False
        try:
            node_b.request(
                "POST",
                "/api/local/friends/messages/send",
                {"peer_id": str(relationship_b["peer_id"]), "text": "must be denied"},
            )
        except JourneyError as exc:
            rejected = "active_friend_required" in str(exc)
        if not rejected:
            raise JourneyError("post-revoke friend request was not rejected")
        report["steps"].append({"step": 11, "status": "passed", "next_request": "denied"})

        export_a = node_a.request("GET", "/api/local/privacy/export")
        export_b = node_b.request("GET", "/api/local/privacy/export")
        export_hashes = [
            _assert_secret_free(export_a, [marker]),
            _assert_secret_free(export_b, [marker]),
        ]
        report["steps"].append(
            {"step": 12, "status": "passed", "secret_free_export_sha256": export_hashes}
        )
        report["result"] = "passed"
        report["completed_at_unix"] = time.time()
        return report
    finally:
        for node in (node_b, node_a):
            try:
                if node.process and node.process.poll() is None and node is node_a:
                    node.request("POST", "/api/local/llm/service/actions/stop", {})
            except JourneyError:
                pass
            node.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.runs <= 3:
        raise SystemExit("--runs must be between 1 and 3")
    work_root = args.work_root.resolve()
    if work_root.exists() and any(work_root.iterdir()):
        raise SystemExit("--work-root must be empty")
    work_root.mkdir(parents=True, exist_ok=True)
    reports = []
    for number in range(1, args.runs + 1):
        reports.append(run_journey(work_root / f"run-{number:02d}", run_number=number))
    final = {
        "version": "ryn.five-user-requirements-journey-suite.v1",
        "runs": reports,
        "result": "passed" if all(row.get("result") == "passed" for row in reports) else "failed",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(final, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(final, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
