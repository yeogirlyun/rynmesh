"""Tests for S05-I1 sequential cursor, 4-level fuzzy matching, and trailing blank line retry in local_patch."""
from __future__ import annotations

import copy

import pytest

from rynmesh.agent_runtime import local_patch
from rynmesh.agent_runtime.store import AgentToolRejected


def _patch(*lines: str) -> str:
    return "\n".join(["*** Begin Patch", *lines, "*** End Patch"])


def test_s05_i1_duplicate_function_bodies_sequential_hunks_update_first_then_second():
    source = (
        "def first_handler():\n"
        "    status = 'pending'\n"
        "    return status\n\n"
        "def second_handler():\n"
        "    status = 'pending'\n"
        "    return status\n"
    )
    ops = local_patch.parse(
        _patch(
            "*** Update File: handlers.py",
            "@@",
            "     status = 'pending'",
            "-    return status",
            "+    return status.upper()",
            "@@",
            "     status = 'pending'",
            "-    return status",
            "+    return status.lower()",
        )
    )
    result = local_patch.apply_hunks(source, ops[0]["hunks"], path="handlers.py")
    assert result == (
        "def first_handler():\n"
        "    status = 'pending'\n"
        "    return status.upper()\n\n"
        "def second_handler():\n"
        "    status = 'pending'\n"
        "    return status.lower()\n"
    )


def test_s05_i1_duplicate_anchor_second_hunk_lands_after_first_hunk():
    source = (
        "class FirstWorker:\n"
        "    def execute(self):\n"
        "        value = 1\n"
        "        return value\n\n"
        "class SecondWorker:\n"
        "    def execute(self):\n"
        "        value = 1\n"
        "        return value\n"
    )
    ops = local_patch.parse(
        _patch(
            "*** Update File: workers.py",
            "@@ def execute(self):",
            "-        value = 1",
            "+        value = 10",
            "@@ def execute(self):",
            "-        return value",
            "+        return value * 2",
        )
    )
    result = local_patch.apply_hunks(source, ops[0]["hunks"], path="workers.py")
    assert result == (
        "class FirstWorker:\n"
        "    def execute(self):\n"
        "        value = 10\n"
        "        return value\n\n"
        "class SecondWorker:\n"
        "    def execute(self):\n"
        "        value = 1\n"
        "        return value * 2\n"
    )


def test_s05_i1_wrong_indentation_tabs_vs_spaces_applies_and_preserves_context_indentation():
    source = (
        "def compute(total):\n"
        "\tstep = 1\n"
        "\ttotal = total + step\n"
        "\tlabel = \u201cdone \u2014 ok\u201d\n"
        "\treturn total\n"
    )
    # Context and '-' lines use spaces (and ASCII quotes/hyphen on level 4), while file uses tabs.
    ops = local_patch.parse(
        _patch(
            "*** Update File: compute.py",
            "@@ def compute(total):",
            "     step = 1",
            "-    total = total + step",
            "+    total = total + step * 2",
            '     label = "done - ok"',
            "     return total",
        )
    )
    result = local_patch.apply_hunks(source, ops[0]["hunks"], path="compute.py")
    assert result == (
        "def compute(total):\n"
        "\tstep = 1\n"
        "    total = total + step * 2\n"
        "\tlabel = \u201cdone \u2014 ok\u201d\n"
        "\treturn total\n"
    )


def test_s05_i1_trailing_empty_old_line_retries_and_updates_last_file_line():
    source = "header = 1\nfooter = 2\n"
    # Trailing ' ' line after '+footer = 20' creates old=[' header = 1'..., ''] at EOF.
    raw_patch = (
        "*** Begin Patch\n"
        "*** Update File: config.py\n"
        "@@\n"
        " header = 1\n"
        "-footer = 2\n"
        "+footer = 20\n"
        " \n"
        "*** End Patch"
    )
    ops = local_patch.parse(raw_patch)
    snapshot = copy.deepcopy(ops)
    assert ops[0]["hunks"][0]["old"][-1] == ""
    first_run = local_patch.apply_hunks(source, ops[0]["hunks"], path="config.py")
    second_run = local_patch.apply_hunks(source, ops[0]["hunks"], path="config.py")
    assert first_run == "header = 1\nfooter = 20\n"
    assert second_run == first_run
    assert ops == snapshot

    # Also test trailing '-' empty line paired with trailing '+' empty line at EOF
    raw_patch_del_add = (
        "*** Begin Patch\n"
        "*** Update File: config.py\n"
        "@@\n"
        " header = 1\n"
        "-footer = 2\n"
        "-\n"
        "+footer = 30\n"
        "+\n"
        "*** End Patch"
    )
    ops2 = local_patch.parse(raw_patch_del_add)
    assert ops2[0]["hunks"][0]["old"][-1] == ""
    assert ops2[0]["hunks"][0]["new"][-1] == ""
    assert local_patch.apply_hunks(source, ops2[0]["hunks"], path="config.py") == "header = 1\nfooter = 30\n"


def test_s05_i1_ambiguous_context_still_rejected_when_unresolved():
    # 1. Pure-addition hunk whose anchor matches multiple times after cursor and in full file
    dup_anchors = (
        "def block():\n"
        "    return 1\n\n"
        "def block():\n"
        "    return 2\n"
    )
    pure_add_ops = local_patch.parse(
        _patch(
            "*** Update File: dup.py",
            "@@ def block():",
            "+    # inserted",
        )
    )
    with pytest.raises(AgentToolRejected, match="agent_patch_context_not_found") as exc_pure:
        local_patch.apply_hunks(dup_anchors, pure_add_ops[0]["hunks"], path="dup.py")
    assert exc_pure.value.details["reason"] == "ambiguous_context"
    assert exc_pure.value.details["match_count"] == 2

    # 2. Fallback to full-file search when cursor already passed duplicate blocks rejects non-unique match
    dup_blocks_with_tail = (
        "def a():\n"
        "    x = 1\n\n"
        "def b():\n"
        "    x = 1\n\n"
        "def tail():\n"
        "    done = False\n"
    )
    fallback_ambiguous_ops = local_patch.parse(
        _patch(
            "*** Update File: tail.py",
            "@@ def tail():",
            "-    done = False",
            "+    done = True",
            "@@",
            "-    x = 1",
            "+    x = 2",
        )
    )
    with pytest.raises(AgentToolRejected, match="agent_patch_context_not_found") as exc_fb:
        local_patch.apply_hunks(dup_blocks_with_tail, fallback_ambiguous_ops[0]["hunks"], path="tail.py")
    assert exc_fb.value.details["reason"] == "ambiguous_context"
    assert exc_fb.value.details["match_count"] == 2


def test_s05_i2_t1_preserves_per_line_lf_crlf_cr_and_missing_final_eol():
    """T1 (Changes 1, 2, 3): pure CRLF, pure LF, mixed LF+CRLF, and isolated CR files keep every other line's exact bytes and final-EOL absence."""
    lines, endings, preferred = local_patch._split_keep_endings("a\r\nb\nc\rd")
    assert lines == ["a", "b", "c", "d"]
    assert endings == ["\r\n", "\n", "\r", ""]
    assert preferred == "\r\n"
    assert local_patch._split_keep_endings("") == ([], [], "\n")
    assert local_patch._split_keep_endings("no_eol") == (["no_eol"], [""], "\n")

    single_edit_hunks = local_patch.parse(
        _patch(
            "*** Update File: sample.txt",
            "@@",
            " line2",
            "-line3",
            "+LINE3",
            " line4",
        )
    )[0]["hunks"]

    cases = [
        # 1. Pure CRLF
        (
            b"line1\r\nline2\r\nline3\r\nline4\r\n",
            b"line1\r\nline2\r\nLINE3\r\nline4\r\n",
        ),
        # 2. Pure LF
        (
            b"line1\nline2\nline3\nline4\n",
            b"line1\nline2\nLINE3\nline4\n",
        ),
        # 3. Mixed LF + CRLF (first ending LF, majority CRLF, context has CRLF, untouched line has CRLF)
        (
            b"line1\nline2\r\nline3\r\nline4\r\nline5\n",
            b"line1\nline2\r\nLINE3\nline4\r\nline5\n",
        ),
        # 4. Isolated CR (pure CR and mixed CR + CRLF + LF without trailing newline)
        (
            b"line1\rline2\rline3\rline4\r",
            b"line1\rline2\rLINE3\rline4\r",
        ),
        (
            b"line1\r\nline2\rline3\nline4\rline5",
            b"line1\r\nline2\rLINE3\r\nline4\rline5",
        ),
    ]
    for before_bytes, expected_bytes in cases:
        after_text = local_patch.apply_hunks(before_bytes.decode("utf-8"), single_edit_hunks, path="sample.txt")
        assert after_text.encode("utf-8") == expected_bytes

    # Replacing the final line of a file that had no trailing newline keeps no trailing newline.
    tail_hunks = local_patch.parse(
        _patch(
            "*** Update File: tail_no_eol.txt",
            "@@",
            " keep_crlf",
            "-last_no_eol",
            "+LAST_NO_EOL",
        )
    )[0]["hunks"]
    res_no_eol = local_patch.apply_hunks("head_lf\nkeep_crlf\r\nlast_no_eol", tail_hunks, path="tail_no_eol.txt")
    assert res_no_eol.encode("utf-8") == b"head_lf\nkeep_crlf\r\nLAST_NO_EOL"


def test_s05_i2_t2_added_lines_use_first_line_ending_preferred():
    """T2 (Changes 1, 2, 3): newly added '+' lines use the file's first line ending (preferred), defaulting to '\\n' when none exists."""
    add_hunks = local_patch.parse(
        _patch(
            "*** Update File: mixed.txt",
            "@@",
            " second",
            "+inserted_a",
            "+inserted_b",
            " third",
        )
    )[0]["hunks"]

    # First ending is CRLF even though remaining lines are LF -> '+' lines get CRLF, existing LF lines stay LF.
    crlf_first = "first\r\nsecond\nthird\nfourth\n"
    assert local_patch.apply_hunks(crlf_first, add_hunks).encode("utf-8") == (
        b"first\r\nsecond\ninserted_a\r\ninserted_b\r\nthird\nfourth\n"
    )

    # First ending is LF even though remaining lines are CRLF -> '+' lines get LF, existing CRLF lines stay CRLF.
    lf_first = "first\nsecond\r\nthird\r\nfourth\r\n"
    assert local_patch.apply_hunks(lf_first, add_hunks).encode("utf-8") == (
        b"first\nsecond\r\ninserted_a\ninserted_b\nthird\r\nfourth\r\n"
    )

    # First ending is isolated CR -> '+' lines get CR.
    cr_first = "first\rsecond\nthird\r\n"
    assert local_patch.apply_hunks(cr_first, add_hunks).encode("utf-8") == (
        b"first\rsecond\ninserted_a\rinserted_b\rthird\r\n"
    )

    # Single-line file without any line ending defaults preferred to '\n' for interior separation and keeps no final EOL.
    append_hunks = local_patch.parse(
        _patch(
            "*** Update File: single.txt",
            "@@",
            " only_line",
            "+added_line",
        )
    )[0]["hunks"]
    assert local_patch.apply_hunks("only_line", append_hunks).encode("utf-8") == b"only_line\nadded_line"


def test_s05_i3_t1_lenient_envelope_unwraps_heredocs_and_code_fences_and_rejects_malformed():
    """T1 (Change 1): unwraps 3 heredoc quote forms (with/without apply_patch prefix) and ```diff fences; rejects unclosed fence and <4 lines with unwrap note."""
    inner_patch = "\n".join([
        "*** Begin Patch",
        "*** Update File: app.py",
        "@@",
        "-x = 1",
        "+x = 2",
        "*** End Patch",
    ])

    # 1. Three heredoc quote variants (bare and with 'apply_patch ' prefix) + 2. ```diff / ```patch / ``` fences
    valid_wrapped = [
        f"<<EOF\n{inner_patch}\nEOF",
        f"<<'EOF'\n{inner_patch}\nEOF",
        f'<<"EOF"\n{inner_patch}\nEOF',
        f"apply_patch <<EOF\n{inner_patch}\nEOF",
        f"apply_patch <<'EOF'\n{inner_patch}\nEOF",
        f'apply_patch <<"EOF"\n{inner_patch}\nEOF',
        f"```diff\n{inner_patch}\n```",
        f"```\n{inner_patch}\n```",
    ]
    for wrapped in valid_wrapped:
        ops = local_patch.parse(wrapped)
        assert len(ops) == 1
        assert ops[0]["kind"] == "update" and ops[0]["path"] == "app.py"
        assert local_patch.apply_hunks("x = 1\n", ops[0]["hunks"]) == "x = 2\n"

    # 3. Opening fence / heredoc without matching closing line -> still rejected as invalid_envelope
    unclosed_cases = [
        f"```diff\n{inner_patch}",
        f"<<'EOF'\n{inner_patch}",
        f"{inner_patch}\n```",
    ]
    for bad in unclosed_cases:
        with pytest.raises(AgentToolRejected, match="agent_patch_invalid_envelope") as exc_info:
            local_patch.parse(bad)
        assert "heredoc" in exc_info.value.correction.lower() or "```" in exc_info.value.correction

    # 4. Fewer than 4 lines -> still rejected even with heredoc or fence markers
    short_cases = [
        "<<EOF\n*** Begin Patch\nEOF",
        "<<'EOF'\n*** End Patch\nEOF",
        "```diff\n*** Begin Patch\n```",
        "```diff\n```",
        "<<EOF\nEOF",
    ]
    for short in short_cases:
        assert len(short.splitlines()) < 4
        with pytest.raises(AgentToolRejected, match="agent_patch_invalid_envelope") as exc_short:
            local_patch.parse(short)
        assert "heredoc" in exc_short.value.correction.lower() or "```" in exc_short.value.correction
