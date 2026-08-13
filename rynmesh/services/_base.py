"""Shared scaffolding for Ryn-network service workers.

Each concrete worker subclass declares:
    capability: str   # advertised job-capacity label
    operation:  str   # exact operation name to match in work orders
    execute(self, params: dict) -> dict
        Returns a dict with `message` (str) and optional
        `result_content_ids` (tuple[str, ...]). Anything raised is caught
        and reported back as a failed result.

The base class handles registration, polling, accept/running/completed/
failed lifecycle messages, and the worker loop. Identical shape to
signal50_service so existing tooling (the webapp's Services screen,
qa/lan_qa.py work-order flow) interoperates without changes.
"""
from __future__ import annotations

import json
import os
import time
import traceback
from typing import Any

from rynmesh.store import RynmeshStore


class ServiceWorker:
    capability: str = ""
    operation: str = ""

    # ---- subclass interface ----------------------------------------------
    def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    # ---- loop ------------------------------------------------------------
    def serve_forever(
        self,
        store: RynmeshStore | None = None,
        *,
        network_id: str | None = None,
        poll_interval_s: float = 2.0,
    ) -> None:
        store = store or RynmeshStore()
        network_id = network_id or os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main")
        store.register_job_capacity(network_id=network_id, capabilities=[self.capability])
        print(
            f"[{self.capability}] registered on network_id={network_id}; "
            f"polling every {poll_interval_s}s"
        )
        while True:
            try:
                self.serve_once(store, network_id=network_id)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - never let the loop die
                print(f"[{self.capability}] loop error: {exc}")
            time.sleep(poll_interval_s)

    def serve_once(self, store: RynmeshStore, *, network_id: str) -> int:
        """One polling pass. Returns the number of orders processed."""
        orders = store.poll_work_orders(
            network_id=network_id, capability=self.capability,
        ).get("work_orders", [])
        n = 0
        for order in orders:
            wo_id = str(order.get("work_order_id", ""))
            req = str(order.get("requester_peer_id", ""))
            if not wo_id or not req:
                continue
            if str(order.get("operation", "")) != self.operation:
                continue
            n += 1
            self._handle_order(store, order, wo_id, req, network_id)
        return n

    # ---- per-order lifecycle ---------------------------------------------
    def _handle_order(
        self,
        store: RynmeshStore,
        order: dict[str, Any],
        wo_id: str,
        req: str,
        network_id: str,
    ) -> None:
        try:
            store.publish_work_result(
                work_order_id=wo_id, requester_peer_id=req,
                status="accepted", message=f"{self.capability}: accepted",
                network_id=network_id,
            )
            result = self.execute(dict(order.get("params") or {}))
            store.publish_work_result(
                work_order_id=wo_id, requester_peer_id=req,
                status="completed",
                message=str(result.get("message", "")),
                result_content_ids=tuple(
                    str(c) for c in (result.get("result_content_ids") or ()) if str(c)
                ),
                network_id=network_id,
            )
        except Exception as exc:  # noqa: BLE001
            store.publish_work_result(
                work_order_id=wo_id, requester_peer_id=req,
                status="failed",
                message=json.dumps({
                    "error": str(exc),
                    "type": type(exc).__name__,
                    "trace_tail": traceback.format_exc(limit=3).splitlines()[-3:],
                }),
                network_id=network_id,
            )
