"""Owner reads of confirmed task-created text files; never a new resource grant."""
from __future__ import annotations

import hashlib
import threading

from ..nas import NasError
from . import lifecycle
from .resource_writes import MAX_TEXT_BYTES
from .store import AgentError, encoded


def _bound(runtime, row, receipt):
    writes = getattr(runtime.tools, "resource_writes", None)
    if writes is None or receipt.get("tool") != "ryn_create_text_file" or receipt.get("status") != "confirmed" or receipt.get("effect") != "write":
        raise AgentError("agent_artifact_not_found")
    record = writes.ledger.get(receipt["operation_id"])
    context = runtime._execution_context(row)
    expected = {"task_id": row["id"], "owner_id": context["owner_id"], "consumer_node_id": context["consumer_node_id"],
                "operation_id": receipt["operation_id"], "tool": receipt["tool"],
                "goal_revision": receipt["goal_revision"], "scope_revision": receipt["scope_revision"],
                "arguments_sha256": hashlib.sha256(encoded(receipt["arguments"])).hexdigest()}
    if (not record or record.get("status") != "confirmed"
            or any(record.get("binding", {}).get(k) != v for k, v in expected.items())
            or receipt.get("arguments_sha256") != expected["arguments_sha256"]
            or record.get("result") != receipt.get("result")):
        raise AgentError("agent_artifact_not_found")
    result, args = record["result"], receipt["arguments"]
    content = args.get("content", "").encode("utf-8")
    if (result.get("created") != "file" or result.get("business_acknowledged") is not True
            or result.get("overwrite") is not False or result.get("operation_id") != receipt["operation_id"]
            or result.get("name") != args.get("name") or result.get("bytes") != len(content)
            or result.get("content_sha256") != hashlib.sha256(content).hexdigest()
            or record.get("preview", {}).get("content_sha256") != result["content_sha256"]):
        raise AgentError("agent_artifact_not_found")
    return writes, context, record


def listing(runtime, row):
    items = []
    for receipt in row.get("receipts", []):
        if receipt.get("tool") != "ryn_create_text_file" or receipt.get("status") != "confirmed":
            continue
        try:
            _, _, record = _bound(runtime, row, receipt)
        except (AgentError, KeyError, TypeError, ValueError):
            continue
        result = record["result"]
        items.append({"id": receipt["operation_id"], "name": result["name"], "bytes": result["bytes"],
                      "sha256": result["content_sha256"], "created_at": receipt.get("confirmed_at")})
    return {"artifacts": items}


def _revision(row):
    return [row["control_revision"], row["scope_revision"], row.get("context_epoch", 0), row["scope"]]


def read(runtime, row, operation_id):
    receipt = next((r for r in row.get("receipts", []) if r.get("operation_id") == operation_id), None)
    if receipt is None:
        raise AgentError("agent_artifact_not_found")
    writes, context, record = _bound(runtime, row, receipt)
    if (receipt["scope_revision"] != row["scope_revision"]
            or receipt.get("context_epoch", 0) != row.get("context_epoch", 0)
            or record["binding"]["scope_sha256"] != hashlib.sha256(encoded(context["resource_grants"])).hexdigest()):
        raise AgentError("agent_artifact_access_changed")
    resources, result = writes.resources, record["result"]
    item, grant = resources._resolve(result["resource_ref"], context, "read_ai")
    parent = resources._open(receipt["arguments"]["directory_ref"])
    expected_path = (parent["path"] + "/" if parent["path"] else "") + result["name"]
    if (item.get("task_id") != row["id"] or item.get("directory") is not False or item["path"] != expected_path
            or item["grant_id"] != parent["grant_id"]
            or grant["connection_id"] != record["preview"].get("connection_id")
            or grant["connection_access_generation"] != record["preview"].get("connection_access_generation")):
        raise AgentError("agent_artifact_access_changed")
    plugin = resources.nas
    with plugin.lock:
        source = resources._source(grant["connection_id"])
        gate = plugin.source_locks.setdefault(source["id"], threading.RLock())
    try:
        with gate:
            with plugin.lock:
                if resources._generation(resources._source(source["id"])) != grant["connection_access_generation"]:
                    raise AgentError("agent_artifact_access_changed")
            with plugin.connection(source, reuse=True) as connector:
                raw = connector.read(item["path"], MAX_TEXT_BYTES + 1)
            if len(raw) != result["bytes"] or hashlib.sha256(raw).hexdigest() != result["content_sha256"]:
                raise AgentError("agent_artifact_content_changed")
            # Recheck task and connection after I/O, before exposing any bytes.
            latest = lifecycle.find(runtime.store, runtime.store.read(), row["id"])
            if _revision(latest) != _revision(row) or receipt not in latest.get("receipts", []):
                raise AgentError("agent_artifact_access_changed")
            _bound(runtime, latest, receipt)
            resources._resolve(result["resource_ref"], runtime._execution_context(latest), "read_ai")
            with plugin.lock:
                if resources._generation(resources._source(source["id"])) != grant["connection_access_generation"]:
                    raise AgentError("agent_artifact_access_changed")
            text = raw.decode("utf-8-sig")
            # _resolve may validate other grants over the network. Check again
            # after all external validation; do no further external I/O.
            latest = lifecycle.find(runtime.store, runtime.store.read(), row["id"])
            if _revision(latest) != _revision(row) or receipt not in latest.get("receipts", []):
                raise AgentError("agent_artifact_access_changed")
    except (NasError, UnicodeError):
        raise AgentError("agent_artifact_unavailable") from None
    return {"id": operation_id, "name": result["name"], "bytes": len(raw), "sha256": result["content_sha256"], "text": text}
