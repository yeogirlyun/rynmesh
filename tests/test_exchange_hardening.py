"""Adversarial lifecycle coverage independent of owner UI restrictions."""

from __future__ import annotations

import copy

import pytest
from test_exchange import Mesh, new_id

from rynmesh.exchange.protocol import VERSION, ExchangeError, apply


def command(node, state, action, value):
    return node.signed(
        {
            "version": VERSION,
            "network": node.ledger.network,
            "id": new_id(),
            "actor": node.actor,
            "nonce": state["accounts"].get(node.actor, {}).get("nonce", 0) + 1,
            "action": action,
            "value": value,
        }
    )


def transition(mesh, node, action, value, *, state=None, timestamp=None):
    state = state if state is not None else node.ledger.state
    return apply(
        state,
        command(node, state, action, value),
        mesh.config,
        mesh.now if timestamp is None else timestamp,
    )


@pytest.mark.parametrize("phase", ["disputed", "first_ruling", "appeal", "appeal_ruling"])
def test_dispute_never_resets_a_case_or_appeal(tmp_path, phase):
    mesh = Mesh(tmp_path, judges=True)
    order = mesh.dispute()
    if phase != "disputed":
        mesh.buyer.action("rule", {"order_id": order}, new_id())
    if phase in {"appeal", "appeal_ruling"}:
        mesh.buyer.action("appeal", {"order_id": order}, new_id())
    if phase == "appeal_ruling":
        mesh.buyer.action("evidence", {"order_id": order, "body": "Buyer appeal"}, new_id())
        mesh.provider.action("evidence", {"order_id": order, "body": "Provider appeal"}, new_id())
        mesh.buyer.action("rule", {"order_id": order}, new_id())
    for node in (mesh.buyer, mesh.provider):
        node.sync()
        before = copy.deepcopy(node.ledger.state)
        with pytest.raises(ExchangeError):
            transition(mesh, node, "dispute", {"order_id": order})
        assert node.ledger.state == before


def test_statements_cannot_invalidate_collected_receipts(tmp_path):
    mesh = Mesh(tmp_path, judges=True)
    order = mesh.dispute()
    mesh.buyer.sync()
    rulings = mesh.buyer.prepare("rule", {"order_id": order}, new_id())
    before = copy.deepcopy(mesh.buyer.ledger.state)
    mesh.provider.sync()
    replacement = mesh.provider.prepare(
        "evidence", {"order_id": order, "body": "Changed statement"}, new_id()
    )
    with pytest.raises(ExchangeError):
        transition(mesh, mesh.provider, "evidence", replacement)
    decided = transition(mesh, mesh.buyer, "rule", rulings, state=before)
    assert decided["orders"][order]["status"] == "ruling_ready"


@pytest.fixture(scope="module")
def lifecycle(tmp_path_factory):
    from rynmesh.exchange.protocol import settlement_commitment

    mesh = Mesh(tmp_path_factory.mktemp("lifecycle"), judges=True)
    order = mesh.order()
    snapshots = {}

    def snapshot(label):
        mesh.buyer.sync()
        snapshots[label] = copy.deepcopy(mesh.buyer.ledger.state)

    snapshot("working")
    mesh.provider.action("deliver", {"order_id": order, "body": "Original delivery"}, new_id())
    snapshot("delivered")
    mesh.buyer.action("dispute", {"order_id": order}, new_id())
    mesh.buyer.action("evidence", {"order_id": order, "body": "Buyer evidence"}, new_id())
    snapshot("initial_partial")
    mesh.provider.action("evidence", {"order_id": order, "body": "Provider evidence"}, new_id())
    snapshot("initial_case")
    mesh.buyer.action("rule", {"order_id": order}, new_id())
    snapshot("initial_ruling")
    mesh.buyer.action("appeal", {"order_id": order}, new_id())
    snapshot("appeal_empty")
    mesh.buyer.action("evidence", {"order_id": order, "body": "Buyer appeal"}, new_id())
    mesh.provider.action("evidence", {"order_id": order, "body": "Provider appeal"}, new_id())
    snapshot("appeal_case")
    mesh.buyer.action("rule", {"order_id": order}, new_id())
    snapshot("appeal_ruling")
    state = snapshots["delivered"]
    snapshots["accepted"] = transition(
        mesh,
        mesh.buyer,
        "accept",
        {"order_id": order, "delivery_hash": state["orders"][order]["delivery_hash"]},
        state=state,
    )
    state = transition(mesh, mesh.buyer, "refund", {"order_id": order}, state=snapshots["working"])
    snapshots["refunded"] = transition(
        mesh, mesh.provider, "refund", {"order_id": order}, state=state
    )
    state = snapshots["appeal_ruling"]
    snapshots["resolved"] = transition(
        mesh,
        mesh.buyer,
        "finalize",
        {"order_id": order, "settlement_hash": settlement_commitment(state["orders"][order])},
        state=state,
        timestamp=mesh.now + 61,
    )
    yield mesh, order, snapshots
    for node in mesh.nodes:
        node.ledger.close()


PHASES = [
    "working",
    "delivered",
    "initial_partial",
    "initial_case",
    "initial_ruling",
    "appeal_empty",
    "appeal_case",
    "appeal_ruling",
    "accepted",
    "refunded",
    "resolved",
]
ACTIONS = [
    "deliver",
    "accept",
    "refund",
    "dispute",
    "evidence",
    "rule",
    "appeal",
    "waive",
    "finalize",
    "timeout",
]


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("role", ["buyer", "provider", "outsider"])
@pytest.mark.parametrize("action", ACTIONS)
def test_role_status_round_matrix(lifecycle, phase, role, action):
    from rynmesh.crypto import canonical_json, sha256_bytes
    from rynmesh.exchange.protocol import case_payload, judge_panel, settlement_commitment

    mesh, order_id, snapshots = lifecycle
    state = snapshots[phase]
    before = copy.deepcopy(state)
    order = state["orders"][order_id]
    node = getattr(mesh, role) if role != "outsider" else mesh.nodes[0]
    value = {"order_id": order_id}
    panel = judge_panel(mesh.config, order, order.get("round", 0))
    if action == "deliver":
        value |= {
            "hash": "sha256:" + "0" * 64,
            "sealed": {"nonce": "nonce", "ciphertext": "ciphertext"},
        }
    if action == "accept":
        value["delivery_hash"] = order.get("delivery_hash", "sha256:" + "0" * 64)
    if action == "evidence":
        value |= {
            "commitment": "sha256:" + "0" * 64,
            "sealed": {j["peer_id"]: {"nonce": "nonce", "ciphertext": "ciphertext"} for j in panel},
        }
    if action == "rule":
        # A valid signed panel isolates lifecycle validation from malformed payloads.
        case_order = dict(order, case_until=order.get("case_until", mesh.now + 172800))
        case_hash = sha256_bytes(
            canonical_json(case_payload(mesh.config, case_order, order.get("round", 0)))
        )
        value["rulings"] = [
            next(n for n in mesh.nodes if n.peer_id == j["peer_id"]).signed(
                {
                    "version": VERSION,
                    "kind": "ruling",
                    "network": mesh.buyer.ledger.network,
                    "case_hash": case_hash,
                    "model": j["model"],
                    "share_bps": 10000,
                    "reason_code": "delivery_matches",
                }
            )
            for j in panel
        ]
    if action in {"waive", "finalize", "timeout"}:
        value["settlement_hash"] = settlement_commitment(order)
    allowed = {
        "working": {"buyer": {"refund", "dispute"}, "provider": {"deliver", "refund", "dispute"}},
        "delivered": {
            "buyer": {"accept", "refund", "dispute"},
            "provider": {"deliver", "refund", "dispute"},
        },
        "initial_partial": {"buyer": {"refund"}, "provider": {"refund", "evidence"}},
        "initial_case": {"buyer": {"refund", "rule"}, "provider": {"refund", "rule"}},
        "initial_ruling": {
            "buyer": {"refund", "appeal", "waive"},
            "provider": {"refund", "appeal", "waive"},
        },
        "appeal_empty": {"buyer": {"refund", "evidence"}, "provider": {"refund", "evidence"}},
        "appeal_case": {"buyer": {"refund", "rule"}, "provider": {"refund", "rule"}},
        "appeal_ruling": {"buyer": {"refund", "waive"}, "provider": {"refund", "waive"}},
    }.get(phase, {}).get(role, set())
    if action in allowed:
        next_state = transition(mesh, node, action, value, state=state)
        assert (
            sum(a["available"] + a["held"] for a in next_state["accounts"].values())
            == next_state["issued"]
        )
        assert all(a["available"] >= 0 and a["held"] >= 0 for a in next_state["accounts"].values())
    else:
        with pytest.raises(ExchangeError):
            transition(mesh, node, action, value, state=state)
    assert state == before


@pytest.mark.parametrize(
    "phase,share",
    [
        ("working", 0),
        ("delivered", 10000),
        ("initial_partial", 5000),
        ("initial_case", 5000),
        ("appeal_empty", 10000),
        ("appeal_case", 10000),
        ("initial_ruling", 10000),
        ("appeal_ruling", 10000),
    ],
)
def test_deadline_outcomes_and_exact_boundary(lifecycle, phase, share):
    from rynmesh.exchange.protocol import deadline, settlement_commitment

    mesh, order_id, snapshots = lifecycle
    state = snapshots[phase]
    order = state["orders"][order_id]
    value = {"order_id": order_id, "settlement_hash": settlement_commitment(order)}
    with pytest.raises(ExchangeError, match="timeout_open"):
        transition(mesh, mesh.buyer, "timeout", value, state=state, timestamp=deadline(order) - 1)
    settled = transition(
        mesh, mesh.provider, "timeout", value, state=state, timestamp=deadline(order)
    )
    assert settled["orders"][order_id]["paid"] == order["price"] * share // 10000
    assert all(a["held"] == 0 for a in settled["accounts"].values())
    assert sum(a["available"] for a in settled["accounts"].values()) == state["issued"]
    for judge in mesh.config["judges"]:
        assert settled["accounts"].get(judge["peer_id"], {}).get("earned", 0) == state[
            "accounts"
        ].get(judge["peer_id"], {}).get("earned", 0)


def test_undelivered_dispute_cannot_gain_timeout_payment(tmp_path):
    from rynmesh.exchange.protocol import deadline, settlement_commitment

    mesh = Mesh(tmp_path, judges=True)
    order_id = mesh.order()
    mesh.provider.action("dispute", {"order_id": order_id}, new_id())
    mesh.buyer.sync()
    order = mesh.buyer.ledger.state["orders"][order_id]
    result = transition(
        mesh,
        mesh.provider,
        "timeout",
        {"order_id": order_id, "settlement_hash": settlement_commitment(order)},
        state=mesh.buyer.ledger.state,
        timestamp=deadline(order),
    )
    assert result["orders"][order_id]["paid"] == 0


@pytest.mark.parametrize("action", ["finalize", "waive", "timeout"])
def test_changed_settlement_cannot_use_stale_confirmation(lifecycle, action):
    from rynmesh.exchange.protocol import deadline, settlement_commitment

    mesh, order_id, snapshots = lifecycle
    initial = snapshots["initial_ruling"]
    final = copy.deepcopy(snapshots["appeal_ruling"])
    final["orders"][order_id]["ruling_share"] = 0
    with pytest.raises(ExchangeError, match="settlement_changed"):
        transition(
            mesh,
            mesh.buyer,
            action,
            {
                "order_id": order_id,
                "settlement_hash": settlement_commitment(initial["orders"][order_id]),
            },
            state=final,
            timestamp=deadline(final["orders"][order_id]),
        )


def proposal_for(mesh, command_wire, timestamp):
    mesh.buyer.sync()
    ledger = mesh.buyer.ledger
    leader = mesh.nodes[ledger.height % 4]
    core = {
        "version": VERSION,
        "network": ledger.network,
        "height": ledger.height + 1,
        "parent": ledger.head,
        "timestamp": timestamp,
        "command": command_wire,
    }
    return {**core, "proposer": leader.signed(core)}


def test_backdating_cannot_reopen_an_expired_appeal(tmp_path):
    mesh = Mesh(tmp_path, judges=True)
    order_id = mesh.dispute()
    mesh.buyer.action("rule", {"order_id": order_id}, new_id())
    for node in mesh.nodes:
        node.sync()
    until = mesh.buyer.ledger.state["orders"][order_id]["appeal_until"]
    mesh.now = until + 1
    # Within the allowed skew: the actual local deadline, not freshness alone, rejects it.
    proposal = proposal_for(
        mesh,
        command(mesh.buyer, mesh.buyer.ledger.state, "appeal", {"order_id": order_id}),
        until - 1,
    )
    for node in mesh.nodes[:4]:
        with pytest.raises(ExchangeError, match="deadline_expired"):
            node.ledger.vote(proposal)
        assert not node.ledger.db.execute(
            "SELECT 1 FROM votes WHERE height=?", (proposal["height"],)
        ).fetchone()


@pytest.mark.parametrize("offset", [-31, 31])
def test_new_votes_reject_clock_skew_without_locking_height(tmp_path, offset):
    mesh = Mesh(tmp_path)
    value = {
        "label": "Changed",
        "encryption_key": mesh.buyer.ledger.state["accounts"][mesh.buyer.actor]["encryption_key"],
    }
    if offset < 0:
        mesh.now += 100
    proposal = proposal_for(
        mesh, command(mesh.buyer, mesh.buyer.ledger.state, "profile", value), mesh.now + offset
    )
    for node in mesh.nodes[:4]:
        node.sync()
        with pytest.raises(ExchangeError, match="time_invalid"):
            node.ledger.vote(proposal)


def test_prior_vote_and_old_certificate_survive_time_and_restart(tmp_path):
    from rynmesh.exchange.ledger import Ledger

    mesh = Mesh(tmp_path)
    mesh.buyer.sync()
    value = {
        "label": "Changed",
        "encryption_key": mesh.buyer.ledger.state["accounts"][mesh.buyer.actor]["encryption_key"],
    }
    for node in mesh.nodes:
        node.sync()
    proposal = proposal_for(
        mesh, command(mesh.buyer, mesh.buyer.ledger.state, "profile", value), mesh.now
    )
    votes = [node.ledger.vote(proposal) for node in mesh.nodes[:3]]
    mesh.now += 100000
    assert mesh.nodes[0].ledger.vote(proposal) == votes[0]
    certificate = {"proposal": proposal, "votes": votes}
    for node in mesh.nodes:
        node.ledger.commit(certificate)
    before = copy.deepcopy(mesh.nodes[0].ledger.state)
    mesh.nodes[0].ledger.close()
    reopened = Ledger(mesh.root / "0", mesh.nodes[0].signing_key, clock=lambda: mesh.now)
    assert reopened.state == before
    reopened.close()


def test_admission_exhaustion_preserves_reserved_closing_capacity(lifecycle):
    from rynmesh.exchange.protocol import MAX_OPERATIONS, deadline, settlement_commitment

    mesh, order_id, snapshots = lifecycle
    state = copy.deepcopy(snapshots["working"])
    remaining = state["orders"][order_id]["remaining_ops"]
    state["operations"] = {f"retained-{i}": "hash" for i in range(MAX_OPERATIONS - remaining)}
    with pytest.raises(ExchangeError, match="admission_exhausted"):
        transition(
            mesh,
            mesh.buyer,
            "profile",
            {
                "label": "No space",
                "encryption_key": state["accounts"][mesh.buyer.actor]["encryption_key"],
            },
            state=state,
        )
    order = state["orders"][order_id]
    closed = transition(
        mesh,
        mesh.buyer,
        "timeout",
        {"order_id": order_id, "settlement_hash": settlement_commitment(order)},
        state=state,
        timestamp=deadline(order),
    )
    assert closed["accounts"][mesh.buyer.actor]["held"] == 0
    assert len(closed["operations"]) <= MAX_OPERATIONS


def test_identity_quota_does_not_block_refund(lifecycle):
    from rynmesh.exchange.protocol import MAX_ACCOUNT_ADMISSIONS

    mesh, order_id, snapshots = lifecycle
    state = copy.deepcopy(snapshots["working"])
    state["accounts"][mesh.buyer.actor]["admissions"] = MAX_ACCOUNT_ADMISSIONS
    with pytest.raises(ExchangeError, match="account_quota"):
        transition(
            mesh,
            mesh.buyer,
            "profile",
            {
                "label": "Another profile",
                "encryption_key": state["accounts"][mesh.buyer.actor]["encryption_key"],
            },
            state=state,
        )
    refunded = transition(mesh, mesh.buyer, "refund", {"order_id": order_id}, state=state)
    refunded = transition(mesh, mesh.provider, "refund", {"order_id": order_id}, state=refunded)
    assert refunded["orders"][order_id]["status"] == "refunded"


def test_storage_budget_rejects_new_activity_before_persisting_proposal_but_closes(
    tmp_path, monkeypatch
):
    import rynmesh.exchange.ledger as ledger_module

    mesh = Mesh(tmp_path)
    order_id = mesh.order()
    monkeypatch.setattr(ledger_module, "MAX_DATABASE_BYTES", 1)
    before = mesh.buyer.ledger.height
    with pytest.raises(ExchangeError, match="admission_exhausted"):
        mesh.buyer.action(
            "listing",
            {
                "kind": "offer",
                "category": "work",
                "title": "New work",
                "description": "Blocked before escrow",
                "price": "0.1",
            },
            new_id(),
        )
    assert not mesh.buyer.ledger.intents()
    assert all(not node.ledger.pending() for node in mesh.nodes[:4])
    mesh.buyer.action("refund", {"order_id": order_id}, new_id())
    mesh.provider.action("refund", {"order_id": order_id}, new_id())
    mesh.buyer.sync()
    assert mesh.buyer.ledger.height == before + 2 and mesh.buyer.status()["wallet"]["held"] == 0


def test_delivery_revisions_refunds_and_waivers_are_bounded(tmp_path):
    from test_exchange import reviewed

    mesh = Mesh(tmp_path, judges=True)
    order_id = mesh.order()
    for i in range(3):
        mesh.provider.action("deliver", {"order_id": order_id, "body": f"Revision {i}"}, new_id())
    first = mesh.provider.ledger.state["orders"][order_id]["review_until"]
    with pytest.raises(ExchangeError, match="revision_limit"):
        mesh.provider.action("deliver", {"order_id": order_id, "body": "Fourth revision"}, new_id())
    assert mesh.provider.ledger.state["orders"][order_id]["review_until"] == first
    mesh.buyer.action("refund", {"order_id": order_id}, new_id())
    with pytest.raises(ExchangeError, match="already_requested"):
        mesh.buyer.action("refund", {"order_id": order_id}, new_id())
    mesh.buyer.action("dispute", {"order_id": order_id}, new_id())
    mesh.buyer.action("evidence", {"order_id": order_id, "body": "Buyer"}, new_id())
    mesh.provider.action("evidence", {"order_id": order_id, "body": "Provider"}, new_id())
    mesh.buyer.action("rule", {"order_id": order_id}, new_id())
    mesh.buyer.action("waive", reviewed(mesh.buyer, order_id), new_id())
    with pytest.raises(ExchangeError, match="already_requested"):
        mesh.buyer.action("waive", reviewed(mesh.buyer, order_id), new_id())


def test_work_cache_cannot_be_filled_by_changing_job_ids(tmp_path):
    mesh = Mesh(tmp_path)
    endpoint = mesh.endpoint(4)
    job_id = new_id()
    assert (
        len(
            mesh.buyer.prepare("reward", {"job_id": job_id, "endpoint": endpoint}, new_id())[
                "receipts"
            ]
        )
        == 4
    )
    for _ in range(5):
        assert (
            mesh.buyer.prepare("reward", {"job_id": new_id(), "endpoint": endpoint}, new_id())[
                "receipts"
            ]
            == []
        )
    for node in mesh.nodes[:4]:
        assert node.ledger.db.execute("SELECT count(*) FROM work").fetchone()[0] == 1
    mesh.now += 601
    assert (
        len(
            mesh.buyer.prepare("reward", {"job_id": new_id(), "endpoint": endpoint}, new_id())[
                "receipts"
            ]
        )
        == 4
    )


def test_uncertain_model_attempts_are_bounded_and_timeout_remains_available(tmp_path):
    from test_exchange import reviewed

    from rynmesh.exchange.protocol import deadline

    mesh = Mesh(tmp_path, judges=True)
    order_id = mesh.dispute()
    mesh.uncertain = True
    mesh.buyer.sync()
    for _ in range(3):
        with pytest.raises(ExchangeError):
            mesh.buyer.prepare("rule", {"order_id": order_id}, new_id())
    calls = len(mesh.model_calls)
    with pytest.raises(ExchangeError):
        mesh.buyer.prepare("rule", {"order_id": order_id}, new_id())
    assert len(mesh.model_calls) == calls == 9
    mesh.buyer.sync()
    mesh.now = deadline(mesh.buyer.ledger.state["orders"][order_id])
    mesh.buyer.action("timeout", reviewed(mesh.buyer, order_id), new_id())
    assert mesh.buyer.status()["wallet"]["held"] == 0
    assert (
        all(
            node.ledger.db.execute(
                "SELECT count(*) FROM attempts WHERE id LIKE 'case:%'"
            ).fetchone()[0]
            == 0
            for node in mesh.nodes[6:]
        )
        is False
    )
    # Explicit refresh reclaims stale local judge caches without a worker.
    for node in mesh.nodes[6:]:
        node.sync()
        assert (
            node.ledger.db.execute(
                "SELECT count(*) FROM attempts WHERE id LIKE 'case:%'"
            ).fetchone()[0]
            == 0
        )


def test_private_registry_probe_requires_explicit_operator_optin(tmp_path):
    mesh = Mesh(tmp_path)
    validator = mesh.nodes[0]
    validator.allow_loopback = False
    claim = mesh.buyer.signed(
        {
            "version": VERSION,
            "kind": "work-request",
            "network": mesh.buyer.ledger.network,
            "job_id": new_id(),
            "provider": mesh.buyer.actor,
            "endpoint": "http://192.168.1.250:9000",
        }
    )
    with pytest.raises(ExchangeError, match="endpoint_blocked"):
        validator.work_receipt(claim)
    validator.options({"registry_lan": True})
    # Once opted in it reaches transport, rather than rejection by policy.
    with pytest.raises(KeyError):
        validator.work_receipt(claim)


def test_legacy_certificates_including_reset_bug_replay_without_enabling_spending(tmp_path):
    import json

    from rynmesh.crypto import canonical_json, sha256_bytes
    from rynmesh.exchange import legacy_v1
    from rynmesh.exchange.ledger import Ledger
    from rynmesh.exchange.service import Exchange
    from rynmesh.services.peer_box import public_key_b64

    mesh = Mesh(tmp_path / "identities", judges=True)
    config = copy.deepcopy(mesh.config)
    config["version"] = legacy_v1.VERSION
    config["covenant"] = legacy_v1.COVENANT
    network = sha256_bytes(canonical_json(config))
    state = legacy_v1.initial_state(config)
    records = []
    parent = network

    def record(node, action, value, op=None):
        nonlocal state, parent
        op = op or new_id()
        cmd = node.signed(
            {
                "version": legacy_v1.VERSION,
                "network": network,
                "id": op,
                "actor": node.actor,
                "nonce": state["accounts"].get(node.actor, {}).get("nonce", 0) + 1,
                "action": action,
                "value": value,
            }
        )
        core = {
            "version": legacy_v1.VERSION,
            "network": network,
            "height": len(records) + 1,
            "parent": parent,
            "timestamp": mesh.now,
            "command": cmd,
        }
        proposal = {**core, "proposer": mesh.nodes[len(records) % 4].signed(core)}
        digest = sha256_bytes(canonical_json(proposal))
        votes = [
            n.signed(
                {
                    "version": legacy_v1.VERSION,
                    "kind": "approval",
                    "network": network,
                    "height": core["height"],
                    "hash": digest,
                }
            )
            for n in mesh.nodes[:3]
        ]
        records.append((core["height"], digest, json.dumps({"proposal": proposal, "votes": votes})))
        state = legacy_v1.apply(state, cmd, config, mesh.now)
        parent = digest
        return op

    for node in (mesh.buyer, mesh.provider):
        record(
            node,
            "profile",
            {"label": "Legacy participant", "encryption_key": public_key_b64(node.messaging_key)},
        )
        job = new_id()
        receipts = [
            n.signed(
                {
                    "version": legacy_v1.VERSION,
                    "kind": "registry-work",
                    "network": network,
                    "job_id": job,
                    "provider": node.actor,
                    "work_hash": legacy_v1.work_hash(config, job, node.actor),
                }
            )
            for n in mesh.nodes[:3]
        ]
        record(node, "reward", {"job_id": job, "receipts": receipts})
    listing = record(
        mesh.buyer,
        "listing",
        {
            "kind": "request",
            "category": "translation",
            "title": "Legacy work",
            "description": "Preserve old commitments",
            "price": 250000,
        },
    )
    proposal = record(
        mesh.provider, "propose", {"listing_id": listing, "scope": "Old scope", "price": 250000}
    )
    terms = legacy_v1.terms_for(config, state["proposals"][proposal], state["listings"][listing])
    order_id = record(
        mesh.buyer,
        "agree",
        {"proposal_id": proposal, "terms_hash": sha256_bytes(canonical_json(terms))},
    )
    record(
        mesh.provider,
        "deliver",
        {
            "order_id": order_id,
            "hash": "sha256:" + "0" * 64,
            "sealed": {"nonce": "old", "ciphertext": "old"},
        },
    )
    record(mesh.buyer, "dispute", {"order_id": order_id})
    for round_number in (0, 1):
        if round_number:
            record(mesh.buyer, "appeal", {"order_id": order_id})
        panel = legacy_v1.judge_panel(config, state["orders"][order_id], round_number)
        for node in (mesh.buyer, mesh.provider):
            record(
                node,
                "evidence",
                {
                    "order_id": order_id,
                    "commitment": "sha256:" + "0" * 64,
                    "sealed": {j["peer_id"]: {"nonce": "old", "ciphertext": "old"} for j in panel},
                },
            )
        case_hash = sha256_bytes(
            canonical_json(legacy_v1.case_payload(config, state["orders"][order_id], round_number))
        )
        rulings = [
            next(n for n in mesh.nodes if n.peer_id == j["peer_id"]).signed(
                {
                    "version": legacy_v1.VERSION,
                    "kind": "ruling",
                    "network": network,
                    "case_hash": case_hash,
                    "model": j["model"],
                    "share_bps": 10000,
                    "reason_code": "delivery_matches",
                }
            )
            for j in panel
        ]
        record(mesh.buyer, "rule", {"order_id": order_id, "rulings": rulings})
    record(mesh.buyer, "dispute", {"order_id": order_id})
    assert (
        state["orders"][order_id]["round"] == 0 and state["orders"][order_id]["buyer_reserve"] == 0
    )
    home = tmp_path / "legacy"
    ledger = Ledger(home, mesh.buyer.signing_key, clock=lambda: mesh.now)
    ledger.set_setting("manifest", config)
    with ledger.db:
        ledger.db.executemany("INSERT INTO blocks VALUES (?,?,?)", records)
    ledger.close()
    legacy = Exchange(
        home,
        mesh.buyer.signing_key,
        mesh.buyer.messaging_key,
        allow_loopback=True,
        clock=lambda: mesh.now,
    )
    assert legacy.ledger.state == state and legacy.status()["read_only"] is True
    for action in (
        lambda: legacy.action("dispute", {"order_id": order_id}, new_id()),
        lambda: legacy.resume(),
        lambda: legacy.ledger.vote({}),
    ):
        with pytest.raises(ExchangeError, match="legacy_read_only"):
            action()
    with pytest.raises(ExchangeError, match="network_changed"):
        legacy.configure(mesh.config)
    assert legacy.ledger.state == state
    legacy.ledger.close()


def test_waiver_prevents_that_party_from_reopening_the_appeal(lifecycle):
    from rynmesh.exchange.protocol import settlement_commitment

    mesh, order_id, snapshots = lifecycle
    state = snapshots["initial_ruling"]
    waived = transition(
        mesh,
        mesh.buyer,
        "waive",
        {"order_id": order_id, "settlement_hash": settlement_commitment(state["orders"][order_id])},
        state=state,
    )
    with pytest.raises(ExchangeError):
        transition(mesh, mesh.buyer, "appeal", {"order_id": order_id}, state=waived)
    appealed = transition(mesh, mesh.provider, "appeal", {"order_id": order_id}, state=waived)
    assert appealed["orders"][order_id]["round"] == 1


def test_new_vote_cannot_finalize_a_future_deadline_even_within_clock_skew(tmp_path):
    from rynmesh.exchange.protocol import settlement_commitment

    mesh = Mesh(tmp_path, judges=True)
    order_id = mesh.dispute()
    mesh.buyer.action("rule", {"order_id": order_id}, new_id())
    for node in mesh.nodes:
        node.sync()
    order = mesh.buyer.ledger.state["orders"][order_id]
    mesh.now = order["appeal_until"] - 1
    value = {"order_id": order_id, "settlement_hash": settlement_commitment(order)}
    proposal = proposal_for(
        mesh, command(mesh.buyer, mesh.buyer.ledger.state, "finalize", value), order["appeal_until"]
    )
    for node in mesh.nodes[:4]:
        with pytest.raises(ExchangeError, match="timeout_open"):
            node.ledger.vote(proposal)


def test_stale_partial_proposals_keep_locks_and_are_not_silently_retimestamped(tmp_path):
    mesh = Mesh(tmp_path)
    mesh.buyer.sync()
    value = {
        "label": "Delayed profile",
        "encryption_key": mesh.buyer.ledger.state["accounts"][mesh.buyer.actor]["encryption_key"],
    }
    proposal = proposal_for(
        mesh, command(mesh.buyer, mesh.buyer.ledger.state, "profile", value), mesh.now
    )
    first = mesh.nodes[0].ledger.vote(proposal)
    mesh.now += 31
    assert mesh.nodes[0].ledger.vote(proposal) == first
    with pytest.raises(ExchangeError, match="time_invalid"):
        mesh.nodes[1].ledger.vote(proposal)
    assert mesh.nodes[0].ledger.pending() == [proposal]
