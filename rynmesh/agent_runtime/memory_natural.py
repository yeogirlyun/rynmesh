from __future__ import annotations

import copy
import json
import re
import time
from typing import Any

from . import memory
from .protocol import digest
from .store import AgentError

NATURAL_CONSOLIDATION_SCHEMA_VERSION = 2

VALID_OBSERVATION_KINDS = frozenset({
    "owner_fact",
    "owner_preference",
    "project_fact",
    "reusable_lesson",
})
VALID_OBSERVATION_SUBJECTS = frozenset({"owner", "project"})
VALID_OBSERVATION_STABILITY = frozenset({"durable"})
VALID_OWNERSHIP_MODES = frozenset({"owned_or_habitual", "replaced_or_sold"})
VALID_CONSOLIDATION_ACTIONS = frozenset({"add", "update", "retract", "noop"})

_FENCED_BLOCK_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~", re.MULTILINE)
_BLOCKQUOTE_LINE_RE = re.compile(r"^\s*>.*$", re.MULTILINE)
_INLINE_QUOTE_RE = re.compile(r"“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"]*\"")
_TEMPORARY_BORROW_RE = re.compile(
    r"(?:借(?:了|用)?朋友|朋友借我|租(?:了|来)(?:一套|一台|一个|周末)|周末租|试(?:用|玩)一下朋友|"
    r"\bborrow(?:ed|ing)\s+(?:a\s+)?friend'?s\b|\brent(?:ed|ing)\s+for\s+the\s+weekend\b)",
    re.IGNORECASE,
)


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def strip_quoted_or_fenced_content(text: str) -> str:
    """Remove fenced code blocks, markdown blockquote lines, and inline quoted strings."""
    raw = str(text or "")
    if not raw:
        return ""
    without_fences = _FENCED_BLOCK_RE.sub(" ", raw)
    without_blockquotes = _BLOCKQUOTE_LINE_RE.sub(" ", without_fences)
    without_inline_quotes = _INLINE_QUOTE_RE.sub(" ", without_blockquotes)
    return _normalize_ws(without_inline_quotes)


def _extract_json_object(raw_text: str, *, error_code: str = "invalid_extraction_json") -> dict[str, Any]:
    text = str(raw_text or "").strip()
    if not text:
        raise ValueError(error_code)
    try:
        parsed = json.loads(text)
    except Exception as exc:
        raise ValueError(error_code) from exc
    if not isinstance(parsed, dict):
        raise ValueError(error_code)
    return parsed


# ---------------------------------------------------------------------------
# Authoritative Owner Acceptance-Order Sequencing
# ---------------------------------------------------------------------------


def _is_stampable_owner_message(msg: Any) -> bool:
    if not isinstance(msg, dict):
        return False
    if str(msg.get("role") or "") != "user":
        return False
    if not isinstance(msg.get("message_id"), str) or not msg["message_id"].strip():
        return False
    if (
        msg.get("synthetic")
        or msg.get("is_summary")
        or msg.get("untrusted")
        or msg.get("source_credential")
        or msg.get("visibility") == "internal"
    ):
        return False
    ui_kind = (msg.get("ui") or {}).get("kind") if isinstance(msg.get("ui"), dict) else None
    if ui_kind == "source_credential":
        return False
    author_role = str(msg.get("author_role") or "owner").strip().lower()
    return author_role in {"owner", "user"}


def stamp_single_owner_message_in_tx(data: dict[str, Any], msg: dict[str, Any]) -> int | None:
    """Assign a monotonically increasing owner_source_seq at owner message acceptance time."""
    if not _is_stampable_owner_message(msg):
        return None
    existing_seq = msg.get("owner_source_seq")
    if type(existing_seq) is int and existing_seq > 0:
        if isinstance(data.get("memory"), dict):
            data["memory"]["source_seq"] = max(int(data["memory"].get("source_seq") or 0), existing_seq)
        return existing_seq
    mem = memory.ensure_memory_state(data)
    next_seq = int(mem.get("source_seq") or 0) + 1
    mem["source_seq"] = next_seq
    msg["owner_source_seq"] = next_seq
    return next_seq


def stamp_task_owner_messages_in_tx(data: dict[str, Any], row_or_task_id: dict[str, Any] | str) -> int:
    """Stamp all unsequenced owner messages in a task's messages and pending_user_messages."""
    if isinstance(row_or_task_id, dict):
        row = row_or_task_id
    else:
        tasks = data.get("tasks")
        if not isinstance(tasks, dict):
            return 0
        row = tasks.get(str(row_or_task_id or ""))
        if not isinstance(row, dict):
            return 0
    stamped = 0
    for list_key in ("messages", "pending_user_messages"):
        msgs = row.get(list_key)
        if not isinstance(msgs, list):
            continue
        for msg in msgs:
            if not _is_stampable_owner_message(msg):
                continue
            had_seq = type(msg.get("owner_source_seq")) is int and int(msg.get("owner_source_seq") or 0) > 0
            seq = stamp_single_owner_message_in_tx(data, msg)
            if not had_seq and seq is not None and seq > 0:
                stamped += 1
    return stamped


def stamp_all_tasks_owner_messages_in_tx(data: dict[str, Any]) -> int:
    """Ensure every owner message across all tasks has an authoritative owner_source_seq."""
    tasks = data.get("tasks")
    if not isinstance(tasks, dict):
        return 0
    total = 0
    for row in tasks.values():
        if isinstance(row, dict):
            total += stamp_task_owner_messages_in_tx(data, row)
    return total


# ---------------------------------------------------------------------------
# Model-Directed Progressive Recall Metadata (Structural Topic/Slot Only)
# ---------------------------------------------------------------------------


def derive_semantic_keywords(
    fact_key: str,
    category: str,
    text: str = "",
    *,
    extra_keywords: Any = None,
) -> list[str]:
    """Derive structural slot/category keywords without special-case query phrases or raw text leakage."""
    del text  # Never copy raw fact text or inject benchmark-specific query synonyms.
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        cleaned = str(tok or "").strip().lower()
        if not cleaned or len(cleaned) > 64 or cleaned in seen:
            return
        seen.add(cleaned)
        tokens.append(cleaned)

    norm_cat = memory.normalize_category(category)
    norm_key = memory.normalize_fact_key(fact_key, category=norm_cat, text="slot") if str(fact_key or "").strip() else ""
    _add(norm_cat)
    if norm_key:
        _add(norm_key)
        for part in re.split(r"[\s./:_-]+", norm_key):
            if len(part) >= 2:
                _add(part)
    for part in re.split(r"[\s./:_-]+", norm_cat):
        if len(part) >= 2:
            _add(part)

    if isinstance(extra_keywords, list):
        for kw in extra_keywords:
            if isinstance(kw, str):
                _add(kw)
            if len(tokens) >= 16:
                break
    return tokens[:16]


def derive_navigation_topic(entry_or_meta: dict[str, Any]) -> str:
    """Build a compact structural topic descriptor strictly from category and fact_key."""
    if not isinstance(entry_or_meta, dict):
        return "general"
    category = memory.normalize_category(entry_or_meta.get("category") or "general")
    raw_fk = str(entry_or_meta.get("fact_key") or "").strip()
    fact_key = memory.normalize_fact_key(raw_fk, category=category, text="slot") if raw_fk else ""
    if fact_key and category and not fact_key.startswith(f"{category}."):
        return f"{category}/{fact_key}"[:80]
    if fact_key:
        return fact_key[:80]
    return category[:80] or "general"


def build_entry_summary(fact_key: str, text: str = "", explicit_summary: str = "") -> str:
    """Build a compact structural summary without copying raw entry text."""
    cleaned_explicit = _normalize_ws(explicit_summary)
    cleaned_text = _normalize_ws(text)
    if cleaned_explicit and cleaned_explicit != cleaned_text:
        return cleaned_explicit[:120]
    raw_fk = str(fact_key or "").strip()
    if raw_fk:
        return memory.normalize_fact_key(raw_fk, category="general", text="slot")[:120]
    return "general"


def build_navigation_and_summary(
    verified_entries: list[dict[str, Any]],
    max_bytes: int = 1536,
) -> tuple[list[str], list[dict[str, Any]], int]:
    """Build a small factual profile and an index from the current verified view.

    Only canonical active facts supplied by the caller enter this projection;
    source excerpts, history and model-generated summaries/tags never do. Long
    facts remain available through the index rather than being cut into a
    potentially misleading partial claim.
    """
    if not verified_entries or max_bytes <= 0:
        return [], [], 0

    def payload_bytes(profile: list[str], index: list[dict[str, Any]]) -> int:
        payload = {
            "matched_facts": [],
            "profile_summary": profile,
            "navigation_index": index,
        }
        return len(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))

    ordered = sorted(
        (entry for entry in verified_entries if isinstance(entry, dict)),
        key=lambda entry: (
            0 if (entry.get("scope") or {}).get("kind") == "project" else 1,
            0 if entry.get("kind") == "owner_preference" else 1,
            str(entry.get("category") or "general"),
            str(entry.get("fact_key") or ""),
            str(entry.get("id") or ""),
        ),
    )
    profile_budget = min(512, max_bytes // 3)
    index_budget = max_bytes - profile_budget
    navigation_index: list[dict[str, Any]] = []
    indexed_entries: list[dict[str, Any]] = []

    for entry in ordered:
        scope_obj = entry.get("scope") if isinstance(entry.get("scope"), dict) else {}
        scope_kind = str(scope_obj.get("kind") or "personal")
        category = memory.normalize_category(entry.get("category") or "general")
        raw_key = str(entry.get("fact_key") or "").strip()
        fact_key = memory.normalize_fact_key(raw_key, category=category, text="slot") if raw_key else ""
        item: dict[str, Any] = {
            "id": str(entry.get("id") or ""),
            "scope": scope_kind,
            "project_id": str(scope_obj.get("project_id") or "") if scope_kind == "project" else None,
            "category": category,
            "fact_key": fact_key,
            "topic": derive_navigation_topic({"category": category, "fact_key": fact_key}),
            "kind": str(entry.get("kind") or "owner_fact"),
            "revision": int(entry.get("revision") or 1),
        }
        if not item["id"]:
            continue
        candidate = navigation_index + [item]
        # Reserve some room for actual profile values. A tiny budget can still
        # carry one index reference if a profile cannot fit alongside it.
        cap = index_budget if navigation_index else max(index_budget, max_bytes - 96)
        if payload_bytes([], candidate) > cap:
            if not navigation_index and payload_bytes([], candidate) <= max_bytes:
                navigation_index.append(item)
                indexed_entries.append(entry)
            break
        navigation_index.append(item)
        indexed_entries.append(entry)
        if len(navigation_index) >= 8:
            break

    if not navigation_index:
        return [], [], 0

    profile_summary: list[str] = []
    profile_used = 0
    seen_text: set[str] = set()
    for entry in indexed_entries:
        text = _normalize_ws(str(entry.get("text") or ""))
        text_bytes = len(text.encode("utf-8"))
        if not text or text in seen_text or text_bytes > 180:
            continue
        category = memory.normalize_category(entry.get("category") or "general")
        line = f"{category}: {text}"
        line_bytes = len(json.dumps(line, ensure_ascii=False).encode("utf-8"))
        if profile_used + line_bytes > profile_budget:
            continue
        candidate_profile = profile_summary + [line]
        if payload_bytes(candidate_profile, navigation_index) > max_bytes:
            continue
        profile_summary.append(line)
        profile_used += line_bytes
        seen_text.add(text)
        if len(profile_summary) >= 4:
            break

    return profile_summary, navigation_index, payload_bytes(profile_summary, navigation_index)


# ---------------------------------------------------------------------------
# Stage 1: Zero-Tool Natural Observation Extraction
# ---------------------------------------------------------------------------


def build_phase1_source_analysis_messages(job: dict[str, Any]) -> list[dict[str, str]]:
    """Build strict zero-tool Stage-1 prompt messages from verified owner source_manifest."""
    source_manifest = job.get("source_manifest") if isinstance(job.get("source_manifest"), list) else []
    project_id = str(job.get("project_id") or "").strip()
    source_payload: list[dict[str, Any]] = []
    for ref in source_manifest:
        if not isinstance(ref, dict):
            continue
        raw_text = str(ref.get("text") if ref.get("text") is not None else (ref.get("excerpt") or ""))
        unquoted = strip_quoted_or_fenced_content(raw_text)
        if not unquoted:
            continue
        source_payload.append({
            "message_id": str(ref.get("message_id") or ""),
            "source_seq": int(ref.get("source_seq") or 0),
            "text": unquoted,
        })
    if not source_payload:
        return []

    # These slot handles are host-filtered for this exact destination. They
    # guide semantic naming, but never constitute evidence for a new owner fact.
    known_memory_slots: list[dict[str, str]] = []
    seen_slots: set[tuple[str, str]] = set()
    raw_known = job.get("known_memory_slots")
    for slot in raw_known[:memory.MAX_ENTRIES] if isinstance(raw_known, list) else []:
        if not isinstance(slot, dict):
            continue
        raw_scope = slot.get("scope")
        scope_kind = raw_scope.get("kind") if isinstance(raw_scope, dict) else raw_scope
        fact_key = slot.get("fact_key")
        category = slot.get("category")
        kind = slot.get("kind")
        if (scope_kind not in {"personal", "project"}
                or not isinstance(fact_key, str) or not fact_key.strip()
                or not isinstance(category, str) or not category.strip()
                or kind not in VALID_OBSERVATION_KINDS):
            continue
        identity = (scope_kind, fact_key)
        if identity in seen_slots:
            continue
        seen_slots.add(identity)
        known_memory_slots.append({
            "scope": scope_kind, "category": category[:memory.MAX_CATEGORY_CHARS],
            "fact_key": fact_key[:memory.MAX_FACT_KEY_CHARS], "kind": kind,
        })

    system_prompt = (
        "You are Stage 1 (Source Observation Extractor) of Ryn's background memory consolidation pipeline.\n"
        "Analyze ONLY the provided verified owner messages. Do NOT call any tools.\n"
        "Extract ONLY durable owner facts, durable owner preferences, durable project facts, or reusable project lessons "
        "that are explicitly stated by the owner in their own voice.\n"
        "Strict rules:\n"
        "1. Ignore assistant text, tool outputs, quoted/fenced blocks, hypothetical examples, temporary plans, "
        "borrowing/renting/testing a friend's item, one-off debugging data, and secrets/credentials.\n"
        "2. Distinguish current/habitual facts (`ownership_mode: \"owned_or_habitual\"`) from explicit owner statements "
        "that an item, preference, or convention was sold, replaced, retired, or abandoned (`ownership_mode: \"replaced_or_sold\"`).\n"
        "3. Keep project-scoped constraints in `scope: \"project\"` with `subject: \"project\"`; never promote project-only facts to personal scope.\n"
        "Each observation represents ONE independently changeable entity/property. Split separately owned items, "
        "components, software habits and preferences into separate observations with separate stable fact_keys. "
        "Do not bundle an equipment combination or an entire workflow into one primary-item fact: later one component "
        "may change while the others remain true. Multiple atomic observations may cite the same exact source span.\n"
        "fact_key identifies a stable property or role, not its current value, model name or date. "
        "When known_memory_slots contains the same semantic property and scope, reuse its exact fact_key and category, "
        "including when the owner changes the value; do not create a new value-named slot.\n"
        "A current request, temporary plan or the word 'again' does not establish a recurring schedule or durable preference. "
        "Retain only what the owner actually states durably; do not turn a one-off outing into a weekly habit.\n"
        "Keep task motivation and current difficulties out of durable facts unless the owner explicitly states an enduring "
        "condition or preference. Do not add an activity/preference/background summary that repeats independently "
        "changeable values already captured in other observations. Each canonical text states only its own property.\n"
        "4. Every observation MUST include an exact verbatim substring `evidence_span` copied from every cited message in `source_message_ids`.\n"
        "5. Return ONLY a strict JSON object matching this exact schema:\n"
        "{\n"
        '  "observations": [\n'
        "    {\n"
        '      "observation_id": "obs_1",\n'
        '      "kind": "owner_fact" | "owner_preference" | "project_fact" | "reusable_lesson",\n'
        '      "scope": "personal" | "project",\n'
        '      "subject": "owner" | "project",\n'
        '      "stability": "durable",\n'
        '      "ownership_mode": "owned_or_habitual" | "replaced_or_sold",\n'
        '      "category": "<compact_category_slug>",\n'
        '      "fact_key": "<hierarchical.slot.key>",\n'
        '      "text": "<concise self-contained durable statement or retraction>",\n'
        '      "evidence_span": "<exact verbatim substring from the cited owner message>",\n'
        '      "source_message_ids": ["<message_id>"]\n'
        "    }\n"
        "  ]\n"
        "}"
    )
    user_payload = {
        "task_id": str(job.get("task_id") or ""),
        "project_id": project_id or None,
        "owner_messages": source_payload,
        "known_memory_slots": known_memory_slots,
    }
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


def _evidence_matches_unquoted_text(evidence_span: str, unquoted_text: str) -> bool:
    ev = str(evidence_span or "").strip()
    txt = str(unquoted_text or "").strip()
    if not ev or not txt:
        return False
    if ev in txt:
        return True
    norm_ev = _normalize_ws(ev)
    norm_txt = _normalize_ws(txt)
    return bool(norm_ev and norm_ev in norm_txt)


def verify_natural_observation_grounding(
    obs: dict[str, Any],
    job: dict[str, Any],
) -> tuple[bool, str, dict[str, Any] | None]:
    """Validate Stage-1 observation authority, scope rules, and exact per-message evidence grounding."""
    if not isinstance(obs, dict):
        return False, "invalid_observation_type", None

    # Require explicit authority fields without permissive defaults
    raw_kind = obs.get("kind")
    raw_stability = obs.get("stability")
    raw_subject = obs.get("subject")
    raw_ownership = obs.get("ownership_mode")
    if not isinstance(raw_kind, str) or raw_kind.strip() not in VALID_OBSERVATION_KINDS:
        return False, "invalid_kind", None
    if not isinstance(raw_stability, str) or raw_stability.strip() not in VALID_OBSERVATION_STABILITY:
        return False, "non_durable_observation", None
    if not isinstance(raw_subject, str) or raw_subject.strip() not in VALID_OBSERVATION_SUBJECTS:
        return False, "invalid_subject", None
    if not isinstance(raw_ownership, str) or raw_ownership.strip() not in VALID_OWNERSHIP_MODES:
        return False, "invalid_ownership_mode", None

    kind = raw_kind.strip()
    stability = raw_stability.strip()
    subject = raw_subject.strip()
    ownership_mode = raw_ownership.strip()

    raw_scope_field = obs.get("scope")
    if isinstance(raw_scope_field, dict):
        raw_scope = str(raw_scope_field.get("kind") or "").strip()
        req_proj_id = str(raw_scope_field.get("project_id") or "").strip() or None
    else:
        raw_scope = str(raw_scope_field or "").strip()
        req_proj_id = str(obs.get("project_id") or "").strip() or None

    if raw_scope not in {"personal", "project"}:
        return False, "invalid_scope", None
    if subject == "project" and raw_scope == "personal":
        return False, "project_subject_cannot_promote_to_personal", None
    if kind in {"project_fact", "reusable_lesson"} and raw_scope != "project":
        return False, "project_kind_requires_project_scope", None
    if kind in {"owner_fact", "owner_preference"} and raw_scope == "project" and subject == "owner":
        return False, "owner_kind_scope_mismatch", None

    job_project_id = str(job.get("project_id") or "").strip()
    if raw_scope == "project":
        if not job_project_id:
            return False, "missing_project_scope", None
        if req_proj_id is not None and req_proj_id != job_project_id:
            return False, "cross_project_escalation_blocked", None

    raw_text_val = obs.get("text")
    raw_evidence_val = obs.get("evidence_span")
    if not isinstance(raw_text_val, str) or not raw_text_val.strip():
        return False, "invalid_text", None
    if not isinstance(raw_evidence_val, str) or not raw_evidence_val.strip():
        return False, "missing_evidence_span", None

    try:
        text = memory._clean_text(raw_text_val, field="text", max_chars=memory.MAX_FACT_TEXT_CHARS)
        evidence_span = memory._clean_text(raw_evidence_val, field="text", max_chars=memory.MAX_FACT_TEXT_CHARS)
    except AgentError as exc:
        return False, str(exc), None

    if _TEMPORARY_BORROW_RE.search(text) or _TEMPORARY_BORROW_RE.search(evidence_span):
        return False, "temporary_or_borrowed_item", None

    raw_fact_key = obs.get("fact_key")
    if not isinstance(raw_fact_key, str) or not raw_fact_key.strip():
        return False, "missing_fact_key", None
    try:
        category = memory.normalize_category(obs.get("category") or "general")
        fact_key = memory.normalize_fact_key(raw_fact_key, category=category, text=text)
    except AgentError as exc:
        return False, str(exc), None

    raw_msg_ids = obs.get("source_message_ids")
    if not isinstance(raw_msg_ids, list) or not raw_msg_ids:
        return False, "missing_source_message_ids", None

    source_manifest = job.get("source_manifest") if isinstance(job.get("source_manifest"), list) else []
    manifest_by_id: dict[str, dict[str, Any]] = {}
    for ref in source_manifest:
        if isinstance(ref, dict) and ref.get("message_id"):
            manifest_by_id[str(ref["message_id"])] = ref

    cited_ids: list[str] = []
    matched_refs: list[dict[str, Any]] = []
    for mid in raw_msg_ids:
        if not isinstance(mid, str) or not mid.strip():
            return False, "empty_source_message_id", None
        mid_str = mid.strip()
        ref = manifest_by_id.get(mid_str)
        if ref is None:
            return False, "ungrounded_source_message_id", None
        if str(ref.get("author_role") or "owner") != "owner":
            return False, "non_owner_source_message", None
        raw_src_text = str(ref.get("text") if ref.get("text") is not None else (ref.get("excerpt") or ""))
        unquoted = strip_quoted_or_fenced_content(raw_src_text)
        if not _evidence_matches_unquoted_text(evidence_span, unquoted):
            return False, "evidence_span_not_in_source", None
        if _TEMPORARY_BORROW_RE.search(unquoted):
            return False, "temporary_or_borrowed_source", None
        if mid_str not in cited_ids:
            cited_ids.append(mid_str)
            matched_refs.append(ref)

    if not matched_refs:
        return False, "no_grounded_source_messages", None

    obs_id = str(obs.get("observation_id") or "").strip() or "obs_1"
    max_seq = max(int(r.get("source_seq") or 0) for r in matched_refs)

    normalized = {
        "observation_id": obs_id,
        "kind": kind,
        "scope": raw_scope,
        "project_id": job_project_id if raw_scope == "project" else None,
        "subject": subject,
        "stability": stability,
        "ownership_mode": ownership_mode,
        "category": category,
        "fact_key": fact_key,
        "text": text,
        "evidence_span": evidence_span,
        "source_message_ids": cited_ids,
        "source_seq": max_seq,
    }
    return True, "ok", normalized


def parse_phase1_source_analysis_response(
    raw_content: str,
    job: dict[str, Any],
    task_row: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Parse Stage-1 response into verified observations (or legacy candidates in owner_review mode)."""
    parsed = _extract_json_object(raw_content, error_code="invalid_extraction_json")

    if "observations" in parsed:
        raw_obs = parsed.get("observations")
        if not isinstance(raw_obs, list):
            raise ValueError("invalid_extraction_json")
        verified: list[dict[str, Any]] = []
        rejected_reasons: list[str] = []
        seen_ids: set[str] = set()
        for idx, item in enumerate(raw_obs, start=1):
            if not isinstance(item, dict):
                raise ValueError("invalid_extraction_json")
            item_copy = dict(item)
            if not str(item_copy.get("observation_id") or "").strip():
                item_copy["observation_id"] = f"obs_{idx}"
            ok, _reason, norm = verify_natural_observation_grounding(item_copy, job)
            if not ok or norm is None:
                rejected_reasons.append(_reason)
                continue
            obs_id = str(norm["observation_id"])
            if obs_id in seen_ids:
                obs_id = f"{obs_id}_{idx}"
                norm["observation_id"] = obs_id
            seen_ids.add(obs_id)
            verified.append(norm)
        if rejected_reasons:
            # Invalid evidence is observably different from the model finding
            # no durable information. Never label a rejected batch a valid no-op.
            raise ValueError("invalid_observation:" + str(rejected_reasons[0] or "schema")[:160])
        return "observations", verified

    activation_policy = str(job.get("activation_policy") or "owner_review")
    if "candidates" in parsed and activation_policy != "automatic":
        from . import memory_runtime

        job_with_scope = {
            **job,
            "task_scope": (
                {
                    "kind": "project",
                    "project_id": job["project_id"],
                    "project_generation": job.get("project_generation"),
                }
                if job.get("project_id")
                else {"kind": "personal", "project_id": None}
            ),
        }
        proposals = memory_runtime.parse_extraction_response(raw_content, job_with_scope, task_row)
        return "legacy_candidates", proposals

    raise ValueError("invalid_extraction_json")


# ---------------------------------------------------------------------------
# Stage 2: Destination-Authorized Consolidation Against Existing Active Memories
# ---------------------------------------------------------------------------


def select_existing_memories_for_consolidation(
    data: dict[str, Any],
    job: dict[str, Any],
    verified_dest: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Select active memories in the job's scope that are authorized for the job's bound destination."""
    mem = memory.read_memory_state(data)
    bound_dest = memory.normalize_destination(
        verified_dest if isinstance(verified_dest, dict) else job.get("destination_binding")
    )
    if not bound_dest:
        return []
    job_project_id = str(job.get("project_id") or "").strip() or None
    job_project_gen = job.get("project_generation")

    permitted: list[dict[str, Any]] = []
    entries = mem.get("entries")
    if not isinstance(entries, dict):
        return []

    for entry in entries.values():
        if not isinstance(entry, dict) or entry.get("status") != "active":
            continue
        scope = entry.get("scope") if isinstance(entry.get("scope"), dict) else {}
        scope_kind = str(scope.get("kind") or "personal")
        if scope.get("owner_id", "local_owner") != "local_owner":
            continue
        if scope_kind == "project":
            if not memory._project_scope_matches(
                scope,
                job_project_id,
                job_project_gen,
                require_generation_match=True,
            ):
                continue
        elif scope_kind != "personal":
            continue

        if not memory.exposure_allows_destination(entry.get("exposure_policy"), bound_dest):
            continue

        sources = entry.get("sources") if isinstance(entry.get("sources"), list) else []
        task_sources = [s for s in sources if isinstance(s, dict) and s.get("source_type") == "task_message"]
        if task_sources and entry.get("origin") != "manual":
            if all(
                not memory.verify_source_ref_against_state(
                    data,
                    s,
                    for_background_job=False,
                    for_active_entry=True,
                )[0]
                for s in task_sources
            ):
                continue

        permitted.append({
            "id": str(entry.get("id") or ""),
            "scope": scope_kind,
            "project_id": str(scope.get("project_id") or "") if scope_kind == "project" else None,
            "project_generation": scope.get("project_generation") if scope_kind == "project" else None,
            "kind": str(entry.get("kind") or "owner_fact"),
            "category": memory.normalize_category(entry.get("category") or "general"),
            "fact_key": str(entry.get("fact_key") or ""),
            "text": str(entry.get("text") or ""),
            "revision": int(entry.get("revision") or 1),
            "last_source_seq": int(entry.get("last_source_seq") or 0),
            "owner_acceptance_seq": int(entry.get("owner_acceptance_seq") or 0),
        })

    permitted.sort(key=lambda item: (item["scope"], item["category"], item["fact_key"], item["id"]))
    return permitted


def build_phase2_consolidation_messages(
    job: dict[str, Any],
    observations: list[dict[str, Any]],
    existing_memories: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Build strict zero-tool Stage-2 consolidation prompt messages."""
    compact_existing = [
        {
            "id": str(e.get("id") or ""),
            "scope": str(e.get("scope") or "personal"),
            "project_id": e.get("project_id"),
            "kind": str(e.get("kind") or "owner_fact"),
            "category": str(e.get("category") or "general"),
            "fact_key": str(e.get("fact_key") or ""),
            "text": str(e.get("text") or ""),
            "revision": int(e.get("revision") or 1),
        }
        for e in existing_memories
        if isinstance(e, dict) and e.get("id")
    ]
    system_prompt = (
        "You are Stage 2 (Memory Consolidator) of Ryn's background memory consolidation pipeline.\n"
        "Reconcile the source-grounded Stage-1 candidate `observations` against the currently active `existing_memories`. "
        "Do NOT call any tools.\n"
        "Source grounding verifies the quotation, not the candidate's semantic quality. Select only independently useful, "
        "explicitly durable, atomic claims. Omit transient, inferred, redundant or composite candidates; not every observation "
        "deserves an operation. Do not retain changing item/software values again inside an activity, preference or "
        "background recap. Prefer the smallest nonredundant set of current facts. Return operations: [] if none qualify.\n"
        "Strict consolidation rules:\n"
        "1. `add`: Use when an observation with `ownership_mode: \"owned_or_habitual\"` introduces a new durable slot "
        "not yet present in `existing_memories`. Set `target_entry_id: null` and `expected_entry_revision: null`.\n"
        "2. `update`: Use when an observation with `ownership_mode: \"owned_or_habitual\"` refines or replaces an existing "
        "entry in `existing_memories`. You MUST specify `target_entry_id` and `expected_entry_revision` matching that entry's `revision`.\n"
        "3. `retract`: Use when an observation with `ownership_mode: \"replaced_or_sold\"` states that an existing active entry "
        "was sold, retired, replaced, or is no longer true, without replacing it in the same operation (or before adding a different slot). "
        "You MUST specify `target_entry_id` and `expected_entry_revision` matching that entry's `revision`.\n"
        "4. `noop`: Use when an observation is already captured by an existing entry or requires no state change.\n"
        "5. Every operation MUST cite its supporting `observation_ids`, preserve the observation's `evidence_span` and `source_message_ids`, "
        "and never invent facts not grounded in `observations`.\n"
        "6. Copy the selected observation fact_key, text, kind, category, scope, evidence_span and source_message_ids unchanged. "
        "The host derives these fields from the validated observation and rejects conflicting values.\n"
        "7. An update/retract target must have the SAME fact_key and scope as its observation; another disclosed entry is not an authorized target. "
        "For a same-slot replacement, update from the new owned observation instead of retracting and adding that same slot.\n"
        "Return ONLY a strict JSON object matching this exact schema:\n"
        "{\n"
        '  "operations": [\n'
        "    {\n"
        '      "action": "add" | "update" | "retract" | "noop",\n'
        '      "observation_ids": ["obs_1"],\n'
        '      "target_entry_id": "<existing_entry_id or null>",\n'
        '      "expected_entry_revision": 1,\n'
        '      "scope": "personal" | "project",\n'
        '      "kind": "owner_fact" | "owner_preference" | "project_fact" | "reusable_lesson",\n'
        '      "category": "<category_slug>",\n'
        '      "fact_key": "<hierarchical.slot.key>",\n'
        '      "text": "<consolidated durable fact text>",\n'
        '      "evidence_span": "<verbatim substring from cited owner message>",\n'
        '      "source_message_ids": ["<message_id>"]\n'
        "    }\n"
        "  ]\n"
        "}"
    )
    user_payload = {
        "task_id": str(job.get("task_id") or ""),
        "project_id": str(job.get("project_id") or "") or None,
        "observations": observations,
        "existing_memories": compact_existing,
    }
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


def _validate_single_consolidation_op(
    op: dict[str, Any],
    job: dict[str, Any],
    obs_map: dict[str, dict[str, Any]],
    existing_map: dict[str, dict[str, Any]] | None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """Limit a plan to one validated observation's value, slot and evidence."""
    if not isinstance(op, dict):
        return False, "invalid_op_type", None
    if not isinstance(op.get("action"), str):
        return False, "invalid_action", None
    action = op["action"].strip().lower()
    if action not in VALID_CONSOLIDATION_ACTIONS:
        return False, "invalid_action", None

    raw_obs_ids = op.get("observation_ids")
    if not isinstance(raw_obs_ids, list) or not raw_obs_ids:
        return False, "missing_observation_ids", None
    obs_ids: list[str] = []
    cited_obs: list[dict[str, Any]] = []
    for raw_id in raw_obs_ids:
        if not isinstance(raw_id, str) or not raw_id.strip():
            return False, "unknown_observation_id", None
        observation_id = raw_id.strip()
        observation = obs_map.get(observation_id)
        if not isinstance(observation, dict):
            return False, "unknown_observation_id", None
        if observation_id not in obs_ids:
            obs_ids.append(observation_id)
            cited_obs.append(observation)

    if action == "noop":
        return True, "ok", {
            "action": "noop", "observation_ids": obs_ids,
            "target_entry_id": None, "expected_entry_revision": None,
        }

    required_mode = "replaced_or_sold" if action == "retract" else "owned_or_habitual"
    eligible = [observation for observation in cited_obs
                if observation.get("ownership_mode") == required_mode]
    if not eligible:
        reason = ("retract_requires_replaced_or_sold_observation" if action == "retract"
                  else "upsert_requires_owned_or_habitual_observation")
        return False, reason, None

    # Prefer the latest accepted owner statement, never model/dispatch time.
    newest_seq = max(int(observation.get("source_seq") or 0) for observation in eligible)
    newest = [observation for observation in eligible
              if int(observation.get("source_seq") or 0) == newest_seq]
    primary = newest[0]
    try:
        scope = str(primary.get("scope") or "").strip()
        kind = str(primary.get("kind") or "").strip()
        category = memory.normalize_category(primary.get("category") or "general")
        text = memory._clean_text(primary.get("text"), field="text", max_chars=memory.MAX_FACT_TEXT_CHARS)
        fact_key = memory.normalize_fact_key(primary.get("fact_key"), category=category, text=text)
        evidence_span = memory._clean_text(primary.get("evidence_span"), field="text", max_chars=memory.MAX_FACT_TEXT_CHARS)
    except AgentError as exc:
        return False, str(exc), None
    if scope not in {"personal", "project"} or kind not in VALID_OBSERVATION_KINDS:
        return False, "invalid_observation_authority", None
    job_project_id = str(job.get("project_id") or "").strip() or None
    project_id = job_project_id if scope == "project" else None
    if scope == "project" and (not project_id or primary.get("project_id") != project_id):
        return False, "invalid_observation_project", None

    # Supporting retired/current witnesses can describe a replacement of this
    # slot, but another slot cannot lend value, sources or retraction authority.
    for observation in cited_obs:
        if (observation.get("scope") != scope
                or observation.get("project_id") != project_id
                or observation.get("fact_key") != fact_key):
            return False, "observation_slot_mismatch", None
    for observation in newest:
        if (_normalize_ws(str(observation.get("text") or "")) != _normalize_ws(text)
                or observation.get("category") != category
                or observation.get("kind") != kind
                or observation.get("evidence_span") != evidence_span
                or set(observation.get("source_message_ids") or []) != set(primary.get("source_message_ids") or [])):
            return False, "ambiguous_observation_authority", None

    # Retain the existing wire fields for compatibility, but none may override
    # the trusted witness. Ordinary language paraphrases are resolved in Stage 1.
    expected_strings = {"kind": kind, "category": category, "fact_key": fact_key,
                        "text": text, "evidence_span": evidence_span}
    for field, expected in expected_strings.items():
        if field not in op:
            continue
        supplied = op[field]
        if not isinstance(supplied, str):
            return False, f"operation_{field}_not_in_observation", None
        try:
            if field == "category":
                matches = memory.normalize_category(supplied) == expected
            elif field == "fact_key":
                matches = memory.normalize_fact_key(supplied, category=category, text=text) == expected
            elif field == "text":
                matches = _normalize_ws(supplied) == _normalize_ws(expected)
            else:
                matches = supplied.strip() == expected
        except AgentError:
            matches = False
        if not matches:
            return False, f"operation_{field}_not_in_observation", None
    if "scope" in op:
        supplied_scope = op["scope"]
        if isinstance(supplied_scope, dict):
            if (supplied_scope.get("kind") != scope
                    or supplied_scope.get("owner_id", "local_owner") != "local_owner"
                    or (scope == "project" and supplied_scope.get("project_id") != project_id)
                    or (scope == "project" and supplied_scope.get("project_generation", job.get("project_generation")) != job.get("project_generation"))):
                return False, "operation_scope_not_in_observation", None
        elif supplied_scope != scope:
            return False, "operation_scope_not_in_observation", None
    if "project_id" in op and op["project_id"] != project_id:
        return False, "operation_project_not_in_observation", None

    source_ids = list(primary.get("source_message_ids") or [])
    if not source_ids or any(not isinstance(mid, str) or not mid for mid in source_ids):
        return False, "missing_observation_sources", None
    if "source_message_ids" in op:
        supplied_ids = op["source_message_ids"]
        if (not isinstance(supplied_ids, list)
                or any(not isinstance(mid, str) for mid in supplied_ids)
                or set(supplied_ids) != set(source_ids)):
            return False, "operation_sources_not_in_observation", None
    manifest = {ref["message_id"]: ref for ref in (job.get("source_manifest") or [])
                if isinstance(ref, dict) and isinstance(ref.get("message_id"), str)}
    matched_refs = []
    for message_id in source_ids:
        ref = manifest.get(message_id)
        if ref is None:
            return False, "ungrounded_source_message_id", None
        source_text = str(ref.get("text") if ref.get("text") is not None else (ref.get("excerpt") or ""))
        if not _evidence_matches_unquoted_text(evidence_span, strip_quoted_or_fenced_content(source_text)):
            return False, "evidence_span_not_in_source", None
        matched_refs.append(ref)
    source_seq = max(int(ref.get("source_seq") or 0) for ref in matched_refs)
    if source_seq != int(primary.get("source_seq") or 0):
        return False, "observation_source_order_mismatch", None

    raw_target = op.get("target_entry_id")
    if raw_target is not None and not isinstance(raw_target, str):
        return False, "invalid_target_entry_id", None
    target_entry_id = raw_target.strip() or None if isinstance(raw_target, str) else None
    raw_revision = op.get("expected_entry_revision")
    if action in {"update", "retract"}:
        if not target_entry_id:
            return False, "missing_target_entry_id", None
        if type(raw_revision) is not int or raw_revision <= 0:
            return False, "invalid_expected_entry_revision", None
        expected_revision = raw_revision
        if existing_map is None:
            return False, "missing_permitted_snapshot", None
        target = existing_map.get(target_entry_id)
        if not isinstance(target, dict):
            return False, "target_entry_not_in_permitted_snapshot", None
        if int(target.get("revision") or 1) != expected_revision:
            return False, "snapshot_revision_mismatch", None
        target_scope = target.get("scope")
        if isinstance(target_scope, dict):
            target_scope_kind = target_scope.get("kind")
            target_project = target_scope.get("project_id")
            target_generation = target_scope.get("project_generation")
        else:
            target_scope_kind = target_scope
            target_project = target.get("project_id")
            target_generation = target.get("project_generation")
        if (target_scope_kind != scope
                or (scope == "project" and (target_project != project_id
                    or target_generation != job.get("project_generation")))):
            return False, "target_scope_mismatch", None
        try:
            target_key = memory.normalize_fact_key(target.get("fact_key"), category=target.get("category") or "general", text=target.get("text") or "slot")
        except AgentError:
            return False, "target_slot_mismatch", None
        if target_key != fact_key:
            return False, "target_slot_mismatch", None
    else:
        if target_entry_id is not None or raw_revision is not None:
            return False, "add_cannot_specify_target_authority", None
        expected_revision = None
        if existing_map is not None and any(
            target.get("fact_key") == fact_key and target.get("scope") == scope
            for target in existing_map.values() if isinstance(target, dict)
        ):
            return False, "add_slot_already_in_permitted_snapshot", None

    return True, "ok", {
        "action": action, "observation_ids": obs_ids,
        "target_entry_id": target_entry_id, "expected_entry_revision": expected_revision,
        "scope": scope, "project_id": project_id, "kind": kind,
        "category": category, "fact_key": fact_key, "text": text,
        "evidence_span": evidence_span, "source_message_ids": source_ids,
        "source_seq": source_seq,
    }


def parse_phase2_consolidation_response(
    raw_content: str,
    job: dict[str, Any],
    observations: list[dict[str, Any]],
    existing_memories: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Parse and validate Stage-2 consolidation operations against Stage-1 observations and existing_memories."""
    parsed = _extract_json_object(raw_content, error_code="invalid_consolidation_json")
    raw_ops = parsed.get("operations")
    if not isinstance(raw_ops, list):
        raise ValueError("invalid_consolidation_json")

    obs_map: dict[str, dict[str, Any]] = {}
    for idx, obs in enumerate(observations or [], start=1):
        if not isinstance(obs, dict):
            continue
        item_copy = dict(obs)
        if not str(item_copy.get("observation_id") or "").strip():
            item_copy["observation_id"] = f"obs_{idx}"
        ok, _reason, norm_obs = verify_natural_observation_grounding(item_copy, job)
        if not ok or norm_obs is None:
            continue
        oid = str(norm_obs.get("observation_id") or f"obs_{idx}")
        obs_map[oid] = norm_obs

    existing_map: dict[str, dict[str, Any]] | None = None
    if existing_memories is not None:
        existing_map = {
            str(e["id"]): e
            for e in existing_memories
            if isinstance(e, dict) and e.get("id")
        }

    validated_ops: list[dict[str, Any]] = []
    for raw_op in raw_ops:
        if not isinstance(raw_op, dict):
            raise ValueError("invalid_consolidation_json")
        ok, _reason, norm_op = _validate_single_consolidation_op(
            raw_op,
            job,
            obs_map,
            existing_map,
        )
        if ok and norm_op is not None:
            validated_ops.append(norm_op)
    return validated_ops


# ---------------------------------------------------------------------------
# Atomic Commit & Scoped Tombstone / Revision / Causal-Order Enforcement
# ---------------------------------------------------------------------------


def _matching_tombstones_for_slot(
    mem: dict[str, Any],
    job_scope: dict[str, Any],
    fact_key: str,
    text: str,
) -> list[dict[str, Any]]:
    """Return only tombstones matching the target scope and either fact_key HMAC or text HMAC."""
    tombstones = mem.get("tombstones")
    if not isinstance(tombstones, dict):
        return []
    fk_hmac = memory._slot_hmac(mem, job_scope, fact_key, mutate=False) if fact_key else ""
    txt_hmac = memory._slot_hmac(mem, job_scope, text, mutate=False) if text else ""
    matched: list[dict[str, Any]] = []
    for tomb in tombstones.values():
        if not isinstance(tomb, dict):
            continue
        if tomb.get("suppression_mode") != "block_background_relearn_until_explicit_owner_restore":
            continue
        tomb_scope = tomb.get("scope") if isinstance(tomb.get("scope"), dict) else {}
        if not memory._scopes_equal(tomb_scope, job_scope):
            continue
        tomb_fk = str(tomb.get("fact_key_hmac") or "")
        tomb_aliases = tomb.get("aliases_hmac") if isinstance(tomb.get("aliases_hmac"), list) else []
        if (fk_hmac and (tomb_fk == fk_hmac or fk_hmac in tomb_aliases)) or (txt_hmac and txt_hmac in tomb_aliases):
            matched.append(tomb)
    return matched


def commit_natural_consolidation_job(
    data: dict[str, Any],
    *,
    job_id: str,
    worker_id: str,
    lease_token: str | None = None,
    phase1_mode: str = "observations",
    phase1_items: list[dict[str, Any]] | None = None,
    phase2_operations: list[dict[str, Any]] | None = None,
    permitted_snapshot: list[dict[str, Any]] | None = None,
    tokens_used: int = 0,
    calls_used: int = 1,
    usage_accounted: bool = False,
) -> dict[str, Any]:
    """Commit a 2-stage natural consolidation job (or delegate owner_review mode to v1 candidate queue)."""
    mem_preview = memory.read_memory_state(data)
    job_preview = (mem_preview.get("jobs") or {}).get(str(job_id or ""))
    if not isinstance(job_preview, dict):
        raise AgentError("agent_memory_job_not_found")
    activation_policy = str(job_preview.get("activation_policy") or "owner_review")

    # Legacy candidates or owner_review policy -> delegate to v1 candidate queue commit
    if phase1_mode == "legacy_candidates" or activation_policy != "automatic":
        proposals: list[dict[str, Any]] = []
        if phase1_mode == "legacy_candidates":
            proposals = [c for c in (phase1_items or []) if isinstance(c, dict)]
        else:
            for item in phase1_items or []:
                if not isinstance(item, dict):
                    continue
                ok, _r, norm_obs = verify_natural_observation_grounding(item, job_preview)
                if not ok or norm_obs is None:
                    continue
                if norm_obs.get("ownership_mode") != "owned_or_habitual":
                    continue
                proposals.append({
                    "kind": norm_obs["kind"],
                    "scope": {
                        "kind": norm_obs["scope"],
                        "project_id": norm_obs.get("project_id"),
                        "project_generation": job_preview.get("project_generation") if norm_obs["scope"] == "project" else None,
                    },
                    "category": norm_obs["category"],
                    "fact_key": norm_obs["fact_key"],
                    "text": norm_obs["text"],
                    "source_message_ids": norm_obs["source_message_ids"],
                })
        res = memory.commit_extraction_job(
            data,
            job_id=job_id,
            worker_id=worker_id,
            proposals=proposals,
            lease_token=lease_token,
            tokens_used=tokens_used,
        )
        if calls_used > 1 and res.get("state") in {"committed", "no_op"}:
            with memory._atomic_mutation(data) as mem:
                usage = mem.get("daily_usage")
                if isinstance(usage, dict):
                    usage["extraction_calls"] = int(usage.get("extraction_calls") or 0) + (int(calls_used) - 1)
        return res

    # Automatic mode: full 2-stage consolidation with CAS, causal ordering, and fail-closed exposure
    from . import memory_integration, memory_runtime

    with memory._atomic_mutation(data) as mem:
        stamp_all_tasks_owner_messages_in_tx(data)

        now = memory._now()
        job = mem["jobs"].get(job_id)
        if not isinstance(job, dict):
            raise AgentError("agent_memory_job_not_found")
        if job.get("state") in {"committed", "no_op", "stale", "failed"}:
            return {
                "state": job["state"],
                "job": copy.deepcopy(job),
                "activated_entry_ids": list(job.get("activated_entry_ids") or []),
                "updated_entry_ids": list(job.get("updated_entry_ids") or []),
                "retracted_entry_ids": list(job.get("retracted_entry_ids") or []),
                "committed_candidates": [],
                "skipped_reasons": list(job.get("skipped_reasons") or []),
            }
        if job.get("lease_owner") != worker_id:
            raise AgentError("agent_memory_job_lease_lost")
        if not isinstance(lease_token, str) or not lease_token.strip() or job.get("lease_token") != lease_token:
            raise AgentError("agent_memory_job_lease_lost")
        if float(job.get("lease_until") or 0.0) < now:
            job["state"] = "stale"
            job["error_code"] = "lease_expired"
            job["updated_at"] = now
            return {
                "state": "stale",
                "job": copy.deepcopy(job),
                "activated_entry_ids": [],
                "updated_entry_ids": [],
                "retracted_entry_ids": [],
                "committed_candidates": [],
                "skipped_reasons": ["lease_expired"],
            }

        # 1. Verify reset_epoch, project generation, and live source_manifest validity
        if job.get("reset_epoch") != mem["reset_epoch"]:
            job["state"] = "stale"
            job["error_code"] = "reset_epoch_mismatch"
            job["updated_at"] = now
            return {
                "state": "stale",
                "job": copy.deepcopy(job),
                "activated_entry_ids": [],
                "updated_entry_ids": [],
                "retracted_entry_ids": [],
                "committed_candidates": [],
                "skipped_reasons": ["reset_epoch_mismatch"],
            }

        live_row = (data.get("tasks") or {}).get(job.get("task_id"))
        if isinstance(live_row, dict) and job.get("project_id"):
            live_proj = live_row.get("project") if isinstance(live_row.get("project"), dict) else {}
            if live_proj.get("id") != job.get("project_id") or live_proj.get("generation") != job.get("project_generation"):
                job["state"] = "stale"
                job["error_code"] = "project_generation_changed"
                job["updated_at"] = now
                return {
                    "state": "stale",
                    "job": copy.deepcopy(job),
                    "activated_entry_ids": [],
                    "updated_entry_ids": [],
                    "retracted_entry_ids": [],
                    "committed_candidates": [],
                    "skipped_reasons": ["project_generation_changed"],
                }

        source_manifest = job.get("source_manifest") if isinstance(job.get("source_manifest"), list) else []
        for src in source_manifest:
            ok, reason = memory.verify_source_ref_against_state(data, src, for_background_job=True)
            if not ok:
                job["state"] = "stale"
                job["error_code"] = reason or "source_invalidated"
                job["updated_at"] = now
                return {
                    "state": "stale",
                    "job": copy.deepcopy(job),
                    "activated_entry_ids": [],
                    "updated_entry_ids": [],
                    "retracted_entry_ids": [],
                    "committed_candidates": [],
                    "skipped_reasons": [reason or "source_invalidated"],
                }

        # The registered worker can settle each stage on its dispatch day.
        job["tokens_used"] = max(0, int(tokens_used or 0))
        job["calls_used"] = max(1, int(calls_used or 1))
        if not usage_accounted:
            # 2. Record daily usage for dispatched calls
            day = time.strftime("%Y-%m-%d", time.gmtime(now))
            usage = mem["daily_usage"]
            if usage.get("day") != day:
                usage["day"] = day
                usage["extraction_calls"] = 0
                usage["extraction_tokens"] = 0
            usage["extraction_calls"] = int(usage.get("extraction_calls") or 0) + max(1, int(calls_used or 1))
            usage["extraction_tokens"] = int(usage.get("extraction_tokens") or 0) + max(0, int(tokens_used or 0))
        # 3. Re-verify Stage-1 observations
        obs_map: dict[str, dict[str, Any]] = {}
        for idx, raw_obs in enumerate(phase1_items or [], start=1):
            if not isinstance(raw_obs, dict):
                continue
            item_copy = dict(raw_obs)
            if not str(item_copy.get("observation_id") or "").strip():
                item_copy["observation_id"] = f"obs_{idx}"
            ok, _reason, norm_obs = verify_natural_observation_grounding(item_copy, job)
            if ok and norm_obs is not None:
                obs_map[str(norm_obs["observation_id"])] = norm_obs

        # 4. Build live destination-authorized snapshot and intersect with permitted_snapshot if supplied
        live_permitted = select_existing_memories_for_consolidation(
            data,
            job,
            job.get("destination_binding"),
        )
        live_permitted_map = {str(e["id"]): e for e in live_permitted if isinstance(e, dict) and e.get("id")}
        if permitted_snapshot is not None:
            snap_ids = {
                str(e["id"]): e
                for e in permitted_snapshot
                if isinstance(e, dict) and e.get("id")
            }
        else:
            snap_ids = {}  # A live scan cannot replace the actual dispatch snapshot.

        manifest_by_id = {
            str(r["message_id"]): r
            for r in source_manifest
            if isinstance(r, dict) and r.get("message_id")
        }

        activated_entry_ids: list[str] = []
        updated_entry_ids: list[str] = []
        retracted_entry_ids: list[str] = []
        skipped_reasons: list[str] = []

        for raw_op in (phase2_operations or [])[:16]:
            ok, reason, norm_op = _validate_single_consolidation_op(
                raw_op,
                job,
                obs_map,
                snap_ids,
            )
            if not ok or norm_op is None:
                skipped_reasons.append(reason)
                continue

            action = str(norm_op["action"])
            if action == "noop":
                continue

            op_scope_kind = str(norm_op["scope"])
            op_project_id = norm_op.get("project_id")
            job_scope = memory.normalize_scope(
                {
                    "kind": op_scope_kind,
                    "owner_id": "local_owner",
                    "project_id": op_project_id if op_scope_kind == "project" else None,
                    "project_generation": job.get("project_generation") if op_scope_kind == "project" else None,
                }
            )

            cited_msg_ids = norm_op["source_message_ids"]
            matched_refs = [manifest_by_id[mid] for mid in cited_msg_ids if mid in manifest_by_id]
            if not matched_refs:
                skipped_reasons.append("no_grounded_source_messages")
                continue

            op_fact_key = str(norm_op["fact_key"])
            op_source_seq = int(norm_op["source_seq"])
            target_id = norm_op.get("target_entry_id")
            expected_rev = norm_op.get("expected_entry_revision")

            if action == "retract":
                if not target_id or target_id not in live_permitted_map:
                    skipped_reasons.append("target_entry_not_permitted")
                    continue
                live_entry = mem["entries"].get(target_id)
                if not isinstance(live_entry, dict) or live_entry.get("status") != "active":
                    skipped_reasons.append("target_entry_not_active")
                    continue
                if int(live_entry.get("revision") or 1) != int(expected_rev or 0):
                    skipped_reasons.append("active_entry_revision_changed")
                    continue
                entry_barrier_seq = max(
                    int(live_entry.get("last_source_seq") or 0),
                    int(live_entry.get("owner_acceptance_seq") or 0),
                )
                if op_source_seq <= entry_barrier_seq:
                    skipped_reasons.append("stale_source_order")
                    continue

                memory.delete_memory_entry(
                    data,
                    target_id,
                    expected_entry_revision=int(expected_rev),
                    reason="natural_retraction",
                )
                if target_id in mem.get("tombstones", {}):
                    mem["tombstones"][target_id]["deleted_at_source_seq"] = op_source_seq
                retracted_entry_ids.append(target_id)
                live_permitted_map.pop(target_id, None)
                continue

            # action in {"add", "update"}
            matched_entry: dict[str, Any] | None = None
            if action == "update":
                if not target_id or target_id not in live_permitted_map:
                    skipped_reasons.append("target_entry_not_permitted")
                    continue
                cand_entry = mem["entries"].get(target_id)
                if not isinstance(cand_entry, dict) or cand_entry.get("status") != "active":
                    skipped_reasons.append("target_entry_not_active")
                    continue
                if int(cand_entry.get("revision") or 1) != int(expected_rev or 0):
                    skipped_reasons.append("active_entry_revision_changed")
                    continue
                matched_entry = cand_entry
            else:
                # action == "add": verify no active entry already occupies this (scope, fact_key)
                for existing_entry in mem["entries"].values():
                    if not isinstance(existing_entry, dict) or existing_entry.get("status") != "active":
                        continue
                    if memory._scopes_equal(existing_entry.get("scope") or {}, job_scope) and str(
                        existing_entry.get("fact_key") or ""
                    ) == op_fact_key:
                        matched_entry = existing_entry
                        break
                if matched_entry is not None:
                    skipped_reasons.append("active_entry_revision_changed")
                    continue

            if matched_entry is not None:
                entry_barrier_seq = max(
                    int(matched_entry.get("last_source_seq") or 0),
                    int(matched_entry.get("owner_acceptance_seq") or 0),
                )
                if op_source_seq <= entry_barrier_seq:
                    skipped_reasons.append("stale_source_order")
                    continue

            # Scoped tombstone check for (job_scope, op_fact_key, op_text)
            op_text = str(norm_op["text"])
            matching_tombs = _matching_tombstones_for_slot(mem, job_scope, op_fact_key, op_text)
            blocked_by_tombstone = False
            for tomb in matching_tombs:
                tomb_seq = int(tomb.get("deleted_at_source_seq") or 0)
                if op_source_seq <= tomb_seq:
                    blocked_by_tombstone = True
                    break
            if blocked_by_tombstone:
                skipped_reasons.append("tombstone_suppressed")
                continue

            # Build grounded source refs carrying source_seq
            built_sources: list[dict[str, Any]] = []
            for r in matched_refs:
                sref = {k: v for k, v in r.items() if k != "text"}
                sref["excerpt"] = str(norm_op["evidence_span"])[: memory.MAX_EXCERPT_CHARS]
                sref["source_seq"] = int(r.get("source_seq") or op_source_seq)
                built_sources.append(sref)

            # Fail-closed default exposure for new entries; preserve exposure_policy on updates
            if matched_entry is not None:
                entry_exposure = copy.deepcopy(
                    matched_entry.get("exposure_policy") or {"mode": "local_only", "allowed_destinations": []}
                )
            else:
                entry_exposure = memory_runtime._derive_new_explicit_entry_exposure_policy(data)

            upsert_payload: dict[str, Any] = {
                "scope": job_scope,
                "kind": str(norm_op["kind"]),
                "category": str(norm_op["category"]),
                "fact_key": op_fact_key,
                "text": op_text,
                "sources": built_sources,
                "exposure_policy": entry_exposure,
                "last_source_seq": op_source_seq,
                "keywords": derive_semantic_keywords(op_fact_key, str(norm_op["category"])),
                "summary": build_entry_summary(op_fact_key, op_text),
            }
            if matched_entry is not None:
                upsert_payload["id"] = str(matched_entry["id"])
                # A content update inherits the existing grant verbatim. Passing
                # it as an edited policy would rotate its authorization revision.
                upsert_payload.pop("exposure_policy", None)

            upserted = memory.upsert_active_entry(
                data,
                upsert_payload,
                origin="automatic_consolidation",
                expected_entry_revision=int(expected_rev) if matched_entry is not None and expected_rev is not None else None,
            )
            eid = str(upserted["entry"]["id"])
            if action == "update":
                updated_entry_ids.append(eid)
            else:
                activated_entry_ids.append(eid)

        has_changes = bool(activated_entry_ids or updated_entry_ids or retracted_entry_ids)
        job["output_hash"] = digest(phase2_operations or [])
        job["activated_entry_ids"] = list(activated_entry_ids)
        job["updated_entry_ids"] = list(updated_entry_ids)
        job["retracted_entry_ids"] = list(retracted_entry_ids)
        job["skipped_reasons"] = list(skipped_reasons)
        job["updated_at"] = now

        if has_changes:
            job["state"] = "committed"
            memory_integration.propagate_memory_revocation_in_tx(data)
        elif skipped_reasons and all(
            r in {"tombstone_suppressed", "active_entry_revision_changed", "stale_source_order"}
            for r in skipped_reasons
        ):
            job["state"] = "stale"
            job["error_code"] = skipped_reasons[0]
        else:
            job["state"] = "no_op"
            if skipped_reasons:
                job["error_code"] = skipped_reasons[0]

        mem["generation"] += 1
        memory._enforce_memory_bounds(mem)

        return {
            "state": job["state"],
            "job": copy.deepcopy(job),
            "activated_entry_ids": list(activated_entry_ids),
            "updated_entry_ids": list(updated_entry_ids),
            "retracted_entry_ids": list(retracted_entry_ids),
            "committed_candidates": [],
            "skipped_reasons": list(skipped_reasons),
        }
