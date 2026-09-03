"""Deterministic local Consumer fixture for Issue #25 browser acceptance.

This intentionally exposes only the local control endpoints needed by the
reader-to-grounded-chat flow. It never contacts a Registry or Provider. The
fixture Provider is represented behind the Consumer API, and request evidence
stores only method/path metadata plus body hashes and marker booleans.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ARTICLE_MARKER = "ISSUE25_BROWSER_ARTICLE_MARKER_6F27"
ARTICLE_URL = "https://fixture.invalid/issue-25-grounded-article"


def _profile() -> dict:
    return {
        "version": 1,
        "direction": "",
        "topics": [],
        "platforms": [],
        "feedback": {},
        "updated_at": "2026-09-03T00:00:00Z",
        "topic_choices": [{"id": "local-ai", "label": "Local AI"}],
        "platform_choices": [{"id": "rss", "label": "RSS"}],
        "learned_signals": 0,
        "feedback_count": 0,
    }


def _item() -> dict:
    return {
        "item_id": "issue-25-browser-item",
        "source_id": "issue-25-browser-source",
        "source_title": "Issue 25 Local Reader Fixture",
        "source_kind": "rss",
        "title": "Multilingual grounding acceptance article",
        "link": ARTICLE_URL,
        "summary": "A deterministic local Reader fixture for grounded Private AI acceptance.",
        "ai_summary": "",
        "ai_summary_grounding_version": 0,
        "author": "Rynmesh acceptance harness",
        "thumbnail": "",
        "media_url": "",
        "content_kind": "document",
        "content_type": "text/html",
        "tags": ["grounding", "local-first"],
        "published_unix": 1_788_393_600,
        "score": 0.99,
        "reasons": ["Deterministic Issue #25 acceptance fixture"],
        "review_basis": "full",
        "safety_outcome": "unscanned",
        "provenance_status": "unsigned",
        "evidence_packet": {
            "version": 1,
            "content_id": "issue-25-browser-item",
            "review_basis": "full",
            "reviewed_at_unix": 1_788_393_600,
            "source": {
                "name": "Issue 25 Local Reader Fixture",
                "platform": "rss",
                "url": ARTICLE_URL,
            },
            "signals": [{"kind": "topic", "label": "Grounded Private AI"}],
            "observations": [
                {"field": "title", "label": "Title", "value": "grounding acceptance"}
            ],
            "citations": [{"kind": "source", "label": "Original", "url": ARTICLE_URL}],
            "limitations": ["Synthetic local acceptance data"],
        },
    }


def _discovery() -> dict:
    return {
        "phase": "ready",
        "message": "Deterministic Issue #25 fixture is ready.",
        "last_started_unix": 1_788_393_590,
        "last_completed_unix": 1_788_393_600,
        "next_refresh_unix": 1_788_397_200,
        "new_items": 1,
        "unread_count": 1,
        "item_count": 1,
        "source_count": 1,
        "formats": ["article"],
        "healthy_sources": 1,
        "failed_sources": 0,
        "cached_sources": 1,
        "degraded": False,
        "offline_ready": True,
        "source_health": [],
    }


def _settings(port: int) -> dict:
    return {
        "node_name": "Issue 25 Browser Consumer",
        "node_storage": "temporary acceptance state",
        "peer_http_host": "127.0.0.1",
        "peer_http_port": port,
        "public_endpoint": f"http://127.0.0.1:{port}",
        "registry_url": "fixture://disabled",
        "trusted_roots": [],
        "safety_policy": "standard",
        "ai_provider": "local",
        "ai_model": "fixture-private-model",
        "cloud_access": False,
        "rank_default": "weight",
        "publish_visibility": "local",
        "fetch_budget_mb": 16,
        "fetch_used_mb": 1,
        "fetch_timeout_s": 5,
        "onboarding_version": 1,
        "notifications_enabled": False,
        "notification_frequency": "immediate",
        "notification_quiet_start": 22,
        "notification_quiet_end": 8,
        "desktop_managed": False,
        "network_id": "issue-25-browser-network",
    }


class FixtureState:
    def __init__(self, state_dir: Path, port: int) -> None:
        self.state_dir = state_dir
        self.port = port
        self.log_path = state_dir / "requests.jsonl"
        self.lock = threading.Lock()
        self.consumption: list[dict] = []

    def record(self, method: str, target: str, body: bytes) -> None:
        parsed = urlsplit(target)
        entry = {
            "time": datetime.now(UTC).isoformat(),
            "method": method,
            "path": parsed.path,
            "query_keys": sorted(parse_qs(parsed.query).keys()),
            "body_bytes": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest() if body else "",
            "article_marker_in_body": ARTICLE_MARKER.encode() in body,
            "article_marker_in_target": ARTICLE_MARKER in target,
        }
        with self.lock:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")


def make_handler(state: FixtureState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "RynmeshIssue25Fixture/1.0"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _cors(self) -> None:
            origin = self.headers.get("Origin", "")
            allowed = origin if origin.startswith(("http://127.0.0.1:", "http://localhost:")) else "*"
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")

        def _json(self, payload: object, status: int = 200) -> None:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0") or 0)
            return self.rfile.read(length) if length else b""

        def do_OPTIONS(self) -> None:  # noqa: N802
            state.record("OPTIONS", self.path, b"")
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            state.record("GET", self.path, b"")
            path = urlsplit(self.path).path
            item = _item()
            if path == "/api/local/auth/status":
                return self._json({"authorized": True, "via": "local", "remote": False})
            if path == "/api/local/node/status":
                return self._json({
                    "node_name": "Issue 25 Browser Consumer",
                    "peer_id": "peer:issue-25-browser-consumer",
                    "daemon_running": True,
                    "desktop_managed": False,
                    "registry": "connected",
                    "peer_count": 1,
                    "local_items": 1,
                    "fetched_items": 1,
                    "pending_recs": 1,
                    "version": "issue-25-browser-fixture",
                    "uptime_seconds": 60,
                })
            if path == "/api/local/registry/status":
                return self._json({"status": "connected", "url": "fixture://disabled"})
            if path == "/api/local/peers":
                return self._json([])
            if path == "/api/local/settings":
                return self._json(_settings(state.port))
            if path == "/api/local/discovery/status":
                return self._json(_discovery())
            if path == "/api/local/digest":
                return self._json({
                    "generated_at_unix": 1_788_393_600,
                    "brief": "Deterministic Issue #25 browser acceptance.",
                    "ai": None,
                    "items": [item],
                    "sources": [],
                })
            if path == "/api/local/sources":
                return self._json([])
            if path == "/api/local/watchers":
                return self._json([])
            if path == "/api/local/ai/status":
                return self._json({"provider": None, "model": None})
            if path == "/api/local/consumption":
                return self._json(state.consumption)
            if path == "/api/local/recommendations/profile":
                return self._json(_profile())
            if path == "/api/local/reader":
                long_text = ("中文😀e\u0301 local evidence remains quoted. " * 420).strip()
                return self._json({
                    "url": ARTICLE_URL,
                    "title": item["title"],
                    "byline": "Local fixture author",
                    "lead_image": "",
                    "blocks": [
                        {"tag": "p", "text": f"{ARTICLE_MARKER}: locally cached evidence."},
                        {"tag": "p", "text": long_text},
                    ],
                    "word_count": 2_100,
                    "cached": True,
                })
            if path == "/api/local/llm/services":
                return self._json({"services": [{
                    "peer_id": "peer:issue-25-fixture-provider",
                    "node_name": "Issue 25 Fixture Provider",
                    "online": True,
                    "capacity": {"available": 1, "max_concurrent": 1, "running": 0},
                    "benchmark": {"latency_ms": 8, "tokens_per_second": 40},
                    "service": {
                        "package_id": "issue-25-fixture-model",
                        "model_alias": "fixture-private-model",
                        "capabilities": ["text-generation"],
                        "context_window": 2048,
                        "max_output_tokens": 128,
                        "pricing": {
                            "currency": "DEV_TASK_BALANCE",
                            "input_per_1k": 0.001,
                            "output_per_1k": 0.002,
                            "minimum": 0.001,
                            "maximum_per_task": 1,
                        },
                        "privacy": {
                            "policy_text": "Synthetic local fixture; Provider sees plaintext.",
                            "compute_node_sees_plaintext": True,
                        },
                        "risk_labels": ["fixture"],
                    },
                }]})
            return self._json({"detail": "fixture endpoint not found"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            body = self._read_body()
            state.record("POST", self.path, body)
            path = urlsplit(self.path).path
            if path == "/api/local/discovery/seen":
                return self._json(_discovery())
            if path == "/api/local/digest/feedback":
                return self._json({"ok": True})
            if path == "/api/local/consumption":
                payload = json.loads(body or b"{}")
                item = payload.get("item", _item())
                record = {
                    "item_id": item["item_id"],
                    "item": item,
                    "first_opened_unix": 1_788_393_600,
                    "last_opened_unix": 1_788_393_600,
                    "last_activity_unix": 1_788_393_600,
                    "open_count": 1,
                    "bookmarked": False,
                    "progress": payload.get("progress") or 0,
                    "completed": False,
                }
                state.consumption = [record]
                return self._json(record)
            if path == "/api/local/llm/orders/async":
                return self._json({
                    "task_id": "task_issue25_browser_acceptance",
                    "state": "succeeded",
                    "output": "The local Consumer received the grounded request and the fixture Provider returned this response.",
                    "model_alias": "fixture-private-model",
                    "input_tokens": 512,
                    "output_tokens": 16,
                    "duration_ms": 20,
                    "amount": 0.001,
                    "transport": "peer_http_direct",
                })
            return self._json({"detail": "fixture endpoint not found"}, 404)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18795)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    state = FixtureState(args.state_dir, args.port)
    state.log_path.write_text("", encoding="utf-8")
    (args.state_dir / "fixture.json").write_text(
        json.dumps({
            "consumer": f"http://{args.host}:{args.port}/api/local",
            "article_marker": ARTICLE_MARKER,
            "article_url": ARTICLE_URL,
            "pid": os.getpid(),
        }, indent=2),
        encoding="utf-8",
    )
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    print(f"Issue #25 browser fixture listening on http://{args.host}:{args.port}/api/local", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
