"""Per-model route notes and request facts for Agent main-loop requests.

Every model shares one core instruction set (``prompt.SYSTEM``), pinned in the
task for continuity. This module adds only facts that differ by the current
model's route and catalog metadata. It is resolved again for every request, so
a model switch, a settings change or a restart never keeps an old adaptation.

Selection is deterministic, highest first:

1. Explicit owner override: Agent setting ``model_profile_overrides``, keyed by
   the exact catalog model id. Used when the profile exists and fits the route;
   otherwise the reason is recorded and selection continues.
2. Route profile: chosen from the catalog ``adapter`` field (an exact enum set
   by the source, CLI discovery or provider manifest), never from a model name.
3. ``generic``: the shared core only.

Capability notes come from catalog image fields that the runtime itself
enforces at dispatch, so they apply under every profile.

Only differences with evidence get a profile or a note:

- ``codex-cli``: Ryn's request reaches a Codex app-server thread
  (``llm_package/codex_session.py``, ``codex_tools.py``). Codex places its own
  model instructions first (openai/codex rust-v0.165.0,
  ``codex-rs/core/src/client.rs`` ``build_responses_request``), and those
  describe Codex host tools. Ryn rejects such names (``codex_tools.FEEDBACK
  ["tool_name"]``, ``tests/test_codex_tool_proposals.py`` uses an
  ``exec_command`` proposal as the invalid case). The note states this before
  the model spends a turn on a rejected proposal.
- ``image_input_unavailable``: the catalog records the model as text-only
  (``model_image_support == "unsupported"``) or the route is text-only by
  design (Antigravity stream-json). ``image_capabilities.require_route_image_support``
  rejects image input there, yet the core prompt asks for image inspection of
  visual claims. Unverified routes (peers) get no note: their model capability
  is unknown, and every added byte is paid on every peer request.

Reasoning effort stays governed by the existing metadata check
(``reasoning.validate``: models with declared supported levels). This module
only records what was sent; it never adds a parameter.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import urlparse

CATALOG = "ryn-model-profiles-1"
SETTING = "model_profile_overrides"
MAX_OVERRIDES = 64

# Catalog adapter values -> wire route. Aliases name the same Chat Completions
# contract (see llm_package.tool_capabilities.client_tools_contract).
ROUTES = {
    "openai_compatible": "openai_chat",
    "openai_chat": "openai_chat",
    "ollama": "openai_chat",
    "llama_cpp": "openai_chat",
    "openai_responses": "openai_responses",
    "anthropic_messages": "anthropic_messages",
    "codex_cli": "codex_cli",
    "claude_cli": "claude_cli",
    "antigravity_cli": "antigravity_cli",
}

FACTS = {
    "parallel_tool_calls": {"type": "boolean"},
    "max_tool_output_bytes": {"type": "integer", "minimum": 1},
    "verbosity": {"type": "string", "enum": ("low", "medium", "high")},
    "patch_tool": {"type": "string", "enum": ("function", "freeform")},
    "context_window": {"type": "integer", "minimum": 256, "maximum": 2000000},
    "max_output_tokens": {"type": "integer", "minimum": 1, "maximum": 393216},
}
ROUTE_FACTS = {route: dict.fromkeys(FACTS) for route in set(ROUTES.values())}
CAPACITY_FACTS = frozenset({"context_window", "max_output_tokens"})

HEADER = ("Model route notes (facts about the current model connection; they never override owner "
          "instructions, workspace rules or host permissions):")

CODEX_BACKEND_TOOLS = (
    "This model is reached through a Codex backend. Its built-in instructions may describe Codex host tools "
    "(such as exec_command, apply_patch, update_plan or web_search); they are not available here and are rejected. "
    "Call only the functions supplied in this request. If ryn_apply_patch is supplied, it accepts the same "
    "*** Begin Patch format as apply_patch.")

# image_capabilities.evaluate_image_capability reasons for routes that are
# text-only by construction (not merely unverified).
TEXT_ONLY_ROUTE_REASONS = frozenset({"antigravity_cli_stream_json_text_only"})

IMAGE_INPUT_UNAVAILABLE = (
    "This model route does not accept image input, so screenshots and image files cannot be shown to the model. "
    "Check visual claims with text, DOM/accessibility or file readbacks, and say when pixels were not inspected.")


@dataclass(frozen=True)
class Profile:
    id: str
    revision: int
    routes: frozenset | None  # None fits every route
    notes: tuple  # ((note_id, text), ...)
    progress_placement: str = "tail"
    custom_tools: bool = False


PROFILES = {
    "generic": Profile("generic", 1, None, (), progress_placement="tail", custom_tools=False),
    "codex-cli": Profile("codex-cli", 1, frozenset({"codex_cli"}),
                         (("codex_backend_tools", CODEX_BACKEND_TOOLS),),
                         progress_placement="header", custom_tools=False),
}

OPENAI_RESPONSES_HOSTS = frozenset({"api.openai.com"})


def supports_custom_tools(model: dict | None) -> bool:
    """Custom freeform tools are default-off and enabled only on verified OpenAI Responses sources."""
    if not isinstance(model, dict) or route_of(model) != "openai_responses":
        return False
    if model.get("source", "local_api") != "local_api":
        return False
    if model.get("custom_tools") is False:
        return False
    evidence = model.get("evidence")
    if not isinstance(evidence, dict):
        settings = model.get("model_settings")
        evidence = settings.get("evidence") if isinstance(settings, dict) else None
    ct_ev = evidence.get("custom_tools") if isinstance(evidence, dict) else None
    if isinstance(ct_ev, dict) and ct_ev.get("status") == "unsupported":
        return False
    if model.get("custom_tools") is True or (isinstance(ct_ev, dict) and ct_ev.get("status") == "supported"):
        return True
    provider = str(model.get("provider") or "").strip().lower()
    base_url = str(model.get("base_url") or "").strip()
    host = (urlparse(base_url).hostname or "").lower() if base_url else ""
    if provider != "openai" and host not in OPENAI_RESPONSES_HOSTS:
        return False
    tools_ev = evidence.get("tools") if isinstance(evidence, dict) else None
    return (
        model.get("tool_support") == "supported"
        or (isinstance(tools_ev, dict) and tools_ev.get("status") == "supported")
    )


def route_of(model: dict | None) -> str:
    if not isinstance(model, dict):
        return "unknown"
    adapter = model.get("adapter") or model.get("cli_source")
    return ROUTES.get(adapter, "unknown") if isinstance(adapter, str) else "unknown"


def _fits(profile: Profile, route: str) -> bool:
    return profile.routes is None or route in profile.routes


def _route_profile(route: str) -> Profile | None:
    # Sorted ids keep the choice stable if two profiles ever claim a route.
    matches = [PROFILES[key] for key in sorted(PROFILES) if PROFILES[key].routes and route in PROFILES[key].routes]
    return matches[0] if matches else None


def capability_notes(model: dict | None) -> tuple:
    # Only definite facts: the model is recorded as text-only, or the route is
    # text-only by design. An unverified route (peers, unknown adapters) is
    # left to the existing dispatch rejection and its correction.
    if (isinstance(model, dict) and model.get("supports_image_input") is False
            and (model.get("model_image_support") == "unsupported"
                 or model.get("image_capability_reason") in TEXT_ONLY_ROUTE_REASONS)):
        return (("image_input_unavailable", IMAGE_INPUT_UNAVAILABLE),)
    return ()


def _valid_fact(name, value):
    if value is None:
        return True
    definition = FACTS[name]
    expected = {"boolean": bool, "integer": int, "string": str}[definition["type"]]
    if type(value) is not expected:
        return False
    if "enum" in definition:
        return value in definition["enum"]
    return (("minimum" not in definition or value >= definition["minimum"])
            and ("maximum" not in definition or value <= definition["maximum"]))


def _capacity_facts_allowed(model):
    return (isinstance(model, dict) and (model.get("source") in {"local_service", "peer"}
            or route_of(model) in {"codex_cli", "claude_cli", "antigravity_cli"}))


def _facts_for_entry(model, entry):
    model = model if isinstance(model, dict) else {}
    facts = dict(ROUTE_FACTS.get(route_of(model), dict.fromkeys(FACTS)))
    for name in FACTS:
        if name not in model:
            continue
        if name in CAPACITY_FACTS and (not _capacity_facts_allowed(model)
                or model.get(name + "_known") is False):
            continue
        value = model.get(name)
        if _valid_fact(name, value):
            facts[name] = value
    if isinstance(entry, dict):
        for name, value in entry.get("facts", {}).items():
            if name in FACTS and _valid_fact(name, value) and (
                    name not in CAPACITY_FACTS or _capacity_facts_allowed(model)):
                facts[name] = value
    return facts


def facts_for(model: dict | None, overrides=None) -> dict:
    """Resolve route defaults, catalog fields, then exact model-id owner facts."""
    model_id = model.get("id") if isinstance(model, dict) else None
    entry = overrides.get(model_id) if isinstance(overrides, dict) else None
    return _facts_for_entry(model, entry)


def resolve(model: dict | None, *, override=None) -> dict:
    """Pick one profile for this model; the result holds no task or owner data."""
    route = route_of(model)
    override_status = None
    profile = basis = fallback_reason = None
    profile_override = override.get("profile") if isinstance(override, dict) else override
    if profile_override is not None:
        candidate = PROFILES.get(profile_override) if isinstance(profile_override, str) else None
        if candidate is None:
            override_status = "unknown_profile"
        elif not _fits(candidate, route):
            override_status = "route_mismatch"
        else:
            profile, basis, override_status = candidate, "override", "applied"
    if profile is None and model is not None:
        matched = _route_profile(route)
        if matched is not None:
            profile, basis = matched, "route"
    if profile is None:
        profile, basis = PROFILES["generic"], "fallback"
        fallback_reason = ("model_unavailable" if model is None else
                           "unknown_route" if route == "unknown" else "no_route_profile")
    facts = _facts_for_entry(model, override)
    parallel_notes = (("single_tool_call", "emit at most one tool call per response"),) if facts["parallel_tool_calls"] is False else ()
    notes = (*profile.notes, *capability_notes(model), *parallel_notes)
    text = HEADER + "".join("\n- " + body for _, body in notes) if notes else ""
    custom_tools = (
        profile.custom_tools
        if basis == "override"
        else (profile.custom_tools or supports_custom_tools(model))
    )
    return {"catalog": CATALOG, "profile": profile.id, "revision": profile.revision, "basis": basis,
            "route": route, "fallback_reason": fallback_reason, "override_status": override_status,
            "notes": [note_id for note_id, _ in notes], "instructions": text,
            "progress_placement": profile.progress_placement, "custom_tools": custom_tools, "facts": facts}


def overrides_from(settings) -> dict:
    value = settings.get(SETTING) if isinstance(settings, dict) else None
    return {key: {"profile": entry} if isinstance(entry, str) else entry
            for key, entry in value.items()} if isinstance(value, dict) else {}


def for_task(runtime, row: dict, model: dict | None = None) -> dict:
    """Resolve for the task's current model and the owner's current override.

    ``model`` is the caller's catalog row when it already has one (an empty
    dict means the model is not in the catalog); None looks it up.
    """
    if model is None:
        lookup = getattr(runtime, "_model_metadata", None)
        model = lookup(row["model_id"]) if callable(lookup) else None
    store = getattr(runtime, "store", None)
    if hasattr(store, "inspect"):
        settings = store.inspect(lambda data: {SETTING: (data.get("settings") or {}).get(SETTING)})
    elif hasattr(store, "read"):
        settings = store.read().get("settings") or {}
    else:
        settings = {}
    return resolve(model or None, override=overrides_from(settings).get(row["model_id"]))


def validate_overrides(value, lookup=None) -> dict:
    """Normalize legacy profiles and validate owner facts on exact catalog ids."""
    if not isinstance(value, dict) or len(value) > MAX_OVERRIDES:
        raise ValueError("model_profile_overrides_invalid")
    normalized = {}
    for model_id, entry in value.items():
        if isinstance(entry, str):
            entry = {"profile": entry}
        if (not isinstance(model_id, str) or not 1 <= len(model_id) <= 1024
                or not isinstance(entry, dict) or set(entry) - {"profile", "facts"}):
            raise ValueError("model_profile_overrides_invalid")
        model = lookup(model_id) if callable(lookup) else None
        if "profile" in entry:
            profile_id = entry["profile"]
            if not isinstance(profile_id, str) or profile_id not in PROFILES:
                raise ValueError("model_profile_overrides_invalid")
            if model is not None and not _fits(PROFILES[profile_id], route_of(model)):
                raise ValueError("model_profile_route_mismatch")
        facts = entry.get("facts", {})
        if not isinstance(facts, dict) or any(name not in FACTS or not _valid_fact(name, fact)
                                               for name, fact in facts.items()):
            raise ValueError("model_profile_facts_invalid")
        if model is not None and not _capacity_facts_allowed(model) and any(
                name in CAPACITY_FACTS and fact is not None for name, fact in facts.items()):
            raise ValueError("model_profile_fact_route_mismatch")
        effective = _facts_for_entry(model, entry)
        # Unknown/offline models can still retain a complete owner's declaration.
        window = effective["context_window"] if model is not None else facts.get("context_window")
        output = effective["max_output_tokens"] if model is not None else facts.get("max_output_tokens")
        if window is not None and output is not None and output > window:
            raise ValueError("model_profile_facts_invalid")
        normalized[model_id] = {**({"profile": entry["profile"]} if "profile" in entry else {}),
                                **({"facts": dict(facts)} if "facts" in entry else {})}
    return normalized


def request_record(selection: dict, messages, options: dict) -> dict:
    """Credential- and prompt-free provenance for one dispatched request."""
    text = selection["instructions"]
    sent = bool(text) and any(isinstance(message, dict) and message.get("role") in {"developer", "system"}
                              and message.get("content") == text for message in messages)
    record = {key: selection[key] for key in ("catalog", "profile", "revision", "basis", "route",
                                              "fallback_reason", "override_status")}
    record["notes"] = list(selection["notes"])
    # Summary, checker and image requests carry their own prompts by design.
    record["instructions"] = "none" if not text else "sent" if sent else "not_in_request"
    record["instructions_sha256"] = hashlib.sha256(text.encode()).hexdigest()[:16] if text else None
    record["reasoning_effort"] = options.get("reasoning_effort")
    record["facts"] = dict(selection["facts"])
    return record
