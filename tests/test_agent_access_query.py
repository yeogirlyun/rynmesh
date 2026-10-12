"""Q06 real permission facts, production gates and both collector cohorts."""
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_agent_integrations import fake_upstream, node  # noqa: F401
from test_agent_planner_policy import planner  # noqa: F401

from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore, AgentToolRejected
from rynmesh.ai_access.store import AIAccessError
from rynmesh.services import peer_box

ROOT = Path(__file__).resolve().parents[1]
CONTROLLERS = ROOT / "docs/acceptance/ryn-agent-delivery/real-model"
REFERENCE = CONTROLLERS.parent / "reference"
NODE = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
UPSTREAM = ROOT / ".codex-tmp/research/zcode-runtime-reference"


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(CONTROLLERS))
    monkeypatch.syspath_prepend(str(REFERENCE))
    return importlib.import_module("query_cases"), importlib.import_module("run_queries")


def runtime_for(store, bridge, tools, catalog, selected):
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    runtime = AgentRuntime(AgentStore(store.home / "agent", key), bridge, tools, catalog, consumer_node_id=store.peer_id)
    runtime.update_settings({"enabled": True, "model_id": selected, "max_steps": 24, "max_seconds": 600,
                             "max_context_chars": 100000, "permission_mode": "read_only"})
    return runtime


def test_q06_real_model_grants_and_signed_admission(node, tmp_path, modules):  # noqa: F811
    app, store = node
    query, _ = modules
    with query.QueryWorld(tmp_path / "world", "Q06") as world:
        selected = world.configure(app, store)
        consumer = world.access_query.consumer
        _, tools, catalog = consumer.bridge, consumer.tools, consumer.models
        world.attach(tools)
        def execute(name):
            return tools.execute(name, {}, operation_id="read-" + name, cancelled=lambda: False)
        rows = {row["fixture_identity"]: row for row in execute("ryn_list_models")["models"]}
        assert set(rows) == {"model-a"}
        assert rows["model-a"]["source"] == "peer" and rows["model-a"]["source_id"] == ""
        assert rows["model-a"]["consumer_peer_id"] == world.bindings["node-b"]
        assert execute("ryn_inspect_services")["friend_grants"] == []
        peers = {r["node_name"]: r for r in execute("ryn_list_devices")["friends"]}
        assert set(peers) == {"owner-provider"}
        assert peers["owner-provider"]["peer_id"] == store.peer_id
        assert consumer.store.peer_id != store.peer_id
        assert consumer.sources.read() == {}
        provider_a = world.bindings["model-a"].removeprefix("local/")
        gate = world.access_query
        permission = {"relationship_id": gate.rid, "revision": gate.grant_a["revision"]}
        # The same grant cannot authorize another model or another identity.
        for sid, peer in ((world.bindings["model-b"].removeprefix("local/"), gate.receiver.peer_id),
                          (provider_a, gate.other.peer_id)):
            with pytest.raises(AIAccessError, match="ai_permission_denied"):
                gate.grants.authorize(peer, sid, permission)
        evidence = world.collect()
        assert [p["result"] for p in evidence["access_query"]["setup_admission_probes"]] == ["allowed", "denied", "denied", "denied"]
        assert [p["upstream_requests"] for p in evidence["access_query"]["setup_admission_probes"]] == [1, 0, 0, 0]
        assert evidence["access_query"]["setup_receiver_visible_services"] == {"node-b": [provider_a], "node-c": []}
        assert evidence["metrics"]["writes"] == 0 and evidence["access_query"]["read_only_boundary_passed"]
        assert evidence["access_query"]["setup_fixture_http_audit"]
        assert evidence["access_query"]["task_fixture_http_audit"] == []
        assert world.upstream.secret not in json.dumps([rows, peers, evidence])
        assert catalog.resolve(selected)["id"] == consumer.selected


@pytest.mark.parametrize("boundary", ["candidate", "reference", "direct"])
def test_q06_write_rejected_at_real_boundaries(node, tmp_path, modules, boundary):  # noqa: F811
    app, store = node
    query, runner = modules
    with query.QueryWorld(tmp_path / "world", "Q06") as world:
        selected = world.configure(app, store)
        consumer = world.access_query.consumer
        _, tools, catalog = consumer.bridge, consumer.tools, consumer.models
        world.attach(tools)
        args = {"model_id": world.bindings["model-b"], "enabled": True, "scope": "friends"}
        if boundary == "direct":
            with pytest.raises(AgentToolRejected, match="agent_permission_denied"):
                tools.execute("ryn_set_model_sharing", args, operation_id="forged", cancelled=lambda: False)
        else:
            class WritePlanner(runner.FixturePlanner):
                def complete(self, model_id, messages, specs, cancelled, on_event=None, **options):
                    if specs and not any(m["role"] == "tool" for m in messages):
                        return {"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "attempt", "type": "function",
                            "function": {"name": "ryn_set_model_sharing", "arguments": json.dumps(args)}}]}}
                    return super().complete(model_id, messages, specs, cancelled, on_event, **options)
            from query_consumer import ScriptedProvider
            manager = app.state.llm_agent_services["provider_for"](world.bindings["model-a"].removeprefix("local/"))
            manager.adapter.inner = ScriptedProvider(WritePlanner("Q01"), selected)
            runtime = runtime_for(consumer.store, consumer.bridge, tools, catalog, selected)
            if boundary == "candidate":
                evidence = world.execute(runtime)
                assert evidence["task"]["permission_mode"] == "read_only"
                assert not any(e["type"] == "tool_started" and e.get("effect") == "write" for e in evidence["task"]["events"])
            else:
                from tool_host import ReferenceToolHost
                task = runtime.create({"message": world.case["prompt"]})
                context = runtime._execution_context(runtime.store.read()["tasks"][task["id"]])
                host = ReferenceToolHost(tools, policy=lambda _: False, cancelled=lambda: False, context=context)
                call = {"name": "ryn_set_model_sharing", "arguments": args, "tool_call_id": "attempt"}
                with pytest.raises(AgentToolRejected, match="agent_model_not_shareable"):
                    host.approval(call)
                with pytest.raises(AgentToolRejected):
                    host.execute(call)
        evidence = world.collect()
        assert evidence["metrics"]["writes"] == 0 and evidence["access_query"]["read_only_boundary_passed"]
        assert evidence["access_query"]["hashes_before"] == evidence["access_query"]["hashes_after"]


def test_q06_detects_out_of_band_grant_mutation(node, tmp_path, modules):  # noqa: F811
    app, store = node
    query, _ = modules
    with query.QueryWorld(tmp_path / "world", "Q06") as world:
        world.configure(app, store)
        gate = world.access_query
        gate.grants.set(gate.grant_a["service_id"], gate.rid, allowed=False, expected_revision=1)
        evidence = world.collect()
        assert not evidence["access_query"]["read_only_boundary_passed"]
        assert evidence["metrics"]["writes"] == 1
        assert evidence["access_query"]["hashes_before"]["grants"] != evidence["access_query"]["hashes_after"]["grants"]


@pytest.mark.parametrize("cohort", ["ryn", "reference"])
def test_q06_controller_cli_both_cohorts_unchanged_facts(cohort):
    if cohort == "reference" and (not NODE.exists() or not (UPSTREAM / "apps/zcode-cli/packages/adapters/dist/storage/index.js").exists()):
        pytest.skip("Pinned built reference and matching Node required")
    command = [sys.executable, str(CONTROLLERS / "run_queries.py"), "--case", "Q06", "--fixture-only", "--cohort", cohort]
    if cohort == "reference":
        command += ["--node", str(NODE), "--upstream", str(UPSTREAM)]
    process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert process.returncode == 0, process.stderr
    last = json.loads(process.stdout.strip().splitlines()[-1])
    record = json.loads((Path(last["output"]) / "run.json").read_text(encoding="utf-8"))
    assert not record.get("error_type"), record.get("error_type")
    assert record["case_id"] == "Q06" and not record["formal_case_score"] and not record["real_model"]
    assert record["source_configuration_unchanged"] and record["code_unchanged"]
    assert record["access_query"]["read_only_boundary_passed"] and record["metrics"]["writes"] == 0
    assert record["access_query"]["setup_fixture_http_audit"]
    assert record["access_query"]["task_fixture_http_audit"] == []
    assert record["consumer_role"] == "node-b_authorized_peer_consumer"
    assert record["consumer_peer_id"] == record["bindings"]["node-b"] != record["provider_peer_id"]
    assert record["consumer_local_sources"] == []
    assert len(record["initial_consumer_models"]) == 1
    assert record["primary_model"].startswith("peer/")
    assert record["consumer_wire_audit"] and all(w["consumer_peer_id"] == record["bindings"]["node-b"]
        and w["expected_model_binding"]["consumer_peer_id"] == record["bindings"]["node-b"] for w in record["consumer_wire_audit"])
    isolation = record["agent_state_isolation"]
    assert isolation["consumer_store_exists"] and not isolation["provider_store_exists"]
    assert isolation["consumer_task_ids"] and not isolation["provider_task_ids"] and not isolation["task_ids_overlap"]
    assert isolation["consumer_tool_execution_peer_id"] == record["bindings"]["node-b"]
    assert all(count >= 1 for count in isolation["consumer_message_counts"].values())
    assert set(record["access_query"]["executed_read_tools"]) >= {"ryn_list_models", "ryn_list_devices", "ryn_inspect_services"}
    assert record["physical_hosts"] == 1 and record["two_physical_nodes"] == "pending"
    assert not record["approvals"]
    assert all(not c["credential_egress_detected"] for c in record["model_calls"])
    if cohort == "reference":
        assert record["reference"]["upstream_commit"] == "29628c9acdb81b703bbd4080c207a0e7ce5e276e"
        assert record["reference_accounting_task"]["permission_mode"] == "read_only"
    else:
        assert record["task"]["permission_mode"] == "read_only"


@pytest.mark.parametrize("cohort", ["ryn", "reference"])
def test_q06_actual_gate_and_pinned_worker_share_generation_policy(node, tmp_path, modules, monkeypatch, planner, cohort):  # noqa: F811
    if cohort == "reference" and (not NODE.exists() or not (UPSTREAM / "apps/zcode-cli/packages/adapters/dist/storage/index.js").exists()):
        pytest.skip("Pinned built reference and matching Node required")
    from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
    app, store = node
    query, runner = modules
    helper, policy, _, adapter = planner
    with query.QueryWorld(tmp_path / "world", "Q06") as world:
        world.planner_policy = policy
        selected = world.configure(app, store)
        helper.assert_mapping(world.sources, world.bindings["model-a"], policy)
        consumer = world.access_query.consumer
        bridge, tools, catalog = consumer.bridge, consumer.tools, consumer.models
        world.attach(tools)
        manager = app.state.llm_agent_services["provider_for"](world.bindings["model-a"].removeprefix("local/"))
        assert isinstance(manager.adapter, ModelDispatchAdapter)
        manager.adapter.inner = adapter
        scripted = runner.FixturePlanner("Q06", cohort)
        def synthetic(body):
            return scripted.complete(selected, body["messages"], body.get("tools", []), lambda: False)["message"]
        requests = fake_upstream(monkeypatch, [synthetic] * 12, json_only=True)
        runtime = runtime_for(consumer.store, bridge, tools, catalog, selected)
        if cohort == "ryn":
            execution = world.execute(runtime)
            assert execution["task"]["status"] != "failed", json.dumps((execution["task"].get("error"), consumer.wire_audit))
        else:
            documents = importlib.import_module("documents")
            task = runtime.create({"message": world.case["prompt"]})
            record = {"prompt": world.case["prompt"], "approvals": []}
            documents.execute(runtime=runtime, tools=tools, catalog=catalog, selected=selected, task=task, bridge=bridge,
                              record=record, home=tmp_path, node=NODE, upstream=UPSTREAM,
                              allow_write=lambda _: False, real_model=False)
            assert record["reference"]["upstream_commit"] == "29628c9acdb81b703bbd4080c207a0e7ce5e276e"
        assert len(requests) >= 4
        assert all(r["max_tokens"] == 128000 and r["thinking"] == {"type": "enabled"}
                   and r["reasoning_effort"] == "max" for r in requests)
        assert world.bindings["model-b"].removeprefix("local/") not in json.dumps(requests)
        assert consumer.wire_audit and all(w["consumer_peer_id"] == world.bindings["node-b"]
            and w["expected_model_binding"]["consumer_peer_id"] == world.bindings["node-b"]
            for w in consumer.wire_audit)
        assert world.collect()["access_query"]["read_only_boundary_passed"]
        assert set(world.access_query.read_tools) >= {"ryn_list_models", "ryn_list_devices", "ryn_inspect_services"}
        helper.assert_mapping(world.sources, world.bindings["model-a"], policy)


@pytest.mark.parametrize("changed", ["nonce", "relationship", "key"])
def test_q06_only_protocol_nonces_excluded_from_relationship_comparison(node, tmp_path, modules, changed):  # noqa: F811
    from rynmesh.atomic_io import atomic_write_json
    app, store = node
    query, _ = modules
    with query.QueryWorld(tmp_path / "world", "Q06") as world:
        world.configure(app, store)
        gate = world.access_query
        gate.consumer.close()
        gate.catalogs["node-b"].refresh(store.peer_id)
        if changed != "nonce":
            path = gate.owner.store.state_path
            raw = json.loads(path.read_text(encoding="utf-8"))
            if changed == "relationship":
                raw["relationships"][gate.rid]["node_name"] = "changed"
            else:
                raw["secrets"][gate.rid] = "changed-secret"
            atomic_write_json(path, raw)
        result = world.collect()["access_query"]
        assert result["hashes_before"]["owner_relationships"] != result["hashes_after"]["owner_relationships"]
        assert result["relationship_hash_exclusions"] == ["nonces"]
        assert result["read_only_boundary_passed"] is (changed == "nonce")
        assert result["nonce_counts_after"]["owner_relationships"] > result["nonce_counts_before"]["owner_relationships"]
        assert "changed-secret" not in json.dumps(result)


def test_q06_refuses_provider_tools_and_unrelated_consumer_runtime(node, tmp_path, modules):  # noqa: F811
    from rynmesh.agent_runtime.bridge import build_agent_integrations
    app, store = node
    query, runner = modules
    with query.QueryWorld(tmp_path / "world", "Q06") as world:
        selected = world.configure(app, store)
        _, owner_tools, _ = build_agent_integrations(app, store, store.home)
        with pytest.raises(RuntimeError, match="query_consumer_tools_required"):
            world.attach(owner_tools)
        consumer = world.access_query.consumer
        runtime = runtime_for(store, runner.FixturePlanner("Q06"), consumer.tools, consumer.models, selected)
        with pytest.raises(RuntimeError, match="query_consumer_identity_mismatch"):
            world.execute(runtime)


@pytest.mark.parametrize("change", ["revoke", "model_revision", "quota"])
def test_q06_actual_consumer_provider_gate_rejects_changes_without_upstream(node, tmp_path, modules, change):  # noqa: F811
    from rynmesh.agent_runtime.store import AgentError
    app, store = node
    query, _ = modules
    with query.QueryWorld(tmp_path / "world", "Q06") as world:
        selected = world.configure(app, store)
        gate, consumer = world.access_query, world.access_query.consumer
        destination = consumer.models.destination(consumer.models.resolve(selected))
        before = len(world.upstream.audit)
        if change == "revoke":
            gate.grants.set(gate.grant_a["service_id"], gate.rid, allowed=False, expected_revision=1)
        else:
            raw = world.sources.read()[world.bindings["source-alpha"]]
            configuration = {"requests_per_day": 1} if change == "quota" else {"max_output_tokens": 777}
            world.sources.configure_model(raw["id"], {"model_id": "alpha",
                "expected_rule_revision": raw["model_settings"]["alpha"]["rule_revision"], "configuration": configuration})
            if change == "quota":
                # Setup already consumed one request; refresh only metadata so
                # the new binding is current and rejection is quota-specific.
                consumer.catalog.refresh(store.peer_id)
                destination = consumer.models.destination(consumer.models.resolve(selected))
        with pytest.raises(AgentError, match="agent_model_(destination_changed|quota_exhausted)"):
            consumer.bridge.complete(selected, [{"role": "user", "content": "Synthetic gate test"}], [],
                lambda: False, expected_destination=destination)
        assert len(world.upstream.audit) == before
        assert consumer.wire_audit[-1]["state"] in {"rejected", "failed"}
        if change == "quota":
            assert consumer.wire_audit[-1]["error_code"] == "model_daily_quota_exhausted"
