"""Exact file grants do not confer authority over parents, siblings or children."""
import copy

import pytest
from test_agent_cloud_resources import cloud  # noqa: F401
from test_agent_resource_writes import writable  # noqa: F401
from test_agent_resources import resources  # noqa: F401

from rynmesh.agent_runtime.store import AgentToolRejected


def file_scope(tools, context, connection, path):
    scope = tools.prepare({"resources": [{"connection_id": connection, "root_path": path, "root_kind": "file",
                                         "actions": ["read_ai"], "recursive": False}], "allow_model_data": True},
        owner_id=context["owner_id"], consumer_node_id=context["consumer_node_id"], model_destination=context["model_destination"])
    return {**context, **scope, "resource_origins": []}


def read(tools, context, ref=None):
    return tools.execute("ryn_read_resource", {"resource_ref": ref or context["resource_grants"][0]["root_resource_ref"]},
                         context=context, cancelled=lambda: False)


def test_path_file_root_reads_directly_but_never_lists_or_expands(resources):  # noqa: F811
    tools, _, context, state = resources
    context = file_scope(tools, context, "docs", "allowed/notes.md")
    roots = tools.execute("ryn_list_resource_roots", {}, context=context, cancelled=lambda: False)["roots"]
    assert roots[0]["root_kind"] == "file" and roots[0]["directory"] is False
    ref = roots[0]["resource_ref"]
    assert read(tools, context)["text"] == state["body"].decode()
    with pytest.raises(AgentToolRejected):
        tools.execute("ryn_list_resources", {"resource_ref": ref}, context=context, cancelled=lambda: False)
    for path in ("allowed", "allowed/sibling.md", "allowed/notes.md/child.md"):
        forged = tools._seal({**tools._open(ref), "path": path})
        with pytest.raises(AgentToolRejected):
            read(tools, context, forged)
    assert state["reads"] == ["allowed/notes.md"]


@pytest.mark.parametrize("change", ["content", "type", "size"])
def test_path_file_old_body_cannot_leave_after_version_or_type_change(resources, change):  # noqa: F811
    tools, _, context, state = resources
    context = file_scope(tools, context, "docs", "allowed/notes.md")
    result = read(tools, context)
    context["resource_origins"] = [result["origin"]]
    tools.validate_context(context)
    if change == "content":
        state["body"] = b"X" * len(state["body"])  # Same metadata size, different bytes.
    elif change == "size":
        state["body"] += b" changed"
    else:
        state["name"] = "elsewhere.md"
    with pytest.raises(AgentToolRejected):
        tools.validate_context(context)


@pytest.mark.parametrize("changes", [{"actions": ["list", "read_ai"]}, {"recursive": True},
    {"actions": ["create_file"]}, {"root_path": "allowed/child"}])
def test_file_scope_cannot_be_directory_or_write_capability(resources, changes):  # noqa: F811
    tools, _, context, _ = resources
    selected = {"connection_id": "docs", "root_path": "allowed/notes.md", "root_kind": "file",
                "recursive": False, "actions": ["read_ai"], **changes}
    with pytest.raises(AgentToolRejected):
        tools.prepare({"resources": [selected], "allow_model_data": True}, owner_id="local_owner", consumer_node_id="node",
                      model_destination=context["model_destination"])


def test_cloud_file_root_pins_actual_type_ancestry_and_version(cloud):  # noqa: F811
    tools, context, state = cloud
    context = file_scope(tools, context, "cloud", "FolderA/FileF")
    result = read(tools, context)
    assert result["text"] == "Verified cloud source text"
    context["resource_origins"] = [result["origin"]]
    tools.validate_context(context)
    for change in ({"version": "changed"}, {"parent": "Other"}, {"folder": True}, {"shortcut": True}):
        before = copy.deepcopy(state["items"]["FileF"])
        state["items"]["FileF"].update(change)
        with pytest.raises(AgentToolRejected):
            tools.validate_context(context)
        state["items"]["FileF"] = before


def test_cloud_directory_cannot_be_authorized_as_file(cloud):  # noqa: F811
    tools, context, _ = cloud
    with pytest.raises(AgentToolRejected, match="agent_scope_file_required"):
        file_scope(tools, context, "cloud", "FolderA")


def test_actual_dav_file_grant_readback_and_same_size_replacement(writable):  # noqa: F811
    business, context, _, _, source, server, root, _, _ = writable
    (root / "allowed/only.md").write_bytes(b"EXACT FILE BODY")
    (root / "allowed/private.md").write_bytes(b"NEVER SENT")
    tools = business.resources
    context = file_scope(tools, context, source["id"], "allowed/only.md")
    result = read(tools, context)
    assert result["text"] == "EXACT FILE BODY"
    assert "private.md" not in str(result)
    assert {entry["path"] for entry in server.audit if entry["method"] == "GET"} == {"allowed/only.md"}
    context["resource_origins"] = [result["origin"]]
    (root / "allowed/only.md").write_bytes(b"CHANGED CONTENT")
    with pytest.raises(AgentToolRejected, match="agent_resource_version_changed"):
        tools.validate_context(context)


def test_actual_dav_file_timeout_has_safe_read_receipt_and_no_parent_authority(writable):  # noqa: F811
    import httpx

    from rynmesh.agent_runtime.store import AgentReadFailure
    business, context, _, nas, source, server, root, _, _ = writable
    (root / "allowed/supplement.md").write_bytes(b"Delayed fixture")
    tools = business.resources
    context = file_scope(tools, context, source["id"], "allowed/supplement.md")
    nas.sessions[source["id"]][1].client.timeout = httpx.Timeout(0.1)
    entered, release = server.arm("GET", "allowed/supplement.md", "barrier_before_io")
    try:
        with pytest.raises(AgentReadFailure, match="resource_read_timeout"):
            read(tools, context)
        assert entered.is_set()
    finally:
        release.set()
