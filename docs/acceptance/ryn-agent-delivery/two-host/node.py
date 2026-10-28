"""Development-only two-host launcher. All network/admission/runtime code is production.

Run this locally on each host; never copy the provider owner's home to the consumer.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import socket
import sys
import threading
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class Audit:
    def __init__(self, path):
        self.path, self.lock = Path(path), threading.Lock()

    def add(self, kind, **value):
        with self.lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": now(), "kind": kind, **value}, ensure_ascii=False) + "\n")


def code_manifest():
    import rynmesh
    root = Path(rynmesh.__file__).parent
    files = {str(path.relative_to(root)).replace("\\", "/"): digest(path)
             for path in sorted(root.rglob("*.py"))}
    return {"python_files": files, "sha256": hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "launcher_sha256": digest(__file__)}


class WireAudit:
    """Observe ASGI bytes without replacing transport or admission decisions."""
    def __init__(self, app, audit, peer_id, messaging_key, allowed_peer=None):
        self.app, self.audit = app, audit
        self.peer_id, self.messaging_key = peer_id, messaging_key
        self.allowed_peer = allowed_peer

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if self.allowed_peer and scope.get("client", ("",))[0] not in {"127.0.0.1", "::1", self.allowed_peer}:
            await send({"type": "http.response.start", "status": 403, "headers": []})
            return await send({"type": "http.response.body", "body": b'test_network_boundary'})
        if path.startswith("/api/local/") and scope.get("client", ("",))[0] not in {"127.0.0.1", "::1", "testclient"}:
            await send({"type": "http.response.start", "status": 403, "headers": []})
            return await send({"type": "http.response.body", "body": b'local_control_requires_loopback'})
        if not path.startswith("/api/peer/"):
            return await self.app(scope, receive, send)
        incoming, outgoing, status = bytearray(), bytearray(), None
        incoming_size, outgoing_size = 0, 0
        async def observed_receive():
            nonlocal incoming_size
            item = await receive()
            if item["type"] == "http.request":
                incoming_size += len(item.get("body", b""))
                incoming.extend(item.get("body", b"")[:max(0, 2 * 1024 * 1024 - len(incoming))])
            return item
        async def observed_send(item):
            nonlocal status, outgoing_size
            if item["type"] == "http.response.start":
                status = item["status"]
            elif item["type"] == "http.response.body":
                outgoing_size += len(item.get("body", b""))
                outgoing.extend(item.get("body", b"")[:max(0, 2 * 1024 * 1024 - len(outgoing))])
            await send(item)
        try:
            await self.app(scope, observed_receive, observed_send)
        finally:
            details = {}
            if path in {"/api/peer/llm/tasks", "/api/peer/llm/tasks/stream"}:
                try:
                    from rynmesh.llm_package.task_protocol import open_task
                    _, body = open_task(json.loads(incoming), recipient_peer_id=self.peer_id,
                                        recipient_messaging_key=self.messaging_key, expected_kind="llm_request")
                    details["authenticated_request"] = body
                except Exception as exc:
                    details["decode_error_type"] = type(exc).__name__
            self.audit.add("peer_http", path=path, client=scope.get("client"), server=scope.get("server"),
                           status=status, request_utf8=incoming.decode("utf-8", "replace"),
                           response_utf8=outgoing.decode("utf-8", "replace"),
                           request_bytes=incoming_size, response_bytes=outgoing_size,
                           request_truncated=incoming_size > len(incoming), response_truncated=outgoing_size > len(outgoing),
                           capture_limit_bytes=2 * 1024 * 1024, **details)


class RestrictedAdapter:
    supports_request_deadline = True

    def __init__(self, inner, owner_path, owner_hash, audit, allowed=True):
        self.inner, self.owner_path, self.owner_hash = inner, owner_path, owner_hash
        self.audit, self.allowed = audit, allowed
        self.model = "glm-5.3-flash" if allowed else "private-boundary-canary"

    def health(self):
        return {"ok": True, "model": "glm-5.3-flash" if self.allowed else "private-boundary-canary"}

    def chat(self, body, **kwargs):
        if not self.allowed:
            raise RuntimeError("two_host_other_model_dispatch_forbidden")
        if digest(self.owner_path) != self.owner_hash:
            raise RuntimeError("two_host_owner_source_changed")
        self.audit.add("upstream_dispatch", task_id=kwargs.get("task_id"), body=body,
                       owner_sources_sha256=self.owner_hash, model="glm-5.3-flash")
        try:
            result = self.inner.chat(body, **kwargs)
            self.audit.add("upstream_response", task_id=kwargs.get("task_id"), result=result)
            return result
        finally:
            if digest(self.owner_path) != self.owner_hash:
                raise RuntimeError("two_host_owner_source_changed")

    def infer(self, *, prompt, max_tokens, **kwargs):
        return self.chat({"messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}, **kwargs)

    def infer_stream(self, **kwargs):
        raise RuntimeError("two_host_unimplemented_legacy_stream")

    def cancel(self, task_id):
        self.audit.add("upstream_cancel", task_id=task_id)
        return self.inner.cancel(task_id)


def install_provider(app, args, audit):
    """No credential is written into the isolated SourceStore or exported evidence."""
    from rynmesh.llm_package import model_settings
    from rynmesh.llm_package.sources import SourceStore
    owner = Path(args.owner_home).resolve()
    if owner == Path(args.home).resolve() or owner in Path(args.home).resolve().parents:
        raise ValueError("isolated_home_required")
    owner_path = owner / "llm/sources.enc"
    before = digest(owner_path)
    key = base64.b64decode((owner / "identity.ed25519").read_text().strip())
    source = SourceStore(owner, key, migrate=False).read()["src-7de9fb1d36c44bc8"]
    if not source.get("enabled") or source.get("status") != "ready" or "glm-5.3-flash" not in source["models"]:
        raise ValueError("configured_glm_source_required")
    inner = SourceStore.adapter(source, "glm-5.3-flash")
    policy = model_settings.effective(source, "glm-5.3-flash")
    sources = app.state.llm_agent_services["source_store"]
    records = sources.read()
    sid = "src-two-host-glm"
    if records and set(records) != {sid}:
        raise ValueError("isolated_source_store_required")
    if not records:
        record = sources.prepare({"name": "Two-host GLM development", "kind": "local",
            "base_url": "http://127.0.0.1:9", "models": ["glm-5.3-flash", "private-boundary-canary"],
            "context_window": policy["context_window"], "max_output_tokens": policy["max_output_tokens"],
            "request_defaults": policy["request_defaults"]}, sid)
        record["status"] = "ready"
        # Only the isolated source's explicit production two-turn probe can
        # establish support. Prior successful acceptance runs are not imported.
        record["tool_support"] = {}
        sources.save(record)
        sources.configure_model(sid, {"model_id": "glm-5.3-flash",
            "expected_rule_revision": record["model_settings"]["glm-5.3-flash"]["rule_revision"],
            "configuration": {"share_scope": "friends"}})
    def adapter(record, model=""):
        model = model or record["models"][0]
        return RestrictedAdapter(inner, owner_path, before, audit,
                                 record["id"] == sid and model == "glm-5.3-flash")
    sources.adapter = adapter
    # create_app may already have cached managers from a prior process's saved
    # source. Force their rebuild through the constrained factory before serving.
    for manager in app.state.llm_agent_services["active_source_managers"]().values():
        if getattr(getattr(manager, "adapter", None), "source_id", None) == sid:
            manager._source_revision = ""
    # Production closure builds ModelDispatchAdapter around the constrained inner adapter.
    managers = app.state.llm_agent_services["active_source_managers"]()
    record = sources.read()[sid]
    for model in record["models"]:
        manager = managers.get(sources.model_service_id(record, model))
        if manager is not None:
            from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
            if not isinstance(manager.adapter, ModelDispatchAdapter) or not isinstance(manager.adapter.inner, RestrictedAdapter):
                raise RuntimeError("two_host_production_dispatch_guard_missing")
    return {"source_id": "src-7de9fb1d36c44bc8", "model_id": "glm-5.3-flash",
            "owner_sources_sha256": before, "owner_source_path": str(owner_path),
            "isolated_source_id": sid, "generation": {k: policy[k] for k in
                ("context_window", "max_output_tokens", "request_defaults")},
            "service_ids": {m: sources.model_service_id(record, m) for m in record["models"]},
            "tool_support_basis": "isolated_production_probe_required", "owner_path_object": owner_path}


def build(args):
    home = Path(args.home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    token_path = home / "two-host-control-token"
    if not token_path.exists():
        token_path.write_text(uuid.uuid4().hex + uuid.uuid4().hex, encoding="ascii")
    for key in list(os.environ):
        if key.startswith("RYNMESH_"):
            del os.environ[key]
    values = {"HOME": str(home), "AUTO_REGISTER": "0", "DISABLE_DISCOVERY": "1",
              "MODEL_PROVIDER": "none", "DEFAULT_DISCOVERY": "0", "REGISTRY_DIR": str(home / "registry"),
              "PEER_HOST": args.bind, "PEER_PORT": str(args.port),
              "PEER_ENDPOINT": f"http://{args.bind}:{args.port}",
              "FRIEND_ENDPOINT": f"http://{args.bind}:{args.port}",
              "LOCAL_TOKEN": token_path.read_text().strip(), "LLM_HOME": str(home / "llm"),
              "NETWORK_ID": "two-host-development", "FRIEND_ALLOW_LOOPBACK": "1" if args.bind == "127.0.0.1" else "0"}
    # Remote Agent inference deliberately requires production strict ICE P2P.
    # A shared real registry is its signaling plane; HTTP friend reachability
    # alone cannot substitute for it. LAN-only ICE needs no external STUN.
    values["LAN_DISCOVERY"] = "0"
    values["P2P_STUN"] = getattr(args, "stun", "off")
    if getattr(args, "registry_url", None):
        values["REGISTRY_URL"] = args.registry_url
    if getattr(args, "p2p_bind_port", None):
        values["P2P_BIND_PORT"] = str(args.p2p_bind_port)
    os.environ.update({"RYNMESH_" + k: v for k, v in values.items()})
    from rynmesh.peer_http import create_app
    from rynmesh.services import peer_box
    from rynmesh.store import RynmeshStore
    store = RynmeshStore(home=home, network_dir=home / "registry")
    app = create_app(store=store)
    audit = Audit(home / "two-host-audit.jsonl")
    owner = install_provider(app, args, audit) if args.role == "provider" else None
    if args.role == "consumer" and app.state.llm_agent_services["source_store"].read():
        raise ValueError("consumer_must_have_no_local_sources")
    identity = {"kind": "two-physical-host-development", "formal_trial_registered": False,
        "role": args.role, "hostname": socket.gethostname(), "platform": platform.platform(),
        "pid": os.getpid(), "python": sys.version, "home": str(home), "peer_id": store.peer_id,
        "messaging_pub": peer_box.public_key_b64(app.state.friends.service.messaging_private),
        "endpoint": values["PEER_ENDPOINT"], "started_at": now(), "code": code_manifest(),
        "signaling_registry_url": values.get("REGISTRY_URL"), "ice_stun": values["P2P_STUN"],
        "ice_bind_port": values.get("P2P_BIND_PORT", "dynamic"), "network_id": values["NETWORK_ID"],
        "transport_claim": "loopback-mechanism-only" if args.bind == "127.0.0.1" else "LAN-direct-configured-not-yet-proven",
        "provider_mapping": {k: v for k, v in owner.items() if k != "owner_path_object"} if owner else None}
    identity_history = home / "two-host-identities"
    identity_history.mkdir(exist_ok=True)
    historical_identity = identity_history / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    write(historical_identity, identity)
    write(home / "two-host-identity.json", identity)
    audit.add("process_start", identity_path=str(historical_identity), identity_sha256=digest(historical_identity),
              pid=os.getpid(), launcher_sha256=identity["code"]["launcher_sha256"],
              signaling_registry_url=identity["signaling_registry_url"])
    runtime = app.state.agent_runtime
    current_task = ContextVar("two_host_observed_task", default=None)
    original_complete = runtime._complete
    def observed_complete(row, messages, specs, cancelled, on_event):
        token = current_task.set(row["id"])
        audit.add("agent_model_start", task_id=row["id"], messages=messages,
                  destination=row.get("model_destination"))
        try:
            result = original_complete(row, messages, specs, cancelled, on_event)
            audit.add("agent_model_result", task_id=row["id"], result=result)
            return result
        finally:
            current_task.reset(token)
    runtime._complete = observed_complete
    services = app.state.llm_agent_services
    original_order = services["execute_order"]
    async def observed_order(body, *positional, **kwargs):
        audit.add("consumer_order_start", agent_task_id=current_task.get(), request=body)
        result = await original_order(body, *positional, **kwargs)
        audit.add("consumer_order_result", agent_task_id=current_task.get(), result=result)
        return result
    services["execute_order"] = observed_order
    original_execute = runtime.tools.execute
    def observed_execute(name, *positional, **kwargs):
        task_id = (kwargs.get("context") or {}).get("task_id")
        audit.add("agent_tool_start", name=name, task_id=task_id)
        try:
            result = original_execute(name, *positional, **kwargs)
            audit.add("agent_tool_result", name=name, task_id=task_id, result=result)
            return result
        except Exception as exc:
            audit.add("agent_tool_error", name=name, task_id=task_id, error_type=type(exc).__name__)
            raise
    runtime.tools.execute = observed_execute

    @app.get("/api/local/two-host/evidence")
    async def evidence(request: Request):
        app.state.friends.local_control(request)
        tasks = runtime.list_tasks(False) + runtime.list_tasks(True)
        return {"identity": identity, "at": now(), "runtime_owner_id": runtime.owner_id,
                "runtime_consumer_node_id": runtime.consumer_node_id,
                "agent_store": {"path": str(runtime.store.path), "encryption_public_key": runtime.store.pub,
                    "sha256": digest(runtime.store.path) if runtime.store.path.exists() else None,
                    "decrypted_bindings": [{k: v for k, v in row.items() if k in
                        {"id", "model_id", "model_destination", "model_destination_required", "status", "created_at"}}
                        for row in runtime.store.read()["tasks"].values()]},
                "tasks": [runtime.get(t["id"]) for t in {t["id"]: t for t in tasks}.values()],
                "owner_source_unchanged": digest(owner["owner_path_object"]) == owner["owner_sources_sha256"] if owner else None,
                "friends": app.state.friends.service.list_friends(),
                "grants": app.state.ai_access.store.list(),
                "sources": [app.state.llm_agent_services["source_store"].public(r)
                            for r in app.state.llm_agent_services["source_store"].read().values()],
                "agent_settings": runtime.settings(), "manual_conversations": app.state.ask_ryn.conversations.list()}
    from release_observation import install_observation_route
    install_observation_route(app, store, code_manifest)
    if getattr(args, "release_controller_config", None):
        from release_tasks import install_registered_routes
        install_registered_routes(app, store, args.release_controller_config)
    return WireAudit(app, audit, store.peer_id, app.state.friends.service.messaging_private, args.allowed_peer)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=("provider", "consumer"))
    parser.add_argument("--release-controller-config", type=Path,
                        help="Explicit active release configuration; requires a fresh isolated AgentStore")
    parser.add_argument("--home", required=True)
    parser.add_argument("--bind", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--owner-home")
    parser.add_argument("--allowed-peer", help="Extra test wrapper IP boundary; not production authorization evidence")
    parser.add_argument("--registry-url", help="Shared production HTTP registry for strict Agent ICE signaling")
    parser.add_argument("--stun", default="off", help="Production P2P STUN setting; off uses LAN host candidates")
    parser.add_argument("--p2p-bind-port", type=int, help="Optional fixed UDP port; disables persistent pooled sessions")
    args = parser.parse_args()
    if args.role == "provider" and not args.owner_home:
        parser.error("provider requires local --owner-home; never send it to consumer")
    if args.role == "consumer" and args.owner_home:
        parser.error("consumer cannot receive owner configuration")
    app = build(args)
    import uvicorn
    sockets = []
    for address in dict.fromkeys(["127.0.0.1", args.bind]):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((address, args.port))
        sock.listen(128)
        sockets.append(sock)
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=sockets)


if __name__ == "__main__":
    main()
