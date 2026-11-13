"""Bound managed file-read retries; reject recognizable failed-read shell loops.

This is a reliability check, not a shell sandbox or a general program verifier.
Arbitrary programs remain governed by the existing process lifetime/cancellation.
"""
from __future__ import annotations

import ntpath
import re
from pathlib import Path

from .store import AgentToolRejected

FILE_READS = {"ryn_file_read", "ryn_file_wait"}
LIMIT = 3
MISSING = {"agent_local_file_missing_or_too_large", "agent_local_file_missing", "agent_file_retry_exhausted"}
_LOOP = re.compile(r"while\s*\(\s*\$(?:true|1)\s*\)|for\s*\(\s*;\s*;\s*\)|while\s+True\s*:|while\s+(?:true|:)\s*;", re.I)
_READ = re.compile(r"\b(?:ReadAllText|ReadAllBytes|Get-Content|read_text|read_bytes|open|cat)\b", re.I)


def file_target(row, call):
    if call.get("name", call.get("tool")) not in FILE_READS:
        return None
    value = (call.get("arguments") or {}).get("path")
    root = (row.get("local_execution") or {}).get("workspace_root", "")
    if not isinstance(value, str) or not value:
        return None
    if re.match(r"^[a-zA-Z]:[\\/]", value) or re.match(r"^[a-zA-Z]:[\\/]", root):
        return ntpath.normcase(ntpath.normpath(ntpath.join(root, value)))
    return str((Path(root) / value).resolve())


def failure_count(row, target):
    count = 0
    for receipt in reversed(row.get("receipts", [])):
        if (file_target(row, receipt) != target or receipt.get("goal_revision") != row["goal"]["revision"]
                or receipt.get("scope_revision") != row["scope_revision"]
                or receipt.get("context_epoch", 0) != row.get("context_epoch", 0)):
            continue
        result = receipt.get("result") or {}
        if receipt.get("status") == "confirmed" and not result.get("error"):
            break
        if receipt.get("status") == "failed" or result.get("error") in MISSING:
            attempts = result.get("attempts", (result.get("details") or {}).get("attempts", 1))
            count += min(LIMIT, max(1, attempts)) if type(attempts) is int else 1
    return count


def _code(command):
    # Mask quoted text so an explanation/echo is not confused with a loop.
    out, quote, escaped, comment = [], None, False, False
    for char in command:
        if comment:
            out.append("\n" if char == "\n" else " ")
            comment = char != "\n"
        elif escaped:
            out.append(" ")
            escaped = False
        elif char in {"`", "\\"} and quote == '"':
            escaped = True
            out.append(" ")
        elif quote:
            if char == quote:
                quote = None
            out.append("\n" if char == "\n" else " ")
        elif char in {"'", '"'}:
            quote = char
            out.append(" ")
        elif char == "#":
            comment = True
            out.append(" ")
        else:
            out.append(char)
    return "".join(out)


def check_shell_retry(row, call):
    if call.get("name") != "ryn_shell_execute":
        return
    command = (call.get("arguments") or {}).get("command", "")
    code = _code(command)
    if not _LOOP.search(code) or not _READ.search(code):
        return
    normalized = command.casefold().replace("/", "\\")
    while "\\\\" in normalized:
        normalized = normalized.replace("\\\\", "\\")
    for receipt in reversed(row.get("receipts", [])):
        target = file_target(row, receipt)
        if target and target.casefold().replace("/", "\\") in normalized and failure_count(row, target):
            raise AgentToolRejected("agent_unbounded_read_retry", correction=
                "This file read already failed. An unbounded shell retry loop was not started. "
                "Use ryn_file_wait for the remaining bounded attempts, inspect the cause, or report the missing input. "
                "A shell timeout is not an attempt limit.")
