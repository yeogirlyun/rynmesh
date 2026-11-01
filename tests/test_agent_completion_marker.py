"""Task completion identity survives metadata updates and detached summaries."""
from __future__ import annotations

import uuid

import pytest
from test_agent_runtime import advance, assistant, make_runtime
from test_agent_runtime_lifecycle import change
from test_agent_runtime_verification import assessment, verified_runtime

from rynmesh.agent_runtime.protocol import event
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore


def completed_seq(row):
    return [item["seq"] for item in row["events"] if item["type"] == "completed"][-1]


def test_initial_task_has_no_completion_marker(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    created = runtime.create({"message": "First question"})
    assert created["completion_event_seq"] == 0
    assert runtime.get(created["id"])["completion_event_seq"] == 0
    assert runtime.list_tasks()[0]["completion_event_seq"] == 0


def test_completed_response_marker_is_in_detached_summary_without_transcript(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("Here is the answer"))
    created = runtime.create({"message": "First question"})
    advance(runtime)
    done = runtime.get(created["id"])
    marker = completed_seq(done)
    assert marker > 0 and done["completion_event_seq"] == marker
    # Finishing a response is distinct from independently verifying its goal.
    assert done["outcome"]["status"] == "unverified"
    summary = runtime.list_tasks()[0]
    assert summary["completion_event_seq"] == marker
    assert "events" not in summary and "messages" not in summary
    assert "completion_event_seq" not in runtime.store.read()["tasks"][created["id"]]


def test_rename_and_archive_keep_completion_identity_across_restart(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path, assistant("First answer"))
    created = runtime.create({"message": "First question"})
    advance(runtime)
    done = runtime.get(created["id"])
    marker = done["completion_event_seq"]
    renamed = runtime.rename_task(done["id"], {"title": "New title",
        "expected_control_revision": done["control_revision"], "client_request_id": str(uuid.uuid4())})
    assert renamed["revision"] > done["revision"]
    assert renamed["event_cursor"] > done["event_cursor"]
    assert renamed["completion_event_seq"] == marker
    archived = change(runtime, done["id"], "archive")
    assert archived["completion_event_seq"] == marker
    assert runtime.list_tasks(True)[0]["completion_event_seq"] == marker
    restarted = AgentRuntime(AgentStore(runtime.store.root, key), model, tools, runtime.catalog)
    assert restarted.get(done["id"])["completion_event_seq"] == marker
    assert change(restarted, done["id"], "restore")["completion_event_seq"] == marker
    assert restarted.list_tasks()[0]["completion_event_seq"] == marker


def test_another_round_updates_only_after_its_completed_event(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("First answer"), assistant("Second answer"))
    created = runtime.create({"message": "First question"})
    advance(runtime)
    done = runtime.get(created["id"])
    first = done["completion_event_seq"]
    continued = runtime.add_message(done["id"], {"message": "Another question",
        "expected_control_revision": done["control_revision"]})
    assert continued["status"] == "queued"
    assert continued["completion_event_seq"] == first
    assert runtime.list_tasks()[0]["completion_event_seq"] == first
    advance(runtime)
    second = runtime.get(done["id"])
    assert second["completion_event_seq"] == completed_seq(second)
    assert second["completion_event_seq"] > first
    assert runtime.list_tasks()[0]["completion_event_seq"] == second["completion_event_seq"]


def test_other_finished_events_do_not_count_as_response_completions(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    created = runtime.create({"message": "First question"})

    def finish_other_work(data):
        row = data["tasks"][created["id"]]
        event(row, "verification_finished", outcome="verified")
        event(row, "reconciliation_finished", outcome="confirmed")

    runtime.store.transaction(finish_other_work)
    row = runtime.get(created["id"])
    assert row["event_cursor"] > created["event_cursor"]
    assert row["completion_event_seq"] == 0
    assert runtime.list_tasks()[0]["completion_event_seq"] == 0


@pytest.mark.parametrize("prior_round", [False, True])
def test_verification_error_marks_completed_answer_in_first_or_later_round(tmp_path, prior_round):
    responses = [assistant("Earlier answer"), assessment(refs=[])] if prior_round else []
    runtime, model, _, _ = verified_runtime(tmp_path, *responses, assistant("Current answer"))
    created = runtime.create({"message": "First question"})
    previous = 0
    if prior_round:
        advance(runtime, 2)
        done = runtime.get(created["id"])
        previous = done["completion_event_seq"]
        assert previous > 0
        runtime.add_message(done["id"], {"message": "Another question",
            "expected_control_revision": done["control_revision"]})
    advance(runtime)
    assert runtime.get(created["id"])["completion_event_seq"] == previous

    def broken_checker(*args):
        raise AgentError("agent_model_connection_failed")

    model.complete = broken_checker
    advance(runtime)
    done = runtime.get(created["id"])
    assert done["status"] == "completed" and done["result"] == "Current answer"
    assert "agent_model_connection_failed" in done["outcome"]["uncertainties"]
    assert [item["type"] for item in done["events"][-2:]] == ["verification_finished", "completed"]
    assert done["completion_event_seq"] == completed_seq(done) > previous
    assert runtime.list_tasks()[0]["completion_event_seq"] == done["completion_event_seq"]


@pytest.mark.parametrize("limit", ["agent_step_limit", "agent_time_limit"])
def test_verification_claim_limits_mark_answer_completed(tmp_path, limit):
    runtime, model, _, _ = verified_runtime(tmp_path, assistant("Current answer"))
    created = runtime.create({"message": "First question"})
    advance(runtime)
    assert runtime.get(created["id"])["completion_event_seq"] == 0

    def exhaust(data):
        row = data["tasks"][created["id"]]
        if limit == "agent_step_limit":
            row["steps"] = row["limits"]["max_steps"]
        else:
            row["elapsed_seconds"] = row["limits"]["max_seconds"]

    runtime.store.transaction(exhaust)
    advance(runtime)
    done = runtime.get(created["id"])
    assert done["status"] == "completed" and done["result"] == "Current answer"
    assert limit in done["outcome"]["uncertainties"]
    assert [item["type"] for item in done["events"][-2:]] == ["verification_finished", "completed"]
    assert done["completion_event_seq"] == completed_seq(done) > 0
    assert runtime.list_tasks()[0]["completion_event_seq"] == done["completion_event_seq"]
    assert len(model.calls) == 1


def test_intermediate_verification_repair_has_no_completion_marker(tmp_path):
    runtime, _, _, _ = verified_runtime(tmp_path, assistant("Candidate answer"),
        assessment("failed", ["A missing detail"], refs=[]), assistant("Corrected answer"), assessment(refs=[]))
    created = runtime.create({"message": "First question"})
    advance(runtime, 2)
    retry = runtime.get(created["id"])
    assert retry["status"] == "queued"
    assert [item["type"] for item in retry["events"][-2:]] == ["verification_finished", "verification_retry_scheduled"]
    assert retry["completion_event_seq"] == 0
    assert runtime.list_tasks()[0]["completion_event_seq"] == 0
    advance(runtime, 2)
    done = runtime.get(created["id"])
    assert done["status"] == "completed" and done["result"] == "Corrected answer"
    assert done["completion_event_seq"] == completed_seq(done) > retry["event_cursor"]


def test_verification_format_retry_marks_only_its_final_failure(tmp_path):
    runtime, _, _, _ = verified_runtime(tmp_path, assistant("Current answer"), assistant("Invalid check"), assistant("Still invalid"))
    created = runtime.create({"message": "First question"})
    advance(runtime, 2)
    retry = runtime.get(created["id"])
    assert retry["status"] == "queued"
    assert retry["events"][-1]["type"] == "verification_format_retry_scheduled"
    assert retry["completion_event_seq"] == 0
    advance(runtime)
    done = runtime.get(created["id"])
    assert done["status"] == "completed" and done["result"] == "Current answer"
    assert done["completion_event_seq"] == completed_seq(done) > retry["event_cursor"]
