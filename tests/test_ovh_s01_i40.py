"""S01-I40: isolate image limits, keep current images and explain omissions."""
import base64
import copy
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from test_agent_image_carrier_profile import _make_bridge
from test_agent_model_image_support import _peer_bridge
from test_agent_runtime import advance, assistant, make_runtime
from rynmesh.agent_runtime import diagnostics, image_capabilities as caps, output_policy
from rynmesh.agent_runtime.store import AgentError
from rynmesh.llm_package import image_input_profile as profile


def picture(fmt="PNG", color="red"):
    target = io.BytesIO()
    Image.new("RGB", (1, 1), color).save(target, fmt)
    return {"type": "image_url", "image_url": {"url": "data:image/" + fmt.lower() + ";base64," + base64.b64encode(target.getvalue()).decode()}}


def message(*images):
    return {"role": "user", "content": [{"type": "text", "text": "Inspect images"}, *images]}


def test_t1_uncompressible_old_image_omitted_and_good_images_reach_shared_transport(monkeypatch):
    monkeypatch.setattr(profile, "LEGACY_PUBLIC_MAX_IMAGE_BYTES", 100)
    old, current = picture("GIF"), picture()
    messages = [message(old), {"role": "assistant", "content": "prior"}, message(current)]
    original = copy.deepcopy(messages)
    bridge, row, calls = _peer_bridge()
    result = bridge.complete(row["id"], messages, [], lambda: False)
    sent = calls[0]["chat"]["messages"]
    assert caps.SHARED_IMAGE_OMITTED_NOTE in sent[0]["content"]
    assert sent[-1]["content"][1] == current
    assert result["shared_image_resize"]["omitted"] == 1
    assert result["shared_image_resize"]["omitted_old"] == 1
    assert result["shared_image_resize"]["resized"] == 0
    assert messages == original


def test_t2_all_latest_images_uncompressible_raise_specific_error(monkeypatch):
    monkeypatch.setattr(profile, "LEGACY_PUBLIC_MAX_IMAGE_BYTES", 100)
    bridge, row, calls = _peer_bridge()
    with pytest.raises(AgentError, match="^agent_image_too_large$"):
        bridge.complete(row["id"], [message(picture("GIF"))], [], lambda: False)
    assert not calls


def test_t2_one_failed_latest_image_does_not_discard_its_good_sibling(monkeypatch):
    monkeypatch.setattr(profile, "LEGACY_PUBLIC_MAX_IMAGE_BYTES", 100)
    bridge, row, calls = _peer_bridge()
    good = picture()
    result = bridge.complete(row["id"], [message(picture("GIF"), good)], [], lambda: False)
    assert calls[0]["chat"]["messages"][-1]["content"][-1] == good
    assert result["shared_image_resize"]["omitted"] == 1
    assert result["shared_image_resize"]["omitted_old"] == 0


def test_t1_old_images_can_all_be_omitted_when_latest_owner_message_is_text(monkeypatch):
    monkeypatch.setattr(profile, "LEGACY_PUBLIC_MAX_IMAGE_BYTES", 100)
    bridge, row, calls = _peer_bridge()
    result = bridge.complete(row["id"], [message(picture("GIF")), {"role": "user", "content": "answer in text"}], [], lambda: False)
    assert len(calls) == 1
    assert result["shared_image_resize"]["omitted_old"] == 1


def test_t3_v2_omits_oldest_first_without_splitting_the_total_budget(monkeypatch):
    old, newer, latest = picture(color="red"), picture(color="blue"), picture(color="green")
    size = len(base64.b64decode(latest["image_url"]["url"].split(",")[1]))
    monkeypatch.setattr(profile, "SHARED_V2_MAX_TOTAL_IMAGE_BYTES", 2 * size + 5)
    sent, report = caps.fit_images_for_shared_route([message(old), message(newer), message(latest)], profile.SHARED_V2_CARRIER)
    assert sent[0]["content"][1]["type"] == "text"
    assert sent[0]["content"][1]["text"] == caps.SHARED_IMAGE_TOTAL_OMITTED_NOTE
    assert sent[1]["content"][1] == newer and sent[2]["content"][1] == latest
    assert report["omitted"] == report["omitted_old"] == 1
    assert report["resized"] == 0


@pytest.mark.parametrize("limits,code,reason", [
    ({"max_single_image_bytes": 10}, "agent_image_too_large", "single_image_decoded_bytes_exceeded"),
    ({"max_total_image_bytes": 100}, "agent_image_too_large", "total_image_decoded_bytes_exceeded"),
    ({"max_images": 1}, "agent_image_count_limit", "max_request_images_exceeded"),
    ({"max_raw_request_bytes": 10}, "agent_context_limit", "raw_request_utf8_bytes_exceeded"),
])
def test_t4_admission_returns_image_specific_codes_and_actual_limits(limits, code, reason):
    with pytest.raises(AgentError, match="^" + code + "$") as error:
        output_policy.admit_request_context({"context_window": 100000, "max_output_tokens": 1024},
                                            [message(picture(), picture())], [], **limits)
    assert error.value.details["reason"] == reason
    assert all(error.value.details["limits"][name] == value for name, value in limits.items())


def runtime_images(tmp_path, history):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("images received"))
    model.supports_image_input = True
    original = runtime.catalog.list_models
    runtime.catalog.list_models = lambda: [{**row, "adapter": "openai_compatible", "supports_image_input": True} for row in original()]
    task_id = runtime.create({"message": "Inspect images"})["id"]
    runtime.store.transaction(lambda data: data["tasks"][task_id].update(messages=copy.deepcopy(history)))
    return runtime, model, task_id


def test_t4_runtime_count_error_is_not_context_compaction_and_has_guidance(tmp_path, monkeypatch):
    runtime, model, task_id = runtime_images(tmp_path, [message(*[picture() for _ in range(5)])])
    compacted = []
    real = runtime._compact
    def compact(*args, **kwargs):
        compacted.append(True)
        return real(*args, **kwargs)
    monkeypatch.setattr(runtime, "_compact", compact)
    advance(runtime)
    row = runtime.store.read()["tasks"][task_id]
    assert row["error"] == "agent_image_count_limit"
    assert not compacted and not model.calls
    tools = diagnostics.DiagnosticTools(SimpleNamespace(state=SimpleNamespace(agent_runtime=runtime)), runtime.catalog, None)
    report = tools.execute("ryn_diagnose_current_task", {}, context={"task_id": task_id}, cancelled=lambda: False)
    assert report["image_limits"]["max_images"] == output_policy.MAX_REQUEST_IMAGES
    assert report["image_limits"]["max_single_image_bytes"] == output_policy.MAX_SINGLE_IMAGE_DECODED_BYTES
    assert "图片太多" in diagnostics._ERROR_GUIDANCE["agent_image_count_limit"][1]
    assert str(output_policy.MAX_REQUEST_IMAGES) in diagnostics._ERROR_GUIDANCE["agent_image_count_limit"][1]
    assert "图片太大" in diagnostics._ERROR_GUIDANCE["agent_image_too_large"][1]


def test_t5_local_bridge_omits_only_history_before_retry_and_sends_latest(monkeypatch):
    bridge, adapter, row, _ = _make_bridge()
    calls = []
    def chat(body, **kwargs):
        calls.append(copy.deepcopy(body))
        return {"message": {"role": "assistant", "content": "seen"}, "finish_reason": "stop"}
    monkeypatch.setattr(adapter, "chat", chat)
    old, latest = message(*[picture() for _ in range(4)]), message(picture(color="blue"))
    result = bridge.complete(row["id"], [old, latest], [], lambda: False)
    assert len(calls) == 1
    assert sum(part["type"] == "image_url" for item in calls[0]["messages"] if isinstance(item["content"], list) for part in item["content"]) == 1
    assert calls[0]["messages"][-1]["content"][1] == latest["content"][1]
    assert result["shared_image_resize"]["omitted_old"] == 4
    assert old["content"][1]["type"] == "image_url"


def test_t5_runtime_pre_admission_keeps_old_image_limits_from_blocking_bridge(tmp_path):
    history = [message(*[picture() for _ in range(4)]), message(picture(color="blue"))]
    runtime, model, task_id = runtime_images(tmp_path, history)
    advance(runtime)
    row = runtime.store.read()["tasks"][task_id]
    assert row["status"] == "completed" and row["error"] is None
    assert len(model.calls) == 1
    sent = model.calls[0][1]
    assert sum(part.get("type") == "image_url" for item in sent if isinstance(item.get("content"), list) for part in item["content"]) == 1
    event = next(entry for entry in row["events"] if entry["type"] == "images_omitted_for_limits")
    assert event["omitted"] == event["omitted_old"] == 4


def test_t2_latest_image_count_limit_is_not_fixed_by_silently_dropping_latest(monkeypatch):
    bridge, _, row, _ = _make_bridge()
    with pytest.raises(AgentError, match="^agent_image_count_limit$"):
        bridge.complete(row["id"], [message(picture()), message(*[picture() for _ in range(5)])], [], lambda: False)
