"""Development collectors exercise real local connectors, never score a model."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rynmesh.agent_runtime.store import AgentReadFailure
from rynmesh.agent_runtime.tools import RynTools
from rynmesh.nas import NasPlugin

_spec = importlib.util.spec_from_file_location("development_document_cases", Path(__file__).resolve().parents[1] /
    "docs/acceptance/ryn-agent-delivery/real-model/document_cases.py")
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)


def configured(home, controller):
    nas = NasPlugin(home)
    nas.enable(True)
    selection = controller.configure(nas)
    tools = RynTools(SimpleNamespace(state=SimpleNamespace(nas_plugin=nas)), SimpleNamespace(home=home), {}, None)
    destination = {"catalog_model_id": "fixture", "provider_peer_id": "local", "source_id": "fixture", "destination_revision": "test"}
    scope = tools.prepare_scope(selection, owner_id="owner-test", consumer_node_id="node-b", model_destination=destination)
    context = {**scope, "task_id": "case", "goal_revision": 1, "scope_revision": 1, "owner_id": "owner-test",
               "consumer_node_id": "node-b", "resource_origins": [], "model_destination": destination}
    controller.after_scope(nas)
    return nas, tools, context, selection


def close(nas):
    for source_id in list(nas.sessions):
        nas._close_session(source_id)


@pytest.mark.parametrize("case_id", sorted(module.SUPPORTED))
def test_each_document_case_mounts_only_frozen_files_and_keeps_exact_scope(tmp_path, case_id):
    with module.DocumentCase(tmp_path, case_id) as controller:
        nas, tools, context, selection = configured(tmp_path, controller)
        try:
            assert controller.case["prompt"] == module.load_case(case_id)["prompt"]
            assert set(controller.fixture_hashes) == set(controller.case["fixture_refs"]) - {"world.json"}
            assert len(selection["resources"]) == len(controller.case["authorization"]["resource_grants"])
            assert set(controller.bindings) <= {"docs-alpha", "docs-offline", "docs-beta"}
            roots = tools.resources.execute("ryn_list_resource_roots", {}, context=context, cancelled=lambda: False)["roots"]
            assert len(roots) == len(selection["resources"])
            report = controller.collect()
            assert not report["original_resource_changes"] and not report["artifact_readback"]
            assert report["semantic_review"].startswith("pending")
        finally:
            close(nas)


def test_development_loader_rejects_heldout_identifiers_without_opening_them():
    with pytest.raises(ValueError, match="development_document_case_unsupported"):
        module.load_case("H01")


def test_exact_directory_and_two_file_approval_then_independent_readback(tmp_path):
    with module.DocumentCase(tmp_path, "F05") as controller:
        nas, tools, context, _ = configured(tmp_path, controller)
        try:
            grant = context["resource_grants"][1]
            def execute(name, args, op):
                preview = tools.preview(name, args, context=context)
                approval = {"tool": name, "arguments": args, "preview": preview}
                assert controller.approve(approval, tools=tools, context=context)
                return tools.execute(name, args, operation_id=op, expected_preview=preview, context=context, cancelled=lambda: False)
            directory = execute("ryn_create_directory", {"directory_ref": grant["root_resource_ref"], "name": "new-week"}, "mkdir")
            for filename in ("overview.md", "risks.md"):
                execute("ryn_create_text_file", {"directory_ref": directory["resource_ref"], "name": filename,
                                               "content": "Synthetic collector test; not model quality."}, filename)
            report = controller.collect()
            assert all(report["mechanical_checks"].values())
            assert len(report["artifact_readback"]) == 2
            assert {event["method"] for event in report["http_audit"] if event["mutation"]} == {"MKCOL", "PUT"}
        finally:
            close(nas)


def test_existing_target_is_never_approved_even_when_name_matches_prompt(tmp_path):
    with module.DocumentCase(tmp_path, "F06") as controller:
        nas, tools, context, _ = configured(tmp_path, controller)
        try:
            args = {"directory_ref": context["resource_grants"][1]["root_resource_ref"], "name": "week-41.md", "content": "Overwrite attempt"}
            approval = {"tool": "ryn_create_text_file", "arguments": args, "preview": tools.preview("ryn_create_text_file", args, context=context)}
            assert not controller.approve(approval, tools=tools, context=context)
            assert (controller.output_root / "week-41.md").read_text() == "ORIGINAL_REPORT_DO_NOT_OVERWRITE\n"
        finally:
            close(nas)


def test_f07_exact_file_grant_produces_actual_timeout_and_collects_failure(tmp_path):
    with module.DocumentCase(tmp_path, "F07") as controller:
        nas, tools, context, selection = configured(tmp_path, controller)
        try:
            assert selection["resources"][-1]["root_kind"] == "file"
            assert selection["resources"][-1]["actions"] == ["read_ai"]
            reference = context["resource_grants"][-1]["root_resource_ref"]
            with pytest.raises(AgentReadFailure, match="resource_read_timeout"):
                tools.resources.execute("ryn_read_resource", {"resource_ref": reference}, context=context, cancelled=lambda: False)
            assert any(event.get("fault") == "persistent_timeout" for event in controller.collect()["http_audit"])
        finally:
            close(nas)


def test_r01_times_out_exactly_first_actual_read_then_returns_original_file(tmp_path):
    from rynmesh.agent_runtime.store import AgentToolRejected
    with module.DocumentCase(tmp_path, "R01") as controller:
        nas, tools, context, _ = configured(tmp_path, controller)
        try:
            root = context["resource_grants"][0]["root_resource_ref"]
            listing = tools.resources.execute("ryn_list_resources", {"resource_ref": root}, context=context, cancelled=lambda: False)
            reference = listing["entries"][0]["resource_ref"]
            controller.start_evaluation()
            with pytest.raises(AgentReadFailure):
                tools.resources.execute("ryn_read_resource", {"resource_ref": reference}, context=context, cancelled=lambda: False)
            result = tools.resources.execute("ryn_read_resource", {"resource_ref": reference}, context=context, cancelled=lambda: False)
            assert result["text"] == (controller.input_root / "progress.md").read_text(encoding="utf-8")
            gets = [row for row in controller.collect()["http_audit"] if row["method"] == "GET"]
            assert len(gets) == 2 and sum(row.get("fault") == "first_read_timeout" for row in gets) == 1
            forged = tools.resources._seal({**tools.resources._open(reference), "path": "private/private-canary.txt"})
            with pytest.raises(AgentToolRejected):
                tools.resources.execute("ryn_read_resource", {"resource_ref": forged}, context=context, cancelled=lambda: False)
        finally:
            close(nas)


def test_p01_prior_task_actual_private_read_never_enters_current_scope_or_context(tmp_path):
    from test_agent_runtime import make_runtime

    from rynmesh.agent_runtime.store import AgentToolRejected
    with module.DocumentCase(tmp_path, "P01") as controller:
        nas, tools, _, selection = configured(tmp_path, controller)
        runtime, model, _, _ = make_runtime(tmp_path / "agent")
        model.supports_destination_binding = True
        runtime.tools = tools
        runtime.catalog.list_models = lambda: [{"id": "local:test", "source": "local_api", "available": True,
            "agent_supported": True, "destination_revision": "test"}]
        try:
            controller.prepare_previous_task(runtime, "local:test")
            previous = runtime.store.read()["tasks"][controller.previous_task]
            canary = "PRIVATE_CONTENT_CANARY_7E2F8A"
            assert canary in str(previous["messages"])
            controller.start_evaluation()
            task = runtime.create({"message": controller.case["prompt"], "scope": selection, "expected_destination_revision": "test"})
            stored = runtime.store.read()["tasks"][task["id"]]
            assert canary not in str(runtime._context(stored))
            old_ref = previous["scope"]["resource_grants"][0]["root_resource_ref"]
            with pytest.raises(AgentToolRejected):
                tools.resources.execute("ryn_read_resource", {"resource_ref": old_ref}, context=runtime._execution_context(stored), cancelled=lambda: False)
            report = controller.collect(model_calls=[{"request_messages": runtime._context(stored)}])
            assert report["security"] == {"forbidden_canary_egress": [], "unauthorized_reads": []}
            assert report["security_audit_coverage"]["canary_scan"] == "retained_public_messages_only"
            observed = controller.collect(model_calls=[{"request_messages": [], "full_request_private_canary_observed": True}])
            assert observed["security"]["forbidden_canary_egress"] == ["private-canary"]
            assert observed["security_audit_coverage"]["canary_scan"] == "complete_in_memory_requests"
            assert "PRIVATE_CONTENT_CANARY_7E2F8A" not in json.dumps(observed)
            assert any(row["connection"] == "docs-beta" and row["method"] == "GET" for row in report["setup_audit"])
        finally:
            close(nas)


def test_f08_budget_and_p06_readonly_come_from_frozen_overrides(tmp_path):
    from test_agent_runtime import make_runtime
    long_case = module.DocumentCase(tmp_path / "long", "F08")
    assert long_case.settings()["max_context_chars"] == 12000
    runtime, _, _, _ = make_runtime(tmp_path / "agent")
    configured = runtime.update_settings(long_case.settings())
    assert configured["permission_mode"] == "confirm_changes"
    assert configured["max_context_chars"] == 12000
    readonly = module.DocumentCase(tmp_path / "readonly", "P06")
    assert readonly.settings()["permission_mode"] == "read_only"


def test_request_observer_detects_canary_in_redacted_continuation_without_altering_input():
    messages = [{"role": "assistant", "content": "Visible public text", "reasoning_content": "PRIVATE_CONTENT_CANARY_7E2F8A"}]
    before = json.dumps(messages)
    destination = {"catalog_model_id": "fixture-model", "destination_revision": "fixed"}
    observed = module.model_request_record("fixture-model", messages, [], destination)
    assert observed["full_request_private_canary_observed"] is True
    assert "PRIVATE_CONTENT_CANARY_7E2F8A" not in json.dumps(observed)
    assert "reasoning_content" not in json.dumps(observed)
    assert json.dumps(messages) == before
    observed["request_messages"][0]["content"] = "edited copy"
    observed["expected_destination"]["destination_revision"] = "edited copy"
    assert json.dumps(messages) == before and destination["destination_revision"] == "fixed"
    clean = module.model_request_record("fixture-model", [{"role": "user", "content": "Summarize authorized notes."}], [], destination)
    assert clean["full_request_private_canary_observed"] is False
