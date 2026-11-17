"""Durable file-tool evidence and owner-requested, compare-before-restore undo.

Stored beside the original task baselines, never derived from shell observations.
Blobs are immutable and bounded; intent and per-path progress precede disk writes.
Interrupted writes are reconciled read-only against their durable intent hashes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import mimetypes
import os
import stat
import time
import uuid
from contextlib import contextmanager
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


IO_REPARSE_TAG_SYMLINK = getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C)
IO_REPARSE_TAG_MOUNT_POINT = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)
IO_REPARSE_TAG_APPEXECLINK = getattr(stat, "IO_REPARSE_TAG_APPEXECLINK", 0x8000001B)
IO_REPARSE_TAG_CLOUD = 0x9000001A
IO_REPARSE_TAG_CLOUD_MASK = 0x0000F000


def _is_cloud_reparse_tag(tag: int) -> bool:
    return isinstance(tag, int) and (tag & ~IO_REPARSE_TAG_CLOUD_MASK) == IO_REPARSE_TAG_CLOUD


def _is_forbidden_link_or_reparse(info) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    if getattr(info, "st_file_attributes", 0) & 0x400:
        tag = getattr(info, "st_reparse_tag", 0)
        if tag in (IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_APPEXECLINK):
            return True
        if _is_cloud_reparse_tag(tag):
            return False
        return True
    return False


def _resolve_root(root) -> tuple[Path, Path]:
    lex_root = Path(os.path.abspath(root))
    try:
        real_root = Path(root).resolve(strict=True)
    except FileNotFoundError:
        if lex_root.is_symlink():
            raise AgentToolRejected("agent_review_link_forbidden") from None
        real_root = lex_root.resolve()
    return lex_root, real_root


def _display_review_path(step: dict, root: str, *, posix: bool = False) -> str:
    raw_path = step["path"]
    if step.get("outside_workspace"):
        return raw_path.replace("\\", "/") if posix else raw_path
    p = Path(os.path.abspath(raw_path))
    lex_root = Path(os.path.abspath(root))
    base = lex_root
    if not p.is_relative_to(lex_root):
        try:
            real_root = Path(root).resolve()
            if p.is_relative_to(real_root):
                base = real_root
        except OSError:
            pass
    try:
        rel = os.path.relpath(p, base)
    except ValueError:
        return raw_path.replace("\\", "/") if posix else raw_path
    return rel.replace("\\", "/") if posix else rel


def safe_path(root, value, *, allow_directory=False, outside_ok=False):
    """Validate lexical paths BEFORE resolving links (below workspace root)."""
    lex_root, real_root = _resolve_root(root)
    expanded = _expand_tilde_str(value)
    requested = Path(expanded)
    is_abs_request = requested.is_absolute()
    raw_str = expanded if isinstance(expanded, str) else str(requested)
    if is_abs_request:
        req_abs = Path(os.path.abspath(raw_str))
        if req_abs.is_relative_to(lex_root):
            raw_parts = list(req_abs.relative_to(lex_root).parts)
        elif req_abs.is_relative_to(real_root):
            raw_parts = list(req_abs.relative_to(real_root).parts)
        else:
            raw_parts = _raw_check_segments(raw_str)
    else:
        raw_parts = _raw_check_segments(raw_str)
    path = Path(os.path.abspath(requested if is_abs_request else lex_root / requested))
    in_lex = path.is_relative_to(lex_root)
    in_real = path.is_relative_to(real_root)
    inside_root = in_lex or in_real
    if (
        path == lex_root
        or path == real_root
        or path.parent == path
        or (not inside_root and not (outside_ok and is_abs_request))
    ):
        raise AgentToolRejected("agent_local_path_outside_workspace")
    base_root = lex_root if in_lex else real_root
    check_parts = (
        path.relative_to(base_root).parts
        if inside_root
        else tuple(p for p in path.parts if p != path.anchor)
    )
    bad_seg = _forbidden_segment(raw_parts) or _forbidden_segment(check_parts)
    if bad_seg is not None:
        raise AgentToolRejected(
            "agent_review_path_forbidden",
            details={"segment": bad_seg, "path": str(path)},
        )
    if inside_root:
        cur = base_root
        lstat_targets = []
        for seg in check_parts:
            cur = cur / seg
            lstat_targets.append(cur)
    else:
        try:
            common = Path(os.path.commonpath([str(lex_root), str(path)]))
            cur = common
            lstat_targets = []
            for seg in path.relative_to(common).parts:
                cur = cur / seg
                lstat_targets.append(cur)
        except ValueError:
            lstat_targets = [p for p in [*reversed(path.parents), path] if p.parent != p]
    for part in lstat_targets:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if _is_forbidden_link_or_reparse(info):
            raise AgentToolRejected("agent_review_link_forbidden")
    resolved = path.resolve()
    if inside_root:
        if resolved == real_root or not resolved.is_relative_to(real_root):
            raise AgentToolRejected(
                "agent_review_path_forbidden",
                details={"segment": resolved.name or str(resolved), "path": str(path)},
            )
        resolved_parts = resolved.relative_to(real_root).parts
    else:
        if resolved == real_root or resolved.parent == resolved:
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


_UNSET = object()


def _meta(data):
    return {"sha256": None, "bytes": 0} if data is None else {"sha256": digest(data), "bytes": len(data)}


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


def _read_existing(path, *, max_bytes=MAX_FILE):
    if not path.exists():
        return None
    limit = max(MAX_FILE, max_bytes)
    if path.stat().st_size > limit:
        return _UNSET
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        return _UNSET
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
        self.usage_path = self.directory.parent / "usage-v1.json"
        safe_path(self.directory.parent, str(self.lock))

    def _read_usage(self):
        safe_path(self.directory.parent, str(self.usage_path))
        now = time.time()
        try:
            usage = read_json(self.usage_path)
            if (type(usage["total_bytes"]) is not int or usage["total_bytes"] < 0
                    or not isinstance(usage["tasks"], dict)
                    or any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in usage["tasks"].items())
                    or sum(usage["tasks"].values()) != usage["total_bytes"]
                    or type(usage["verified_at"]) not in (int, float) or not math.isfinite(usage["verified_at"])
                    or now - usage["verified_at"] > 600 or usage["total_bytes"] > MAX_HOME_BYTES * .9):
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            tasks = {}
            for path in self.directory.parent.glob("*/*"):
                if path.is_file():
                    tasks[path.parent.name] = tasks.get(path.parent.name, 0) + path.stat().st_size
            usage = {"total_bytes": sum(tasks.values()), "tasks": tasks, "verified_at": now}
            atomic_write_json(self.usage_path, usage)
        return usage

    @contextmanager
    def _usage_transaction(self):
        # The caller holds the shared file lock. Nested blob/manifest writes
        # reuse one snapshot, including a near-limit recount, for this operation.
        if hasattr(self, "_usage"):
            yield
            return
        self._usage = self._read_usage()
        self._usage_changed = False
        try:
            yield
        finally:
            usage, changed = self._usage, self._usage_changed
            del self._usage
            del self._usage_changed
            if changed:
                safe_path(self.directory.parent, str(self.usage_path))
                atomic_write_json(self.usage_path, usage)

    def _total(self):
        """Read under file_transaction(self.lock); no cross-operation memory cache."""
        return (self._usage if hasattr(self, "_usage") else self._read_usage())["total_bytes"]

    def _adjust_usage(self, delta):
        usage = self._usage
        task = self.directory.name
        size = usage["tasks"].get(task, 0) + delta
        if size:
            usage["tasks"][task] = size
        else:
            usage["tasks"].pop(task, None)
        usage["total_bytes"] += delta
        self._usage_changed = True

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
        with file_transaction(self.lock), self._usage_transaction():
            safe_path(self.directory.parent, str(self.manifest))
            old_size = self.manifest.stat().st_size if self.manifest.exists() else 0
            atomic_write_json(self.manifest, state)
            self._adjust_usage(self.manifest.stat().st_size - old_size)

    def reserve_progress(self, state):
        # Include manifest/history growth, not just blob bytes. Reserve room for
        # completion markers even when the task is close to its storage ceiling.
        with file_transaction(self.lock), self._usage_transaction():
            old_size = self.manifest.stat().st_size if self.manifest.exists() else 0
            growth = len(json.dumps(state).encode()) - old_size + 16384
            used = sum(p.stat().st_size for p in self.directory.glob("*") if p.is_file())
            if used + growth > MAX_BYTES or self._total() + growth > MAX_HOME_BYTES:
                raise AgentToolRejected("agent_review_storage_limit")

    def blob(self, data):
        if data is None:
            return {"sha256": None, "bytes": 0}
        with file_transaction(self.lock), self._usage_transaction():
            key = digest(data)
            path = self.directory / (key + ".blob")
            safe_path(self.directory, str(path))
            if not path.exists():
                atomic_write_bytes(path, data)
                self._adjust_usage(len(data))
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
        with file_transaction(self.lock), self._usage_transaction():
            state = self.load()
            original_state = copy.deepcopy(state)
            lex_root, real_root = _resolve_root(self.root)
            checked_changes = []
            for path, before, after, group in changes:
                sp = safe_path(self.root, str(path), outside_ok=outside_ok)
                actual = _read_existing(sp, max_bytes=len(before) if before is not None else 0)
                if actual is _UNSET or actual != before:
                    raise AgentToolRejected("agent_local_file_changed_read_again")
                is_outside = not (sp.is_relative_to(lex_root) or sp.is_relative_to(real_root))
                checked_changes.append((sp, before, after, group, is_outside))

            normal_records = [r for r in state["records"] if not r.get("overflow_summary")]
            over_op_limit = len(normal_records) >= MAX_OPERATIONS
            snapshotted_paths = {
                os.path.normcase(s["path"])
                for r in state["records"]
                for s in r.get("steps", [])
                if s.get("snapshot", True) is not False
            }
            unsnapshotted_paths = {
                os.path.normcase(s["path"])
                for r in state["records"]
                for s in r.get("steps", [])
                if s.get("snapshot", True) is False
            }
            used = sum(p.stat().st_size for p in self.directory.glob("*") if p.is_file()) if self.directory.exists() else 0
            total = self._total()
            existing_blobs = {p.stem for p in self.directory.glob("*.blob") if p.is_file()} if self.directory.exists() else set()

            order_major = state.get("next_order", len(state["records"]))
            state["next_order"] = order_major + 1
            mutation_plan = []

            if over_op_limit:
                summary_rec = next((r for r in state["records"] if r.get("overflow_summary")), None)
                if summary_rec is None:
                    summary_rec = {"id": "overflow_summary", "overflow_summary": True, "overflow_operations": 0, "steps": []}
                    state["records"].append(summary_rec)
                summary_rec["overflow_operations"] = summary_rec.get("overflow_operations", 0) + 1
                summary_rec["last_operation_id"] = operation_id or uuid.uuid4().hex
                by_norm = {os.path.normcase(s["path"]): s for s in summary_rec["steps"]}
                for index, (sp, before, after, group, is_outside) in enumerate(checked_changes):
                    expected_meta = _meta(before)
                    expected_stamp = fingerprint(sp)
                    mode = stat.S_IMODE(sp.stat().st_mode) if sp.exists() else 0o644
                    norm_sp = os.path.normcase(str(sp))
                    existing_step = by_norm.get(norm_sp)
                    if existing_step is not None:
                        existing_step["after"] = _meta(after)
                        existing_step["snapshot"] = False
                        existing_step["state"] = "pending"
                        existing_step["order"] = [order_major, index]
                        if group:
                            existing_step["group"] = group
                        if is_outside:
                            existing_step["outside_workspace"] = True
                        existing_step.pop("undone", None)
                        mutation_plan.append((existing_step, expected_meta, expected_stamp, after))
                    elif len(summary_rec["steps"]) < 512:
                        step_entry = {
                            "path": str(sp),
                            "before": expected_meta,
                            "after": _meta(after),
                            "snapshot": False,
                            "mode": mode,
                            "group": group,
                            "state": "pending",
                            "before_stamp": expected_stamp,
                            "order": [order_major, index],
                        }
                        if is_outside:
                            step_entry["outside_workspace"] = True
                        summary_rec["steps"].append(step_entry)
                        by_norm[norm_sp] = step_entry
                        mutation_plan.append((step_entry, expected_meta, expected_stamp, after))
                    else:
                        summary_rec["extra_skipped"] = summary_rec.get("extra_skipped", 0) + 1
                        transient = {
                            "path": str(sp),
                            "before": expected_meta,
                            "after": _meta(after),
                            "snapshot": False,
                            "mode": mode,
                            "group": group,
                            "state": "pending",
                            "before_stamp": expected_stamp,
                            **({"outside_workspace": True} if is_outside else {}),
                        }
                        mutation_plan.append((transient, expected_meta, expected_stamp, after))
            else:
                # Evaluate rename groups atomically so paired move endpoints share the same snapshot decision.
                units = []
                group_buckets = {}
                for index, item in enumerate(checked_changes):
                    grp = item[3]
                    if grp is not None:
                        if grp not in group_buckets:
                            bucket = []
                            group_buckets[grp] = bucket
                            units.append(bucket)
                        group_buckets[grp].append((index, *item))
                    else:
                        units.append([(index, *item)])

                step_decisions = {}
                for unit in units:
                    unit_norms = {os.path.normcase(str(sp)) for _, sp, *_ in unit}
                    new_paths = unit_norms - snapshotted_paths
                    oversize_file = any(
                        (before is not None and len(before) > MAX_FILE) or (after is not None and len(after) > MAX_FILE)
                        for _, _, before, after, _, _ in unit
                    )
                    can_snap = not (unit_norms & unsnapshotted_paths) and len(snapshotted_paths) + len(new_paths) <= MAX_FILES and not oversize_file
                    unit_blobs = {}
                    unit_growth = 0
                    if can_snap:
                        unit_blobs = {
                            digest(raw): raw
                            for _, _, before, after, _, _ in unit
                            for raw in (before, after)
                            if raw is not None
                        }
                        unit_growth = sum(len(raw) for key, raw in unit_blobs.items() if key not in existing_blobs)
                        if used + unit_growth + 32768 > MAX_BYTES or total + unit_growth + 32768 > MAX_HOME_BYTES:
                            can_snap = False
                    if can_snap:
                        try:
                            for raw in unit_blobs.values():
                                self.blob(raw)
                        except OSError:
                            can_snap = False
                        else:
                            snapshotted_paths.update(new_paths)
                            existing_blobs.update(unit_blobs)
                            used += unit_growth
                            total += unit_growth
                    if not can_snap:
                        unsnapshotted_paths.update(unit_norms)
                    for idx, *_ in unit:
                        step_decisions[idx] = can_snap

                record = {"id": operation_id or uuid.uuid4().hex, "steps": []}
                for index, (sp, before, after, group, is_outside) in enumerate(checked_changes):
                    snapped = step_decisions[index]
                    before_meta = self.blob(before) if snapped else _meta(before)
                    after_meta = self.blob(after) if snapped else _meta(after)
                    before_stamp = fingerprint(sp)
                    step_entry = {
                        "path": str(sp),
                        "before": before_meta,
                        "after": after_meta,
                        **({} if snapped else {"snapshot": False}),
                        "mode": stat.S_IMODE(sp.stat().st_mode) if sp.exists() else 0o644,
                        "group": group,
                        "state": "pending",
                        "before_stamp": before_stamp,
                        "order": [order_major, index],
                    }
                    if is_outside:
                        step_entry["outside_workspace"] = True
                    record["steps"].append(step_entry)
                    mutation_plan.append((step_entry, before_meta, before_stamp, after))
                state["records"].append(record)

            try:
                self.save(state)  # Manifest durable BEFORE the first workspace mutation.
            except OSError:
                raise AgentToolRejected("agent_review_storage_limit") from None
            wrote = False
            try:
                for step, expected_meta, expected_stamp, after_raw in mutation_plan:
                    step["state"] = "writing"
                    self.save(state)
                    self.replace(
                        step,
                        expected_meta,
                        step["after"],
                        outside_ok=outside_ok or bool(step.get("outside_workspace")),
                        raw_bytes=after_raw if step.get("snapshot", True) is False else _UNSET,
                        expected_stamp=expected_stamp,
                    )
                    wrote = True
                    step["after_stamp"] = fingerprint(Path(step["path"]))
                    step["state"] = "applied"
                    self.save(state)
            except Exception as exc:
                if getattr(exc, "nothing_written", False) and not wrote:
                    # Also preserve older history in the merged, hash-only summary.
                    self.save(original_state)
                elif isinstance(exc, AgentToolRejected):
                    exc.after_side_effect = True
                raise

    def replace(self, step, expected, target, *, outside_ok=False, raw_bytes=_UNSET, expected_stamp=_UNSET):
        eff_outside = bool(outside_ok or step.get("outside_workspace"))
        path = safe_path(self.root, step["path"], outside_ok=eff_outside)
        raw = self.content(target) if raw_bytes is _UNSET else raw_bytes
        # Recheck immediately at the mutation boundary, in addition to whole-batch preflight.
        from .file_review_io import replace_checked
        stamp = (
            (step.get("after_stamp") if expected is step["after"] else step.get("before_stamp"))
            if expected_stamp is _UNSET
            else expected_stamp
        )
        replace_checked(path, self.root, expected["sha256"], raw, step["mode"], stamp, outside_ok=eff_outside)

    def recover(self, state, *, outside_ok=False):
        """Reconcile durable intent without resuming writes to workspace files."""
        changed = False
        for record in state["records"]:
            preceding_write = False
            for step in record["steps"]:
                if step["state"] in {"writing", "pending"}:
                    try:
                        path = safe_path(self.root, step["path"], outside_ok=outside_ok or bool(step.get("outside_workspace")))
                        raw = read_file(path)
                        current = digest(raw)
                        if step["state"] == "pending":
                            if not preceding_write or current == step["before"]["sha256"]:
                                step["state"] = "not_applied"
                                changed = True
                        elif current == step["after"]["sha256"]:
                            step.update(state="applied", after_stamp=fingerprint(path))
                            changed = True
                        elif current == step["before"]["sha256"]:
                            step["state"] = "not_applied"
                            changed = True
                        else:
                            # Hash-only operations remain non-restorable; never fabricate a before snapshot.
                            step.update(state="partial_write", after=self.blob(raw) if step.get("snapshot", True) else _meta(raw), after_stamp=fingerprint(path))
                            changed = True
                    except (OSError, AgentError):
                        pass  # Unreadable/oversize files retain unconfirmed intent.
                preceding_write |= step["state"] in {"writing", "applied", "partial_write"}
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

    def record_outcome(self, operation_id):
        with file_transaction(self.lock):
            state = self.load()
            self.recover(state)
            record = next((r for r in state["records"] if r["id"] == operation_id and not r.get("overflow_summary")), None)
            files = [{"path": _display_review_path(s, self.root, posix=True), "state": s["state"]} for s in record["steps"]] if record else []
            # Merged overflow records no longer prove which operation owns each path.
            absent = record is None and not any(r.get("overflow_summary") for r in state["records"])
            return {"all_applied": bool(files) and all(f["state"] == "applied" for f in files),
                    "none_applied": absent or bool(files) and all(f["state"] == "not_applied" for f in files), "files": files}

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
                if step.get("undone") or step["state"] in {"pending", "not_applied"}:
                    continue
                is_skipped = step.get("snapshot", True) is False
                step_view = (
                    {**step, "can_undo": False, "reason": "snapshot_skipped_limit"}
                    if is_skipped
                    else {**step, "can_undo": step.get("state") in {"applied", "partial_write"}}
                )
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
                group["steps"].append(step_view)
                groups.append(group)
        for group in groups:
            group["id"] = digest(json.dumps(sorted(group["paths"])).encode())[:24]
            group["can_undo"] = all(s.get("snapshot", True) is not False for s in group["steps"])
        return groups

    def net(self, group):
        paths, problem = {}, None
        group_skipped = any(s.get("snapshot", True) is False for s in group["steps"])
        for step in sorted(group["steps"], key=lambda s: s.get("order", [0, 0])):
            if step["state"] == "partial_write" and problem is None:
                problem = "partial_write"
            elif step["state"] not in {"applied", "partial_write"}:
                problem = "interrupted_file_operation"
            key = os.path.normcase(step["path"])
            previous = paths.get(key)
            if previous and (previous["after"] != step["before"] or previous.get("after_stamp") != step.get("before_stamp")):
                problem = "intervening_changes"
            step_skipped = (
                group_skipped
                or step.get("snapshot", True) is False
                or (previous is not None and previous.get("snapshot", True) is False)
            )
            merged = {
                **step,
                "before": previous["before"] if previous else step["before"],
                "mode": previous["mode"] if previous else step["mode"],
            }
            if step_skipped:
                merged["snapshot"] = False
                merged["can_undo"] = False
                merged["reason"] = problem or "snapshot_skipped_limit"
            paths[key] = merged
        return list(paths.values()), problem

    def plan(self, state, ids=None):
        groups = self.groups(state)
        if ids is not None and (not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids) or set(ids) - {g["id"] for g in groups}):
            raise AgentError("agent_review_selection_changed")
        items, conflicts, skipped = [], [], []
        for group in groups:
            if ids is not None and group["id"] not in ids:
                continue
            net, problem = self.net(group)
            for item in net:
                rel_path = _display_review_path(item, self.root)
                if item.get("snapshot", True) is False and problem in {None, "partial_write"}:
                    skipped.append({"path": rel_path, "can_undo": False, "reason": "snapshot_skipped_limit"})
                    if ids is not None:
                        conflicts.append({"path": rel_path, "reason": "snapshot_skipped_limit"})
                    continue
                try:
                    self.content(item["before"])
                    self.content(item["after"])
                    current = digest(read_file(safe_path(self.root, item["path"], outside_ok=bool(item.get("outside_workspace")))))
                    reason = (problem if problem != "partial_write" else None) or ("file_changed" if current != item["after"]["sha256"] or fingerprint(Path(item["path"])) != item.get("after_stamp") else None)
                except (OSError, AgentError) as exc:
                    reason = str(exc) if isinstance(exc, AgentError) else "file_unavailable"
                if reason:
                    conflicts.append({"path": rel_path, "reason": reason})
                items.append(item)
        token = digest(json.dumps({"items": items, "skipped": skipped, "ids": ids}, sort_keys=True).encode())
        return {"token": token, "items": items, "conflicts": conflicts, "skipped": skipped, "can_undo": bool(items) and not conflicts}

    def preview(self, ids=None):
        with file_transaction(self.lock):
            state = self.load()
            self.recover(state)
            plan = self.plan(state, ids)
            return {**plan, "items": [{"path": _display_review_path(i, self.root), "before": i["before"], "after": i["after"]} for i in plan["items"]]}

    def undo(self, ids, token, request_id):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 80 or not isinstance(token, str):
            raise AgentError("agent_invalid_review_request")
        with file_transaction(self.lock), self._usage_transaction():
            state = self.load()
            self.recover(state)
            prior = next((u for u in state["undos"] if u["id"] == request_id), None)
            if prior:
                if prior["token"] != token:
                    raise AgentError("agent_review_request_id_conflict")
                return self.undo_result(prior)
            plan = self.plan(state, ids)
            if plan["conflicts"]:
                return {"status": "conflict", "conflicts": plan["conflicts"], "skipped": plan["skipped"], "completed": []}
            if token == plan["token"] and ids is None and not plan["items"] and plan["skipped"]:
                undo = {
                    "id": request_id,
                    "token": token,
                    "status": "completed",
                    "record_ids": [r["id"] for r in state["records"]],
                    "items": [],
                    "skipped": plan["skipped"],
                }
                state["undos"].append(undo)
                self.save(state)
                return self.undo_result(undo)
            if token != plan["token"] or not plan["can_undo"]:
                raise AgentError("agent_review_preview_changed")
            if len(state["undos"]) >= MAX_OPERATIONS:
                raise AgentError("agent_review_operation_limit")
            undo = {"id": request_id, "token": token, "status": "running", "record_ids": [r["id"] for r in state["records"]],
                    "items": [{**item, "state": "pending"} for item in plan["items"]], "skipped": plan["skipped"]}
            state["undos"].append(undo)
            try:
                self.save(state)
            except OSError:
                raise AgentError("agent_review_storage_unavailable") from None
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
            except Exception as exc:
                # Disk may be full even when saving the failure. The prior durable
                # 'writing' marker still prevents a misleading success on restart.
                if str(exc) == "agent_local_file_in_use" and getattr(exc, "nothing_written", False):
                    item.update(state="pending", problem="file_in_use")
                    undo["status"] = "blocked_file_in_use"
                else:
                    undo["status"] = "interrupted"
                try:
                    self.save(state)
                except OSError:
                    pass
            return self.undo_result(undo)

    def undo_result(self, undo):
        result = {"id": undo["id"], "status": undo["status"],
                  "completed": [_display_review_path(i, self.root) for i in undo["items"] if i["state"] in {"done", "recovered"}],
                  "remaining": [{"path": _display_review_path(i, self.root), "state": i.get("problem", i["state"])} for i in undo["items"] if i["state"] not in {"done", "recovered"}]}
        if "skipped" in undo:
            result["skipped"] = undo["skipped"]
        return result

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
            snapshot_skipped = sum(r.get("extra_skipped", 0) for r in state["records"] if r.get("overflow_summary"))
            for group in self.groups(state):
                net, problem = self.net(group)
                for step in net:
                    is_skipped = step.get("snapshot", True) is False
                    if is_skipped:
                        snapshot_skipped += 1
                    if step["before"] == step["after"] and not problem:
                        continue
                    relative = _display_review_path(step, self.root, posix=True)
                    before, after = step["before"], step["after"]
                    eff_problem = "snapshot_skipped_limit" if is_skipped and problem in {None, "partial_write"} else problem
                    entry = {"id": group["id"], "path": relative, "status": "created" if before["sha256"] is None else "deleted" if after["sha256"] is None else "modified",
                             "existed_before": before["sha256"] is not None, "sha256": after["sha256"], "test_file": bool(TEST_PATH.search(relative)),
                             "before_bytes": before["bytes"], "after_bytes": after["bytes"], "source": "file_tool", "can_undo": eff_problem in {None, "partial_write"} and not is_skipped,
                             "state": step["state"],
                             "rename_group": len(group["paths"]) > 1, "problem": eff_problem, "media_type": mimetypes.guess_type(relative)[0] or "application/octet-stream",
                             **({"snapshot": False} if is_skipped else {}),
                             **({"outside_workspace": True} if step.get("outside_workspace") else {})}
                    if not is_skipped:
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
                    if step["state"] == "partial_write":
                        entry["restore_available"] = entry["can_undo"]
                    files.append(entry)
            represented = {f["path"] for f in files}
            for record in state["records"]:
                for step in record["steps"]:
                    relative = _display_review_path(step, self.root, posix=True)
                    if step.get("undone") or step["state"] not in {"pending", "not_applied"} or relative in represented:
                        continue
                    files.append({"id": digest(json.dumps([os.path.normcase(step["path"])]).encode())[:24],
                                  "path": relative, "state": step["state"], "status": "modified", "source": "file_tool", "can_undo": False,
                                  "before_bytes": step["before"]["bytes"], "after_bytes": step["after"]["bytes"],
                                  "media_type": mimetypes.guess_type(relative)[0] or "application/octet-stream"})
                    represented.add(relative)
            return {"files": files, "files_total": len(files), "snapshot_skipped": snapshot_skipped,
                    "digest": digest(json.dumps(files, sort_keys=True).encode()) if files else None,
                    "undo_operations": [self.undo_result(u) for u in state["undos"][-10:]],
                    "limits": {"task_bytes": MAX_BYTES, "home_bytes": MAX_HOME_BYTES, "files": MAX_FILES, "operations": MAX_OPERATIONS},
                    "not_covered": "Shell/build observations are not proof of Agent ownership and cannot be undone. External services and conversation history are not reverted."}
