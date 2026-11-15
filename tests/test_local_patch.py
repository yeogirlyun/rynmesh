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
