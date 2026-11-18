"""Recognize visible forced deletes and URL openers; this is not a shell sandbox."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .execution_policy import executable_key


@dataclass(frozen=True)
class _Word:
    text: str
    static: bool


def _segments(script: str, shell: str) -> tuple[list[list[_Word]], bool]:
    segments, words, word = [], [], []
    quote = None
    present, static, strict = False, True, True
    escape = "\\" if shell in {"sh", "bash", "zsh"} else "`"

    def finish_word():
        nonlocal word, present, static
        if present:
            words.append(_Word("".join(word), static))
        word, present, static = [], False, True

    index = 0
    while index < len(script):
        char = script[index]
        if char == escape and quote != "'" and index + 1 < len(script):
            word.append(script[index + 1])
            present = True
            index += 2
            continue
        if quote:
            if char == quote:
                if shell == "powershell" and quote == "'" and script[index:index + 2] == "''":
                    word.append("'")
                    index += 2
                    continue
                quote = None
            else:
                word.append(char)
                if quote == '"' and char == "$":
                    static = False
        elif char in "'\"":
            quote, present = char, True
        elif char in ";|&\r\n\t":
            finish_word()
            if words:
                segments.append(words)
                words = []
        elif char.isspace():
            finish_word()
        elif char == "#" and not present and shell != "cmd":
            while index < len(script) and script[index] not in "\r\n":
                index += 1
            continue
        else:
            word.append(char)
            present = True
            if char == "$":
                static = False
            if char in "(){}<>" or (char == "`" and escape != "`"):
                strict = False
        index += 1
    finish_word()
    if words:
        segments.append(words)
    return segments, strict and quote is None


def _key(word: _Word) -> str:
    return executable_key(word.text.strip("(){}")) if word.static else ""


def _is_http_url(word: str) -> bool:
    try:
        return urlparse(word.strip("\"'(); ")).scheme.lower() in {"http", "https"}
    except ValueError:
        return False


_DELETES = {"remove-item", "ri", "rm", "del", "erase", "rd", "rmdir"}
_OPENERS = {"start-process", "start", "ii", "invoke-item", "explorer", "mshta",
            "chrome", "google-chrome", "chromium", "msedge", "firefox", "iexplore", "brave", "opera", "vivaldi", "safari"}
_ECHO = {"echo", "printf", "write-output", "write-host"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SUDO_VALUE_OPTIONS = {"-u", "-g", "-h", "-p", "-C", "-T", "-D", "-R",
                       "--user", "--group", "--host", "--prompt", "--close-from", "--command-timeout", "--chdir", "--chroot"}


def _windows_danger(words: list[_Word], *, coarse=False) -> str | None:
    if os.name != "nt" or not words:
        return None
    programs = {_key(word) for word in words} if coarse else {_key(words[0])}
    flags = {word.text.lower().strip("(){}") for word in words if word.static}
    if programs & _DELETES and ("-force" in flags or "/f" in flags or {"/s", "/q"} <= flags):
        return "force_delete"
    opener = bool(programs & _OPENERS) or (
        "rundll32" in programs and any(_key(word).split(",", 1)[0] == "url.dll" for word in words[1:]))
    if opener and any(word.static and _is_http_url(word.text) for word in words[1:]):
        return "open_url"
    return None


def _invocation(words: list[_Word], shell: str, depth: int) -> str | None:
    # Peel known execution prefixes, retaining dynamic assignment values without
    # treating them as a statically known executable.
    while words and _key(words[0]) in {"sudo", "env"}:
        program = _key(words[0])
        index = 1
        options = True
        while index < len(words):
            value = words[index].text
            if _ASSIGNMENT.match(value):
                index += 1
            elif options and value == "--":
                index += 1
                options = False
            elif options and program == "env" and value in {"-i", "--ignore-environment"}:
                index += 1
            elif options and program == "sudo" and value.startswith("-"):
                index += 2 if value in _SUDO_VALUE_OPTIONS else 1
            else:
                break
        words = words[index:]
    if not words:
        return None
    program = _key(words[0])
    if not program or program in _ECHO:
        return None
    if program == "trap":
        index = 2 if len(words) > 1 and words[1].text == "--" else 1
        if index < len(words) and words[index].static and not words[index].text.startswith("-"):
            return dangerous_shell({"command": words[index].text, "shell": "sh"}, depth=depth + 1)
        return None
    if program in {"sh", "bash", "zsh", "powershell", "pwsh", "cmd"}:
        for index, word in enumerate(words[1:], 1):
            if not word.static:
                continue
            flag = word.text.lower()
            posix = program in {"sh", "bash", "zsh"}
            command_flag = (flag.startswith("-") and not flag.startswith("--") and "c" in flag[1:]) if posix else (
                flag == "/c" if program == "cmd" else flag in {"-command", "-c"})
            if command_flag:
                body = words[index + 1:index + 2] if posix else words[index + 1:]
                if body and all(part.static for part in body):
                    inner = " ".join(part.text for part in body)
                    inner_shell = "sh" if posix else "cmd" if program == "cmd" else "powershell"
                    return dangerous_shell({"command": inner, "shell": inner_shell}, depth=depth + 1)
                return None
        return None
    windows = _windows_danger(words)
    if windows:
        return windows
    if program == "rm":
        for word in words[1:]:
            if not word.static:
                continue
            if word.text == "--":
                break
            if word.text == "--force" or (word.text.startswith("-") and not word.text.startswith("--") and "f" in word.text[1:]):
                return "forced_rm"
    return None


def dangerous_shell(arguments, *, depth=0) -> str | None:
    if depth > 8:
        return "wrapper_depth"
    script = arguments.get("command", "")
    if not isinstance(script, str):
        return None
    shell = arguments.get("shell", "powershell" if os.name == "nt" else "sh")
    segments, strict = _segments(script, shell)
    for words in segments:
        reason = _invocation(words, shell, depth)
        if reason:
            return reason
    if not strict:
        return _windows_danger([word for words in segments for word in words], coarse=True)
    return None
