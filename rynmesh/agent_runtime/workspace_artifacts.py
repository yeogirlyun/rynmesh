"""Files a task produced in its local workspace, identified from confirmed file-tool receipts.

Confirmed writes, command manifests and named file reads make a file an artifact: a file name
that appears in a model answer without such a receipt is never presented (it is reported
as an unbacked mention instead). One stable id per path keeps an artifact link valid when
a later turn revises the same file, and every read re-checks the bytes on disk against the
last receipt, so a file changed by something else is flagged, never silently passed off.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

from .store import AgentError

FILE_TOOLS = frozenset({"ryn_file_write", "ryn_file_edit", "ryn_file_manage", "ryn_apply_patch"})
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
IMAGE_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}
TEXT_TYPES = {".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv", ".json": "application/json",
              ".html": "text/html", ".htm": "text/html", ".css": "text/css", ".js": "text/javascript",
              ".ts": "text/plain", ".tsx": "text/plain", ".py": "text/x-python", ".xml": "application/xml",
              ".yaml": "text/plain", ".yml": "text/plain", ".toml": "text/plain", ".ini": "text/plain",
              ".log": "text/plain", ".ps1": "text/plain", ".sh": "text/plain", ".bat": "text/plain"}
DOCUMENT_TYPES = {".pdf": "application/pdf", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                  ".xls": "application/vnd.ms-excel", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                  ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".zip": "application/zip"}
HELPER_TYPES = frozenset({
    ".py", ".pyc", ".pyo", ".json", ".jsonl", ".sh", ".bash", ".zsh",
    ".ps1", ".bat", ".cmd", ".js", ".mjs", ".cjs", ".ts", ".tsx",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".log", ".tmp", ".bak",
})
# Served bytes never run with the application's privileges: no scripts, no requests, a unique origin.
CONTENT_SECURITY_POLICY = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; sandbox"
# Names that look like a file in an answer: word characters, dots and dashes (no spaces), then a known
# deliverable extension. .js/.ts/.css are left out: "node.js" or "Next.js" in prose is a product, not a file.
MENTION = re.compile(r"(?<![\w.-])([\w][\w.-]{0,120}\.(?:svg|png|jpe?g|gif|webp|html?|md|txt|csv|json|pdf|py|xlsx?|docx?|pptx?|zip))\b",
                     re.IGNORECASE)


def artifact_id(path) -> str:
    return "file_" + hashlib.sha256(os.path.normcase(os.path.normpath(str(path))).encode("utf-8")).hexdigest()[:24]


def media_type(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return IMAGE_TYPES.get(suffix) or TEXT_TYPES.get(suffix) or DOCUMENT_TYPES.get(suffix) or "application/octet-stream"


def preview_kind(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return "image" if suffix in IMAGE_TYPES else "text" if suffix in TEXT_TYPES else "download"


def _touch(files, receipt, path, sha256, created, source="write"):
    stamp = receipt.get("confirmed_at") or receipt.get("started_at")
    key = artifact_id(path)
    state = files.get(key)
    if state is None or state["deleted"]:
        state = files[key] = {"id": key, "path": str(path), "created_by_task": created, "first_at": stamp,
                              "revisions": 0, "goal_revisions": [], "source": source}
    state.update(sha256=sha256, updated_at=stamp, deleted=False, operation_id=receipt["operation_id"], source=source)
    state["created_by_task"] |= bool(created)
    state["revisions"] += 1
    if receipt.get("goal_revision") not in state["goal_revisions"]:
        state["goal_revisions"].append(receipt.get("goal_revision"))


def _remove(files, receipt, path):
    state = files.get(artifact_id(path))
    if state is not None:
        state.update(deleted=True, updated_at=receipt.get("confirmed_at") or receipt.get("started_at"))


def history(row) -> dict:
    """Replay confirmed file-tool receipts in order: the last known state of every touched path."""
    files = {}
    receipts = [*row.get("receipts", []), *(row.get("workspace_file_receipts") or {}).values()]
    receipts.sort(key=lambda receipt: receipt.get("confirmed_at") or receipt.get("started_at") or "")
    # Files referenced in any owner-visible answer stay available in later turns.
    answers = "\n".join([row.get("result") or "", *[message.get("content") or "" for message in row.get("messages", [])
                      if message.get("role") == "assistant" and not message.get("tool_calls")
                      and (message.get("ui") or {}).get("kind") not in {"change_review", "verification_feedback"}]]).casefold()
    for receipt in receipts:
        if receipt.get("status") != "confirmed":
            continue
        result = receipt.get("result")
        if not isinstance(result, dict) or result.get("error") or result.get("result_ref"):
            continue
        tool, arguments = receipt["tool"], receipt.get("arguments") or {}
        if tool == "ryn_craft_action" and result.get("ok"):
            for item in result.get("files", []):
                if isinstance(item, dict) and item.get("path") and item.get("sha256"):
                    _touch(files, receipt, item["path"], item["sha256"], True)
        elif tool in {"ryn_shell_execute", "ryn_process_read", "ryn_process_stop"}:
            for item in result.get("workspace_files", []):
                if not isinstance(item, dict) or not item.get("path"):
                    continue
                if item.get("deleted"):
                    _remove(files, receipt, item["path"])
                elif "sha256" in item:
                    previous = files.get(artifact_id(item["path"]))
                    if (previous is None or previous.get("sha256") != item["sha256"]
                            or item["sha256"] is None and previous.get("command_stamp") != item.get("modified_ns")):
                        # A directory diff observes timing, not which process wrote
                        # bytes. Concurrent editors must never become "Ryn writes".
                        _touch(files, receipt, item["path"], item["sha256"], False, "command")
                        files[artifact_id(item["path"])]["new_during_command"] = item.get("created", False)
                        files[artifact_id(item["path"])]["command_stamp"] = item.get("modified_ns")
        elif tool in {"ryn_file_read", "ryn_render_inspect"} and result.get("path") and result.get("sha256"):
            path = result["path"]
            # A referenced existing file is openable, but never claimed as generated by this task.
            if (Path(path).name.casefold() in answers or artifact_id(path) in row.get("referenced_file_ids", [])) and artifact_id(path) not in files:
                _touch(files, receipt, path, result["sha256"], False, "read")
                files[artifact_id(path)]["revisions"] = 0
        elif tool in {"ryn_file_write", "ryn_file_edit"} and result.get("written") and result.get("path"):
            created = tool == "ryn_file_write" and (receipt.get("preview") or {}).get("before_sha256") is None
            _touch(files, receipt, result["path"], result.get("sha256"), created)
        elif tool == "ryn_apply_patch" and result.get("applied"):
            for item in result.get("files", []):
                if item.get("action") == "deleted":
                    _remove(files, receipt, item.get("path", ""))
                    continue
                if item.get("from"):
                    _remove(files, receipt, item["from"])
                _touch(files, receipt, item["path"], item.get("sha256"), item.get("action") in {"added", "moved"})
        elif tool == "ryn_file_manage":
            action = arguments.get("action")
            if action == "delete" and result.get("deleted"):
                _remove(files, receipt, result.get("path", ""))
            elif action in {"copy", "move"} and result.get("destination"):
                _touch(files, receipt, result["destination"], result.get("sha256"), True)
                if action == "move":
                    _remove(files, receipt, result.get("path", ""))
        elif str(tool or "").startswith("mcp__") and (
            isinstance(result.get("mcp_artifacts"), list)
            or isinstance(result.get("native_observation_artifacts"), list)
        ):
            native = isinstance(result.get("native_observation_artifacts"), list)
            binding = receipt.get("extension_binding") or {}
            if native and (binding.get("server_id") != "computer-use"
                           or binding.get("instance_id") != "builtin-computer-use"):
                continue
            root_value = (row.get("local_execution") or {}).get("workspace_root") or ""
            if root_value:
                ws_root = Path(root_value).resolve()
                safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(row.get("id") or "task").strip()).strip("._")[:64] or "task"
                safe_op = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(receipt.get("operation_id") or "op").strip()).strip("._")[:64] or "op"
                expected_dir = (ws_root / ".ryn" / ("computer" if native else "mcp") / safe_task / safe_op).resolve()
                for item in result.get("native_observation_artifacts" if native else "mcp_artifacts") or []:
                    if not isinstance(item, dict) or not item.get("path") or not item.get("sha256"):
                        continue
                    if native:
                        descriptor = item.get("native_descriptor") or {}
                        if (descriptor.get("task_id") != row.get("id")
                                or descriptor.get("operation_id") != receipt.get("operation_id")
                                or descriptor.get("source_sha256") != item["sha256"]):
                            continue
                    raw_p = str(item["path"])
                    if Path(raw_p).is_absolute():
                        continue
                    try:
                        resolved_p = (ws_root / raw_p).resolve()
                    except (OSError, ValueError):
                        continue
                    if not resolved_p.is_relative_to(expected_dir) or resolved_p == expected_dir:
                        continue
                    _touch(files, receipt, str(resolved_p), item["sha256"], True,
                           "computer_observation" if native else "write")
    return files


def snapshot(root) -> dict:
    """Bounded metadata inventory around a command; do not read unrelated file contents."""
    found, pending, visited, readable, directories = {}, [Path(root)], 0, True, set()
    deadline = time.monotonic() + 1.0
    skip = {".git", "node_modules", ".venv", "venv", "__pycache__", "target", ".next", ".ryn", ".codex_scratch"}
    while pending and visited < 5000 and time.monotonic() < deadline:
        directory = pending.pop()
        complete_directory = True
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > 5000 or time.monotonic() >= deadline:
                        complete_directory = False
                        break
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in skip:
                            pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        stat = entry.stat(follow_symlinks=False)
                        found[entry.path] = (stat.st_size, stat.st_mtime_ns)
            if complete_directory:
                directories.add(str(directory))
        except OSError:
            readable = False
            continue
    return {"files": found, "directories": directories, "complete": readable and not pending and visited < 5000 and time.monotonic() < deadline}


def command_files(root, before) -> list[dict]:
    after = snapshot(root)
    changes = []
    # An incomplete baseline cannot prove a newly seen file was created by this command.
    for value, stamp in after["files"].items():
        old = before["files"].get(value)
        if old == stamp or (old is None and not before["complete"] and str(Path(value).parent) not in before.get("directories", set())):
            continue
        path = _inside(Path(value), Path(root))
        if path is None:
            continue
        try:
            size, digest = _digest(path)
        except OSError:
            # A file held open by Excel or another writer is still a real output.
            size, digest = stamp[0], None
        changes.append({"path": str(path), "sha256": digest, "bytes": size, "created": old is None, "modified_ns": stamp[1],
                        "attribution": "observed_during_command", "creator_confirmed": False})
        if len(changes) >= 64:
            break
    if after["complete"]:
        changes.extend({"path": value, "deleted": True} for value in before["files"] if value not in after["files"])
    return changes[:64]


def _inside(path: Path, root: Path) -> Path | None:
    try:
        resolved = path.resolve(strict=True)
        return resolved if resolved.is_relative_to(root.resolve(strict=True)) and resolved.is_file() else None
    except (OSError, ValueError):
        return None


def _digest(path: Path):
    size = path.stat().st_size
    if size > MAX_ARTIFACT_BYTES:
        return size, None
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return size, hasher.hexdigest()


def final_text(row) -> str:
    """The latest answer the owner sees; internal review prompts are not answers."""
    if row.get("result"):
        return row["result"]
    for message in reversed(row.get("messages", [])):
        if message.get("role") == "assistant" and message.get("content") and not message.get("tool_calls") \
                and (message.get("ui") or {}).get("kind") != "change_review":
            return message["content"]
    return ""


def _turn_answers(row) -> dict[int, str]:
    """Map each 0-based user turn index to public assistant answer text produced in that turn."""
    by_turn: dict[int, list[str]] = {}
    turn_idx = -1
    for message in row.get("messages", []):
        if message.get("role") == "user":
            turn_idx += 1
        elif (message.get("role") == "assistant" and message.get("content")
              and not message.get("tool_calls")
              and (message.get("ui") or {}).get("kind") not in {"change_review", "verification_feedback"}):
            by_turn.setdefault(max(0, turn_idx), []).append(message["content"])
    latest_turn = max(0, turn_idx)
    if row.get("result"):
        existing = by_turn.setdefault(latest_turn, [])
        if row["result"] not in existing:
            existing.append(row["result"])
    return {idx: "\n".join(parts) for idx, parts in by_turn.items()}


def _operation_turns(row) -> dict[str, int]:
    """Map operation_id to the 0-based user turn index where the tool call was dispatched."""
    call_to_turn: dict[str, int] = {}
    turn_idx = -1
    for message in row.get("messages", []):
        if message.get("role") == "user":
            turn_idx += 1
        elif message.get("role") == "assistant" and message.get("tool_calls"):
            for call in message["tool_calls"]:
                if isinstance(call, dict) and call.get("id"):
                    call_to_turn[str(call["id"])] = max(0, turn_idx)
    op_to_turn: dict[str, int] = {}
    for event in row.get("events", []):
        if isinstance(event, dict) and event.get("operation_id") and event.get("call_id") in call_to_turn:
            op_to_turn[str(event["operation_id"])] = call_to_turn[str(event["call_id"])]
    return op_to_turn


def inspections(row) -> dict:
    """sha256 -> latest confirmed render inspection of exactly those bytes."""
    found = {}
    for receipt in row.get("receipts", []):
        result = receipt.get("result")
        if receipt.get("tool") == "ryn_render_inspect" and receipt.get("status") == "confirmed" and isinstance(result, dict) \
                and result.get("sha256") and not result.get("error"):
            found[result["sha256"]] = {"at": receipt.get("confirmed_at"), "findings": len(result.get("findings") or [])}
    return found


def listing(row) -> dict:
    root_value = (row.get("local_execution") or {}).get("workspace_root") or ""
    if not root_value:
        return {"files": [], "unbacked_mentions": []}
    root = Path(root_value)
    answer = final_text(row)
    lowered = answer.casefold()
    answers_by_turn = _turn_answers(row)
    op_turns = _operation_turns(row)
    checked = inspections(row)
    items = []
    for state in history(row).values():
        if state["deleted"]:
            continue
        path = Path(state["path"])
        try:
            relative = os.path.relpath(path, root).replace("\\", "/")
        except ValueError:
            continue
        if relative.startswith("../") or relative.startswith(".ryn/attachments/"):
            continue
        mentioned_turns = [
            t_idx for t_idx, text in sorted(answers_by_turn.items())
            if path.name.casefold() in text.casefold() or relative.casefold() in text.casefold()
        ]
        mentioned = bool(mentioned_turns) or path.name.casefold() in lowered or relative.casefold() in lowered
        valid_goals = [g for g in state["goal_revisions"] if isinstance(g, int) and g >= 1]
        turn_index = op_turns.get(state["operation_id"])
        if turn_index is None:
            turn_index = mentioned_turns[-1] if mentioned_turns else ((valid_goals[-1] - 1) if valid_goals else 0)
        item = {"id": state["id"], "name": path.name, "path": relative, "absolute_path": str(path), "source": state["source"], "operation_id": state["operation_id"], "media_type": media_type(path.name),
                "preview": preview_kind(path.name), "created_by_task": bool(state["created_by_task"]),
                "revisions": state["revisions"], "goal_revisions": state["goal_revisions"],
                "turn_index": turn_index,
                "created_at": state["first_at"], "updated_at": state["updated_at"], "receipt_sha256": state["sha256"],
                "mentioned_in_answer": mentioned}
        if state["source"] == "command":
            item["attribution"] = "observed_during_command"
            # Modified original project files are audit evidence, not automatic
            # deliverables. Expose one only when the answer actually references it.
            if row.get("project") and not state["created_by_task"] and not item["mentioned_in_answer"]:
                continue
        current = _inside(path, root)
        if current is None:
            item["status"] = "missing"
        else:
            try:
                size, digest = _digest(current)
            except OSError:
                item["status"] = "unavailable"
                items.append(item)
                continue
            item.update(bytes=size, sha256=digest)
            item["status"] = ("too_large" if digest is None else "unverified" if state["sha256"] is None
                              else "current" if digest == state["sha256"] else "changed_outside_task")
            if digest and digest in checked:
                item["render_inspected"] = checked[digest]
        items.append(item)
    present = [item for item in items if item["status"] != "missing"]
    named = [item for item in present if item["mentioned_in_answer"]]
    has_non_helper = any(
        Path(item["name"]).suffix.lower() not in HELPER_TYPES
        and item["source"] not in {"computer_observation", "read"}
        for item in present
    )
    for item in items:
        suffix = Path(item["name"]).suffix.lower()
        is_helper = suffix in HELPER_TYPES
        if item["source"] == "computer_observation":
            item["role"] = "supporting"
            continue
        if is_helper and (item["source"] == "command" or has_non_helper or item not in named):
            item["role"] = "supporting"
            continue
        # Evidence first: an answer can only rank files that receipts already prove; it cannot add one.
        document = (item["created_by_task"] or item["source"] == "command") and suffix in DOCUMENT_TYPES
        if item["source"] == "command":
            # Shell directory diffs observe side effects (such as scratch files), not explicit file-tool
            # deliverables. Following Codex's turn-artifact rules, command-observed files become
            # deliverables only when named in the assistant's answer or produced as binary documents.
            item["role"] = "deliverable" if (item in named if named else (document and item in present)) else "supporting"
        else:
            item["role"] = ("deliverable" if item in named else "supporting") if named else ("deliverable" if item in present else "supporting")
    # Deliverables first, then most recently updated; the sorts are stable.
    items.sort(key=lambda item: item["updated_at"] or "", reverse=True)
    items.sort(key=lambda item: (item["role"] != "deliverable", item["status"] == "missing"))
    known = {item["name"].casefold() for item in items}
    unbacked = []
    mention_text = answer
    # Mask full known names first, including spaces and parentheses in office filenames.
    for item in items:
        mention_text = re.sub(re.escape(item["name"]), lambda match: " " * len(match.group()), mention_text, flags=re.IGNORECASE)
    for match in MENTION.finditer(mention_text):
        name = Path(match.group(1).strip().replace("\\", "/")).name
        if name.casefold() not in known and name not in unbacked:
            unbacked.append(name)
    return {"files": items, "unbacked_mentions": unbacked[:8]}


def location(row, file_id) -> Path:
    """Current bytes of one receipt-backed workspace file, for the owner's preview."""
    root_value = (row.get("local_execution") or {}).get("workspace_root") or ""
    state = history(row).get(file_id) if root_value else None
    if state is None or state["deleted"]:
        raise AgentError("agent_artifact_not_found")
    path = _inside(Path(state["path"]), Path(root_value))
    if path is None:
        raise AgentError("agent_artifact_unavailable")
    return path


def read(row, file_id) -> dict:
    path = location(row, file_id)
    state = history(row)[file_id]
    if path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise AgentError("agent_artifact_too_large")
    raw = path.read_bytes()
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise AgentError("agent_artifact_too_large")
    digest = hashlib.sha256(raw).hexdigest()
    return {"id": file_id, "name": path.name, "media_type": media_type(path.name), "bytes": raw, "sha256": digest,
            "status": "unverified" if state["sha256"] is None else "current" if digest == state["sha256"] else "changed_outside_task"}


def open_file(row, file_id, action) -> dict:
    """An explicit owner click opens a bound file; model paths are never passed to a shell."""
    import subprocess
    import sys
    if not isinstance(action, str) or action not in {"open", "reveal"}:
        raise AgentError("agent_artifact_action_invalid")
    path = location(row, file_id)
    try:
        if os.name == "nt":
            if action == "open":
                os.startfile(str(path))
            else:
                subprocess.Popen(["explorer.exe", f'/select,{path}'], creationflags=0x08000000)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", *(["-R"] if action == "reveal" else []), str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent if action == "reveal" else path)])
    except OSError:
        raise AgentError("agent_artifact_open_failed") from None
    return {"id": file_id, "action": action, "opened": True}
