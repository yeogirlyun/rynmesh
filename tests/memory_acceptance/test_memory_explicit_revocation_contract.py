"""Independent offline integration boundary: explicit owner forget in an existing window.

Imports the same real FastAPI/Runtime/RecordingBridge fixture. Existing acceptance
cases are unchanged. No product patch, helper substitute, or model request.
"""
from test_memory_integration_contract import (
    FORGET,
    MARKER,
    finish,
    front_requests,
    packed,
    seed_bound_memory,
)
from test_memory_integration_contract import (
    integrated as integrated,
)
from test_memory_integration_contract import (
    prohibit_network as prohibit_network,
)


def test_current_owner_forget_rotates_its_own_provider_key_and_derived_context(integrated):  # noqa: F811 - imported pytest fixture
    env = integrated
    seed_bound_memory(env)
    owner_start = "请根据我的摄影器材提供建议。"
    env.bridge.front_responses.append("The remembered body is " + MARKER)
    task_id = env.api.create_task(owner_start)
    finish(env, task_id)
    before = front_requests(env)[-1]
    assert MARKER in packed(before["messages"])
    assert before["options"].get("conversation")

    def checkpoint(data):
        row = data["tasks"][task_id]
        row["context_checkpoint"] = {
            "through": max(0, len(row["messages"]) - 1),
            "content": "Derived memory from the prior response: " + MARKER,
            "references": {},
        }

    env.runtime.store.transaction(checkpoint)
    env.api.continue_task(task_id, FORGET)
    finish(env, task_id)
    assert not any(MARKER in entry["text"] for entry in env.api.snapshot()["entries"])
    after = front_requests(env)[-1]
    assert after["options"].get("conversation") != before["options"]["conversation"], (
        "Explicit forget must revoke provider-held context in its own current task too"
    )
    assert MARKER not in packed(after["messages"]), (
        "The deleting owner turn must not re-disclose a revoked fact via its own old checkpoint or assistant output"
    )
    row = env.runtime.store.read()["tasks"][task_id]
    assert MARKER not in packed(row.get("context_checkpoint"))
    assert row["messages"][0]["content"] == owner_start
    assert any(msg.get("role") == "user" and msg.get("content") == FORGET for msg in row["messages"])
