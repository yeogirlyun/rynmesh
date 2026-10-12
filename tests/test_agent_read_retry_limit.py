"""Persistent same-read failure limit leaves other resources and owner control usable."""
import pytest
from test_agent_runtime import Catalog, advance, assistant, make_runtime

from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore


def failing_reads(tools, *, successful_attempts=()):
    actual = tools.execute
    def execute(name, arguments, **options):
        result = actual(name, arguments, **options)
        if arguments["service_id"] == "offline" and len(tools.calls) not in successful_attempts:
            raise AgentError("agent_resource_unavailable")
        return result
    tools.execute = execute


def read(resource="offline"):
    return assistant(name="inspect_service", args={"service_id": resource})


def test_three_actual_failures_block_fourth_after_restart_but_other_read_continues(tmp_path):
    runtime, model, tools, key = make_runtime(tmp_path, *[read() for _ in range(4)], read("available"), assistant("Only the available resource was observed."))
    failing_reads(tools)
    task = runtime.create({"message": "Inspect both resources; do not invent offline results."})
    advance(runtime, 6)
    assert len(tools.calls) == 3
    before = runtime.get(task["id"])["budget"]["lifetime"]["tool_calls"]
    runtime = AgentRuntime(AgentStore(tmp_path, key), model, tools, Catalog())
    runtime.reconcile()
    advance(runtime, 2)
    row = runtime.store.read()["tasks"][task["id"]]
    assert row["status"] == "queued" and len(tools.calls) == 3
    assert runtime.get(task["id"])["budget"]["lifetime"]["tool_calls"] == before
    last = row["receipts"][-1]
    assert last["status"] == "not_executed"
    assert last["result"] == {"error": "agent_read_retry_limit", "not_executed": True, "no_mutation": True}
    assert len([r for r in row["receipts"] if r["status"] == "failed"]) == 3
    advance(runtime, 3)
    assert runtime.get(task["id"])["status"] == "completed"
    assert [c[1]["service_id"] for c in tools.calls] == ["offline"] * 3 + ["available"]


def test_confirmed_same_read_resets_failure_streak(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path, *[read() for _ in range(7)])
    failing_reads(tools, successful_attempts={3})
    task = runtime.create({"message": "Read the resource."})
    advance(runtime, 14)
    row = runtime.store.read()["tasks"][task["id"]]
    assert len(tools.calls) == 6
    assert [r["status"] for r in row["receipts"]] == ["failed", "failed", "confirmed", "failed", "failed", "failed", "not_executed"]


def test_explicit_owner_goal_revision_can_reassess_failed_resource(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path, *[read() for _ in range(4)])
    failing_reads(tools)
    task = runtime.create({"message": "Read the resource."})
    advance(runtime, 6)
    row = runtime.get(task["id"])
    runtime.add_message(task["id"], {"message": "The resource has been repaired. Try reading again.", "intent": "steer",
        "expected_control_revision": row["control_revision"], "expected_goal_revision": row["goal"]["revision"]})
    advance(runtime, 2)
    assert len(tools.calls) == 4
    row = runtime.store.read()["tasks"][task["id"]]
    assert row["receipts"][-1]["goal_revision"] == 2 and row["receipts"][-1]["status"] == "failed"


@pytest.mark.parametrize("binding", ["scope_revision", "context_epoch"])
def test_different_binding_failures_do_not_count_toward_current_read(tmp_path, binding):
    runtime, _, tools, _ = make_runtime(tmp_path, *[read() for _ in range(4)])
    failing_reads(tools)
    task = runtime.create({"message": "Read the resource."})
    advance(runtime, 6)
    # Historic receipt binding is server data; unlike a failed current read it
    # must not suppress a newly authorized scope/model-history epoch.
    def historic(data):
        for receipt in data["tasks"][task["id"]]["receipts"]:
            receipt[binding] = -1
    runtime.store.transaction(historic)
    advance(runtime, 2)
    assert len(tools.calls) == 4


def test_not_executed_receipts_do_not_count_as_actual_read_failures(tmp_path):
    runtime, _, tools, _ = make_runtime(tmp_path, *[read() for _ in range(4)])
    failing_reads(tools)
    task = runtime.create({"message": "Read if permitted."})
    advance(runtime, 6)
    def rejected(data):
        for receipt in data["tasks"][task["id"]]["receipts"]:
            receipt["status"] = "not_executed"
            receipt["result"]["not_executed"] = True
    runtime.store.transaction(rejected)
    advance(runtime, 2)
    assert len(tools.calls) == 4


def test_resource_cursor_changes_cannot_bypass_three_failure_limit(tmp_path):
    class Resources:
        def __init__(self):
            self.calls = []
        def specs(self):
            return [{"type": "function", "function": {"name": "ryn_read_resource", "description": "Read one resource page.",
                "parameters": {"type": "object", "properties": {"resource_ref": {"type": "string"}, "cursor": {"type": "string"}},
                               "required": ["resource_ref"], "additionalProperties": False}}}]
        def metadata(self, name):
            return {"effect": "read"}
        def execute(self, name, arguments, **options):
            self.calls.append(arguments)
            if arguments["resource_ref"] == "offline":
                raise AgentError("agent_resource_unavailable")
            return {"text": "Observed other resource."}
    responses = [assistant(name="ryn_read_resource", args={"resource_ref": "offline", "cursor": str(n)}) for n in range(4)]
    responses.append(assistant(name="ryn_read_resource", args={"resource_ref": "other"}))
    runtime, _, _, _ = make_runtime(tmp_path, *responses)
    tools = runtime.tools = Resources()
    task = runtime.create({"message": "Read the authorized resources."})
    advance(runtime, 10)
    assert [c["resource_ref"] for c in tools.calls] == ["offline"] * 3 + ["other"]
    row = runtime.store.read()["tasks"][task["id"]]
    assert row["receipts"][3]["result"]["error"] == "agent_read_retry_limit"
    assert row["receipts"][4]["status"] == "confirmed"
