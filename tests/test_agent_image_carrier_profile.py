"""Focused regression suite for full-resolution local canonical image carrier profiles."""

from __future__ import annotations

import base64
from collections import deque
import copy
import dataclasses
import hashlib
import io
import json
from pathlib import Path
import random
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from PIL import Image
import pytest

from rynmesh.agent_runtime import image_capabilities, output_policy
from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore, AgentToolRejected, encoded
from rynmesh.llm_package import codex_session, codex_threads, peer_binding
from rynmesh.llm_package.adapters import AdapterError, OpenAICompatibleAdapter
from rynmesh.llm_package.chat import validate_chat
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.image_input_profile import (
    LEGACY_PUBLIC_MAX_IMAGE_BYTES,
    LEGACY_PUBLIC_MAX_URL_CHARS,
    MAX_INTERNAL_RAW_REQUEST_BYTES,
    MAX_INTERNAL_REQUEST_IMAGES,
    MAX_INTERNAL_SINGLE_IMAGE_BYTES,
    MAX_INTERNAL_SINGLE_IMAGE_PIXELS,
    MAX_INTERNAL_TOTAL_IMAGE_BYTES,
    MAX_INTERNAL_TOTAL_IMAGE_PIXELS,
    ImagePartIdentity,
    InternalImageProfile,
    RouteImageIdentity,
    check_legacy_public_carrier_compatible,
    current_internal_image_profile,
    inspect_canonical_user_image,
    internal_image_scope,
    prepare_internal_image_profile,
    project_protocol_request_without_admitted_images,
    verify_messages_against_profile,
)
from rynmesh.llm_package.protocol_adapter import NativeProtocolAdapter
from rynmesh.llm_package.protocol_codecs import (
    MESSAGES,
    RESPONSES,
    StreamDecoder,
    _copy,
    make_request,
    parse_response,
)

ANSWER = "Authored terminal transport response."


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _data_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _image_part(raw_or_url: bytes | str, *, mime: str = "image/png", detail: str | None = "high") -> dict[str, object]:
    url = raw_or_url if isinstance(raw_or_url, str) else _data_url(raw_or_url, mime=mime)
    image_url: dict[str, object] = {"url": url}
    if detail is not None:
        image_url["detail"] = detail
    return {"type": "image_url", "image_url": image_url}


def _text_part(text: str = "Owned transport fixture. Return an authored answer.") -> dict[str, object]:
    return {"type": "text", "text": text}


def _user_image_messages(
    raw_or_url: bytes | str,
    *,
    mime: str = "image/png",
    detail: str | None = "high",
    text: str = "Owned transport fixture. Return an authored answer.",
) -> list[dict[str, object]]:
    return [{"role": "user", "content": [_text_part(text), _image_part(raw_or_url, mime=mime, detail=detail)]}]


def _make_uncompressed_png(width: int, height: int, *, seed: int = 337) -> bytes:
    buf = io.BytesIO()
    raw_pixels = random.Random(seed).randbytes(width * height * 3)
    Image.frombytes("RGB", (width, height), raw_pixels).save(buf, format="PNG", compress_level=0)
    return buf.getvalue()


def _make_solid_png(width: int = 64, height: int = 48, color: tuple[int, int, int] = (23, 45, 67)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _make_format_bytes(fmt: str, width: int = 512, height: int = 384, *, seed: int = 99) -> bytes:
    buf = io.BytesIO()
    raw_pixels = random.Random(seed).randbytes(width * height * 3)
    img = Image.frombytes("RGB", (width, height), raw_pixels)
    if fmt == "PNG":
        img.save(buf, format="PNG", compress_level=0)
    elif fmt == "JPEG":
        img.save(buf, format="JPEG", quality=100)
    elif fmt == "WEBP":
        img.save(buf, format="WEBP", lossless=True)
    elif fmt == "GIF":
        img.save(buf, format="GIF")
    else:
        raise ValueError(fmt)
    return buf.getvalue()


# 1280x900 uncompressed PNG (~3.46 MiB decoded, > 128 KiB legacy cap)
PNG_3_46MIB = _make_uncompressed_png(1280, 900, seed=301)
PNG_3_46MIB_SHA = _sha256(PNG_3_46MIB)

# 2048x1360 uncompressed PNG (~8.36 MiB decoded, just under 8 MiB = 8,388,608 bytes)
PNG_NEAR_8MIB = _make_uncompressed_png(2048, 1360, seed=337)
PNG_NEAR_8MIB_SHA = _sha256(PNG_NEAR_8MIB)

# Small canonical PNG (<= 128 KiB legacy cap)
PNG_SEED_SMALL = _make_solid_png(64, 48)
PNG_SEED_SMALL_SHA = _sha256(PNG_SEED_SMALL)


def _native_response(protocol: str) -> dict[str, object]:
    if protocol == "openai_responses":
        return {
            "id": "own_resp",
            "status": "completed",
            "error": None,
            "output": [
                {
                    "type": "message",
                    "id": "own_msg",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": ANSWER, "annotations": []}],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    if protocol == "anthropic_messages":
        return {
            "id": "own_msg",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": ANSWER}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": ANSWER},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _stream_events(protocol: str) -> list[dict[str, object]]:
    final = _native_response(protocol)
    if protocol == "openai_responses":
        return [
            {"type": "response.created", "response": {"id": "own_resp", "status": "in_progress"}},
            {"type": "response.output_text.delta", "delta": ANSWER},
            {"type": "response.completed", "response": final},
        ]
    if protocol == "anthropic_messages":
        return [
            {
                "type": "message_start",
                "message": {
                    "id": "own_msg",
                    "role": "assistant",
                    "content": [],
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": ANSWER}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ANSWER}, "finish_reason": None}]},
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    ]


class _HTTPResponse:
    def __init__(self, protocol: str, stream: bool) -> None:
        self.headers = {"content-type": "text/event-stream" if stream else "application/json"}
        if stream:
            raw = (
                "".join("data: " + json.dumps(e) + "\n\n" for e in _stream_events(protocol))
                + ("data: [DONE]\n\n" if protocol == "openai_compatible" else "")
            ).encode("utf-8")
        else:
            raw = json.dumps(_native_response(protocol)).encode("utf-8")
        self.buffer = io.BytesIO(raw)
        self.closed = False

    def __enter__(self) -> "_HTTPResponse":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def read(self, n: int = -1) -> bytes:
        return self.buffer.read(n)

    def readline(self, n: int = -1) -> bytes:
        return self.buffer.readline(n)

    def close(self) -> None:
        self.closed = True
        self.buffer.close()


class _EgressCapture:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.responses: list[_HTTPResponse] = []
        self.inside = None
        self.fail = False

    def __call__(self, request, *args, **kwargs) -> _HTTPResponse:
        assert current_internal_image_profile() is not None
        body = json.loads(request.data)
        path = request.full_url
        protocol = (
            "openai_responses"
            if path.endswith("/responses")
            else "anthropic_messages"
            if path.endswith("/messages")
            else "openai_compatible"
        )
        self.calls.append({"protocol": protocol, "body": body, "thread_id": threading.get_ident()})
        if self.inside:
            self.inside()
        if self.fail:
            raise OSError("Owned synthetic transport failure after capture")
        response = _HTTPResponse(protocol, bool(body.get("stream")))
        self.responses.append(response)
        return response

    def extracted_images(self) -> list[dict[str, object]]:
        values: list[dict[str, object]] = []
        for call in self.calls:
            raw_list: list[bytes] = []
            body = call["body"]
            proto = call["protocol"]
            messages_or_input = body.get("input" if proto == "openai_responses" else "messages", [])
            for message in messages_or_input:
                content = message.get("content", []) if isinstance(message.get("content"), list) else []
                for part in content:
                    if part.get("type") == "image_url":
                        raw_list.append(base64.b64decode(part["image_url"]["url"].split(",", 1)[1]))
                    elif part.get("type") == "input_image":
                        raw_list.append(base64.b64decode(part["image_url"].split(",", 1)[1]))
                    elif part.get("type") == "image":
                        raw_list.append(base64.b64decode(part["source"]["data"]))
            values.append(
                {
                    "protocol": proto,
                    "raw_outgoing_bytes": len(json.dumps(body).encode("utf-8")),
                    "image_count": len(raw_list),
                    "images": [{"bytes": len(x), "sha256": _sha256(x)} for x in raw_list],
                    "thread_id": call["thread_id"],
                }
            )
        return values


def _make_bridge(
    protocol: str = "openai_compatible",
    streaming: bool = False,
    **row_overrides: object,
) -> tuple[AgentModelBridge, object, dict[str, object], dict[str, object]]:
    if protocol == "codex_cli":
        adapter: object = CLIAgentAdapter("codex_cli", executable=sys.executable)
    elif protocol in {"openai_responses", "anthropic_messages"}:
        adapter = NativeProtocolAdapter(
            protocol=protocol,
            base_url="http://127.0.0.1:1",
            model="own-model",
            api_key="dummy-key",
        )
    else:
        adapter = OpenAICompatibleAdapter(
            base_url="http://127.0.0.1:1",
            model="own-model",
            api_key="dummy-key",
        )
    row: dict[str, object] = {
        "id": "own-local-model",
        "name": "Owned capturing catalog fixture",
        "available": True,
        "source": "local_service" if protocol == "codex_cli" else "local_api",
        "adapter": protocol,
        "service_id": "own-service",
        "model_id": "own-model",
        "cli_model": "own-model" if protocol == "codex_cli" else "",
        "context_window": 4_000_000,
        "max_output_tokens": 2048,
        "supports_image_input": True,
        "agent_supported": True,
        "streaming": streaming,
        **row_overrides,
    }

    class _Catalog:
        network = "own-network"

        def list_models(self):
            return [copy.deepcopy(row)]

        def resolve(self, _):
            return copy.deepcopy(row)

        @staticmethod
        def destination(r):
            return {
                "catalog_model_id": r["id"],
                "provider_peer_id": r.get("peer_id", ""),
                "source_id": r.get("source_id", ""),
                "destination_revision": r.get("destination_revision", ""),
            }

    manager = SimpleNamespace(
        adapter=adapter,
        manifest=SimpleNamespace(timeout_seconds=10),
        _slots=threading.Semaphore(4),
        _lock=threading.Lock(),
        _running=0,
    )
    services: dict[str, object] = {"provider_for": lambda _: manager}
    bridge = AgentModelBridge(services, _Catalog())
    return bridge, adapter, row, services


class _BoundCLIAdapterProxy:
    """Wraps a real CLIAgentAdapter to exercise bridge.complete's call_bound path."""

    def __init__(self, inner: CLIAgentAdapter, binding: dict[str, object]) -> None:
        self._inner = inner
        self._binding = copy.deepcopy(binding)
        self.call_bound_count = 0

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def binding_for_model(self, cli_model: str = "") -> dict[str, object]:
        return copy.deepcopy(self._binding)

    def call_bound(self, method_name: str, expected_binding: dict[str, object], refresh_fn, *args, **kwargs):
        assert method_name == "chat"
        assert expected_binding == self._binding
        assert current_internal_image_profile() is not None
        self.call_bound_count += 1
        return getattr(self._inner, method_name)(*args, **kwargs)


# ============================================================================
# 1. Public Default Contract & No-Caller-Bypass Tests
# ============================================================================


def test_public_validate_chat_retains_128kib_limit_and_rejects_json_grant_flags():
    assert len(PNG_3_46MIB) > LEGACY_PUBLIC_MAX_IMAGE_BYTES
    assert len(PNG_NEAR_8MIB) < MAX_INTERNAL_SINGLE_IMAGE_BYTES

    with pytest.raises(ValueError, match="images must be bounded inline data URLs"):
        validate_chat({"messages": _user_image_messages(PNG_3_46MIB), "max_tokens": 32})

    # Image with URL <= 180,000 chars but decoded > 128 KiB (e.g. ~131,500 bytes -> ~175,336 base64 chars)
    over_128kib = _make_uncompressed_png(210, 210, seed=12)
    assert LEGACY_PUBLIC_MAX_IMAGE_BYTES < len(over_128kib) < 135_000
    over_128kib_url = _data_url(over_128kib)
    assert len(over_128kib_url) < LEGACY_PUBLIC_MAX_URL_CHARS
    with pytest.raises(ValueError, match="image size limit exceeded"):
        validate_chat({"messages": _user_image_messages(over_128kib_url), "max_tokens": 32})

    for forbidden_key in (
        "image_profile",
        "image_input_carrier_profile",
        "_image_carrier_profile",
        "allow_large_images",
        "trusted_carrier",
    ):
        with pytest.raises(ValueError, match="unsupported chat parameters"):
            validate_chat(
                {
                    "messages": _user_image_messages(PNG_SEED_SMALL),
                    "max_tokens": 32,
                    forbidden_key: {"trusted": True},
                }
            )


def test_internal_image_scope_rejects_non_profile_objects_and_forged_detached_identities():
    with pytest.raises(TypeError, match="invalid_internal_image_profile"):
        with internal_image_scope({"trusted": True}):  # type: ignore[arg-type]
            pass

    _, _, row, _ = _make_bridge()
    messages = _user_image_messages(PNG_3_46MIB)
    ident, err = inspect_canonical_user_image("user", messages[0]["content"][1], message_index=0, part_index=1)
    assert err is None and ident is not None

    # Positive path: genuine parser-issued ImagePartIdentity for unchanged message part succeeds
    genuine_profile = prepare_internal_image_profile(
        messages,
        route_identity=row,
        request_identity="own-genuine-detached",
        admitted_image_identities=[ident],
    )
    assert isinstance(genuine_profile, InternalImageProfile)
    assert genuine_profile.admitted_images == (ident,)
    assert genuine_profile.total_decoded_bytes == len(PNG_3_46MIB)
    assert genuine_profile.total_pixels == 1280 * 900

    # Original message part remains untouched and independently loads via PIL
    raw_from_part = base64.b64decode(messages[0]["content"][1]["image_url"]["url"].split(",", 1)[1], validate=True)
    assert _sha256(raw_from_part) == PNG_3_46MIB_SHA
    with Image.open(io.BytesIO(raw_from_part)) as pil_img:
        pil_img.load()
        assert pil_img.size == (1280, 900)
        assert pil_img.format == "PNG"

    # Combined P2 false-dimensions/bytes/hash replacement vector
    forged_combined = dataclasses.replace(
        ident,
        decoded_bytes=1,
        width=1,
        height=1,
        pixels=1,
        decoded_sha256="0" * 64,
    )
    with pytest.raises(ValueError, match="admitted_image_identities_mismatch"):
        prepare_internal_image_profile(
            messages,
            route_identity=row,
            request_identity="own-forged-record",
            admitted_image_identities=[forged_combined],
        )

    # Every individual field mutation via dataclasses.replace is refused
    field_mutations = [
        dataclasses.replace(ident, decoded_bytes=ident.decoded_bytes - 1),
        dataclasses.replace(ident, width=ident.width - 1),
        dataclasses.replace(ident, height=ident.height - 1),
        dataclasses.replace(ident, pixels=ident.pixels - 1),
        dataclasses.replace(ident, mime_type="image/jpeg"),
        dataclasses.replace(ident, format="JPEG"),
        dataclasses.replace(ident, decoded_sha256="0" * 64),
        dataclasses.replace(ident, url_sha256="f" * 64),
        dataclasses.replace(ident, raw_url_utf8_bytes=ident.raw_url_utf8_bytes - 1),
        dataclasses.replace(ident, detail="low"),
        dataclasses.replace(ident, explicit_detail="low"),
        dataclasses.replace(ident, message_index=1),
        dataclasses.replace(ident, part_index=0),
    ]
    for idx, mutated_ident in enumerate(field_mutations):
        with pytest.raises(ValueError, match="admitted_image_identities_mismatch"):
            prepare_internal_image_profile(
                messages,
                route_identity=row,
                request_identity=f"own-mutated-field-{idx}",
                admitted_image_identities=[mutated_ident],
            )

    # Direct typed fabrication via ImagePartIdentity constructor (without parser proof) cannot grant
    direct_fabricated = ImagePartIdentity(
        message_index=ident.message_index,
        part_index=ident.part_index,
        url_sha256=ident.url_sha256,
        raw_url_utf8_bytes=ident.raw_url_utf8_bytes,
        decoded_sha256=ident.decoded_sha256,
        decoded_bytes=ident.decoded_bytes,
        mime_type=ident.mime_type,
        format=ident.format,
        width=ident.width,
        height=ident.height,
        pixels=ident.pixels,
        detail=ident.detail,
        explicit_detail=ident.explicit_detail,
    )
    with pytest.raises(ValueError, match="admitted_image_identities_mismatch"):
        prepare_internal_image_profile(
            messages,
            route_identity=row,
            request_identity="own-direct-fabricated",
            admitted_image_identities=[direct_fabricated],
        )

    direct_profile = InternalImageProfile(
        request_identity="own-direct-profile",
        route_identity=genuine_profile.route_identity,
        admitted_images=(direct_fabricated,),
        total_decoded_bytes=direct_fabricated.decoded_bytes,
        total_pixels=direct_fabricated.pixels,
    )
    with pytest.raises(ValueError, match="invalid_internal_image_profile"):
        with internal_image_scope(direct_profile):
            pass
    with pytest.raises(ValueError, match="invalid_internal_image_profile"):
        verify_messages_against_profile(messages, direct_profile)


def test_public_protocol_codecs_reject_large_image_without_active_profile():
    scope = "a" * 64
    for protocol in (RESPONSES, MESSAGES):
        with pytest.raises(ValueError, match="invalid native image data|native protocol payload limit exceeded"):
            make_request(
                {"messages": _user_image_messages(PNG_NEAR_8MIB), "max_tokens": 32},
                protocol,
                "own-model",
                scope,
            )


# ============================================================================
# 2. Local Full-Image Positive Matrix Through Actual AgentModelBridge & Adapters
# ============================================================================


@pytest.mark.parametrize(
    "protocol",
    ["openai_compatible", "openai_responses", "anthropic_messages"],
)
@pytest.mark.parametrize("streaming", [False, True])
def test_bridge_local_http_adapters_deliver_exact_near_8mib_png_bytes(protocol: str, streaming: bool):
    capture = _EgressCapture()
    bridge, _, row, _ = _make_bridge(protocol, streaming=streaming)
    events: list[dict[str, object]] = []

    with patch("urllib.request.urlopen", capture):
        answer = bridge.complete(
            row["id"],
            _user_image_messages(PNG_NEAR_8MIB),
            [],
            lambda: False,
            on_event=events.append if streaming else None,
        )

    assert current_internal_image_profile() is None
    assert answer["message"]["content"] == ANSWER
    assert all(r.closed for r in capture.responses)
    if streaming:
        event_types = [e.get("type") for e in events]
        assert "provider_selected" in event_types
        assert "text_delta" in event_types
        assert "response_complete" in event_types

    meta = capture.extracted_images()
    assert len(meta) == 1
    assert meta[0]["image_count"] == 1
    assert meta[0]["images"][0]["bytes"] == len(PNG_NEAR_8MIB)
    assert meta[0]["images"][0]["sha256"] == PNG_NEAR_8MIB_SHA


def test_bridge_local_codex_cli_stateless_bound_and_pooled_resume_deliver_exact_near_8mib_png():
    captures: list[dict[str, object]] = []
    instances: list[object] = []

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        class _FakeCodexRPC:
            def __init__(self, *args, **kwargs) -> None:
                self.root = root
                self.pending: deque = deque()
                self.deltas: list[dict[str, object]] = []
                self.delta_lock = threading.Lock()
                self._delta_signalled = False
                self.process = SimpleNamespace(poll=lambda: None)
                self.closed = False
                self.stream = False
                self.output_bytes = 0
                self.stage = "init"
                self.events_received = 0
                self.deltas_received = 0
                instances.append(self)

            def initialize(self) -> None:
                return None

            def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
                assert current_internal_image_profile() is not None
                if method == "thread/start":
                    return {"thread": {"id": "own-thread"}}
                assert method == "turn/start", f"unexpected RPC method: {method}"
                captures.append(copy.deepcopy(params))
                turn = f"own-turn-{len(captures)}"
                text = (
                    json.dumps({"content": ANSWER, "tool_calls": []})
                    if "outputSchema" in params
                    else ANSWER
                )
                if self.stream:
                    with self.delta_lock:
                        self.deltas.append(
                            {
                                "threadId": "own-thread",
                                "turnId": turn,
                                "itemId": f"item-{len(captures)}",
                                "delta": text,
                            }
                        )
                        self._delta_signalled = True
                    self.pending.append(codex_session.DELTA_SIGNAL)
                self.pending.extend(
                    [
                        {
                            "method": "item/completed",
                            "params": {
                                "threadId": "own-thread",
                                "turnId": turn,
                                "item": {"type": "agentMessage", "text": text},
                            },
                        },
                        {
                            "method": "turn/completed",
                            "params": {
                                "threadId": "own-thread",
                                "turn": {"id": turn, "status": "completed"},
                            },
                        },
                    ]
                )
                return {"turn": {"id": turn}}

            def take_deltas(self) -> list[dict[str, object]]:
                with self.delta_lock:
                    out, self.deltas = self.deltas, []
                    self._delta_signalled = False
                    return out

            def receive(self):
                raise AssertionError("own RPC queue exhausted")

            def close(self) -> None:
                self.closed = True

            def diagnostics(self) -> dict[str, object]:
                return {"stage": "owned-rpc-terminal"}

            def alive(self) -> bool:
                return not self.closed

            def settle(self) -> bool:
                return True

            def cpu_seconds(self, _):
                return None

        binding = {
            "version": peer_binding.VERSION,
            "service_id": "own-service",
            "model_generation": "1" * 32,
            "rule_revision": "2" * 32,
        }
        dest_rev = peer_binding.local_revision(binding, "node-local")
        bridge, raw_adapter, row, services = _make_bridge(
            "codex_cli",
            streaming=True,
            peer_id="node-local",
            local_model_binding=binding,
            destination_revision=dest_rev,
        )
        bound_proxy = _BoundCLIAdapterProxy(raw_adapter, binding)
        services["provider_for"]("own-service").adapter = bound_proxy
        expected_dest = bridge.catalog.destination(row)

        tool = {
            "type": "function",
            "function": {
                "name": "own_read",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        }
        seed_messages = _user_image_messages(PNG_SEED_SMALL)
        stream_events: list[dict[str, object]] = []

        with patch.object(codex_session, "CodexRPC", _FakeCodexRPC):
            try:
                # 1) Stateless streaming turn via bound call_bound with near-8MiB PNG
                standalone = bridge.complete(
                    row["id"],
                    _user_image_messages(PNG_NEAR_8MIB),
                    [],
                    lambda: False,
                    on_event=stream_events.append,
                    expected_destination=expected_dest,
                )
                assert bound_proxy.call_bound_count == 1
                assert any(e.get("type") == "text_delta" for e in stream_events)

                # Switch manager back to raw CLIAgentAdapter for pooled thread test
                services["provider_for"]("own-service").adapter = raw_adapter

                # 2) Pooled turn 1 (seed small PNG) + turn 2 resumed (near-8MiB PNG)
                first = bridge.complete(
                    row["id"],
                    seed_messages,
                    [tool],
                    lambda: False,
                    conversation="own-carrier-window",
                )
                resumed_messages = seed_messages + [
                    first["message"],
                    {"role": "user", "content": _user_image_messages(PNG_NEAR_8MIB)[0]["content"]},
                ]
                second = bridge.complete(
                    row["id"],
                    resumed_messages,
                    [tool],
                    lambda: False,
                    conversation="own-carrier-window",
                )
                stats = copy.deepcopy(codex_threads.pool_for(raw_adapter).stats)
            finally:
                raw_adapter.close_sessions()

        assert current_internal_image_profile() is None
        assert standalone["message"]["content"] == ANSWER
        assert first["message"]["content"] == ANSWER
        assert second["message"]["content"] == ANSWER
        assert len(captures) == 3
        assert stats["resumed"] == 1
        assert all(r.closed for r in instances)

        for capture, expected_bytes in zip(captures, [PNG_NEAR_8MIB, PNG_SEED_SMALL, PNG_NEAR_8MIB], strict=True):
            images = [p for p in capture["input"] if p["type"] == "image"]
            assert len(images) == 1
            assert images[0]["url"] == _data_url(expected_bytes)
            decoded = base64.b64decode(images[0]["url"].split(",", 1)[1])
            assert _sha256(decoded) == _sha256(expected_bytes)


def test_bridge_local_supports_canonical_mimes_and_refuses_disallowed_route_mime():
    for fmt, mime in [
        ("PNG", "image/png"),
        ("JPEG", "image/jpeg"),
        ("WEBP", "image/webp"),
        ("GIF", "image/gif"),
    ]:
        raw = _make_format_bytes(fmt, 400, 300)
        capture = _EgressCapture()
        bridge, _, row, _ = _make_bridge("openai_compatible")
        with patch("urllib.request.urlopen", capture):
            res = bridge.complete(row["id"], _user_image_messages(raw, mime=mime), [], lambda: False)
        assert res["message"]["content"] == ANSWER
        meta = capture.extracted_images()
        assert len(meta) == 1 and meta[0]["images"][0]["sha256"] == _sha256(raw)

    # Route restricting supported_image_mimes to PNG/JPEG refuses GIF without transcoding or egress
    gif_raw = _make_format_bytes("GIF", 128, 96)
    capture = _EgressCapture()
    bridge_restricted, _, row_restricted, _ = _make_bridge(
        "openai_compatible",
        supported_image_mimes=["image/png", "image/jpeg"],
    )
    with patch("urllib.request.urlopen", capture):
        with pytest.raises(AgentToolRejected) as exc_info:
            bridge_restricted.complete(
                row_restricted["id"],
                _user_image_messages(gif_raw, mime="image/gif"),
                [],
                lambda: False,
            )
    assert str(exc_info.value) == "agent_image_input_unsupported"
    assert exc_info.value.details.get("reason") == "unsupported_image_mime"
    assert len(capture.calls) == 0


# ============================================================================
# 3. Carrier Envelope & Byte / Count / Pixel / Raw Request / Manual Cap Bounds
# ============================================================================


def test_carrier_profile_enforces_byte_count_pixel_raw_and_manual_caps_with_zero_egress(tmp_path: Path):
    bridge, _, row, _ = _make_bridge("openai_compatible")
    capture = _EgressCapture()

    # 1) Single image > 8 MiB (2100 x 1400 RGB uncompressed = ~8.82 MiB > 8 MiB, 2.94M pixels)
    over_8mib = _make_uncompressed_png(2100, 1400, seed=55)
    assert len(over_8mib) > MAX_INTERNAL_SINGLE_IMAGE_BYTES
    with patch("urllib.request.urlopen", capture):
        with pytest.raises(AgentError) as exc_info:
            bridge.complete(row["id"], _user_image_messages(over_8mib), [], lambda: False)
    assert str(exc_info.value) == "agent_context_limit"
    assert len(capture.calls) == 0

    # 2) Total decoded image bytes > 12 MiB across 2 valid ~6.5 MiB images (1800 x 1220 RGB = ~6.59 MiB each)
    img_6_5m_a = _make_uncompressed_png(1800, 1220, seed=61)
    img_6_5m_b = _make_uncompressed_png(1800, 1220, seed=62)
    assert len(img_6_5m_a) < MAX_INTERNAL_SINGLE_IMAGE_BYTES
    assert len(img_6_5m_a) + len(img_6_5m_b) > MAX_INTERNAL_TOTAL_IMAGE_BYTES
    two_6_5m_msgs = [
        {
            "role": "user",
            "content": [
                _text_part("Two 6.5MiB images"),
                _image_part(img_6_5m_a),
                _image_part(img_6_5m_b),
            ],
        }
    ]
    with patch("urllib.request.urlopen", capture):
        with pytest.raises(AgentError) as exc_info:
            bridge.complete(row["id"], two_6_5m_msgs, [], lambda: False)
    assert str(exc_info.value) == "agent_context_limit"
    assert len(capture.calls) == 0

    # 3) More than 4 images in request (5 small canonical images)
    five_images_msgs = [
        {
            "role": "user",
            "content": [_image_part(_make_solid_png(32, 32, (i, i, i))) for i in range(5)],
        }
    ]
    with patch("urllib.request.urlopen", capture):
        with pytest.raises(AgentError) as exc_info:
            bridge.complete(row["id"], five_images_msgs, [], lambda: False)
    assert str(exc_info.value) == "agent_context_limit"
    assert len(capture.calls) == 0

    # 4) Single image > 16,000,000 pixels (4001 x 4000 solid PNG = 16,004,000 pixels, small byte size)
    over_16mp = _make_solid_png(4001, 4000)
    assert len(over_16mp) < MAX_INTERNAL_SINGLE_IMAGE_BYTES
    with patch("urllib.request.urlopen", capture):
        with pytest.raises(AgentError) as exc_info:
            bridge.complete(row["id"], _user_image_messages(over_16mp), [], lambda: False)
    assert str(exc_info.value) == "agent_image_pixel_limit"
    assert len(capture.calls) == 0

    # 5) Total pixels > 32,000,000 across 3 images of 3500 x 3500 (12.25M pixels each = 36.75M pixels)
    img_12mp = _make_solid_png(3500, 3500)
    three_12mp_msgs = [
        {
            "role": "user",
            "content": [
                _image_part(img_12mp),
                _image_part(img_12mp),
                _image_part(img_12mp),
            ],
        }
    ]
    with patch("urllib.request.urlopen", capture):
        with pytest.raises(AgentError) as exc_info:
            bridge.complete(row["id"], three_12mp_msgs, [], lambda: False)
    assert str(exc_info.value) == "agent_image_pixel_limit"
    assert len(capture.calls) == 0

    # 6) Raw request > 16 MiB (two ~5.99 MiB images -> ~11.97 MiB decoded <= 12 MiB cap + 1 MiB text <= 4 MiB projection, > 16 MiB raw UTF-8)
    img_6m_a = _make_uncompressed_png(1750, 1140, seed=71)
    img_6m_b = _make_uncompressed_png(1750, 1140, seed=72)
    assert len(img_6m_a) + len(img_6m_b) <= MAX_INTERNAL_TOTAL_IMAGE_BYTES
    raw_over_16m_msgs = [
        {
            "role": "user",
            "content": [
                _text_part("x" * (1024 * 1024)),
                _image_part(img_6m_a),
                _image_part(img_6m_b),
            ],
        }
    ]
    assert len(encoded(raw_over_16m_msgs)) > MAX_INTERNAL_RAW_REQUEST_BYTES
    with patch("urllib.request.urlopen", capture):
        with pytest.raises(AgentError) as exc_info:
            bridge.complete(row["id"], raw_over_16m_msgs, [], lambda: False)
    assert str(exc_info.value) == "agent_context_limit"
    assert len(capture.calls) == 0

    # 7) Owner manual context byte limit (`max_context_chars=100000`) enforced on raw request bytes
    with pytest.raises(AgentError) as exc_info:
        output_policy.admit_request_context(
            row,
            _user_image_messages(PNG_3_46MIB),
            [],
            manual_limit=100_000,
        )
    assert str(exc_info.value) == "agent_context_limit"

    ws = tmp_path / "ws"
    ws.mkdir()

    class _Tools:
        def specs(self):
            return []

        def metadata(self, _):
            return {"effect": "read"}

    store = AgentStore(tmp_path / "agent", X25519PrivateKey.generate())
    runtime = AgentRuntime(store, bridge, _Tools(), bridge.catalog)
    runtime.update_settings(
        {
            "enabled": True,
            "model_id": row["id"],
            "workspace_root": str(ws),
            "max_context_chars": 100_000,
        }
    )
    task = runtime.create({"message": "Own manual byte cap fixture."})
    store.transaction(lambda d: d["tasks"][task["id"]].update(status="running"))
    selected = store.read()["tasks"][task["id"]]
    selected["_run_control_revision"] = selected["control_revision"]
    with patch("urllib.request.urlopen", capture):
        with pytest.raises(AgentError) as exc_info:
            runtime._complete(selected, _user_image_messages(PNG_3_46MIB), [], lambda: False, lambda _: None)
    assert str(exc_info.value) == "agent_context_limit"
    assert len(capture.calls) == 0


# ============================================================================
# 4. Exact Identity & Mutation / Malformed / Camouflage Rejection Tests
# ============================================================================


def test_profile_rejects_moved_duplicated_assistant_mutated_and_detail_changed_parts():
    _, _, row, _ = _make_bridge()
    messages = _user_image_messages(PNG_NEAR_8MIB)
    profile = prepare_internal_image_profile(
        messages,
        route_identity=row,
        request_identity="own-profile",
    )
    assert isinstance(profile, InternalImageProfile)

    mutations = {
        "moved": [{"role": "user", "content": [messages[0]["content"][1], messages[0]["content"][0]]}],
        "duplicate": [
            {
                "role": "user",
                "content": messages[0]["content"] + [copy.deepcopy(messages[0]["content"][1])],
            }
        ],
        "assistant": [{"role": "assistant", "content": copy.deepcopy(messages[0]["content"])}],
        "malformed": [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
            }
        ],
        "remote": [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}}],
            }
        ],
        "detail": [
            {
                "role": "user",
                "content": [
                    copy.deepcopy(messages[0]["content"][0]),
                    _image_part(PNG_NEAR_8MIB, detail="low"),
                ],
            }
        ],
        "mutated_bytes": [
            {
                "role": "user",
                "content": [
                    copy.deepcopy(messages[0]["content"][0]),
                    _image_part(PNG_3_46MIB, detail="high"),
                ],
            }
        ],
    }

    scope = "a" * 64
    with internal_image_scope(profile):
        # Exact match can be validated multiple times across bridge -> adapter -> codec
        validate_chat({"messages": messages, "max_tokens": 32})
        validate_chat({"messages": messages, "max_tokens": 32})
        make_request({"messages": messages, "max_tokens": 32}, RESPONSES, "own-model", scope)
        make_request({"messages": messages, "max_tokens": 32}, MESSAGES, "own-model", scope)

        for name, bad_messages in mutations.items():
            with pytest.raises(ValueError), pytest.MonkeyPatch.context():
                validate_chat({"messages": bad_messages, "max_tokens": 32})
            with pytest.raises(ValueError):
                make_request({"messages": bad_messages, "max_tokens": 32}, RESPONSES, "own-model", scope)


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://example.invalid/screen.png",
        "http://127.0.0.1/screen.png",
        "file:///C:/screen.png",
        "C:\\Users\\screen.png",
        "data:image/svg+xml;base64," + base64.b64encode(b"<svg></svg>").decode("ascii"),
        "data:image/png;base64,not_valid_base64$$$",
        "data:image/png;base64," + base64.b64encode(b"not-a-valid-png").decode("ascii"),
        "data:image/jpeg;base64," + base64.b64encode(PNG_SEED_SMALL).decode("ascii"),
    ],
)
def test_malformed_or_non_canonical_image_urls_rejected_with_zero_egress(bad_url: str):
    bridge, _, row, _ = _make_bridge("openai_compatible")
    capture = _EgressCapture()
    with patch("urllib.request.urlopen", capture):
        with pytest.raises((AgentError, AgentToolRejected)):
            bridge.complete(row["id"], _user_image_messages(bad_url), [], lambda: False)
    assert len(capture.calls) == 0


def test_prose_and_tool_result_base64_receive_no_image_exemption():
    fake_inline = _data_url(PNG_3_46MIB)
    prose_msgs = [{"role": "user", "content": fake_inline}]
    prose_analysis = output_policy.analyze_request(prose_msgs, [])
    assert prose_analysis["image_count"] == 0
    assert prose_analysis["text_projection_utf8_bytes"] == prose_analysis["raw_serialized_utf8_bytes"]

    tool_msgs = [
        {"role": "user", "content": "Capture screen"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "grab", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": fake_inline},
    ]
    tool_analysis = output_policy.analyze_request(tool_msgs, [])
    assert tool_analysis["image_count"] == 0
    assert tool_analysis["text_projection_utf8_bytes"] == tool_analysis["raw_serialized_utf8_bytes"]


# ============================================================================
# 5. Protocol Codec Non-Image 4 MiB Guards Unchanged Under Active Profile
# ============================================================================


def test_protocol_codecs_preserve_4mib_limits_for_non_image_projections_and_responses():
    for protocol in ("openai_responses", "anthropic_messages"):
        bridge, _, row, _ = _make_bridge(protocol)
        capture = _EgressCapture()
        cases = [
            (
                "nonimage_text_4mib",
                _user_image_messages(PNG_3_46MIB) + [{"role": "user", "content": "x" * (4 * 1024 * 1024 + 1)}],
                [],
            ),
            (
                "nonimage_json_data_camouflage",
                _user_image_messages(PNG_3_46MIB)
                + [{"role": "user", "content": json.dumps({"data": "x" * (4 * 1024 * 1024 + 1)})}],
                [],
            ),
            (
                "tool_schema_4mib",
                _user_image_messages(PNG_3_46MIB),
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "own_read",
                            "description": "x" * (4 * 1024 * 1024 + 1),
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            ),
            (
                "raw_request_16mib",
                [{"role": "user", "content": "x" * (16 * 1024 * 1024 + 1)}],
                [],
            ),
        ]
        for case_name, msgs, specs in cases:
            capture.calls.clear()
            with patch("urllib.request.urlopen", capture):
                with pytest.raises(AgentError), pytest.MonkeyPatch.context():
                    bridge.complete(row["id"], msgs, specs, lambda: False)
            assert len(capture.calls) == 0, f"unexpected egress for {protocol} {case_name}"

    _, _, row_resp, _ = _make_bridge("openai_responses")
    profile = prepare_internal_image_profile(
        _user_image_messages(PNG_NEAR_8MIB),
        route_identity=row_resp,
        request_identity="own-copy-limit",
    )
    scope = "a" * 64
    with internal_image_scope(profile):
        with pytest.raises(ValueError, match="native protocol payload limit exceeded"):
            _copy({"data": "x" * (4 * 1024 * 1024 + 1)})
        with pytest.raises(ValueError, match="native protocol payload limit exceeded"):
            parse_response(
                {
                    "id": "own",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": "ownmsg",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "x" * (4 * 1024 * 1024 + 1)}],
                        }
                    ],
                },
                RESPONSES,
                "own-model",
                scope,
            )
        decoder = StreamDecoder(RESPONSES, "own-model", scope)
        decoder.feed({"type": "response.created", "response": {"id": "own", "status": "in_progress"}})
        with pytest.raises(ValueError, match="invalid native text delta"):
            decoder.feed({"type": "response.output_text.delta", "delta": "x" * (4 * 1024 * 1024 + 1)})


# ============================================================================
# 6. Scope Hygiene: Exception, Cancel, Callback Re-entry & Thread Isolation
# ============================================================================


@pytest.mark.parametrize("protocol", ["openai_compatible", "openai_responses", "anthropic_messages"])
def test_scope_cleanup_on_callbacks_concurrent_threads_exceptions_and_cancellation(protocol: str):
    bridge, adapter, row, _ = _make_bridge(protocol, streaming=True)
    capture = _EgressCapture()
    checks: list[dict[str, object]] = []
    callback_seen: set[str] = set()
    thread_results: list[dict[str, object]] = []

    def callback(event: dict[str, object]) -> None:
        typ = str(event.get("type", "unknown"))
        if typ in callback_seen:
            return
        callback_seen.add(typ)
        ambient = current_internal_image_profile() is not None
        accepted = True
        try:
            validate_chat({"messages": _user_image_messages(PNG_NEAR_8MIB), "max_tokens": 32})
        except ValueError:
            accepted = False
        checks.append(
            {
                "event_type": typ,
                "ambient_profile": ambient,
                "ordinary_validate_accepted": accepted,
            }
        )

    def policy_callback(_: dict[str, object]) -> None:
        callback({"type": "on_request_policy"})

    entered = False

    def inside() -> None:
        nonlocal entered
        if entered:
            return
        entered = True

        def ordinary() -> None:
            ambient = current_internal_image_profile() is not None
            accepted = True
            err_msg = ""
            try:
                adapter.chat(
                    {"messages": _user_image_messages(PNG_NEAR_8MIB), "max_tokens": 32},
                    task_id="owned-ordinary-thread",
                    timeout_s=2,
                )
            except Exception as exc:
                accepted = False
                err_msg = str(exc)
            thread_results.append(
                {
                    "ambient_profile": ambient,
                    "accepted": accepted,
                    "error": err_msg,
                }
            )

        t = threading.Thread(target=ordinary)
        t.start()
        t.join(5)
        assert not t.is_alive()

    capture.inside = inside
    with patch("urllib.request.urlopen", capture):
        bridge.complete(
            row["id"],
            _user_image_messages(PNG_NEAR_8MIB),
            [],
            lambda: False,
            on_event=callback,
            on_request_policy=policy_callback,
        )

    assert checks and all(not x["ambient_profile"] and not x["ordinary_validate_accepted"] for x in checks)
    assert len(thread_results) == 1
    assert not thread_results[0]["ambient_profile"]
    assert not thread_results[0]["accepted"]
    assert len(capture.calls) == 1
    assert current_internal_image_profile() is None

    # Transport exception resets profile in finally
    capture.fail = True
    with patch("urllib.request.urlopen", capture):
        with pytest.raises(AgentError):
            bridge.complete(row["id"], _user_image_messages(PNG_NEAR_8MIB), [], lambda: False)
    assert current_internal_image_profile() is None

    # Pre-cancelled request resets profile and never dispatches
    capture.calls.clear()
    with patch("urllib.request.urlopen", capture):
        with pytest.raises(AgentError):
            bridge.complete(row["id"], _user_image_messages(PNG_NEAR_8MIB), [], lambda: True)
    assert current_internal_image_profile() is None
    assert len(capture.calls) == 0


# ============================================================================
# 7. Peer Route Boundary Tests
# ============================================================================


def test_peer_route_blocks_expanded_image_with_zero_execute_order_and_allows_legacy_small_image():
    bridge, _, row, services = _make_bridge()
    row.update(source="peer", peer_id="own-peer")
    calls: list[dict[str, object]] = []

    async def execute_order(request, *args, **kwargs):
        assert current_internal_image_profile() is None
        calls.append(copy.deepcopy(request))
        return {
            "state": "succeeded",
            "message": {"role": "assistant", "content": ANSWER},
            "finish_reason": "stop",
            "reported_usage": {"input_tokens": 12, "output_tokens": 4},
        }

    services["execute_order"] = execute_order
    services["cancel_order"] = lambda _: None

    # 1) Expanded local-sized image (> 128 KiB) must be refused before execute_order
    with pytest.raises(AgentToolRejected) as exc_info:
        bridge.complete(row["id"], _user_image_messages(PNG_3_46MIB), [], lambda: False)
    assert str(exc_info.value) == "agent_image_input_unsupported"
    assert exc_info.value.details.get("reason") == "peer_expanded_image_carrier_unsupported"
    assert len(calls) == 0

    # 2) Legacy-sized canonical image (<= 128 KiB, <= 180,000 chars) still dispatches to peer
    ok_legacy, reason = check_legacy_public_carrier_compatible(_user_image_messages(PNG_SEED_SMALL))
    assert ok_legacy and reason is None
    result = bridge.complete(row["id"], _user_image_messages(PNG_SEED_SMALL), [], lambda: False)
    assert result["message"]["content"] == ANSWER
    assert len(calls) == 1


# ============================================================================
# 8. Text-Only & Token Accounting / Policy Hash Non-Regression Tests
# ============================================================================


def test_text_only_and_canonical_image_token_accounting_and_policy_sha256_preserved():
    _, _, row, _ = _make_bridge()
    text_msgs = [{"role": "user", "content": "Plain text request for accounting verification."}]

    assert (
        prepare_internal_image_profile(
            text_msgs,
            route_identity=row,
            request_identity="own-text-only",
        )
        is None
    )

    text_policy = output_policy.select(row, text_msgs, [])
    assert text_policy["image_count"] == 0
    assert text_policy["estimated_image_tokens"] == 0
    assert text_policy["estimate_basis"] == output_policy.ESTIMATE_BASIS
    assert isinstance(text_policy["policy_sha256"], str) and len(text_policy["policy_sha256"]) == 64

    # 1280x900 canonical PNG: 3 * ceil(1280/16) * ceil(900/16) + 1024 = 3 * 80 * 57 + 1024 = 14704
    expected_img_tokens = output_policy.estimate_image_tokens(1280, 900)
    assert expected_img_tokens == 3 * 80 * 57 + 1024 == 14_704

    img_msgs = _user_image_messages(PNG_3_46MIB, text="Plain text request for accounting verification.")
    img_policy = output_policy.select(row, img_msgs, [])
    assert img_policy["image_count"] == 1
    assert img_policy["image_decoded_bytes"] == len(PNG_3_46MIB)
    assert img_policy["total_pixels"] == 1280 * 900
    assert img_policy["estimated_image_tokens"] == expected_img_tokens
    assert img_policy["estimated_input_tokens"] == img_policy["estimated_text_tokens"] + expected_img_tokens
    assert img_policy["estimate_basis"] == output_policy.IMAGE_ESTIMATE_BASIS
    assert img_policy["policy_sha256"] == text_policy["policy_sha256"]
    assert img_policy["text_projection_utf8_bytes"] < len(encoded(img_msgs))
