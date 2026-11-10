import copy
from types import SimpleNamespace

import pytest

from rynmesh.agent_runtime.resources import ResourceTools
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.agent_runtime.tools import RynTools
from rynmesh.cloud_drives import GoogleConnector
from rynmesh.nas import NasPlugin
from rynmesh.onedrive import OneDriveConnector


@pytest.fixture(params=["google", "onedrive"])
def cloud(request, tmp_path, monkeypatch):
    protocol = request.param
    plugin = NasPlugin(tmp_path)
    plugin.state = {"enabled": True, "sources": [{"id": "cloud", "name": "Documents", "protocol": protocol,
                    "root": "", "writable": False, "allow_ai": True}]}
    plugin.save()
    items = {"root": {"id": "Root", "name": "Drive", "parent": None, "folder": True},
             "FolderA": {"id": "FolderA", "name": "Allowed", "parent": "Root", "folder": True},
             "Other": {"id": "Other", "name": "Other", "parent": "Root", "folder": True},
             "FileF": {"id": "FileF", "name": "notes.md", "parent": "FolderA", "folder": False}}
    state = {"items": items, "reads": 0, "move_on_read": False}

    def metadata(key):
        item = items[key]
        if protocol == "google":
            return {"id": item["id"], "name": item["name"], "parents": [item["parent"]] if item["parent"] else [],
                    "mimeType": "application/vnd.google-apps.shortcut" if item.get("shortcut") else
                        "application/vnd.google-apps.folder" if item["folder"] else "text/plain",
                    "version": item.get("version", "1"), **({"driveId": "foreign"} if item.get("foreign") else {})}
        return {"id": item["id"], "name": item["name"], "parentReference": {
                    "driveId": "foreign" if item.get("foreign") else "DriveD", **({"id": item["parent"]} if item["parent"] else {})},
                "folder" if item["folder"] else "file": {}, "eTag": item.get("version", "v1"),
                **({"remoteItem": {"id": "external"}} if item.get("shortcut") else {})}

    def provider(_connector, method, path, **kwargs):
        assert method == "GET"
        if path == "/files" or path.endswith("/children"):
            return {"files" if protocol == "google" else "value": [metadata("FileF")]}
        return metadata(path.rsplit("/", 1)[-1])

    def read(_connector, _path, _limit):
        state["reads"] += 1
        if state["move_on_read"]:
            items["FileF"]["parent"] = "Other"
        return b"Verified cloud source text"

    klass = GoogleConnector if protocol == "google" else OneDriveConnector
    monkeypatch.setattr(klass, "request", provider)
    monkeypatch.setattr(klass, "read", read)
    tools = ResourceTools(SimpleNamespace(state=SimpleNamespace(nas_plugin=plugin)))
    destination = {"catalog_model_id": "model", "provider_peer_id": "", "source_id": "source", "destination_revision": "fixture-generation"}
    scope = tools.prepare({"resources": [{"connection_id": "cloud", "root_path": "FolderA", "recursive": True,
                          "actions": ["list", "read_ai"]}], "allow_model_data": True},
                         owner_id="local_owner", consumer_node_id="node", model_destination=destination)
    context = {**scope, "task_id": "task", "owner_id": "local_owner", "consumer_node_id": "node",
               "scope_revision": 1, "model_destination": destination, "resource_origins": []}
    return tools, context, state


def read_ref(tools, context):
    root = tools.execute("ryn_list_resource_roots", {}, context=context, cancelled=lambda: False)["roots"][0]["resource_ref"]
    return tools.execute("ryn_list_resources", {"resource_ref": root}, context=context, cancelled=lambda: False)["entries"][0]["resource_ref"]


def test_current_ancestry_allows_read_and_blocks_old_history_after_move(cloud):
    tools, context, state = cloud
    ref = read_ref(tools, context)
    result = tools.execute("ryn_read_resource", {"resource_ref": ref}, context=context, cancelled=lambda: False)
    assert result["text"] == "Verified cloud source text" and state["reads"] == 1
    context["resource_origins"].append(result["origin"])
    tools.validate_context(context)
    state["items"]["FileF"]["parent"] = "Other"
    with pytest.raises(AgentToolRejected, match="agent_resource_ancestry_changed"):
        tools.validate_context(context)


@pytest.mark.parametrize("change", ["moved", "shortcut", "foreign"])
def test_signed_child_reference_never_bypasses_current_parent_or_external_checks(cloud, change):
    tools, context, state = cloud
    ref = read_ref(tools, context)
    if change == "moved":
        state["items"]["FileF"]["parent"] = "Other"
    else:
        state["items"]["FileF"][change] = True
    with pytest.raises(AgentToolRejected, match="agent_resource_ancestry_changed"):
        tools.execute("ryn_read_resource", {"resource_ref": ref}, context=context, cancelled=lambda: False)
    assert state["reads"] == 0


def test_move_during_cloud_read_discards_downloaded_body(cloud):
    tools, context, state = cloud
    ref = read_ref(tools, context)
    state["move_on_read"] = True
    with pytest.raises(AgentToolRejected, match="agent_resource_ancestry_changed"):
        tools.execute("ryn_read_resource", {"resource_ref": ref}, context=context, cancelled=lambda: False)
    assert state["reads"] == 1


def test_root_move_and_missing_origin_hook_fail_closed(cloud):
    tools, context, state = cloud
    no_hook = copy.deepcopy(context)
    no_hook.pop("resource_origins")
    with pytest.raises(AgentToolRejected, match="agent_resource_origin_tracking_required"):
        tools.validate_context(no_hook)
    state["items"]["FolderA"]["parent"] = "Other"
    with pytest.raises(AgentToolRejected, match="agent_resource_ancestry_changed"):
        tools.validate_context(context)


def test_scope_prepare_validates_real_parent_not_forged_id_prefix(cloud):
    tools, context, state = cloud
    state["items"]["FileF"]["parent"] = "Other"
    with pytest.raises(AgentToolRejected, match="agent_resource_ancestry_changed"):
        tools.prepare({"resources": [{"connection_id": "cloud", "root_path": "FolderA/FileF", "actions": ["list"]}],
                       "allow_model_data": True}, owner_id="local_owner", consumer_node_id="node", model_destination=context["model_destination"])


def test_listed_child_move_is_blocked_before_any_file_read(cloud):
    tools, context, state = cloud
    root = tools.execute("ryn_list_resource_roots", {}, context=context, cancelled=lambda: False)["roots"][0]["resource_ref"]
    result = tools.execute("ryn_list_resources", {"resource_ref": root}, context=context, cancelled=lambda: False)
    context["resource_origins"] = [result["origin"]]
    tools.validate_context(context)
    state["items"]["FileF"]["parent"] = "Other"  # Parent folder's version deliberately unchanged.
    with pytest.raises(AgentToolRejected, match="agent_resource_ancestry_changed"):
        tools.validate_context(context)
    assert state["reads"] == 0


def test_stale_origin_not_accepted_with_empty_or_different_scope(cloud):
    tools, context, _state = cloud
    ref = read_ref(tools, context)
    result = tools.execute("ryn_read_resource", {"resource_ref": ref}, context=context, cancelled=lambda: False)
    context["resource_origins"] = [result["origin"]]
    context["resource_grants"] = []
    with pytest.raises(AgentToolRejected, match="agent_resource_origin_invalid"):
        tools.validate_context(context)


def test_cloud_result_overflow_never_returns_unattributed_preview(cloud, monkeypatch):
    resource_tools, context, _state = cloud
    tools = RynTools(resource_tools.app, SimpleNamespace(), {}, None)
    root = resource_tools.execute("ryn_list_resource_roots", {}, context=context, cancelled=lambda: False)["roots"][0]["resource_ref"]
    # Start with a real authorized cloud list, then exercise the output boundary
    # with worst-case opaque references and names; no private prefix may escape.
    result = resource_tools.execute("ryn_list_resources", {"resource_ref": root}, context=context, cancelled=lambda: False)
    result["entries"] = [{"name": "private-name-" + "字" * 128, "resource_ref": "x" * 4096} for _ in range(10)]
    monkeypatch.setattr(tools.resources, "execute", lambda *_args, **_kwargs: result)
    with pytest.raises(AgentToolRejected, match="^agent_resource_output_limit$"):
        tools.execute("ryn_list_resources", {"resource_ref": root}, operation_id="large-list", context=context, cancelled=lambda: False)
    assert not tools.completed
