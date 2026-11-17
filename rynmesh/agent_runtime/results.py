"""Immutable encrypted large tool outputs; references confer no resource access."""
from __future__ import annotations

import copy
from collections import OrderedDict
import hashlib
import json
import re
import uuid
from threading import RLock

from cryptography.exceptions import InvalidTag

from ..atomic_io import atomic_write_json, read_json
from ..services import peer_box
from . import diagnostic_log
from .store import AgentError, AgentToolRejected, encoded

MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_PAGE_CHARS = 65536
_TEXT_CACHE = OrderedDict()
_TEXT_CACHE_LOCK = RLock()
_TEXT_CACHE_MAX_CHARS = 8 * 1024 * 1024
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
    if reference.get("truncated") is True:
        base["truncated"] = True
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
        for art_key in ("images", "native_observation_artifacts", "image_artifacts", "mcp_artifacts", "files"):
            if isinstance(result.get(art_key), list):
                art_bytes = len(encoded(result[art_key]))
                if art_bytes <= max(2048, max_bytes // 3):
                    preview[art_key] = result[art_key]
        for meta_key in ("truncated", "structured_truncated"):
            if isinstance(result.get(meta_key), dict):
                preview[meta_key] = result[meta_key]
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
        if isinstance(preview.get("text"), str) and len(preview["text"]) > max(512, max_bytes // 3):
            preview["text"] = truncate_head_tail(
                preview["text"], max(256, max_bytes // 4), reference_id=reference["id"]
            )
        if isinstance(result.get("truncated"), dict):
            preview["truncated"] = result["truncated"]
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


def _fit_json_string_field(shell: dict, field: str, text: str, max_bytes: int) -> bytes:
    empty_raw = encoded({**shell, field: ""})
    if len(empty_raw) >= max_bytes:
        minimal = {"truncated": True, field: ""}
        min_overhead = len(encoded(minimal))
        cap = max(0, max_bytes - min_overhead - 8)
        prefix = text.encode("utf-8")[:cap].decode("utf-8", errors="ignore")
        while True:
            cand = encoded({"truncated": True, field: prefix})
            if len(cand) <= max_bytes or not prefix:
                return cand
            prefix = prefix[: max(0, len(prefix) - max(1, (len(cand) - max_bytes) // 2 + 1))]
    avail = max(0, max_bytes - len(empty_raw))
    prefix = text.encode("utf-8")[:avail].decode("utf-8", errors="ignore")
    while True:
        cand = encoded({**shell, field: prefix})
        if len(cand) <= max_bytes or not prefix:
            return cand
        excess = len(cand) - max_bytes
        step = max(1, excess // 2 + 16)
        prefix = prefix[: max(0, len(prefix) - step)]


def _truncate_encoded_result(value: object, raw: bytes, max_bytes: int) -> bytes:
    original_bytes = len(raw)
    if isinstance(value, dict):
        kept: dict = {}
        for k, v in value.items():
            if k in {"text", "output", "content", "structuredContent"}:
                continue
            if isinstance(v, (str, int, float, bool, type(None))) and (not isinstance(v, str) or len(v) <= 4096):
                kept[k] = v
            elif k in {"mcp_artifacts", "files", "images", "structured_truncated"} and len(encoded(v)) <= max_bytes // 8:
                kept[k] = v
        kept["truncated"] = value["truncated"] if isinstance(value.get("truncated"), dict) else True
        kept["original_bytes"] = original_bytes
        for str_field in ("text", "output"):
            if isinstance(value.get(str_field), str) and value[str_field]:
                return _fit_json_string_field(kept, str_field, value[str_field], max_bytes)
        raw_prefix = raw[:max_bytes].decode("utf-8", errors="ignore")
        return _fit_json_string_field(kept, "content", raw_prefix, max_bytes)
    raw_prefix = raw[:max_bytes].decode("utf-8", errors="ignore")
    return _fit_json_string_field(
        {"truncated": True, "original_bytes": original_bytes},
        "content",
        raw_prefix,
        max_bytes,
    )


def seal(store, row, call, value):
    """Write immutable ciphertext before the caller's reference transaction."""
    raw = encoded(value)
    was_truncated = False
    original_bytes = len(raw)
    if original_bytes > MAX_RESULT_BYTES:
        raw = _truncate_encoded_result(value, raw, MAX_RESULT_BYTES)
        was_truncated = True
    result_id = "result_" + uuid.uuid4().hex
    reference = {"id": result_id, "task_id": row["id"], "tool": call["name"], "operation_id": call["operation_id"],
                 "goal_revision": row["goal"]["revision"], "scope_revision": row["scope_revision"], "context_epoch": row.get("context_epoch", 0),
                 "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    if was_truncated:
        reference["truncated"] = True
        reference["original_bytes"] = original_bytes
    state = store.inspect(lambda data: copy.deepcopy((data.get("tasks", {}).get(row["id"]) or row).get("data_policy_state")))
    if state:
        reference["data_policy_state"] = state
    nonce, ciphertext = peer_box.seal(store.key, store.pub, raw, info=CHANNEL)
    atomic_write_json(path(store, row["id"], result_id), {"nonce": nonce, "ciphertext": ciphertext}, max_bytes=3 * MAX_RESULT_BYTES)
    return reference


def save(store, row, call, value):
    """Compatibility entry point; callers register the returned reference."""
    return seal(store, row, call, value)


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
    note = (
        "\nRetained large result references (use ryn_read_result): "
        + json.dumps(active[-16:], ensure_ascii=False, separators=(",", ":"))
    )
    if len(active) > 16:
        note += "\n" + json.dumps({"older_result_ids_omitted": len(active) - 16}, separators=(",", ":"))
        note += "\nUse ryn_history_search with the tool_name filter to recover older result IDs."
    return note



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


def _result_text(store, row, result_id, *, provider=False):
    reference = next((ref for ref in row.get("result_refs", []) if ref["id"] == result_id), None)
    if reference is None:
        if provider and isinstance(result_id, str) and row.get("context_checkpoint"):
            value, reference = _read_compacted_entry(row, result_id)
            return encoded(value).decode(), reference
        raise AgentError("agent_result_not_found")
    if reference.get("expired"):
        raise AgentToolRejected("agent_result_expired", correction="This old result expired after the local storage limit was reached. Read the original source again if it is still authorized; this reference cannot be restored by retrying.")
    if provider and reference["context_epoch"] != row.get("context_epoch", 0):
        raise AgentError("agent_result_scope_changed")
    cache_key = (row["id"], result_id, reference["sha256"])
    with _TEXT_CACHE_LOCK:
        cached = _TEXT_CACHE.get(cache_key)
        if cached is not None:
            _TEXT_CACHE.move_to_end(cache_key)
            return cached, reference
    try:
        envelope = read_json(path(store, row["id"], result_id), max_bytes=3 * MAX_RESULT_BYTES)
        raw = peer_box.open_sealed(store.key, store.pub, envelope["nonce"], envelope["ciphertext"], info=CHANNEL)
        if len(raw) > MAX_RESULT_BYTES or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            raise ValueError
        text = encoded(json.loads(raw)).decode()
        with _TEXT_CACHE_LOCK:
            _TEXT_CACHE[cache_key] = text
            _TEXT_CACHE.move_to_end(cache_key)
            while len(_TEXT_CACHE) > 4 or sum(map(len, _TEXT_CACHE.values())) > _TEXT_CACHE_MAX_CHARS:
                _TEXT_CACHE.popitem(last=False)
        return text, reference
    except (OSError, InvalidTag, ValueError, KeyError, TypeError) as exc:
        cause = exc.__cause__ or exc  # read_json wraps filesystem and JSON errors.
        if isinstance(cause, FileNotFoundError):
            code = "agent_result_missing"
        elif isinstance(cause, OSError):
            code = "agent_result_unreadable"
        else:
            code = "agent_result_corrupt"
        diagnostic_log.failure("result_read", exc, task_id=row["id"])
        raise AgentError(code) from None


def read(store, row, result_id, *, provider=False):
    text, reference = _result_text(store, row, result_id, provider=provider)
    return json.loads(text), reference


def page(store, row, result_id, offset=0, limit=8192, *, provider=False, max_bytes=None, return_reference=False):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 64 <= limit <= MAX_PAGE_CHARS:
        raise AgentError("agent_invalid_cursor")
    text, reference = _result_text(store, row, result_id, provider=provider)
    if offset > len(text):
        raise AgentError("agent_invalid_cursor")
    budget = row["limits"]["max_output_chars"] - 512 if max_bytes is None else max_bytes
    length = min(limit, budget * 3 // 4, len(text) - offset)
    out = {"id": result_id, "sha256": reference["sha256"], "bytes": reference["bytes"], "characters": len(text),
           "offset": offset}
    if reference.get("truncated") is True:
        out["truncated"] = True
    while True:
        end = offset + length
        out.update(next_offset=end if end < len(text) else None, content=text[offset:end])
        if len(encoded(out)) <= budget:
            return (out, reference) if return_reference else out
        if not length:
            raise AgentError("agent_result_page_limit")
        length = int(length * 0.8)


def lines(store, row, result_id, *, pattern=None, context_lines=1, line_start=None, line_count=100,
          provider=False, max_bytes=None, return_reference=False):
    if (pattern is not None and line_start is not None
            or pattern is not None and (not isinstance(pattern, str) or not 1 <= len(pattern) <= 200)
            or type(context_lines) is not int or not 0 <= context_lines <= 5
            or type(line_count) is not int or not 1 <= line_count <= 400
            or line_start is not None and (type(line_start) is not int or line_start < 1)):
        raise AgentError("agent_invalid_cursor")
    text, reference = _result_text(store, row, result_id, provider=provider)
    value = json.loads(text)
    field = None
    if isinstance(value, str):
        text = value
    elif isinstance(value, dict):
        field = next((key for key in ("text", "output", "content") if isinstance(value.get(key), str)), None)
        if field:
            text = value[field]
    source = text.split("\n")
    total = len(source)
    if pattern is not None:
        hits = [i for i, line in enumerate(source) if pattern in line]
        chosen_hits = set(hits[:40])
        indices = sorted({i for hit in hits[:40]
                          for i in range(max(0, hit - context_lines), min(total, hit + context_lines + 1))})
        more = len(hits) > 40
    else:
        start = (line_start or 1) - 1
        if start > total:
            raise AgentError("agent_invalid_cursor")
        indices = list(range(start, min(total, start + line_count)))
        more = bool(indices and indices[-1] + 1 < total)
        chosen_hits = set()
    out = {"id": result_id, "sha256": reference["sha256"], "bytes": reference["bytes"],
           "total_lines": total, "lines": [], "next_line": None}
    if field:
        out["field"] = field
    budget = row["limits"]["max_output_chars"] - 512 if max_bytes is None else max_bytes
    for index in indices:
        item = {"line": index + 1, "text": source[index]}
        if pattern is not None:
            item["match"] = index in chosen_hits
        candidate = {**out, "lines": [*out["lines"], item], "next_line": index + 2 if index + 1 < total else None}
        if len(encoded(candidate)) > budget:
            if out["lines"]:
                out["next_line"] = index + 1
                break
            item["truncated"] = True
            length = min(len(item["text"]), budget * 3 // 4)
            while True:
                item["text"] = source[index][:length]
                if len(encoded(candidate)) <= budget:
                    out = candidate
                    break
                if not length:
                    raise AgentError("agent_result_page_limit")
                length = int(length * 0.8)
            break
        out = candidate
    else:
        out["next_line"] = indices[-1] + 2 if more and indices else None
    return (out, reference) if return_reference else out


def purge(store, task_id):
    with _TEXT_CACHE_LOCK:
        for key in list(_TEXT_CACHE):
            if key[0] == task_id:
                del _TEXT_CACHE[key]
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


RESULT_STORAGE_BYTES = 64 * 1024 * 1024


def prune(store, row):
    """Retire old, unprotected results and invisible unreferenced ciphertext."""
    refs = row.get("result_refs", [])
    directory = store.root / "results" / hashlib.sha256(row["id"].encode()).hexdigest()
    if not directory.exists():
        return
    if not directory.resolve().is_relative_to((store.root / "results").resolve()):
        raise AgentError("agent_result_cleanup_failed")
    checkpoint = encoded(row.get("context_checkpoint") or {})
    retained = {ref["id"]: ref for ref in refs if not ref.get("expired")}
    files = []
    for file in directory.iterdir():
        if not re.fullmatch(r"result_[a-f0-9]{32}\.json", file.name):
            continue
        ref = retained.get(file.stem)
        if ref is None:
            file.unlink()
        else:
            info = file.stat()
            files.append((info.st_mtime_ns, file, ref, info.st_size))
    total = sum(size for _, _, _, size in files)
    for _, file, ref, size in sorted(files, key=lambda item: item[0]):
        if total <= RESULT_STORAGE_BYTES:
            break
        if ref.get("context_epoch", 0) >= row.get("context_epoch", 0) or ref["id"].encode() in checkpoint:
            continue
        file.unlink()
        ref["expired"] = True
        total -= size
