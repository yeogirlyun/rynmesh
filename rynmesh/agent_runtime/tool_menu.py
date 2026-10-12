"""Shared bounded schema discovery. Visibility never grants execution authority."""
from __future__ import annotations

import copy
import hashlib

from .store import AgentError, AgentToolRejected, encoded

NAME = "ryn_discover_tools"
SPEC = {"type": "function", "function": {"name": NAME,
    "description": "Load exact business-tool schemas by name; load replaces exposed tools and [] clears them. Available names appear in load.items.enum when they fit. Optional cursor browses bounded brief descriptions; next_cursor continues. Full descriptions/parameters are returned as loaded schemas. Cached schemas may be released for space; reload absent tools. Loading never grants permission.",
    "parameters": {"type": "object", "properties": {
        "cursor": {"type": "integer", "minimum": 0},
        "load": {"type": "array", "maxItems": 8, "uniqueItems": True, "items": {"type": "string", "minLength": 1, "maxLength": 128}}},
        "additionalProperties": False}}}


def capacity(max_context_bytes, authority_bytes):
    # The proportion is a mode-selection target, never an artificial per-tool
    # ceiling: every schema which fits the actual hard space remains reachable.
    return max_context_bytes - authority_bytes - 256


def catalog_revision(specs):
    return hashlib.sha256(encoded(specs)).hexdigest()


def directory_spec(business_specs):
    """A persistent name index belongs in the schema, not repeated page bodies."""
    spec = copy.deepcopy(SPEC)
    names = sorted(item["function"]["name"] for item in business_specs)
    if len(encoded(names)) <= 1500:
        spec["function"]["parameters"]["properties"]["load"]["items"]["enum"] = names
    return spec


def release(state):
    """Drop only the reloadable schema cache, never permission or tool data."""
    if state.get("mode") != "directory" or not state.get("loaded_names"):
        return copy.deepcopy(state)
    return {**copy.deepcopy(state), "loaded_names": [], "revision": state["revision"] + 1}


def project(business_specs, internal_specs, state, *, max_context_bytes, authority_bytes, epoch, goal_revision):
    """Return (schemas, durable state), without changing any caller-owned data."""
    internal = [spec for spec in internal_specs if spec["function"]["name"] != NAME]
    all_specs = [*business_specs, *internal]
    limit = capacity(max_context_bytes, authority_bytes)
    revision = catalog_revision(business_specs)
    prior = state or {}
    binding = {"catalog_revision": revision, "context_epoch": epoch, "goal_revision": goal_revision}
    same = all(prior.get(key) == value for key, value in binding.items())
    base = [*internal, directory_spec(business_specs)]
    target = min(max_context_bytes // 2, max_context_bytes - authority_bytes - 2048)
    # For small registries a discovery schema would cost more than all tools.
    directory_useful = len(encoded(all_specs)) > len(encoded(base))
    mode = "directory" if directory_useful and (len(encoded(all_specs)) > target or prior.get("mode") == "directory") else "full"
    loaded = list(prior.get("loaded_names", [])) if same and mode == "directory" else []
    by_name = {spec["function"]["name"]: spec for spec in business_specs}
    if len(by_name) != len(business_specs) or NAME in by_name:
        raise AgentError("agent_tool_definition_changed")
    if any(name not in by_name for name in loaded):
        raise AgentError("agent_tool_definition_changed")
    exposed = all_specs if mode == "full" else [*base, *(by_name[name] for name in loaded)]
    if len(encoded(exposed)) > limit:
        # Never spend a summary request trying to shrink an immutable schema.
        raise AgentError("agent_tool_schema_limit")
    next_state = {**binding, "mode": mode, "loaded_names": loaded,
                  "revision": prior.get("revision", 0)}
    if any(prior.get(key) != value for key, value in next_state.items() if key != "revision"):
        next_state["revision"] += 1
    return copy.deepcopy(exposed), next_state


def discover(business_specs, internal_specs, metadata, state, args, *, capacity_bytes):
    """Paged catalog and exact replace, charged as an ordinary internal tool."""
    if state.get("mode") != "directory" or state.get("catalog_revision") != catalog_revision(business_specs):
        raise AgentToolRejected("agent_tool_definition_changed")
    names = {spec["function"]["name"]: spec for spec in business_specs}
    next_state = copy.deepcopy(state)
    if "load" in args:
        loaded = args["load"]
        if any(name not in names for name in loaded):
            raise AgentToolRejected("agent_unknown_tool")
        internal = [spec for spec in internal_specs if spec["function"]["name"] != NAME]
        exposed = [*internal, directory_spec(business_specs), *(names[name] for name in loaded)]
        if len(encoded(exposed)) > capacity_bytes:
            raise AgentToolRejected("agent_tool_schema_limit")
        if next_state["loaded_names"] != loaded:
            next_state["loaded_names"] = list(loaded)
            next_state["revision"] += 1
    cursor = args.get("cursor", 0)
    ordered = sorted(names)
    if cursor > len(ordered):
        raise AgentToolRejected("agent_invalid_tool_cursor")
    browse = "load" not in args or "cursor" in args
    result = {"tools": [], "cursor": cursor, "next_cursor": None,
              "total": len(ordered), "loaded_names": next_state["loaded_names"],
              "catalog_revision": next_state["catalog_revision"], "menu_revision": next_state["revision"],
              "permission_granted": False}
    # Up to eight tool calls share one indivisible model exchange. Reserve a
    # third of the available space for their envelopes/checkpoint and keep each
    # catalog result within the remainder; never repeat the full name index.
    if browse:
        page_limit = max(256, capacity_bytes // 12)
        for name in ordered[cursor:cursor + 8]:
            item = {"name": name, "description": names[name]["function"].get("description", "")[:80],
                    "effect": metadata(name)["effect"], "schema_bytes": len(encoded(names[name]))}
            candidate = {**result, "tools": [*result["tools"], item], "next_cursor": cursor + len(result["tools"]) + 1}
            if len(encoded(candidate)) > page_limit:
                if result["tools"]:
                    break
                item["description"] = ""
                if len(encoded(candidate)) > page_limit:
                    raise AgentToolRejected("agent_tool_schema_limit")
            result["tools"].append(item)
        following = cursor + len(result["tools"])
        result["next_cursor"] = following if following < len(ordered) else None
    return result, next_state
