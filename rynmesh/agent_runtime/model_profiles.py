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
(``reasoning.validate``: Codex CLI models with advertised levels). This module
only records what was sent; it never adds a parameter.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

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
    "antigravity_cli": "antigravity_cli",
}

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


PROFILES = {
    "generic": Profile("generic", 1, None, ()),
    "codex-cli": Profile("codex-cli", 1, frozenset({"codex_cli"}),
                         (("codex_backend_tools", CODEX_BACKEND_TOOLS),)),
}


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


def resolve(model: dict | None, *, override=None) -> dict:
    """Pick one profile for this model; the result holds no task or owner data."""
    route = route_of(model)
    override_status = None
    profile = basis = fallback_reason = None
    if override is not None:
        candidate = PROFILES.get(override) if isinstance(override, str) else None
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
    notes = (*profile.notes, *capability_notes(model))
    text = HEADER + "".join("\n- " + body for _, body in notes) if notes else ""
    return {"catalog": CATALOG, "profile": profile.id, "revision": profile.revision, "basis": basis,
            "route": route, "fallback_reason": fallback_reason, "override_status": override_status,
            "notes": [note_id for note_id, _ in notes], "instructions": text}


def overrides_from(settings) -> dict:
    value = settings.get(SETTING) if isinstance(settings, dict) else None
    return value if isinstance(value, dict) else {}


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
    """Owner configuration: exact catalog ids to known profiles that fit the route."""
    if not isinstance(value, dict) or len(value) > MAX_OVERRIDES:
        raise ValueError("model_profile_overrides_invalid")
    for model_id, profile_id in value.items():
        if (not isinstance(model_id, str) or not 1 <= len(model_id) <= 1024
                or not isinstance(profile_id, str) or profile_id not in PROFILES):
            raise ValueError("model_profile_overrides_invalid")
        model = lookup(model_id) if callable(lookup) else None
        if model is not None and not _fits(PROFILES[profile_id], route_of(model)):
            raise ValueError("model_profile_route_mismatch")
    return dict(value)


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
    return record
