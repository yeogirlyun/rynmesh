"""Typed Craft discovery; no arbitrary URL, shell, installation or JS bridge.

Real document commands must add a reviewed adapter and mechanical output check.
The current pinned office builds expose no verified Ryn document-editing API.
"""

import copy

from ..agent_runtime.store import AgentToolRejected
from .builtin_plugins import BUSINESS
from .craft_catalog import CRAFT
from .craft_host import CraftError
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
        "description": "List enabled local plugins and their actual capabilities: Craft editors, Resume, Recruit, Desk and NAS. Enabled plugins are available to Agent by default. App operations keep their exact approval boundary; NAS keeps resource permissions. No installation, native launch or shell access.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": ALL_INFO,
        "description": "Inspect one verified enabled local plugin. Business operations use ryn_node_app_action and its reviewed workspace context/approval. NAS uses authorized resource tools. Craft metadata does not edit documents or open an editor.",
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
                return {"plugins": [{k: p[k] for k in ("id", "name", "version", "kind", "agent_tools", "editing_supported")}
                                    for p in self.manager.catalog()["plugins"] if p["enabled"] and (name == ALL_LIST or p["id"] in CRAFT)]}
            if name == INFO and args["app"] not in CRAFT:
                raise AgentToolRejected("agent_tool_arguments_invalid")
            with self.manager.lease(args["app"]) as spec:
                if cancelled():
                    raise AgentToolRejected("agent_cancelled")
                if args["app"] not in CRAFT:
                    row = next(p for p in self.manager.catalog()["plugins"] if p["id"] == args["app"])
                    return {"app": row["id"], "version": row["version"], "sha256": row["sha256"],
                            "kind": row["kind"], "capabilities": row["agent_tools"],
                            "execution_tool": "ryn_node_app_action" if row["id"] in BUSINESS else "authorized_resource_tools",
                            "operation_approval_required": row["id"] in BUSINESS,
                            "resource_permissions_required": row["id"] == "nas",
                            "editor_opened": False, "source": spec["source"]}
                return {"app": args["app"], "version": spec["tag"], "sha256": spec["web_sha256"],
                        "formats": spec["formats"], "formats_verified": False,
                        "source_url": f"https://github.com/storytold/{args['app']}/tree/{spec['tag']}",
                        "document_editing": "not_integrated", "editor_opened": False,
                        "tools": ["editor_info"], "warnings": [WARNINGS[args["app"]]] if args["app"] in WARNINGS else []}
        except CraftError as exc:
            raise AgentToolRejected(str(exc)) from None
