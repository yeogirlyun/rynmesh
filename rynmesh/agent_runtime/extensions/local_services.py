"""Owner-only launch handshake for installer-managed loopback MCP services."""
from __future__ import annotations

import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from ..store import AgentError
from ...file_transactions import file_transaction


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
            try:
                server = self.store.get_mcp_server_internal(sid)
            except AgentError as exc:
                if str(exc) == "agent_mcp_server_not_found":
                    continue  # Removing one plugin must not prevent its shared page service starting.
                raise
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
        channel = getattr(getattr(self.manager, 'oauth', None), 'company_authorization', None)
        if channel is not None:
            channel.register(service_id=service_id, plugin_ids=plugin_ids,
                             server_ids=[sid for sid, *_ in bindings], port=port,
                             credential=credential, generation=generation)
        return {"generation": generation, "credential": credential}

    def activate(self, *, service_id, generation):
        with self._lock:
            pending = self._pending.get(service_id)
            if not pending or pending["expires"] <= self.clock() or pending["generation"] != generation:
                raise AgentError("agent_local_service_stale")
            # Per-launch service transport credentials are not a change of user
            # or issuer. Preserve an exactly bound OAuth row across this rotation.
            oauth = getattr(self.manager, 'oauth', None)
            with file_transaction(self.store.lock):
                previous = [self.store.get_mcp_server_internal(sid, require_enabled=False)
                            for sid, *_ in pending['bindings']]
                self.store.activate_local_service(pending["bindings"], pending["credential"], generation)
                if oauth is not None:
                    oauth.rebind_local_service(previous)
            del self._pending[service_id]
        for sid, *_ in pending["bindings"]:
            self.manager.invalidate_server(sid)
        return {"ready": True, "server_ids": [sid for sid, *_ in pending["bindings"]]}
