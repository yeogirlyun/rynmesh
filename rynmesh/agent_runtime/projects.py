"""Owner-selected local projects. Selection is a task grant, never a model proposal."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

from .store import AgentError, AgentToolRejected, encoded

ENTRY_FILES = ("AGENTS.md", "README.md", "AI_HANDOFF.md", "PROJECT_MAP.md")
MANIFESTS = ("package.json", "pyproject.toml", "Cargo.toml", "go.mod", "pom.xml", "build.gradle")
SOURCE_DIRS = ("src", "app", "lib", "rynmesh", "webapp", "test", "tests", "docs", "scripts")


def root_path(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or not Path(value).is_absolute():
        raise AgentError("agent_invalid_project_path")
    try:
        root = Path(value).resolve(strict=True)
        if not root.is_dir():
            raise OSError
    except (OSError, ValueError):
        raise AgentError("agent_project_not_directory") from None
    return root


def project_id(root):
    return "project_" + hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()[:24]


def inside_file(root, relative):
    path = root / relative
    try:
        return path.is_file() and path.resolve(strict=True).is_relative_to(root)
    except OSError:
        return False


_KNOWN_FRAMEWORKS = (
    "typescript",
    "react",
    "vue",
    "next",
    "nuxt",
    "svelte",
    "vite",
    "electron",
    "tauri",
    "tailwindcss",
    "express",
    "fastify",
    "@nestjs/core",
    "@midwayjs/core",
    "jest",
    "vitest",
    "playwright",
    "cypress",
)


def overview(root):
    """Bounded structural metadata; never enumerate run/, config values or credentials."""
    result = {"name": root.name, "path": str(root),
              "entry_files": [name for name in ENTRY_FILES if inside_file(root, name)],
              "manifests": [name for name in MANIFESTS if inside_file(root, name)],
              "directories": [name for name in SOURCE_DIRS if (root / name).is_dir()
                              and (root / name).resolve().is_relative_to(root)]}
    if inside_file(root, "package.json") and (root / "package.json").stat().st_size <= 128 * 1024:
        try:
            package = json.loads((root / "package.json").read_text(encoding="utf-8-sig"))
            if isinstance(package, dict):
                scripts = package.get("scripts")
                result["scripts"] = sorted(str(key)[:80] for key in scripts)[:32] if isinstance(scripts, dict) else []
                deps = {**(package.get("dependencies") or {}), **(package.get("devDependencies") or {})}
                result["frameworks"] = [name for name in _KNOWN_FRAMEWORKS if name in deps]
        except (OSError, ValueError, TypeError):
            result["manifest_unreadable"] = True
    return result


def register(data, value):
    if set(value) - {"path"}:
        raise AgentError("agent_invalid_request")
    root = root_path(value.get("path"))
    info = overview(root)
    ident = project_id(root)
    projects = data.setdefault("projects", {})
    if ident not in projects and len(projects) >= 32:
        raise AgentError("agent_project_limit")
    existing = projects.get(ident)
    if existing:
        info["name"] = existing["name"]
    generation = (existing or {}).get("generation", 1)
    if existing and not existing.get("enabled"):
        generation += 1
    projects[ident] = {"id": ident, **info, "enabled": True, "generation": generation}
    data["selected_project_id"] = ident
    return projects[ident]


def rename(data, ident, value):
    if set(value) != {"name"}:
        raise AgentError("agent_invalid_request")
    name = value["name"]
    if not isinstance(name, str) or not name.strip() or len(name) > 160 or any(ord(char) < 32 for char in name):
        raise AgentError("agent_invalid_project_name")
    project = data.get("projects", {}).get(ident)
    if not project or not project.get("enabled"):
        raise AgentError("agent_project_unavailable")
    project["name"] = name.strip()
    return project


def selected(data, ident, *, refresh=False):
    if not isinstance(ident, str):
        raise AgentError("agent_invalid_project_id")
    value = data.get("projects", {}).get(ident)
    if not value or not value.get("enabled"):
        raise AgentError("agent_project_unavailable")
    root = root_path(value["path"])
    if str(root) != value["path"]:
        raise AgentError("agent_project_path_changed")
    result = {**value, **overview(root), "name": value["name"]} if refresh else dict(value)
    if refresh and inside_file(root, "AGENTS.md"):
        with (root / "AGENTS.md").open("rb") as stream:
            raw = stream.read(64 * 1024 + 1)
        result["instructions"] = {"path": "AGENTS.md", "text": raw[:64 * 1024].decode("utf-8-sig", errors="replace"),
                                  "incomplete": len(raw) > 64 * 1024}
    return result


def validate_binding(data, binding):
    current = selected(data, binding.get("id"))
    if current["path"] != binding.get("path") or current["generation"] != binding.get("generation"):
        raise AgentError("agent_project_permission_revoked")
    return current


INSPECT_BYTES = 18500
INSPECT_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "target", "__pycache__",
                               ".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox"})


def _inspect_cancelled(cancelled):
    if cancelled():
        raise AgentToolRejected("agent_cancelled")


def _inspect_link(path):
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _inspect_listing(root, path, cancelled, *, byte_limit, max_entries):
    """A bounded shallow sample; only a complete scan has an exact sorted offset."""
    relative = path.relative_to(root).as_posix()
    listing = {"path": relative, "entries": [], "incomplete": False, "next_offset": None,
               "continuation": None}
    entries, directories, scanned = [], [], True
    started = time.monotonic()
    try:
        with os.scandir(path) as scanner:
            for index, entry in enumerate(scanner):
                if index % 16 == 0:
                    _inspect_cancelled(cancelled)
                if index >= 1000 or time.monotonic() - started >= 0.25:
                    scanned = False
                    break
                child = Path(entry.path)
                linked = _inspect_link(child)
                directory = entry.is_dir(follow_symlinks=False) and not linked
                entries.append({"name": entry.name, "directory": directory, "symlink": linked})
                if directory:
                    directories.append(child)
    except OSError:
        listing.update(incomplete=True, error="directory_unreadable")
        scanned = False
    entries.sort(key=lambda item: item["name"].casefold())
    for entry in entries[:max_entries]:
        listing["entries"].append(entry)
        if len(encoded(listing)) > byte_limit:
            listing["entries"].pop()
            break
    if not scanned or len(listing["entries"]) < len(entries):
        offset = len(listing["entries"]) if scanned else 0
        listing.update(incomplete=True, next_offset=offset,
                       continuation={"tool": "ryn_file_list", "arguments": {"path": relative, "offset": offset}})
        if not scanned:
            listing["note"] = "Bounded sample, not an exact sorted prefix; continuation restarts the sorted listing."
    return listing, sorted(directories, key=lambda item: item.name.casefold()), scanned


def _inspect_page(page, name, byte_limit):
    """Fit whole supplied lines; never label an omitted line as already supplied."""
    result = {key: value for key, value in page.items() if key in
              {"sha256", "bytes", "start_line", "end_line", "total_lines", "next_start_line", "next_offset"}}
    result.update(path=name, status="read", text="", incomplete=True,
                  continuation={"tool": "ryn_file_read", "arguments": {"path": name, "start_line": 1}})
    supplied = []
    for line in page.get("text", "").splitlines(keepends=True):
        result["text"] = "".join([*supplied, line])
        if len(encoded(result)) > byte_limit:
            result["text"] = "".join(supplied)
            break
        supplied.append(line)
    if supplied:
        result["end_line"] = result.get("start_line", 1) + len(supplied) - 1
        next_line = result["end_line"] + 1
        result["incomplete"] = next_line <= result.get("total_lines", next_line)
        result["next_start_line"] = next_line if result["incomplete"] else None
        result["next_offset"] = None
        result["continuation"] = ({"tool": "ryn_file_read", "arguments": {"path": name, "start_line": next_line}}
                                  if result["incomplete"] else None)
    elif page.get("total_lines") == 0:
        result.update(incomplete=False, continuation=None, next_start_line=None, next_offset=None)
    else:
        result.update(status="deferred", next_start_line=1, next_offset=0,
                      continuation={"tool": "ryn_file_read", "arguments": {"path": name, "offset": 0}})
        result.pop("start_line", None)
        result.pop("end_line", None)
    return result


def _inspect_fit(result):
    """Keep complete metadata and continuations even for unusually large names/pages."""
    while len(encoded(result)) > INSPECT_BYTES:
        result["snapshot_incomplete"] = True
        if result["directory_listings"]:
            result["directory_listings"].pop()
            result["directory_listings_incomplete"] = True
            if isinstance(result.get("nested_instructions"), dict):
                result["nested_instructions"]["complete"] = False
            result["directory_listings_continuation"] = {
                "tool": "ryn_file_list", "arguments": {"path": ".", "offset": 0},
                "note": "List unobserved child directories explicitly; this snapshot is only one level deep."}
        elif result["root_listing"]["entries"]:
            listing = result["root_listing"]
            listing["entries"].pop()
            offset = 0 if listing.get("note") else len(listing["entries"])
            listing.update(incomplete=True, next_offset=offset,
                           continuation={"tool": "ryn_file_list", "arguments": {"path": ".", "offset": offset}})
            if isinstance(result.get("nested_instructions"), dict):
                result["nested_instructions"]["complete"] = False
        else:
            page = next((item for item in reversed(result["entry_documents"]) if item.get("text")), None)
            if page is not None:
                lines = page["text"].splitlines(keepends=True)
                shorter = _inspect_page({**page, "text": "".join(lines[:-1])}, page["path"], len(encoded(page)))
                page.clear()
                page.update(shorter)
            elif result.get("git", {}).get("changes"):
                result["git"]["changes"].pop()
                result["git"]["changes_incomplete"] = True
                result["git"]["changes_continuation"] = {
                    "tool": "ryn_shell_execute", "arguments": {
                        "command": "git status --porcelain --untracked-files=no", "cwd": result["path"]}}
            elif result.get("scripts"):
                result["scripts"].pop()
                result["scripts_incomplete"] = True
                result["scripts_continuation"] = {"tool": "ryn_file_read", "arguments": {"path": "package.json"}}
            else:
                raise AgentToolRejected("agent_project_snapshot_limit")


def inspect(root, *, read_file=None, cancelled=lambda: False, validate=None):
    """One bounded entry observation; file bodies use the caller's ordinary reader."""
    root = Path(root).resolve(strict=True)

    def check():
        _inspect_cancelled(cancelled)
        if validate is not None:
            validate()

    check()
    result = overview(root)
    result.update(root_listing={}, directory_listings=[], directory_listings_incomplete=False,
                  directory_listings_continuation=None, entry_documents=[],
                  snapshot_depth=1, pruned_directories=[], snapshot_incomplete=False,
                  entry_snapshot_version=1, observed_at=time.time())
    if (root / ".git").exists():
        result["git"] = {}
        for field, arguments in (("branch", ["branch", "--show-current"]),
                                 ("changes", ["status", "--porcelain", "--untracked-files=no"])):
            check()
            try:
                run = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True,
                                     timeout=3, encoding="utf-8", errors="replace",
                                     creationflags=0x08000000 if os.name == "nt" else 0)
                if run.returncode == 0:
                    lines = run.stdout.splitlines()
                    if field == "changes":
                        shown = []
                        for line in lines[:80]:
                            if len(encoded([*shown, line])) > 2000:
                                break
                            shown.append(line)
                        result["git"].update(changes=shown, changes_incomplete=len(shown) < len(lines))
                        if len(shown) < len(lines):
                            result["git"]["changes_continuation"] = {
                                "tool": "ryn_shell_execute", "arguments": {
                                    "command": "git status --porcelain --untracked-files=no", "cwd": str(root)}}
                    else:
                        result["git"][field] = run.stdout.strip()[:160]
                else:
                    result["git"][field] = "unavailable"
            except (OSError, subprocess.TimeoutExpired):
                result["git"][field] = "unavailable"
    document_bytes = 0
    for name in ENTRY_FILES:
        check()
        path = root / name
        item = {"path": name, "status": "absent", "incomplete": False, "continuation": None}
        if _inspect_link(path):
            item.update(status="skipped_symlink", incomplete=True)
        elif path.exists():
            item.update(status="available", incomplete=True,
                        continuation={"tool": "ryn_file_read", "arguments": {"path": name, "start_line": 1}})
            if read_file is not None and inside_file(root, name):
                try:
                    page = read_file(name)
                    item = _inspect_page(page, name, max(400, min(3000, 8000 - document_bytes)))
                except AgentToolRejected as exc:
                    if str(exc) not in {"agent_local_file_missing_or_too_large", "agent_local_file_too_large", "agent_local_file_not_utf8"}:
                        raise
                    item.update(status="unreadable", error=str(exc))
                except OSError:
                    item.update(status="unreadable", error="file_unreadable")
            elif not inside_file(root, name):
                item.update(status="unreadable", error="not_a_regular_workspace_file", continuation=None)
        result["entry_documents"].append(item)
        document_bytes += len(encoded(item))
    check()
    listing, directories, scanned = _inspect_listing(root, root, cancelled, byte_limit=3500, max_entries=60)
    result["root_listing"] = listing
    directories = [path for path in directories if path.name not in INSPECT_SKIP_DIRS]
    result["pruned_directories"] = [entry["name"] for entry in listing["entries"]
                                    if entry["directory"] and entry["name"] in INSPECT_SKIP_DIRS]
    remaining = max(0, INSPECT_BYTES - len(encoded(result)) - 1000)
    for path in directories[:8]:
        check()
        # A directory swapped for a link is never traversed by the composite read.
        if _inspect_link(path) or not path.resolve().is_relative_to(root):
            result["directory_listings_incomplete"] = True
            continue
        child, _, _ = _inspect_listing(root, path, cancelled, byte_limit=max(250, min(1400, remaining)), max_entries=30)
        if len(encoded(result)) + len(encoded(child)) > INSPECT_BYTES - 700:
            result["directory_listings_incomplete"] = True
            break
        result["directory_listings"].append(child)
        remaining = max(0, remaining - len(encoded(child)))
    if not scanned or len(directories) > len(result["directory_listings"]):
        result["directory_listings_incomplete"] = True
    if result["directory_listings_incomplete"]:
        result["directory_listings_continuation"] = {
            "tool": "ryn_file_list", "arguments": {"path": ".", "offset": 0},
            "note": "List unobserved child directories explicitly; this snapshot is only one level deep."}
    nested_found = sorted(
        f"{child['path']}/{entry['name']}"
        for child in result["directory_listings"]
        for entry in child.get("entries", [])
        if not entry.get("directory") and not entry.get("symlink") and entry.get("name") in ("AGENTS.md", "CLAUDE.md")
    )
    nested_complete = bool(
        not listing["incomplete"]
        and not result["directory_listings_incomplete"]
        and all(
            not child.get("incomplete")
            and not any(entry.get("directory") or entry.get("symlink") for entry in child.get("entries", []))
            for child in result["directory_listings"]
        )
    )
    result["nested_instructions"] = {"found": nested_found, "complete": nested_complete}
    result["snapshot_incomplete"] = bool(
        listing["incomplete"] or result["directory_listings_incomplete"]
        or any(item["incomplete"] for item in result["entry_documents"])
        or any(item["incomplete"] for item in result["directory_listings"])
        or result.get("git", {}).get("changes_incomplete") or result.get("scripts_incomplete")
        or "unavailable" in result.get("git", {}).values())
    _inspect_fit(result)
    check()
    if len(encoded(result)) > INSPECT_BYTES:
        raise AgentToolRejected("agent_project_snapshot_limit")
    return result


def guidance(row):
    project = row.get("project")
    if not project:
        return ""
    path = project.get("path") or ""
    info = {key: project[key] for key in ("name", "path", "entry_files", "manifests", "directories") if key in project}
    env_block = (
        "<environment_context>\n"
        f"  <cwd>{path}</cwd>\n"
        f"  <project>{json.dumps(info, ensure_ascii=False, separators=(',', ':'))}</project>\n"
        "</environment_context>"
    )
    instructions = project.get("instructions") or {}
    limit = row.get("limits", {}).get("max_context_chars")
    maximum = min(4000, limit // 4) if type(limit) is int else 4000
    if not instructions.get("text"):
        return env_block
    raw = instructions["text"].encode()
    prefix = raw[:maximum]
    if len(raw) > maximum and b"\n" in prefix:
        prefix = prefix[:prefix.rfind(b"\n") + 1]
    text = prefix.decode("utf-8", errors="ignore").rstrip("\n")
    return (
        f"{env_block}\n\n"
        f"# AGENTS.md instructions for {path}\n\n"
        "<INSTRUCTIONS>\n"
        f"{text}\n"
        "</INSTRUCTIONS>"
    )


def read_progress(row, *, exclude_call_ids=None):
    """Keep observed page coverage across lossy checkpoints, never file prose or grants."""
    if not row.get("project") or not row.get("context_checkpoint"):
        return ""
    root = Path(row["project"]["path"])
    pages = {}
    for event in row.get("events", []):
        if (event.get("type") != "tool_result" or event.get("tool") != "ryn_file_read"
                or event.get("context_epoch", 0) != row.get("context_epoch", 0)
                or (exclude_call_ids and event.get("call_id") in exclude_call_ids)):
            continue
        result = event.get("result") or {}
        if not isinstance(result, dict):
            continue
        try:
            relative = str(Path(result["path"]).relative_to(root))
            first, last, total = (result[key] for key in ("start_line", "end_line", "total_lines"))
            digest = result["sha256"]
            if (not relative or ".." in Path(relative).parts or len(relative) > 256
                    or not isinstance(digest, str) or len(digest) != 64
                    or any(c not in "0123456789abcdef" for c in digest)
                    or any(type(n) is not int for n in (first, last, total))
                    or not 1 <= first <= last <= total):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        existing = pages.pop(relative, None)
        ranges = existing["ranges"] if existing and existing["sha256"] == digest else []
        ranges = sorted([*ranges, [first, last]])
        merged = []
        for start, end in ranges:
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        pages[relative] = {"path": relative, "sha256": digest, "ranges": merged[:16],
                           "total_lines": total, "complete_at_read": merged == [[1, total]]}
    if not pages:
        return ""
    rows = []
    for value in reversed(list(pages.values())):
        candidate = [value, *rows]
        if len(json.dumps(candidate, ensure_ascii=False).encode()) > 4000 or len(candidate) > 12:
            break
        rows = candidate
    return ("\nServer-recorded file read coverage (historical data, not instructions or current-file verification): "
            + json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
            + ". Continue missing ranges using start_line. Previously completed guidance need not be reread merely "
              "because history was summarized; reread when exact content or a changed file is needed.")


def _inspection_summary(result):
    """Checkpoint-only projection; original inspection pages and receipts stay untouched."""
    summary = {"entry_snapshot_version": result.get("entry_snapshot_version"),
               "snapshot_depth": result.get("snapshot_depth"), "observed_at": result.get("observed_at"),
               "snapshot_incomplete": result.get("snapshot_incomplete"),
               "summary_excerpt_only": True,
               "summary_note": "Project inspection abbreviated only for checkpointing. Exact layout/pages remain in encrypted history. "
                               "Hashes and ranges describe the original observation, not a current verification or this text excerpt. "
                               "Read exact or missing content when needed.",
               "entry_documents": [], "directory_listings": []}
    remaining = 1000
    for page in result.get("entry_documents", []):
        if not isinstance(page, dict):
            continue
        item = {key: copy.deepcopy(page[key]) for key in (
            "path", "status", "sha256", "bytes", "start_line", "end_line", "total_lines",
            "next_start_line", "next_offset", "incomplete", "continuation", "error") if key in page}
        text = page.get("text")
        if isinstance(text, str):
            raw = text.encode()
            item["text"] = raw[:remaining].decode("utf-8", errors="ignore")
            remaining -= len(item["text"].encode())
            if item["text"] != text:
                item["summary_text_omitted"] = True
        summary["entry_documents"].append(item)
    root = result.get("root_listing") or {}
    summary["root_listing"] = {"path": ".", "entry_names": [item.get("name") for item in root.get("entries", [])[:8]],
                               "observation_incomplete": root.get("incomplete"),
                               "summary_entries_omitted": len(root.get("entries", [])) > 8,
                               "continuation": {"tool": "ryn_file_list", "arguments": {"path": ".", "offset": 0}}}
    for directory in result.get("directory_listings", [])[:3]:
        summary["directory_listings"].append({
            "path": directory.get("path"),
            "entry_names": [item.get("name") for item in directory.get("entries", [])[:3]],
            "observation_incomplete": directory.get("incomplete"),
            "summary_entries_omitted": len(directory.get("entries", [])) > 3,
            "continuation": copy.deepcopy(directory.get("continuation"))})
    if result.get("git"):
        git = result["git"]
        summary["git"] = {"branch": git.get("branch"), "changes": [],
                          "changes_incomplete": git.get("changes_incomplete", False)}
        changes = git.get("changes", [])
        if not isinstance(changes, list):
            summary["git"]["changes_status"] = changes
            changes = []
        for line in changes:
            if len(encoded([*summary["git"]["changes"], line])) > 600:
                break
            summary["git"]["changes"].append(line)
        if len(summary["git"]["changes"]) < len(changes):
            summary["git"]["summary_changes_omitted"] = True
        if summary["git"]["changes_incomplete"] or summary["git"].get("summary_changes_omitted"):
            summary["git"]["continuation"] = {
                "tool": "ryn_shell_execute", "arguments": {"command": "git status --porcelain --untracked-files=no"}}
    summary["summary_layout_omitted"] = True
    # Directory names and Git fields can dominate even with no document text.
    # Drop reloadable samples first; retain every entry page's exact hash/range/continuation.
    while len(encoded(summary)) > 4000:
        if summary["directory_listings"]:
            summary["directory_listings"].pop()
        elif summary["root_listing"]["entry_names"]:
            summary["root_listing"]["entry_names"].pop()
            summary["root_listing"]["summary_entries_omitted"] = True
        elif summary.get("git"):
            summary.pop("git")
            summary["summary_git_omitted"] = True
        else:
            page = next((item for item in reversed(summary["entry_documents"]) if item.get("text")), None)
            if page is None:
                raise AgentError("agent_context_limit")
            page["text"] = ""
            page["summary_text_omitted"] = True
    return summary


def summary_history(messages):
    """Bound source excerpts only in a lossy summary request, never the archive.

    A closed batch of eight source reads can exceed the model window by itself.
    Reading progress remains deterministic; exact source can be read again.
    Business/resource results and owner messages are deliberately untouched.
    """
    projected = copy.deepcopy(messages)
    for message in projected:
        message.pop("provider_state", None)
    names = {call["id"]: call["function"]["name"] for message in projected
             for call in message.get("tool_calls", [])}
    for message in projected:
        name = names.get(message.get("tool_call_id"))
        if message.get("role") != "tool" or name not in {"ryn_file_read", "ryn_project_inspect"}:
            continue
        try:
            result = json.loads(message["content"])
        except (TypeError, ValueError):
            continue
        if not isinstance(result, dict):
            continue
        if name == "ryn_project_inspect":
            if len(encoded(result)) > 4000:
                message["content"] = encoded(_inspection_summary(result)).decode()
            continue
        if not isinstance(result.get("text"), str):
            continue
        raw = result["text"].encode()
        if len(raw) > 2000:
            result["text"] = raw[:2000].decode("utf-8", errors="ignore")
            result["summary_excerpt_only"] = True
            result["summary_note"] = "Source excerpt abbreviated for checkpointing only. Exact source remains in encrypted history; do not infer unseen code."
            message["content"] = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return projected
