"""Instance-bound MCP OAuth. Credentials never cross the host/UI boundary."""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

from ... import __version__
from ...atomic_io import atomic_write_json, read_json
from ...file_transactions import file_transaction
from ...services import peer_box
from ..store import AgentError

CHANNEL = b"rynmesh-agent-mcp-oauth-v1"
VERSION = "ryn.agent.mcp-oauth.v1"
USER_AGENT = "Ryn/" + __version__
RYN_MCP_CLIENT_METADATA_URL = ""
MAX_RESPONSE = 65536
MAX_STORAGE = 4 * 1024 * 1024


class McpOAuthError(AgentError):
    def __init__(self, code):
        super().__init__(code)
        self.dispatched = False


class OAuthHttpError(McpOAuthError):
    def __init__(self, status=503, error=None):
        super().__init__("agent_mcp_oauth_request_failed")
        self.status = status
        self.oauth_error = error if error in {"invalid_grant", "invalid_client", "invalid_scope"} else None


def fail(code="agent_mcp_oauth_invalid_response"):
    raise McpOAuthError(code)


def canonical_url(value):
    if not isinstance(value, str) or not value or len(value) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in value) or "\\" in value or "#" in value:
        fail("agent_mcp_oauth_invalid_url")
    try:
        p = urlsplit(value)
        host, port = p.hostname, p.port
        if p.scheme not in {"http", "https"} or not host or "@" in p.netloc or port == 0:
            raise ValueError
        loopback = host == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if p.scheme == "http" and not loopback:
            raise ValueError
        host = "[" + host.lower() + "]" if ":" in host else host.lower()
        authority = host + (":" + str(port) if port and port != (443 if p.scheme == "https" else 80) else "")
        return urlunsplit((p.scheme.lower(), authority, p.path, p.query, ""))
    except (ValueError, UnicodeError):
        fail("agent_mcp_oauth_invalid_url")


def origin(value):
    p = urlsplit(canonical_url(value))
    return p.scheme + "://" + p.netloc


def _text(value, limit=8192, *, allow_space=False):
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < (32 if allow_space else 33) or ord(c) > 126 for c in value):
        fail()
    return value


def _scopes(value):
    if isinstance(value, str):
        value = value.split()
    if not isinstance(value, list) or len(value) > 64 or any(not isinstance(s, str) or not re.fullmatch(r'[\x21\x23-\x5b\x5d-\x7e]{1,128}', s) for s in value):
        fail()
    return sorted(set(value))


def has_static_authorization(server):
    # A configured, even currently missing environment credential is not an
    # invitation to obtain a token from a different authorization mechanism.
    return bool(server.get("bearer_token_env") or any(
        str(key).lower() == "authorization"
        for field in ("secret_headers", "env_http_headers") for key in (server.get(field) or {})
    ))


def parse_auth_challenge(value):
    """Bounded Bearer auth-params; never preserve arbitrary upstream prose."""
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value):
        return {}
    match = re.search(r"(?:^|,)\s*Bearer\s+", value, re.I)
    if not match:
        return {}
    result, rest = {}, value[match.end():]
    pattern = re.compile(r'\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^,\s]+))\s*(?:,|$)')
    while rest:
        found = pattern.match(rest)
        if not found:
            break
        key = found[1].lower()
        val = re.sub(r"\\(.)", r"\1", found[2]) if found[2] is not None else found[3]
        if key in result:
            return {}
        if key in {"resource_metadata", "scope"}:
            result[key] = val
        elif key == "error" and val in {"invalid_token", "insufficient_scope", "invalid_request"}:
            result[key] = val
        rest = rest[found.end():]
    return result


def request_json(method, target, *, headers=None, data=None, json=None):
    canonical_url(target)
    try:
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            with client.stream(method, target, headers={"User-Agent": USER_AGENT, **(headers or {})}, data=data, json=json) as response:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        fail("agent_mcp_oauth_response_limit")
                try:
                    value = __import__("json").loads(raw) if raw else {}
                except (ValueError, UnicodeError):
                    value = None
                if not 200 <= response.status_code < 300:
                    raise OAuthHttpError(response.status_code, value.get("error") if isinstance(value, dict) else None)
                if not isinstance(value, dict):
                    fail()
                return value
    except McpOAuthError:
        raise
    except Exception:
        raise OAuthHttpError() from None


class McpOAuthStore:
    def __init__(self, store):
        self.path, self.lock = store.root / "mcp_oauth.json", store.root / ".mcp_oauth.lock"
        self.key, self.pub = store.key, store.pub

    def _read(self):
        if not self.path.exists():
            return {"version": VERSION, "servers": {}}
        try:
            envelope = read_json(self.path, max_bytes=MAX_STORAGE)
            value = json.loads(peer_box.open_sealed(self.key, self.pub, envelope["nonce"], envelope["ciphertext"], info=CHANNEL))
            if value.get("version") != VERSION or not isinstance(value.get("servers"), dict):
                raise ValueError
            return value
        except Exception:
            raise McpOAuthError("agent_mcp_oauth_storage_unavailable") from None

    def _write(self, value):
        try:
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            if len(raw) > MAX_STORAGE // 2:
                raise ValueError
            nonce, ciphertext = peer_box.seal(self.key, self.pub, raw, info=CHANNEL)
            atomic_write_json(self.path, {"version": VERSION, "nonce": nonce, "ciphertext": ciphertext}, max_bytes=MAX_STORAGE)
        except Exception:
            raise McpOAuthError("agent_mcp_oauth_storage_unavailable") from None

    def read(self):
        with file_transaction(self.lock):
            return self._read()

    @contextmanager
    def transaction(self):
        with file_transaction(self.lock):
            value = self._read()
            before = copy.deepcopy(value)
            yield value["servers"]
            if value != before:
                self._write(value)


class _Loopback:
    def __init__(self, callback, port, language):
        messages = {"connected": "Authorization completed. Return to Ryn.", "failed": "Authorization was not completed. Return to Ryn."}
        try:
            locale = "zh-CN" if str(language).lower().startswith("zh") else "en"
            messages.update(json.loads((Path(__file__).parents[2] / "locales" / f"mcp_oauth.{locale}.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)
            def log_message(self, *_args):
                pass
            def do_GET(self):
                status = 400
                try:
                    parsed = urlsplit(self.path)
                    pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=12)
                    if (len(self.path) + sum(len(k) + len(v) for k, v in self.headers.items()) > 16384 or parsed.scheme or parsed.netloc or parsed.path != "/callback" or parsed.fragment
                            or self.headers.get_all("Host") != [f"127.0.0.1:{self.server.server_port}"]
                            or any(self.headers.get(name) for name in ("Origin", "Forwarded", "X-Forwarded-For"))
                            or len(pairs) != len(dict(pairs))):
                        raise ValueError
                    status = 200 if callback(dict(pairs)) else 400
                except Exception:
                    pass
                body = messages["connected" if status == 200 else "failed"].encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)
        class CallbackServer(ThreadingHTTPServer):
            allow_reuse_address = False
            def server_bind(self):
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()
        self.server = CallbackServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.url = f"http://127.0.0.1:{self.server.server_port}/callback"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True, name="ryn-mcp-oauth-loopback")
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        if threading.current_thread() is not self.thread:
            self.thread.join(timeout=1)


class McpOAuthManager:
    def __init__(self, store, *, transport=None, clock=time.time, on_connected=None, trusted_origin=None):
        self.store, self.credentials, self.clock = store, McpOAuthStore(store), clock
        self.transport, self.on_connected = transport or request_json, on_connected
        self.trusted_origin = trusted_origin
        self._lock, self._flows = threading.RLock(), {}

    def _binding(self, server):
        return {"server_id": server["id"], "instance_id": server["instance_id"],
                "config_generation": server.get("config_generation", 1), "plugin_revision": server.get("plugin_revision"),
                "effective_enabled": server.get("effective_enabled", server.get("enabled", False)),
                "resource": canonical_url(server["url"]), "auth": server.get("auth") or {"mode": "auto"}}

    def _current(self, server):
        current = self.store.get_mcp_server_internal(server["id"], require_enabled=False)
        if self._binding(current) != self._binding(server):
            fail("agent_mcp_oauth_configuration_changed")
        return current

    def _row(self, server):
        # The extension lock precedes the credential lock everywhere. This
        # makes identity rechecking and credential writes one local transaction.
        with file_transaction(self.store.lock):
            self._current(server)
            binding = self._binding(server)
            with self.credentials.transaction() as rows:
                row = rows.get(server["instance_id"])
                if not row or row.get("binding") != binding:
                    row = {"binding": binding, "resource": binding["resource"], "epoch": secrets.token_hex(16), "granted_scopes": []}
                    rows[server["instance_id"]] = row
                if row.get("pending") and row.get("pending_expires_at", 0) <= self.clock():
                    row.pop("pending", None)
                return copy.deepcopy(row)

    def _update(self, server, epoch, change):
        with file_transaction(self.store.lock):
            self._current(server)
            with self.credentials.transaction() as rows:
                row = rows.get(server["instance_id"])
                if not row or row.get("epoch") != epoch or row.get("binding") != self._binding(server):
                    fail("agent_mcp_oauth_configuration_changed")
                change(row)
                row["updated_at"] = self.clock()
                return copy.deepcopy(row)

    def _allowed_url(self, resource, target):
        target = canonical_url(target)
        if origin(resource) != origin(target) and not (self.trusted_origin and self.trusted_origin(resource, target)):
            fail("agent_mcp_oauth_untrusted_endpoint")
        return target

    def _call(self, method, target, **kwargs):
        try:
            value = self.transport(method, target, headers={"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}, **kwargs)
            if not isinstance(value, dict) or len(json.dumps(value).encode()) > MAX_RESPONSE:
                fail("agent_mcp_oauth_response_limit")
            return value
        except McpOAuthError:
            raise
        except Exception:
            raise OAuthHttpError() from None

    def discover(self, server, challenge=None):
        row = self._row(server)
        resource, challenge = row["resource"], challenge or row.get("challenge") or {}
        parsed = urlsplit(resource)
        candidates = [challenge["resource_metadata"]] if challenge.get("resource_metadata") else list(dict.fromkeys([
            origin(resource) + "/.well-known/oauth-protected-resource" + parsed.path,
            origin(resource) + "/.well-known/oauth-protected-resource"]))
        prm = self._metadata(resource, candidates)
        if canonical_url(prm.get("resource")) != resource:
            fail("agent_mcp_oauth_resource_mismatch")
        issuers = prm.get("authorization_servers")
        if not isinstance(issuers, list) or not issuers or len(issuers) > 16:
            fail()
        if row.get("issuer") and row["issuer"] not in issuers:
            self._update(server, row["epoch"], lambda r: (r.pop("tokens", None), r.pop("client", None), r.update(error="agent_mcp_oauth_issuer_changed")))
            fail("agent_mcp_oauth_issuer_changed")
        issuer = row.get("issuer") or issuers[0]
        self._allowed_url(resource, issuer)
        if canonical_url(issuer) != issuer or urlsplit(issuer).query:
            fail("agent_mcp_oauth_issuer_mismatch")
        root, path = origin(issuer), urlsplit(issuer).path.rstrip("/")
        candidates = [root + "/.well-known/oauth-authorization-server" + path,
                      root + "/.well-known/openid-configuration" + path]
        if path:
            candidates.append(issuer.rstrip("/") + "/.well-known/openid-configuration")
        metadata = self._metadata(resource, list(dict.fromkeys(candidates)))
        if metadata.get("issuer") != issuer:
            fail("agent_mcp_oauth_issuer_mismatch")
        if "S256" not in metadata.get("code_challenge_methods_supported", []):
            fail("agent_mcp_oauth_pkce_unsupported")
        safe = {"issuer": issuer, "iss_required": metadata.get("authorization_response_iss_parameter_supported") is True,
                "auth_methods": metadata.get("token_endpoint_auth_methods_supported", ["client_secret_basic"]),
                "cimd_supported": metadata.get("client_id_metadata_document_supported") is True,
                "scopes": _scopes(prm.get("scopes_supported", []))}
        for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint"):
            if key in metadata or key in {"authorization_endpoint", "token_endpoint"}:
                safe[key] = self._allowed_url(resource, metadata.get(key))
        self._update(server, row["epoch"], lambda r: r.update(issuer=issuer, metadata=safe))
        return safe

    def _metadata(self, resource, candidates):
        for candidate in candidates:
            target = self._allowed_url(resource, candidate)
            try:
                return self._call("GET", target)
            except OAuthHttpError as exc:
                if exc.status not in {404, 405}:
                    raise
        fail("agent_mcp_oauth_discovery_failed")

    def _server(self, server_id, expected_revision=None):
        server = self.store.get_mcp_server_internal(server_id, require_enabled=False, expected_revision=expected_revision)
        if not server.get("effective_enabled", server.get("enabled", False)):
            fail("agent_mcp_server_disabled")
        if server.get("transport") != "streamable_http" or has_static_authorization(server) or (server.get("auth") or {}).get("mode") == "none":
            fail("agent_mcp_oauth_not_available")
        return server

    def _expire(self, auth_id):
        with self._lock:
            flow = self._flows.get(auth_id)
            if not flow or flow["state"] not in {"waiting", "exchanging"}:
                return
            flow["state"] = "expired"
            listener = flow.pop("listener", None)
            try:
                self._update(flow["server"], flow["epoch"], lambda r: r.pop("pending", None) if r.get("pending") == auth_id else None)
            except AgentError:
                pass
        if listener:
            listener.close()

    def start_login(self, server_id, expected_revision, language="zh"):
        server = self._server(server_id, expected_revision)
        for ident, old in list(self._flows.items()):
            if old["state"] in {"waiting", "exchanging"}:
                try:
                    self._current(old["server"])
                except AgentError:
                    self.cancel_login(old["server"]["id"], ident)
        row = self._row(server)
        with self._lock:
            for ident, old in list(self._flows.items()):
                if old["expires_at"] <= self.clock():
                    self._expire(ident)
                    self._flows.pop(ident, None)
            if len(self._flows) >= 64:
                for ident, old in list(self._flows.items()):
                    if old["state"] not in {"waiting", "exchanging"}:
                        self._flows.pop(ident)
                        break
            if sum(f["state"] in {"waiting", "exchanging"} for f in self._flows.values()) >= 8:
                fail("agent_mcp_oauth_pending_limit")
            if any(f["server"]["instance_id"] == server["instance_id"] and f["state"] in {"waiting", "exchanging"} for f in self._flows.values()):
                fail("agent_mcp_oauth_login_pending")
            auth_id = secrets.token_urlsafe(32)
            flow = {"server": server, "epoch": row["epoch"], "state": "waiting", "expires_at": self.clock() + 600}
            self._flows[auth_id] = flow
            self._update(server, row["epoch"], lambda r: r.update(pending=auth_id, pending_expires_at=flow["expires_at"]))
        listener = None
        try:
            metadata = self.discover(server)
            auth = server.get("auth") or {}
            prior = row.get("client") or {}
            port = auth.get("callback_port") or (prior.get("redirect_port") if prior.get("registration") == "dcr" else 0) or 0
            try:
                listener = _Loopback(lambda params: self.complete_login(auth_id, params), port, language)
            except OSError:
                if auth.get("callback_port") or prior.get("registration") != "dcr":
                    fail("agent_mcp_oauth_callback_unavailable")
                prior = {}
                listener = _Loopback(lambda params: self.complete_login(auth_id, params), 0, language)
            client = self._client(server, metadata, prior, listener.url)
            requested = set(_scopes(auth.get("scopes", []))) | set(row.get("granted_scopes", []))
            requested.update(_scopes((row.get("challenge") or {}).get("scope", metadata["scopes"])))
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
            flow.update(metadata=metadata, client=client, listener=listener, redirect_uri=listener.url,
                        oauth_state=state, verifier=verifier, scopes=sorted(requested))
            def save(current):
                if current.get("pending") != auth_id or flow["state"] != "waiting":
                    fail("agent_mcp_oauth_login_cancelled")
                current.update(client=client, metadata=metadata, issuer=metadata["issuer"])
            with self._lock:
                self._update(server, row["epoch"], save)
            params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": listener.url,
                      "state": state, "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("="),
                      "code_challenge_method": "S256", "resource": row["resource"]}
            if requested:
                params["scope"] = " ".join(sorted(requested))
            endpoint = urlsplit(metadata["authorization_endpoint"])
            existing = parse_qsl(endpoint.query, keep_blank_values=True)
            if set(dict(existing)) & set(params) or len(existing) != len(dict(existing)):
                fail()
            authorization_url = urlunsplit(endpoint._replace(query=urlencode([*existing, *params.items()])))
            if len(authorization_url) > 8192:
                fail()
            timer = threading.Timer(600, self._expire, (auth_id,))
            timer.daemon = True
            flow["timer"] = timer
            timer.start()
            return {"auth_id": auth_id, "authorization_url": authorization_url, "expires_in": 600}
        except Exception as exc:
            with self._lock:
                flow["state"], flow["error"] = "failed", str(exc) if isinstance(exc, McpOAuthError) else "agent_mcp_oauth_request_failed"
                flow.pop("listener", None)
                try:
                    self._update(server, row["epoch"], lambda r: (r.pop("pending", None), r.update(error=flow["error"])) if r.get("pending") == auth_id else None)
                except AgentError:
                    pass
            if listener:
                listener.close()
            raise McpOAuthError(flow["error"]) from None

    def _client(self, server, metadata, prior, redirect_uri):
        auth = server.get("auth") or {}
        if auth.get("client_id"):
            return {"client_id": _text(auth["client_id"], 256, allow_space=True), "registration": "preregistered", "redirect_port": 0, "client_secret": auth.get("client_secret")}
        mode = server.get("oauth_client_registration", "auto")
        if mode in {"auto", "cimd"} and metadata["cimd_supported"] and RYN_MCP_CLIENT_METADATA_URL:
            return {"client_id": canonical_url(RYN_MCP_CLIENT_METADATA_URL), "registration": "cimd", "redirect_port": 0}
        if mode not in {"auto", "dcr"} or "registration_endpoint" not in metadata:
            fail("agent_mcp_oauth_client_required")
        if prior.get("registration") == "dcr" and prior.get("redirect_port") == urlsplit(redirect_uri).port:
            return prior
        result = self._call("POST", metadata["registration_endpoint"], json={"client_name": "Ryn", "application_type": "native",
            "redirect_uris": [redirect_uri], "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "token_endpoint_auth_method": "none"})
        if result.get("token_endpoint_auth_method", "none") != "none" or result.get("client_secret") or ("redirect_uris" in result and result["redirect_uris"] != [redirect_uri]):
            fail()
        return {"client_id": _text(result.get("client_id"), 256, allow_space=True), "registration": "dcr", "redirect_port": urlsplit(redirect_uri).port}

    def _token_request(self, metadata, client, data):
        headers = {}
        data = {"client_id": client["client_id"], **data}
        secret = client.get("client_secret")
        if secret:
            _text(secret)
            if "client_secret_basic" in metadata["auth_methods"]:
                raw = quote(client["client_id"], safe="") + ":" + quote(secret, safe="")
                headers["Authorization"] = "Basic " + base64.b64encode(raw.encode()).decode()
            elif "client_secret_post" in metadata["auth_methods"]:
                data["client_secret"] = secret
            else:
                fail("agent_mcp_oauth_client_auth_unsupported")
        return self._call("POST", metadata["token_endpoint"], headers=headers, data=data)

    def _tokens(self, result, resource, scopes, previous=None):
        if str(result.get("token_type", "")).lower() != "bearer" or result.get("resource", resource) != resource:
            fail()
        granted = _scopes(result.get("scope", scopes))
        if not set(granted).issubset(scopes):
            fail()
        expires = result.get("expires_in", 300)
        if type(expires) is not int or not 1 <= expires <= 31536000:
            fail()
        refresh = result.get("refresh_token", (previous or {}).get("refresh_token"))
        return {"access_token": _text(result.get("access_token")), "refresh_token": _text(refresh) if refresh else None,
                "expires_at": self.clock() + expires, "scope": " ".join(granted), "token_type": "Bearer"}

    def complete_login(self, auth_id, params):
        with self._lock:
            flow = self._flows.get(auth_id)
            if not flow or flow["state"] != "waiting" or not isinstance(params, dict) or not all(isinstance(k, str) and isinstance(val, str) for k, val in params.items()):
                return False
            if flow["expires_at"] <= self.clock():
                self._expire(auth_id)
                return False
            metadata = flow.get("metadata")
            if not metadata or set(params) - {"state", "iss", "code", "error", "error_description", "error_uri"}:
                return False
            if len(params.get("state", "")) > 128 or not hmac.compare_digest(params.get("state", "").encode(), flow["oauth_state"].encode()) or (metadata["iss_required"] and "iss" not in params) or ("iss" in params and params["iss"] != metadata["issuer"]):
                return False
            flow["state"] = "exchanging"
        tokens = None
        try:
            if params.get("error") or not params.get("code"):
                fail("agent_mcp_oauth_login_denied")
            self.store.get_mcp_server_internal(flow["server"]["id"], expected_revision=flow["server"]["revision"])
            self._current(flow["server"])
            result = self._token_request(metadata, flow["client"], {"grant_type": "authorization_code", "code": _text(params["code"], 4096),
                "redirect_uri": flow["redirect_uri"], "code_verifier": flow["verifier"], "resource": self._binding(flow["server"])["resource"]})
            tokens = self._tokens(result, self._binding(flow["server"])["resource"], flow["scopes"])
            def save(row):
                if row.get("pending") != auth_id or flow["state"] != "exchanging" or flow["expires_at"] <= self.clock():
                    fail("agent_mcp_oauth_login_cancelled")
                self.store.get_mcp_server_internal(flow["server"]["id"], expected_revision=flow["server"]["revision"])
                row.update(tokens=tokens, granted_scopes=_scopes(tokens["scope"]))
                row.pop("pending", None)
                row.pop("error", None)
                row.pop("challenge", None)
            with self._lock:
                self._update(flow["server"], flow["epoch"], save)
                flow["state"] = "connected"
                flow["discovery_pending"] = self.on_connected is not None
            if self.on_connected:
                def notify():
                    try:
                        self.on_connected(flow["server"]["id"])
                    except Exception:
                        pass
                    finally:
                        with self._lock:
                            flow["discovery_pending"] = False
                threading.Thread(target=notify, daemon=True, name="ryn-mcp-oauth-discovery").start()
            return True
        except Exception as exc:
            with self._lock:
                if flow["state"] == "exchanging":
                    flow["state"], flow["error"] = "failed", str(exc) if isinstance(exc, McpOAuthError) else "agent_mcp_oauth_request_failed"
                try:
                    self._update(flow["server"], flow["epoch"], lambda r: (r.pop("pending", None), r.update(error=flow.get("error", "agent_mcp_auth_required"))) if r.get("pending") == auth_id else None)
                except AgentError:
                    pass
            if tokens:
                self._revoke({"tokens": tokens, "client": flow["client"], "metadata": metadata})
            return False
        finally:
            with self._lock:
                listener = flow.pop("listener", None)
                timer = flow.pop("timer", None)
                flow.pop("verifier", None)
                flow.pop("oauth_state", None)
            if timer:
                timer.cancel()
            if listener:
                listener.close()

    def login_status(self, server_id, auth_id):
        with self._lock:
            flow = self._flows.get(auth_id)
            if not flow or flow["server"]["id"] != server_id:
                fail("agent_mcp_oauth_login_not_found")
            if flow["expires_at"] <= self.clock():
                self._expire(auth_id)
            try:
                self._current(flow["server"])
            except AgentError:
                if flow["state"] in {"waiting", "exchanging"}:
                    self.cancel_login(server_id, auth_id)
            state = "exchanging" if flow["state"] == "connected" and flow.get("discovery_pending") else flow["state"]
            return {"state": state, **({"error": flow["error"]} if flow.get("error") else {})}

    def cancel_login(self, server_id, auth_id):
        with self._lock:
            flow = self._flows.get(auth_id)
            if not flow or flow["server"]["id"] != server_id:
                fail("agent_mcp_oauth_login_not_found")
            if flow["state"] not in {"waiting", "exchanging"}:
                return {"state": flow["state"]}
            flow["state"] = "cancelled"
            listener, timer = flow.pop("listener", None), flow.pop("timer", None)
            try:
                self._update(flow["server"], flow["epoch"], lambda r: r.pop("pending", None) if r.get("pending") == auth_id else None)
            except AgentError:
                pass
        if timer:
            timer.cancel()
        if listener:
            listener.close()
        return {"state": "cancelled"}

    def record_challenge(self, server, challenge):
        if has_static_authorization(server) or (server.get("auth") or {}).get("mode") == "none":
            return
        row = self._row(server)
        safe = {}
        if challenge.get("resource_metadata"):
            safe["resource_metadata"] = self._allowed_url(row["resource"], challenge["resource_metadata"])
        scopes = set(_scopes((row.get("challenge") or {}).get("scope", []))) | set(_scopes(challenge.get("scope", [])))
        if scopes:
            safe["scope"] = " ".join(sorted(scopes))
        if challenge.get("error") in {"insufficient_scope", "invalid_token", "invalid_request"}:
            safe["error"] = challenge["error"]
        self._update(server, row["epoch"], lambda r: r.update(challenge=safe, error="agent_mcp_auth_required"))

    def access_token(self, server, force_refresh=False, *, refresh_state=None):
        if has_static_authorization(server) or (server.get("auth") or {}).get("mode") == "none":
            return None
        row = self._row(server)
        if row.get("pending"):
            fail("agent_mcp_auth_required")
        if (row.get("challenge") or {}).get("error") == "insufficient_scope":
            fail("agent_mcp_auth_required")
        if row.get("error") and not force_refresh:
            fail("agent_mcp_auth_required")
        tokens = row.get("tokens")
        if not tokens:
            if (server.get("auth") or {}).get("mode") == "oauth" or row.get("challenge") or row.get("error"):
                fail("agent_mcp_auth_required")
            return None
        if not force_refresh and tokens["expires_at"] > self.clock() + (0 if refresh_state and refresh_state[0] else 60):
            return tokens["access_token"]
        if refresh_state and refresh_state[0]:
            fail("agent_mcp_auth_required")
        lock = self.store.root / (".mcp-oauth-refresh-" + hashlib.sha256(server["instance_id"].encode()).hexdigest() + ".lock")
        with file_transaction(lock):
            current = self._row(server)
            if current["epoch"] != row["epoch"] or current.get("pending"):
                fail("agent_mcp_auth_required")
            latest = current.get("tokens")
            if latest and latest != tokens and latest["expires_at"] > self.clock() + 60:
                return latest["access_token"]
            if not latest or not latest.get("refresh_token"):
                self._update(server, row["epoch"], lambda r: (r.pop("tokens", None), r.update(error="agent_mcp_auth_required")))
                fail("agent_mcp_auth_required")
            try:
                if refresh_state is not None:
                    refresh_state[0] = True
                response = self._token_request(current["metadata"], current["client"], {"grant_type": "refresh_token", "refresh_token": latest["refresh_token"], "resource": current["resource"]})
                refreshed = self._tokens(response, current["resource"], _scopes(latest["scope"]), latest)
            except OAuthHttpError as exc:
                if exc.oauth_error == "invalid_grant":
                    self._update(server, row["epoch"], lambda r: (r.pop("tokens", None), r.update(error="agent_mcp_auth_required")))
                    fail("agent_mcp_auth_required")
                fail("agent_mcp_oauth_refresh_unavailable")
            try:
                def save(value):
                    if value.get("pending"):
                        fail("agent_mcp_auth_required")
                    value.update(tokens=refreshed, granted_scopes=_scopes(refreshed["scope"]))
                    value.pop("error", None)
                self._update(server, row["epoch"], save)
            except AgentError:
                self._revoke({**current, "tokens": refreshed})
                raise
            return refreshed["access_token"]

    def _revoke(self, row):
        metadata, client, tokens = row.get("metadata") or {}, row.get("client") or {}, row.get("tokens") or {}
        token = tokens.get("refresh_token") or tokens.get("access_token")
        if metadata.get("revocation_endpoint") and token and client.get("client_id"):
            try:
                self._token_request({**metadata, "token_endpoint": metadata["revocation_endpoint"]}, client,
                    {"token": token, "token_type_hint": "refresh_token" if tokens.get("refresh_token") else "access_token"})
            except Exception:
                pass

    def logout(self, server_id, expected_revision=None):
        server = self.store.get_mcp_server_internal(server_id, require_enabled=False, expected_revision=expected_revision)
        previous = self.credentials.read()["servers"].get(server["instance_id"])
        for ident, flow in list(self._flows.items()):
            if flow["server"]["instance_id"] == server["instance_id"]:
                self.cancel_login(server_id, ident)
        row = self._row(server)
        self._update(server, row["epoch"], lambda r: (r.pop("tokens", None), r.pop("pending", None), r.pop("error", None), r.update(epoch=secrets.token_hex(16))))
        self._revoke(previous or row)
        return self.public_status(server["instance_id"])

    def forget(self, instance_id):
        for ident, flow in list(self._flows.items()):
            if flow["server"]["instance_id"] == instance_id:
                self.cancel_login(flow["server"]["id"], ident)
        with self.credentials.transaction() as rows:
            row = rows.pop(instance_id, None)
        if row:
            self._revoke(row)

    def public_status(self, instance_id):
        server = next((s for s in self.store.list_mcp_servers_internal() if s["instance_id"] == instance_id), None)
        if not server or server.get("transport") != "streamable_http" or has_static_authorization(server) or (server.get("auth") or {}).get("mode") == "none":
            return {"state": "not_required", "expires_at": None, "scopes": [], "issuer_host": None, "error": None}
        row = self._row(server)
        tokens = row.get("tokens") or {}
        state = "error" if row.get("error") else "logged_in" if tokens else "logged_out" if (server.get("auth") or {}).get("mode") == "oauth" or row.get("challenge") else "not_required"
        if tokens and not tokens.get("refresh_token") and tokens.get("expires_at", 0) <= self.clock() + 60:
            state = "logged_out"
        if row.get("pending"):
            state = "pending"
        return {"state": state, "expires_at": tokens.get("expires_at"), "scopes": row.get("granted_scopes", []),
                "issuer_host": urlsplit(row.get("issuer", "")).hostname, "error": row.get("error")}

    def close(self):
        for ident, flow in list(self._flows.items()):
            self.cancel_login(flow["server"]["id"], ident)

    def clear_all(self):
        self.close()
        for ident in list(self.credentials.read()["servers"]):
            self.forget(ident)
        return {"cleared": True}
