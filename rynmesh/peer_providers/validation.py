"""Bounded, credential-free records for authenticated peer directories."""
from __future__ import annotations

import base64
import copy
import ipaddress
import json
import math
import re
from urllib.parse import urlsplit, urlunsplit


class PeerProviderError(ValueError):
    """Only fixed public error codes cross the provider boundary."""


def fail(code="peer_provider_invalid_response"):
    raise PeerProviderError(code)


def text(value, maximum=255, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        fail()
    return value


def url(value, *, endpoint=False):
    text(value, 512 if endpoint else 2048)
    if any(c in value for c in ("\\", "#", "@")) or any(c.isspace() for c in value):
        fail("peer_provider_invalid_url")
    try:
        p = urlsplit(value)
        if p.scheme not in (("http", "https") if endpoint else ("https",)) or not p.hostname or p.query or p.fragment or p.username or p.password:
            fail("peer_provider_invalid_url")
        if p.port is not None and not 1 <= p.port <= 65535:
            fail("peer_provider_invalid_url")
        if endpoint:
            if p.path not in ("", "/"):
                fail("peer_provider_invalid_url")
            try:
                address = ipaddress.ip_address(p.hostname)
            except ValueError:
                address = None
                if p.hostname.lower() in {"localhost", "metadata", "metadata.google.internal"}:
                    fail("peer_provider_invalid_url")
            if address is not None and (address.is_unspecified or address.is_multicast or address.is_link_local or address.is_loopback):
                fail("peer_provider_invalid_url")
        host = p.hostname.lower()
        if ":" in host:
            host = "[" + host + "]"
        port = p.port
        authority = host + (":" + str(port) if port and port != (443 if p.scheme == "https" else 80) else "")
        path = p.path.rstrip("/")
        if any(part in (".", "..") for part in path.split("/")) or "%" in path:
            fail("peer_provider_invalid_url")
        return urlunsplit((p.scheme, authority, path, "", ""))
    except PeerProviderError:
        raise
    except (TypeError, ValueError):
        fail("peer_provider_invalid_url")


def origin(value):
    p = urlsplit(url(value))
    return p.scheme + "://" + p.netloc


def key(value):
    text(value, 44)
    try:
        raw = base64.b64decode(value, validate=True)
        if len(raw) != 32 or not any(raw) or base64.b64encode(raw).decode() != value:
            fail()
    except (ValueError, TypeError):
        fail()
    return value


def endpoints(values):
    if not isinstance(values, list) or len(values) > 4:
        fail()
    return list(dict.fromkeys(url(v, endpoint=True) for v in values))


def manifest(value):
    if not isinstance(value, dict) or set(value) != {"url", "oauth"}:
        fail("peer_provider_invalid_configuration")
    oauth = value["oauth"]
    if not isinstance(oauth, dict) or set(oauth) != {"clientId", "scopes"}:
        fail("peer_provider_invalid_configuration")
    client = text(oauth["clientId"], 255)
    scopes = oauth["scopes"]
    if not isinstance(scopes, list) or not 1 <= len(scopes) <= 16 or any(not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", s) for s in scopes):
        fail("peer_provider_invalid_configuration")
    return {"url": url(value["url"]), "oauth": {"clientId": client, "scopes": sorted(set(scopes))}}


def revision(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or len(str(value)) > 128 or not str(value):
        fail()
    return value


SERVICE_TEXT = {"package_id", "model_alias", "adapter", "runtime", "version", "protocol_version", "license_id"}
SERVICE_NUM = {"context_window", "max_output_tokens", "max_concurrent", "timeout_seconds"}


def model_record(value):
    """Project public fields, never copy arbitrary nested metadata."""
    from ..llm_package.peer_binding import valid

    if not isinstance(value, dict) or not isinstance(value.get("service"), dict):
        fail()
    source = value["service"]
    service = {k: text(v, 255) for k, v in source.items() if k in SERVICE_TEXT}
    sid = service.get("package_id")
    if not sid or not valid(value.get("model_binding"), sid):
        fail()
    for name in SERVICE_NUM:
        if name in source:
            number = source[name]
            types = (int, float) if name == "timeout_seconds" else (int,)
            if type(number) not in types or not math.isfinite(number) or not 0 < number <= 2000000:
                fail()
            service[name] = number
    caps = source.get("capabilities", [])
    if not isinstance(caps, list) or len(caps) > 32:
        fail()
    service["capabilities"] = [text(c, 80) for c in caps]
    pricing = source.get("pricing", {})
    if isinstance(pricing, dict):
        service["pricing"] = {k: v for k, v in pricing.items() if k in {"input_per_1k", "output_per_1k", "minimum", "maximum_per_task"} and type(v) in (int, float) and 0 <= v <= 1000000000}
        if "currency" in pricing:
            service["pricing"]["currency"] = text(pricing["currency"], 32)
    capacity = value.get("capacity", {})
    if not isinstance(capacity, dict):
        fail()
    capacity = {k: v for k, v in capacity.items() if k in {"available", "max_concurrent", "running", "queue_limit"} and type(v) is int and 0 <= v <= 2000000}
    protocols = value.get("delivery_protocols", [])
    if not isinstance(protocols, list) or any(p not in {"complete-v1", "stream-v1"} for p in protocols):
        fail()
    chat = value.get("chat_protocol")
    if chat not in (None, "rynmesh.chat.v1") or value.get("tool_support", "unknown") not in {"supported", "unsupported", "unknown"}:
        fail()
    return {"service": service, "capacity": capacity, "model_binding": copy.deepcopy(value["model_binding"]),
            "chat_protocol": chat, "tool_support": value.get("tool_support", "unknown"), "delivery_protocols": protocols[:]}


def directory(value, *, issuer, resource):
    if not isinstance(value, dict) or value.get("version") != "ryn.peer-directory.v1" or value.get("issuer") != issuer or value.get("resource") != resource:
        fail()
    if len(json.dumps(value, ensure_ascii=False).encode()) > 1048576:
        fail()
    members, models = value.get("members"), value.get("models")
    if not isinstance(members, list) or len(members) > 500 or not isinstance(models, list) or len(models) > 500:
        fail()
    safe_members, devices = [], {}
    for member in members:
        if not isinstance(member, dict) or not isinstance(member.get("devices"), list) or len(member["devices"]) > 32:
            fail()
        row = {"id": text(member.get("id")), "name": text(member.get("name"), empty=True), "devices": []}
        for device in member["devices"]:
            if not isinstance(device, dict):
                fail()
            d = {"peer_id": key(device.get("peer_id")), "messaging_pub": key(device.get("messaging_pub")), "name": text(device.get("name"), empty=True), "endpoints": endpoints(device.get("endpoints"))}
            if d["peer_id"] in devices:
                fail()
            devices[d["peer_id"]] = d
            row["devices"].append(d)
        safe_members.append(row)
    safe_models, seen = [], set()
    for model in models:
        row = model_record(model)
        peer = key(model.get("peer_id"))
        device = devices.get(peer)
        if not device or key(model.get("messaging_pub")) != device["messaging_pub"] or endpoints(model.get("endpoints")) != device["endpoints"]:
            fail()
        identity = (peer, row["service"]["package_id"])
        if identity in seen:
            fail()
        seen.add(identity)
        safe_models.append({**row, "peer_id": peer, "messaging_pub": device["messaging_pub"], "endpoints": device["endpoints"][:]})
    return {"version": "ryn.peer-directory.v1", "issuer": issuer, "resource": resource, "revision": revision(value.get("revision")), "members": safe_members, "models": safe_models}
