"""Focused unit and live QuickJS WASM sandbox tests for the Ryn JS runtime host core."""

from __future__ import annotations

import base64
import os
import struct
import time
import zlib
from pathlib import Path

import pytest

from rynmesh.agent_runtime.js_runtime import (
    HOST_CALL_TRANSITION_TABLE,
    MAX_FRAME_BYTES,
    SKY_METHOD_SPECS,
    TERMINAL_HOST_CALL_STATUSES,
    JsExecutionCoordinator,
    JsProtocolError,
    JsRuntimeManager,
    build_cancel_frame,
    build_evaluate_frame,
    build_host_result_frame,
    canonical_arguments_sha256,
    encode_parent_frame,
    is_js_runtime_available,
    parse_worker_frame,
    sanitized_worker_env,
    validate_host_call_transition,
    validate_limits,
    validate_sky_host_call,
)
from rynmesh.agent_runtime.js_runtime.resources import MANIFEST_FILENAME

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEV_PROFILE_CANDIDATE = (
    _REPO_ROOT
    / "work"
    / "computer-use-parity-20261019"
    / "prepared-js-runtime-independent-98ac78c9d0"
)


def _resolve_test_dev_profile_root(default_candidate: Path) -> Path | None:
    env_override = os.environ.get("RYN_JS_TEST_PROFILE_ROOT", "").strip()
    if env_override:
        explicit_path = Path(env_override)
        if not (explicit_path / MANIFEST_FILENAME).is_file():
            raise RuntimeError(
                f"RYN_JS_TEST_PROFILE_ROOT={env_override!r} does not point to a prepared JS runtime profile with {MANIFEST_FILENAME}"
            )
        return explicit_path
    if (default_candidate / MANIFEST_FILENAME).is_file():
        return default_candidate
    return None


_DEV_PROFILE_ROOT = _resolve_test_dev_profile_root(_DEV_PROFILE_CANDIDATE)

requires_quickjs_worker = pytest.mark.skipif(
    not is_js_runtime_available(development_profile_root=_DEV_PROFILE_ROOT),
    reason="Explicit development JS runtime profile not prepared",
)


def _make_dev_manager(**kwargs) -> JsRuntimeManager:
    profile_root = _resolve_test_dev_profile_root(_DEV_PROFILE_CANDIDATE)
    assert profile_root is not None
    return JsRuntimeManager(development_profile_root=profile_root, **kwargs)


def _make_96kib_png_data_url() -> str:
    def _chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    head = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    tail = _chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + _chunk(b"IEND", b"")
    pad_len = 98304 - len(head) - len(tail) - 12
    raw = head + _chunk(b"tEXt", b"K\x00" + (b"A" * (pad_len - 2))) + tail
    assert len(raw) == 98304
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def test_protocol_sky_method_allowlist_and_canonical_hash() -> None:
    assert "list_windows" in SKY_METHOD_SPECS
    assert "get_window_state" in SKY_METHOD_SPECS
    assert "click" in SKY_METHOD_SPECS

    read_spec, read_args, read_hash = validate_sky_host_call("list_windows", {})
    assert read_spec == {
        "effect": "read",
        "target_tool": "computer_observe_windows",
    }
    assert read_args == {}
    assert read_hash == canonical_arguments_sha256({})

    write_spec, write_args, write_hash_a = validate_sky_host_call(
        "click",
        {
            "window": {"app": "notepad.exe", "id": 1, "title": "Untitled"},
            "x": 100,
            "y": 200,
            "click_count": 1,
            "mouse_button": "left",
        },
    )
    _, _, write_hash_b = validate_sky_host_call(
        "click",
        {
            "mouse_button": "left",
            "click_count": 1,
            "y": 200,
            "x": 100,
            "window": {"id": 1, "title": "Untitled", "app": "notepad.exe"},
        },
    )
    assert write_spec == {
        "effect": "write",
        "target_tool": "computer_act_window",
    }
    assert write_args["window"] == {"app": "notepad.exe", "id": 1, "title": "Untitled"}
    assert write_hash_a == write_hash_b

    with pytest.raises(JsProtocolError) as exc_info:
        validate_sky_host_call("exec_shell", {"cmd": "calc.exe"})
    assert exc_info.value.code == "ryn_sky_method_unsupported"

    with pytest.raises(JsProtocolError) as exc_info_nan:
        validate_sky_host_call(
            "click",
            {"window": {"app": "notepad.exe", "id": 1}, "x": float("nan"), "y": 10},
        )
    assert exc_info_nan.value.code == "ryn_js_non_finite_number"


def test_protocol_v1_frames_limits_and_env_sanitization() -> None:
    ready = parse_worker_frame(
        b'{"v":1,"type":"ready","session_id":"s1","generation":1,"engine":"quickjs-emscripten","release_variant":"RELEASE_SYNC","pid":1234}\n'
    )
    assert ready["v"] == 1
    assert ready["type"] == "ready"
    assert ready["session_id"] == "s1"
    assert ready["generation"] == 1
    assert ready["engine"] == "quickjs-emscripten"

    with pytest.raises(JsProtocolError) as bad_ver:
        parse_worker_frame(b'{"v":2,"type":"ready","session_id":"s1","generation":1}\n')
    assert bad_ver.value.code == "ryn_js_invalid_frame"

    with pytest.raises(JsProtocolError) as bad_type:
        parse_worker_frame(b'{"v":1,"type":"unknown_event","session_id":"s1","generation":1}\n')
    assert bad_type.value.code == "ryn_js_unknown_frame_type"

    limits = validate_limits({"cpu_timeout_ms": 500, "max_host_calls": 16})
    assert limits["cpu_timeout_ms"] == 500
    assert limits["max_host_calls"] == 16

    for invalid_limits in (
        {"cpu_timeout_ms": "NaN"},
        {"cpu_timeout_ms": True},
        {"cpu_timeout_ms": 1.5},
        {"unknown_limit": 100},
    ):
        with pytest.raises(JsProtocolError) as lim_err:
            build_evaluate_frame(
                session_id="s1",
                generation=1,
                execution_id="e1",
                code="1 + 1",
                limits=invalid_limits,
            )
        assert lim_err.value.code == "ryn_js_invalid_limits"

    fake_env = {
        "PATH": "C:\\Windows\\System32",
        "SYSTEMROOT": "C:\\Windows",
        "OPENAI_API_KEY": "sk-secret",
        "RYNMESH_SECRET": "top-secret",
        "CODEX_HOME": "C:\\Users\\test\\.codex",
        "NODE_OPTIONS": "--inspect",
    }
    clean = sanitized_worker_env("sess_1", 3, base_env=fake_env)
    assert clean["PATH"] == "C:\\Windows\\System32"
    assert clean["RYN_JS_SESSION_ID"] == "sess_1"
    assert clean["RYN_JS_GENERATION"] == "3"
    assert "OPENAI_API_KEY" not in clean
    assert "RYNMESH_SECRET" not in clean
    assert "CODEX_HOME" not in clean
    assert "NODE_OPTIONS" not in clean


@requires_quickjs_worker
def test_quickjs_public_sky_proxy_and_persistent_state() -> None:
    manager = _make_dev_manager()
    try:
        seen_methods: list[str] = []

        def host_handler(envelope):
            seen_methods.append(envelope.method)
            if envelope.method == "list_windows":
                return [{"app": "notepad.exe", "id": 1, "title": "Untitled - Notepad"}]
            raise AssertionError(f"Unexpected method: {envelope.method}")

        init_code = """
        if (!globalThis.sky) {
          const { sky } = await import('@oai/sky');
          globalThis.sky = sky;
        }
        const windows = await sky.list_windows();
        globalThis.savedCounter = 41;
        return {
          target: sky.target,
          provider: sky.provider,
          windows,
        };
        """
        snap1 = manager.run_to_completion("task_persist", init_code, host_handler=host_handler)
        assert snap1.status == "completed"
        assert seen_methods == ["list_windows"]
        assert snap1.value == {
            "target": "windows",
            "provider": "ryn",
            "windows": [{"app": "notepad.exe", "id": 1, "title": "Untitled - Notepad"}],
        }

        followup_code = """
        globalThis.savedCounter += 1;
        nodeRepl.write(`counter=${globalThis.savedCounter}`);
        return globalThis.savedCounter;
        """
        snap2 = manager.run_to_completion("task_persist", followup_code)
        assert snap2.status == "completed"
        assert snap2.value == 42
        assert "counter=42" in snap2.stdout
    finally:
        manager.close()


@requires_quickjs_worker
def test_quickjs_task_isolation_across_sessions() -> None:
    manager = _make_dev_manager()
    try:
        snap_a = manager.run_to_completion(
            "task_alpha",
            "globalThis.secretToken = 'alpha-only'; return globalThis.secretToken;",
        )
        snap_b = manager.run_to_completion(
            "task_beta",
            "return typeof globalThis.secretToken;",
        )
        assert snap_a.status == "completed"
        assert snap_a.value == "alpha-only"
        assert snap_b.status == "completed"
        assert snap_b.value == "undefined"

        session_a = manager.get_session("task_alpha")
        session_b = manager.get_session("task_beta")
        assert session_a is not None and session_b is not None
        assert session_a.pid != session_b.pid
    finally:
        manager.close()


@requires_quickjs_worker
def test_coordinator_suspends_for_approval_resets_cpu_slice_and_resumes_without_replay() -> None:
    manager = _make_dev_manager()
    coordinator = JsExecutionCoordinator(manager)
    try:
        code = """
        const { sky } = await import('@oai/sky');
        globalThis.evalRuns = (globalThis.evalRuns || 0) + 1;
        const windows = await sky.list_windows();
        globalThis.afterObserve = (globalThis.afterObserve || 0) + 1;
        const clickReturn = await sky.click({
          window: windows[0],
          x: 120,
          y: 80,
          click_count: 1,
          mouse_button: 'left',
        });
        return {
          evalRuns: globalThis.evalRuns,
          afterObserve: globalThis.afterObserve,
          firstWindow: windows[0],
          clickReturn: clickReturn === undefined ? 'void' : 'non-void',
        };
        """
        exec_id, generation = coordinator.start(
            "task_coord",
            code,
            limits={"cpu_timeout_ms": 150},
        )

        # Step 1: guest suspends on sky.list_windows (read)
        step1 = coordinator.poll_step("task_coord", timeout=2.0)
        assert step1.status == "host_call_pending"
        assert step1.host_call is not None
        assert step1.host_call.method == "list_windows"
        assert step1.host_call.target_tool == "computer_observe_windows"
        assert step1.host_call.effect == "read"
        assert step1.host_call.arguments == {}

        coordinator.mark_dispatched(
            "task_coord",
            execution_id=exec_id,
            generation=generation,
            callback_id=step1.host_call.callback_id,
            operation_id="op-read-1",
        )
        coordinator.resolve_host_call(
            "task_coord",
            execution_id=exec_id,
            generation=generation,
            callback_id=step1.host_call.callback_id,
            result=[{"app": "notepad.exe", "id": 1, "title": "Untitled - Notepad"}],
        )

        # Step 2: guest suspends on sky.click (write requiring approval)
        step2 = coordinator.poll_step("task_coord", timeout=2.0)
        assert step2.status == "host_call_pending"
        assert step2.host_call is not None
        assert step2.host_call.method == "click"
        assert step2.host_call.target_tool == "computer_act_window"
        assert step2.host_call.effect == "write"
        assert step2.host_call.arguments == {
            "window": {"app": "notepad.exe", "id": 1, "title": "Untitled - Notepad"},
            "x": 120,
            "y": 80,
            "click_count": 1,
            "mouse_button": "left",
        }

        rec = coordinator.mark_awaiting_approval(
            "task_coord",
            execution_id=exec_id,
            generation=generation,
            callback_id=step2.host_call.callback_id,
            approval_id="appr-123",
        )
        assert rec.status == "awaiting_approval"
        assert rec.not_executed is True

        # Simulate owner approval wait longer than cpu_timeout_ms (220ms > 150ms)
        time.sleep(0.22)

        coordinator.mark_dispatched(
            "task_coord",
            execution_id=exec_id,
            generation=generation,
            callback_id=step2.host_call.callback_id,
            operation_id="op-write-1",
        )
        coordinator.resolve_host_call(
            "task_coord",
            execution_id=exec_id,
            generation=generation,
            callback_id=step2.host_call.callback_id,
            result={"ok": True},
        )

        with pytest.raises(JsProtocolError) as dup_err:
            coordinator.resolve_host_call(
                "task_coord",
                execution_id=exec_id,
                generation=generation,
                callback_id=step2.host_call.callback_id,
                result={"ok": True},
            )
        assert dup_err.value.code == "ryn_js_duplicate_host_result"

        # Step 3: guest completes with evalRuns == 1 and afterObserve == 1 (no replay, idle wait not charged)
        step3 = coordinator.poll_step("task_coord", timeout=2.0)
        assert step3.status == "completed"
        assert step3.snapshot.value == {
            "evalRuns": 1,
            "afterObserve": 1,
            "firstWindow": {"app": "notepad.exe", "id": 1, "title": "Untitled - Notepad"},
            "clickReturn": "void",
        }
    finally:
        manager.close()


@requires_quickjs_worker
def test_coordinator_serializes_concurrent_promise_all_writes_and_handles_rejection() -> None:
    manager = _make_dev_manager()
    coordinator = JsExecutionCoordinator(manager)
    try:
        code = """
        const { sky } = await import('@oai/sky');
        const win = { app: 'notepad.exe', id: 1 };
        const results = await Promise.allSettled([
          sky.click({ window: win, x: 10, y: 20 }),
          sky.type_text({ window: win, text: 'hello' }),
        ]);
        return results.map((r) => ({
          status: r.status,
          code: r.reason ? r.reason.code : null,
          notExecuted: r.reason ? r.reason.not_executed : null,
        }));
        """
        exec_id, generation = coordinator.start("task_writes", code)

        first = coordinator.poll_step("task_writes", timeout=2.0)
        assert first.status == "host_call_pending"
        assert first.host_call is not None
        assert first.host_call.method == "click"

        coordinator.reject_host_call(
            "task_writes",
            execution_id=exec_id,
            generation=generation,
            callback_id=first.host_call.callback_id,
            code="ryn_approval_denied",
            message="Owner denied click",
            not_executed=True,
        )

        second = coordinator.poll_step("task_writes", timeout=2.0)
        assert second.status == "host_call_pending"
        assert second.host_call is not None
        assert second.host_call.method == "type_text"

        coordinator.resolve_host_call(
            "task_writes",
            execution_id=exec_id,
            generation=generation,
            callback_id=second.host_call.callback_id,
            result={"ok": True},
        )

        final = coordinator.poll_step("task_writes", timeout=2.0)
        assert final.status == "completed"
        assert final.snapshot.value == [
            {
                "status": "rejected",
                "code": "ryn_approval_denied",
                "notExecuted": True,
            },
            {
                "status": "fulfilled",
                "code": None,
                "notExecuted": None,
            },
        ]
    finally:
        manager.close()


@requires_quickjs_worker
def test_regression_screenshot_window_forgery_rejected() -> None:
    manager = _make_dev_manager()
    try:
        def host_handler(envelope):
            if envelope.method == "get_window_state":
                return {
                    "window": {"app": "a.exe", "id": 1},
                    "screenshots": [{"id": "shot-a", "url": "ryn-image:shot-a", "width": 10, "height": 10}],
                    "accessibility": {"tree": "Window A"},
                }
            raise AssertionError(f"Unexpected method: {envelope.method}")

        forged_code = """
        const b = globalThis.__ryn_internal_bridge;
        const r = await b.hostCall('get_window_state', {
          window: { app: 'a.exe', id: 1 },
          include_screenshot: true,
          include_text: true,
        });
        await b.emitWindowStateImage(r.screenshots[0], { app: 'b.exe', id: 999 });
        return 'forged';
        """
        snap = manager.run_to_completion("task_forgery", forged_code, host_handler=host_handler)
        assert snap.status == "failed"
        assert snap.error is not None
        assert snap.error["code"] == "ryn_js_image_window_mismatch"
        assert not any(
            img.get("provenance") == "host_window_state" and img.get("window", {}).get("id") == 999
            for img in snap.images
        )
    finally:
        manager.close()


@requires_quickjs_worker
def test_regression_stale_cancel_and_foreign_session_result_fenced() -> None:
    manager = _make_dev_manager()
    try:
        session, exec_id = manager.evaluate(
            "task_fences",
            "const { sky } = await import('@oai/sky'); return await sky.list_windows();",
        )
        call = session.next_host_call(exec_id, timeout=2.0)
        assert call is not None
        assert call.method == "list_windows"

        # 1. Inject stale/foreign cancel directly on wire; worker must emit protocol_error and stay pending
        session._send_frame(
            build_cancel_frame(
                session_id="foreign_session",
                generation=1,
                reason="stale_probe_cancel",
            )
        )
        time.sleep(0.05)
        assert session.last_protocol_error is not None
        assert session.last_protocol_error["code"] == "ryn_js_session_mismatch"

        # 2. Inject foreign-session host_result directly on wire; worker must reject and stay pending
        session.last_protocol_error = None
        session._send_frame(
            build_host_result_frame(
                session_id="foreign_session",
                generation=session.generation,
                execution_id=exec_id,
                callback_id=call.callback_id,
                ok=True,
                result=[{"app": "foreign.exe", "id": 9}],
            )
        )
        time.sleep(0.05)
        assert session.last_protocol_error is not None
        assert session.last_protocol_error["code"] == "ryn_js_session_mismatch"

        # Still awaiting legitimate response
        mid_snap = session.poll(exec_id, timeout=0.0)
        assert mid_snap.status == "awaiting_host"

        # 3. Legitimate response completes the evaluation cleanly
        session.respond(
            execution_id=exec_id,
            generation=call.generation,
            callback_id=call.callback_id,
            ok=True,
            result=[{"app": "calc.exe", "id": 2, "title": "Calculator"}],
        )
        final_snap = session.poll(exec_id, timeout=2.0)
        assert final_snap.status == "completed"
        assert final_snap.value == [{"app": "calc.exe", "id": 2, "title": "Calculator"}]
    finally:
        manager.close()


@requires_quickjs_worker
def test_regression_aggregate_frame_bound_and_metadata_only_images() -> None:
    manager = _make_dev_manager()
    try:
        url = _make_96kib_png_data_url()
        code = f"""
        const u = {url!r};
        for (let i = 0; i < 16; i += 1) {{
          await nodeRepl.emitImage(u);
        }}
        return 'done';
        """
        snap = manager.run_to_completion("task_agg_img", code, timeout=5.0)
        # Must fail cleanly with bounded error before terminal frame can approach MAX_FRAME_BYTES
        assert snap.status == "failed"
        assert snap.error is not None
        assert snap.error["code"] == "ryn_js_aggregate_output_limit_exceeded"
        serialized_snap = encode_parent_frame({"v": 1, "type": "test", **snap.to_dict()})
        assert len(serialized_snap) < MAX_FRAME_BYTES
        for img in snap.images:
            assert "url" not in img
            assert img["provenance"] == "guest_inline_data_url"
            assert isinstance(img["sha256"], str) and len(img["sha256"]) == 64
    finally:
        manager.close()


@requires_quickjs_worker
def test_regression_old_screenshot_cache_eviction_and_end_turn_retirement() -> None:
    manager = _make_dev_manager()
    try:
        call_count = 0

        def host_handler(envelope):
            nonlocal call_count
            if envelope.method == "get_window_state":
                idx = call_count
                call_count += 1
                return {
                    "window": {"app": "a.exe", "id": 1},
                    "screenshots": [
                        {
                            "id": f"shot-{idx}",
                            "url": f"ryn-image:cache-{idx}",
                            "width": 100,
                            "height": 100,
                        }
                    ],
                    "accessibility": {"tree": f"obs-{idx}"},
                }
            raise AssertionError(f"Unexpected method: {envelope.method}")

        code = """
        const b = globalThis.__ryn_internal_bridge;
        for (let i = 0; i < 66; i += 1) {
          const r = await b.hostCall('get_window_state', {
            window: { app: 'a.exe', id: 1 },
            include_screenshot: true,
          });
          if (i === 0) {
            globalThis.firstShot = r.screenshots[0];
          }
          globalThis.latestShot = r.screenshots[0];
        }
        await nodeRepl.emitImage(globalThis.firstShot);
        return 'old-reused';
        """
        snap = manager.run_to_completion(
            "task_cache",
            code,
            host_handler=host_handler,
            limits={"max_host_calls": 128},
            timeout=10.0,
        )
        assert call_count == 66
        assert snap.status == "failed"
        assert snap.error is not None
        assert snap.error["code"] == "ryn_js_image_untrusted"

        # Latest observation is valid before end_turn, then retired after end_turn
        latest_ok = manager.run_to_completion(
            "task_cache",
            "return await nodeRepl.emitImage(globalThis.latestShot);",
        )
        assert latest_ok.status == "completed"
        assert latest_ok.images[0]["provenance"] == "host_issued_descriptor"
        assert latest_ok.images[0]["window"] == {"app": "a.exe", "id": 1}

        manager.end_turn("task_cache")

        after_turn = manager.run_to_completion(
            "task_cache",
            "return await nodeRepl.emitImage(globalThis.latestShot);",
        )
        assert after_turn.status == "failed"
        assert after_turn.error is not None
        assert after_turn.error["code"] == "ryn_js_image_untrusted"
    finally:
        manager.close()


@requires_quickjs_worker
def test_unawaited_host_call_and_cancel_realm_reset() -> None:
    manager = _make_dev_manager()
    try:
        # Unawaited host proposal must not outlive a completed evaluation
        unawaited_snap = manager.run_to_completion(
            "task_unawaited",
            """
            const { sky } = await import('@oai/sky');
            sky.click({ window: { app: 'a.exe', id: 1 }, x: 5, y: 5 });
            return 'completed-early';
            """,
        )
        assert unawaited_snap.status == "failed"
        assert unawaited_snap.error is not None
        assert unawaited_snap.error["code"] == "ryn_js_unawaited_host_call"

        # Cancellation resets the guest realm and increments generation cleanly
        session, exec_id = manager.evaluate(
            "task_cancel_reset",
            """
            globalThis.dirtyBeforeCancel = 'should_be_wiped';
            const { sky } = await import('@oai/sky');
            return await sky.list_windows();
            """,
        )
        call = session.next_host_call(exec_id, timeout=2.0)
        assert call is not None
        cancel_snap = manager.cancel_task("task_cancel_reset")
        assert cancel_snap is not None
        assert cancel_snap.status == "cancelled"
        assert session.generation == 2

        after_cancel = manager.run_to_completion(
            "task_cancel_reset",
            "return typeof globalThis.dirtyBeforeCancel;",
        )
        assert after_cancel.status == "completed"
        assert after_cancel.value == "undefined"
    finally:
        manager.close()


@requires_quickjs_worker
def test_sandbox_blocks_forbidden_imports_globals_and_resource_abuse() -> None:
    manager = _make_dev_manager()
    try:
        for spec in (
            "node:fs",
            "fs",
            "child_process",
            "http",
            "./relative.js",
            "file:///C:/Windows/win.ini",
            "data:text/javascript,export default 1",
        ):
            snap = manager.run_to_completion("task_sec", f"await import({spec!r});")
            assert snap.status == "failed", f"Expected import({spec!r}) to fail"

        globals_snap = manager.run_to_completion(
            "task_sec",
            """
            const fnGlobal = ({}).constructor.constructor('return this')();
            return {
              processType: typeof process,
              requireType: typeof require,
              fetchType: typeof fetch,
              wsType: typeof WebSocket,
              fnProcessType: typeof fnGlobal.process,
            };
            """,
        )
        assert globals_snap.status == "completed"
        assert globals_snap.value == {
            "processType": "undefined",
            "requireType": "undefined",
            "fetchType": "undefined",
            "wsType": "undefined",
            "fnProcessType": "undefined",
        }

        for bad_img in ("https://evil.example/leak.png", "file:///C:/Windows/win.ini", "C:\\Windows\\win.ini"):
            img_snap = manager.run_to_completion("task_sec", f"await nodeRepl.emitImage({bad_img!r});")
            assert img_snap.status == "failed"
            assert img_snap.error is not None
            assert img_snap.error["code"] == "ryn_js_image_forbidden_url"

        bad_sky = manager.run_to_completion(
            "task_sec",
            "const { sky } = await import('@oai/sky'); await sky.exec_command('whoami');",
        )
        assert bad_sky.status == "failed"
        assert bad_sky.error is not None
        assert bad_sky.error["code"] == "ryn_sky_method_unsupported"

        loop_snap = manager.run_to_completion(
            "task_limits",
            "while (true) {}",
            limits={"cpu_timeout_ms": 120},
            timeout=3.0,
        )
        assert loop_snap.status == "failed"
        assert loop_snap.error is not None
        assert loop_snap.error["code"] == "ryn_js_execution_timeout"

        flood_snap = manager.run_to_completion(
            "task_limits",
            "nodeRepl.write('x'.repeat(300000));",
        )
        assert flood_snap.status == "failed"
        assert flood_snap.error is not None
        assert flood_snap.error["code"] == "ryn_js_output_limit_exceeded"
    finally:
        manager.close()


@requires_quickjs_worker
def test_host_call_transition_table_monotonicity_and_deferred_write_guards() -> None:
    for terminal_state in TERMINAL_HOST_CALL_STATUSES:
        assert HOST_CALL_TRANSITION_TABLE[terminal_state] == frozenset()
        for candidate in HOST_CALL_TRANSITION_TABLE:
            with pytest.raises(JsProtocolError) as exc_info:
                validate_host_call_transition(terminal_state, candidate, callback_id="cb_terminal")
            assert exc_info.value.code == "ryn_js_invalid_host_call_transition"

    with pytest.raises(JsProtocolError) as back_err:
        validate_host_call_transition("dispatched", "awaiting_approval", callback_id="cb_back")
    assert back_err.value.code == "ryn_js_invalid_host_call_transition"

    for premature_target in ("awaiting_approval", "dispatched", "completed", "unknown"):
        with pytest.raises(JsProtocolError) as unadm_err:
            validate_host_call_transition("pending_admission", premature_target, callback_id="cb_unadm")
        assert unadm_err.value.code == "ryn_js_invalid_host_call_transition"

    manager = _make_dev_manager()
    coordinator = JsExecutionCoordinator(manager)
    try:
        code = """
        const { sky } = await import('@oai/sky');
        const windows = await sky.list_windows();
        const win = { app: 'fixture.exe', id: 7 };
        const settled = await Promise.allSettled([
          sky.click({ window: win, x: 10, y: 20 }),
          sky.type_text({ window: win, text: 'second-write' }),
        ]);
        return { windows, settled: settled.map((item) => item.status) };
        """
        exec_id, generation = coordinator.start("task_transitions", code)

        # 1. Read callback: test idempotent repeat, binding immutability, dispatched -> awaiting_approval prohibition,
        # and completed terminal immutability against late mark_awaiting_approval / mark_dispatched.
        step_read = coordinator.poll_step("task_transitions", timeout=2.0)
        assert step_read.status == "host_call_pending"
        assert step_read.host_call is not None
        read_call = step_read.host_call
        read_ids = {
            "session_id": read_call.session_id,
            "execution_id": exec_id,
            "generation": generation,
            "callback_id": read_call.callback_id,
        }

        rec = coordinator.mark_awaiting_approval("task_transitions", **read_ids, approval_id="appr-read-1")
        assert rec.status == "awaiting_approval"
        assert rec.approval_id == "appr-read-1"
        # Valid idempotent repeat with same approval_id
        rec_same = coordinator.mark_awaiting_approval("task_transitions", **read_ids, approval_id="appr-read-1")
        assert rec_same is rec and rec_same.approval_id == "appr-read-1"
        # Conflicting approval_id rejected
        with pytest.raises(JsProtocolError) as appr_conflict:
            coordinator.mark_awaiting_approval("task_transitions", **read_ids, approval_id="appr-read-2")
        assert appr_conflict.value.code == "ryn_js_invalid_host_call_transition"
        assert rec.approval_id == "appr-read-1"

        rec = coordinator.mark_dispatched("task_transitions", **read_ids, operation_id="op-read-1")
        assert rec.status == "dispatched"
        assert rec.operation_id == "op-read-1"
        assert rec.not_executed is False
        # Valid idempotent repeat with same operation_id
        rec_disp_same = coordinator.mark_dispatched("task_transitions", **read_ids, operation_id="op-read-1")
        assert rec_disp_same is rec and rec_disp_same.operation_id == "op-read-1"
        # Conflicting operation_id rejected
        with pytest.raises(JsProtocolError) as op_conflict:
            coordinator.mark_dispatched("task_transitions", **read_ids, operation_id="op-read-2")
        assert op_conflict.value.code == "ryn_js_invalid_host_call_transition"
        assert rec.operation_id == "op-read-1"

        # Prohibited backward transition: dispatched -> awaiting_approval
        with pytest.raises(JsProtocolError) as back_disp:
            coordinator.mark_awaiting_approval("task_transitions", **read_ids, approval_id="appr-read-1")
        assert back_disp.value.code == "ryn_js_invalid_host_call_transition"
        assert rec.status == "dispatched"
        assert rec.not_executed is False

        # Complete the read callback
        rec = coordinator.resolve_host_call("task_transitions", **read_ids, result=[{"app": "fixture.exe", "id": 7}])
        assert rec.status == "completed"
        assert rec.outcome == "succeeded"

        # Late mark_awaiting_approval and mark_dispatched on completed record must fail and preserve completed state
        with pytest.raises(JsProtocolError) as late_appr_completed:
            coordinator.mark_awaiting_approval("task_transitions", **read_ids, approval_id="late-appr")
        assert late_appr_completed.value.code == "ryn_js_invalid_host_call_transition"
        assert rec.status == "completed"

        with pytest.raises(JsProtocolError) as late_disp_completed:
            coordinator.mark_dispatched("task_transitions", **read_ids, operation_id="late-op")
        assert late_disp_completed.value.code == "ryn_js_invalid_host_call_transition"
        assert rec.status == "completed"

        with pytest.raises(JsProtocolError) as late_res_completed:
            coordinator.resolve_host_call("task_transitions", **read_ids, result=[])
        assert late_res_completed.value.code == "ryn_js_duplicate_host_result"

        # 2. Concurrent writes: first write (click) is admitted; second write (type_text) is deferred in pending_admission.
        step_write1 = coordinator.poll_step("task_transitions", timeout=2.0)
        assert step_write1.status == "host_call_pending"
        assert step_write1.host_call is not None
        assert step_write1.host_call.method == "click"
        write1_call = step_write1.host_call
        write1_ids = {
            "session_id": write1_call.session_id,
            "execution_id": exec_id,
            "generation": generation,
            "callback_id": write1_call.callback_id,
        }

        deferred_cid = f"cb_{generation}_{exec_id}_3"
        deferred_rec = coordinator.get_record("task_transitions", deferred_cid)
        assert deferred_rec is not None
        assert deferred_rec.status == "pending_admission"
        deferred_ids = {
            "session_id": write1_call.session_id,
            "execution_id": exec_id,
            "generation": generation,
            "callback_id": deferred_cid,
        }

        # Cannot approve, dispatch, or resolve a deferred/unadmitted write before admission
        for fn in (
            lambda: coordinator.mark_awaiting_approval("task_transitions", **deferred_ids, approval_id="early-appr"),
            lambda: coordinator.mark_dispatched("task_transitions", **deferred_ids, operation_id="early-op"),
            lambda: coordinator.resolve_host_call("task_transitions", **deferred_ids, result={"ok": True}),
        ):
            with pytest.raises(JsProtocolError) as unadm_exc:
                fn()
            assert unadm_exc.value.code == "ryn_js_invalid_host_call_transition"
        assert deferred_rec.status == "pending_admission"

        # Reject first write (not dispatched -> rejected / not_executed=True)
        w1_rec = coordinator.reject_host_call(
            "task_transitions",
            **write1_ids,
            code="ryn_approval_denied",
            message="Denied click",
            not_executed=True,
        )
        assert w1_rec.status == "rejected"
        assert w1_rec.not_executed is True

        # Rejected record is terminal and immutable
        for fn in (
            lambda: coordinator.mark_awaiting_approval("task_transitions", **write1_ids, approval_id="late-appr"),
            lambda: coordinator.mark_dispatched("task_transitions", **write1_ids, operation_id="late-op"),
        ):
            with pytest.raises(JsProtocolError) as rej_transition_err:
                fn()
            assert rej_transition_err.value.code == "ryn_js_invalid_host_call_transition"
        assert w1_rec.status == "rejected"

        # 3. Second write is now admitted; dispatch it and cancel the task -> status becomes unknown
        step_write2 = coordinator.poll_step("task_transitions", timeout=2.0)
        assert step_write2.status == "host_call_pending"
        assert step_write2.host_call is not None
        assert step_write2.host_call.callback_id == deferred_cid
        w2_rec = coordinator.mark_dispatched("task_transitions", **deferred_ids, operation_id="op-write-2")
        assert w2_rec.status == "dispatched"

        cancel_snap = coordinator.cancel_task("task_transitions")
        assert cancel_snap is not None
        assert cancel_snap.status in {"cancelled", "lost"}
        assert w2_rec.status == "unknown"
        assert w2_rec.outcome == "unknown"
        assert w2_rec.not_executed is False
        assert w2_rec.operation_id == "op-write-2"

        # Unknown record is terminal and immutable
        for fn in (
            lambda: coordinator.mark_awaiting_approval("task_transitions", **deferred_ids, approval_id="late-appr"),
            lambda: coordinator.mark_dispatched("task_transitions", **deferred_ids, operation_id="late-op-2"),
        ):
            with pytest.raises(JsProtocolError) as unk_transition_err:
                fn()
            assert unk_transition_err.value.code == "ryn_js_invalid_host_call_transition"
        with pytest.raises(JsProtocolError) as unk_res_err:
            coordinator.resolve_host_call("task_transitions", **deferred_ids, result={"ok": True})
        assert unk_res_err.value.code == "ryn_js_duplicate_host_result"
        assert w2_rec.status == "unknown"
        assert w2_rec.not_executed is False
    finally:
        manager.close()


@requires_quickjs_worker
def test_dispatched_worker_loss_surfaces_unknown_refuses_late_callbacks_and_fences_new_generation() -> None:
    manager = _make_dev_manager()
    coordinator = JsExecutionCoordinator(manager)
    try:
        code = """
        const { sky } = await import('@oai/sky');
        globalThis.beforeCrash = (globalThis.beforeCrash || 0) + 1;
        const win = { app: 'fixture.exe', id: 7 };
        await Promise.all([
          sky.click({ window: win, x: 1, y: 2 }),
          sky.type_text({ window: win, text: 'queued-unadmitted' }),
        ]);
        return globalThis.beforeCrash;
        """
        exec_id, gen1 = coordinator.start("task_fatal_loss", code)
        step = coordinator.poll_step("task_fatal_loss", timeout=2.0)
        assert step.status == "host_call_pending"
        assert step.host_call is not None
        assert step.host_call.method == "click"
        call = step.host_call
        call_ids = {
            "session_id": call.session_id,
            "execution_id": call.execution_id,
            "generation": call.generation,
            "callback_id": call.callback_id,
        }
        queued_cid = f"cb_{gen1}_{exec_id}_2"

        coordinator.mark_dispatched("task_fatal_loss", **call_ids, operation_id="operation-fatal-fixed")
        old_session = manager.get_session("task_fatal_loss")
        assert old_session is not None and old_session.process is not None
        owned_pid = old_session.process.pid

        # Terminate only the test-owned Node worker process created by manager
        old_session.process.kill()
        old_session.process.wait(timeout=2.0)

        # Polling must surface lost status (not ryn_js_no_session) and finalize dispatched write as unknown
        lost = coordinator.poll_step("task_fatal_loss", timeout=2.0)
        assert lost.status == "lost"
        assert lost.snapshot.status == "lost"
        assert lost.snapshot.error is not None
        assert lost.snapshot.error["code"] == "ryn_js_worker_lost"

        rec = coordinator.get_record("task_fatal_loss", call.callback_id)
        assert rec is not None
        assert rec.status == "unknown"
        assert rec.outcome == "unknown"
        assert rec.not_executed is False
        assert rec.operation_id == "operation-fatal-fixed"
        assert rec.result is None
        assert rec.envelope.execution_id == exec_id
        assert rec.envelope.generation == gen1

        queued_rec = coordinator.get_record("task_fatal_loss", queued_cid)
        assert queued_rec is not None
        assert queued_rec.status == "rejected"
        assert queued_rec.outcome == "not_executed"
        assert queued_rec.not_executed is True
        assert queued_rec.result is None

        # Subsequent polls on lost worker return stable lost snapshot without implicit respawn or generic missing error
        lost_again = coordinator.poll_step("task_fatal_loss", timeout=0.2)
        assert lost_again.status == "lost"
        assert lost_again.snapshot.execution_id == exec_id
        retained_session = manager.get_session("task_fatal_loss")
        assert retained_session is old_session
        assert not retained_session.is_alive

        # Late host result or state rollback before restart is refused
        with pytest.raises(JsProtocolError) as late_resolve:
            coordinator.resolve_host_call("task_fatal_loss", **call_ids, result={"ok": True})
        assert late_resolve.value.code == "ryn_js_duplicate_host_result"

        with pytest.raises(JsProtocolError) as late_reject:
            coordinator.reject_host_call("task_fatal_loss", **call_ids, code="ryn_late_error")
        assert late_reject.value.code == "ryn_js_duplicate_host_result"

        with pytest.raises(JsProtocolError) as late_appr:
            coordinator.mark_awaiting_approval("task_fatal_loss", **call_ids, approval_id="late-appr")
        assert late_appr.value.code == "ryn_js_invalid_host_call_transition"

        # Explicit new generation after lifecycle boundary spawns a fresh worker and clean realm
        exec_id2, gen2 = coordinator.start("task_fatal_loss", "return typeof globalThis.beforeCrash;")
        fresh = coordinator.poll_step("task_fatal_loss", timeout=2.0)
        new_session = manager.get_session("task_fatal_loss")
        assert new_session is not None
        assert fresh.status == "completed"
        assert fresh.snapshot.value == "undefined"
        assert new_session.pid != owned_pid
        assert gen2 > gen1
        assert new_session.generation == gen2

        # Late callbacks from old generation are refused after restart without touching new realm
        with pytest.raises(JsProtocolError) as late_after_restart:
            coordinator.resolve_host_call("task_fatal_loss", **call_ids, result={"ok": True})
        assert late_after_restart.value.code in {
            "ryn_js_unknown_execution",
            "ryn_js_stale_generation",
            "ryn_js_session_mismatch",
        }

        with pytest.raises(JsProtocolError) as mismatch_gen:
            coordinator.resolve_host_call(
                "task_fatal_loss",
                session_id=new_session.session_id,
                execution_id=exec_id2,
                generation=gen1,
                callback_id=call.callback_id,
                result={"ok": True},
            )
        assert mismatch_gen.value.code == "ryn_js_stale_generation"

        # Old generation's unknown record and terminal snapshot remain accessible until trusted owner retirement
        archived_rec = coordinator.get_record("task_fatal_loss", call.callback_id, execution_id=exec_id)
        assert archived_rec is rec
        assert archived_rec.status == "unknown"
        assert coordinator.get_unknown_records("task_fatal_loss") == [rec]
        archived_snap = coordinator.get_terminal_snapshot("task_fatal_loss", execution_id=exec_id)
        assert archived_snap is not None and archived_snap.status == "lost"

        coordinator.retire_task("task_fatal_loss", execution_id=exec_id)
        assert coordinator.get_record("task_fatal_loss", call.callback_id, execution_id=exec_id) is None
        assert coordinator.get_unknown_records("task_fatal_loss") == []
    finally:
        manager.close()


@requires_quickjs_worker
def test_unknown_write_outcome_is_fatal_fence_and_blocks_guest_catch_fallback_write() -> None:
    manager = _make_dev_manager()
    coordinator = JsExecutionCoordinator(manager)
    try:
        code = """
        const { sky } = await import('@oai/sky');
        const win = { app: 'fixture.exe', id: 7 };
        try {
          await sky.click({ window: win, x: 10, y: 10 });
        } catch (err) {
          await sky.type_text({ window: win, text: 'must-never-be-emitted' });
          return 'caught-and-retried';
        }
        return 'completed';
        """
        exec_id, gen1 = coordinator.start("task_unknown_fence", code)
        step = coordinator.poll_step("task_unknown_fence", timeout=2.0)
        assert step.status == "host_call_pending"
        assert step.host_call is not None
        assert step.host_call.method == "click"
        call = step.host_call
        call_ids = {
            "session_id": call.session_id,
            "execution_id": exec_id,
            "generation": gen1,
            "callback_id": call.callback_id,
        }

        coordinator.mark_dispatched("task_unknown_fence", **call_ids, operation_id="op-uncertain-click")
        rec = coordinator.reject_host_call(
            "task_unknown_fence",
            **call_ids,
            code="ryn_sky_provider_timeout",
            message="Provider timed out after dispatch",
            not_executed=False,
            outcome="unknown",
        )
        assert rec.status == "unknown"
        assert rec.outcome == "unknown"
        assert rec.not_executed is False

        terminal = coordinator.poll_step("task_unknown_fence", timeout=2.0)
        assert terminal.status == "failed"
        assert terminal.host_call is None
        assert terminal.snapshot.error is not None
        assert terminal.snapshot.error["code"] == "ryn_sky_provider_timeout"
        assert terminal.snapshot.error["outcome"] == "unknown"
        assert terminal.snapshot.error["not_executed"] is False

        session = manager.get_session("task_unknown_fence")
        assert session is not None
        assert session.generation == gen1 + 1
    finally:
        manager.close()


@requires_quickjs_worker
def test_coordinator_queued_write_pre_admission_rejection_semantics() -> None:
    manager = _make_dev_manager()
    coordinator = JsExecutionCoordinator(manager)
    try:
        code = """
        const { sky } = await import('@oai/sky');
        const win = { app: 'fixture.exe', id: 7 };
        const settled = await Promise.allSettled([
          sky.click({ window: win, x: 1, y: 2 }),
          sky.type_text({ window: win, text: 'second-queued' }),
        ]);
        return settled.map((item) => ({
          status: item.status,
          code: item.reason ? item.reason.code : null,
          not_executed: item.reason ? item.reason.not_executed : null,
        }));
        """
        exec_id, gen = coordinator.start("task_queued_reject", code)
        step = coordinator.poll_step("task_queued_reject", timeout=2.0)
        assert step.status == "host_call_pending"
        assert step.host_call is not None
        assert step.host_call.method == "click"
        first = step.host_call
        first_ids = {
            "session_id": first.session_id,
            "execution_id": exec_id,
            "generation": gen,
            "callback_id": first.callback_id,
        }
        second_cid = f"cb_{gen}_{exec_id}_2"

        deadline = time.monotonic() + 1.0
        second_rec = coordinator.get_record("task_queued_reject", second_cid)
        while second_rec is None and time.monotonic() < deadline:
            coordinator.poll_step("task_queued_reject", timeout=0.05)
            second_rec = coordinator.get_record("task_queued_reject", second_cid)
        assert second_rec is not None
        assert second_rec.status == "pending_admission"
        assert second_rec.envelope.method == "type_text"
        second_ids = {
            "session_id": first.session_id,
            "execution_id": exec_id,
            "generation": gen,
            "callback_id": second_cid,
        }

        # Premature approval, dispatch, or completion before admission must raise
        for fn in (
            lambda: coordinator.mark_awaiting_approval("task_queued_reject", **second_ids, approval_id="early-appr"),
            lambda: coordinator.mark_dispatched("task_queued_reject", **second_ids, operation_id="early-op"),
            lambda: coordinator.resolve_host_call("task_queued_reject", **second_ids, result={"ok": True}),
        ):
            with pytest.raises(JsProtocolError) as exc_info:
                fn()
            assert exc_info.value.code == "ryn_js_invalid_host_call_transition"
        assert second_rec.status == "pending_admission"

        # Legal pre-admission cancellation of queued write (pending_admission -> rejected)
        rejected_second = coordinator.reject_host_call(
            "task_queued_reject",
            **second_ids,
            code="owner_queued_cancel",
            message="Cancelled queued write before admission",
            not_executed=True,
        )
        assert rejected_second is second_rec
        assert rejected_second.status == "rejected"
        assert rejected_second.not_executed is True
        assert rejected_second.outcome == "not_executed"
        assert rejected_second.operation_id is None
        assert rejected_second.approval_id is None
        assert rejected_second.result is None
        assert rejected_second.envelope.session_id == first.session_id
        assert rejected_second.envelope.execution_id == exec_id
        assert rejected_second.envelope.generation == gen
        assert rejected_second.envelope.callback_id == second_cid

        # Rejected queued record is terminal and immutable
        for fn in (
            lambda: coordinator.mark_awaiting_approval("task_queued_reject", **second_ids, approval_id="late-appr"),
            lambda: coordinator.mark_dispatched("task_queued_reject", **second_ids, operation_id="late-op"),
        ):
            with pytest.raises(JsProtocolError) as late_trans_err:
                fn()
            assert late_trans_err.value.code == "ryn_js_invalid_host_call_transition"
        for fn in (
            lambda: coordinator.resolve_host_call("task_queued_reject", **second_ids, result={"ok": True}),
            lambda: coordinator.reject_host_call("task_queued_reject", **second_ids, code="duplicate_reject"),
        ):
            with pytest.raises(JsProtocolError) as late_res_err:
                fn()
            assert late_res_err.value.code == "ryn_js_duplicate_host_result"

        # Admitted first write independently dispatches and completes exactly once
        first_rec = coordinator.mark_dispatched("task_queued_reject", **first_ids, operation_id="op-first-click")
        assert first_rec.status == "dispatched"
        assert first_rec.operation_id == "op-first-click"
        completed_first = coordinator.resolve_host_call(
            "task_queued_reject",
            **first_ids,
            result={"ok": True, "action": "click"},
        )
        assert completed_first is first_rec
        assert completed_first.status == "completed"
        assert completed_first.not_executed is False
        assert completed_first.outcome == "succeeded"

        done = coordinator.poll_step("task_queued_reject", timeout=2.0)
        assert done.status == "completed"
        assert done.host_call is None
        assert done.snapshot.value == [
            {"status": "fulfilled", "code": None, "not_executed": None},
            {"status": "rejected", "code": "owner_queued_cancel", "not_executed": True},
        ]

        # Records and identities remain stable after completion; no extra write callback was created
        assert coordinator.get_record("task_queued_reject", first.callback_id) is first_rec
        assert first_rec.status == "completed"
        assert first_rec.operation_id == "op-first-click"
        assert coordinator.get_record("task_queued_reject", second_cid) is second_rec
        assert second_rec.status == "rejected"
        assert second_rec.not_executed is True
        assert second_rec.operation_id is None
        assert coordinator.get_record("task_queued_reject", f"cb_{gen}_{exec_id}_3") is None
    finally:
        manager.close()

