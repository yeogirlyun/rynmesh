"""Shared local-app authentication for rynmesh and clawpad.

Both products run a private control API on a local process and want to reach it
remotely through a tunnel. The rule that makes that safe is small:

    a loopback socket is only trustworthy when nothing proxied the request

`cloudflared` runs on the same machine and dials the origin from 127.0.0.1, so
socket address alone says nothing about who is calling. Any forwarding header
means "remote", and remote means a valid session is required.

Stdlib only, no framework imports, no product-specific coupling — this file is
meant to be vendored verbatim by both codebases. Each product supplies the
request adapter (headers, client host) and wires its own middleware.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "COOKIE_NAME",
    "AuthDecision",
    "NodeAuth",
    "is_browser_cross_site",
    "is_forwarded",
    "is_loopback_addr",
]

COOKIE_NAME = "ryn_session"
SESSION_TTL_S = 30 * 24 * 3600
MAX_FAILURES = 8
FAILURE_WINDOW_S = 300.0
JWKS_TTL_S = 3600.0
# Floor between forced refetches, so unknown-kid probes cannot amplify.
JWKS_MIN_REFETCH_S = 60.0

# Any of these means something proxied the request, so the socket address is
# the proxy's, not the caller's. Checked case-insensitively by the adapter.
_FORWARD_HEADERS = (
    "x-forwarded-for",
    "x-real-ip",
    "forwarded",
    "cf-connecting-ip",
    "x-forwarded-host",
    "x-forwarded-proto",
)

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1", "testclient"}

# Host header values a genuine local request can carry. A DNS rebinding attack
# needs a *name* that resolves to 127.0.0.1, so names outside this set are
# refused on the loopback-trust path while bare IP literals stay fine.
_LOCAL_HOST_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]", "testserver"}


def is_loopback_addr(host: str) -> bool:
    return (host or "").strip().lower() in _LOOPBACK


def _hostname_of(value: str) -> str:
    """Strip scheme and port: 'http://127.0.0.1:8791' -> '127.0.0.1'."""
    text = str(value or "").strip().lower()
    if "//" in text:
        text = text.split("//", 1)[1]
    text = text.split("/", 1)[0]
    if text.startswith("["):  # bracketed IPv6 keeps its brackets
        return text.split("]", 1)[0] + "]" if "]" in text else text
    return text.rsplit(":", 1)[0] if text.count(":") == 1 else text


def _is_local_hostname(value: str) -> bool:
    name = _hostname_of(value)
    if not name or name in _LOCAL_HOST_NAMES:
        return True
    try:  # a bare IP literal cannot be a rebinding target
        int(name.replace(".", ""))
        return True
    except ValueError:
        return False


def is_browser_cross_site(headers: Mapping[str, str]) -> bool:
    """True when a browser says this request came from another site.

    Closes the hole loopback trust would otherwise leave open: a malicious page
    can make the visitor's own browser call http://127.0.0.1:<port>, and that
    request really is local. Browsers label it — non-browser clients (curl,
    agents, the desktop shell) send neither header and are unaffected.
    """
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}

    # Origin is the more precise signal, so it wins when present. The desktop
    # shell legitimately calls the node from another origin (tauri://localhost,
    # the Vite dev server), and those are local origins rather than other sites.
    origin = lowered.get("origin", "").strip()
    if origin and origin.lower() != "null":
        if _hostname_of(origin) == _hostname_of(lowered.get("host", "")):
            return False
        return not _is_local_hostname(origin)

    # No Origin: a cross-site GET (navigation, <img>, <script>) omits it, and
    # only Fetch Metadata gives it away.
    return lowered.get("sec-fetch-site", "").strip().lower() in {"cross-site", "same-site"}


def is_forwarded(headers: Mapping[str, str]) -> bool:
    lowered = {str(k).lower() for k in headers.keys()}
    return any(name in lowered for name in _FORWARD_HEADERS)


@dataclass(frozen=True)
class AuthDecision:
    allowed: bool
    reason: str
    # "local" | "session" | "perimeter" | "" — useful for logging and for the
    # UI to explain why it is or isn't asking for the token.
    via: str = ""


@dataclass
class NodeAuth:
    """Device token + signed session, with optional verified perimeter auth."""

    home: Path
    token_filename: str = "control_token"
    ttl_s: float = SESSION_TTL_S
    # Cloudflare Access (layer 3). Both must be set to be considered at all.
    access_team_domain: str = ""
    access_audience: str = ""
    # Last JWKS fetch failure, "" when healthy. Surfaced so a misconfigured
    # perimeter is diagnosable rather than an invisible fallback to the token.
    access_error: str = ""
    _failures: dict[str, list[float]] = field(default_factory=dict, repr=False)
    _jwks: dict[str, Any] = field(default_factory=dict, repr=False)
    _jwks_fetched_at: float = field(default=0.0, repr=False)
    _jwks_refetch_at: float = field(default=0.0, repr=False)

    # ---- device token ---------------------------------------------------
    @property
    def token_path(self) -> Path:
        return Path(self.home).expanduser() / self.token_filename

    def token(self) -> str:
        """The device token, created on first use with 0600 permissions."""
        path = self.token_path
        try:
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except (OSError, ValueError):
            pass
        created = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create with restrictive permissions from the start rather than
        # writing world-readable and chmod-ing after.
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(created)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return created

    def rotate_token(self) -> str:
        """Invalidate the token and every session derived from it."""
        try:
            self.token_path.unlink()
        except OSError:
            pass
        self._failures.clear()
        return self.token()

    def _secret(self) -> bytes:
        return hashlib.sha256(("rynmesh.session:" + self.token()).encode("utf-8")).digest()

    # ---- sessions -------------------------------------------------------
    def issue_session(self, *, now: float | None = None) -> str:
        now = time.time() if now is None else now
        expires = int(now + self.ttl_s)
        payload = f"{int(now)}.{expires}"
        signature = hmac.new(self._secret(), payload.encode("utf-8"), hashlib.sha256).digest()
        return payload + "." + base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")

    def verify_session(self, cookie: str, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        parts = str(cookie or "").split(".")
        if len(parts) != 3:
            return False
        issued, expires, provided = parts
        try:
            if float(expires) < now:
                return False
            int(issued)
        except (TypeError, ValueError):
            return False
        payload = f"{issued}.{expires}".encode("utf-8")
        expected = hmac.new(self._secret(), payload, hashlib.sha256).digest()
        expected_b64 = base64.urlsafe_b64encode(expected).decode("ascii").rstrip("=")
        return hmac.compare_digest(expected_b64, provided)

    # ---- unlock (rate limited) -----------------------------------------
    def _record_failure(self, client: str, now: float) -> None:
        window = [t for t in self._failures.get(client, []) if now - t < FAILURE_WINDOW_S]
        window.append(now)
        self._failures[client] = window

    def is_rate_limited(self, client: str, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        window = [t for t in self._failures.get(client, []) if now - t < FAILURE_WINDOW_S]
        self._failures[client] = window
        return len(window) >= MAX_FAILURES

    def unlock(self, presented: str, *, client: str = "", now: float | None = None) -> str:
        """Exchange the device token for a session. Returns "" on failure."""
        now = time.time() if now is None else now
        if self.is_rate_limited(client, now=now):
            return ""
        # Constant-time: never leak how much of the token matched.
        if not hmac.compare_digest(str(presented or ""), self.token()):
            self._record_failure(client, now)
            return ""
        self._failures.pop(client, None)
        return self.issue_session(now=now)

    # ---- perimeter (optional) ------------------------------------------
    @staticmethod
    def _ssl_context() -> Any:
        """Trust store for the JWKS fetch.

        python.org framework builds ship without a CA bundle unless the bundled
        "Install Certificates.command" has been run, so every HTTPS fetch fails
        with CERTIFICATE_VERIFY_FAILED. Fall back to certifi in that case —
        otherwise Access verification silently never succeeds. Verification is
        never disabled; without a usable store the fetch is allowed to fail.
        """
        import ssl

        context = ssl.create_default_context()
        if context.cert_store_stats().get("x509_ca", 0):
            return context
        try:
            import certifi
        except ImportError:
            return context
        return ssl.create_default_context(cafile=certifi.where())

    def _access_keys(self, *, now: float, force: bool = False) -> list[dict[str, Any]]:
        if not self.access_team_domain:
            return []
        fresh = self._jwks and now - self._jwks_fetched_at < JWKS_TTL_S
        if fresh and not force:
            return list(self._jwks.get("keys", []))
        if force:
            # An unknown kid triggers this, and an attacker can mint unknown
            # kids at will — throttle so that can't become a fetch amplifier.
            if now - self._jwks_refetch_at < JWKS_MIN_REFETCH_S:
                return list(self._jwks.get("keys", []))
            self._jwks_refetch_at = now
        url = f"https://{self.access_team_domain}/cdn-cgi/access/certs"
        try:
            with urllib.request.urlopen(url, timeout=10, context=self._ssl_context()) as response:
                self._jwks = json.loads(response.read().decode("utf-8"))
                self._jwks_fetched_at = now
                self.access_error = ""
        except Exception as exc:
            # Keep serving the cached keys rather than locking everyone out
            # over a transient failure — but record why, so a permanently
            # broken perimeter is visible instead of silently falling back to
            # the token forever.
            self.access_error = f"{type(exc).__name__}: {exc}"[:200]
            return list(self._jwks.get("keys", []))
        return list(self._jwks.get("keys", []))

    def warm_access_keys(self) -> int:
        """Fetch the JWKS ahead of time. Returns the number of keys held.

        Called at startup so the first real request doesn't pay a blocking
        network fetch inside the async request path. A zero return with
        `access_error` set means Access is configured but unusable.
        """
        if not (self.access_team_domain and self.access_audience):
            return 0
        return len(self._access_keys(now=time.time()))

    @property
    def access_configured(self) -> bool:
        return bool(self.access_team_domain and self.access_audience)

    def verify_access_jwt(self, token: str, *, now: float | None = None) -> bool:
        """Verify a Cloudflare Access assertion properly.

        Presence of the header is never enough: anything that can reach the
        origin directly could set it. Requires RS256 signature over the team's
        published keys, plus audience and expiry.
        """
        now = time.time() if now is None else now
        if not (self.access_team_domain and self.access_audience and token):
            return False
        parts = str(token).split(".")
        if len(parts) != 3:
            return False
        header_b64, payload_b64, signature_b64 = parts

        def decode(segment: str) -> bytes:
            return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))

        try:
            header = json.loads(decode(header_b64))
            payload = json.loads(decode(payload_b64))
            signature = decode(signature_b64)
        except Exception:
            return False
        if header.get("alg") != "RS256":
            return False
        if float(payload.get("exp", 0)) < now:
            return False
        audience = payload.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if self.access_audience not in [str(a) for a in audiences]:
            return False

        signed = f"{header_b64}.{payload_b64}".encode("ascii")
        kid = header.get("kid")

        def find(keys: list[dict[str, Any]]) -> dict[str, Any] | None:
            return next((k for k in keys if k.get("kid") == kid), None)

        key = find(self._access_keys(now=now))
        if key is None:
            # Cloudflare rotates signing keys. Without this refetch a rotation
            # would break Access sign-in until the cache expired.
            key = find(self._access_keys(now=now, force=True))
        if key is None:
            return False
        return _rs256_verify(signed, signature, key)

    # ---- the decision ---------------------------------------------------
    def authorize(
        self,
        *,
        client_host: str,
        headers: Mapping[str, str],
        cookie: str = "",
        now: float | None = None,
    ) -> AuthDecision:
        now = time.time() if now is None else now
        forwarded = is_forwarded(headers)

        # Local desktop use: trusted, but only when nothing proxied the
        # request, the Host is a name that cannot be rebound onto loopback, and
        # no browser is telling us this came from another site.
        lowered_headers = {str(k).lower(): str(v) for k, v in headers.items()}
        if (
            not forwarded
            and is_loopback_addr(client_host)
            and _is_local_hostname(lowered_headers.get("host", ""))
            and not is_browser_cross_site(headers)
        ):
            return AuthDecision(True, "loopback", via="local")

        if cookie and self.verify_session(cookie, now=now):
            return AuthDecision(True, "session", via="session")

        lowered = {str(k).lower(): v for k, v in headers.items()}
        assertion = lowered.get("cf-access-jwt-assertion", "")
        if assertion and self.verify_access_jwt(assertion, now=now):
            return AuthDecision(True, "cloudflare_access", via="perimeter")

        # A bearer token is accepted for API clients (agents, scripts).
        bearer = lowered.get("authorization", "")
        if bearer.lower().startswith("bearer "):
            presented = bearer.split(" ", 1)[1].strip()
            if hmac.compare_digest(presented, self.token()):
                return AuthDecision(True, "bearer_token", via="session")

        return AuthDecision(False, "unauthenticated", via="")


def _rs256_verify(signed: bytes, signature: bytes, jwk: Mapping[str, Any]) -> bool:
    """RSA-SHA256 verify against a JWK. Uses `cryptography` when available."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError:  # pragma: no cover - cryptography is a rynmesh dep
        return False

    def to_int(segment: str) -> int:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        return int.from_bytes(raw, "big")

    try:
        public = rsa.RSAPublicNumbers(to_int(jwk["e"]), to_int(jwk["n"])).public_key()
        public.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        return False
    return True
