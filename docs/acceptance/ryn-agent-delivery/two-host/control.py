"""Local-only client. Requests use the host's token file, never a remote admin token."""
from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx
from node import now, write


class Control:
    def __init__(self, home, port):
        self.home = Path(home)
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=240,
            trust_env=False, headers={"X-Ryn-Local-Token": (self.home / "two-host-control-token").read_text().strip()})

    def request(self, method, path, body=None):
        if not path.startswith("/api/local/"):
            raise ValueError("local_api_only")
        response = self.client.request(method, path, json=body)
        try:
            value = response.json()
        except ValueError:
            value = {"body": response.text}
        record = {"at": now(), "method": method, "path": path, "request": body,
                  "status_code": response.status_code, "response": value}
        out = self.home / "two-host-control"
        out.mkdir(exist_ok=True)
        write(out / (uuid.uuid4().hex + ".json"), record)
        return record

    def snapshot(self):
        return {path: self.request("GET", path) for path in (
            "/api/local/two-host/evidence", "/api/local/agent/models",
            "/api/local/ai-access/friend-services", "/api/local/llm/orders",
            "/api/local/llm/provider-orders")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True)
    parser.add_argument("--port", required=True, type=int)
    sub = parser.add_subparsers(dest="action", required=True)
    api = sub.add_parser("api")
    api.add_argument("method", choices=("GET", "POST", "PUT", "DELETE"))
    api.add_argument("path")
    api.add_argument("--body-file", help="UTF-8 JSON file; never put credentials in it")
    snap = sub.add_parser("snapshot")
    snap.add_argument("--out", required=True)
    sub.add_parser("invite")
    join = sub.add_parser("join")
    join.add_argument("--invite-file", required=True, help="control invite response file or object containing invite_uri")
    refresh = sub.add_parser("refresh")
    refresh.add_argument("peer_id")
    grant = sub.add_parser("grant")
    grant.add_argument("service_id")
    grant.add_argument("relationship_id")
    grant.add_argument("--revision", required=True, type=int)
    grant.add_argument("--deny", action="store_true")
    args = parser.parse_args()
    control = Control(args.home, args.port)
    if args.action == "api":
        result = control.request(args.method, args.path, json.loads(Path(args.body_file).read_text(encoding="utf-8-sig")) if args.body_file else None)
    elif args.action == "snapshot":
        result = control.snapshot()
        write(args.out, result)
        print(json.dumps({"saved": args.out}))
        return
    elif args.action == "invite":
        result = control.request("POST", "/api/local/friends/invites", {})
    elif args.action == "join":
        invite = json.loads(Path(args.invite_file).read_text(encoding="utf-8-sig"))
        result = control.request("POST", "/api/local/friends/join", {"invite_uri": invite.get("response", invite)["invite_uri"]})
    elif args.action == "refresh":
        result = control.request("POST", "/api/local/ai-access/friend-services", {"peer_id": args.peer_id})
    else:
        result = control.request("PUT", "/api/local/ai-access/" + quote(args.service_id, safe="") + "/" + quote(args.relationship_id, safe=""),
                                 {"allowed": not args.deny, "expected_revision": args.revision})
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
