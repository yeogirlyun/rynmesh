"""Tests for S01-I26: dev Task Balance grant, snapshot pruning, single-read startup,
corrupt snapshot recovery/quarantine, deterministic replay ordering, and replay_skipped."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.credits import CreditEvent, TASK_BALANCE_CATEGORY, sign_credit_event
from rynmesh.llm_package import routes as llm_routes
from rynmesh.llm_package.manifest import LLMPackageManifest, Pricing
from rynmesh.llm_package.task_balance import (
    TaskBalanceError,
    TaskBalanceLedger,
)
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def _ledger(store: RynmeshStore, path, **kwargs) -> TaskBalanceLedger:
    return TaskBalanceLedger(
        path,
        credit_ledger=store.credit_ledger,
        peer_id=store.peer_id,
        private_key_bytes=store.private_key_bytes,
        **kwargs,
    )


@pytest.fixture()
def store(tmp_path) -> RynmeshStore:
    return RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "net")


def test_t1_dev_grant_below_10_succeeds_ge_10_rejected_and_balance_insufficient_code(
    tmp_path, monkeypatch
):
    node = RynmeshStore(home=tmp_path / "consumer", network_dir=tmp_path / "network")
    peer = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "network")
    key = peer_box.load_or_create_messaging_key(node.home / "messaging.x25519")
    peer_key = peer_box.load_or_create_messaging_key(peer.home / "messaging.x25519")
    manifest = LLMPackageManifest(
        package_id="paid-model",
        mode="openai_compatible",
        public_model_alias="Paid model",
        base_url="http://127.0.0.1",
        pricing=Pricing(minimum=8.0, maximum_per_task=50.0),
    )
    public = {
        "online": True,
        "service": manifest.public_dict(),
        "node_messaging_pub": peer_box.public_key_b64(peer_key),
        "capacity": {"available": 1},
    }
    monkeypatch.setattr(
        node,
        "list_job_capacities",
        lambda **_: {
            "capacities": [
                {
                    "peer_id": peer.peer_id,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "metadata": {"llm_service": public},
                }
            ]
        },
    )

    app = FastAPI()
    llm_routes.install_llm_routes(
        app,
        store=node,
        home=node.home,
        messaging_key=key,
        resolve_endpoint=lambda _: "http://synthetic-peer.invalid",
        resolve_pubkey=lambda _: peer_box.public_key_b64(peer_key),
    )
    client = TestClient(app)

    # Initial available is 100.0 (>= 10): dev-grant must reject.
    denied = client.post("/api/local/llm/task-balance/dev-grant")
    assert denied.status_code == 409
    assert client.get("/api/local/task-balance").json()["available"] == 100.0

    # Drain available to 5.0 (< 10) and record earning to ensure dev-grant replay preserves earned.
    bal_path = node.home / "llm" / "task-balance.json"
    direct_ledger = _ledger(node, bal_path)
    direct_ledger.hold(task_id="spend-1", amount=95.0, service_id="svc", provider_peer_id="p")
    direct_ledger.settle(
        task_id="spend-1",
        amount=95.0,
        input_tokens=10,
        output_tokens=10,
        duration_ms=5,
        service_id="svc",
        provider_peer_id="p",
    )
    direct_ledger.earn(
        task_id="earn-1",
        amount=12.5,
        input_tokens=5,
        output_tokens=5,
        duration_ms=3,
        service_id="svc",
        consumer_peer_id="c",
    )
    assert client.get("/api/local/task-balance").json()["available"] == 5.0

    # Order requiring minimum 8.0 when available is 5.0 must fail with closed code balance_insufficient.
    order_res = client.post(
        "/api/local/llm/orders",
        json={
            "provider_peer_id": peer.peer_id,
            "service_id": "paid-model",
            "prompt": "hello",
        },
    )
    assert order_res.status_code == 409
    assert order_res.json()["detail"] == "balance_insufficient"

    # Available < 10: dev-grant succeeds once (+100.0 -> 105.0) and writes opening:dev_grant:1.
    granted = client.post("/api/local/llm/task-balance/dev-grant")
    assert granted.status_code == 200
    body = granted.json()
    assert body["available"] == 105.0
    assert body["earned"] == 12.5

    signed_openings = [
        CreditEvent.from_payload(item.payload)
        for item in node.credit_ledger.list_events(
            subject_peer_id=node.peer_id, category=TASK_BALANCE_CATEGORY
        )
        if item.payload["kind"] == "task_balance_opening"
    ]
    dedupe_keys = sorted(ev.dedupe_key for ev in signed_openings)
    assert dedupe_keys == ["opening:dev_grant:1", "opening:initial"]

    # Immediate duplicate request is rejected (>= 10) and does not grant twice.
    dup = client.post("/api/local/llm/task-balance/dev-grant")
    assert dup.status_code == 409
    assert client.get("/api/local/task-balance").json()["available"] == 105.0
    signed_openings_after = [
        CreditEvent.from_payload(item.payload)
        for item in node.credit_ledger.list_events(
            subject_peer_id=node.peer_id, category=TASK_BALANCE_CATEGORY
        )
        if item.payload["kind"] == "task_balance_opening"
    ]
    assert len(signed_openings_after) == 2

    # Rebuild from signed ledger keeps both initial + dev_grant openings and preserves earned.
    rebuilt = _ledger(node, bal_path).rebuild()
    assert rebuilt["available"] == 105.0
    assert rebuilt["earned"] == 12.5


def test_t2_old_settled_holds_and_earnings_pruned_from_snapshot_but_replay_matches(
    store, tmp_path, monkeypatch
):
    path = tmp_path / "balance.json"
    now = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)
    thirty_one_days_ago = (now - timedelta(days=31)).isoformat()
    five_days_ago = (now - timedelta(days=5)).isoformat()

    current_iso = [thirty_one_days_ago]
    monkeypatch.setattr("rynmesh.llm_package.task_balance._now", lambda: current_iso[0])

    ledger = _ledger(store, path, initial_dev_balance=100.0)
    # 31 days ago: settled hold, released hold, earning, and one still-open hold.
    ledger.hold(task_id="old_settled", amount=4.0, service_id="svc", provider_peer_id="p")
    ledger.settle(
        task_id="old_settled",
        amount=3.0,
        input_tokens=1,
        output_tokens=1,
        duration_ms=1,
        service_id="svc",
        provider_peer_id="p",
    )
    ledger.hold(task_id="old_released", amount=5.0, service_id="svc", provider_peer_id="p")
    ledger.release(task_id="old_released", reason="cancelled")
    ledger.earn(
        task_id="old_earn",
        amount=6.0,
        input_tokens=1,
        output_tokens=1,
        duration_ms=1,
        service_id="svc",
        consumer_peer_id="c",
    )
    ledger.hold(task_id="open_old", amount=1.5, service_id="svc", provider_peer_id="p")

    # 5 days ago: recent settled hold.
    current_iso[0] = five_days_ago
    ledger.hold(task_id="recent_settled", amount=2.0, service_id="svc", provider_peer_id="p")
    ledger.settle(
        task_id="recent_settled",
        amount=2.0,
        input_tokens=1,
        output_tokens=1,
        duration_ms=1,
        service_id="svc",
        provider_peer_id="p",
    )

    # Advance to `now` and write via another transition so >30d entries are pruned.
    current_iso[0] = now.isoformat()
    ledger.hold(task_id="today_hold", amount=1.0, service_id="svc", provider_peer_id="p")
    ledger.release(task_id="today_hold", reason="done")

    raw = path.read_text(encoding="utf-8")
    snap = json.loads(raw)
    # Snapshot JSON must be written without indent=2.
    assert raw == json.dumps(snap, sort_keys=True)
    assert "\n  " not in raw

    # Terminal entries >30d old are pruned; <30d terminal and still-open holds remain.
    assert "old_settled" not in snap["holds"]
    assert "old_released" not in snap["holds"]
    assert "earning:old_earn" not in snap["earnings"]
    assert "recent_settled" in snap["holds"]
    assert "open_old" in snap["holds"]

    expected_summary = ledger.summary()
    assert expected_summary["available"] == 93.5
    assert expected_summary["held"] == 1.5
    assert expected_summary["earned"] == 6.0

    # Replay from signed ledger produces identical balances and keeps snapshot pruned.
    path.unlink()
    replayed = _ledger(store, path)
    replayed_summary = replayed.summary()
    assert replayed_summary["available"] == 93.5
    assert replayed_summary["held"] == 1.5
    assert replayed_summary["earned"] == 6.0
    assert "old_settled" not in json.loads(path.read_text(encoding="utf-8"))["holds"]


def test_t3_startup_calls_list_events_once_and_verifies_last_dedupe_key(
    store, tmp_path, monkeypatch
):
    path = tmp_path / "balance.json"
    ledger = _ledger(store, path, initial_dev_balance=50.0)
    ledger.hold(task_id="t1", amount=10.0, service_id="svc", provider_peer_id="p")
    ledger.settle(
        task_id="t1",
        amount=6.0,
        input_tokens=2,
        output_tokens=3,
        duration_ms=4,
        service_id="svc",
        provider_peer_id="p",
    )

    snap = json.loads(path.read_text(encoding="utf-8"))
    assert snap.get("last_dedupe_key") == "settle:t1"

    calls = 0
    orig_list_events = store.credit_ledger.list_events

    def counted_list_events(*args, **kwargs):
        nonlocal calls
        calls += 1
        return orig_list_events(*args, **kwargs)

    monkeypatch.setattr(store.credit_ledger, "list_events", counted_list_events)

    # Case 1: valid snapshot (both count and last_dedupe_key match) -> 1 list_events call.
    calls = 0
    reloaded = _ledger(store, path)
    assert calls == 1
    assert reloaded.summary()["available"] == 44.0

    # Case 2: missing snapshot triggers replay during startup -> still only 1 list_events call.
    path.unlink()
    calls = 0
    rebuilt = _ledger(store, path)
    assert calls == 1
    assert rebuilt.summary()["available"] == 44.0

    # Case 3: matching count but mismatched last_dedupe_key triggers replay -> still 1 call.
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["last_dedupe_key"] = "settle:other"
    tampered["available"] = 999.0
    path.write_text(json.dumps(tampered), encoding="utf-8")
    calls = 0
    recovered = _ledger(store, path)
    assert calls == 1
    assert recovered.summary()["available"] == 44.0


def test_t4_corrupt_snapshot_auto_rebuilds_in_ledger_mode_and_quarantines_in_standalone(
    store, tmp_path
):
    # Ledger-backed mode: corrupting snapshot at runtime rebuilds automatically on next call.
    ledger_path = tmp_path / "ledger-balance.json"
    ledger = _ledger(store, ledger_path, initial_dev_balance=20.0)
    ledger.hold(task_id="h1", amount=5.0, service_id="svc", provider_peer_id="p")
    ledger.settle(
        task_id="h1",
        amount=3.0,
        input_tokens=1,
        output_tokens=1,
        duration_ms=1,
        service_id="svc",
        provider_peer_id="p",
    )
    ledger.hold(task_id="h2", amount=4.0, service_id="svc", provider_peer_id="p")
    assert ledger.summary()["available"] == 13.0
    assert ledger.summary()["held"] == 4.0

    # Corrupt with invalid JSON while `ledger` is already constructed.
    ledger_path.write_text("{corrupt-json", encoding="utf-8")
    recovered = ledger.summary()
    assert recovered["available"] == 13.0
    assert recovered["held"] == 4.0
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["available"] == 13.0

    # Corrupt with wrong schema/version while `ledger` is running -> next hold still succeeds.
    ledger_path.write_text(json.dumps({"version": "bad"}), encoding="utf-8")
    ledger.hold(task_id="h3", amount=2.0, service_id="svc", provider_peer_id="p")
    assert ledger.summary()["available"] == 11.0
    assert ledger.summary()["held"] == 6.0

    # Standalone mode: renames corrupt file to .corrupt-<timestamp> and raises task_balance_corrupt.
    standalone_path = tmp_path / "standalone-balance.json"
    standalone = TaskBalanceLedger(standalone_path, initial_dev_balance=30.0)
    standalone_path.write_text("{broken-standalone", encoding="utf-8")

    with pytest.raises(TaskBalanceError) as exc_info:
        standalone.summary()
    assert str(exc_info.value) == "task_balance_corrupt"
    assert not standalone_path.exists()
    corrupt_backups = list(tmp_path.glob("standalone-balance.json.corrupt-*"))
    assert len(corrupt_backups) == 1
    assert corrupt_backups[0].read_text(encoding="utf-8") == "{broken-standalone"


def test_t5_same_timestamp_hold_release_replays_in_sequence_order_without_drift(
    store, tmp_path, monkeypatch
):
    fixed_ts = "2026-11-16T08:00:00+00:00"
    monkeypatch.setattr("rynmesh.llm_package.task_balance._now", lambda: fixed_ts)

    path = tmp_path / "seq-balance.json"
    ledger = _ledger(store, path, initial_dev_balance=100.0)
    # Multiple transitions at the exact same timestamp, including hold -> release -> rehold -> settle.
    ledger.hold(task_id="z_task", amount=30.0, service_id="svc", provider_peer_id="p")
    ledger.release(task_id="z_task", reason="temp")
    ledger.hold(task_id="z_task", amount=30.0, service_id="svc", provider_peer_id="p")
    ledger.settle(
        task_id="z_task",
        amount=18.0,
        input_tokens=2,
        output_tokens=2,
        duration_ms=2,
        service_id="svc",
        provider_peer_id="p",
    )
    ledger.hold(task_id="a_task", amount=25.0, service_id="svc", provider_peer_id="p")
    ledger.release(task_id="a_task", reason="cancelled")

    # Verify sequence metadata is written and monotonic.
    signed = store.credit_ledger.list_events(
        subject_peer_id=store.peer_id, category=TASK_BALANCE_CATEGORY
    )
    sequences = [
        CreditEvent.from_payload(item.payload).metadata.get("sequence") for item in signed
    ]
    assert {CreditEvent.from_payload(item.payload).created_at for item in signed} == {fixed_ts}
    assert all(isinstance(seq, int) for seq in sequences)
    assert sorted(sequences) == list(range(1, len(signed) + 1))

    rebuilt = ledger.rebuild()
    assert rebuilt["available"] == 82.0
    assert rebuilt["held"] == 0.0
    assert rebuilt["replay_skipped"] == 0

    # Legacy events without `sequence` at the same timestamp fall back to kind priority
    # (hold < rehold < settle < release < earning) before dedupe_key.
    legacy_store = RynmeshStore(home=tmp_path / "legacy-node", network_dir=tmp_path / "net2")
    for kind, role, subject_id, amount, dedupe, meta in [
        ("task_release", "consumer", "legacy-1", 15.0, "a_release_first_lex", {"phase": "release", "reason": "r"}),
        ("task_hold", "consumer", "legacy-1", 15.0, "z_hold_last_lex", {"phase": "hold", "service_id": "svc", "provider_peer_id": "p", "idempotency_key": "legacy-1", "request_fingerprint": ""}),
        ("task_balance_opening", "provider", "", 50.0, "z_opening", {"phase": "opening", "earned": 0.0}),
    ]:
        ev = CreditEvent(
            subject_peer_id=legacy_store.peer_id,
            issuer_peer_id=legacy_store.peer_id,
            kind=kind,
            amount=amount,
            role=role,
            category=TASK_BALANCE_CATEGORY,
            subject_id=subject_id,
            reason=meta["phase"],
            dedupe_key=dedupe,
            metadata=meta,
            created_at=fixed_ts,
        )
        legacy_store.credit_ledger.append(
            sign_credit_event(ev, private_key_bytes=legacy_store.private_key_bytes)
        )

    legacy_ledger = _ledger(legacy_store, tmp_path / "legacy-replay.json")
    summary = legacy_ledger.summary()
    assert summary["available"] == 50.0
    assert summary["held"] == 0.0
    assert summary["replay_skipped"] == 0


def test_t6_replay_skipped_count_is_visible_and_logged(tmp_path, caplog):
    node = RynmeshStore(home=tmp_path / "skip-node", network_dir=tmp_path / "skip-net")
    bal_path = node.home / "llm" / "task-balance.json"
    ledger = _ledger(node, bal_path, initial_dev_balance=40.0)

    # Append an orphan release with no matching hold so replay hits TaskBalanceError and skips it.
    orphan = CreditEvent(
        subject_peer_id=node.peer_id,
        issuer_peer_id=node.peer_id,
        kind="task_release",
        amount=5.0,
        role="consumer",
        category=TASK_BALANCE_CATEGORY,
        subject_id="nonexistent-task",
        reason="release",
        dedupe_key="release:nonexistent-task:0",
        metadata={"phase": "release", "reason": "orphan"},
    )
    node.credit_ledger.append(
        sign_credit_event(orphan, private_key_bytes=node.private_key_bytes)
    )

    with caplog.at_level(logging.WARNING, logger="rynmesh.llm_package.task_balance"):
        summary = ledger.rebuild()

    assert summary["available"] == 40.0
    assert summary["replay_skipped"] == 1
    assert json.loads(bal_path.read_text(encoding="utf-8"))["replay_skipped"] == 1
    assert any("release:nonexistent-task:0" in rec.message for rec in caplog.records)

    # Also visible via GET /api/local/task-balance.
    key = peer_box.load_or_create_messaging_key(node.home / "messaging.x25519")
    app = FastAPI()
    llm_routes.install_llm_routes(
        app,
        store=node,
        home=node.home,
        messaging_key=key,
        resolve_endpoint=lambda _: "",
        resolve_pubkey=lambda _: "",
    )
    api_view = TestClient(app).get("/api/local/task-balance").json()
    assert api_view["replay_skipped"] == 1
