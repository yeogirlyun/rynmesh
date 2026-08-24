"""FastAPI routes for service publication and the private encrypted LLM data path."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException, Request

from rynmesh.crypto import SignedPayload, sign_payload, verify_signed_payload
from rynmesh.store import RynmeshStore
from rynmesh.transport import network_key_header

from .adapters import LLMAdapter, adapter_from_manifest
from .manifest import LLMPackageManifest, load_manifest
from .p2p import IceSignal, consumer_exchange, provider_exchange
from .task_balance import TaskBalanceError, TaskBalanceLedger
from .task_protocol import (
    TERMINAL_STATES,
    TaskOrderStore,
    TaskProtocolError,
    open_task,
    seal_task,
)

CAPABILITY = "rynmesh.llm.private.v1"
OPERATION = "rynmesh.llm.private.infer.v1"


def _expires(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _price(manifest: LLMPackageManifest, input_tokens: int, output_tokens: int) -> float:
    value = (
        input_tokens * manifest.pricing.input_per_1k / 1000
        + output_tokens * manifest.pricing.output_per_1k / 1000
    )
    return round(max(manifest.pricing.minimum, value), 8)


def _estimate_price(manifest: LLMPackageManifest, prompt: str, max_tokens: int) -> float:
    estimated_input = max(1, len(prompt) // 4)
    return _price(manifest, estimated_input, max_tokens)


class ProviderService:
    def __init__(self, *, manifest: LLMPackageManifest, adapter: LLMAdapter,
                 store: RynmeshStore, task_store: TaskOrderStore,
                 balance: TaskBalanceLedger, messaging_key: Any) -> None:
        self.manifest = manifest
        self.adapter = adapter
        self.store = store
        self.task_store = task_store
        self.balance = balance
        self.messaging_key = messaging_key
        self._slots = threading.BoundedSemaphore(manifest.max_concurrent)
        self._lock = threading.Lock()
        self._running = 0
        if manifest.debug_log_bodies or os.environ.get("RYNMESH_LLM_DEBUG_BODIES", "") == "1":
            print("WARNING: RYNMESH LLM task-body debug logging is enabled; prompts/outputs may be exposed.")

    def public_status(self, *, benchmark: bool = False) -> dict[str, Any]:
        health = self.adapter.health()
        result: dict[str, Any] = {
            "service": self.manifest.public_dict(), "online": bool(health.get("ok")),
            "health": {k: v for k, v in health.items() if k not in {"base_url", "path"}},
            "capacity": {"max_concurrent": self.manifest.max_concurrent,
                         "running": self._running, "available": max(0, self.manifest.max_concurrent - self._running),
                         "queue_limit": self.manifest.queue_limit, "queue_policy": "reject_when_full"},
        }
        from rynmesh.services import peer_box

        result["node_messaging_pub"] = peer_box.public_key_b64(self.messaging_key)
        if benchmark and health.get("ok"):
            measured = self.adapter.infer(
                prompt="Reply with one word: ready", max_tokens=8,
                task_id="benchmark-" + uuid.uuid4().hex, timeout_s=self.manifest.timeout_seconds,
            )
            result["benchmark"] = {
                "duration_ms": measured["duration_ms"], "input_tokens": measured["input_tokens"],
                "output_tokens": measured["output_tokens"],
            }
        return result

    def publish(self, *, network_id: str, benchmark: bool = True) -> dict[str, Any]:
        status = self.public_status(benchmark=benchmark)
        if not status["online"]:
            raise TaskProtocolError("unhealthy service cannot be published online")
        self.store.register_node(capabilities=["publish", "seed", CAPABILITY], network_id=network_id)
        return self.store.register_job_capacity(
            capabilities=[CAPABILITY], network_id=network_id,
            capacity_units=self.manifest.max_concurrent,
            max_concurrent=self.manifest.max_concurrent,
            price_credits={}, polling_interval_sec=15,
            metadata={"llm_service": status, "billing": "development_task_balance_not_credits"},
        )

    def handle(self, signed_request: dict[str, Any]) -> dict[str, Any]:
        outer, body = open_task(
            signed_request, recipient_peer_id=self.store.peer_id,
            recipient_messaging_key=self.messaging_key, expected_kind="llm_request",
        )
        task_id = str(outer["task_id"])
        existing = self.task_store.get(task_id)
        if existing and existing.get("state") in TERMINAL_STATES and existing.get("encrypted_response"):
            return dict(existing["encrypted_response"])
        if str(body.get("service_id")) != self.manifest.package_id:
            raise TaskProtocolError("requested service is not available")
        prompt = str(body.get("prompt") or "")
        if not prompt:
            raise TaskProtocolError("prompt is required")
        max_tokens = min(int(body.get("max_tokens") or 64), self.manifest.max_output_tokens)
        if max_tokens < 1:
            raise TaskProtocolError("max_tokens is invalid")
        reply_pub = str(body.get("reply_messaging_pub") or "")
        max_amount = float(body.get("max_amount") or 0)
        if max_amount > self.manifest.pricing.maximum_per_task:
            raise TaskProtocolError("task maximum exceeds provider price limit")
        metadata = {"consumer_peer_id": outer["from_peer_id"], "service_id": self.manifest.package_id,
                    "request_hash": SignedPayload.from_dict(signed_request).subject_hash}
        self.task_store.transition(task_id=task_id, state="created", metadata=metadata)
        if not self.adapter.health().get("ok"):
            return self._failure(task_id, reply_pub, outer["from_peer_id"], "rejected", "service_unhealthy")
        if not self._slots.acquire(blocking=False):
            return self._failure(task_id, reply_pub, outer["from_peer_id"], "rejected", "capacity_exhausted")
        with self._lock:
            self._running += 1
        try:
            self.task_store.transition(task_id=task_id, state="accepted", metadata=metadata)
            self.task_store.transition(task_id=task_id, state="running", metadata=metadata)
            started = time.monotonic()
            result = self.adapter.infer(
                prompt=prompt, max_tokens=max_tokens, task_id=task_id,
                timeout_s=self.manifest.timeout_seconds,
            )
            duration_ms = int(result.get("duration_ms") or (time.monotonic() - started) * 1000)
            amount = _price(self.manifest, int(result["input_tokens"]), int(result["output_tokens"]))
            if amount > max_amount:
                return self._failure(task_id, reply_pub, outer["from_peer_id"], "failed", "price_exceeds_hold")
            response_body = {
                "task_id": task_id, "state": "succeeded", "service_id": self.manifest.package_id,
                "model_alias": self.manifest.public_model_alias, "output": str(result["text"]),
                "input_tokens": int(result["input_tokens"]), "output_tokens": int(result["output_tokens"]),
                "duration_ms": duration_ms, "amount": amount, "currency": "DEV_TASK_BALANCE",
            }
            encrypted = seal_task(
                body=response_body, task_id=task_id, kind="llm_response",
                sender_peer_id=self.store.peer_id, recipient_peer_id=str(outer["from_peer_id"]),
                sender_signing_key=self.store.private_key_bytes, recipient_messaging_pub=reply_pub,
                expires_at=_expires(max(300, self.manifest.timeout_seconds * 2)),
            ).to_dict()
            self.task_store.transition(
                task_id=task_id, state="succeeded",
                metadata={**metadata, "input_tokens": response_body["input_tokens"],
                          "output_tokens": response_body["output_tokens"], "duration_ms": duration_ms,
                          "amount": amount}, encrypted_response=encrypted,
            )
            return encrypted
        except Exception as exc:
            state = "timed_out" if "timed out" in str(exc).lower() else "failed"
            return self._failure(task_id, reply_pub, outer["from_peer_id"], state,
                                 "inference_timeout" if state == "timed_out" else "inference_failed")
        finally:
            prompt = ""  # best-effort reference cleanup; see privacy documentation
            with self._lock:
                self._running -= 1
            self._slots.release()

    def _failure(self, task_id: str, reply_pub: str, consumer_peer_id: str,
                 state: str, code: str) -> dict[str, Any]:
        response = seal_task(
            body={"task_id": task_id, "state": state, "error_code": code,
                  "service_id": self.manifest.package_id},
            task_id=task_id, kind="llm_response", sender_peer_id=self.store.peer_id,
            recipient_peer_id=consumer_peer_id, sender_signing_key=self.store.private_key_bytes,
            recipient_messaging_pub=reply_pub, expires_at=_expires(300),
        ).to_dict()
        self.task_store.transition(task_id=task_id, state=state,
                                   metadata={"consumer_peer_id": consumer_peer_id,
                                             "service_id": self.manifest.package_id,
                                             "error_code": code}, encrypted_response=response)
        return response

    def settle_earning(self, signed: dict[str, Any]) -> dict[str, Any]:
        envelope = SignedPayload.from_dict(signed)
        verify_signed_payload(envelope)
        payload = envelope.payload
        if payload.get("kind") != "llm_settlement" or payload.get("to_peer_id") != self.store.peer_id:
            raise TaskProtocolError("invalid settlement recipient/kind")
        if payload.get("from_peer_id") != envelope.public_key:
            raise TaskProtocolError("settlement identity mismatch")
        task_id = str(payload.get("task_id") or "")
        record = self.task_store.get(task_id)
        if not record or record.get("state") != "succeeded":
            raise TaskProtocolError("settlement task is not succeeded")
        final = record["history"][-1]
        amount = float(payload.get("amount") or 0)
        if amount != float(final.get("amount") or 0):
            raise TaskProtocolError("settlement amount mismatch")
        return self.balance.earn(
            task_id=task_id, amount=amount, input_tokens=int(final.get("input_tokens") or 0),
            output_tokens=int(final.get("output_tokens") or 0), duration_ms=int(final.get("duration_ms") or 0),
            service_id=self.manifest.package_id, consumer_peer_id=envelope.public_key,
        )

    def cancel(self, task_id: str) -> bool:
        self.adapter.cancel(task_id)
        record = self.task_store.get(task_id)
        if record and record.get("state") not in TERMINAL_STATES:
            self.task_store.transition(task_id=task_id, state="cancelled", metadata={"reason": "consumer_cancelled"})
        return True


def _recover_consumer_orders(
    consumer_orders: TaskOrderStore, balance: TaskBalanceLedger
) -> None:
    """Fail interrupted local orders and release their development holds.

    The encrypted network exchange is not resumable across a process restart.
    Keeping its hold frozen would strand simulated funds forever, so restart is
    an explicit terminal retry boundary. Both transition and release are
    idempotent.
    """
    for record in consumer_orders.list():
        task_id = str(record.get("task_id") or "")
        state = str(record.get("state") or "created")
        if not task_id or state == "succeeded":
            continue
        if state not in TERMINAL_STATES:
            consumer_orders.transition(
                task_id=task_id,
                state="failed",
                metadata={"error_code": "consumer_restarted_before_completion"},
            )
        try:
            balance.release(task_id=task_id, reason="consumer_restart_recovery")
        except TaskBalanceError as exc:
            if "task hold not found" not in str(exc):
                raise


def install_llm_routes(app: Any, *, store: RynmeshStore, home: Path, messaging_key: Any,
                       resolve_endpoint: Callable[[str], str], resolve_pubkey: Callable[[str], str]) -> None:
    provider_orders = TaskOrderStore(home / "llm" / "provider-orders")
    consumer_orders = TaskOrderStore(home / "llm" / "consumer-orders")
    balance = TaskBalanceLedger(home / "llm" / "task-balance.json")
    _recover_consumer_orders(consumer_orders, balance)
    manager: ProviderService | None = None
    p2p_sessions: set[str] = set()
    p2p_sessions_lock = threading.Lock()

    def active_manager(path: str = "") -> ProviderService | None:
        nonlocal manager
        configured = path or os.environ.get("RYNMESH_LLM_SERVICE_MANIFEST", "")
        if manager is None and configured:
            manifest = load_manifest(configured)
            manager = ProviderService(
                manifest=manifest, adapter=adapter_from_manifest(manifest), store=store,
                task_store=provider_orders, balance=balance, messaging_key=messaging_key,
            )
        return manager

    def discover(network_id: str) -> list[dict[str, Any]]:
        values = store.list_job_capacities(
            network_id=network_id, capability=CAPABILITY, max_age_hours=1,
        ).get("capacities", [])
        services = []
        for value in values:
            public = dict(value.get("metadata") or {}).get("llm_service")
            if isinstance(public, dict):
                services.append({"peer_id": value.get("peer_id"), "node_name": value.get("node_name"),
                                 "updated_at": value.get("updated_at"), **public})
        return services

    def publish_once() -> dict[str, Any]:
        """Refresh the configured provider's short-lived discovery record."""
        current = active_manager()
        if current is None:
            return {"configured": False, "online": False}
        return current.publish(
            network_id=os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main"),
            benchmark=False,
        )

    def _run_p2p_provider(order: dict[str, Any], current: ProviderService) -> None:
        task_order_id = str(order.get("work_order_id") or "")
        requester = str(order.get("requester_peer_id") or "")
        network_id = str(order.get("network_id") or "rynmesh-main")
        params = dict(order.get("params") or {})

        def publish_answer(answer: IceSignal) -> None:
            store.publish_work_result(
                work_order_id=task_order_id,
                requester_peer_id=requester,
                status="accepted",
                message="ICE candidates exchanged; direct connectivity check starting",
                result_refs={
                    "transport": "ice_udp_direct",
                    "relay_allowed": False,
                    "session_id": str(params.get("session_id") or ""),
                    "ice_signal": answer.to_dict(),
                },
                network_id=network_id,
            )

        try:
            offer = IceSignal.from_dict(dict(params.get("ice_signal") or {}))
            evidence = asyncio.run(provider_exchange(
                offer=offer,
                publish_answer=publish_answer,
                handle_request=current.handle,
                timeout_s=float(params.get("timeout_seconds") or current.manifest.timeout_seconds + 30),
            ))
            store.publish_work_result(
                work_order_id=task_order_id,
                requester_peer_id=requester,
                status="completed",
                message="LLM task completed over a nominated direct ICE/UDP candidate pair",
                result_refs={"transport_evidence": evidence},
                network_id=network_id,
            )
        except Exception:
            store.publish_work_result(
                work_order_id=task_order_id,
                requester_peer_id=requester,
                status="failed",
                message="strict P2P ICE/UDP connection or task processing failed",
                result_refs={"transport": "ice_udp_direct", "relay_used": False},
                network_id=network_id,
            )
        finally:
            with p2p_sessions_lock:
                p2p_sessions.discard(task_order_id)

    def relay_once() -> int:
        """Process signaling, settlement, and optional legacy ciphertext relay work."""
        current = active_manager()
        relay_url = os.environ.get("RYNMESH_LLM_RELAY_URL", "").strip()
        if current is None:
            return 0
        orders = store.poll_work_orders(
            network_id=os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main"), capability=CAPABILITY,
        ).get("work_orders", [])
        processed = 0
        for order in orders:
            operation = str(order.get("operation") or "")
            if operation not in {
                OPERATION + ".p2p_offer",
                OPERATION + ".relay",
                OPERATION + ".settlement",
            }:
                continue
            task_order_id = str(order.get("work_order_id") or "")
            prior = store.list_work_results(work_order_id=task_order_id).get("work_results", [])
            if any(item.get("status") in {"completed", "failed", "cancelled"} for item in prior):
                continue
            reference = str(dict(order.get("params") or {}).get("encrypted_task_ref") or "")
            requester = str(order.get("requester_peer_id") or "")
            if not requester:
                continue
            if operation == OPERATION + ".p2p_offer":
                with p2p_sessions_lock:
                    if task_order_id in p2p_sessions:
                        continue
                    p2p_sessions.add(task_order_id)
                threading.Thread(
                    target=_run_p2p_provider,
                    args=(order, current),
                    name=f"rynmesh-llm-p2p-{task_order_id[-8:]}",
                    daemon=True,
                ).start()
                processed += 1
                continue
            if operation == OPERATION + ".settlement":
                try:
                    current.settle_earning(
                        dict(dict(order.get("params") or {}).get("signed_settlement") or {})
                    )
                    store.publish_work_result(
                        work_order_id=task_order_id, requester_peer_id=requester,
                        status="completed", message="body-free LLM settlement recorded",
                        network_id=str(order.get("network_id") or "rynmesh-main"),
                    )
                    processed += 1
                except Exception:
                    store.publish_work_result(
                        work_order_id=task_order_id, requester_peer_id=requester,
                        status="failed", message="body-free LLM settlement failed",
                        network_id=str(order.get("network_id") or "rynmesh-main"),
                    )
                continue
            if not relay_url:
                store.publish_work_result(
                    work_order_id=task_order_id, requester_peer_id=requester,
                    status="failed", message="ciphertext relay is not configured",
                    network_id=str(order.get("network_id") or "rynmesh-main"),
                )
                continue
            if not reference:
                continue
            request_path = response_path = None
            try:
                fd, name = tempfile.mkstemp(prefix="rynmesh-llm-request-", suffix=".ciphertext", dir=home)
                os.close(fd)
                request_path = Path(name)
                store.download_relay_artifact(reference, request_path, relay_url=relay_url)
                encrypted_response = current.handle(json.loads(request_path.read_text(encoding="utf-8")))
                fd, name = tempfile.mkstemp(prefix="rynmesh-llm-response-", suffix=".ciphertext", dir=home)
                os.close(fd)
                response_path = Path(name)
                response_path.write_text(json.dumps(encrypted_response), encoding="utf-8")
                uploaded = store.upload_relay_artifact(
                    response_path, relay_url=relay_url, media_type="application/vnd.rynmesh.llm-ciphertext+json",
                    filename=f"{task_order_id}.ciphertext",
                )
                store.publish_work_result(
                    work_order_id=task_order_id, requester_peer_id=requester, status="completed",
                    message="encrypted LLM relay response ready",
                    result_refs={"encrypted_task_ref": uploaded["blob"]["content_hash"]},
                    network_id=str(order.get("network_id") or "rynmesh-main"),
                )
                processed += 1
            except Exception:
                store.publish_work_result(
                    work_order_id=task_order_id, requester_peer_id=requester, status="failed",
                    message="encrypted LLM relay processing failed", network_id=str(order.get("network_id") or "rynmesh-main"),
                )
            finally:
                if request_path:
                    request_path.unlink(missing_ok=True)
                if response_path:
                    response_path.unlink(missing_ok=True)
        return processed

    app.state.llm_relay_once = relay_once
    app.state.llm_publish_once = publish_once

    @app.get("/api/local/llm/hardware")
    def local_llm_hardware() -> dict[str, Any]:
        from .hardware import detect_hardware, recommend
        report = detect_hardware(home)
        return {"hardware": report.to_dict(), "recommendations": recommend(report)}

    @app.get("/api/local/llm/services")
    def local_llm_services(network_id: str = "rynmesh-main") -> dict[str, Any]:
        return {"network_id": network_id, "services": discover(network_id)}

    @app.post("/api/local/llm/services/publish")
    async def local_llm_publish(request: Request) -> dict[str, Any]:
        body = await request.json()
        current = active_manager(str(body.get("manifest") or ""))
        if current is None:
            raise HTTPException(status_code=400, detail="LLM service manifest is not configured")
        try:
            return current.publish(network_id=str(body.get("network_id") or "rynmesh-main"),
                                   benchmark=bool(body.get("benchmark", True)))
        except (TaskProtocolError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/local/llm/service/status")
    def local_llm_service_status() -> dict[str, Any]:
        current = active_manager()
        return current.public_status() if current else {"configured": False, "online": False}

    @app.get("/api/local/task-balance")
    def local_task_balance() -> dict[str, Any]:
        return {**balance.summary(), "events": balance.events()}

    @app.get("/api/local/llm/orders")
    def local_llm_orders() -> dict[str, Any]:
        return {"orders": consumer_orders.list()}

    @app.get("/api/local/llm/provider-orders")
    def local_llm_provider_orders() -> dict[str, Any]:
        return {"orders": provider_orders.list()}

    @app.post("/api/local/llm/orders")
    async def local_llm_order(request: Request) -> dict[str, Any]:
        body = await request.json()
        network_id = str(body.get("network_id") or "rynmesh-main")
        provider_peer_id = str(body.get("provider_peer_id") or "")
        service_id = str(body.get("service_id") or "")
        prompt = str(body.get("prompt") or "")
        max_tokens = int(body.get("max_tokens") or 64)
        if not provider_peer_id or not service_id or not prompt:
            raise HTTPException(status_code=400, detail="provider_peer_id, service_id, and prompt are required")
        selected = next((item for item in discover(network_id)
                         if item.get("peer_id") == provider_peer_id
                         and dict(item.get("service") or {}).get("package_id") == service_id), None)
        if not selected or not selected.get("online"):
            raise HTTPException(status_code=409, detail="service is absent, stale, offline, or unhealthy")
        public_manifest = dict(selected["service"])
        manifest = LLMPackageManifest.from_dict({
            "package_id": public_manifest["package_id"], "mode": "openai_compatible",
            "public_model_alias": public_manifest["model_alias"], "base_url": "http://127.0.0.1",
            "version": public_manifest["version"], "protocol_version": public_manifest["protocol_version"],
            "adapter": public_manifest["adapter"], "runtime": public_manifest["runtime"],
            "capabilities": public_manifest["capabilities"], "context_window": public_manifest["context_window"],
            "max_output_tokens": public_manifest["max_output_tokens"], "max_concurrent": public_manifest["max_concurrent"],
            "queue_limit": public_manifest["queue_limit"], "timeout_seconds": public_manifest["timeout_seconds"],
            "hardware_requirements": public_manifest["hardware_requirements"], "pricing": public_manifest["pricing"],
            "privacy": public_manifest["privacy"], "license_id": public_manifest["license_id"],
            "license_notice": public_manifest["license_notice"], "risk_labels": public_manifest["risk_labels"],
            "content_rules": public_manifest["content_rules"], "model_fingerprint": public_manifest["model_fingerprint"],
        })
        max_tokens = min(max_tokens, manifest.max_output_tokens)
        maximum = _estimate_price(manifest, prompt, max_tokens)
        if maximum > manifest.pricing.maximum_per_task:
            raise HTTPException(status_code=409, detail="estimated task cost exceeds provider maximum")
        task_id = str(body.get("task_id") or ("task_" + uuid.uuid4().hex))
        idempotency_key = str(body.get("idempotency_key") or task_id)
        try:
            balance.hold(task_id=task_id, amount=maximum, service_id=service_id,
                         provider_peer_id=provider_peer_id)
            consumer_orders.transition(task_id=task_id, state="created",
                                       metadata={"provider_peer_id": provider_peer_id, "service_id": service_id})
            consumer_orders.transition(task_id=task_id, state="accepted",
                                       metadata={"provider_peer_id": provider_peer_id, "service_id": service_id})
            recipient_pub = str(selected.get("node_messaging_pub") or "") or resolve_pubkey(provider_peer_id)
            from rynmesh.services import peer_box
            signed = seal_task(
                body={"task_id": task_id, "idempotency_key": idempotency_key,
                      "service_id": service_id, "prompt": prompt, "max_tokens": max_tokens,
                      "max_amount": maximum, "reply_messaging_pub": peer_box.public_key_b64(messaging_key)},
                task_id=task_id, kind="llm_request", sender_peer_id=store.peer_id,
                recipient_peer_id=provider_peer_id, sender_signing_key=store.private_key_bytes,
                recipient_messaging_pub=recipient_pub,
                expires_at=_expires(max(60, manifest.timeout_seconds + 30)),
            )
            endpoint = resolve_endpoint(provider_peer_id)
            encrypted_response = None
            direct_error: Exception | None = None
            transport_evidence: dict[str, Any] = {}
            transport_mode = os.environ.get("RYNMESH_LLM_TRANSPORT", "auto").strip().lower()
            if transport_mode not in {"auto", "direct", "p2p", "relay"}:
                raise TaskProtocolError("RYNMESH_LLM_TRANSPORT must be auto, direct, p2p, or relay")
            force_relay = os.environ.get("RYNMESH_LLM_FORCE_RELAY", "").strip().lower() in {
                "1", "true", "yes",
            }
            if force_relay:
                transport_mode = "relay"
            consumer_orders.transition(
                task_id=task_id,
                state="running",
                metadata={
                    "provider_peer_id": provider_peer_id,
                    "service_id": service_id,
                    "transport": transport_mode,
                },
            )
            if transport_mode == "p2p":
                p2p_work_order_id = ""

                async def publish_offer(offer: IceSignal) -> IceSignal:
                    nonlocal p2p_work_order_id
                    submitted = await asyncio.to_thread(
                        store.submit_work_order,
                        provider_peer_id=provider_peer_id,
                        capability=CAPABILITY,
                        operation=OPERATION + ".p2p_offer",
                        params={
                            "session_id": task_id,
                            "ice_signal": offer.to_dict(),
                            "timeout_seconds": manifest.timeout_seconds + 30,
                        },
                        network_id=network_id,
                        idempotency_key="p2p:" + idempotency_key,
                        expires_in_hours=max(1, manifest.timeout_seconds / 3600),
                    )
                    p2p_work_order_id = str(submitted["order"]["work_order_id"])
                    deadline = time.monotonic() + manifest.timeout_seconds + 30
                    while time.monotonic() < deadline:
                        values = await asyncio.to_thread(
                            store.list_work_results,
                            work_order_id=p2p_work_order_id,
                            network_id=network_id,
                        )
                        results = values.get("work_results", [])
                        failed = next(
                            (item for item in results if item.get("status") in {"failed", "cancelled"}),
                            None,
                        )
                        if failed:
                            raise TaskProtocolError("provider rejected strict P2P signaling")
                        accepted = next(
                            (item for item in results if item.get("status") == "accepted"),
                            None,
                        )
                        if accepted:
                            refs = dict(accepted.get("result_refs") or {})
                            if refs.get("relay_allowed") is not False:
                                raise TaskProtocolError("provider answer did not forbid relay")
                            return IceSignal.from_dict(dict(refs.get("ice_signal") or {}))
                        await asyncio.sleep(0.25)
                    raise TaskProtocolError("timed out waiting for provider ICE answer")

                encrypted_response, transport_evidence = await consumer_exchange(
                    signed_request=signed.to_dict(),
                    publish_offer=publish_offer,
                    timeout_s=manifest.timeout_seconds + 30,
                )
            elif endpoint and transport_mode in {"auto", "direct"}:
                try:
                    req = urllib.request.Request(
                        endpoint + "/api/peer/llm/tasks", data=json.dumps(signed.to_dict()).encode(),
                        headers={"Content-Type": "application/json", **network_key_header()}, method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=manifest.timeout_seconds + 30) as response:
                        encrypted_response = json.load(response)
                    transport_evidence = {
                        "transport": "peer_http_direct",
                        "relay_used": False,
                    }
                except Exception as exc:
                    direct_error = exc
            if encrypted_response is None and transport_mode == "direct":
                raise TaskProtocolError("strict direct provider path failed") from direct_error
            if encrypted_response is None:
                relay_url = os.environ.get("RYNMESH_LLM_RELAY_URL", "").strip()
                if transport_mode == "p2p":
                    raise TaskProtocolError("strict P2P path failed; relay fallback is disabled")
                if not relay_url:
                    raise TaskProtocolError("direct provider path failed and no dedicated LLM relay is configured") from direct_error
                request_path = response_path = None
                try:
                    fd, name = tempfile.mkstemp(prefix="rynmesh-llm-request-", suffix=".ciphertext", dir=home)
                    os.close(fd)
                    request_path = Path(name)
                    request_path.write_text(json.dumps(signed.to_dict()), encoding="utf-8")
                    uploaded = store.upload_relay_artifact(
                        request_path, relay_url=relay_url,
                        media_type="application/vnd.rynmesh.llm-ciphertext+json",
                        filename=f"{task_id}.ciphertext",
                    )
                    submitted = store.submit_work_order(
                        provider_peer_id=provider_peer_id, capability=CAPABILITY,
                        operation=OPERATION + ".relay",
                        params={"encrypted_task_ref": uploaded["blob"]["content_hash"]},
                        network_id=network_id, idempotency_key=idempotency_key,
                        expires_in_hours=max(1, manifest.timeout_seconds / 3600),
                    )
                    work_order_id = str(submitted["order"]["work_order_id"])
                    deadline = time.monotonic() + manifest.timeout_seconds + 60
                    while time.monotonic() < deadline:
                        results = store.list_work_results(work_order_id=work_order_id, network_id=network_id).get("work_results", [])
                        terminal = next((item for item in results if item.get("status") in {"completed", "failed", "cancelled"}), None)
                        if terminal:
                            if terminal.get("status") != "completed":
                                raise TaskProtocolError("encrypted relay task failed")
                            ref = str(dict(terminal.get("result_refs") or {}).get("encrypted_task_ref") or "")
                            fd, name = tempfile.mkstemp(prefix="rynmesh-llm-response-", suffix=".ciphertext", dir=home)
                            os.close(fd)
                            response_path = Path(name)
                            store.download_relay_artifact(ref, response_path, relay_url=relay_url)
                            encrypted_response = json.loads(response_path.read_text(encoding="utf-8"))
                            transport_evidence = {
                                "transport": "encrypted_relay",
                                "relay_used": True,
                            }
                            break
                        time.sleep(0.5)
                    if encrypted_response is None:
                        raise TaskProtocolError("encrypted relay task timed out")
                finally:
                    if request_path:
                        request_path.unlink(missing_ok=True)
                    if response_path:
                        response_path.unlink(missing_ok=True)
            _, result = open_task(
                encrypted_response, recipient_peer_id=store.peer_id,
                recipient_messaging_key=messaging_key, expected_kind="llm_response",
            )
            state = str(result.get("state") or "failed")
            result["transport"] = str(transport_evidence.get("transport") or "unknown")
            result["transport_evidence"] = transport_evidence
            if state != "succeeded":
                balance.release(task_id=task_id, reason=str(result.get("error_code") or state))
                consumer_orders.transition(task_id=task_id, state=state,
                                           metadata={"provider_peer_id": provider_peer_id,
                                                     "service_id": service_id,
                                                     "error_code": result.get("error_code", state)},
                                           encrypted_response=encrypted_response)
                return result
            balance.settle(
                task_id=task_id, amount=float(result["amount"]), input_tokens=int(result["input_tokens"]),
                output_tokens=int(result["output_tokens"]), duration_ms=int(result["duration_ms"]),
                service_id=service_id, provider_peer_id=provider_peer_id,
            )
            consumer_orders.transition(
                task_id=task_id, state="succeeded",
                metadata={"provider_peer_id": provider_peer_id, "service_id": service_id,
                          "input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"],
                          "duration_ms": result["duration_ms"], "amount": result["amount"],
                          "transport": result["transport"], "relay_used": transport_evidence.get("relay_used")},
                encrypted_response=encrypted_response,
                allow_recovery=True,
            )
            settlement = sign_payload({
                "kind": "llm_settlement", "task_id": task_id, "from_peer_id": store.peer_id,
                "to_peer_id": provider_peer_id, "amount": result["amount"],
                "settlement_id": "settle:" + task_id,
            }, private_key_bytes=store.private_key_bytes)
            settlement_delivered = False
            if endpoint and transport_mode in {"auto", "direct"}:
                try:
                    ack_req = urllib.request.Request(
                        endpoint + "/api/peer/llm/settlements",
                        data=json.dumps(settlement.to_dict()).encode(),
                        headers={"Content-Type": "application/json", **network_key_header()},
                        method="POST",
                    )
                    with urllib.request.urlopen(ack_req, timeout=15):
                        settlement_delivered = True
                except Exception:
                    pass
            if not settlement_delivered:
                store.submit_work_order(
                    provider_peer_id=provider_peer_id, capability=CAPABILITY,
                    operation=OPERATION + ".settlement",
                    params={"signed_settlement": settlement.to_dict()}, network_id=network_id,
                    idempotency_key="settle:" + task_id, expires_in_hours=1,
                )
            return result
        except Exception as exc:
            try:
                balance.release(task_id=task_id, reason="delivery_or_processing_failed")
                consumer_orders.transition(task_id=task_id, state="failed",
                                           metadata={"provider_peer_id": provider_peer_id,
                                                     "service_id": service_id,
                                                     "error_code": "delivery_or_processing_failed"})
            except (TaskBalanceError, TaskProtocolError):
                pass
            reason = str(exc).strip() or type(exc).__name__
            raise HTTPException(
                status_code=502,
                detail=f"private LLM task failed: {type(exc).__name__}: {reason}",
            ) from exc
        finally:
            prompt = ""

    @app.post("/api/local/llm/orders/{task_id}/cancel")
    def local_llm_cancel(task_id: str) -> dict[str, Any]:
        record = consumer_orders.get(task_id)
        if not record:
            raise HTTPException(status_code=404, detail="task not found")
        if record.get("state") not in TERMINAL_STATES:
            balance.release(task_id=task_id, reason="consumer_cancelled")
            consumer_orders.transition(task_id=task_id, state="cancelled",
                                       metadata={"reason": "consumer_cancelled"})
        return {"task_id": task_id, "state": (consumer_orders.get(task_id) or {}).get("state")}

    @app.post("/api/peer/llm/tasks")
    async def peer_llm_task(request: Request) -> dict[str, Any]:
        current = active_manager()
        if current is None:
            raise HTTPException(status_code=503, detail="LLM service not configured")
        try:
            return current.handle(await request.json())
        except TaskProtocolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/peer/llm/settlements")
    async def peer_llm_settlement(request: Request) -> dict[str, Any]:
        current = active_manager()
        if current is None:
            raise HTTPException(status_code=503, detail="LLM service not configured")
        try:
            earning = current.settle_earning(await request.json())
            return {"ok": True, "event_id": earning["event_id"]}
        except TaskProtocolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    app.state.llm_provider = active_manager()
