"""Host-owned bounded, versioned long-term memory state and transactional governance."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import time
import uuid
from typing import Any

from .protocol import digest, sanitized
from .store import AgentError, encoded

SCHEMA_VERSION = 1
MAX_MEMORY_BYTES = 262144
MAX_MEMORY_TOKENS = 2048
MAX_INJECTED_BYTES = 6144
MAX_ENTRIES = 128
MAX_CANDIDATES = 64
MAX_JOBS = 64
MAX_TOMBSTONES = 256
MAX_FENCED_SOURCES = 512
MAX_CONTROL_REQUESTS = 128
MAX_FACT_TEXT_CHARS = 1000
MAX_FACT_KEY_CHARS = 160
MAX_CATEGORY_CHARS = 64
MAX_EXCERPT_CHARS = 240
MAX_SOURCES_PER_ITEM = 8
DEFAULT_DAILY_EXTRACTION_CALLS = 32
DEFAULT_DAILY_EXTRACTION_TOKENS = 65536
DEFAULT_LEASE_SECONDS = 60.0

VALID_SCOPE_KINDS = {"personal", "project"}
VALID_ENTRY_KINDS = {"owner_fact", "owner_preference", "project_fact", "reusable_lesson"}
VALID_STATUSES = {"active", "superseded", "deleted"}
VALID_CANDIDATE_STATUSES = {"pending", "accepted", "rejected", "stale"}
VALID_ORIGINS = {"manual", "explicit_owner_chat", "accepted_extraction", "automatic_consolidation"}
VALID_ACTIVATION_POLICIES = {"owner_review", "automatic"}
VALID_EXPOSURE_MODES = {"all_selected", "destinations", "local_only"}
VALID_DEFAULT_EXPOSURE_MODES = {"destination", "selected_models", "restricted"}
VALID_JOB_STATES = {"queued", "leased", "extracted", "committed", "no_op", "stale", "failed"}

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "what", "when", "where", "which",
    "who", "whom", "whose", "why", "how", "are", "was", "were", "been", "being", "have",
    "has", "had", "having", "does", "did", "doing", "will", "would", "shall", "should",
    "can", "could", "may", "might", "must", "need", "use", "using", "used", "please",
    "help", "give", "recommend", "tell", "about", "into", "onto", "upon", "over", "under",
    "我的", "我们", "一个", "一些", "什么", "怎么", "如何", "可以", "请问", "帮我", "推荐", "现有", "目前", "使用", "根据", "按照",
}

_CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "photography": ("摄影", "拍照", "拍摄", "人像", "风光", "街拍", "人文", "旅行", "快门", "光圈", "iso", "photo", "photography", "camera", "lens", "portrait", "street", "shoot"),
    "equipment": ("器材", "设备", "机身", "镜头", "相机", "三脚架", "脚架", "硬件", "camera", "lens", "body", "gear", "equipment", "device", "hardware", "setup", "kit"),
    "gear": ("器材", "设备", "机身", "镜头", "相机", "camera", "lens", "body", "gear", "equipment", "setup", "kit"),
    "hobby": ("爱好", "喜欢", "兴趣", "摄影", "拍照", "hobby", "interest", "like", "photography"),
    "workflow": ("工作流", "习惯", "流程", "代码", "测试", "workflow", "process", "habit", "testing"),
    "project": ("项目", "仓库", "架构", "模块", "构建", "project", "repo", "architecture", "module", "build"),
}

_SECRET_PATTERN = re.compile(
    r"(?i)(?:\bsk-[A-Za-z0-9_-]{12,}\b|\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b|"
    r"\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|private[_-]?key|password|passwd)\s*[:=]\s*[^\s,;]{6,})"
)


def _now() -> float:
    return round(time.time(), 6)


def _clean_text(value: Any, *, field: str, max_chars: int = MAX_FACT_TEXT_CHARS, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise AgentError(f"agent_memory_invalid_{field}")
    cleaned = sanitized(value).strip()
    if not allow_empty and not cleaned:
        raise AgentError(f"agent_memory_invalid_{field}")
    if len(cleaned) > max_chars:
        raise AgentError(f"agent_memory_{field}_too_long")
    if _SECRET_PATTERN.search(cleaned):
        raise AgentError("agent_memory_secret_prohibited")
    return cleaned


def normalize_fact_key(raw_key: Any, *, category: str = "general", text: str = "") -> str:
    if isinstance(raw_key, str) and raw_key.strip():
        candidate = raw_key.strip().lower()
        candidate = re.sub(r"[^a-z0-9._:/\-\u4e00-\u9fff]+", "_", candidate).strip("._-")
        if candidate and len(candidate) <= MAX_FACT_KEY_CHARS:
            return candidate
    base = _clean_text(text, field="text", max_chars=MAX_FACT_TEXT_CHARS) if text else "fact"
    cat = re.sub(r"[^a-z0-9_\-\u4e00-\u9fff]+", "_", (category or "general").strip().lower()).strip("_") or "general"
    short_hash = hashlib.sha256(base.lower().encode("utf-8")).hexdigest()[:12]
    return f"{cat}.{short_hash}"


def normalize_category(raw_category: Any) -> str:
    if not isinstance(raw_category, str) or not raw_category.strip():
        return "general"
    cleaned = re.sub(r"[^a-z0-9._\-\u4e00-\u9fff]+", "_", raw_category.strip().lower()).strip("._-")
    if not cleaned:
        return "general"
    if len(cleaned) > MAX_CATEGORY_CHARS:
        raise AgentError("agent_memory_category_too_long")
    return cleaned


def _hmac_key(memory_state: dict, *, mutate: bool = True) -> bytes:
    salt = memory_state.get("hmac_salt")
    if not isinstance(salt, str) or len(salt) < 32:
        if not mutate:
            return b"ryn-memory-uninitialized-readonly-salt"
        salt = uuid.uuid4().hex + uuid.uuid4().hex
        memory_state["hmac_salt"] = salt
    return salt.encode("utf-8")


def _slot_hmac(memory_state: dict, scope: dict, slot_text: str, *, mutate: bool = True) -> str:
    gen_part = f":{scope.get('project_generation')}" if scope.get("project_generation") is not None else ""
    scope_prefix = f"{scope.get('kind', 'personal')}:{scope.get('owner_id', 'local_owner')}:{scope.get('project_id') or ''}{gen_part}"
    normalized = re.sub(r"\s+", " ", (slot_text or "").strip().lower())
    payload = f"{scope_prefix}|{normalized}".encode("utf-8")
    return hmac.new(_hmac_key(memory_state, mutate=mutate), payload, hashlib.sha256).hexdigest()


def normalize_destination(destination: Any) -> dict[str, str] | None:
    if destination is None:
        return None
    if not isinstance(destination, dict):
        raise AgentError("agent_memory_invalid_destination")
    catalog_model_id = str(destination.get("catalog_model_id") or destination.get("model_id") or "").strip()
    if not catalog_model_id:
        raise AgentError("agent_memory_invalid_destination")
    return {
        "catalog_model_id": catalog_model_id[:256],
        "provider_peer_id": str(destination.get("provider_peer_id") or destination.get("peer_id") or "").strip()[:256],
        "source_id": str(destination.get("source_id") or "").strip()[:256],
        "destination_revision": str(destination.get("destination_revision") or "").strip()[:256],
    }


def destination_matches(allowed: dict[str, str], actual: dict[str, str] | None) -> bool:
    if not actual or not isinstance(allowed, dict) or not isinstance(actual, dict):
        return False
    if not allowed.get("catalog_model_id") or allowed.get("catalog_model_id") != actual.get("catalog_model_id"):
        return False
    for key in ("provider_peer_id", "destination_revision"):
        expected_val = str(allowed.get(key) or "").strip()
        actual_val = str(actual.get(key) or "").strip()
        if not expected_val or not actual_val or expected_val != actual_val:
            return False
    expected_source = str(allowed.get("source_id") if allowed.get("source_id") is not None else "").strip()
    actual_source = str(actual.get("source_id") if actual.get("source_id") is not None else "").strip()
    if expected_source != actual_source:
        return False
    return True


def normalize_scope(raw_scope: Any, *, default_owner_id: str = "local_owner") -> dict[str, Any]:
    if raw_scope is None:
        return {"kind": "personal", "owner_id": default_owner_id, "project_id": None, "project_generation": None}
    if isinstance(raw_scope, str):
        if raw_scope == "personal":
            return {"kind": "personal", "owner_id": default_owner_id, "project_id": None, "project_generation": None}
        raise AgentError("agent_memory_invalid_scope")
    if not isinstance(raw_scope, dict):
        raise AgentError("agent_memory_invalid_scope")
    kind = str(raw_scope.get("kind") or ("project" if raw_scope.get("project_id") else "personal")).strip()
    if kind not in VALID_SCOPE_KINDS:
        raise AgentError("agent_memory_invalid_scope")
    owner_id = str(raw_scope.get("owner_id") or default_owner_id).strip() or default_owner_id
    if kind == "personal":
        return {"kind": "personal", "owner_id": owner_id, "project_id": None, "project_generation": None}
    project_id = str(raw_scope.get("project_id") or "").strip()
    if not project_id:
        raise AgentError("agent_memory_project_id_required")
    gen = raw_scope.get("project_generation")
    if gen is not None and (type(gen) is not int or gen < 0):
        raise AgentError("agent_memory_invalid_scope")
    return {
        "kind": "project",
        "owner_id": owner_id,
        "project_id": project_id[:128],
        "project_generation": gen,
    }


def _project_scope_matches(
    scope: dict[str, Any],
    project_id: str | None,
    project_generation: int | None = None,
    *,
    require_generation_match: bool = True,
) -> bool:
    if not isinstance(scope, dict) or scope.get("kind") != "project":
        return False
    if not project_id or scope.get("project_id") != project_id:
        return False
    if not require_generation_match:
        return True
    s_gen = scope.get("project_generation")
    if s_gen is not None or project_generation is not None:
        return s_gen is not None and project_generation is not None and int(s_gen) == int(project_generation)
    return True


def _scopes_equal(scope_a: dict[str, Any] | None, scope_b: dict[str, Any] | None) -> bool:
    if not isinstance(scope_a, dict) or not isinstance(scope_b, dict):
        return False
    if scope_a.get("kind") != scope_b.get("kind"):
        return False
    if scope_a.get("owner_id", "local_owner") != scope_b.get("owner_id", "local_owner"):
        return False
    if scope_a.get("kind") == "project":
        return _project_scope_matches(
            scope_a,
            scope_b.get("project_id"),
            scope_b.get("project_generation"),
            require_generation_match=True,
        )
    return True


def normalize_exposure_policy(raw_policy: Any, *, default_destination: dict[str, str] | None = None, prev_revision: int = 0) -> dict[str, Any]:
    if raw_policy is None:
        if default_destination:
            return {
                "mode": "destinations",
                "allowed_destinations": [copy.deepcopy(default_destination)],
                "revision": prev_revision + 1,
            }
        return {
            "mode": "all_selected",
            "allowed_destinations": [],
            "revision": prev_revision + 1,
        }
    if not isinstance(raw_policy, dict):
        raise AgentError("agent_memory_invalid_exposure_policy")
    mode = str(raw_policy.get("mode") or "all_selected").strip()
    if mode not in VALID_EXPOSURE_MODES:
        raise AgentError("agent_memory_invalid_exposure_policy")
    raw_dests = raw_policy.get("allowed_destinations") or []
    if not isinstance(raw_dests, list) or len(raw_dests) > 32:
        raise AgentError("agent_memory_invalid_exposure_policy")
    dests: list[dict[str, str]] = []
    for item in raw_dests:
        norm = normalize_destination(item)
        if norm and not any(destination_matches(existing, norm) for existing in dests):
            dests.append(norm)
    if mode == "destinations" and not dests and default_destination:
        dests = [copy.deepcopy(default_destination)]
    return {
        "mode": mode,
        "allowed_destinations": dests,
        "revision": prev_revision + 1,
    }


def exposure_allows_destination(policy: dict[str, Any] | None, destination: dict[str, str] | None) -> bool:
    if not isinstance(policy, dict):
        return False
    mode = policy.get("mode", "local_only")
    if mode == "local_only":
        return False
    if mode == "all_selected":
        return destination is not None and bool(destination.get("catalog_model_id"))
    if mode == "destinations":
        if not destination:
            return False
        allowed = policy.get("allowed_destinations") or []
        return any(destination_matches(item, destination) for item in allowed)
    return False


def exposure_is_wider(candidate_policy: dict[str, Any], bound_destination: dict[str, str] | None) -> bool:
    mode = candidate_policy.get("mode")
    if mode == "local_only":
        return False
    if mode == "all_selected":
        return True
    if mode == "destinations":
        allowed = candidate_policy.get("allowed_destinations") or []
        if not bound_destination:
            return bool(allowed)
        return any(not destination_matches(bound_destination, item) for item in allowed)
    return True


def message_content_sha256(content: Any) -> str:
    if isinstance(content, str):
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
    return digest(content)


def build_source_ref(
    *,
    source_type: str = "task_message",
    task_id: str | None = None,
    message_id: str | None = None,
    conversation_revision: int = 0,
    context_epoch: int = 0,
    source_revision: int = 1,
    content: str = "",
    content_sha256: str | None = None,
    author_role: str = "owner",
    project_id: str | None = None,
    excerpt: str = "",
) -> dict[str, Any]:
    if author_role != "owner":
        raise AgentError("agent_memory_untrusted_source_role")
    if source_type not in {"manual_owner", "task_message"}:
        raise AgentError("agent_memory_invalid_source_type")
    if source_type == "task_message":
        if not isinstance(task_id, str) or not task_id.strip():
            raise AgentError("agent_memory_source_task_required")
        if not isinstance(message_id, str) or not message_id.strip():
            raise AgentError("agent_memory_source_message_required")
        if type(conversation_revision) is not int or conversation_revision < 0:
            raise AgentError("agent_memory_invalid_source_revision")
        if type(context_epoch) is not int or context_epoch < 0:
            raise AgentError("agent_memory_invalid_source_epoch")
    computed_hash = content_sha256 or message_content_sha256(content)
    if not isinstance(computed_hash, str) or len(computed_hash) != 64:
        raise AgentError("agent_memory_invalid_source_hash")
    short_excerpt = sanitized(excerpt or content).strip()[:MAX_EXCERPT_CHARS]
    return {
        "source_type": source_type,
        "task_id": task_id.strip() if isinstance(task_id, str) and task_id.strip() else None,
        "message_id": message_id.strip() if isinstance(message_id, str) and message_id.strip() else None,
        "conversation_revision": int(conversation_revision) if source_type == "task_message" else 0,
        "context_epoch": int(context_epoch) if source_type == "task_message" else 0,
        "source_revision": int(source_revision),
        "content_sha256": computed_hash,
        "author_role": "owner",
        "project_id": project_id.strip() if isinstance(project_id, str) and project_id.strip() else None,
        "excerpt": short_excerpt,
    }


def _default_settings() -> dict[str, Any]:
    return {
        "capture_enabled": False,
        "use_enabled": True,
        "activation_policy": "owner_review",
        "extraction_model_id": "",
        "extraction_destination": None,
        "default_exposure": {
            "mode": "selected_models",
            "allowed_destinations": [],
        },
        "budgets": {
            "max_memory_tokens": MAX_MEMORY_TOKENS,
            "max_memory_bytes": MAX_MEMORY_BYTES,
            "max_injected_bytes": MAX_INJECTED_BYTES,
            "max_entries": MAX_ENTRIES,
            "max_candidates": MAX_CANDIDATES,
            "max_tombstones": MAX_TOMBSTONES,
            "max_fenced_sources": MAX_FENCED_SOURCES,
            "max_daily_extraction_calls": DEFAULT_DAILY_EXTRACTION_CALLS,
            "max_daily_extraction_tokens": DEFAULT_DAILY_EXTRACTION_TOKENS,
        },
    }


def default_memory_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generation": 0,
        "reset_epoch": 0,
        "policy_revision": 0,
        "source_seq": 0,
        "consent_generation": 0,
        "automatic_capture_frontier_seq": 0,
        "hmac_salt": uuid.uuid4().hex + uuid.uuid4().hex,
        "settings": _default_settings(),
        "entries": {},
        "candidates": {},
        "jobs": {},
        "tombstones": {},
        "processed_sources": {},
        "source_fences": {
            "deleted_tasks": {},
            "archived_tasks": {},
            "retired_branches": [],
            "fenced_messages": {},
            "fenced_task_watermarks": {},
            "reset_task_boundaries": {},
            "relearn_tasks": {},
            "reset_watermark": {
                "reset_epoch": 0,
                "at": 0.0,
                "task_revisions": {},
            },
        },
        "manual_preference_exposure_overrides": {},
        "daily_usage": {
            "day": "",
            "extraction_calls": 0,
            "extraction_tokens": 0,
        },
        "control_requests": {},
    }


def _validate_memory_containers(mem: dict[str, Any]) -> None:
    version = mem.get("schema_version", SCHEMA_VERSION)
    if type(version) is not int or version != SCHEMA_VERSION:
        raise AgentError("agent_memory_unsupported_schema")
    for dict_key in ("entries", "candidates", "jobs", "tombstones", "source_fences", "settings"):
        if dict_key in mem and not isinstance(mem[dict_key], dict):
            raise AgentError("agent_memory_corrupted_state")


def read_memory_state(data: dict[str, Any]) -> dict[str, Any]:
    """Pure read-only projection of memory state. NEVER mutates `data` or cached store roots."""
    if not isinstance(data, dict) or "memory" not in data or data.get("memory") is None:
        return default_memory_state()
    raw = data.get("memory")
    if not isinstance(raw, dict):
        raise AgentError("agent_memory_corrupted_state")
    _validate_memory_containers(raw)
    defaults = default_memory_state()
    mem = copy.deepcopy(raw)
    for key, val in defaults.items():
        if key not in mem or not isinstance(mem[key], type(val)):
            mem[key] = copy.deepcopy(val)
    for key, val in defaults["settings"].items():
        if key not in mem["settings"]:
            mem["settings"][key] = copy.deepcopy(val)
    for key, val in defaults["settings"]["budgets"].items():
        if key not in mem["settings"]["budgets"]:
            mem["settings"]["budgets"][key] = val
    for key, val in defaults["source_fences"].items():
        if key not in mem["source_fences"] or not isinstance(mem["source_fences"][key], type(val)):
            mem["source_fences"][key] = copy.deepcopy(val)
    return mem


def ensure_memory_state(data: dict[str, Any]) -> dict[str, Any]:
    """Transaction-only initializer/migrator for `data['memory']`. Only call inside store.transaction."""
    if "memory" not in data or data.get("memory") is None:
        mem = default_memory_state()
        data["memory"] = mem
        return mem
    mem = data.get("memory")
    if not isinstance(mem, dict):
        raise AgentError("agent_memory_corrupted_state")
    _validate_memory_containers(mem)
    defaults = default_memory_state()
    for key, val in defaults.items():
        if key not in mem or not isinstance(mem[key], type(val)):
            mem[key] = copy.deepcopy(val)
    for key, val in defaults["settings"].items():
        if key not in mem["settings"]:
            mem["settings"][key] = copy.deepcopy(val)
    for key, val in defaults["settings"]["budgets"].items():
        if key not in mem["settings"]["budgets"]:
            mem["settings"]["budgets"][key] = val
    for key, val in defaults["source_fences"].items():
        if key not in mem["source_fences"] or not isinstance(mem["source_fences"][key], type(val)):
            mem["source_fences"][key] = copy.deepcopy(val)
    _hmac_key(mem, mutate=True)
    return mem


def _extract_fence_task_and_revision(fkey: str, meta: Any) -> tuple[str | None, int | None]:
    if isinstance(meta, dict):
        m_tid = meta.get("task_id")
        m_crev = meta.get("conversation_revision")
        if isinstance(m_tid, str) and m_tid.strip() and type(m_crev) is int:
            return m_tid.strip(), m_crev
    if not isinstance(fkey, str):
        return None, None
    rparts = fkey.rsplit(":", 2)
    if len(rparts) != 3:
        return None, None
    prefix, crev_str, _chash = rparts
    try:
        crev = int(crev_str)
    except ValueError:
        return None, None
    tid = prefix.split(":", 1)[0].strip()
    if not tid:
        return None, None
    return tid, crev


_SLOT_FENCE_REASONS = frozenset({"owner_deleted", "updated", "superseded", "candidate_rejected", "forgotten", "natural_retraction"})


def _compact_source_fences_into_watermarks(mem: dict[str, Any], max_fenced: int) -> None:
    """Fold older per-message/per-branch fences into monotonic per-task revision watermarks without losing protection."""
    fences = mem["source_fences"]
    watermarks = fences.setdefault("fenced_task_watermarks", {})
    relearn_watermarks = fences.setdefault("fenced_relearn_watermarks", {})
    fenced_msgs = fences.get("fenced_messages", {})
    if len(fenced_msgs) > max_fenced:
        ordered = sorted(
            fenced_msgs.items(),
            key=lambda item: float(item[1].get("at") or 0.0) if isinstance(item[1], dict) else 0.0,
        )
        while len(fenced_msgs) > max_fenced and ordered:
            fkey, meta = ordered.pop(0)
            tid, crev = _extract_fence_task_and_revision(fkey, meta)
            if tid is not None and crev is not None:
                reason = meta.get("reason") if isinstance(meta, dict) else "branch_edited"
                if reason in _SLOT_FENCE_REASONS:
                    relearn_watermarks[tid] = max(int(relearn_watermarks.get(tid, -1)), crev)
                else:
                    watermarks[tid] = max(int(watermarks.get(tid, -1)), crev)
                fenced_msgs.pop(fkey, None)

    retired_branches = fences.get("retired_branches", [])
    if len(retired_branches) > max_fenced:
        overflow = len(retired_branches) - max_fenced
        kept = []
        for branch in retired_branches[:overflow]:
            if isinstance(branch, dict) and branch.get("task_id") and type(branch.get("conversation_revision")) is int:
                tid = str(branch["task_id"])
                crev = int(branch["conversation_revision"])
                watermarks[tid] = max(int(watermarks.get(tid, -1)), crev)
            else:
                kept.append(branch)
        retired_branches[:overflow] = kept


def _enforce_memory_bounds(mem: dict[str, Any]) -> None:
    # Prune oldest terminal jobs and control requests first to keep state compact.
    jobs = mem.get("jobs", {})
    if len(jobs) > MAX_JOBS:
        terminal = [
            (jid, j.get("updated_at", 0.0))
            for jid, j in jobs.items()
            if j.get("state") in {"committed", "no_op", "stale", "failed"}
        ]
        terminal.sort(key=lambda item: item[1])
        while len(jobs) > MAX_JOBS and terminal:
            jid, _ = terminal.pop(0)
            jobs.pop(jid, None)
    if len(jobs) > MAX_JOBS:
        raise AgentError("agent_memory_job_limit")

    requests = mem.get("control_requests", {})
    while len(requests) > MAX_CONTROL_REQUESTS:
        oldest_key = next(iter(requests))
        requests.pop(oldest_key, None)

    processed = mem.get("processed_sources", {})
    if isinstance(processed, dict):
        while len(processed) > MAX_FENCED_SOURCES:
            oldest_ps = next(iter(processed))
            processed.pop(oldest_ps, None)

    # Security invariant: NEVER silently evict tombstones!
    tombstones = mem.get("tombstones", {})
    max_tombstones = int(mem["settings"]["budgets"].get("max_tombstones", MAX_TOMBSTONES))
    if len(tombstones) > max_tombstones:
        raise AgentError("agent_memory_tombstone_limit")

    # Security invariant: NEVER silently drop source fences without folding into monotonic task revision watermarks.
    max_fenced = int(mem["settings"]["budgets"].get("max_fenced_sources", MAX_FENCED_SOURCES))
    _compact_source_fences_into_watermarks(mem, max_fenced)
    if (
        len(mem["source_fences"].get("fenced_messages", {})) > max_fenced
        or len(mem["source_fences"].get("retired_branches", [])) > max_fenced
        or len(mem["source_fences"].get("fenced_task_watermarks", {})) > max_fenced * 2
    ):
        raise AgentError("agent_memory_fence_limit")

    # Purge non-active deleted entries from entries map (their tombstones remain in tombstones)
    entries = mem.get("entries", {})
    for eid in [k for k, v in entries.items() if v.get("status") == "deleted"]:
        entries.pop(eid, None)
    active_count = sum(1 for v in entries.values() if v.get("status") == "active")
    max_entries = int(mem["settings"]["budgets"].get("max_entries", MAX_ENTRIES))
    if active_count > max_entries:
        raise AgentError("agent_memory_entry_limit")
    if len(entries) > max_entries * 2:
        superseded = [
            (eid, e.get("updated_at", 0.0))
            for eid, e in entries.items()
            if e.get("status") == "superseded"
        ]
        superseded.sort(key=lambda item: item[1])
        while len(entries) > max_entries * 2 and superseded:
            eid, _ = superseded.pop(0)
            entries.pop(eid, None)
    candidates = mem.get("candidates", {})
    pending_count = sum(1 for c in candidates.values() if c.get("status") == "pending")
    max_candidates = int(mem["settings"]["budgets"].get("max_candidates", MAX_CANDIDATES))
    if pending_count > max_candidates:
        raise AgentError("agent_memory_candidate_limit")
    if len(candidates) > max_candidates * 2:
        resolved = [
            (cid, c.get("updated_at", 0.0))
            for cid, c in candidates.items()
            if c.get("status") != "pending"
        ]
        resolved.sort(key=lambda item: item[1])
        while len(candidates) > max_candidates * 2 and resolved:
            cid, _ = resolved.pop(0)
            candidates.pop(cid, None)
    max_bytes = int(mem["settings"]["budgets"].get("max_memory_bytes", MAX_MEMORY_BYTES))
    if len(encoded(mem)) > max_bytes:
        raise AgentError("agent_memory_byte_limit")


def _record_idempotency(
    mem: dict[str, Any],
    client_request_id: str | None,
    operation: str,
    payload: dict[str, Any],
) -> tuple[bool, Any]:
    if not client_request_id:
        return False, None
    if not isinstance(client_request_id, str) or len(client_request_id) > 128:
        raise AgentError("agent_memory_invalid_request_id")
    stable_payload = {
        k: v
        for k, v in payload.items()
        if k not in {"client_request_id", "expected_generation", "expected_revision", "expected_entry_revision", "expected_candidate_revision"}
    }
    fp = digest({"op": operation, "payload": stable_payload})
    prior = mem["control_requests"].get(client_request_id)
    if prior is not None:
        if prior.get("fingerprint") != fp:
            raise AgentError("agent_request_conflict")
        return True, copy.deepcopy(prior.get("result"))
    return False, fp


def _save_idempotency(mem: dict[str, Any], client_request_id: str | None, fp: str | None, result: Any) -> Any:
    if client_request_id and fp:
        mem["control_requests"][client_request_id] = {
            "fingerprint": fp,
            "result": copy.deepcopy(result),
            "at": _now(),
        }
    _enforce_memory_bounds(mem)
    return result


def _check_expected_generation(mem: dict[str, Any], expected_generation: int | None) -> None:
    if expected_generation is not None:
        if type(expected_generation) is not int or expected_generation != mem["generation"]:
            raise AgentError("agent_memory_generation_conflict")


def _message_fence_key(task_id: str, message_id: str, conversation_revision: int, content_sha256: str) -> str:
    return f"{task_id}:{message_id}:{conversation_revision}:{content_sha256}"


def verify_source_ref_against_state(
    data: dict[str, Any],
    source_ref: dict[str, Any],
    *,
    for_background_job: bool = False,
    for_active_entry: bool = False,
) -> tuple[bool, str | None]:
    """Verify that a source reference is authentic, current, and not fenced/retired. Pure read-only."""
    if not isinstance(source_ref, dict):
        return False, "invalid_source_ref"
    if source_ref.get("author_role") != "owner":
        return False, "untrusted_author_role"
    source_type = source_ref.get("source_type")
    if source_type == "manual_owner":
        return True, None
    if source_type != "task_message":
        return False, "invalid_source_type"
    task_id = source_ref.get("task_id")
    message_id = source_ref.get("message_id")
    conv_rev = source_ref.get("conversation_revision", 0)
    content_hash = source_ref.get("content_sha256", "")
    if not task_id or not message_id or not content_hash:
        return False, "incomplete_task_source"

    mem = read_memory_state(data)
    fences = mem["source_fences"]
    if task_id in fences.get("deleted_tasks", {}):
        return False, "source_task_deleted"

    fkey = _message_fence_key(task_id, message_id, conv_rev, content_hash)
    if fkey in fences.get("fenced_messages", {}):
        f_meta = fences["fenced_messages"][fkey]
        f_reason = f_meta.get("reason") if isinstance(f_meta, dict) else "branch_edited"
        if not (for_active_entry and f_reason in _SLOT_FENCE_REASONS):
            return False, "source_message_fenced"

    task_watermarks = fences.get("fenced_task_watermarks") or {}
    if task_id in task_watermarks and conv_rev <= int(task_watermarks[task_id]):
        return False, "source_task_watermark_fenced"

    if not for_active_entry:
        relearn_watermarks = fences.get("fenced_relearn_watermarks") or {}
        if task_id in relearn_watermarks and conv_rev <= int(relearn_watermarks[task_id]):
            return False, "source_task_watermark_fenced"

    for branch in fences.get("retired_branches", []):
        if branch.get("task_id") == task_id:
            if message_id in (branch.get("retired_message_ids") or []):
                return False, "source_branch_retired"
            if branch.get("conversation_revision") == conv_rev and branch.get("fenced_all"):
                return False, "source_revision_retired"

    tasks = data.get("tasks") or {}
    row = tasks.get(task_id)
    if not isinstance(row, dict):
        return False, "source_task_missing"

    relearn_epoch = int((fences.get("relearn_tasks") or {}).get(task_id, -1))
    reset_boundaries = fences.get("reset_task_boundaries") or {}
    if task_id in reset_boundaries and isinstance(reset_boundaries[task_id], dict):
        b_info = reset_boundaries[task_id]
        b_epoch = int(b_info.get("reset_epoch", 0))
        if relearn_epoch < b_epoch:
            b_crev = int(b_info.get("conversation_revision", 0))
            if conv_rev < b_crev:
                return False, "source_precedes_reset_watermark"
            if conv_rev == b_crev:
                if message_id in (b_info.get("fenced_message_ids") or []):
                    return False, "source_precedes_reset_watermark"
                b_count = int(b_info.get("message_count", 0))
                for idx, msg in enumerate(row.get("messages") or []):
                    if isinstance(msg, dict) and msg.get("message_id") == message_id:
                        if idx < b_count:
                            return False, "source_precedes_reset_watermark"
                        break

    if for_background_job:
        if row.get("archived") and task_id not in fences.get("relearn_tasks", {}):
            return False, "source_task_archived"
        watermark = fences.get("reset_watermark") or {}
        reset_revs = watermark.get("task_revisions") or {}
        if task_id not in reset_boundaries and task_id in reset_revs and conv_rev <= reset_revs[task_id]:
            if relearn_epoch < mem["reset_epoch"]:
                return False, "source_precedes_reset_watermark"

    # Check active branch messages in the task
    active_conv_rev = row.get("conversation_revision", 0)
    if not for_active_entry and active_conv_rev != conv_rev:
        return False, "source_conversation_revision_changed"

    matched_msg = None
    for msg in row.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("message_id") == message_id:
            matched_msg = msg
            break
    if matched_msg is None:
        return False, "source_message_not_in_active_branch"
    if matched_msg.get("role") != "user":
        return False, "source_message_not_owner_user"
    actual_text = matched_msg.get("ui", {}).get("text") if isinstance(matched_msg.get("ui"), dict) and "text" in matched_msg["ui"] else matched_msg.get("content", "")
    actual_hash = message_content_sha256(actual_text)
    if actual_hash != content_hash:
        return False, "source_message_hash_mismatch"

    return True, None


def is_slot_tombstoned(mem: dict[str, Any], scope: dict[str, Any], fact_key: str, text: str = "") -> bool:
    key_mac = _slot_hmac(mem, scope, fact_key, mutate=False)
    text_mac = _slot_hmac(mem, scope, text, mutate=False) if text else ""
    for tomb in mem.get("tombstones", {}).values():
        if tomb.get("suppression_mode") != "block_background_relearn_until_explicit_owner_restore":
            continue
        t_scope = tomb.get("scope") or {}
        if not _scopes_equal(t_scope, scope):
            continue
        if tomb.get("fact_key_hmac") == key_mac:
            return True
        aliases = tomb.get("aliases_hmac") or []
        if key_mac in aliases or (text_mac and text_mac in aliases):
            return True
    return False


def _clear_tombstone_for_explicit_restore(mem: dict[str, Any], scope: dict[str, Any], fact_key: str, text: str = "") -> None:
    key_mac = _slot_hmac(mem, scope, fact_key, mutate=True)
    text_mac = _slot_hmac(mem, scope, text, mutate=True) if text else ""
    to_remove = []
    for tid, tomb in mem.get("tombstones", {}).items():
        t_scope = tomb.get("scope") or {}
        if not _scopes_equal(t_scope, scope):
            continue
        aliases = tomb.get("aliases_hmac") or []
        if tomb.get("fact_key_hmac") == key_mac or key_mac in aliases or (text_mac and text_mac in aliases):
            to_remove.append(tid)
    for tid in to_remove:
        mem["tombstones"].pop(tid, None)


def _fence_sources_in_state(mem: dict[str, Any], sources: list[dict[str, Any]], *, reason: str) -> None:
    fenced_msgs = mem["source_fences"].setdefault("fenced_messages", {})
    now = _now()
    for src in sources:
        if src.get("source_type") == "task_message" and src.get("task_id") and src.get("message_id"):
            tid = str(src["task_id"])
            mid = str(src["message_id"])
            crev = int(src.get("conversation_revision", 0))
            chash = str(src.get("content_sha256", ""))
            fkey = _message_fence_key(tid, mid, crev, chash)
            existing = fenced_msgs.get(fkey)
            if isinstance(existing, dict) and existing.get("reason") not in _SLOT_FENCE_REASONS and reason in _SLOT_FENCE_REASONS:
                continue
            fenced_msgs[fkey] = {
                "task_id": tid,
                "message_id": mid,
                "conversation_revision": crev,
                "content_sha256": chash,
                "reason": reason,
                "at": now,
                "reset_epoch": mem["reset_epoch"],
            }


def _write_tombstone(
    mem: dict[str, Any],
    entry: dict[str, Any],
    *,
    reason: str = "owner_deleted",
    owner_action_id: str | None = None,
) -> dict[str, Any]:
    max_tombstones = int(mem["settings"]["budgets"].get("max_tombstones", MAX_TOMBSTONES))
    if entry["id"] not in mem["tombstones"] and len(mem["tombstones"]) >= max_tombstones:
        raise AgentError("agent_memory_tombstone_limit")
    scope = entry["scope"]
    fact_key = entry["fact_key"]
    text = entry.get("text", "")
    key_mac = _slot_hmac(mem, scope, fact_key, mutate=True)
    aliases = [key_mac]
    if text:
        text_mac = _slot_hmac(mem, scope, text, mutate=True)
        if text_mac not in aliases:
            aliases.append(text_mac)
    for alias in entry.get("aliases") or []:
        if isinstance(alias, str) and alias.strip():
            amac = _slot_hmac(mem, scope, alias, mutate=True)
            if amac not in aliases:
                aliases.append(amac)
    sources = entry.get("sources") or []
    _fence_sources_in_state(mem, sources, reason=reason)
    source_fences = [
        {
            "task_id": s.get("task_id"),
            "message_id": s.get("message_id"),
            "conversation_revision": s.get("conversation_revision", 0),
            "content_sha256": s.get("content_sha256", ""),
        }
        for s in sources
        if s.get("source_type") == "task_message"
    ]
    tomb = {
        "entry_id": entry["id"],
        "scope": copy.deepcopy(scope),
        "fact_key_hmac": key_mac,
        "aliases_hmac": aliases,
        "deleted_revision": entry["revision"],
        "deletion_generation": mem["generation"],
        "deleted_at_source_seq": int(mem.get("source_seq") or 0),
        "reset_epoch": mem["reset_epoch"],
        "source_fences": source_fences,
        "suppression_mode": "block_background_relearn_until_explicit_owner_restore",
        "owner_action_id": owner_action_id or ("act_" + uuid.uuid4().hex[:16]),
        "deleted_at": _now(),
        "reason": reason,
    }
    mem["tombstones"][entry["id"]] = tomb
    return tomb


from contextlib import contextmanager


@contextmanager
def _atomic_mutation(data: dict[str, Any]):
    had_memory = "memory" in data
    backup = copy.deepcopy(data.get("memory")) if had_memory else None
    try:
        yield ensure_memory_state(data)
    except Exception:
        if had_memory:
            data["memory"] = backup
        else:
            data.pop("memory", None)
        raise


def update_memory_settings(
    data: dict[str, Any],
    patch: dict[str, Any],
    *,
    expected_generation: int | None = None,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    with _atomic_mutation(data) as mem:
        hit, cached = _record_idempotency(mem, client_request_id, "update_settings", patch)
        if hit:
            return cached
        _check_expected_generation(mem, expected_generation)
        settings = mem["settings"]
        policy_changed = False
        prev_capture = bool(settings.get("capture_enabled"))
        prev_policy = str(settings.get("activation_policy") or "owner_review")
        prev_dest = copy.deepcopy(settings.get("extraction_destination"))

        if "capture_enabled" in patch:
            if not isinstance(patch["capture_enabled"], bool):
                raise AgentError("agent_memory_invalid_settings")
            if patch["capture_enabled"] and not (patch.get("extraction_destination") or settings.get("extraction_destination")):
                if patch.get("require_destination_consent", False):
                    raise AgentError("agent_memory_destination_consent_required")
            if settings["capture_enabled"] != patch["capture_enabled"]:
                settings["capture_enabled"] = patch["capture_enabled"]
                policy_changed = True

        if "use_enabled" in patch:
            if not isinstance(patch["use_enabled"], bool):
                raise AgentError("agent_memory_invalid_settings")
            if settings["use_enabled"] != patch["use_enabled"]:
                settings["use_enabled"] = patch["use_enabled"]
                policy_changed = True

        if "activation_policy" in patch:
            if patch["activation_policy"] not in VALID_ACTIVATION_POLICIES:
                raise AgentError("agent_memory_invalid_settings")
            if settings.get("activation_policy") != patch["activation_policy"]:
                settings["activation_policy"] = patch["activation_policy"]
                policy_changed = True
                now_ts = _now()
                for job in mem.get("jobs", {}).values():
                    if isinstance(job, dict) and job.get("state") in {"queued", "leased", "extracted"}:
                        job["state"] = "stale"
                        job["error_code"] = "activation_policy_changed"
                        job["updated_at"] = now_ts

        if "extraction_model_id" in patch:
            if not isinstance(patch["extraction_model_id"], str):
                raise AgentError("agent_memory_invalid_settings")
            settings["extraction_model_id"] = patch["extraction_model_id"].strip()[:256]
            policy_changed = True

        if "extraction_destination" in patch:
            dest = normalize_destination(patch["extraction_destination"])
            if settings.get("extraction_destination") != dest:
                settings["extraction_destination"] = dest
                if dest and not patch.get("extraction_model_id"):
                    settings["extraction_model_id"] = dest["catalog_model_id"]
                policy_changed = True
                now_ts = _now()
                for job in mem.get("jobs", {}).values():
                    if isinstance(job, dict) and job.get("state") in {"queued", "leased", "extracted"}:
                        job["state"] = "stale"
                        job["error_code"] = "extraction_destination_changed"
                        job["updated_at"] = now_ts

        if "default_exposure" in patch:
            raw_exp = patch["default_exposure"]
            if not isinstance(raw_exp, dict):
                raise AgentError("agent_memory_invalid_exposure_policy")
            mode = str(raw_exp.get("mode") or "selected_models").strip()
            if mode not in VALID_DEFAULT_EXPOSURE_MODES:
                raise AgentError("agent_memory_invalid_exposure_policy")
            raw_dests = raw_exp.get("allowed_destinations") or []
            if not isinstance(raw_dests, list):
                raise AgentError("agent_memory_invalid_exposure_policy")
            dests = [d for d in (normalize_destination(x) for x in raw_dests) if d]
            settings["default_exposure"] = {"mode": mode, "allowed_destinations": dests}
            policy_changed = True

        if "budgets" in patch:
            raw_b = patch["budgets"]
            if not isinstance(raw_b, dict):
                raise AgentError("agent_memory_invalid_settings")
            for bkey, min_v, max_v in (
                ("max_memory_tokens", 128, MAX_MEMORY_TOKENS),
                ("max_memory_bytes", 16384, MAX_MEMORY_BYTES),
                ("max_memory_bytes", 16384, MAX_MEMORY_BYTES),
                ("max_injected_bytes", 512, MAX_INJECTED_BYTES),
                ("max_entries", 4, MAX_ENTRIES),
                ("max_candidates", 4, MAX_CANDIDATES),
                ("max_tombstones", 4, MAX_TOMBSTONES),
                ("max_fenced_sources", 8, MAX_FENCED_SOURCES),
                ("max_daily_extraction_calls", 0, 256),
                ("max_daily_extraction_tokens", 0, 524288),
            ):
                if bkey in raw_b:
                    val = raw_b[bkey]
                    if type(val) is not int or val < min_v or val > max_v:
                        raise AgentError("agent_memory_invalid_budget")
                    settings["budgets"][bkey] = val

        new_capture = bool(settings.get("capture_enabled"))
        new_policy = str(settings.get("activation_policy") or "owner_review")
        new_dest = settings.get("extraction_destination")
        if (
            (new_capture and not prev_capture)
            or (new_policy == "automatic" and prev_policy != "automatic")
            or (new_dest != prev_dest and new_dest is not None)
        ):
            from . import memory_natural

            memory_natural.stamp_all_tasks_owner_messages_in_tx(data)
            mem["consent_generation"] = int(mem.get("consent_generation") or 0) + 1
            mem["automatic_capture_frontier_seq"] = int(mem.get("source_seq") or 0)

        mem["generation"] += 1
        if policy_changed:
            mem["policy_revision"] += 1
        _enforce_memory_bounds(mem)
        result = get_memory_settings_snapshot(data)
        return _save_idempotency(mem, client_request_id, cached, result)


def get_memory_settings_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    mem = read_memory_state(data)
    return {
        "schema_version": mem["schema_version"],
        "generation": mem["generation"],
        "reset_epoch": mem["reset_epoch"],
        "policy_revision": mem["policy_revision"],
        "settings": copy.deepcopy(mem["settings"]),
        "daily_usage": copy.deepcopy(mem["daily_usage"]),
    }


def upsert_active_entry(
    data: dict[str, Any],
    payload: dict[str, Any],
    *,
    origin: str = "manual",
    expected_generation: int | None = None,
    expected_entry_revision: int | None = None,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    """Create or supersede/update an active memory entry under owner authority."""
    if origin not in VALID_ORIGINS:
        raise AgentError("agent_memory_invalid_origin")
    with _atomic_mutation(data) as mem:
        hit, cached = _record_idempotency(mem, client_request_id, f"upsert_{origin}", payload)
        if hit:
            return cached
        _check_expected_generation(mem, expected_generation)

        text = _clean_text(payload.get("text"), field="text", max_chars=MAX_FACT_TEXT_CHARS)
        category = normalize_category(payload.get("category"))
        scope = normalize_scope(payload.get("scope"))
        kind = str(payload.get("kind") or ("project_fact" if scope["kind"] == "project" else "owner_fact")).strip()
        if kind not in VALID_ENTRY_KINDS:
            raise AgentError("agent_memory_invalid_kind")
        fact_key = normalize_fact_key(payload.get("fact_key"), category=category, text=text)

        raw_sources = payload.get("sources")
        sources: list[dict[str, Any]] = []
        if origin in {"explicit_owner_chat", "accepted_extraction", "automatic_consolidation"}:
            if not isinstance(raw_sources, list) or not raw_sources:
                raise AgentError("agent_memory_source_required")
        if isinstance(raw_sources, list) and raw_sources:
            for raw_src in raw_sources[:MAX_SOURCES_PER_ITEM]:
                if not isinstance(raw_src, dict):
                    raise AgentError("agent_memory_invalid_source_type")
                src_type = str(raw_src.get("source_type") or ("task_message" if raw_src.get("task_id") else "manual_owner"))
                if origin in {"explicit_owner_chat", "accepted_extraction", "automatic_consolidation"} and src_type != "task_message":
                    raise AgentError("agent_memory_invalid_source_type")
                src = build_source_ref(
                    source_type=src_type,
                    task_id=raw_src.get("task_id"),
                    message_id=raw_src.get("message_id"),
                    conversation_revision=int(raw_src.get("conversation_revision", 0)) if raw_src.get("conversation_revision") is not None else 0,
                    context_epoch=int(raw_src.get("context_epoch", 0)) if raw_src.get("context_epoch") is not None else 0,
                    source_revision=int(raw_src.get("source_revision", 1)) if raw_src.get("source_revision") is not None else 1,
                    content=str(raw_src.get("content") or raw_src.get("excerpt") or text),
                    content_sha256=raw_src.get("content_sha256"),
                    author_role=str(raw_src.get("author_role") or "owner"),
                    project_id=raw_src.get("project_id") or scope.get("project_id"),
                    excerpt=str(raw_src.get("excerpt") or text),
                )
                if "source_seq" in raw_src and type(raw_src["source_seq"]) is int:
                    src["source_seq"] = raw_src["source_seq"]
                ok, reason = verify_source_ref_against_state(data, src, for_background_job=False)
                if not ok:
                    raise AgentError(f"agent_memory_invalid_source:{reason}")
                sources.append(src)
        else:
            sources.append(
                build_source_ref(
                    source_type="manual_owner",
                    content=text,
                    author_role="owner",
                    project_id=scope.get("project_id"),
                    excerpt=text,
                )
            )

        from . import memory_natural

        memory_natural.stamp_all_tasks_owner_messages_in_tx(data)

        entry_keywords = memory_natural.derive_semantic_keywords(
            fact_key,
            category,
            text,
            extra_keywords=payload.get("keywords"),
        )
        entry_summary = memory_natural.build_entry_summary(
            fact_key,
            text,
            str(payload.get("summary") or ""),
        )
        owner_acc_seq = 0
        if origin != "automatic_consolidation":
            mem["source_seq"] = int(mem.get("source_seq") or 0) + 1
            owner_acc_seq = mem["source_seq"]
        src_seq = max(
            [
                owner_acc_seq,
                int(payload.get("last_source_seq") or 0),
                *(int(s.get("source_seq") or 0) for s in sources),
            ],
            default=0,
        )

        default_dest = normalize_destination(payload.get("default_destination"))
        now = _now()
        entry_id = payload.get("id") or payload.get("entry_id")

        target_entry = None
        if isinstance(entry_id, str) and entry_id.strip():
            target_entry = mem["entries"].get(entry_id.strip())
            if target_entry is None or target_entry.get("status") != "active":
                raise AgentError("agent_memory_not_found")
            if expected_entry_revision is not None and target_entry["revision"] != expected_entry_revision:
                raise AgentError("agent_memory_revision_conflict")
        else:
            for existing in mem["entries"].values():
                if (
                    existing.get("status") == "active"
                    and _scopes_equal(existing.get("scope"), scope)
                    and existing["fact_key"] == fact_key
                ):
                    target_entry = existing
                    break
            if target_entry is not None and expected_entry_revision is not None:
                if target_entry["revision"] != expected_entry_revision:
                    raise AgentError("agent_memory_revision_conflict")

        # Explicit owner action restores tombstoned slot if it was previously deleted
        _clear_tombstone_for_explicit_restore(mem, scope, fact_key, text)

        supersedes: list[str] = list(payload.get("supersedes") or [])
        if target_entry is not None and payload.get("create_superseding_entry"):
            old_id = target_entry["id"]
            if old_id not in supersedes:
                supersedes.append(old_id)
            new_id = "mem_" + uuid.uuid4().hex[:16]
            exposure = normalize_exposure_policy(
                payload.get("exposure_policy", target_entry.get("exposure_policy")),
                default_destination=default_dest,
                prev_revision=target_entry.get("exposure_policy", {}).get("revision", 0),
            )
            target_entry["status"] = "superseded"
            target_entry["superseded_by"] = new_id
            target_entry["revision"] += 1
            target_entry["updated_at"] = now
            _fence_sources_in_state(mem, target_entry.get("sources") or [], reason="superseded")
            entry = {
                "id": new_id,
                "scope": scope,
                "category": category,
                "fact_key": fact_key,
                "text": text,
                "summary": entry_summary,
                "keywords": entry_keywords,
                "kind": kind,
                "status": "active",
                "revision": 1,
                "origin": origin,
                "sources": sources,
                "last_source_seq": src_seq,
                "owner_acceptance_seq": owner_acc_seq,
                "source_history": [],
                "supersedes": supersedes,
                "superseded_by": None,
                "exposure_policy": exposure,
                "created_at": now,
                "updated_at": now,
                "last_used_at": None,
                "reviewed_by_owner_at": now,
            }
            mem["entries"][new_id] = entry
        elif target_entry is not None:
            prev_exp = target_entry.get("exposure_policy") or {}
            exposure = (
                normalize_exposure_policy(
                    payload["exposure_policy"],
                    default_destination=default_dest,
                    prev_revision=prev_exp.get("revision", 0),
                )
                if "exposure_policy" in payload
                else copy.deepcopy(prev_exp)
            )
            history = list(target_entry.get("source_history") or [])
            if target_entry.get("text") != text:
                history.append(
                    {
                        "revision": int(target_entry.get("revision", 1)),
                        "fact_key": target_entry.get("fact_key"),
                        "text": target_entry.get("text"),
                        "sources": copy.deepcopy(target_entry.get("sources") or []),
                        "last_source_seq": int(target_entry.get("last_source_seq") or 0),
                        "replaced_at": now,
                    }
                )
                _fence_sources_in_state(mem, target_entry.get("sources") or [], reason="updated")
            target_entry.update(
                scope=scope,
                category=category,
                fact_key=fact_key,
                text=text,
                summary=entry_summary,
                keywords=entry_keywords,
                kind=kind,
                origin=origin,
                sources=sources,
                last_source_seq=max(src_seq, int(target_entry.get("last_source_seq") or 0)),
                owner_acceptance_seq=max(owner_acc_seq, int(target_entry.get("owner_acceptance_seq") or 0)),
                source_history=history[-8:],
                exposure_policy=exposure,
                revision=target_entry["revision"] + 1,
                updated_at=now,
                reviewed_by_owner_at=now,
            )
            for sup_id in supersedes:
                if sup_id != target_entry["id"] and sup_id not in target_entry["supersedes"]:
                    target_entry["supersedes"].append(sup_id)
                    sup_entry = mem["entries"].get(sup_id)
                    if sup_entry and sup_entry.get("status") == "active":
                        sup_entry["status"] = "superseded"
                        sup_entry["superseded_by"] = target_entry["id"]
                        sup_entry["revision"] += 1
                        sup_entry["updated_at"] = now
                        _fence_sources_in_state(mem, sup_entry.get("sources") or [], reason="superseded")
            entry = target_entry
        else:
            new_id = "mem_" + uuid.uuid4().hex[:16]
            exposure = normalize_exposure_policy(
                payload.get("exposure_policy"),
                default_destination=default_dest,
                prev_revision=0,
            )
            for sup_id in supersedes:
                sup_entry = mem["entries"].get(sup_id)
                if sup_entry and sup_entry.get("status") == "active":
                    sup_entry["status"] = "superseded"
                    sup_entry["superseded_by"] = new_id
                    sup_entry["revision"] += 1
                    sup_entry["updated_at"] = now
                    _fence_sources_in_state(mem, sup_entry.get("sources") or [], reason="superseded")
            entry = {
                "id": new_id,
                "scope": scope,
                "category": category,
                "fact_key": fact_key,
                "text": text,
                "summary": entry_summary,
                "keywords": entry_keywords,
                "kind": kind,
                "status": "active",
                "revision": 1,
                "origin": origin,
                "sources": sources,
                "last_source_seq": src_seq,
                "owner_acceptance_seq": owner_acc_seq,
                "source_history": [],
                "supersedes": supersedes,
                "superseded_by": None,
                "exposure_policy": exposure,
                "created_at": now,
                "updated_at": now,
                "last_used_at": None,
                "reviewed_by_owner_at": now,
            }
            mem["entries"][new_id] = entry

        for cand in mem["candidates"].values():
            if (
                cand.get("status") == "pending"
                and _scopes_equal(cand.get("scope"), scope)
                and cand["fact_key"] == fact_key
            ):
                cand["status"] = "stale"
                cand["stale_reason"] = "superseded_by_owner"
                cand["revision"] += 1
                cand["updated_at"] = now

        mem["generation"] += 1
        mem["policy_revision"] += 1
        _enforce_memory_bounds(mem)
        result = {"entry": copy.deepcopy(entry), "generation": mem["generation"], "policy_revision": mem["policy_revision"]}
        return _save_idempotency(mem, client_request_id, cached, result)


def delete_memory_entry(
    data: dict[str, Any],
    entry_id: str,
    *,
    expected_entry_revision: int | None = None,
    expected_generation: int | None = None,
    client_request_id: str | None = None,
    reason: str = "owner_deleted",
) -> dict[str, Any]:
    """Delete an active or superseded memory entry, writing an opaque tombstone and fencing its sources."""
    with _atomic_mutation(data) as mem:
        req_payload = {"entry_id": entry_id, "reason": reason}
        hit, cached = _record_idempotency(mem, client_request_id, "delete_entry", req_payload)
        if hit:
            return cached
        _check_expected_generation(mem, expected_generation)

        entry = mem["entries"].get(entry_id)
        if entry is None:
            raise AgentError("agent_memory_not_found")
        if expected_entry_revision is not None and entry["revision"] != expected_entry_revision:
            raise AgentError("agent_memory_revision_conflict")

        from . import memory_natural

        memory_natural.stamp_all_tasks_owner_messages_in_tx(data)
        mem["source_seq"] = int(mem.get("source_seq") or 0) + 1
        tomb = _write_tombstone(mem, entry, reason=reason, owner_action_id=client_request_id)
        mem["generation"] += 1
        mem["policy_revision"] += 1
        tomb["deletion_generation"] = mem["generation"]
        scope = entry["scope"]
        fact_key = entry["fact_key"]
        mem["entries"].pop(entry_id, None)

        now = _now()
        for cand in mem["candidates"].values():
            if (
                cand.get("status") == "pending"
                and _scopes_equal(cand.get("scope"), scope)
                and cand["fact_key"] == fact_key
            ):
                cand["status"] = "stale"
                cand["stale_reason"] = "slot_deleted_by_owner"
                cand["revision"] += 1
                cand["updated_at"] = now

        _enforce_memory_bounds(mem)
        result = {
            "deleted": True,
            "id": entry_id,
            "tombstone_recorded": True,
            "deleted_revision": tomb["deleted_revision"],
            "generation": mem["generation"],
            "policy_revision": mem["policy_revision"],
        }
        return _save_idempotency(mem, client_request_id, cached, result)


def _scope_filter_matches(item_scope: dict[str, Any] | None, filter_scope: dict[str, Any] | None) -> bool:
    if filter_scope is None:
        return True
    if not isinstance(item_scope, dict):
        return False
    if item_scope.get("kind") != filter_scope.get("kind"):
        return False
    if item_scope.get("owner_id", "local_owner") != filter_scope.get("owner_id", "local_owner"):
        return False
    if filter_scope.get("kind") == "project":
        return _project_scope_matches(
            item_scope,
            filter_scope.get("project_id"),
            filter_scope.get("project_generation"),
            require_generation_match=filter_scope.get("project_generation") is not None,
        )
    return True


def forget_by_slot_or_query(
    data: dict[str, Any],
    *,
    scope: dict[str, Any] | None = None,
    fact_keys: list[str] | None = None,
    categories: list[str] | None = None,
    keywords: list[str] | None = None,
    source_ref: dict[str, Any] | None = None,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    """Deterministically forget matching active entries and record tombstones."""
    with _atomic_mutation(data) as mem:
        norm_scope = normalize_scope(scope) if scope else None
        req_payload = {
            "scope": norm_scope,
            "fact_keys": sorted(fact_keys or []),
            "categories": sorted(categories or []),
            "keywords": sorted(keywords or []),
        }
        hit, cached = _record_idempotency(mem, client_request_id, "forget_query", req_payload)
        if hit:
            return cached
        if source_ref is not None:
            ok, reason = verify_source_ref_against_state(data, source_ref, for_background_job=False)
            if not ok:
                raise AgentError(f"agent_memory_invalid_source:{reason}")

        key_set = {k.strip().lower() for k in (fact_keys or []) if isinstance(k, str) and k.strip()}
        cat_set = {c.strip().lower() for c in (categories or []) if isinstance(c, str) and c.strip()}
        kw_list = [k.strip().lower() for k in (keywords or []) if isinstance(k, str) and k.strip()]

        matched_ids: list[str] = []
        for eid, entry in list(mem["entries"].items()):
            if entry.get("status") != "active":
                continue
            if not _scope_filter_matches(entry.get("scope"), norm_scope):
                continue
            ekey = entry.get("fact_key", "").lower()
            ecat = entry.get("category", "").lower()
            etext = entry.get("text", "").lower()
            if (
                (key_set and (ekey in key_set or any(ekey.startswith(prefix) for prefix in key_set)))
                or (cat_set and ecat in cat_set)
                or (kw_list and any(kw in etext or kw in ekey or kw in ecat for kw in kw_list))
            ):
                matched_ids.append(eid)

        max_tombstones = int(mem["settings"]["budgets"].get("max_tombstones", MAX_TOMBSTONES))
        new_tombstones = sum(1 for eid in matched_ids if eid not in mem["tombstones"])
        if len(mem["tombstones"]) + new_tombstones > max_tombstones:
            raise AgentError("agent_memory_tombstone_limit")

        from . import memory_natural

        memory_natural.stamp_all_tasks_owner_messages_in_tx(data)
        mem["source_seq"] = int(mem.get("source_seq") or 0) + 1
        mem["generation"] += 1
        mem["policy_revision"] += 1
        deleted_ids: list[str] = []
        for eid in matched_ids:
            entry = mem["entries"].pop(eid, None)
            if entry:
                _write_tombstone(mem, entry, reason="explicit_owner_forget", owner_action_id=client_request_id)
                deleted_ids.append(eid)

        now = _now()
        for cand in mem["candidates"].values():
            if cand.get("status") != "pending":
                continue
            if not _scope_filter_matches(cand.get("scope"), norm_scope):
                continue
            ckey = cand.get("fact_key", "").lower()
            ccat = cand.get("category", "").lower()
            ctext = cand.get("text", "").lower()
            if (
                (key_set and (ckey in key_set or any(ckey.startswith(prefix) for prefix in key_set)))
                or (cat_set and ccat in cat_set)
                or (kw_list and any(kw in ctext or kw in ckey or kw in ccat for kw in kw_list))
            ):
                cand["status"] = "stale"
                cand["stale_reason"] = "forgotten_by_owner"
                cand["revision"] += 1
                cand["updated_at"] = now

        _enforce_memory_bounds(mem)
        result = {
            "deleted_ids": deleted_ids,
            "deleted_count": len(deleted_ids),
            "generation": mem["generation"],
            "policy_revision": mem["policy_revision"],
        }
        return _save_idempotency(mem, client_request_id, cached, result)


def _record_task_reset_boundary(mem: dict[str, Any], tid: str, row: dict[str, Any]) -> None:
    crev = int(row.get("conversation_revision", 0))
    msgs = row.get("messages") or []
    mids: list[str] = []
    sources_to_fence: list[dict[str, Any]] = []
    for msg in msgs:
        if not isinstance(msg, dict):
            continue
        mid = msg.get("message_id")
        if isinstance(mid, str) and mid:
            mids.append(mid)
            if msg.get("role") == "user":
                text = (
                    msg.get("ui", {}).get("text")
                    if isinstance(msg.get("ui"), dict) and "text" in msg["ui"]
                    else msg.get("content", "")
                )
                if isinstance(text, str):
                    sources_to_fence.append(
                        {
                            "source_type": "task_message",
                            "task_id": tid,
                            "message_id": mid,
                            "conversation_revision": crev,
                            "content_sha256": message_content_sha256(text),
                        }
                    )
    _fence_sources_in_state(mem, sources_to_fence, reason="memory_reset")
    mem["source_fences"].setdefault("reset_task_boundaries", {})[tid] = {
        "reset_epoch": mem["reset_epoch"],
        "conversation_revision": crev,
        "message_count": len(msgs),
        "fenced_message_ids": mids,
        "at": _now(),
    }


def reset_memory(
    data: dict[str, Any],
    *,
    scope: dict[str, Any] | None = None,
    expected_generation: int | None = None,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    """Reset/clear memory (all, personal, or project-scoped), advancing reset_epoch and recording source boundaries."""
    with _atomic_mutation(data) as mem:
        norm_scope = None
        if scope is not None and not (isinstance(scope, dict) and scope.get("kind") == "all"):
            norm_scope = normalize_scope(scope)
        req_payload = {"scope": norm_scope}
        hit, cached = _record_idempotency(mem, client_request_id, "reset_memory", req_payload)
        if hit:
            return cached
        _check_expected_generation(mem, expected_generation)

        mem["reset_epoch"] += 1
        mem["generation"] += 1
        mem["policy_revision"] += 1
        now = _now()

        cleared_entries = 0
        cleared_candidates = 0

        if norm_scope and norm_scope["kind"] == "project":
            pid = norm_scope["project_id"]
            for eid in [k for k, v in mem["entries"].items() if _scope_filter_matches(v.get("scope"), norm_scope)]:
                entry = mem["entries"].pop(eid)
                _fence_sources_in_state(mem, entry.get("sources") or [], reason="project_reset")
                cleared_entries += 1
            for cid in [k for k, v in mem["candidates"].items() if _scope_filter_matches(v.get("scope"), norm_scope)]:
                cand = mem["candidates"].pop(cid, None)
                if cand:
                    _fence_sources_in_state(mem, cand.get("sources") or [], reason="project_reset")
                cleared_candidates += 1
            for tomb_id in [k for k, v in mem["tombstones"].items() if _scope_filter_matches(v.get("scope"), norm_scope)]:
                mem["tombstones"].pop(tomb_id, None)
            for tid, row in (data.get("tasks") or {}).items():
                if isinstance(row, dict) and isinstance(row.get("project"), dict) and row["project"].get("id") == pid:
                    _record_task_reset_boundary(mem, tid, row)
                    mem["source_fences"].get("relearn_tasks", {}).pop(tid, None)
            for job in mem["jobs"].values():
                if job.get("project_id") == pid and job.get("state") in {"queued", "leased", "extracted"}:
                    job["state"] = "stale"
                    job["error_code"] = "reset_epoch_mismatch"
                    job["updated_at"] = now
        elif norm_scope and norm_scope["kind"] == "personal":
            for eid in [k for k, v in mem["entries"].items() if (v.get("scope") or {}).get("kind") == "personal"]:
                entry = mem["entries"].pop(eid)
                _fence_sources_in_state(mem, entry.get("sources") or [], reason="personal_reset")
                cleared_entries += 1
            for cid in [k for k, v in mem["candidates"].items() if (v.get("scope") or {}).get("kind") == "personal"]:
                cand = mem["candidates"].pop(cid, None)
                if cand:
                    _fence_sources_in_state(mem, cand.get("sources") or [], reason="personal_reset")
                cleared_candidates += 1
            for tomb_id in [k for k, v in mem["tombstones"].items() if (v.get("scope") or {}).get("kind") == "personal"]:
                mem["tombstones"].pop(tomb_id, None)
            for tid, row in (data.get("tasks") or {}).items():
                if isinstance(row, dict) and not (isinstance(row.get("project"), dict) and row["project"].get("id")):
                    _record_task_reset_boundary(mem, tid, row)
                    mem["source_fences"].get("relearn_tasks", {}).pop(tid, None)
            for job in mem["jobs"].values():
                if not job.get("project_id") and job.get("state") in {"queued", "leased", "extracted"}:
                    job["state"] = "stale"
                    job["error_code"] = "reset_epoch_mismatch"
                    job["updated_at"] = now
        else:
            cleared_entries = len(mem["entries"])
            cleared_candidates = len(mem["candidates"])
            task_revs = dict((mem["source_fences"].get("reset_watermark") or {}).get("task_revisions") or {})
            for tid, w_rev in (mem["source_fences"].get("fenced_task_watermarks") or {}).items():
                task_revs[tid] = max(int(task_revs.get(tid, 0)), int(w_rev))
            for fkey, meta in (mem["source_fences"].get("fenced_messages") or {}).items():
                f_tid, f_crev = _extract_fence_task_and_revision(fkey, meta)
                if f_tid is not None and f_crev is not None:
                    task_revs[f_tid] = max(int(task_revs.get(f_tid, 0)), int(f_crev))
            for tid, row in (data.get("tasks") or {}).items():
                if isinstance(row, dict):
                    task_revs[tid] = max(int(task_revs.get(tid, 0)), int(row.get("conversation_revision", 0)))
                    _record_task_reset_boundary(mem, tid, row)

            mem["entries"] = {}
            mem["candidates"] = {}
            mem["tombstones"] = {}
            mem["processed_sources"] = {}
            mem["hmac_salt"] = uuid.uuid4().hex + uuid.uuid4().hex
            for job in mem["jobs"].values():
                if job.get("state") in {"queued", "leased", "extracted"}:
                    job["state"] = "stale"
                    job["error_code"] = "reset_epoch_mismatch"
                    job["updated_at"] = now
            mem["source_fences"]["reset_watermark"] = {
                "reset_epoch": mem["reset_epoch"],
                "at": now,
                "task_revisions": task_revs,
            }
            mem["source_fences"]["relearn_tasks"] = {}

        _enforce_memory_bounds(mem)
        result = {
            "reset": True,
            "scope": norm_scope or {"kind": "all"},
            "cleared_entries": cleared_entries,
            "cleared_candidates": cleared_candidates,
            "reset_epoch": mem["reset_epoch"],
            "generation": mem["generation"],
            "policy_revision": mem["policy_revision"],
        }
        return _save_idempotency(mem, client_request_id, cached, result)


def enqueue_extraction_job(
    data: dict[str, Any],
    *,
    task_id: str,
    message_ids: list[str] | None = None,
    force_relearn: bool = False,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    """Snapshot owner message sources and enqueue a bounded background extraction job."""
    with _atomic_mutation(data) as mem:
        tasks = data.get("tasks") or {}
        row = tasks.get(task_id)
        if not isinstance(row, dict):
            raise AgentError("agent_not_found")
        if row.get("archived") and not force_relearn:
            raise AgentError("agent_memory_source_archived")
        if task_id in mem["source_fences"].get("deleted_tasks", {}):
            raise AgentError("agent_memory_source_deleted")

        if force_relearn:
            mem["source_fences"].setdefault("relearn_tasks", {})[task_id] = mem["reset_epoch"]

        from . import memory_natural

        memory_natural.stamp_task_owner_messages_in_tx(data, row)

        conv_rev = int(row.get("conversation_revision", 0))
        epoch = int(row.get("context_epoch", 0))
        project = row.get("project") if isinstance(row.get("project"), dict) else None
        project_id = project.get("id") if project else None
        project_gen = project.get("generation") if project else None
        task_dest = normalize_destination(
            row.get("model_destination") or ({"catalog_model_id": row["model_id"]} if row.get("model_id") else None)
        )
        ext_dest = normalize_destination(mem["settings"].get("extraction_destination")) or task_dest
        if not ext_dest:
            raise AgentError("agent_memory_destination_consent_required")

        wanted_ids = set(message_ids) if message_ids else None
        manifest: list[dict[str, Any]] = []
        for msg in row.get("messages") or []:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            mid = msg.get("message_id")
            if not isinstance(mid, str) or not mid.strip():
                continue
            if wanted_ids is not None and mid not in wanted_ids:
                continue
            ui_kind = (msg.get("ui") or {}).get("kind") if isinstance(msg.get("ui"), dict) else None
            if ui_kind in {"source_credential"}:
                continue
            text = msg.get("ui", {}).get("text") if isinstance(msg.get("ui"), dict) and "text" in msg["ui"] else msg.get("content", "")
            if not isinstance(text, str) or not text.strip():
                continue
            if _SECRET_PATTERN.search(text):
                continue
            src = build_source_ref(
                source_type="task_message",
                task_id=task_id,
                message_id=mid,
                conversation_revision=conv_rev,
                context_epoch=int(msg.get("context_epoch", epoch)),
                source_revision=int(row.get("revision", 1)),
                content=text,
                author_role="owner",
                project_id=project_id,
                excerpt=text,
            )
            src["source_seq"] = int(msg.get("owner_source_seq") or 0)
            ok, _ = verify_source_ref_against_state(data, src, for_background_job=not force_relearn)
            if ok:
                manifest.append({**src, "text": sanitized(text).strip()[:MAX_FACT_TEXT_CHARS]})

        if not manifest:
            return {"queued": False, "reason": "no_eligible_owner_sources", "reset_epoch": mem["reset_epoch"]}

        idempotency_key = digest(
            {
                "task_id": task_id,
                "conversation_revision": conv_rev,
                "reset_epoch": mem["reset_epoch"],
                "sources": [(m["message_id"], m["content_sha256"]) for m in manifest],
                "destination": ext_dest,
                "activation_policy": mem["settings"].get("activation_policy", "owner_review"),
            }
        )
        for existing in mem["jobs"].values():
            if existing.get("idempotency_key") == idempotency_key and existing.get("state") in {"queued", "leased", "extracted", "committed", "no_op"}:
                return {"queued": True, "deduplicated": True, "job": copy.deepcopy(existing)}

        now = _now()
        job_id = "mjob_" + uuid.uuid4().hex[:16]
        job = {
            "id": job_id,
            "idempotency_key": idempotency_key,
            "client_request_id": client_request_id,
            "state": "queued",
            "task_id": task_id,
            "project_id": project_id,
            "project_generation": project_gen,
            "conversation_revision": conv_rev,
            "context_epoch": epoch,
            "source_manifest": manifest,
            "expected_generation": mem["generation"],
            "reset_epoch": mem["reset_epoch"],
            "activation_policy": mem["settings"].get("activation_policy", "owner_review"),
            "destination_binding": ext_dest,
            "lease_owner": None,
            "lease_token": None,
            "lease_until": 0.0,
            "attempts": 0,
            "output_hash": None,
            "error_code": None,
            "created_at": now,
            "updated_at": now,
        }
        mem["jobs"][job_id] = job
        mem["generation"] += 1
        _enforce_memory_bounds(mem)
        return {"queued": True, "deduplicated": False, "job": copy.deepcopy(job)}


def claim_extraction_job(
    data: dict[str, Any],
    *,
    worker_id: str,
    job_id: str | None = None,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> dict[str, Any] | None:
    """Claim a queued or expired-lease extraction job for a worker, issuing a unique per-attempt lease_token."""
    with _atomic_mutation(data) as mem:
        now = _now()
        candidates = []
        for jid, job in mem["jobs"].items():
            if job_id and jid != job_id:
                continue
            state = job.get("state")
            if state == "queued" or (state == "leased" and float(job.get("lease_until") or 0.0) <= now):
                candidates.append(job)
        if not candidates:
            return None
        candidates.sort(key=lambda j: j.get("created_at", 0.0))
        selected = candidates[0]

        if selected.get("reset_epoch") != mem["reset_epoch"]:
            selected["state"] = "stale"
            selected["error_code"] = "reset_epoch_mismatch"
            selected["updated_at"] = now
            return None

        for src in selected.get("source_manifest") or []:
            ok, reason = verify_source_ref_against_state(data, src, for_background_job=True)
            if not ok:
                selected["state"] = "stale"
                selected["error_code"] = reason or "source_fenced"
                selected["updated_at"] = now
                return None

        selected["state"] = "leased"
        selected["lease_owner"] = worker_id
        selected["lease_token"] = "lease_" + uuid.uuid4().hex
        selected["lease_until"] = round(now + max(1.0, float(lease_seconds)), 6)
        selected["attempts"] = int(selected.get("attempts", 0)) + 1
        selected["updated_at"] = now
        return copy.deepcopy(selected)


def commit_extraction_job(
    data: dict[str, Any],
    *,
    job_id: str,
    worker_id: str,
    proposals: list[dict[str, Any]],
    lease_token: str | None = None,
    tokens_used: int = 0,
) -> dict[str, Any]:
    """Transactionally verify lease_token, source fences, reset epoch, tombstones, and revisions before committing candidates."""
    with _atomic_mutation(data) as mem:
        now = _now()
        job = mem["jobs"].get(job_id)
        if not isinstance(job, dict):
            raise AgentError("agent_memory_job_not_found")
        if job.get("state") in {"committed", "no_op"}:
            return {"state": job["state"], "job": copy.deepcopy(job), "committed_candidates": []}
        if job.get("state") in {"stale", "failed"}:
            return {"state": job["state"], "job": copy.deepcopy(job), "committed_candidates": []}
        if job.get("lease_owner") != worker_id:
            raise AgentError("agent_memory_job_lease_lost")
        if not isinstance(lease_token, str) or not lease_token.strip() or job.get("lease_token") != lease_token:
            raise AgentError("agent_memory_job_lease_lost")
        if float(job.get("lease_until") or 0.0) < now:
            job["state"] = "stale"
            job["error_code"] = "lease_expired"
            job["updated_at"] = now
            return {"state": "stale", "job": copy.deepcopy(job), "committed_candidates": []}

        # 1. Re-verify reset_epoch at commit time
        if job.get("reset_epoch") != mem["reset_epoch"]:
            job["state"] = "stale"
            job["error_code"] = "reset_epoch_mismatch"
            job["updated_at"] = now
            return {"state": "stale", "job": copy.deepcopy(job), "committed_candidates": []}

        # 2. Re-verify task project generation & every source in the job's manifest against live task/branch/fence state
        live_row = (data.get("tasks") or {}).get(job.get("task_id"))
        if isinstance(live_row, dict) and job.get("project_id"):
            live_proj = live_row.get("project") if isinstance(live_row.get("project"), dict) else {}
            if live_proj.get("id") != job.get("project_id") or live_proj.get("generation") != job.get("project_generation"):
                job["state"] = "stale"
                job["error_code"] = "project_generation_changed"
                job["updated_at"] = now
                return {"state": "stale", "job": copy.deepcopy(job), "committed_candidates": []}

        manifest = job.get("source_manifest") or []
        manifest_by_msg = {m["message_id"]: m for m in manifest if m.get("message_id")}
        for src in manifest:
            ok, reason = verify_source_ref_against_state(data, src, for_background_job=True)
            if not ok:
                job["state"] = "stale"
                job["error_code"] = reason or "source_invalidated"
                job["updated_at"] = now
                return {"state": "stale", "job": copy.deepcopy(job), "committed_candidates": []}

        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        usage = mem["daily_usage"]
        if usage.get("day") != day:
            usage["day"] = day
            usage["extraction_calls"] = 0
            usage["extraction_tokens"] = 0
        usage["extraction_calls"] += 1
        usage["extraction_tokens"] += max(0, int(tokens_used))

        if not proposals:
            job["state"] = "no_op"
            job["output_hash"] = digest([])
            job["updated_at"] = now
            mem["generation"] += 1
            return {"state": "no_op", "job": copy.deepcopy(job), "committed_candidates": []}

        bound_dest = normalize_destination(job.get("destination_binding"))
        job_project_id = job.get("project_id")
        job_project_gen = job.get("project_generation")

        committed_candidates: list[dict[str, Any]] = []
        skipped_reasons: list[str] = []

        for raw_prop in proposals[:16]:
            if not isinstance(raw_prop, dict):
                continue
            try:
                text = _clean_text(raw_prop.get("text"), field="text", max_chars=MAX_FACT_TEXT_CHARS)
                category = normalize_category(raw_prop.get("category"))
                fact_key = normalize_fact_key(raw_prop.get("fact_key"), category=category, text=text)
            except AgentError as exc:
                skipped_reasons.append(str(exc))
                continue

            raw_scope = raw_prop.get("scope")
            if raw_scope is None:
                if job_project_id:
                    scope = {
                        "kind": "project",
                        "owner_id": "local_owner",
                        "project_id": job_project_id,
                        "project_generation": job_project_gen,
                    }
                else:
                    scope = {"kind": "personal", "owner_id": "local_owner", "project_id": None, "project_generation": None}
            else:
                scope = normalize_scope(raw_scope)
                if scope["kind"] == "project":
                    if not job_project_id or scope["project_id"] != job_project_id:
                        skipped_reasons.append("cross_project_escalation_blocked")
                        continue
                    if scope.get("project_generation") is not None and scope.get("project_generation") != job_project_gen:
                        skipped_reasons.append("project_generation_mismatch")
                        continue
                    scope["project_generation"] = job_project_gen
                elif scope["kind"] == "personal" and job_project_id and not raw_prop.get("allow_personal_from_project"):
                    scope = {
                        "kind": "project",
                        "owner_id": "local_owner",
                        "project_id": job_project_id,
                        "project_generation": job_project_gen,
                    }

            cand_exposure = normalize_exposure_policy(
                raw_prop.get("exposure_policy"),
                default_destination=bound_dest,
                prev_revision=0,
            )
            if exposure_is_wider(cand_exposure, bound_dest):
                cand_exposure = normalize_exposure_policy(None, default_destination=bound_dest, prev_revision=0)

            if is_slot_tombstoned(mem, scope, fact_key, text):
                skipped_reasons.append("tombstone_suppressed")
                continue

            active_match = None
            for existing in mem["entries"].values():
                if (
                    existing.get("status") == "active"
                    and _scopes_equal(existing.get("scope"), scope)
                    and existing["fact_key"] == fact_key
                ):
                    active_match = existing
                    break
            expected_entry_rev = raw_prop.get("expected_entry_revision")
            if active_match is not None:
                if expected_entry_rev is not None and active_match["revision"] != expected_entry_rev:
                    skipped_reasons.append("active_entry_revision_changed")
                    continue
                if active_match.get("text") == text:
                    skipped_reasons.append("already_active")
                    continue
                if float(active_match.get("updated_at") or 0.0) >= float(job.get("created_at") or 0.0):
                    skipped_reasons.append("active_entry_newer_than_job")
                    continue

            prop_msg_ids = raw_prop.get("source_message_ids")
            cand_sources: list[dict[str, Any]] = []
            if isinstance(prop_msg_ids, list) and prop_msg_ids:
                for mid in prop_msg_ids:
                    if mid in manifest_by_msg:
                        m_item = manifest_by_msg[mid]
                        cand_sources.append({k: v for k, v in m_item.items() if k != "text"})
            else:
                cand_sources = [{k: v for k, v in m_item.items() if k != "text"} for m_item in manifest[:MAX_SOURCES_PER_ITEM]]

            if not cand_sources:
                skipped_reasons.append("no_verified_owner_source")
                continue

            existing_cand = None
            for cand in mem["candidates"].values():
                if (
                    cand.get("status") == "pending"
                    and _scopes_equal(cand.get("scope"), scope)
                    and cand["fact_key"] == fact_key
                ):
                    existing_cand = cand
                    break

            kind = str(raw_prop.get("kind") or ("project_fact" if scope["kind"] == "project" else "owner_fact")).strip()
            if kind not in VALID_ENTRY_KINDS:
                kind = "owner_fact"

            if existing_cand is not None:
                existing_cand.update(
                    text=text,
                    category=category,
                    kind=kind,
                    sources=cand_sources,
                    exposure_policy=cand_exposure,
                    job_id=job_id,
                    reset_epoch=mem["reset_epoch"],
                    target_entry_id=active_match["id"] if active_match else None,
                    expected_entry_revision=active_match["revision"] if active_match else None,
                    revision=existing_cand["revision"] + 1,
                    updated_at=now,
                )
                committed_candidates.append(copy.deepcopy(existing_cand))
            else:
                cid = "mcand_" + uuid.uuid4().hex[:16]
                cand = {
                    "id": cid,
                    "scope": scope,
                    "category": category,
                    "fact_key": fact_key,
                    "text": text,
                    "kind": kind,
                    "status": "pending",
                    "revision": 1,
                    "origin": "background_extraction",
                    "job_id": job_id,
                    "reset_epoch": mem["reset_epoch"],
                    "sources": cand_sources,
                    "target_entry_id": active_match["id"] if active_match else None,
                    "expected_entry_revision": active_match["revision"] if active_match else None,
                    "exposure_policy": cand_exposure,
                    "created_at": now,
                    "updated_at": now,
                }
                mem["candidates"][cid] = cand
                committed_candidates.append(copy.deepcopy(cand))

        job["output_hash"] = digest(proposals)
        job["updated_at"] = now
        if committed_candidates:
            job["state"] = "committed"
        elif skipped_reasons and all(r in {"tombstone_suppressed", "active_entry_revision_changed", "active_entry_newer_than_job"} for r in skipped_reasons):
            job["state"] = "stale"
            job["error_code"] = skipped_reasons[0]
        else:
            job["state"] = "no_op"
            if skipped_reasons:
                job["error_code"] = skipped_reasons[0]

        mem["generation"] += 1
        _enforce_memory_bounds(mem)
        return {
            "state": job["state"],
            "job": copy.deepcopy(job),
            "committed_candidates": committed_candidates,
            "skipped_reasons": skipped_reasons,
        }


def decide_candidate(
    data: dict[str, Any],
    candidate_id: str,
    *,
    decision: str,
    edited_text: str | None = None,
    edited_category: str | None = None,
    edited_fact_key: str | None = None,
    edited_scope: dict[str, Any] | None = None,
    exposure_policy: dict[str, Any] | None = None,
    expected_candidate_revision: int | None = None,
    expected_generation: int | None = None,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    """Owner review decision on a pending candidate: accept, edit_and_accept, or reject."""
    with _atomic_mutation(data) as mem:
        if decision not in {"accept", "edit_and_accept", "reject"}:
            raise AgentError("agent_memory_invalid_decision")
        req_payload = {
            "candidate_id": candidate_id,
            "decision": decision,
            "edited_text": edited_text,
            "edited_category": edited_category,
            "edited_fact_key": edited_fact_key,
            "edited_scope": edited_scope,
            "exposure_policy": exposure_policy,
        }
        hit, cached = _record_idempotency(mem, client_request_id, "decide_candidate", req_payload)
        if hit:
            return cached
        _check_expected_generation(mem, expected_generation)

        cand = mem["candidates"].get(candidate_id)
        if not isinstance(cand, dict):
            raise AgentError("agent_memory_candidate_not_found")
        if expected_candidate_revision is not None and cand["revision"] != expected_candidate_revision:
            raise AgentError("agent_memory_revision_conflict")
        if cand.get("status") != "pending":
            raise AgentError("agent_memory_candidate_not_pending")
        if cand.get("reset_epoch") != mem["reset_epoch"]:
            cand["status"] = "stale"
            cand["stale_reason"] = "reset_epoch_mismatch"
            raise AgentError("agent_memory_candidate_stale")

        for src in cand.get("sources") or []:
            ok, reason = verify_source_ref_against_state(data, src, for_background_job=False)
            if not ok:
                cand["status"] = "stale"
                cand["stale_reason"] = reason or "source_invalidated"
                raise AgentError(f"agent_memory_candidate_source_stale:{reason}")

        now = _now()
        if decision == "reject":
            from . import memory_natural

            memory_natural.stamp_all_tasks_owner_messages_in_tx(data)
            mem["source_seq"] = int(mem.get("source_seq") or 0) + 1
            _write_tombstone(
                mem,
                {
                    "id": cand["id"],
                    "scope": cand["scope"],
                    "fact_key": cand["fact_key"],
                    "text": cand["text"],
                    "revision": cand["revision"],
                    "sources": cand.get("sources") or [],
                },
                reason="candidate_rejected",
                owner_action_id=client_request_id,
            )
            cand["status"] = "rejected"
            cand["revision"] += 1
            cand["updated_at"] = now
            mem["generation"] += 1
            _enforce_memory_bounds(mem)
            result = {"candidate": copy.deepcopy(cand), "entry": None, "generation": mem["generation"]}
            return _save_idempotency(mem, client_request_id, cached, result)

        if cand.get("target_entry_id"):
            target = mem["entries"].get(cand["target_entry_id"])
            if target and cand.get("expected_entry_revision") is not None:
                if target["revision"] != cand["expected_entry_revision"]:
                    cand["status"] = "stale"
                    cand["stale_reason"] = "target_entry_revision_changed"
                    raise AgentError("agent_memory_revision_conflict")

        final_text = _clean_text(edited_text if edited_text is not None else cand["text"], field="text")
        final_category = normalize_category(edited_category if edited_category is not None else cand["category"])
        final_scope = normalize_scope(edited_scope if edited_scope is not None else cand["scope"])
        final_key = normalize_fact_key(edited_fact_key if edited_fact_key is not None else cand["fact_key"], category=final_category, text=final_text)
        final_exposure = exposure_policy if exposure_policy is not None else cand.get("exposure_policy")

        upserted = upsert_active_entry(
            data,
            {
                "scope": final_scope,
                "category": final_category,
                "fact_key": final_key,
                "text": final_text,
                "kind": cand.get("kind", "owner_fact"),
                "sources": cand.get("sources") or [],
                "exposure_policy": final_exposure,
                "supersedes": [cand["target_entry_id"]] if cand.get("target_entry_id") else [],
                "create_superseding_entry": bool(cand.get("target_entry_id")),
            },
            origin="accepted_extraction",
        )
        cand["status"] = "accepted"
        cand["accepted_entry_id"] = upserted["entry"]["id"]
        cand["revision"] += 1
        cand["updated_at"] = now
        _enforce_memory_bounds(mem)
        result = {
            "candidate": copy.deepcopy(cand),
            "entry": upserted["entry"],
            "generation": mem["generation"],
            "policy_revision": mem["policy_revision"],
        }
        return _save_idempotency(mem, client_request_id, cached, result)


def record_branch_rewrite_fence(
    data: dict[str, Any],
    *,
    task_id: str,
    previous_conversation_revision: int,
    new_conversation_revision: int,
    retired_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fence discarded messages from an edited conversation branch and invalidate derived candidates/entries."""
    with _atomic_mutation(data) as mem:
        now = _now()
        retired_ids: list[str] = []
        fenced_msgs = mem["source_fences"].setdefault("fenced_messages", {})
        for msg in retired_messages:
            if not isinstance(msg, dict):
                continue
            mid = msg.get("message_id")
            if isinstance(mid, str) and mid:
                retired_ids.append(mid)
                text = msg.get("ui", {}).get("text") if isinstance(msg.get("ui"), dict) and "text" in msg["ui"] else msg.get("content", "")
                chash = message_content_sha256(text)
                fkey = _message_fence_key(task_id, mid, previous_conversation_revision, chash)
                fenced_msgs[fkey] = {
                    "reason": "branch_edited",
                    "at": now,
                    "reset_epoch": mem["reset_epoch"],
                    "task_id": task_id,
                    "message_id": mid,
                    "conversation_revision": int(previous_conversation_revision),
                    "content_sha256": chash,
                }

        mem["source_fences"].setdefault("retired_branches", []).append(
            {
                "task_id": task_id,
                "conversation_revision": previous_conversation_revision,
                "new_conversation_revision": new_conversation_revision,
                "retired_message_ids": retired_ids,
                "fenced_all": False,
                "at": now,
            }
        )

        invalidated_candidates = 0
        invalidated_entries = 0

        for cand in mem["candidates"].values():
            if cand.get("status") != "pending":
                continue
            for src in cand.get("sources") or []:
                ok, reason = verify_source_ref_against_state(data, src, for_background_job=False)
                if not ok:
                    cand["status"] = "stale"
                    cand["stale_reason"] = reason or "branch_edited"
                    cand["revision"] += 1
                    cand["updated_at"] = now
                    invalidated_candidates += 1
                    break

        for eid, entry in list(mem["entries"].items()):
            if entry.get("status") != "active":
                continue
            sources = entry.get("sources") or []
            task_sources = [s for s in sources if s.get("source_type") == "task_message"]
            if not task_sources:
                continue
            if all(not verify_source_ref_against_state(data, s, for_background_job=False, for_active_entry=True)[0] for s in task_sources):
                _write_tombstone(mem, entry, reason="source_branch_retired")
                mem["entries"].pop(eid, None)
                invalidated_entries += 1

        for job in mem["jobs"].values():
            if job.get("task_id") == task_id and job.get("state") in {"queued", "leased", "extracted"}:
                if any(not verify_source_ref_against_state(data, s, for_background_job=True)[0] for s in (job.get("source_manifest") or [])):
                    job["state"] = "stale"
                    job["error_code"] = "source_branch_retired"
                    job["updated_at"] = now

        mem["generation"] += 1
        if invalidated_entries:
            mem["policy_revision"] += 1
        _enforce_memory_bounds(mem)
        return {
            "fenced_message_ids": retired_ids,
            "invalidated_candidates": invalidated_candidates,
            "invalidated_entries": invalidated_entries,
        }


def record_task_lifecycle_fence(
    data: dict[str, Any],
    *,
    task_id: str,
    operation: str,
) -> dict[str, Any]:
    """Record task archive, restore, or permanent deletion in memory source fences."""
    with _atomic_mutation(data) as mem:
        now = _now()
        fences = mem["source_fences"]
        invalidated_candidates = 0
        invalidated_entries = 0

        if operation == "archive":
            fences.setdefault("archived_tasks", {})[task_id] = {"at": now}
            fences.get("relearn_tasks", {}).pop(task_id, None)
            for job in mem["jobs"].values():
                if job.get("task_id") == task_id and job.get("state") in {"queued", "leased", "extracted"}:
                    job["state"] = "stale"
                    job["error_code"] = "source_task_archived"
                    job["updated_at"] = now
        elif operation == "restore":
            fences.get("archived_tasks", {}).pop(task_id, None)
        elif operation == "delete":
            fences.setdefault("deleted_tasks", {})[task_id] = {"at": now, "reset_epoch": mem["reset_epoch"]}
            fences.get("archived_tasks", {}).pop(task_id, None)
            fences.get("relearn_tasks", {}).pop(task_id, None)
            for job in mem["jobs"].values():
                if job.get("task_id") == task_id and job.get("state") in {"queued", "leased", "extracted"}:
                    job["state"] = "stale"
                    job["error_code"] = "source_task_deleted"
                    job["updated_at"] = now
            for cand in mem["candidates"].values():
                if cand.get("status") == "pending" and any(s.get("task_id") == task_id for s in (cand.get("sources") or [])):
                    cand["status"] = "stale"
                    cand["stale_reason"] = "source_task_deleted"
                    cand["revision"] += 1
                    cand["updated_at"] = now
                    invalidated_candidates += 1
            for eid, entry in list(mem["entries"].items()):
                if entry.get("status") != "active":
                    continue
                sources = entry.get("sources") or []
                task_sources = [s for s in sources if s.get("source_type") == "task_message"]
                if task_sources and all(s.get("task_id") == task_id for s in task_sources) and entry.get("origin") != "manual":
                    _write_tombstone(mem, entry, reason="source_task_deleted")
                    mem["entries"].pop(eid, None)
                    invalidated_entries += 1

        mem["generation"] += 1
        if invalidated_entries:
            mem["policy_revision"] += 1
        _enforce_memory_bounds(mem)
        return {
            "task_id": task_id,
            "operation": operation,
            "invalidated_candidates": invalidated_candidates,
            "invalidated_entries": invalidated_entries,
        }


def set_manual_preference_exposure(
    data: dict[str, Any],
    preference_id: str,
    *,
    expected_preferences_revision: int,
    exposure_policy: dict[str, Any] | None,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    """Attach or update an optional destination exposure policy for a manual preference ID."""
    with _atomic_mutation(data) as mem:
        prefs = data.get("preferences") or {"revision": 0, "items": []}
        if prefs.get("revision", 0) != expected_preferences_revision:
            raise AgentError("agent_preferences_revision_conflict")
        pref_item = next(
            (item for item in (prefs.get("items") or []) if isinstance(item, dict) and item.get("id") == preference_id),
            None,
        )
        if pref_item is None:
            raise AgentError("agent_preference_not_found")

        req_payload = {
            "preference_id": preference_id,
            "expected_preferences_revision": expected_preferences_revision,
            "exposure_policy": exposure_policy,
        }
        hit, cached = _record_idempotency(mem, client_request_id, "pref_exposure", req_payload)
        if hit:
            return cached

        overrides = mem.setdefault("manual_preference_exposure_overrides", {})
        if exposure_policy is None:
            overrides.pop(preference_id, None)
            norm = None
        else:
            prev_rev = (overrides.get(preference_id) or {}).get("revision", 0)
            norm = normalize_exposure_policy(exposure_policy, prev_revision=prev_rev)
            overrides[preference_id] = {
                "preference_id": preference_id,
                "expected_preferences_revision": expected_preferences_revision,
                "text_sha256": message_content_sha256(str(pref_item.get("text") or "")),
                "exposure_policy": norm,
                "revision": norm["revision"],
                "updated_at": _now(),
            }
        mem["generation"] += 1
        mem["policy_revision"] += 1
        _enforce_memory_bounds(mem)
        result = {
            "preference_id": preference_id,
            "override": copy.deepcopy(overrides.get(preference_id)),
            "generation": mem["generation"],
            "policy_revision": mem["policy_revision"],
        }
        return _save_idempotency(mem, client_request_id, cached, result)


def filter_manual_preferences_for_task(
    data: dict[str, Any],
    row: dict[str, Any],
    prefs: dict[str, Any],
) -> dict[str, Any]:
    """Filter manual preferences using optional sidecar destination overrides while keeping GET/PUT /preferences unchanged."""
    items = prefs.get("items") or []
    if not items:
        return prefs
    mem = data.get("memory") if isinstance(data, dict) else None
    if not isinstance(mem, dict):
        return prefs
    overrides = mem.get("manual_preference_exposure_overrides") or {}
    if not overrides:
        return prefs
    pref_rev = prefs.get("revision", 0)
    task_dest = normalize_destination(
        row.get("model_destination") or ({"catalog_model_id": row["model_id"]} if row.get("model_id") else None)
    )
    filtered = []
    for item in items:
        if not isinstance(item, dict):
            continue
        pid = item.get("id")
        ov = overrides.get(pid)
        if isinstance(ov, dict):
            expected_hash = ov.get("text_sha256")
            if expected_hash:
                actual_hash = message_content_sha256(str(item.get("text") or ""))
                if actual_hash != expected_hash:
                    # Restricted preference changed without re-authorizing exposure: fail-closed
                    continue
            policy = ov.get("exposure_policy")
            if not exposure_allows_destination(policy, task_dest):
                continue
        filtered.append(item)
    return {"revision": pref_rev, "items": filtered}


def _tokenize(text: str) -> set[str]:
    tokens: set[str] = set()
    lowered = (text or "").lower()
    for word in re.findall(r"[a-z0-9][a-z0-9._\-]{1,31}", lowered):
        if word not in _STOPWORDS:
            tokens.add(word)
            for part in re.split(r"[._\-]+", word):
                if len(part) >= 2 and part not in _STOPWORDS:
                    tokens.add(part)
    for seq in re.findall(r"[\u4e00-\u9fff]{2,12}", lowered):
        if seq not in _STOPWORDS:
            tokens.add(seq)
        for n in (2, 3, 4):
            for i in range(len(seq) - n + 1):
                gram = seq[i : i + n]
                if gram not in _STOPWORDS:
                    tokens.add(gram)
    return tokens


def _expand_query_categories(query_tokens: set[str], raw_query: str) -> set[str]:
    matched_cats: set[str] = set()
    lowered = raw_query.lower()
    for cat, aliases in _CATEGORY_ALIASES.items():
        if cat in query_tokens or any(
            (alias in query_tokens if alias.isascii() else (alias in lowered or alias in query_tokens))
            for alias in aliases
        ):
            matched_cats.add(cat)
    if matched_cats & {"photography", "equipment", "gear"}:
        matched_cats.update({"photography", "equipment", "gear", "hobby"})
    return matched_cats


def score_entry_relevance(entry: dict[str, Any], query_text: str) -> float:
    if not query_text or not query_text.strip():
        return 0.0
    query_tokens = _tokenize(query_text)
    if not query_tokens:
        return 0.0
    expanded_cats = _expand_query_categories(query_tokens, query_text)
    entry_cat = (entry.get("category") or "").lower()
    entry_key = (entry.get("fact_key") or "").lower()
    entry_text = entry.get("text") or ""
    entry_tokens = _tokenize(f"{entry_cat} {entry_key} {entry_text}")

    overlap = query_tokens & entry_tokens
    score = float(len(overlap)) * 3.0

    if entry_cat in expanded_cats:
        score += 4.0
    for cat in expanded_cats:
        if cat in entry_key:
            score += 3.5
        for alias in _CATEGORY_ALIASES.get(cat, ()):
            if alias in entry_text.lower() or alias in entry_key:
                score += 1.5
    return score


def select_memories_for_task(
    data: dict[str, Any],
    row: dict[str, Any],
    *,
    query_text: str | None = None,
    remaining_context_bytes: int | None = None,
) -> dict[str, Any]:
    """Select active, scope- and destination-authorized, relevant memories within token/byte budgets. Pure read-only."""
    mem = read_memory_state(data)
    settings = mem["settings"]
    if not settings.get("use_enabled", True):
        return {
            "enabled": False,
            "selected": [],
            "profile_summary": [],
            "navigation_index": [],
            "omitted_count": 0,
            "policy_revision": mem["policy_revision"],
            "generation": mem["generation"],
            "reset_epoch": mem["reset_epoch"],
        }

    task_project = row.get("project") if isinstance(row.get("project"), dict) else None
    task_project_id = task_project.get("id") if task_project else None
    task_project_gen = task_project.get("generation") if task_project else None
    task_dest = normalize_destination(
        row.get("model_destination") or ({"catalog_model_id": row["model_id"]} if row.get("model_id") else None)
    )

    if query_text is None:
        owner_parts = []
        if isinstance(row.get("goal"), dict) and row["goal"].get("text"):
            owner_parts.append(str(row["goal"]["text"]))
        for msg in (row.get("messages") or [])[-6:]:
            if isinstance(msg, dict) and msg.get("role") == "user" and isinstance(msg.get("content"), str):
                owner_parts.append(msg["content"])
        query_text = "\n".join(owner_parts)

    is_automatic_mode = str(settings.get("activation_policy") or "owner_review") == "automatic"
    verified_entries: list[dict[str, Any]] = []
    eligible: list[tuple[float, dict[str, Any]]] = []
    for entry in mem["entries"].values():
        if entry.get("status") != "active":
            continue
        scope = entry.get("scope") or {}
        if scope.get("owner_id", "local_owner") != "local_owner":
            continue
        if scope.get("kind") == "project":
            if not _project_scope_matches(scope, task_project_id, task_project_gen, require_generation_match=True):
                continue
        if not exposure_allows_destination(entry.get("exposure_policy"), task_dest):
            continue
        sources = entry.get("sources") or []
        task_sources = [s for s in sources if s.get("source_type") == "task_message"]
        if task_sources and entry.get("origin") != "manual":
            if all(not verify_source_ref_against_state(data, s, for_background_job=False, for_active_entry=True)[0] for s in task_sources):
                continue

        verified_entries.append(entry)
        if not is_automatic_mode:
            relevance = score_entry_relevance(entry, query_text)
            if relevance > 0.0:
                eligible.append((relevance, entry))

    eligible.sort(key=lambda item: (-item[0], -float(item[1].get("updated_at") or 0.0), item[1]["id"]))

    max_tokens = int(settings["budgets"].get("max_memory_tokens", MAX_MEMORY_TOKENS))
    max_bytes = min(
        int(settings["budgets"].get("max_injected_bytes", MAX_INJECTED_BYTES)),
        max_tokens * 3,
    )
    if remaining_context_bytes is not None:
        if remaining_context_bytes < 2400:
            max_bytes = 0
        else:
            max_bytes = min(max_bytes, max(0, remaining_context_bytes // 12))

    selected: list[dict[str, Any]] = []
    used_bytes = 0
    if not is_automatic_mode:
        for _score, entry in eligible:
            projected_item = {
                "id": entry["id"],
                "scope": entry["scope"]["kind"],
                "project_id": entry["scope"].get("project_id"),
                "project_generation": entry["scope"].get("project_generation"),
                "category": entry["category"],
                "fact_key": entry["fact_key"],
                "text": entry["text"],
                "origin": entry["origin"],
                "revision": entry["revision"],
                "sources": [
                    {
                        "source_type": s.get("source_type"),
                        "task_id": s.get("task_id"),
                        "message_id": s.get("message_id"),
                        "conversation_revision": s.get("conversation_revision"),
                        "content_sha256": s.get("content_sha256", "")[:16],
                    }
                    for s in (entry.get("sources") or [])[:2]
                ],
            }
            item_bytes = len(encoded(projected_item))
            if used_bytes + item_bytes > max_bytes:
                continue
            selected.append(projected_item)
            used_bytes += item_bytes

    if is_automatic_mode:
        from . import memory_natural

        profile_summary, navigation_index, nav_bytes = memory_natural.build_navigation_and_summary(
            verified_entries,
            max_bytes,
        )
        include_nav = bool(navigation_index and max_bytes > 0)
    else:
        profile_summary = []
        navigation_index = []
        nav_bytes = 0
        include_nav = False

    return {
        "enabled": True,
        "selected": selected,
        "profile_summary": profile_summary,
        "navigation_index": navigation_index,
        "include_navigation": include_nav,
        "omitted_count": max(0, len(eligible) - len(selected)),
        "used_bytes": used_bytes + (nav_bytes if include_nav else 0),
        "max_bytes": max_bytes,
        "policy_revision": mem["policy_revision"],
        "generation": mem["generation"],
        "reset_epoch": mem["reset_epoch"],
    }


def search_memories(
    data: dict[str, Any],
    *,
    query: str = "",
    scope_kind: str | None = None,
    project_id: str | None = None,
    project_generation: int | None = None,
    category: str | None = None,
    topic: str | None = None,
    entry_ids: list[str] | None = None,
    fact_keys: list[str] | None = None,
    include_candidates: bool = False,
    destination: dict[str, str] | None = None,
    enforce_destination: bool = False,
    for_model_tool: bool = False,
    task_row: dict[str, Any] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Read-only search and source-detail inspection over active memories (and optionally candidates). Pure read-only."""
    mem = read_memory_state(data)
    if for_model_tool or task_row is not None:
        if not mem["settings"].get("use_enabled", True):
            return {
                "entries": [],
                "total": 0,
                "generation": mem["generation"],
                "reset_epoch": mem["reset_epoch"],
                "policy_revision": mem["policy_revision"],
            }
        include_candidates = False
        enforce_destination = True
        if isinstance(task_row, dict):
            task_project = task_row.get("project") if isinstance(task_row.get("project"), dict) else None
            task_pid = task_project.get("id") if task_project else None
            task_pgen = task_project.get("generation") if task_project else None
            if destination is None:
                destination = (
                    task_row.get("model_destination")
                    or ({"catalog_model_id": task_row["model_id"]} if task_row.get("model_id") else None)
                )
        else:
            task_pid = project_id
            task_pgen = project_generation
    else:
        task_pid = project_id
        task_pgen = project_generation

    from . import memory_natural

    norm_dest = normalize_destination(destination) if destination else None
    id_filter = {str(x).strip() for x in (entry_ids or []) if isinstance(x, str) and str(x).strip()}
    fk_filter = [str(k).strip().lower() for k in (fact_keys or []) if isinstance(k, str) and str(k).strip()]
    topic_clean = str(topic or "").strip().lower()
    norm_cat = normalize_category(category) if category else None
    results: list[tuple[float, dict[str, Any]]] = []
    authorized_pool: list[dict[str, Any]] = []

    for entry in mem["entries"].values():
        if entry.get("status") != "active":
            continue
        scope = entry.get("scope") or {}
        if scope.get("owner_id", "local_owner") != "local_owner":
            continue
        if scope_kind and scope.get("kind") != scope_kind:
            continue
        if scope.get("kind") == "project":
            if for_model_tool or task_row is not None:
                if not _project_scope_matches(scope, task_pid, task_pgen, require_generation_match=True):
                    continue
                if project_id is not None and project_id != task_pid:
                    continue
            elif project_id is not None or project_generation is not None:
                if not _project_scope_matches(
                    scope,
                    project_id,
                    project_generation,
                    require_generation_match=(project_generation is not None),
                ):
                    continue
        if enforce_destination and not exposure_allows_destination(entry.get("exposure_policy"), norm_dest):
            continue
        sources = entry.get("sources") or []
        task_sources = [s for s in sources if s.get("source_type") == "task_message"]
        if task_sources and entry.get("origin") != "manual":
            if all(not verify_source_ref_against_state(data, s, for_background_job=False, for_active_entry=True)[0] for s in task_sources):
                continue

        authorized_pool.append(entry)

        if id_filter and entry.get("id") not in id_filter:
            continue
        ekey_low = str(entry.get("fact_key") or "").lower()
        ecat_low = str(entry.get("category") or "").lower()
        kw_list = [str(k).lower() for k in (entry.get("keywords") or []) if isinstance(k, str)]
        kw_str = " ".join(kw_list)
        nav_topic = memory_natural.derive_navigation_topic(entry).lower()

        if norm_cat:
            if for_model_tool:
                if (
                    ecat_low != norm_cat
                    and norm_cat not in ekey_low.split(".")
                    and norm_cat not in kw_list
                    and not ({ecat_low, norm_cat} <= {"equipment", "gear", "photography"})
                ):
                    continue
            elif ecat_low != norm_cat:
                continue

        if fk_filter and not any(ekey_low == fk or ekey_low.startswith(fk + ".") or fk in ekey_low for fk in fk_filter):
            continue

        topic_matched = bool(
            topic_clean
            and (
                topic_clean in nav_topic
                or nav_topic in topic_clean
                or topic_clean in ekey_low
                or topic_clean in ecat_low
                or any(topic_clean in k or k in topic_clean for k in kw_list)
            )
        )
        structural_match = bool(id_filter or fk_filter or norm_cat or topic_matched)
        combined_query = " ".join(p for p in (query.strip(), topic_clean) if p).strip()
        score = score_entry_relevance(entry, combined_query) if combined_query else 1.0
        if combined_query and kw_str:
            kw_overlap = _tokenize(combined_query) & _tokenize(f"{nav_topic} {kw_str}")
            score += float(len(kw_overlap)) * 2.5
        if topic_matched:
            score += 4.0
        if combined_query and score <= 0.0 and not structural_match:
            q_lower = combined_query.lower()
            if (
                q_lower not in entry.get("text", "").lower()
                and q_lower not in ekey_low
                and q_lower not in kw_str
                and q_lower not in nav_topic
            ):
                continue
            score = 0.5
        results.append((max(score, 0.5 if structural_match else score), copy.deepcopy(entry)))

    if not results and for_model_tool and authorized_pool and not id_filter and not fk_filter and not norm_cat:
        # When the foreground model explicitly invokes ryn_memory_search to inspect
        # authorized memories from navigation_index using a free-form query/topic,
        # return the authorized active entries rather than an empty set.
        for entry in authorized_pool:
            results.append((0.1, copy.deepcopy(entry)))

    results.sort(key=lambda item: (-item[0], -float(item[1].get("updated_at") or 0.0), item[1]["id"]))
    bounded = [item[1] for item in results[: max(1, min(100, int(limit)))]]

    out: dict[str, Any] = {
        "entries": bounded,
        "total": len(results),
        "generation": mem["generation"],
        "reset_epoch": mem["reset_epoch"],
        "policy_revision": mem["policy_revision"],
    }
    if include_candidates:
        cands = []
        for c in mem["candidates"].values():
            if c.get("status") != "pending":
                continue
            c_scope = c.get("scope") or {}
            if c_scope.get("owner_id", "local_owner") != "local_owner":
                continue
            if scope_kind and c_scope.get("kind") != scope_kind:
                continue
            if c_scope.get("kind") == "project":
                if project_id is not None or project_generation is not None:
                    if not _project_scope_matches(
                        c_scope,
                        project_id,
                        project_generation,
                        require_generation_match=(project_generation is not None),
                    ):
                        continue
            elif project_id is not None and scope_kind == "project":
                continue
            if category and c.get("category") != normalize_category(category):
                continue
            if enforce_destination and not exposure_allows_destination(c.get("exposure_policy"), norm_dest):
                continue
            csources = c.get("sources") or []
            if csources and all(not verify_source_ref_against_state(data, s, for_background_job=False)[0] for s in csources):
                continue
            if query.strip():
                c_score = score_entry_relevance(c, query)
                if c_score <= 0.0:
                    q_lower = query.strip().lower()
                    if q_lower not in c.get("text", "").lower() and q_lower not in c.get("fact_key", "").lower():
                        continue
            cands.append(copy.deepcopy(c))
        cands.sort(key=lambda c: (-float(c.get("updated_at") or 0.0), c["id"]))
        out["candidates"] = cands
    return out


def list_memories_snapshot(
    data: dict[str, Any],
    *,
    scope_kind: str | None = None,
    project_id: str | None = None,
    project_generation: int | None = None,
    status: str = "active",
) -> dict[str, Any]:
    """Pure read-only snapshot of memory entries, candidates, and settings."""
    mem = read_memory_state(data)
    entries = []
    for entry in mem["entries"].values():
        if status != "all" and entry.get("status") != status:
            continue
        scope = entry.get("scope") or {}
        if scope_kind and scope.get("kind") != scope_kind:
            continue
        if project_id is not None or project_generation is not None:
            if scope.get("kind") != "project":
                continue
            if not _project_scope_matches(
                scope,
                project_id,
                project_generation,
                require_generation_match=(project_generation is not None),
            ):
                continue
        sources = entry.get("sources") or []
        task_sources = [s for s in sources if s.get("source_type") == "task_message"]
        if status == "active" and task_sources and entry.get("origin") != "manual":
            if all(not verify_source_ref_against_state(data, s, for_background_job=False, for_active_entry=True)[0] for s in task_sources):
                continue
        entries.append(copy.deepcopy(entry))
    entries.sort(key=lambda e: (-float(e.get("updated_at") or 0.0), e["id"]))

    candidates = []
    for cand in mem["candidates"].values():
        if cand.get("status") != "pending" and status != "all":
            continue
        scope = cand.get("scope") or {}
        if scope_kind and scope.get("kind") != scope_kind:
            continue
        if project_id is not None or project_generation is not None:
            if scope.get("kind") != "project":
                continue
            if not _project_scope_matches(
                scope,
                project_id,
                project_generation,
                require_generation_match=(project_generation is not None),
            ):
                continue
        csources = cand.get("sources") or []
        if cand.get("status") == "pending" and csources:
            if all(not verify_source_ref_against_state(data, s, for_background_job=False)[0] for s in csources):
                continue
        candidates.append(copy.deepcopy(cand))
    candidates.sort(key=lambda c: (-float(c.get("updated_at") or 0.0), c["id"]))

    return {
        "schema_version": mem["schema_version"],
        "generation": mem["generation"],
        "reset_epoch": mem["reset_epoch"],
        "policy_revision": mem["policy_revision"],
        "settings": copy.deepcopy(mem["settings"]),
        "entries": entries,
        "candidates": candidates,
        "tombstone_count": len(mem.get("tombstones") or {}),
        "manual_preference_exposure_overrides": copy.deepcopy(mem.get("manual_preference_exposure_overrides") or {}),
    }
