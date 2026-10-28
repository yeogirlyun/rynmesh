"""Real HTTP business creates and encrypted receipts; fixture audit is test-only."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from agent_tool_fixtures import expose
from test_agent_runtime import advance, approve, assistant
from test_agent_webdav_fixture import FixtureDAV

from rynmesh.agent_runtime import operation_ledger
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore, AgentToolRejected, encoded
from rynmesh.agent_runtime.tools import RynTools
from rynmesh.nas import NasError, NasPlugin
from rynmesh.services import peer_box


@pytest.fixture
def writable(tmp_path):
    root = tmp_path / "dav"
    root.mkdir()
    (root / ".ryn-acceptance-isolated").touch()
    (root / "allowed").mkdir()
    (root / "private").mkdir()
    home = tmp_path / "node"
    with FixtureDAV(root) as server:
        nas = NasPlugin(home)
        nas.enable(True)
        source = nas.configure({"name": "Isolated writable DAV", "system": "generic", "protocol": "webdav", "url": server.url,
                                "username": "fixture", "password": "fixture", "allow_ai": True, "writable": True})
        app, store = SimpleNamespace(state=SimpleNamespace(nas_plugin=nas)), SimpleNamespace(home=home)
        tools = RynTools(app, store, {}, None)
        destination = {"catalog_model_id": "fixture", "provider_peer_id": "local", "source_id": "fixture", "destination_revision": "rev1"}
        scope = tools.prepare_scope({"resources": [{"connection_id": source["id"], "root_path": "allowed", "recursive": True,
                                                    "actions": ["list", "read_ai", "create_file", "create_directory"]}], "allow_model_data": True},
                                    owner_id="local_owner", consumer_node_id="local", model_destination=destination)
        context = {**scope, "task_id": "file-task", "goal_revision": 1, "scope_revision": 1, "owner_id": "local_owner",
                   "consumer_node_id": "local", "model_destination": destination, "resource_origins": []}
        args = {"directory_ref": scope["resource_grants"][0]["root_resource_ref"], "name": "report.md", "content": "Approved test report."}
        try:
            yield tools, context, args, nas, source, server, root, app, store
        finally:
            for source_id in list(nas.sessions):
                nas._close_session(source_id)


def execute(tools, context, args, operation_id="op-create", name="ryn_create_text_file", **kwargs):
    return tools.execute(name, args, operation_id=operation_id, context=context,
                         expected_preview=tools.preview(name, args, context=context), cancelled=kwargs.get("cancelled", lambda: False))


def operation(args, op="op-create", tool="ryn_create_text_file"):
    return {"operation_id": op, "tool": tool, "arguments": args, "arguments_sha256": hashlib.sha256(encoded(args)).hexdigest()}


def test_confirmed_business_receipt_survives_new_registry_before_agent_receipt(writable):
    tools, context, args, _nas, _source, server, root, app, store = writable
    result = execute(tools, context, args)
    assert (root / "allowed/report.md").read_text() == args["content"]
    # Simulate loss of the Agent task transaction after business acknowledgement.
    restarted = RynTools(app, store, {}, None)
    queried = restarted.reconcile("op-create", context={**context, "operation": operation(args)}, cancelled=lambda: False)
    assert queried["outcome"] == "executed" and queried["receipt"]["result"] == result
    assert execute(restarted, context, args) == result
    assert len([e for e in server.audit if e["method"] == "PUT"]) == 1
    raw = (store.home / "agent/business-operations.json").read_text()
    assert args["content"] not in raw and "report.md" not in raw and "op-create" not in raw
    checked = restarted.verify(operation(args), [{"verifier": "created_text_file_state"}], context=context, cancelled=lambda: False)
    assert checked["status"] == "passed" and checked["evidence"]["current_state_matches"]
    (root / "allowed/report.md").write_text("Independent later edit", encoding="utf-8")
    assert restarted.verify(operation(args), [{"verifier": "created_text_file_state"}], context=context, cancelled=lambda: False)["status"] == "failed"


def test_committed_without_ack_remains_unknown_after_restart_and_never_replays(writable):
    tools, context, args, _nas, _source, server, root, app, store = writable
    server.arm("PUT", "allowed/report.md", "drop_after_commit")
    with pytest.raises(NasError):
        execute(tools, context, args)
    assert (root / "allowed/report.md").read_text() == args["content"]
    restarted = RynTools(app, store, {}, None)
    assert restarted.reconcile("op-create", context={**context, "operation": operation(args)}, cancelled=lambda: False)["outcome"] == "unknown"
    with pytest.raises(AgentError, match="agent_write_outcome_unknown"):
        execute(restarted, context, args)
    assert len([e for e in server.audit if e["method"] == "PUT"]) == 1


def test_no_overwrite_and_mkdir_mechanical_readback(writable):
    tools, context, args, _nas, _source, server, root, _app, _store = writable
    (root / "allowed/report.md").write_text("Keep existing", encoding="utf-8")
    with pytest.raises(AgentToolRejected, match="agent_resource_create_conflict"):
        execute(tools, context, args)
    assert (root / "allowed/report.md").read_text() == "Keep existing"
    assert tools.reconcile("op-create", context={**context, "operation": operation(args)}, cancelled=lambda: False)["outcome"] == "not_executed"
    directory = {"directory_ref": args["directory_ref"], "name": "reports"}
    execute(tools, context, directory, "op-dir", "ryn_create_directory")
    assert (root / "allowed/reports").is_dir()
    receipt = operation(directory, "op-dir", "ryn_create_directory")
    assert tools.verify(receipt, [{"verifier": "created_directory_state"}], context=context, cancelled=lambda: False)["status"] == "passed"
    assert len([e for e in server.audit if e["method"] == "MKCOL"]) == 1


@pytest.mark.parametrize("change", ["arguments", "task", "scope", "goal", "digest"])
def test_reconcile_requires_exact_operation_binding(writable, change):
    tools, context, args, *_ = writable
    execute(tools, context, args)
    ctx = copy.deepcopy(context)
    receipt = operation(copy.deepcopy(args))
    if change == "arguments":
        receipt["arguments"]["name"] = "different.md"
    elif change == "digest":
        receipt["arguments_sha256"] = "forged"
    else:
        ctx[{"task": "task_id", "scope": "scope_revision", "goal": "goal_revision"}[change]] = "different"
    assert tools.reconcile("op-create", context={**ctx, "operation": receipt}, cancelled=lambda: False)["outcome"] == "unknown"


@pytest.mark.parametrize("name", ["../private.txt", "nested/file.txt", "evil%2f.txt", "report.pdf", ""])
def test_invalid_target_rejected_before_network_write(writable, name):
    tools, context, args, _nas, _source, server, *_ = writable
    with pytest.raises(AgentToolRejected):
        execute(tools, context, {**args, "name": name})
    assert not any(e["mutation"] for e in server.audit)


def test_missing_action_preview_change_and_last_boundary_revocation_reject(writable, monkeypatch):
    tools, context, args, nas, source, server, *_ = writable
    narrowed = copy.deepcopy(context)
    narrowed["resource_grants"][0]["actions"] = ["list", "read_ai"]
    with pytest.raises(AgentToolRejected, match="agent_resource_scope_denied"):
        execute(tools, narrowed, args)
    preview = tools.preview("ryn_create_text_file", args, context=context)
    with pytest.raises(AgentToolRejected, match="agent_approval_preview_changed"):
        tools.execute("ryn_create_text_file", {**args, "content": "Changed after approval"}, operation_id="changed", context=context,
                      cancelled=lambda: False, expected_preview=preview)
    original = tools.resource_writes.ledger.begin
    def revoke_after_intent(*a):
        value = original(*a)
        nas.update_source(source["id"], {"allow_ai": False})
        return value
    monkeypatch.setattr(tools.resource_writes.ledger, "begin", revoke_after_intent)
    with pytest.raises(AgentToolRejected, match="agent_resource_ai_not_allowed"):
        execute(tools, context, args)
    assert tools.resource_writes.ledger.get("op-create")["status"] == "not_executed"
    assert not any(e["mutation"] for e in server.audit)


def test_business_receipt_failure_after_ack_is_unknown_but_no_second_write(writable, monkeypatch):
    tools, context, args, _nas, _source, server, root, app, store = writable
    original = tools.resource_writes.ledger.update
    def fail_confirm(op, status, **values):
        if status == "confirmed":
            raise OSError("injected disk failure")
        return original(op, status, **values)
    monkeypatch.setattr(tools.resource_writes.ledger, "update", fail_confirm)
    with pytest.raises(OSError):
        execute(tools, context, args)
    assert (root / "allowed/report.md").exists()
    restarted = RynTools(app, store, {}, None)
    assert restarted.reconcile("op-create", context={**context, "operation": operation(args)}, cancelled=lambda: False)["outcome"] == "unknown"
    with pytest.raises(AgentError, match="agent_write_outcome_unknown"):
        execute(restarted, context, args)
    assert len([e for e in server.audit if e["method"] == "PUT"]) == 1


def test_near_capacity_reserves_confirmed_receipt_before_any_put(writable, monkeypatch):
    tools, context, args, _nas, _source, server, *_ = writable
    writes = tools.resource_writes
    preview = tools.preview("ryn_create_text_file", args, context=context)
    binding = writes.binding("ryn_create_text_file", args, "op-create", context)
    intent = {"binding": binding, "preview": preview, "status": "intent", "receipt_reserve": 4096}
    # Intent alone would fit. The exact required receipt reservation cannot.
    monkeypatch.setattr(operation_ledger, "LIMIT", len(encoded({"op-create": intent})) + 4095)
    with pytest.raises(AgentToolRejected, match="agent_operation_ledger_unavailable"):
        execute(tools, context, args)
    assert not any(e["method"] == "PUT" for e in server.audit)
    assert not writes.ledger.path.exists()


def test_other_operations_cannot_consume_first_receipt_reservation(tmp_path, monkeypatch):
    ledger = operation_ledger.OperationLedger(tmp_path)
    ledger.begin({"operation_id": "first"}, {})
    first = ledger.get("first")
    second = {"binding": {"operation_id": "second"}, "preview": {}, "status": "intent", "receipt_reserve": 4096}
    records = {"first": first, "second": second}
    monkeypatch.setattr(operation_ledger, "LIMIT", len(encoded(records)) + 4096)
    with pytest.raises(AgentError, match="agent_operation_ledger_limit"):
        ledger.begin({"operation_id": "second"}, {})
    assert ledger.get("second") is None
    ledger.update("first", "confirmed", result={"payload": "x" * 3000})
    assert ledger.get("first")["status"] == "confirmed"


def test_forget_task_scrubs_ledger_content_but_keeps_no_replay_tombstone(writable):
    tools, context, args, _nas, _source, server, root, app, store = writable
    execute(tools, context, args)
    orphan = tools.resource_writes.ledger.path.with_name(".business-operations.json.crashed.tmp")
    orphan.write_bytes(tools.resource_writes.ledger.path.read_bytes())
    tools.completed["deleted-task-op"] = ("fingerprint", {"private": "derived data"})
    tools.completed["other-task-op"] = ("fingerprint", {"keep": "other task evidence"})
    tools.forget_task(context["task_id"], operation_ids=["op-create", "deleted-task-op"])
    tools.forget_task(context["task_id"], operation_ids=["op-create", "deleted-task-op"])
    assert "deleted-task-op" not in tools.completed and "other-task-op" in tools.completed
    assert not orphan.exists()
    tombstone = tools.resource_writes.ledger.get("op-create")
    assert tombstone["status"] == "retired" and "preview" not in tombstone and "result" not in tombstone
    assert args["content"] not in str(tombstone) and args["name"] not in str(tombstone)
    assert (root / "allowed/report.md").read_text() == args["content"]  # Business artifact retained.
    restarted = RynTools(app, store, {}, None)
    with pytest.raises(AgentToolRejected, match="agent_operation_retired"):
        execute(restarted, context, args)
    assert restarted.reconcile("op-create", context={**context, "operation": operation(args)}, cancelled=lambda: False)["outcome"] == "unknown"
    assert len([e for e in server.audit if e["method"] == "PUT"]) == 1


def test_forget_failure_is_safe_and_retryable(writable, monkeypatch):
    tools, context, args, *_ = writable
    execute(tools, context, args)
    ledger = tools.resource_writes.ledger
    original = ledger._write
    def fail(records):
        raise OSError("injected private diagnostic")
    monkeypatch.setattr(ledger, "_write", fail)
    with pytest.raises(AgentError, match="^agent_task_cleanup_failed$"):
        tools.forget_task(context["task_id"], operation_ids=["op-create"])
    assert ledger.get("op-create")["status"] == "confirmed"
    monkeypatch.setattr(ledger, "_write", original)
    tools.forget_task(context["task_id"], operation_ids=["op-create"])
    assert ledger.get("op-create")["status"] == "retired"


@pytest.mark.parametrize("confirmed", [True, False])
def test_runtime_restart_queries_business_ledger_without_replaying(confirmed, writable, monkeypatch):
    tools, _context, args, _nas, source, server, root, app, store = writable
    class BoundModel:
        supports_destination_binding = True
        responses = []
        def complete(self, model_id, messages, specs, cancelled, on_event, *, expected_destination):
            assert expected_destination["destination_revision"] == "rev1"
            if self.responses:
                return self.responses.pop(0)
            if specs:
                return assistant("Created the approved report once.")
            payload = json.loads(messages[-1]["content"])
            return assistant(json.dumps({"status": "passed", "summary": "Exact created file read back; original write was not replayed.",
                "uncovered_requirements": [], "evidence_refs": [e["id"] for e in payload["evidence"]]}))
    catalog = SimpleNamespace(list_models=lambda: [{"id": "fixture", "source": "local_api", "available": True, "agent_supported": True,
        "peer_id": "local", "source_id": "fixture", "destination_revision": "rev1"}])
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    model = BoundModel()
    runtime = AgentRuntime(AgentStore(store.home / "agent", key), model, tools, catalog, consumer_node_id="local")
    runtime.update_settings({"enabled": True, "model_id": "fixture", "verify_text_responses": True})
    task = runtime.create({"message": "Create the approved report once", "scope": {"resources": [{"connection_id": source["id"],
        "root_path": "allowed", "actions": ["create_file"]}], "allow_model_data": True}, "expected_destination_revision": "rev1"})
    expose(runtime, task["id"], ['ryn_create_text_file'])
    private = runtime.store.read()["tasks"][task["id"]]
    args = {**args, "directory_ref": private["scope"]["resource_grants"][0]["root_resource_ref"]}
    model.responses = [assistant(name="ryn_create_text_file", args=args)]
    execute_original = tools.execute
    if confirmed:
        def crash_before_agent_receipt(*a, **kw):
            execute_original(*a, **kw)
            raise RuntimeError("Simulated Agent transaction loss after durable business receipt")
        monkeypatch.setattr(tools, "execute", crash_before_agent_receipt)
    else:
        server.arm("PUT", "allowed/report.md", "drop_after_commit")
    advance(runtime, 2)
    approve(runtime, task["id"])
    advance(runtime)
    assert runtime.get(task["id"])["outcome"]["status"] == "unknown"
    assert (root / "allowed/report.md").read_text() == args["content"]
    fresh_tools = RynTools(app, store, {}, None)
    restarted = AgentRuntime(AgentStore(store.home / "agent", key), model, fresh_tools, catalog, consumer_node_id="local")
    restarted.reconcile()
    advance(restarted, 6)
    recovered = restarted.get(task["id"])
    stored = restarted.store.read()["tasks"][task["id"]]
    if confirmed:
        assert recovered["status"] == "completed" and recovered["reconciliation"]["state"] == "executed"
        assert stored["in_flight"] is None and stored["queue"] == [] and stored["receipts"][0]["status"] == "confirmed"
        assert any(m["role"] == "tool" for m in stored["messages"]) and recovered["outcome"]["status"] == "verified"
    else:
        assert recovered["status"] == "interrupted" and recovered["reconciliation"]["state"] == "unknown"
        assert stored["in_flight"] and recovered["outcome"]["status"] == "unknown"
    assert len([e for e in server.audit if e["method"] == "PUT"]) == 1


def test_baidu_netdisk_default_scope_creates_folder_and_uploads_workspace_file(tmp_path, monkeypatch):
    home = tmp_path / "node"
    workspace = tmp_path / "workspace"
    (workspace / "qa_test").mkdir(parents=True)
    excel_bytes = b"PK\x03\x04fake-xlsx-binary-payload-12345"
    (workspace / "qa_test" / "qa_budget.xlsx").write_bytes(excel_bytes)

    nas = NasPlugin(home)
    nas.enable(True)
    baidu_source = {
        "id": "baidu1",
        "name": "百度",
        "system": "baidu",
        "protocol": "baidu",
        "root": "",
        "username": "user",
        "writable": True,
        "write_authorized": True,
        "allow_ai": True,
    }
    nas.state["sources"] = [baidu_source]
    nas.save()

    remote_dirs = {"": []}
    remote_files = {}

    class FakeBaiduConnector:
        def list(self, rel):
            return list(remote_dirs.get(rel, []))

        def mkdir(self, rel):
            parent, _, name = rel.rpartition("/")
            remote_dirs.setdefault(parent, []).append({
                "name": name,
                "path": rel,
                "directory": True,
                "size": 0,
                "modified": "2026-10-22T00:00:00+00:00",
            })
            remote_dirs.setdefault(rel, [])

        def write(self, rel, data):
            parent, _, name = rel.rpartition("/")
            remote_files[rel] = bytes(data)
            remote_dirs.setdefault(parent, []).append({
                "name": name,
                "path": rel,
                "directory": False,
                "size": len(data),
                "modified": "2026-10-22T00:00:00+00:00",
            })

        def read(self, rel, _limit):
            return remote_files[rel]

        def close(self):
            pass

    nas.connectors["baidu"] = lambda _cfg: FakeBaiduConnector()

    app, store = SimpleNamespace(state=SimpleNamespace(nas_plugin=nas)), SimpleNamespace(home=home)
    tools = RynTools(app, store, {}, None)

    class BoundModel:
        supports_destination_binding = True
        def complete(self, model_id, messages, specs, cancelled, on_event, *, expected_destination):
            return assistant("Done.")

    catalog = SimpleNamespace(list_models=lambda: [{
        "id": "fixture", "source": "local_api", "available": True, "agent_supported": True,
        "peer_id": "local", "source_id": "fixture", "destination_revision": "rev1",
    }])
    key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    runtime = AgentRuntime(AgentStore(home / "agent", key), BoundModel(), tools, catalog, consumer_node_id="local")
    runtime.update_settings({"enabled": True, "model_id": "fixture"})

    task = runtime.create({"message": "这个表格，能存到我的百度网盘吗？创建一个ryn文件夹，存进去"})
    runtime.store.transaction(lambda data: data["tasks"][task["id"]]["local_execution"].update(workspace_root=str(workspace)))
    row = runtime.store.read()["tasks"][task["id"]]

    exposed_names = {spec["function"]["name"] for spec in runtime._specs(row)}
    assert {"ryn_list_resource_roots", "ryn_list_resources", "ryn_create_directory", "ryn_upload_resource_file"} <= exposed_names

    ctx = runtime._execution_context(row)
    assert len(ctx["resource_grants"]) == 1

    roots = tools.execute("ryn_list_resource_roots", {}, operation_id="op-roots", context=ctx, cancelled=lambda: False)
    root_ref = roots["roots"][0]["resource_ref"]
    assert roots["roots"][0]["connection_name"] == "百度"
    assert "create_file" in roots["roots"][0]["actions"] and "create_directory" in roots["roots"][0]["actions"]

    mkdir_args = {"directory_ref": root_ref, "name": "ryn"}
    mkdir_res = execute(tools, ctx, mkdir_args, operation_id="op-mkdir", name="ryn_create_directory")
    ryn_dir_ref = mkdir_res["resource_ref"]
    assert mkdir_res["created"] == "directory" and mkdir_res["name"] == "ryn"

    upload_args = {"directory_ref": ryn_dir_ref, "workspace_path": "qa_test/qa_budget.xlsx"}
    upload_res = execute(tools, ctx, upload_args, operation_id="op-upload", name="ryn_upload_resource_file")
    assert upload_res["created"] == "file" and upload_res["name"] == "qa_budget.xlsx"
    assert upload_res["bytes"] == len(excel_bytes)
    assert remote_files["ryn/qa_budget.xlsx"] == excel_bytes

    checked = tools.verify(
        operation(upload_args, op="op-upload", tool="ryn_upload_resource_file"),
        [{"verifier": "uploaded_resource_file_state"}],
        context=ctx,
        cancelled=lambda: False,
    )
    assert checked["status"] == "passed" and checked["evidence"]["current_state_matches"]

