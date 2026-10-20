"""Owner memory retries are idempotent and return the current flat snapshot.

This covers a later genuine state change between the original mutation and its
network retry. It does not change any existing acceptance oracle.
"""
import uuid

from test_memory_integration_contract import MEM, integrated, prohibit_network


def test_idempotent_old_request_replay_returns_fresh_state_without_reapplying(integrated):
    env = integrated
    start = env.api.snapshot()
    original = {
        "expected_generation": start["generation"],
        "client_request_id": str(uuid.uuid4()),
        "use_enabled": False,
    }
    first = env.api.request("PUT", MEM + "/settings", original)
    assert first["settings"]["use_enabled"] is False
    current = env.api.write("PUT", "/settings", {"use_enabled": True})
    assert current["generation"] > first["generation"]
    assert current["settings"]["use_enabled"] is True

    replay = env.api.request("PUT", MEM + "/settings", original)
    after = env.api.snapshot()
    assert after["generation"] == current["generation"], "An idempotent retry must not mutate again"
    assert after["settings"]["use_enabled"] is True
    assert replay["generation"] == after["generation"], "Mutation responses, including replays, must be fresh snapshots"
    assert replay["settings"] == after["settings"]


def test_committed_create_replay_survives_catalog_drift_without_new_authorization(integrated):
    env = integrated
    start = env.api.snapshot()
    original = {
        "expected_generation": start["generation"],
        "client_request_id": str(uuid.uuid4()),
        "text": "Persisted owner camera fact",
        "scope": {"kind": "personal"},
        "exposure_policy": {"mode": "destinations", "allowed_destinations": [
            env.catalog.destination(env.catalog.row),
        ]},
    }
    first = env.api.request("POST", MEM + "/entries", original)
    assert len(first["entries"]) == 1
    old_policy = first["entries"][0]["exposure_policy"]
    env.catalog.row["destination_revision"] = "fixture-rev2"
    current = env.api.write("PUT", "/settings", {"use_enabled": False})
    replay = env.api.request("POST", MEM + "/entries", original)
    after = env.api.snapshot()
    assert after["generation"] == current["generation"]
    assert replay == after, "A committed idempotent replay must return current state despite catalog drift"
    assert len(after["entries"]) == 1
    assert after["entries"][0]["exposure_policy"] == old_policy, "Replay cannot authorize a new destination revision"
    env.api.request("POST", MEM + "/entries", {**original, "text": "Different owner fact"}, expected=409)
    assert env.api.snapshot() == after
