"""The sharing computer accepts full-size images only on the carrier it announced."""
from __future__ import annotations

import base64
import copy

import pytest
from test_agent_image_carrier_profile import PNG_3_46MIB, _user_image_messages
from test_llm_package import _FakeAdapter

from rynmesh.llm_package.chat import validate_chat
from rynmesh.llm_package.manifest import LLMPackageManifest
from rynmesh.llm_package.routes import ProviderService, _expires, _open_provider_response
from rynmesh.llm_package.task_balance import TaskBalanceLedger
from rynmesh.llm_package.task_protocol import TaskOrderStore, TaskProtocolError, seal_task
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


class _ChatAdapter(_FakeAdapter):
    def __init__(self):
        super().__init__()
        self.images = []

    def chat(self, body, *, task_id, timeout_s, on_event=None, **kwargs):
        checked = validate_chat(copy.deepcopy(body))  # Real adapters validate again before the upstream call.
        part = checked["messages"][0]["content"][1]
        self.images.append(base64.b64decode(part["image_url"]["url"].split(",", 1)[1]))
        return {"text": "Seen.", "message": {"role": "assistant", "content": "Seen."}, "finish_reason": "stop",
                "input_tokens": 5, "output_tokens": 1, "reported_usage": {"input_tokens": 5, "output_tokens": 1}}


def _order(tmp_path, capabilities, carrier):
    provider = RynmeshStore(home=tmp_path / "provider", network_dir=tmp_path / "net")
    consumer = RynmeshStore(home=tmp_path / "consumer", network_dir=tmp_path / "net")
    pk = peer_box.load_or_create_messaging_key(provider.home / "fixture-key")
    ck = peer_box.load_or_create_messaging_key(consumer.home / "fixture-key")
    adapter = _ChatAdapter()
    service = ProviderService(manifest=LLMPackageManifest(package_id="svc", mode="openai_compatible", public_model_alias="fixture",
        base_url="http://127.0.0.1:1", capabilities=["text-generation", "client-tools", *capabilities]), adapter=adapter,
        store=provider, task_store=TaskOrderStore(tmp_path / "orders"), balance=TaskBalanceLedger(tmp_path / "balance.json"),
        messaging_key=pk, access_check=lambda *_: {})
    chat = {"messages": _user_image_messages(PNG_3_46MIB), "max_tokens": 16}
    body = {"task_id": "img", "service_id": "svc", "prompt": "[chat]", "chat": chat, "max_tokens": 16,
            "max_amount": 1, "reply_messaging_pub": peer_box.public_key_b64(ck), **({"image_carrier": carrier} if carrier else {})}
    request = seal_task(body=body, task_id="img", kind="llm_request", sender_peer_id=consumer.peer_id,
        recipient_peer_id=provider.peer_id, sender_signing_key=consumer.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(pk), expires_at=_expires(300)).to_dict()
    return service, adapter, request, consumer, ck, provider


def test_announced_v2_carrier_delivers_the_full_size_image(tmp_path):
    service, adapter, request, consumer, ck, provider = _order(tmp_path, ["image-carrier:inline-v2"], "inline-v2")
    _, value = _open_provider_response(service.handle(request), recipient_peer_id=consumer.peer_id, messaging_key=ck,
                                       task_id="img", provider_peer_id=provider.peer_id, service_id="svc")
    assert value["state"] == "succeeded"
    assert adapter.images == [PNG_3_46MIB]


def test_v2_request_is_refused_by_a_model_that_did_not_announce_it(tmp_path):
    service, adapter, request, *_ = _order(tmp_path, [], "inline-v2")
    with pytest.raises(TaskProtocolError, match="unsupported image carrier"):
        service.handle(request)
    assert adapter.images == []


def test_without_the_carrier_the_legacy_size_limit_still_applies(tmp_path):
    service, adapter, request, *_ = _order(tmp_path, ["image-carrier:inline-v2"], None)
    with pytest.raises(Exception):
        service.handle(request)
    assert adapter.images == []


def test_context_estimate_counts_an_inline_image_as_a_bounded_cost():
    from rynmesh.llm_package.context_safety import IMAGE_CONTEXT_TOKENS, context_request_fits, estimate_context_safety_tokens

    image = "data:image/png;base64," + base64.b64encode(PNG_3_46MIB).decode()
    prompt = '{"messages":[{"role":"user","content":[{"type":"text","text":"看图"},{"type":"image_url","image_url":{"url":"' + image + '"}}]}]}'
    assert estimate_context_safety_tokens(prompt) < 4000 + IMAGE_CONTEXT_TOKENS
    assert context_request_fits(prompt, output_tokens=4096, context_window=32768)
    assert estimate_context_safety_tokens("中文" * 10) == len(("中文" * 10).encode())
