"""Loopback-only OpenAI-compatible source for real HTTP draft acceptance.

The random credential stays in memory. Audit records contain neither headers nor
request text. This controlled upstream is not a second independent AI source.
"""
from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FixtureSource:
    def __init__(self, *, saved=lambda: False, tool_models=("alpha", "beta"),
                 models=("alpha", "beta"), failure_status=lambda: None):
        self.secret = "SYNTHETIC-" + secrets.token_hex(24)
        self.saved, self.audit = saved, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, code, data):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def authorized(self):
                if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + outer.secret):
                    self.reply(401, {"error": {"message": "invalid fixture credential"}})
                    return False
                return True

            def do_GET(self):
                if self.authorized():
                    self.reply(200, {"data": [{"id": name} for name in models]})

            def do_POST(self):
                if not self.authorized():
                    return
                size = int(self.headers.get("Content-Length", "0"))
                if self.path != "/v1/chat/completions" or not 0 < size < 65536:
                    self.reply(400, {"error": {"message": "invalid fixture request"}})
                    return
                body = json.loads(self.rfile.read(size))
                if body.get("model") not in models:
                    self.reply(404, {"error": {"message": "fixture model not found"}})
                    return
                calls = body.get("tools", [])
                message = {"role": "assistant", "content": "OK"}
                if calls and body.get("tool_choice") == "required" and body["model"] in tool_models:
                    message = {"role": "assistant", "content": None, "tool_calls": [{"id": "fixture-probe", "type": "function",
                        "function": {"name": "ryn_capability_probe", "arguments": '{"value":"ryn-tool-test"}'}}]}
                outer.audit.append({"model": body["model"], "saved": outer.saved(), "time": time.time(),
                    "kind": "tool_start" if message.get("tool_calls") else "tool_continue" if any(m.get("role") == "tool" for m in body.get("messages", [])) else "chat",
                    "max_tokens": body.get("max_tokens"), "authenticated": True})
                status = failure_status() or 200
                outer.audit[-1]["status"] = status
                if status != 200:
                    self.reply(status, {"error": {"message": "controlled source failure"}})
                    return
                self.reply(200, {"choices": [{"message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                                 "usage": {"prompt_tokens": 8, "completion_tokens": 4}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:" + str(self.server.server_port) + "/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
