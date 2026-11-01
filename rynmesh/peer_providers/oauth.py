"""Resource-bound public-client OAuth with one-use loopback PKCE callbacks."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from .validation import PeerProviderError, fail, origin, text, url


class ProviderHttpError(PeerProviderError):
    def __init__(self, status=503):
        self.status = status
        super().__init__("peer_provider_request_failed")


async def request_json(method, target, *, headers=None, json=None, data=None):
    """No redirects, proxy credentials, unbounded responses, or raw error text."""
    url(target)
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            async with client.stream(method, target, headers=headers, json=json, data=data) as response:
                if not 200 <= response.status_code < 300:
                    raise ProviderHttpError(response.status_code)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 1048576:
                        fail()
                if not raw:
                    return {}
                result = __import__("json").loads(raw)
                if not isinstance(result, dict):
                    fail()
                return result
    except PeerProviderError:
        raise
    except Exception:
        raise ProviderHttpError() from None


class OAuthClient:
    """Network-only protocol engine; owner supplies sealed state and lifecycle."""
    def __init__(self, transport=request_json, *, clock, callback_factory=None):
        self.transport, self.clock = transport, clock
        self.callback_factory = callback_factory or self._listen
        self.listeners = {}

    async def call(self, method, target, **kwargs):
        try:
            result = await self.transport(method, target, **kwargs)
            if not isinstance(result, dict) or len(json.dumps(result).encode()) > 1048576:
                fail()
            return result
        except PeerProviderError:
            raise
        except Exception:
            raise ProviderHttpError() from None

    async def discover(self, resource):
        p = urlsplit(resource)
        metadata = await self.call("GET", origin(resource) + "/.well-known/oauth-protected-resource" + p.path)
        if metadata.get("resource") != resource:
            fail("peer_provider_resource_mismatch")
        issuers = metadata.get("authorization_servers")
        if not isinstance(issuers, list) or len(issuers) != 1:
            fail("peer_provider_invalid_metadata")
        issuer = url(issuers[0])
        # A portable declaration may not redirect this client's credentials to
        # an unrelated authority. Cross-origin issuers require a future explicit
        # owner-approved configuration, never an incoming peer hint.
        if origin(issuer) != origin(resource):
            fail("peer_provider_invalid_metadata")
        issuer_path = urlsplit(issuer).path
        doc = await self.call("GET", origin(issuer) + "/.well-known/oauth-authorization-server" + issuer_path)
        if doc.get("issuer") != issuer or "S256" not in doc.get("code_challenge_methods_supported", []) or doc.get("authorization_response_iss_parameter_supported") is not True:
            fail("peer_provider_invalid_metadata")
        result = {"issuer": issuer}
        for name in ("authorization_endpoint", "token_endpoint", "revocation_endpoint"):
            endpoint = url(doc.get(name))
            if origin(endpoint) != origin(issuer):
                fail("peer_provider_invalid_metadata")
            result[name] = endpoint
        if "none" not in doc.get("token_endpoint_auth_methods_supported", []) or "code" not in doc.get("response_types_supported", []):
            fail("peer_provider_invalid_metadata")
        return result

    async def start(self, config, callback):
        metadata = await self.discover(config["url"])
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        redirect_uri, close = await self.callback_factory(callback)
        p = urlsplit(redirect_uri)
        if p.scheme != "http" or p.hostname != "127.0.0.1" or not p.port or p.path != "/callback" or p.query or p.fragment:
            close()
            fail("peer_provider_callback_invalid")
        pending = {**metadata, "state": state, "verifier": verifier, "redirect_uri": redirect_uri,
                   "expires_at": self.clock() + 600, "resource": config["url"], "client_id": config["oauth"]["clientId"], "scopes": config["oauth"]["scopes"]}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        query = {"response_type": "code", "client_id": pending["client_id"], "redirect_uri": redirect_uri,
                 "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
                 "resource": pending["resource"], "scope": " ".join(pending["scopes"])}
        return pending, metadata["authorization_endpoint"] + "?" + urlencode(query), close

    def validate_callback(self, pending, params):
        if not isinstance(params, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in params.items()):
            fail("peer_provider_callback_invalid")
        if set(params) - {"code", "state", "iss", "error", "error_description", "error_uri"}:
            fail("peer_provider_callback_invalid")
        state = params.get("state", "")
        if len(state) > 128 or not hmac.compare_digest(state, pending["state"]) or params.get("iss") != pending["issuer"]:
            fail("peer_provider_callback_invalid")
        if self.clock() >= pending["expires_at"]:
            fail("peer_provider_login_expired")
        if params.get("error") or not params.get("code") or len(params["code"]) > 4096:
            fail("peer_provider_login_denied")

    def tokens(self, response, pending, previous=None):
        if not isinstance(response.get("token_type"), str) or response["token_type"].lower() != "bearer":
            fail("peer_provider_invalid_token_response")
        scope = response.get("scope", " ".join(pending["scopes"]))
        if not isinstance(scope, str) or response.get("resource", pending["resource"]) != pending["resource"] or set(scope.split()) != set(pending["scopes"]):
            fail("peer_provider_invalid_token_response")
        access = text(response.get("access_token"), 8192)
        refresh = text(response.get("refresh_token", (previous or {}).get("refresh_token")), 8192)
        if any(c.isspace() for c in access + refresh):
            fail("peer_provider_invalid_token_response")
        lifetime = response.get("expires_in")
        if type(lifetime) is not int or not 1 <= lifetime <= 86400:
            fail("peer_provider_invalid_token_response")
        return {**{k: pending[k] for k in ("issuer", "resource", "client_id", "scopes", "token_endpoint", "revocation_endpoint")},
                "access_token": access, "refresh_token": refresh, "expires_at": self.clock() + lifetime}

    async def exchange(self, pending, params):
        response = await self.call("POST", pending["token_endpoint"], data={"grant_type": "authorization_code", "client_id": pending["client_id"], "code": params["code"], "code_verifier": pending["verifier"], "redirect_uri": pending["redirect_uri"], "resource": pending["resource"]})
        return self.tokens(response, pending)

    async def refresh(self, session):
        response = await self.call("POST", session["token_endpoint"], data={"grant_type": "refresh_token", "client_id": session["client_id"], "refresh_token": session["refresh_token"], "resource": session["resource"]})
        return self.tokens(response, session, session)

    async def revoke(self, session):
        success = True
        for name in ("refresh_token", "access_token"):
            try:
                await self.call("POST", session["revocation_endpoint"], data={"client_id": session["client_id"], "token": session[name], "token_type_hint": name})
            except PeerProviderError:
                success = False
        return success

    async def _listen(self, callback):
        server = None
        timeout = None
        loop = asyncio.get_running_loop()

        def close():
            def close_on_loop():
                if timeout:
                    timeout.cancel()
                if server:
                    server.close()
            if not loop.is_closed():
                loop.call_soon_threadsafe(close_on_loop)

        async def receive(reader, writer):
            status, message = "400 Bad Request", b"Sign-in was not completed."
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                if len(raw) > 16384:
                    raise ValueError
                lines = raw.decode("ascii").split("\r\n")
                method, target, protocol = lines[0].split(" ")
                p = urlsplit(target)
                headers = {}
                for line in lines[1:]:
                    if not line:
                        continue
                    k, v = line.split(":", 1)
                    k = k.lower()
                    if k in headers:
                        raise ValueError
                    headers[k] = v.strip()
                if method != "GET" or protocol != "HTTP/1.1" or p.scheme or p.netloc or p.path != "/callback" or p.fragment:
                    raise ValueError
                if headers.get("host") != f"127.0.0.1:{server.sockets[0].getsockname()[1]}" or any(k in headers for k in ("forwarded", "x-forwarded-for", "origin")):
                    raise ValueError
                pairs = parse_qsl(p.query, keep_blank_values=True, strict_parsing=True)
                if len(pairs) != len(dict(pairs)):
                    raise ValueError
                await callback(dict(pairs))
                status, message = "200 OK", b"Sign-in completed. You can close this window."
                close()
            except Exception:
                pass  # Never echo callback parameters or upstream exceptions.
            try:
                writer.write(("HTTP/1.1 " + status + "\r\nContent-Type: text/plain; charset=utf-8\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nContent-Security-Policy: default-src 'none'; frame-ancestors 'none'\r\nConnection: close\r\nContent-Length: " + str(len(message)) + "\r\n\r\n").encode() + message)
                await writer.drain()
            except Exception:
                pass
            finally:
                writer.close()

        server = await asyncio.start_server(receive, "127.0.0.1", 0, limit=16384)
        timeout = asyncio.get_running_loop().call_later(600, close)
        return f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/callback", close
