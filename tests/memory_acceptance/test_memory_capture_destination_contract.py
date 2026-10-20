"""Every new capture enable requires an explicit current destination choice."""
import uuid

from test_memory_integration_contract import MEM, integrated, prohibit_network


def test_reenabling_capture_requires_destination_in_this_owner_write(integrated):
    env = integrated
    destination = env.catalog.destination(env.catalog.row)
    enabled = env.api.write("PUT", "/settings", {
        "capture_enabled": True, "extraction_destination": destination,
    })
    assert enabled["settings"]["capture_enabled"] is True
    disabled = env.api.write("PUT", "/settings", {"capture_enabled": False})
    assert disabled["settings"]["capture_enabled"] is False
    # A prior paid/provider choice is persisted but cannot be treated as fresh
    # permission to turn extraction back on without the owner's current choice.
    env.api.request("PUT", MEM + "/settings", {
        "expected_generation": disabled["generation"],
        "client_request_id": str(uuid.uuid4()),
        "capture_enabled": True,
    }, expected=400)
    assert env.api.snapshot() == disabled
    renewed = env.api.write("PUT", "/settings", {
        "capture_enabled": True, "extraction_destination": destination,
    })
    assert renewed["settings"]["capture_enabled"] is True

