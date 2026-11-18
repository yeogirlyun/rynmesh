"""Shared bounded schema discovery. Visibility never grants execution authority."""
from __future__ import annotations

import copy
import hashlib
import math
import re

from .store import AgentError, AgentToolRejected, encoded
from .subagents import SUBAGENT_TOOL_NAMES

NAME = "ryn_discover_tools"
MAX_EXPOSED_TOOLS = 120
# Larger windows reserve more room for pinned/control schemas. Explicit loads
# append at every capacity; eviction still enforces the actual window bound.
ADDITIVE_CAPACITY = 48000
# Windows up to this many request bytes keep plan/input/result schemas behind
# discovery and send only the slimmest menu.
SMALL_WINDOW_BYTES = 16000
# Exposed on every step while they fit, in priority order when space is tight.
PLUGIN_DISCOVERY_TOOLS = ("ryn_list_plugins",)
CORE_TOOLS = ("ryn_shell_execute", "ryn_file_read", "ryn_file_search", "ryn_apply_patch", "ryn_file_edit",
              "ryn_file_write", "ryn_file_list", "ryn_process_read", "ryn_web_search", "ryn_download_file", "ryn_image_generate")
BROWSER_TOOLS = ("ryn_browser_open", "ryn_browser_read", "ryn_browser_action")
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")

SPEC = {"type": "function", "function": {"name": NAME,
    "description": "Search or load tool schemas. Pass query (BM25 keyword search) to find and load matching tools in one step, load to activate exact names (load [] clears), unload to remove, or cursor to page brief descriptions. Loading never grants permission.",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 240},
        "limit": {"type": "integer", "minimum": 1, "maximum": 8},
        "cursor": {"type": "integer", "minimum": 0},
        "load": {"type": "array", "maxItems": 8, "uniqueItems": True, "items": {"type": "string", "minLength": 1, "maxLength": 128}},
        "unload": {"type": "array", "maxItems": 8, "uniqueItems": True, "items": {"type": "string", "minLength": 1, "maxLength": 128}}},
        "additionalProperties": False}}}


def dedupe_specs(specs, reserved=()):
    """Keep the first definition, excluding host-reserved tool names."""
    seen, unique, dropped = set(reserved), [], []
    for spec in specs:
        try:
            function = spec["function"] if spec.get("type") == "function" else spec
            name = function["name"]
            if not isinstance(name, str):
                raise TypeError("invalid tool name")
        except (KeyError, TypeError, AttributeError):
            raise AgentError("agent_tool_definition_changed") from None
        if name in seen:
            dropped.append(name)
        else:
            seen.add(name)
            unique.append(spec)
    return unique, list(dict.fromkeys(dropped))


def _tokenize(text: str) -> list[str]:
    text = text or ""
    # Keep exact identifiers searchable while also indexing their word boundaries.
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    tokens = _TOKEN_RE.findall(text.lower())
    if split != text:
        tokens += _TOKEN_RE.findall(split.lower())
    cjk = [t for t in tokens if "\u4e00" <= t <= "\u9fff"]
    return tokens + [cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)]


def _bm25_rank(names_map: dict[str, dict], metadata, query: str, limit: int = 8) -> list[str]:
    q_tokens = _tokenize(query)
    if not q_tokens or not names_map:
        return []
    docs: dict[str, list[str]] = {}
    for name, spec in names_map.items():
        fn = spec.get("function") or {}
        desc = str(fn.get("description") or "")
        params = (fn.get("parameters") or {}).get("properties") or {}
        param_text = " ".join(
            f"{k} {(v.get('description') or '') if isinstance(v, dict) else ''}"
            for k, v in params.items()
        )
        try:
            info = metadata(name) or {}
            title = str(info.get("title") or "")
            title += " " + str((info.get("extension_binding") or {}).get("original_tool_name") or "")
        except Exception:
            title = ""
        docs[name] = _tokenize(f"{name} {name} {title} {param_text} {desc}")
    n_docs = len(docs)
    avgdl = max(1.0, sum(len(d) for d in docs.values()) / n_docs)
    df = {q: sum(1 for d in docs.values() if q in d) for q in set(q_tokens)}
    k1, b = 1.5, 0.75
    q_lower = query.strip().lower()
    scores: list[tuple[float, str]] = []
    for name, doc_tokens in docs.items():
        dl = len(doc_tokens)
        counts: dict[str, int] = {}
        for tok in doc_tokens:
            counts[tok] = counts.get(tok, 0) + 1
        score = 0.0
        for q in q_tokens:
            tf = counts.get(q, 0)
            if tf <= 0:
                continue
            idf = math.log(1.0 + (n_docs - df.get(q, 0) + 0.5) / (df.get(q, 0) + 0.5))
            score += idf * ((tf * (k1 + 1.0)) / (tf + k1 * (1.0 - b + b * (dl / avgdl))))
        if q_lower and q_lower in name.lower():
            score += 3.0
        if score > 0:
            scores.append((score, name))
    scores.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, name in scores[: max(1, min(limit, 8))]]


def capacity(max_context_bytes, authority_bytes):
    # The proportion is a mode-selection target, never an artificial per-tool
    # ceiling: every schema which fits the actual hard space remains reachable.
    return max_context_bytes - authority_bytes - 256


def catalog_revision(specs):
    return hashlib.sha256(encoded(specs)).hexdigest()


def plugin_preloads(specs, metadata, plugin_ids):
    """Bound relevant plugin schemas without granting execution permission."""
    candidates = []
    for spec in specs:
        name = spec["function"]["name"]
        info = metadata(name)
        if (info.get("extension_binding") or {}).get("plugin_id") in plugin_ids:
            candidates.append((info.get("effect") != "read", name, spec))
    selected, size = [], 0
    for _, name, spec in sorted(candidates, key=lambda item: (item[0], len(encoded(item[2])), item[1])):
        cost = len(encoded(spec))
        if len(selected) < 4 and size + cost <= 8192:
            selected.append(name)
            size += cost
    return tuple(selected)


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
    # Pinned core tools yield too, until the goal, context epoch or pinned set changes.
    return {**copy.deepcopy(state), "loaded_names": [], "pinned_names": [], "pins_released": True,
            "revision": state["revision"] + 1}


def record_usage(row, names, *, epoch):
    """Keep call recency outside schema state, including across step resets."""
    if row.get("context_epoch", 0) != epoch:
        return
    usage = row.setdefault("tool_menu_usage", {})
    step = max(row["steps"], max(usage.values(), default=-1) + 1)
    usage.update({name: step for name in names})


def _victim(loaded, requested, usage):
    candidates = [name for name in reversed(loaded) if name not in requested]
    unused = next((name for name in candidates if name not in usage), None)
    return unused if unused is not None else min(candidates, key=lambda name: usage[name], default=None)


def project(business_specs, internal_specs, state, *, max_context_bytes, authority_bytes, epoch, goal_revision,
            initial_names=(), pinned_names=()):
    """Return (schemas, durable state), without changing any caller-owned data.

    pinned_names are core tools exposed on every step in directory mode while
    they fit, so ordinary file/shell work never spends a model step on
    discovery. They are recomputed each projection and never count as loads.
    """
    internal_specs, _ = dedupe_specs(internal_specs, {NAME})
    business_specs, _ = dedupe_specs(business_specs, {NAME, *(s["function"]["name"] for s in internal_specs)})
    internal = [
        spec for spec in internal_specs
        if spec["function"]["name"] != NAME
        and (max_context_bytes >= ADDITIVE_CAPACITY or spec["function"]["name"] not in SUBAGENT_TOOL_NAMES)
    ]
    all_specs = [*business_specs, *internal]
    full_specs = (
        [
            s for s in all_specs
            if s["function"]["name"] not in {"ryn_automation_update", "ryn_memory_search", "ryn_memory_propose", *SUBAGENT_TOOL_NAMES}
        ]
        if max_context_bytes <= SMALL_WINDOW_BYTES
        else all_specs
    )
    limit = capacity(max_context_bytes, authority_bytes)
    prior = state or {}
    base = [*internal, directory_spec(business_specs)]
    target = min(8192, max_context_bytes // 2, max_context_bytes - authority_bytes - 2048)
    # For small registries a discovery schema would cost more than all tools.
    # Small windows keep control schemas lazy (see below), so their directory
    # costs only the discovery schema; comparing with every control schema
    # picked full mode there and spent kilobytes the history needed.
    small_lazy_base = [directory_spec(all_specs)] if internal and max_context_bytes <= SMALL_WINDOW_BYTES else base
    directory_useful = len(encoded(all_specs)) > len(encoded(small_lazy_base))
    mode = "directory" if (len(full_specs) > MAX_EXPOSED_TOOLS or len(encoded(full_specs)) > limit or
                           directory_useful and (len(encoded(full_specs)) > target or prior.get("mode") == "directory")) else "full"
    # In a small window, unused plan/input/result schemas can occupy more
    # space than all usable conversation. Keep them discoverable instead of
    # repeatedly summarizing or unloading necessary business schemas.
    lazy = bool(internal) and mode == "directory" and (max_context_bytes <= SMALL_WINDOW_BYTES or len(encoded(base)) > limit or len(base) > MAX_EXPOSED_TOOLS)
    catalog = [*business_specs, *internal] if lazy else business_specs
    revision = catalog_revision(catalog)
    binding = {"catalog_revision": revision, "context_epoch": epoch, "goal_revision": goal_revision, "lazy_controls": lazy}
    same_epoch = (
        prior.get("catalog_revision") == revision
        and prior.get("context_epoch") == epoch
        and prior.get("lazy_controls") == lazy
    )
    same = same_epoch and prior.get("goal_revision") == goal_revision
    # Medium/large windows keep discovered tool schemas across owner follow-ups
    # so the tools prefix at token 0 remains KV-cache stable across turns.
    keep_loaded = same or (same_epoch and max_context_bytes > SMALL_WINDOW_BYTES)
    if lazy:
        base = [directory_spec(catalog)]
    loaded = list(prior.get("loaded_names", [])) if keep_loaded and mode == "directory" else []
    by_name = {spec["function"]["name"]: spec for spec in catalog}
    loaded = [name for name in loaded if name in by_name]
    requested_pins = [name for name in dict.fromkeys(pinned_names) if name in by_name]
    if "pinned_candidates" in prior:
        prior_candidates = list(prior.get("pinned_candidates") or [])
    elif prior.get("pins_released"):
        prior_candidates = list(requested_pins)
    else:
        prior_candidates = list(prior.get("pinned_names") or [])
    pins_changed = bool(prior.get("mode") == "directory" and prior_candidates != requested_pins)
    has_browser_pins = any(
        name in BROWSER_TOOLS or name.startswith("mcp__computer_use__computer_")
        for name in requested_pins
    )
    has_resource_pins = "ryn_list_resource_roots" in requested_pins
    # ``candidate`` below includes ``base`` (control + directory schemas), so
    # additive pin/seed budgets reserve ``len(encoded(base))`` on top of the
    # business-tool schema allowance instead of a stale fixed estimate.
    base_bytes = len(encoded(base)) if max_context_bytes >= ADDITIVE_CAPACITY else 0
    resource_allowance = 3000 if has_resource_pins else 0
    pinned = []
    pin_floor = base_bytes + (24000 if has_browser_pins else 15000) + resource_allowance
    seed_cap = (pin_floor + 2000) if max_context_bytes >= ADDITIVE_CAPACITY else (17000 + resource_allowance)
    seed_limit = min(limit, max(target, min(seed_cap, max_context_bytes // 3)))
    if max_context_bytes >= ADDITIVE_CAPACITY:
        seed_limit = min(limit, max(seed_limit, pin_floor))
    # Pins use at most a tenth of the window in small contexts where they would
    # crowd out conversation; normal additive windows (>= ADDITIVE_CAPACITY)
    # reserve enough quota to keep PLUGIN_DISCOVERY_TOOLS + CORE_TOOLS (+ BROWSER_TOOLS when enabled) exposed.
    pin_limit = (
        min(seed_limit, max(pin_floor, max_context_bytes // 6))
        if max_context_bytes >= ADDITIVE_CAPACITY
        else min(seed_limit, max_context_bytes // 10)
    )
    released = bool(same and not pins_changed and prior.get("pins_released") and not prior.get("degraded"))
    if pins_changed:
        removed_candidates = set(prior_candidates) - set(requested_pins)
        loaded = [name for name in loaded if name not in removed_candidates]
    if mode == "directory" and not released:
        for name in requested_pins:
            if name not in pinned:
                candidate = [*base, *(by_name[item] for item in [*pinned, name])]
                if len(candidate) <= MAX_EXPOSED_TOOLS and len(encoded(candidate)) <= pin_limit:
                    pinned.append(name)
        loaded = [name for name in loaded if name not in pinned]
    if mode == "directory" and (not prior.get("mode") or not same or (pins_changed and not released)):
        for name in initial_names:
            if name in by_name and name not in loaded and name not in pinned:
                candidate = [*base, *(by_name[item] for item in [*pinned, *loaded, name])]
                if len(encoded(candidate)) <= seed_limit:
                    loaded.append(name)
        if lazy:
            # Lazy controls save the control schemas; a business registry that
            # fits whole stays directly callable, as it was in full mode.
            rest = [spec["function"]["name"] for spec in business_specs
                    if spec["function"]["name"] not in loaded and spec["function"]["name"] not in pinned]
            if len(encoded([*base, *(by_name[item] for item in [*pinned, *loaded, *rest])])) <= seed_limit:
                loaded.extend(rest)
    def exposure(names):
        if mode == "full":
            return full_specs
        return [*base, *(by_name[name] for name in [*pinned, *names])]
    while mode == "directory" and loaded and len(exposure(loaded)) > MAX_EXPOSED_TOOLS:
        loaded.pop(0)
    # Trim the tail to retain the longest stable schema prefix. Initial tools
    # requested for this projection are protected from ordinary cache eviction.
    while mode == "directory" and loaded and len(encoded(exposure(loaded))) > limit:
        victim = next((name for name in reversed(loaded) if name not in initial_names), None)
        if victim is None:
            break
        loaded.remove(victim)
    exposed = exposure(loaded)
    degraded = None
    if len(encoded(exposed)) > limit:
        # Controls must remain discoverable even when their schemas crowd out
        # the menu. Release pins too before asking for a larger model.
        catalog = all_specs
        binding.update(catalog_revision=catalog_revision(catalog), lazy_controls=True)
        exposed = [directory_spec(catalog)]
        loaded, pinned, released = [], [], True
        degraded = "directory_only"
        if len(encoded(exposed)) > limit:
            raise AgentToolRejected("agent_tool_schema_limit", correction="请换更大上下文的模型")
    elif mode == "directory" and lazy and not loaded and not pinned and len(encoded(full_specs)) > limit:
        degraded, released = "directory_only", True
    next_state = {**binding, "mode": mode, "loaded_names": loaded,
                  "revision": prior.get("revision", 0)}
    if pinned or prior.get("pinned_names"):
        next_state["pinned_names"] = pinned
    if requested_pins or prior.get("pinned_candidates"):
        next_state["pinned_candidates"] = requested_pins
    if released:
        next_state["pins_released"] = True
    if degraded:
        next_state.update(degraded=degraded, pinned_names=[])
    if any(prior.get(key) != value for key, value in next_state.items() if key != "revision"):
        next_state["revision"] += 1
    return copy.deepcopy(exposed), next_state


def discover(business_specs, internal_specs, metadata, state, args, *, capacity_bytes, usage=None, retain_bytes=None):
    """Paged catalog, BM25 search and append-only loads within the schema budget.

    ``retain_bytes`` bounds older loads only: past it they are released (unused,
    then least recently used) while this call's requested schemas stay.
    """
    usage = usage or {}
    internal_specs, _ = dedupe_specs(internal_specs, {NAME})
    business_specs, _ = dedupe_specs(business_specs, {NAME, *(s["function"]["name"] for s in internal_specs)})
    all_internal = [spec for spec in internal_specs if spec["function"]["name"] != NAME]
    no_subagent_internal = [spec for spec in all_internal if spec["function"]["name"] not in SUBAGENT_TOOL_NAMES]
    if state.get("lazy_controls"):
        internal = (
            all_internal
            if state.get("catalog_revision") == catalog_revision([*business_specs, *all_internal])
            else no_subagent_internal
        )
    else:
        internal = all_internal if capacity_bytes >= ADDITIVE_CAPACITY // 2 else no_subagent_internal
    catalog = [*business_specs, *internal] if state.get("lazy_controls") else business_specs
    if state.get("mode") != "directory" or state.get("catalog_revision") != catalog_revision(catalog):
        raise AgentToolRejected("agent_tool_definition_changed")
    names = {spec["function"]["name"]: spec for spec in catalog}
    next_state = copy.deepcopy(state)
    pinned = [name for name in state.get("pinned_names", []) if name in names]

    def size(selection):
        return len(encoded([*([] if state.get("lazy_controls") else internal), directory_spec(catalog),
                            *(names[name] for name in [*pinned, *selection])]))

    def retain(loaded, protected):
        while retain_bytes is not None and size(loaded) > retain_bytes:
            victim = _victim(loaded, protected, usage)
            if victim is None:
                break
            loaded.remove(victim)
        return loaded

    query_text = args.get("query")
    has_query = isinstance(query_text, str) and bool(query_text.strip())
    matched_names: list[str] = []
    if has_query:
        matched_names = _bm25_rank(names, metadata, query_text, limit=int(args.get("limit") or 8))

    if "load" in args or "unload" in args:
        requested, removed = args.get("load", []), args.get("unload", [])
        def resolve(name):
            if name in names:
                return name
            matches = [registered for registered in names
                       if (metadata(registered).get("extension_binding") or {}).get("original_tool_name") == name]
            if len(matches) != 1:
                raise AgentToolRejected("agent_unknown_tool", correction="Discover the exact registered tool name; canonical names must identify one active connection.")
            return matches[0]
        requested, removed = [resolve(name) for name in requested], [resolve(name) for name in removed]
        if any(name not in names for name in [*requested, *removed]):
            raise AgentToolRejected("agent_unknown_tool")
        if "load" in args and not requested:
            loaded = []  # load [] clears every loaded (not pinned) schema.
        else:
            loaded = [name for name in next_state["loaded_names"] if name not in removed and name not in pinned]
            for name in requested:
                if name not in pinned and name not in loaded:
                    loaded.append(name)
        # Adding never silently drops what this call asked for: older loads
        # are released first, and an unloadable request is still refused.
        while size(loaded) > capacity_bytes:
            victim = _victim(loaded, requested, usage)
            if victim is None:
                raise AgentToolRejected("agent_tool_schema_limit")
            loaded.remove(victim)
        loaded = retain(loaded, requested)
        if next_state["loaded_names"] != loaded:
            next_state["loaded_names"] = loaded
            next_state["revision"] += 1
    elif has_query and matched_names:
        # Codex-aligned 1-step tool_search: automatically activate matched tools that fit capacity.
        loaded = [name for name in next_state["loaded_names"] if name not in pinned]
        for name in matched_names:
            if name in pinned or name in loaded:
                continue
            candidate_loaded = [*loaded, name]
            while size(candidate_loaded) > capacity_bytes:
                victim = _victim(candidate_loaded, matched_names, usage)
                if victim is None:
                    break
                candidate_loaded.remove(victim)
            if size(candidate_loaded) <= capacity_bytes:
                loaded = candidate_loaded
        loaded = retain(loaded, matched_names)
        if next_state["loaded_names"] != loaded:
            next_state["loaded_names"] = loaded
            next_state["revision"] += 1

    cursor = args.get("cursor", 0)
    ordered = sorted(names)
    if cursor > len(ordered):
        raise AgentToolRejected("agent_invalid_tool_cursor")
    browse = (not ("load" in args or "unload" in args) and not has_query) or "cursor" in args
    result = {"tools": [], "cursor": cursor, "next_cursor": None,
              "total": len(ordered), "loaded_names": next_state["loaded_names"],
              "catalog_revision": next_state["catalog_revision"], "menu_revision": next_state["revision"],
              "permission_granted": False}
    if pinned:
        result["always_available"] = list(pinned)
    if has_query:
        internal_subagent_map = {s["function"]["name"]: s for s in internal if s["function"]["name"] in SUBAGENT_TOOL_NAMES}
        matched_internal = _bm25_rank(internal_subagent_map, metadata, query_text, limit=6) if internal_subagent_map else []
        if matched_internal:
            avail = list(result.get("always_available") or [])
            for m_name in matched_internal:
                if m_name not in avail:
                    avail.append(m_name)
            result["always_available"] = avail
        result["query"] = query_text.strip()
        result["matches"] = matched_names or matched_internal
        if not result["matches"]:
            result["hint"] = (
                "If the owner named a plugin that is not installed, tell them to install it from the Applications page "
                "instead of substituting another tool."
            )
    if has_query and not browse:
        page_limit = max(256, capacity_bytes // 12)
        for name in matched_names:
            item = {"name": name, "description": names[name]["function"].get("description", "")[:80],
                    "effect": metadata(name)["effect"], "schema_bytes": len(encoded(names[name]))}
            with_params = {**item, "parameters": names[name]["function"].get("parameters", {})}
            candidate_full = {**result, "tools": [*result["tools"], with_params]}
            if len(encoded(candidate_full)) <= page_limit:
                result["tools"].append(with_params)
                continue
            candidate = {**result, "tools": [*result["tools"], item]}
            if len(encoded(candidate)) <= page_limit:
                result["tools"].append(item)
            elif not result["tools"]:
                item["description"] = ""
                result["tools"].append(item)
                break
            else:
                break
        return result, next_state
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
