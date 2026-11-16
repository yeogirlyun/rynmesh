"""Pure, fail-closed data destination rules and monotonic task snapshots."""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from .store import AgentError

MAX_POLICY_SOURCES = 64
_LOOPBACK_IPV4 = ipaddress.IPv4Network("127.0.0.0/8")
_LOOPBACK_IPV6 = ipaddress.IPv6Address("::1")


def _invalid():
    raise AgentError("agent_invalid_data_policy")


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv4Address):
        return addr in _LOOPBACK_IPV4
    return addr == _LOOPBACK_IPV6


def _url(value: Any, *, prefix: bool = False, allow_loopback_http: bool = False):
    if not isinstance(value, str) or not value or len(value) > 8192:
        _invalid()
    if any(ord(ch) <= 32 or ord(ch) == 127 for ch in value) or "\\" in value or "#" in value:
        _invalid()
    try:
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            _invalid()
        if "@" in parsed.netloc or parsed.query or parsed.port == 0 or "%" in parsed.hostname:
            _invalid()
        try:
            host = str(ipaddress.ip_address(parsed.hostname))
        except ValueError:
            host = parsed.hostname.encode("idna").decode("ascii").lower()
        if parsed.scheme == "https":
            port = parsed.port or 443
        elif parsed.scheme == "http" and allow_loopback_http:
            if parsed.port is None or not _is_loopback_host(host):
                _invalid()
            port = parsed.port
        else:
            _invalid()
        path = parsed.path or "/"
        if re.search(r"%(?![0-9a-fA-F]{2})", path) or re.search(r"%(?:2f|5c|2e|25)", path, re.I):
            _invalid()
        path = unquote(path, errors="strict")
        if any(ord(ch) <= 32 or ord(ch) == 127 for ch in path) or any(part in {".", ".."} for part in path.split("/")):
            _invalid()
        if prefix and (not parsed.path.endswith("/") or not value.endswith("/")):
            _invalid()
        return (parsed.scheme, host, port), path
    except (ValueError, UnicodeError):
        _invalid()


def normalize_policy(value: Any) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"models", "memory"}:
        _invalid()
    models = value.get("models", {"mode": "any"})
    if not isinstance(models, dict) or set(models) - {"mode", "allow"}:
        _invalid()
    mode = models.get("mode", "any")
    memory = value.get("memory", "allow")
    if not isinstance(mode, str) or mode not in {"any", "allowlist"} or not isinstance(memory, str) or memory not in {"allow", "deny"}:
        _invalid()
    result = {"models": {"mode": mode}, "memory": value.get("memory", "allow")}
    if mode == "any":
        if "allow" in models:
            _invalid()
        return result
    rules = models.get("allow")
    if not isinstance(rules, list) or len(rules) > 256:
        _invalid()
    normalized = []
    for rule in rules:
        if not isinstance(rule, dict) or len(rule) != 1:
            _invalid()
        if "on_device" in rule:
            if rule["on_device"] is not True:
                _invalid()
        elif "api_base_url_prefix" in rule:
            _url(rule["api_base_url_prefix"], prefix=True, allow_loopback_http=True)
        elif "catalog_model_id" in rule:
            identifier = rule["catalog_model_id"]
            if not isinstance(identifier, str) or not identifier or len(identifier) > 1024 or any(ord(ch) < 32 or ord(ch) == 127 for ch in identifier):
                _invalid()
        else:
            _invalid()
        if rule not in normalized:
            normalized.append(copy.deepcopy(rule))
    result["models"]["allow"] = sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
    return result


def snapshot_source(server: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(server, dict):
        _invalid()
    identifier, instance = server.get("id", server.get("server_id")), server.get("instance_id")
    if not isinstance(identifier, str) or not identifier or not isinstance(instance, str) or not instance:
        _invalid()
    policy = normalize_policy(server.get("data_policy", server.get("policy")))
    fingerprint = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return {"server_id": identifier, "instance_id": instance, "policy_sha256": fingerprint, "policy": policy}


def allows(policy: Any, context: Any = None) -> bool:
    try:
        policy = normalize_policy(policy)
        if policy["models"]["mode"] == "any":
            return True
        if not isinstance(context, dict) or context.get("model_destination_verified") is not True:
            return False
        model, destination = context.get("model"), context.get("model_destination")
        if not isinstance(model, dict) or not isinstance(destination, dict):
            return False
        if not model.get("id") or not model.get("destination_revision"):
            return False
        expected = {"catalog_model_id": model["id"], "provider_peer_id": model.get("peer_id", ""),
                    "source_id": model.get("source_id", ""), "destination_revision": model["destination_revision"]}
        if any(destination.get(key) != value for key, value in expected.items()):
            return False
        for rule in policy["models"]["allow"]:
            if rule.get("catalog_model_id") == model["id"]:
                return True
            if rule.get("on_device") is True and context.get("consumer_node_id") and model.get("peer_id") == context["consumer_node_id"]:
                if model.get("source") == "local_service" and model.get("runtime") == "native_llama_cpp":
                    return True
                if model.get("source") == "local_api" and model.get("source_id"):
                    api_src = context.get("api_source")
                    if isinstance(api_src, dict) and api_src.get("destination_revision") == destination["destination_revision"]:
                        try:
                            src_origin, _ = _url(api_src.get("base_url"), allow_loopback_http=True)
                        except AgentError:
                            src_origin = None
                        if src_origin is not None and _is_loopback_host(src_origin[1]):
                            return True
            if "api_base_url_prefix" in rule and model.get("source") == "local_api" and model.get("source_id"):
                source = context.get("api_source")
                if not isinstance(source, dict) or source.get("destination_revision") != destination["destination_revision"]:
                    continue
                try:
                    origin, path = _url(source.get("base_url"), allow_loopback_http=True)
                    allowed_origin, allowed_path = _url(rule["api_base_url_prefix"], prefix=True, allow_loopback_http=True)
                except AgentError:
                    continue
                if origin == allowed_origin and (path.startswith(allowed_path) or path == allowed_path.rstrip("/")):
                    return True
        return False
    except (AgentError, TypeError, ValueError, KeyError):
        return False


def _state(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and "data_policy_state" in value:
        value = value["data_policy_state"]
    if value is None or value == {}:
        return {"version": 1, "revision": 0, "sources": [], "memory_deny": False}
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("sources"), list):
        _invalid()
    if type(value.get("memory_deny", False)) is not bool or type(value.get("revision", 0)) is not int:
        _invalid()
    sources = []
    for item in value["sources"]:
        if not isinstance(item, dict) or "policy" not in item or item["policy"] is None:
            _invalid()
        snap = snapshot_source(item)
        if snap not in sources:
            sources.append(snap)
    return {"version": 1, "revision": max(0, value.get("revision", 0)), "sources": sources,
            "memory_deny": value.get("memory_deny", False) or any(s["policy"]["memory"] == "deny" for s in sources)}


def _intersect_rule(r1: dict[str, Any], r2: dict[str, Any]) -> dict[str, Any] | None:
    if r1 == r2:
        return copy.deepcopy(r1)
    if "on_device" in r1 and "api_base_url_prefix" in r2:
        origin, _ = _url(r2["api_base_url_prefix"], prefix=True, allow_loopback_http=True)
        return copy.deepcopy(r2) if _is_loopback_host(origin[1]) else None
    if "api_base_url_prefix" in r1 and "on_device" in r2:
        origin, _ = _url(r1["api_base_url_prefix"], prefix=True, allow_loopback_http=True)
        return copy.deepcopy(r1) if _is_loopback_host(origin[1]) else None
    if "api_base_url_prefix" in r1 and "api_base_url_prefix" in r2:
        o1, p1 = _url(r1["api_base_url_prefix"], prefix=True, allow_loopback_http=True)
        o2, p2 = _url(r2["api_base_url_prefix"], prefix=True, allow_loopback_http=True)
        if o1 != o2:
            return None
        if p2.startswith(p1):
            return copy.deepcopy(r2)
        if p1.startswith(p2):
            return copy.deepcopy(r1)
    return None


def _intersect_policies(p1: Any, p2: Any) -> dict[str, Any]:
    left = normalize_policy(p1)
    right = normalize_policy(p2)
    memory = "deny" if (left["memory"] == "deny" or right["memory"] == "deny") else "allow"
    m1, m2 = left["models"], right["models"]
    if m1["mode"] == "any":
        models = copy.deepcopy(m2)
    elif m2["mode"] == "any":
        models = copy.deepcopy(m1)
    else:
        combined: list[dict[str, Any]] = []
        for r1 in m1["allow"]:
            for r2 in m2["allow"]:
                inter = _intersect_rule(r1, r2)
                if inter is not None and inter not in combined:
                    combined.append(inter)
        models = {"mode": "allowlist", "allow": combined}
    return normalize_policy({"models": models, "memory": memory})


def merge_state(state: Any, snapshot: Any) -> dict[str, Any]:
    """Merge a source snapshot or inherited state without ever removing restrictions."""
    result = _state(state)
    incoming = _state(snapshot) if isinstance(snapshot, dict) and ("sources" in snapshot or "data_policy_state" in snapshot) else _state(None)
    if snapshot is not None and not (isinstance(snapshot, dict) and ("sources" in snapshot or "data_policy_state" in snapshot)):
        incoming["sources"] = [snapshot_source(snapshot)]
    changed = incoming["memory_deny"] and not result["memory_deny"]

    merged_sources: list[dict[str, Any]] = []
    index_by_server: dict[str, int] = {}
    for item in result["sources"]:
        sid = item["server_id"]
        if sid not in index_by_server:
            index_by_server[sid] = len(merged_sources)
            merged_sources.append(item)
        else:
            idx = index_by_server[sid]
            existing = merged_sources[idx]
            strict_pol = _intersect_policies(existing["policy"], item["policy"])
            strict_snap = snapshot_source({"id": sid, "instance_id": item["instance_id"], "data_policy": strict_pol})
            if strict_snap["policy_sha256"] != existing["policy_sha256"]:
                merged_sources[idx] = strict_snap
            changed = True

    for item in incoming["sources"]:
        sid = item["server_id"]
        if sid not in index_by_server:
            index_by_server[sid] = len(merged_sources)
            merged_sources.append(item)
            changed = True
        else:
            idx = index_by_server[sid]
            existing = merged_sources[idx]
            strict_pol = _intersect_policies(existing["policy"], item["policy"])
            strict_snap = snapshot_source({"id": sid, "instance_id": item["instance_id"], "data_policy": strict_pol})
            if strict_snap["policy_sha256"] != existing["policy_sha256"]:
                merged_sources[idx] = strict_snap
                changed = True

    if len(merged_sources) > MAX_POLICY_SOURCES:
        overflow = len(merged_sources) - MAX_POLICY_SOURCES
        oldest = merged_sources[:overflow + 1]
        folded_pol = oldest[0]["policy"]
        for old_item in oldest[1:]:
            folded_pol = _intersect_policies(folded_pol, old_item["policy"])
        folded_snap = snapshot_source({"id": "__compacted__", "instance_id": "compacted", "data_policy": folded_pol})
        merged_sources = [folded_snap, *merged_sources[overflow + 1:]]
        changed = True

    result["sources"] = merged_sources
    result["memory_deny"] = result["memory_deny"] or incoming["memory_deny"] or any(item["policy"]["memory"] == "deny" for item in result["sources"])
    if changed:
        result["revision"] += 1
    return result


def allows_task(state: Any, context: Any = None) -> bool:
    try:
        return all(allows(item["policy"], context) for item in _state(state)["sources"])
    except (AgentError, TypeError, ValueError):
        return False


def task_requires_model_restriction(state: Any) -> bool:
    try:
        return any(item["policy"]["models"]["mode"] == "allowlist" for item in _state(state)["sources"])
    except (AgentError, TypeError, ValueError):
        return True


def memory_denied(row_or_state: Any) -> bool:
    if row_or_state is None:
        return False
    if isinstance(row_or_state, dict) and "data_policy_state" not in row_or_state and "sources" not in row_or_state and "version" not in row_or_state:
        return False
    try:
        return _state(row_or_state)["memory_deny"]
    except (AgentError, TypeError, ValueError):
        return True


def memory_blocked(row_or_state: Any) -> bool:
    """Until memory carries destination rules, restricted tasks cannot feed it."""
    state = row_or_state
    if isinstance(state, dict) and "sources" not in state and "version" not in state:
        state = state.get("data_policy_state")
    return memory_denied(row_or_state) or task_requires_model_restriction(state)
