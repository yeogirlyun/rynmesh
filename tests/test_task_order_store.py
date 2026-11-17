"""S01-I25: TaskOrderStore corrupt-file quarantine, O(1) count, consumer auto-prune, top-level timestamps, memory TTL/32 cap, and Condition-based wait_terminal."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

import rynmesh.llm_package.task_protocol as protocol_mod
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.manifest import LLMPackageManifest, save_manifest
from rynmesh.llm_package.routes import ProviderService, _expires, install_llm_routes
from rynmesh.llm_package.task_balance import TaskBalanceLedger
from rynmesh.llm_package.task_protocol import (
    TaskOrderStore,
    TaskProtocolError,
    _awaiting_archive,
    open_task,
    seal_task,
)
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def test_t1_corrupt_json_quarantined_and_sweeps_continue(tmp_path, caplog) -> None:
    """T1: Corrupt JSON file is moved to quarantine/ during list/prune/purge, self.quarantined increments, logs omit content, and get() still raises."""
    root = tmp_path / "orders"
    store = TaskOrderStore(root)

    store.claim(task_id="good_active", bindings={"peer": "p1"})
    store.claim(task_id="good_old", bindings={"peer": "p1"})
    store.transition(task_id="good_old", state="failed")
    old_path = root / "good_old.json"
    old_doc = json.loads(old_path.read_text(encoding="utf-8"))
    old_doc["updated_at"] = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    old_path.write_text(json.dumps(old_doc), encoding="utf-8")

    # Direct get() on a corrupt file MUST still raise TaskProtocolError
    corrupt_get = root / "corrupt_direct.json"
    corrupt_get.write_text("{SENSITIVE_BROKEN_PAYLOAD", encoding="utf-8")
    with pytest.raises(TaskProtocolError):
        store.get("corrupt_direct")
    assert corrupt_get.exists()

    # list() quarantines corrupt_direct.json, increments store.quarantined, logs without body content, and returns valid records
    with caplog.at_level(logging.WARNING):
        listed = store.list()
    assert {r["task_id"] for r in listed} == {"good_active", "good_old"}
    assert store.quarantined == 1
    assert not corrupt_get.exists()
    quarantined_files = list((root / "quarantine").glob("corrupt_direct.json.*"))
    assert len(quarantined_files) == 1
    assert "SENSITIVE_BROKEN_PAYLOAD" not in caplog.text

    # prune_terminal() and purge_expired_responses() also quarantine newly corrupted files and continue normally
    (root / "corrupt_prune.json").write_text("not-json-at-all", encoding="utf-8")
    removed = store.prune_terminal(older_than=datetime.now(timezone.utc) - timedelta(days=1))
    assert removed == 1
    assert store.quarantined == 2
    assert store.get("good_old") is None
    assert store.get("good_active") is not None

    (root / "corrupt_purge.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert store.purge_expired_responses() == 0
    assert store.quarantined == 3
    assert len(list((root / "quarantine").iterdir())) == 3


def test_t2_counter_matches_directory_and_enforces_limit_without_glob(tmp_path, monkeypatch) -> None:
    """T2: self._count matches *.json count across __init__, claim, delete, prune_terminal, and quarantine, and enforces the 10000 cap."""
    root = tmp_path / "orders"
    root.mkdir(parents=True)
    # Seed 2 files before __init__ (1 valid, 1 corrupt)
    seed_store = TaskOrderStore(root)
    seed_store.claim(task_id="seed_valid", bindings={"peer": "p1"})
    (root / "seed_corrupt.json").write_text("{broken", encoding="utf-8")

    store = TaskOrderStore(root)
    assert store._count == 2 == len(list(root.glob("*.json")))

    # claim new (+1) vs idempotent claim (+0)
    _, created = store.claim(task_id="t_one", bindings={"peer": "p1"})
    assert created is True
    assert store._count == 3 == len(list(root.glob("*.json")))
    _, created_again = store.claim(task_id="t_one", bindings={"peer": "p1"})
    assert created_again is False
    assert store._count == 3 == len(list(root.glob("*.json")))

    # list() quarantines seed_corrupt (-1)
    store.list()
    assert store._count == 2 == len(list(root.glob("*.json")))

    # delete (-1)
    assert store.delete("seed_valid") is True
    assert store._count == 1 == len(list(root.glob("*.json")))
    assert store.delete("seed_valid") is False
    assert store._count == 1

    # prune_terminal (-1)
    store.transition(task_id="t_one", state="failed")
    removed = store.prune_terminal(older_than=datetime.now(timezone.utc) + timedelta(seconds=10))
    assert removed == 1
    assert store._count == 0 == len(list(root.glob("*.json")))

    # Verify claim checks self._count (no directory globbing during claim) and rejects at 10000
    def forbid_glob(*_args, **_kwargs):
        raise AssertionError("claim() must use self._count instead of globbing root")

    monkeypatch.setattr(type(store.root), "glob", forbid_glob)
    store._count = 10000
    with pytest.raises(TaskProtocolError, match="task_record_limit_reached"):
        store.claim(task_id="over_limit", bindings={"peer": "p1"})


def test_t3_consumer_auto_prunes_old_terminal_while_keeping_awaiting_archive_and_settlement_pending(
    tmp_path, monkeypatch
) -> None:
    """T3: 7-day-old terminal consumer orders are pruned on startup and at _count>=9000, while _awaiting_archive and settlement_pending stay."""
    home = tmp_path / "node"
    consumer_dir = home / "llm" / "consumer-orders"
    pre_store = TaskOrderStore(consumer_dir)
    eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()

    # 1) 8-day-old terminal failed order -> MUST be pruned
    pre_store.claim(task_id="old_failed", bindings={"provider_peer_id": "p", "service_id": "s"})
    pre_store.transition(task_id="old_failed", state="failed", metadata={"error_code": "boom"})
    p = consumer_dir / "old_failed.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["updated_at"] = eight_days_ago
    p.write_text(json.dumps(doc), encoding="utf-8")

    # 2) Unacknowledged succeeded order with encrypted_response within 7-day grace (even with backdated updated_at) -> MUST be kept
    pre_store.claim(task_id="awaiting_arch", bindings={"provider_peer_id": "p", "service_id": "s"})
    pre_store.transition(task_id="awaiting_arch", state="accepted")
    pre_store.transition(task_id="awaiting_arch", state="running")
    pre_store.transition(
        task_id="awaiting_arch",
        state="succeeded",
        encrypted_response={"ciphertext": "keep-me"},
    )
    p_arch = consumer_dir / "awaiting_arch.json"
    doc_arch = json.loads(p_arch.read_text(encoding="utf-8"))
    doc_arch["updated_at"] = eight_days_ago
    doc_arch["completed_at"] = two_days_ago
    for ev in doc_arch["history"]:
        if ev.get("state") == "succeeded":
            ev["at"] = two_days_ago
    p_arch.write_text(json.dumps(doc_arch), encoding="utf-8")

    # 3) 8-day-old succeeded order still carrying unsettled settlement_pending -> MUST be kept by prune_terminal
    direct_store = TaskOrderStore(tmp_path / "direct_prune")
    direct_store.claim(task_id="pending_settle", bindings={"provider_peer_id": "p"})
    direct_store.transition(task_id="pending_settle", state="accepted")
    direct_store.transition(task_id="pending_settle", state="running")
    direct_store.checkpoint(task_id="pending_settle", metadata={"settlement_pending": True, "amount": 0.5})
    direct_store.transition(task_id="pending_settle", state="succeeded")
    p_pend = (tmp_path / "direct_prune") / "pending_settle.json"
    doc_pend = json.loads(p_pend.read_text(encoding="utf-8"))
    doc_pend["updated_at"] = eight_days_ago
    p_pend.write_text(json.dumps(doc_pend), encoding="utf-8")

    assert direct_store.prune_terminal(older_than=datetime.now(timezone.utc) - timedelta(days=7)) == 0
    assert direct_store.get("pending_settle") is not None

    # Startup auto-prune in install_llm_routes
    node_store = RynmeshStore(home=home, network_dir=tmp_path / "net")
    msg_key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    app = FastAPI()
    install_llm_routes(
        app,
        store=node_store,
        home=home,
        messaging_key=msg_key,
        resolve_endpoint=lambda _: "",
        resolve_pubkey=lambda _: "",
    )
    after_startup = TaskOrderStore(consumer_dir)
    assert after_startup.get("old_failed") is None
    assert after_startup.get("awaiting_arch") is not None

    # Also test _count >= 9000 pre-claim cleanup in execute_local_cli
    manifest_file = save_manifest(
        LLMPackageManifest(
            package_id="codex-cli",
            mode="codex_cli",
            adapter="codex_cli",
            runtime="external",
            public_model_alias="Codex CLI",
        ),
        home / "llm" / "codex-cli" / "manifest.json",
    )
    import rynmesh.llm_package.routes as routes_mod

    monkeypatch.setattr(routes_mod, "connect_cli", lambda **_: {"manifest": str(manifest_file)})
    monkeypatch.setattr(
        "rynmesh.llm_package.cli_models.list_models",
        lambda *_, **__: [{"id": "gpt-5", "context_window": 128000, "max_output_tokens": 32000}],
    )
    monkeypatch.setattr(CLIAgentAdapter, "health", lambda _: {"ok": True})
    monkeypatch.setattr(
        CLIAgentAdapter,
        "infer",
        lambda *_, **__: {"text": "ok", "input_tokens": 2, "output_tokens": 1, "duration_ms": 1},
    )
    with TestClient(app) as client:
        assert client.post("/api/local/llm/cli-services/codex_cli/setup").status_code == 200
        # Plant an 8-day-old terminal record directly and bump consumer_orders._count to 9000 via closure inspection or helper
        stale_file = consumer_dir / "stale_9000.json"
        stale_file.write_text(
            json.dumps(
                {
                    "task_id": "stale_9000",
                    "state": "failed",
                    "bindings": {"provider_peer_id": node_store.peer_id},
                    "created_at": eight_days_ago,
                    "updated_at": eight_days_ago,
                    "history": [{"state": "failed", "at": eight_days_ago}],
                }
            ),
            encoding="utf-8",
        )
        # Re-install routes on a fresh app so consumer_orders initializes and we set _count = 9000
        app2 = FastAPI()
        orig_init = TaskOrderStore.__init__
        consumer_instances: list[TaskOrderStore] = []

        def track_init(self, root):
            orig_init(self, root)
            if str(root).endswith("consumer-orders"):
                consumer_instances.append(self)

        monkeypatch.setattr(TaskOrderStore, "__init__", track_init)
        install_llm_routes(
            app2,
            store=node_store,
            home=home,
            messaging_key=msg_key,
            resolve_endpoint=lambda _: "",
            resolve_pubkey=lambda _: "",
        )
        # Re-plant stale_9000 after startup prune and set _count = 9000
        stale_file.write_text(
            json.dumps(
                {
                    "task_id": "stale_9000",
                    "state": "failed",
                    "bindings": {"provider_peer_id": node_store.peer_id},
                    "created_at": eight_days_ago,
                    "updated_at": eight_days_ago,
                    "history": [{"state": "failed", "at": eight_days_ago}],
                }
            ),
            encoding="utf-8",
        )
        consumer_instances[-1]._count = 9000
        with TestClient(app2) as client2:
            assert client2.post("/api/local/llm/cli-services/codex_cli/setup").status_code == 200
            # S01-I42: admission reads the inventory snapshot; the background worker fills it.
            app2.state.llm_agent_services["provider_for"]("codex-cli").adapter.refresh_inventory()
            res = client2.post(
                "/api/local/llm/orders",
                json={
                    "provider_peer_id": node_store.peer_id,
                    "service_id": "codex-cli",
                    "prompt": "ping",
                    "max_tokens": 16,
                    "task_id": "trigger_prune_9000",
                },
            )
            assert res.status_code == 200, res.text
        assert not stale_file.exists()
        assert (consumer_dir / "awaiting_arch.json").exists()


def test_t4_seventy_checkpoints_keep_awaiting_archive_and_response_expires_at(tmp_path) -> None:
    """T4: Top-level completed_at and response_expires_at survive >64 history checkpoints; legacy history fallback still works."""
    store = TaskOrderStore(tmp_path / "orders")
    now = datetime.now(timezone.utc)
    past_expiry = (now - timedelta(minutes=10)).isoformat()

    store.claim(task_id="heavy_ckpt", bindings={"peer": "p1"})
    store.transition(task_id="heavy_ckpt", state="accepted")
    store.transition(task_id="heavy_ckpt", state="running")
    store.transition(
        task_id="heavy_ckpt",
        state="succeeded",
        metadata={"response_expires_at": past_expiry},
        encrypted_response={"ciphertext": "important_paid_answer"},
    )

    initial = store.get("heavy_ckpt")
    assert initial is not None
    assert initial.get("completed_at")
    assert initial.get("response_expires_at") == past_expiry

    # Write 70 checkpoints so the original succeeded transition event is evicted from the 64-entry history window
    for step in range(70):
        store.checkpoint(task_id="heavy_ckpt", metadata={"restart_seq": step})

    after_70 = store.get("heavy_ckpt")
    assert after_70 is not None
    assert len(after_70["history"]) == 64
    assert not any(
        ev.get("state") == "succeeded" and not ev.get("checkpoint") for ev in after_70["history"]
    )
    assert not any(ev.get("response_expires_at") for ev in after_70["history"])

    # _awaiting_archive MUST still be True via top-level completed_at!
    assert _awaiting_archive(after_70, datetime.now(timezone.utc)) is True
    assert store.purge_expired_responses() == 0
    assert "encrypted_response" in (store.get("heavy_ckpt") or {})

    # Once acknowledged, top-level response_expires_at MUST still trigger purge_expired_responses!
    assert store.mark_acknowledged("heavy_ckpt") is True
    assert store.purge_expired_responses() == 1
    assert "encrypted_response" not in (store.get("heavy_ckpt") or {})

    # Legacy record without top-level completed_at / response_expires_at falls back to history
    legacy_path = (tmp_path / "orders") / "legacy_rec.json"
    legacy_doc = {
        "task_id": "legacy_rec",
        "state": "succeeded",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "encrypted_response": {"ciphertext": "legacy_cipher"},
        "history": [
            {"state": "succeeded", "at": now.isoformat(), "response_expires_at": past_expiry}
        ],
    }
    legacy_path.write_text(json.dumps(legacy_doc), encoding="utf-8")
    assert _awaiting_archive(store.get("legacy_rec") or {}, datetime.now(timezone.utc)) is True
    store.mark_acknowledged("legacy_rec")
    assert store.purge_expired_responses() == 1


def test_t5_memory_response_cache_ttl_follows_timeout_and_holds_32_entries(tmp_path, monkeypatch) -> None:
    """T5: use_memory_responses(ttl_seconds=...) scales with provider manifest.timeout_seconds + 60 and holds up to 32 entries."""
    base_mono = protocol_mod.time.monotonic()
    clock = [base_mono]
    monkeypatch.setattr(protocol_mod.time, "monotonic", lambda: clock[0])

    net = tmp_path / "net"
    provider_node = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    pk = peer_box.load_or_create_messaging_key(tmp_path / "pk")
    orders = TaskOrderStore(tmp_path / "provider_orders")

    class _FastAdapter:
        def health(self):
            return {"ok": True}

        def infer(self, **_kw):
            return {"text": "ok", "input_tokens": 1, "output_tokens": 1, "duration_ms": 1}

        def cancel(self, _tid):
            return True

    # timeout_seconds=240 -> ttl_seconds = max(60, 240 + 60) = 300s
    service = ProviderService(
        manifest=LLMPackageManifest(
            package_id="svc",
            mode="openai_compatible",
            public_model_alias="alias",
            base_url="http://127.0.0.1:1",
            timeout_seconds=240.0,
        ),
        adapter=_FastAdapter(),  # type: ignore[arg-type]
        store=provider_node,
        task_store=orders,
        balance=TaskBalanceLedger(tmp_path / "balance.json"),
        messaging_key=pk,
        access_check=lambda *_: None,
    )
    assert service.manifest.timeout_seconds == 240.0

    # Store 32 responses -> all 32 retained (capacity increased from 8 to 32)
    for i in range(32):
        tid = f"task_{i}"
        orders.claim(task_id=tid, bindings={"peer": "p"})
        orders.transition(task_id=tid, state="failed", encrypted_response={"ciphertext": f"enc_{i}"})

    # Advance 120s (> old 60s TTL, < new 300s TTL): all 32 must still be present!
    clock[0] = base_mono + 120.0
    for i in range(32):
        rec = orders.get(f"task_{i}")
        assert rec is not None and rec.get("encrypted_response") == {"ciphertext": f"enc_{i}"}

    # Adding 33rd entry evicts only task_0, keeping task_1..task_32
    orders.claim(task_id="task_32", bindings={"peer": "p"})
    orders.transition(task_id="task_32", state="failed", encrypted_response={"ciphertext": "enc_32"})
    assert "encrypted_response" not in (orders.get("task_0") or {})
    assert (orders.get("task_32") or {}).get("encrypted_response") == {"ciphertext": "enc_32"}

    # Advance past 300s from initial writes (base_mono + 301s): task_1 expires while task_32 (written at +120s) stays until +420s
    clock[0] = base_mono + 301.0
    assert "encrypted_response" not in (orders.get("task_1") or {})
    assert (orders.get("task_32") or {}).get("encrypted_response") == {"ciphertext": "enc_32"}

    clock[0] = base_mono + 421.0
    assert "encrypted_response" not in (orders.get("task_32") or {})


def test_t6_wait_terminal_returns_immediately_on_transition_and_duplicate_request_avoids_sleep_polling(
    tmp_path, monkeypatch
) -> None:
    """T6: wait_terminal wakes immediately via Condition on transition, returns None on timeout, and ProviderService.handle uses it."""
    store = TaskOrderStore(tmp_path / "orders")
    store.claim(task_id="wait_me", bindings={"peer": "p1"})
    store.transition(task_id="wait_me", state="accepted")
    store.transition(task_id="wait_me", state="running")

    # Timeout while still running returns None
    assert store.wait_terminal("wait_me", 0.03) is None

    entered = threading.Event()
    observed: list[tuple[float, dict | None]] = []

    def waiter() -> None:
        entered.set()
        res = store.wait_terminal("wait_me", 5.0)
        observed.append((time.monotonic(), res))

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    assert entered.wait(timeout=2.0)
    time.sleep(0.02)
    t_transition = time.monotonic()
    store.transition(task_id="wait_me", state="succeeded", encrypted_response={"ciphertext": "fast"})
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert len(observed) == 1
    wake_at, final_rec = observed[0]
    assert final_rec is not None and final_rec["state"] == "succeeded"
    # Must wake immediately via Condition notification (far below old 0.25s polling interval)
    assert wake_at - t_transition < 0.15

    # End-to-end duplicate request in ProviderService.handle without 0.25s time.sleep polling
    net = tmp_path / "net"
    provider_node = RynmeshStore(home=tmp_path / "provider", network_dir=net)
    consumer_node = RynmeshStore(home=tmp_path / "consumer", network_dir=net)
    pk = peer_box.load_or_create_messaging_key(tmp_path / "pk")
    ck = peer_box.load_or_create_messaging_key(tmp_path / "ck")

    in_flight = threading.Event()
    finish_infer = threading.Event()

    class _GatedAdapter:
        calls = 0

        def health(self):
            return {"ok": True}

        def infer(self, **_kw):
            self.calls += 1
            in_flight.set()
            assert finish_infer.wait(timeout=5.0)
            return {"text": "deduped-answer", "input_tokens": 3, "output_tokens": 2, "duration_ms": 5}

        def cancel(self, _tid):
            return True

    adapter = _GatedAdapter()
    prov_orders = TaskOrderStore(tmp_path / "prov_orders")
    service = ProviderService(
        manifest=LLMPackageManifest(
            package_id="svc",
            mode="openai_compatible",
            public_model_alias="alias",
            base_url="http://127.0.0.1:1",
            timeout_seconds=30.0,
        ),
        adapter=adapter,  # type: ignore[arg-type]
        store=provider_node,
        task_store=prov_orders,
        balance=TaskBalanceLedger(tmp_path / "prov_balance.json"),
        messaging_key=pk,
        access_check=lambda *_: None,
    )
    signed_req = seal_task(
        body={
            "task_id": "dup_req_1",
            "service_id": "svc",
            "prompt": "hello",
            "max_tokens": 16,
            "max_amount": 1.0,
            "reply_messaging_pub": peer_box.public_key_b64(ck),
        },
        task_id="dup_req_1",
        kind="llm_request",
        sender_peer_id=consumer_node.peer_id,
        recipient_peer_id=provider_node.peer_id,
        sender_signing_key=consumer_node.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(pk),
        expires_at=_expires(300),
    ).to_dict()

    results: list[dict] = []

    def first_call() -> None:
        results.append(service.handle(signed_req))

    t1 = threading.Thread(target=first_call, daemon=True)
    t1.start()
    assert in_flight.wait(timeout=2.0)

    # Guard against routes.py sleeping 0.25s in duplicate loop
    import rynmesh.llm_package.routes as routes_mod

    orig_sleep = routes_mod.time.sleep

    def no_quarter_sec_sleep(secs: float) -> None:
        assert secs != 0.25, "ProviderService.handle must use wait_terminal instead of time.sleep(0.25)"
        orig_sleep(secs)

    monkeypatch.setattr(routes_mod.time, "sleep", no_quarter_sec_sleep)

    dup_results: list[dict] = []
    t2 = threading.Thread(target=lambda: dup_results.append(service.handle(signed_req)), daemon=True)
    t2.start()
    orig_sleep(0.02)
    finish_infer.set()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)
    assert adapter.calls == 1
    assert len(results) == 1 and len(dup_results) == 1
    _, body1 = open_task(results[0], recipient_peer_id=consumer_node.peer_id, recipient_messaging_key=ck, expected_kind="llm_response")
    _, body2 = open_task(dup_results[0], recipient_peer_id=consumer_node.peer_id, recipient_messaging_key=ck, expected_kind="llm_response")
    assert body1["output"] == body2["output"] == "deduped-answer"
