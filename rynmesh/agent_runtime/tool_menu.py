"""Shared bounded schema discovery. Visibility never grants execution authority."""
from __future__ import annotations

import copy
import hashlib

from .store import AgentError, AgentToolRejected, encoded

NAME = "ryn_discover_tools"
# Loads accumulate only with at least this much schema space; smaller windows
# replace the loaded set, because every kept schema crowds out conversation.
ADDITIVE_CAPACITY = 48000
# Exposed on every step while they fit, in priority order when space is tight.
CORE_TOOLS = ("ryn_shell_execute", "ryn_file_read", "ryn_file_search", "ryn_apply_patch", "ryn_file_edit",
              "ryn_file_write", "ryn_file_list", "ryn_process_read")
SPEC = {"type": "function", "function": {"name": NAME,
    "description": "Load exact business-tool schemas by name. load adds tools to those already exposed (small context windows replace them instead); unload removes them; load [] clears loaded tools. Core workstation tools that are already exposed stay available and need no loading. Available names appear in load.items.enum when they fit. Optional cursor browses bounded brief descriptions; next_cursor continues. The least recently loaded schemas may be released for space; reload absent tools. Loading never grants permission.",
    "parameters": {"type": "object", "properties": {
        "cursor": {"type": "integer", "minimum": 0},
        "load": {"type": "array", "maxItems": 8, "uniqueItems": True, "items": {"type": "string", "minLength": 1, "maxLength": 128}},
        "unload": {"type": "array", "maxItems": 8, "uniqueItems": True, "items": {"type": "string", "minLength": 1, "maxLength": 128}}},
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
    if state.get("mode") != "directory" or not (state.get("loaded_names") or state.get("pinned_names")):
        return copy.deepcopy(state)
    # Pinned core tools yield too, until the goal or context epoch changes.
    return {**copy.deepcopy(state), "loaded_names": [], "pinned_names": [], "pins_released": True,
            "revision": state["revision"] + 1}


def project(business_specs, internal_specs, state, *, max_context_bytes, authority_bytes, epoch, goal_revision,
            initial_names=(), pinned_names=()):
    """Return (schemas, durable state), without changing any caller-owned data.

    pinned_names are core tools exposed on every step in directory mode while
    they fit, so ordinary file/shell work never spends a model step on
    discovery. They are recomputed each projection and never count as loads.
    """
    internal = [spec for spec in internal_specs if spec["function"]["name"] != NAME]
    all_specs = [*business_specs, *internal]
    limit = capacity(max_context_bytes, authority_bytes)
    prior = state or {}
    base = [*internal, directory_spec(business_specs)]
    target = min(8192, max_context_bytes // 2, max_context_bytes - authority_bytes - 2048)
    # For small registries a discovery schema would cost more than all tools.
    directory_useful = len(encoded(all_specs)) > len(encoded(base))
    mode = "directory" if (len(encoded(all_specs)) > limit or
                           directory_useful and (len(encoded(all_specs)) > target or prior.get("mode") == "directory")) else "full"
    # In a small window, unused plan/input/result schemas can occupy more
    # space than all usable conversation. Keep them discoverable instead of
    # repeatedly summarizing or unloading necessary business schemas.
    lazy = bool(internal) and mode == "directory" and (max_context_bytes <= 16000 or len(encoded(base)) > limit)
    catalog = [*business_specs, *internal] if lazy else business_specs
    revision = catalog_revision(catalog)
    binding = {"catalog_revision": revision, "context_epoch": epoch, "goal_revision": goal_revision, "lazy_controls": lazy}
    same = all(prior.get(key) == value for key, value in binding.items())
    if lazy:
        base = [directory_spec(catalog)]
    loaded = list(prior.get("loaded_names", [])) if same and mode == "directory" else []
    by_name = {spec["function"]["name"]: spec for spec in catalog}
    if len(by_name) != len(catalog) or NAME in by_name:
        raise AgentError("agent_tool_definition_changed")
    if any(name not in by_name for name in loaded):
        raise AgentError("agent_tool_definition_changed")
    pinned = []
    seed_limit = min(limit, max(target, min(16000, max_context_bytes // 3)))
    # Pins use at most a tenth of the window: in small windows they would
    # crowd out the conversation and force extra compaction calls.
    pin_limit = min(seed_limit, max_context_bytes // 10)
    released = bool(same and prior.get("pins_released"))
    if mode == "directory" and not released:
        for name in pinned_names:
            if name in by_name and name not in pinned:
                candidate = [*base, *(by_name[item] for item in [*pinned, name])]
                if len(encoded(candidate)) <= pin_limit:
                    pinned.append(name)
        loaded = [name for name in loaded if name not in pinned]
    if mode == "directory" and (not prior.get("mode") or not same):
        for name in initial_names:
            if name in by_name and name not in loaded and name not in pinned:
                candidate = [*base, *(by_name[item] for item in [*pinned, *loaded, name])]
                if len(encoded(candidate)) <= seed_limit:
                    loaded.append(name)
    def exposure(names):
        return all_specs if mode == "full" else [*base, *(by_name[name] for name in [*pinned, *names])]
    # Loaded schemas are a reloadable cache: release the least recently loaded
    # before refusing, as pinned core tools now share the same space.
    while mode == "directory" and loaded and len(encoded(exposure(loaded))) > limit:
        loaded = loaded[1:]
    exposed = exposure(loaded)
    if len(encoded(exposed)) > limit:
        # Never spend a summary request trying to shrink an immutable schema.
        raise AgentError("agent_tool_schema_limit")
    next_state = {**binding, "mode": mode, "loaded_names": loaded,
                  "revision": prior.get("revision", 0)}
    if pinned or prior.get("pinned_names"):
        next_state["pinned_names"] = pinned
    if released:
        next_state["pins_released"] = True
    if any(prior.get(key) != value for key, value in next_state.items() if key != "revision"):
        next_state["revision"] += 1
    return copy.deepcopy(exposed), next_state


def discover(business_specs, internal_specs, metadata, state, args, *, capacity_bytes):
    """Paged catalog and exact replace, charged as an ordinary internal tool."""
    internal = [spec for spec in internal_specs if spec["function"]["name"] != NAME]
    catalog = [*business_specs, *internal] if state.get("lazy_controls") else business_specs
    if state.get("mode") != "directory" or state.get("catalog_revision") != catalog_revision(catalog):
        raise AgentToolRejected("agent_tool_definition_changed")
    names = {spec["function"]["name"]: spec for spec in catalog}
    next_state = copy.deepcopy(state)
    pinned = [name for name in state.get("pinned_names", []) if name in names]
    if "load" in args or "unload" in args:
        requested, removed = args.get("load", []), args.get("unload", [])
        if any(name not in names for name in [*requested, *removed]):
            raise AgentToolRejected("agent_unknown_tool")
        if "load" in args and not requested:
            loaded = []  # load [] clears every loaded (not pinned) schema.
        elif "load" in args and capacity_bytes < ADDITIVE_CAPACITY:
            loaded = [name for name in requested if name not in pinned]
        else:
            loaded = [name for name in next_state["loaded_names"] if name not in removed and name not in pinned]
            for name in requested:
                if name not in pinned and name not in loaded:
                    loaded.append(name)

        def size(selection):
            return len(encoded([*([] if state.get("lazy_controls") else internal), directory_spec(catalog),
                                *(names[name] for name in [*pinned, *selection])]))
        # Adding never silently drops what this call asked for: older loads
        # are released first, and an unloadable request is still refused.
        while size(loaded) > capacity_bytes:
            victim = next((name for name in loaded if name not in requested), None)
            if victim is None:
                raise AgentToolRejected("agent_tool_schema_limit")
            loaded.remove(victim)
        if next_state["loaded_names"] != loaded:
            next_state["loaded_names"] = loaded
            next_state["revision"] += 1
    cursor = args.get("cursor", 0)
    ordered = sorted(names)
    if cursor > len(ordered):
        raise AgentToolRejected("agent_invalid_tool_cursor")
    browse = not ("load" in args or "unload" in args) or "cursor" in args
    result = {"tools": [], "cursor": cursor, "next_cursor": None,
              "total": len(ordered), "loaded_names": next_state["loaded_names"],
              "catalog_revision": next_state["catalog_revision"], "menu_revision": next_state["revision"],
              "permission_granted": False}
    if pinned:
        result["always_available"] = pinned
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
