"""Exercise a real NAS through an isolated Ryn node; preserve synthetic test files.

Example: python scripts/nas_acceptance.py --config build/nas-test/connection.json
The private JSON contains the same fields as the Add NAS form. Never commit it.
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--node", default="http://127.0.0.1:18841")
    parser.add_argument("--output", type=Path, default=Path("build/nas-test/acceptance.json"))
    args = parser.parse_args()
    config = json.loads(
        sys.stdin.read() if str(args.config) == "-" else args.config.read_text(encoding="utf-8-sig")
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = f"ryn-acceptance-{stamp}"
    payload = f"Ryn NAS acceptance\n验收文件：飞牛本地存储\nRun: {stamp}\n".encode()
    checks = []

    with httpx.Client(base_url=args.node, timeout=60, trust_env=False) as client:
        base = "/api/local/plugins/nas"

        def call(method, route="", expected=200, **kwargs):
            response = client.request(method, base + route, **kwargs)
            assert response.status_code == expected, (
                method,
                route,
                response.status_code,
                response.text[:300],
            )
            return response

        call("PUT", json={"enabled": True})
        source = call(
            "POST",
            "/sources",
            json={**config, "name": "fnOS acceptance", "writable": True, "allow_ai": True},
        ).json()
        prefix = f"/sources/{source['id']}"
        call("POST", prefix + "/folders", json={"path": folder})
        path = folder + "/验收报告.txt"
        content = prefix + "/content"
        call("PUT", content, params={"path": path}, content=payload)
        checks.append("create folder and upload UTF-8 file")
        entries = call("GET", prefix + "/files", params={"path": folder}).json()["entries"]
        assert any(e["path"] == path and e["size"] == len(payload) for e in entries)
        downloaded = call("GET", content, params={"path": path}).content
        assert downloaded == payload
        checks.append("list and byte-identical download")
        for mode in ("preview", "ai"):
            assert (
                call("GET", content, params={"path": path, "mode": mode}).json()["text"]
                == payload.decode()
            )
        checks.append("native preview and explicitly permitted AI document read")
        call("PUT", content, expected=409, params={"path": path}, content=b"must not overwrite")
        assert call("GET", content, params={"path": path}).content == payload
        call("GET", content, expected=400, params={"path": "../outside"})
        checks.append("overwrite and traversal refused; original bytes preserved")
        readonly = call(
            "POST",
            "/sources",
            json={
                **config,
                "name": "fnOS read only",
                "root": "/".join(filter(None, [config.get("root", ""), folder])),
                "writable": False,
                "allow_ai": False,
            },
        ).json()
        ro_content = f"/sources/{readonly['id']}/content"
        call("PUT", ro_content, expected=403, params={"path": "blocked.txt"}, content=b"blocked")
        call("GET", ro_content, expected=403, params={"path": "验收报告.txt", "mode": "ai"})
        assert call("GET", ro_content, params={"path": "验收报告.txt"}).content == payload
        checks.append("second connection, scoped root, read-only and AI permission enforcement")
        before = call("GET").json()
        assert config["password"] not in json.dumps(before)
        assert "password" not in source
        checks.append("credentials excluded from API responses")
        # Only use this script on an isolated test node: toggling affects all its sources.
        call("PUT", json={"enabled": False})
        try:
            call("GET", content, expected=409, params={"path": path})
        finally:
            call("PUT", json={"enabled": True})
        assert call("GET", content, params={"path": path}).content == payload
        checks.append("disable rejects operations; enable restores existing connections")
        result = {
            "tested_at_utc": stamp,
            "node": args.node,
            "system": config["system"],
            "protocol": config["protocol"],
            "source_id": source["id"],
            "readonly_source_id": readonly["id"],
            "path": path,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "checks": checks,
            "real_ai_inference": False,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
