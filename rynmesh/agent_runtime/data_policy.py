"""Pure, fail-closed data destination rules and monotonic task snapshots."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from .store import AgentError


def _invalid():
    raise AgentError("agent_invalid_data_policy")


def _url(value: Any, *, prefix: bool = False):
    if not isinstance(value, str) or not value or len(value) > 8192:
        _invalid()
    if any(ord(ch) <= 32 or ord(ch) == 127 for ch in value) or "\\" in value or "#" in value:
        _invalid()
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            _invalid()
        if "@" in parsed.netloc or parsed.query or parsed.port == 0:
            _invalid()
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port or 443
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
            _url(rule["api_base_url_prefix"], prefix=True)
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
            if rule.get("on_device") is True and model.get("source") == "local_service" and model.get("runtime") == "native_llama_cpp" and context.get("consumer_node_id") and model.get("peer_id") == context["consumer_node_id"]:
                return True
            if "api_base_url_prefix" in rule and model.get("source") == "local_api" and model.get("source_id"):
                source = context.get("api_source")
                if not isinstance(source, dict) or source.get("destination_revision") != destination["destination_revision"]:
                    continue
                try:
                    origin, path = _url(source.get("base_url"))
                    allowed_origin, allowed_path = _url(rule["api_base_url_prefix"], prefix=True)
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


def merge_state(state: Any, snapshot: Any) -> dict[str, Any]:
    """Merge a source snapshot or inherited state without ever removing restrictions."""
    result = _state(state)
    incoming = _state(snapshot) if isinstance(snapshot, dict) and ("sources" in snapshot or "data_policy_state" in snapshot) else _state(None)
    if snapshot is not None and not (isinstance(snapshot, dict) and ("sources" in snapshot or "data_policy_state" in snapshot)):
        incoming["sources"] = [snapshot_source(snapshot)]
    changed = incoming["memory_deny"] and not result["memory_deny"]
    seen = {(item["instance_id"], item["policy_sha256"]) for item in result["sources"]}
    for item in incoming["sources"]:
        key = item["instance_id"], item["policy_sha256"]
        if key not in seen:
            result["sources"].append(item)
            seen.add(key)
            changed = True
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
