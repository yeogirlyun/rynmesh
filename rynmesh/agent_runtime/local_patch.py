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

import posixpath
import re
import shlex
from pathlib import PureWindowsPath

from .protocol import sanitized
from .store import AgentToolRejected

BEGIN, END = "*** Begin Patch", "*** End Patch"
ADD, DELETE, UPDATE, MOVE, EOF = "*** Add File: ", "*** Delete File: ", "*** Update File: ", "*** Move to: ", "*** End of File"
MAX_FILES = 128
MAX_SNIPPET_LINES = 15
MAX_SNIPPET_CHARS = 1600


def shell_patch(command: str, shell: str):
    """Recognize a single literal patch invocation without executing shell text."""
    command = command.replace("\r\n", "\n").strip()
    wrapper = re.fullmatch(r"(?:bash|sh|zsh|pwsh|powershell|cmd)(?:\.exe)?\s+(?:-c|-lc|-Command|/c)\s+([\s\S]+)", command, re.I)
    if wrapper:
        command = wrapper.group(1)
        if command[:1] in {"'", '"'}:
            try:
                words = shlex.split(command)
            except ValueError:
                return None
            if len(words) != 1:
                return None
            command = words[0]
        return shell_patch(command, shell)
    if command.startswith(BEGIN):
        return {"error": "implicit_patch"}
    cd = None
    prefix = re.match(r"cd\s+([^\n]+?)\s+&&\s*", command)
    if prefix:
        try:
            words = shlex.split(prefix.group(1), posix=False)
        except ValueError:
            return None
        if len(words) != 1:
            return None
        cd = words[0]
        if cd[:1] in {"'", '"'} and cd[-1:] == cd[:1]:
            cd = cd[1:-1]
        if not cd or re.search(r"[$`*?(){}\[\]&|;<>]", cd):
            return None
        command = command[prefix.end():]
    lines = command.split("\n")
    if len(lines) < 3:
        return None
    heredoc = re.fullmatch(r"(?:apply_patch|applypatch)\s+<<\s*(?:'([A-Za-z_][A-Za-z_0-9]*)'|\"([A-Za-z_][A-Za-z_0-9]*)\"|([A-Za-z_][A-Za-z_0-9]*))\s*", lines[0])
    if heredoc:
        delimiter = next(group for group in heredoc.groups() if group is not None)
        if lines[-1] == delimiter and delimiter not in lines[1:-1]:
            return {"patch": "\n".join(lines[1:-1]), "cd": cd}
    if lines[0] == "@'" and re.fullmatch(r"'@\s*\|\s*(?:apply_patch|applypatch)", lines[-1]) and "'@" not in lines[1:-1]:
        return {"patch": "\n".join(lines[1:-1]), "cd": cd}
    return None


def rebase_paths(patch: str, base: str) -> str:
    """Rebase only operation headers; patch content and absolute paths stay literal."""
    lines = patch.splitlines(keepends=True)
    for index, line in enumerate(lines):
        for header in (ADD, UPDATE, DELETE, MOVE):
            if line.startswith(header):
                path = line[len(header):].rstrip("\r\n")
                if not posixpath.isabs(path) and not PureWindowsPath(path).is_absolute():
                    rebased = posixpath.normpath(posixpath.join(base.replace("\\", "/"), path.replace("\\", "/")))
                    lines[index] = header + rebased + line[len(line.rstrip("\r\n")):]
                break
    return "".join(lines)


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


_HEREDOC_OPENERS = frozenset({"<<EOF", "<<'EOF'", '<<"EOF"'})


def _trim_blank_edges(lines: list[str]) -> list[str]:
    trimmed = list(lines)
    while trimmed and not trimmed[-1].strip():
        trimmed.pop()
    while trimmed and not trimmed[0].strip():
        trimmed.pop(0)
    return trimmed


def _is_patch_envelope(lines: list[str]) -> bool:
    return len(lines) >= 2 and lines[0].strip() == BEGIN and lines[-1].strip() == END


def _unwrap_lenient_envelope(lines: list[str]) -> list[str]:
    if len(lines) < 4:
        return lines
    first = lines[0].strip()
    last = lines[-1].strip()
    heredoc_head = first[len("apply_patch "):].lstrip() if first.startswith("apply_patch ") else first
    if heredoc_head in _HEREDOC_OPENERS and last.endswith("EOF"):
        return _trim_blank_edges(lines[1:-1])
    if first.startswith("```") and last == "```":
        return _trim_blank_edges(lines[1:-1])
    return lines


def parse(text: str) -> list[dict]:
    lines = _trim_blank_edges(text.replace("\r\n", "\n").split("\n"))
    if not _is_patch_envelope(lines):
        lines = _unwrap_lenient_envelope(lines)
    if not _is_patch_envelope(lines):
        reject(
            "invalid_envelope",
            correction=(
                "Wrap the patch in '*** Begin Patch' and '*** End Patch' lines "
                "(already tried stripping heredoc <<EOF...EOF and ```...``` fences)."
            ),
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


_UNICODE_PUNCT_TABLE = str.maketrans({
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2015": "-",
    "\u2212": "-",
    "\u00a0": " ",
    "\u202f": " ",
    "\u205f": " ",
    "\u3000": " ",
    "\u2026": "...",
})


def normalize_unicode_punctuation(value: str) -> str:
    """Codex seek_sequence.rs-aligned Unicode punctuation normalization preserving leading indentation."""
    return value.translate(_UNICODE_PUNCT_TABLE).rstrip()


def _normalize_unicode_stripped(value: str) -> str:
    return normalize_unicode_punctuation(value).strip()


def _find_matches(
    lines: list[str],
    pattern: list[str],
    start: int,
    eof: bool,
    *,
    normalizers=(lambda value: value, str.rstrip, str.strip, _normalize_unicode_stripped),
) -> list[int]:
    """All matches at or after start on the immutable file lines across the 4 fuzzy levels."""
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


def _trim_trailing_empty_line(
    old: list[str],
    new: list[str],
    ops: list[tuple[str, str]],
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    trimmed_old = old[:-1]
    trimmed_new = new[:-1] if new and new[-1] == "" else list(new)
    trimmed_ops = list(ops)
    removed_marker = None
    for idx in range(len(trimmed_ops) - 1, -1, -1):
        marker, value = trimmed_ops[idx]
        if marker in (" ", "-") and value == "":
            removed_marker = marker
            trimmed_ops.pop(idx)
            break
    if removed_marker == "-" and new and new[-1] == "":
        for idx in range(len(trimmed_ops) - 1, -1, -1):
            marker, value = trimmed_ops[idx]
            if marker != "-":
                if marker == "+" and value == "":
                    trimmed_ops.pop(idx)
                break
    return trimmed_old, trimmed_new, trimmed_ops


def _match_old_with_trailing_retry(
    lines: list[str],
    old: list[str],
    new: list[str],
    ops: list[tuple[str, str]],
    start: int,
    eof: bool,
) -> tuple[list[int], list[str], list[str], list[tuple[str, str]]]:
    matches = _find_matches(lines, old, start, eof)
    if matches or not old or old[-1] != "":
        return matches, old, new, ops
    trimmed_old, trimmed_new, trimmed_ops = _trim_trailing_empty_line(old, new, ops)
    if trimmed_old:
        retry_matches = _find_matches(lines, trimmed_old, start, eof)
        if retry_matches:
            return retry_matches, trimmed_old, trimmed_new, trimmed_ops
    elif not lines and start == 0:
        return [0], trimmed_old, trimmed_new, trimmed_ops
    return [], old, new, ops


def _narrow_by_anchor(matches: list[int], anchor_matches: list[int], total_lines: int) -> list[int]:
    if anchor_matches and len(matches) > 1:
        contiguous = [a_pos + 1 for a_pos in anchor_matches if (a_pos + 1) in matches]
        if len(contiguous) == 1:
            return contiguous
        scoped = []
        for a_idx, a_pos in enumerate(anchor_matches):
            next_a = anchor_matches[a_idx + 1] if a_idx + 1 < len(anchor_matches) else total_lines
            in_scope = [m for m in matches if a_pos < m < next_a]
            if in_scope:
                scoped.extend(in_scope)
        if len(scoped) == 1:
            return scoped
    return matches


def _split_keep_endings(text: str) -> tuple[list[str], list[str], str]:
    if not text:
        return [], [], "\n"
    lines: list[str] = []
    endings: list[str] = []
    preferred: str | None = None
    n = len(text)
    i = 0
    line_start = 0
    while i < n:
        ch = text[i]
        if ch == "\r":
            if i + 1 < n and text[i + 1] == "\n":
                ending = "\r\n"
                next_i = i + 2
            else:
                ending = "\r"
                next_i = i + 1
            lines.append(text[line_start:i])
            endings.append(ending)
            if preferred is None:
                preferred = ending
            i = line_start = next_i
        elif ch == "\n":
            lines.append(text[line_start:i])
            endings.append("\n")
            if preferred is None:
                preferred = "\n"
            i = line_start = i + 1
        else:
            i += 1
    if line_start < n:
        lines.append(text[line_start:n])
        endings.append("")
    return lines, endings, (preferred if preferred is not None else "\n")


def apply_hunks(text: str, hunks: list[dict], *, path: str | None = None) -> str:
    """Locate all hunks on the immutable original snapshot using a sequential cursor, verify non-overlap, then apply in reverse order."""
    lines, endings, preferred = _split_keep_endings(text)
    trailing = (not text) or bool(endings[-1])

    cursor = 0
    located: list[tuple[int, int, int, list[tuple[str, str]]]] = []
    for hunk_index, hunk in enumerate(hunks):
        anchor = hunk["anchor"]
        eof = hunk["eof"]
        old = list(hunk["old"])
        new = list(hunk["new"])
        ops = list(hunk["ops"])

        if old:
            anchor_matches: list[int] = []
            forward_matches: list[int] = []
            f_old, f_new, f_ops = old, new, ops
            if anchor:
                anchor_matches = _find_matches(lines, [anchor], cursor, False)
                if anchor_matches:
                    search_start = anchor_matches[0] + 1
                    forward_matches, f_old, f_new, f_ops = _match_old_with_trailing_retry(
                        lines, old, new, ops, search_start, eof
                    )
                    forward_matches = _narrow_by_anchor(forward_matches, anchor_matches, len(lines))
            else:
                forward_matches, f_old, f_new, f_ops = _match_old_with_trailing_retry(
                    lines, old, new, ops, cursor, eof
                )

            if forward_matches:
                position = forward_matches[0]
                old, new, ops = f_old, f_new, f_ops
            else:
                full_anchor_matches: list[int] = []
                if anchor:
                    full_anchor_matches = _find_matches(lines, [anchor], 0, False)
                    if not full_anchor_matches:
                        hint = _best_hint_line(lines, [anchor, *old], 0)
                        _reject_hunk(
                            path=path,
                            hunk_index=hunk_index,
                            match_count=0,
                            reason="context_not_found",
                            lines=lines,
                            hint_start=hint,
                            span=max(1, len(old)),
                            correction=f"Hunk {hunk_index} anchor '@@ {anchor}' was not found in the file.",
                        )
                    full_search_start = full_anchor_matches[0] + 1
                else:
                    full_search_start = 0

                full_matches, fb_old, fb_new, fb_ops = _match_old_with_trailing_retry(
                    lines, old, new, ops, full_search_start, eof
                )
                full_matches = _narrow_by_anchor(full_matches, full_anchor_matches, len(lines))
                if not full_matches:
                    default_hint = (
                        full_anchor_matches[0]
                        if full_anchor_matches
                        else (max(0, len(lines) - 10) if eof else 0)
                    )
                    hint = _best_hint_line(lines, old, default_hint)
                    _reject_hunk(
                        path=path,
                        hunk_index=hunk_index,
                        match_count=0,
                        reason="context_not_found",
                        lines=lines,
                        hint_start=hint,
                        span=len(old),
                        correction=(
                            f"Hunk {hunk_index} context lines were not found in the file. "
                            "Use the actual file lines below to update your hunk context."
                        ),
                    )
                if len(full_matches) > 1:
                    _reject_hunk(
                        path=path,
                        hunk_index=hunk_index,
                        match_count=len(full_matches),
                        reason="ambiguous_context",
                        lines=lines,
                        hint_start=full_matches[0],
                        span=len(fb_old),
                        correction=(
                            f"Hunk {hunk_index} matched {len(full_matches)} locations in the file (lines "
                            + ", ".join(str(m + 1) for m in full_matches[:6])
                            + "). Include more surrounding context lines or a unique '@@' anchor so the hunk matches uniquely."
                        ),
                        extra_details={"match_lines": [m + 1 for m in full_matches[:10]]},
                    )
                position = full_matches[0]
                old, new, ops = fb_old, fb_new, fb_ops
        else:
            if anchor and not eof:
                anchor_matches = _find_matches(lines, [anchor], cursor, False)
                if not anchor_matches and cursor > 0:
                    anchor_matches = _find_matches(lines, [anchor], 0, False)
                if not anchor_matches:
                    hint = _best_hint_line(lines, [anchor], 0)
                    _reject_hunk(
                        path=path,
                        hunk_index=hunk_index,
                        match_count=0,
                        reason="context_not_found",
                        lines=lines,
                        hint_start=hint,
                        span=1,
                        correction=f"Hunk {hunk_index} anchor '@@ {anchor}' was not found in the file.",
                    )
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
                            f"Pure addition hunk {hunk_index} anchor '@@ {anchor}' matched "
                            f"{len(anchor_matches)} locations. Include context lines to disambiguate."
                        ),
                        extra_details={"match_lines": [m + 1 for m in anchor_matches[:10]]},
                    )
                position = anchor_matches[0] + 1
            else:
                position = len(lines)

        cursor = position + len(old)
        start_pos = position
        end_pos = position + len(old)
        # Matched context keeps the file's own text and line ending, while '+' lines keep their exact indentation and use preferred ending.
        original = iter(zip(lines[start_pos:end_pos], endings[start_pos:end_pos]))
        replacement: list[tuple[str, str]] = []
        for marker, value in ops:
            if marker == " ":
                replacement.append(next(original))
            elif marker == "-":
                next(original)
            else:
                replacement.append((value, preferred))
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

    result_pairs: list[tuple[str, str]] = list(zip(lines, endings))
    for _hunk_index, start_pos, end_pos, replacement in sorted(
        located, key=lambda item: (item[1], item[2], item[0]), reverse=True
    ):
        result_pairs[start_pos:end_pos] = replacement

    if not result_pairs:
        return ""
    last_idx = len(result_pairs) - 1
    out: list[str] = []
    for idx, (line_text, line_ending) in enumerate(result_pairs):
        if idx < last_idx:
            out.append(line_text + (line_ending or preferred))
        else:
            out.append(line_text + ((line_ending or preferred) if trailing else ""))
    return "".join(out)
