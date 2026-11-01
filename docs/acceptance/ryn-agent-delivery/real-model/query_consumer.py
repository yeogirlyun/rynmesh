"""Isolated Q06 consumer; only the encrypted wire transport is local fixture code."""
from __future__ import annotations

import asyncio
import copy
import threading
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.ai_access.routes import AIAccessState
from rynmesh.ai_access.store import AIAccessStore
from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.llm_package.task_protocol import open_task


class QueryConsumer:
    def __init__(self, access):
        self.access = access
        self.store = access.receiver_store
        self.home = self.store.home
        self.store.registry = access.store.registry
        self.app = FastAPI()
        self.app.state.friends = SimpleNamespace(service=access.receiver)
        self.catalog = access.catalogs["node-b"]
        self.app.state.ai_access = AIAccessState(
            AIAccessStore(self.home, relationship=access.receiver.store.relationship), lambda _: None,
            catalog=self.catalog)
        install_llm_routes(self.app, store=self.store, home=self.home,
            messaging_key=access.receiver.messaging_private,
            resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
        access.world.stack.callback(self.app.state.llm_shutdown)
        self.services = self.app.state.llm_agent_services
        self.sources = self.services["source_store"]
        self.wire_audit, self.refresh_errors = [], []
        access.world.stack.enter_context(patch("rynmesh.llm_package.routes.consumer_exchange", self.exchange))
        self.bridge, self.tools, self.models = build_agent_integrations(self.app, self.store, self.home)
        rows = self.models.list_models()
        service_id = access.world.bindings["model-a"].removeprefix("local/")
        selected = [row for row in rows if row["peer_id"] == access.store.peer_id and row["service_id"] == service_id]
        if len(rows) != 1 or len(selected) != 1 or self.sources.read():
            raise RuntimeError("query_consumer_initial_visibility_mismatch")
        self.selected = selected[0]["id"]
        self.initial_observations = copy.deepcopy(rows)
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self.poll, daemon=True)
        self.worker.start()
        access.world.stack.callback(self.close)

    def poll(self):
        # Same discovery cadence as the production friend catalog worker. This
        # refreshes authentication metadata, never grants or source settings.
        while not self.stop.wait(10):
            try:
                self.catalog.refresh(self.access.store.peer_id)
            except Exception as exc:
                self.refresh_errors.append(type(exc).__name__)

    def close(self):
        self.stop.set()
        self.worker.join(5)

    async def exchange(self, *, signed_request, **kwargs):
        access = self.access
        envelope, body = open_task(signed_request, recipient_peer_id=access.store.peer_id,
            recipient_messaging_key=access.owner.messaging_private, expected_kind="llm_request")
        # Verification/decryption here supplies audit metadata only. The actual
        # provider reopens the original envelope and performs its normal gates.
        sid = body["service_id"]
        item = {"task_id": body["task_id"], "service_id": sid,
            "provider_peer_id": access.store.peer_id,
            "consumer_peer_id": envelope["from_peer_id"],
            "expected_model_binding": copy.deepcopy(body.get("expected_model_binding")),
            "transport": "local_signed_wire_fixture", "response_received": False}
        self.wire_audit.append(item)
        try:
            manager = access.services["provider_for"](sid)
        except RuntimeError as exc:
            if str(exc) in {"owner_source_changed_before_dispatch", "owner_source_unavailable_before_dispatch"}:
                item.update(state="not_dispatched", error_code=str(exc))
            raise
        if manager is None:
            raise RuntimeError("query_provider_unavailable")
        response = await asyncio.to_thread(manager.handle, signed_request, on_event=kwargs.get("on_event"))
        item["response_received"] = True
        _, result = open_task(response, recipient_peer_id=self.store.peer_id,
            recipient_messaging_key=access.receiver.messaging_private, expected_kind="llm_response")
        item.update(state=result.get("state"), error_code=result.get("error_code"))
        return response, {"transport": "local_signed_wire_fixture", "relay_used": False}

    def evidence(self):
        consumer_store = AgentStore(self.home / "agent", self.access.receiver.messaging_private)
        provider_store = AgentStore(self.access.store.home / "agent", self.access.owner.messaging_private)
        consumer_tasks = consumer_store.read()["tasks"] if consumer_store.path.exists() else {}
        provider_tasks = provider_store.read()["tasks"] if provider_store.path.exists() else {}
        return {"consumer_role": "node-b_authorized_peer_consumer",
            "consumer_peer_id": self.store.peer_id, "provider_peer_id": self.access.store.peer_id,
            "primary_model": self.selected, "consumer_local_sources": [self.sources.public(row) for row in self.sources.read().values()],
            "initial_consumer_models": self.initial_observations,
            "current_consumer_models": self.models.list_models(),
            "consumer_wire_audit": copy.deepcopy(self.wire_audit),
            "agent_state_isolation": {"consumer_store_exists": consumer_store.path.exists(),
                "provider_store_exists": provider_store.path.exists(),
                "consumer_task_ids": sorted(consumer_tasks), "provider_task_ids": sorted(provider_tasks),
                "task_ids_overlap": sorted(set(consumer_tasks) & set(provider_tasks)),
                "consumer_message_counts": {key: len(row.get("messages", [])) for key, row in consumer_tasks.items()},
                "consumer_tool_execution_peer_id": self.tools.store.peer_id,
                "provider_persistence_scope": "Provider inference orders, quota counters and transport logs are permitted; these are not Agent tasks or tool execution."},
            "catalog_refresh_errors": list(self.refresh_errors),
            "transport_scope": "Production signed consumer/provider protocol over a local fixture wire; no ICE or physical-node claim."}


class ScriptedProvider:
    """Synthetic planner behind the real provider admission gate, never live."""
    def __init__(self, planner, selected):
        self.planner, self.selected = planner, selected

    def health(self):
        return {"ok": True}

    def chat(self, body, **kwargs):
        result = self.planner.complete(self.selected, body["messages"], body.get("tools", []), lambda: False)
        return {"message": result["message"], "text": result["message"].get("content") or "",
            "finish_reason": result.get("finish_reason", "stop"), "input_tokens": 0, "output_tokens": 0,
            "reported_usage": {"input_tokens": 0, "output_tokens": 0}, "duration_ms": 0}
