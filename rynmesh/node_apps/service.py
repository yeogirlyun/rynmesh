"""Explicit free app calls over existing authenticated encrypted friend transport."""

import json
import re
import uuid
from contextlib import contextmanager, nullcontext
from copy import deepcopy

from ..crypto import canonical_json, sha256_bytes
from ..services import peer_box
from .catalog_data import CATALOG
from .craft_host import CraftError
from .engine import AppError, contract, run

PATH = "/api/peer/node-apps"
WIRE = "ryn.node-app-wire.v1"
CHANNEL = b"rynmesh-node-app-wire-v1"
MAX_WIRE = 384 * 1024


def require(test, error="apps_invalid_request"):
    if not test:
        raise AppError(error)


def fingerprint(value):
    return sha256_bytes(canonical_json(value))


def preview_for(context, action, arguments):
    item = contract(context["app"], action, arguments)
    value = {
        **deepcopy(context),
        "action": action,
        "arguments": deepcopy(arguments),
        "effect": item["effect"],
        "price_ryncoin": "0",
        "platform_commission": "0",
    }
    return {**value, "preview_hash": fingerprint(value)}


class NodeApps:
    def __init__(self, store, peer_id, friends, plugins=None):
        self.store, self.peer_id, self.friends = store, peer_id, friends
        self.plugins = plugins

    @contextmanager
    def _plugin_lease(self, app):
        try:
            with self.plugins.lease(app) if self.plugins else nullcontext():
                yield
        except CraftError as exc:
            raise AppError("apps_plugin_disabled" if str(exc) == "plugin_disabled" else "apps_plugin_unavailable") from None

    def _plugin_generation(self, app):
        try:
            return self.plugins.generation(app) if self.plugins else None
        except CraftError:
            raise AppError("apps_plugin_disabled") from None

    def _slot(self, actor, app):
        require(app in CATALOG)
        return fingerprint([actor, app])

    def _active(self, actor, relationship_id=None):
        service = self.friends()
        state = service.store._read(service.store.state_path, service.store._empty())
        relation = next(
            (
                r
                for r in state["relationships"].values()
                if r["peer_id"] == actor
                and r["status"] == "active"
                and (relationship_id is None or r["relationship_id"] == relationship_id)
            ),
            None,
        )
        require(relation, "apps_friend_inactive")
        return relation

    def _authorize(self, data, actor, app, relationship_id=None, *, check_plugin=True):
        plugin_generation = self._plugin_generation(app) if check_plugin else None
        if actor == self.peer_id:
            return fingerprint(["owner", plugin_generation]) if plugin_generation else "owner"
        relation = self._active(actor, relationship_id)
        grant = data["grants"].get(self._slot(actor, app), {})
        require(
            grant.get("allowed") is True
            and grant.get("relationship_id") == relation["relationship_id"],
            "apps_access_denied",
        )
        return fingerprint([grant["generation"], plugin_generation]) if plugin_generation else grant["generation"]

    def _context(self, data, actor, app, relationship_id=None):
        generation = self._authorize(data, actor, app, relationship_id)
        workspace = data["workspaces"].get(
            self._slot(actor, app), {"revision": 0, "state": {}, "operations": {}}
        )
        return {
            "app": app,
            "provider_peer": self.peer_id,
            "consumer_peer": actor,
            "access_generation": generation,
            "revision": workspace["revision"],
            "state_hash": fingerprint(workspace["state"]),
        }

    def _handle(self, actor, operation, value, relationship_id=None):
        # Historical receipts remain auditable with the original friend grant.
        # Every new view/preview/invocation must hold an installed-plugin lease.
        lease = self._plugin_lease(value.get("app")) if operation not in {"catalog", "receipt"} else nullcontext()
        with lease:
            return self._handle_active(actor, operation, value, relationship_id)

    def _handle_active(self, actor, operation, value, relationship_id=None):
        gate = self.friends().store._guard() if actor != self.peer_id else nullcontext()
        with gate:
            if operation == "catalog":
                require(value == {})
                data = self.store.read()
                result = []
                for app, item in CATALOG.items():
                    try:
                        context = self._context(data, actor, app, relationship_id)
                    except AppError:
                        continue
                    result.append(
                        {
                            **deepcopy(item),
                            "workspace_context": context,
                        }
                    )
                return {"provider_peer": self.peer_id, "apps": result, "price_ryncoin": "0"}
            app = value.get("app")
            require(app in CATALOG)
            if operation in {"context", "view"}:
                require(set(value) == {"app"})
                data = self.store.read()
                context = self._context(data, actor, app, relationship_id)
                if operation == "context":
                    return context
                workspace = data["workspaces"].get(self._slot(actor, app), {})
                return {
                    **context,
                    "records": deepcopy(workspace.get("state", {}).get("records", {})),
                }
            if operation == "receipt":
                require(set(value) == {"app", "operation_id"})
                data = self.store.read()
                self._authorize(data, actor, app, relationship_id, check_plugin=False)
                return deepcopy(
                    data["workspaces"]
                    .get(self._slot(actor, app), {})
                    .get("operations", {})
                    .get(value["operation_id"])
                )
            require(
                operation == "invoke"
                and set(value) == {"app", "action", "arguments", "operation_id", "preview_hash"}
            )
            action, args, operation_id = value["action"], value["arguments"], value["operation_id"]
            require(
                isinstance(operation_id, str)
                and re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", operation_id)
            )
            require(len(canonical_json(args)) <= 24 * 1024)
            item = contract(app, action, args)
            request_hash = fingerprint(value)

            def mutate(data):
                context = self._context(data, actor, app, relationship_id)
                slot = self._slot(actor, app)
                workspace = data["workspaces"].get(
                    slot, {"revision": 0, "state": {}, "operations": {}}
                )
                old = workspace["operations"].get(operation_id)
                if old:
                    require(old["request_hash"] == request_hash, "apps_operation_changed")
                    return old
                require(
                    preview_for(context, action, args)["preview_hash"] == value["preview_hash"],
                    "apps_review_changed",
                )
                require(len(workspace["operations"]) < 128, "apps_operation_capacity")
                result, state = run(app, action, args, workspace["state"])
                require(
                    len(canonical_json(state)) <= 128 * 1024
                    and len(canonical_json(result)) <= 64 * 1024,
                    "apps_workspace_capacity",
                )
                if item["effect"] == "write":
                    workspace["state"] = state
                    workspace["revision"] += 1
                receipt = {
                    "operation_id": operation_id,
                    "request_hash": request_hash,
                    "app": app,
                    "action": action,
                    "arguments_hash": fingerprint(args),
                    "revision": workspace["revision"],
                    "provider_peer": self.peer_id,
                    "consumer_peer": actor,
                    "result": result,
                    "price_ryncoin": "0",
                }
                workspace["operations"][operation_id] = receipt
                data["workspaces"][slot] = workspace
                return receipt

            return self.store.mutate(mutate)

    def call(self, peer, operation, value):
        if not peer or peer == self.peer_id:
            return self._handle(self.peer_id, operation, value)
        friends = self.friends()
        relation, secret = friends._relationship(peer)
        request_id = uuid.uuid4().hex
        request = {
            "version": WIRE,
            "request_id": request_id,
            "relationship_id": relation["relationship_id"],
            "from": self.peer_id,
            "to": peer,
            "operation": operation,
            "value": value,
        }
        nonce, ciphertext = peer_box.seal(
            friends.messaging_private,
            relation["messaging_pub"],
            canonical_json(request),
            info=CHANNEL,
        )
        try:
            response = friends._request_wire(
                relation,
                secret,
                PATH,
                {"nonce": nonce, "ciphertext": ciphertext},
                max_response_bytes=MAX_WIRE,
            )
        except Exception:
            raise AppError("apps_response_unconfirmed") from None
        try:
            raw = peer_box.open_sealed(
                friends.messaging_private,
                relation["messaging_pub"],
                response["nonce"],
                response["ciphertext"],
                info=CHANNEL,
            )
            result = json.loads(raw)
            require(
                len(raw) <= MAX_WIRE
                and result["version"] == WIRE
                and result["request_id"] == request_id
                and result["from"] == peer
                and result["to"] == self.peer_id,
                "apps_response_invalid",
            )
        except Exception:
            raise AppError("apps_response_invalid") from None
        friends._relationship(peer)
        if result.get("error"):
            raise AppError(result["error"])
        return result["result"]

    def preview(self, peer, app, action, arguments, expected_context=None):
        # Draft arguments never leave this consumer until the owner approves.
        current = self.call(peer, "context", {"app": app})
        if expected_context is not None:
            require(
                isinstance(expected_context, dict)
                and type(expected_context.get("revision")) is int
                and expected_context == current,
                "apps_review_changed",
            )
        return preview_for(current, action, arguments)

    def pending(self):
        return [
            {
                "operation_id": key,
                "app": row["command"]["app"],
                "action": row["command"]["action"],
                "provider_peer": row["provider_peer"],
            }
            for key, row in self.store.read()["intents"].items()
            if row["status"] == "pending"
        ]

    def resume(self, operation_id):
        row = self.store.read()["intents"].get(operation_id)
        require(row, "apps_operation_unavailable")
        return self.invoke(row["provider_peer"], row["command"])

    def invoke(self, peer, value):
        require(set(value) == {"app", "action", "arguments", "operation_id", "preview_hash"})
        contract(value["app"], value["action"], value["arguments"])
        operation_id = value["operation_id"]
        require(
            isinstance(operation_id, str) and re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", operation_id)
        )
        target = peer or self.peer_id
        request_hash = fingerprint({"provider_peer": target, "command": value})

        def prepare(data):
            old = data["intents"].get(operation_id)
            if old:
                require(old["request_hash"] == request_hash, "apps_operation_changed")
                require(old["status"] != "rejected", "apps_operation_rejected")
                return old
            require(
                not any(
                    r["status"] == "pending"
                    and r["provider_peer"] == target
                    and r["command"]["app"] == value["app"]
                    for r in data["intents"].values()
                ),
                "apps_pending_required",
            )
            require(len(data["intents"]) < 128, "apps_operation_capacity")
            row = {
                "status": "pending",
                "provider_peer": target,
                "request_hash": request_hash,
                "command": deepcopy(value),
            }
            data["intents"][operation_id] = row
            return row

        old = self.store.mutate(prepare)
        if old["status"] == "committed":
            return old["result"]
        try:
            result = self.call(target, "invoke", value)
        except AppError as exc:
            error = str(exc)
            if error not in {
                "apps_response_unconfirmed",
                "apps_response_invalid",
                "apps_store_unavailable",
            }:
                self.store.mutate(
                    lambda data: data["intents"][operation_id].update(
                        status="rejected", error=error
                    )
                )
            raise
        self.store.mutate(
            lambda data: data["intents"][operation_id].update(status="committed", result=result)
        )
        return result

    def respond(self, wire, relation):
        friends = self.friends()
        try:
            raw = peer_box.open_sealed(
                friends.messaging_private,
                relation["messaging_pub"],
                wire["nonce"],
                wire["ciphertext"],
                info=CHANNEL,
            )
            request = json.loads(raw)
            require(
                len(raw) <= 64 * 1024
                and set(request)
                == {"version", "request_id", "relationship_id", "from", "to", "operation", "value"}
            )
            require(
                request["version"] == WIRE
                and request["from"] == relation["peer_id"]
                and request["to"] == self.peer_id
                and request["relationship_id"] == relation["relationship_id"]
            )
            require(
                isinstance(request["request_id"], str)
                and re.fullmatch(r"[a-f0-9]{32}", request["request_id"])
            )
            require(isinstance(request["value"], dict))
        except Exception:
            raise AppError("apps_request_rejected") from None
        response = {
            "version": WIRE,
            "request_id": request["request_id"],
            "from": self.peer_id,
            "to": relation["peer_id"],
        }
        try:
            response["result"] = self._handle(
                relation["peer_id"],
                request["operation"],
                request["value"],
                relation["relationship_id"],
            )
        except AppError as exc:
            response["error"] = str(exc)
        nonce, ciphertext = peer_box.seal(
            friends.messaging_private,
            relation["messaging_pub"],
            canonical_json(response),
            info=CHANNEL,
        )
        return {"nonce": nonce, "ciphertext": ciphertext}

    def grant_preview(self, app, peer, allowed):
        require(app in CATALOG and type(allowed) is bool and peer != self.peer_id)
        friends = self.friends()
        with friends.store._guard():
            relation = self._active(peer)
            old = self.store.read()["grants"].get(self._slot(peer, app), {})
            value = {
                "app": app,
                "provider_peer": self.peer_id,
                "consumer_peer": peer,
                "relationship_id": relation["relationship_id"],
                "recipient_name": relation.get("node_name", ""),
                "allowed": allowed,
                "current": old,
            }
            return {**value, "preview_hash": fingerprint(value)}

    def grant(self, app, peer, allowed, preview_hash):
        friends = self.friends()
        with friends.store._guard():
            relation = self._active(peer)

            def mutate(data):
                slot = self._slot(peer, app)
                old = data["grants"].get(slot, {})
                value = {
                    "app": app,
                    "provider_peer": self.peer_id,
                    "consumer_peer": peer,
                    "relationship_id": relation["relationship_id"],
                    "recipient_name": relation.get("node_name", ""),
                    "allowed": allowed,
                    "current": old,
                }
                require(
                    type(allowed) is bool and preview_hash == fingerprint(value),
                    "apps_review_changed",
                )
                row = {
                    "allowed": allowed,
                    "generation": uuid.uuid4().hex,
                    "relationship_id": relation["relationship_id"],
                }
                require(len(data["grants"]) < 96 or slot in data["grants"], "apps_capacity")
                data["grants"][slot] = row
                return row

            return self.store.mutate(mutate)
