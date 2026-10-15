"""Host-authored dispatch policy, independent of model plans and tool effects.

An effect describes what must be journalled for crash recovery. It does not by
itself imply a human approval. Workspace mode grants workstation execution under
the user's OS account; it is deliberately NOT described as an OS sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass

MODES = frozenset({"workspace", "read_only", "confirm_changes", "automatic"})
WORKSPACE_TOOLS = frozenset({
    "ryn_file_write", "ryn_file_edit", "ryn_file_manage", "ryn_apply_patch", "ryn_shell_execute",
    "ryn_process_stop", "ryn_process_write", "ryn_browser_open",
    "ryn_browser_action", "ryn_computer_action",
})


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


def decide(call, metadata, row, settings):
    modes = {row["permission_mode"], settings["permission_mode"]}
    if not modes <= MODES:
        return Decision("deny", "invalid_policy")
    # Only trusted metadata or this host's literal-command recognizer can mark
    # a call read-only. Model arguments cannot self-declare their risk.
    read = metadata.get("effect") == "read" or (
        call["name"] == "ryn_shell_execute" and shell_read(call["arguments"]))
    if read and not metadata.get("requires_explicit_approval"):
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
