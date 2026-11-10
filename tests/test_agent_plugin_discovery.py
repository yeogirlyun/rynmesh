"""Plugin discovery stays visible and carries live capabilities through the runtime."""

import json

import pytest
from test_agent_integrations import node as node
from test_agent_runtime import advance, assistant, make_runtime
from test_craft_plugins import manager as manager

from rynmesh.agent_runtime import tool_menu
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.store import encoded
from rynmesh.node_apps.plugin_agent import ALL_INFO, ALL_LIST


def plugin_runtime(tmp_path, node, manager, *responses, context_limit=120000):  # noqa: F811 - imported pytest fixture parameter
    app, store = node
    app.state.craft_plugins = manager
    _, tools, _ = build_agent_integrations(app, store, store.home)
    runtime, model, _, _ = make_runtime(tmp_path / "agent", *responses)
    runtime.tools = tools
    runtime.update_settings({"max_context_chars": context_limit, "permission_mode": "read_only", "desktop_access": False})
    return runtime, model, tools


def tool_data(call, call_id):
    return json.loads(next(message["content"] for message in call[1]
                           if message.get("tool_call_id") == call_id and message["role"] == "tool"))


@pytest.mark.parametrize("context_limit", [12000, 120000])
def test_current_apps_first_menu_and_discovered_info_use_live_read_only_tools(tmp_path, node, manager, context_limit):  # noqa: F811 - imported pytest fixture
    manager.install("wordcraft")
    runtime, model, _ = plugin_runtime(tmp_path, node, manager,
        assistant(name=ALL_LIST, call_id="plugins"),
        assistant(name=tool_menu.NAME, args={"load": [ALL_INFO]}, call_id="load-info"),
        assistant(name=ALL_INFO, args={"app": "wordcraft"}, call_id="plugin-info"),
        assistant("Word 文档应用已启用；当前接入仅支持查询编辑器信息。"),
        context_limit=context_limit)
    task = runtime.create({"message": "你查一下我目前有的应用，说一下你能做什么"})
    advance(runtime, 10)

    first_names = [spec["function"]["name"] for spec in model.calls[0][2]]
    assert ALL_LIST in first_names and tool_menu.NAME in first_names
    assert ALL_INFO not in first_names  # The real registry uses bounded discovery.
    if "ryn_shell_execute" in first_names:
        assert first_names.index(ALL_LIST) < first_names.index("ryn_shell_execute")
    inventory = tool_data(model.calls[1], "plugins")
    assert inventory["scope"] == "ryn_enabled_plugins" and inventory["includes_os_installed_apps"] is False
    listed = inventory["plugins"]
    assert [plugin["id"] for plugin in listed] == ["wordcraft"]
    assert listed[0]["agent_tools"] == ["editor_info"]
    assert listed[0]["editing_supported"] is False
    assert listed[0]["execution_tool"] == ALL_INFO and listed[0]["document_editing"] == "not_integrated"
    loaded = tool_data(model.calls[2], "load-info")
    assert ALL_INFO in loaded["loaded_names"]
    assert ALL_INFO in {spec["function"]["name"] for spec in model.calls[2][2]}
    info = tool_data(model.calls[3], "plugin-info")
    assert info["app"] == "wordcraft" and info["document_editing"] == "not_integrated"
    assert info["editor_opened"] is False
    done = runtime.get(task["id"])
    assert done["status"] == "completed" and done["pending_approval"] is None
    assert not done["local_execution"]["desktop_access"]
    assert {event["tool"] for event in done["events"] if event["type"] == "tool_started"} <= {ALL_LIST, ALL_INFO, tool_menu.NAME}
    assert all(len(encoded(call[1])) + len(encoded(call[2])) <= context_limit for call in model.calls)


def test_runtime_plugin_catalog_tracks_disable_and_reenable_without_desktop_access(tmp_path, node, manager):  # noqa: F811 - imported pytest fixture
    manager.install("wordcraft")
    runtime, model, tools = plugin_runtime(tmp_path, node, manager,
        assistant(name=ALL_LIST, call_id="enabled"), assistant("已启用 Word 文档。"),
        assistant(name=ALL_LIST, call_id="disabled"), assistant("目前没有启用的应用。"),
        assistant(name=ALL_LIST, call_id="reenabled"), assistant("Word 文档已重新启用。"))

    for enabled, call_id, expected in [(True, "enabled", ["wordcraft"]), (False, "disabled", []),
                                       (True, "reenabled", ["wordcraft"])]:
        manager.set_enabled("wordcraft", enabled)
        task = runtime.create({"message": "查看 Ryn 当前应用及 Agent 能力"})
        advance(runtime, 5)
        assert runtime.get(task["id"])["status"] == "completed"
        inventory = tool_data(model.calls[-1], call_id)
        assert inventory["scope"] == "ryn_enabled_plugins" and inventory["includes_os_installed_apps"] is False
        plugins = inventory["plugins"]
        assert [plugin["id"] for plugin in plugins] == expected
        info_specs = [spec for spec in tools.specs() if spec["function"]["name"] == ALL_INFO]
        assert bool(info_specs) is enabled
        if enabled:
            assert info_specs[0]["function"]["parameters"]["properties"]["app"]["enum"] == ["wordcraft"]
        assert runtime.get(task["id"])["pending_approval"] is None
    assert len(model.calls) == 6
