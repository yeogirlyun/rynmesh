"""Tests for native computer observation descriptor trust boundaries, Rust WindowRef identity
normalization, lossless crop/tile pixel-to-geometry binding, and bounded file/handle protections.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import os
import random
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from rynmesh.agent_runtime import image_capabilities as ic


def _make_marked_png(
    width: int = 800,
    height: int = 600,
    *,
    base_color: tuple[int, int, int] = (24, 36, 52),
    markers: dict[tuple[int, int], tuple[int, int, int]] | None = None,
    noisy: bool = False,
    seed: int = 20261019,
) -> bytes:
    if noisy:
        rng = random.Random(seed)
        raw = rng.randbytes(width * height * 3)
        img = Image.frombytes("RGB", (width, height), raw)
    else:
        img = Image.new("RGB", (width, height), base_color)

    for (mx, my), rgb in (markers or {}).items():
        img.putpixel((mx, my), rgb)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _rust_window_ref(
    *,
    worker_instance: str = "worker-native-win32-01",
    window_key: str = "win-key-001204a0-4820-1",
    hwnd: str = "0x001204a0",
    pid: int = 4820,
    process_started: str = "133740000000000000",
    generation: int = 3,
) -> dict:
    return {
        "workerInstance": worker_instance,
        "windowKey": window_key,
        "hwnd": hwnd,
        "pid": pid,
        "processStarted": process_started,
        "generation": generation,
    }


def _base_descriptor_inputs(
    png_bytes: bytes,
    width: int = 800,
    height: int = 600,
    *,
    task_id: str = "task_trust_01",
    operation_id: str = "op_capture_01",
    observation_id: str = "obs_snap_01",
    state_token: str = "state_tok_01",
    window_ref: dict | None = None,
) -> dict:
    wref = copy.deepcopy(window_ref) if window_ref is not None else _rust_window_ref()
    return {
        "task_id": task_id,
        "operation_id": operation_id,
        "observation_id": observation_id,
        "state_token": state_token,
        "window_ref": wref,
        "source_sha256": hashlib.sha256(png_bytes).hexdigest(),
        "source_width": width,
        "source_height": height,
        "source_bytes": len(png_bytes),
        "dpi": 144,
        "dpi_scale": 1.5,
        "window_rect": {"x": -1920, "y": -150, "width": width + 16, "height": height + 39},
        "client_rect": {"x": -1912, "y": -119, "width": width, "height": height},
        "physical_origin": [8, 31],
        "screen_origin": [-1912, -119],
    }


def _write_native_file(
    workspace: Path,
    task_id: str,
    operation_id: str,
    raw_bytes: bytes,
    *,
    index: int = 0,
    ext: str = ".png",
    mime_type: str = "image/png",
    width: int | None = None,
    height: int | None = None,
) -> tuple[Path, dict]:
    safe_task = ic.safe_segment(task_id, "task")
    safe_op = ic.safe_segment(operation_id, "op")
    rel_path = Path(".ryn") / "computer" / safe_task / safe_op / f"image_{index}{ext}"
    full_path = workspace / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(raw_bytes)
    if width is None or height is None:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            w, h = img.size
    else:
        w, h = width, height
    return full_path, {
        "path": rel_path.as_posix(),
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "bytes": len(raw_bytes),
        "mime_type": mime_type,
        "width": w,
        "height": h,
    }


def test_unsealed_self_authored_and_stripped_seal_copy_rejected_by_public_validator_loader_and_resolver(tmp_path):
    """Verify P1A:
    - An arbitrary unsealed dictionary (even with self-declared provenance='native_computer_observation'
      and coordinate_authority=True, and no MCP/guest denylist markers) is rejected by public
      validate_native_observation_descriptor, _validate_native_observation_descriptor,
      verify_and_load_native_observation_artifact, and resolve_computer_image_point.
    - A copy of a valid host-issued descriptor with _host_seal removed ('nosealcopy') is also
      rejected by all public entrypoints without re-signing.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    raw_png = _make_marked_png(800, 600)
    wref = _rust_window_ref()
    base = _base_descriptor_inputs(raw_png, 800, 600, window_ref=wref)
    _, native_art = _write_native_file(workspace, base["task_id"], base["operation_id"], raw_png)

    expected_bindings = {
        "expected_task_id": base["task_id"],
        "expected_operation_id": base["operation_id"],
        "expected_observation_id": base["observation_id"],
        "current_state_token": base["state_token"],
        "expected_window_ref": wref,
        "expected_worker_instance_id": wref["workerInstance"],
    }

    # 1. Entirely self-authored unsealed dictionary claiming native provenance + coordinate_authority
    self_authored_unsealed = {
        **copy.deepcopy(base),
        "provenance": "native_computer_observation",
        "coordinate_authority": True,
    }
    with pytest.raises(ValueError, match="native_descriptor_missing_host_seal"):
        ic.validate_native_observation_descriptor(self_authored_unsealed, **expected_bindings)
    with pytest.raises(ValueError, match="native_descriptor_missing_host_seal"):
        ic._validate_native_observation_descriptor(self_authored_unsealed, **expected_bindings)
    with pytest.raises(ValueError, match="native_descriptor_missing_host_seal"):
        ic.verify_and_load_native_observation_artifact(
            native_art,
            descriptor=self_authored_unsealed,
            workspace_root=workspace,
            task_id=base["task_id"],
            operation_id=base["operation_id"],
            current_state_token=base["state_token"],
            expected_window_ref=wref,
            expected_observation_id=base["observation_id"],
            expected_worker_instance_id=wref["workerInstance"],
        )
    with pytest.raises(ValueError, match="native_descriptor_missing_host_seal"):
        ic.resolve_computer_image_point(self_authored_unsealed, 50, 60, **expected_bindings)
    assert "_host_seal" not in self_authored_unsealed

    # 2. Valid private issuance succeeds, but stripping _host_seal from a copy ('nosealcopy') fails closed
    sealed = ic._issue_native_observation_descriptor(**base)
    assert isinstance(sealed.get("_host_seal"), str) and len(sealed["_host_seal"]) == 64

    no_seal_copy = copy.deepcopy(sealed)
    del no_seal_copy["_host_seal"]

    with pytest.raises(ValueError, match="native_descriptor_missing_host_seal"):
        ic.validate_native_observation_descriptor(no_seal_copy, **expected_bindings)
    with pytest.raises(ValueError, match="native_descriptor_missing_host_seal"):
        ic.verify_and_load_native_observation_artifact(
            native_art,
            descriptor=no_seal_copy,
            workspace_root=workspace,
            task_id=base["task_id"],
            operation_id=base["operation_id"],
            expected_observation_id=base["observation_id"],
            current_state_token=base["state_token"],
            expected_window_ref=wref,
        )
    with pytest.raises(ValueError, match="native_descriptor_missing_host_seal"):
        ic.resolve_computer_image_point(no_seal_copy, 50, 60, **expected_bindings)


def test_forged_seal_post_seal_mutations_untrusted_tags_and_mcp_paths_rejected(tmp_path):
    """Verify invalid seals, forged HMACs, post-issuance field mutations, untrusted tags, and MCP paths all fail closed."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    raw_png = _make_marked_png(640, 480)
    wref = _rust_window_ref()
    base = _base_descriptor_inputs(raw_png, 640, 480, window_ref=wref)
    _, native_art = _write_native_file(workspace, base["task_id"], base["operation_id"], raw_png)
    sealed = ic._issue_native_observation_descriptor(**base)

    expected_bindings = {
        "expected_task_id": base["task_id"],
        "expected_operation_id": base["operation_id"],
        "expected_observation_id": base["observation_id"],
        "current_state_token": base["state_token"],
        "expected_window_ref": wref,
        "expected_worker_instance_id": wref["workerInstance"],
    }

    # 1. Invalid / forged _host_seal values
    for bad_seal, err_pattern in [
        (None, "native_descriptor_missing_host_seal"),
        ("", "native_descriptor_tampered_after_seal"),
        (12345, "native_descriptor_tampered_after_seal"),
        ("deadbeef", "native_descriptor_tampered_after_seal"),
        ("z" * 64, "native_descriptor_tampered_after_seal"),
        ("0" * 64, "native_descriptor_tampered_after_seal"),
        ("f" * 64, "native_descriptor_tampered_after_seal"),
    ]:
        forged = {**copy.deepcopy(sealed), "_host_seal": bad_seal}
        with pytest.raises(ValueError, match=err_pattern):
            ic.validate_native_observation_descriptor(forged, **expected_bindings)
        with pytest.raises(ValueError, match=err_pattern):
            ic.verify_and_load_native_observation_artifact(
                native_art,
                descriptor=forged,
                workspace_root=workspace,
                task_id=base["task_id"],
                operation_id=base["operation_id"],
                expected_observation_id=base["observation_id"],
                current_state_token=base["state_token"],
                expected_window_ref=wref,
            )
        with pytest.raises(ValueError, match=err_pattern):
            ic.resolve_computer_image_point(forged, 10, 10, **expected_bindings)

    # 2. Post-issuance mutations on a genuinely sealed descriptor (without re-signing)
    mutations = [
        lambda d: d.update(task_id="task_mutated"),
        lambda d: d.update(operation_id="op_mutated"),
        lambda d: d.update(observation_id="obs_mutated"),
        lambda d: d.update(state_token="tok_mutated"),
        lambda d: d["window_ref"].update(pid=99999),
        lambda d: d["window_ref"].update(window_key="win-key-mutated"),
        lambda d: d["window_ref"].update(generation=99),
        lambda d: d["physical_origin"].__setitem__(0, 0),
        lambda d: d["screen_origin"].__setitem__(1, 0),
        lambda d: d["window_rect"].update(x=0),
        lambda d: d["client_rect"].update(y=0),
        lambda d: d["representation"].update(origin=[10, 10]),
        lambda d: d["representation"].update(scale=[2.0, 2.0]),
        lambda d: d.update(extra_unsealed_field="injected"),
    ]
    for mutate in mutations:
        tampered = copy.deepcopy(sealed)
        mutate(tampered)
        with pytest.raises(ValueError, match="native_descriptor_tampered_after_seal"):
            ic.validate_native_observation_descriptor(tampered, **expected_bindings)
        with pytest.raises(ValueError, match="native_descriptor_tampered_after_seal"):
            ic.verify_and_load_native_observation_artifact(
                native_art,
                descriptor=tampered,
                workspace_root=workspace,
                task_id=base["task_id"],
                operation_id=base["operation_id"],
                expected_observation_id=base["observation_id"],
                current_state_token=base["state_token"],
                expected_window_ref=wref,
            )
        with pytest.raises(ValueError, match="native_descriptor_tampered_after_seal"):
            ic.resolve_computer_image_point(tampered, 10, 10, **expected_bindings)

    # 3a. Untrusted MCP / guest markers BEFORE private issuance (or on unsealed inputs) must refuse with native_descriptor_untrusted_source
    untrusted_marker_variants = [
        {"_untrusted_mcp_artifact": True},
        {"_guest_relabeled": True},
        {"provenance": "mcp_visual_only"},
        {"provenance": "guest"},
        {"source": "mcp"},
        {"source": "tool_result"},
        {"mcp_artifacts": []},
        {"tool_call_id": "call_123"},
        {"path": ".ryn/mcp/task_trust_01/op_capture_01/image_0.png"},
    ]
    for marker in untrusted_marker_variants:
        unsealed_with_marker = {**copy.deepcopy(base), **marker}
        with pytest.raises(ValueError, match=r"^native_descriptor_untrusted_source$"):
            ic._issue_native_observation_descriptor(**unsealed_with_marker)
        with pytest.raises(ValueError, match=r"^native_descriptor_untrusted_source$"):
            ic.validate_native_observation_descriptor(unsealed_with_marker, **expected_bindings)
        with pytest.raises(ValueError, match=r"^native_descriptor_untrusted_source$"):
            ic.verify_and_load_native_observation_artifact(
                native_art,
                descriptor=unsealed_with_marker,
                workspace_root=workspace,
                task_id=base["task_id"],
                operation_id=base["operation_id"],
                expected_observation_id=base["observation_id"],
                current_state_token=base["state_token"],
                expected_window_ref=wref,
            )

        # Even if an untrusted marker is injected AFTER trusted seal issuance, validator and loader
        # refuse with native_descriptor_untrusted_source before seal comparison
        tagged_sealed = {**copy.deepcopy(sealed), **marker}
        with pytest.raises(ValueError, match=r"^native_descriptor_untrusted_source$"):
            ic.validate_native_observation_descriptor(tagged_sealed, **expected_bindings)
        with pytest.raises(ValueError, match=r"^native_descriptor_untrusted_source$"):
            ic.verify_and_load_native_observation_artifact(
                native_art,
                descriptor=tagged_sealed,
                workspace_root=workspace,
                task_id=base["task_id"],
                operation_id=base["operation_id"],
                expected_observation_id=base["observation_id"],
                current_state_token=base["state_token"],
                expected_window_ref=wref,
            )

    # 3b. coordinate_authority=False or non-marker provenance mutation:
    # - Before private issuance -> refuses with native_descriptor_untrusted_source
    # - Mutated AFTER trusted seal -> validator/loader refuse with native_descriptor_tampered_after_seal,
    #   and point resolver refuses with coordinate_authority_denied
    for pre_issue_bad in (
        {"coordinate_authority": False},
        {"provenance": "custom_guest_tag"},
    ):
        with pytest.raises(ValueError, match=r"^native_descriptor_untrusted_source$"):
            ic._issue_native_observation_descriptor(**{**copy.deepcopy(base), **pre_issue_bad})

        mutated_after_seal = {**copy.deepcopy(sealed), **pre_issue_bad}
        with pytest.raises(ValueError, match=r"^native_descriptor_tampered_after_seal$"):
            ic.validate_native_observation_descriptor(mutated_after_seal, **expected_bindings)
        with pytest.raises(ValueError, match=r"^native_descriptor_tampered_after_seal$"):
            ic.verify_and_load_native_observation_artifact(
                native_art,
                descriptor=mutated_after_seal,
                workspace_root=workspace,
                task_id=base["task_id"],
                operation_id=base["operation_id"],
                expected_observation_id=base["observation_id"],
                current_state_token=base["state_token"],
                expected_window_ref=wref,
            )
        with pytest.raises(ValueError, match=r"^coordinate_authority_denied$"):
            ic.resolve_computer_image_point(mutated_after_seal, 10, 10, **expected_bindings)

    # Confirm the unmutated host-sealed original descriptor remains valid across validator, loader, and resolver
    assert ic.validate_native_observation_descriptor(sealed, **expected_bindings) == sealed
    valid_loaded = ic.verify_and_load_native_observation_artifact(
        native_art,
        descriptor=sealed,
        workspace_root=workspace,
        task_id=base["task_id"],
        operation_id=base["operation_id"],
        expected_observation_id=base["observation_id"],
        current_state_token=base["state_token"],
        expected_window_ref=wref,
    )
    assert valid_loaded["coordinate_authority"] is True
    assert ic.resolve_computer_image_point(sealed, 10, 10, loaded_artifact=valid_loaded, **expected_bindings)["image_point"] == (10, 10)

    # 4. Native artifact loader rejects MCP paths and non-canonical native filenames
    safe_task = ic.safe_segment(base["task_id"], "task")
    safe_op = ic.safe_segment(base["operation_id"], "op")
    mcp_rel = f".ryn/mcp/{safe_task}/{safe_op}/image_0.png"
    mcp_file = workspace / mcp_rel
    mcp_file.parent.mkdir(parents=True, exist_ok=True)
    mcp_file.write_bytes(raw_png)

    for bad_path in [
        mcp_rel,
        f".ryn/computer/{safe_task}/{safe_op}/screenshot.png",
        f".ryn/computer/{safe_task}/other_op/image_0.png",
        f".ryn/computer/other_task/{safe_op}/image_0.png",
        "../image_0.png",
    ]:
        bad_art = {**copy.deepcopy(native_art), "path": bad_path}
        with pytest.raises(ValueError, match="artifact_path_outside_native_computer_scope"):
            ic.verify_and_load_native_observation_artifact(
                bad_art,
                descriptor=sealed,
                workspace_root=workspace,
                task_id=base["task_id"],
                operation_id=base["operation_id"],
                expected_observation_id=base["observation_id"],
                current_state_token=base["state_token"],
                expected_window_ref=wref,
            )


def test_trusted_private_issuer_rust_window_ref_identity_bindings_and_missing_wildcard_refusals(tmp_path):
    """Verify:
    - Full Rust camelCase WindowRef (workerInstance, windowKey, hwnd, pid, processStarted, generation)
      and legacy snake_case WindowRef (worker_instance_id, window_key, hwnd, pid, creation_time, generation)
      are normalized with window_key required and preserved on both spellings.
    - Invalid WindowRef fields (missing windowKey/window_key on either camelCase or snake_case, mixed-alias
      conflicts, pid <= 0, non-decimal processStarted/creation_time, zero/invalid hwnd, generation < 1,
      or invented from guest title/app/id) are rejected by both private issuer and public verifier.
    - Positive original descriptor loads and maps negative multi-monitor origin, 144 DPI, and client
      border offset accurately without double DPI multiplication.
    - Stale/mismatched task, op, obs, state_token, worker, or any WindowRef field (in both camelCase
      and snake_case) is rejected.
    - Omitting, passing None/empty, or passing a foreign expected_observation_id (or any required binding)
      in verify_and_load_native_observation_artifact fails closed BEFORE opening or reading the artifact file.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    raw_png = _make_marked_png(800, 600, markers={(300, 200): (250, 12, 34)})
    wref = _rust_window_ref(
        worker_instance="worker-rust-77",
        window_key="win-key-77-0x001204a0",
        hwnd="0x001204a0",
        pid=8192,
        process_started="133750000000000000",
        generation=4,
    )
    snake_wref = {
        "worker_instance_id": "worker-rust-77",
        "window_key": "win-key-77-0x001204a0",
        "hwnd": "0x001204a0",
        "pid": 8192,
        "creation_time": "133750000000000000",
        "generation": 4,
    }
    base = _base_descriptor_inputs(raw_png, 800, 600, window_ref=wref)
    _, native_art = _write_native_file(workspace, base["task_id"], base["operation_id"], raw_png)

    sealed = ic._issue_native_observation_descriptor(**base)
    sealed_from_snake = ic._issue_native_observation_descriptor(**{**base, "window_ref": snake_wref})
    assert sealed["window_ref"] == snake_wref
    assert sealed_from_snake["window_ref"] == snake_wref

    expected_bindings = {
        "expected_task_id": base["task_id"],
        "expected_operation_id": base["operation_id"],
        "expected_observation_id": base["observation_id"],
        "current_state_token": base["state_token"],
        "expected_window_ref": wref,
        "expected_worker_instance_id": "worker-rust-77",
    }

    validated = ic.validate_native_observation_descriptor(sealed, **expected_bindings)
    assert validated == sealed
    assert ic.validate_native_observation_descriptor(sealed, **{**expected_bindings, "expected_window_ref": snake_wref}) == sealed

    loaded = ic.verify_and_load_native_observation_artifact(
        native_art,
        descriptor=sealed,
        workspace_root=workspace,
        task_id=base["task_id"],
        operation_id=base["operation_id"],
        current_state_token=base["state_token"],
        expected_window_ref=wref,
        expected_observation_id=base["observation_id"],
        expected_worker_instance_id="worker-rust-77",
    )
    assert loaded["coordinate_authority"] is True
    assert loaded["provenance"] == "native_computer_observation"
    assert loaded["original_dimensions_preserved"] is True
    assert loaded["width"] == 800 and loaded["height"] == 600
    assert loaded["sha256"] == base["source_sha256"]
    assert loaded["payload_sha256"] == base["source_sha256"]
    decoded_bytes = base64.b64decode(loaded["data_url"].split(",", 1)[1])
    assert decoded_bytes == raw_png

    mapped = ic.resolve_computer_image_point(
        sealed,
        300,
        200,
        require_client_area=True,
        loaded_artifact=loaded,
        **expected_bindings,
    )
    assert mapped["image_point"] == (300, 200)
    assert mapped["source_point"] == (300, 200)
    assert mapped["window_point"] == (308, 231)
    assert mapped["screen_point"] == (-1612, 81)
    assert mapped["client_point"] == (300, 200)
    assert mapped["within_client_area"] is True
    assert mapped["dpi"] == 144
    assert mapped["dpi_scale"] == 1.5
    assert mapped["window_ref"]["window_key"] == "win-key-77-0x001204a0"
    assert mapped["attached_pixels_verified"] is True

    # 1. Invalid WindowRef shapes on BOTH camelCase and snake_case (including missing windowKey/window_key, mixed alias conflicts, guest title)
    invalid_wrefs = [
        # Missing each required field in Rust camelCase
        {k: v for k, v in wref.items() if k != "workerInstance"},
        {k: v for k, v in wref.items() if k != "windowKey"},
        {k: v for k, v in wref.items() if k != "hwnd"},
        {k: v for k, v in wref.items() if k != "pid"},
        {k: v for k, v in wref.items() if k != "processStarted"},
        {k: v for k, v in wref.items() if k != "generation"},
        # Missing each required field in legacy snake_case (including missing window_key!)
        {k: v for k, v in snake_wref.items() if k != "worker_instance_id"},
        {k: v for k, v in snake_wref.items() if k != "window_key"},
        {k: v for k, v in snake_wref.items() if k != "hwnd"},
        {k: v for k, v in snake_wref.items() if k != "pid"},
        {k: v for k, v in snake_wref.items() if k != "creation_time"},
        {k: v for k, v in snake_wref.items() if k != "generation"},
        # Conflicting mixed camelCase + snake_case aliases
        {**wref, "worker_instance_id": "conflicting-worker"},
        {**wref, "window_key": "conflicting-window-key"},
        {**wref, "HWND": "0x00999999"},
        {**wref, "PID": 9999},
        {**wref, "creation_time": "133750000000000999"},
        {**wref, "window_generation": 99},
        # Empty windowKey / window_key
        {**wref, "windowKey": "   "},
        {**snake_wref, "window_key": ""},
        # Guest title, app name, or window_id injected into WindowRef
        {**wref, "title": "Untitled - Notepad"},
        {**snake_wref, "app": "notepad.exe"},
        {"title": "Untitled - Notepad", "window_id": 1001, "pid": 8192},
        # Zero / negative / boolean PID
        {**wref, "pid": 0},
        {**snake_wref, "pid": -5},
        {**wref, "pid": True},
        # Non-decimal processStarted / creation_time
        {**wref, "processStarted": "Notepad.exe"},
        {**snake_wref, "creation_time": "2026-10-19T18:00:00Z"},
        {**wref, "processStarted": "0"},
        {**snake_wref, "creation_time": -100},
        # Invalid hwnd
        {**wref, "hwnd": "0"},
        {**wref, "hwnd": "0x0"},
        {**wref, "hwnd": "0x00000000"},
        {**snake_wref, "hwnd": "not_a_handle"},
        {**wref, "hwnd": 0},
        # Invalid generation
        {**wref, "generation": 0},
        {**snake_wref, "generation": -1},
    ]
    for bad_wref in invalid_wrefs:
        with pytest.raises(ValueError, match=r"^native_descriptor_"):
            ic._issue_native_observation_descriptor(**{**base, "window_ref": bad_wref})
        with pytest.raises(ValueError, match=r"^native_descriptor_"):
            ic.validate_native_observation_descriptor(sealed, **{**expected_bindings, "expected_window_ref": bad_wref})
        with pytest.raises(ValueError, match=r"^native_descriptor_"):
            ic.resolve_computer_image_point(sealed, 100, 100, **{**expected_bindings, "expected_window_ref": bad_wref})

    # Also verify that a descriptor tampered to omit window_key from its inner window_ref is rejected by public verifier/resolver
    tampered_no_wk = copy.deepcopy(sealed)
    del tampered_no_wk["window_ref"]["window_key"]
    with pytest.raises(ValueError, match=r"^native_descriptor_tampered_after_seal$"):
        ic.validate_native_observation_descriptor(tampered_no_wk, **expected_bindings)
    with pytest.raises(ValueError, match=r"^native_descriptor_tampered_after_seal$"):
        ic.resolve_computer_image_point(tampered_no_wk, 100, 100, **expected_bindings)

    # 2. All identity mismatch negatives across both camelCase and snake_case expected_window_ref
    mismatch_cases = [
        ({"expected_task_id": "other_task"}, r"^native_descriptor_task_mismatch$"),
        ({"expected_operation_id": "other_op"}, r"^native_descriptor_operation_mismatch$"),
        ({"expected_observation_id": "other_obs"}, r"^native_descriptor_observation_mismatch$"),
        ({"current_state_token": "stale_token"}, r"^native_descriptor_stale_state_token$"),
        ({"expected_worker_instance_id": "other_worker"}, r"^native_descriptor_worker_mismatch$"),
        ({"expected_window_ref": {**wref, "workerInstance": "other_worker"}}, r"^native_descriptor_worker_mismatch$"),
        ({"expected_window_ref": {**snake_wref, "worker_instance_id": "other_worker"}}, r"^native_descriptor_worker_mismatch$"),
        ({"expected_window_ref": {**wref, "windowKey": "win-key-different"}}, r"^native_descriptor_window_mismatch$"),
        ({"expected_window_ref": {**snake_wref, "window_key": "win-key-different"}}, r"^native_descriptor_window_mismatch$"),
        ({"expected_window_ref": {**wref, "hwnd": "0x00999999"}}, r"^native_descriptor_window_mismatch$"),
        ({"expected_window_ref": {**snake_wref, "hwnd": "0x00999999"}}, r"^native_descriptor_window_mismatch$"),
        ({"expected_window_ref": {**wref, "pid": 9999}}, r"^native_descriptor_window_mismatch$"),
        ({"expected_window_ref": {**snake_wref, "pid": 9999}}, r"^native_descriptor_window_mismatch$"),
        ({"expected_window_ref": {**wref, "processStarted": "133750000000000001"}}, r"^native_descriptor_window_mismatch$"),
        ({"expected_window_ref": {**snake_wref, "creation_time": "133750000000000001"}}, r"^native_descriptor_window_mismatch$"),
        ({"expected_window_ref": {**wref, "generation": 5}}, r"^native_descriptor_window_mismatch$"),
        ({"expected_window_ref": {**snake_wref, "generation": 5}}, r"^native_descriptor_window_mismatch$"),
    ]
    for override, expected_err in mismatch_cases:
        call_kwargs = {**expected_bindings, **override}
        with pytest.raises(ValueError, match=expected_err):
            ic.validate_native_observation_descriptor(sealed, **call_kwargs)
        with pytest.raises(ValueError, match=expected_err):
            ic.resolve_computer_image_point(sealed, 100, 100, **call_kwargs)

    # 3. Missing required expected bindings (no missing-field wildcards or descriptor self-backfill!)
    for required_key in (
        "expected_task_id",
        "expected_operation_id",
        "expected_observation_id",
        "current_state_token",
        "expected_window_ref",
    ):
        missing_kwargs = {**expected_bindings, required_key: None}
        with pytest.raises(ValueError, match=r"^native_descriptor_"):
            ic.validate_native_observation_descriptor(sealed, **missing_kwargs)
        with pytest.raises(ValueError, match=r"^native_descriptor_"):
            ic.resolve_computer_image_point(sealed, 100, 100, **missing_kwargs)

    # 4. Native loader must require explicit expected_observation_id (even if artifact has observation_id)
    # and refuse omitted/None/empty/wrong expected_observation_id BEFORE opening or reading the file.
    art_with_obs = {**copy.deepcopy(native_art), "observation_id": base["observation_id"]}
    base_loader_kwargs = {
        "descriptor": sealed,
        "workspace_root": workspace,
        "task_id": base["task_id"],
        "operation_id": base["operation_id"],
        "expected_observation_id": base["observation_id"],
        "current_state_token": base["state_token"],
        "expected_window_ref": wref,
        "expected_worker_instance_id": "worker-rust-77",
    }
    loader_bad_cases = [
        # Omitted expected_observation_id entirely
        (
            {k: v for k, v in base_loader_kwargs.items() if k != "expected_observation_id"},
            r"^native_descriptor_expected_observation_id_required$",
        ),
        # Explicit None
        (
            {**base_loader_kwargs, "expected_observation_id": None},
            r"^native_descriptor_expected_observation_id_required$",
        ),
        # Empty / whitespace string
        (
            {**base_loader_kwargs, "expected_observation_id": "   "},
            r"^native_descriptor_expected_observation_id_required$",
        ),
        # Foreign / wrong observation_id
        (
            {**base_loader_kwargs, "expected_observation_id": "obs_foreign_99"},
            r"^native_descriptor_observation_mismatch$",
        ),
        # Other missing required bindings
        ({**base_loader_kwargs, "current_state_token": None}, r"^native_descriptor_current_state_token_required$"),
        ({**base_loader_kwargs, "expected_window_ref": None}, r"^native_descriptor_window_ref_invalid$"),
        ({**base_loader_kwargs, "task_id": ""}, r"^native_descriptor_expected_task_id_required$"),
        ({**base_loader_kwargs, "operation_id": ""}, r"^native_descriptor_expected_operation_id_required$"),
    ]
    for bad_loader_kwargs, err_regex in loader_bad_cases:
        with patch.object(ic.os, "open", side_effect=AssertionError("os.open should not be called before binding check")):
            with pytest.raises(ValueError, match=err_regex):
                ic.verify_and_load_native_observation_artifact(art_with_obs, **bad_loader_kwargs)
            with pytest.raises(ValueError, match=err_regex):
                ic._verify_and_load_native_observation_artifact(art_with_obs, **bad_loader_kwargs)


def test_non_original_presentation_payload_mismatch_refused_and_lossless_crop_tile_verified_with_pixel_markers(tmp_path):
    """Verify P1B:
    - Scaled crop descriptor (e.g. 200x100 presentation from 400x200 source_rect with scale=[2, 2])
      with an 800x600 PNG on disk is REFUSED by verify_and_load_native_observation_artifact and never
      attaches the 800x600 image with coordinate_authority=True.
    - 1:1 crop descriptor without bound payload_sha256 or with payload_sha256 equal to the 800x600
      source_sha256 (or mismatched hash) is REFUSED.
    - Passing an 800x600 loaded payload or dimensions to resolve_computer_image_point with a 200x100
      descriptor is REFUSED with coordinate_presented_image_dimensions_mismatch.
    - Trusted lossless 1:1 PNG crop and tile descriptors issued with source_image_bytes produce and
      verify the exact 200x100 cropped PNG payload, preserve source identity separately from payload
      identity, preserve planted RGB pixel markers at exact cropped coordinates, and resolve image
      coordinates consistently with the attached pixels.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    wref = _rust_window_ref()

    # Plant distinct RGB markers in the 800x600 source PNG:
    # - Source (20, 30) outside the crop has marker (11, 22, 33)
    # - Source (100, 200) [crop top-left -> crop (0, 0)] has marker (210, 40, 60)
    # - Source (120, 230) [crop (20, 30)] has marker (15, 225, 95)
    # - Source (299, 299) [crop bottom-right (199, 99)] has marker (45, 85, 245)
    markers = {
        (20, 30): (11, 22, 33),
        (100, 200): (210, 40, 60),
        (120, 230): (15, 225, 95),
        (299, 299): (45, 85, 245),
    }
    raw_800x600 = _make_marked_png(800, 600, markers=markers)
    base = _base_descriptor_inputs(raw_800x600, 800, 600, window_ref=wref)
    _, art_800x600 = _write_native_file(workspace, base["task_id"], base["operation_id"], raw_800x600, index=0)

    expected_bindings = {
        "expected_task_id": base["task_id"],
        "expected_operation_id": base["operation_id"],
        "expected_observation_id": base["observation_id"],
        "current_state_token": base["state_token"],
        "expected_window_ref": wref,
        "expected_worker_instance_id": wref["workerInstance"],
    }

    # 1. Exact P1B defect reproduction: scaled crop (200x100 from 400x200 source_rect, scale=[2, 2]) + 800x600 file -> REFUSED by loader
    scaled_crop_desc = ic._issue_native_observation_descriptor(
        **{
            **base,
            "representation": {
                "kind": "crop",
                "image_width": 200,
                "image_height": 100,
                "source_rect": {"x": 100, "y": 200, "width": 400, "height": 200},
                "origin": [100, 200],
                "scale": [2.0, 2.0],
            },
        }
    )
    with pytest.raises(ValueError, match="native_descriptor_scaled_presentation_unsupported"):
        ic.verify_and_load_native_observation_artifact(
            art_800x600,
            descriptor=scaled_crop_desc,
            workspace_root=workspace,
            task_id=base["task_id"],
            operation_id=base["operation_id"],
            expected_observation_id=base["observation_id"],
            current_state_token=base["state_token"],
            expected_window_ref=wref,
        )

    # 2. If caller supplies 800x600 actual dimensions or an 800x600 loaded artifact to resolve_computer_image_point with a 200x100 descriptor -> REFUSED
    with pytest.raises(ValueError, match="coordinate_presented_image_dimensions_mismatch"):
        ic.resolve_computer_image_point(
            scaled_crop_desc,
            20,
            30,
            actual_image_width=800,
            actual_image_height=600,
            **expected_bindings,
        )
    orig_sealed = ic._issue_native_observation_descriptor(**base)
    loaded_orig_800x600 = ic.verify_and_load_native_observation_artifact(
        art_800x600,
        descriptor=orig_sealed,
        workspace_root=workspace,
        task_id=base["task_id"],
        operation_id=base["operation_id"],
        expected_observation_id=base["observation_id"],
        current_state_token=base["state_token"],
        expected_window_ref=wref,
    )
    with pytest.raises(ValueError, match="coordinate_presented_image_dimensions_mismatch"):
        ic.resolve_computer_image_point(
            scaled_crop_desc,
            20,
            30,
            loaded_artifact=loaded_orig_800x600,
            **expected_bindings,
        )

    # 2b. Original presentation payload digest binding in resolve_computer_image_point & issuer:
    # - Replacing loaded_artifact.data_url with a different same-size (800x600) valid PNG and setting
    #   loaded_artifact.payload_sha256 to the replacement PNG's hash MUST be refused (cannot self-prove against sealed source_sha256).
    # - Lying loaded_artifact.payload_sha256 (equal to sealed source_sha256) while data_url contains the replacement PNG MUST be refused.
    # - Passing actual_payload_sha256 != sealed source_sha256 on original representation MUST be refused.
    # - Issuing or validating an original representation with conflicting representation.payload_sha256 != source_sha256 MUST be refused.
    # - Proper original loaded_artifact with exact sealed source_sha256 succeeds and sets attached_pixels_verified=True.
    alt_raw_800x600 = _make_marked_png(800, 600, base_color=(99, 88, 77))
    alt_sha256 = hashlib.sha256(alt_raw_800x600).hexdigest()
    assert alt_sha256 != base["source_sha256"]
    alt_data_url = "data:image/png;base64," + base64.b64encode(alt_raw_800x600).decode("ascii")

    # Same-dimension different PNG where attacker updates payload_sha256 and payload_bytes to match the replacement PNG
    substituted_same_dim = {
        **copy.deepcopy(loaded_orig_800x600),
        "payload_sha256": alt_sha256,
        "payload_bytes": len(alt_raw_800x600),
        "data_url": alt_data_url,
    }
    with pytest.raises(ValueError, match=r"^coordinate_presented_image_sha256_mismatch$"):
        ic.resolve_computer_image_point(
            orig_sealed,
            20,
            30,
            loaded_artifact=substituted_same_dim,
            **expected_bindings,
        )

    # Lying loaded_artifact metadata: claims sealed source_sha256 in payload_sha256, but data_url has replacement PNG bytes
    lying_hash_loaded = {
        **copy.deepcopy(loaded_orig_800x600),
        "payload_sha256": base["source_sha256"],
        "payload_bytes": len(alt_raw_800x600),
        "data_url": alt_data_url,
    }
    with pytest.raises(ValueError, match=r"^coordinate_presented_image_(?:sha256|bytes)_mismatch$"):
        ic.resolve_computer_image_point(
            orig_sealed,
            20,
            30,
            loaded_artifact=lying_hash_loaded,
            **expected_bindings,
        )

    # Even if replacement PNG has identical byte length to source_bytes, lying payload_sha256 vs actual data_url bytes is caught by verify_original_image_bytes
    no_source_bytes_desc = ic._issue_native_observation_descriptor(
        **{k: v for k, v in base.items() if k != "source_bytes"}
    )
    lying_hash_no_len_bound = {
        **copy.deepcopy(loaded_orig_800x600),
        "payload_sha256": base["source_sha256"],
        "payload_bytes": len(alt_raw_800x600),
        "data_url": alt_data_url,
    }
    with pytest.raises(ValueError, match=r"^coordinate_presented_image_sha256_mismatch$"):
        ic.resolve_computer_image_point(
            no_source_bytes_desc,
            20,
            30,
            loaded_artifact=lying_hash_no_len_bound,
            **expected_bindings,
        )

    # Passing actual_payload_sha256 that disagrees with sealed source_sha256 on original representation -> REFUSED
    with pytest.raises(ValueError, match=r"^coordinate_presented_image_sha256_mismatch$"):
        ic.resolve_computer_image_point(
            orig_sealed,
            20,
            30,
            actual_payload_sha256=alt_sha256,
            **expected_bindings,
        )

    # Conflicting representation.payload_sha256 on original representation -> REFUSED at issuance
    with pytest.raises(ValueError, match=r"^native_descriptor_original_payload_sha256_mismatch$"):
        ic._issue_native_observation_descriptor(
            **{
                **base,
                "representation": {
                    "kind": "original",
                    "image_width": 800,
                    "image_height": 600,
                    "source_rect": {"x": 0, "y": 0, "width": 800, "height": 600},
                    "origin": [0, 0],
                    "scale": [1.0, 1.0],
                    "payload_sha256": alt_sha256,
                },
            }
        )

    # Proper original positive case: exact source_sha256 verified from data_url bytes
    orig_resolved = ic.resolve_computer_image_point(
        orig_sealed,
        20,
        30,
        loaded_artifact=loaded_orig_800x600,
        actual_payload_sha256=base["source_sha256"],
        actual_image_width=800,
        actual_image_height=600,
        **expected_bindings,
    )
    assert orig_resolved["source_point"] == (20, 30)
    assert orig_resolved["attached_pixels_verified"] is True

    # Pure math mode (no loaded_artifact) succeeds mathematically but does not claim attached_pixels_verified
    pure_math_orig = ic.resolve_computer_image_point(orig_sealed, 20, 30, **expected_bindings)
    assert pure_math_orig["source_point"] == (20, 30)
    assert pure_math_orig["attached_pixels_verified"] is False

    # 3. 1:1 crop descriptor without bound payload_sha256 or with untransformed source_sha256 -> REFUSED by loader/issuer
    unbound_crop_desc = ic._issue_native_observation_descriptor(
        **{
            **base,
            "representation": {
                "kind": "crop",
                "image_width": 200,
                "image_height": 100,
                "source_rect": {"x": 100, "y": 200, "width": 200, "height": 100},
                "origin": [100, 200],
                "scale": [1.0, 1.0],
            },
        }
    )
    with pytest.raises(ValueError, match="native_descriptor_missing_presented_payload_sha256"):
        ic.verify_and_load_native_observation_artifact(
            art_800x600,
            descriptor=unbound_crop_desc,
            workspace_root=workspace,
            task_id=base["task_id"],
            operation_id=base["operation_id"],
            expected_observation_id=base["observation_id"],
            current_state_token=base["state_token"],
            expected_window_ref=wref,
        )
    with pytest.raises(ValueError, match="native_descriptor_payload_sha256_matches_untransformed_source"):
        ic._issue_native_observation_descriptor(
            **{
                **base,
                "representation": {
                    "kind": "crop",
                    "image_width": 200,
                    "image_height": 100,
                    "source_rect": {"x": 100, "y": 200, "width": 200, "height": 100},
                    "origin": [100, 200],
                    "scale": [1.0, 1.0],
                    "payload_sha256": base["source_sha256"],
                },
            }
        )
    wrong_hash_crop_desc = ic._issue_native_observation_descriptor(
        **{
            **base,
            "representation": {
                "kind": "crop",
                "image_width": 200,
                "image_height": 100,
                "source_rect": {"x": 100, "y": 200, "width": 200, "height": 100},
                "origin": [100, 200],
                "scale": [1.0, 1.0],
                "payload_sha256": "b" * 64,
            },
        }
    )
    with pytest.raises(ValueError, match="artifact_descriptor_payload_sha256_mismatch"):
        ic.verify_and_load_native_observation_artifact(
            art_800x600,
            descriptor=wrong_hash_crop_desc,
            workspace_root=workspace,
            task_id=base["task_id"],
            operation_id=base["operation_id"],
            expected_observation_id=base["observation_id"],
            current_state_token=base["state_token"],
            expected_window_ref=wref,
        )

    # 4. Positive lossless 1:1 crop and tile issued with source_image_bytes: attaches exact 200x100 PNG with verified pixel markers
    for rep_kind in ("crop", "tile"):
        lossless_desc = ic._issue_native_observation_descriptor(
            **{
                **base,
                "source_image_bytes": raw_800x600,
                "representation": {
                    "kind": rep_kind,
                    "image_width": 200,
                    "image_height": 100,
                    "source_rect": {"x": 100, "y": 200, "width": 200, "height": 100},
                    "origin": [100, 200],
                    "scale": [1.0, 1.0],
                },
            }
        )
        assert lossless_desc["representation"]["payload_sha256"] != base["source_sha256"]

        loaded_crop = ic.verify_and_load_native_observation_artifact(
            art_800x600,
            descriptor=lossless_desc,
            workspace_root=workspace,
            task_id=base["task_id"],
            operation_id=base["operation_id"],
            expected_observation_id=base["observation_id"],
            current_state_token=base["state_token"],
            expected_window_ref=wref,
        )
        assert loaded_crop["coordinate_authority"] is True
        assert loaded_crop["original_dimensions_preserved"] is False
        assert loaded_crop["width"] == 200 and loaded_crop["height"] == 100
        assert loaded_crop["payload_width"] == 200 and loaded_crop["payload_height"] == 100
        assert loaded_crop["source_width"] == 800 and loaded_crop["source_height"] == 600
        assert loaded_crop["source_sha256"] == base["source_sha256"]
        assert loaded_crop["payload_sha256"] == lossless_desc["representation"]["payload_sha256"]
        assert loaded_crop["payload_sha256"] != loaded_crop["source_sha256"]

        attached_raw = base64.b64decode(loaded_crop["data_url"].split(",", 1)[1])
        assert hashlib.sha256(attached_raw).hexdigest() == loaded_crop["payload_sha256"]
        with Image.open(io.BytesIO(attached_raw)) as crop_img:
            assert crop_img.size == (200, 100)
            assert crop_img.format == "PNG"
            assert crop_img.getpixel((0, 0)) == (210, 40, 60)
            assert crop_img.getpixel((20, 30)) == (15, 225, 95)
            assert crop_img.getpixel((199, 99)) == (45, 85, 245)

        resolved_pt = ic.resolve_computer_image_point(
            loaded_crop["native_descriptor"],
            20,
            30,
            loaded_artifact=loaded_crop,
            **expected_bindings,
        )
        assert resolved_pt["image_point"] == (20, 30)
        assert resolved_pt["source_point"] == (120, 230)
        assert resolved_pt["client_point"] == (120, 230)
        assert resolved_pt["window_point"] == (128, 261)
        assert resolved_pt["screen_point"] == (-1792, 111)
        assert resolved_pt["representation_kind"] == rep_kind
        assert resolved_pt["attached_pixels_verified"] is True

    # 5. Pure coordinate mapping for scaled + padded descriptor rejects padding & out-of-bounds, maps active region
    padded_scaled = ic._issue_native_observation_descriptor(
        **{
            **base,
            "representation": {
                "kind": "scaled",
                "image_width": 220,
                "image_height": 120,
                "source_rect": {"x": 100, "y": 200, "width": 400, "height": 200},
                "origin": [100, 200],
                "scale": [2.0, 2.0],
                "padding": {"left": 10, "top": 10, "right": 10, "bottom": 10},
            },
        }
    )
    for pad_x, pad_y in [(5, 30), (215, 30), (30, 5), (30, 115)]:
        with pytest.raises(ValueError, match="coordinate_target_in_padding_or_letterbox"):
            ic.resolve_computer_image_point(padded_scaled, pad_x, pad_y, **expected_bindings)
    for oob_x, oob_y in [(-1, 30), (220, 30), (30, -1), (30, 120)]:
        with pytest.raises(ValueError, match="coordinate_out_of_image_bounds"):
            ic.resolve_computer_image_point(padded_scaled, oob_x, oob_y, **expected_bindings)

    active_mapped = ic.resolve_computer_image_point(padded_scaled, 30, 40, **expected_bindings)
    # active_x = 30 - 10 = 20, active_y = 40 - 10 = 30; scale = (2, 2) -> source = (100 + 40, 200 + 60) = (140, 260)
    assert active_mapped["source_point"] == (140, 260)
    assert active_mapped["attached_pixels_verified"] is False


def test_generic_mcp_4mib_vs_native_8mib_and_bounded_fd_containment_protections(tmp_path):
    """Verify:
    - Reproducible high-entropy 1600x1200 RGB PNG (random.Random(seed).randbytes) has actual encoded
      size > 4 MiB and <= 8 MiB while 1600*1200 <= 16M pixels, is rejected by generic MCP 4 MiB cap,
      and is accepted by host-sealed native 8 MiB loader with exact bytes/hash/dimensions roundtrip.
    - >8 MiB native descriptor or file size is rejected.
    - File growth race from small PNG to 6 MiB on open reads at most expected_bytes + 1 bytes and fails closed.
    - Opened-handle path resolving outside expected native task/operation directory fails closed.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    wref = _rust_window_ref()
    width_5m, height_5m = 1600, 1200
    assert width_5m * height_5m <= ic.MAX_IMAGE_PIXELS
    png_5mib = _make_marked_png(width_5m, height_5m, noisy=True, seed=20261019)
    actual_encoded_len = len(png_5mib)
    expected_sha256 = hashlib.sha256(png_5mib).hexdigest()
    assert ic.MAX_IMAGE_SOURCE_BYTES < actual_encoded_len <= ic.MAX_NATIVE_IMAGE_SOURCE_BYTES

    task_id = "task_8mib"
    op_id = "op_8mib"
    safe_task = ic.safe_segment(task_id, "task")
    safe_op = ic.safe_segment(op_id, "op")

    # 1. Generic 4 MiB cap rejects >4 MiB high-entropy PNG
    with pytest.raises(ValueError, match=r"^artifact_file_too_large$"):
        ic.verify_original_image_bytes(png_5mib)

    mcp_rel = Path(".ryn") / "mcp" / safe_task / safe_op / "image_0.png"
    mcp_full = workspace / mcp_rel
    mcp_full.parent.mkdir(parents=True, exist_ok=True)
    mcp_full.write_bytes(png_5mib)
    mcp_art = {
        "path": mcp_rel.as_posix(),
        "sha256": expected_sha256,
        "bytes": actual_encoded_len,
        "mime_type": "image/png",
        "width": width_5m,
        "height": height_5m,
    }
    with pytest.raises(ValueError, match="artifact_bytes_invalid"):
        ic.verify_and_load_mcp_image_artifact(mcp_art, workspace_root=workspace, task_id=task_id, operation_id=op_id)

    # 2. Host-sealed native 8 MiB loader accepts high-entropy PNG and preserves exact original bytes & hash roundtrip
    base_5m = _base_descriptor_inputs(png_5mib, width_5m, height_5m, task_id=task_id, operation_id=op_id, window_ref=wref)
    sealed_5m = ic._issue_native_observation_descriptor(**base_5m)
    _, native_5m_art = _write_native_file(workspace, task_id, op_id, png_5mib, index=0)

    loaded_5m = ic.verify_and_load_native_observation_artifact(
        native_5m_art,
        descriptor=sealed_5m,
        workspace_root=workspace,
        task_id=task_id,
        operation_id=op_id,
        expected_observation_id=base_5m["observation_id"],
        current_state_token=base_5m["state_token"],
        expected_window_ref=wref,
    )
    assert loaded_5m["width"] == width_5m and loaded_5m["height"] == height_5m
    assert loaded_5m["bytes"] == actual_encoded_len
    assert loaded_5m["sha256"] == expected_sha256
    assert loaded_5m["payload_sha256"] == expected_sha256
    roundtrip_bytes = base64.b64decode(loaded_5m["data_url"].split(",", 1)[1])
    assert roundtrip_bytes == png_5mib
    assert hashlib.sha256(roundtrip_bytes).hexdigest() == expected_sha256

    # 3. >8 MiB rejected by native issuer and loader
    with pytest.raises(ValueError, match=r"^native_descriptor_source_bytes_exceeded$"):
        ic._issue_native_observation_descriptor(
            **{**base_5m, "source_bytes": ic.MAX_NATIVE_IMAGE_SOURCE_BYTES + 1}
        )

    # 4. Bounded read race on native artifact: small PNG swapped to 6 MiB on open reads at most expected_bytes + 1
    small_png = _make_marked_png(64, 48)
    base_small = _base_descriptor_inputs(small_png, 64, 48, task_id=task_id, operation_id=op_id, window_ref=wref)
    sealed_small = ic._issue_native_observation_descriptor(**base_small)
    small_file, small_art = _write_native_file(workspace, task_id, op_id, small_png, index=1)

    real_open = os.open
    real_read = os.read
    read_lengths: list[int] = []

    def _swap_on_open(path_arg, flags, *args, **kwargs):
        if Path(path_arg) == small_file:
            small_file.write_bytes(b"Z" * (6 * 1024 * 1024))
        return real_open(path_arg, flags, *args, **kwargs)

    def _track_read(fd, n):
        chunk = real_read(fd, n)
        read_lengths.append(len(chunk))
        return chunk

    with patch("os.open", side_effect=_swap_on_open), patch("os.read", side_effect=_track_read):
        with pytest.raises(ValueError, match="artifact_(?:file_too_large|size_mismatch|handle_identity_changed)"):
            ic.verify_and_load_native_observation_artifact(
                small_art,
                descriptor=sealed_small,
                workspace_root=workspace,
                task_id=task_id,
                operation_id=op_id,
                expected_observation_id=base_small["observation_id"],
                current_state_token=base_small["state_token"],
                expected_window_ref=wref,
            )
    assert sum(read_lengths) <= len(small_png) + 1

    # Restore small file and verify opened-handle outside-scope rejection before any read
    small_file.write_bytes(small_png)
    outside_png = tmp_path / "outside.png"
    outside_png.write_bytes(small_png)
    read_lengths.clear()
    with (
        patch("rynmesh.agent_runtime.image_capabilities._resolve_opened_fd_path", return_value=outside_png.resolve()),
        patch("os.read", side_effect=_track_read),
    ):
        with pytest.raises(ValueError, match="artifact_resolved_outside_scope"):
            ic.verify_and_load_native_observation_artifact(
                small_art,
                descriptor=sealed_small,
                workspace_root=workspace,
                task_id=task_id,
                operation_id=op_id,
                expected_observation_id=base_small["observation_id"],
                current_state_token=base_small["state_token"],
                expected_window_ref=wref,
            )
    assert read_lengths == []
