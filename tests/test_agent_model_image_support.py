"""Image input is decided per model, and a shared model behaves like a local one."""
from __future__ import annotations

import base64
import copy
import io
import json
from urllib.error import HTTPError

import pytest
from PIL import Image
from test_agent_image_carrier_profile import ANSWER, PNG_3_46MIB, PNG_SEED_SMALL, _make_bridge, _user_image_messages
from test_agent_runtime import Catalog, Model, advance, assistant, make_runtime

from rynmesh.agent_runtime import image_capabilities as caps
from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.store import AgentError
from rynmesh.llm_package import model_settings
from rynmesh.llm_package.chat import validate_chat
from rynmesh.llm_package.image_input_profile import SHARED_V2_CARRIER, shared_image_carrier_scope
from rynmesh.llm_package.transport_failure import describe, image_rejection


@pytest.fixture(autouse=True)
def _clean_learned():
    caps.reset_learned_image_rejections()
    yield
    caps.reset_learned_image_rejections()


def api_row(**extra):
    return {"id": "local/src-1", "name": "DeepSeek · deepseek-flash", "source": "local_api", "adapter": "openai_compatible",
            "model_id": "deepseek-flash", "agent_supported": True, **extra}


def peer_row(*capabilities, **extra):
    return {"id": "peer/abc/svc", "name": "DeepSeek · deepseek-flash", "source": "peer", "adapter": "openai_compatible",
            "model_id": "DeepSeek · deepseek-flash", "agent_supported": True, "capabilities": list(capabilities), **extra}


def decide(row):
    return caps.evaluate_image_capability(row, model_client=AgentModelBridge)


# --- 1. The decision itself -------------------------------------------------------------------

def test_shared_models_follow_the_providers_declaration_instead_of_refusing_every_image():
    unknown = decide(peer_row())
    assert unknown["supports_image_input"] is True and unknown["image_transport"] == "peer_inline_v1"
    declared = decide(peer_row("image-input", "image-carrier:inline-v2"))
    assert declared["supports_image_input"] is True and declared["image_support"] == "supported"
    assert declared["image_transport"] == "peer_inline_v2"
    refused = decide(peer_row("image-unsupported"))
    assert refused["supports_image_input"] is False
    assert refused["image_capability_reason"] == "provider_declared_image_unsupported"


def test_catalog_projection_keeps_the_shared_transport():
    projected = {**peer_row("image-input", "image-carrier:inline-v2")}
    projected.update(caps.catalog_image_fields(projected))
    projected.pop("capabilities")
    assert decide(projected)["image_transport"] == "peer_inline_v2"


def test_owner_setting_wins_over_provider_metadata_and_learned_rejections():
    text_only = {"model_metadata": {"deepseek-flash": {"input_modalities": ["text"]}}}
    assert decide(api_row(**text_only))["supports_image_input"] is False
    forced = api_row(**text_only, model_settings={"overrides": {"image_input": "supported"}})
    assert decide(forced)["supports_image_input"] is True
    assert decide(forced)["image_capability_reason"] == "owner_setting_supported"
    off = api_row(model_settings={"overrides": {"image_input": "unsupported"}})
    assert decide(off)["supports_image_input"] is False
    caps.mark_image_rejected(api_row())
    learned = decide(api_row())
    assert learned["supports_image_input"] is False and learned["image_capability_reason"] == "upstream_rejected_image"
    assert decide(api_row(model_settings={"overrides": {"image_input": "supported"}}))["supports_image_input"] is True


def test_learned_rejection_expires_after_a_day():
    caps.mark_image_rejected(api_row(), now=1000.0)
    assert caps.learned_image_rejection(api_row(), now=1000.0 + 3600)
    assert not caps.learned_image_rejection(api_row(), now=1000.0 + caps.LEARNED_REJECTION_SECONDS + 1)


def test_image_test_result_reaches_the_decision():
    tested = api_row(evidence={"image": {"status": "unsupported", "checked_at": "2026-11-09T00:00:00+00:00"}})
    assert decide(tested)["supports_image_input"] is False
    assert decide(api_row(evidence={"image": {"status": "supported"}}))["image_support"] == "supported"


def test_local_service_with_an_api_protocol_carries_images_like_a_local_api():
    assert decide({**api_row(), "source": "local_service"})["supports_image_input"] is True


# --- 2. The sharing computer publishes the same answer ---------------------------------------

def test_image_input_state_order_and_setting_validation():
    record = {"models": ["m"], "model_settings": {"m": {"overrides": {}, "evidence": {}}}, "model_metadata": {}}
    assert model_settings.image_input_state(record, "m") == "unknown"
    record["model_metadata"] = {"m": {"input_modalities": ["text", "image"]}}
    assert model_settings.image_input_state(record, "m") == "supported"
    record["model_settings"]["m"]["evidence"] = {"image": {"status": "unsupported"}}
    assert model_settings.image_input_state(record, "m") == "unsupported"
    record["model_settings"]["m"]["overrides"] = {"image_input": "supported"}
    assert model_settings.image_input_state(record, "m") == "supported"
    assert model_settings.validate({"image_input": "auto"}) == {"image_input": "auto"}
    with pytest.raises(ValueError, match="model_image_input_invalid"):
        model_settings.validate({"image_input": "maybe"})


# --- 3. A provider's "no images" answer is recognised without keeping its text ---------------

def test_image_refusals_are_recognised_from_common_error_bodies():
    deepseek = {"error": {"message": "Failed to deserialize the JSON body into the target type: messages[0]: unknown variant `image_url`, expected `text`", "type": "invalid_request_error"}}
    openai = {"error": {"message": "Invalid content type. image_url is only supported by certain models.", "code": "invalid_value"}}
    unrelated = {"error": {"message": "max_tokens is too large", "type": "invalid_request_error"}}
    assert image_rejection(deepseek) and image_rejection(openai) and not image_rejection(unrelated)
    exc = HTTPError("http://x", 400, "Bad Request", {}, None)
    described = describe(exc, "request", deepseek)
    assert described == {"network_reason": "http", "network_phase": "request", "http_status": 400, "image_rejected": True}
    assert "image_rejected" not in describe(HTTPError("http://x", 500, "x", {}, None), "request", deepseek)


# --- 4. The shared route carries the same images ----------------------------------------------

def _peer_bridge(*capabilities):
    bridge, _, row, services = _make_bridge()
    row.update(source="peer", peer_id="other-peer", capabilities=list(capabilities))
    row.update(caps.catalog_image_fields(row))
    calls = []

    async def execute_order(request, *args, **kwargs):
        calls.append(copy.deepcopy(request))
        return {"state": "succeeded", "message": {"role": "assistant", "content": ANSWER}, "finish_reason": "stop",
                "reported_usage": {"input_tokens": 12, "output_tokens": 4}}

    services["execute_order"] = execute_order
    services["cancel_order"] = lambda _: None
    return bridge, row, calls


def _sent_image_bytes(request):
    part = request["chat"]["messages"][0]["content"][1]
    return base64.b64decode(part["image_url"]["url"].split(",", 1)[1])


def test_shared_route_sends_an_attachment_image_unchanged():
    bridge, row, calls = _peer_bridge()
    result = bridge.complete(row["id"], _user_image_messages(PNG_SEED_SMALL), [], lambda: False)
    assert result["message"]["content"] == ANSWER and "shared_image_resize" not in result
    assert _sent_image_bytes(calls[0]) == PNG_SEED_SMALL and "image_carrier" not in calls[0]


def test_older_sharing_computer_gets_a_compressed_copy_instead_of_an_error():
    bridge, row, calls = _peer_bridge()
    result = bridge.complete(row["id"], _user_image_messages(PNG_3_46MIB), [], lambda: False)
    sent = _sent_image_bytes(calls[0])
    assert len(sent) <= 128 * 1024 and Image.open(io.BytesIO(sent)).format == "JPEG"
    assert result["shared_image_resize"]["resized"] == 1
    assert result["shared_image_resize"]["original_bytes"] == len(PNG_3_46MIB)


def test_new_sharing_computer_gets_the_full_size_image():
    bridge, row, calls = _peer_bridge("image-input", "image-carrier:inline-v2")
    result = bridge.complete(row["id"], _user_image_messages(PNG_3_46MIB), [], lambda: False)
    assert _sent_image_bytes(calls[0]) == PNG_3_46MIB
    assert calls[0]["image_carrier"] == SHARED_V2_CARRIER and "shared_image_resize" not in result


def test_shared_v2_validation_accepts_larger_images_only_inside_its_scope():
    chat = {"messages": _user_image_messages(PNG_3_46MIB), "max_tokens": 64}
    with pytest.raises(ValueError):
        validate_chat(copy.deepcopy(chat))
    with shared_image_carrier_scope(SHARED_V2_CARRIER):
        assert validate_chat(copy.deepcopy(chat))["messages"][0]["content"][1]["type"] == "image_url"


def test_declared_text_only_shared_model_is_not_sent_images():
    bridge, row, calls = _peer_bridge("image-unsupported")
    with pytest.raises(AgentError):
        bridge.complete(row["id"], _user_image_messages(PNG_SEED_SMALL), [], lambda: False)
    assert calls == []


# --- 5. History images never break a later request --------------------------------------------

def _png_attachment():
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (10, 120, 200)).save(buf, "PNG")
    return {"name": "screen.png", "media_type": "image/png", "data_base64": base64.b64encode(buf.getvalue()).decode()}


def send(runtime, task_id, message):
    row = runtime.get(task_id)
    return runtime.add_message(task_id, {"message": message, "intent": "information",
                                         "expected_control_revision": row["control_revision"],
                                         "expected_goal_revision": row["goal"]["revision"]})


def test_text_only_route_gets_a_note_instead_of_failing_now_and_later(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("I cannot see the picture."), assistant("Second answer."))
    row = runtime.create({"message": "What is in this screenshot?", "attachments": [_png_attachment()]})
    advance(runtime)
    current = runtime.get(row["id"])
    assert current["status"] == "completed" and current["error"] is None
    first = json.dumps(model.calls[0][1])
    assert "image_url" not in first and "selected model cannot read images" in first
    omitted = [event for event in current["events"] if event["type"] == "images_omitted"]
    assert len(omitted) == 1 and omitted[0]["latest_message"] is True
    send(runtime, row["id"], "Fine, answer from the file name then.")
    advance(runtime)
    assert runtime.get(row["id"])["result"] == "Second answer."
    assert "image_url" not in json.dumps(model.calls[1][1])


class _RefusingModel(Model):
    supports_image_input = True

    def complete(self, model_id, messages, tool_specs, cancelled, on_event, **kwargs):
        if "image_url" in json.dumps(messages):
            self.calls.append((model_id, messages, tool_specs))
            raise AgentError("agent_model_image_rejected")
        return super().complete(model_id, messages, tool_specs, cancelled, on_event)


def test_refused_image_is_retried_once_without_it_and_remembered(tmp_path):
    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.store import AgentStore
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from test_agent_runtime import Tools

    model = _RefusingModel(assistant("Answered without the image."), assistant("Still fine."))
    runtime = AgentRuntime(AgentStore(tmp_path, X25519PrivateKey.generate()), model, Tools(), Catalog())
    runtime.update_settings({"enabled": True, "model_id": "local:test"})
    row = runtime.create({"message": "Describe this.", "attachments": [_png_attachment()]})
    advance(runtime)
    current = runtime.get(row["id"])
    assert current["status"] == "completed" and current["result"] == "Answered without the image."
    assert len(model.calls) == 2  # One refused attempt, one retry without the image.
    assert [e["reason"] for e in current["events"] if e["type"] == "images_omitted"] == ["upstream_rejected_image"]
    send(runtime, row["id"], "And again.")
    advance(runtime)
    assert runtime.get(row["id"])["result"] == "Still fine."
    assert len(model.calls) == 3  # Remembered: no second refused attempt.
