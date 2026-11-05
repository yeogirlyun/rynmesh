"""The credit ledger verifies each event once per change, not once per lookup.

A device list asked for one account per device, and every account used to read
and verify every event file twice. These tests pin that the verified-event cache
returns exactly what a cold ledger computes, follows writes from this process
and from others, expires for identity-tier changes, and is built once under
concurrency.
"""
from __future__ import annotations

import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

import rynmesh.credits as credits
from rynmesh.credits import (
    TASK_BALANCE_CATEGORY,
    VERIFIED_EVENTS_TTL_SECONDS,
    CreditPolicy,
    FileCreditLedger,
)
from rynmesh.crypto import public_key_from_private
from rynmesh.identity import IdentityTier


def _key() -> bytes:
    return os.urandom(32)


def _populate(ledger: FileCreditLedger, *, issuers=3, subjects=4, per_pair=5):
    issuer_keys = [_key() for _ in range(issuers)]
    subject_ids = [public_key_from_private(_key()) for _ in range(subjects)]
    kinds = ["node_joined", "full_served", "content_published", "preview_served"]
    count = 0
    for i, key in enumerate(issuer_keys):
        issuer_id = public_key_from_private(key)
        for j, subject in enumerate(subject_ids):
            for n in range(per_pair):
                ledger.record(
                    subject_peer_id=subject, issuer_peer_id=issuer_id, private_key_bytes=key,
                    kind=kinds[(i + j + n) % len(kinds)], category=("general" if n % 2 else "content"),
                    dedupe_key=f"{i}-{j}-{n}",
                )
                count += 1
    # Escrow accounting lives in its own category and stays out of reputation.
    key = issuer_keys[0]
    ledger.record(subject_peer_id=subject_ids[0], issuer_peer_id=public_key_from_private(key),
                  private_key_bytes=key, kind="task_hold", amount=3.0, category=TASK_BALANCE_CATEGORY)
    count += 1
    return issuer_keys, subject_ids, count


def _board(ledger: FileCreditLedger, category: str = credits.GLOBAL_CATEGORY):
    return {peer: account.to_dict() for peer, account in ledger.scoreboard(category=category).items()}


@pytest.fixture
def counted(monkeypatch):
    calls = {"n": 0}
    real = credits.verify_credit_event_with_policy

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(credits, "verify_credit_event_with_policy", counting)
    return calls


def test_cached_results_equal_a_cold_ledger(tmp_path):
    ledger = FileCreditLedger(tmp_path)
    _, subjects, count = _populate(ledger)
    assert count >= 50
    # A broken file and a hidden temp file are skipped exactly as before.
    broken = ledger.events_dir / "deadbeefdeadbeef"
    broken.mkdir()
    (broken / "broken.json").write_text("{not json", encoding="utf-8")
    ledger.scoreboard()  # warm
    cold = FileCreditLedger(tmp_path)
    for category in (credits.GLOBAL_CATEGORY, "content", TASK_BALANCE_CATEGORY):
        assert _board(ledger, category) == _board(cold, category)
        assert [e.to_dict() for e in ledger.list_events(category=category)] == \
               [e.to_dict() for e in cold.list_events(category=category)]
    assert [e.to_dict() for e in ledger.list_events(subject_peer_id=subjects[1])] == \
           [e.to_dict() for e in cold.list_events(subject_peer_id=subjects[1])]
    assert ledger.account(subjects[2]).to_dict() == cold.account(subjects[2]).to_dict()
    unknown = public_key_from_private(_key())
    assert ledger.accounts_for([subjects[0], unknown])[unknown].to_dict() == cold.account(unknown).to_dict()
    assert ledger.accounts_for([subjects[0]])[subjects[0]].to_dict() == cold.account(subjects[0]).to_dict()


def test_repeated_lookups_verify_each_event_once(tmp_path, counted):
    ledger = FileCreditLedger(tmp_path)
    _, subjects, count = _populate(ledger)
    counted["n"] = 0
    for _ in range(10):
        ledger.account(subjects[0])
    ledger.accounts_for(subjects)
    ledger.list_events()
    assert counted["n"] == count


def test_an_append_is_visible_at_once(tmp_path):
    ledger = FileCreditLedger(tmp_path)
    keys, subjects, _ = _populate(ledger, issuers=1, subjects=1, per_pair=3)
    before = ledger.account(subjects[0]).event_count
    key = keys[0]
    ledger.record(subject_peer_id=subjects[0], issuer_peer_id=public_key_from_private(key),
                  private_key_bytes=key, kind="full_served", dedupe_key="new")
    assert ledger.account(subjects[0]).event_count == before + 1


def test_an_event_written_by_another_process_is_picked_up(tmp_path):
    ledger = FileCreditLedger(tmp_path)
    keys, subjects, _ = _populate(ledger, issuers=1, subjects=1, per_pair=3)
    before = ledger.account(subjects[0]).event_count
    other = FileCreditLedger(tmp_path)  # a second process sharing the directory
    key = keys[0]
    other.record(subject_peer_id=subjects[0], issuer_peer_id=public_key_from_private(key),
                 private_key_bytes=key, kind="full_served", dedupe_key="from-elsewhere")
    assert ledger.account(subjects[0]).event_count == before + 1


def test_identity_tier_changes_apply_after_the_ttl(tmp_path):
    tiers: dict[str, IdentityTier] = {}
    ledger = FileCreditLedger(tmp_path, policy=CreditPolicy(enforce_issuer_tier=True),
                              tier_resolver=lambda peer: tiers.get(peer, IdentityTier.ATTESTED))
    key = _key()
    issuer = public_key_from_private(key)
    subject = public_key_from_private(_key())
    ledger.record(subject_peer_id=subject, issuer_peer_id=issuer, private_key_bytes=key,
                  kind="registry_operated", dedupe_key="ops")  # needs ATTESTED
    now = [1000.0]
    ledger._clock = lambda: now[0]
    assert ledger.account(subject).event_count == 1
    tiers[issuer] = IdentityTier.UNVERIFIED  # demoted without any file changing
    now[0] += VERIFIED_EVENTS_TTL_SECONDS - 1
    assert ledger.account(subject).event_count == 1
    now[0] += 2
    assert ledger.account(subject).event_count == 0


def test_concurrent_lookups_build_the_cache_once(tmp_path, counted, monkeypatch):
    ledger = FileCreditLedger(tmp_path)
    _, subjects, count = _populate(ledger)
    ledger._invalidate_verified_events()
    counted["n"] = 0
    real = credits.verify_credit_event_with_policy

    def slow(*args, **kwargs):
        time.sleep(0.002)
        return real(*args, **kwargs)

    monkeypatch.setattr(credits, "verify_credit_event_with_policy", slow)
    start = threading.Barrier(8)
    results = []

    def lookup():
        start.wait()
        results.append(ledger.account(subjects[0]).to_dict())

    threads = [threading.Thread(target=lookup) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert counted["n"] == count
    assert len(results) == 8 and all(result == results[0] for result in results)


def test_callers_cannot_change_cached_results(tmp_path):
    ledger = FileCreditLedger(tmp_path)
    _, subjects, _ = _populate(ledger, issuers=1, subjects=1, per_pair=3)
    account = ledger.account(subjects[0])
    expected = account.to_dict()
    account.categories.clear()
    events = ledger.list_events()
    events[0].payload["kind"] = "tampered"
    assert ledger.account(subjects[0]).to_dict() == expected
    assert ledger.list_events()[0].payload["kind"] != "tampered"


def test_device_list_verifies_each_event_at_most_once(tmp_path, monkeypatch, counted):
    """20 devices and 480 events: the list used to verify 20 x 480 x 2 times."""
    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_REGISTRY_URL", "")
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DEFAULT_DISCOVERY", "0")
    store = RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "network")
    for n in range(19):
        RynmeshStore(home=tmp_path / f"device-{n}", network_dir=tmp_path / "network").register_node(
            network_id="rynmesh-main")
    key = _key()
    issuer = public_key_from_private(key)
    for n in range(480):
        # Distinct subject ids keep events unique without a dedupe lookup per append.
        store.credit_ledger.record(subject_peer_id=store.peer_id, issuer_peer_id=issuer,
                                   private_key_bytes=key, kind="preview_served", subject_id=f"e{n}")
    client = TestClient(create_app(store))
    client.get("/api/local/peers")  # fills the registry cache read by quick mode
    store.credit_ledger._invalidate_verified_events()
    counted["n"] = 0
    peers = client.get("/api/local/peers?quick=1").json()
    assert len(peers) >= 20
    # Each registering device also recorded its own node_joined event.
    events = len(list(store.credit_ledger.events_dir.glob("*/*.json")))
    assert events >= 480
    assert counted["n"] <= events
    self_row = next(peer for peer in peers if peer["isSelf"])
    assert self_row["credits"] > 0
