"""Explicit later-phase live actions; does not modify the frozen node observer.

Provider Agent history is now a real provider-owned task, distinct from the
earlier phase's zero-provider-task invariant. Each action runs only locally.
"""
from __future__ import annotations

import argparse
import json
import uuid

import httpx
from control import Control
from node import now, write

TERMINAL = {"completed", "failed", "paused"}


def snapshot(client):
    result = client.request("GET", "/api/local/two-host/evidence")
    if result["status_code"] != 200:
        raise ValueError("evidence_unavailable_see_control_receipt")
    return result["response"]


def require_idle(evidence):
    if any(row.get("status") not in TERMINAL for row in evidence["tasks"]):
        raise ValueError("active_tasks_present_do_not_toggle_agent")


def select_model(client, model_id, expected_source):
    response = client.request("GET", "/api/local/agent/models")
    rows = response.get("response", {}).get("models", [])
    row = next((row for row in rows if row["id"] == model_id), None)
    if (response["status_code"] != 200 or row is None or not row.get("available")
            or row.get("source") != expected_source):
        raise ValueError("actual_catalog_model_unavailable_or_wrong_source")
    return row


def provider_history(client, model_id):
    before = snapshot(client)
    if before["identity"]["role"] != "provider":
        raise ValueError("provider_role_required")
    require_idle(before)
    selected = select_model(client, model_id, "local_api")
    if selected.get("model_id") != "glm-5.3-flash" or not selected.get("agent_supported"):
        raise ValueError("authorized_glm_agent_model_required")
    marker = "PROVIDER_AGENT_PRIVATE_" + uuid.uuid4().hex
    settings = client.request("PUT", "/api/local/agent/settings", {"enabled": True, "model_id": model_id,
        "permission_mode": "confirm_changes", "allowed_write_tools": []})
    result = {"phase": "provider_own_agent_history", "before": before, "selected_model": selected,
              "marker": marker, "settings": settings}
    if settings["status_code"] == 200:
        result["create"] = client.request("POST", "/api/local/agent/tasks", {
            "message": "这是本机所有者的独立私有会话。请记住标记 " + marker +
                "。只读检查本机模型目录，简短说明哪些模型本机可用，最后原样回复标记。不要发送消息或修改配置。",
            "model_id": model_id, "title": "Provider-owned Agent private history",
            "client_request_id": str(uuid.uuid4())})
    result["claim"] = "Creation receipt only; wait for actual completed status and saved response before claiming assistant history."
    return result


def agent_off(client):
    before = snapshot(client)
    require_idle(before)
    return {"before": before, "change": client.request("PUT", "/api/local/agent/settings", {"enabled": False}),
            "after": snapshot(client)}


def manual_off(client, model_id, max_tokens):
    before = snapshot(client)
    if before["identity"]["role"] != "consumer":
        raise ValueError("consumer_role_required")
    require_idle(before)
    if before["agent_settings"].get("enabled") is not False:
        raise ValueError("disable_agent_explicitly_first")
    selected = select_model(client, model_id, "peer")
    binding = selected.get("expected_model_binding")
    if not binding or binding.get("consumer_peer_id") != before["identity"]["peer_id"]:
        raise ValueError("actual_authenticated_recipient_binding_required")
    marker = "MANUAL_WHILE_AGENT_OFF_" + uuid.uuid4().hex
    request = {"network_id": before["identity"]["network_id"], "provider_peer_id": selected["peer_id"],
        "service_id": selected["service_id"], "expected_model_binding": binding,
        "task_id": "two-host-manual-" + uuid.uuid4().hex, "transport": "p2p", "max_tokens": max_tokens,
        "chat": {"messages": [{"role": "user", "content": "只回复这个标记：" + marker}], "max_tokens": max_tokens}}
    result = client.request("POST", "/api/local/llm/orders", request)
    after = snapshot(client)
    return {"phase": "manual_peer_inference_while_consumer_agent_off", "marker": marker,
            "before": before, "selected_model": selected, "order": result, "after": after,
            "agent_task_ids_unchanged": sorted(row["id"] for row in before["tasks"]) == sorted(row["id"] for row in after["tasks"]),
            "agent_stayed_off": before["agent_settings"].get("enabled") is False and after["agent_settings"].get("enabled") is False}


def connectivity(client, provider_peer_id, endpoint, expected):
    # The operator stops/restarts only the isolated provider externally. This
    # helper never kills processes, changes firewall rules, or retries inference.
    result = {"phase": "operator_disconnect_reconnect_observation", "at": now(), "expected": expected,
              "provider_peer_id": provider_peer_id, "endpoint": endpoint, "before": snapshot(client)}
    try:
        response = httpx.get(endpoint.rstrip("/") + "/health", timeout=5, trust_env=False)
        health = {"status_code": response.status_code, "body": response.json()}
        reached = response.status_code == 200 and health["body"].get("peer_id") == provider_peer_id
    except Exception as exc:
        health, reached = {"error_type": type(exc).__name__}, False
    result.update(health=health, expected_health_observation=(reached == (expected == "reachable")),
        refresh=client.request("POST", "/api/local/ai-access/friend-services", {"peer_id": provider_peer_id}),
        after=snapshot(client), claim="Health/catalog observation only. Actual failed/successful model operation requires its own receipt.")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--out", required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    provider = sub.add_parser("provider-agent-history")
    provider.add_argument("--model-id", required=True)
    sub.add_parser("agent-off")
    manual = sub.add_parser("manual-off")
    manual.add_argument("--model-id", required=True)
    manual.add_argument("--max-tokens", type=int, default=2048)
    connect = sub.add_parser("connectivity-check")
    connect.add_argument("--provider-peer-id", required=True)
    connect.add_argument("--provider-endpoint", required=True)
    connect.add_argument("--expected", required=True, choices=("reachable", "unreachable"))
    args = parser.parse_args()
    client = Control(args.home, args.port)
    try:
        if args.action == "provider-agent-history":
            result = provider_history(client, args.model_id)
        elif args.action == "agent-off":
            result = agent_off(client)
        elif args.action == "manual-off":
            result = manual_off(client, args.model_id, args.max_tokens)
        else:
            result = connectivity(client, args.provider_peer_id, args.provider_endpoint, args.expected)
    except Exception as exc:
        # Preserve the failed action while keeping any detailed HTTP receipt in
        # control's append-only directory. No success is inferred from exit 0.
        result = {"error_type": type(exc).__name__, "error": str(exc)}
    write(args.out, {"kind": "two-host-extra-development-scenario", "formal_trial_registered": False,
                     "action": args.action, "at": now(), "result": result})
    print(json.dumps({"saved": args.out, "error_type": result.get("error_type")}))


if __name__ == "__main__":
    main()
