"""Host-authored dispatch policy, independent of model plans and tool effects.

An effect describes what must be journalled for crash recovery. It does not by
itself imply a human approval. Workspace mode grants workstation execution under
the user's OS account; it is deliberately NOT described as an OS sandbox.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .store import encoded

MODES = frozenset({"workspace", "read_only", "confirm_changes", "automatic"})
WORKSPACE_TOOLS = frozenset({
    "ryn_file_write", "ryn_file_edit", "ryn_file_manage", "ryn_apply_patch", "ryn_shell_execute",
    "ryn_process_stop", "ryn_process_write", "ryn_browser_open",
    "ryn_browser_action", "ryn_computer_action", "ryn_craft_action",
})
SHELL_ARGUMENT_KEYS = frozenset({"command", "shell", "cwd", "timeout_seconds", "background"})


@dataclass(frozen=True)
class Decision:
    action: str
    basis: str


def literal_command(command, shell):
    """Recognize one literal invocation; reject shell syntax and interpolation.

    This narrow compatibility rule handles ordinary reads in strict legacy
    sessions. It is not a general command safety classifier or a sandbox.
    """
    if not isinstance(command, str) or shell not in {"powershell", "sh"}:
        return None
    words, word, quote = [], [], None
    index = 0
    while index < len(command):
        char = command[index]
        if char in "\r\n\x00":
            return None
        if quote:
            if char == quote:
                if shell == "powershell" and quote == "'" and command[index:index + 2] == "''":
                    word.append("'")
                    index += 2
                    continue
                quote = None
            elif quote == '"' and char in "$`\\":
                return None
            else:
                word.append(char)
        elif char in "'\"":
            quote = char
        elif char in ";&|<>`$(){}#":
            return None
        elif char == "\\" and shell == "sh":
            return None
        elif char.isspace():
            if word:
                words.append("".join(word))
                word = []
        else:
            word.append(char)
        index += 1
    if quote:
        return None
    if word:
        words.append("".join(word))
    return words or None


def shell_read(arguments):
    if not isinstance(arguments, dict) or set(arguments) - SHELL_ARGUMENT_KEYS:
        return False
    if arguments.get("background"):
        return False
    # The actual executor uses this same platform default.
    import os
    shell = arguments.get("shell", "powershell" if os.name == "nt" else "sh")
    words = literal_command(arguments.get("command"), shell)
    if not words:
        return False
    program = words[0].lower()
    if program in {"rg", "rg.exe"}:
        # These ripgrep flags invoke external programs. Do not inspect the
        # pattern after -- as flags, but conservatively reject it if ambiguous.
        return not any(word.lower().startswith(("--pre", "--hostname-bin")) for word in words[1:])
    if shell == "powershell":
        return program in {"get-content", "get-childitem", "get-item", "get-location", "test-path", "select-string"}
    return program in {"pwd", "cat", "head", "tail", "wc", "ls"}


def classify_operation(name, arguments, metadata, *, operation_id="", task_id="", workspace_root="",
                       goal_revision=None, scope_revision=None, context_epoch=None, control_revision=None):
    """Host-authored operation classification bound to tool, arguments and task context.

    Never accepts self-declared effect/authorization fields from model arguments or
    untrusted records.
    """
    arguments_sha256 = hashlib.sha256(encoded(arguments)).hexdigest() if isinstance(arguments, dict) else ""
    effect_class = "write"
    basis = "write"
    if isinstance(name, str) and isinstance(arguments, dict) and isinstance(metadata, dict):
        if metadata.get("requires_explicit_approval"):
            effect_class, basis = "write", "explicit_approval"
        elif metadata.get("effect") == "read" and name not in WORKSPACE_TOOLS:
            effect_class, basis = "read", "static_read"
        elif name == "ryn_shell_execute" and shell_read(arguments):
            effect_class, basis = "read", "literal_shell_read"
    return {
        "version": 1,
        "effect_class": effect_class,
        "basis": basis,
        "operation_id": str(operation_id or ""),
        "task_id": str(task_id or ""),
        "tool": str(name or ""),
        "arguments_sha256": arguments_sha256,
        "workspace_root": str(workspace_root or ""),
        "goal_revision": goal_revision,
        "scope_revision": scope_revision,
        "context_epoch": context_epoch,
        "control_revision": control_revision,
    }


def verified_read_classification(record, *, row=None, task_id=None, workspace_root=None,
                                 operation_id=None, tool=None, arguments=None,
                                 require_current_revisions=False) -> bool:
    """Verify that a stored flight/receipt/ledger record carries an authentic host read classification.

    Historical write records lacking operation_classification fail closed (treated as writes).
    """
    if not isinstance(record, dict):
        return False
    cls = record.get("operation_classification")
    tool_name = tool if tool is not None else (record.get("name") or record.get("tool"))
    if cls is None:
        return bool(record.get("effect") == "read" and isinstance(tool_name, str)
                    and tool_name and tool_name not in WORKSPACE_TOOLS)
    if (not isinstance(cls, dict) or cls.get("version") != 1
            or cls.get("effect_class") != "read"
            or cls.get("basis") not in {"static_read", "literal_shell_read"}):
        return False
    if not isinstance(tool_name, str) or not tool_name or cls.get("tool") != tool_name:
        return False
    actual_args = arguments if arguments is not None else record.get("arguments")
    if actual_args is not None:
        if not isinstance(actual_args, dict):
            return False
        actual_sha = hashlib.sha256(encoded(actual_args)).hexdigest()
        if cls.get("arguments_sha256") != actual_sha:
            return False
        if record.get("arguments_sha256") is not None and record.get("arguments_sha256") != actual_sha:
            return False
        if cls.get("basis") == "literal_shell_read":
            if tool_name != "ryn_shell_execute" or not shell_read(actual_args):
                return False
            if record.get("authorization") is not None and record.get("authorization") != "read":
                return False
        else:
            if tool_name in WORKSPACE_TOOLS or record.get("effect") not in {None, "read"}:
                return False
    else:
        # Without arguments to re-verify literal_shell_read, only a binding with matching sha is allowed
        binding = record.get("binding") if isinstance(record.get("binding"), dict) else {}
        bound_sha = record.get("arguments_sha256") or binding.get("arguments_sha256")
        if not isinstance(bound_sha, str) or not bound_sha or cls.get("arguments_sha256") != bound_sha:
            return False
        if cls.get("basis") == "literal_shell_read" and tool_name != "ryn_shell_execute":
            return False
        if cls.get("basis") == "static_read" and tool_name in WORKSPACE_TOOLS:
            return False
    binding = record.get("binding") if isinstance(record.get("binding"), dict) else {}
    expected_op = operation_id if operation_id is not None else (record.get("operation_id") or binding.get("operation_id"))
    if not isinstance(expected_op, str) or not expected_op or cls.get("operation_id") != expected_op:
        return False
    expected_task = task_id if task_id is not None else (
        (row.get("id") if isinstance(row, dict) else None) or record.get("task_id") or binding.get("task_id"))
    if expected_task is not None or cls.get("task_id"):
        if not isinstance(expected_task, str) or not expected_task or cls.get("task_id") != expected_task:
            return False
    expected_ws = workspace_root if workspace_root is not None else (
        (row.get("local_execution") or {}).get("workspace_root", "") if isinstance(row, dict) else
        (binding.get("local_execution") or {}).get("workspace_root") if isinstance(binding.get("local_execution"), dict) else None)
    if expected_ws is not None and cls.get("workspace_root", "") != expected_ws:
        return False
    for rev_key in ("goal_revision", "scope_revision", "context_epoch", "control_revision"):
        if record.get(rev_key) is not None and cls.get(rev_key) is not None and cls.get(rev_key) != record.get(rev_key):
            return False
    if isinstance(row, dict):
        if record.get("goal_revision") is not None and cls.get("goal_revision") != record.get("goal_revision"):
            return False
        if require_current_revisions:
            if (cls.get("goal_revision") != (row.get("goal") or {}).get("revision")
                    or cls.get("scope_revision") != row.get("scope_revision")
                    or cls.get("context_epoch") != row.get("context_epoch", 0)
                    or cls.get("control_revision") != row.get("control_revision")):
                return False
    return True


def decide(call, metadata, row, settings):
    modes = {row["permission_mode"], settings["permission_mode"]}
    if not modes <= MODES:
        return Decision("deny", "invalid_policy")
    # Only trusted metadata or this host's literal-command recognizer can mark
    # a call read-only. Model arguments cannot self-declare their risk.
    classification = classify_operation(call.get("name"), call.get("arguments"), metadata)
    if classification["effect_class"] == "read":
        return Decision("execute", "read")
    if "read_only" in modes:
        return Decision("deny", "read_only")
    if row.get("approved"):
        return Decision("execute", "approved")
    if metadata.get("requires_explicit_approval"):
        return Decision("ask", "explicit_approval")
    if "confirm_changes" in modes:
        return Decision("ask", "confirm_changes")
    for policy in (row, settings):
        if policy["permission_mode"] == "workspace":
            if call["name"] not in WORKSPACE_TOOLS:
                return Decision("ask", "external_change")
        elif policy["permission_mode"] == "automatic":
            if call["name"] not in policy.get("allowed_write_tools", []):
                return Decision("ask", "tool_not_granted")
    return Decision("execute", "automatic")
