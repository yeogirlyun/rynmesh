"""Operator-triggered local scenarios; every action is recorded by control.py."""
from __future__ import annotations

import argparse
import json
import uuid

from control import Control
from node import now, write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--out", required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("provider-history")
    start = sub.add_parser("consumer-two-sessions")
    start.add_argument("--model-id", required=True)
    follow = sub.add_parser("followup")
    follow.add_argument("--task-id", required=True)
    follow.add_argument("--revision", required=True, type=int)
    args = parser.parse_args()
    client = Control(args.home, args.port)
    if args.action == "provider-history":
        evidence = client.request("GET", "/api/local/two-host/evidence")["response"]
        identity = evidence["identity"]
        if identity["role"] != "provider":
            raise ValueError("provider_required")
        marker = "PROVIDER_PRIVATE_HISTORY_" + uuid.uuid4().hex
        identifier = "two_host_provider_canary_" + uuid.uuid4().hex
        timestamp = now()
        body = {"expected_revision": 0, "conversation": {"id": identifier, "title": "Provider isolation canary",
            "serviceKey": identity["peer_id"] + "::" + identity["provider_mapping"]["service_ids"]["glm-5.3-flash"],
            "serviceName": "Provider-local history (no inference)", "providerPeerId": identity["peer_id"],
            "networkId": "two-host-development", "createdAt": timestamp, "updatedAt": timestamp,
            "messages": [{"id": uuid.uuid4().hex, "role": "user", "content": marker,
                          "createdAt": timestamp, "status": "complete"}]}}
        result = {"marker": marker, "operation": client.request("PUT", "/api/local/ask/conversations/" + identifier, body)}
    elif args.action == "consumer-two-sessions":
        settings = client.request("PUT", "/api/local/agent/settings", {"enabled": True,
            "model_id": args.model_id, "permission_mode": "confirm_changes", "allowed_write_tools": []})
        result = {"settings": settings, "sessions": []}
        if settings["status_code"] != 200:
            raise RuntimeError("agent_settings_rejected_see_control_receipt")
        for label in ("A", "B"):
            marker = "CONSUMER_" + label + "_" + uuid.uuid4().hex
            message = ("这是只读双机开发检查。请记住本会话标记 " + marker + "。先读取本机设备和模型目录；"
                       "根据实际发现的好友/模型身份，再进行第二轮服务配置检查，核对模型来自哪台设备、"
                       "哪些可见权限仍未知。不要改设置或发送消息。最后简短报告，包含本会话标记。")
            created = client.request("POST", "/api/local/agent/tasks", {"message": message,
                "model_id": args.model_id, "client_request_id": str(uuid.uuid4()), "title": "Two-host isolation " + label})
            result["sessions"].append({"label": label, "marker": marker, "created": created})
        result["execution_claim"] = "Two queued sessions; actual interleaving/concurrency must be established from receipts"
    else:
        result = client.request("POST", "/api/local/agent/tasks/" + args.task_id + "/messages",
            {"message": "请再只读检查一次目前可见的模型目录，然后报告我们本会话此前的标记。不要猜测其他会话内容。",
             "expected_revision": args.revision, "client_request_id": str(uuid.uuid4())})
    write(args.out, result)
    print(json.dumps({"saved": args.out}))


if __name__ == "__main__":
    main()
