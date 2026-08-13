"""Rynmesh MCP stdio server.

Each running server is one local Rynmesh node. Nodes can discover peers through
an HTTP registry and then fetch content directly from peer HTTP endpoints; the
older shared-directory announcement bus remains only for local development.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from typing import Any

from . import __version__
from .credits import default_credit_amount
from .store import RynmeshStore

PROTOCOL_VERSION = "2025-11-25"
SERVER_INFO = {
    "name": "rynmesh-local",
    "version": __version__,
}


TOOLS = [
    {
        "name": "rynmesh_digest_get",
        "description": "Get the owner's current Daily Digest: ranked items from their chosen sources (RSS/YouTube/Reddit feeds, read-later saves, page watchers) with reasons per pick.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_digest_refresh",
        "description": "Re-fetch all of the owner's digest sources and watched pages, then rebuild and return the ranked digest.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "rynmesh_digest_add_source",
        "description": "Add a content source to the owner's digest: an RSS/Atom URL, a YouTube channel URL or @handle, or a subreddit like r/selfhosted.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_digest_feedback",
        "description": "Record the owner's reaction to a digest item (up/down/opened); tunes future ranking and suppresses seen items.",
        "inputSchema": {
            "type": "object",
            "properties": {"item_id": {"type": "string"}, "action": {"type": "string", "enum": ["up", "down", "opened"]}},
            "required": ["item_id", "action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_readlater_save",
        "description": "Save a URL to the owner's read-later queue; the node fetches it and surfaces it in the next digest.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_watcher_add",
        "description": "Watch a web page for changes (price, stock, release, edits). Changes surface as digest items on refresh.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}, "note": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_send_message",
        "description": "Send an end-to-end encrypted message to a peer's node over the mesh (requires the local node daemon to be running).",
        "inputSchema": {
            "type": "object",
            "properties": {"peer_id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["peer_id", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_read_messages",
        "description": "Read the message history with a peer (requires the local node daemon to be running).",
        "inputSchema": {
            "type": "object",
            "properties": {"peer_id": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["peer_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_node_info",
        "description": "Return this local Rynmesh node identity and storage configuration.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_publish_clip",
        "description": "Compatibility tool: publish a local AI-generated video into Rynmesh.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "media_path": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "transcript": {"type": "string"},
                "preview_path": {"type": "string"},
                "run_id": {"type": "string"},
                "work_id": {"type": "string"},
                "envelope_hash": {"type": "string"},
                "category": {"type": "string"},
                "content_type": {"type": "string"},
                "content_kind": {"type": "string"},
            },
            "required": ["media_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_publish_content",
        "description": "Publish AI-generated or AI-curated content into Rynmesh with signed run and safety receipts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content_path": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "transcript": {"type": "string"},
                "summary_text": {"type": "string"},
                "preview_path": {"type": "string"},
                "run_id": {"type": "string"},
                "work_id": {"type": "string"},
                "envelope_hash": {"type": "string"},
                "category": {"type": "string"},
                "content_type": {"type": "string"},
                "content_kind": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["content_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_list_clips",
        "description": "Compatibility tool: list signed clip announcements visible to this node.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_invalid": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_list_content",
        "description": "List signed content announcements visible to this node.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_invalid": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_register_node",
        "description": "Publish this node's self-signed peer record to the configured registry.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "endpoints": {"type": "array", "items": {"type": "string"}},
                "capabilities": {"type": "array", "items": {"type": "string"}},
                "network_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_discover_peers",
        "description": "Read signed peer records from the configured registry.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "network_id": {"type": "string"},
                "include_self": {"type": "boolean"},
                "max_age_hours": {"type": "number"},
                "use_cache_on_error": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_register_job_capacity",
        "description": "Advertise this node as a polling work-order provider for bounded job capabilities.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "capabilities": {"type": "array", "items": {"type": "string"}},
                "network_id": {"type": "string"},
                "capacity_units": {"type": "integer"},
                "max_concurrent": {"type": "integer"},
                "price_credits": {"type": "object"},
                "polling_interval_sec": {"type": "integer"},
                "metadata": {"type": "object"},
            },
            "required": ["capabilities"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_list_job_capacities",
        "description": "List signed polling job capacity records from the configured registry.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "network_id": {"type": "string"},
                "capability": {"type": "string"},
                "max_age_hours": {"type": "number"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_submit_work_order",
        "description": "Submit a signed polling work order to a provider node through the registry mailbox.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider_peer_id": {"type": "string"},
                "capability": {"type": "string"},
                "operation": {"type": "string"},
                "params": {"type": "object"},
                "network_id": {"type": "string"},
                "input_content_ids": {"type": "array", "items": {"type": "string"}},
                "max_credit_cost": {"type": "number"},
                "idempotency_key": {"type": "string"},
                "result_policy": {"type": "object"},
                "expires_at": {"type": "string"},
                "expires_in_hours": {"type": "number"},
            },
            "required": ["provider_peer_id", "capability", "operation"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_poll_work_orders",
        "description": "Poll this node's provider mailbox for open, accepted, running, completed, failed, or cancelled work orders.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "network_id": {"type": "string"},
                "capability": {"type": "string"},
                "status": {"type": "string"},
                "max_age_hours": {"type": "number"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_publish_work_result",
        "description": "Publish a signed provider result or status update for a work order.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "work_order_id": {"type": "string"},
                "requester_peer_id": {"type": "string"},
                "status": {"type": "string"},
                "message": {"type": "string"},
                "result_content_ids": {"type": "array", "items": {"type": "string"}},
                "result_refs": {"type": "object"},
                "credit_amount": {"type": "number"},
                "network_id": {"type": "string"},
            },
            "required": ["work_order_id", "requester_peer_id", "status"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_list_work_results",
        "description": "List signed provider work-result messages visible in the registry mailbox.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "work_order_id": {"type": "string"},
                "network_id": {"type": "string"},
                "requester_peer_id": {"type": "string"},
                "provider_peer_id": {"type": "string"},
                "status": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_upload_relay_artifact",
        "description": "Upload a local artifact to the configured relay as content-addressed bytes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "relay_url": {"type": "string"},
                "media_type": {"type": "string"},
                "filename": {"type": "string"},
                "expected_hash": {"type": "string"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_download_relay_artifact",
        "description": "Download and hash-verify a relay artifact by content hash.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content_hash": {"type": "string"},
                "destination": {"type": "string"},
                "relay_url": {"type": "string"},
            },
            "required": ["content_hash", "destination"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_relay_artifact_info",
        "description": "Read relay metadata for a content-addressed artifact.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content_hash": {"type": "string"},
                "relay_url": {"type": "string"},
            },
            "required": ["content_hash"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_fetch_preview",
        "description": "Compatibility tool: fetch and locally cache a content preview by clip ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "clip_id": {"type": "string"},
            },
            "required": ["clip_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_fetch_full",
        "description": "Compatibility tool: fetch and locally cache full content by clip ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "clip_id": {"type": "string"},
            },
            "required": ["clip_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_fetch_content_preview",
        "description": "Fetch and locally cache a content preview after validating its receipts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content_id": {"type": "string"},
            },
            "required": ["content_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_fetch_content_full",
        "description": "Fetch and locally cache full content after validating its receipts and hash.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content_id": {"type": "string"},
            },
            "required": ["content_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_list_peer_clips",
        "description": "Compatibility tool: list clips advertised directly by a peer HTTP endpoint.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string"},
            },
            "required": ["endpoint"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_list_peer_content",
        "description": "List content advertised directly by a peer HTTP endpoint.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string"},
            },
            "required": ["endpoint"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_fetch_peer_preview",
        "description": "Compatibility tool: fetch a preview directly from a peer endpoint by clip ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string"},
                "clip_id": {"type": "string"},
                "expected_peer_id": {"type": "string"},
            },
            "required": ["endpoint", "clip_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_fetch_peer_full",
        "description": "Compatibility tool: fetch full content directly from a peer endpoint by clip ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string"},
                "clip_id": {"type": "string"},
                "expected_peer_id": {"type": "string"},
            },
            "required": ["endpoint", "clip_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_fetch_peer_content_preview",
        "description": "Fetch a content preview directly from a peer endpoint after local manifest validation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string"},
                "content_id": {"type": "string"},
                "expected_peer_id": {"type": "string"},
            },
            "required": ["endpoint", "content_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_fetch_peer_content_full",
        "description": "Fetch full content directly from a peer endpoint after local hash validation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string"},
                "content_id": {"type": "string"},
                "expected_peer_id": {"type": "string"},
            },
            "required": ["endpoint", "content_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_credit_summary",
        "description": "Return credit score and distribution weight for a Rynmesh peer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "peer_id": {"type": "string"},
                "category": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_credit_scoreboard",
        "description": "Return visible Rynmesh Credit accounts ranked by distribution weight.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_record_credit_event",
        "description": "Append a signed Rynmesh Credit event for useful work or protocol penalties.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "subject_peer_id": {"type": "string"},
                "amount": {"type": "number"},
                "role": {"type": "string"},
                "category": {"type": "string"},
                "subject_id": {"type": "string"},
                "evidence_hash": {"type": "string"},
                "reason": {"type": "string"},
                "dedupe_key": {"type": "string"},
                "metadata": {"type": "object"},
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_rank_clips",
        "description": "Compatibility tool: rank visible clips by Rynmesh Credit distribution weight.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_invalid": {"type": "boolean"},
                "category": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "rynmesh_rank_content",
        "description": "Rank visible content by Rynmesh Credit distribution weight.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_invalid": {"type": "boolean"},
                "category": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
]


def _write(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, indent=2, ensure_ascii=False),
            }
        ],
        "structuredContent": payload,
        "isError": False,
    }


def _error_response(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def _daemon_json(path: str, payload: dict[str, Any] | None = None, *, method: str = "GET") -> dict[str, Any]:
    """Call the local node daemon over loopback (messaging needs its keys/transport)."""
    import urllib.error
    import urllib.request

    port = os.environ.get("RYNMESH_PEER_PORT", "8791")
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method=method if payload is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"local node daemon not reachable on port {port} — start it with `rynmesh-peer` ({exc})"
        ) from exc


def _dispatch_tool(store: RynmeshStore, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from .services.digest import DigestService

    if tool_name.startswith(("rynmesh_digest_", "rynmesh_readlater_", "rynmesh_watcher_")):
        digest_service = DigestService(store.home)
        if tool_name == "rynmesh_digest_get":
            digest = digest_service.last_digest() or digest_service.build(now_unix=time.time())
            limit = int(arguments.get("limit", 0) or 0)
            if limit > 0:
                digest = {**digest, "items": digest["items"][:limit]}
            return digest
        if tool_name == "rynmesh_digest_refresh":
            digest_service.check_watchers(now_unix=time.time())
            refresh = digest_service.refresh()
            digest = digest_service.build(now_unix=time.time())
            return {"refresh": refresh, "digest": digest}
        if tool_name == "rynmesh_digest_add_source":
            return digest_service.add_source(
                str(arguments.get("url", "")), tags=_string_list(arguments.get("tags", [])) or None
            )
        if tool_name == "rynmesh_digest_feedback":
            return digest_service.feedback(
                str(arguments.get("item_id", "")), str(arguments.get("action", ""))
            )
        if tool_name == "rynmesh_readlater_save":
            return digest_service.save_link(str(arguments.get("url", "")), now_unix=time.time())
        if tool_name == "rynmesh_watcher_add":
            return digest_service.add_watcher(
                str(arguments.get("url", "")), note=str(arguments.get("note", ""))
            )
    if tool_name == "rynmesh_send_message":
        return _daemon_json(
            "/api/local/messages/send",
            {"peer_id": str(arguments.get("peer_id", "")), "text": str(arguments.get("text", ""))},
        )
    if tool_name == "rynmesh_read_messages":
        from urllib.parse import quote as _quote

        peer_id = str(arguments.get("peer_id", ""))
        history = _daemon_json(f"/api/local/messages?peer_id={_quote(peer_id, safe='')}")
        limit = int(arguments.get("limit", 0) or 0)
        if isinstance(history, list):
            return {"messages": history[-limit:] if limit > 0 else history}
        return {"messages": history}
    if tool_name == "rynmesh_node_info":
        return store.node_info()
    if tool_name == "rynmesh_publish_clip":
        return store.publish_clip(
            arguments.get("media_path", ""),
            title=str(arguments.get("title", "") or ""),
            description=str(arguments.get("description", "") or ""),
            transcript=str(arguments.get("transcript", "") or ""),
            preview_path=str(arguments.get("preview_path", "") or "") or None,
            run_id=str(arguments.get("run_id", "") or ""),
            work_id=str(arguments.get("work_id", "") or ""),
            envelope_hash=str(arguments.get("envelope_hash", "") or ""),
            category=str(arguments.get("category", "general") or "general"),
            content_type=str(arguments.get("content_type", "") or ""),
            content_kind=str(arguments.get("content_kind", "video") or "video"),
        )
    if tool_name == "rynmesh_publish_content":
        return store.publish_content(
            arguments.get("content_path", ""),
            title=str(arguments.get("title", "") or ""),
            description=str(arguments.get("description", "") or ""),
            transcript=str(arguments.get("transcript", "") or ""),
            summary_text=str(arguments.get("summary_text", "") or ""),
            preview_path=str(arguments.get("preview_path", "") or "") or None,
            run_id=str(arguments.get("run_id", "") or ""),
            work_id=str(arguments.get("work_id", "") or ""),
            envelope_hash=str(arguments.get("envelope_hash", "") or ""),
            category=str(arguments.get("category", "general") or "general"),
            content_type=str(arguments.get("content_type", "") or ""),
            content_kind=str(arguments.get("content_kind", "") or ""),
            tags=_string_list(arguments.get("tags", [])),
        )
    if tool_name == "rynmesh_list_clips":
        return store.list_clips(include_invalid=bool(arguments.get("include_invalid", False)))
    if tool_name == "rynmesh_list_content":
        return store.list_content(include_invalid=bool(arguments.get("include_invalid", False)))
    if tool_name == "rynmesh_register_node":
        return store.register_node(
            endpoints=arguments.get("endpoints") or None,
            capabilities=arguments.get("capabilities") or None,
            network_id=str(arguments.get("network_id", "rynmesh-main") or "rynmesh-main"),
        )
    if tool_name == "rynmesh_discover_peers":
        return store.discover_peers(
            network_id=str(arguments.get("network_id", "rynmesh-main") or "rynmesh-main"),
            include_self=bool(arguments.get("include_self", False)),
            max_age_hours=_optional_float(arguments.get("max_age_hours")),
            use_cache_on_error=bool(arguments.get("use_cache_on_error", True)),
        )
    if tool_name == "rynmesh_register_job_capacity":
        return store.register_job_capacity(
            capabilities=_string_list(arguments.get("capabilities", [])),
            network_id=str(arguments.get("network_id", "rynmesh-main") or "rynmesh-main"),
            capacity_units=int(arguments.get("capacity_units", 1) or 1),
            max_concurrent=int(arguments.get("max_concurrent", 1) or 1),
            price_credits=_float_dict(arguments.get("price_credits", {})),
            polling_interval_sec=int(arguments.get("polling_interval_sec", 30) or 30),
            metadata=_dict_arg(arguments.get("metadata", {})),
        )
    if tool_name == "rynmesh_list_job_capacities":
        return store.list_job_capacities(
            network_id=str(arguments.get("network_id", "rynmesh-main") or "rynmesh-main"),
            capability=str(arguments.get("capability", "") or ""),
            max_age_hours=_optional_float(arguments.get("max_age_hours")),
        )
    if tool_name == "rynmesh_submit_work_order":
        return store.submit_work_order(
            provider_peer_id=str(arguments.get("provider_peer_id", "") or ""),
            capability=str(arguments.get("capability", "") or ""),
            operation=str(arguments.get("operation", "") or ""),
            params=_dict_arg(arguments.get("params", {})),
            network_id=str(arguments.get("network_id", "rynmesh-main") or "rynmesh-main"),
            input_content_ids=_string_list(arguments.get("input_content_ids", [])),
            max_credit_cost=float(arguments.get("max_credit_cost", 0.0) or 0.0),
            idempotency_key=str(arguments.get("idempotency_key", "") or ""),
            result_policy=_dict_arg(arguments.get("result_policy", {})),
            expires_at=str(arguments.get("expires_at", "") or ""),
            expires_in_hours=float(arguments.get("expires_in_hours", 6.0) or 6.0),
        )
    if tool_name == "rynmesh_poll_work_orders":
        return store.poll_work_orders(
            network_id=str(arguments.get("network_id", "rynmesh-main") or "rynmesh-main"),
            capability=str(arguments.get("capability", "") or ""),
            status=str(arguments.get("status", "open") or "open"),
            max_age_hours=_optional_float(arguments.get("max_age_hours")),
        )
    if tool_name == "rynmesh_publish_work_result":
        return store.publish_work_result(
            work_order_id=str(arguments.get("work_order_id", "") or ""),
            requester_peer_id=str(arguments.get("requester_peer_id", "") or ""),
            status=str(arguments.get("status", "") or ""),
            message=str(arguments.get("message", "") or ""),
            result_content_ids=_string_list(arguments.get("result_content_ids", [])),
            result_refs=_dict_arg(arguments.get("result_refs", {})),
            credit_amount=float(arguments.get("credit_amount", 0.0) or 0.0),
            network_id=str(arguments.get("network_id", "rynmesh-main") or "rynmesh-main"),
        )
    if tool_name == "rynmesh_list_work_results":
        return store.list_work_results(
            work_order_id=str(arguments.get("work_order_id", "") or ""),
            network_id=str(arguments.get("network_id", "rynmesh-main") or "rynmesh-main"),
            requester_peer_id=str(arguments.get("requester_peer_id", "") or ""),
            provider_peer_id=str(arguments.get("provider_peer_id", "") or ""),
            status=str(arguments.get("status", "") or ""),
        )
    if tool_name == "rynmesh_upload_relay_artifact":
        return store.upload_relay_artifact(
            arguments.get("path", ""),
            relay_url=str(arguments.get("relay_url", "") or ""),
            media_type=str(arguments.get("media_type", "") or ""),
            filename=str(arguments.get("filename", "") or ""),
            expected_hash=str(arguments.get("expected_hash", "") or ""),
        )
    if tool_name == "rynmesh_download_relay_artifact":
        return store.download_relay_artifact(
            str(arguments.get("content_hash", "") or ""),
            arguments.get("destination", ""),
            relay_url=str(arguments.get("relay_url", "") or ""),
        )
    if tool_name == "rynmesh_relay_artifact_info":
        return store.relay_artifact_info(
            str(arguments.get("content_hash", "") or ""),
            relay_url=str(arguments.get("relay_url", "") or ""),
        )
    if tool_name == "rynmesh_fetch_preview":
        return store.fetch_preview(str(arguments.get("clip_id", "") or ""))
    if tool_name == "rynmesh_fetch_full":
        return store.fetch_full(str(arguments.get("clip_id", "") or ""))
    if tool_name == "rynmesh_fetch_content_preview":
        return store.fetch_content_preview(str(arguments.get("content_id", "") or ""))
    if tool_name == "rynmesh_fetch_content_full":
        return store.fetch_content_full(str(arguments.get("content_id", "") or ""))
    if tool_name == "rynmesh_list_peer_clips":
        return store.list_peer_clips(str(arguments.get("endpoint", "") or ""))
    if tool_name == "rynmesh_list_peer_content":
        return store.list_peer_content(str(arguments.get("endpoint", "") or ""))
    if tool_name == "rynmesh_fetch_peer_preview":
        return store.fetch_peer_preview(
            str(arguments.get("endpoint", "") or ""),
            str(arguments.get("clip_id", "") or ""),
            expected_peer_id=str(arguments.get("expected_peer_id", "") or ""),
        )
    if tool_name == "rynmesh_fetch_peer_full":
        return store.fetch_peer_full(
            str(arguments.get("endpoint", "") or ""),
            str(arguments.get("clip_id", "") or ""),
            expected_peer_id=str(arguments.get("expected_peer_id", "") or ""),
        )
    if tool_name == "rynmesh_fetch_peer_content_preview":
        return store.fetch_peer_content_preview(
            str(arguments.get("endpoint", "") or ""),
            str(arguments.get("content_id", "") or ""),
            expected_peer_id=str(arguments.get("expected_peer_id", "") or ""),
        )
    if tool_name == "rynmesh_fetch_peer_content_full":
        return store.fetch_peer_content_full(
            str(arguments.get("endpoint", "") or ""),
            str(arguments.get("content_id", "") or ""),
            expected_peer_id=str(arguments.get("expected_peer_id", "") or ""),
        )
    if tool_name == "rynmesh_credit_summary":
        return store.credit_summary(
            peer_id=str(arguments.get("peer_id", "") or "") or None,
            category=str(arguments.get("category", "global") or "global"),
        )
    if tool_name == "rynmesh_credit_scoreboard":
        return store.credit_scoreboard(category=str(arguments.get("category", "global") or "global"))
    if tool_name == "rynmesh_record_credit_event":
        raw_metadata = arguments.get("metadata", {}) or {}
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        kind = str(arguments.get("kind", "") or "")
        subject_peer_id = str(arguments.get("subject_peer_id", "") or "") or None
        _validate_manual_credit_event(store, kind=kind, subject_peer_id=subject_peer_id)
        return store.record_credit_event(
            kind=kind,
            subject_peer_id=subject_peer_id,
            amount=arguments.get("amount") if "amount" in arguments else None,
            role=str(arguments.get("role", "node") or "node"),
            category=str(arguments.get("category", "general") or "general"),
            subject_id=str(arguments.get("subject_id", "") or ""),
            evidence_hash=str(arguments.get("evidence_hash", "") or ""),
            reason=str(arguments.get("reason", "") or ""),
            dedupe_key=str(arguments.get("dedupe_key", "") or ""),
            metadata=metadata,
        )
    if tool_name == "rynmesh_rank_clips":
        return store.rank_clips(
            include_invalid=bool(arguments.get("include_invalid", False)),
            category=str(arguments.get("category", "global") or "global"),
        )
    if tool_name == "rynmesh_rank_content":
        return store.rank_content(
            include_invalid=bool(arguments.get("include_invalid", False)),
            category=str(arguments.get("category", "global") or "global"),
        )
    raise ValueError(f"Unknown Rynmesh tool: {tool_name}")


def _validate_manual_credit_event(
    store: RynmeshStore,
    *,
    kind: str,
    subject_peer_id: str | None,
) -> None:
    amount = default_credit_amount(kind)
    if amount > 0 and not _manual_positive_credit_enabled():
        raise ValueError("manual_positive_credit_event_disabled")
    if amount >= 0:
        return
    active_subject = subject_peer_id or store.peer_id
    if active_subject == store.peer_id:
        return
    if store.peer_id in store.credit_ledger.policy.trusted_penalty_issuers:
        return
    raise ValueError("manual_external_penalty_event_disabled")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [str(item) for item in value if str(item)]


def _dict_arg(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _float_dict(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {str(key): float(raw) for key, raw in value.items()}


def _optional_float(value: Any) -> float | None:
    if value in {"", None}:
        return None
    return float(value)


def _manual_positive_credit_enabled() -> bool:
    return os.environ.get("RYNMESH_ALLOW_MANUAL_POSITIVE_CREDITS", "").lower() in {
        "1",
        "true",
        "yes",
    }


def serve() -> int:
    store = RynmeshStore()
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue

        req_id = message.get("id")
        method = message.get("method", "")
        params = message.get("params", {}) or {}
        if not isinstance(params, dict):
            _write(_error_response(req_id, -32602, "Invalid params"))
            continue

        if not req_id and method == "notifications/initialized":
            continue

        if method == "initialize":
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {
                            "tools": {
                                "listChanged": False,
                            }
                        },
                        "serverInfo": SERVER_INFO,
                    },
                }
            )
            continue

        if method == "tools/list":
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "tools": TOOLS,
                    },
                }
            )
            continue

        if method == "tools/call":
            try:
                tool_name = str(params.get("name", "") or "")
                arguments = params.get("arguments", {}) or {}
                if not isinstance(arguments, dict):
                    raise ValueError("Invalid tool arguments")
                payload = _dispatch_tool(store, tool_name, arguments)
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": _result_payload(payload),
                    }
                )
            except Exception as exc:
                _write(_error_response(req_id, -32000, str(exc)))
            continue

        _write(_error_response(req_id, -32601, f"Method not found: {method}"))
    return 0


def main() -> int:
    try:
        return serve()
    except KeyboardInterrupt:
        return 0
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
