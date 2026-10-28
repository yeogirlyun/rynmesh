"""Stage 4 image route capability, Codex CLI image carrier preservation, and verified MCP screenshot materialization tests."""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import time
import uuid
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from PIL import Image

from rynmesh.agent_runtime import image_capabilities
from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore, AgentToolRejected
from rynmesh.llm_package import codex_session
from rynmesh.llm_package.cli_images import image_inputs


def _make_png_bytes(color=(32, 160, 224), size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", size, color=color)
    img.save(buf, format="PNG")
    return buf.getvalue()


def _assistant(content="", name=None, args=None, call_id=None):
    message = {"role": "assistant", "content": content}
    if name:
        message["tool_calls"] = [{
            "id": call_id or "call_" + uuid.uuid4().hex[:12],
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args or {})},
        }]
    return {
        "message": message,
        "usage": {"input_tokens": 24, "output_tokens": 12, "total_tokens": 36},
    }


class CapturingModelBridge:
    supports_image_input = True
    supports_destination_binding = True
    supports_output_token_limit = True
    supports_request_id = True

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, model_id, messages, tool_specs, cancelled, on_event, **options):
        if callable(cancelled) and cancelled():
            raise AgentToolRejected("agent_cancelled")
        snapshot = copy.deepcopy(messages)
        self.calls.append({
            "model_id": model_id,
            "messages": snapshot,
            "tool_specs": copy.deepcopy(tool_specs),
            "options": dict(options),
        })
        if not self.responses:
            return _assistant("Synthetic observation from capturing model.")
        item = self.responses.pop(0)
        return item(model_id, snapshot, tool_specs, options) if callable(item) else item


class StaticCatalog:
    def __init__(self, models):
        self._models = [copy.deepcopy(m) for m in models]

    def list_models(self, *, selected_model_id=None):
        out = []
        for row in self._models:
            enriched = copy.deepcopy(row)
            enriched.update(image_capabilities.catalog_image_fields(enriched))
            out.append(enriched)
        return out


class LocalToolBridge:
    """Connects LocalTools and LOCAL_SPECS to AgentRuntime's tool interface."""

    def __init__(self, store: AgentStore):
        self.store = store
        self.local = LocalTools(store, lambda: OperationLedger(store.home))

    def specs(self):
        out = []
        for name in ("ryn_image_inspect", "ryn_local_environment", "ryn_file_read"):
            desc, _effect, _risk, schema = SPECS[name]
            out.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": copy.deepcopy(schema),
                },
            })
        return out

    def metadata(self, name: str):
        desc, effect, risk, _schema = SPECS[name]
        return {
            "effect": effect,
            "risk": risk,
            "title": desc.split(".")[0],
            "scope_context_supported": True,
            "supports_preview": False,
            "preview_precondition": False,
        }

    def preview(self, name: str, arguments: dict, context: dict | None = None):
        return self.local.preview(name, arguments, context or {})

    def prepare_scope(self, selection, *, owner_id, consumer_node_id, model_destination):
        return {"resource_grants": [], "egress_grants": []}

    def validate_context(self, context: dict, *, cheap_only: bool = False):
        return None

    def execute(self, name: str, arguments: dict, *, operation_id: str, cancelled, context: dict | None = None, expected_preview=None):
        return self.local.execute(
            name,
            arguments,
            operation_id=operation_id,
            context=dict(context or {}),
            expected_preview=expected_preview,
            cancelled=cancelled,
        )

    def close(self):
        self.local.processes.close()


def _setup_runtime_with_model(tmp_path: Path, model_row: dict, *responses, max_context_chars: int | None = None):
    base = tmp_path / ("env_" + uuid.uuid4().hex[:8])
    workspace = base / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    store = AgentStore(base / "agent", X25519PrivateKey.generate())
    bridge = CapturingModelBridge(*responses)
    tools = LocalToolBridge(store)
    catalog = StaticCatalog([model_row])
    runtime = AgentRuntime(store, bridge, tools, catalog, owner_id="owner-local", consumer_node_id="node-local")
    settings = {
        "enabled": True,
        "model_id": model_row["id"],
        "workspace_root": str(workspace),
        "permission_mode": "workspace",
    }
    if max_context_chars is not None:
        settings["max_context_chars"] = max_context_chars
    runtime.update_settings(settings)
    return runtime, bridge, tools, store, workspace


def _codex_model_row(service_id="svc-codex", cli_model="gpt-5.3-codex", **extra):
    from rynmesh.llm_package import peer_binding

    binding = {
        "version": peer_binding.VERSION,
        "service_id": service_id,
        "model_generation": "11111111111111111111111111111111",
        "rule_revision": "22222222222222222222222222222222",
    }
    rev = peer_binding.local_revision(binding, "node-local")
    row = {
        "id": f"local/{service_id}/model/{cli_model}",
        "name": f"Codex ({cli_model})",
        "source": "local_service",
        "service_id": service_id,
        "peer_id": "node-local",
        "runtime": "external",
        "adapter": "codex_cli",
        "cli_model": cli_model,
        "model_id": cli_model,
        "available": True,
        "agent_supported": True,
        "context_window": 100000,
        "max_output_tokens": 2048,
        "destination_binding_supported": True,
        "destination_revision": rev,
        "local_model_binding": binding,
    }
    row.update(extra)
    return row


def _antigravity_model_row(service_id="svc-agy", cli_model="gemini-2.5-pro", **extra):
    from rynmesh.llm_package import peer_binding

    binding = {
        "version": peer_binding.VERSION,
        "service_id": service_id,
        "model_generation": "11111111111111111111111111111111",
        "rule_revision": "22222222222222222222222222222222",
    }
    rev = peer_binding.local_revision(binding, "node-local")
    row = {
        "id": f"local/{service_id}/model/{cli_model}",
        "name": f"Antigravity ({cli_model})",
        "source": "local_service",
        "service_id": service_id,
        "peer_id": "node-local",
        "runtime": "external",
        "adapter": "antigravity_cli",
        "cli_model": cli_model,
        "model_id": cli_model,
        "available": True,
        "agent_supported": True,
        "context_window": 100000,
        "max_output_tokens": 2048,
        "destination_binding_supported": True,
        "destination_revision": rev,
        "local_model_binding": binding,
    }
    row.update(extra)
    return row


def _api_model_row(model_id="local/api-src-1", **extra):
    row = {
        "id": model_id,
        "name": "Local API Model",
        "source": "local_api",
        "adapter": "openai_compatible",
        "source_id": "src-1",
        "peer_id": "node-local",
        "available": True,
        "agent_supported": True,
        "context_window": 100000,
        "max_output_tokens": 2048,
        "destination_binding_supported": True,
        "destination_revision": "rev-api-1",
    }
    row.update(extra)
    return row


class _FixtureCodexRPC(SimpleNamespace):
    def call(self, method, params):
        assert method == "turn/start" and params["threadId"] == "fixture-thread"
        return {"turn": {"id": "fixture-turn"}}

    def receive(self):
        raise AssertionError("Authored event fixture exhausted")


def _completed_codex_events(text="Authored answer"):
    return [
        {
            "method": "item/completed",
            "params": {
                "threadId": "fixture-thread",
                "turnId": "fixture-turn",
                "item": {"type": "agentMessage", "text": text},
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "fixture-thread",
                "turn": {"id": "fixture-turn", "status": "completed"},
            },
        },
    ]


def test_central_route_and_model_image_capability_matrix():
    # 1. Local API with unverified model metadata -> carrier available, model unknown, route allowed without claiming verified pixel recognition.
    api_unknown = image_capabilities.evaluate_image_capability(_api_model_row())
    assert api_unknown["image_transport"] == "api_multimodal_content"
    assert api_unknown["image_carrier_available"] is True
    assert api_unknown["model_image_support"] == "unknown"
    assert api_unknown["image_support"] == "unknown"
    assert api_unknown["supports_image_input"] is True
    assert api_unknown["pixel_recognition_verified"] is False

    # 2. Local API with explicit positive model metadata -> model_image_support == "supported".
    api_positive = image_capabilities.evaluate_image_capability(
        _api_model_row(input_modalities=["text", "image"])
    )
    assert api_positive["image_carrier_available"] is True
    assert api_positive["model_image_support"] == "supported"
    assert api_positive["image_support"] == "supported"
    assert api_positive["supports_image_input"] is True
    assert api_positive["pixel_recognition_verified"] is False

    # 3. Local API with explicit negative model metadata in all supported forms -> supports_image_input is False.
    for negative_kwargs in (
        {"supports_image_input": False},
        {"input_modalities": ["text"]},
        {"capabilities": {"vision": False}},
        {"image_input": "unsupported"},
        {"capabilities": ["api-text-only"]},
        {"model_settings": {"overrides": {"vision": False}}},
    ):
        cap = image_capabilities.evaluate_image_capability(_api_model_row(**negative_kwargs))
        assert cap["image_carrier_available"] is True
        assert cap["model_image_support"] == "unsupported", f"Failed for {negative_kwargs}: {cap}"
        assert cap["image_support"] == "unsupported"
        assert cap["supports_image_input"] is False

    # 4. Codex CLI route -> carrier available via turn/start image items; pixel recognition not verified.
    codex_cap = image_capabilities.evaluate_image_capability(_codex_model_row())
    assert codex_cap["image_transport"] == "codex_cli_turn_input"
    assert codex_cap["image_carrier_available"] is True
    assert codex_cap["model_image_support"] == "unknown"
    assert codex_cap["supports_image_input"] is True
    assert codex_cap["pixel_recognition_verified"] is False

    # Codex CLI with explicit model negative -> rejected.
    for neg in ({"supports_image_input": False}, {"capabilities": {"vision": False}}, {"image_input": "unsupported"}):
        codex_neg = image_capabilities.evaluate_image_capability(_codex_model_row(**neg))
        assert codex_neg["image_carrier_available"] is True
        assert codex_neg["model_image_support"] == "unsupported"
        assert codex_neg["supports_image_input"] is False

    # 5. Antigravity CLI -> stream-json is text-only; carrier unsupported even if positive model metadata is present.
    agy_cap = image_capabilities.evaluate_image_capability(
        _antigravity_model_row(supports_image_input=True, input_modalities=["text", "image"])
    )
    assert agy_cap["image_transport"] == "none"
    assert agy_cap["image_carrier_available"] is False
    assert agy_cap["supports_image_input"] is False
    assert agy_cap["image_capability_reason"] == "antigravity_cli_stream_json_text_only"

    # 6. Peer and other unverified CLI routes remain unsupported.
    peer_cap = image_capabilities.evaluate_image_capability({
        "id": "peer/node-2/svc-1",
        "source": "peer",
        "adapter": "openai_compatible",
        "agent_supported": True,
    })
    assert peer_cap["supports_image_input"] is False
    assert peer_cap["image_carrier_available"] is False
    assert peer_cap["image_capability_reason"] == "peer_image_route_unverified"


def test_model_catalog_exposes_safe_image_capability_fields(tmp_path):
    records = {
        "src-1": {
            "id": "src-1",
            "name": "OpenAI Compatible",
            "protocol": "openai_compatible",
            "enabled": True,
            "status": "ready",
            "destination_revision": "rev-src-1",
            "models": ["gpt-4.1", "text-only-model"],
            "tool_support": {"gpt-4.1": "supported", "text-only-model": "supported"},
            "model_metadata": {
                "gpt-4.1": {"input_modalities": ["text", "image"]},
                "text-only-model": {"capabilities": {"vision": False}},
            },
            "model_settings": {},
        }
    }
    source_store = SimpleNamespace(
        read=lambda: copy.deepcopy(records),
        model_service_id=lambda record, model: f"api-{record['id']}-{model}",
        shared_models=lambda record: [],
        path=tmp_path / "sources.json",
    )
    discovered = [
        {
            "peer_id": "peer-remote",
            "node_name": "Remote Peer",
            "online": True,
            "ready": True,
            "chat_protocol": "rynmesh.chat.v1",
            "service": {
                "package_id": "svc-codex-peer",
                "model_alias": "Remote Codex",
                "adapter": "codex_cli",
                "runtime": "external",
                "capabilities": ["client-tools"],
            },
            "cli_models": [
                {"id": "gpt-5.3-codex", "name": "GPT-5.3 Codex"},
            ],
        },
        {
            "peer_id": "node-local",
            "node_name": "Local Node",
            "online": True,
            "ready": True,
            "chat_protocol": "rynmesh.chat.v1",
            "service": {
                "package_id": "svc-agy",
                "model_alias": "Local Antigravity",
                "adapter": "antigravity_cli",
                "runtime": "external",
                "capabilities": ["client-tools"],
            },
        },
    ]
    services = {
        "source_store": source_store,
        "discover": lambda _net: copy.deepcopy(discovered),
        "provider_for": lambda _svc_id: None,
    }
    store = SimpleNamespace(
        peer_id="node-local",
        llm_tool_capabilities=SimpleNamespace(support=lambda _m, _id: "supported"),
        personal_space=SimpleNamespace(allows_ai=lambda _p: False),
    )
    catalog = ModelCatalog(services, store)
    by_id = {m["id"]: m for m in catalog.list_models(selected_model_id="local/api-src-1-gpt-4.1")}

    api_vision = by_id["local/api-src-1-gpt-4.1"]
    assert api_vision["supports_image_input"] is True
    assert api_vision["image_carrier_available"] is True
    assert api_vision["image_transport"] == "api_multimodal_content"
    assert api_vision["model_image_support"] == "supported"
    assert api_vision["pixel_recognition_verified"] is False

    api_text = by_id["local/api-src-1-text-only-model"]
    assert api_text["supports_image_input"] is False
    assert api_text["image_carrier_available"] is True
    assert api_text["model_image_support"] == "unsupported"
    assert api_text["image_support"] == "unsupported"

    agy = by_id["local/svc-agy"]
    assert agy["supports_image_input"] is False
    assert agy["image_carrier_available"] is False
    assert agy["image_transport"] == "none"
    assert agy["image_capability_reason"] == "antigravity_cli_stream_json_text_only"

    peer_rows = [m for m in by_id.values() if m["source"] == "peer"]
    assert peer_rows and all(m["supports_image_input"] is False for m in peer_rows)


def test_codex_cli_image_inspect_preserves_turn_start_image_items_and_keeps_store_base64_free(tmp_path, monkeypatch):
    codex_row = _codex_model_row()
    runtime, bridge, tools, store, workspace = _setup_runtime_with_model(
        tmp_path,
        codex_row,
        _assistant(name="ryn_image_inspect", args={"path": "canary.png", "question": "What color is the box?"}, call_id="call_img_1"),
        _assistant("Observed a blue-teal rectangle from the submitted image."),
        _assistant("Observed a blue-teal rectangle from the submitted image."),
    )
    try:
        png_bytes = _make_png_bytes(color=(16, 128, 200), size=(80, 60))
        (workspace / "canary.png").write_bytes(png_bytes)

        task = runtime.create({"message": "Inspect canary.png with ryn_image_inspect."})
        for _ in range(6):
            runtime.run_once()
            if runtime.get(task["id"])["status"] in {"completed", "failed"}:
                break

        current = runtime.get(task["id"])
        assert current["status"] == "completed", current.get("error")

        # Call 0 is the main loop turn that emitted tool_call ryn_image_inspect.
        # Call 1 is the nested image_inspection stage invoked by ryn_image_inspect (exactly ONE nested call).
        assert len(bridge.calls) == 3
        inspection_call = bridge.calls[1]
        inspection_messages = inspection_call["messages"]
        nested_req_id = inspection_call["options"].get("request_id")
        assert isinstance(nested_req_id, str) and nested_req_id.startswith("req_")
        assert nested_req_id != bridge.calls[0]["options"].get("request_id")

        # Verify cli_images.image_inputs returns [{'type': 'image', 'url': 'data:image/png;base64,...'}]
        extracted_items = image_inputs(inspection_messages)
        assert len(extracted_items) == 1
        assert extracted_items[0]["type"] == "image"
        assert extracted_items[0]["url"].startswith("data:image/png;base64,")

        # Verify actual codex_session._turn preserves image items in turn/start.input on initial turn
        initial_inputs = []
        rpc_initial = _FixtureCodexRPC(pending=deque(_completed_codex_events()))
        rpc_initial.call = lambda method, params: (
            initial_inputs.extend(params["input"]) or {"turn": {"id": "fixture-turn"}}
        )
        answer, _, _ = codex_session._turn(
            rpc_initial,
            "fixture-thread",
            {"messages": inspection_messages},
            "authored prompt",
            deadline=time.monotonic() + 1,
        )
        assert answer == "Authored answer"
        assert initial_inputs == [
            {"type": "text", "text": "authored prompt", "text_elements": []},
            *extracted_items,
        ]

        # And on resumed turns using preparsed _image_inputs without re-reading message bodies
        resumed_inputs = []
        rpc_resumed = _FixtureCodexRPC(pending=deque(_completed_codex_events("Resumed answer")))
        rpc_resumed.call = lambda method, params: (
            resumed_inputs.extend(params["input"]) or {"turn": {"id": "fixture-turn"}}
        )
        monkeypatch.setattr(
            "rynmesh.llm_package.cli_images.image_inputs",
            lambda _: pytest.fail("preparsed images must not be decoded again on resumed turn"),
        )
        resumed_answer, _, _ = codex_session._turn(
            rpc_resumed,
            "fixture-thread",
            {"_image_inputs": extracted_items},
            "resumed prompt",
            deadline=time.monotonic() + 1,
            resumed=True,
        )
        assert resumed_answer == "Resumed answer"
        assert resumed_inputs == [
            {"type": "text", "text": "resumed prompt", "text_elements": []},
            *extracted_items,
        ]

        # Verify tool result metadata and request linkage
        tool_events = [e for e in current["events"] if e["type"] == "tool_result" and e.get("tool") == "ryn_image_inspect"]
        assert len(tool_events) == 1
        result = tool_events[0]["result"]
        assert result["sha256"] == hashlib.sha256(png_bytes).hexdigest()
        assert result["image_submitted"] is True
        assert result["pixel_recognition_verified"] is False
        linkage = result["request_linkage"]
        assert linkage["request_id"] == nested_req_id
        assert linkage["operation_id"] == tool_events[0]["operation_id"]
        assert linkage["image_transport"] == "codex_cli_turn_input"
        assert linkage["image_carrier_available"] is True
        assert linkage["dispatch_status"] == "submitted"
        assert linkage["image_submitted"] is True
        assert linkage["semantic_response_received"] is True
        assert linkage["pixel_recognition_verified"] is False
        assert len(linkage["submitted_images"]) == 1
        assert linkage["submitted_images"][0]["payload_sha256"] == result["payload_sha256"]
        assert linkage["source_artifacts"][0]["path"] == "canary.png"

        # Persistent store must NOT contain base64 image payloads anywhere, and budget records exactly 1 image_inspection call.
        stored_data = store.read()
        assert "data:image/" not in json.dumps(stored_data)
        stored_task = stored_data["tasks"][task["id"]]
        inspection_budget_records = [
            r for r in stored_task["budget"].get("records", [])
            if r.get("kind") == "model" and r.get("stage") == "image_inspection"
        ]
        assert len(inspection_budget_records) == 1
        assert len(stored_task.get("image_request_links") or []) == 1
        assert stored_task["image_request_links"][0]["request_id"] == nested_req_id
        assert stored_task["image_request_links"][0]["binding_current"] is True
    finally:
        tools.close()


def test_inspect_image_uses_model_configured_output_up_to_16384_tokens(tmp_path):
    api_row = _api_model_row(context_window=128000, max_output_tokens=32768)
    runtime, bridge, tools, _store, workspace = _setup_runtime_with_model(
        tmp_path,
        api_row,
        _assistant(name="ryn_image_inspect", args={"path": "canary.png", "question": "Check layout"}, call_id="call_img_cap"),
        _assistant("Detailed layout inspection without length truncation."),
        _assistant("Layout verified."),
    )
    try:
        (workspace / "canary.png").write_bytes(_make_png_bytes(color=(20, 80, 140), size=(80, 60)))
        task = runtime.create({"message": "Inspect canary.png with reasoning model output budget."})
        for _ in range(6):
            runtime.run_once()
            if runtime.get(task["id"])["status"] in {"completed", "failed"}:
                break

        current = runtime.get(task["id"])
        assert current["status"] == "completed", current.get("error")
        assert len(bridge.calls) == 3
        assert bridge.calls[1]["options"]["max_output_tokens"] == 16384
    finally:
        tools.close()


def test_inspect_image_never_reinvokes_model_on_type_error(tmp_path):
    api_row = _api_model_row()

    def _raise_type_error(_model_id, _messages, _specs, _options):
        raise TypeError("simulated post-dispatch type failure")

    runtime, bridge, tools, store, workspace = _setup_runtime_with_model(
        tmp_path,
        api_row,
        _raise_type_error,
    )
    try:
        (workspace / "canary.png").write_bytes(_make_png_bytes())
        task = runtime.create({"message": "Single invocation check"})
        store.transaction(lambda data: data["tasks"][task["id"]].update(status="running"))
        selected = store.read()["tasks"][task["id"]]
        selected["_run_control_revision"] = selected["control_revision"]
        context = runtime._tool_context(selected, "op-single-call")

        with pytest.raises(TypeError, match="simulated post-dispatch type failure"):
            tools.execute(
                "ryn_image_inspect",
                {"path": "canary.png"},
                operation_id="op-single-call",
                cancelled=lambda: False,
                context=context,
            )

        # Must have invoked the model and charged budget ONCE, never re-invoked on TypeError.
        assert len(bridge.calls) == 1
        stored_task = store.read()["tasks"][task["id"]]
        model_records = [r for r in stored_task["budget"].get("records", []) if r.get("kind") == "model"]
        assert len(model_records) == 1
        links = stored_task.get("image_request_links") or []
        assert len(links) == 1
        assert links[0]["task_id"] == task["id"]
        assert links[0]["operation_id"] == "op-single-call"
        assert links[0]["request_id"] == bridge.calls[0]["options"].get("request_id")
        assert links[0]["dispatch_status"] == "attempted"
        assert links[0]["image_submitted"] is None
        assert links[0]["image_dispatch_attempted"] is True
        assert links[0]["semantic_response_received"] is False
    finally:
        tools.close()


def test_antigravity_cli_and_explicit_negative_models_refuse_both_tool_and_existing_canonical_images(tmp_path):
    # 1. Antigravity CLI route refuses ryn_image_inspect AND refuses existing canonical user image_url in _complete.
    agy_row = _antigravity_model_row()
    runtime, bridge, tools, store, workspace = _setup_runtime_with_model(
        tmp_path,
        agy_row,
        _assistant(name="ryn_image_inspect", args={"path": "sample.png"}, call_id="call_agy_img"),
        _assistant("Cannot inspect pixels on Antigravity CLI because stream-json is text-only."),
    )
    try:
        raw_png = _make_png_bytes()
        (workspace / "sample.png").write_bytes(raw_png)
        task = runtime.create({"message": "Try inspecting sample.png."})
        for _ in range(6):
            runtime.run_once()
            if runtime.get(task["id"])["status"] in {"completed", "failed"}:
                break

        current = runtime.get(task["id"])
        assert current["status"] == "completed", current.get("error")
        for call_record in bridge.calls:
            assert image_inputs(call_record["messages"]) == []
        rejections = [e for e in current["events"] if e["type"] == "tool_result" and e.get("call_id") == "call_agy_img"]
        assert len(rejections) == 1
        err = rejections[0]["result"]
        assert err["error"] == "agent_image_input_unsupported"
        assert "stream-json" in err.get("correction", "")
        assert err["details"]["reason"] == "antigravity_cli_stream_json_text_only"

        # Now test _complete directly with an already-present canonical user image_url message on Antigravity CLI:
        calls_before = len(bridge.calls)
        store.transaction(lambda data: data["tasks"][task["id"]].update(status="running"))
        selected = store.read()["tasks"][task["id"]]
        selected["_run_control_revision"] = selected["control_revision"]
        inline_url = "data:image/png;base64," + base64.b64encode(raw_png).decode("ascii")
        canonical_image_messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Look at this inline image."},
                {"type": "image_url", "image_url": {"url": inline_url}},
            ],
        }]
        with pytest.raises(AgentToolRejected, match="agent_image_input_unsupported"):
            runtime._complete(selected, canonical_image_messages, [], lambda: False, lambda _event: None)
        assert len(bridge.calls) == calls_before
    finally:
        tools.close()

    # 2. Local API model with explicit negative metadata (capabilities={"vision": False}) refuses both tool and canonical image_url.
    neg_api_row = _api_model_row(capabilities={"vision": False})
    runtime2, bridge2, tools2, store2, workspace2 = _setup_runtime_with_model(
        tmp_path,
        neg_api_row,
        _assistant(name="ryn_image_inspect", args={"path": "sample.png"}, call_id="call_neg_api"),
        _assistant("Selected API model is text-only."),
    )
    try:
        (workspace2 / "sample.png").write_bytes(_make_png_bytes())
        task2 = runtime2.create({"message": "Try inspecting sample.png on text-only API model."})
        for _ in range(6):
            runtime2.run_once()
            if runtime2.get(task2["id"])["status"] in {"completed", "failed"}:
                break

        current2 = runtime2.get(task2["id"])
        for call_record in bridge2.calls:
            assert image_inputs(call_record["messages"]) == []
        rejections2 = [e for e in current2["events"] if e["type"] == "tool_result" and e.get("call_id") == "call_neg_api"]
        assert len(rejections2) == 1
        err2 = rejections2[0]["result"]
        assert err2["error"] == "agent_image_input_unsupported"
        assert err2["details"]["reason"] == "route_capability_text_only"
    finally:
        tools2.close()


def test_image_inspect_retains_destination_budget_and_cancellation_guards(tmp_path):
    codex_row = _codex_model_row()
    runtime, bridge, tools, store, workspace = _setup_runtime_with_model(
        tmp_path,
        codex_row,
        _assistant("Image inspected."),
    )
    try:
        (workspace / "pic.png").write_bytes(_make_png_bytes())
        task = runtime.create({"message": "Check guards"})

        def prepare_running(data):
            row = data["tasks"][task["id"]]
            row["status"] = "running"
            row["_run_started"] = 100.0
            row["_run_lifetime_remaining"] = 300.0
            row["_run_control_revision"] = row["control_revision"]
            return row

        selected = store.transaction(prepare_running)
        ctx = runtime._tool_context(selected, operation_id="op-guard")

        # 1. Cancellation guard
        with pytest.raises(AgentToolRejected, match="agent_cancelled"):
            tools.execute(
                "ryn_image_inspect",
                {"path": "pic.png"},
                operation_id="op-cancel",
                cancelled=lambda: True,
                context=ctx,
            )

        # 2. Token budget exhaustion guard
        from rynmesh.agent_runtime import budget

        original_records = []

        def exhaust_tokens(data):
            nonlocal original_records
            row = data["tasks"][task["id"]]
            state = budget.initialize(row)
            original_records = copy.deepcopy(state["records"])
            max_tokens = state["limits"]["max_total_tokens"]
            reservation = budget.reserve(
                row,
                "model",
                "fixture",
                input_bound=max_tokens,
                output_bound=0,
                seconds_bound=0,
            )
            budget.settle(
                row,
                reservation["id"],
                usage={"input_tokens": max_tokens, "output_tokens": 0},
                elapsed_seconds=0.0,
            )

        store.transaction(exhaust_tokens)
        with pytest.raises(AgentError, match="agent_token_budget_exhausted"):
            tools.execute(
                "ryn_image_inspect",
                {"path": "pic.png"},
                operation_id="op-budget",
                cancelled=lambda: False,
                context=ctx,
            )

        # 3. Destination revision mismatch guard
        def tamper_destination(data):
            row = data["tasks"][task["id"]]
            row["budget"]["records"] = copy.deepcopy(original_records)
            row["model_destination_required"] = True
            row["model_destination"]["destination_revision"] = "tampered-rev"
            return copy.deepcopy(row)

        tampered = store.transaction(tamper_destination)
        tampered["_run_started"] = 100.0
        tampered["_run_lifetime_remaining"] = 300.0
        tampered["_run_control_revision"] = tampered["control_revision"]
        ctx_tampered = runtime._tool_context(tampered, operation_id="op-dest")
        with pytest.raises(AgentError, match="agent_model_destination_changed"):
            tools.execute(
                "ryn_image_inspect",
                {"path": "pic.png"},
                operation_id="op-dest",
                cancelled=lambda: False,
                context=ctx_tampered,
            )
        assert bridge.calls == []
    finally:
        tools.close()


def _write_mcp_artifact(
    workspace: Path,
    task_id: str,
    operation_id: str,
    index: int = 0,
    png_bytes: bytes | None = None,
) -> tuple[Path, dict]:
    raw = png_bytes if png_bytes is not None else _make_png_bytes(color=(200, 40, 80), size=(96, 72))
    safe_task = image_capabilities.safe_segment(task_id, "task")
    safe_op = image_capabilities.safe_segment(operation_id, "op")
    rel_dir = Path(".ryn") / "mcp" / safe_task / safe_op
    full_dir = workspace / rel_dir
    full_dir.mkdir(parents=True, exist_ok=True)
    file_path = full_dir / f"image_{index}.png"
    file_path.write_bytes(raw)
    with Image.open(io.BytesIO(raw)) as img:
        width, height = img.size
    artifact = {
        "index": index,
        "path": (rel_dir / f"image_{index}.png").as_posix(),
        "mime_type": "image/png",
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "width": width,
        "height": height,
    }
    return file_path, artifact


def _append_confirmed_mcp_turn(row: dict, call_id: str, op_id: str, artifacts: list[dict]) -> dict:
    epoch = row.get("context_epoch", 0)
    payload = {
        "plugin_id": "desktop",
        "tool_name": "capture",
        "is_error": False,
        "mcp_artifacts": artifacts,
        "content": [{"type": "image", **art} for art in artifacts],
    }
    row["messages"].append({
        "role": "assistant",
        "content": "",
        "context_epoch": epoch,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": "mcp__desktop__capture", "arguments": "{}"},
        }],
    })
    row["messages"].append({
        "role": "tool",
        "tool_call_id": call_id,
        "context_epoch": epoch,
        "content": json.dumps(payload),
    })
    row.setdefault("events", []).append({
        "type": "tool_result",
        "call_id": call_id,
        "operation_id": op_id,
        "context_epoch": epoch,
    })
    row.setdefault("receipts", []).append({
        "id": "rec_" + uuid.uuid4().hex[:8],
        "operation_id": op_id,
        "call_id": call_id,
        "tool": "mcp__desktop__capture",
        "effect": "read",
        "status": "confirmed",
        "goal_revision": row["goal"]["revision"],
        "scope_revision": row["scope_revision"],
        "context_epoch": epoch,
        "result": payload,
    })
    return payload


def test_mcp_screenshot_materializes_into_main_loop_with_preparation_vs_submission_evidence(tmp_path):
    codex_row = _codex_model_row()
    runtime, bridge, tools, store, workspace = _setup_runtime_with_model(
        tmp_path,
        codex_row,
        _assistant("I see the MCP screenshot window."),
    )
    try:
        task = runtime.create({"message": "Observe the latest MCP desktop screenshot."})
        task_id = task["id"]
        op_id = "op_mcp_shot_1"
        _, artifact = _write_mcp_artifact(workspace, task_id, op_id, index=0)

        store.transaction(lambda data: _append_confirmed_mcp_turn(data["tasks"][task_id], "call_mcp_1", op_id, [artifact]))

        # 1. Pure materialization before model call MUST report image_prepared=True, image_submitted=False.
        snapshot_row = store.read()["tasks"][task_id]
        projected, report = image_capabilities.materialize_operation_images(
            snapshot_row, snapshot_row["messages"], model_row=codex_row, model_client=bridge
        )
        assert report["status"] == "materialized"
        assert report["image_prepared"] is True
        assert report["image_submitted"] is False
        assert len(report["materialized_artifacts"]) == 1
        assert len(image_inputs(projected)) == 1

        # 2. Running the task dispatches to the model and records image_submitted=True after response.
        runtime.run_once()
        current = runtime.get(task_id)
        assert current["status"] == "completed", current.get("error")
        assert len(bridge.calls) == 1

        outgoing_messages = bridge.calls[0]["messages"]
        items = image_inputs(outgoing_messages)
        assert len(items) == 1
        assert items[0]["type"] == "image"
        assert items[0]["url"].startswith("data:image/png;base64,")

        roles = [m["role"] for m in outgoing_messages]
        tool_idx = roles.index("tool")
        assert outgoing_messages[tool_idx + 1]["role"] == "user"
        assert any(
            isinstance(part, dict) and part.get("type") == "image_url"
            for part in outgoing_messages[tool_idx + 1]["content"]
        )

        # Persistent store must NOT contain base64 image URLs.
        persisted_data = store.read()
        assert "data:image/" not in json.dumps(persisted_data)
        stored_row = persisted_data["tasks"][task_id]
        links = stored_row.get("image_request_links") or []
        assert len(links) == 1
        assert links[0]["stage"] == "main_loop_mcp_observation"
        assert links[0]["observation_status"] == "materialized"
        assert links[0]["dispatch_status"] == "submitted"
        assert links[0]["image_prepared"] is True
        assert links[0]["image_submitted"] is True
        assert links[0]["semantic_response_received"] is True
        assert links[0]["pixel_recognition_verified"] is False
        assert links[0]["source_artifacts"][0]["sha256"] == artifact["sha256"]
        assert links[0]["request_id"] == bridge.calls[0]["options"].get("request_id")
    finally:
        tools.close()


def test_mcp_screenshot_rejected_by_context_budget_never_claims_submitted(tmp_path):
    api_row = _api_model_row()
    # Set a small context limit (4096 bytes) and a noisy 128x128 image whose base64 exceeds 4096 bytes.
    runtime, bridge, tools, store, workspace = _setup_runtime_with_model(
        tmp_path,
        api_row,
        max_context_chars=4096,
    )
    try:
        task = runtime.create({"message": "Context budget check"})
        task_id = task["id"]
        op_id = "op_budget_mcp"
        noisy_img = Image.frombytes("RGB", (128, 128), os.urandom(128 * 128 * 3))
        buf = io.BytesIO()
        noisy_img.save(buf, format="PNG")
        _, artifact = _write_mcp_artifact(workspace, task_id, op_id, index=0, png_bytes=buf.getvalue())

        def prepare(data):
            row = data["tasks"][task_id]
            row["status"] = "running"
            _append_confirmed_mcp_turn(row, "call_budget_mcp", op_id, [artifact])

        store.transaction(prepare)
        selected = store.read()["tasks"][task_id]
        selected["_run_control_revision"] = selected["control_revision"]

        _, report = image_capabilities.materialize_operation_images(
            selected, selected["messages"], model_row=api_row, model_client=bridge
        )
        assert report["image_prepared"] is True
        assert report["image_submitted"] is False

        with pytest.raises(AgentError, match="agent_context_limit"):
            runtime._complete(selected, selected["messages"], [], lambda: False, lambda _event: None)

        assert bridge.calls == []
        stored_row = store.read()["tasks"][task_id]
        assert (stored_row.get("image_request_links") or []) == []
    finally:
        tools.close()


def test_mcp_screenshot_on_unsupported_route_surfaces_unavailable_state_without_dropping_silently(tmp_path):
    agy_row = _antigravity_model_row()
    runtime, bridge, tools, store, workspace = _setup_runtime_with_model(
        tmp_path,
        agy_row,
        _assistant("Image input is unavailable on this route."),
    )
    try:
        task = runtime.create({"message": "Check MCP screenshot on Antigravity route."})
        task_id = task["id"]
        op_id = "op_mcp_agy"
        _, artifact = _write_mcp_artifact(workspace, task_id, op_id, index=0)

        store.transaction(lambda data: _append_confirmed_mcp_turn(data["tasks"][task_id], "call_mcp_agy", op_id, [artifact]))
        runtime.run_once()

        assert len(bridge.calls) == 1
        outgoing = bridge.calls[0]["messages"]
        assert image_inputs(outgoing) == []
        assert any(
            m["role"] == "user"
            and isinstance(m.get("content"), str)
            and "unavailable_on_selected_route" in m["content"]
            for m in outgoing
        )
        stored_row = store.read()["tasks"][task_id]
        links = stored_row.get("image_request_links") or []
        assert len(links) == 1
        assert links[0]["observation_status"] == "route_image_unsupported"
        assert links[0]["dispatch_status"] == "not_dispatched"
        assert links[0]["image_prepared"] is False
        assert links[0]["image_submitted"] is False
    finally:
        tools.close()


def test_mcp_artifact_verification_rejects_tamper_crosstask_outside_symlink_and_bounded_read_race(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id = "task_123"
    op_id = "op_456"
    file_path, valid_artifact = _write_mcp_artifact(workspace, task_id, op_id, index=0)

    # 1. Valid artifact passes verification and returns data_url.
    verified = image_capabilities.verify_and_load_mcp_image_artifact(
        valid_artifact, workspace_root=workspace, task_id=task_id, operation_id=op_id
    )
    assert verified["data_url"].startswith("data:image/png;base64,")
    assert verified["sha256"] == valid_artifact["sha256"]

    # 2a. Same-length byte tampering -> rejected with artifact_sha256_mismatch.
    original_bytes = file_path.read_bytes()
    tampered_same_len = bytearray(original_bytes)
    tampered_same_len[-1] ^= 0xFF
    file_path.write_bytes(bytes(tampered_same_len))
    with pytest.raises(ValueError, match="artifact_sha256_mismatch"):
        image_capabilities.verify_and_load_mcp_image_artifact(
            valid_artifact, workspace_root=workspace, task_id=task_id, operation_id=op_id
        )

    # 2b. Different-length tampering -> rejected with artifact_stat_mismatch.
    file_path.write_bytes(original_bytes + b"extra")
    with pytest.raises(ValueError, match="artifact_stat_mismatch"):
        image_capabilities.verify_and_load_mcp_image_artifact(
            valid_artifact, workspace_root=workspace, task_id=task_id, operation_id=op_id
        )

    # Restore valid file
    file_path.write_bytes(original_bytes)

    # 3. Controlled stat-to-open/read race: swap file to 6 MiB right as os.open is called;
    # verify os.read NEVER reads 6 MiB (reads at most expected_bytes + 1).
    real_os_open = os.open
    real_os_read = os.read
    bytes_read_during_race: list[int] = []

    def _open_with_6mb_swap(path_arg, flags, *args, **kwargs):
        if Path(path_arg) == file_path:
            file_path.write_bytes(b"x" * (6 * 1024 * 1024))
        return real_os_open(path_arg, flags, *args, **kwargs)

    def _tracking_os_read(fd, n):
        chunk = real_os_read(fd, n)
        bytes_read_during_race.append(len(chunk))
        return chunk

    with patch("os.open", side_effect=_open_with_6mb_swap), patch("os.read", side_effect=_tracking_os_read):
        with pytest.raises(ValueError, match="artifact_(?:file_too_large|size_mismatch|handle_identity_changed)"):
            image_capabilities.verify_and_load_mcp_image_artifact(
                valid_artifact, workspace_root=workspace, task_id=task_id, operation_id=op_id
            )
    assert sum(bytes_read_during_race) <= valid_artifact["bytes"] + 1
    assert sum(bytes_read_during_race) < 6 * 1024 * 1024

    # Restore valid file
    file_path.write_bytes(original_bytes)

    # 4. Cross-task artifact path -> rejected.
    _, other_task_artifact = _write_mcp_artifact(workspace, "other_task", op_id, index=0)
    with pytest.raises(ValueError, match="artifact_path_outside_task_operation_scope"):
        image_capabilities.verify_and_load_mcp_image_artifact(
            other_task_artifact, workspace_root=workspace, task_id=task_id, operation_id=op_id
        )

    # 5. Cross-operation artifact path -> rejected.
    _, other_op_artifact = _write_mcp_artifact(workspace, task_id, "other_op", index=0)
    with pytest.raises(ValueError, match="artifact_path_outside_task_operation_scope"):
        image_capabilities.verify_and_load_mcp_image_artifact(
            other_op_artifact, workspace_root=workspace, task_id=task_id, operation_id=op_id
        )

    # 6. Path traversal / outside workspace -> rejected.
    outside_file = tmp_path / "outside.png"
    outside_bytes = _make_png_bytes()
    outside_file.write_bytes(outside_bytes)
    traversal_artifact = {
        **valid_artifact,
        "path": "../outside.png",
        "bytes": len(outside_bytes),
        "sha256": hashlib.sha256(outside_bytes).hexdigest(),
    }
    with pytest.raises(ValueError, match="artifact_path_outside_task_operation_scope"):
        image_capabilities.verify_and_load_mcp_image_artifact(
            traversal_artifact, workspace_root=workspace, task_id=task_id, operation_id=op_id
        )

    # 7. Opened-handle containment check: if the opened file handle resolves outside expected_dir, reject immediately.
    with patch(
        "rynmesh.agent_runtime.image_capabilities._resolve_opened_fd_path",
        return_value=outside_file.resolve(),
    ):
        with pytest.raises(ValueError, match="artifact_resolved_outside_scope"):
            image_capabilities.verify_and_load_mcp_image_artifact(
                valid_artifact, workspace_root=workspace, task_id=task_id, operation_id=op_id
            )

    # 8. Bounded count & stale history exclusion in materialize_operation_images:
    _, art0 = _write_mcp_artifact(workspace, task_id, op_id, index=0, png_bytes=_make_png_bytes((10, 20, 30)))
    _, art1 = _write_mcp_artifact(workspace, task_id, op_id, index=1, png_bytes=_make_png_bytes((40, 50, 60)))
    _, art2 = _write_mcp_artifact(workspace, task_id, op_id, index=2, png_bytes=_make_png_bytes((70, 80, 90)))
    row = {
        "id": task_id,
        "goal": {"revision": 1},
        "scope_revision": 1,
        "control_revision": 1,
        "context_epoch": 0,
        "local_execution": {"workspace_root": str(workspace)},
        "messages": [{"role": "user", "content": "Capture screen"}],
        "events": [],
        "receipts": [],
    }
    _append_confirmed_mcp_turn(row, "c1", op_id, [art0, art1, art2])

    out_msgs, report = image_capabilities.materialize_operation_images(
        row, row["messages"], model_row=_codex_model_row(), max_images=2
    )
    assert report["status"] == "materialized"
    assert report["image_prepared"] is True
    assert report["image_submitted"] is False
    assert len(report["materialized_artifacts"]) == 2
    assert report["omitted_count"] == 1
    assert len(image_inputs(out_msgs)) == 2

    # Once the assistant replies and a new turn begins without trailing fresh tool results, old screenshots are not re-sent.
    later_messages = [
        *row["messages"],
        {"role": "assistant", "content": "Observed.", "context_epoch": 0},
        {"role": "user", "content": "Now do something text-only."},
    ]
    out_later, report_later = image_capabilities.materialize_operation_images(
        row, later_messages, model_row=_codex_model_row(), max_images=2
    )
    assert report_later["has_candidate_artifacts"] is False
    assert image_inputs(out_later) == []


def test_inspect_image_auto_downscales_oversized_pixel_image_and_retries_transient_invalid_response(tmp_path):
    api_row = _api_model_row()
    attempts = {"count": 0}

    def _transient_then_valid(_model_id, _messages, _specs, _options):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise AgentError("agent_invalid_model_response")
        return {
            "message": {"role": "assistant", "content": "Downscaled high-DPI image inspected successfully."},
            "usage": {"input_tokens": 120, "output_tokens": 18},
            "finish_reason": "stop",
        }

    runtime, bridge, tools, store, workspace = _setup_runtime_with_model(
        tmp_path,
        api_row,
        _transient_then_valid,
        _transient_then_valid,
    )
    try:
        # 5000 x 3600 = 18,000,000 pixels (> 16 MP single-image transport limit, within 64 MP source limit)
        (workspace / "high_dpi_page.png").write_bytes(_make_png_bytes(color=(30, 90, 150), size=(5000, 3600)))
        task = runtime.create({"message": "Inspect oversized high-DPI page"})
        store.transaction(lambda data: data["tasks"][task["id"]].update(status="running"))
        selected = store.read()["tasks"][task["id"]]
        selected["_run_control_revision"] = selected["control_revision"]
        context = runtime._tool_context(selected, "op-high-dpi")

        result = tools.execute(
            "ryn_image_inspect",
            {"path": "high_dpi_page.png", "question": "Check high-DPI render"},
            operation_id="op-high-dpi",
            cancelled=lambda: False,
            context=context,
        )
        assert result["image_submitted"] is True
        assert result["width"] == 5000 and result["height"] == 3600
        assert result["original_dimensions_preserved"] is False
        assert isinstance(result["resized_to"], list) and len(result["resized_to"]) == 2
        assert result["resized_to"][0] * result["resized_to"][1] <= 2_400_000
        assert isinstance(result.get("coordinate_scale"), list)
        assert result["observed"] == "Downscaled high-DPI image inspected successfully."
        assert len(bridge.calls) == 2
    finally:
        tools.close()

