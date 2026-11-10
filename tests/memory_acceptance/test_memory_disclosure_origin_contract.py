"""A quoted owner header is not a host-authored memory disclosure.

Uses the same actual offline integration fixture; the bridge capability is a
fixture input, not a patch to product behavior. Existing acceptance is unchanged.
"""
from test_memory_integration_contract import (
    finish,
    front_requests,
)
from test_memory_integration_contract import (
    integrated as integrated,
)
from test_memory_integration_contract import (
    prohibit_network as prohibit_network,
)


def test_owner_quoted_memory_header_does_not_require_memory_destination_binding(integrated):  # noqa: F811 - imported pytest fixture
    env = integrated
    assert not env.api.snapshot()["entries"]
    env.bridge.supports_destination_binding = False
    owner_text = "Explain this literal header in documentation: Owner long-term memory facts ("
    task_id = env.api.create_task(owner_text)
    finish(env, task_id)
    request = front_requests(env)[-1]
    assert "expected_destination" not in request["options"], (
        "The host disclosed no memory or restricted preferences; user text cannot forge a disclosure marker"
    )
    assert any(msg.get("role") == "user" and msg.get("content") == owner_text for msg in request["messages"])


def test_model_quoted_memory_header_cannot_forge_host_disclosure_on_later_turn(integrated):  # noqa: F811 - imported pytest fixture
    env = integrated
    assert not env.api.snapshot()["entries"]
    env.bridge.supports_destination_binding = False
    env.bridge.front_responses.append("Documentation example: Owner long-term memory facts (")
    task_id = env.api.create_task("Explain a documentation heading.")
    finish(env, task_id)
    env.api.continue_task(task_id, "Continue explaining the text only.")
    finish(env, task_id)
    requests = front_requests(env)
    assert len(requests) == 2
    assert all("expected_destination" not in request["options"] for request in requests)
    assert not env.api.snapshot()["entries"]
