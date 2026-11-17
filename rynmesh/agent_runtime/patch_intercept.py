"""Recognize a single shell patch invocation without executing shell syntax."""
import re
from pathlib import Path

from .store import AgentToolRejected

CORRECTION = "Call ryn_apply_patch with the patch text instead of running apply_patch in a shell."
_NAME = r"apply_?patch"


def reject_shell_patch():
    raise AgentToolRejected("agent_shell_patch_use_patch_tool", correction=CORRECTION)


def extract_patch(command):
    """Return patch text and optional leading cd, or None for compound commands."""
    command = command.strip().replace("\r\n", "\n")
    cwd = None
    cd = re.match(r'''^cd\s+(?:'([^']+)'|"([^"]+)"|([^\s;&|]+))\s*&&\s*''', command)
    if cd:
        cwd = next(value for value in cd.groups() if value is not None)
        command = command[cd.end():]
    heredoc = re.match(_NAME + r'''\s+<<\s*(['"]?)(\w+)\1[^\S\n]*\n''', command)
    if heredoc:
        closing = re.search(r"^" + re.escape(heredoc[2]) + r"(?=\s|[;&|]|$)", command[heredoc.end():], re.M)
        if closing is None:
            reject_shell_patch()
        body = command[heredoc.end():]
        if body[closing.end():].strip():
            return None
        patch = body[:closing.start()].rstrip("\n")
    elif command.startswith(("@'\n", '@"\n')):
        quote = command[1]
        closing = re.search(r"^" + quote + r"@", command[3:], re.M)
        if closing is None:
            if "*** Begin Patch" in command:
                reject_shell_patch()
            return None
        body = command[3:]
        if not re.fullmatch(r"\s*\|\s*" + _NAME, body[closing.end():]):
            return None
        patch = body[:closing.start()].rstrip("\n")
    else:
        quoted = re.match(_NAME + r'''\s+(['"])''', command)
        if quoted:
            body = command[quoted.end():]
            end = body.find(quoted[1])
            if end >= 0:
                if body[end + 1:].strip():
                    return None
                patch = body[:end]
            else:
                reject_shell_patch()
        elif re.match(_NAME + r"(?:\s|$)", command) and "*** Begin Patch" in command:
            reject_shell_patch()
        else:
            return None
    if not patch.startswith("*** Begin Patch\n") or not patch.rstrip().endswith("*** End Patch"):
        reject_shell_patch()
    return {"patch": patch, "cwd": cwd}


def patch_arguments(extracted, arguments, workspace_root):
    """Express shell-relative patch paths relative to the workspace patch tool."""
    root = Path(workspace_root).resolve()
    cwd = root / Path(arguments.get("cwd") or ".").expanduser()
    if extracted["cwd"]:
        cwd /= Path(extracted["cwd"]).expanduser()
    cwd = cwd.resolve()
    if not cwd.is_dir():
        raise AgentToolRejected("agent_local_cwd_not_directory")

    def prefix(match):
        path = Path(match[2])
        if path.is_absolute():
            return match[0]
        target = cwd / path
        try:
            target = target.relative_to(root)
        except ValueError:
            pass  # The normal patch policy validates absolute out-of-workspace paths.
        return match[1] + target.as_posix()

    return {"patch": re.sub(r"^(\*\*\* (?:Add File|Update File|Delete File|Move to): )(.+)$",
                            prefix, extracted["patch"], flags=re.M)}
