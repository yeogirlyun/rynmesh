"""Owner-only launch handshake for installer-managed loopback MCP services."""
from __future__ import annotations

import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from ..store import AgentError


class LocalServiceCredentials:
    def __init__(self, store, manager, clock=time.monotonic):
        self.store, self.manager, self.clock = store, manager, clock
        self._pending = {}
        self._lock = threading.RLock()

    def prepare(self, *, service_id, plugin_ids, server_ids, port):
        if (not isinstance(service_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", service_id)
                or type(port) is not int or not 1 <= port <= 65535
                or not isinstance(plugin_ids, list) or not isinstance(server_ids, list)
                or any(not isinstance(s, str) for s in [*plugin_ids, *server_ids])
                or not 1 <= len(server_ids) <= 32 or len(set(server_ids)) != len(server_ids)):
            raise AgentError("agent_local_service_invalid")
        bindings = []
        for sid in server_ids:
            server = self.store.get_mcp_server_internal(sid)
            url = urlsplit(server.get("url") or "")
            if (server.get("plugin_id") not in plugin_ids or server.get("transport") != "streamable_http"
                    or url.scheme != "http" or url.hostname != "127.0.0.1" or url.port != port
                    or url.username or url.password or url.query or url.fragment):
                raise AgentError("agent_local_service_binding_invalid")
            bindings.append((sid, server["instance_id"], server["config_generation"], server["effective_enabled"]))
        generation, credential = secrets.token_hex(16), secrets.token_urlsafe(48)
        with self._lock:
            now = self.clock()
            self._pending = {k: v for k, v in self._pending.items() if v["expires"] > now}
            if service_id not in self._pending and len(self._pending) >= 32:
                raise AgentError("agent_local_service_limit")
            self._pending[service_id] = {"generation": generation, "credential": credential,
                                         "bindings": bindings, "expires": now + 60}
        return {"generation": generation, "credential": credential}

    def activate(self, *, service_id, generation):
        with self._lock:
            pending = self._pending.get(service_id)
            if not pending or pending["expires"] <= self.clock() or pending["generation"] != generation:
                raise AgentError("agent_local_service_stale")
            self.store.activate_local_service(pending["bindings"], pending["credential"], generation)
            del self._pending[service_id]
        for sid, *_ in pending["bindings"]:
            self.manager.invalidate_server(sid)
        return {"ready": True}
