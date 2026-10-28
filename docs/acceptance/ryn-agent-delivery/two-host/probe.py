"""Direct signed negative probe, run only on the consumer with its own local key.

The recipient's public identity file is safe to transfer; no owner secret is used.
An unexpected admission MAY call the one authorized GLM, so run with the live gate.
"""
from __future__ import annotations

import argparse
import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from node import now, write

from rynmesh.crypto import public_key_from_private
from rynmesh.llm_package.routes import _open_provider_response
from rynmesh.llm_package.task_protocol import seal_task
from rynmesh.services import peer_box


def probe(home, identity, service, permission, expected=None):
    home = Path(home)
    private = base64.b64decode((home / "identity.ed25519").read_text().strip())
    messaging = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
    peer_id = public_key_from_private(private)
    task_id = "two-host-probe-" + uuid.uuid4().hex
    body = {"task_id": task_id, "service_id": service, "max_tokens": 32, "max_amount": .01,
        "chat": {"messages": [{"role": "user", "content": "Reply OK."}], "max_tokens": 32},
        "ai_permission": permission, "reply_messaging_pub": peer_box.public_key_b64(messaging)}
    if expected is not None:
        body["expected_model_binding"] = expected
    wire = seal_task(body=body, task_id=task_id, kind="llm_request", sender_peer_id=peer_id,
        recipient_peer_id=identity["peer_id"], sender_signing_key=private,
        recipient_messaging_pub=identity["messaging_pub"],
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()).to_dict()
    result = {"kind": "two-host-signed-admission-probe", "at": now(), "task_id": task_id,
              "sender_peer_id": peer_id, "recipient_peer_id": identity["peer_id"],
              "endpoint": identity["endpoint"], "body": body, "wire_request": wire}
    try:
        response = httpx.post(identity["endpoint"] + "/api/peer/llm/tasks", json=wire, timeout=180, trust_env=False)
        result.update(status_code=response.status_code, wire_response=response.text)
        if response.status_code == 200:
            _, decoded = _open_provider_response(response.json(), recipient_peer_id=peer_id,
                messaging_key=messaging, task_id=task_id, provider_peer_id=identity["peer_id"], service_id=service)
            result["authenticated_response"] = decoded
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True)
    parser.add_argument("--provider-identity", required=True)
    parser.add_argument("--service-id", required=True)
    parser.add_argument("--permission-file", required=True)
    parser.add_argument("--expected-binding-file")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    def read(path):
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    result = probe(args.home, read(args.provider_identity), args.service_id,
                   read(args.permission_file), read(args.expected_binding_file) if args.expected_binding_file else None)
    write(args.out, result)
    print(json.dumps({"saved": args.out, "status_code": result.get("status_code"), "error_type": result.get("error_type")}))


if __name__ == "__main__":
    main()
