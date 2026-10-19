"""Actual loopback HTTP/NAS/resource-tool integration, no real model inference."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from test_agent_webdav_fixture import FixtureDAV

from rynmesh.agent_runtime.store import AgentReadFailure, AgentToolRejected
from rynmesh.agent_runtime.tools import RynTools
from rynmesh.nas import NasPlugin


@pytest.fixture
def http_resources(tmp_path):
    root = tmp_path / "dav"
    root.mkdir()
    (root / ".ryn-acceptance-isolated").touch()
    (root / "allowed").mkdir()
    (root / "private").mkdir()
    text = "Bounded network evidence. " * 180
    (root / "allowed/source.md").write_text(text, encoding="utf-8")
    (root / "private/secret.md").write_text("Must never enter model data", encoding="utf-8")
    with FixtureDAV(root) as server:
        nas = NasPlugin(tmp_path / "node")
        nas.enable(True)
        source = nas.configure({"name": "Isolated DAV", "system": "generic", "protocol": "webdav", "url": server.url,
                                "username": "fixture", "password": "fixture", "allow_ai": True, "writable": False})
        tools = RynTools(SimpleNamespace(state=SimpleNamespace(nas_plugin=nas)), SimpleNamespace(), {}, None)
        destination = {"catalog_model_id": "fixture-api", "provider_peer_id": "fixture-node", "source_id": "fixture-source",
                       "destination_revision": "fixture-revision"}
        scope = tools.prepare_scope({"resources": [{"connection_id": source["id"], "root_path": "allowed", "actions": ["list", "read_ai"]}],
                                     "allow_model_data": True}, owner_id="local_owner", consumer_node_id="fixture-node", model_destination=destination)
        context = {**scope, "task_id": "task-http", "scope_revision": 1, "owner_id": "local_owner", "consumer_node_id": "fixture-node",
                   "model_destination": destination, "resource_origins": []}
        try:
            yield tools, nas, source, server, context, text
        finally:
            for source_id in list(nas.sessions):
                nas._close_session(source_id)


def call(tools, context, name, args, operation):
    return tools.execute(name, args, operation_id=operation, context=context, cancelled=lambda: False)


def reference(tools, context):
    root = call(tools, context, "ryn_list_resource_roots", {}, "roots")["roots"][0]["resource_ref"]
    entries = call(tools, context, "ryn_list_resources", {"resource_ref": root}, "list")["entries"]
    assert len(entries) == 1 and entries[0]["name"] == "source.md"
    return entries[0]["resource_ref"]


def test_real_http_read_continuation_preserves_origin_and_private_directory_boundary(http_resources):
    tools, _nas, _source, server, context, expected = http_resources
    ref = reference(tools, context)
    cursor, chunks = "", []
    for index in range(10):
        result = call(tools, context, "ryn_read_resource", {"resource_ref": ref, "cursor": cursor}, f"read-{index}")
        assert result["origin"]["resource_ref"] == ref
        context["resource_origins"].append(result["origin"])
        tools.validate_context(context)
        chunks.append(result["text"])
        cursor = result["next_cursor"]
        if not cursor:
            break
    assert "".join(chunks) == expected
    assert not any(e["mutation"] for e in server.audit)
    assert not any(e["method"] == "GET" and "private" in str(e) for e in server.audit)


def test_actual_http_timeout_retains_safe_connection_evidence(http_resources):
    import httpx

    tools, nas, source, server, context, _expected = http_resources
    ref = reference(tools, context)
    # Bound an actual network read; the server withholds its response rather than
    # substituting a synthetic exception in the connector or resource tool.
    nas.sessions[source["id"]][1].client.timeout = httpx.Timeout(0.1)
    entered, release = server.arm("GET", "allowed/source.md", "barrier_before_io")
    try:
        with pytest.raises(AgentReadFailure, match="^resource_read_timeout$") as failure:
            call(tools, context, "ryn_read_resource", {"resource_ref": ref}, "timeout-read")
        assert entered.is_set()
        assert failure.value.connection == {"connection_id": source["id"], "connection_name": "Isolated DAV"}
        assert "127.0.0.1" not in str(failure.value) and "fixture" not in str(failure.value)
    finally:
        release.set()
    assert not any(e["mutation"] for e in server.audit)


def test_real_http_inflight_revocation_discards_the_completed_read(http_resources):
    tools, nas, source, server, context, _expected = http_resources
    ref = reference(tools, context)
    entered, release = server.arm("GET", "allowed/source.md", "barrier_before_io")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call, tools, context, "ryn_read_resource", {"resource_ref": ref}, "inflight-read")
        try:
            assert entered.wait(5)
            nas.update_source(source["id"], {"allow_ai": False})
        finally:
            release.set()
        with pytest.raises(AgentToolRejected, match="agent_resource_ai_not_allowed"):
            future.result(timeout=10)
    assert not tools.completed
