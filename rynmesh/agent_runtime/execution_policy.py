"""Host-authored dispatch policy, independent of model plans and tool effects.

An effect describes what must be journalled for crash recovery. It does not by
itself imply a human approval. Workspace mode grants workstation execution under
the user's OS account; it is deliberately NOT described as an OS sandbox.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .store import encoded

MODES = frozenset({"workspace", "read_only", "confirm_changes", "automatic"})
WORKSPACE_TOOLS = frozenset({
    "ryn_file_write", "ryn_file_edit", "ryn_file_manage", "ryn_apply_patch", "ryn_shell_execute",
    "ryn_process_stop", "ryn_process_write", "ryn_browser_open", "ryn_table_export",
    "ryn_browser_action", "ryn_computer_action", "ryn_craft_action", "ryn_download_file", "ryn_image_generate",
})
SHELL_ARGUMENT_KEYS = frozenset({"command", "shell", "cwd", "timeout_seconds", "background", "yield_seconds", "login", "stdin"})
GIT_SAFE_SUBCOMMANDS = frozenset({"status", "diff", "log", "show", "rev-parse", "merge-base"})
GIT_UNSAFE_FLAGS = (
    "--output",
    "-o",
    "--ext-diff",
    "--textconv",
    "--exec",
    "-c",
    "--config-env",
    "--git-dir",
    "--work-tree",
)


@dataclass(frozen=True)
class Decision:
    action: str
    basis: str


# What the model should do after a refusal. These ride on the refused tool
# result only, so a task that is never refused (for example in automatic
# mode, where nothing asks) pays nothing for them. Attaching the next-step
# rule to the rejection itself follows openai/codex rust-v0.167.0
# (ext/guardian-reviewer/src/completion.rs, Apache-2.0); the text is Ryn's.
# The refusal stays in the history after the owner speaks again, and a later
# owner message does reopen the call (loop.py matches the denial by goal
# revision), so the rule must not read as permanent. A subagent's reply goes
# to its parent, so both texts say "in your reply" rather than "tell the owner".
OWNER_DENIED_CORRECTION = (
    "The owner declined this operation and nothing ran. Unless a later owner message asks for it, do not retry "
    "it or get the same effect another way (a different tool, a shell command, a script or the browser). "
    "Finish the parts of the task that do not depend on it, then say in your reply which action was declined "
    "and what is still waiting on the owner.")
PERMISSION_DENIED_CORRECTION = (
    "Current host permissions do not allow this operation and nothing ran. Do not work around the setting "
    "with another tool. Continue with what the permissions allow and say in your reply which setting blocks the rest.")


POWERSHELL_NON_ASCII_SYNTAX_CHARS = frozenset({
    "\u2018", "\u2019", "\u201a", "\u201b",
    "\u201c", "\u201d", "\u201e", "\u201f",
    "\u2013", "\u2014", "\u2015",
})

POSIX_ANY_ARG_READ_PROGRAMS = frozenset({
    "pwd", "cat", "head", "tail", "wc", "ls",
    "grep", "egrep", "fgrep",
    "cut", "stat", "file", "du", "tree", "which", "basename", "dirname", "realpath",
})
POSIX_PIPE_FILTER_PROGRAMS = frozenset({
    "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "sort", "uniq", "cut",
})
FIND_UNSAFE_FLAGS = frozenset({
    "-exec", "-execdir", "-ok", "-okdir", "-delete",
    "-fprint", "-fprint0", "-fprintf", "-fls",
})
SED_SCRIPT_RE = re.compile(r"^[0-9,$]*p(;[0-9,$]*p)*$")
UNIQ_VALUE_FLAGS = frozenset({"-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars"})

POWERSHELL_HEAD_READ_CMDLETS = frozenset({
    "get-content", "get-childitem", "get-item", "get-location", "test-path", "select-string",
    "get-filehash", "resolve-path", "split-path",
})
POWERSHELL_PIPE_FILTER_CMDLETS = frozenset({
    "select-string", "select-object", "measure-object", "sort-object", "where-object",
})


def executable_key(word: str) -> str:
    """Normalize an executable token to its bare command key."""
    if not isinstance(word, str) or not word:
        return ""
    name = word.replace("\\", "/").rsplit("/", 1)[-1]
    if os.name == "nt":
        name = name.lower()
        for suffix in (".exe", ".cmd", ".bat", ".com"):
            if len(name) > len(suffix) and name.endswith(suffix):
                name = name[:-len(suffix)]
                break
    return name


def literal_command(command, shell):
    """Recognize one literal invocation; reject shell syntax and interpolation.

    This narrow compatibility rule handles ordinary reads in strict legacy
    sessions. It is not a general command safety classifier or a sandbox.
    """
    if not isinstance(command, str) or shell not in {"powershell", "sh"}:
        return None
    if shell == "powershell" and any(ch in POWERSHELL_NON_ASCII_SYNTAX_CHARS for ch in command):
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
            elif quote == '"' and char in ("$`" if shell == "powershell" else "$`\\"):
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


def _scan_literal_segments(command, shell) -> list[tuple[str | None, list[str]]] | None:
    """Scan literal segments separated by unquoted `|`, `;`, `&&`, or `||`."""
    if not isinstance(command, str) or shell not in {"powershell", "sh"}:
        return None
    if shell == "powershell" and any(ch in POWERSHELL_NON_ASCII_SYNTAX_CHARS for ch in command):
        return None
    segments: list[tuple[str | None, list[str]]] = []
    pending_op: str | None = None
    words: list[str] = []
    word: list[str] = []
    has_token = False
    quote: str | None = None
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
            elif quote == '"' and char in ("$`" if shell == "powershell" else "$`\\"):
                return None
            else:
                word.append(char)
        elif char in "'\"":
            quote = char
            has_token = True
        elif char in "<>`$(){}#":
            return None
        elif char == "\\" and shell == "sh":
            return None
        elif char == ";":
            if word or has_token:
                words.append("".join(word))
                word, has_token = [], False
            if not words:
                return None
            segments.append((pending_op, words))
            words = []
            pending_op = ";"
        elif char == "|":
            if word or has_token:
                words.append("".join(word))
                word, has_token = [], False
            if not words:
                return None
            segments.append((pending_op, words))
            words = []
            if command[index:index + 2] == "||":
                pending_op = "||"
                index += 2
                continue
            pending_op = "|"
        elif char == "&":
            if command[index:index + 2] != "&&":
                return None
            if word or has_token:
                words.append("".join(word))
                word, has_token = [], False
            if not words:
                return None
            segments.append((pending_op, words))
            words = []
            pending_op = "&&"
            index += 2
            continue
        elif char.isspace():
            if word or has_token:
                words.append("".join(word))
                word, has_token = [], False
        else:
            word.append(char)
        index += 1
    if quote:
        return None
    if word or has_token:
        words.append("".join(word))
    if not words:
        return None
    segments.append((pending_op, words))
    return segments


def literal_segments(command, shell) -> list[list[str]] | None:
    """Split a literal command into per-segment word lists across `|`, `;`, `&&`, `||`."""
    parsed = _scan_literal_segments(command, shell)
    return [seg_words for _, seg_words in parsed] if parsed is not None else None


def _rg_read_ok(words: list[str]) -> bool:
    for word in words[1:]:
        low = word.lower()
        if low.startswith(("--pre", "--hostname-bin", "--search-zip")):
            return False
        if word.startswith("-") and not word.startswith("--") and len(word) > 1 and "z" in word[1:]:
            return False
    return True


def _git_read_ok(words: list[str]) -> bool:
    if len(words) < 2 or words[1].lower() not in GIT_SAFE_SUBCOMMANDS:
        return False
    return not any(word.lower().startswith(GIT_UNSAFE_FLAGS) for word in words[2:])


def _sed_read_ok(words: list[str]) -> bool:
    has_n = False
    scripts: list[str] = []
    positional: list[str] = []
    after_dd = False
    idx = 1
    while idx < len(words):
        arg = words[idx]
        if after_dd:
            positional.append(arg)
            idx += 1
            continue
        if arg == "--":
            after_dd = True
            idx += 1
            continue
        if arg in {"--quiet", "--silent"}:
            has_n = True
            idx += 1
            continue
        if arg.startswith(("--in-place", "--file")):
            return False
        if arg == "--expression":
            if idx + 1 >= len(words):
                return False
            scripts.append(words[idx + 1])
            idx += 2
            continue
        if arg.startswith("--expression="):
            scripts.append(arg.split("=", 1)[1])
            idx += 1
            continue
        if arg in {"--regexp-extended", "--posix", "--unbuffered", "--null-data", "--separate"}:
            idx += 1
            continue
        if arg.startswith("--"):
            return False
        if arg.startswith("-") and len(arg) > 1:
            shorts = arg[1:]
            if "i" in shorts or "f" in shorts:
                return False
            pos = 0
            while pos < len(shorts):
                ch = shorts[pos]
                if ch == "n":
                    has_n = True
                    pos += 1
                elif ch == "e":
                    if pos + 1 < len(shorts):
                        scripts.append(shorts[pos + 1:])
                        pos = len(shorts)
                    else:
                        if idx + 1 >= len(words):
                            return False
                        scripts.append(words[idx + 1])
                        idx += 1
                        pos += 1
                elif ch in "Ersuz":
                    pos += 1
                else:
                    return False
            idx += 1
            continue
        positional.append(arg)
        idx += 1
    if not has_n:
        return False
    if not scripts:
        if not positional:
            return False
        scripts.append(positional[0])
    return bool(scripts) and all(bool(SED_SCRIPT_RE.fullmatch(s)) for s in scripts)


def _uniq_read_ok(words: list[str]) -> bool:
    positional = 0
    after_dd = False
    idx = 1
    while idx < len(words):
        arg = words[idx]
        if after_dd:
            positional += 1
            idx += 1
            continue
        if arg == "--":
            after_dd = True
            idx += 1
            continue
        if arg in UNIQ_VALUE_FLAGS:
            idx += 2
            continue
        if arg.startswith("-") and len(arg) > 1:
            idx += 1
            continue
        positional += 1
        idx += 1
    return positional <= 1


def _posix_read_ok(words: list[str]) -> bool:
    prog = executable_key(words[0])
    if prog in POSIX_ANY_ARG_READ_PROGRAMS:
        # `tree -o FILE` and `file -C` write files.
        if prog == "tree":
            return not any(arg.startswith("-o") for arg in words[1:])
        if prog == "file":
            return not any(arg in {"-C", "--compile"} for arg in words[1:])
        return True
    if prog == "find":
        return not any(arg in FIND_UNSAFE_FLAGS for arg in words[1:])
    if prog == "sed":
        return _sed_read_ok(words)
    if prog == "sort":
        for arg in words[1:]:
            if arg.startswith(("--output", "--compress-program")):
                return False
            if arg.startswith("-") and not arg.startswith("--") and "o" in arg[1:]:
                return False
        return True
    if prog == "uniq":
        return _uniq_read_ok(words)
    return False


def _powershell_read_ok(words: list[str], *, in_pipe: bool = False) -> bool:
    prog = executable_key(words[0]).lower()
    if in_pipe:
        return prog in POWERSHELL_PIPE_FILTER_CMDLETS
    return prog in POWERSHELL_HEAD_READ_CMDLETS


def _segment_read_ok(words: list[str], shell: str, *, in_pipe: bool = False) -> bool:
    prog = executable_key(words[0])
    prog_low = prog.lower()
    if shell == "sh":
        if in_pipe and prog not in POSIX_PIPE_FILTER_PROGRAMS:
            return False
        if prog == "rg":
            return _rg_read_ok(words)
        if prog == "git":
            return _git_read_ok(words)
        return _posix_read_ok(words)
    if in_pipe:
        return _powershell_read_ok(words, in_pipe=True)
    if prog_low in {"rg", "rg.exe"}:
        return _rg_read_ok(words)
    if prog_low in {"git", "git.exe"}:
        return _git_read_ok(words)
    return _powershell_read_ok(words, in_pipe=False)


def _has_parent_segment(value: str) -> bool:
    return ".." in re.split(r"[\\/]", value)


def _expand_tilde(value: str) -> str:
    return os.path.expanduser(value) if (value == "~" or value.startswith(("~/", "~\\"))) else value


def _is_abs_path(value: str, shell: str) -> bool:
    expanded = _expand_tilde(value)
    return Path(expanded).is_absolute() or (shell == "sh" and expanded.startswith("/"))


def _within_workspace(arguments: dict, segments: list[list[str]], shell: str, workspace_root: str | os.PathLike | None) -> bool:
    raw_cwd = arguments.get("cwd")
    if raw_cwd is not None and (not isinstance(raw_cwd, str) or not raw_cwd or "\x00" in raw_cwd):
        return False
    ws_str = str(workspace_root) if workspace_root is not None else ""
    if ws_str:
        try:
            root = Path(ws_str).resolve()
            if raw_cwd:
                exp_cwd = _expand_tilde(raw_cwd)
                base = (Path(exp_cwd) if Path(exp_cwd).is_absolute() else root / exp_cwd).resolve()
                if not base.is_relative_to(root):
                    return False
            else:
                base = root
            for words in segments:
                for arg in words[1:]:
                    candidates = [arg] if not arg.startswith("-") else ([arg.split("=", 1)[1]] if "=" in arg else [])
                    for cand in candidates:
                        if not cand:
                            continue
                        if _is_abs_path(cand, shell) or _has_parent_segment(cand):
                            exp = _expand_tilde(cand)
                            resolved = (Path(exp) if Path(exp).is_absolute() else base / exp).resolve()
                            if not resolved.is_relative_to(root):
                                return False
        except (OSError, ValueError):
            return False
        return True

    if raw_cwd and _has_parent_segment(raw_cwd) and not _is_abs_path(raw_cwd, shell):
        norm_cwd = os.path.normpath(raw_cwd)
        if norm_cwd == ".." or norm_cwd.startswith(("../", "..\\")):
            return False
    rel_base = raw_cwd if (raw_cwd and not _is_abs_path(raw_cwd, shell)) else "."
    for words in segments:
        for arg in words[1:]:
            candidates = [arg] if not arg.startswith("-") else ([arg.split("=", 1)[1]] if "=" in arg else [])
            for cand in candidates:
                if cand and _has_parent_segment(cand) and not _is_abs_path(cand, shell):
                    norm = os.path.normpath(os.path.join(rel_base, cand))
                    if norm == ".." or norm.startswith(("../", "..\\")):
                        return False
    return True


def shell_read(arguments, workspace_root=None):
    if not isinstance(arguments, dict) or set(arguments) - SHELL_ARGUMENT_KEYS:
        return False
    if "login" in arguments and type(arguments["login"]) is not bool:
        return False
    if "stdin" in arguments and arguments["stdin"] != "closed":
        return False
    if arguments.get("background") or arguments.get("yield_seconds") is not None:
        return False
    # The actual executor uses this same platform default.
    shell = arguments.get("shell", "powershell" if os.name == "nt" else "sh")
    command = arguments.get("command")
    single = literal_command(command, shell)
    if single is not None:
        if not _segment_read_ok(single, shell, in_pipe=False):
            return False
        return _within_workspace(arguments, [single], shell, workspace_root)
    parsed = _scan_literal_segments(command, shell)
    if not parsed:
        return False
    for op, words in parsed:
        if not _segment_read_ok(words, shell, in_pipe=(op == "|")):
            return False
    return _within_workspace(arguments, [w for _, w in parsed], shell, workspace_root)


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
        elif name == "ryn_shell_execute" and shell_read(arguments, workspace_root=workspace_root):
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
    binding = record.get("binding") if isinstance(record.get("binding"), dict) else {}
    expected_ws = workspace_root if workspace_root is not None else (
        (row.get("local_execution") or {}).get("workspace_root", "") if isinstance(row, dict) else
        (binding.get("local_execution") or {}).get("workspace_root") if isinstance(binding.get("local_execution"), dict) else None)
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
            record_ws = cls.get("workspace_root") or expected_ws or ""
            if tool_name != "ryn_shell_execute" or not shell_read(actual_args, workspace_root=record_ws):
                return False
            if record.get("authorization") is not None and record.get("authorization") != "read":
                return False
        else:
            if tool_name in WORKSPACE_TOOLS or record.get("effect") not in {None, "read"}:
                return False
    else:
        # Without arguments to re-verify literal_shell_read, only a binding with matching sha is allowed
        bound_sha = record.get("arguments_sha256") or binding.get("arguments_sha256")
        if not isinstance(bound_sha, str) or not bound_sha or cls.get("arguments_sha256") != bound_sha:
            return False
        if cls.get("basis") == "literal_shell_read" and tool_name != "ryn_shell_execute":
            return False
        if cls.get("basis") == "static_read" and tool_name in WORKSPACE_TOOLS:
            return False
    expected_op = operation_id if operation_id is not None else (record.get("operation_id") or binding.get("operation_id"))
    if not isinstance(expected_op, str) or not expected_op or cls.get("operation_id") != expected_op:
        return False
    expected_task = task_id if task_id is not None else (
        (row.get("id") if isinstance(row, dict) else None) or record.get("task_id") or binding.get("task_id"))
    if expected_task is not None or cls.get("task_id"):
        if not isinstance(expected_task, str) or not expected_task or cls.get("task_id") != expected_task:
            return False
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


def is_full_automatic(row: dict, settings: dict) -> bool:
    return isinstance(row, dict) and row.get("permission_mode") == "automatic"


def decide(call, metadata, row, settings):
    mode = row["permission_mode"]
    if mode not in MODES:
        return Decision("deny", "invalid_policy")
    # Only trusted metadata or this host's literal-command recognizer can mark
    # a call read-only. Model arguments cannot self-declare their risk.
    ws_root = ((row.get("local_execution") or {}).get("workspace_root", "")
               if isinstance(row, dict) and isinstance(row.get("local_execution"), dict) else "")
    classification = classify_operation(call.get("name"), call.get("arguments"), metadata, workspace_root=ws_root)
    if classification["effect_class"] == "read":
        return Decision("execute", "read")
    if mode == "read_only" or (row.get("automation_run_id") and row.get("automation_kind") != "heartbeat"
                               and settings.get("permission_mode") == "read_only"):
        return Decision("deny", "read_only")
    if row.get("approved"):
        return Decision("execute", "approved")
    if metadata.get("requires_explicit_approval"):
        return Decision("ask", "explicit_approval")
    if mode == "confirm_changes":
        return Decision("ask", "confirm_changes")
    # This alias is authored by the host only after verifying the builtin
    # provider, schema and enablement. Never derive it from a model tool name.
    workstation_tool = metadata.get("workstation_permission_tool")
    desktop_granted = (row.get("local_execution") or {}).get("desktop_access") is True
    inherited_desktop = (workstation_tool == "ryn_computer_action"
                         and metadata.get("computer_access") is True
                         and desktop_granted)
    if mode == "workspace":
        from .dangerous_commands import dangerous_shell
        if call["name"] == "ryn_shell_execute" and dangerous_shell(call.get("arguments") or {}):
            return Decision("ask", "dangerous_command")
        if call["name"] not in WORKSPACE_TOOLS and not inherited_desktop:
            return Decision("ask", "external_change")
    elif mode == "automatic":
        if metadata.get("computer_access") and not desktop_granted:
            return Decision("ask", "desktop_access")
    return Decision("execute", "automatic")
