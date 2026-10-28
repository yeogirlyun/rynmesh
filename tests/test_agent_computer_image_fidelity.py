"""Tests for full-original Computer Use image fidelity, canonical image-aware request/budget analysis,
post-entry nested inspect error receipts, and native observation geometry coordinate mapping.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import random
import time
import uuid
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from PIL import Image, ImageDraw

from rynmesh.agent_runtime import budget, image_capabilities, output_policy
from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore, AgentToolRejected, encoded
from rynmesh.llm_package import codex_session, peer_binding
from rynmesh.llm_package.cli_images import image_inputs


def _make_png_bytes(
    width: int = 1280,
    height: int = 900,
    color=(20, 40, 80),
    *,
    noisy: bool = False,
    draw_chinese_ui_glyphs: bool = False,
    seed: int = 20261019,
) -> bytes:
    if noisy:
        # Deterministic incompressible pseudo-random RGB bytes so lossless PNG is >3 MiB for 1280x900
        # and ~5.77 MiB (between 4 MiB and 8 MiB) for 1600x1200.
        rng = random.Random(seed + width * 10007 + height)
        raw = rng.randbytes(width * height * 3)
        img = Image.frombytes("RGB", (width, height), raw)
    else:
        img = Image.new("RGB", (width, height), color)

    if draw_chinese_ui_glyphs:
        draw = ImageDraw.Draw(img)
        # Render synthetic high-contrast 1px/2px stroke patterns representing small Chinese UI labels ("确定" / "取消")
        # in the bottom-right region (x > 1000, y > 750) that would blur or vanish if downscaled to 512px.
        button_rect = (1080, 810, 1180, 846)
        draw.rectangle(button_rect, fill=(245, 245, 250), outline=(10, 10, 10), width=1)
        glyph_offsets = [
            # Left character strokes ("确")
            (1096, 818), (1097, 818), (1098, 818), (1099, 818), (1100, 818),
            (1098, 819), (1097, 821), (1096, 823), (1096, 825), (1096, 827),
            (1097, 823), (1099, 823), (1099, 825), (1097, 827), (1099, 827),
            (1104, 816), (1106, 818), (1103, 820), (1109, 820), (1105, 822),
            (1105, 824), (1105, 826), (1105, 828), (1108, 824), (1108, 828),
            # Right character strokes ("定")
            (1122, 816), (1123, 817), (1118, 819), (1128, 819), (1119, 819),
            (1120, 822), (1126, 822), (1123, 822), (1123, 824), (1123, 826),
            (1123, 828), (1125, 825), (1127, 825), (1120, 825), (1119, 828),
            (1121, 829), (1125, 830), (1129, 830),
        ]
        for gx, gy in glyph_offsets:
            img.putpixel((gx, gy), (220, 15, 25))
            img.putpixel((gx + 1, gy), (10, 200, 35))
        # Set intended distinct 1-pixel markers AFTER all glyph drawing at known >512 bottom-right coordinates
        img.putpixel((1096, 818), (220, 15, 25))
        img.putpixel((1097, 818), (10, 200, 35))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_jpeg_bytes(width: int = 1280, height: int = 900, quality: int = 92) -> bytes:
    img = Image.new("RGB", (width, height), (35, 95, 165))
    draw = ImageDraw.Draw(img)
    draw.rectangle((900, 650, 1210, 860), fill=(240, 180, 40), outline=(15, 15, 15), width=2)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
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
    destination = staticmethod(ModelCatalog.destination)

    def __init__(self, models):
        self._models = [copy.deepcopy(m) for m in models]

    def list_models(self, *, selected_model_id=None):
        out = []
        for row in self._models:
            enriched = copy.deepcopy(row)
            enriched.update(image_capabilities.catalog_image_fields(enriched))
            out.append(enriched)
        return out

    def get_model(self, model_id):
        return next((row for row in self.list_models(selected_model_id=model_id) if row["id"] == model_id), None)

    def resolve(self, model_id, *, require_tools=True):
        row = self.get_model(model_id)
        if not row or not row.get("available"):
            raise ValueError("agent_model_unavailable")
        if require_tools and not row.get("agent_supported"):
            raise ValueError("agent_model_tools_unsupported")
        return row


class LocalToolBridge:
    """Connects LocalTools and SPECS to AgentRuntime's tool interface."""

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


def _write_mcp_artifact(
    workspace: Path,
    task_id: str,
    operation_id: str,
    index: int = 0,
    *,
    image_bytes: bytes | None = None,
    ext: str = ".png",
    mime_type: str = "image/png",
) -> tuple[Path, dict]:
    raw = image_bytes if image_bytes is not None else _make_png_bytes(1280, 900)
    safe_task = image_capabilities.safe_segment(task_id, "task")
    safe_op = image_capabilities.safe_segment(operation_id, "op")
    rel_dir = Path(".ryn") / "mcp" / safe_task / safe_op
    full_dir = workspace / rel_dir
    full_dir.mkdir(parents=True, exist_ok=True)
    filename = f"image_{index}{ext}"
    file_path = full_dir / filename
    file_path.write_bytes(raw)
    with Image.open(io.BytesIO(raw)) as img:
        width, height = img.size
    artifact = {
        "index": index,
        "path": (rel_dir / filename).as_posix(),
        "mime_type": mime_type,
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


def test_full_original_image_fidelity_and_small_chinese_ui_pixels_preserved_across_inspect_mcp_and_codex_rpc(tmp_path, monkeypatch):
    """Verify >512px (1280x900) PNG and JPEG with fine 1px Chinese UI label strokes preserve exact original
    bytes, dimensions, MIME type, SHA-256, and pixel values without silent 512px JPEG downscaling across
    ryn_image_inspect, MCP observation materialization, API detail='high', and real codex_session._turn
    (both initial and resumed turns). Fake assistant text verifies host carrier/pixel transport fidelity only,
    not live model OCR/semantic pixel recognition.
    """
    png_bytes = _make_png_bytes(1280, 900, noisy=True, draw_chinese_ui_glyphs=True)
    assert 3_000_000 < len(png_bytes) <= image_capabilities.MAX_IMAGE_SOURCE_BYTES
    with Image.open(io.BytesIO(png_bytes)) as src_img:
        assert src_img.size == (1280, 900)
        assert src_img.getpixel((1096, 818)) == (220, 15, 25)
        assert src_img.getpixel((1097, 818)) == (10, 200, 35)
    png_sha = hashlib.sha256(png_bytes).hexdigest()
    expected_png_data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    jpeg_bytes = _make_jpeg_bytes(1280, 900)
    jpeg_sha = hashlib.sha256(jpeg_bytes).hexdigest()
    expected_jpeg_data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode("ascii")

    codex_row = _codex_model_row()
    runtime, bridge, tools, store, workspace = _setup_runtime_with_model(
        tmp_path,
        codex_row,
        _assistant(name="ryn_image_inspect", args={"path": "ui_1280x900.png", "question": "Read small Chinese button label"}, call_id="call_inspect_png"),
        _assistant("Synthetic fake-provider response for transport/pixel-fidelity check (not live OCR): 确定."),
        _assistant("Completed inspection."),
    )
    try:
        (workspace / "ui_1280x900.png").write_bytes(png_bytes)
        task = runtime.create({"message": "Inspect full resolution 1280x900 UI screenshot."})
        for _ in range(6):
            runtime.run_once()
            if runtime.get(task["id"])["status"] in {"completed", "failed"}:
                break

        current = runtime.get(task["id"])
        assert current["status"] == "completed", current.get("error")
        assert len(bridge.calls) == 3

        # 1. Inspect call outgoing message preserves exact 1280x900 PNG bytes and detail="high"
        inspect_messages = bridge.calls[1]["messages"]
        user_parts = inspect_messages[1]["content"]
        image_part = next(p for p in user_parts if isinstance(p, dict) and p.get("type") == "image_url")
        assert image_part["image_url"]["detail"] == "high"
        assert image_part["image_url"]["url"] == expected_png_data_url

        # Decode outgoing image and verify fine 1px Chinese UI glyph stroke pixels in bottom-right (>512px) are intact
        b64_payload = image_part["image_url"]["url"].split(",", 1)[1]
        submitted_raw = base64.b64decode(b64_payload)
        assert submitted_raw == png_bytes
        assert hashlib.sha256(submitted_raw).hexdigest() == png_sha
        with Image.open(io.BytesIO(submitted_raw)) as decoded_img:
            assert decoded_img.size == (1280, 900)
            assert decoded_img.format == "PNG"
            assert decoded_img.getpixel((1096, 818)) == (220, 15, 25)
            assert decoded_img.getpixel((1097, 818)) == (10, 200, 35)

        # Tool result reports original==submitted dimensions, format, bytes, and sha256 (with pixel_recognition_verified=False)
        tool_events = [e for e in current["events"] if e["type"] == "tool_result" and e.get("tool") == "ryn_image_inspect"]
        assert len(tool_events) == 1
        res = tool_events[0]["result"]
        assert res["width"] == 1280 and res["height"] == 900
        assert res["resized_to"] == [1280, 900]
        assert res["original_dimensions_preserved"] is True
        assert res["coordinate_origin"] == [0, 0]
        assert res["coordinate_scale"] == [1.0, 1.0]
        assert res["mime_type"] == "image/png" and res["payload_mime_type"] == "image/png"
        assert res["bytes"] == len(png_bytes) and res["payload_bytes"] == len(png_bytes)
        assert res["sha256"] == png_sha and res["payload_sha256"] == png_sha
        assert res["pixel_recognition_verified"] is False

        # 2. Verify real codex_session._turn constructs turn/start input with exact original PNG data URL on initial and resumed turns
        extracted = image_inputs(inspect_messages)
        assert extracted == [{"type": "image", "url": expected_png_data_url}]
        initial_inputs = []
        rpc_initial = _FixtureCodexRPC(pending=deque(_completed_codex_events("synthetic-turn-initial")))
        rpc_initial.call = lambda method, params: (
            initial_inputs.extend(params["input"]) or {"turn": {"id": "fixture-turn"}}
        )
        ans, _, _ = codex_session._turn(
            rpc_initial,
            "fixture-thread",
            {"messages": inspect_messages},
            "Read UI label",
            deadline=time.monotonic() + 2,
        )
        assert ans == "synthetic-turn-initial"
        assert initial_inputs == [
            {"type": "text", "text": "Read UI label", "text_elements": []},
            {"type": "image", "url": expected_png_data_url},
        ]

        resumed_inputs = []
        rpc_resumed = _FixtureCodexRPC(pending=deque(_completed_codex_events("synthetic-turn-resumed")))
        rpc_resumed.call = lambda method, params: (
            resumed_inputs.extend(params["input"]) or {"turn": {"id": "fixture-turn"}}
        )
        monkeypatch.setattr(
            "rynmesh.llm_package.cli_images.image_inputs",
            lambda _: pytest.fail("preparsed images must not be decoded again on resumed turn"),
        )
        resumed_ans, _, _ = codex_session._turn(
            rpc_resumed,
            "fixture-thread",
            {"_image_inputs": extracted},
            "Read UI label resumed",
            deadline=time.monotonic() + 2,
            resumed=True,
        )
        assert resumed_ans == "synthetic-turn-resumed"
        assert resumed_inputs == [
            {"type": "text", "text": "Read UI label resumed", "text_elements": []},
            {"type": "image", "url": expected_png_data_url},
        ]
        monkeypatch.undo()

        # 3. Verify MCP observation materialization preserves exact original 1280x900 JPEG bytes and detail="high"
        task2 = runtime.create({"message": "Materialize 1280x900 JPEG MCP observation."})
        t2_id = task2["id"]
        op_id = "op_mcp_full_jpeg"
        _, jpeg_artifact = _write_mcp_artifact(
            workspace, t2_id, op_id, index=0, image_bytes=jpeg_bytes, ext=".jpg", mime_type="image/jpeg"
        )
        bridge.responses.append(_assistant("Observed full 1280x900 JPEG."))
        store.transaction(lambda data: _append_confirmed_mcp_turn(data["tasks"][t2_id], "call_mcp_jpeg", op_id, [jpeg_artifact]))
        runtime.run_once()

        mcp_call = bridge.calls[-1]
        mcp_items = image_inputs(mcp_call["messages"])
        assert mcp_items == [{"type": "image", "url": expected_jpeg_data_url}]
        mcp_user_msg = mcp_call["messages"][-1]
        mcp_img_part = next(p for p in mcp_user_msg["content"] if isinstance(p, dict) and p.get("type") == "image_url")
        assert mcp_img_part["image_url"]["detail"] == "high"
        assert mcp_img_part["image_url"]["url"] == expected_jpeg_data_url

        stored_t2 = store.read()["tasks"][t2_id]
        mcp_link = stored_t2["image_request_links"][0]
        assert mcp_link["submitted_images"][0]["payload_sha256"] == jpeg_sha
        assert mcp_link["submitted_images"][0]["width"] == 1280
        assert mcp_link["submitted_images"][0]["height"] == 900
        assert mcp_link["submitted_images"][0]["mime_type"] == "image/jpeg"
        assert mcp_link["source_artifacts"][0]["coordinate_authority"] is False
        assert mcp_link["source_artifacts"][0]["provenance"] == "mcp_visual_only"
    finally:
        tools.close()


def test_canonical_request_analysis_text_compatibility_compression_invariance_and_camouflaged_inputs():
    """Verify:
    - Text-only requests produce identical estimate_input and output_policy.select values.
    - Equal-dimension images (1280x900 flat PNG vs noisy PNG vs JPEG) produce identical
      estimated_image_tokens and estimated_input_tokens while raw_serialized_utf8_bytes differs.
    - Larger image dimensions scale estimated_image_tokens higher.
    - Base64 strings inside prose, tool arguments, tool outputs, remote https URLs, malformed base64,
      corrupt image bytes, MIME mismatch, or non-user roles NEVER receive image text-projection exemption.
    """
    model = _api_model_row(context_window=128000)
    text_messages = [
        {"role": "system", "content": "You are a helpful coding assistant."},
        {"role": "user", "content": "Explain how output_policy.select works."},
    ]
    specs = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]

    # 1. Text-only invariance
    analysis_text = output_policy.analyze_request(text_messages, specs)
    raw_text_bytes = len(encoded(text_messages)) + len(encoded(specs))
    expected_text_tokens = (
        (raw_text_bytes + output_policy.ESTIMATED_BYTES_PER_TOKEN - 1) // output_policy.ESTIMATED_BYTES_PER_TOKEN
        + output_policy.FRAMING_RESERVE_TOKENS
    )
    assert analysis_text["image_count"] == 0
    assert analysis_text["invalid_image_parts"] == []
    assert analysis_text["raw_serialized_utf8_bytes"] == raw_text_bytes
    assert analysis_text["text_projection_utf8_bytes"] == raw_text_bytes
    assert analysis_text["projected_context_utf8_bytes"] == raw_text_bytes
    assert analysis_text["estimated_text_tokens"] == expected_text_tokens
    assert analysis_text["estimated_image_tokens"] == 0
    assert analysis_text["estimated_input_tokens"] == expected_text_tokens
    est_input = output_policy.estimate_input(text_messages, specs)
    assert est_input["estimated_input_utf8_bytes"] == raw_text_bytes
    assert est_input["estimated_input_tokens"] == expected_text_tokens
    selected_text = output_policy.select(model, text_messages, specs)
    assert selected_text["estimated_input_utf8_bytes"] == raw_text_bytes
    assert selected_text["estimated_input_tokens"] == expected_text_tokens
    assert selected_text["estimate_basis"] == output_policy.ESTIMATE_BASIS

    # 2. Equal dimensions (1280x900) across flat PNG, noisy PNG, and JPEG yield identical token estimates
    flat_png = _make_png_bytes(1280, 900, noisy=False)
    noisy_png = _make_png_bytes(1280, 900, noisy=True)
    jpeg_1280 = _make_jpeg_bytes(1280, 900)
    assert len(noisy_png) > 3_000_000
    assert len(noisy_png) > len(flat_png) * 50

    def _canonical_img_msg(mime: str, raw: bytes):
        b64 = base64.b64encode(raw).decode("ascii")
        return [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Inspect this desktop window"},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "high"}},
                ],
            }
        ]

    a_flat = output_policy.analyze_request(_canonical_img_msg("image/png", flat_png), [])
    a_noisy = output_policy.analyze_request(_canonical_img_msg("image/png", noisy_png), [])
    a_jpeg = output_policy.analyze_request(_canonical_img_msg("image/jpeg", jpeg_1280), [])

    expected_1280x900_tokens = 3 * ((1280 + 15) // 16) * ((900 + 15) // 16) + 1024  # 3 * 80 * 57 + 1024 = 14704
    assert a_flat["estimated_image_tokens"] == expected_1280x900_tokens
    assert a_noisy["estimated_image_tokens"] == expected_1280x900_tokens
    assert a_jpeg["estimated_image_tokens"] == expected_1280x900_tokens
    assert a_flat["text_projection_utf8_bytes"] == a_noisy["text_projection_utf8_bytes"] == a_jpeg["text_projection_utf8_bytes"]
    assert a_flat["estimated_input_tokens"] == a_noisy["estimated_input_tokens"] == a_jpeg["estimated_input_tokens"]
    assert a_noisy["raw_serialized_utf8_bytes"] > a_flat["raw_serialized_utf8_bytes"]

    # 3. Larger dimensions scale higher
    small_png = _make_png_bytes(320, 240, noisy=False)
    large_png = _make_png_bytes(1920, 1080, noisy=False)
    a_small = output_policy.analyze_request(_canonical_img_msg("image/png", small_png), [])
    a_large = output_policy.analyze_request(_canonical_img_msg("image/png", large_png), [])
    assert a_small["estimated_image_tokens"] < a_flat["estimated_image_tokens"] < a_large["estimated_image_tokens"]

    # 4. Camouflaged base64 in prose, tool arguments, and tool outputs retain full text byte accounting
    b64_blob = "data:image/png;base64," + base64.b64encode(noisy_png[:4096]).decode("ascii")
    prose_messages = [{"role": "user", "content": f"Here is raw text containing {b64_blob}"}]
    a_prose = output_policy.analyze_request(prose_messages, [])
    assert a_prose["image_count"] == 0
    assert a_prose["text_projection_utf8_bytes"] == a_prose["raw_serialized_utf8_bytes"]

    tool_messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "foo", "arguments": json.dumps({"img": b64_blob})}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"data": b64_blob})},
    ]
    a_tool = output_policy.analyze_request(tool_messages, [])
    assert a_tool["image_count"] == 0
    assert a_tool["text_projection_utf8_bytes"] == a_tool["raw_serialized_utf8_bytes"]

    # 5. Invalid/non-exempt image_url shapes (remote URL, malformed base64, corrupt bytes, MIME mismatch, assistant role)
    invalid_cases = [
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}],
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,not_valid_base64!!!"}}]}],
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(b"not-an-image").decode("ascii")}}]}],
        # Declared JPEG but actual PNG bytes -> mime_mismatch
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(small_png).decode("ascii")}}]}],
        # Assistant role with image_url part
        [{"role": "assistant", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(small_png).decode("ascii")}}]}],
    ]
    for bad_msgs in invalid_cases:
        a_bad = output_policy.analyze_request(bad_msgs, [])
        assert a_bad["image_count"] == 0
        assert len(a_bad["invalid_image_parts"]) == 1
        assert a_bad["text_projection_utf8_bytes"] == a_bad["raw_serialized_utf8_bytes"]
        with pytest.raises(AgentError, match="agent_image_invalid"):
            output_policy.select(model, bad_msgs, [])


def test_independent_budget_context_and_transport_gates_before_model_call(tmp_path):
    """Verify:
    - A 1280x900 noisy PNG (whose raw base64 is ~4.6 MB > 1.5M tokens if divided by 3) FITS a 32k token
      automatic context window because automatic admission uses projected_context_utf8_bytes.
    - Manual max_context_chars still rejects when raw_serialized_utf8_bytes > max_context_chars.
    - Automatic context window still rejects when projected_context_utf8_bytes > auto_limit.
    - Lifetime token budget reserve still rejects when estimated_input_tokens + effective_cap > remaining tokens.
    - Independent transport limits (image count > 4, single/total pixels > 16M, decoded bytes) reject before model call.
    - Direct AgentModelBridge.complete preflight enforces route image support and output_policy bounds.
    """
    noisy_1280 = _make_png_bytes(1280, 900, noisy=True)
    assert 3_000_000 < len(noisy_1280) <= image_capabilities.MAX_IMAGE_SOURCE_BYTES

    # 1. Fits 32k context_window model under automatic context admission (no manual max_context_chars)
    model_32k = _api_model_row(context_window=32768)
    runtime, bridge, tools, store, workspace = _setup_runtime_with_model(
        tmp_path,
        model_32k,
        _assistant("Observed 1280x900 noisy screenshot within 32k context window."),
    )
    try:
        task = runtime.create({"message": "Observe 1280x900 noisy PNG."})
        task_id = task["id"]
        op_id = "op_fits_32k"
        _, artifact = _write_mcp_artifact(workspace, task_id, op_id, index=0, image_bytes=noisy_1280)
        store.transaction(lambda data: _append_confirmed_mcp_turn(data["tasks"][task_id], "call_fits", op_id, [artifact]))
        runtime.run_once()

        done_task = runtime.get(task_id)
        assert done_task["status"] == "completed", done_task.get("error")
        assert len(bridge.calls) == 1
        pol = output_policy.select(model_32k, bridge.calls[0]["messages"], bridge.calls[0]["tool_specs"])
        ctx_budget_32k = output_policy.context_budget(model_32k)
        assert ctx_budget_32k["mode"] == "auto"
        assert pol["image_count"] == 1
        assert pol["raw_serialized_utf8_bytes"] > 4_000_000
        assert pol["text_projection_utf8_bytes"] < pol["raw_serialized_utf8_bytes"] - 4_000_000
        assert pol["estimated_text_tokens"] == (
            (pol["text_projection_utf8_bytes"] + output_policy.ESTIMATED_BYTES_PER_TOKEN - 1)
            // output_policy.ESTIMATED_BYTES_PER_TOKEN
            + output_policy.FRAMING_RESERVE_TOKENS
        )
        assert pol["estimated_image_tokens"] == 14704
        assert pol["estimated_input_tokens"] == pol["estimated_text_tokens"] + pol["estimated_image_tokens"]
        assert pol["projected_context_utf8_bytes"] == (
            pol["text_projection_utf8_bytes"]
            + pol["estimated_image_tokens"] * output_policy.ESTIMATED_BYTES_PER_TOKEN
        )
        assert pol["projected_context_utf8_bytes"] <= ctx_budget_32k["effective_max_context_bytes"] < pol["raw_serialized_utf8_bytes"]
        assert pol["effective_cap"] == ctx_budget_32k["reserved_output_tokens"]
        assert pol["estimated_input_tokens"] + pol["effective_cap"] <= ctx_budget_32k["context_window_tokens"]
        assert pol["image_token_count_is_heuristic"] is True
        assert pol["provider_tokenizer_verified"] is False

        # 2. Manual max_context_chars rejects based on RAW serialized UTF-8 bytes before model dispatch
        runtime_manual, bridge_manual, tools_manual, store_manual, workspace_manual = _setup_runtime_with_model(
            tmp_path,
            model_32k,
            _assistant("Should not be called"),
            max_context_chars=100_000,
        )
        try:
            t_man = runtime_manual.create({"message": "Manual cap check"})
            _, art_man = _write_mcp_artifact(workspace_manual, t_man["id"], "op_man", index=0, image_bytes=noisy_1280)
            store_manual.transaction(lambda data: _append_confirmed_mcp_turn(data["tasks"][t_man["id"]], "call_man", "op_man", [art_man]))
            runtime_manual.run_once()
            failed_man = runtime_manual.get(t_man["id"])
            assert failed_man["status"] == "failed"
            assert failed_man["error"] == "agent_context_limit"
            assert bridge_manual.calls == []
            assert (store_manual.read()["tasks"][t_man["id"]].get("image_request_links") or []) == []
        finally:
            tools_manual.close()

        # 3. Automatic context gate rejects when projected_context_utf8_bytes exceeds small context window (8192 tokens)
        model_8k = _api_model_row(context_window=8192)
        runtime_auto, bridge_auto, tools_auto, store_auto, workspace_auto = _setup_runtime_with_model(
            tmp_path,
            model_8k,
            _assistant("Should not be called"),
        )
        try:
            t_auto = runtime_auto.create({"message": "Auto cap check"})
            _, art_auto = _write_mcp_artifact(workspace_auto, t_auto["id"], "op_auto", index=0, image_bytes=noisy_1280)
            store_auto.transaction(lambda data: _append_confirmed_mcp_turn(data["tasks"][t_auto["id"]], "call_auto", "op_auto", [art_auto]))
            runtime_auto.run_once()
            failed_auto = runtime_auto.get(t_auto["id"])
            assert failed_auto["status"] == "failed"
            assert failed_auto["error"] == "agent_context_limit"
            assert bridge_auto.calls == []
            assert (store_auto.read()["tasks"][t_auto["id"]].get("image_request_links") or []) == []
        finally:
            tools_auto.close()

        # 4. Lifetime token budget reserve rejects before model dispatch when remaining budget < estimated_input_tokens + effective_cap
        t_budget = runtime.create({"message": "Lifetime budget reserve check"})
        tb_id = t_budget["id"]
        _, art_b = _write_mcp_artifact(workspace, tb_id, "op_b", index=0, image_bytes=noisy_1280)

        def _exhaust_most_budget(data):
            row = data["tasks"][tb_id]
            row["status"] = "running"
            state = budget.initialize(row)
            max_tokens = state["limits"]["max_total_tokens"]
            # Leave only 10,000 tokens remaining (< 14,704 image tokens + output cap)
            reservation = budget.reserve(
                row,
                "model",
                "fixture",
                input_bound=max_tokens - 10000,
                output_bound=0,
                seconds_bound=0,
            )
            budget.settle(
                row,
                reservation["id"],
                usage={"input_tokens": max_tokens - 10000, "output_tokens": 0},
                elapsed_seconds=0.0,
            )
            _append_confirmed_mcp_turn(row, "call_b", "op_b", [art_b])

        store.transaction(_exhaust_most_budget)
        bridge.calls.clear()
        selected_b = store.read()["tasks"][tb_id]
        selected_b["_run_started"] = 100.0
        selected_b["_run_lifetime_remaining"] = 300.0
        selected_b["_run_control_revision"] = selected_b["control_revision"]
        with pytest.raises(AgentError, match="agent_token_budget_exhausted"):
            runtime._complete(selected_b, selected_b["messages"], [], lambda: False, lambda _e: None)
        assert bridge.calls == []
        assert (store.read()["tasks"][tb_id].get("image_request_links") or []) == []

        # 5. Independent transport limits: >4 images rejected with agent_context_limit
        small_b64 = base64.b64encode(_make_png_bytes(64, 64)).decode("ascii")
        five_images_msg = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{small_b64}"}}
                    for _ in range(5)
                ],
            }
        ]
        with pytest.raises(AgentError, match="agent_context_limit"):
            output_policy.select(model_32k, five_images_msg, [])

        # 6. Real AgentModelBridge.complete preflight enforces route image support and output_policy bounds
        neg_row = _api_model_row(model_id="local/api-src-neg", supports_image_input=False)
        neg_catalog = StaticCatalog([neg_row])
        real_neg_bridge = AgentModelBridge({"provider_for": lambda _sid: None}, neg_catalog)
        one_image_msg = [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{small_b64}"}}],
            }
        ]
        with pytest.raises(AgentToolRejected, match="agent_image_input_unsupported"):
            real_neg_bridge.complete(neg_row["id"], one_image_msg, [], lambda: False)

        pos_catalog = StaticCatalog([model_32k])
        real_pos_bridge = AgentModelBridge({"provider_for": lambda _sid: None}, pos_catalog)
        with pytest.raises(AgentError, match="agent_context_limit"):
            real_pos_bridge.complete(model_32k["id"], five_images_msg, [], lambda: False)
    finally:
        tools.close()


def test_nested_inspect_post_entry_exception_records_attempted_link_while_pre_entry_refusal_records_none(tmp_path):
    """Verify P2 nested inspect receipt semantics:
    - Provider exception after model dispatch entry records exactly 1 attempted/unknown image_request_link
      (dispatch_status='attempted', image_submitted=None, image_dispatch_attempted=True, matching task_id/operation_id/request_id)
      and does not retry on TypeError.
    - Pre-entry context/budget refusal before model dispatch records zero image_request_links and zero model calls.
    """
    api_row = _api_model_row(context_window=128000)

    def _fail_post_entry(_model_id, _messages, _specs, _options):
        raise RuntimeError("provider transport dropped after request entry")

    runtime, bridge, tools, store, workspace = _setup_runtime_with_model(
        tmp_path,
        api_row,
        _fail_post_entry,
    )
    try:
        png_bytes = _make_png_bytes(800, 600)
        (workspace / "window.png").write_bytes(png_bytes)
        task = runtime.create({"message": "Nested inspect post-entry failure check"})
        task_id = task["id"]
        store.transaction(lambda data: data["tasks"][task_id].update(status="running"))
        selected = store.read()["tasks"][task_id]
        selected["_run_control_revision"] = selected["control_revision"]
        context = runtime._tool_context(selected, "op_nested_post_entry")

        with pytest.raises(RuntimeError, match="provider transport dropped after request entry"):
            tools.execute(
                "ryn_image_inspect",
                {"path": "window.png", "question": "Check window state"},
                operation_id="op_nested_post_entry",
                cancelled=lambda: False,
                context=context,
            )

        assert len(bridge.calls) == 1
        nested_req_id = bridge.calls[0]["options"]["request_id"]
        persisted = store.read()
        assert "data:image/" not in json.dumps(persisted)
        stored_task = persisted["tasks"][task_id]
        links = stored_task.get("image_request_links") or []
        assert len(links) == 1
        link = links[0]
        assert link["task_id"] == task_id
        assert link["operation_id"] == "op_nested_post_entry"
        assert link["request_id"] == nested_req_id
        assert link["stage"] == "image_inspection"
        assert link["dispatch_status"] == "attempted"
        assert link["image_submitted"] is None
        assert link["image_dispatch_attempted"] is True
        assert link["semantic_response_received"] is False
        assert link["source_artifacts"][0]["sha256"] == hashlib.sha256(png_bytes).hexdigest()

        # Pre-entry refusal: use permitted manual max_context_chars (4096 in allowed 4000..2_000_000 range)
        # and a noisy 128x128 PNG (~49 KB raw / ~65 KB base64) so admit_request_context refuses before model entry.
        runtime_pre, bridge_pre, tools_pre, store_pre, workspace_pre = _setup_runtime_with_model(
            tmp_path,
            api_row,
            _assistant("Unreachable"),
            max_context_chars=4096,
        )
        try:
            noisy_small_png = _make_png_bytes(128, 128, noisy=True)
            assert len(noisy_small_png) > 30_000
            (workspace_pre / "window_noisy.png").write_bytes(noisy_small_png)
            t_pre = runtime_pre.create({"message": "Pre-entry refusal check"})
            tp_id = t_pre["id"]
            store_pre.transaction(lambda data: data["tasks"][tp_id].update(status="running"))
            sel_pre = store_pre.read()["tasks"][tp_id]
            sel_pre["_run_control_revision"] = sel_pre["control_revision"]
            ctx_pre = runtime_pre._tool_context(sel_pre, "op_nested_pre_refusal")

            with pytest.raises(AgentError, match="agent_context_limit"):
                tools_pre.execute(
                    "ryn_image_inspect",
                    {"path": "window_noisy.png"},
                    operation_id="op_nested_pre_refusal",
                    cancelled=lambda: False,
                    context=ctx_pre,
                )
            assert bridge_pre.calls == []
            assert (store_pre.read()["tasks"][tp_id].get("image_request_links") or []) == []
        finally:
            tools_pre.close()
    finally:
        tools.close()


def test_native_operation_pixels_use_owner_registry_and_keep_mcp_scope_strict(tmp_path):
    task_id, operation_id = "task_native_delivery", "op_native_read"
    raw = _make_png_bytes(80, 60)
    digest = hashlib.sha256(raw).hexdigest()
    rel = f".ryn/computer/{task_id}/{operation_id}/image_1.png"
    target = tmp_path / rel
    target.parent.mkdir(parents=True)
    target.write_bytes(raw)
    wref = {"workerInstance": "worker_delivery", "windowKey": "window_delivery",
            "hwnd": "123", "pid": 456, "processStarted": "789", "generation": 1}
    descriptor = image_capabilities._issue_native_observation_descriptor(
        task_id=task_id, operation_id=operation_id, observation_id="obs_delivery",
        state_token="state_delivery", window_ref=wref, source_sha256=digest,
        source_width=80, source_height=60, source_bytes=len(raw), dpi=96, dpi_scale=1.0,
        window_rect={"x": 0, "y": 0, "width": 80, "height": 60},
        client_rect={"x": 0, "y": 0, "width": 80, "height": 60},
        physical_origin=[0, 0], screen_origin=[0, 0],
    )
    artifact = {"id": "art_delivery", "path": rel, "sha256": digest, "bytes": len(raw),
                "mime_type": "image/png", "width": 80, "height": 60,
                "observation_id": "obs_delivery", "state_token": "state_delivery",
                "window_ref": wref, "native_descriptor": descriptor,
                "provenance": "native_computer_observation", "coordinate_authority": True}
    result = {"native_observation_artifacts": [artifact], "images": [{
        k: v for k, v in artifact.items() if k != "native_descriptor"}]}
    row = {"id": task_id, "goal": {"revision": 1}, "scope_revision": 1,
           "context_epoch": 0, "local_execution": {"workspace_root": str(tmp_path)},
           "receipts": [{"operation_id": operation_id, "call_id": "call_delivery",
                         "status": "confirmed", "goal_revision": 1, "scope_revision": 1,
                         "context_epoch": 0, "result": result}]}
    messages = [_assistant(name="computer_read_window", call_id="call_delivery")["message"],
                {"role": "tool", "tool_call_id": "call_delivery", "content": json.dumps(result)}]
    original = copy.deepcopy(messages)
    calls = []

    def loader(**bindings):
        calls.append(bindings)
        assert bindings == {"task_id": task_id, "artifact_id": "art_delivery",
                            "expected_operation_id": operation_id,
                            "expected_state_token": "state_delivery",
                            "expected_observation_id": "obs_delivery", "expected_window_ref": wref}
        # Use the actual native loader's flat return shape and exact byte checks.
        return image_capabilities.verify_and_load_native_observation_artifact(
            artifact, descriptor=descriptor, workspace_root=tmp_path, task_id=task_id,
            operation_id=operation_id, current_state_token="state_delivery",
            expected_observation_id="obs_delivery", expected_window_ref=wref)

    projected, report = image_capabilities.materialize_operation_images(
        row, messages, model_row=_codex_model_row(), model_client=CapturingModelBridge(),
        native_artifact_loader=loader)
    assert report["materialized_count"] == 1 and len(calls) == 1
    parts = projected[-1]["content"]
    assert base64.b64decode(parts[1]["image_url"]["url"].split(",", 1)[1]) == raw
    assert "_host_seal" not in json.dumps(report)
    assert "native_descriptor" not in parts[0]["text"]
    assert messages == original

    _, absent = image_capabilities.materialize_operation_images(
        row, messages, model_row=_codex_model_row(), model_client=CapturingModelBridge())
    assert absent["materialized_count"] == 0
    assert absent["rejected_artifacts"][0]["reason"] == "native_artifact_loader_unavailable"

    def stale(**bindings):
        raise ValueError("native_descriptor_stale_state_token")

    _, rejected = image_capabilities.materialize_operation_images(
        row, messages, model_row=_codex_model_row(), model_client=CapturingModelBridge(),
        native_artifact_loader=stale)
    assert rejected["materialized_count"] == 0
    assert rejected["rejected_artifacts"][0]["reason"] == "native_descriptor_stale_state_token"
    with pytest.raises(ValueError, match="artifact_path_outside_task_operation_scope"):
        image_capabilities.verify_and_load_mcp_image_artifact(
            artifact, workspace_root=tmp_path, task_id=task_id, operation_id=operation_id)


def test_runtime_native_image_loader_requires_its_own_store():
    from rynmesh.agent_runtime.native_computer import NativeComputerFacade

    store, other = SimpleNamespace(), SimpleNamespace()
    runtime = object.__new__(AgentRuntime)
    runtime.store = store
    runtime.tools = SimpleNamespace(native_computer=NativeComputerFacade(other))
    assert runtime._native_artifact_loader() is None
    store._ryn_native_computer = runtime.tools.native_computer
    assert runtime._native_artifact_loader() is None
    store._ryn_native_computer = NativeComputerFacade(store)
    assert runtime._native_artifact_loader().__self__ is store._ryn_native_computer


def test_native_8mib_vs_mcp_4mib_provenance_and_computer_image_geometry_mapping(tmp_path):
    """Verify Geometry03 trust contract:
    - 1600x1200 noisy PNG (~5.77 MiB, 4 MiB < size <= 8 MiB) is rejected by generic MCP
      verify_and_load_mcp_image_artifact and verify_original_image_bytes (4 MiB cap),
      but accepted by host-sealed verify_and_load_native_observation_artifact (up to 8 MiB cap).
    - >8 MiB image is rejected even on native observation descriptors/artifacts.
    - Public validate_native_observation_descriptor never issues HMAC seals; unsealed dicts,
      guest/MCP relabeling, or tampered _host_seal are rejected.
    - Loader and resolver require explicit expected_task_id, expected_operation_id,
      expected_observation_id, current_state_token, and full WindowRef (workerInstance,
      windowKey, hwnd, pid, processStarted, generation).
    - resolve_computer_image_point maps:
      * Full original screenshot (origin=(0,0), scale=(1,1)) with attached 5.77 MiB payload verified
        (attached_pixels_verified=True) as well as pure math mode (attached_pixels_verified=False)
      * Negative multi-monitor coordinates (e.g. x=-1920, y=-120)
      * Non-100% DPI (e.g. 144 DPI / 150% scale) without double DPI multiplication
      * Client-area border offset (physical_origin=[8, 31], screen_origin=[-1912, -89])
      * Lossless crop/tile source_rect + payload_sha256 binding (distinct from source_sha256)
      * Scaled presentation rejected by artifact loader while pure math mode maps active region
        with attached_pixels_verified=False and rejects padding/letterbox target points
      * Stale or mismatched task_id, operation_id, observation_id, state_token, worker_instance_id,
        or window_ref (hwnd, windowKey, pid, processStarted, generation) fail closed.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id = "task_native_geom"
    op_id = "op_native_capture_1"

    # Generate a valid ~5.77 MiB 1600x1200 noisy PNG (strictly between 4 MiB MCP cap and 8 MiB native cap)
    png_5mib = _make_png_bytes(1600, 1200, noisy=True)
    png_5mib_sha = hashlib.sha256(png_5mib).hexdigest()
    assert image_capabilities.MAX_IMAGE_SOURCE_BYTES < len(png_5mib) <= image_capabilities.MAX_NATIVE_IMAGE_SOURCE_BYTES

    # 1. Generic 4 MiB cap rejects 5.77 MiB PNG in verify_original_image_bytes and verify_and_load_mcp_image_artifact
    with pytest.raises(ValueError, match="artifact_file_too_large"):
        image_capabilities.verify_original_image_bytes(png_5mib)

    _, mcp_5m_artifact = _write_mcp_artifact(workspace, task_id, op_id, index=0, image_bytes=png_5mib)
    with pytest.raises(ValueError, match="artifact_bytes_invalid"):
        image_capabilities.verify_and_load_mcp_image_artifact(
            mcp_5m_artifact, workspace_root=workspace, task_id=task_id, operation_id=op_id
        )

    # 2. Unsealed self-authored dicts, fake MCP/guest relabeling, or forged _host_seal claiming native provenance are rejected
    safe_task = image_capabilities.safe_segment(task_id, "task")
    safe_op = image_capabilities.safe_segment(op_id, "op")
    rel_native = Path(".ryn") / "computer" / safe_task / safe_op / "image_0.png"
    abs_native = workspace / rel_native
    abs_native.parent.mkdir(parents=True, exist_ok=True)
    abs_native.write_bytes(png_5mib)

    wref = {
        "workerInstance": "worker_win32_1",
        "windowKey": "win-key-001204a0-4242-1",
        "hwnd": "0x001204a0",
        "pid": 4242,
        "processStarted": "133740000000000000",
        "generation": 1,
    }
    base_desc_kwargs = {
        "task_id": task_id,
        "operation_id": op_id,
        "observation_id": "obs_1",
        "state_token": "tok_current_1",
        "window_ref": wref,
        "source_sha256": png_5mib_sha,
        "source_width": 1600,
        "source_height": 1200,
        "source_bytes": len(png_5mib),
        "dpi": 144,
        "dpi_scale": 1.5,
        "window_rect": {"x": -1920, "y": -120, "width": 1616, "height": 1239},
        "client_rect": {"x": -1912, "y": -89, "width": 1600, "height": 1200},
        "physical_origin": [8, 31],
        "screen_origin": [-1912, -89],
    }
    expected_bindings = {
        "expected_task_id": task_id,
        "expected_operation_id": op_id,
        "expected_observation_id": "obs_1",
        "current_state_token": "tok_current_1",
        "expected_window_ref": wref,
        "expected_worker_instance_id": "worker_win32_1",
    }

    # Public validate_native_observation_descriptor refuses unsealed dictionary without signing
    unsealed_self_authored = {
        **copy.deepcopy(base_desc_kwargs),
        "provenance": "native_computer_observation",
        "coordinate_authority": True,
    }
    with pytest.raises(ValueError, match="native_descriptor_missing_host_seal"):
        image_capabilities.validate_native_observation_descriptor(
            unsealed_self_authored,
            **expected_bindings,
        )

    # Untrusted MCP/guest markers and .ryn/mcp paths are rejected
    with pytest.raises(ValueError, match="native_descriptor_untrusted_source"):
        image_capabilities.validate_native_observation_descriptor(
            {**base_desc_kwargs, "_untrusted_mcp_artifact": True},
            **expected_bindings,
        )
    with pytest.raises(ValueError, match="native_descriptor_untrusted_source"):
        image_capabilities._issue_native_observation_descriptor(
            **{**base_desc_kwargs, "path": mcp_5m_artifact["path"]}
        )

    # Forged _host_seal is rejected
    with pytest.raises(ValueError, match="native_descriptor_tampered_after_seal"):
        image_capabilities.validate_native_observation_descriptor(
            {
                **base_desc_kwargs,
                "provenance": "native_computer_observation",
                "coordinate_authority": True,
                "_host_seal": "0" * 64,
            },
            **expected_bindings,
        )

    # 3. Host-sealed native descriptor validates and loads 5.77 MiB native observation artifact
    sealed_descriptor = image_capabilities._issue_native_observation_descriptor(**base_desc_kwargs)
    native_artifact = {
        "path": rel_native.as_posix(),
        "bytes": len(png_5mib),
        "sha256": png_5mib_sha,
        "mime_type": "image/png",
        "width": 1600,
        "height": 1200,
    }
    loaded_native = image_capabilities.verify_and_load_native_observation_artifact(
        native_artifact,
        descriptor=sealed_descriptor,
        workspace_root=workspace,
        task_id=task_id,
        operation_id=op_id,
        expected_observation_id="obs_1",
        current_state_token="tok_current_1",
        expected_window_ref=wref,
        expected_worker_instance_id="worker_win32_1",
    )
    assert loaded_native["provenance"] == "native_computer_observation"
    assert loaded_native["coordinate_authority"] is True
    assert loaded_native["width"] == 1600 and loaded_native["height"] == 1200
    assert loaded_native["bytes"] == len(png_5mib)
    assert loaded_native["sha256"] == png_5mib_sha
    assert loaded_native["payload_sha256"] == png_5mib_sha

    # 4. >8 MiB image is rejected even on sealed native observation descriptor
    oversized_len = image_capabilities.MAX_NATIVE_IMAGE_SOURCE_BYTES + 1024
    with pytest.raises(ValueError, match="native_descriptor_source_bytes_exceeded"):
        image_capabilities._issue_native_observation_descriptor(
            **{**base_desc_kwargs, "source_bytes": oversized_len}
        )

    # 5. Geometry mapping on full original image with negative secondary monitor coordinates, 144 DPI (1.5x), and client border offset
    # Window rect is (-1920, -120), physical_origin is [8, 31] -> screen_origin is (-1912, -89) == client_rect origin.
    # Physical pixel (300, 200) -> window_point (308, 231), screen_point (-1612, 111), client_point (300, 200) — NO double 1.5x multiplication!
    mapped_verified = image_capabilities.resolve_computer_image_point(
        sealed_descriptor,
        300,
        200,
        loaded_artifact=loaded_native,
        require_client_area=True,
        **expected_bindings,
    )
    assert mapped_verified["image_point"] == (300, 200)
    assert mapped_verified["source_point"] == (300, 200)
    assert mapped_verified["window_point"] == (308, 231)
    assert mapped_verified["client_point"] == (300, 200)
    assert mapped_verified["screen_point"] == (-1612, 111)
    assert mapped_verified["within_client_area"] is True
    assert mapped_verified["dpi"] == 144
    assert mapped_verified["dpi_scale"] == 1.5
    assert mapped_verified["dpi_applied_in_capture"] is True
    assert mapped_verified["attached_pixels_verified"] is True

    mapped_pure_math = image_capabilities.resolve_computer_image_point(
        sealed_descriptor,
        300,
        200,
        require_client_area=True,
        **expected_bindings,
    )
    assert mapped_pure_math["screen_point"] == (-1612, 111)
    assert mapped_pure_math["attached_pixels_verified"] is False

    # 6. Lossless crop descriptor + artifact loading + payload_sha256 binding
    crop_descriptor = image_capabilities._issue_native_observation_descriptor(
        task_id=task_id,
        operation_id=op_id,
        observation_id="obs_crop",
        state_token="tok_crop_2",
        window_ref=wref,
        source_sha256=png_5mib_sha,
        source_width=1600,
        source_height=1200,
        source_bytes=len(png_5mib),
        source_image_bytes=png_5mib,
        dpi=96,
        dpi_scale=1.0,
        window_rect={"x": 100, "y": 200, "width": 1616, "height": 1239},
        client_rect={"x": 108, "y": 231, "width": 1600, "height": 1200},
        physical_origin=[8, 31],
        screen_origin=[108, 231],
        representation={
            "kind": "crop",
            "image_width": 400,
            "image_height": 300,
            "source_rect": {"x": 400, "y": 300, "width": 400, "height": 300},
            "origin": [400, 300],
            "scale": [1.0, 1.0],
            "padding": {"left": 0, "top": 0, "right": 0, "bottom": 0},
        },
    )
    assert crop_descriptor["representation"]["payload_sha256"] != png_5mib_sha
    loaded_crop = image_capabilities.verify_and_load_native_observation_artifact(
        native_artifact,
        descriptor=crop_descriptor,
        workspace_root=workspace,
        task_id=task_id,
        operation_id=op_id,
        expected_observation_id="obs_crop",
        current_state_token="tok_crop_2",
        expected_window_ref=wref,
        expected_worker_instance_id="worker_win32_1",
    )
    assert loaded_crop["width"] == 400 and loaded_crop["height"] == 300
    assert loaded_crop["payload_sha256"] == crop_descriptor["representation"]["payload_sha256"]
    assert loaded_crop["source_sha256"] == png_5mib_sha

    mapped_crop = image_capabilities.resolve_computer_image_point(
        crop_descriptor,
        100,
        50,
        loaded_artifact=loaded_crop,
        expected_task_id=task_id,
        expected_operation_id=op_id,
        expected_observation_id="obs_crop",
        current_state_token="tok_crop_2",
        expected_window_ref=wref,
        expected_worker_instance_id="worker_win32_1",
    )
    assert mapped_crop["source_point"] == (500, 350)
    assert mapped_crop["client_point"] == (500, 350)
    assert mapped_crop["window_point"] == (508, 381)
    assert mapped_crop["screen_point"] == (608, 581)
    assert mapped_crop["attached_pixels_verified"] is True

    # Scaled descriptor: loader refuses scaled presentation, while pure math resolver maps active region
    # (with attached_pixels_verified=False) and rejects points inside padding/letterbox regions
    padded_scaled_descriptor = image_capabilities._issue_native_observation_descriptor(
        task_id=task_id,
        operation_id=op_id,
        observation_id="obs_scaled",
        state_token="tok_scaled_3",
        window_ref=wref,
        source_sha256=png_5mib_sha,
        source_width=1600,
        source_height=1200,
        source_bytes=len(png_5mib),
        dpi=96,
        dpi_scale=1.0,
        window_rect={"x": 100, "y": 200, "width": 1616, "height": 1239},
        client_rect={"x": 108, "y": 231, "width": 1600, "height": 1200},
        physical_origin=[8, 31],
        screen_origin=[108, 231],
        representation={
            "kind": "scaled",
            "image_width": 820,
            "image_height": 640,
            "source_rect": {"x": 0, "y": 0, "width": 1600, "height": 1200},
            "origin": [0, 0],
            "scale": [2.0, 2.0],
            "padding": {"left": 10, "top": 20, "right": 10, "bottom": 20},
        },
    )
    with pytest.raises(ValueError, match="native_descriptor_scaled_presentation_unsupported"):
        image_capabilities.verify_and_load_native_observation_artifact(
            native_artifact,
            descriptor=padded_scaled_descriptor,
            workspace_root=workspace,
            task_id=task_id,
            operation_id=op_id,
            expected_observation_id="obs_scaled",
            current_state_token="tok_scaled_3",
            expected_window_ref=wref,
            expected_worker_instance_id="worker_win32_1",
        )

    mapped_scaled_math = image_capabilities.resolve_computer_image_point(
        padded_scaled_descriptor,
        110,
        120,
        expected_task_id=task_id,
        expected_operation_id=op_id,
        expected_observation_id="obs_scaled",
        current_state_token="tok_scaled_3",
        expected_window_ref=wref,
        expected_worker_instance_id="worker_win32_1",
    )
    assert mapped_scaled_math["source_point"] == (200, 200)
    assert mapped_scaled_math["attached_pixels_verified"] is False

    with pytest.raises(ValueError, match="coordinate_target_in_padding_or_letterbox"):
        image_capabilities.resolve_computer_image_point(
            padded_scaled_descriptor,
            5,
            120,
            expected_task_id=task_id,
            expected_operation_id=op_id,
            expected_observation_id="obs_scaled",
            current_state_token="tok_scaled_3",
            expected_window_ref=wref,
            expected_worker_instance_id="worker_win32_1",
        )

    # 7. Stale or mismatched identity bindings (state_token, window_ref fields, worker_instance_id, task_id, operation_id, observation_id) fail closed
    for kwargs, expected_err in [
        ({"current_state_token": "tok_stale_999"}, "native_descriptor_stale_state_token"),
        ({"expected_window_ref": {**wref, "hwnd": "0xdead"}}, "native_descriptor_window_mismatch"),
        ({"expected_window_ref": {**wref, "windowKey": "win-key-other"}}, "native_descriptor_window_mismatch"),
        ({"expected_window_ref": {**wref, "pid": 9999}}, "native_descriptor_window_mismatch"),
        ({"expected_window_ref": {**wref, "processStarted": "133749999999999999"}}, "native_descriptor_window_mismatch"),
        ({"expected_window_ref": {**wref, "generation": 2}}, "native_descriptor_window_mismatch"),
        ({"expected_worker_instance_id": "worker_restarted_2"}, "native_descriptor_worker_mismatch"),
        ({"expected_task_id": "task_other"}, "native_descriptor_task_mismatch"),
        ({"expected_operation_id": "op_other"}, "native_descriptor_operation_mismatch"),
        ({"expected_observation_id": "obs_other"}, "native_descriptor_observation_mismatch"),
    ]:
        call_kwargs = {
            **expected_bindings,
            **kwargs,
        }
        with pytest.raises(ValueError, match=expected_err):
            image_capabilities.resolve_computer_image_point(sealed_descriptor, 100, 100, **call_kwargs)
