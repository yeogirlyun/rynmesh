"""Project tests for the Ryn JS full-image transport core and HostImageRegistry."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from typing import Any

from PIL import Image

from rynmesh.agent_runtime.js_runtime import (
    JsExecutionCoordinator,
    JsProtocolError,
    JsRuntimeManager,
)
from rynmesh.agent_runtime.js_runtime.images import (
    CAPABILITY_TOKEN_HEX_LEN,
    CAPABILITY_URL_PREFIX,
    MAX_ACTIVE_PINNED_IMAGES,
    MAX_GUEST_INLINE_BASE64_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_NATIVE_IMAGE_BYTES,
    MAX_PROMOTION_QUEUE_ENTRIES,
    MAX_SCREENSHOT_DESCRIPTOR_BYTES,
    MAX_SESSION_IMAGE_ENTRIES,
    MAX_SESSION_ORIGINAL_BYTES,
    MAX_TOTAL_PROMOTION_BYTES,
    HostImageIssuanceContext,
    HostImageRegistry,
    ImagePromotionResult,
    PromotedImageAttachment,
    VerifiedHostImageArtifact,
    native_window_refs_equal,
    normalize_native_window_ref,
    normalize_window_identity,
    prepare_durable_window_state_payload,
    revalidate_artifact_png_bytes,
    seal_verified_host_artifact,
)
from rynmesh.agent_runtime.js_runtime.protocol import (
    MAX_AGGREGATE_EVENT_BYTES,
    MAX_FRAME_BYTES,
    MAX_RESULT_BYTES,
    REDACTED_CAPABILITY_PLACEHOLDER,
    REDACTED_DATA_URL_PLACEHOLDER,
)


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type)
    crc = zlib.crc32(data, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def build_valid_png_bytes(
    *,
    width: int = 64,
    height: int = 64,
    target_min_bytes: int = 0,
    seed_byte: int = 0x5A,
) -> bytes:
    """Construct a valid, PIL-decodable RGB PNG with optional uncompressed IDAT payload."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row_bytes = 1 + width * 3
    total_raw = row_bytes * height
    pattern = bytes([(seed_byte + i * 17) & 0xFF for i in range(256)])
    row = b"\x00" + (pattern * ((width * 3 // len(pattern)) + 1))[: width * 3]
    raw_scanlines = row * height
    assert len(raw_scanlines) == total_raw

    if target_min_bytes > 0 and total_raw >= target_min_bytes - 65536:
        compressed = zlib.compress(raw_scanlines, level=0)
    else:
        compressed = zlib.compress(raw_scanlines, level=6)

    chunks = [sig, _png_chunk(b"IHDR", ihdr)]
    chunk_step = 256 * 1024
    for offset in range(0, len(compressed), chunk_step):
        chunks.append(_png_chunk(b"IDAT", compressed[offset : offset + chunk_step]))
    chunks.append(_png_chunk(b"IEND", b""))
    return b"".join(chunks)


def make_native_window_ref(
    *,
    worker_instance: str = "fixture_native_worker_1",
    window_key: str = "window_owned_101",
    hwnd: str = "1985229328",
    pid: int = 4242,
    process_started: str = "133700000000000000",
    generation: int = 7,
    app_id: str = "notepad.exe",
) -> dict[str, Any]:
    return {
        "workerInstance": worker_instance,
        "windowKey": window_key,
        "hwnd": hwnd,
        "pid": pid,
        "processStarted": process_started,
        "generation": generation,
        "appId": app_id,
    }


class StrictFixtureArtifactVerifier:
    """Test-only artifact verifier backed by an explicit receipt ledger and containment root.

    Distinguished from production NativeBridge provenance via
    `provenance_kind="test_fixture_owned_artifact"`. Rejects any candidate dict or
    path that was not explicitly registered in this fixture's receipt ledger or that
    mismatches the active task/operation/window/native_window/worker bindings.
    """

    provenance_kind = "test_fixture_owned_artifact"

    def __init__(self, containment_root: Path) -> None:
        self.containment_root = containment_root.resolve(strict=True)
        self._receipts: dict[tuple[str, str], dict[str, Any]] = {}
        self.call_count = 0

    def register_owned_png(
        self,
        *,
        artifact_id: str,
        receipt_id: str,
        task_id: str,
        operation_id: str,
        native_worker_instance: str = "fixture_native_worker_1",
        window: dict[str, Any] | None = None,
        native_window: dict[str, Any] | None = None,
        state_token: str = "st_fixture_1",
        observation_id: str = "obs_fixture_1",
        screenshot_id: str = "screenshot-0",
        width: int | None = None,
        height: int | None = None,
        origin_x: int = 0,
        origin_y: int = 0,
        z_index: int = 0,
        png_bytes: bytes,
        filename: str | None = None,
        write_file: bool = True,
    ) -> dict[str, Any]:
        if width is None or height is None:
            with Image.open(io.BytesIO(png_bytes)) as img:
                actual_w, actual_h = img.size
            resolved_width = int(actual_w) if width is None else width
            resolved_height = int(actual_h) if height is None else height
        else:
            resolved_width = width
            resolved_height = height
        win = dict(window or {"app": "notepad.exe", "id": 101, "title": "Untitled - Notepad"})
        win_id_int = win.get("id") if type(win.get("id")) is int else 101
        win_app_str = str(win.get("app") or "notepad.exe")
        nat_win = dict(
            native_window
            or make_native_window_ref(
                worker_instance=native_worker_instance,
                window_key=f"window_owned_{win_id_int}",
                app_id=win_app_str,
            )
        )
        artifact_path = self.containment_root / (filename or f"{artifact_id}.png")
        if write_file or not artifact_path.exists():
            artifact_path.write_bytes(png_bytes)
        sha256_hex = hashlib.sha256(png_bytes).hexdigest()
        record = {
            "id": screenshot_id,
            "artifact_id": artifact_id,
            "receipt_id": receipt_id,
            "task_id": task_id,
            "operation_id": operation_id,
            "native_worker_instance": native_worker_instance,
            "window": win,
            "native_window": nat_win,
            "stateToken": state_token,
            "observation_id": observation_id,
            "sha256": sha256_hex,
            "mimeType": "image/png",
            "byteLength": len(png_bytes),
            "width": resolved_width,
            "height": resolved_height,
            "originX": origin_x,
            "originY": origin_y,
            "zIndex": z_index,
            "provenance_kind": self.provenance_kind,
            "_fixture_artifact_path": artifact_path,
        }
        self._receipts[(artifact_id, receipt_id)] = record
        return {k: v for k, v in record.items() if not k.startswith("_")}

    def __call__(
        self,
        issuance_context: HostImageIssuanceContext,
        candidate: Any,
    ) -> VerifiedHostImageArtifact:
        self.call_count += 1
        if isinstance(candidate, VerifiedHostImageArtifact):
            return candidate
        if not isinstance(candidate, dict):
            raise JsProtocolError("ryn_js_image_untrusted", "Screenshot candidate must be a dict")
        artifact_id = str(candidate.get("artifact_id") or "").strip()
        receipt_id = str(candidate.get("receipt_id") or "").strip()
        if not artifact_id or not receipt_id:
            raise JsProtocolError(
                "ryn_js_image_untrusted",
                "Fixture verifier requires explicit artifact_id and receipt_id",
            )
        stored = self._receipts.get((artifact_id, receipt_id))
        if stored is None:
            raise JsProtocolError(
                "ryn_js_image_untrusted",
                f"Unknown or unowned artifact receipt ({artifact_id!r}, {receipt_id!r})",
            )
        if candidate.get("provenance_kind") not in {None, self.provenance_kind}:
            raise JsProtocolError(
                "ryn_js_image_untrusted",
                f"Fixture verifier rejects forged provenance_kind {candidate.get('provenance_kind')!r}",
            )
        if stored["task_id"] != issuance_context.task_id:
            raise JsProtocolError("ryn_js_image_scope_mismatch", "Receipt task_id mismatch")
        if issuance_context.operation_id and stored["operation_id"] != issuance_context.operation_id:
            raise JsProtocolError("ryn_js_image_scope_mismatch", "Receipt operation_id mismatch")
        if stored["native_worker_instance"] != issuance_context.native_worker_instance:
            raise JsProtocolError("ryn_js_image_scope_mismatch", "Receipt native_worker_instance mismatch")
        if normalize_window_identity(stored["window"])[1] != normalize_window_identity(issuance_context.window)[1]:
            raise JsProtocolError("ryn_js_image_window_mismatch", "Receipt Sky window identity mismatch")
        if not native_window_refs_equal(stored["native_window"], issuance_context.native_window):
            raise JsProtocolError("ryn_js_image_window_mismatch", "Receipt native_window WindowRef mismatch")

        return seal_verified_host_artifact(
            artifact_id=stored["artifact_id"],
            task_id=stored["task_id"],
            operation_id=stored["operation_id"],
            receipt_id=stored["receipt_id"],
            native_worker_instance=stored["native_worker_instance"],
            window=stored["window"],
            native_window=stored["native_window"],
            state_token=stored["stateToken"],
            observation_id=stored["observation_id"],
            sha256=stored["sha256"],
            byte_length=stored["byteLength"],
            width=stored["width"],
            height=stored["height"],
            artifact_path=stored["_fixture_artifact_path"],
            containment_root=self.containment_root,
            expected_parent=self.containment_root,
            mime_type="image/png",
            origin_x=stored["originX"],
            origin_y=stored["originY"],
            z_index=stored["zIndex"],
            screenshot_id=stored["id"],
            provenance_kind=self.provenance_kind,
        )


class RecordingImageCarrierCallback:
    """Test carrier callback that records `PromotedImageAttachment` payloads at the parent boundary.

    Proves only transport-to-callback byte/metadata fidelity; never claims model or pixel recognition.
    """

    def __init__(self, *, return_stage: str = "prepared") -> None:
        self.return_stage = return_stage
        self.attachments: list[PromotedImageAttachment] = []

    def __call__(self, attachment: PromotedImageAttachment) -> dict[str, Any]:
        self.attachments.append(attachment)
        return {
            "ok": True,
            "stage": self.return_stage,
            "carrier": "recording_test_carrier",
            "promotion_id": attachment.promotion_id,
            "byte_length": len(attachment.png_bytes),
        }


_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEV_PROFILE_CANDIDATE = (
    _REPO_ROOT
    / "work"
    / "computer-use-parity-20261019"
    / "prepared-js-runtime-independent-98ac78c9d0"
)


def _resolve_test_dev_profile_root(default_candidate: Path) -> Path | None:
    import os

    env_override = os.environ.get("RYN_JS_TEST_PROFILE_ROOT", "").strip()
    if env_override:
        explicit_path = Path(env_override)
        if not (explicit_path / "manifest.json").is_file():
            raise RuntimeError(
                f"RYN_JS_TEST_PROFILE_ROOT={env_override!r} does not point to a prepared JS runtime profile with manifest.json"
            )
        return explicit_path
    if (default_candidate / "manifest.json").is_file():
        return default_candidate
    return None


class TestAgentJsImages(unittest.TestCase):
    def setUp(self) -> None:
        dev_profile_root = _resolve_test_dev_profile_root(_DEV_PROFILE_CANDIDATE)
        if dev_profile_root is None:
            self.skipTest("Explicit development JS runtime profile not prepared")
        self._tmp = tempfile.TemporaryDirectory(prefix="ryn_js_img_test_")
        self.root = Path(self._tmp.name).resolve(strict=True)
        self.verifier = StrictFixtureArtifactVerifier(self.root)
        self.registry = HostImageRegistry(
            owner_id="owner_test_1",
            artifact_verifier=self.verifier,
        )
        self.manager = JsRuntimeManager(
            development_profile_root=dev_profile_root,
            image_registry=self.registry,
        )
        self.coordinator = JsExecutionCoordinator(self.manager, image_registry=self.registry)

    def tearDown(self) -> None:
        self.manager.close()
        self._tmp.cleanup()

    def test_near_8mib_png_transport_to_carrier_callback_and_same_turn_cross_cell_emission(self) -> None:
        """Near-8MiB (8.36MB) 2048x1360 PNG reaches carrier callback byte-for-byte while all JS frames stay < 16KiB."""
        near_8mib_png = build_valid_png_bytes(
            width=2048,
            height=1360,
            target_min_bytes=8_000_000,
            seed_byte=0x3C,
        )
        self.assertGreaterEqual(len(near_8mib_png), 8_000_000)
        self.assertLessEqual(len(near_8mib_png), MAX_NATIVE_IMAGE_BYTES)
        expected_sha = hashlib.sha256(near_8mib_png).hexdigest()
        nat_win = make_native_window_ref(
            worker_instance="fixture_native_worker_1",
            window_key="window_owned_101",
            hwnd="1985229328",
            pid=4242,
            process_started="133700000000000000",
            generation=7,
            app_id="notepad.exe",
        )

        receipt = self.verifier.register_owned_png(
            artifact_id="art_8mib_1",
            receipt_id="rcpt_8mib_1",
            task_id="task_8mib",
            operation_id="op_win_state_1",
            native_worker_instance="fixture_native_worker_1",
            window={"app": "notepad.exe", "id": 101, "title": "Large Canvas"},
            native_window=nat_win,
            state_token="st_large_1",
            observation_id="obs_large_1",
            screenshot_id="screenshot-0",
            width=2048,
            height=1360,
            origin_x=10,
            origin_y=20,
            z_index=1,
            png_bytes=near_8mib_png,
        )

        # Cell 1: import @oai/sky and call sky.get_window_state (which auto-emits via emitWindowStateImage)
        # and save the returned descriptor in globalThis.savedShot for a subsequent cell in the same turn.
        exec_1, gen_1 = self.coordinator.start(
            "task_8mib",
            """
            const { sky } = await import("@oai/sky");
            const state = await sky.get_window_state({
              window: { app: "notepad.exe", id: 101 },
              include_screenshot: true,
              include_text: true,
            });
            globalThis.savedShot = state.screenshots[0];
            return {
              count: state.screenshots.length,
              windowId: state.window.id,
              width: state.screenshots[0].width,
              height: state.screenshots[0].height,
              byteLength: state.screenshots[0].byteLength,
              hasCapPrefix: state.screenshots[0].url.startsWith("ryn-image:cap:"),
              urlLen: state.screenshots[0].url.length,
              rawUrlLeakAttempt: state.screenshots[0].url,
            };
            """,
        )

        step = self.coordinator.poll_step("task_8mib", timeout=2.0)
        self.assertEqual(step.status, "host_call_pending")
        assert step.host_call is not None
        self.assertEqual(step.host_call.method, "get_window_state")
        self.assertEqual(step.host_call.arguments["window"], {"app": "notepad.exe", "id": 101})

        self.coordinator.mark_dispatched(
            "task_8mib",
            execution_id=exec_1,
            generation=gen_1,
            callback_id=step.host_call.callback_id,
            operation_id="op_win_state_1",
        )

        durable_payload = prepare_durable_window_state_payload(
            {
                "window": {"app": "notepad.exe", "id": 101, "title": "Large Canvas"},
                "native_window": nat_win,
                "native_worker_instance": "fixture_native_worker_1",
                "accessibility": {"tree": "RootWindow"},
                "screenshots": [
                    {
                        **receipt,
                        "url": "ryn-image:cap:" + ("a" * 64),
                        "token": "a" * 64,
                        "base64": "AAAA",
                    }
                ],
            }
        )
        durable_json = json.dumps(durable_payload)
        self.assertNotIn(CAPABILITY_URL_PREFIX, durable_json)
        self.assertNotIn("base64", durable_json)
        self.assertNotIn("artifact_path", durable_json)
        self.assertEqual(durable_payload["window"]["id"], 101)
        self.assertEqual(durable_payload["native_window"]["windowKey"], "window_owned_101")

        ledger_rec = self.coordinator.resolve_window_state_from_outbox(
            "task_8mib",
            execution_id=exec_1,
            generation=gen_1,
            callback_id=step.host_call.callback_id,
            durable_outbox_payload=durable_payload,
            native_worker_instance="fixture_native_worker_1",
            native_window=nat_win,
            operation_id="op_win_state_1",
            outbox_committed=True,
        )
        self.assertIn(REDACTED_CAPABILITY_PLACEHOLDER, json.dumps(ledger_rec.result))
        self.assertNotIn(CAPABILITY_URL_PREFIX, json.dumps(ledger_rec.result))

        done_1 = self.coordinator.poll_step("task_8mib", timeout=2.0)
        self.assertEqual(done_1.status, "completed")
        self.assertEqual(done_1.snapshot.value["count"], 1)
        self.assertEqual(done_1.snapshot.value["windowId"], 101)
        self.assertEqual(done_1.snapshot.value["width"], 2048)
        self.assertEqual(done_1.snapshot.value["height"], 1360)
        self.assertEqual(done_1.snapshot.value["byteLength"], len(near_8mib_png))
        self.assertTrue(done_1.snapshot.value["hasCapPrefix"])
        self.assertEqual(done_1.snapshot.value["urlLen"], len(CAPABILITY_URL_PREFIX) + CAPABILITY_TOKEN_HEX_LEN)
        self.assertEqual(
            done_1.snapshot.value["rawUrlLeakAttempt"],
            REDACTED_CAPABILITY_PLACEHOLDER,
        )
        self.assertNotIn(CAPABILITY_URL_PREFIX, json.dumps(done_1.snapshot.to_dict()))

        # A mere image_request in snapshot.images is metadata-only (stage="requested"), not promoted pixels.
        self.assertEqual(len(done_1.snapshot.images), 1)
        self.assertEqual(done_1.snapshot.images[0]["promotion_stage"], "requested")
        self.assertEqual(done_1.snapshot.images[0]["window"], {"app": "notepad.exe", "id": 101, "title": "Large Canvas"})
        self.assertEqual(done_1.snapshot.images[0]["native_window"], nat_win)
        self.assertEqual(done_1.snapshot.images[0]["native_worker_instance"], "fixture_native_worker_1")
        self.assertNotIn("url", done_1.snapshot.images[0])
        self.assertNotIn("artifact_ref", done_1.snapshot.images[0])

        carrier = RecordingImageCarrierCallback(return_stage="prepared")
        promotions_1 = self.coordinator.drain_image_promotions(
            "task_8mib",
            execution_id=exec_1,
            carrier=carrier,
            parent_request_id="parent_cell_1",
        )
        self.assertEqual(len(promotions_1), 1)
        self.assertEqual(promotions_1[0].status, "promoted")
        self.assertEqual(promotions_1[0].disposition, "promoted")
        self.assertEqual(promotions_1[0].stage, "prepared")
        self.assertEqual(promotions_1[0].window, {"app": "notepad.exe", "id": 101, "title": "Large Canvas"})
        self.assertEqual(promotions_1[0].native_window, nat_win)
        self.assertEqual(promotions_1[0].native_worker_instance, "fixture_native_worker_1")
        self.assertEqual(len(carrier.attachments), 1)
        att_1 = carrier.attachments[0]
        self.assertEqual(att_1.provenance, "host_window_state")
        self.assertEqual(att_1.provenance_kind, "test_fixture_owned_artifact")
        self.assertEqual(att_1.sha256, expected_sha)
        self.assertEqual(att_1.png_bytes, near_8mib_png)
        self.assertEqual(att_1.emitting_execution_id, exec_1)
        self.assertEqual(att_1.issued_execution_id, exec_1)
        self.assertEqual(att_1.issued_callback_id, step.host_call.callback_id)
        self.assertEqual(att_1.window, {"app": "notepad.exe", "id": 101, "title": "Large Canvas"})
        self.assertEqual(att_1.native_window, nat_win)
        self.assertEqual(att_1.native_worker_instance, "fixture_native_worker_1")
        self.assertEqual(att_1.generation, 1)
        self.assertEqual(att_1.native_window["generation"], 7)

        # Cell 2 in the SAME turn: re-emit the saved descriptor via nodeRepl.emitImage(globalThis.savedShot)
        self.coordinator.retire_task("task_8mib", execution_id=exec_1, generation=gen_1)
        exec_2, _gen_2 = self.coordinator.start(
            "task_8mib",
            """
            const summary = await nodeRepl.emitImage(globalThis.savedShot);
            return summary;
            """,
        )
        done_2 = self.coordinator.poll_step("task_8mib", timeout=2.0)
        self.assertEqual(done_2.status, "completed")
        self.assertEqual(done_2.snapshot.value["provenance"], "host_issued_descriptor")
        self.assertNotIn("artifact_ref", done_2.snapshot.value)
        self.assertNotIn("url", done_2.snapshot.value)

        promotions_2 = self.coordinator.drain_image_promotions(
            "task_8mib",
            execution_id=exec_2,
            carrier=carrier,
            parent_request_id="parent_cell_2",
        )
        self.assertEqual(len(promotions_2), 1)
        self.assertEqual(promotions_2[0].status, "promoted")
        self.assertEqual(promotions_2[0].disposition, "promoted")
        self.assertEqual(promotions_2[0].stage, "prepared")
        self.assertEqual(promotions_2[0].emitting_execution_id, exec_2)
        self.assertEqual(promotions_2[0].issued_execution_id, exec_1)
        self.assertEqual(len(carrier.attachments), 2)
        att_2 = carrier.attachments[1]
        self.assertEqual(att_2.provenance, "host_issued_descriptor")
        self.assertEqual(att_2.emitting_execution_id, exec_2)
        self.assertEqual(att_2.issued_execution_id, exec_1)
        self.assertEqual(att_2.issued_callback_id, step.host_call.callback_id)
        self.assertEqual(att_2.observation_id, "obs_large_1")
        self.assertEqual(att_2.window, {"app": "notepad.exe", "id": 101, "title": "Large Canvas"})
        self.assertEqual(att_2.native_window, nat_win)
        self.assertEqual(att_2.png_bytes, near_8mib_png)

        # Verify every observed wire frame across both cells stayed well below all transport caps.
        session = self.manager.get_session("task_8mib")
        assert session is not None
        self.assertGreaterEqual(len(session.observed_wire_frames), 4)
        for wire_frame in session.observed_wire_frames:
            self.assertLess(wire_frame["byte_length"], 16 * 1024)
            self.assertLessEqual(wire_frame["byte_length"], MAX_RESULT_BYTES)
            self.assertLessEqual(wire_frame["byte_length"], MAX_FRAME_BYTES)
            if wire_frame["type"] == "image_request":
                self.assertLessEqual(wire_frame["byte_length"], MAX_SCREENSHOT_DESCRIPTOR_BYTES)

    def test_post_commit_outbox_ordering_and_transport_uncertainty_retirement(self) -> None:
        """Before-commit false refuses with 0 verifier calls and 0 tokens; post-commit valid mints and unknown retires."""
        png_bytes = build_valid_png_bytes(width=48, height=48)
        nat_win = make_native_window_ref(window_key="window_owned_101", app_id="notepad.exe")
        receipt = self.verifier.register_owned_png(
            artifact_id="art_outbox_1",
            receipt_id="rcpt_outbox_1",
            task_id="task_outbox",
            operation_id="op_outbox_1",
            window={"app": "notepad.exe", "id": 101},
            native_window=nat_win,
            png_bytes=png_bytes,
        )

        exec_id, gen = self.coordinator.start(
            "task_outbox",
            """
            const { sky } = await import("@oai/sky");
            return await sky.get_window_state({
              window: { app: "notepad.exe", id: 101 },
              include_screenshot: true,
            });
            """,
        )
        step = self.coordinator.poll_step("task_outbox", timeout=2.0)
        self.assertEqual(step.status, "host_call_pending")
        assert step.host_call is not None

        # Before outbox commit, prepare_durable_window_state_payload produces metadata only and mints 0 tokens.
        durable_payload = prepare_durable_window_state_payload(
            {
                "window": {"app": "notepad.exe", "id": 101},
                "native_window": nat_win,
                "native_worker_instance": "fixture_native_worker_1",
                "screenshots": [receipt],
            }
        )
        self.assertEqual(self.verifier.call_count, 0)
        self.assertEqual(len(self.registry._capabilities_by_token), 0)
        self.assertNotIn("url", durable_payload["screenshots"][0])

        # 1. Calling coordinator.bind_post_commit_window_state_result with outbox_committed=False (or default)
        # MUST fail closed with ryn_js_image_precommit_forbidden BEFORE calling verifier or minting tokens.
        with self.assertRaises(JsProtocolError) as ctx_uncommitted:
            self.coordinator.bind_post_commit_window_state_result(
                "task_outbox",
                durable_payload,
                execution_id=exec_id,
                generation=gen,
                callback_id=step.host_call.callback_id,
                operation_id="op_outbox_1",
                native_worker_instance="fixture_native_worker_1",
                native_window=nat_win,
                outbox_committed=False,
            )
        self.assertEqual(ctx_uncommitted.exception.code, "ryn_js_image_precommit_forbidden")
        self.assertEqual(self.verifier.call_count, 0)
        self.assertEqual(len(self.registry._capabilities_by_token), 0)

        # 2. Without mark_dispatched or explicit operation_id, coordinator refuses rather than fabricating op_<callback_id>.
        with self.assertRaises(JsProtocolError) as ctx_no_op:
            self.coordinator.bind_post_commit_window_state_result(
                "task_outbox",
                durable_payload,
                execution_id=exec_id,
                generation=gen,
                callback_id=step.host_call.callback_id,
                native_worker_instance="fixture_native_worker_1",
                native_window=nat_win,
                outbox_committed=True,
            )
        self.assertEqual(ctx_no_op.exception.code, "ryn_js_image_scope_mismatch")
        self.assertEqual(self.verifier.call_count, 0)
        self.assertEqual(len(self.registry._capabilities_by_token), 0)

        self.coordinator.mark_dispatched(
            "task_outbox",
            execution_id=exec_id,
            generation=gen,
            callback_id=step.host_call.callback_id,
            operation_id="op_outbox_1",
        )

        # 3. Attempting to bypass bind_post_commit_window_state_result by passing unbound durable_payload directly
        # to resolve_host_call also fails closed with ryn_js_image_precommit_forbidden.
        with self.assertRaises(JsProtocolError) as ctx_bypass:
            self.coordinator.resolve_host_call(
                "task_outbox",
                execution_id=exec_id,
                generation=gen,
                callback_id=step.host_call.callback_id,
                result=durable_payload,
            )
        self.assertEqual(ctx_bypass.exception.code, "ryn_js_image_precommit_forbidden")

        # 4. A durable payload contaminated with a pre-populated URL must be rejected.
        contaminated_payload = {
            "window": {"app": "notepad.exe", "id": 101},
            "native_window": nat_win,
            "native_worker_instance": "fixture_native_worker_1",
            "screenshots": [{**receipt, "url": CAPABILITY_URL_PREFIX + ("b" * 64)}],
        }
        with self.assertRaises(JsProtocolError) as ctx_pre:
            self.coordinator.bind_post_commit_window_state_result(
                "task_outbox",
                contaminated_payload,
                execution_id=exec_id,
                generation=gen,
                callback_id=step.host_call.callback_id,
                operation_id="op_outbox_1",
                native_worker_instance="fixture_native_worker_1",
                native_window=nat_win,
                outbox_committed=True,
            )
        self.assertEqual(ctx_pre.exception.code, "ryn_js_image_untrusted")

        # 5. Missing host native_worker_instance or missing host native_window must be rejected (no self-issued trust).
        with self.assertRaises(JsProtocolError) as ctx_no_worker:
            self.coordinator.bind_post_commit_window_state_result(
                "task_outbox",
                durable_payload,
                execution_id=exec_id,
                generation=gen,
                callback_id=step.host_call.callback_id,
                operation_id="op_outbox_1",
                native_window=nat_win,
                outbox_committed=True,
            )
        self.assertEqual(ctx_no_worker.exception.code, "ryn_js_image_scope_mismatch")

        with self.assertRaises(JsProtocolError) as ctx_no_nat_win:
            self.coordinator.bind_post_commit_window_state_result(
                "task_outbox",
                durable_payload,
                execution_id=exec_id,
                generation=gen,
                callback_id=step.host_call.callback_id,
                operation_id="op_outbox_1",
                native_worker_instance="fixture_native_worker_1",
                outbox_committed=True,
            )
        self.assertEqual(ctx_no_nat_win.exception.code, "ryn_js_image_window_mismatch")

        # 6. Mint post-commit capability descriptor after explicit outbox commit.
        bound_payload = self.coordinator.bind_post_commit_window_state_result(
            "task_outbox",
            durable_payload,
            execution_id=exec_id,
            generation=gen,
            callback_id=step.host_call.callback_id,
            operation_id="op_outbox_1",
            native_worker_instance="fixture_native_worker_1",
            native_window=nat_win,
            outbox_committed=True,
        )
        self.assertEqual(self.verifier.call_count, 1)
        cap_url = bound_payload["screenshots"][0]["url"]
        token = cap_url[len(CAPABILITY_URL_PREFIX) :]
        self.assertIn(token, self.registry._capabilities_by_token)

        # Simulate transport uncertainty on the dispatched callback -> retires and prunes session capabilities without replay.
        rec = self.coordinator.reject_host_call(
            "task_outbox",
            execution_id=exec_id,
            generation=gen,
            callback_id=step.host_call.callback_id,
            code="ryn_bridge_transport_lost",
            message="Transport interrupted after dispatch",
            outcome="unknown",
        )
        self.assertEqual(rec.status, "unknown")
        self.assertEqual(len(self.coordinator.get_unknown_records("task_outbox")), 1)
        done = self.coordinator.poll_step("task_outbox", timeout=2.0)
        self.assertEqual(done.status, "failed")
        self.assertNotIn(token, self.registry._capabilities_by_token)

    def test_default_registry_without_verifier_and_untrusted_dict_or_forged_seal_refused(self) -> None:
        """Default registry without verifier, unsealed VerifiedHostImageArtifact, and native WindowRef mismatches fail closed."""
        png_bytes = build_valid_png_bytes(width=32, height=32)
        nat_win = make_native_window_ref(window_key="window_owned_101", app_id="notepad.exe")
        receipt = self.verifier.register_owned_png(
            artifact_id="art_ver_1",
            receipt_id="rcpt_ver_1",
            task_id="task_ver",
            operation_id="op_ver_1",
            window={"app": "notepad.exe", "id": 101},
            native_window=nat_win,
            png_bytes=png_bytes,
        )

        # 0. normalize_window_identity refuses bare native WindowRef (never synthesizes Sky id: 0),
        # and normalize_native_window_ref requires explicit expected_worker_instance and all 6 native fields.
        with self.assertRaises(JsProtocolError) as ctx_bare_native:
            normalize_window_identity(nat_win)
        self.assertEqual(ctx_bare_native.exception.code, "ryn_js_invalid_window")

        with self.assertRaises(JsProtocolError) as ctx_missing_exp_worker:
            normalize_native_window_ref(nat_win)
        self.assertEqual(ctx_missing_exp_worker.exception.code, "ryn_js_image_scope_mismatch")

        for req_key in ("workerInstance", "windowKey", "hwnd", "pid", "processStarted", "generation"):
            incomplete_nat = {k: v for k, v in nat_win.items() if k != req_key}
            with self.assertRaises(JsProtocolError) as ctx_incomplete_nat:
                normalize_native_window_ref(
                    incomplete_nat,
                    expected_worker_instance="fixture_native_worker_1",
                )
            self.assertEqual(ctx_incomplete_nat.exception.code, "ryn_js_invalid_window")

        # 1. Default HostImageRegistry with no verifier refuses issuance.
        default_registry = HostImageRegistry(owner_id="owner_default")
        issue_ctx = HostImageIssuanceContext(
            owner_id="owner_default",
            node_runtime_id="node_sess_1",
            task_id="task_ver",
            session_id="sess_1",
            generation=1,
            turn=1,
            execution_id="exec_1",
            callback_id="cb_1",
            request_id="req_1",
            window={"app": "notepad.exe", "id": 101},
            native_window=nat_win,
            native_worker_instance="fixture_native_worker_1",
            operation_id="op_ver_1",
        )
        with self.assertRaises(JsProtocolError) as ctx_no_ver:
            default_registry.issue_screenshot_capability(receipt, issuance_context=issue_ctx)
        self.assertEqual(ctx_no_ver.exception.code, "ryn_js_image_verifier_unavailable")

        # 2. Direct VerifiedHostImageArtifact construction without seal_verified_host_artifact raises immediately.
        with self.assertRaises(JsProtocolError) as ctx_unsealed:
            VerifiedHostImageArtifact(
                artifact_id="forged",
                task_id="task_ver",
                operation_id="op_ver_1",
                receipt_id="rcpt_forged",
                native_worker_instance="worker_1",
                window={"app": "notepad.exe", "id": 101},
                native_window=nat_win,
                state_token="st_1",
                observation_id="obs_1",
                sha256=receipt["sha256"],
                byte_length=len(png_bytes),
                width=32,
                height=32,
                mime_type="image/png",
                origin_x=0,
                origin_y=0,
                z_index=0,
                screenshot_id="screenshot-0",
                artifact_path=self.root / "art_ver_1.png",
                containment_root=self.root,
                _seal=object(),
            )
        self.assertEqual(ctx_unsealed.exception.code, "ryn_js_image_untrusted")

        # 3. Arbitrary dict claiming unknown receipt or production_native_receipt is rejected by fixture verifier.
        valid_ctx = HostImageIssuanceContext(
            owner_id="owner_test_1",
            node_runtime_id="node_sess_1",
            task_id="task_ver",
            session_id="sess_1",
            generation=1,
            turn=1,
            execution_id="exec_1",
            callback_id="cb_1",
            request_id="req_1",
            window={"app": "notepad.exe", "id": 101},
            native_window=nat_win,
            native_worker_instance="fixture_native_worker_1",
            operation_id="op_ver_1",
        )
        with self.assertRaises(JsProtocolError) as ctx_forged_dict:
            self.registry.issue_screenshot_capability(
                {**receipt, "receipt_id": "rcpt_nonexistent"},
                issuance_context=valid_ctx,
            )
        self.assertEqual(ctx_forged_dict.exception.code, "ryn_js_image_untrusted")

        with self.assertRaises(JsProtocolError) as ctx_forged_prov:
            self.registry.issue_screenshot_capability(
                {**receipt, "provenance_kind": "production_native_receipt"},
                issuance_context=valid_ctx,
            )
        self.assertEqual(ctx_forged_prov.exception.code, "ryn_js_image_untrusted")

        # 4. Every native WindowRef field mismatch (workerInstance, windowKey, hwnd, pid, processStarted, generation, appId)
        # and Sky window mismatch (app, id) fails closed.
        with self.assertRaises(JsProtocolError) as ctx_worker_mismatch:
            HostImageIssuanceContext(
                owner_id="owner_test_1",
                node_runtime_id="node_sess_1",
                task_id="task_ver",
                session_id="sess_1",
                generation=1,
                turn=1,
                execution_id="exec_1",
                callback_id="cb_1",
                request_id="req_1",
                window={"app": "notepad.exe", "id": 101},
                native_window={**nat_win, "workerInstance": "other_native_worker_9"},
                native_worker_instance="fixture_native_worker_1",
                operation_id="op_ver_1",
            )
        self.assertEqual(ctx_worker_mismatch.exception.code, "ryn_js_image_window_mismatch")

        for field_name, bad_val in (
            ("windowKey", "window_owned_999"),
            ("hwnd", "9999999"),
            ("pid", 9999),
            ("processStarted", "133799999999999999"),
            ("generation", 8),
            ("appId", "calc.exe"),
        ):
            mismatched_nat_win = {**nat_win, field_name: bad_val}
            bad_ctx = HostImageIssuanceContext(
                owner_id="owner_test_1",
                node_runtime_id="node_sess_1",
                task_id="task_ver",
                session_id="sess_1",
                generation=1,
                turn=1,
                execution_id="exec_1",
                callback_id="cb_1",
                request_id="req_1",
                window={"app": "notepad.exe", "id": 101},
                native_window=mismatched_nat_win,
                native_worker_instance="fixture_native_worker_1",
                operation_id="op_ver_1",
            )
            with self.assertRaises(JsProtocolError) as ctx_nat_mismatch:
                self.registry.issue_screenshot_capability(receipt, issuance_context=bad_ctx)
            self.assertEqual(ctx_nat_mismatch.exception.code, "ryn_js_image_window_mismatch")

        for bad_sky_win in (
            {"app": "notepad.exe", "id": 999},
            {"app": "calc.exe", "id": 101},
        ):
            bad_sky_ctx = HostImageIssuanceContext(
                owner_id="owner_test_1",
                node_runtime_id="node_sess_1",
                task_id="task_ver",
                session_id="sess_1",
                generation=1,
                turn=1,
                execution_id="exec_1",
                callback_id="cb_1",
                request_id="req_1",
                window=bad_sky_win,
                native_window=nat_win,
                native_worker_instance="fixture_native_worker_1",
                operation_id="op_ver_1",
            )
            with self.assertRaises(JsProtocolError) as ctx_sky_mismatch:
                self.registry.issue_screenshot_capability(receipt, issuance_context=bad_sky_ctx)
            self.assertEqual(ctx_sky_mismatch.exception.code, "ryn_js_image_window_mismatch")

        # 5. Manager without HostImageRegistry marks drained promotions unavailable (ryn_js_image_registry_unavailable).
        plain_manager = JsRuntimeManager(
            development_profile_root=_DEV_PROFILE_ROOT,
            image_registry=None,
        )
        try:
            snap = plain_manager.run_to_completion(
                "task_no_reg",
                """
                const { sky } = await import("@oai/sky");
                return await sky.get_window_state({
                  window: { app: "notepad.exe", id: 101 },
                  include_screenshot: true,
                });
                """,
                host_handler=lambda _env: {
                    "window": {"app": "notepad.exe", "id": 101},
                    "screenshots": [
                        {
                            "id": "screenshot-0",
                            "url": CAPABILITY_URL_PREFIX + ("c" * 64),
                            "sha256": receipt["sha256"],
                            "width": 32,
                            "height": 32,
                        }
                    ],
                },
            )
            self.assertEqual(snap.status, "completed")
            unverified_res = plain_manager.drain_image_promotions("task_no_reg", execution_id=snap.execution_id)
            self.assertEqual(len(unverified_res), 1)
            self.assertEqual(unverified_res[0].status, "unavailable")
            self.assertEqual(unverified_res[0].disposition, "unavailable")
            self.assertEqual(unverified_res[0].error_code, "ryn_js_image_registry_unavailable")
        finally:
            plain_manager.close()

    def test_guest_forgery_masquerade_and_scope_mismatch_vectors_fail_closed(self) -> None:
        """Guest-side and wire-level forgery, masquerade, and post-rejection host_call fencing fail closed."""
        png_bytes = build_valid_png_bytes(width=32, height=32)
        nat_win = make_native_window_ref(window_key="window_owned_101", app_id="notepad.exe")
        receipt = self.verifier.register_owned_png(
            artifact_id="art_sec_1",
            receipt_id="rcpt_sec_1",
            task_id="task_sec",
            operation_id="op_sec_1",
            window={"app": "notepad.exe", "id": 101},
            native_window=nat_win,
            state_token="st_sec_1",
            png_bytes=png_bytes,
        )

        exec_1, gen_1 = self.coordinator.start(
            "task_sec",
            """
            const { sky } = await import("@oai/sky");
            const state = await sky.get_window_state({
              window: { app: "notepad.exe", id: 101 },
              include_screenshot: true,
            });
            globalThis.validShot = state.screenshots[0];
            return state.screenshots[0].id;
            """,
        )
        step = self.coordinator.poll_step("task_sec", timeout=2.0)
        assert step.host_call is not None
        self.coordinator.mark_dispatched(
            "task_sec",
            execution_id=exec_1,
            generation=gen_1,
            callback_id=step.host_call.callback_id,
            operation_id="op_sec_1",
        )
        durable = prepare_durable_window_state_payload(
            {
                "window": {"app": "notepad.exe", "id": 101},
                "native_window": nat_win,
                "native_worker_instance": "fixture_native_worker_1",
                "screenshots": [receipt],
            }
        )
        self.coordinator.resolve_window_state_from_outbox(
            "task_sec",
            durable,
            execution_id=exec_1,
            generation=gen_1,
            callback_id=step.host_call.callback_id,
            operation_id="op_sec_1",
            native_worker_instance="fixture_native_worker_1",
            native_window=nat_win,
            outbox_committed=True,
        )
        done_1 = self.coordinator.poll_step("task_sec", timeout=2.0)
        self.assertEqual(done_1.status, "completed")
        self.coordinator.drain_image_promotions(
            "task_sec",
            execution_id=exec_1,
            carrier=RecordingImageCarrierCallback(),
        )
        self.coordinator.retire_task("task_sec", execution_id=exec_1, generation=gen_1)

        small_b64 = base64.b64encode(png_bytes).decode("ascii")
        forgery_scripts = [
            (
                "fabricated_token",
                """
                await nodeRepl.emitImage({
                  id: "screenshot-0",
                  url: "ryn-image:cap:" + "f".repeat(64),
                });
                """,
                "ryn_js_image_untrusted",
            ),
            (
                "tampered_sha256",
                """
                await nodeRepl.emitImage({
                  ...globalThis.validShot,
                  sha256: "0".repeat(64),
                });
                """,
                "ryn_js_image_untrusted",
            ),
            (
                "tampered_width",
                """
                await nodeRepl.emitImage({
                  ...globalThis.validShot,
                  width: 9999,
                });
                """,
                "ryn_js_image_untrusted",
            ),
            (
                "tampered_state_token",
                """
                await nodeRepl.emitImage({
                  ...globalThis.validShot,
                  stateToken: "st_forged",
                });
                """,
                "ryn_js_image_untrusted",
            ),
            (
                "tampered_window",
                """
                await nodeRepl.emitImage({
                  ...globalThis.validShot,
                  window: { app: "calc.exe", id: 999 },
                });
                """,
                "ryn_js_image_window_mismatch",
            ),
            (
                "windows_path",
                """
                await nodeRepl.emitImage("C:\\\\Windows\\\\System32\\\\cmd.exe");
                """,
                "ryn_js_image_forbidden_url",
            ),
            (
                "https_url",
                """
                await nodeRepl.emitImage("https://example.com/shot.png");
                """,
                "ryn_js_image_forbidden_url",
            ),
            (
                "inline_data_url_masquerade",
                f"""
                await nodeRepl.emitImage({{
                  url: "data:image/png;base64,{small_b64}",
                  window: {{ app: "notepad.exe", id: 101 }},
                  stateToken: "st_sec_1",
                }});
                """,
                "ryn_js_image_untrusted",
            ),
        ]

        for label, script, expected_err in forgery_scripts:
            exec_f, gen_f = self.coordinator.start("task_sec", script, execution_id=f"exec_{label}")
            res_f = self.coordinator.poll_step("task_sec", timeout=2.0)
            self.assertEqual(res_f.status, "failed", f"Forgery vector {label} should have failed")
            assert res_f.snapshot.error is not None
            self.assertIn(expected_err, res_f.snapshot.error.get("message", "") + res_f.snapshot.error.get("code", ""))
            self.coordinator.retire_task("task_sec", execution_id=exec_f, generation=gen_f)

        # Verify host-side registry rejection fences subsequent sky.* host_calls in the same cell:
        # Retire the session token on the Python side (while the worker's JS turn cache still holds validShot),
        # then have the guest cell call nodeRepl.emitImage(globalThis.validShot) followed by sky.get_window_state.
        session = self.manager.get_session("task_sec")
        assert session is not None
        self.registry.retire_session(session.session_id, reason="ryn_js_image_retired", cancel_pinned=True)
        exec_fence, gen_fence = self.coordinator.start(
            "task_sec",
            """
            const { sky } = await import("@oai/sky");
            await nodeRepl.emitImage(globalThis.validShot);
            await sky.get_window_state({ window: { app: "notepad.exe", id: 101 } });
            return "should_not_reach";
            """,
            execution_id="exec_host_fence",
        )
        res_fence = self.coordinator.poll_step("task_sec", timeout=2.0)
        self.assertEqual(res_fence.status, "failed")
        self.assertIsNone(res_fence.host_call)
        assert res_fence.snapshot.error is not None
        self.assertEqual(res_fence.snapshot.error["code"], "ryn_js_image_untrusted")
        self.coordinator.retire_task("task_sec", execution_id=exec_fence, generation=gen_fence)

        # Re-issue a fresh capability to test direct Python registry scope checks:
        b_direct = self.registry.issue_screenshot_capability(
            receipt,
            issuance_context=HostImageIssuanceContext(
                owner_id="owner_test_1",
                node_runtime_id=session.node_runtime_id,
                task_id="task_sec",
                session_id=session.session_id,
                generation=session.generation,
                turn=session.turn,
                execution_id="exec_direct_issue",
                callback_id="cb_direct",
                request_id="req_direct",
                window={"app": "notepad.exe", "id": 101},
                native_window=nat_win,
                native_worker_instance="fixture_native_worker_1",
                operation_id="op_sec_1",
            ),
        )
        valid_wire_img = {
            "provenance": "host_issued_descriptor",
            "id": "screenshot-0",
            "url": b_direct.capability_url,
            "sha256": receipt["sha256"],
            "width": 32,
            "height": 32,
            "stateToken": "st_sec_1",
            "window": {"app": "notepad.exe", "id": 101},
        }
        # Wrong task_id / session_id / generation / turn / node_runtime_id -> ryn_js_image_untrusted
        with self.assertRaises(JsProtocolError) as ctx_cross_task:
            self.registry.validate_and_queue_request(
                valid_wire_img,
                owner_id="owner_test_1",
                node_runtime_id=session.node_runtime_id,
                task_id="other_task",
                session_id=session.session_id,
                generation=session.generation,
                turn=session.turn,
                emitting_execution_id="exec_direct_1",
            )
        self.assertEqual(ctx_cross_task.exception.code, "ryn_js_image_untrusted")

        # Wrong context_epoch or selection_scope -> ryn_js_image_scope_mismatch
        with self.assertRaises(JsProtocolError) as ctx_cross_epoch:
            self.registry.validate_and_queue_request(
                valid_wire_img,
                owner_id="owner_test_1",
                node_runtime_id=session.node_runtime_id,
                task_id="task_sec",
                session_id=session.session_id,
                generation=session.generation,
                turn=session.turn,
                emitting_execution_id="exec_direct_2",
                context_epoch="epoch_other",
                selection_scope=session.selection_scope,
            )
        self.assertEqual(ctx_cross_epoch.exception.code, "ryn_js_image_scope_mismatch")

    def test_observation_supersession_turn_cancel_worker_loss_and_pin_lifecycle(self) -> None:
        """New observation for same window, scope change, end_turn, cancel, and pin/retire enforce lifecycle."""
        png_1 = build_valid_png_bytes(width=40, height=40, seed_byte=0x11)
        png_2 = build_valid_png_bytes(width=40, height=40, seed_byte=0x22)
        nat_win = make_native_window_ref(window_key="window_owned_101", app_id="notepad.exe")
        receipt_1 = self.verifier.register_owned_png(
            artifact_id="art_obs_1",
            receipt_id="rcpt_obs_1",
            task_id="task_lifecycle",
            operation_id="op_obs_1",
            window={"app": "notepad.exe", "id": 101},
            native_window=nat_win,
            state_token="st_obs_1",
            observation_id="obs_1",
            png_bytes=png_1,
        )
        receipt_2 = self.verifier.register_owned_png(
            artifact_id="art_obs_2",
            receipt_id="rcpt_obs_2",
            task_id="task_lifecycle",
            operation_id="op_obs_2",
            window={"app": "notepad.exe", "id": 101},
            native_window=nat_win,
            state_token="st_obs_2",
            observation_id="obs_2",
            png_bytes=png_2,
        )

        # Cell 1: two sequential sky.get_window_state calls on the same window in one cell.
        exec_1, gen_1 = self.coordinator.start(
            "task_lifecycle",
            """
            const { sky } = await import("@oai/sky");
            const first = await sky.get_window_state({
              window: { app: "notepad.exe", id: 101 },
              include_screenshot: true,
            });
            globalThis.firstShot = first.screenshots[0];
            const second = await sky.get_window_state({
              window: { app: "notepad.exe", id: 101 },
              include_screenshot: true,
            });
            globalThis.secondShot = second.screenshots[0];
            return {
              firstToken: first.screenshots[0].stateToken,
              secondToken: second.screenshots[0].stateToken,
            };
            """,
        )

        step_1 = self.coordinator.poll_step("task_lifecycle", timeout=2.0)
        assert step_1.host_call is not None
        self.coordinator.mark_dispatched(
            "task_lifecycle",
            execution_id=exec_1,
            generation=gen_1,
            callback_id=step_1.host_call.callback_id,
            operation_id="op_obs_1",
        )
        self.coordinator.resolve_window_state_from_outbox(
            "task_lifecycle",
            prepare_durable_window_state_payload(
                {
                    "window": {"app": "notepad.exe", "id": 101},
                    "native_window": nat_win,
                    "native_worker_instance": "fixture_native_worker_1",
                    "screenshots": [receipt_1],
                }
            ),
            execution_id=exec_1,
            generation=gen_1,
            callback_id=step_1.host_call.callback_id,
            operation_id="op_obs_1",
            native_worker_instance="fixture_native_worker_1",
            native_window=nat_win,
            outbox_committed=True,
        )

        step_2 = self.coordinator.poll_step("task_lifecycle", timeout=2.0)
        assert step_2.host_call is not None
        self.coordinator.mark_dispatched(
            "task_lifecycle",
            execution_id=exec_1,
            generation=gen_1,
            callback_id=step_2.host_call.callback_id,
            operation_id="op_obs_2",
        )
        self.coordinator.resolve_window_state_from_outbox(
            "task_lifecycle",
            prepare_durable_window_state_payload(
                {
                    "window": {"app": "notepad.exe", "id": 101},
                    "native_window": nat_win,
                    "native_worker_instance": "fixture_native_worker_1",
                    "screenshots": [receipt_2],
                }
            ),
            execution_id=exec_1,
            generation=gen_1,
            callback_id=step_2.host_call.callback_id,
            operation_id="op_obs_2",
            native_worker_instance="fixture_native_worker_1",
            native_window=nat_win,
            outbox_committed=True,
        )

        done_1 = self.coordinator.poll_step("task_lifecycle", timeout=2.0)
        self.assertEqual(done_1.status, "completed")
        carrier = RecordingImageCarrierCallback()
        promotions = self.coordinator.drain_image_promotions(
            "task_lifecycle",
            execution_id=exec_1,
            carrier=carrier,
        )
        # First observation was superseded before drain -> unavailable (ryn_js_image_retired); second -> promoted.
        self.assertEqual(len(promotions), 2)
        self.assertEqual(promotions[0].status, "unavailable")
        self.assertEqual(promotions[0].disposition, "unavailable")
        self.assertEqual(promotions[0].error_code, "ryn_js_image_retired")
        self.assertIn("superseded_by_new_observation", promotions[0].reason or "")
        self.assertEqual(promotions[1].status, "promoted")
        self.assertEqual(promotions[1].disposition, "promoted")
        self.assertEqual(promotions[1].stage, "prepared")
        self.assertEqual(len(carrier.attachments), 1)
        self.assertEqual(carrier.attachments[0].png_bytes, png_2)
        self.coordinator.retire_task("task_lifecycle", execution_id=exec_1, generation=gen_1)

        # Re-emitting superseded firstShot in Cell 2 fails closed.
        exec_old, gen_old = self.coordinator.start(
            "task_lifecycle",
            "return await nodeRepl.emitImage(globalThis.firstShot);",
        )
        done_old = self.coordinator.poll_step("task_lifecycle", timeout=2.0)
        self.assertEqual(done_old.status, "failed")
        self.coordinator.retire_task("task_lifecycle", execution_id=exec_old, generation=gen_old)

        # Emit valid secondShot in Cell 3, then test public pin_promotion_request + concurrent retirement:
        exec_pin, gen_pin = self.coordinator.start(
            "task_lifecycle",
            "return await nodeRepl.emitImage(globalThis.secondShot);",
        )
        done_pin = self.coordinator.poll_step("task_lifecycle", timeout=2.0)
        self.assertEqual(done_pin.status, "completed")
        session = self.manager.get_session("task_lifecycle")
        assert session is not None
        pending_promos = self.registry.get_session_pending_promotions(session.session_id, execution_id=exec_pin)
        self.assertEqual(len(pending_promos), 1)
        promo_id = pending_promos[0]["promotion_id"]

        pinned_req = self.registry.pin_promotion_request(
            promo_id,
            parent_request_id="parent_req_1",
            task_id="task_lifecycle",
            session_id=session.session_id,
            generation=gen_pin,
            emitting_execution_id=exec_pin,
        )
        self.assertEqual(pinned_req.stage, "prepared")
        self.assertEqual(pinned_req.disposition, "pinned")

        # Non-cancelling turn retirement (cancel_pinned=False) allows the already-pinned parent request to finish.
        self.registry.retire_session(session.session_id, reason="ryn_js_turn_ended", cancel_pinned=False)
        completed_pin = self.registry.complete_pinned_promotion(
            promo_id,
            parent_request_id="parent_req_1",
            task_id="task_lifecycle",
            session_id=session.session_id,
            generation=gen_pin,
            emitting_execution_id=exec_pin,
            carrier=carrier,
        )
        self.assertEqual(completed_pin.status, "promoted")
        self.assertEqual(completed_pin.disposition, "promoted")
        self.assertEqual(completed_pin.stage, "prepared")
        self.assertEqual(len(carrier.attachments), 2)
        # Once the pinned promotion completes, the retired capability and consumed promotion are pruned from RAM.
        self.assertEqual(len(self.registry._capabilities_by_token), 0)
        self.assertEqual(len(self.registry._promotion_queue), 0)
        self.coordinator.retire_task("task_lifecycle", execution_id=exec_pin, generation=gen_pin)

        # End turn on coordinator -> subsequent cell cannot emit previous turn's secondShot.
        self.coordinator.end_turn("task_lifecycle")
        exec_after_turn, gen_after_turn = self.coordinator.start(
            "task_lifecycle",
            "return await nodeRepl.emitImage(globalThis.secondShot);",
        )
        done_after_turn = self.coordinator.poll_step("task_lifecycle", timeout=2.0)
        self.assertEqual(done_after_turn.status, "failed")
        self.coordinator.retire_task("task_lifecycle", execution_id=exec_after_turn, generation=gen_after_turn)

        # Issue a turn-2 screenshot and test release_pinned_promotion, scope change, cancel, and worker loss:
        receipt_3 = self.verifier.register_owned_png(
            artifact_id="art_obs_3",
            receipt_id="rcpt_obs_3",
            task_id="task_lifecycle",
            operation_id="op_obs_3",
            window={"app": "notepad.exe", "id": 101},
            native_window=nat_win,
            state_token="st_obs_3",
            observation_id="obs_3",
            png_bytes=png_2,
        )
        exec_t2, gen_t2 = self.coordinator.start(
            "task_lifecycle",
            """
            const { sky } = await import("@oai/sky");
            const st = await sky.get_window_state({
              window: { app: "notepad.exe", id: 101 },
              include_screenshot: true,
            });
            globalThis.turn2Shot = st.screenshots[0];
            return st.screenshots[0].id;
            """,
        )
        step_t2 = self.coordinator.poll_step("task_lifecycle", timeout=2.0)
        assert step_t2.host_call is not None
        self.coordinator.mark_dispatched(
            "task_lifecycle",
            execution_id=exec_t2,
            generation=gen_t2,
            callback_id=step_t2.host_call.callback_id,
            operation_id="op_obs_3",
        )
        self.coordinator.resolve_window_state_from_outbox(
            "task_lifecycle",
            prepare_durable_window_state_payload(
                {
                    "window": {"app": "notepad.exe", "id": 101},
                    "native_window": nat_win,
                    "native_worker_instance": "fixture_native_worker_1",
                    "screenshots": [receipt_3],
                }
            ),
            execution_id=exec_t2,
            generation=gen_t2,
            callback_id=step_t2.host_call.callback_id,
            operation_id="op_obs_3",
            native_worker_instance="fixture_native_worker_1",
            native_window=nat_win,
            outbox_committed=True,
        )
        done_t2 = self.coordinator.poll_step("task_lifecycle", timeout=2.0)
        self.assertEqual(done_t2.status, "completed")
        pending_t2 = self.registry.get_session_pending_promotions(session.session_id, execution_id=exec_t2)
        self.assertEqual(len(pending_t2), 1)
        promo_t2_id = pending_t2[0]["promotion_id"]

        self.registry.pin_promotion_request(
            promo_t2_id,
            parent_request_id="parent_release_1",
            task_id="task_lifecycle",
            session_id=session.session_id,
            generation=gen_t2,
            emitting_execution_id=exec_t2,
        )
        released_res = self.registry.release_pinned_promotion(
            promo_t2_id,
            parent_request_id="parent_release_1",
        )
        self.assertEqual(released_res.status, "unavailable")
        self.assertEqual(released_res.disposition, "unavailable")
        self.assertEqual(released_res.error_code, "ryn_js_image_pin_released")
        self.assertEqual(len(self.registry._promotion_queue), 0)
        self.coordinator.retire_task("task_lifecycle", execution_id=exec_t2, generation=gen_t2)

        # Updating task scope retires and prunes active capabilities in the session (ryn_js_image_scope_changed).
        self.coordinator.update_task_scope(
            "task_lifecycle",
            context_epoch="epoch_2",
            selection_scope="monitor_2",
        )
        self.assertEqual(len(self.registry._capabilities_by_token), 0)
        exec_scope, gen_scope = self.coordinator.start(
            "task_lifecycle",
            "return await nodeRepl.emitImage(globalThis.turn2Shot);",
        )
        done_scope = self.coordinator.poll_step("task_lifecycle", timeout=2.0)
        self.assertEqual(done_scope.status, "failed")
        assert done_scope.snapshot.error is not None
        self.assertEqual(done_scope.snapshot.error["code"], "ryn_js_image_untrusted")
        self.coordinator.retire_task("task_lifecycle", execution_id=exec_scope, generation=gen_scope)

        # Unexpected worker loss retires any active capability and marks any pinned promotion unavailable:
        b_loss = self.registry.issue_screenshot_capability(
            receipt_3,
            issuance_context=HostImageIssuanceContext(
                owner_id="owner_test_1",
                node_runtime_id=session.node_runtime_id,
                task_id="task_lifecycle",
                session_id=session.session_id,
                generation=session.generation,
                turn=session.turn,
                execution_id="exec_before_kill",
                callback_id="cb_kill",
                request_id="req_kill",
                window={"app": "notepad.exe", "id": 101},
                native_window=nat_win,
                native_worker_instance="fixture_native_worker_1",
                operation_id="op_obs_3",
                context_epoch=session.context_epoch,
                selection_scope=session.selection_scope,
            ),
        )
        self.registry.validate_and_queue_request(
            {
                "provenance": "host_window_state",
                "id": b_loss.screenshot_id,
                "url": b_loss.capability_url,
                "sha256": b_loss.sha256,
                "window": b_loss.window,
            },
            owner_id="owner_test_1",
            node_runtime_id=session.node_runtime_id,
            task_id="task_lifecycle",
            session_id=session.session_id,
            generation=session.generation,
            turn=session.turn,
            emitting_execution_id="exec_before_kill",
            context_epoch=session.context_epoch,
            selection_scope=session.selection_scope,
        )
        assert session.process is not None
        session.process.kill()
        session.process.wait(timeout=2.0)
        session._sync_process_state()
        self.assertNotIn(b_loss.token, self.registry._capabilities_by_token)
        lost_promos = self.coordinator.drain_image_promotions(
            "task_lifecycle",
            execution_id="exec_before_kill",
            carrier=carrier,
        )
        self.assertEqual(len(lost_promos), 1)
        self.assertEqual(lost_promos[0].status, "unavailable")
        self.assertEqual(lost_promos[0].error_code, "ryn_js_worker_lost")
        self.assertEqual(len(self.registry._promotion_queue), 0)
        self.assertEqual(len(self.registry._session_promotions.get(session.session_id, ())), 0)

    def test_tampered_artifact_containment_fd_hygiene_dimensions_and_quotas_fail_closed(self) -> None:
        """Tampered files after issuance, path escapes, bombs, and quotas fail closed with bounded RAM metadata."""
        png_bytes = build_valid_png_bytes(width=48, height=48, seed_byte=0x77)
        nat_win = make_native_window_ref(window_key="window_owned_101", app_id="notepad.exe")
        receipt = self.verifier.register_owned_png(
            artifact_id="art_tamper_1",
            receipt_id="rcpt_tamper_1",
            task_id="task_tamper",
            operation_id="op_tamper_1",
            window={"app": "notepad.exe", "id": 101},
            native_window=nat_win,
            png_bytes=png_bytes,
        )

        exec_1, gen_1 = self.coordinator.start(
            "task_tamper",
            """
            const { sky } = await import("@oai/sky");
            return await sky.get_window_state({
              window: { app: "notepad.exe", id: 101 },
              include_screenshot: true,
            });
            """,
        )
        step = self.coordinator.poll_step("task_tamper", timeout=2.0)
        assert step.host_call is not None
        self.coordinator.mark_dispatched(
            "task_tamper",
            execution_id=exec_1,
            generation=gen_1,
            callback_id=step.host_call.callback_id,
            operation_id="op_tamper_1",
        )
        self.coordinator.resolve_window_state_from_outbox(
            "task_tamper",
            prepare_durable_window_state_payload(
                {
                    "window": {"app": "notepad.exe", "id": 101},
                    "native_window": nat_win,
                    "native_worker_instance": "fixture_native_worker_1",
                    "screenshots": [receipt],
                }
            ),
            execution_id=exec_1,
            generation=gen_1,
            callback_id=step.host_call.callback_id,
            operation_id="op_tamper_1",
            native_worker_instance="fixture_native_worker_1",
            native_window=nat_win,
            outbox_committed=True,
        )
        done = self.coordinator.poll_step("task_tamper", timeout=2.0)
        self.assertEqual(done.status, "completed")

        # Tamper with the PNG bytes on disk after issuance, before draining promotions.
        tampered_path = self.root / "art_tamper_1.png"
        tampered_path.write_bytes(build_valid_png_bytes(width=48, height=48, seed_byte=0x99))
        carrier = RecordingImageCarrierCallback()
        promotions = self.coordinator.drain_image_promotions(
            "task_tamper",
            execution_id=exec_1,
            carrier=carrier,
        )
        self.assertEqual(len(promotions), 1)
        self.assertEqual(promotions[0].status, "unavailable")
        self.assertEqual(promotions[0].disposition, "unavailable")
        self.assertEqual(promotions[0].error_code, "ryn_js_image_artifact_changed")
        self.assertEqual(len(carrier.attachments), 0)

        # File handle hygiene check: on Windows, any leaked open FD prevents unlinking the file.
        tampered_path.unlink()
        self.assertFalse(tampered_path.exists())

        # Excessive dimensions (5000x4000 = 20,000,000 > MAX_IMAGE_PIXELS = 16,000,000) fail closed.
        bomb_ihdr = struct.pack(">IIBBBBB", 5000, 4000, 8, 2, 0, 0, 0)
        bomb_png = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", bomb_ihdr)
            + _png_chunk(b"IDAT", zlib.compress(b"\x00" * 16))
            + _png_chunk(b"IEND", b"")
        )
        bomb_path = self.root / "bomb.png"
        bomb_path.write_bytes(bomb_png)
        with self.assertRaises(JsProtocolError) as ctx_bomb:
            revalidate_artifact_png_bytes(
                artifact_path=bomb_path,
                containment_root=self.root,
                expected_sha256=hashlib.sha256(bomb_png).hexdigest(),
                expected_bytes=len(bomb_png),
                expected_width=5000,
                expected_height=4000,
            )
        self.assertEqual(ctx_bomb.exception.code, "ryn_js_image_dimensions_exceeded")
        bomb_path.unlink()

        # Outside containment root fails closed with ryn_js_image_outside_containment.
        with tempfile.TemporaryDirectory(prefix="ryn_outside_") as outside_dir:
            outside_file = Path(outside_dir).resolve(strict=True) / "outside.png"
            outside_file.write_bytes(png_bytes)
            with self.assertRaises(JsProtocolError) as ctx_escape:
                revalidate_artifact_png_bytes(
                    artifact_path=outside_file,
                    containment_root=self.root,
                    expected_sha256=hashlib.sha256(png_bytes).hexdigest(),
                    expected_bytes=len(png_bytes),
                    expected_width=48,
                    expected_height=48,
                )
            self.assertEqual(ctx_escape.exception.code, "ryn_js_image_outside_containment")

        # Intentional metadata dimension mismatch (declaring 64x64 for a 48x48 PNG) fails closed.
        bad_dim_receipt = self.verifier.register_owned_png(
            artifact_id="art_bad_dim",
            receipt_id="rcpt_bad_dim",
            task_id="task_tamper",
            operation_id="op_bad_dim",
            window={"app": "notepad.exe", "id": 101},
            native_window=nat_win,
            width=64,
            height=64,
            png_bytes=png_bytes,
        )
        with self.assertRaises(JsProtocolError) as ctx_bad_dim:
            self.registry.issue_screenshot_capability(
                bad_dim_receipt,
                issuance_context=HostImageIssuanceContext(
                    owner_id="owner_test_1",
                    node_runtime_id="node_dim",
                    task_id="task_tamper",
                    session_id="sess_dim",
                    generation=1,
                    turn=1,
                    execution_id="exec_dim",
                    callback_id="cb_dim",
                    request_id="req_dim",
                    window={"app": "notepad.exe", "id": 101},
                    native_window=nat_win,
                    native_worker_instance="fixture_native_worker_1",
                    operation_id="op_bad_dim",
                ),
            )
        self.assertEqual(ctx_bad_dim.exception.code, "ryn_js_image_artifact_changed")
        (self.root / "art_bad_dim.png").unlink()

        # Bounded 16-entry RAM metadata check across >= 1000 issued capabilities and drained promotions in one session:
        cap_test_reg = HostImageRegistry(
            owner_id="owner_cap_test",
            artifact_verifier=self.verifier,
            max_entries_per_session=MAX_SESSION_IMAGE_ENTRIES,
        )
        cap_carrier = RecordingImageCarrierCallback()
        for idx in range(1050):
            sky_id = 1000 + idx
            win_k = f"window_owned_{sky_id}"
            nw_idx = make_native_window_ref(window_key=win_k, hwnd=str(sky_id), app_id="notepad.exe")
            r_idx = self.verifier.register_owned_png(
                artifact_id=f"art_cap_{idx}",
                receipt_id=f"rcpt_cap_{idx}",
                task_id="task_cap_1050",
                operation_id=f"op_cap_{idx}",
                window={"app": "notepad.exe", "id": sky_id},
                native_window=nw_idx,
                observation_id=f"obs_cap_{idx}",
                width=48,
                height=48,
                png_bytes=png_bytes,
                filename="shared_cap_test.png",
                write_file=(idx == 0),
            )
            b_idx = cap_test_reg.issue_screenshot_capability(
                r_idx,
                issuance_context=HostImageIssuanceContext(
                    owner_id="owner_cap_test",
                    node_runtime_id="node_cap",
                    task_id="task_cap_1050",
                    session_id="sess_cap_1050",
                    generation=1,
                    turn=1,
                    execution_id=f"exec_cap_{idx}",
                    callback_id=f"cb_cap_{idx}",
                    request_id=f"req_cap_{idx}",
                    window={"app": "notepad.exe", "id": sky_id},
                    native_window=nw_idx,
                    native_worker_instance="fixture_native_worker_1",
                    operation_id=f"op_cap_{idx}",
                ),
            )
            cap_test_reg.validate_and_queue_request(
                {
                    "provenance": "host_window_state",
                    "id": b_idx.screenshot_id,
                    "url": b_idx.capability_url,
                    "sha256": b_idx.sha256,
                    "window": b_idx.window,
                },
                owner_id="owner_cap_test",
                node_runtime_id="node_cap",
                task_id="task_cap_1050",
                session_id="sess_cap_1050",
                generation=1,
                turn=1,
                emitting_execution_id=f"exec_cap_{idx}",
            )
            cap_test_reg.drain_promotions(
                task_id="task_cap_1050",
                session_id="sess_cap_1050",
                execution_id=f"exec_cap_{idx}",
                carrier=cap_carrier,
            )
            self.assertLessEqual(len(cap_test_reg._capabilities_by_token), MAX_SESSION_IMAGE_ENTRIES)
            self.assertLessEqual(len(cap_test_reg._session_tokens.get("sess_cap_1050", ())), MAX_SESSION_IMAGE_ENTRIES)
            self.assertEqual(len(cap_test_reg._promotion_queue), 0)
            self.assertEqual(len(cap_test_reg._session_promotions.get("sess_cap_1050", ())), 0)

        # Quota / promotion byte budget check on HostImageRegistry:
        quota_png = build_valid_png_bytes(
            width=48,
            height=48,
            target_min_bytes=2048,
            seed_byte=0x77,
        )
        tight_registry = HostImageRegistry(
            owner_id="owner_tight",
            artifact_verifier=self.verifier,
            max_entries_per_session=2,
            max_active_pins=1,
            max_total_promotion_bytes=len(quota_png),
        )
        nw_201 = make_native_window_ref(window_key="window_owned_201", hwnd="201", app_id="notepad.exe")
        nw_202 = make_native_window_ref(window_key="window_owned_202", hwnd="202", app_id="notepad.exe")
        r_q1 = self.verifier.register_owned_png(
            artifact_id="art_q1",
            receipt_id="rcpt_q1",
            task_id="task_quota",
            operation_id="op_q1",
            window={"app": "notepad.exe", "id": 201},
            native_window=nw_201,
            observation_id="obs_q1",
            png_bytes=quota_png,
        )
        r_q2 = self.verifier.register_owned_png(
            artifact_id="art_q2",
            receipt_id="rcpt_q2",
            task_id="task_quota",
            operation_id="op_q2",
            window={"app": "notepad.exe", "id": 202},
            native_window=nw_202,
            observation_id="obs_q2",
            png_bytes=quota_png,
        )
        b1 = tight_registry.issue_screenshot_capability(
            r_q1,
            issuance_context=HostImageIssuanceContext(
                owner_id="owner_tight",
                node_runtime_id="node_q",
                task_id="task_quota",
                session_id="sess_q",
                generation=1,
                turn=1,
                execution_id="exec_q1",
                callback_id="cb_q1",
                request_id="req_q1",
                window={"app": "notepad.exe", "id": 201},
                native_window=nw_201,
                native_worker_instance="fixture_native_worker_1",
                operation_id="op_q1",
            ),
        )
        b2 = tight_registry.issue_screenshot_capability(
            r_q2,
            issuance_context=HostImageIssuanceContext(
                owner_id="owner_tight",
                node_runtime_id="node_q",
                task_id="task_quota",
                session_id="sess_q",
                generation=1,
                turn=1,
                execution_id="exec_q1",
                callback_id="cb_q2",
                request_id="req_q2",
                window={"app": "notepad.exe", "id": 202},
                native_window=nw_202,
                native_worker_instance="fixture_native_worker_1",
                operation_id="op_q2",
            ),
        )
        for b in (b1, b2):
            tight_registry.validate_and_queue_request(
                {
                    "provenance": "host_window_state",
                    "id": b.screenshot_id,
                    "url": b.capability_url,
                    "sha256": b.sha256,
                    "window": b.window,
                },
                owner_id="owner_tight",
                node_runtime_id="node_q",
                task_id="task_quota",
                session_id="sess_q",
                generation=1,
                turn=1,
                emitting_execution_id="exec_q1",
            )
        # Draining both when max_total_promotion_bytes == len(png_bytes) promotes the first and fails the second.
        q_results = tight_registry.drain_promotions(
            task_id="task_quota",
            session_id="sess_q",
            execution_id="exec_q1",
            carrier=RecordingImageCarrierCallback(),
        )
        self.assertEqual(len(q_results), 2)
        self.assertEqual(q_results[0].status, "promoted")
        self.assertEqual(q_results[1].status, "unavailable")
        self.assertEqual(q_results[1].error_code, "ryn_js_image_promotion_budget_exceeded")
        self.assertEqual(len(tight_registry._promotion_queue), 0)

    def test_snapshot_stdout_return_error_sanitization_and_guest_limits_preserved(self) -> None:
        """Capability URLs and inline data URLs are scrubbed from snapshot keys/values/stdout/errors, and 128KB cap holds."""
        png_bytes = build_valid_png_bytes(width=24, height=24, seed_byte=0x44)
        png_bytes_2 = build_valid_png_bytes(width=24, height=24, seed_byte=0x55)
        b64_small = base64.b64encode(png_bytes).decode("ascii")
        self.assertLessEqual(len(b64_small), MAX_GUEST_INLINE_BASE64_BYTES)

        nat_win_101 = make_native_window_ref(window_key="window_owned_101", hwnd="101", app_id="notepad.exe")
        nat_win_102 = make_native_window_ref(window_key="window_owned_102", hwnd="102", app_id="notepad.exe")
        receipt_1 = self.verifier.register_owned_png(
            artifact_id="art_san_1",
            receipt_id="rcpt_san_1",
            task_id="task_san",
            operation_id="op_san_1",
            window={"app": "notepad.exe", "id": 101},
            native_window=nat_win_101,
            state_token="st_san_1",
            observation_id="obs_san_1",
            png_bytes=png_bytes,
        )
        receipt_2 = self.verifier.register_owned_png(
            artifact_id="art_san_2",
            receipt_id="rcpt_san_2",
            task_id="task_san",
            operation_id="op_san_2",
            window={"app": "notepad.exe", "id": 102},
            native_window=nat_win_102,
            state_token="st_san_2",
            observation_id="obs_san_2",
            png_bytes=png_bytes_2,
        )

        exec_1, gen_1 = self.coordinator.start(
            "task_san",
            f"""
            const {{ sky }} = await import("@oai/sky");
            const state1 = await sky.get_window_state({{
              window: {{ app: "notepad.exe", id: 101 }},
              include_screenshot: true,
            }});
            const state2 = await sky.get_window_state({{
              window: {{ app: "notepad.exe", id: 102 }},
              include_screenshot: true,
            }});
            const saved1 = state1.screenshots[0];
            const saved2 = state2.screenshots[0];
            globalThis.savedSan1 = saved1;
            globalThis.savedSan2 = saved2;
            const inlineDataUrl = "data:image/png;base64,{b64_small}";
            console.log("leaked_stdout=" + JSON.stringify({{ [saved1.url]: saved2.url }}));
            return {{
              [saved1.url]: "owned_return_key",
              normal_key: "normal_value",
              artifact_id: "art_san_1",
              nested: {{
                [saved1.url]: "owned_nested_key",
                cap: saved1.url,
              }},
              array_nested: [
                {{
                  [saved1.url]: "in_array_cap_key",
                  [inlineDataUrl]: "in_array_data_key",
                }},
              ],
              two_caps: {{
                [saved1.url]: "first_cap_val",
                [saved2.url]: "second_cap_val",
              }},
              collision_with_reserved: {{
                [saved1.url]: "redacted_cap_1",
                "{REDACTED_CAPABILITY_PLACEHOLDER}": "literal_reserved_placeholder",
                [saved2.url]: "redacted_cap_2",
              }},
            }};
            """,
        )
        step_1 = self.coordinator.poll_step("task_san", timeout=2.0)
        assert step_1.host_call is not None
        self.coordinator.mark_dispatched(
            "task_san",
            execution_id=exec_1,
            generation=gen_1,
            callback_id=step_1.host_call.callback_id,
            operation_id="op_san_1",
        )
        self.coordinator.resolve_window_state_from_outbox(
            "task_san",
            prepare_durable_window_state_payload(
                {
                    "window": {"app": "notepad.exe", "id": 101},
                    "native_window": nat_win_101,
                    "native_worker_instance": "fixture_native_worker_1",
                    "screenshots": [receipt_1],
                }
            ),
            execution_id=exec_1,
            generation=gen_1,
            callback_id=step_1.host_call.callback_id,
            operation_id="op_san_1",
            native_worker_instance="fixture_native_worker_1",
            native_window=nat_win_101,
            outbox_committed=True,
        )

        step_2 = self.coordinator.poll_step("task_san", timeout=2.0)
        assert step_2.host_call is not None
        self.coordinator.mark_dispatched(
            "task_san",
            execution_id=exec_1,
            generation=gen_1,
            callback_id=step_2.host_call.callback_id,
            operation_id="op_san_2",
        )
        self.coordinator.resolve_window_state_from_outbox(
            "task_san",
            prepare_durable_window_state_payload(
                {
                    "window": {"app": "notepad.exe", "id": 102},
                    "native_window": nat_win_102,
                    "native_worker_instance": "fixture_native_worker_1",
                    "screenshots": [receipt_2],
                }
            ),
            execution_id=exec_1,
            generation=gen_1,
            callback_id=step_2.host_call.callback_id,
            operation_id="op_san_2",
            native_worker_instance="fixture_native_worker_1",
            native_window=nat_win_102,
            outbox_committed=True,
        )

        done = self.coordinator.poll_step("task_san", timeout=2.0)
        self.assertEqual(done.status, "completed")
        active_tokens = list(self.registry._capabilities_by_token.keys())
        self.assertEqual(len(active_tokens), 2)

        snapshot_dict = done.snapshot.to_dict()
        snapshot_json = json.dumps(snapshot_dict)
        self.assertNotIn(CAPABILITY_URL_PREFIX, snapshot_json)
        self.assertNotIn("data:image/png;base64,", snapshot_json)
        self.assertNotIn(b64_small, snapshot_json)
        for tok in active_tokens:
            self.assertNotIn(tok, snapshot_json)

        self.assertIn(REDACTED_CAPABILITY_PLACEHOLDER, done.snapshot.stdout)
        self.assertNotIn(CAPABILITY_URL_PREFIX, done.snapshot.stdout)

        # Verify top-level key, nested key/value, array-nested keys, two-cap collision, and reserved placeholder collision.
        val = snapshot_dict["value"]
        self.assertEqual(val[REDACTED_CAPABILITY_PLACEHOLDER], "owned_return_key")
        self.assertEqual(val["normal_key"], "normal_value")
        self.assertEqual(val["artifact_id"], "art_san_1")
        self.assertEqual(
            val["nested"],
            {
                REDACTED_CAPABILITY_PLACEHOLDER: "owned_nested_key",
                "cap": REDACTED_CAPABILITY_PLACEHOLDER,
            },
        )
        self.assertEqual(
            val["array_nested"],
            [
                {
                    REDACTED_CAPABILITY_PLACEHOLDER: "in_array_cap_key",
                    REDACTED_DATA_URL_PLACEHOLDER: "in_array_data_key",
                }
            ],
        )
        self.assertEqual(
            val["two_caps"],
            {
                REDACTED_CAPABILITY_PLACEHOLDER: "first_cap_val",
                f"{REDACTED_CAPABILITY_PLACEHOLDER}#2": "second_cap_val",
            },
        )
        self.assertEqual(
            val["collision_with_reserved"],
            {
                REDACTED_CAPABILITY_PLACEHOLDER: "literal_reserved_placeholder",
                f"{REDACTED_CAPABILITY_PLACEHOLDER}#2": "redacted_cap_1",
                f"{REDACTED_CAPABILITY_PLACEHOLDER}#3": "redacted_cap_2",
            },
        )

        # Original RAM capabilities remain intact and valid for promotion in the current turn (stage="prepared").
        carrier_1 = RecordingImageCarrierCallback(return_stage="prepared")
        promos_1 = self.coordinator.drain_image_promotions(
            "task_san",
            execution_id=exec_1,
            carrier=carrier_1,
        )
        self.assertEqual(len(promos_1), 2)
        self.assertEqual(promos_1[0].status, "promoted")
        self.assertEqual(promos_1[0].stage, "prepared")
        self.assertEqual(promos_1[1].status, "promoted")
        self.assertEqual(promos_1[1].stage, "prepared")
        self.assertEqual(carrier_1.attachments[0].png_bytes, png_bytes)
        self.assertEqual(carrier_1.attachments[1].png_bytes, png_bytes_2)
        self.coordinator.retire_task("task_san", execution_id=exec_1, generation=gen_1)

        # Re-emitting savedSan1 in the next cell of the same turn still succeeds, proving guest/RAM descriptor was not mutated.
        exec_reemit, gen_reemit = self.coordinator.start(
            "task_san",
            "return await nodeRepl.emitImage(globalThis.savedSan1);",
        )
        done_reemit = self.coordinator.poll_step("task_san", timeout=2.0)
        self.assertEqual(done_reemit.status, "completed")
        carrier_reemit = RecordingImageCarrierCallback(return_stage="prepared")
        promos_reemit = self.coordinator.drain_image_promotions(
            "task_san",
            execution_id=exec_reemit,
            carrier=carrier_reemit,
        )
        self.assertEqual(len(promos_reemit), 1)
        self.assertEqual(promos_reemit[0].status, "promoted")
        self.assertEqual(promos_reemit[0].stage, "prepared")
        self.assertEqual(carrier_reemit.attachments[0].png_bytes, png_bytes)
        self.coordinator.retire_task("task_san", execution_id=exec_reemit, generation=gen_reemit)

        # Guest thrown error containing capability URL in message and JSON payload is also scrubbed in public snapshot.
        exec_err, gen_err = self.coordinator.start(
            "task_san",
            """
            throw new Error("err_leak=" + JSON.stringify({ [globalThis.savedSan1.url]: globalThis.savedSan2.url }));
            """,
        )
        done_err = self.coordinator.poll_step("task_san", timeout=2.0)
        self.assertEqual(done_err.status, "failed")
        err_dict_json = json.dumps(done_err.snapshot.to_dict())
        self.assertNotIn(CAPABILITY_URL_PREFIX, err_dict_json)
        self.assertIn(REDACTED_CAPABILITY_PLACEHOLDER, err_dict_json)
        for tok in active_tokens:
            self.assertNotIn(tok, err_dict_json)
        self.coordinator.retire_task("task_san", execution_id=exec_err, generation=gen_err)

        # Small valid guest inline data:image/png;base64 (<= 128 KiB) still succeeds with sanitized metadata.
        exec_2, gen_2 = self.coordinator.start(
            "task_san",
            f"""
            const summary = await nodeRepl.emitImage("data:image/png;base64,{b64_small}");
            return summary;
            """,
        )
        done_2 = self.coordinator.poll_step("task_san", timeout=2.0)
        self.assertEqual(done_2.status, "completed")
        self.assertEqual(done_2.snapshot.value["provenance"], "guest_inline_data_url")
        self.assertEqual(len(done_2.snapshot.images), 1)
        self.assertEqual(done_2.snapshot.images[0]["provenance"], "guest_inline_data_url")
        self.assertNotIn("url", done_2.snapshot.images[0])
        self.coordinator.retire_task("task_san", execution_id=exec_2, generation=gen_2)

        # Oversized guest inline data URL (> 128 KiB base64) still fails closed with ryn_js_image_limit_exceeded.
        oversized_b64 = "A" * (MAX_GUEST_INLINE_BASE64_BYTES + 1024)
        exec_3, gen_3 = self.coordinator.start(
            "task_san",
            f'await nodeRepl.emitImage("data:image/png;base64,{oversized_b64}");',
        )
        done_3 = self.coordinator.poll_step("task_san", timeout=2.0)
        self.assertEqual(done_3.status, "failed")
        assert done_3.snapshot.error is not None
        self.assertEqual(done_3.snapshot.error["code"], "ryn_js_image_limit_exceeded")
        self.coordinator.retire_task("task_san", execution_id=exec_3, generation=gen_3)


if __name__ == "__main__":
    unittest.main()
