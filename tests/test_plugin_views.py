"""Real sealed import/store/routes and transport dispatch, with synthetic tool results."""
import copy
import json
import threading
from datetime import date
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from rynmesh.agent_runtime.extensions import ExtensionImportManager, ExtensionStore
from rynmesh.agent_runtime.extensions.mcp_transport import McpSessionManager
from rynmesh.agent_runtime.extensions.routes import install_extension_routes
from rynmesh.agent_runtime.extensions.views import normalize_views, query_view, safe_path
from rynmesh.agent_runtime.store import AgentError
from rynmesh.node_auth import NodeAuth

VIEW = {"id": "table", "title": "Table", "mcpServer": "reader", "tool": "read_table",
        "inputs": [{"name": "startDate", "label": "Start", "type": "date", "required": True, "default": "month_start"},
                   {"name": "keyword", "label": "Search", "type": "text"}],
        "columns": [{"key": "名称", "label": "Name"}, {"key": "amount", "label": "Amount", "format": "money"}],
        "rowsPath": "data.rows", "summaryFields": [{"path": "data.summary.合计", "label": "Total", "format": "number"}],
        "pagination": {"pageArgument": "page", "sizeArgument": "pageSize", "path": "data.pagination", "pageSize": 20}}
SCHEMA = {"type": "object", "required": ["startDate", "page", "pageSize"], "additionalProperties": False,
          "properties": {"startDate": {"type": "string"}, "keyword": {"type": "string", "maxLength": 50},
                         "page": {"type": "integer", "minimum": 1}, "pageSize": {"const": 20}}}
RESULT = {"isError": False, "structuredContent": {"success": True, "data": {"rows": [{"名称": "Synthetic", "amount": 12}],
          "summary": {"合计": 12}, "pagination": {"page": 1, "pageSize": 20, "total": 1}}}}
CANARY = "synthetic_token_code_secret_do_not_echo"
PREFIX = "/api/local/agent/extensions/plugins/bundle/views/table/query"


def package(tmp_path, view=VIEW):
    path = tmp_path / "package"
    path.mkdir(exist_ok=True)
    (path / "plugin.json").write_text(json.dumps({"id": "bundle", "name": "Bundle", "version": "1",
        "ryn": {"views": [view]}}), encoding="utf-8")
    (path / "mcp.json").write_text(json.dumps({"mcpServers": {"reader": {
        "type": "http", "url": "https://view.example.test/mcp", "auth": {"mode": "none"}}}}), encoding="utf-8")
    return path


def plugin(store):
    return next(x for x in store.overview()["plugins"] if x["id"] == "bundle")


def install(store, tmp_path):
    importer = ExtensionImportManager(store)
    preview = importer.preview({"kind": "plugin", "path": str(package(tmp_path))})
    assert preview["can_import"]
    importer.commit(preview["preview_id"], expected_revision=preview["expected_revision"])
    store.set_plugin_enabled("bundle", True, expected_revision=plugin(store)["revision"])
    discover(store)
    return preview


def discover(store, *, schema=SCHEMA, read_only=True, name="read_table", confirm=True):
    row = store.get_mcp_server_internal("bundle-reader")
    result = store.record_mcp_discovery("bundle-reader", expected_revision=row["revision"], status="ready",
        discovered_tools=[{"name": name, "inputSchema": schema, "annotations": {"readOnlyHint": read_only}}])
    if confirm and read_only is True:
        # Exercise the existing owner confirmation against the exact discovery.
        candidate = next(tool for tool in result["tools"] if tool["name"] == name)
        store.apply_server_hints("bundle-reader", {"expected_revision": result["revision"],
            "instance_id": result["instance_id"], "discovery_revision": result["discovery_revision"],
            "tools": [{"name": name, "schema_sha256": candidate["schema_sha256"]}]})
    return result


class Session:
    def __init__(self, server, host):
        self.host = host
        self.closed = threading.Event()
        self.instance_id, self.config_generation, self.revision = (server[key] for key in ("instance_id", "config_generation", "revision"))

    def is_usable(self):
        return True

    def close(self):
        self.closed.set()

    def call_tool(self, name, arguments, *, before_dispatch, cancelled):
        if self.host.before:
            self.host.before()
        before_dispatch()
        assert not cancelled()
        self.host.sent.append((name, copy.deepcopy(arguments)))
        if self.host.after:
            self.host.after()
        if self.host.error:
            raise self.host.error
        return copy.deepcopy(self.host.result)


@pytest.fixture
def host(tmp_path, monkeypatch):
    store = ExtensionStore(tmp_path / "agent", X25519PrivateKey.generate())
    install(store, tmp_path)
    manager = McpSessionManager(store)
    value = SimpleNamespace(store=store, manager=manager, before=None, after=None, error=None,
                            result=copy.deepcopy(RESULT), sent=[], tmp_path=tmp_path)
    monkeypatch.setattr(manager, "_get_or_connect_enabled_session", lambda server, **kwargs: Session(server, value))
    app = FastAPI()
    auth = NodeAuth(tmp_path)
    def local_control(request):
        if not auth.authorize(client_host=request.client.host, headers=request.headers,
                cookie=request.cookies.get("ryn_session", "")).allowed:
            raise HTTPException(401, "local_control_unauthorized")
    install_extension_routes(app, runtime=SimpleNamespace(), extensions=value, local_control=local_control)
    value.client = TestClient(app)
    yield value
    value.client.close()
    manager.close()


def body(host, **updates):
    p = plugin(host.store)
    return {"arguments": {"startDate": "2026-10-01"}, "page": 1,
            "expected_instance_id": p["instance_id"], "expected_revision": p["revision"], **updates}


def query(host, payload=None):
    return query_view(host.store, host.manager, "bundle", "table", payload if payload is not None else body(host))


def test_real_import_resolves_only_package_key_and_keeps_disabled(tmp_path):
    store = ExtensionStore(tmp_path / "agent", X25519PrivateKey.generate())
    importer = ExtensionImportManager(store)
    preview = importer.preview({"kind": "plugin", "path": str(package(tmp_path))})
    assert preview["plugins"][0]["views"][0]["mcpServer"] == "bundle-reader"
    imported = importer.commit(preview["preview_id"], expected_revision=preview["expected_revision"])
    p = next(x for x in imported["plugins"] if x["id"] == "bundle")
    assert p["views"] == preview["plugins"][0]["views"] and not p["enabled"]
    assert p["views"][0]["summaryFields"][0]["path"] == "data.summary.合计"
    with pytest.raises(AgentError, match="agent_plugin_disabled"):
        store.get_plugin_view_internal("bundle", "table")


@pytest.mark.parametrize("server", ["other-reader", "bundle-reader", "reader.extra"])
def test_import_cannot_select_other_plugin_or_normalized_server_id(tmp_path, server):
    store = ExtensionStore(tmp_path / "agent", X25519PrivateKey.generate())
    value = {**VIEW, "mcpServer": server}
    with pytest.raises(AgentError, match="^agent_invalid_plugin_view$"):
        ExtensionImportManager(store).preview({"kind": "plugin", "path": str(package(tmp_path, value))})


@pytest.mark.parametrize("update", [
    {"url": "https://unexpected.test"}, {"id": "../bad"}, {"tool": "__proto__"},
    {"rowsPath": "data.__proto__.rows"}, {"rowsPath": "data[rows]"},
    {"rowsPath": "data.constructor"}, {"rowsPath": "data..rows"}, {"rowsPath": "a." * 9 + "b"},
    {"inputs": [{"name": "user.id", "label": "X", "type": "text"}]},
    {"inputs": [{"name": "keyword", "label": "X", "type": "text", "default": "today"}]},
    {"inputs": [{"name": "keyword", "label": "X", "type": "text", "required": 1}]},
    {"columns": [{"key": "prototype", "label": "X"}]}, {"columns": []},
    {"columns": [{"key": "x", "label": "X", "format": []}]},
    {"pagination": {**VIEW["pagination"], "pageSize": 200}},
    {"pagination": {**VIEW["pagination"], "pageArgument": "keyword"}},
    {"title": "x" * 121}, {"description": "<script>\n"},
])
def test_manifest_rejects_unsafe_or_unbounded_declarations(update):
    with pytest.raises(AgentError, match="^agent_invalid_plugin_view$"):
        normalize_views([{**VIEW, **update}], {"reader": "bundle-reader"})


def test_manifest_limits_and_duplicate_ids():
    with pytest.raises(AgentError):
        normalize_views([VIEW] * 9, {"reader": "bundle-reader"})
    with pytest.raises(AgentError):
        normalize_views([VIEW, VIEW], {"reader": "bundle-reader"})
    assert safe_path("data.reportSummary.totals.运营利润") == "data.reportSummary.totals.运营利润"


def test_success_uses_real_manager_bound_tool_and_does_not_persist_results(host):
    before = {str(p): p.read_bytes() for p in (host.tmp_path / "agent").rglob("*") if p.is_file()}
    response = host.client.post(PREFIX, json=body(host, arguments={"keyword": "synthetic"}, page=2))
    assert response.status_code == 200 and response.json() == {"view_id": "table", "result": RESULT["structuredContent"]}
    assert response.headers["cache-control"] == "no-store"
    assert host.sent == [("read_table", {"keyword": "synthetic", "startDate": date.today().replace(day=1).isoformat(), "page": 2, "pageSize": 20})]
    after = {str(p): p.read_bytes() for p in (host.tmp_path / "agent").rglob("*") if p.is_file()}
    assert after == before


@pytest.mark.parametrize("headers", [{"origin": "https://foreign.example.test"}, {"x-forwarded-for": "203.0.113.10"}])
def test_view_route_requires_local_owner_and_origin(host, headers):
    response = host.client.post(PREFIX, json=body(host), headers=headers)
    assert response.status_code == 401 and host.sent == []


@pytest.mark.parametrize("update", [
    {"arguments": {"tool": "another"}}, {"arguments": {"keyword": {"$ref": "https://unexpected.test"}}},
    {"arguments": {"keyword": "x" * 513}}, {"arguments": {"startDate": "2026-02-30"}},
    {"arguments": {"startDate": "20261001"}}, {"arguments": {"startDate": ""}},
    {"arguments": {"keyword": "\x00"}}, {"arguments": {"startDate": None}},
    {"arguments": {"keyword": "x" * 51}}, {"page": 0}, {"page": True}, {"page": 100001},
    {"pageSize": 999}, {"expected_revision": True}, {"expected_instance_id": "old"},
])
def test_query_rejects_unlisted_invalid_and_stale_arguments_before_dispatch(host, update):
    response = host.client.post(PREFIX, json=body(host, **update))
    assert response.status_code in (400, 409) and host.sent == []


@pytest.mark.parametrize("hint", [False, "true", 1, None])
def test_read_only_requires_literal_server_hint(host, hint):
    discover(host.store, read_only=hint)
    with pytest.raises(AgentError, match="agent_plugin_view_not_read_only"):
        query(host)
    assert host.sent == []


def test_unknown_tool_and_disabled_tool_are_rejected(host):
    discover(host.store, name="another")
    with pytest.raises(AgentError, match="agent_mcp_tool_disabled"):
        query(host)
    discover(host.store)
    row = host.store.get_mcp_server_internal("bundle-reader")
    host.store.upsert_mcp_server({"disabled_tools": ["read_table"]}, server_id=row["id"], expected_revision=row["revision"])
    with pytest.raises(AgentError, match="agent_mcp_tool_disabled"):
        query(host)
    assert host.sent == []


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("change", ["disable", "toggle", "reimport", "delete", "schema", "hint"])
def test_lifecycle_and_schema_changes_block_old_dispatch_or_results(host, phase, change):
    def mutate():
        p = plugin(host.store)
        if change in ("disable", "toggle"):
            host.store.set_plugin_enabled("bundle", False, expected_revision=p["revision"])
            if change == "toggle":
                host.store.set_plugin_enabled("bundle", True, expected_revision=plugin(host.store)["revision"])
        elif change == "reimport":
            install(host.store, host.tmp_path)
        elif change == "delete":
            host.store.delete_plugin("bundle", expected_revision=p["revision"])
        elif change == "schema":
            discover(host.store, schema={**SCHEMA, "description": "new schema"})
        else:
            discover(host.store, read_only=False)
    setattr(host, phase, mutate)
    with pytest.raises(AgentError):
        query(host)
    assert len(host.sent) == (1 if phase == "after" else 0)


def test_stale_frontend_installation_cannot_query_reimported_view(host):
    previous = body(host)
    install(host.store, host.tmp_path)
    with pytest.raises(AgentError, match="agent_stale_revision"):
        query(host, previous)
    assert host.sent == []


def test_cross_plugin_internal_commit_rejected_atomically(host):
    prior = host.store.overview()
    with pytest.raises(AgentError, match="agent_invalid_plugin_view"):
        host.store.commit_imported_items(plugins=[{"id": "foreign", "mcp_server_ids": ["bundle-reader"],
            "views": [{**VIEW, "mcpServer": "bundle-reader"}]}], skills=[], mcp_servers=[])
    assert host.store.overview() == prior


@pytest.mark.parametrize("error,code", [(RuntimeError(CANARY), "agent_plugin_view_query_failed"),
    (AgentError(CANARY), "agent_plugin_view_query_failed"), (AgentError("agent_mcp_auth_required"), "agent_mcp_auth_required")])
def test_upstream_errors_are_fixed_and_auth_required_is_preserved(host, caplog, error, code):
    host.error = error
    response = host.client.post(PREFIX, json=body(host))
    assert response.json() == {"detail": code}
    assert CANARY not in response.text + caplog.text


@pytest.mark.parametrize("raw", [
    {"isError": True, "content": [{"type": "text", "text": CANARY}]},
    {"content": [{"type": "text", "text": CANARY}]},
    {"structuredContent": {"success": False, "message": CANARY}},
    {"structuredContent": {"access_token": CANARY}},
    {"structuredContent": {"data": {"__proto__": CANARY}}},
    {"structuredContent": {"data": {"rows": [None] * 1001}}},
    {"structuredContent": {"data": float("nan")}},
])
def test_errors_markdown_credentials_and_unbounded_results_are_not_returned(host, raw, caplog):
    host.result = raw
    response = host.client.post(PREFIX, json=body(host))
    assert response.status_code in (400, 413) and set(response.json()) == {"detail"}
    assert CANARY not in response.text + caplog.text


def test_remote_schema_reference_is_never_retrieved(host):
    discover(host.store, schema={"$ref": "https://unexpected.test/schema"})
    with pytest.raises(AgentError, match="agent_invalid_request"):
        query(host)
    assert host.sent == []


@pytest.mark.parametrize("phase", ["before", "after"])
def test_session_invalidation_drops_old_query_and_result(host, phase):
    setattr(host, phase, lambda: host.manager.invalidate_server("bundle-reader"))
    with pytest.raises(AgentError, match="agent_stale_revision"):
        query(host)
    assert len(host.sent) == (1 if phase == "after" else 0)


def test_signed_out_view_requires_auth_before_dispatch(host, monkeypatch):
    monkeypatch.setattr(host.manager.oauth, "public_status", lambda _: {"state": "logged_out"})
    with pytest.raises(AgentError, match="agent_mcp_auth_required"):
        query(host)
    assert not host.sent


@pytest.mark.parametrize("phase", ["initial", "before", "after"])
def test_computer_access_server_is_never_available_to_views(host, phase):
    def require_computer_access():
        server = host.store.get_mcp_server_internal("bundle-reader")
        host.store.upsert_mcp_server({"computer_access": True}, server_id=server["id"], expected_revision=server["revision"])
    if phase == "initial":
        require_computer_access()
    else:
        setattr(host, phase, require_computer_access)
    response = host.client.post(PREFIX, json=body(host))
    assert response.status_code in (400, 409)
    assert response.json()["detail"] in {"agent_plugin_view_unsupported_server", "agent_extensions_revision_conflict"}
    assert len(host.sent) == (1 if phase == "after" else 0)
    if phase == "initial":
        assert response.json() == {"detail": "agent_plugin_view_unsupported_server"}


@pytest.mark.parametrize("phase", ["initial", "before", "after"])
def test_owner_confirmation_is_required_and_withdrawal_drops_old_results(host, phase):
    def withdraw_confirmation():
        server = host.store.get_mcp_server_internal("bundle-reader")
        host.store.upsert_mcp_server({"read_only_tools": []}, server_id=server["id"], expected_revision=server["revision"])
    if phase == "initial":
        withdraw_confirmation()
    else:
        setattr(host, phase, withdraw_confirmation)
    response = host.client.post(PREFIX, json=body(host))
    assert response.status_code in (400, 409)
    assert response.json()["detail"] in {"agent_plugin_view_read_only_confirmation_required", "agent_extensions_revision_conflict"}
    assert len(host.sent) == (1 if phase == "after" else 0)
    if phase == "initial":
        assert response.json() == {"detail": "agent_plugin_view_read_only_confirmation_required"}


def test_changed_schema_drops_hint_confirmation_until_owner_reviews_again(host):
    discover(host.store, schema={**SCHEMA, "description": "changed schema"}, confirm=False)
    with pytest.raises(AgentError, match="agent_plugin_view_read_only_confirmation_required"):
        query(host)
    assert not host.sent
    discover(host.store)
    assert query(host)["result"] == RESULT["structuredContent"]


def test_manual_read_only_confirmation_still_requires_server_hint(host):
    discover(host.store, read_only=False)
    server = host.store.get_mcp_server_internal("bundle-reader")
    host.store.upsert_mcp_server({"read_only_tools": ["read_table"]}, server_id=server["id"], expected_revision=server["revision"])
    with pytest.raises(AgentError, match="agent_plugin_view_not_read_only"):
        query(host)
    assert not host.sent



