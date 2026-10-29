"""Immutable encrypted large tool outputs; references confer no resource access."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid

from ..atomic_io import atomic_write_json, read_json
from ..services import peer_box
from .store import AgentError, encoded

MAX_RESULT_BYTES = 2 * 1024 * 1024
CHANNEL = b"rynmesh-agent-result-v1"
WORKSTATION_INLINE_PREVIEW_FIELDS = {
    "ryn_file_read": "text",
    "ryn_shell_execute": "output",
    "ryn_process_read": "output",
    "ryn_code_exec": "output",
}


def truncate_head_tail(text: str, max_chars: int, *, reference_id: str | None = None) -> str:
    """Codex truncate.rs-aligned head + tail string truncation."""
    if not isinstance(text, str) or len(text) <= max_chars or max_chars <= 0:
        return text if isinstance(text, str) else ""
    ref_hint = f"; full output in {reference_id}" if reference_id else ""
    marker_reserve = 96 + len(ref_hint)
    if max_chars <= marker_reserve:
        return text[:max_chars]
    budget = max_chars - marker_reserve
    head_budget = budget // 2
    tail_budget = budget - head_budget
    omitted = max(0, len(text) - head_budget - tail_budget)
    marker = f"\n... [{omitted} chars truncated{ref_hint}] ...\n"
    return text[:head_budget] + marker + (text[len(text) - tail_budget:] if tail_budget > 0 else "")


def format_reference_result(
    tool_name: str,
    result: object,
    reference: dict,
    *,
    max_bytes: int,
    note: str = "Full output is encrypted locally. Use ryn_read_result for bounded pages; this reference is not proof of the requested outcome.",
) -> dict:
    base = {
        "result_ref": reference["id"],
        "output_sha256": reference["sha256"],
        "bytes": reference["bytes"],
        "note": note,
    }
    field = WORKSTATION_INLINE_PREVIEW_FIELDS.get(tool_name)
    if field and isinstance(result, dict) and isinstance(result.get(field), str):
        preview = {
            key: value
            for key, value in result.items()
            if key != field and isinstance(value, (str, int, float, bool, type(None)))
        }
        preview.update(base)
        preview["truncated_inline"] = True
        overhead = len(encoded({**preview, field: ""}))
        available_bytes = max_bytes - overhead - 64
        if available_bytes < 128:
            return base
        raw_text = result[field]
        char_limit = min(len(raw_text), available_bytes)
        while char_limit >= 64:
            candidate_text = truncate_head_tail(raw_text, char_limit, reference_id=reference["id"])
            candidate = {**preview, field: candidate_text}
            if len(encoded(candidate)) <= max_bytes:
                return candidate
            char_limit = int(char_limit * 0.75) - 16
        return base

    if (
        isinstance(result, dict)
        and (tool_name.startswith("mcp__") or tool_name in {"ryn_computer_read", "ryn_computer_action"})
    ):
        preview = {
            key: value
            for key, value in result.items()
            if key not in {"structuredContent", "content", "native_observation_artifacts"}
            and isinstance(value, (str, int, float, bool, type(None)))
        }
        for art_key in ("images", "native_observation_artifacts", "image_artifacts", "mcp_artifacts"):
            if isinstance(result.get(art_key), list):
                art_bytes = len(encoded(result[art_key]))
                if art_bytes <= max(2048, max_bytes // 3):
                    preview[art_key] = result[art_key]
        sc = result.get("structuredContent")
        if isinstance(sc, dict):
            scalar_sc = {}
            for k, v in sc.items():
                if isinstance(v, list) and k != "rect":
                    continue
                if k in {"screenshot_b64", "screenshot"}:
                    continue
                if k == "observation" and isinstance(v, dict):
                    scalar_sc["observation"] = {
                        ok: ov for ok, ov in v.items() if ok != "elements"
                    }
                else:
                    scalar_sc[k] = v
            if len(encoded(scalar_sc)) <= max(1024, max_bytes // 4):
                preview["structuredContent"] = scalar_sc
        preview.update(base)
        preview["truncated_inline"] = True
        if isinstance( preview.get("text"), str) and len(preview["text"]) > max(512, max_bytes // 3):
            preview["text"] = truncate_head_tail(
                preview["text"], max(256, max_bytes // 4), reference_id=reference["id"]
            )
        structured_obj = (
            result.get("structuredContent")
            if "structuredContent" in result
            else {k: v for k, v in result.items() if k not in preview and k != "native_observation_artifacts"}
        )
        raw_structured = encoded(structured_obj).decode("utf-8", errors="replace")
        overhead = len(encoded({**preview, "structured_preview": ""}))
        available_bytes = max_bytes - overhead - 64
        if available_bytes >= 128:
            char_limit = min(len(raw_structured), available_bytes)
            while char_limit >= 64:
                candidate_preview = truncate_head_tail(
                    raw_structured, char_limit, reference_id=reference["id"]
                )
                candidate = {**preview, "structured_preview": candidate_preview}
                if len(encoded(candidate)) <= max_bytes:
                    return candidate
                char_limit = int(char_limit * 0.75) - 16
        if len(encoded(preview)) <= max_bytes:
            return preview
    return base


def path(store, task_id, result_id):
    if not isinstance(result_id, str) or not re.fullmatch(r"result_[a-f0-9]{32}", result_id):
        raise AgentError("agent_result_not_found")
    return store.root / "results" / hashlib.sha256(task_id.encode()).hexdigest() / (result_id + ".json")


def save(store, row, call, value):
    raw = encoded(value)
    if len(raw) > MAX_RESULT_BYTES:
        raise AgentError("agent_result_storage_limit")
    result_id = "result_" + uuid.uuid4().hex
    reference = {"id": result_id, "task_id": row["id"], "tool": call["name"], "operation_id": call["operation_id"],
                 "goal_revision": row["goal"]["revision"], "scope_revision": row["scope_revision"], "context_epoch": row.get("context_epoch", 0),
                 "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    state = store.inspect(lambda data: copy.deepcopy((data.get("tasks", {}).get(row["id"]) or row).get("data_policy_state")))
    if state:
        reference["data_policy_state"] = state
    nonce, ciphertext = peer_box.seal(store.key, store.pub, raw, info=CHANNEL)
    atomic_write_json(path(store, row["id"], result_id), {"nonce": nonce, "ciphertext": ciphertext}, max_bytes=3 * MAX_RESULT_BYTES)
    return reference


def checkpoint_note(row: dict) -> str:
    """List active large result_refs across checkpoints so their IDs remain discoverable."""
    if not isinstance(row, dict) or not row.get("context_checkpoint"):
        return ""
    epoch = row.get("context_epoch", 0)
    active = [
        {"result_id": ref["id"], "tool": ref.get("tool", ""), "bytes": ref.get("bytes", 0)}
        for ref in (row.get("result_refs") or [])
        if isinstance(ref, dict) and ref.get("context_epoch", 0) == epoch and isinstance(ref.get("id"), str)
    ]
    if not active:
        return ""
    return (
        "\nRetained large result references (use ryn_read_result): "
        + json.dumps(active[:16], ensure_ascii=False, separators=(",", ":"))
    )


def _compacted_history_slice(row: dict) -> list[dict]:
    checkpoint = row.get("context_checkpoint") if isinstance(row, dict) else None
    if not isinstance(checkpoint, dict):
        return []
    through = int(checkpoint.get("through") or 0)
    if through <= 0:
        return []
    epoch = row.get("context_epoch", 0)
    history = [
        msg for msg in (row.get("messages") or [])
        if isinstance(msg, dict) and (msg.get("role") == "user" or msg.get("context_epoch", 0) == epoch)
    ]
    return history[:through]


def _read_compacted_entry(row: dict, result_id: str) -> tuple[object, dict]:
    compacted = _compacted_history_slice(row)
    if not compacted:
        raise AgentError("agent_result_not_found")
    epoch = row.get("context_epoch", 0)
    call_tools: dict[str, str] = {}
    for msg in compacted:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and isinstance(tc.get("id"), str):
                    fn = tc.get("function") or {}
                    call_tools[tc["id"]] = str(fn.get("name") or "")
    if result_id in {"checkpoint_index", "compacted"}:
        items = []
        for msg in compacted:
            if msg.get("role") == "tool" and msg.get("context_epoch", 0) == epoch:
                cid = str(msg.get("tool_call_id") or "")
                raw_content = str(msg.get("content") or "")
                items.append({
                    "call_id": cid,
                    "tool": call_tools.get(cid, ""),
                    "bytes": len(raw_content.encode("utf-8")),
                })
        refs = [
            {"result_id": ref["id"], "tool": ref.get("tool", ""), "bytes": ref.get("bytes", 0)}
            for ref in (row.get("result_refs") or [])
            if isinstance(ref, dict) and ref.get("context_epoch", 0) == epoch and isinstance(ref.get("id"), str)
        ]
        value = {"compacted_tool_results": items, "result_refs": refs}
        raw = encoded(value)
        return value, {"id": result_id, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "context_epoch": epoch}

    for msg in compacted:
        if msg.get("role") != "tool":
            continue
        cid = str(msg.get("tool_call_id") or "")
        if cid and (result_id == cid or result_id == f"call_{cid}"):
            if msg.get("context_epoch", 0) != epoch:
                raise AgentError("agent_result_scope_changed")
            raw_text = str(msg.get("content") or "")
            try:
                value = json.loads(raw_text)
            except (ValueError, TypeError):
                value = {"content": raw_text}
            raw = encoded(value)
            ref = {
                "id": result_id,
                "tool": call_tools.get(cid, ""),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "context_epoch": epoch,
            }
            if isinstance(msg.get("resource_origin"), dict):
                ref["resource_origin"] = msg["resource_origin"]
            return value, ref
    raise AgentError("agent_result_not_found")


def read(store, row, result_id, *, provider=False):
    reference = next((ref for ref in row.get("result_refs", []) if ref["id"] == result_id), None)
    if reference is None:
        if provider and isinstance(result_id, str) and row.get("context_checkpoint"):
            return _read_compacted_entry(row, result_id)
        raise AgentError("agent_result_not_found")
    if provider and reference["context_epoch"] != row.get("context_epoch", 0):
        raise AgentError("agent_result_scope_changed")
    try:
        envelope = read_json(path(store, row["id"], result_id), max_bytes=3 * MAX_RESULT_BYTES)
        raw = peer_box.open_sealed(store.key, store.pub, envelope["nonce"], envelope["ciphertext"], info=CHANNEL)
        if len(raw) > MAX_RESULT_BYTES or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            raise ValueError
        return json.loads(raw), reference
    except Exception:
        raise AgentError("agent_result_unreadable") from None


def page(store, row, result_id, offset=0, limit=8192, *, provider=False):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 64 <= limit <= 16384:
        raise AgentError("agent_invalid_cursor")
    value, reference = read(store, row, result_id, provider=provider)
    text = encoded(value).decode()
    if offset > len(text):
        raise AgentError("agent_invalid_cursor")
    end = min(len(text), offset + limit)
    return {"id": result_id, "sha256": reference["sha256"], "bytes": reference["bytes"], "characters": len(text),
            "offset": offset, "next_offset": end if end < len(text) else None, "content": text[offset:end]}


def purge(store, task_id):
    root = (store.root / "results").resolve()
    directory = root / hashlib.sha256(task_id.encode()).hexdigest()
    if not directory.exists():
        return
    if not directory.resolve().is_relative_to(root):
        raise AgentError("agent_result_cleanup_failed")
    # Includes orphan ciphertext from a crash before its reference was committed.
    for file in directory.iterdir():
        if re.fullmatch(r"result_[a-f0-9]{32}\.json|\.result_[a-f0-9]{32}\.json\.[a-f0-9]{32}\.tmp", file.name):
            file.unlink()
