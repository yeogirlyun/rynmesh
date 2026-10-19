"""Owner credential control without task/SourceStore lock inversion."""
from __future__ import annotations

import copy
import hashlib
import uuid

from ..file_transactions import file_transaction
from .control import ambiguous, check_version
from .store import AgentError, encoded


def gate(store, task_id):
    # Same gate is held by task deletion, so a late owner form cannot recreate
    # secret storage after its task tombstone and credential purge.
    name = hashlib.sha256(task_id.encode()).hexdigest()
    return file_transaction(store.root / "credential-gates" / (name + ".lock"))


def available(row, settings):
    return (settings.get("enabled", False) and not row.get("archived") and not row.get("in_flight")
            and not ambiguous(row) and row["status"] in {"queued", "waiting_input", "paused", "completed", "failed"})


def change(runtime, task_id, value, *, revoke=False):
    # Import at call time avoids coupling the runtime's construction to this
    # owner-only endpoint module.
    from .runtime import event, public_task, task

    owner_fields = {"expected_control_revision", "expected_goal_revision", "pending_input_id"}
    safe_fields = {"client_request_id", "kind", "base_url", "source_id"} | ({"expected_source_revision"} if not revoke else set())
    if not isinstance(value, dict) or set(value) - owner_fields - safe_fields - ({"api_key", "reuse_existing"} if not revoke else set()):
        raise AgentError("agent_invalid_request")
    request_id = value.get("client_request_id")
    try:
        if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
            raise ValueError
    except ValueError:
        raise AgentError("agent_invalid_request") from None
    operation = "revoke" if revoke else "create"
    method = getattr(runtime.tools, "revoke_source_credential" if revoke else "create_source_credential", None)
    if method is None:
        raise AgentError("agent_source_credentials_unavailable")
    with gate(runtime.store, task_id):
        data = runtime.store.read()
        row = task(data, task_id)
        prior = row.get("credential_requests", {}).get(request_id)
        control = {key: value.get(key) for key in owner_fields}
        if prior and (prior["operation"] != operation or prior["control"] != control):
            raise AgentError("agent_request_conflict")
        if not prior:
            check_version(row, value, goal=True)
            if not available(row, data["settings"]):
                raise AgentError("agent_invalid_transition")
            if len(row.get("credential_requests", {})) >= 128:
                raise AgentError("agent_task_limit")
            pending = row.get("pending_input")
            if value.get("pending_input_id") and (not pending or pending["id"] != value["pending_input_id"] or pending["kind"] != "credential_ref"):
                raise AgentError("agent_input_mismatch")
        # No AgentStore lock here. Source tools may hold their own lock and call
        # cancelled(), which reads AgentStore in the opposite direction.
        body = {key: item for key, item in value.items() if key not in owner_fields}
        metadata = method(task_id, body, owner_id=runtime.owner_id, consumer_node_id=runtime.consumer_node_id,
                          goal_revision=value.get("expected_goal_revision"))
        # Defense in depth: even an integration bug cannot put raw secrets into
        # task events or the public form response.
        metadata = {key: copy.deepcopy(item) for key, item in metadata.items() if key in
                    {"has_credential", "kind", "base_url", "source_id", "credential_version", "expires_at"}}

        def finish(current):
            target = task(current, task_id)
            if not prior:
                check_version(target, value, goal=True)
                if not available(target, current["settings"]):
                    raise AgentError("agent_invalid_transition")
                target.setdefault("credential_requests", {})[request_id] = {"operation": operation, "control": control}
                event(target, "source_credential_revoked" if revoke else "source_credential_saved", metadata=metadata)
                if value.get("pending_input_id") and not revoke:
                    pending = target["pending_input"]
                    target["messages"].append({"role": "user", "message_id": "owner_" + uuid.uuid4().hex,
                        "content": "The owner supplied credentials through the secure local form. No new permissions were granted. "
                                   "Use the task-bound credential slot for this confirmed source endpoint: " + encoded(metadata).decode(),
                        "ui": {"kind": "source_credential", "endpoint": str(metadata.get("base_url", "")),
                               "has_credential": metadata.get("has_credential") is True,
                               "prompt": pending["prompt"], "pending_input_id": pending["id"],
                               "fields": [{"label": "Source endpoint", "value": str(metadata.get("base_url", ""))},
                                          {"label": "Credential", "value": "Configured in secure storage"}]}})
                    target["pending_input"] = None
                    target["status"] = "paused" if target["status"] == "paused" else "queued"
                    event(target, "input_answered", pending_input_id=pending["id"])
            return {"credential": metadata, "task": public_task(target, settings=current["settings"])}
        try:
            return runtime.store.transaction(finish)
        except AgentError as exc:
            if str(exc) in {"agent_control_revision_conflict", "agent_goal_revision_conflict", "agent_invalid_transition"}:
                raise AgentError("agent_credential_changed_control_conflict") from None
            raise
