"""Codex-style multi-file patches for workspace files.

The grammar matches the published Codex ``apply_patch`` envelope::

    *** Begin Patch
    *** Add File: path            (+ lines follow)
    *** Delete File: path
    *** Update File: path
    *** Move to: new/path         (optional, directly after Update File)
    @@ optional line to locate
     context line
    -removed line
    +added line
    *** End of File               (optional: the hunk ends at end of file)
    *** End Patch

Parsing and hunk application are pure functions. Every hunk must match before
anything is written, so a patch that does not apply changes no file.
"""
from __future__ import annotations

from .store import AgentToolRejected

BEGIN, END = "*** Begin Patch", "*** End Patch"
ADD, DELETE, UPDATE, MOVE, EOF = "*** Add File: ", "*** Delete File: ", "*** Update File: ", "*** Move to: ", "*** End of File"
MAX_FILES = 32


def reject(code):
    raise AgentToolRejected("agent_patch_" + code)


def _hunk(anchor=""):
    return {"anchor": anchor, "old": [], "new": [], "ops": [], "eof": False}


def parse(text: str) -> list[dict]:
    lines = text.replace("\r\n", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    if len(lines) < 2 or lines[0].strip() != BEGIN or lines[-1].strip() != END:
        reject("invalid_envelope")
    operations, body, index = [], lines[1:-1], 0
    while index < len(body):
        line = body[index]
        if line.startswith(ADD):
            path, index, added = line[len(ADD):].strip(), index + 1, []
            while index < len(body) and not body[index].startswith("*** "):
                if not body[index].startswith("+"):
                    reject("invalid_add_line")
                added.append(body[index][1:])
                index += 1
            operations.append({"kind": "add", "path": path, "lines": added})
        elif line.startswith(DELETE):
            operations.append({"kind": "delete", "path": line[len(DELETE):].strip()})
            index += 1
        elif line.startswith(UPDATE):
            operation = {"kind": "update", "path": line[len(UPDATE):].strip(), "move_to": None, "hunks": []}
            index += 1
            if index < len(body) and body[index].startswith(MOVE):
                operation["move_to"] = body[index][len(MOVE):].strip()
                index += 1
            hunk = None
            while index < len(body) and (not body[index].startswith("*** ") or body[index].strip() == EOF):
                current = body[index]
                index += 1
                if current.strip() == EOF:
                    if hunk is None:
                        reject("invalid_hunk")
                    hunk["eof"] = True
                    continue
                if current.startswith("@@"):
                    hunk = _hunk(current[2:].strip())
                    operation["hunks"].append(hunk)
                    continue
                if hunk is None:  # The first hunk may omit its @@ line.
                    hunk = _hunk()
                    operation["hunks"].append(hunk)
                # Models often drop the single space of an empty context line.
                marker, value = (current[:1], current[1:]) if current else (" ", "")
                if marker not in " -+":
                    reject("invalid_hunk_line")
                if marker != "+":
                    hunk["old"].append(value)
                if marker != "-":
                    hunk["new"].append(value)
                hunk["ops"].append((marker, value))
            if not operation["hunks"] and not operation["move_to"]:
                reject("empty_update")
            if any(not item["old"] and not item["new"] for item in operation["hunks"]):
                reject("empty_hunk")
            operations.append(operation)
        else:
            reject("invalid_operation")
    if not operations:
        reject("empty")
    if len(operations) > MAX_FILES:
        reject("too_many_files")
    if any(not operation["path"] or operation.get("move_to") == "" for operation in operations):
        reject("path_required")
    return operations


def _seek(lines, pattern, start, eof):
    """First match at or after start: exact, then ignoring trailing, then outer whitespace."""
    last = len(lines) - len(pattern)
    if last < start:
        return None
    candidates = [last] if eof else range(start, last + 1)
    for normalize in (lambda value: value, str.rstrip, str.strip):
        wanted = [normalize(value) for value in pattern]
        for position in candidates:
            if [normalize(value) for value in lines[position:position + len(pattern)]] == wanted:
                return position
    return None


def apply_hunks(text: str, hunks: list[dict]) -> str:
    """Apply hunks in order, keeping the file's line-ending and final-newline conventions."""
    eol = "\r\n" if "\r\n" in text and text.count("\r\n") * 2 >= text.count("\n") else "\n"
    trailing = text.endswith("\n") or not text
    lines = text.replace("\r\n", "\n").split("\n") if text else []
    if text.endswith("\n"):
        lines.pop()
    cursor = 0
    for hunk in hunks:
        if hunk["anchor"]:
            found = _seek(lines, [hunk["anchor"]], cursor, False)
            if found is None:
                reject("context_not_found")
            cursor = found + 1
        if hunk["old"]:
            position = _seek(lines, hunk["old"], cursor, hunk["eof"])
            if position is None:
                reject("context_not_found")
        else:
            # A pure addition goes after its anchor, otherwise at end of file.
            position = cursor if hunk["anchor"] and not hunk["eof"] else len(lines)
        # Matched context keeps the file's own text, even when it matched
        # only after whitespace normalization.
        original, replacement = iter(lines[position:position + len(hunk["old"])]), []
        for marker, value in hunk["ops"]:
            if marker == " ":
                replacement.append(next(original))
            elif marker == "-":
                next(original)
            else:
                replacement.append(value)
        lines[position:position + len(hunk["old"])] = replacement
        cursor = position + len(replacement)
    result = eol.join(lines)
    return result + eol if trailing and lines else result
