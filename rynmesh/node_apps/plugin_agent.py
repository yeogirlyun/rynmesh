"""Typed Craft discovery; no arbitrary URL, shell, installation or JS bridge.

Real document commands must add a reviewed adapter and mechanical output check.
The current pinned office builds expose no verified Ryn document-editing API.
"""

import copy

from ..agent_runtime.store import AgentToolRejected
from .builtin_plugins import BUSINESS
from .catalog_data import CATALOG
from .craft_agent import ACTION as CRAFT_ACTION
from .craft_agent import INSTALL as CRAFT_INSTALL
from .craft_agent import READ as CRAFT_READ
from .craft_agent import TOOLS as CRAFT_TOOLS
from .craft_agent_policy import OUTPUT_FORMATS
from .craft_catalog import CRAFT
from .craft_host import CraftError
from .engine import AppError
from .plugins import WARNINGS

LIST = "ryn_list_craft_plugins"
INFO = "ryn_craft_editor_info"
ALL_LIST = "ryn_list_plugins"
ALL_INFO = "ryn_plugin_info"
NAMES = {LIST, INFO, ALL_LIST, ALL_INFO}
SPECS = [
    {"type": "function", "function": {
        "name": LIST,
        "description": "List enabled, owner-installed Craft plugins and their actual Agent tools. No installation, launch, document editing or private file access. Plugins are available to Agent by default when enabled.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": INFO,
        "description": "Get verified pinned version, upstream source and claimed formats of an enabled Craft editor. This information read does not open an editor or process documents. Never claim a document was edited or exported from this result.",
        "parameters": {"type": "object", "properties": {"app": {"type": "string", "maxLength": 32}},
                       "required": ["app"], "additionalProperties": False},
    }},
]
SPECS += [
    {"type": "function", "function": {
        "name": ALL_LIST,
        "description": "Discover the user's apps on the Ryn Applications page: enabled plugins and their actual Agent capabilities, including Craft editors, Resume, Recruit, Desk and NAS. This is the Ryn app inventory, separate from OS-installed software. App operations keep their approval boundary; NAS keeps resource permissions. No installation or launch.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": ALL_INFO,
        "description": "Inspect one verified enabled local plugin. For Resume, Recruit and Desk returns exact operation schemas and fresh workspace_context; copy that context into ryn_node_app_action.reviewed_context. NAS returns exact resource tools and scope guidance. Craft metadata does not edit documents or open an editor.",
        "parameters": {"type": "object", "properties": {"app": {"type": "string", "maxLength": 32}},
                       "required": ["app"], "additionalProperties": False},
    }},
]


class PluginTools:
    def __init__(self, app):
        self.app = app

    @property
    def manager(self):
        value = getattr(self.app.state, "craft_plugins", None)
        if value is None:
            raise AgentToolRejected("plugin_unavailable")
        return value

    def specs(self):
        if not getattr(self.app.state, "craft_plugins", None):
            return []
        catalog = self.manager.catalog()
        enabled = [p["id"] for p in catalog["plugins"] if p["enabled"]]
        editors = [slug for slug in enabled if slug in CRAFT]
        specs = []
        for original in SPECS:
            name = original["function"]["name"]
            choices = editors if name == INFO else enabled
            if name in {INFO, ALL_INFO} and not choices:
                continue
            spec = copy.deepcopy(original)
            if name in {INFO, ALL_INFO}:
                spec["function"]["parameters"]["properties"]["app"]["enum"] = choices
            specs.append(spec)
        return specs

    def execute(self, name, args, cancelled):
        if cancelled():
            raise AgentToolRejected("agent_cancelled")
        try:
            if name in {LIST, ALL_LIST}:
                plugins = []
                for row in self.manager.catalog()["plugins"]:
                    if not row["enabled"] or name == LIST and row["id"] not in CRAFT:
                        continue
                    item = {key: row[key] for key in ("id", "name", "version", "kind", "agent_tools", "editing_supported")}
                    if row["id"] in CRAFT:
                        item.update(discovery_tool=CRAFT_TOOLS if row.get("automation_supported") else ALL_INFO,
                                    execution_tool=CRAFT_ACTION if row["editing_supported"] else ALL_INFO,
                                    document_editing="task_headless_document" if row["editing_supported"] else "not_integrated")
                        if row.get("automation_supported") and not row["editing_supported"]:
                            item["setup_tool"] = CRAFT_INSTALL
                    else:
                        item.update(discovery_tool=ALL_INFO,
                                    execution_tool="ryn_node_app_action" if row["id"] in BUSINESS else "ryn_list_resource_roots")
                    plugins.append(item)
                if name == LIST:
                    return {"plugins": plugins}
                return {"scope": "ryn_enabled_plugins", "includes_os_installed_apps": False, "plugins": plugins}
            if name == INFO and args["app"] not in CRAFT:
                raise AgentToolRejected("agent_tool_arguments_invalid")
            with self.manager.lease(args["app"]) as spec:
                if cancelled():
                    raise AgentToolRejected("agent_cancelled")
                if args["app"] not in CRAFT:
                    row = next(p for p in self.manager.catalog()["plugins"] if p["id"] == args["app"])
                    info = {"app": row["id"], "version": row["version"], "sha256": row["sha256"],
                            "kind": row["kind"], "capabilities": row["agent_tools"],
                            "execution_tool": "ryn_node_app_action" if row["id"] in BUSINESS else "ryn_list_resource_roots",
                            "operation_approval_required": row["id"] in BUSINESS,
                            "resource_permissions_required": row["id"] == "nas",
                            "editor_opened": False, "source": spec["source"]}
                    if row["id"] in BUSINESS:
                        service = getattr(self.app.state, "node_apps", None)
                        if service is None:
                            raise AgentToolRejected("agent_node_apps_unavailable")
                        info.update(operations=copy.deepcopy(CATALOG[row["id"]]["operations"]),
                                    workspace_context=service.call("", "context", {"app": row["id"]}),
                                    operation_guidance="Load ryn_node_app_action if absent. Choose an operation and arguments from operations; copy this complete workspace_context into reviewed_context, and use this app. The runtime stages exact owner approval, including private reads and model data release. Fetch this info again after a write or apps_review_changed; never invent or reuse stale context.")
                    else:
                        info.update(execution_tools=copy.deepcopy(row["agent_tools"]),
                                    scope_guidance="Load and call ryn_list_resource_roots first. Only returned task-authorized resource_ref values grant access. If roots is empty, ask the owner to select NAS files or directories in the Agent resource scope and allow data release to the selected model. Use ryn_list_resources, ryn_read_resource and ryn_search_resource within those grants; create tools additionally require a writable create grant and operation approval. Enabling NAS alone grants no resource access.")
                    return info
                row = next(p for p in self.manager.catalog()["plugins"] if p["id"] == args["app"])
                ready = bool(row["editing_supported"])
                return {"app": args["app"], "version": spec["tag"], "sha256": spec["web_sha256"],
                        "formats": spec["formats"], "formats_verified": False,
                        "source_url": f"https://github.com/storytold/{args['app']}/tree/{spec['tag']}",
                        "document_editing": "task_headless_document" if ready else "not_integrated", "editor_opened": False,
                        "tools": row["agent_tools"], "editing_supported": ready,
                        **({"tool_discovery": CRAFT_TOOLS, "read_tool": CRAFT_READ, "action_tool": CRAFT_ACTION,
                            "setup_tool": CRAFT_INSTALL, "component_ready": ready,
                            "output_formats": sorted(OUTPUT_FORMATS[args["app"]]),
                            "document_guidance": "Discover exact schemas with ryn_craft_tools before calling read/action. Open or create a document in this task's separate headless session. Only workspace files are accessible; save to an explicit new path and read back. Confirmed output files include hashes and reopen evidence. The user's existing editor window and its unsaved content are separate. No desktop permission is required. Install the pinned component only if requested work needs it and after the existing exact approval."} if row.get("automation_supported") else {}),
                        "warnings": [WARNINGS[args["app"]]] if args["app"] in WARNINGS else []}
        except (CraftError, AppError) as exc:
            raise AgentToolRejected(str(exc)) from None
