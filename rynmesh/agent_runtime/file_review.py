"""Durable file-tool evidence and owner-requested, compare-before-restore undo.

Stored beside the original task baselines, never derived from shell observations.
Blobs are immutable and bounded; intent and per-path progress precede disk writes.
An interrupted tool write is deliberately NOT promoted to confirmed ownership.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import stat
import uuid
from pathlib import Path

from ..atomic_io import atomic_write_bytes, atomic_write_json, read_json
from ..file_transactions import file_transaction
from .store import AgentError, AgentToolRejected

MAX_BYTES = 8 * 1024 * 1024
MAX_HOME_BYTES = 256 * 1024 * 1024
MAX_FILES = 64
MAX_OPERATIONS = 256
MAX_FILE = 2 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _expand_tilde_str(value):
    if isinstance(value, str) and (value == "~" or value.startswith(("~/", "~\\"))):
        return os.path.expanduser(value)
    return value


def _raw_check_segments(raw_value: str) -> list[str]:
    parts = [seg for seg in raw_value.replace("\\", "/").split("/") if seg and seg not in {".", ".."}]
    if os.name == "nt" and parts and len(parts[0]) == 2 and parts[0][1] == ":" and parts[0][0].isalpha():
        parts = parts[1:]
    return parts


def _forbidden_segment(parts) -> str | None:
    for p in parts:
        if p.casefold() == ".git" or ":" in p or p.endswith((".", " ")):
            return p
    return None


def _display_review_path(step: dict, root: str, *, posix: bool = False) -> str:
    raw_path = step["path"]
    if step.get("outside_workspace"):
        return raw_path.replace("\\", "/") if posix else raw_path
    try:
        rel = os.path.relpath(raw_path, root)
    except ValueError:
        return raw_path.replace("\\", "/") if posix else raw_path
    return rel.replace("\\", "/") if posix else rel


def safe_path(root, value, *, allow_directory=False, outside_ok=False):
    """Validate lexical paths BEFORE resolving links (including Windows reparse points)."""
    root = Path(os.path.abspath(root))
    expanded = _expand_tilde_str(value)
    requested = Path(expanded)
    is_abs_request = requested.is_absolute()
    raw_str = expanded if isinstance(expanded, str) else str(requested)
    if is_abs_request:
        try:
            rel_raw = Path(raw_str).relative_to(root)
            raw_parts = list(rel_raw.parts)
        except ValueError:
            raw_parts = _raw_check_segments(raw_str)
    else:
        raw_parts = _raw_check_segments(raw_str)
    path = Path(os.path.abspath(requested if is_abs_request else root / requested))
    inside_root = path.is_relative_to(root)
    if path == root or path.parent == path or (not inside_root and not (outside_ok and is_abs_request)):
        raise AgentToolRejected("agent_local_path_outside_workspace")
    check_parts = (
        path.relative_to(root).parts
        if inside_root
        else tuple(p for p in path.parts if p != path.anchor)
    )
    bad_seg = _forbidden_segment(raw_parts) or _forbidden_segment(check_parts)
    if bad_seg is not None:
        raise AgentToolRejected(
            "agent_review_path_forbidden",
            details={"segment": bad_seg, "path": str(path)},
        )
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise AgentToolRejected("agent_review_link_forbidden")
    resolved = path.resolve()
    resolved_root = root.resolve()
    if inside_root:
        if not resolved.is_relative_to(resolved_root):
            raise AgentToolRejected(
                "agent_review_path_forbidden",
                details={"segment": resolved.name or str(resolved), "path": str(path)},
            )
        resolved_parts = resolved.relative_to(resolved_root).parts
    else:
        if resolved == resolved_root or resolved.parent == resolved:
            raise AgentToolRejected(
                "agent_review_path_forbidden",
                details={"segment": resolved.name or str(resolved), "path": str(path)},
            )
        resolved_parts = tuple(p for p in resolved.parts if p != resolved.anchor)
    bad_resolved = next((p for p in resolved_parts if p.casefold() == ".git"), None)
    if bad_resolved is not None:
        raise AgentToolRejected(
            "agent_review_path_forbidden",
            details={"segment": bad_resolved, "path": str(path)},
        )
    if path.exists() and not (allow_directory and path.is_dir()) and (not path.is_file() or path.stat().st_nlink > 1):
        raise AgentToolRejected("agent_review_regular_file_required")
    return path


def read_file(path):
    if not path.exists():
        return None
    if path.stat().st_size > MAX_FILE:
        raise AgentToolRejected("agent_review_file_limit")
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise AgentToolRejected("agent_review_file_limit")
    return data


def fingerprint(path):
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


class FileReview:
    def __init__(self, local, task_id, root):
        self.local, self.task_id, self.root = local, task_id, str(root)
        self.directory = local._baseline_dir(task_id)
        if self.directory is None:
            raise AgentToolRejected("agent_review_storage_required")
        self.manifest = self.directory / "review-v2.json"
        # Shared by ALL file tools/tasks and undo, including other node processes.
        self.lock = self.directory.parent / ".review.lock"
        safe_path(self.directory.parent, str(self.lock))

    def load(self):
        try:
            safe_path(self.directory.parent, str(self.manifest))
            state = read_json(self.manifest) if self.manifest.exists() else {"version": 2, "root": self.root, "records": [], "undos": []}
            if state["version"] != 2 or os.path.normcase(state["root"]) != os.path.normcase(self.root):
                raise ValueError()
            return state
        except (OSError, ValueError, KeyError):
            raise AgentError("agent_review_storage_unreadable") from None

    def save(self, state):
        safe_path(self.directory.parent, str(self.manifest))
        atomic_write_json(self.manifest, state)

    def reserve_progress(self, state):
        # Include manifest/history growth, not just blob bytes. Reserve room for
        # completion markers even when the task is close to its storage ceiling.
        old_size = self.manifest.stat().st_size if self.manifest.exists() else 0
        growth = len(json.dumps(state).encode()) - old_size + 16384
        used = sum(p.stat().st_size for p in self.directory.glob("*") if p.is_file())
        total = sum(p.stat().st_size for p in self.directory.parent.glob("*/*") if p.is_file())
        if used + growth > MAX_BYTES or total + growth > MAX_HOME_BYTES:
            raise AgentToolRejected("agent_review_storage_limit")

    def blob(self, data):
        if data is None:
            return {"sha256": None, "bytes": 0}
        key = digest(data)
        path = self.directory / (key + ".blob")
        safe_path(self.directory, str(path))
        if not path.exists():
            atomic_write_bytes(path, data)
        return {"sha256": key, "bytes": len(data)}

    def content(self, version):
        if version["sha256"] is None:
            return None
        key = version["sha256"]
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise AgentError("agent_review_snapshot_missing")
        try:
            raw = read_file(safe_path(self.directory, str(self.directory / (key + ".blob"))))
            if raw is None or digest(raw) != key:
                raise ValueError()
            return raw
        except (OSError, ValueError):
            raise AgentError("agent_review_snapshot_missing") from None

    def apply(self, changes, *, operation_id=None, outside_ok=False):
        """changes: (path, exact before bytes, intended after bytes, rename group)."""
        with file_transaction(self.lock):
            state = self.load()
            if len(state["records"]) >= MAX_OPERATIONS:
                raise AgentToolRejected("agent_review_operation_limit")
            paths = {s["path"] for r in state["records"] for s in r["steps"]}
            paths.update(str(p) for p, _, _, _ in changes)
            if len(paths) > MAX_FILES:
                raise AgentToolRejected("agent_review_file_count_limit")
            blobs = {digest(raw): raw for _, before, after, _ in changes for raw in (before, after) if raw is not None}
            if any(len(raw) > MAX_FILE for raw in blobs.values()):
                raise AgentToolRejected("agent_review_file_limit")
            growth = sum(len(raw) for key, raw in blobs.items() if not (self.directory / (key + ".blob")).exists())
            used = sum(p.stat().st_size for p in self.directory.glob("*") if p.is_file())
            total = sum(p.stat().st_size for p in self.directory.parent.glob("*/*") if p.is_file())
            if used + growth + 32768 > MAX_BYTES or total + growth + 32768 > MAX_HOME_BYTES:
                raise AgentToolRejected("agent_review_storage_limit")
            root_abs = Path(os.path.abspath(self.root))
            checked_changes = []
            for path, before, after, group in changes:
                sp = safe_path(self.root, str(path), outside_ok=outside_ok)
                if read_file(sp) != before:
                    raise AgentToolRejected("agent_local_file_changed_read_again")
                checked_changes.append((sp, before, after, group, not sp.is_relative_to(root_abs)))
            record = {"id": operation_id or uuid.uuid4().hex, "steps": []}
            for index, (sp, before, after, group, is_outside) in enumerate(checked_changes):
                step_entry = {"path": str(sp), "before": self.blob(before), "after": self.blob(after),
                    "mode": stat.S_IMODE(sp.stat().st_mode) if sp.exists() else 0o644,
                    "group": group, "state": "pending", "before_stamp": fingerprint(sp), "order": [len(state["records"]), index]}
                if is_outside:
                    step_entry["outside_workspace"] = True
                record["steps"].append(step_entry)
            state["records"].append(record)
            self.reserve_progress(state)
            self.save(state)  # All snapshots durable BEFORE the first workspace mutation.
            try:
                for step in record["steps"]:
                    step["state"] = "writing"
                    self.save(state)
                    self.replace(step, step["before"], step["after"], outside_ok=outside_ok or bool(step.get("outside_workspace")))
                    step["after_stamp"] = fingerprint(Path(step["path"]))
                    step["state"] = "applied"
                    self.save(state)
            except Exception as exc:
                if isinstance(exc, AgentToolRejected):
                    exc.after_side_effect = True
                raise

    def replace(self, step, expected, target, *, outside_ok=False):
        eff_outside = bool(outside_ok or step.get("outside_workspace"))
        path = safe_path(self.root, step["path"], outside_ok=eff_outside)
        raw = self.content(target)
        # Recheck immediately at the mutation boundary, in addition to whole-batch preflight.
        from .file_review_io import replace_checked
        stamp = step.get("after_stamp") if expected is step["after"] else step.get("before_stamp")
        replace_checked(path, self.root, expected["sha256"], raw, step["mode"], stamp, outside_ok=eff_outside)

    def recover(self, state, *, outside_ok=False):
        """Never resume writes on read/startup. Reconcile only an interrupted undo step."""
        changed = False
        for undo in state["undos"]:
            if undo["status"] != "running" and not (undo["status"] == "interrupted" and any(i["state"] == "writing" for i in undo["items"])):
                continue
            for item in undo["items"]:
                if item["state"] == "writing":
                    try:
                        eff_outside = bool(outside_ok or item.get("outside_workspace"))
                        restored = digest(read_file(safe_path(self.root, item["path"], outside_ok=eff_outside))) == item["before"]["sha256"]
                    except (OSError, AgentError):
                        restored = False
                    item["state"] = "recovered" if restored else "uncertain"
                    if restored:
                        self.mark_undone(state, item["path"], undo["record_ids"])
            undo["status"] = "completed" if all(i["state"] == "done" for i in undo["items"]) else "interrupted"
            changed = True
        if changed:
            self.save(state)

    @staticmethod
    def mark_undone(state, path, record_ids):
        for record in state["records"]:
            if record["id"] in record_ids:
                for step in record["steps"]:
                    if os.path.normcase(step["path"]) == os.path.normcase(path):
                        step["undone"] = True

    def groups(self, state):
        """Connected paths form one reversible unit: both ends of a rename stay together."""
        groups = []
        for record in state["records"]:
            for step in record["steps"]:
                if step.get("undone"):
                    continue
                touching = [g for g in groups if os.path.normcase(step["path"]) in g["paths"] or (step.get("group") and step["group"] in g["links"])]
                group = {"paths": set(), "links": set(), "steps": []}
                for previous in touching:
                    groups.remove(previous)
                    group["paths"].update(previous["paths"])
                    group["links"].update(previous["links"])
                    group["steps"].extend(previous["steps"])
                group["paths"].add(os.path.normcase(step["path"]))
                if step.get("group"):
                    group["links"].add(step["group"])
                group["steps"].append(step)
                groups.append(group)
        for group in groups:
            group["id"] = digest(json.dumps(sorted(group["paths"])).encode())[:24]
        return groups

    def net(self, group):
        paths, problem = {}, None
        for step in sorted(group["steps"], key=lambda s: s.get("order", [0, 0])):
            if step["state"] != "applied":
                problem = "interrupted_file_operation"
            key = os.path.normcase(step["path"])
            previous = paths.get(key)
            if previous and (previous["after"] != step["before"] or previous.get("after_stamp") != step.get("before_stamp")):
                problem = "intervening_changes"
            paths[key] = {**step, "before": previous["before"] if previous else step["before"],
                                   "mode": previous["mode"] if previous else step["mode"]}
        return list(paths.values()), problem

    def plan(self, state, ids=None):
        groups = self.groups(state)
        if ids is not None and (not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids) or set(ids) - {g["id"] for g in groups}):
            raise AgentError("agent_review_selection_changed")
        items, conflicts = [], []
        for group in groups:
            if ids is not None and group["id"] not in ids:
                continue
            net, problem = self.net(group)
            for item in net:
                try:
                    self.content(item["before"])
                    self.content(item["after"])
                    current = digest(read_file(safe_path(self.root, item["path"], outside_ok=bool(item.get("outside_workspace")))))
                    reason = problem or ("file_changed" if current != item["after"]["sha256"] or fingerprint(Path(item["path"])) != item.get("after_stamp") else None)
                except (OSError, AgentError) as exc:
                    reason = str(exc) if isinstance(exc, AgentError) else "file_unavailable"
                if reason:
                    conflicts.append({"path": _display_review_path(item, self.root), "reason": reason})
                items.append(item)
        token = digest(json.dumps({"items": items, "ids": ids}, sort_keys=True).encode())
        return {"token": token, "items": items, "conflicts": conflicts, "can_undo": bool(items) and not conflicts}

    def preview(self, ids=None):
        with file_transaction(self.lock):
            state = self.load()
            self.recover(state)
            plan = self.plan(state, ids)
            return {**plan, "items": [{"path": _display_review_path(i, self.root), "before": i["before"], "after": i["after"]} for i in plan["items"]]}

    def undo(self, ids, token, request_id):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 80 or not isinstance(token, str):
            raise AgentError("agent_invalid_review_request")
        with file_transaction(self.lock):
            state = self.load()
            self.recover(state)
            prior = next((u for u in state["undos"] if u["id"] == request_id), None)
            if prior:
                if prior["token"] != token:
                    raise AgentError("agent_review_request_id_conflict")
                return self.undo_result(prior)
            plan = self.plan(state, ids)
            if plan["conflicts"]:
                return {"status": "conflict", "conflicts": plan["conflicts"], "completed": []}
            if token != plan["token"] or not plan["can_undo"]:
                raise AgentError("agent_review_preview_changed")
            if len(state["undos"]) >= MAX_OPERATIONS:
                raise AgentError("agent_review_operation_limit")
            undo = {"id": request_id, "token": token, "status": "running", "record_ids": [r["id"] for r in state["records"]],
                    "items": [{**item, "state": "pending"} for item in plan["items"]]}
            state["undos"].append(undo)
            self.reserve_progress(state)
            self.save(state)
            try:
                for item in undo["items"]:
                    # No application after an uncertain/failed step, and no automatic retry.
                    item["state"] = "writing"
                    self.save(state)
                    self.replace(item, item["after"], item["before"])
                    item["state"] = "done"
                    self.mark_undone(state, item["path"], undo["record_ids"])
                    self.save(state)
                undo["status"] = "completed"
                self.save(state)
            except Exception:
                # Disk may be full even when saving the failure. The prior durable
                # 'writing' marker still prevents a misleading success on restart.
                undo["status"] = "interrupted"
                try:
                    self.save(state)
                except OSError:
                    pass
            return self.undo_result(undo)

    def undo_result(self, undo):
        return {"id": undo["id"], "status": undo["status"],
                "completed": [_display_review_path(i, self.root) for i in undo["items"] if i["state"] in {"done", "recovered"}],
                "remaining": [{"path": _display_review_path(i, self.root), "state": i["state"]} for i in undo["items"] if i["state"] not in {"done", "recovered"}]}

    def review(self):
        from .local_tools import (
            CHECK_LINE,
            REVIEW_DIFF_CHARS,
            SEARCH_BINARY_SUFFIXES,
            TEST_PATH,
            change_summary,
            removed_lines,
        )
        with file_transaction(self.lock):
            state = self.load()
            self.recover(state)
            files = []
            for group in self.groups(state):
                net, problem = self.net(group)
                for step in net:
                    if step["before"] == step["after"] and not problem:
                        continue
                    relative = _display_review_path(step, self.root, posix=True)
                    before, after = step["before"], step["after"]
                    entry = {"id": group["id"], "path": relative, "status": "created" if before["sha256"] is None else "deleted" if after["sha256"] is None else "modified",
                             "existed_before": before["sha256"] is not None, "sha256": after["sha256"], "test_file": bool(TEST_PATH.search(relative)),
                             "before_bytes": before["bytes"], "after_bytes": after["bytes"], "source": "file_tool", "can_undo": not problem,
                             "rename_group": len(group["paths"]) > 1, "problem": problem, "media_type": mimetypes.guess_type(relative)[0] or "application/octet-stream",
                             **({"outside_workspace": True} if step.get("outside_workspace") else {})}
                    try:
                        original, current = self.content(before), self.content(after)
                        if before["sha256"]:
                            entry["original_copy"] = str(self.directory / (before["sha256"] + ".blob"))
                        if Path(relative).suffix.lower() in SEARCH_BINARY_SUFFIXES or b"\0" in (original or b"") + (current or b""):
                            raise UnicodeError()
                        a, b = (original or b"").decode("utf-8-sig"), (current or b"").decode("utf-8-sig")
                        summary = change_summary(a, b)
                        entry.update(summary)
                        entry["diff"] = summary.get("diff", "")[:REVIEW_DIFF_CHARS]
                        entry["diff_truncated"] = not summary or "(diff truncated)" in summary.get("diff", "") or len(summary.get("diff", "")) >= REVIEW_DIFF_CHARS
                        dropped = [line for line in removed_lines(a, b) if line and CHECK_LINE.search(line)]
                        if dropped:
                            entry["removed_check_like_lines"] = dropped[:10]
                    except UnicodeError:
                        entry["binary"] = True
                    except (OSError, AgentError):
                        entry.update(can_undo=False, problem="snapshot_missing")
                    files.append(entry)
            return {"files": files, "files_total": len(files), "digest": digest(json.dumps(files, sort_keys=True).encode()) if files else None,
                    "undo_operations": [self.undo_result(u) for u in state["undos"][-10:]],
                    "limits": {"task_bytes": MAX_BYTES, "home_bytes": MAX_HOME_BYTES, "files": MAX_FILES, "operations": MAX_OPERATIONS},
                    "not_covered": "Shell/build observations are not proof of Agent ownership and cannot be undone. External services and conversation history are not reverted."}
