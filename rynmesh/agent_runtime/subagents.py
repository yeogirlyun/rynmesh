"""Codex-aligned Multi-Agent / Subagent orchestration runtime."""
from __future__ import annotations

import copy
import fnmatch
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath

from . import budget
from .control import close_queue, settle
from .protocol import event, now
from .state import initialize, owner_update
from .store import DEFAULT_SETTINGS, AgentToolRejected

NICKNAMES: tuple[str, ...] = (
    "Turing",
    "Curie",
    "Feynman",
    "Euclid",
    "Ada",
    "Gauss",
    "Fourier",
    "Noether",
    "Euler",
    "Pascal",
    "Laplace",
    "Dirac",
    "Hopper",
    "Bernoulli",
    "Hilbert",
    "Poincare",
    "Riemann",
    "Boole",
    "Shannon",
    "Lovelace",
    "Archimedes",
    "Ptolemy",
    "Hypatia",
    "Avicenna",
    "Galileo",
    "Kepler",
    "Descartes",
    "Newton",
    "Leibniz",
    "Lagrange",
    "Faraday",
    "Darwin",
    "Maxwell",
    "Mendel",
    "Pasteur",
    "Planck",
    "Einstein",
    "Bohr",
    "Schrodinger",
    "Heisenberg",
    "Galois",
    "Ramanujan",
    "Godel",
    "VonNeumann",
    "Socrates",
    "Plato",
    "Aristotle",
    "Epicurus",
    "Zeno",
    "Locke",
    "Hume",
    "Kant",
    "Hegel",
    "Kierkegaard",
    "Nietzsche",
    "Russell",
    "Wittgenstein",
    "Popper",
    "Rawls",
    "Arendt",
    "Beauvoir",
    "Sartre",
    "Camus",
    "Foucault",
    "Erdos",
)

MAX_OPEN_SUBAGENTS: int = 6
MAX_SUBAGENT_DEPTH: int = 2

FORK_ISOLATION_PROMPT: str = (
    "You are now a newly spawned child agent. The preceding conversation is inherited parent context. "
    "Follow your own role, identity, and task below; earlier identities, tasks, and action-specific approvals belong to your ancestors. "
    "Inherited tool calls are historical records; live tool sessions and process handles are not inherited. "
    "Report your result to your parent."
)

MULTI_AGENT_COORDINATOR_GUIDANCE: str = (
    "<multi_agent_mode>"
    "Delegate sidecar exploration (role=explorer, read-only), parallel scoped implementation (role=worker with disjoint write_scope), "
    "or long test/process waits (role=awaiter) via ryn_spawn_agent; keep work blocking your immediate next step local. "
    "Give each worker explicit write_scope ownership. Call ryn_wait_agent sparingly after finishing non-overlapping local steps. "
    "Completed subagents emit <subagent_notification> and remain open against the concurrency limit until closed with ryn_close_agent."
    "</multi_agent_mode>"
)

ROLES: frozenset[str] = frozenset({"default", "explorer", "worker", "awaiter"})
FORK_MODES: frozenset[str] = frozenset({"none", "all"})

SUBAGENT_TOOL_NAMES: frozenset[str] = frozenset({
    "ryn_spawn_agent",
    "ryn_send_message",
    "ryn_followup_task",
    "ryn_wait_agent",
    "ryn_interrupt_agent",
    "ryn_close_agent",
    "ryn_list_agents",
    "ryn_resume_agent",
})

# Keep schemas compact so ALL_INTERNAL_SPECS stays well under the pin floor in tests.
SUBAGENT_TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "ryn_spawn_agent",
            "description": (
                "Spawn a background subagent (Turing, Curie, Feynman, etc.) to explore code, implement scoped file "
                "changes, or run checks in parallel. Returns immediately with agent_id, nickname, and agent_path."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task_name", "prompt"],
                "properties": {
                    "task_name": {"type": "string", "minLength": 1, "maxLength": 64},
                    "prompt": {"type": "string", "minLength": 1, "maxLength": 8000},
                    "role": {"type": "string", "enum": ["default", "explorer", "worker", "awaiter"]},
                    "fork_turns": {"type": "string", "enum": ["none", "all"]},
                    "write_scope": {
                        "type": "array",
                        "maxItems": 32,
                        "items": {"type": "string", "minLength": 1, "maxLength": 260},
                    },
                    "model_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "reasoning_effort": {"type": "string", "enum": ["low", "medium", "high"]},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ryn_send_message",
            "description": "Send supplemental context or a message to an existing subagent by nickname, task_name, agent_path, or agent_id.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["target", "message"],
                "properties": {
                    "target": {"type": "string", "minLength": 1, "maxLength": 128},
                    "message": {"type": "string", "minLength": 1, "maxLength": 8000},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ryn_followup_task",
            "description": "Assign a new follow-up task prompt to an existing non-closed subagent and trigger its execution.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["target", "prompt"],
                "properties": {
                    "target": {"type": "string", "minLength": 1, "maxLength": 128},
                    "prompt": {"type": "string", "minLength": 1, "maxLength": 8000},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ryn_wait_agent",
            "description": (
                "Wait for running subagents to complete or emit <subagent_notification> results. "
                "Specify targets or omit to wait for any active subagent."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "targets": {
                        "type": "array",
                        "maxItems": 20,
                        "items": {"type": "string", "minLength": 1, "maxLength": 128},
                    },
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ryn_interrupt_agent",
            "description": "Interrupt/pause a running subagent (or 'all') immediately.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["target"],
                "properties": {
                    "target": {"type": "string", "minLength": 1, "maxLength": 128},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ryn_close_agent",
            "description": "Close a completed or running subagent (or 'all') and release its resources.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["target"],
                "properties": {
                    "target": {"type": "string", "minLength": 1, "maxLength": 128},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ryn_list_agents",
            "description": "List live agents in the current root thread tree. Optionally filter by task-path prefix.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path_prefix": {"type": "string", "minLength": 1, "maxLength": 128},
                    "include_closed": {"type": "boolean"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ryn_resume_agent",
            "description": "Resume a previously closed, paused, or interrupted agent by target so it can receive messages and tasks.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["target"],
                "properties": {
                    "target": {"type": "string", "minLength": 1, "maxLength": 128},
                    "prompt": {"type": "string", "minLength": 1, "maxLength": 8000},
                },
            },
        },
    },
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ms() -> int:
    return int(time.time() * 1000)


def _slug_task_name(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_\-]+", "_", (raw or "").strip()).strip("_").lower()
    return (cleaned or "subtask")[:48]


def _pick_nickname(existing_nicknames: set[str]) -> str:
    for candidate in NICKNAMES:
        if candidate not in existing_nicknames:
            return candidate
    idx = 2
    while True:
        for base in NICKNAMES:
            candidate = f"{base}-{idx}"
            if candidate not in existing_nicknames:
                return candidate
        idx += 1


def role_guidance(meta: dict) -> str:
    role = str(meta.get("role") or "default")
    nickname = str(meta.get("nickname") or "Agent")
    agent_path = str(meta.get("agent_path") or "/root/subagent")
    write_scope = meta.get("write_scope") or []
    parts = [
        f"<subagent_identity nickname=\"{nickname}\" role=\"{role}\" agent_path=\"{agent_path}\">",
        f"<agent name=\"{agent_path}\" nickname=\"{nickname}\" role=\"{role}\" />",
        f"You are subagent {nickname} ({role}) at {agent_path}, delegated a focused task by the parent coordinator agent.",
        "Do NOT call ryn_request_input to ask the human user questions directly; if blocked, report clearly in your final answer.",
        "Do NOT spawn unnecessary recursive subagents unless explicitly instructed.",
        "<multi_agent_role>",
    ]
    if role == "explorer":
        parts.append(
            "Role=explorer: You are strictly READ-ONLY. Search and read files quickly, gather exact file paths, "
            "line numbers, and symbols, and return a concise, high-signal technical summary without modifying any files."
        )
    elif role == "worker":
        parts.append(
            "Role=worker: You are responsible for implementing concrete code changes and verifying them. "
            "You are not alone in the codebase. Ignore unrelated edits made by the user or sibling agents, "
            "never revert others' changes, and keep your modifications tightly scoped."
        )
        if write_scope:
            scope_str = ", ".join(str(p) for p in write_scope)
            parts.append(
                f"Write Scope Ownership: You are ONLY permitted to modify files matching: [{scope_str}]. "
                "Do not edit files outside your write_scope."
            )
    elif role == "awaiter":
        parts.append(
            "Role=awaiter: Monitor the requested command, process, or test suite until completion or failure. "
            "Do not modify source files unless explicitly instructed. Report the final exit status and concise root-cause findings."
        )
    else:
        parts.append(
            "Role=default: Complete the delegated task directly and report verified results to your parent agent."
        )
    parts.append("</multi_agent_role>")
    parts.append("</subagent_identity>")
    return "\n".join(parts)


def validate_subagent_tool(name: str, arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise AgentToolRejected("agent_invalid_tool_arguments", correction="Tool arguments must be a JSON object.")
    if name == "ryn_spawn_agent":
        task_name = str(arguments.get("task_name") or "").strip()
        prompt = str(arguments.get("prompt") or "").strip()
        if not task_name:
            raise AgentToolRejected("agent_invalid_tool_arguments", correction="task_name is required for ryn_spawn_agent.")
        if not prompt:
            raise AgentToolRejected("agent_invalid_tool_arguments", correction="prompt is required for ryn_spawn_agent.")
        role = str(arguments.get("role") or "default").strip().lower()
        if role not in ROLES:
            raise AgentToolRejected(
                "agent_invalid_tool_arguments",
                correction=f"role must be one of {sorted(ROLES)}.",
            )
        fork_turns = str(arguments.get("fork_turns") or "none").strip().lower()
        if fork_turns not in FORK_MODES:
            raise AgentToolRejected(
                "agent_invalid_tool_arguments",
                correction="fork_turns must be 'none' or 'all'.",
            )
        raw_scope = arguments.get("write_scope")
        write_scope: list[str] = []
        if raw_scope is not None:
            if not isinstance(raw_scope, list) or any(not isinstance(item, str) or not item.strip() for item in raw_scope):
                raise AgentToolRejected(
                    "agent_invalid_tool_arguments",
                    correction="write_scope must be an array of non-empty file/directory path strings.",
                )
            write_scope = [item.strip().replace("\\", "/") for item in raw_scope[:32]]
        model_id = str(arguments.get("model_id") or "").strip() or None
        effort = str(arguments.get("reasoning_effort") or "").strip().lower() or None
        if effort is not None and effort not in {"low", "medium", "high"}:
            raise AgentToolRejected("agent_invalid_tool_arguments", correction="reasoning_effort must be low, medium, or high.")
        return {
            "task_name": _slug_task_name(task_name),
            "prompt": prompt,
            "role": role,
            "fork_turns": fork_turns,
            "write_scope": write_scope,
            "model_id": model_id,
            "reasoning_effort": effort,
        }
    if name == "ryn_send_message":
        target = str(arguments.get("target") or "").strip()
        message = str(arguments.get("message") or "").strip()
        if not target or not message:
            raise AgentToolRejected("agent_invalid_tool_arguments", correction="target and message are required.")
        return {"target": target, "message": message}
    if name == "ryn_followup_task":
        target = str(arguments.get("target") or "").strip()
        prompt = str(arguments.get("prompt") or "").strip()
        if not target or not prompt:
            raise AgentToolRejected("agent_invalid_tool_arguments", correction="target and prompt are required.")
        return {"target": target, "prompt": prompt}
    if name == "ryn_wait_agent":
        raw_targets = arguments.get("targets")
        targets: list[str] = []
        if raw_targets is not None:
            if isinstance(raw_targets, str) and raw_targets.strip():
                targets = [raw_targets.strip()]
            elif isinstance(raw_targets, list):
                targets = [str(item).strip() for item in raw_targets if str(item).strip()]
            else:
                raise AgentToolRejected("agent_invalid_tool_arguments", correction="targets must be an array of strings.")
        timeout = arguments.get("timeout_seconds", 30)
        if type(timeout) is not int or timeout < 1 or timeout > 120:
            timeout = 30
        return {"targets": targets, "timeout_seconds": timeout}
    if name in {"ryn_interrupt_agent", "ryn_close_agent"}:
        target = str(arguments.get("target") or "").strip()
        if not target:
            raise AgentToolRejected("agent_invalid_tool_arguments", correction=f"target is required for {name}.")
        return {"target": target}
    if name == "ryn_list_agents":
        path_prefix = str(arguments.get("path_prefix") or "").strip() or None
        include_closed = bool(arguments.get("include_closed", False))
        return {"path_prefix": path_prefix, "include_closed": include_closed}
    if name == "ryn_resume_agent":
        target = str(arguments.get("target") or "").strip()
        if not target:
            raise AgentToolRejected("agent_invalid_tool_arguments", correction="target is required for ryn_resume_agent.")
        prompt = str(arguments.get("prompt") or "").strip() or None
        return {"target": target, "prompt": prompt}
    raise AgentToolRejected("agent_unknown_internal_tool")


def _normalize_rel_path(path_str: str, workspace_root: str = "") -> str:
    clean = str(path_str or "").strip().replace("\\", "/")
    if workspace_root:
        ws = str(workspace_root).strip().replace("\\", "/").rstrip("/")
        if ws and clean.lower().startswith(ws.lower() + "/"):
            clean = clean[len(ws) + 1 :]
    while clean.startswith("./"):
        clean = clean[2:]
    return clean.strip("/")


def _matches_scope_entry(rel_path: str, scope_entry: str, workspace_root: str = "") -> bool:
    norm_target = _normalize_rel_path(rel_path, workspace_root).lower()
    norm_scope = _normalize_rel_path(scope_entry, workspace_root).lower()
    if not norm_scope or norm_scope in {".", "*", "**"}:
        return True
    if norm_target == norm_scope:
        return True
    if norm_target.startswith(norm_scope.rstrip("/") + "/"):
        return True
    if any(ch in norm_scope for ch in ("*", "?", "[")) and fnmatch.fnmatch(norm_target, norm_scope):
        return True
    if "/" in norm_scope and norm_target.endswith("/" + norm_scope):
        return True
    if "/" not in norm_scope and PurePosixPath(norm_target).name == norm_scope:
        return True
    return False


def _extract_write_paths(tool_name: str, arguments: dict) -> list[str]:
    if not isinstance(arguments, dict):
        return []
    if tool_name in {"ryn_file_write", "ryn_file_edit"}:
        p = arguments.get("path")
        return [str(p)] if isinstance(p, str) and p.strip() else []
    if tool_name == "ryn_file_manage":
        paths = []
        for k in ("path", "destination"):
            v = arguments.get(k)
            if isinstance(v, str) and v.strip():
                paths.append(str(v))
        return paths
    if tool_name == "ryn_apply_patch":
        patch_text = str(arguments.get("patch") or "")
        paths = []
        for line in patch_text.splitlines():
            for prefix in ("*** Add File:", "*** Update File:", "*** Delete File:", "*** Move to:"):
                if line.startswith(prefix):
                    candidate = line[len(prefix) :].strip()
                    if candidate:
                        paths.append(candidate)
        return paths
    return []


def check_subagent_tool_policy(row: dict, tool_name: str, arguments: dict) -> None:
    """Enforce role=explorer read-only and role=worker write_scope boundaries."""
    meta = row.get("subagent_meta")
    if not isinstance(meta, dict):
        return
    role = str(meta.get("role") or "default")
    nickname = str(meta.get("nickname") or "Subagent")
    write_tools = {"ryn_file_write", "ryn_file_edit", "ryn_file_manage", "ryn_apply_patch"}
    if role == "explorer" and tool_name in write_tools:
        raise AgentToolRejected(
            "agent_subagent_read_only",
            correction=(
                f"Subagent {nickname} has role='explorer' (read-only) and cannot call {tool_name}. "
                "Report required changes to the parent agent instead."
            ),
        )
    write_scope = meta.get("write_scope") or []
    if write_scope and tool_name in write_tools:
        ws_root = str((row.get("local_execution") or {}).get("workspace_root") or "")
        target_paths = _extract_write_paths(tool_name, arguments)
        for target in target_paths:
            if not any(_matches_scope_entry(target, scope_item, ws_root) for scope_item in write_scope):
                raise AgentToolRejected(
                    "agent_subagent_write_scope_violation",
                    correction=(
                        f"Subagent {nickname} is restricted to write_scope={write_scope} and cannot modify '{target}'. "
                        "Only edit files inside your assigned write_scope."
                    ),
                    details={"path": target, "write_scope": write_scope, "nickname": nickname},
                )


def _collect_tree_subagents(data: dict, row: dict) -> list[dict]:
    """Collect all subagent task rows in the root thread tree breadth-first."""
    tasks = data.get("tasks") or {}
    meta = row.get("subagent_meta") or {}
    root_id = str(meta.get("root_task_id") or row.get("id") or "")
    root_row = tasks.get(root_id) if isinstance(tasks.get(root_id), dict) else row
    collected: list[dict] = []
    seen: set[str] = {str(root_row.get("id") or "")}
    queue: list[dict] = [root_row]
    while queue:
        cur = queue.pop(0)
        for cid in list(cur.get("subagent_ids") or []):
            child = tasks.get(cid)
            if isinstance(child, dict) and cid not in seen:
                seen.add(cid)
                collected.append(child)
                queue.append(child)
    return collected


def resolve_subagent_targets(data: dict, parent_row: dict, targets: list[str] | str) -> list[dict]:
    """Resolve one or more target specifiers (id, nickname, task_name, canonical agent_path, or 'all') to subagent rows."""
    tasks = data.get("tasks") or {}
    direct_ids = list(parent_row.get("subagent_ids") or [])
    direct_children = [tasks[cid] for cid in direct_ids if isinstance(tasks.get(cid), dict)]
    tree_children = _collect_tree_subagents(data, parent_row)
    caller_id = str(parent_row.get("id") or "")
    all_candidates = direct_children if direct_children else [c for c in tree_children if str(c.get("id")) != caller_id]

    if isinstance(targets, str):
        if targets.strip().lower() == "all":
            return all_candidates
        target_list = [targets.strip()]
    else:
        target_list = [str(t).strip() for t in (targets or []) if str(t).strip()]
        if not target_list or any(t.lower() == "all" for t in target_list):
            return all_candidates

    matched: list[dict] = []
    seen_ids: set[str] = set()
    for raw in target_list:
        needle = raw.lower()
        needle_mention = needle[1:] if needle.startswith("@") else needle
        found = None
        # 1. Prioritize direct children of caller (relative task_name, nickname, id, or path)
        for child in direct_children:
            cid = str(child.get("id") or "")
            meta = child.get("subagent_meta") or {}
            nick = str(meta.get("nickname") or "").lower()
            tname = str(meta.get("task_name") or "").lower()
            apath = str(meta.get("agent_path") or "").lower()
            if needle_mention in {cid.lower(), nick, tname, apath} or apath.endswith("/" + needle_mention):
                found = child
                break
        # 2. Fall back to the entire root thread tree (supporting sibling/cousin canonical paths like /root/task1/task_3)
        if found is None:
            for child in tree_children:
                cid = str(child.get("id") or "")
                if cid == caller_id:
                    continue
                meta = child.get("subagent_meta") or {}
                nick = str(meta.get("nickname") or "").lower()
                tname = str(meta.get("task_name") or "").lower()
                apath = str(meta.get("agent_path") or "").lower()
                if needle_mention in {cid.lower(), nick, tname, apath} or apath.endswith("/" + needle_mention):
                    found = child
                    break
        if found is None:
            raise AgentToolRejected(
                "agent_subagent_not_found",
                correction=f"Subagent '{raw}' was not found in the current root thread tree.",
            )
        if found["id"] not in seen_ids:
            seen_ids.add(found["id"])
            matched.append(found)
    return matched


def _new_user_msg(content: str, *, kind: str = "message") -> dict:
    return {
        "role": "user",
        "content": content,
        "message_id": "owner_" + uuid.uuid4().hex,
        "ui": {"kind": kind, "text": content},
    }


def spawn_agent_in_tx(data: dict, parent_row: dict, args: dict) -> dict:
    """Create and queue a new subagent child task linked to parent_row."""
    from .prompt import PROMPT_VERSION, select_base_instructions

    tasks = data.setdefault("tasks", {})
    settings = {**DEFAULT_SETTINGS, **(data.get("settings") or {})}
    tree_agents = _collect_tree_subagents(data, parent_row)

    # Codex v2 resident eviction: completed agents remain open and count toward MAX_OPEN_SUBAGENTS
    # until closed. If quota is reached, automatically close oldest settled open subagent first.
    open_agents = [c for c in tree_agents if not (c.get("subagent_meta") or {}).get("closed")]
    if len(open_agents) >= MAX_OPEN_SUBAGENTS:
        settled_open = [
            c
            for c in open_agents
            if c.get("status") in {"completed", "failed", "cancelled", "interrupted"}
        ]
        if settled_open:
            evict_target = settled_open[0]
            evict_target.setdefault("subagent_meta", {})["closed"] = True
        else:
            raise AgentToolRejected(
                "agent_subagent_limit_reached",
                correction=(
                    f"Maximum of {MAX_OPEN_SUBAGENTS} open subagents reached. "
                    "Call ryn_wait_agent or ryn_close_agent before spawning more."
                ),
            )

    child_ids = parent_row.setdefault("subagent_ids", [])
    existing_nicks = {
        str((c.get("subagent_meta") or {}).get("nickname") or "")
        for c in tree_agents
    }
    nickname = _pick_nickname(existing_nicks)
    task_name = _slug_task_name(str(args.get("task_name") or nickname))
    parent_meta = parent_row.get("subagent_meta") or {}
    parent_path = str(parent_meta.get("agent_path") or "/root").rstrip("/")
    parent_depth = max(0, len([seg for seg in parent_path.split("/") if seg]) - 1)
    if parent_depth >= MAX_SUBAGENT_DEPTH:
        raise AgentToolRejected(
            "agent_subagent_depth_limit_reached",
            correction=f"Maximum subagent nesting depth ({MAX_SUBAGENT_DEPTH}) reached at {parent_path}. Handle this task directly.",
        )
    agent_path = f"{parent_path}/{task_name}"
    existing_paths = {str((c.get("subagent_meta") or {}).get("agent_path") or "").lower() for c in tree_agents}
    if agent_path.lower() in existing_paths:
        suffix = 2
        while f"{agent_path}_{suffix}".lower() in existing_paths:
            suffix += 1
        task_name = f"{task_name}_{suffix}"
        agent_path = f"{parent_path}/{task_name}"
    root_task_id = str(parent_meta.get("root_task_id") or parent_row.get("id") or "")
    role = str(args.get("role") or "default")
    fork_turns = str(args.get("fork_turns") or "none")
    write_scope = list(args.get("write_scope") or [])
    prompt = str(args.get("prompt") or "").strip()
    model_id = str(args.get("model_id") or parent_row.get("model_id") or settings.get("model_id") or "")
    explicit_effort = args.get("reasoning_effort") or parent_row.get("reasoning_effort")
    effort = explicit_effort or ("low" if role == "awaiter" else "medium")

    parent_perm = str(parent_row.get("permission_mode") or settings.get("permission_mode") or "workspace")
    if role == "explorer" or parent_perm == "read_only":
        child_perm = "read_only"
    elif parent_perm == "confirm_changes" and role == "worker":
        child_perm = "workspace"
    else:
        child_perm = parent_perm

    child_id = f"agent_{uuid.uuid4().hex}"
    created = now()

    initial_messages: list[dict] = []
    if fork_turns == "all":
        for msg in parent_row.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            m_role = msg.get("role")
            m_content = str(msg.get("content") or "").strip()
            if m_role == "user" and m_content:
                initial_messages.append(_new_user_msg(m_content))
            elif m_role == "assistant" and m_content:
                initial_messages.append({"role": "assistant", "content": m_content, "context_epoch": 0})
        initial_messages.append(_new_user_msg(FORK_ISOLATION_PROMPT, kind="information"))

    first_msg = _new_user_msg(prompt)
    initial_messages.append(first_msg)

    meta = {
        "id": child_id,
        "parent_task_id": str(parent_row.get("id") or ""),
        "root_task_id": root_task_id,
        "nickname": nickname,
        "task_name": task_name,
        "agent_path": agent_path,
        "role": role,
        "fork_turns": fork_turns,
        "write_scope": write_scope,
        "instructions": prompt,
        "spawned_at_ms": now_ms(),
        "settled_at_ms": None,
        "closed": False,
        "notified_event_seq": -1,
        "notified_status": None,
        "last_notification": None,
    }

    child_limits = {key: settings[key] for key in DEFAULT_SETTINGS if key.startswith("max_")}
    if isinstance(parent_row.get("limits"), dict):
        child_limits.update(copy.deepcopy(parent_row["limits"]))

    child_row = {
        "id": child_id,
        "parent_task_id": str(parent_row.get("id") or ""),
        "subagent_meta": meta,
        "title": f"[{nickname}] {prompt[:56]}",
        "model_id": model_id,
        "status": "queued",
        "revision": 0,
        "created_at": created,
        "updated_at": created,
        "messages": initial_messages,
        "pending_user_messages": [],
        "events": [],
        "receipts": [],
        "result_refs": [],
        "pending_approval": None,
        "steps": 0,
        "result": "",
        "error": None,
        "limits": child_limits,
        "permission_mode": child_perm,
        "allowed_write_tools": list(parent_row.get("allowed_write_tools") or settings.get("allowed_write_tools") or []),
        "queue": [],
        "in_flight": None,
        "approved": None,
        "elapsed_seconds": 0.0,
        "base_instructions": parent_row.get("base_instructions") or select_base_instructions(),
        "context_budget_provenance": parent_row.get("context_budget_provenance", "default_auto"),
    }
    initialize(child_row)
    budget.initialize(child_row)
    if isinstance(parent_row.get("budget"), dict) and isinstance(parent_row["budget"].get("limits"), dict):
        child_row["budget"]["limits"] = copy.deepcopy(parent_row["budget"]["limits"])
    if explicit_effort:
        child_row["reasoning_effort"] = explicit_effort
    meta["display_effort"] = effort
    child_row["local_execution"] = copy.deepcopy(
        parent_row.get("local_execution") or {"workspace_root": settings.get("workspace_root") or "", "desktop_access": settings.get("desktop_access", False)}
    )
    if parent_row.get("project"):
        child_row["project"] = copy.deepcopy(parent_row["project"])
    child_row["skills"] = copy.deepcopy(parent_row.get("skills") or [])
    child_row["verify_text_responses"] = False
    child_row["response_language"] = parent_row.get("response_language") or "zh-CN"
    child_row["model_destination"] = copy.deepcopy(parent_row.get("model_destination"))
    if parent_row.get("model_destination_confirmed"):
        child_row["model_destination_confirmed"] = True
    if parent_row.get("scope"):
        child_row["scope"] = copy.deepcopy(parent_row["scope"])
    child_row["prompt_version"] = PROMPT_VERSION
    event(
        child_row,
        "created",
        parent_task_id=parent_row.get("id"),
        nickname=nickname,
        role=role,
        agent_path=agent_path,
    )
    tasks[child_id] = child_row
    child_ids.append(child_id)
    event(
        parent_row,
        "subagent_spawned",
        subagent_id=child_id,
        nickname=nickname,
        task_name=task_name,
        agent_path=agent_path,
        role=role,
        write_scope=write_scope,
    )
    return {
        "ok": True,
        "agent_id": child_id,
        "nickname": nickname,
        "task_name": task_name,
        "agent_path": agent_path,
        "role": role,
        "fork_turns": fork_turns,
        "write_scope": write_scope,
        "model_id": model_id,
        "reasoning_effort": effort,
        "status": "queued",
        "instructions": prompt,
    }


def send_message_in_tx(data: dict, parent_row: dict, args: dict) -> dict:
    targets = resolve_subagent_targets(data, parent_row, args["target"])
    child = targets[0]
    meta = child.get("subagent_meta") or {}
    if meta.get("closed"):
        raise AgentToolRejected("agent_subagent_closed", correction=f"Subagent {meta.get('nickname')} is already closed.")
    msg_text = str(args["message"]).strip()
    user_msg = _new_user_msg(msg_text, kind="information")
    if child.get("in_flight") or child.get("queue"):
        child.setdefault("pending_user_messages", []).append(user_msg)
        if not child.get("pending_control"):
            child["pending_control"] = {"kind": "steer", "remain_paused": child.get("status") == "paused"}
    else:
        child.setdefault("messages", []).append(user_msg)
    event(child, "information_added", goal_revision=child["goal"]["revision"])
    event(
        parent_row,
        "subagent_message_sent",
        subagent_id=child["id"],
        nickname=meta.get("nickname"),
        agent_path=meta.get("agent_path"),
    )
    return {
        "ok": True,
        "agent_id": child["id"],
        "nickname": meta.get("nickname"),
        "agent_path": meta.get("agent_path"),
        "status": child.get("status"),
        "delivered": True,
    }


def followup_task_in_tx(data: dict, parent_row: dict, args: dict) -> dict:
    targets = resolve_subagent_targets(data, parent_row, args["target"])
    child = targets[0]
    meta = child.setdefault("subagent_meta", {})
    if meta.get("closed"):
        raise AgentToolRejected("agent_subagent_closed", correction=f"Subagent {meta.get('nickname')} is already closed.")
    prompt = str(args["prompt"]).strip()
    user_msg = _new_user_msg(prompt, kind="steer")
    meta["instructions"] = prompt
    meta["notified_event_seq"] = -1
    meta["notified_status"] = None
    meta["settled_at_ms"] = None
    ended = child.get("status") in {"completed", "failed", "interrupted", "cancelled"}
    if not child.get("in_flight") or ended:
        owner_update(child, user_msg)
    budget.new_round(child)
    child.update(
        approved=None,
        pending_approval=None,
        pending_input=None,
        result="",
        error=None,
        pause_requested=False,
    )
    child.setdefault("pending_user_messages", []).append(user_msg)
    child["pending_control"] = {"kind": "steer", "remain_paused": False}
    if not child.get("in_flight"):
        child.update(steps=0, elapsed_seconds=0.0)
    event(child, "goal_changed", goal_revision=child["goal"]["revision"])
    close_queue(child, event, "agent_owner_changed")
    settle(child, event, data=data)
    event(
        parent_row,
        "subagent_followup",
        subagent_id=child["id"],
        nickname=meta.get("nickname"),
        agent_path=meta.get("agent_path"),
    )
    return {
        "ok": True,
        "agent_id": child["id"],
        "nickname": meta.get("nickname"),
        "agent_path": meta.get("agent_path"),
        "status": child.get("status"),
        "instructions": prompt,
    }


def interrupt_agent_in_tx(data: dict, parent_row: dict, args: dict) -> dict:
    targets = resolve_subagent_targets(data, parent_row, args["target"])
    interrupted: list[dict] = []
    now = now_iso()
    for child in targets:
        meta = child.setdefault("subagent_meta", {})
        prev_status = child.get("status")
        if prev_status in {"queued", "running", "waiting_approval", "waiting_input", "pausing"}:
            close_queue(child, event, "agent_cancelled", keep_flight=False)
            child["pause_requested"] = True
            child["status"] = "paused"
            child["in_flight"] = None
            child["pending_approval"] = None
            child["pending_input"] = None
            child["updated_at"] = now
            meta["settled_at_ms"] = now_ms()
            on_subagent_settled_in_tx(data, child)
            cascade_stop_subagents_in_tx(data, child, reason="subagent_interrupted", close=False)
        interrupted.append({
            "agent_id": child["id"],
            "nickname": meta.get("nickname"),
            "agent_path": meta.get("agent_path"),
            "previous_status": prev_status,
            "status": child.get("status"),
        })
    parent_row["updated_at"] = now
    return {"ok": True, "interrupted": interrupted}


def close_agent_in_tx(data: dict, parent_row: dict, args: dict) -> dict:
    targets = resolve_subagent_targets(data, parent_row, args["target"])
    closed_list: list[dict] = []
    now = now_iso()
    for child in targets:
        meta = child.setdefault("subagent_meta", {})
        prev_status = child.get("status")
        meta["closed"] = True
        if prev_status in {"queued", "running", "waiting_approval", "waiting_input", "pausing", "paused"}:
            close_queue(child, event, "agent_cancelled", keep_flight=False)
            child["pause_requested"] = True
            child["status"] = "cancelled"
            child["in_flight"] = None
            child["pending_approval"] = None
            child["pending_input"] = None
            child["updated_at"] = now
            meta["settled_at_ms"] = now_ms()
            on_subagent_settled_in_tx(data, child)
        cascade_stop_subagents_in_tx(data, child, reason="subagent_closed", close=True)
        closed_list.append({
            "agent_id": child["id"],
            "nickname": meta.get("nickname"),
            "agent_path": meta.get("agent_path"),
            "previous_status": prev_status,
            "status": child.get("status"),
            "closed": True,
        })
    parent_row["updated_at"] = now
    return {"ok": True, "closed": closed_list}


def list_agents_in_tx(data: dict, parent_row: dict, args: dict) -> dict:
    """List agents in the current root thread tree, optionally filtered by path_prefix."""
    tree_agents = _collect_tree_subagents(data, parent_row)
    path_prefix = str(args.get("path_prefix") or "").strip().lower()
    include_closed = bool(args.get("include_closed", False))
    items: list[dict] = []
    for child in tree_agents:
        meta = child.get("subagent_meta") or {}
        if not include_closed and meta.get("closed"):
            continue
        apath = str(meta.get("agent_path") or "")
        if path_prefix and not apath.lower().startswith(path_prefix):
            continue
        summary = project_subagent_summary(child)
        items.append({
            "agent_id": summary["id"],
            "nickname": summary["nickname"],
            "task_name": summary["task_name"],
            "agent_path": summary["agent_path"],
            "role": summary["role"],
            "status": summary["status"],
            "is_waiting": summary["is_waiting"],
            "closed": summary["closed"],
            "write_scope": summary["write_scope"],
            "diff_stats": summary["diff_stats"],
            "current_step_text": summary["current_step_text"],
        })
    return {"ok": True, "agents": items, "count": len(items)}


def resume_agent_in_tx(data: dict, parent_row: dict, args: dict) -> dict:
    """Resume a previously closed, paused, or interrupted subagent so it can receive work again."""
    targets = resolve_subagent_targets(data, parent_row, args["target"])
    child = targets[0]
    meta = child.setdefault("subagent_meta", {})
    meta["closed"] = False
    prompt = str(args.get("prompt") or "").strip() if args.get("prompt") else None
    now = now_iso()
    if prompt:
        user_msg = _new_user_msg(prompt, kind="steer")
        meta["instructions"] = prompt
        meta["notified_event_seq"] = -1
        meta["notified_status"] = None
        meta["settled_at_ms"] = None
        if not child.get("in_flight"):
            owner_update(child, user_msg)
        budget.new_round(child)
        child.update(
            approved=None,
            pending_approval=None,
            pending_input=None,
            result="",
            error=None,
            pause_requested=False,
        )
        child.setdefault("pending_user_messages", []).append(user_msg)
        child["pending_control"] = {"kind": "steer", "remain_paused": False}
        if not child.get("in_flight"):
            child.update(steps=0, elapsed_seconds=0.0)
        event(child, "goal_changed", goal_revision=child["goal"]["revision"])
        close_queue(child, event, "agent_owner_changed")
        settle(child, event, data=data)
    elif child.get("status") in {"paused", "interrupted", "cancelled"}:
        child["pause_requested"] = False
        child["error"] = None
        child["status"] = "queued"
        meta["settled_at_ms"] = None
    child["updated_at"] = now
    parent_row["updated_at"] = now
    event(
        parent_row,
        "subagent_resumed",
        subagent_id=child["id"],
        nickname=meta.get("nickname"),
        agent_path=meta.get("agent_path"),
    )
    return {
        "ok": True,
        "agent_id": child["id"],
        "nickname": meta.get("nickname"),
        "agent_path": meta.get("agent_path"),
        "status": child.get("status"),
        "closed": False,
    }


def drain_subagent_notifications_in_tx(data: dict, parent_row: dict, target_ids: set[str] | None = None) -> list[dict]:
    """Mark matching unread notifications on parent_row as delivered and return them."""
    queue = parent_row.get("subagent_notifications") or []
    delivered: list[dict] = []
    for item in queue:
        if not isinstance(item, dict) or item.get("delivered"):
            continue
        if target_ids and item.get("agent_id") not in target_ids:
            continue
        item["delivered"] = True
        delivered.append(copy.deepcopy(item))
    return delivered


def format_notification_tag(notif: dict) -> str:
    payload = {
        "agent_id": notif.get("agent_id"),
        "agent_path": notif.get("agent_path"),
        "nickname": notif.get("nickname"),
        "role": notif.get("role"),
        "status": notif.get("status"),
        "summary": notif.get("summary") or "",
    }
    if notif.get("diff_stats"):
        payload["diff_stats"] = notif["diff_stats"]
    return f"<subagent_notification>\n{json.dumps(payload, ensure_ascii=False)}\n</subagent_notification>"


def compute_subagent_diff_and_timeline(child_row: dict) -> tuple[dict, list[dict], str]:
    """Compute diff_stats, structured timeline items, and current_step_text for Stitch UI views."""
    added_total = 0
    removed_total = 0
    edited_files: set[str] = set()
    ws_root = str((child_row.get("local_execution") or {}).get("workspace_root") or "")

    receipt_by_call: dict[str, dict] = {}
    for receipt in child_row.get("receipts") or []:
        if isinstance(receipt, dict) and receipt.get("call_id"):
            receipt_by_call[str(receipt["call_id"])] = receipt

    result_by_call: dict[str, dict] = {}
    for ev in child_row.get("events") or []:
        if not isinstance(ev, dict):
            continue
        cid = str(ev.get("call_id") or "")
        if cid and ev.get("type") == "tool_result":
            result_by_call[cid] = ev

    raw_steps: list[dict] = []
    for msg in child_row.get("messages") or []:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        thought = str(msg.get("reasoning_content") or msg.get("content") or "").strip()
        tool_calls = msg.get("tool_calls") or []
        if thought and tool_calls:
            raw_steps.append({"kind": "thought", "text": thought[:500]})
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            cid = str(call.get("id") or "")
            fn = call.get("function") or {}
            tname = str(fn.get("name") or "")
            raw_args = fn.get("arguments")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args if isinstance(raw_args, dict) else {})
            except Exception:
                args = {}
            res_ev = result_by_call.get(cid)
            receipt = receipt_by_call.get(cid) or {}
            preview = receipt.get("preview") if isinstance(receipt.get("preview"), dict) else {}
            status = "running" if res_ev is None else (
                "error" if isinstance(res_ev.get("result"), dict) and res_ev["result"].get("error") else "completed"
            )
            ev_timing = res_ev.get("timing") if res_ev and isinstance(res_ev.get("timing"), dict) else {}
            dur_sec = max(1, int(round(float(ev_timing.get("duration_seconds") or ev_timing.get("elapsed_seconds") or 1.0))))

            if tname in {"ryn_file_read", "ryn_file_search", "ryn_file_list", "ryn_project_inspect"}:
                target = _normalize_rel_path(str(args.get("path") or args.get("query") or "."), ws_root)
                res_obj = res_ev.get("result") if res_ev and isinstance(res_ev.get("result"), dict) else {}
                line_range = None
                if tname == "ryn_file_read":
                    s_line = res_obj.get("start_line") or args.get("start_line") or 1
                    e_line = res_obj.get("end_line") or args.get("end_line") or ""
                    line_range = f"第 {s_line}-{e_line} 行" if e_line else f"第 {s_line} 行起"
                    summary = f"读取 {target}" + (f" (L{s_line}-L{e_line})" if e_line else "")
                elif tname == "ryn_file_search":
                    q = str(args.get("query") or "")
                    matches = len(res_obj.get("matches") or []) if isinstance(res_obj.get("matches"), list) else 0
                    summary = f"搜索 \"{q}\" ({matches} 处匹配)"
                else:
                    summary = f"浏览目录 {target}"
                raw_steps.append({
                    "kind": "read_step",
                    "tool": tname,
                    "target": target,
                    "line_range": line_range,
                    "summary": summary,
                    "duration_seconds": dur_sec,
                    "status": status,
                })
            elif tname in {"ryn_file_write", "ryn_file_edit", "ryn_apply_patch"}:
                files_list = preview.get("files") if tname == "ryn_apply_patch" and isinstance(preview.get("files"), list) else None
                if files_list:
                    for fitem in files_list:
                        fpath = _normalize_rel_path(str(fitem.get("path") or ""), ws_root)
                        f_add = int(fitem.get("added_lines") or 0)
                        f_rem = int(fitem.get("removed_lines") or 0)
                        if status == "completed":
                            added_total += f_add
                            removed_total += f_rem
                            if fpath:
                                edited_files.add(fpath)
                        raw_steps.append({
                            "kind": "file_edit",
                            "tool": tname,
                            "path": fpath or "file",
                            "added": f_add,
                            "removed": f_rem,
                            "diff_lines": _parse_unified_diff_lines(str(fitem.get("diff") or "")),
                            "duration_seconds": dur_sec,
                            "status": status,
                        })
                else:
                    fpath = _normalize_rel_path(str(args.get("path") or preview.get("path") or ""), ws_root)
                    if not fpath and tname == "ryn_apply_patch":
                        extracted = _extract_write_paths(tname, args)
                        fpath = _normalize_rel_path(extracted[0], ws_root) if extracted else "patch"
                    f_add = int(preview.get("added_lines") or 0)
                    f_rem = int(preview.get("removed_lines") or 0)
                    diff_text = str(preview.get("diff") or "")
                    if not diff_text and tname == "ryn_file_edit" and args.get("old_text") and args.get("new_text"):
                        diff_text = _fallback_diff(str(args.get("old_text")), str(args.get("new_text")))
                        if not f_add and not f_rem:
                            f_add = len(str(args.get("new_text")).splitlines())
                            f_rem = len(str(args.get("old_text")).splitlines())
                    elif not diff_text and tname == "ryn_file_write" and args.get("content"):
                        lines = str(args.get("content")).splitlines()
                        if not f_add:
                            f_add = len(lines)
                        diff_text = "\n".join(f"+{ln}" for ln in lines[:24])
                    if status == "completed":
                        added_total += f_add
                        removed_total += f_rem
                        if fpath:
                            edited_files.add(fpath)
                    raw_steps.append({
                        "kind": "file_edit",
                        "tool": tname,
                        "path": fpath or "file",
                        "added": f_add,
                        "removed": f_rem,
                        "diff_lines": _parse_unified_diff_lines(diff_text),
                        "duration_seconds": dur_sec,
                        "status": status,
                    })
            else:
                target = str(args.get("command") or args.get("path") or args.get("query") or tname)[:80]
                raw_steps.append({
                    "kind": "tool_call",
                    "tool": tname,
                    "target": target,
                    "summary": f"{tname}: {target}" if target != tname else tname,
                    "duration_seconds": dur_sec,
                    "status": status,
                })

    timeline: list[dict] = []
    for item in raw_steps:
        if item["kind"] == "read_step":
            if timeline and timeline[-1]["kind"] == "read_group":
                timeline[-1]["items"].append(item)
                timeline[-1]["count"] = len(timeline[-1]["items"])
            else:
                timeline.append({"kind": "read_group", "count": 1, "items": [item]})
        else:
            timeline.append(item)

    child_status = str(child_row.get("status") or "queued")
    current_step_text = ""
    if child_status in {"queued", "running"}:
        if raw_steps:
            last = raw_steps[-1]
            if last["kind"] == "file_edit":
                current_step_text = f"正在编辑 {last['path']}"
            elif last["kind"] == "read_step":
                current_step_text = f"正在{last['summary']}"
            elif last["kind"] == "tool_call":
                current_step_text = f"正在执行 {last['summary']}"
            elif last["kind"] == "thought":
                current_step_text = f"{last['text'][:80]}"
        if not current_step_text:
            current_step_text = "正在分析任务并生成执行方案..."
    elif child_status == "completed":
        current_step_text = str(child_row.get("result") or "任务已完成").strip()[:160]
    elif child_status in {"paused", "interrupted"}:
        current_step_text = "已中断 · 等待后续指示"
    elif child_status == "failed":
        current_step_text = f"执行异常: {child_row.get('error') or 'unknown'}"

    diff_stats = {
        "added": added_total,
        "removed": removed_total,
        "files": len(edited_files),
    }
    return diff_stats, timeline[-24:], current_step_text


def _fallback_diff(old_text: str, new_text: str) -> str:
    lines: list[str] = ["@@ -1 +1 @@"]
    for ln in old_text.splitlines()[:12]:
        lines.append(f"-{ln}")
    for ln in new_text.splitlines()[:12]:
        lines.append(f"+{ln}")
    return "\n".join(lines)


def _parse_unified_diff_lines(diff_text: str) -> list[dict]:
    if not diff_text:
        return []
    out: list[dict] = []
    old_ln = 1
    new_ln = 1
    for raw in diff_text.splitlines():
        if raw.startswith(("---", "+++")):
            continue
        if raw.startswith("@@"):
            m = re.search(r"-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?", raw)
            if m:
                old_ln = int(m.group(1))
                new_ln = int(m.group(2))
            continue
        if raw.startswith("+"):
            out.append({"type": "add", "old_lineno": None, "new_lineno": new_ln, "text": raw[1:]})
            new_ln += 1
        elif raw.startswith("-"):
            out.append({"type": "del", "old_lineno": old_ln, "new_lineno": None, "text": raw[1:]})
            old_ln += 1
        else:
            text = raw[1:] if raw.startswith(" ") else raw
            out.append({"type": "ctx", "old_lineno": old_ln, "new_lineno": new_ln, "text": text})
            old_ln += 1
            new_ln += 1
        if len(out) >= 36:
            break
    return out


def _clean_conclusion_preview(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = re.sub(r"^#+\s*", "", text.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"[*_`>\-]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:160] if cleaned else None


def project_subagent_summary(child_row: dict) -> dict:
    meta = child_row.get("subagent_meta") or {}
    status = str(child_row.get("status") or "queued")
    role = str(meta.get("role") or "default")
    diff_stats, timeline, current_step_text = compute_subagent_diff_and_timeline(child_row)
    spawned_ms = int(meta.get("spawned_at_ms") or now_ms())
    settled_ms = meta.get("settled_at_ms")
    run_timing = child_row.get("run_timing") or {}
    recorded_sec = float(child_row.get("elapsed_seconds") or child_row.get("active_seconds") or run_timing.get("recorded_seconds") or 0.0)
    if status in {"queued", "running"}:
        elapsed = max(recorded_sec, max(0.0, (now_ms() - spawned_ms) / 1000.0))
    elif isinstance(settled_ms, (int, float)) and settled_ms >= spawned_ms:
        elapsed = max(recorded_sec, max(1.0, (settled_ms - spawned_ms) / 1000.0))
    else:
        elapsed = max(1.0, recorded_sec)

    steps = int(child_row.get("steps") or 0)
    step_index = max(1, steps)
    est_total = max(step_index + (1 if status in {"queued", "running"} else 0), 4 if role == "worker" else 3)
    if status == "completed":
        est_total = max(1, steps)
        progress = 100
    elif status in {"failed", "cancelled", "interrupted"}:
        progress = min(95, max(15, steps * 18))
    elif status in {"queued", "running"}:
        progress = min(92, max(18, int(round((step_index / max(1, est_total)) * 85))))
    else:
        progress = min(90, max(20, steps * 18))

    is_waiting = bool(
        not meta.get("closed")
        and status in {"paused", "waiting_input", "waiting_approval"}
    )
    if is_waiting:
        step_progress_label = f"步骤 {step_index}/{est_total} · 等待主智能体或条件就绪..."
    elif status in {"queued", "running"}:
        step_progress_label = f"步骤 {step_index}/{est_total} · {current_step_text}"
    elif status == "completed":
        step_progress_label = f"共 {est_total} 步已完成"
    else:
        step_progress_label = current_step_text

    final_reply = str(child_row.get("result") or "").strip() or None
    conclusion_preview = _clean_conclusion_preview(final_reply)
    notif = meta.get("last_notification")
    if not notif and status == "completed" and final_reply:
        notif = {
            "agent_id": child_row.get("id"),
            "agent_path": meta.get("agent_path") or f"/root/{meta.get('task_name') or 'subagent'}",
            "nickname": meta.get("nickname") or "Turing",
            "role": role,
            "status": status,
            "summary": final_reply,
            "diff_stats": diff_stats,
        }

    return {
        "id": str(child_row.get("id") or ""),
        "parent_task_id": str(child_row.get("parent_task_id") or meta.get("parent_task_id") or ""),
        "nickname": str(meta.get("nickname") or "Turing"),
        "task_name": str(meta.get("task_name") or "subtask"),
        "agent_path": str(meta.get("agent_path") or "/root/subtask"),
        "role": role,
        "fork_turns": str(meta.get("fork_turns") or "none"),
        "write_scope": list(meta.get("write_scope") or []),
        "instructions": str(meta.get("instructions") or child_row.get("title") or ""),
        "status": status,
        "is_waiting": is_waiting,
        "closed": bool(meta.get("closed")),
        "model_id": str(child_row.get("model_id") or ""),
        "reasoning_effort": str(child_row.get("reasoning_effort") or meta.get("display_effort") or "medium"),
        "steps": steps,
        "step_index": step_index,
        "estimated_total_steps": est_total,
        "step_progress_label": step_progress_label,
        "elapsed_seconds": round(elapsed, 1),
        "progress_percent": progress,
        "current_step_text": current_step_text,
        "conclusion_preview": conclusion_preview,
        "completed_at_ms": int(settled_ms) if isinstance(settled_ms, (int, float)) else None,
        "diff_stats": diff_stats,
        "timeline": timeline,
        "final_reply": final_reply,
        "notification_payload": notif,
        "created_at": child_row.get("created_at"),
        "updated_at": child_row.get("updated_at"),
    }


def project_subagents_for_parent(data: dict, parent_row: dict) -> list[dict]:
    tree_children = _collect_tree_subagents(data, parent_row)
    if not tree_children:
        return []
    return [project_subagent_summary(child) for child in tree_children]


def on_subagent_settled_in_tx(data: dict, child_row: dict) -> dict | None:
    """Record a <subagent_notification> on the child and push it to the parent task when a subagent turn ends."""
    parent_id = child_row.get("parent_task_id")
    if not parent_id:
        return None
    meta = child_row.setdefault("subagent_meta", {})
    status = str(child_row.get("status") or "")
    if status not in {"completed", "failed", "interrupted", "cancelled", "paused"}:
        return None
    event_seq = int(child_row.get("event_cursor") or child_row.get("event_seq") or 0)
    if meta.get("notified_event_seq") == event_seq and meta.get("notified_status") == status:
        return meta.get("last_notification")

    meta["settled_at_ms"] = now_ms()
    meta["notified_event_seq"] = event_seq
    meta["notified_status"] = status
    diff_stats, _, _ = compute_subagent_diff_and_timeline(child_row)
    summary_text = str(child_row.get("result") or "").strip()
    if not summary_text:
        if child_row.get("error"):
            summary_text = f"Subagent ended with status={status}, error={child_row.get('error')}"
        else:
            for msg in reversed(child_row.get("messages") or []):
                if isinstance(msg, dict) and msg.get("role") == "assistant" and str(msg.get("content") or "").strip():
                    summary_text = str(msg.get("content")).strip()
                    break
    notif = {
        "id": f"subnotif_{uuid.uuid4().hex[:12]}",
        "agent_id": str(child_row.get("id") or ""),
        "agent_path": str(meta.get("agent_path") or f"/root/{meta.get('task_name') or 'subagent'}"),
        "nickname": str(meta.get("nickname") or "Turing"),
        "role": str(meta.get("role") or "default"),
        "status": status,
        "summary": summary_text[:4000],
        "diff_stats": diff_stats,
        "created_at": now_iso(),
        "delivered": False,
    }
    meta["last_notification"] = notif
    parent_row = (data.get("tasks") or {}).get(parent_id)
    if isinstance(parent_row, dict):
        queue = parent_row.setdefault("subagent_notifications", [])
        queue.append(copy.deepcopy(notif))
        event(
            parent_row,
            "subagent_settled",
            subagent_id=child_row.get("id"),
            nickname=meta.get("nickname"),
            agent_path=meta.get("agent_path"),
            status=status,
        )
    return notif


def cascade_stop_subagents_in_tx(data: dict, parent_row: dict, *, reason: str = "parent_stopped", close: bool = False) -> list[str]:
    """Stop/interrupt all active subagents belonging to parent_row (recursively)."""
    tasks = data.get("tasks") or {}
    stopped_ids: list[str] = []
    now = now_iso()

    def _stop_tree(row: dict):
        for cid in list(row.get("subagent_ids") or []):
            child = tasks.get(cid)
            if not isinstance(child, dict):
                continue
            meta = child.setdefault("subagent_meta", {})
            if close:
                meta["closed"] = True
            target_statuses = (
                {"queued", "running", "waiting_approval", "waiting_input", "pausing", "paused"}
                if close
                else {"queued", "running", "waiting_approval", "waiting_input", "pausing"}
            )
            if child.get("status") in target_statuses:
                child["pause_requested"] = True
                child["status"] = "cancelled" if close else "paused"
                child["in_flight"] = None
                child["pending_approval"] = None
                child["pending_input"] = None
                child["updated_at"] = now
                meta["settled_at_ms"] = now_ms()
                stopped_ids.append(cid)
                on_subagent_settled_in_tx(data, child)
            _stop_tree(child)

    _stop_tree(parent_row)
    return stopped_ids
