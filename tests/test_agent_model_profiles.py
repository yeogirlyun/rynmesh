"""Model route notes: deterministic selection, and proof at the provider payload.

The integration tests run the real AgentRuntime -> ModelCatalog ->
AgentModelBridge -> provider adapter chain. Only the last hop is replaced:
Codex's app-server turn (``codex_session._native_chat``) and the Anthropic HTTP
call (``protocol_adapter.model_urlopen``). What they capture is exactly what
would leave the process.
"""
from __future__ import annotations

import copy
import json
import threading
import uuid
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime import model_profiles
from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.prompt import SYSTEM, instruction_layers
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore
from rynmesh.llm_package import cli_models, codex_session, model_settings, protocol_adapter
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.manifest import LLMPackageManifest
from rynmesh.llm_package.protocol_adapter import NativeProtocolAdapter

CODEX_MODEL = "local/codex-cli/model/gpt-fixture"
API_MODEL = "local/api-src-a-claude-fixture"
API_SOURCE = {
    "id": "src-a", "name": "Fixture Anthropic", "protocol": "anthropic_messages", "enabled": True,
    "status": "ready", "destination_revision": "rev-src-a", "models": ["claude-fixture"],
    "tool_support": {"claude-fixture": "supported"},
    # Owner metadata: this model is text-only (catalog_image_fields reads it).
    "model_metadata": {"claude-fixture": {"capabilities": {"vision": False}}},
    "model_settings": {},
}
CHECKER_JSON = json.dumps({"status": "unknown", "evidence_refs": [], "summary": "No mechanical evidence.",
                           "uncovered_requirements": []})
TOOL_NAMES = ("ryn_shell_execute", "ryn_apply_patch", "ryn_file_read", "ryn_file_search")


# ---------------------------------------------------------------- selection rules

def codex_row(model_id=CODEX_MODEL, **extra):
    return {"id": model_id, "source": "local_service", "adapter": "codex_cli", "cli_model": "gpt-fixture",
            "supports_image_input": True, **extra}


def api_row(adapter="openai_compatible", model="custom-model", **extra):
    return {"id": "local/api-x-" + model, "source": "local_api", "adapter": adapter, "model_id": model,
            "supports_image_input": True, **extra}


@pytest.mark.parametrize("model, profile, basis, reason", [
    (codex_row(), "codex-cli", "route", None),
    # The same Codex backend reached from a peer or through a provider keeps the route.
    (codex_row("peer/abc/codex-cli/model/gpt-fixture", source="peer"), "codex-cli", "route", None),
    (codex_row("provider/def/peer/abc/codex-cli", source="peer"), "codex-cli", "route", None),
    ({"id": "x", "cli_source": "codex_cli"}, "codex-cli", "route", None),
    # A custom endpoint named like a Codex model is still an HTTP API route: no name guessing.
    (api_row(model="gpt-5.5-codex"), "generic", "fallback", "no_route_profile"),
    (api_row("openai_responses", "gpt-5.5"), "generic", "fallback", "no_route_profile"),
    (api_row("anthropic_messages", "claude-fixture"), "generic", "fallback", "no_route_profile"),
    ({"id": "local/agy", "adapter": "antigravity_cli"}, "generic", "fallback", "no_route_profile"),
    ({"id": "local/vllm", "adapter": "vllm_custom"}, "generic", "fallback", "unknown_route"),
    (None, "generic", "fallback", "model_unavailable"),
])
def test_route_metadata_selects_profile_and_unknown_routes_fall_back(model, profile, basis, reason):
    selection = model_profiles.resolve(model)
    assert (selection["profile"], selection["basis"], selection["fallback_reason"]) == (profile, basis, reason)
    assert selection["catalog"] == model_profiles.CATALOG and selection["revision"] == 1


@pytest.mark.parametrize("adapter, route", [
    ("openai_compatible", "openai_chat"), ("openai_chat", "openai_chat"), ("ollama", "openai_chat"),
    ("llama_cpp", "openai_chat"), ("openai_responses", "openai_responses"),
    ("anthropic_messages", "anthropic_messages"), ("codex_cli", "codex_cli"), ("antigravity_cli", "antigravity_cli"),
])
def test_adapter_aliases_name_one_route(adapter, route):
    assert model_profiles.route_of({"adapter": adapter}) == route


def test_explicit_override_wins_and_invalid_override_is_reported_not_guessed():
    applied = model_profiles.resolve(codex_row(), override="generic")
    assert (applied["profile"], applied["basis"], applied["override_status"]) == ("generic", "override", "applied")
    assert "codex_backend_tools" not in applied["notes"]
    unknown = model_profiles.resolve(codex_row(), override="no-such-profile")
    assert (unknown["profile"], unknown["basis"], unknown["override_status"]) == ("codex-cli", "route", "unknown_profile")
    mismatch = model_profiles.resolve(api_row(), override="codex-cli")
    assert (mismatch["profile"], mismatch["override_status"], mismatch["fallback_reason"]) == (
        "generic", "route_mismatch", "no_route_profile")
    # Without catalog metadata no route is proven, so a route profile cannot apply.
    assert model_profiles.resolve(None, override="codex-cli")["override_status"] == "route_mismatch"


TEXT_ONLY = {"supports_image_input": False, "model_image_support": "unsupported"}


def test_capability_note_needs_definite_text_only_metadata_under_every_profile():
    text_only = model_profiles.resolve(codex_row(**TEXT_ONLY))
    assert text_only["notes"] == ["codex_backend_tools", "image_input_unavailable"]
    assert model_profiles.resolve(api_row(**TEXT_ONLY), override="generic")["notes"] == ["image_input_unavailable"]
    antigravity = {"id": "local/agy", "adapter": "antigravity_cli", "supports_image_input": False,
                   "model_image_support": "unknown", "image_capability_reason": "antigravity_cli_stream_json_text_only"}
    assert model_profiles.resolve(antigravity)["notes"] == ["image_input_unavailable"]
    # Unverified (peer) or unknown capability is not a claim either way; the
    # dispatch-time rejection and its correction still apply there.
    peer = {"id": "peer/x/svc", "source": "peer", "adapter": "openai_compatible", "supports_image_input": False,
            "model_image_support": "unknown", "image_capability_reason": "peer_image_route_unverified"}
    assert model_profiles.resolve(peer)["notes"] == []
    assert model_profiles.resolve({"id": "x", "adapter": "openai_compatible"})["notes"] == []


def test_resolution_is_stable_and_never_repeats_a_note():
    first = model_profiles.resolve(codex_row(**TEXT_ONLY))
    assert model_profiles.resolve(codex_row(**TEXT_ONLY)) == first
    text = first["instructions"]
    assert text.count(model_profiles.CODEX_BACKEND_TOOLS) == 1
    assert text.count(model_profiles.IMAGE_INPUT_UNAVAILABLE) == 1
    assert model_profiles.resolve(api_row())["instructions"] == ""


def test_override_validation_rejects_unknown_profiles_and_route_mismatch():
    lookup = {CODEX_MODEL: codex_row(), API_MODEL: api_row("anthropic_messages")}.get
    assert model_profiles.validate_overrides({CODEX_MODEL: "generic"}, lookup) == {CODEX_MODEL: "generic"}
    for value, code in (({CODEX_MODEL: "bogus"}, "model_profile_overrides_invalid"),
                        ({API_MODEL: "codex-cli"}, "model_profile_route_mismatch"),
                        ({"": "generic"}, "model_profile_overrides_invalid"),
                        (["generic"], "model_profile_overrides_invalid")):
        with pytest.raises(ValueError, match=code):
            model_profiles.validate_overrides(value, lookup)


def test_notes_sit_between_shared_base_and_authority_without_replacing_either():
    row = {"goal": {"revision": 1, "original_message_id": "m1", "constraints": [{"message_id": "m1"}]},
           "criteria": [], "plan": {"revision": 0}, "model_id": CODEX_MODEL, "scope_revision": 0,
           "permission_mode": "read_only", "scope": {"resource_grants": [], "egress_grants": []}}
    notes = model_profiles.resolve(codex_row())["instructions"]
    layers = instruction_layers(row, persisted_base=SYSTEM, model_notes=notes, project_guidance="AGENTS: keep tests.")
    assert [m["role"] for m in layers] == ["developer", "developer", "developer", "user"]
    assert layers[0]["content"] == SYSTEM and layers[1]["content"] == notes
    assert layers[2]["content"].startswith("This task is read-only")
    assert layers[3]["content"] == "AGENTS: keep tests."
    assert instruction_layers(row, persisted_base=SYSTEM) == [layers[0], layers[2]]


# ------------------------------------------------------------- real dispatch chain

class ToolBridge:
    """Real local tool specs and execution, as the desktop runtime exposes them."""

    def __init__(self, store):
        self.local = LocalTools(store, lambda: OperationLedger(store.home))

    def specs(self):
        return [{"type": "function", "function": {"name": name, "description": SPECS[name][0],
                                                  "parameters": copy.deepcopy(SPECS[name][3])}}
                for name in TOOL_NAMES]

    def metadata(self, name):
        description, effect, risk, _ = SPECS[name]
        return {"effect": effect, "risk": risk, "title": description.split(".")[0], "scope_context_supported": True,
                "supports_preview": False, "preview_precondition": False}

    def preview(self, name, arguments, context=None):
        return self.local.preview(name, arguments, context or {})

    def prepare_scope(self, selection, *, owner_id, consumer_node_id, model_destination):
        return {"resource_grants": [], "egress_grants": []}

    def validate_context(self, context, *, cheap_only=False):
        return None

    def execute(self, name, arguments, *, operation_id, cancelled, context=None, expected_preview=None):
        return self.local.execute(name, arguments, operation_id=operation_id, context=dict(context or {}),
                                  expected_preview=expected_preview, cancelled=cancelled)

    # Enables the optional text-answer checker; no mechanical verifier exists here.
    def verification_specs(self):
        return []

    def verify(self, receipt, criteria, *, context, cancelled):
        return {"status": "unknown", "evidence": {}, "checked_at": "fixed"}


class FakeResponse:
    def __init__(self, value):
        self.body = json.dumps(value).encode()
        self.headers = {"content-type": "application/json"}

    def read(self, _limit=None):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class World:
    """One Ryn node with a local Codex CLI and a local Anthropic Messages source."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp_path, self.key = tmp_path, X25519PrivateKey.generate()
        self.codex_turns, self.http_payloads, self.codex_replies = [], [], []
        monkeypatch.setattr(CLIAgentAdapter, "_command_path", lambda _self: "fixture-codex")
        monkeypatch.setattr(cli_models, "cached_models", lambda *_args: None)
        monkeypatch.setattr(cli_models, "list_models", lambda *_args: [
            {"id": "gpt-fixture", "name": "GPT Fixture", "default": True,
             "supported_reasoning_efforts": ["low", "medium", "high"], "default_reasoning_effort": "medium"}])
        monkeypatch.setattr(codex_session, "_native_chat", self._codex_turn)
        monkeypatch.setattr(protocol_adapter, "model_urlopen", self._http)
        self.workspace = tmp_path / "ws"
        self.workspace.mkdir()
        (self.workspace / "notes.txt").write_text("alpha\n", encoding="utf-8")
        self.source_store = SimpleNamespace(
            read=lambda: {"src-a": copy.deepcopy(API_SOURCE)}, lock=threading.Lock(),
            model_service_id=lambda record, model: f"api-{record['id']}-{model}",
            shared_models=lambda record: [], path=tmp_path / "sources.json")
        codex_manifest = LLMPackageManifest(package_id="codex-cli", mode="codex_cli", adapter="codex_cli",
                                            runtime="external", public_model_alias="Codex CLI")
        api_adapter = NativeProtocolAdapter(protocol="anthropic_messages", base_url="http://127.0.0.1:9",
                                            model="claude-fixture", api_key="fixture-key")
        self.managers = {
            "codex-cli": SimpleNamespace(manifest=codex_manifest, adapter=CLIAgentAdapter("codex_cli"),
                                         _slots=threading.BoundedSemaphore(1), _lock=threading.Lock(), _running=0),
            "api-src-a-claude-fixture": SimpleNamespace(
                manifest=SimpleNamespace(timeout_seconds=60), adapter=api_adapter,
                _slots=threading.BoundedSemaphore(1), _lock=threading.Lock(), _running=0,
                _source_revision=model_settings.fingerprint(API_SOURCE, "claude-fixture")),
        }
        online = {"peer_id": "node-local", "node_name": "Local", "online": True, "ready": True,
                  "chat_protocol": "rynmesh.chat.v1"}
        self.discovered = [
            {**online, "service": {"package_id": "codex-cli", "adapter": "codex_cli", "runtime": "external",
                                   "model_alias": "Codex CLI", "capabilities": ["client-tools"],
                                   "context_window": 200000, "max_output_tokens": 8192}},
            {**online, "service": {"package_id": "api-src-a-claude-fixture", "adapter": "openai_compatible",
                                   "runtime": "external", "model_alias": "claude-fixture",
                                   "capabilities": ["client-tools"]}},
        ]
        self.runtime = self.boot()

    def boot(self):
        services = {"source_store": self.source_store, "discover": lambda _net: copy.deepcopy(self.discovered),
                    "provider_for": lambda service_id: self.managers.get(service_id)}
        store = SimpleNamespace(peer_id="node-local", personal_space=SimpleNamespace(allows_ai=lambda _peer: False),
                                llm_tool_capabilities=SimpleNamespace(support=lambda _manifest, _model: "supported"))
        catalog = ModelCatalog(services, store)
        agent_store = AgentStore(self.tmp_path / "agent", self.key)
        runtime = AgentRuntime(agent_store, AgentModelBridge(services, catalog), ToolBridge(agent_store), catalog,
                               owner_id="owner-local", consumer_node_id="node-local")
        return runtime

    def _codex_turn(self, _adapter, body, **kwargs):
        self.codex_turns.append({"body": copy.deepcopy(body), "model": kwargs.get("model")})
        if body.get("_client_proposals"):
            reply = self.codex_replies.pop(0) if self.codex_replies else {"content": "Done on Codex.", "tool_calls": []}
            text = json.dumps(reply)
        else:
            text = CHECKER_JSON
        return {"text": text, "message": {"role": "assistant", "content": text}, "finish_reason": "stop",
                "input_tokens": 40, "output_tokens": 6, "duration_ms": 1}

    def _http(self, request, timeout=None):
        payload = json.loads(request.data)
        self.http_payloads.append(payload)
        system = " ".join(part.get("text", "") for part in payload.get("system", []))
        text = CHECKER_JSON if system.startswith("Independently assess") else "Done on the API route."
        return FakeResponse({"id": "msg_fixture", "type": "message", "role": "assistant", "model": "claude-fixture",
                             "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                             "usage": {"input_tokens": 40, "output_tokens": 6}})

    def settings(self, **values):
        self.runtime.update_settings({"enabled": True, "workspace_root": str(self.workspace),
                                      "permission_mode": "workspace", **values})

    def run(self, task_id, limit=8):
        for _ in range(limit):
            self.runtime.run_once()
            if self.runtime.get(task_id)["status"] in {"completed", "failed", "waiting_approval", "paused"}:
                break
        return self.runtime.get(task_id)

    def records(self, task_id):
        row = self.runtime.store.read()["tasks"][task_id]
        return [record["output_policy"]["model_profile"] for record in row["budget"]["records"]
                if isinstance(record.get("output_policy"), dict) and "model_profile" in record["output_policy"]]


def codex_messages(turn):
    """The client transcript Ryn hands to the Codex turn (after the rules preamble)."""
    prompt = turn["body"]["messages"][0]["content"]
    return json.loads(prompt.split("\n", 1)[1])


def instruction_messages(messages):
    return [m["content"] for m in messages if m["role"] in {"system", "developer"}]


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def test_codex_and_api_routes_receive_their_own_notes_and_parameters_at_the_provider(world):
    world.settings(model_id=CODEX_MODEL)
    codex_task = world.runtime.create({"message": "Summarize notes.txt.", "model_id": CODEX_MODEL,
                                       "reasoning_effort": "high"})
    assert world.run(codex_task["id"])["status"] == "completed"
    turn = world.codex_turns[-1]
    payload = codex_messages(turn)
    instructions = instruction_messages(payload["messages"])
    assert turn["model"] == "gpt-fixture" and turn["body"]["reasoning_effort"] == "high"
    assert instructions[0] == SYSTEM
    assert sum(model_profiles.CODEX_BACKEND_TOOLS in text for text in instructions) == 1
    assert not any(model_profiles.IMAGE_INPUT_UNAVAILABLE in text for text in instructions)
    assert [m["content"] for m in payload["messages"] if m["role"] == "user"][-1] == "Summarize notes.txt."

    world.settings(model_id=API_MODEL)
    api_task = world.runtime.create({"message": "Summarize notes.txt.", "model_id": API_MODEL})
    assert world.run(api_task["id"])["status"] == "completed"
    native = world.http_payloads[-1]
    system = "\n".join(part["text"] for part in native["system"])
    assert system.startswith(SYSTEM)
    assert model_profiles.IMAGE_INPUT_UNAVAILABLE in system and model_profiles.CODEX_BACKEND_TOOLS not in system
    assert "reasoning" not in native and "reasoning_effort" not in native and "thinking" not in native
    assert native["model"] == "claude-fixture"

    codex_record, api_record = world.records(codex_task["id"])[-1], world.records(api_task["id"])[-1]
    assert codex_record | {"instructions_sha256": None} == {
        "catalog": model_profiles.CATALOG, "profile": "codex-cli", "revision": 1, "basis": "route", "route": "codex_cli",
        "fallback_reason": None, "override_status": None, "notes": ["codex_backend_tools"], "instructions": "sent",
        "instructions_sha256": None, "reasoning_effort": "high"}
    assert (api_record["profile"], api_record["basis"], api_record["fallback_reason"], api_record["route"]) == (
        "generic", "fallback", "no_route_profile", "anthropic_messages")
    assert api_record["notes"] == ["image_input_unavailable"] and api_record["reasoning_effort"] is None
    # Provenance never carries prompt or owner text.
    assert "Summarize" not in json.dumps(world.records(api_task["id"]))
    assert model_profiles.CODEX_BACKEND_TOOLS not in json.dumps(world.records(codex_task["id"]))


def test_switch_and_restart_reresolve_without_old_notes_or_parameters(world):
    world.settings(model_id=CODEX_MODEL)
    task = world.runtime.create({"message": "Read notes.txt.", "model_id": CODEX_MODEL, "reasoning_effort": "high"})
    done = world.run(task["id"])
    preview = world.runtime.preview_model_switch(task["id"], API_MODEL)
    switched = world.runtime.switch_model(task["id"], {
        "model_id": API_MODEL, "expected_control_revision": preview["control_revision"],
        "expected_scope_revision": preview["scope_revision"], "expected_destination": preview["destination"],
        "confirm_data_destination": True, "transfer_resource_scope": False, "client_request_id": str(uuid.uuid4())})
    assert "reasoning_effort" not in switched and done["status"] == "completed"
    world.runtime.add_message(task["id"], {"message": "Continue on the new model.", "expected_revision": switched["revision"]})
    assert world.run(task["id"])["status"] == "completed"
    after_switch = world.http_payloads[-1]
    system = "\n".join(part["text"] for part in after_switch["system"])
    assert model_profiles.CODEX_BACKEND_TOOLS not in system and model_profiles.IMAGE_INPUT_UNAVAILABLE in system
    assert "reasoning" not in after_switch

    # A restart rebuilds everything from the store and the current catalog.
    world.runtime = world.boot()
    world.runtime.reconcile()
    row = world.runtime.get(task["id"])
    world.runtime.add_message(task["id"], {"message": "One more check.", "expected_revision": row["revision"]})
    assert world.run(task["id"])["status"] == "completed"
    restarted = world.http_payloads[-1]
    assert restarted["system"][:2] == after_switch["system"][:2]  # shared base + notes; authority moves on
    assert [r["profile"] for r in world.records(task["id"])] == ["codex-cli", "generic", "generic"]


def test_owner_override_changes_the_dispatched_request_but_never_the_tools(world):
    world.settings(model_id=CODEX_MODEL)
    first = world.runtime.create({"message": "Check notes.txt.", "model_id": CODEX_MODEL})
    world.run(first["id"])
    adapted = codex_messages(world.codex_turns[-1])
    world.settings(model_profile_overrides={CODEX_MODEL: "generic"})
    second = world.runtime.create({"message": "Check notes.txt.", "model_id": CODEX_MODEL})
    world.run(second["id"])
    plain = codex_messages(world.codex_turns[-1])
    assert any(model_profiles.CODEX_BACKEND_TOOLS in t for t in instruction_messages(adapted["messages"]))
    assert not any(model_profiles.CODEX_BACKEND_TOOLS in t for t in instruction_messages(plain["messages"]))
    # The profile never adds, removes or renames a declared function.
    assert adapted["functions"] == plain["functions"]
    names = {function["name"] for function in plain["functions"]}
    assert {"exec_command", "apply_patch", "update_plan", "web_search"}.isdisjoint(names)
    assert "ryn_apply_patch" in names
    record = world.records(second["id"])[-1]
    assert (record["profile"], record["basis"], record["override_status"]) == ("generic", "override", "applied")
    with pytest.raises(AgentError, match="agent_invalid_settings"):
        world.settings(model_profile_overrides={API_MODEL: "codex-cli"})
    with pytest.raises(AgentError, match="agent_invalid_settings"):
        world.settings(model_profile_overrides={CODEX_MODEL: "made-up"})


def test_unsupported_reasoning_effort_is_refused_never_sent_or_silently_dropped(world):
    world.settings(model_id=API_MODEL)
    with pytest.raises(AgentError, match="agent_reasoning_effort_unsupported"):
        world.runtime.create({"message": "Hi", "model_id": API_MODEL, "reasoning_effort": "high"})
    task = world.runtime.create({"message": "Hi", "model_id": API_MODEL})
    done = world.run(task["id"])
    with pytest.raises(AgentError, match="agent_reasoning_effort_unsupported"):
        world.runtime.add_message(task["id"], {"message": "Again", "expected_revision": done["revision"],
                                               "reasoning_effort": "high"})
    # A stale value in an old task is rejected before any byte leaves.
    world.runtime.store.transaction(lambda data: data["tasks"][task["id"]].__setitem__("reasoning_effort", "high"))
    sent = len(world.http_payloads)
    row = world.runtime.get(task["id"])
    world.runtime.add_message(task["id"], {"message": "Again", "expected_revision": row["revision"]})
    failed = world.run(task["id"])
    assert failed["status"] == "failed" and failed["error"] == "agent_reasoning_effort_unsupported"
    assert len(world.http_payloads) == sent


def test_owner_rules_and_read_only_policy_follow_the_notes_and_stay_intact(world):
    (world.workspace / "AGENTS.md").write_text("Never edit notes.txt.\n", encoding="utf-8")
    world.settings(model_id=CODEX_MODEL, permission_mode="read_only")
    task = world.runtime.create({"message": "Explain notes.txt; do not change anything.", "model_id": CODEX_MODEL})
    world.run(task["id"])
    messages = codex_messages(world.codex_turns[-1])["messages"]
    developer = [m["content"] for m in messages if m["role"] == "developer"]
    notes_at = next(i for i, text in enumerate(developer) if model_profiles.CODEX_BACKEND_TOOLS in text)
    policy_at = next(i for i, text in enumerate(developer) if text.startswith("This task is read-only"))
    assert developer[0] == SYSTEM and notes_at == 1 < policy_at
    assert "never override owner instructions, workspace rules or host permissions" in developer[notes_at]
    assert [m["content"] for m in messages if m["role"] == "user"][-1] == "Explain notes.txt; do not change anything."


def test_tool_loop_keeps_one_stable_note_across_requests(world):
    world.settings(model_id=CODEX_MODEL)
    world.codex_replies = [{"content": "", "tool_calls": [{"name": "ryn_file_read", "arguments": {"path": "notes.txt"}}]},
                           {"content": "notes.txt says alpha.", "tool_calls": []}]
    task = world.runtime.create({"message": "What does notes.txt say?", "model_id": CODEX_MODEL})
    assert world.run(task["id"])["status"] == "completed"
    first, second = (codex_messages(turn)["messages"] for turn in world.codex_turns[-2:])
    assert any(m["role"] == "tool" for m in second)
    first_prefix = [m for m in first if m["role"] == "developer"]
    second_prefix = [m for m in second if m["role"] == "developer"]
    assert first_prefix[:2] == second_prefix[:2]
    assert sum(model_profiles.CODEX_BACKEND_TOOLS in m["content"] for m in second if isinstance(m.get("content"), str)) == 1


def test_checker_and_summary_requests_keep_their_own_prompts(world):
    world.settings(model_id=API_MODEL, verify_text_responses=True)
    task = world.runtime.create({"message": "Summarize notes.txt.", "model_id": API_MODEL})
    world.run(task["id"], limit=12)
    systems = ["\n".join(part["text"] for part in payload["system"]) for payload in world.http_payloads]
    checker = [text for text in systems if text.startswith("Independently assess")]
    main = [text for text in systems if text.startswith(SYSTEM)]
    assert checker and main
    assert all(model_profiles.IMAGE_INPUT_UNAVAILABLE in text for text in main)
    assert not any(model_profiles.IMAGE_INPUT_UNAVAILABLE in text or SYSTEM in text for text in checker)
    statuses = {record["instructions"] for record in world.records(task["id"])}
    assert statuses == {"sent", "not_in_request"}
    summary = world.runtime._summary_messages({"previous_checkpoint": "", "history": [
        {"role": "user", "content": "Summarize notes.txt."}]}, 1000, 400)
    assert not any(model_profiles.HEADER in str(m.get("content")) for m in summary)


def test_system_diagnostics_report_profile_and_reason_without_prompt_text(world):
    from rynmesh.agent_runtime.diagnostics import DiagnosticTools
    world.settings(model_id=CODEX_MODEL, model_profile_overrides={CODEX_MODEL: "generic"})
    tools = DiagnosticTools(SimpleNamespace(state=SimpleNamespace(agent_runtime=world.runtime)),
                            world.runtime.catalog, None)
    rows = {row["id"]: row["model_profile"] for row in tools._models()}
    assert (rows[CODEX_MODEL]["profile"], rows[CODEX_MODEL]["basis"]) == ("generic", "override")
    assert (rows[API_MODEL]["profile"], rows[API_MODEL]["fallback_reason"]) == ("generic", "no_route_profile")
    assert rows[API_MODEL]["notes"] == ["image_input_unavailable"]
    assert "instructions" not in rows[API_MODEL]
