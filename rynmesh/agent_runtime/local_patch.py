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

from .protocol import sanitized
from .store import AgentToolRejected

BEGIN, END = "*** Begin Patch", "*** End Patch"
ADD, DELETE, UPDATE, MOVE, EOF = "*** Add File: ", "*** Delete File: ", "*** Update File: ", "*** Move to: ", "*** End of File"
MAX_FILES = 32
MAX_SNIPPET_LINES = 15
MAX_SNIPPET_CHARS = 1600


def reject(code: str, *, correction: str | None = None, details: dict | None = None):
    raise AgentToolRejected("agent_patch_" + code, correction=correction, details=details)


def _hunk(anchor=""):
    return {"anchor": anchor, "old": [], "new": [], "ops": [], "eof": False}


def bounded_snippet(
    lines: list[str],
    center_start: int,
    span: int = 1,
    *,
    max_lines: int = MAX_SNIPPET_LINES,
    max_chars: int = MAX_SNIPPET_CHARS,
) -> tuple[str, int, int]:
    if not lines:
        return "", 1, 1
    total = len(lines)
    center_start = max(0, min(center_start, total - 1))
    span = max(1, span)
    padding = max(0, (max_lines - min(span, max_lines)) // 2)
    start_idx = max(0, center_start - padding)
    end_idx = min(total, start_idx + max_lines)
    if end_idx - start_idx < max_lines and start_idx > 0:
        start_idx = max(0, end_idx - max_lines)
    out_lines = []
    used_chars = 0
    actual_end = start_idx
    for idx in range(start_idx, end_idx):
        formatted = f"{idx + 1}: {lines[idx]}"
        if len(formatted) > 300:
            formatted = formatted[:297] + "..."
        extra = len(formatted) + (1 if out_lines else 0)
        if out_lines and used_chars + extra > max_chars:
            break
        out_lines.append(formatted[:max_chars])
        used_chars += extra
        actual_end = idx + 1
    text = sanitized("\n".join(out_lines))
    if isinstance(text, str) and len(text) > max_chars:
        text = text[:max_chars]
    return text, start_idx + 1, max(start_idx + 1, actual_end)


def parse(text: str) -> list[dict]:
    lines = text.replace("\r\n", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    if len(lines) < 2 or lines[0].strip() != BEGIN or lines[-1].strip() != END:
        reject(
            "invalid_envelope",
            correction="Wrap the patch in '*** Begin Patch' and '*** End Patch' lines.",
            details={"field_path": "patch", "reason": "invalid_envelope"},
        )
    operations, body, index = [], lines[1:-1], 0
    while index < len(body):
        line = body[index]
        if line.startswith(ADD):
            path, index, added = line[len(ADD):].strip(), index + 1, []
            while index < len(body) and not body[index].startswith("*** "):
                if not body[index].startswith("+"):
                    reject(
                        "invalid_add_line",
                        correction=f"Every content line under '*** Add File: {path}' must start with '+'.",
                        details={"path": path, "field_path": "patch", "line_index": index + 1},
                    )
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
                        reject(
                            "invalid_hunk",
                            correction="Place '*** End of File' only at the end of a hunk inside '*** Update File:'.",
                            details={"path": operation["path"], "field_path": "patch"},
                        )
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
                    reject(
                        "invalid_hunk_line",
                        correction=f"Hunk lines in '{operation['path']}' must start with ' ' (context), '-' (remove), or '+' (add).",
                        details={"path": operation["path"], "hunk_index": len(operation["hunks"]) - 1, "field_path": "patch"},
                    )
                if marker != "+":
                    hunk["old"].append(value)
                if marker != "-":
                    hunk["new"].append(value)
                hunk["ops"].append((marker, value))
            if not operation["hunks"] and not operation["move_to"]:
                reject(
                    "empty_update",
                    correction=f"'*** Update File: {operation['path']}' has no hunks and no '*** Move to:' target.",
                    details={"path": operation["path"], "field_path": "patch"},
                )
            if any(not item["old"] and not item["new"] for item in operation["hunks"]):
                reject(
                    "empty_hunk",
                    correction=f"Remove empty '@@' hunks in '{operation['path']}'.",
                    details={"path": operation["path"], "field_path": "patch"},
                )
            operations.append(operation)
        else:
            reject(
                "invalid_operation",
                correction="Each file section in the patch must start with '*** Add File: ', '*** Update File: ', or '*** Delete File: '.",
                details={"field_path": "patch", "line_index": index + 1},
            )
    if not operations:
        reject("empty", correction="The patch contains no file operations.", details={"field_path": "patch"})
    if len(operations) > MAX_FILES:
        reject(
            "too_many_files",
            correction=f"A single patch may touch at most {MAX_FILES} files.",
            details={"field_path": "patch", "max_files": MAX_FILES},
        )
    if any(not operation["path"] or operation.get("move_to") == "" for operation in operations):
        reject(
            "path_required",
            correction="Every '*** Add/Update/Delete File:' and '*** Move to:' line requires a non-empty workspace path.",
            details={"field_path": "patch"},
        )
    return operations


def _find_matches(
    lines: list[str],
    pattern: list[str],
    start: int,
    eof: bool,
    *,
    normalizers=(lambda value: value, str.rstrip),
) -> list[int]:
    """All matches at or after start on the immutable file lines: exact first, then trailing-whitespace normalized."""
    last = len(lines) - len(pattern)
    if last < start:
        return []
    candidates = [last] if eof else range(start, last + 1)
    for normalize in normalizers:
        wanted = [normalize(value) for value in pattern]
        found = [
            position
            for position in candidates
            if [normalize(value) for value in lines[position:position + len(pattern)]] == wanted
        ]
        if found:
            return found
    return []


def _best_hint_line(lines: list[str], pattern: list[str], default: int = 0) -> int:
    for probe in pattern:
        stripped = probe.strip()
        if not stripped:
            continue
        for idx, line in enumerate(lines):
            if line.strip() == stripped or stripped in line:
                return idx
    return max(0, min(default, max(len(lines) - 1, 0)))


def _hunks_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    if a_start == a_end and b_start == b_end:
        return a_start == b_start
    if a_start == a_end:
        return b_start <= a_start < b_end
    if b_start == b_end:
        return a_start <= b_start < a_end
    return max(a_start, b_start) < min(a_end, b_end)


def _reject_hunk(
    *,
    path: str | None,
    hunk_index: int,
    match_count: int,
    reason: str,
    lines: list[str],
    hint_start: int,
    span: int = 1,
    correction: str,
    extra_details: dict | None = None,
) -> None:
    snippet, s_start, s_end = bounded_snippet(lines, hint_start, span)
    details = {
        **({"path": path} if path else {}),
        "hunk_index": hunk_index,
        "match_count": match_count,
        "reason": reason,
        "field_path": "patch",
        "snippet": snippet,
        "snippet_start_line": s_start,
        "snippet_end_line": s_end,
        **(extra_details or {}),
    }
    full_correction = correction
    if snippet:
        full_correction = f"{correction}\nActual file lines ({s_start}-{s_end}):\n{snippet}"
    reject("context_not_found", correction=full_correction, details=details)


def apply_hunks(text: str, hunks: list[dict], *, path: str | None = None) -> str:
    """Locate all hunks on the immutable original snapshot, verify uniqueness and non-overlap, then apply in reverse order."""
    eol = "\r\n" if "\r\n" in text and text.count("\r\n") * 2 >= text.count("\n") else "\n"
    trailing = text.endswith("\n") or not text
    lines = text.replace("\r\n", "\n").split("\n") if text else []
    if text.endswith("\n"):
        lines.pop()

    located: list[tuple[int, int, int, list[str]]] = []
    for hunk_index, hunk in enumerate(hunks):
        anchor_matches: list[int] = []
        search_start = 0
        if hunk["anchor"]:
            anchor_matches = _find_matches(
                lines,
                [hunk["anchor"]],
                0,
                False,
                normalizers=(lambda value: value, str.rstrip, str.strip),
            )
            if not anchor_matches:
                hint = _best_hint_line(lines, [hunk["anchor"], *hunk["old"]], 0)
                _reject_hunk(
                    path=path,
                    hunk_index=hunk_index,
                    match_count=0,
                    reason="context_not_found",
                    lines=lines,
                    hint_start=hint,
                    span=max(1, len(hunk["old"])),
                    correction=f"Hunk {hunk_index} anchor '@@ {hunk['anchor']}' was not found in the file.",
                )
            search_start = anchor_matches[0] + 1

        if hunk["old"]:
            matches = _find_matches(lines, hunk["old"], search_start, hunk["eof"])
            if hunk["anchor"] and len(matches) > 1:
                contiguous = [a_pos + 1 for a_pos in anchor_matches if (a_pos + 1) in matches]
                if len(contiguous) == 1:
                    matches = contiguous
                else:
                    # Also check if each anchor scopes to at most one match before the next anchor.
                    scoped = []
                    for a_idx, a_pos in enumerate(anchor_matches):
                        next_a = anchor_matches[a_idx + 1] if a_idx + 1 < len(anchor_matches) else len(lines)
                        in_scope = [m for m in matches if a_pos < m < next_a]
                        if in_scope:
                            scoped.extend(in_scope)
                    if len(scoped) == 1:
                        matches = scoped
            if not matches:
                strip_matches = _find_matches(lines, hunk["old"], search_start, hunk["eof"], normalizers=(str.strip,))
                if strip_matches:
                    _reject_hunk(
                        path=path,
                        hunk_index=hunk_index,
                        match_count=0,
                        reason="indentation_mismatch",
                        lines=lines,
                        hint_start=strip_matches[0],
                        span=len(hunk["old"]),
                        correction=(
                            f"Hunk {hunk_index} context lines match only if leading indentation is ignored. "
                            "Preserve the file's exact leading indentation in context, '-', and '+' lines."
                        ),
                    )
                default_hint = anchor_matches[0] if anchor_matches else (max(0, len(lines) - 10) if hunk["eof"] else 0)
                hint = _best_hint_line(lines, hunk["old"], default_hint)
                _reject_hunk(
                    path=path,
                    hunk_index=hunk_index,
                    match_count=0,
                    reason="context_not_found",
                    lines=lines,
                    hint_start=hint,
                    span=len(hunk["old"]),
                    correction=(
                        f"Hunk {hunk_index} context lines were not found in the file. "
                        "Use the actual file lines below to update your hunk context."
                    ),
                )
            if len(matches) > 1:
                _reject_hunk(
                    path=path,
                    hunk_index=hunk_index,
                    match_count=len(matches),
                    reason="ambiguous_context",
                    lines=lines,
                    hint_start=matches[0],
                    span=len(hunk["old"]),
                    correction=(
                        f"Hunk {hunk_index} matched {len(matches)} locations in the file (lines "
                        + ", ".join(str(m + 1) for m in matches[:6])
                        + "). Include more surrounding context lines or a unique '@@' anchor so the hunk matches uniquely."
                    ),
                    extra_details={"match_lines": [m + 1 for m in matches[:10]]},
                )
            position = matches[0]
        else:
            if hunk["anchor"] and not hunk["eof"]:
                if len(anchor_matches) > 1:
                    _reject_hunk(
                        path=path,
                        hunk_index=hunk_index,
                        match_count=len(anchor_matches),
                        reason="ambiguous_context",
                        lines=lines,
                        hint_start=anchor_matches[0],
                        span=1,
                        correction=(
                            f"Pure addition hunk {hunk_index} anchor '@@ {hunk['anchor']}' matched "
                            f"{len(anchor_matches)} locations. Include context lines to disambiguate."
                        ),
                        extra_details={"match_lines": [m + 1 for m in anchor_matches[:10]]},
                    )
                position = anchor_matches[0] + 1
            else:
                position = len(lines)

        start_pos = position
        end_pos = position + len(hunk["old"])
        # Matched context keeps the file's own text, while '+' lines keep their exact supplied indentation.
        original, replacement = iter(lines[start_pos:end_pos]), []
        for marker, value in hunk["ops"]:
            if marker == " ":
                replacement.append(next(original))
            elif marker == "-":
                next(original)
            else:
                replacement.append(value)
        located.append((hunk_index, start_pos, end_pos, replacement))

    for i in range(len(located)):
        idx_a, start_a, end_a, _ = located[i]
        for j in range(i + 1, len(located)):
            idx_b, start_b, end_b, _ = located[j]
            if _hunks_overlap(start_a, end_a, start_b, end_b):
                overlap_start = min(start_a, start_b)
                overlap_span = max(1, max(end_a, end_b) - overlap_start)
                _reject_hunk(
                    path=path,
                    hunk_index=idx_b,
                    match_count=2,
                    reason="overlapping_hunks",
                    lines=lines,
                    hint_start=overlap_start,
                    span=overlap_span,
                    correction=(
                        f"Hunk {idx_b} overlaps with hunk {idx_a} on the original file snapshot. "
                        "Merge overlapping edits into a single hunk."
                    ),
                    extra_details={"conflicting_hunk_index": idx_a},
                )

    result_lines = list(lines)
    for _hunk_index, start_pos, end_pos, replacement in sorted(
        located, key=lambda item: (item[1], item[2], item[0]), reverse=True
    ):
        result_lines[start_pos:end_pos] = replacement

    result = eol.join(result_lines)
    return result + eol if trailing and result_lines else result
