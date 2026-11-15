"""Overhaul S04-I12 tests: destructive-operation and Windows safety rules in ryn_shell_execute description."""
from __future__ import annotations

import copy
import os
import runpy
from pathlib import Path

from rynmesh.agent_runtime import code_mode, execution_policy, local_specs, tool_menu
from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.loop import ALL_INTERNAL_SPECS
from rynmesh.llm_package import codex_tools


def _eval_specs_with_os_name(monkeypatch, os_name: str) -> dict:
    monkeypatch.setattr(os, "name", os_name)
    ns = runpy.run_path(str(Path(local_specs.__file__)))
    return ns["SPECS"]


def test_t1_shell_description_contains_universal_destructive_safety_rules():
    """T1: ryn_shell_execute description includes universal pre-delete check, root/glob guards, and post-delete reporting (Step 1)."""
    description, effect, risk, _schema = SPECS["ryn_shell_execute"]
    assert effect == "write"
    assert risk == "high"
    assert "Before deleting or overwriting" in description
    assert "read-only check" in description
    assert "drive root" in description
    assert "home directory" in description
    assert "workspace root" in description
    assert "unexpanded variables or globs" in description
    assert "state what was deleted" in description


def test_t2_windows_branch_includes_literalpath_and_hidden_and_posix_omits_them(monkeypatch):
    """T2: os.name == 'nt' includes -LiteralPath and -WindowStyle Hidden rules; non-Windows omits them (Step 2)."""
    win_specs = _eval_specs_with_os_name(monkeypatch, "nt")
    win_desc = win_specs["ryn_shell_execute"][0]
    assert "Remove-Item" in win_desc
    assert "Move-Item -LiteralPath" in win_desc
    assert "-LiteralPath" in win_desc
    assert "resolved absolute path" in win_desc
    assert "Start-Process" in win_desc
    assert "-WindowStyle Hidden" in win_desc

    posix_specs = _eval_specs_with_os_name(monkeypatch, "posix")
    posix_desc = posix_specs["ryn_shell_execute"][0]
    # Universal destructive safety guidance stays present on POSIX:
    assert "Before deleting or overwriting" in posix_desc
    assert "workspace root" in posix_desc
    # Windows-only rules are absent on POSIX:
    assert "-LiteralPath" not in posix_desc
    assert "-WindowStyle Hidden" not in posix_desc
    assert "Start-Process" not in posix_desc


def test_t3_schema_projection_consistency_and_automatic_mode_never_asks(tmp_path):
    """T3: Projected tool schema stays valid/unchanged in parameters, reflects updated description, and adds no approval prompt in automatic mode (Steps 1, 2, 3)."""
    description, effect, risk, params = SPECS["ryn_shell_execute"]
    assert set(params["properties"]) == {
        "command", "shell", "cwd", "timeout_seconds", "background",
        "yield_seconds", "tty", "rows", "cols",
    }
    assert params["required"] == ["command"]
    assert params["additionalProperties"] is False

    strict = codex_tools._strict_parameters(copy.deepcopy(params))
    assert strict["type"] == "object"
    assert "command" in strict["properties"]

    # Step 3: automatic mode remains full-auto without asking on destructive shell commands,
    # and description does not instruct the model to ask the user first.
    lowered = description.lower()
    assert "ask the user" not in lowered
    assert "ask for confirmation" not in lowered
    assert "Before deleting or overwriting" in description

    row = {
        "permission_mode": "automatic",
        "local_execution": {
            "enabled": True,
            "workspace_root": str(tmp_path),
            "desktop_access": False,
        },
    }
    settings = {
        "permission_mode": "automatic",
        "desktop_access": False,
    }
    for cmd in (
        "Remove-Item -LiteralPath build -Recurse -Force",
        "rm -rf dist",
    ):
        call = {"name": "ryn_shell_execute", "arguments": {"command": cmd}}
        decision = execution_policy.decide(call, {"effect": effect, "risk": risk}, row, settings)
        assert decision.action == "execute"
        assert decision.basis == "automatic"

    # Expanded shell description must not crowd CORE_TOOLS (such as ryn_file_write)
    # out of additive-window tool menu projections alongside ALL_INTERNAL_SPECS.
    business = [
        {"type": "function", "function": {"name": name, "description": desc, "parameters": schema}}
        for name, (desc, _eff, _risk, schema) in SPECS.items()
    ]
    business.append(code_mode.SPEC)
    business.append(
        {
            "type": "function",
            "function": {
                "name": "ryn_list_plugins",
                "description": "List installed plugins.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    )
    pinned_request = (
        tool_menu.PLUGIN_DISCOVERY_TOOLS
        + ("ryn_project_inspect",)
        + tool_menu.CORE_TOOLS
        + ("ryn_code_exec",)
    )
    for window_bytes in (60000, 84480):
        exposed, menu_state = tool_menu.project(
            business,
            ALL_INTERNAL_SPECS,
            {},
            max_context_bytes=window_bytes,
            authority_bytes=8856,
            epoch=0,
            goal_revision=1,
            pinned_names=pinned_request,
        )
        exposed_names = {spec["function"]["name"] for spec in exposed}
        assert set(tool_menu.CORE_TOOLS) <= set(menu_state["pinned_names"])
        assert "ryn_file_write" in exposed_names
        assert "ryn_shell_execute" in exposed_names
