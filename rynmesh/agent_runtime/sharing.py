"""One-source atomic sharing activation, exact grants and attributable receipts.

Legacy grants remain readable until this operation imports the selected service
into sources.enc. The encrypted source then owns that service's grants; there is
no write to permissions.json and no intermediate publicly usable grant/scope.
"""
from __future__ import annotations

import copy
import hashlib
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone

from ..ai_access.store import rule_key
from ..llm_package import model_settings
from ..llm_package.peer_binding import model_binding
from ..llm_package.peer_binding import valid as valid_model_binding
from .observation_transitions import MAX_TRANSITION_BYTES, model_fields
from .observation_transitions import VERSION as TRANSITION_VERSION
from .store import AgentToolRejected, encoded

NAME = "ryn_share_model_with_friend"
VERIFIER = "exact_friend_sharing_state"
IDENTITY = ("task_id", "owner_id", "consumer_node_id")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


class FriendSharingTool:
    def __init__(self, app, services, *, personal_space=None):
        self.app, self.services = app, services
        self.personal_space = personal_space

    def space_lock(self):
        return self.personal_space.lock if self.personal_space else nullcontext()

    @property
    def sources(self):
        return self.services["source_store"]

    @property
    def access(self):
        access = getattr(self.app.state, "ai_access", None)
        if access is None:
            raise AgentToolRejected("agent_friend_model_not_shareable")
        # Also supports independently constructed runtime hosts. Production
        # startup binds this before catalogs or provider requests can run.
        access.store.sources = self.sources
        return access.store

    def target(self, args, records=None):
        for record in (records if records is not None else self.sources.read()).values():
            for model in record["models"]:
                sid = self.sources.model_service_id(record, model)
                if args["model_id"] == "local/" + sid:
                    return record, model, sid
        raise AgentToolRejected("agent_model_not_shareable")

    @staticmethod
    def identity(context):
        if not isinstance(context, dict) or any(not context.get(key) for key in IDENTITY):
            raise AgentToolRejected("agent_sharing_context_required")
        return {key: context[key] for key in (*IDENTITY, "goal_revision", "scope_revision")}

    def relationship(self, rid):
        row = self.access.relationship(rid)
        if row is None:
            return None
        return {key: row.get(key) for key in ("relationship_id", "peer_id", "node_name", "status", "revision", "signing_pub", "messaging_pub")}

    def preview(self, args, context=None):
        with self.space_lock(), self.sources.lock:
            record, model, sid = self.target(args)
            relation = self.relationship(args["relationship_id"])
            if not relation or relation["status"] != "active":
                raise AgentToolRejected("agent_friend_inactive")
            if not record.get("enabled") or record.get("status") != "ready":
                raise AgentToolRejected("agent_model_not_ready")
            grants = sorted((row for row in self.access.list() if row["service_id"] == sid), key=lambda row: row["relationship_id"])
            if len(grants) > 100:
                raise AgentToolRejected("agent_sharing_recipient_limit")
            selected = next((row for row in grants if row["relationship_id"] == args["relationship_id"]), None)
            if selected and selected["peer_id"] != relation["peer_id"]:
                raise AgentToolRejected("agent_friend_identity_changed")
            recipient = {"relationship_id": args["relationship_id"], "peer_id": relation["peer_id"], "name": relation["node_name"] or ""}
            space = self.personal_space.status() if self.personal_space else {}
            members = [{key: row.get(key) for key in ("peer_id", "name", "node_name", "role", "removed", "status")}
                       for row in space.get("members", [])]
            if len(members) > 100:
                raise AgentToolRejected("agent_sharing_recipient_limit")
            return {"title": "Share this model with exactly one friend", "model_id": args["model_id"], "service_id": sid,
                "model": record["name"] + " · " + model,
                "configuration_revision": record.get("source_revision") or hashlib.sha256(self.sources.path.read_bytes()).hexdigest(),
                "before": {"sharing": model in self.sources.shared_models(record), "scope": model_settings.effective(record, model)["share_scope"],
                           "friend_grants": grants, "personal_space": {"membership": space.get("membership"),
                               "ai_access": space.get("ai_access"), "space_revision": (space.get("space") or {}).get("revision"),
                               "members": sorted(members, key=lambda row: row.get("peer_id") or "")}},
                "relationships": [self.relationship(rid) for rid in sorted({args["relationship_id"], *(row["relationship_id"] for row in grants)})],
                "after": {"sharing": True, "scope": "friends", "recipients": [recipient]},
                "other_friend_access": "revoke", "verification_scope": "persisted configuration and exact friend grants; receiver connectivity requires separate evidence"}

    def _receipt(self, operation_id, records=None):
        for record in (records if records is not None else self.sources.read()).values():
            found = record.get("_sharing_operations", {}).get(operation_id)
            if found:
                return found
        return None

    def execute(self, args, *, operation_id, context, expected_preview, cancelled):
        binding = self.identity(context)
        if cancelled():
            raise AgentToolRejected("agent_cancelled")
        with self.space_lock(), self.sources.lock:
            records = self.sources.read()
            prior = self._receipt(operation_id, records)
            if prior:
                if prior["binding"] != binding or prior["arguments_sha256"] != digest(args):
                    raise AgentToolRejected("agent_operation_id_conflict")
                if prior.get("retired"):
                    raise AgentToolRejected("agent_operation_retired")
                return copy.deepcopy(prior["result"])
            preview = self.preview(args, context)
            if expected_preview is None or expected_preview != preview:
                raise AgentToolRejected("agent_approval_preview_changed")
            record, model, sid = self.target(args, records)
            if sum(len(row.get("_sharing_operations", {})) for row in records.values()) >= 2500:
                raise AgentToolRejected("agent_operation_ledger_limit")
            old = copy.deepcopy(record)
            grants = {rule_key(sid, row["relationship_id"]): {key: row[key] for key in
                      ("service_id", "relationship_id", "peer_id", "allowed", "revision")}
                      for row in preview["before"]["friend_grants"]}
            recipient = preview["after"]["recipients"][0]
            key = rule_key(sid, args["relationship_id"])
            if key not in grants:
                grants[key] = {"service_id": sid, "relationship_id": args["relationship_id"], "peer_id": recipient["peer_id"],
                               "allowed": False, "revision": 0}
            for row in grants.values():
                desired = row["relationship_id"] == args["relationship_id"]
                if row["allowed"] != desired:
                    row.update(allowed=desired, revision=row["revision"] + 1)
            record.setdefault("_friend_grants", {})[sid] = grants
            record["shared_models"] = [m for m in record["models"] if m == model or m in self.sources.shared_models(record)]
            record["sharing"] = True
            settings = record.setdefault("model_settings", {}).setdefault(model, {})
            settings["overrides"] = {**settings.get("overrides", {}), "share_scope": "friends"}
            model_settings.reconcile(record, old)
            record["source_revision"] = uuid.uuid4().hex
            result = {"ok": True, "operation_id": operation_id, "model_id": args["model_id"], "service_id": sid,
                      "sharing": True, "scope": "friends", "recipients": copy.deepcopy(preview["after"]["recipients"]),
                      "model_binding": model_binding(record, model),
                      "source_revision": record["source_revision"], "publication": "refresh_requested",
                      "grant_revisions": {row["relationship_id"]: row["revision"] for row in grants.values()}}
            transition = {"version": TRANSITION_VERSION,
                "before": model_fields(self.sources, old, model), "after": model_fields(self.sources, record, model),
                "before_grants": copy.deepcopy(preview["before"]["friend_grants"]),
                "after_grants": [self.access._public(row) for row in grants.values()]}
            operation = {
                "operation_id": operation_id, "tool": NAME, "arguments_sha256": digest(args),
                "binding": binding, "result": copy.deepcopy(result)}
            # Private, bounded verifier metadata; no credentials, extra business
            # write or provider context cost. Oversized snapshots require a new
            # observation; never store or apply a partial transition.
            if len(encoded(transition)) <= MAX_TRANSITION_BYTES:
                operation["observation_transition"] = transition
            record.setdefault("_sharing_operations", {})[operation_id] = operation
            if cancelled():
                raise AgentToolRejected("agent_cancelled")
            # Sole durable business write. Unexpected failures remain unknown;
            # reconciliation reads the co-committed receipt, never retries.
            self.sources._write(records)
        # Configuration is authoritative at dispatch. Publication refresh is
        # best effort and reconstructible after restart, not a second mutation.
        for callback in (self.services.get("active_source_managers"), self.services.get("refresh_publication"),
                         getattr(getattr(self.app.state, "ai_access", None), "recheck", None)):
            if callback:
                try:
                    callback()
                except Exception:
                    pass
        return result

    def reconcile(self, operation_id, *, context, cancelled):
        if cancelled():
            raise AgentToolRejected("agent_cancelled")
        unknown = {"outcome": "unknown", "reason": "No matching atomic sharing receipt; no write was retried."}
        operation = (context or {}).get("operation") or {}
        with self.sources.lock:
            record = self._receipt(operation_id)
            if (not record or record.get("retired") or operation.get("operation_id") != operation_id or operation.get("tool") != NAME
                    or operation.get("arguments_sha256") != record["arguments_sha256"]
                    or digest(operation.get("arguments")) != record["arguments_sha256"]
                    or any(record["binding"].get(key) != (context or {}).get(key) for key in IDENTITY)):
                return unknown
            return {"outcome": "executed", "receipt": {key: copy.deepcopy(record[key]) for key in
                    ("operation_id", "tool", "arguments_sha256", "result")}}

    def verify(self, receipt, *, context, cancelled):
        recovered = self.reconcile(receipt.get("operation_id"), context={**context, "operation": receipt}, cancelled=cancelled)
        proven = recovered["outcome"] == "executed" and recovered["receipt"]["result"] == receipt.get("result")
        result = receipt.get("result") or {}
        binding_known = proven and valid_model_binding(result.get("model_binding"), result.get("service_id"))
        matches = grants_match = binding_matches = source_ready = False
        with self.sources.lock:
            if binding_known:
                try:
                    record, model, sid = self.target(receipt["arguments"])
                    grants = [row for row in self.access.list() if row["service_id"] == sid]
                    allowed = sorted((row["relationship_id"], row["peer_id"]) for row in grants if row["allowed"])
                    expected = sorted((row["relationship_id"], row["peer_id"]) for row in result["recipients"])
                    grants_match = (allowed == expected and all(row["effective"] for row in grants if row["allowed"])
                                    and {row["relationship_id"]: row["revision"] for row in grants} == result["grant_revisions"])
                    binding_matches = model_binding(record, model) == result["model_binding"]
                    source_ready = bool(record.get("enabled") and record.get("status") == "ready")
                    matches = (grants_match and binding_matches and source_ready and model in self.sources.shared_models(record)
                               and model_settings.effective(record, model)["share_scope"] == "friends")
                except AgentToolRejected:
                    pass
        return {"status": "passed" if matches else "failed" if binding_known else "unknown",
                "checked_at": datetime.now(timezone.utc).isoformat(), "evidence": {
                    "operation_attribution": "atomic_sharing_receipt" if proven else "not_established",
                    "receipt_model_binding_valid": bool(binding_known), "current_model_binding_matches": bool(binding_matches),
                    "source_enabled_and_ready": bool(source_ready), "current_state_matches": bool(matches),
                    "exact_recipient_grants": bool(grants_match),
                    "readback_scope": "persisted sharing, grants and committed model binding; no receiver inference probe"}}

    def observation_transition(self, receipt, *, context, cancelled):
        with self.sources.lock:
            verified = self.verify(receipt, context=context, cancelled=cancelled)
            if verified["status"] != "passed":
                return None
            stored = self._receipt(receipt["operation_id"])
            if any(stored["binding"].get(key) != receipt.get(key) for key in ("goal_revision", "scope_revision")):
                return None
            return copy.deepcopy(stored.get("observation_transition"))

    def forget_task(self, task_id):
        if self.services.get("source_store") is None:
            return
        with self.sources.lock:
            records = self.sources.read()
            changed = False
            for source in records.values():
                for row in source.get("_sharing_operations", {}).values():
                    if row["binding"].get("task_id") == task_id and not row.get("retired"):
                        row.pop("result", None)
                        row.pop("observation_transition", None)
                        row["retired"] = True
                        changed = True
            if changed:
                self.sources._write(records)
