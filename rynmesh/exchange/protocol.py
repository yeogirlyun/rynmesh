"""Public, deterministic exchange rules. No fiat or reputation conversion."""

from __future__ import annotations

import copy
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from rynmesh.crypto import SignedPayload, canonical_json, sha256_bytes, verify_signed_payload

VERSION = "ryn.exchange.v2"
LEGACY_VERSION = "ryn.exchange.v1"
MAX_OPERATIONS = 10000
MAX_ACCOUNT_ADMISSIONS = 100
ORDER_OPERATION_RESERVE = 24
MAX_OPEN_ORDERS = 6
CLOCK_SKEW = 30
DELIVERY_WINDOW = 7 * 86400
REVIEW_WINDOW = 2 * 86400
CASE_WINDOW = 2 * 86400
TERMINAL = {"accepted", "refunded", "resolved"}
ORDER_ACTIONS = {
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
}
UNITS = 1_000_000
MAX_MINOR = 10**15
COVENANT = {
    "version": "ryn.covenant.digital.v2",
    "principles": [
        "free participation",
        "no platform commission",
        "direct agreed prices",
        "honest delivery",
        "lawful ethical work",
        "consent and rights",
        "public rules and audit",
        "private keys and deliverables",
        "independent AI appeals",
    ],
    "acceptance": "The buyer accepts delivery or both parties agree a refund. Disputed outcomes use independent model receipts. Accepted deadlines allow explicit timeout settlement: undelivered work is refunded; delivered work is paid after review; an unresolved initial dispute with delivery splits the price equally (without delivery it refunds); an unresolved appeal retains the first ruling. Committed judge fees remain paid.",
}


class ExchangeError(ValueError):
    pass


def require(condition: Any, code: str = "exchange_invalid") -> None:
    if not condition:
        raise ExchangeError(code)


def text(value: Any, limit: int = 2000, *, empty: bool = False) -> str:
    require(isinstance(value, str) and len(value.encode("utf-8")) <= limit)
    require(empty or bool(value.strip()))
    return value


def integer(value: Any, maximum: int = MAX_MINOR) -> int:
    require(type(value) is int and 0 <= value <= maximum)
    return value


def amount(value: str) -> int:
    try:
        number = Decimal(value)
        require(number.is_finite() and number >= 0 and number <= MAX_MINOR / UNITS)
        scaled = number * UNITS
        require(scaled == scaled.to_integral_value(), "exchange_precision")
        return int(scaled)
    except (InvalidOperation, TypeError, ValueError):
        raise ExchangeError("exchange_amount_invalid") from None


def identifier(value: Any) -> str:
    require(isinstance(value, str) and re.fullmatch("[a-zA-Z0-9_-]{8,80}", value))
    return value


def key(value: Any) -> str:
    import base64

    try:
        require(isinstance(value, str))
        raw = base64.b64decode(value, validate=True)
        require(len(raw) == 32 and base64.b64encode(raw).decode() == value)
    except (ValueError, TypeError):
        raise ExchangeError("exchange_key_invalid") from None
    return value


def signature(wire: dict) -> SignedPayload:
    try:
        signed = SignedPayload.from_dict(wire)
        verify_signed_payload(signed)
        key(signed.public_key)
        require(len(canonical_json(signed.payload)) <= 256 * 1024, "exchange_capacity")
        return signed
    except ExchangeError:
        raise
    except Exception:
        raise ExchangeError("exchange_signature_invalid") from None


def genesis(value: dict) -> dict:
    if isinstance(value, dict) and value.get("version") == LEGACY_VERSION:
        from .legacy_v1 import genesis as legacy_genesis

        return legacy_genesis(value)
    require(
        isinstance(value, dict)
        and set(value)
        == {
            "version",
            "name",
            "validators",
            "judges",
            "issuance_limit",
            "work_reward",
            "appeal_window_s",
            "covenant",
        }
    )
    require(value["version"] == VERSION and value["covenant"] == COVENANT)
    text(value["name"], 100)
    validators = value["validators"]
    judges = value["judges"]
    require(isinstance(validators, list) and 4 <= len(validators) <= 8)
    require(isinstance(judges, list) and (len(judges) == 0 or 6 <= len(judges) <= 12))
    seen = set()
    for entry in validators + judges:
        require(isinstance(entry, dict))
        require(
            set(entry)
            == (
                {"peer_id", "endpoint"}
                if entry in validators
                else {"peer_id", "endpoint", "encryption_key", "model", "fee"}
            )
        )
        peer = key(entry["peer_id"])
        require(peer not in seen)
        seen.add(peer)
        text(entry["endpoint"], 2048)
        if entry in judges:
            key(entry["encryption_key"])
            text(entry["model"], 100)
            integer(entry["fee"], UNITS)
    if judges:
        require(
            len({entry["model"] for entry in judges}) == len(judges),
            "exchange_independent_models_required",
        )
    integer(value["issuance_limit"], 1_000_000 * UNITS)
    integer(value["work_reward"], 100 * UNITS)
    require(0 < value["work_reward"] <= value["issuance_limit"])
    require(type(value["appeal_window_s"]) is int and 60 <= value["appeal_window_s"] <= 604800)
    return copy.deepcopy(value)


def initial_state(config: dict) -> dict:
    return {
        "accounts": {},
        "listings": {},
        "proposals": {},
        "orders": {},
        "issued": 0,
        "rewards": {},
        "operations": {},
        "time": 0,
        "covenant": config["covenant"],
    }


def account(state: dict, owner: str) -> dict:
    return state["accounts"].setdefault(
        owner,
        {
            "available": 0,
            "held": 0,
            "earned": 0,
            "encryption_key": "",
            "label": "",
            "devices": [],
            "nonce": 0,
            "admissions": 0,
        },
    )


def hold(state: dict, owner: str, value: int) -> None:
    row = account(state, owner)
    require(row["available"] >= value, "exchange_insufficient_balance")
    row["available"] -= value
    row["held"] += value


def unhold(state: dict, owner: str, value: int) -> None:
    row = account(state, owner)
    require(row["held"] >= value, "exchange_balance_invariant")
    row["held"] -= value
    row["available"] += value


def earn(state: dict, owner: str, value: int) -> None:
    row = account(state, owner)
    require(row["available"] + value <= MAX_MINOR, "exchange_capacity")
    row["available"] += value
    row["earned"] += value


def fees(config: dict) -> tuple[int, int]:
    rounds = [sum(j["fee"] for j in config["judges"][start : start + 3]) for start in (0, 3)]
    return sum((fee + 1) // 2 for fee in rounds), sum(fee // 2 for fee in rounds)


def close(state: dict, order: dict, paid: int, status: str) -> None:
    price = order["price"]
    require(0 <= paid <= price)
    unhold(state, order["buyer"], price + order["buyer_reserve"])
    unhold(state, order["provider"], order["provider_reserve"])
    account(state, order["buyer"])["available"] -= paid
    earn(state, order["provider"], paid)
    order.update(status=status, paid=paid, buyer_reserve=0, provider_reserve=0, remaining_ops=0)


def judge_panel(config: dict, order: dict, round_number: int) -> list[dict]:
    require(round_number in {0, 1})
    if "terms" in order:
        eligible = order["terms"]["judges"]
    else:
        eligible = [
            j for j in config["judges"] if j["peer_id"] not in {order["buyer"], order["provider"]}
        ]
    require(len(eligible) >= 6, "exchange_judges_unavailable")
    panel = eligible[3 * round_number : 3 * (round_number + 1)]
    require(
        all(j["peer_id"] not in {order["buyer"], order["provider"]} for j in panel),
        "exchange_judge_conflict",
    )
    return panel


def case_payload(config: dict, order: dict, round_number: int) -> dict:
    return {
        "version": VERSION,
        "kind": "case",
        "order_id": order["id"],
        "round": round_number,
        "terms": order["terms"],
        "delivery_hash": order.get("delivery_hash", ""),
        "deadline": order["case_until"],
        "previous_ruling_share": order.get("ruling_share") if round_number == 1 else None,
        "evidence": order["evidence"],
        "covenant": config["covenant"],
    }


def deadline(order: dict) -> int:
    return order[
        {
            "working": "delivery_until",
            "delivered": "review_until",
            "disputed": "case_until",
            "ruling_ready": "appeal_until",
        }[order["status"]]
    ]


def order_commitment(order: dict) -> str:
    # Includes the decreasing lifecycle reserve: an old context cannot recur.
    return sha256_bytes(canonical_json(order))


def settlement_commitment(order: dict) -> str:
    return sha256_bytes(
        canonical_json(
            {
                k: order.get(k)
                for k in (
                    "id",
                    "status",
                    "round",
                    "price",
                    "terms",
                    "delivery_hash",
                    "delivery_until",
                    "review_until",
                    "case_until",
                    "appeal_until",
                    "ruling_share",
                    "rulings",
                )
            }
        )
    )


def vote_deadlines(state: dict, command: dict, now: int) -> None:
    """Local clock guards for first votes only; historical replay has no wall clock."""
    action, value = command["action"], command["value"]
    order = state["orders"].get(value.get("order_id"))
    if not order or order["status"] in TERMINAL:
        return
    if action in {"appeal", "dispute", "evidence", "rule", "deliver"}:
        require(now < deadline(order), "exchange_deadline_expired")
    if action == "deliver":
        require(now < order["delivery_until"], "exchange_deadline_expired")
    if action == "timeout" or (action == "finalize" and len(order["waivers"]) != 2):
        require(now >= deadline(order), "exchange_timeout_open")


def apply(state: dict, command_wire: dict, config: dict, timestamp: int) -> dict:
    """Pure transition: validators and consumers run exactly the same rules."""
    result = copy.deepcopy(state)
    signed = signature(command_wire)
    cmd = signed.payload
    require(set(cmd) == {"version", "network", "id", "actor", "nonce", "action", "value"})
    require(cmd["version"] == VERSION and cmd["network"] == sha256_bytes(canonical_json(config)))
    op_id = identifier(cmd["id"])
    owner = key(cmd["actor"])
    row = account(result, owner)
    require(
        signed.public_key == owner or signed.public_key in row["devices"], "exchange_unauthorized"
    )
    require(op_id not in result["operations"], "exchange_operation_exists")
    require(integer(cmd["nonce"]) == row["nonce"] + 1, "exchange_nonce_changed")
    require(type(timestamp) is int and timestamp >= result["time"])
    action = cmd["action"]
    value = cmd["value"]
    require(isinstance(value, dict))
    if action not in ORDER_ACTIONS:
        reserved = sum(
            o["remaining_ops"] for o in result["orders"].values() if o["status"] not in TERMINAL
        )
        needed = 1 + (ORDER_OPERATION_RESERVE if action == "agree" else 0)
        require(
            len(result["operations"]) + reserved + needed <= MAX_OPERATIONS,
            "exchange_admission_exhausted",
        )
        require(row["admissions"] < MAX_ACCOUNT_ADMISSIONS, "exchange_account_quota")
        row["admissions"] += 1
    if action == "profile":
        require(set(value) == {"label", "encryption_key"})
        row["label"] = text(value["label"], 100)
        # Changing encryption keys would strand existing encrypted work.
        encryption_key = key(value["encryption_key"])
        require(
            not row["encryption_key"] or row["encryption_key"] == encryption_key,
            "exchange_encryption_key_changed",
        )
        row["encryption_key"] = encryption_key
    elif action == "device":
        require(set(value) == {"peer_id", "allowed"} and signed.public_key == owner)
        peer = key(value["peer_id"])
        require(type(value["allowed"]) is bool)
        if value["allowed"]:
            require(len(row["devices"]) < 10, "exchange_capacity")
            if peer not in row["devices"]:
                row["devices"].append(peer)
        elif peer in row["devices"]:
            row["devices"].remove(peer)
    elif action == "transfer":
        require(set(value) == {"recipient", "amount"})
        recipient = key(value["recipient"])
        require(
            recipient != owner and account(result, recipient)["encryption_key"],
            "exchange_recipient_invalid",
        )
        paid = integer(value["amount"])
        require(paid > 0 and row["available"] >= paid, "exchange_insufficient_balance")
        row["available"] -= paid
        earn(result, recipient, paid)
    elif action == "listing":
        require(set(value) == {"kind", "category", "title", "description", "price"})
        require(value["kind"] in {"offer", "request"})
        require(row["encryption_key"], "exchange_profile_required")
        result["listings"][op_id] = {
            "id": op_id,
            "owner": owner,
            "kind": value["kind"],
            "category": text(value["category"], 100),
            "title": text(value["title"], 200),
            "description": text(value["description"], 4000),
            "price": integer(value["price"]),
            "active": True,
            "created_at": timestamp,
        }
    elif action == "withdraw":
        require(set(value) == {"listing_id"})
        listing = result["listings"].get(value["listing_id"])
        require(listing and listing["owner"] == owner, "exchange_unauthorized")
        require(listing["active"], "exchange_listing_closed")
        listing["active"] = False
    elif action == "propose":
        require(set(value) == {"listing_id", "scope", "price"})
        listing = result["listings"].get(value["listing_id"])
        require(listing and listing["active"], "exchange_listing_unavailable")
        require(row["encryption_key"] and listing["owner"] != owner, "exchange_profile_required")
        buyer, provider = (
            (listing["owner"], owner) if listing["kind"] == "request" else (owner, listing["owner"])
        )
        if config["judges"]:
            judge_panel(config, {"buyer": buyer, "provider": provider}, 0)
            judge_panel(config, {"buyer": buyer, "provider": provider}, 1)
        result["proposals"][op_id] = {
            "id": op_id,
            "listing_id": listing["id"],
            "buyer": buyer,
            "provider": provider,
            "proposed_by": owner,
            "scope": text(value["scope"], 4000),
            "price": integer(value["price"]),
            "status": "offered",
        }
    elif action == "agree":
        require(set(value) == {"proposal_id", "terms_hash"})
        proposal = result["proposals"].get(value["proposal_id"])
        require(proposal and proposal["status"] == "offered", "exchange_proposal_unavailable")
        require(
            owner in {proposal["buyer"], proposal["provider"]} and owner != proposal["proposed_by"],
            "exchange_unauthorized",
        )
        listing = result["listings"][proposal["listing_id"]]
        require(listing["active"], "exchange_listing_unavailable")
        require(
            sum(
                1
                for o in result["orders"].values()
                if o["status"] not in {"accepted", "refunded", "resolved"}
                and (
                    o["buyer"] in {proposal["buyer"], proposal["provider"]}
                    or o["provider"] in {proposal["buyer"], proposal["provider"]}
                )
            )
            < 100,
            "exchange_open_orders_limit",
        )
        terms = terms_for(config, proposal, listing)
        require(
            value["terms_hash"] == sha256_bytes(canonical_json(terms)), "exchange_terms_changed"
        )
        require(
            sum(o["status"] not in TERMINAL for o in result["orders"].values()) < MAX_OPEN_ORDERS,
            "exchange_open_orders_limit",
        )
        buyer_fee, provider_fee = terms["buyer_dispute_reserve"], terms["provider_dispute_reserve"]
        hold(result, proposal["buyer"], proposal["price"] + buyer_fee)
        hold(result, proposal["provider"], provider_fee)
        proposal["status"] = "accepted"
        result["orders"][op_id] = {
            "id": op_id,
            "proposal_id": proposal["id"],
            "buyer": proposal["buyer"],
            "provider": proposal["provider"],
            "terms": terms,
            "price": proposal["price"],
            "buyer_reserve": buyer_fee,
            "provider_reserve": provider_fee,
            "status": "working",
            "evidence": {},
            "refund_requests": [],
            "waivers": [],
            "created_at": timestamp,
            "delivery_until": timestamp + terms["delivery_window_s"],
            "remaining_ops": ORDER_OPERATION_RESERVE,
            "delivery_revisions": 0,
        }
    elif action == "reward":
        require(set(value) == {"job_id", "receipts"})
        job_id = identifier(value["job_id"])
        require(job_id not in result["rewards"], "exchange_reward_duplicate")
        require(owner not in result["rewards"].values(), "exchange_registry_already_rewarded")
        receipts = value["receipts"]
        require(isinstance(receipts, list) and len(receipts) <= 8)
        witnesses = set()
        for wire in receipts:
            receipt = signature(wire)
            payload = receipt.payload
            require(
                receipt.public_key in {v["peer_id"] for v in config["validators"]}
                and receipt.public_key != owner
            )
            require(
                set(payload) == {"version", "kind", "network", "job_id", "provider", "work_hash"}
            )
            require(
                payload
                == {
                    "version": VERSION,
                    "kind": "registry-work",
                    "network": cmd["network"],
                    "job_id": job_id,
                    "provider": owner,
                    "work_hash": work_hash(config, job_id, owner),
                }
            )
            witnesses.add(receipt.public_key)
        require(len(witnesses) >= quorum(config), "exchange_work_unverified")
        reward = config["work_reward"]
        require(
            result["issued"] + reward <= config["issuance_limit"], "exchange_issuance_exhausted"
        )
        earn(result, owner, reward)
        result["issued"] += reward
        result["rewards"][job_id] = owner
    else:
        order = result["orders"].get(value.get("order_id"))
        require(
            order and owner in {order["buyer"], order["provider"]}, "exchange_order_unavailable"
        )
        require(value.get("reviewed_order") == order_commitment(order), "exchange_order_changed")
        require(order["status"] not in TERMINAL, "exchange_order_closed")
        value = {k: v for k, v in value.items() if k != "reviewed_order"}
        require(action in ORDER_ACTIONS, "exchange_action_invalid")
        require(order["remaining_ops"] > 0, "exchange_order_capacity")
        order["remaining_ops"] -= 1
        if action == "deliver":
            require(set(value) == {"order_id", "hash", "sealed"})
            require(
                owner == order["provider"] and order["status"] in {"working", "delivered"},
                "exchange_unauthorized",
            )
            require(timestamp < order["delivery_until"], "exchange_delivery_expired")
            require(order["delivery_revisions"] < 3, "exchange_delivery_revision_limit")
            order["delivery_revisions"] += 1
            if order["status"] == "working":
                order["review_until"] = timestamp + order["terms"]["review_window_s"]
            require(timestamp < order["review_until"], "exchange_review_expired")
            require(re.fullmatch("sha256:[a-f0-9]{64}", value["hash"]))
            sealed = value["sealed"]
            require(isinstance(sealed, dict) and set(sealed) == {"nonce", "ciphertext"})
            text(sealed["nonce"], 100)
            text(sealed["ciphertext"], 64 * 1024)
            order.update(status="delivered", delivery_hash=value["hash"], delivery=sealed)
        elif action == "accept":
            require(
                set(value) == {"order_id", "delivery_hash"}
                and owner == order["buyer"]
                and order["status"] == "delivered",
                "exchange_unauthorized",
            )
            require(value["delivery_hash"] == order["delivery_hash"], "exchange_delivery_changed")
            close(result, order, order["price"], "accepted")
        elif action == "refund":
            require(set(value) == {"order_id"})
            require(owner not in order["refund_requests"], "exchange_refund_already_requested")
            order["refund_requests"].append(owner)
            if len(order["refund_requests"]) == 2:
                close(result, order, 0, "refunded")
        elif action == "dispute":
            require(
                set(value) == {"order_id"} and order["status"] in {"working", "delivered"},
                "exchange_dispute_state",
            )
            require(timestamp < deadline(order), "exchange_case_expired")
            judge_panel(config, order, 0)
            judge_panel(config, order, 1)
            order.update(
                status="disputed",
                round=0,
                evidence={},
                waivers=[],
                case_until=timestamp + order["terms"]["case_window_s"],
            )
        elif action == "evidence":
            require(set(value) == {"order_id", "commitment", "sealed"})
            require(order["status"] == "disputed")
            require(timestamp < order["case_until"], "exchange_case_expired")
            require(owner not in order["evidence"], "exchange_evidence_locked")
            require(re.fullmatch("sha256:[a-f0-9]{64}", value["commitment"]))
            panel = judge_panel(config, order, order["round"])
            require(
                isinstance(value["sealed"], dict)
                and set(value["sealed"]) == {j["peer_id"] for j in panel}
            )
            for entry in value["sealed"].values():
                require(set(entry) == {"nonce", "ciphertext"})
                text(entry["nonce"], 100)
                text(entry["ciphertext"], 64 * 1024)
            order["evidence"][owner] = {
                "commitment": value["commitment"],
                "sealed": value["sealed"],
            }
        elif action == "rule":
            require(set(value) == {"order_id", "rulings"} and order["status"] == "disputed")
            require(
                set(order["evidence"]) == {order["buyer"], order["provider"]},
                "exchange_evidence_incomplete",
            )
            require(timestamp < order["case_until"], "exchange_case_expired")
            panel = judge_panel(config, order, order["round"])
            expected_hash = sha256_bytes(
                canonical_json(case_payload(config, order, order["round"]))
            )
            require(isinstance(value["rulings"], list) and len(value["rulings"]) == 3)
            judges = {j["peer_id"]: j for j in panel}
            seen = set()
            shares = []
            for wire in value["rulings"]:
                receipt = signature(wire)
                ruling = receipt.payload
                require(receipt.public_key in judges and receipt.public_key not in seen)
                require(
                    set(ruling)
                    == {
                        "version",
                        "kind",
                        "network",
                        "case_hash",
                        "model",
                        "share_bps",
                        "reason_code",
                    }
                )
                require(
                    ruling["version"] == VERSION
                    and ruling["kind"] == "ruling"
                    and ruling["network"] == cmd["network"]
                )
                require(
                    ruling["case_hash"] == expected_hash
                    and ruling["model"] == judges[receipt.public_key]["model"]
                )
                require(
                    ruling["reason_code"]
                    in {"delivery_matches", "delivery_missing", "partial_delivery", "terms_not_met"}
                )
                shares.append(integer(ruling["share_bps"], 10000))
                seen.add(receipt.public_key)
            # Material disagreement never silently transfers funds.
            require(max(shares) - min(shares) <= 1000, "exchange_models_disagree")
            paid_fee = sum(j["fee"] for j in panel)
            buyer_fee = (paid_fee + 1) // 2
            provider_fee = paid_fee // 2
            require(
                order["buyer_reserve"] >= buyer_fee and order["provider_reserve"] >= provider_fee
            )
            account(result, order["buyer"])["held"] -= buyer_fee
            account(result, order["provider"])["held"] -= provider_fee
            order["buyer_reserve"] -= buyer_fee
            order["provider_reserve"] -= provider_fee
            for judge in panel:
                earn(result, judge["peer_id"], judge["fee"])
            order.update(
                status="ruling_ready",
                ruling_share=sorted(shares)[1],
                rulings=value["rulings"],
                appeal_until=timestamp + config["appeal_window_s"],
                waivers=[],
            )
        elif action == "appeal":
            require(
                set(value) == {"order_id"}
                and order["status"] == "ruling_ready"
                and order["round"] == 0
                and owner not in order["waivers"]
            )
            require(timestamp < order["appeal_until"], "exchange_appeal_expired")
            order.update(
                status="disputed",
                round=1,
                evidence={},
                waivers=[],
                case_until=timestamp + order["terms"]["case_window_s"],
            )
        elif action == "waive":
            require(
                value.get("settlement_hash") == settlement_commitment(order),
                "exchange_settlement_changed",
            )
            require(
                set(value) == {"order_id", "settlement_hash"} and order["status"] == "ruling_ready"
            )
            require(owner not in order["waivers"], "exchange_waiver_already_requested")
            order["waivers"].append(owner)
        elif action == "finalize":
            require(
                value.get("settlement_hash") == settlement_commitment(order),
                "exchange_settlement_changed",
            )
            require(
                set(value) == {"order_id", "settlement_hash"} and order["status"] == "ruling_ready"
            )
            require(
                timestamp >= order["appeal_until"] or len(order["waivers"]) == 2,
                "exchange_appeal_open",
            )
            close(result, order, order["price"] * order["ruling_share"] // 10000, "resolved")
        elif action == "timeout":
            require(set(value) == {"order_id", "settlement_hash"})
            require(
                value["settlement_hash"] == settlement_commitment(order),
                "exchange_settlement_changed",
            )
            require(timestamp >= deadline(order), "exchange_timeout_open")
            if order["status"] == "working":
                close(result, order, 0, "refunded")
            elif order["status"] == "delivered":
                close(result, order, order["price"], "accepted")
            else:
                share = (
                    order["ruling_share"]
                    if order["status"] == "ruling_ready" or order.get("round") == 1
                    else order["terms"]["dispute_timeout_share_bps"]
                    if order.get("delivery_hash")
                    else 0
                )
                close(result, order, order["price"] * share // 10000, "resolved")
        else:
            raise ExchangeError("exchange_action_invalid")
    row["nonce"] += 1
    result["operations"][op_id] = sha256_bytes(canonical_json(command_wire))
    result["time"] = timestamp
    require(
        sum(a["available"] + a["held"] for a in result["accounts"].values()) == result["issued"],
        "exchange_supply_invariant",
    )
    require(
        all(a["available"] >= 0 and a["held"] >= 0 for a in result["accounts"].values()),
        "exchange_balance_invariant",
    )
    return result


def terms_for(config: dict, proposal: dict, listing: dict) -> dict:
    if config["version"] == LEGACY_VERSION:
        from .legacy_v1 import terms_for as legacy_terms

        return legacy_terms(config, proposal, listing)
    selected = (
        judge_panel(config, proposal, 0) + judge_panel(config, proposal, 1)
        if config["judges"]
        else []
    )
    buyer_fee, provider_fee = fees({"judges": selected})
    return {
        "proposal_id": proposal["id"],
        "buyer": proposal["buyer"],
        "provider": proposal["provider"],
        "title": listing["title"],
        "category": listing["category"],
        "scope": proposal["scope"],
        "price": proposal["price"],
        "buyer_dispute_reserve": buyer_fee,
        "provider_dispute_reserve": provider_fee,
        "covenant": config["covenant"],
        "appeal_window_s": config["appeal_window_s"],
        "judges": selected,
        "delivery": "encrypted digital text, at most 32 KiB",
        "platform_commission": 0,
        "delivery_window_s": DELIVERY_WINDOW,
        "review_window_s": REVIEW_WINDOW,
        "case_window_s": CASE_WINDOW,
        "dispute_timeout_share_bps": 5000,
        "appeal_timeout": "retain first ruling",
        "evidence_policy": "one immutable statement per party per round",
        "delivery_revisions": 3,
    }


def quorum(config: dict) -> int:
    return len(config["validators"]) * 2 // 3 + 1


def work_hash(config: dict, job_id: str, provider: str) -> str:
    # A bounded registry assignment: serve this exact public network manifest
    # in response to independently signed witness challenges.
    return sha256_bytes(
        canonical_json({"job_id": job_id, "provider": provider, "manifest": config})
    )
