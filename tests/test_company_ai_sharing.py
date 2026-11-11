"""Company-channel sharing of a CLI model between two real nodes.

The provider publishes a fake Codex CLI through a plugin peer provider and a
colleague's node calls it. Envelopes, admission, binding checks and the usage
ledger are the real ones; only the CLI process and the network are replaced.
"""
import copy
import json
import os
import socket
import subprocess
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.drop.peers import make_hello
from rynmesh.llm_package import cli_models
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.manifest import LLMPackageManifest, save_manifest
from rynmesh.llm_package.model_usage import ModelUsageStore, company_usage, usage_caller_scope
from rynmesh.llm_package.peer_binding import expected, valid
from rynmesh.llm_package.routes import _expires, install_llm_routes
from rynmesh.llm_package.task_protocol import open_task, seal_task
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore

RESOURCE = "https://company.test/ryn/connect"
TOOL_CALL = {"id": "call_1", "type": "function",
             "function": {"name": "shell", "arguments": json.dumps({"command": "echo provider-must-not-run > marker.txt"})}}
SHELL_TOOL = {"type": "function", "function": {"name": "shell", "description": "Run a command",
              "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}


class CompanyNode:
    """The plugin peer provider as the host sees it; the company center is not contacted."""

    def __init__(self):
        self.allowed = True
        self.selected = {}
        self.models = []
        self.members = []
        self.calls = []

    def locally_published(self, resource, service_id, binding):
        return resource == RESOURCE and self.selected.get(service_id) == binding

    async def authorize(self, resource, sender, recipient, capability, service_id=None):
        self.calls.append((sender, recipient, capability, service_id))
        if not self.allowed or resource != RESOURCE or capability != "model.invoke":
            raise ValueError("denied")
        return {"allowed": True}

    def cached_models(self):
        return copy.deepcopy(self.models)

    def cached_peers(self):
        return copy.deepcopy(self.members)

    def status(self):
        return [{"plugin_id": "company-workspace", "status": "connected", "bound": True,
                 "selected_service_ids": sorted(self.selected)}]


def configure_cli(node, tmp_path, kind, service_id, alias):
    manifest = LLMPackageManifest(package_id=service_id, mode=kind, adapter=kind, public_model_alias=alias,
                                  capabilities=["text-generation", "buffered-streaming"])
    path = node.store.home / (service_id + ".json")
    save_manifest(manifest, path)
    settings = node.store.home / "llm" / "cli-services.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    current = json.loads(settings.read_text()) if settings.exists() else {}
    # Not shared with the personal space: the company channel is independent.
    current[kind] = {"manifest": str(path), "publication_enabled": False}
    settings.write_text(json.dumps(current))
    return node.services["provider_for"](service_id)


@pytest.fixture
def company(tmp_path, monkeypatch):
    owned_ports = set()
    original_bind, original_connect, original_resolve = socket.socket.bind, socket.socket.connect, socket.getaddrinfo

    def local_bind(sock, address):
        result = original_bind(sock, address)
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            owned_ports.add(sock.getsockname()[1])
        return result

    def no_network(*args, **kwargs):
        raise AssertionError("unexpected network")

    def connect(sock, address):
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"} and address[1] in owned_ports:
            return original_connect(sock, address)
        return no_network()

    def resolve(host, *args, **kwargs):
        if host in {"127.0.0.1", "::1"}:
            return original_resolve(host, *args, **kwargs)
        return no_network()
    monkeypatch.setattr(socket.socket, "bind", local_bind)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr("rynmesh.llm_package.routes.warm_stun_choice", lambda: None)

    command = tmp_path / "codex-test.exe"
    command.write_bytes(b"fixture-v1")
    monkeypatch.setattr(CLIAgentAdapter, "_command_path", lambda _: str(command))
    monkeypatch.setattr(CLIAgentAdapter, "health", lambda _: {"ok": True})
    monkeypatch.setattr(cli_models, "list_models", lambda *_, **__: [{"id": "", "name": "CLI default", "default": True}])
    monkeypatch.setattr(cli_models, "cached_models", cli_models.list_models)
    prompts = []

    def suggest(self, body, **options):
        # What a text-only CLI returns for an agent turn: a suggestion, no execution.
        prompts.append({"kind": self.kind, "body": copy.deepcopy(body)})
        return {"text": "", "message": {"role": "assistant", "content": "", "tool_calls": [copy.deepcopy(TOOL_CALL)]},
                "finish_reason": "tool_calls", "input_tokens": 11, "output_tokens": 7, "duration_ms": 1}
    monkeypatch.setattr(CLIAgentAdapter, "_chat_inner", suggest)

    nodes = []
    for name in ("boss", "colleague"):
        store = RynmeshStore(home=tmp_path / name, network_dir=tmp_path / "network")
        key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
        authority = CompanyNode()
        store.peer_providers = authority
        app = FastAPI()
        app.state.peer_providers = authority
        install_llm_routes(app, store=store, home=store.home, messaging_key=key,
                           resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
        nodes.append(SimpleNamespace(store=store, key=key, authority=authority, app=app,
                                     services=app.state.llm_agent_services))
    p, c = nodes
    manager = configure_cli(p, tmp_path, "codex_cli", "codex-cli", "Codex")
    # The owner ticks "company colleagues": the real candidate list, then the
    # same record the company directory would hand to a colleague.
    published = next(row for row in p.authority.model_status() if row["service"]["package_id"] == "codex-cli")
    p.authority.selected["codex-cli"] = published["model_binding"]
    member = {"peer_id": c.store.peer_id, "member_id": "m-colleague", "member_name": "李同事",
              "display_name": "同事电脑", "provider_resource": RESOURCE}
    owner = {"peer_id": p.store.peer_id, "member_id": "m-boss", "member_name": "老板",
             "display_name": "老板电脑", "provider_resource": RESOURCE}
    p.authority.members = [owner, member]
    c.authority.members = [owner, member]
    row = {**published, "peer_id": p.store.peer_id, "messaging_pub": peer_box.public_key_b64(p.key),
           "endpoints": ["http://127.0.0.1:28111"], "provider_resource": RESOURCE, "plugin_id": "company-workspace",
           "external_permission": {"resource": RESOURCE}, "consumer_peer_id": c.store.peer_id, "binding_verified": True}
    c.authority.models = [row]
    catalog = c.services["external_catalog"]

    def hello(endpoint, method, path, **kwargs):
        nonce = parse_qs(urlsplit(path).query)["nonce"][0]
        return 200, make_hello(peer_id=p.store.peer_id, node_name="Boss", messaging_pub=peer_box.public_key_b64(p.key),
                               nonce=nonce, private_key_bytes=p.store.private_key_bytes)
    catalog.request = hello
    catalog.run_once()
    monkeypatch.setattr("rynmesh.llm_package.routes._peer_post_json",
                        lambda endpoint, path, wire, **kwargs: p.services["provider_for"](
                            open_service_id(wire, p)).handle(wire))

    spawned = []

    def refuse(*args, **kwargs):
        spawned.append(args[0] if args else kwargs)
        raise AssertionError("the provider must not start a process for a colleague")
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(os, "system", refuse)
    result = SimpleNamespace(p=p, c=c, manager=manager, row=row, prompts=prompts, spawned=spawned,
                             published=published, tmp=tmp_path)
    yield result
    for node in nodes:
        node.app.state.llm_shutdown()


def open_service_id(wire, provider):
    return open_task(copy.deepcopy(wire), recipient_peer_id=provider.store.peer_id,
                     recipient_messaging_key=provider.key, expected_kind="llm_request")[1]["service_id"]


def wire(company, task, service_id="codex-cli", row=None, **overrides):
    p, c = company.p, company.c
    body = {"task_id": task, "service_id": service_id,
            "chat": {"messages": [{"role": "user", "content": "list files"}], "tools": [SHELL_TOOL]},
            "max_tokens": 32, "max_amount": .01, "expected_model_binding": expected(row or company.row, c.store.peer_id),
            "external_permission": {"resource": RESOURCE}, "reply_messaging_pub": peer_box.public_key_b64(c.key), **overrides}
    return seal_task(body=body, task_id=task, kind="llm_request", sender_peer_id=c.store.peer_id,
                     recipient_peer_id=p.store.peer_id, sender_signing_key=c.store.private_key_bytes,
                     recipient_messaging_pub=peer_box.public_key_b64(p.key), expires_at=_expires(300)).to_dict()


def opened(company, encrypted):
    return open_task(encrypted, recipient_peer_id=company.c.store.peer_id,
                     recipient_messaging_key=company.c.key, expected_kind="llm_response")[1]


def test_cli_models_can_be_published_to_the_company(company):
    candidates = {row["service"]["package_id"]: row for row in company.p.authority.model_status()}
    assert candidates["codex-cli"]["service"]["adapter"] == "codex_cli"
    assert valid(candidates["codex-cli"]["model_binding"], "codex-cli")
    # Only public fields are offered to the directory: no CLI inventory, paths or keys.
    assert set(candidates["codex-cli"]) <= {"service", "capacity", "model_binding", "chat_protocol",
                                            "tool_support", "delivery_protocols"}
    assert CLIAgentAdapter("codex_cli").capabilities()["tool_execution"] == "origin_client"


def test_two_nodes_colleague_gets_a_suggestion_and_the_provider_runs_nothing(company):
    p, c = company.p, company.c
    assert not company.manager.accepting_orders  # not shared with the personal space
    assert not p.store.personal_space.allows_ai(c.store.peer_id)
    bridge, _, catalog = build_agent_integrations(c.app, c.store, c.store.home)
    model = next(row for row in catalog.list_models() if row.get("provider_resource") == RESOURCE)
    assert model["id"].startswith("provider/") and model["adapter"] == "codex_cli"
    assert model["source_name"] == "老板" and model["available"] is True
    assert model["recipient_access"]["kind"] == "provider"

    result = bridge.complete(model["id"], [{"role": "user", "content": "list files"}], [SHELL_TOOL], lambda: False,
                             expected_destination=catalog.destination(model))
    # The colleague's node receives the tool-call suggestion to run on its own machine.
    assert result["message"]["tool_calls"][0]["function"]["name"] == "shell"
    assert len(company.prompts) == 1 and company.prompts[0]["kind"] == "codex_cli"
    # Nothing was executed for the colleague on the provider.
    assert company.spawned == []
    assert not list(company.tmp.rglob("marker.txt"))
    assert all(call[2] == "model.invoke" for call in p.authority.calls) and p.authority.calls

    # The provider's own ledger names the colleague.
    rows = company_usage(p.services["usage_store"].read_cached())
    assert [(row["model_key"], row["peer_id"], row["name"]) for row in rows] == [("local/codex-cli", c.store.peer_id, "李同事")]
    assert rows[0]["today"] == {"requests": 1, "input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
    assert rows[0]["last_30_days"]["total_tokens"] == 18
    public = TestClient(p.app).get("/api/local/llm/models/usage/company").json()
    assert public["sharing"] is True and public["published_service_ids"] == ["codex-cli"]
    assert public["rows"][0]["member_name"] == "李同事" and public["rows"][0]["model_name"] == "Codex"
    assert "name" not in public["rows"][0]
    # The colleague's node recorded nothing as a provider.
    assert company_usage(c.services["usage_store"].read_cached()) == []

    # The center withdraws the colleague: the same call is refused before any model work.
    p.authority.allowed = False
    with pytest.raises(Exception):
        bridge.complete(model["id"], [{"role": "user", "content": "again"}], [SHELL_TOOL], lambda: False,
                        expected_destination=catalog.destination(model))
    assert len(company.prompts) == 1
    assert company_usage(p.services["usage_store"].read_cached())[0]["today"]["requests"] == 1


@pytest.mark.parametrize("change", ["revoked", "unpublished"])
def test_revoked_or_unpublished_cli_is_rejected(company, change):
    if change == "revoked":
        company.p.authority.allowed = False
    else:
        company.p.authority.selected.clear()
    result = opened(company, company.manager.handle(wire(company, "denied-" + change)))
    assert result["state"] == "rejected" and "output" not in result
    assert company.prompts == [] and company.spawned == []
    assert company_usage(company.p.services["usage_store"].read_cached()) == []


def test_revoke_during_cli_generation_withholds_the_result(company):
    original = CLIAgentAdapter._chat_inner

    def revoke(self, body, **options):
        value = original(self, body, **options)
        company.p.authority.allowed = False
        return value
    CLIAgentAdapter._chat_inner = revoke
    try:
        result = opened(company, company.manager.handle(wire(company, "revoked-midway")))
    finally:
        CLIAgentAdapter._chat_inner = original
    assert result["state"] != "succeeded" and "message" not in result and "output" not in result


def test_company_request_cannot_borrow_personal_space_or_friend_authority(company):
    for extra in ({"ai_permission": {"relationship_id": "a" * 32, "revision": 1}},
                  {"space_permission": {"space_id": "b" * 32, "snapshot_revision": 1, "access_generation": "c" * 32}}):
        result = opened(company, company.manager.handle(wire(company, "mixed-" + next(iter(extra)), **extra)))
        assert result["state"] == "rejected"
    # Without the company permission the CLI still requires a personal space.
    body = wire(company, "no-company", external_permission=None)
    with pytest.raises(Exception):
        opened(company, company.manager.handle(body))
    assert company.prompts == []


def test_claude_cli_is_bound_for_the_company_only(company):
    p, c = company.p, company.c
    manager = configure_cli(p, company.tmp, "claude_cli", "claude-cli", "Claude Code")
    # Personal-space catalogs are unchanged: Claude Code still advertises no binding.
    assert "model_binding" not in manager.public_status()
    published = next(row for row in p.authority.model_status() if row["service"]["package_id"] == "claude-cli")
    assert valid(published["model_binding"], "claude-cli")
    p.authority.selected["claude-cli"] = published["model_binding"]
    row = {**company.row, **published}
    result = opened(company, manager.handle(wire(company, "claude-1", "claude-cli", row)))
    assert result["state"] == "succeeded" and company.prompts[-1]["kind"] == "claude_cli"
    # A changed executable invalidates what was published.
    (company.tmp / "codex-test.exe").write_bytes(b"fixture-v2-different")
    assert opened(company, manager.handle(wire(company, "claude-2", "claude-cli", row)))["state"] == "rejected"
    assert company.spawned == []


def test_usage_written_before_per_colleague_buckets_still_reads(tmp_path):
    path = tmp_path / "llm" / "model-usage.json"
    path.parent.mkdir(parents=True)
    store = ModelUsageStore(path)
    store.admit_and_record_dispatch("local/model-a", caller="shared", task_id="old-1")
    store.record_completion("local/model-a", caller="shared", task_id="old-1", input_tokens=5, output_tokens=5)
    old = json.loads(path.read_text())
    for entry in old.values():
        entry.pop("by_peer")  # the format written by earlier versions
    old["legacy-model"] = {"day": "2020-01-01", "requests": 4}
    path.write_text(json.dumps(old))
    before = store.public_usage("local/model-a")
    assert before["total"]["total_tokens"] == 10 and before["by_caller"]["shared"]["total"]["requests"] == 1
    assert company_usage(store.read_cached()) == []

    peer = {"channel": "company", "peer_id": "peer-1", "name": "王同事"}
    with usage_caller_scope("shared", peer=peer):
        store.admit_and_record_dispatch("local/model-a", task_id="new-1")
        store.record_completion("local/model-a", task_id="new-1", input_tokens=3, output_tokens=4)
    # A personal-space call has no per-colleague bucket, and "self" never does.
    store.admit_and_record_dispatch("local/model-a", caller="shared", task_id="space-1")
    with usage_caller_scope("self", peer=peer):
        store.admit_and_record_dispatch("local/model-a", task_id="self-1")
    after = store.public_usage("local/model-a")
    assert after["total"]["total_tokens"] == 17 and after["total"]["requests"] == 4
    assert after["by_caller"]["shared"]["total"]["requests"] == 3
    assert store.public_usage("local/legacy-model")["total"]["requests"] == 4
    rows = company_usage(store.read_cached())
    assert len(rows) == 1 and rows[0]["name"] == "王同事"
    assert rows[0]["today"] == {"requests": 1, "input_tokens": 3, "output_tokens": 4, "total_tokens": 7}
    assert store.reset_usage("local/model-a", scope="today")["today"]["requests"] == 0
    assert company_usage(store.read_cached())[0]["today"]["requests"] == 0
