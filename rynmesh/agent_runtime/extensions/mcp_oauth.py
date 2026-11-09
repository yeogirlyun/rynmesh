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
from ..diagnostic_log import oauth_event
from ..store import AgentError
from .company_authorization import CompanyAuthorizationChannel, record_revocation

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
        super().__init__("agent_mcp_oauth_device_limit" if error == "device_limit" else "agent_mcp_oauth_request_failed")
        self.status = status
        self.oauth_error = error if error in {"invalid_grant", "invalid_client", "invalid_scope", "device_limit"} else None


def fail(code="agent_mcp_oauth_invalid_response"):
    raise McpOAuthError(code)


# RFC 6749 4.1.2.1 / RFC 8707 authorization-response errors -> controlled codes.
# Unknown values keep the historical generic denial code.
AUTHORIZATION_ERRORS = {
    "device_limit": "agent_mcp_oauth_device_limit",
    "access_denied": "agent_mcp_oauth_login_denied",
    "invalid_target": "agent_mcp_oauth_invalid_target",
    "invalid_scope": "agent_mcp_oauth_invalid_scope",
    "invalid_request": "agent_mcp_oauth_authorization_rejected",
    "unauthorized_client": "agent_mcp_oauth_authorization_rejected",
    "unsupported_response_type": "agent_mcp_oauth_authorization_rejected",
    "server_error": "agent_mcp_oauth_authorization_unavailable",
    "temporarily_unavailable": "agent_mcp_oauth_authorization_unavailable",
}


def flow_ref(auth_id):
    """One-way correlation id for logs; never reveals or reconstructs auth_id."""
    return hashlib.sha256(("ryn.oauth.flow.v1:" + str(auth_id)).encode()).hexdigest()[:12]


def _trace(stage, flow=None, **fields):
    try:
        server = (flow or {}).get("server") or {}
        oauth_event(stage, server=server.get("id"), flow=(flow or {}).get("ref"), **fields)
    except Exception:
        pass


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
        # Token renewal may wait on the authorization server's own lookups. Giving
        # up early keeps an old credential that a rotating server may reject.
        with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
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


COMPANY_PLUGIN_IDS = frozenset({"company-workspace", "company-dashboard", "company-affiliate"})


class McpOAuthStore:
    def __init__(self, store):
        self.path, self.lock = store.root / "mcp_oauth.json", store.root / ".mcp_oauth.lock"
        self.key, self.pub = store.key, store.pub
        self._cached_stamp = None
        self._cached_value = None

    def _read(self):
        if not self.path.exists():
            self._cached_stamp = None
            self._cached_value = None
            return {"version": VERSION, "servers": {}}
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
            if self._cached_stamp == stamp and self._cached_value is not None:
                return copy.deepcopy(self._cached_value)
            envelope = read_json(self.path, max_bytes=MAX_STORAGE)
            value = json.loads(peer_box.open_sealed(self.key, self.pub, envelope["nonce"], envelope["ciphertext"], info=CHANNEL))
            if value.get("version") != VERSION or not isinstance(value.get("servers"), dict):
                raise ValueError
            self._cached_stamp = stamp
            self._cached_value = copy.deepcopy(value)
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
            try:
                st = self.path.stat()
                self._cached_stamp = (st.st_mtime_ns, st.st_size)
                self._cached_value = copy.deepcopy(value)
            except OSError:
                self._cached_stamp = None
                self._cached_value = None
        except Exception:
            raise McpOAuthError("agent_mcp_oauth_storage_unavailable") from None

    def read(self):
        with file_transaction(self.lock):
            return self._read()

    @contextmanager
    def transaction(self, *, document=False):
        with file_transaction(self.lock):
            value = self._read()
            before = copy.deepcopy(value)
            yield value if document else value["servers"]
            if value != before:
                self._write(value)


class _Loopback:
    def __init__(self, callback, port, language, trace=None):
        trace = trace or (lambda *_args, **_kwargs: None)
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
                status, reason = 400, "parse"
                try:
                    parsed = urlsplit(self.path)
                    if parsed.path != "/callback":
                        # Browsers probe /favicon.ico etc.; never part of a flow.
                        reason = "path"
                        raise ValueError
                    pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=12)
                    reason = ("oversize" if len(self.path) + sum(len(k) + len(v) for k, v in self.headers.items()) > 16384
                              else "absolute_target" if parsed.scheme or parsed.netloc or parsed.fragment
                              else "host" if self.headers.get_all("Host") != [f"127.0.0.1:{self.server.server_port}"]
                              else "proxy_or_origin_header" if any(self.headers.get(name) for name in ("Origin", "Forwarded", "X-Forwarded-For"))
                              else "duplicate_param" if len(pairs) != len(dict(pairs)) else "")
                    if reason:
                        raise ValueError
                    status = 200 if callback(dict(pairs)) else 400
                except Exception:
                    if reason and reason != "path":
                        trace("callback_rejected", reason=reason)
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
            # POSIX can retain the closed callback in TIME_WAIT. Reuse that
            # address for the registered client; Windows keeps exclusive binds.
            allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")
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
        self._member_lock = threading.Lock()
        self._workspace_proof = None
        self.company_authorization = CompanyAuthorizationChannel(self)
        if self.credentials.read().get("company_authorization", {}).get("service"):
            self.company_authorization.sync_bindings()
            self.company_authorization.start()

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

    @staticmethod
    def _same_authorization(left, right):
        # Plugin/UI revisions and temporary disablement are not OAuth identity.
        # Endpoint, auth configuration, owner instance and config generation are.
        ignored = {"plugin_revision", "effective_enabled"}
        return ({k: v for k, v in (left or {}).items() if k not in ignored}
                == {k: v for k, v in (right or {}).items() if k not in ignored})

    def _identity_group_siblings(self, server):
        identity_group_id = server.get("identity_group_id")
        if not identity_group_id or not server.get("url"):
            return []
        try:
            target_res = canonical_url(server["url"])
        except Exception:
            return []
        siblings = []
        for other in self.store.list_mcp_servers_internal(include_tools=False):
            if (other["instance_id"] != server["instance_id"]
                    and other.get("identity_group_id") == identity_group_id
                    and other.get("effective_enabled", other.get("enabled", False))
                    and other.get("transport") == "streamable_http"
                    and not has_static_authorization(other)
                    and (other.get("auth") or {}).get("mode") != "none"):
                try:
                    source_auth, target_auth = server.get('auth') or {}, other.get('auth') or {}
                    compatible = (source_auth.get('client_id') == target_auth.get('client_id')
                                  and set(source_auth.get('scopes') or []) == set(target_auth.get('scopes') or []))
                    if compatible and canonical_url(other["url"]) == target_res:
                        siblings.append(other)
                except Exception:
                    pass
        return siblings

    def _propagate_identity_group_tokens(self, server):
        siblings = self._identity_group_siblings(server)
        if not siblings:
            return []
        propagated_ids = []
        with file_transaction(self.store.lock):
            with self.credentials.transaction() as rows:
                src = rows.get(server["instance_id"])
                if not src or not src.get("tokens"):
                    return []
                for sib in siblings:
                    sib_binding = self._binding(sib)
                    sib_row = rows.get(sib["instance_id"])
                    if sib_row and sib_row.get('local_logged_out'):
                        continue
                    if not sib_row or sib_row.get("binding") != sib_binding:
                        sib_row = {"binding": sib_binding, "resource": sib_binding["resource"], "epoch": secrets.token_hex(16), "granted_scopes": []}
                        rows[sib["instance_id"]] = sib_row
                    for k in ("tokens", "granted_scopes", "client", "metadata", "issuer"):
                        if k in src:
                            sib_row[k] = copy.deepcopy(src[k])
                    sib_row.pop("pending", None)
                    sib_row.pop("error", None)
                    sib_row.pop("challenge", None)
                    sib_row["updated_at"] = self.clock()
                    propagated_ids.append(sib["id"])
        return propagated_ids

    def _row(self, server):
        # The extension lock precedes the credential lock everywhere. This
        # makes identity rechecking and credential writes one local transaction.
        with file_transaction(self.store.lock):
            current = self._current(server)
            binding = self._binding(server)
            siblings = self._identity_group_siblings(current) if current.get("identity_group_id") else []
            with self.credentials.transaction() as rows:
                row = rows.get(server["instance_id"])
                if row and row.get("binding") != binding and self._same_authorization(row.get("binding"), binding):
                    row["binding"] = binding
                    row.pop("pending", None)
                if not row or row.get("binding") != binding:
                    row = {"binding": binding, "resource": binding["resource"], "epoch": secrets.token_hex(16), "granted_scopes": []}
                    rows[server["instance_id"]] = row
                if row.get("pending") and row.get("pending_expires_at", 0) <= self.clock():
                    row.pop("pending", None)
                row_tok = row.get("tokens") or {}
                row_valid = bool(row_tok and not row.get("error") and row_tok.get("expires_at", 0) > self.clock() + 60)
                if not row_valid and not row.get("pending") and not row.get('local_logged_out') and current.get("identity_group_id"):
                    for sib in siblings:
                        sib_row = rows.get(sib["instance_id"])
                        sib_tok = (sib_row or {}).get("tokens") or {}
                        if (
                            sib_row
                            and sib_row.get("binding") == self._binding(sib)
                            and sib_tok
                            and not sib_row.get("error")
                            and sib_tok.get("expires_at", 0) > self.clock() + 60
                        ):
                            for k in ("tokens", "granted_scopes", "client", "metadata", "issuer"):
                                if k in sib_row:
                                    row[k] = copy.deepcopy(sib_row[k])
                            row.pop("error", None)
                            row.pop("challenge", None)
                            row["updated_at"] = self.clock()
                            row_valid = True
                            break
                return copy.deepcopy(row)

    def _update(self, server, epoch, change, *, login_id=None, refresh=False):
        with file_transaction(self.store.lock):
            if refresh:
                current = self.store.get_mcp_server_internal(server["id"], require_enabled=False)
                if not self._same_authorization(self._binding(current), self._binding(server)):
                    fail("agent_mcp_oauth_configuration_changed")
            else:
                current = self._current(server)
            with self.credentials.transaction(document=True) as document:
                row = document["servers"].get(server["instance_id"])
                matches = (self._same_authorization(row.get("binding"), self._binding(server)) if row and refresh
                           else bool(row and row.get("binding") == self._binding(server)))
                if not row or row.get("epoch") != epoch or not matches:
                    fail("agent_mcp_oauth_configuration_changed")
                row["binding"] = self._binding(current)
                previous_tokens = copy.deepcopy(row.get('tokens'))
                previously_authorized = bool(previous_tokens)
                previous_scopes = set(row.get('granted_scopes') or [])
                previous_error = row.get('error')
                change(row)
                if previous_tokens != row.get('tokens'):
                    row['lease_revision'] = row.get('lease_revision', 0) + 1
                authorization_changed = False
                if (previously_authorized and not row.get('tokens')
                        and server.get('plugin_id') in COMPANY_PLUGIN_IDS):
                    record_revocation(document, [server['plugin_id']], reason='authorization_removed')
                    authorization_changed = True
                elif (previously_authorized and server.get('plugin_id') in COMPANY_PLUGIN_IDS
                      and ((row.get('error') == 'agent_mcp_auth_required' and not previous_error)
                           or (previous_scopes - set(row.get('granted_scopes') or [])))):
                    record_revocation(document, [server['plugin_id']], reason='authorization_reduced')
                    authorization_changed = True
                if login_id:
                    row.pop('local_logged_out', None)
                if login_id and server.get('plugin_id') in COMPANY_PLUGIN_IDS:
                    if server['id'] in {'company-workspace-mcp', 'company-dashboard-dashboard'}:
                        document['company_action'] = {'id': hashlib.sha256(login_id.encode()).hexdigest(), 'action': 'login',
                                                      'serverId': server['id'], 'instanceId': server['instance_id']}
                        sequence = record_revocation(document, ['*'], reason='account_login')
                        document['company_action']['authorizationSequence'] = sequence
                    else:
                        # A connection grant is not a new company person. Keep
                        # the verified identity action and its renewable lease.
                        state = document.setdefault('company_authorization',
                            {'sequence': 0, 'revocations': {}, 'acknowledged': -1})
                        state['sequence'] += 1
                        state.setdefault('scope_grants', {})[server['plugin_id']] = state['sequence']
                row["updated_at"] = self.clock()
                result = copy.deepcopy(row)
        if login_id and server.get("plugin_id") in COMPANY_PLUGIN_IDS:
            self.company_authorization.changed()
        elif authorization_changed:
            self.company_authorization._wake.set()
        return result

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
        _trace("login_expired", flow)
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
            flow = {"server": server, "epoch": row["epoch"], "state": "waiting", "expires_at": self.clock() + 600, "ref": flow_ref(auth_id)}
            self._flows[auth_id] = flow
            def begin(r):
                # An error from an earlier, finished attempt must not survive as the
                # status of this new attempt. Rows holding tokens keep their error:
                # it is what blocks those credentials until a login succeeds.
                if not r.get("tokens"):
                    r.pop("error", None)
                r.update(pending=auth_id, pending_expires_at=flow["expires_at"])
            self._update(server, row["epoch"], begin)
        listener = None
        try:
            metadata = self.discover(server)
            auth = server.get("auth") or {}
            prior = row.get("client") or {}
            port = auth.get("callback_port") or (prior.get("redirect_port") if prior.get("registration") == "dcr" else 0) or 0
            try:
                listener = _Loopback(lambda params: self.complete_login(auth_id, params), port, language,
                                     lambda stage, **kw: _trace(stage, flow, **kw))
            except OSError:
                if auth.get("callback_port") or prior.get("registration") != "dcr":
                    fail("agent_mcp_oauth_callback_unavailable")
                prior = {}
                listener = _Loopback(lambda params: self.complete_login(auth_id, params), 0, language,
                                     lambda stage, **kw: _trace(stage, flow, **kw))
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
            if server.get("plugin_id") in COMPANY_PLUGIN_IDS:
                # Persist independently of connection revisions and token rotation.
                # Scope the pseudonymous installation hint to the issuer.
                with self.credentials.transaction(document=True) as document:
                    seed = document.setdefault("device_seed", secrets.token_hex(32))
                    params["ryn_device_id"] = hmac.new(seed.encode(), metadata["issuer"].encode(), hashlib.sha256).hexdigest()
                params["ryn_replace_legacy"] = "oldest"
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
            _trace("login_started", flow, redirect_port=listener.server.server_port)
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
            _trace("login_start_failed", flow, code=flow["error"])
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
                "expires_at": self.clock() + expires, "refreshed_at": self.clock(), "scope": " ".join(granted), "token_type": "Bearer"}

    def complete_login(self, auth_id, params):
        with self._lock:
            flow = self._flows.get(auth_id)
            if not flow:
                _trace("callback_rejected", reason="flow_missing")
                return False
            if flow["state"] != "waiting":
                _trace("callback_rejected", flow, reason="flow_not_waiting", flow_state=flow["state"])
                return False
            if not isinstance(params, dict) or not all(isinstance(k, str) and isinstance(val, str) for k, val in params.items()):
                _trace("callback_rejected", flow, reason="params_type")
                return False
            if flow["expires_at"] <= self.clock():
                _trace("callback_rejected", flow, reason="flow_expired")
                self._expire(auth_id)
                return False
            metadata = flow.get("metadata")
            if not metadata or set(params) - {"state", "iss", "code", "error", "error_description", "error_uri"}:
                _trace("callback_rejected", flow, reason="unexpected_params" if metadata else "flow_not_ready")
                return False
            if len(params.get("state", "")) > 128 or not hmac.compare_digest(params.get("state", "").encode(), flow["oauth_state"].encode()):
                _trace("callback_rejected", flow, reason="state_mismatch")
                return False
            if (metadata["iss_required"] and "iss" not in params) or ("iss" in params and params["iss"] != metadata["issuer"]):
                _trace("callback_rejected", flow, reason="issuer_mismatch" if "iss" in params else "issuer_missing")
                return False
            flow["state"] = "exchanging"
        _trace("callback_accepted", flow, ok=bool(params.get("code")) and not params.get("error"))
        tokens = None
        try:
            if params.get("error") or not params.get("code"):
                upstream = params.get("error", "")
                code = AUTHORIZATION_ERRORS.get(upstream, "agent_mcp_oauth_login_denied")
                _trace("authorization_error", flow, code=code,
                       oauth_error=upstream if upstream in AUTHORIZATION_ERRORS else ("missing_code" if not upstream else "other"))
                fail(code)
            self.store.get_mcp_server_internal(flow["server"]["id"], expected_revision=flow["server"]["revision"])
            self._current(flow["server"])
            _trace("token_exchange_started", flow)
            try:
                result = self._token_request(metadata, flow["client"], {"grant_type": "authorization_code", "code": _text(params["code"], 4096),
                    "redirect_uri": flow["redirect_uri"], "code_verifier": flow["verifier"], "resource": self._binding(flow["server"])["resource"]})
            except OAuthHttpError as exc:
                _trace("token_exchange_failed", flow, code=str(exc), http_status=exc.status, oauth_error=exc.oauth_error or "other")
                raise
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
                self._update(flow["server"], flow["epoch"], save, login_id=auth_id)
                sibling_ids = self._propagate_identity_group_tokens(flow["server"])
                flow["state"] = "connected"
                flow["discovery_pending"] = self.on_connected is not None
            _trace("login_connected", flow)
            if self.on_connected:
                def notify():
                    try:
                        self.on_connected(flow["server"]["id"])
                        for sib_id in sibling_ids:
                            try:
                                self.on_connected(sib_id)
                            except Exception:
                                pass
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
                _trace("login_failed", flow, code=flow.get("error"))
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
            _trace("login_cancelled", flow)
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
        if not server.get("effective_enabled", server.get("enabled", False)):
            fail("agent_mcp_oauth_server_disabled")
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
        lock_key = f"{server.get('identity_group_id')}:{row.get('resource')}" if server.get("identity_group_id") else server["instance_id"]
        lock = self.store.root / (".mcp-oauth-refresh-" + hashlib.sha256(lock_key.encode()).hexdigest() + ".lock")
        with file_transaction(lock):
            current = self._row(server)
            if current["epoch"] != row["epoch"] or current.get("pending"):
                fail("agent_mcp_auth_required")
            latest = current.get("tokens")
            if latest and latest["expires_at"] > self.clock() + 60 and (latest != tokens or (not force_refresh and latest.get("refreshed_at", 0) > self.clock() - 30)):
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
                self._update(server, row["epoch"], save, refresh=True)
                current_server = self.store.get_mcp_server_internal(server["id"], require_enabled=False)
                self._propagate_identity_group_tokens(current_server)
            except AgentError:
                self._revoke({**current, "tokens": refreshed})
                raise
            if not current_server.get("effective_enabled", current_server.get("enabled", False)):
                fail("agent_mcp_oauth_server_disabled")
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

    def workspace_member(self):
        # Only this installed plugin owns the workspace resource. Never forward a
        # configurable server's bearer to an authority supplied by the client.
        import httpx
        servers = [s for s in self.store.list_mcp_servers_internal(include_tools=False)
                   if s.get("plugin_id") == "company-workspace" and s.get("effective_enabled")
                   and re.fullmatch(r"http://127[.]0[.]0[.]1:[0-9]+/ryn/agent-mcp", s.get("url") or "")]
        if len(servers) != 1:
            fail("agent_mcp_auth_required")
        server = servers[0]
        token = self.access_token(server)
        if not token or not re.fullmatch(r"ryn_at_[A-Za-z0-9_-]{43}", token):
            fail("agent_mcp_auth_required")
        # Multiple simultaneously visible views verify the same actual bearer.
        key = (server["instance_id"], hashlib.sha256(token.encode()).digest())
        with self._member_lock:
            cached = self._workspace_proof
            if cached and cached[0] == key and cached[1] > time.monotonic():
                return dict(cached[2])
            with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
                response = client.post("https://dashboard-hk-api.gshopper.com.cn/ryn/workspace/authorize",
                                       headers={"Authorization": "Bearer " + token}, json={"resource": server["url"]})
            if response.status_code != 200:
                fail("agent_mcp_auth_required")
            actor = response.json()
            member = actor.get("userid")
            if (actor.get("resource") != server["url"] or actor.get("scope") != "workspace.access"
                    or not isinstance(member, str) or not member or len(member) > 200):
                fail("agent_mcp_auth_required")
            result = {"memberId": member, "instanceId": server["instance_id"]}
            self._workspace_proof = (key, time.monotonic() + 2, result)
            return result

    def rebind_local_service(self, previous):
        """Preserve identity only for a verified host-owned transport rotation."""
        with self.credentials.transaction() as rows:
            for server in previous:
                row = rows.get(server['instance_id'])
                if row and row.get('binding') == self._binding(server):
                    current = self.store.get_mcp_server_internal(server['id'], require_enabled=False)
                    before, after = self._binding(server), self._binding(current)
                    if {k: v for k, v in before.items() if k != 'config_generation'} != {
                            k: v for k, v in after.items() if k != 'config_generation'}:
                        fail('agent_mcp_oauth_configuration_changed')
                    row['binding'] = after

    def company_connections(self):
        states = {}
        rows = self.credentials.read()["servers"]
        for server in self.store.list_mcp_servers_internal(include_tools=False):
            if server.get("plugin_id") not in COMPANY_PLUGIN_IDS or server.get("transport") != "streamable_http":
                continue
            row = rows.get(server["instance_id"], {})
            tokens = row.get("tokens") or {}
            usable = (server.get("effective_enabled") and not row.get("error") and not row.get("pending")
                      and self._same_authorization(row.get("binding"), self._binding(server))
                      and (tokens.get("refresh_token") or tokens.get("expires_at", 0) > self.clock()))
            states[server["id"]] = bool(usable)
        return states

    def company_action(self):
        return self.credentials.read().get("company_action", {"id": "", "action": "none"})

    def logout_company(self):
        with self._lock, file_transaction(self.store.lock):
            servers = [s for s in self.store.list_mcp_servers_internal(include_tools=False)
                       if s.get("plugin_id") in COMPANY_PLUGIN_IDS]
            instances = {s["instance_id"] for s in servers}
            with self.credentials.transaction(document=True) as document:
                for instance in instances:
                    document["servers"].pop(instance, None)
                action = {"id": secrets.token_hex(16), "action": "logout"}
                document["company_action"] = action
                record_revocation(document, ["*"], reason="company_logout")
            pending = [(flow["server"]["id"], ident) for ident, flow in self._flows.items()
                       if flow["server"]["instance_id"] in instances and flow["state"] in {"waiting", "exchanging"}]
        for sid, ident in pending:
            self.cancel_login(sid, ident)
        delivery = self.company_authorization.changed()
        return {"cleared": True, "servers": [s["id"] for s in servers], "action": action, "authorizationDelivery": delivery}

    def logout(self, server_id, expected_revision=None):
        server = self.store.get_mcp_server_internal(server_id, require_enabled=False, expected_revision=expected_revision)
        previous = self.credentials.read()["servers"].get(server["instance_id"])
        siblings = (self._identity_group_siblings(server) if server.get("identity_group_id")
                    and server.get("plugin_id") not in COMPANY_PLUGIN_IDS else [])
        sibling_instances = {s["instance_id"] for s in siblings}
        for ident, flow in list(self._flows.items()):
            if flow["server"]["instance_id"] == server["instance_id"] or flow["server"]["instance_id"] in sibling_instances:
                self.cancel_login(flow["server"]["id"], ident)
        if server.get("plugin_id") in COMPANY_PLUGIN_IDS:
            self.company_authorization.revoke([server["plugin_id"]], "connection_logout")
        row = self._row(server)
        self._update(server, row["epoch"], lambda r: (r.pop("tokens", None), r.pop("pending", None), r.pop("error", None), r.update(epoch=secrets.token_hex(16), local_logged_out=True)))
        if siblings:
            with file_transaction(self.store.lock):
                with self.credentials.transaction() as rows:
                    for sib in siblings:
                        sib_row = rows.get(sib["instance_id"])
                        if sib_row:
                            sib_row.pop("tokens", None)
                            sib_row.pop("pending", None)
                            sib_row.pop("error", None)
                            sib_row["epoch"] = secrets.token_hex(16)
                            sib_row["updated_at"] = self.clock()
        # Revoking a shared upstream refresh token would log out unrelated plugins.
        token = ((previous or row).get("tokens") or {}).get("refresh_token")
        shared = token and any((r.get("tokens") or {}).get("refresh_token") == token
                               for r in self.credentials.read()["servers"].values())
        if not shared:
            if server.get('plugin_id') in COMPANY_PLUGIN_IDS:
                threading.Thread(target=self._revoke, args=(previous or row,),
                                 name='company-remote-revoke', daemon=True).start()
            else:
                self._revoke(previous or row)
        result = self.public_status(server['instance_id'])
        if server.get('plugin_id') in COMPANY_PLUGIN_IDS:
            result['authorizationDelivery'] = self.company_authorization.changed()
        return result

    def forget(self, instance_id, *, previous=None):
        if previous is None:
            previous = next((s for s in self.store.list_mcp_servers_internal(include_tools=False)
                             if s["instance_id"] == instance_id), None)
        if previous and previous.get("plugin_id") in COMPANY_PLUGIN_IDS:
            self.company_authorization.revoke([previous["plugin_id"]], "connection_removed")
        for ident, flow in list(self._flows.items()):
            if flow["server"]["instance_id"] == instance_id:
                self.cancel_login(flow["server"]["id"], ident)
        shared_elsewhere = False
        with self.credentials.transaction() as rows:
            row = rows.pop(instance_id, None)
            if row and row.get("tokens"):
                tok = (row["tokens"].get("refresh_token") or row["tokens"].get("access_token"))
                if tok:
                    shared_elsewhere = any(
                        (other.get("tokens") or {}).get("refresh_token") == tok
                        or (other.get("tokens") or {}).get("access_token") == tok
                        for other in rows.values()
                    )
        if row and not shared_elsewhere:
            if previous and previous.get('plugin_id') in COMPANY_PLUGIN_IDS:
                threading.Thread(target=self._revoke, args=(row,),
                                 name='company-remote-revoke', daemon=True).start()
            else:
                self._revoke(row)

    def public_status(self, instance_id):
        server = next((s for s in self.store.list_mcp_servers_internal(include_tools=False) if s["instance_id"] == instance_id), None)
        if not server or server.get("transport") != "streamable_http" or has_static_authorization(server) or (server.get("auth") or {}).get("mode") == "none":
            return {"state": "not_required", "expires_at": None, "scopes": [], "issuer_host": None, "error": None}
        row = self._row(server)
        tokens = row.get("tokens") or {}
        state = "error" if row.get("error") else "logged_in" if tokens else "logged_out" if (server.get("auth") or {}).get("mode") == "oauth" or row.get("challenge") else "not_required"
        if tokens and not tokens.get("refresh_token") and tokens.get("expires_at", 0) <= self.clock() + 60:
            state = "logged_out"
        if row.get("pending"):
            state = "pending"
        # Opaque change marker lets local consumers reuse a short signed identity
        # lease while detecting logout, re-login and token replacement. Never
        # expose credentials (or use this marker as an authentication token).
        session_revision = hashlib.sha256(json.dumps(
            ["ryn.oauth.session.v1", row.get("epoch"), tokens], sort_keys=True,
        ).encode()).hexdigest() if tokens else None
        return {"state": state, "session_revision": session_revision,
                "expires_at": tokens.get("expires_at"), "scopes": row.get("granted_scopes", []),
                "issuer_host": urlsplit(row.get("issuer", "")).hostname, "error": row.get("error")}

    def close(self):
        self.company_authorization.close()
        for ident, flow in list(self._flows.items()):
            self.cancel_login(flow["server"]["id"], ident)

    def clear_all(self):
        self.logout_company()
        self.close()
        for ident in list(self.credentials.read()["servers"]):
            self.forget(ident)
        return {"cleared": True}
