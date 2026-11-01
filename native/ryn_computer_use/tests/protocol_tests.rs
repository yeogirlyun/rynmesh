use ryn_computer_use::engine::{OperationControlTable, PhysicalActorEngine};
use ryn_computer_use::errors::{DiagnosticReason, ErrorCode, InternalMutationDetail, MutationState};
use ryn_computer_use::input::{
    execute_resolved_action_with_backend, parse_virtual_key, resolve_action_plan,
    InputExecutionBackend, MouseButtonKind, ResolvedActionPlan, ResolvedExecutionOp,
    SnapshotElementBinding,
};
use ryn_computer_use::input_lock::DesktopInputLock;
use ryn_computer_use::lifecycle_model::{
    HostWorkerNonce, WindowLifecycleRegistry, WindowOsTuple,
};
use ryn_computer_use::protocol::{
    parse_and_validate_request_line, read_bounded_jsonl_frame, ActionSpec, BoundedFrameRead,
    ElementTarget, EventEnvelope, PointTarget, RectPx, RequestEnvelope, ResponseEnvelope,
    UiaElement, WindowGeometry, WindowRef, MAX_COMMAND_LINE_BYTES, WIRE_VERSION,
};
use ryn_computer_use::snapshot::{
    csprng_hex, generate_prepare_ticket, generate_state_token, generate_worker_instance_id,
    SnapshotRecord, SnapshotStore,
};
use ryn_computer_use::uia::{
    evaluate_password_protection, verify_live_element_state_matches_snapshot,
};
use ryn_computer_use::windows::{
    evaluate_directional_window_relation, u64_to_hwnd, WindowIdentityTracker,
    WindowRelationDecision, WindowRelationNode,
};
use std::collections::{HashMap, VecDeque};
use std::io::{self, BufReader, Cursor, Read};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use windows::Win32::Foundation::HWND;
use windows::Win32::UI::Input::KeyboardAndMouse::{
    INPUT, INPUT_KEYBOARD, INPUT_MOUSE, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, MOUSEEVENTF_HWHEEL,
    MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_MOVE, MOUSEEVENTF_WHEEL,
};

#[test]
fn test_wire_v1_strict_envelope_validation() {
    // 1. Missing `v` field must be rejected with InvalidRequest + MissingVersion
    let missing_v = r#"{"id":"req-1","method":"initialize","params":{}}"#;
    let err = parse_and_validate_request_line(missing_v).unwrap_err().1;
    assert_eq!(err.code, ErrorCode::InvalidRequest);
    assert_eq!(
        err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::MissingVersion)
    );

    // 2. Wrong `v` version must be rejected with UnsupportedProtocolVersion + VersionMismatch
    let wrong_v = r#"{"v":2,"id":"req-2","method":"initialize","params":{}}"#;
    let err = parse_and_validate_request_line(wrong_v).unwrap_err().1;
    assert_eq!(err.code, ErrorCode::UnsupportedProtocolVersion);
    assert_eq!(
        err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::VersionMismatch)
    );

    // 3. Unknown top-level field must be rejected
    let unknown_top = r#"{"v":1,"id":"req-3","method":"initialize","params":{},"extra":true}"#;
    let err = parse_and_validate_request_line(unknown_top).unwrap_err().1;
    assert_eq!(err.code, ErrorCode::InvalidRequest);

    // 4. Duplicate keys at top level or nested inside params must be rejected
    let dup_top = r#"{"v":1,"id":"req-4","id":"req-4-override","method":"initialize","params":{}}"#;
    let err = parse_and_validate_request_line(dup_top).unwrap_err().1;
    assert_eq!(err.code, ErrorCode::InvalidRequest);
    assert_eq!(
        err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::DuplicateJsonKey)
    );

    let dup_nested = r#"{"v":1,"id":"req-5","method":"windows.list","params":{"maxWindows":10,"maxWindows":99}}"#;
    let err = parse_and_validate_request_line(dup_nested).unwrap_err().1;
    assert_eq!(err.code, ErrorCode::InvalidRequest);
    assert_eq!(
        err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::DuplicateJsonKey)
    );

    // 5. Valid v=1 request parses cleanly and ResponseEnvelope / EventEnvelope carry v=1
    let valid = r#"{"v":1,"id":"req-ok","method":"windows.list","params":{"maxWindows":5}}"#;
    let parsed = parse_and_validate_request_line(valid).expect("valid v=1 envelope should parse");
    assert_eq!(parsed.v, WIRE_VERSION);
    assert_eq!(parsed.method, "windows.list");

    let ok_resp = ResponseEnvelope::ok(parsed.id.clone(), serde_json::json!({"ok": true})).unwrap();
    assert!(ok_resp.validate_invariant());
    let resp_json = serde_json::to_value(&ok_resp).unwrap();
    assert_eq!(resp_json.get("v").and_then(|v| v.as_u64()), Some(1));

    let ev = EventEnvelope::new(
        "action.ready",
        parsed.id,
        Some("op-1".to_string()),
        serde_json::json!({"status": "ready"}),
    )
    .unwrap();
    let ev_json = serde_json::to_value(&ev).unwrap();
    assert_eq!(ev_json.get("v").and_then(|v| v.as_u64()), Some(1));
}

#[test]
fn test_removed_bypass_fields_and_compound_actions_rejected() {
    let mut engine =
        PhysicalActorEngine::new_inert_for_protocol_test(OperationControlTable::new()).unwrap();
    let init_req = parse_and_validate_request_line(
        r#"{"v":1,"id":"init-1","method":"initialize","params":{"protocolVersion":"1.0.0","client":{"name":"test","version":"1.0"}}}"#,
    )
    .unwrap();
    let init_resp = engine.handle_request(init_req, &mut |_| {});
    assert!(init_resp.error.is_none());

    // 1. Reject `requireStateMatch: false` or `acquireInputLock: false` in action.prepare constraints
    let bypass_prepare = parse_and_validate_request_line(
        r#"{"v":1,"id":"prep-bypass","method":"action.prepare","params":{"taskSessionId":"t1","operationId":"op1","window":{"workerInstance":"wrk_1","windowKey":"win_1","hwnd":"123","pid":1,"processStarted":"1","generation":1},"expectedStateToken":"st_1","action":{"type":"focus_window"},"constraints":{"requireStateMatch":false}}}"#,
    )
    .unwrap();
    let resp = engine.handle_request(bypass_prepare, &mut |_| {});
    assert_eq!(resp.error.unwrap().code, ErrorCode::InvalidParams);

    // 2. Reject `TypeText` with `submit: true` or `clearExisting: true`
    let compound_type = parse_and_validate_request_line(
        r#"{"v":1,"id":"prep-compound","method":"action.prepare","params":{"taskSessionId":"t1","operationId":"op2","window":{"workerInstance":"wrk_1","windowKey":"win_1","hwnd":"123","pid":1,"processStarted":"1","generation":1},"expectedStateToken":"st_1","action":{"type":"type_text","text":"hello","submit":true}}}"#,
    )
    .unwrap();
    let resp = engine.handle_request(compound_type, &mut |_| {});
    assert_eq!(resp.error.unwrap().code, ErrorCode::InvalidParams);

    // 3. Reject `ActionSpec::Batch`
    let batch_action = parse_and_validate_request_line(
        r#"{"v":1,"id":"prep-batch","method":"action.prepare","params":{"taskSessionId":"t1","operationId":"op3","window":{"workerInstance":"wrk_1","windowKey":"win_1","hwnd":"123","pid":1,"processStarted":"1","generation":1},"expectedStateToken":"st_1","action":{"type":"batch","actions":[]}}}"#,
    )
    .unwrap();
    let resp = engine.handle_request(batch_action, &mut |_| {});
    assert_eq!(resp.error.unwrap().code, ErrorCode::InvalidParams);

    // 4. Reject `closeLaunchedWindows` on `session.end`
    let session_bypass = parse_and_validate_request_line(
        r#"{"v":1,"id":"sess-bypass","method":"session.end","params":{"taskSessionId":"t1","closeLaunchedWindows":true}}"#,
    )
    .unwrap();
    let resp = engine.handle_request(session_bypass, &mut |_| {});
    assert_eq!(resp.error.unwrap().code, ErrorCode::InvalidParams);

    // 5. Reject `focus` override on `action.commit`
    let commit_override = parse_and_validate_request_line(
        r#"{"v":1,"id":"commit-override","method":"action.commit","params":{"taskSessionId":"t1","operationId":"op1","prepareTicket":"pt_1","focus":{"required":false}}}"#,
    )
    .unwrap();
    let resp = engine.handle_request(commit_override, &mut |_| {});
    assert_eq!(resp.error.unwrap().code, ErrorCode::InvalidParams);

    // 6. Direct `apps.launch` must be rejected as unsupported without spawning anything
    let direct_launch = parse_and_validate_request_line(
        r#"{"v":1,"id":"launch-direct","method":"apps.launch","params":{"taskSessionId":"t1","operationId":"op-launch","target":{"kind":"app_id","appId":"notepad.exe"}}}"#,
    )
    .unwrap();
    let resp = engine.handle_request(direct_launch, &mut |_| {});
    let err = resp.error.unwrap();
    assert_eq!(err.code, ErrorCode::Unsupported);
    assert_eq!(err.mutation, MutationState::None);
    assert_eq!(
        err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::DirectLaunchDisallowed)
    );
}

#[test]
fn test_error_redaction_never_echoes_caller_sentinels() {
    let sentinel_secret = "SENTINEL_TOP_SECRET_TOKEN_998877";
    let sentinel_path = "C:\\Users\\ConfidentialUser\\SecretVault\\app.exe";

    let mut engine =
        PhysicalActorEngine::new_inert_for_protocol_test(OperationControlTable::new()).unwrap();
    let init_req = parse_and_validate_request_line(
        r#"{"v":1,"id":"init-1","method":"initialize","params":{"protocolVersion":"1.0.0","client":{"name":"test","version":"1.0"}}}"#,
    )
    .unwrap();
    let _ = engine.handle_request(init_req, &mut |_| {});

    let bad_get_line = format!(
        r#"{{"v":1,"id":"req-sec-1","method":"windows.get","params":{{"hwnd":"{}"}}}}"#,
        sentinel_secret
    );
    let req1 = parse_and_validate_request_line(&bad_get_line).unwrap();
    let resp1 = engine.handle_request(req1, &mut |_| {});
    let serialized1 = serde_json::to_string(&resp1).unwrap();
    assert!(
        !serialized1.contains(sentinel_secret),
        "Error payload leaked sentinel secret: {}",
        serialized1
    );

    let bad_launch_line = format!(
        r#"{{"v":1,"id":"req-sec-2","method":"apps.launch","params":{{"taskSessionId":"t1","operationId":"op-sec","target":{{"kind":"executable","executablePath":"{}","args":["{}"]}}}}}}"#,
        sentinel_path.replace('\\', "\\\\"),
        sentinel_secret
    );
    let req2 = parse_and_validate_request_line(&bad_launch_line).unwrap();
    let resp2 = engine.handle_request(req2, &mut |_| {});
    let serialized2 = serde_json::to_string(&resp2).unwrap();
    assert!(!serialized2.contains(sentinel_secret));
    assert!(!serialized2.contains("ConfidentialUser"));
    assert!(!serialized2.contains("SecretVault"));
}

#[test]
fn test_csprng_tokens_and_snapshot_isolation() {
    let w1 = generate_worker_instance_id();
    let w2 = generate_worker_instance_id();
    assert_ne!(w1, w2);
    assert!(w1.starts_with("wrk_") && w1.len() == 36);

    let st1 = generate_state_token();
    let st2 = generate_state_token();
    assert_ne!(st1, st2);
    assert!(st1.starts_with("st_") && st1.len() == 35);

    let pt1 = generate_prepare_ticket();
    let pt2 = generate_prepare_ticket();
    assert_ne!(pt1, pt2);
    assert!(pt1.starts_with("pt_") && pt1.len() == 35);

    let raw_hex = csprng_hex(16);
    assert_eq!(raw_hex.len(), 32);

    let mut reg = WindowLifecycleRegistry::new(
        HostWorkerNonce::new(0x1111_2222_3333_4444_5555_6666_7777_8888).unwrap(),
        1,
        16,
    )
    .unwrap();
    let issued = reg
        .issue_or_reuse(WindowOsTuple::new(1001, 4242, 133_000_000_000_000_000).unwrap())
        .unwrap();
    let stamp = issued.stamp;

    let store = SnapshotStore::new();
    let wref = WindowRef {
        worker_instance: stamp.host_nonce.worker_instance_label(),
        window_key: stamp.window_key.clone(),
        hwnd: "1001".to_string(),
        pid: 4242,
        process_started: "133000000000000000".to_string(),
        generation: stamp.issue_generation,
        app_id: Some("fixture".to_string()),
    };
    let geom = WindowGeometry {
        window_rect: RectPx::from_ltrb(100, 100, 500, 400),
        client_rect: RectPx::from_ltrb(108, 132, 492, 392),
        screen_origin: [100, 100],
        dpi: 96,
        scale_factor: 1.0,
    };

    let rec = store.insert_snapshot(
        "task-alpha".to_string(),
        wref.clone(),
        stamp.clone(),
        geom.clone(),
        Vec::new(),
        "hash0".to_string(),
    );

    // Valid match succeeds
    assert!(store
        .validate_state_token(&rec.state_token, "task-alpha", &wref, &stamp, &geom)
        .is_ok());

    // Cross-task reuse rejected with TaskConflict
    let cross_task_err = store
        .validate_state_token(&rec.state_token, "task-beta", &wref, &stamp, &geom)
        .unwrap_err();
    assert_eq!(cross_task_err.code, ErrorCode::TaskConflict);

    // Moved geometry rejected with StateTokenStale + GeometryChanged
    let mut moved_geom = geom.clone();
    moved_geom.window_rect = RectPx::from_ltrb(120, 100, 520, 400);
    let moved_err = store
        .validate_state_token(&rec.state_token, "task-alpha", &wref, &stamp, &moved_geom)
        .unwrap_err();
    assert_eq!(moved_err.code, ErrorCode::StateTokenStale);
    assert_eq!(
        moved_err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::GeometryChanged)
    );

    // Changed DPI rejected with StateTokenStale + DpiChanged
    let mut dpi_geom = geom.clone();
    dpi_geom.dpi = 144;
    let dpi_err = store
        .validate_state_token(&rec.state_token, "task-alpha", &wref, &stamp, &dpi_geom)
        .unwrap_err();
    assert_eq!(dpi_err.code, ErrorCode::StateTokenStale);
    assert_eq!(
        dpi_err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::DpiChanged)
    );

    // Advancing mutation epoch invalidates old state token
    store.advance_window_epoch(&wref.window_key);
    let epoch_err = store
        .validate_state_token(&rec.state_token, "task-alpha", &wref, &stamp, &geom)
        .unwrap_err();
    assert_eq!(epoch_err.code, ErrorCode::StateTokenStale);
    assert_eq!(
        epoch_err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::EpochAdvanced)
    );
}

#[test]
fn test_input_lock_same_thread_acquire_cancel_and_release() {
    let lock = DesktopInputLock::new().expect("DesktopInputLock should initialize on real desktop");
    assert!(lock
        .mutex_name()
        .starts_with("Local\\RynComputerUseInputLock_v1_"));

    let cancel_flag = AtomicBool::new(false);
    let mut guard = lock
        .acquire_for_current_thread("op-lock-1".to_string(), 1000, &cancel_flag)
        .expect("Should acquire desktop input lock");
    let debug_str = format!("{:?}", guard);
    assert!(debug_str.contains("DesktopInputLockGuard"));
    assert!(debug_str.contains("held: true"));

    // In-process contention from a second acquire while held must time out with InputLockBusy
    let busy_err = match lock.acquire_for_current_thread("op-lock-2".to_string(), 60, &cancel_flag)
    {
        Err(err) => err,
        Ok(_) => panic!("Expected InputLockBusy when lock is already held in-process"),
    };
    assert_eq!(busy_err.code, ErrorCode::InputLockBusy);
    assert_eq!(
        busy_err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::MutexWaitTimeout)
    );

    // Pre-cancelled flag aborts immediately with CancelledBeforeLock
    let cancelled_flag = AtomicBool::new(true);
    let cancel_err =
        match lock.acquire_for_current_thread("op-lock-3".to_string(), 1000, &cancelled_flag) {
            Err(err) => err,
            Ok(_) => panic!("Expected Cancelled when cancel_flag is set prior to lock acquisition"),
        };
    assert_eq!(cancel_err.code, ErrorCode::Cancelled);
    assert_eq!(
        cancel_err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::CancelledBeforeLock)
    );

    // Release on the owning thread succeeds and does not quarantine the lock
    guard
        .release()
        .expect("ReleaseMutex on owning thread should succeed");
    assert!(!lock.is_quarantined());
}

struct ChunkedBoundedTestReader {
    chunks: Vec<Vec<u8>>,
    index: usize,
    total_bytes_yielded: usize,
    panic_if_read_past_bytes: Option<usize>,
}

impl Read for ChunkedBoundedTestReader {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if self.index >= self.chunks.len() {
            return Ok(0);
        }
        if let Some(limit) = self.panic_if_read_past_bytes {
            assert!(
                self.total_bytes_yielded < limit,
                "Reader polled past bounded cap ({} >= {}); stream read is not bounded!",
                self.total_bytes_yielded,
                limit
            );
        }
        let chunk = &self.chunks[self.index];
        let n = chunk.len().min(buf.len());
        buf[..n].copy_from_slice(&chunk[..n]);
        if n == chunk.len() {
            self.index += 1;
        } else {
            self.chunks[self.index].drain(..n);
        }
        self.total_bytes_yielded += n;
        Ok(n)
    }
}

#[test]
fn test_bounded_jsonl_frame_limit_boundary_oversized_chunked_and_no_newline() {
    let mut scratch = Vec::new();

    // 1a. Exact 64 KiB content bytes with LF (65,536 ASCII bytes + '\n' = 65,537 raw bytes) succeeds
    // and strips the delimiter to return exactly MAX_COMMAND_LINE_BYTES (65,536).
    let exact_content_lf_payload = format!("{}\n", "a".repeat(MAX_COMMAND_LINE_BYTES));
    assert_eq!(exact_content_lf_payload.len(), MAX_COMMAND_LINE_BYTES + 1);
    let mut reader = BufReader::new(Cursor::new(exact_content_lf_payload.as_bytes()));
    let frame = read_bounded_jsonl_frame(&mut reader, MAX_COMMAND_LINE_BYTES, &mut scratch)
        .expect("Exact 64 KiB content with LF must succeed");
    match frame {
        BoundedFrameRead::Line(s) => assert_eq!(s.len(), MAX_COMMAND_LINE_BYTES),
        other => panic!("Expected BoundedFrameRead::Line, got {:?}", other),
    }

    // 1b. 64 KiB - 1 content bytes + LF (65,535 ASCII bytes + '\n' = 65,536 raw frame bytes) succeeds
    // and strips the delimiter to return MAX_COMMAND_LINE_BYTES - 1 (65,535).
    let raw_cap_lf_payload = format!("{}\n", "a".repeat(MAX_COMMAND_LINE_BYTES - 1));
    assert_eq!(raw_cap_lf_payload.len(), MAX_COMMAND_LINE_BYTES);
    let mut reader = BufReader::new(Cursor::new(raw_cap_lf_payload.as_bytes()));
    let frame = read_bounded_jsonl_frame(&mut reader, MAX_COMMAND_LINE_BYTES, &mut scratch)
        .expect("65,535 content bytes + LF must succeed");
    match frame {
        BoundedFrameRead::Line(s) => assert_eq!(s.len(), MAX_COMMAND_LINE_BYTES - 1),
        other => panic!("Expected BoundedFrameRead::Line, got {:?}", other),
    }

    // 2. Exact 64 KiB content bytes with CRLF (65,536 content bytes + "\r\n") succeeds
    // and strips CRLF to return exactly MAX_COMMAND_LINE_BYTES (65,536).
    let exact_crlf_payload = format!("{}\r\n", "b".repeat(MAX_COMMAND_LINE_BYTES));
    assert_eq!(exact_crlf_payload.len(), MAX_COMMAND_LINE_BYTES + 2);
    let mut reader = BufReader::new(Cursor::new(exact_crlf_payload.as_bytes()));
    let frame = read_bounded_jsonl_frame(&mut reader, MAX_COMMAND_LINE_BYTES, &mut scratch)
        .expect("Exact 64 KiB content with CRLF must succeed");
    match frame {
        BoundedFrameRead::Line(s) => assert_eq!(s.len(), MAX_COMMAND_LINE_BYTES),
        other => panic!("Expected BoundedFrameRead::Line, got {:?}", other),
    }

    // 2b. Split CR and LF across separate chunks at exact 64 KiB content boundary succeeds
    let split_cr_lf = ChunkedBoundedTestReader {
        chunks: vec![
            vec![b'k'; MAX_COMMAND_LINE_BYTES],
            vec![b'\r'],
            vec![b'\n'],
        ],
        index: 0,
        total_bytes_yielded: 0,
        panic_if_read_past_bytes: None,
    };
    let mut split_reader = BufReader::with_capacity(16, split_cr_lf);
    let frame = read_bounded_jsonl_frame(&mut split_reader, MAX_COMMAND_LINE_BYTES, &mut scratch)
        .expect("Split CR/LF at exact cap must succeed");
    match frame {
        BoundedFrameRead::Line(s) => assert_eq!(s.len(), MAX_COMMAND_LINE_BYTES),
        other => panic!("Expected BoundedFrameRead::Line, got {:?}", other),
    }

    // 3. 64 KiB + 1 byte before newline is immediately rejected as Oversized
    let over_by_one_payload = format!("{}\n", "c".repeat(MAX_COMMAND_LINE_BYTES + 1));
    let mut reader = BufReader::new(Cursor::new(over_by_one_payload.as_bytes()));
    let frame = read_bounded_jsonl_frame(&mut reader, MAX_COMMAND_LINE_BYTES, &mut scratch)
        .expect("Oversized read should return Ok(BoundedFrameRead::Oversized)");
    assert_eq!(frame, BoundedFrameRead::Oversized);
    assert!(scratch.len() <= MAX_COMMAND_LINE_BYTES + 2);

    // 4. Chunked oversized input without newline stops reading at cap + 1 without buffering the rest
    // We supply twenty 8 KiB chunks (160 KiB total) with no newline, and configure the reader to
    // panic if more than 72 KiB (9 chunks) is ever pulled from the underlying Read stream.
    let mut chunks = Vec::new();
    for _ in 0..20 {
        chunks.push(vec![b'x'; 8192]);
    }
    let chunked_source = ChunkedBoundedTestReader {
        chunks,
        index: 0,
        total_bytes_yielded: 0,
        panic_if_read_past_bytes: Some(MAX_COMMAND_LINE_BYTES + 8192),
    };
    let mut chunked_reader = BufReader::with_capacity(8192, chunked_source);
    let frame = read_bounded_jsonl_frame(&mut chunked_reader, MAX_COMMAND_LINE_BYTES, &mut scratch)
        .expect("Chunked oversized no-newline stream must terminate at cap + 1");
    assert_eq!(frame, BoundedFrameRead::Oversized);
    assert!(
        scratch.len() <= MAX_COMMAND_LINE_BYTES + 2,
        "Scratch buffer grew beyond cap + 2: {}",
        scratch.len()
    );

    // 5. Unterminated non-empty line at EOF (<= 64 KiB, no '\n') returns UnterminatedEof
    let unterminated = b"{\"v\":1,\"id\":\"req-unterminated\",\"method\":\"initialize\",\"params\":{}}";
    let mut reader = BufReader::new(Cursor::new(&unterminated[..]));
    let frame = read_bounded_jsonl_frame(&mut reader, MAX_COMMAND_LINE_BYTES, &mut scratch)
        .expect("Unterminated EOF should return Ok(BoundedFrameRead::UnterminatedEof)");
    assert_eq!(frame, BoundedFrameRead::UnterminatedEof);

    // 6. Clean EOF on empty stream returns BoundedFrameRead::Eof
    let mut empty_reader = BufReader::new(Cursor::new(&b""[..]));
    let frame = read_bounded_jsonl_frame(&mut empty_reader, MAX_COMMAND_LINE_BYTES, &mut scratch)
        .expect("Empty stream should return Ok(BoundedFrameRead::Eof)");
    assert_eq!(frame, BoundedFrameRead::Eof);

    // 7. Invalid UTF-8 within cap returns BoundedFrameRead::InvalidUtf8
    let bad_utf8 = b"{\"v\":1,\"id\":\"\xFF\xFE\"}\n";
    let mut bad_utf8_reader = BufReader::new(Cursor::new(&bad_utf8[..]));
    let frame =
        read_bounded_jsonl_frame(&mut bad_utf8_reader, MAX_COMMAND_LINE_BYTES, &mut scratch)
            .expect("Invalid UTF-8 frame should return Ok(BoundedFrameRead::InvalidUtf8)");
    assert_eq!(frame, BoundedFrameRead::InvalidUtf8);

    // 8. Multiple valid frames back-to-back followed by Eof
    let multi = b"{\"v\":1,\"id\":1,\"method\":\"a\",\"params\":{}}\n{\"v\":1,\"id\":2,\"method\":\"b\",\"params\":{}}\r\n";
    let mut multi_reader = BufReader::new(Cursor::new(&multi[..]));
    let f1 = read_bounded_jsonl_frame(&mut multi_reader, MAX_COMMAND_LINE_BYTES, &mut scratch)
        .unwrap();
    let f2 = read_bounded_jsonl_frame(&mut multi_reader, MAX_COMMAND_LINE_BYTES, &mut scratch)
        .unwrap();
    let f3 = read_bounded_jsonl_frame(&mut multi_reader, MAX_COMMAND_LINE_BYTES, &mut scratch)
        .unwrap();
    assert_eq!(
        f1,
        BoundedFrameRead::Line("{\"v\":1,\"id\":1,\"method\":\"a\",\"params\":{}}".to_string())
    );
    assert_eq!(
        f2,
        BoundedFrameRead::Line("{\"v\":1,\"id\":2,\"method\":\"b\",\"params\":{}}".to_string())
    );
    assert_eq!(f3, BoundedFrameRead::Eof);
}

// ============================================================================
// Pure / Injectable Counter & Fault Tests for P1 Static Findings (Zero UI / Zero OS Windows)
// ============================================================================

#[derive(Debug, Clone, PartialEq, Eq)]
enum RecordedInputOp {
    MouseMove,
    MouseDown,
    MouseUp,
    MouseWheel,
    MouseHWheel,
    KeyDown(u16, u16, bool), // (vk, scan, is_unicode)
    KeyUp(u16, u16, bool),
}

struct MockInjectableBackend {
    target_hwnd: HWND,
    foreground_ok: bool,
    hit_test_ok: bool,
    live_bounds: HashMap<String, RectPx>,
    live_enabled: bool,
    live_offscreen: bool,
    live_password: Option<bool>,
    uia_focus_result: Result<(), ErrorCode>,
    uia_invoke_result: Result<(), ErrorCode>,
    send_input_script: VecDeque<usize>,
    cancel_on_sleep: Option<Arc<AtomicBool>>,
    cancel_after_first_send_batch: Option<Arc<AtomicBool>>,
    lose_foreground_after_first_send_batch: bool,
    shift_bounds_on_sleep: Option<(String, RectPx)>,
    recorded_ops: Vec<RecordedInputOp>,
    send_batch_calls: usize,
    uia_focus_calls: usize,
    uia_invoke_calls: usize,
    uia_set_value_calls: usize,
    uia_select_calls: usize,
}

impl MockInjectableBackend {
    fn new(target_hwnd: HWND) -> Self {
        Self {
            target_hwnd,
            foreground_ok: true,
            hit_test_ok: true,
            live_bounds: HashMap::new(),
            live_enabled: true,
            live_offscreen: false,
            live_password: Some(false),
            uia_focus_result: Ok(()),
            uia_invoke_result: Ok(()),
            send_input_script: VecDeque::new(),
            cancel_on_sleep: None,
            cancel_after_first_send_batch: None,
            lose_foreground_after_first_send_batch: false,
            shift_bounds_on_sleep: None,
            recorded_ops: Vec::new(),
            send_batch_calls: 0,
            uia_focus_calls: 0,
            uia_invoke_calls: 0,
            uia_set_value_calls: 0,
            uia_select_calls: 0,
        }
    }

    fn decode_input(input: &INPUT) -> RecordedInputOp {
        unsafe {
            if input.r#type == INPUT_MOUSE {
                let flags = input.Anonymous.mi.dwFlags;
                if flags.contains(MOUSEEVENTF_LEFTDOWN) {
                    RecordedInputOp::MouseDown
                } else if flags.contains(MOUSEEVENTF_LEFTUP) {
                    RecordedInputOp::MouseUp
                } else if flags.contains(MOUSEEVENTF_WHEEL) {
                    RecordedInputOp::MouseWheel
                } else if flags.contains(MOUSEEVENTF_HWHEEL) {
                    RecordedInputOp::MouseHWheel
                } else if flags.contains(MOUSEEVENTF_MOVE) {
                    RecordedInputOp::MouseMove
                } else {
                    RecordedInputOp::MouseMove
                }
            } else if input.r#type == INPUT_KEYBOARD {
                let ki = input.Anonymous.ki;
                let is_uni = ki.dwFlags.contains(KEYEVENTF_UNICODE);
                if ki.dwFlags.contains(KEYEVENTF_KEYUP) {
                    RecordedInputOp::KeyUp(ki.wVk.0, ki.wScan, is_uni)
                } else {
                    RecordedInputOp::KeyDown(ki.wVk.0, ki.wScan, is_uni)
                }
            } else {
                RecordedInputOp::MouseMove
            }
        }
    }
}

impl InputExecutionBackend for MockInjectableBackend {
    fn check_foreground(&mut self, _target_hwnd: HWND) -> (bool, HWND) {
        if self.foreground_ok {
            (true, self.target_hwnd)
        } else {
            (false, u64_to_hwnd(99999))
        }
    }

    fn check_hit_test(
        &mut self,
        target_hwnd: HWND,
        _screen_pt: [i32; 2],
        stage: ryn_computer_use::errors::ErrorStage,
        internal_mutation: InternalMutationDetail,
    ) -> Result<(), ryn_computer_use::errors::ProtocolError> {
        if self.hit_test_ok {
            Ok(())
        } else {
            Err(ryn_computer_use::errors::ProtocolError::commit(
                ErrorCode::WindowOccluded,
                stage,
                internal_mutation,
                DiagnosticReason::HitTestUnrelatedWindow,
            )
            .with_hwnds(Some(target_hwnd.0 as usize as u64), Some(99999)))
        }
    }

    fn verify_live_element(
        &mut self,
        _target_hwnd: HWND,
        binding: &SnapshotElementBinding,
        allow_password_target: bool,
        cancel_flag: &AtomicBool,
    ) -> Result<RectPx, ryn_computer_use::errors::ProtocolError> {
        if cancel_flag.load(Ordering::SeqCst) {
            return Err(ryn_computer_use::errors::ProtocolError::commit(
                ErrorCode::Cancelled,
                ryn_computer_use::errors::ErrorStage::Validating,
                InternalMutationDetail::FocusOnly,
                DiagnosticReason::CancelledDuringExecution,
            ));
        }
        let Some(live_rect) = self.live_bounds.get(&binding.runtime_id).copied() else {
            return Err(ryn_computer_use::errors::ProtocolError::commit(
                ErrorCode::UiaElementStale,
                ryn_computer_use::errors::ErrorStage::Validating,
                InternalMutationDetail::FocusOnly,
                DiagnosticReason::UiaPatternMissing,
            ));
        };
        verify_live_element_state_matches_snapshot(
            &binding.runtime_id,
            &binding.snapshot_bounds,
            allow_password_target,
            &binding.runtime_id,
            &live_rect,
            Some(self.live_enabled),
            Some(self.live_offscreen),
            self.live_password,
        )?;
        Ok(live_rect)
    }

    fn send_input_batch(&mut self, inputs: &[INPUT]) -> usize {
        self.send_batch_calls += 1;
        let to_accept = self
            .send_input_script
            .pop_front()
            .unwrap_or(inputs.len())
            .min(inputs.len());
        for inp in &inputs[..to_accept] {
            self.recorded_ops.push(Self::decode_input(inp));
        }
        if self.send_batch_calls == 1 {
            if let Some(ref flag) = self.cancel_after_first_send_batch {
                flag.store(true, Ordering::SeqCst);
            }
            if self.lose_foreground_after_first_send_batch {
                self.foreground_ok = false;
            }
        }
        to_accept
    }

    fn uia_focus(
        &mut self,
        target_hwnd: HWND,
        binding: &SnapshotElementBinding,
        allow_password_target: bool,
        cancel_flag: &AtomicBool,
    ) -> Result<(), ryn_computer_use::errors::ProtocolError> {
        self.verify_live_element(target_hwnd, binding, allow_password_target, cancel_flag)?;
        self.uia_focus_calls += 1;
        match self.uia_focus_result {
            Ok(()) => Ok(()),
            Err(code) => Err(ryn_computer_use::errors::ProtocolError::commit(
                code,
                ryn_computer_use::errors::ErrorStage::Activating,
                InternalMutationDetail::FocusOnly,
                DiagnosticReason::ForegroundVerificationFailed,
            )),
        }
    }

    fn uia_invoke(
        &mut self,
        target_hwnd: HWND,
        binding: &SnapshotElementBinding,
        cancel_flag: &AtomicBool,
    ) -> Result<(), ryn_computer_use::errors::ProtocolError> {
        self.verify_live_element(target_hwnd, binding, false, cancel_flag)?;
        self.uia_invoke_calls += 1;
        match self.uia_invoke_result {
            Ok(()) => Ok(()),
            Err(code) => Err(ryn_computer_use::errors::ProtocolError::commit(
                code,
                ryn_computer_use::errors::ErrorStage::InputStarted,
                InternalMutationDetail::UnknownOrPartial,
                DiagnosticReason::UiaPatternMissing,
            )),
        }
    }

    fn uia_set_value(
        &mut self,
        target_hwnd: HWND,
        binding: &SnapshotElementBinding,
        _value: &str,
        cancel_flag: &AtomicBool,
    ) -> Result<(), ryn_computer_use::errors::ProtocolError> {
        self.verify_live_element(target_hwnd, binding, false, cancel_flag)?;
        self.uia_set_value_calls += 1;
        Ok(())
    }

    fn uia_select(
        &mut self,
        target_hwnd: HWND,
        binding: &SnapshotElementBinding,
        cancel_flag: &AtomicBool,
    ) -> Result<(), ryn_computer_use::errors::ProtocolError> {
        self.verify_live_element(target_hwnd, binding, false, cancel_flag)?;
        self.uia_select_calls += 1;
        Ok(())
    }

    fn sleep_ms(&mut self, _ms: u64) {
        if let Some(ref flag) = self.cancel_on_sleep {
            flag.store(true, Ordering::SeqCst);
        }
        if let Some((ref rid, new_rect)) = self.shift_bounds_on_sleep {
            self.live_bounds.insert(rid.clone(), new_rect);
        }
    }
}

#[test]
fn test_p1_pure_fault_injection_focus_cancel_short_sendinput_bounds_and_relations() {
    let fake_hwnd = u64_to_hwnd(0x10010);
    let cancel_flag = Arc::new(AtomicBool::new(false));
    let edit_bounds = RectPx::from_ltrb(120, 180, 320, 210);
    let edit_binding = SnapshotElementBinding {
        index: 2,
        runtime_id: "42.1002".to_string(),
        snapshot_bounds: edit_bounds,
        snapshot_password: false,
    };

    // ------------------------------------------------------------------------
    // P1 #1a: Element SetFocus failure or stale runtimeId in `type_text(element)`
    // MUST block typing (zero keyboard events sent, zero fallback click on old center).
    // ------------------------------------------------------------------------
    {
        let plan = ResolvedActionPlan {
            summary: ryn_computer_use::protocol::ResolvedTargetSummary {
                window_hwnd: "65552".to_string(),
                screen_point: Some([220, 195]),
                secondary_screen_point: None,
                element_index: Some(2),
                element_runtime_id: Some("42.1002".to_string()),
            },
            op: ResolvedExecutionOp::TypeText {
                click_first_pt: None,
                element_binding: Some(edit_binding.clone()),
                text: "Hello中文".to_string(),
            },
        };

        // Case 1: SetFocus returns Err(FocusFailed) -> 0 SendInput batches, 0 key events
        let mut backend = MockInjectableBackend::new(fake_hwnd);
        backend
            .live_bounds
            .insert("42.1002".to_string(), edit_bounds);
        backend.uia_focus_result = Err(ErrorCode::FocusFailed);

        let err =
            execute_resolved_action_with_backend(fake_hwnd, &plan, &cancel_flag, &mut backend)
                .expect_err("type_text must fail when element SetFocus fails");
        assert_eq!(err.code, ErrorCode::FocusFailed);
        assert_eq!(backend.uia_focus_calls, 1);
        assert_eq!(backend.send_batch_calls, 0);
        assert!(backend.recorded_ops.is_empty());

        // Case 2: Element disappeared (missing from live_bounds) -> UiaElementStale, 0 focus/input
        let mut backend_missing = MockInjectableBackend::new(fake_hwnd);
        let err_missing = execute_resolved_action_with_backend(
            fake_hwnd,
            &plan,
            &cancel_flag,
            &mut backend_missing,
        )
        .expect_err("type_text must fail when element is missing");
        assert_eq!(err_missing.code, ErrorCode::UiaElementStale);
        assert_eq!(backend_missing.uia_focus_calls, 0);
        assert_eq!(backend_missing.send_batch_calls, 0);

        // Case 3: Foreground lost after first Unicode code unit -> stops immediately before 2nd character
        let mut backend_fg_loss = MockInjectableBackend::new(fake_hwnd);
        backend_fg_loss
            .live_bounds
            .insert("42.1002".to_string(), edit_bounds);
        backend_fg_loss.lose_foreground_after_first_send_batch = true;
        let err_fg = execute_resolved_action_with_backend(
            fake_hwnd,
            &plan,
            &cancel_flag,
            &mut backend_fg_loss,
        )
        .expect_err("type_text must abort if foreground is lost mid-string");
        assert_eq!(err_fg.code, ErrorCode::FocusFailed);
        assert_eq!(err_fg.mutation, MutationState::Possible);
        assert_eq!(backend_fg_loss.send_batch_calls, 1); // Only 1st code unit sent!
    }

    // ------------------------------------------------------------------------
    // P1 #2: Multi-step actions check cancellation & target between stages
    // (double_click between click 1 and click 2; scroll between move, vertical, horizontal;
    // UIA action when cancelled before write).
    // ------------------------------------------------------------------------
    {
        // DoubleClick cancelled during 35ms inter-click sleep -> click 2 is NEVER sent
        let dbl_cancel = Arc::new(AtomicBool::new(false));
        let dbl_plan = ResolvedActionPlan {
            summary: ryn_computer_use::protocol::ResolvedTargetSummary {
                window_hwnd: "65552".to_string(),
                screen_point: Some([200, 200]),
                secondary_screen_point: None,
                element_index: None,
                element_runtime_id: None,
            },
            op: ResolvedExecutionOp::MouseClick {
                screen_pt: [200, 200],
                element_binding: None,
                button: MouseButtonKind::Left,
                double: true,
            },
        };
        let mut backend = MockInjectableBackend::new(fake_hwnd);
        backend.cancel_on_sleep = Some(dbl_cancel.clone());
        let err =
            execute_resolved_action_with_backend(fake_hwnd, &dbl_plan, &dbl_cancel, &mut backend)
                .expect_err("DoubleClick must stop when cancelled between clicks");
        assert_eq!(err.code, ErrorCode::Cancelled);
        assert_eq!(err.mutation, MutationState::Possible);
        // Batch 1 = move, Batch 2 = first click (down+up). Batch 3 (second click) was NOT called!
        assert_eq!(backend.send_batch_calls, 2);
        assert_eq!(
            backend.recorded_ops,
            vec![
                RecordedInputOp::MouseMove,
                RecordedInputOp::MouseDown,
                RecordedInputOp::MouseUp,
            ]
        );

        // Scroll (delta_y != 0 and delta_x != 0) cancelled after vertical wheel -> horizontal wheel NOT sent
        let scroll_cancel = Arc::new(AtomicBool::new(false));
        let scroll_plan = ResolvedActionPlan {
            summary: ryn_computer_use::protocol::ResolvedTargetSummary {
                window_hwnd: "65552".to_string(),
                screen_point: Some([200, 200]),
                secondary_screen_point: None,
                element_index: None,
                element_runtime_id: None,
            },
            op: ResolvedExecutionOp::Scroll {
                screen_pt: [200, 200],
                delta_x: 120,
                delta_y: -120,
            },
        };
        let mut scroll_backend = MockInjectableBackend::new(fake_hwnd);
        scroll_backend.cancel_after_first_send_batch = Some(scroll_cancel.clone());
        let err_scroll = execute_resolved_action_with_backend(
            fake_hwnd,
            &scroll_plan,
            &scroll_cancel,
            &mut scroll_backend,
        )
        .expect_err("Scroll must stop when cancelled after move");
        assert_eq!(err_scroll.code, ErrorCode::Cancelled);
        // Only mouse move (batch 1) executed; neither vertical nor horizontal wheel executed
        assert_eq!(scroll_backend.send_batch_calls, 1);
        assert_eq!(scroll_backend.recorded_ops, vec![RecordedInputOp::MouseMove]);
    }

    // ------------------------------------------------------------------------
    // P1 #3: Short SendInput release-only compensation for held downs (0..2N-1)
    // and quarantine if release-only cleanup fails. Never replays business combo.
    // ------------------------------------------------------------------------
    {
        let vks = vec![
            parse_virtual_key("ctrl").unwrap(),
            parse_virtual_key("shift").unwrap(),
            parse_virtual_key("a").unwrap(),
        ];
        let chord_plan = ResolvedActionPlan {
            summary: ryn_computer_use::protocol::ResolvedTargetSummary {
                window_hwnd: "65552".to_string(),
                screen_point: None,
                secondary_screen_point: None,
                element_index: None,
                element_runtime_id: None,
            },
            op: ResolvedExecutionOp::KeyChord { vks: vks.clone() },
        };
        let fresh_cancel = AtomicBool::new(false);

        // Total events in 3-key chord = 6: [CtrlDown, ShiftDown, ADown, AUp, ShiftUp, CtrlUp]
        // Expected held keys requiring release-only KeyUp for each partial `sent` in 0..6:
        let expected_compensations: Vec<(usize, Vec<u16>)> = vec![
            (0, vec![]),                                     // 0 downs sent -> 0 release ups
            (1, vec![vks[0].0]),                             // CtrlDown sent -> release [CtrlUp]
            (2, vec![vks[1].0, vks[0].0]),                   // Ctrl,Shift down -> release [ShiftUp, CtrlUp]
            (3, vec![vks[2].0, vks[1].0, vks[0].0]),         // All 3 down -> release [AUp, ShiftUp, CtrlUp]
            (4, vec![vks[1].0, vks[0].0]),                   // AUp already sent -> release [ShiftUp, CtrlUp]
            (5, vec![vks[0].0]),                             // AUp,ShiftUp sent -> release [CtrlUp]
        ];

        for (partial_sent, expected_release_vks) in expected_compensations {
            let mut backend = MockInjectableBackend::new(fake_hwnd);
            backend.send_input_script.push_back(partial_sent);
            if !expected_release_vks.is_empty() {
                backend
                    .send_input_script
                    .push_back(expected_release_vks.len());
            }

            let err = execute_resolved_action_with_backend(
                fake_hwnd,
                &chord_plan,
                &fresh_cancel,
                &mut backend,
            )
            .expect_err("Short SendInput must return InputFailed");
            assert_eq!(err.code, ErrorCode::InputFailed);
            assert_eq!(err.mutation, MutationState::Possible);

            // Verify no extra key-downs were replayed during compensation, and every down has a matching up!
            let mut down_counts: HashMap<u16, usize> = HashMap::new();
            let mut up_counts: HashMap<u16, usize> = HashMap::new();
            for op in &backend.recorded_ops {
                match *op {
                    RecordedInputOp::KeyDown(vk, _, _) => {
                        *down_counts.entry(vk).or_insert(0) += 1;
                    }
                    RecordedInputOp::KeyUp(vk, _, _) => {
                        *up_counts.entry(vk).or_insert(0) += 1;
                    }
                    _ => {}
                }
            }
            assert_eq!(
                down_counts, up_counts,
                "Every sent KeyDown must be balanced by exactly one KeyUp for partial_sent={}",
                partial_sent
            );

            // Also verify the compensation batch contained ONLY KeyUp events in expected order
            let comp_ops = &backend.recorded_ops[partial_sent..];
            let actual_release_vks: Vec<u16> = comp_ops
                .iter()
                .map(|op| match *op {
                    RecordedInputOp::KeyUp(vk, _, false) => vk,
                    ref other => panic!("Compensation sent non-KeyUp event: {:?}", other),
                })
                .collect();
            assert_eq!(actual_release_vks, expected_release_vks);
        }

        // If release-only cleanup ALSO fails (returns 0), must return InputLockQuarantined
        let mut quarantine_backend = MockInjectableBackend::new(fake_hwnd);
        quarantine_backend.send_input_script.push_back(2); // 2 downs sent
        quarantine_backend.send_input_script.push_back(1); // Only 1 of 2 release ups accepted!
        let q_err = execute_resolved_action_with_backend(
            fake_hwnd,
            &chord_plan,
            &fresh_cancel,
            &mut quarantine_backend,
        )
        .expect_err("Failed release compensation must quarantine");
        assert_eq!(q_err.code, ErrorCode::InputLockQuarantined);
        assert_eq!(q_err.mutation, MutationState::Possible);
    }

    // ------------------------------------------------------------------------
    // P1 #4: Element click re-resolves live element by runtimeId and verifies bounds/state
    // at commit; refuses child movement within unchanged top-level window rect with 0 clicks.
    // ------------------------------------------------------------------------
    {
        let btn_bounds = RectPx::from_ltrb(150, 200, 270, 236);
        let btn_binding = SnapshotElementBinding {
            index: 1,
            runtime_id: "42.1001".to_string(),
            snapshot_bounds: btn_bounds,
            snapshot_password: false,
        };
        let click_el_plan = ResolvedActionPlan {
            summary: ryn_computer_use::protocol::ResolvedTargetSummary {
                window_hwnd: "65552".to_string(),
                screen_point: Some([210, 218]),
                secondary_screen_point: None,
                element_index: Some(1),
                element_runtime_id: Some("42.1001".to_string()),
            },
            op: ResolvedExecutionOp::MouseClick {
                screen_pt: [210, 218],
                element_binding: Some(btn_binding),
                button: MouseButtonKind::Left,
                double: false,
            },
        };
        let fresh_cancel = AtomicBool::new(false);

        // Child moved 40px right inside the same top-level window
        let mut moved_backend = MockInjectableBackend::new(fake_hwnd);
        moved_backend.live_bounds.insert(
            "42.1001".to_string(),
            RectPx::from_ltrb(190, 200, 310, 236),
        );
        let err = execute_resolved_action_with_backend(
            fake_hwnd,
            &click_el_plan,
            &fresh_cancel,
            &mut moved_backend,
        )
        .expect_err("Moved child element must be rejected as StateTokenStale");
        assert_eq!(err.code, ErrorCode::StateTokenStale);
        assert_eq!(
            err.details.as_ref().and_then(|d| d.reason),
            Some(DiagnosticReason::GeometryChanged)
        );
        assert_eq!(moved_backend.send_batch_calls, 0);
        assert!(moved_backend.recorded_ops.is_empty());
    }

    // ------------------------------------------------------------------------
    // P1 #5: Directional owned-window relationship graph evaluation:
    // Rejects sibling modals sharing a common owner, rejects cross-PID popups,
    // rejects unproven/zero process creation time, and allows true child or owned popup chain.
    // ------------------------------------------------------------------------
    {
        let mut graph: HashMap<u64, WindowRelationNode> = HashMap::new();
        // Main root window M (100) in PID 500, creation time 9000
        graph.insert(
            100,
            WindowRelationNode {
                hwnd: 100,
                root_hwnd: 100,
                parent_hwnd: 0,
                owner_hwnd: 0,
                pid: 500,
                process_started: Some(9000),
            },
        );
        // Child button C (101) inside M (100)
        graph.insert(
            101,
            WindowRelationNode {
                hwnd: 101,
                root_hwnd: 100,
                parent_hwnd: 100,
                owner_hwnd: 0,
                pid: 500,
                process_started: Some(9000),
            },
        );
        // Owned modal dialog A (200) owned by M (100)
        graph.insert(
            200,
            WindowRelationNode {
                hwnd: 200,
                root_hwnd: 200,
                parent_hwnd: 0,
                owner_hwnd: 100,
                pid: 500,
                process_started: Some(9000),
            },
        );
        // Sibling modal dialog B (300) ALSO owned by M (100)
        graph.insert(
            300,
            WindowRelationNode {
                hwnd: 300,
                root_hwnd: 300,
                parent_hwnd: 0,
                owner_hwnd: 100,
                pid: 500,
                process_started: Some(9000),
            },
        );
        // Sub-popup A2 (201) owned by dialog A (200)
        graph.insert(
            201,
            WindowRelationNode {
                hwnd: 201,
                root_hwnd: 201,
                parent_hwnd: 0,
                owner_hwnd: 200,
                pid: 500,
                process_started: Some(9000),
            },
        );
        // Cross-PID popup X (400) claiming owner M (100) but in PID 600
        graph.insert(
            400,
            WindowRelationNode {
                hwnd: 400,
                root_hwnd: 400,
                parent_hwnd: 0,
                owner_hwnd: 100,
                pid: 600,
                process_started: Some(9500),
            },
        );
        // Unproven creation time window Z (500) in PID 500
        graph.insert(
            500,
            WindowRelationNode {
                hwnd: 500,
                root_hwnd: 500,
                parent_hwnd: 0,
                owner_hwnd: 100,
                pid: 500,
                process_started: None,
            },
        );

        let lookup = |h: u64| graph.get(&h).copied();

        // 1. Exact target -> AllowedExactTarget
        assert_eq!(
            evaluate_directional_window_relation(100, 100, lookup),
            WindowRelationDecision::AllowedExactTarget
        );
        // 2. True child of target -> AllowedTargetChildDescendant
        assert_eq!(
            evaluate_directional_window_relation(101, 100, lookup),
            WindowRelationDecision::AllowedTargetChildDescendant
        );
        // 3. Owned modal A when target is root M -> AllowedOwnedPopupChain
        assert_eq!(
            evaluate_directional_window_relation(200, 100, lookup),
            WindowRelationDecision::AllowedOwnedPopupChain
        );
        // 4. Nested popup A2 owned by modal A when target is modal A -> AllowedOwnedPopupChain
        assert_eq!(
            evaluate_directional_window_relation(201, 200, lookup),
            WindowRelationDecision::AllowedOwnedPopupChain
        );
        // 5. CRITICAL: Sibling modal B (300) when target is modal A (200) -> MUST BE REJECTED!
        assert_eq!(
            evaluate_directional_window_relation(300, 200, lookup),
            WindowRelationDecision::RejectedSiblingOrUnownedPopup
        );
        assert!(!evaluate_directional_window_relation(300, 200, lookup).is_allowed());
        // 6. Owner M (100) when target is modal A (200) -> rejected (owner is not owned by modal A)
        assert_eq!(
            evaluate_directional_window_relation(100, 200, lookup),
            WindowRelationDecision::RejectedSiblingOrUnownedPopup
        );
        // 7. Cross-PID popup X (400) -> RejectedCrossProcess
        assert_eq!(
            evaluate_directional_window_relation(400, 100, lookup),
            WindowRelationDecision::RejectedCrossProcess
        );
        // 8. Unproven creation time Z (500) -> RejectedUnprovenProcessIdentity
        assert_eq!(
            evaluate_directional_window_relation(500, 100, lookup),
            WindowRelationDecision::RejectedUnprovenProcessIdentity
        );
    }

    // ------------------------------------------------------------------------
    // P1 #6 & #7: Monotonic mutation merging, password fail-closed when CurrentIsPassword
    // fails (`None`), and zero creation time WindowRef rejection.
    // ------------------------------------------------------------------------
    {
        // Password fail-closed when CurrentIsPassword fails (None)
        assert!(evaluate_password_protection(None, false, "NormalLabel"));
        assert!(evaluate_password_protection(Some(true), false, "NormalLabel"));
        assert!(evaluate_password_protection(
            Some(false),
            true,
            "User Password"
        ));
        assert!(evaluate_password_protection(Some(false), true, "登录密码"));
        assert!(!evaluate_password_protection(Some(false), true, "Username"));

        // Password element rejected for TypeText / UiaInvoke / UiaSetValue in resolve_action_plan
        let pwd_el = UiaElement {
            index: 3,
            runtime_id: "42.999".to_string(),
            automation_id: None,
            name: "Secret".to_string(),
            control_type: "edit".to_string(),
            class_name: None,
            value: None,
            password: true,
            enabled: true,
            offscreen: false,
            focusable: true,
            focused: false,
            bounds: RectPx::from_ltrb(10, 10, 100, 40),
            depth: 1,
            patterns: vec!["value".to_string()],
        };
        let geom = WindowGeometry {
            window_rect: RectPx::from_ltrb(0, 0, 500, 500),
            client_rect: RectPx::from_ltrb(0, 0, 500, 500),
            screen_origin: [0, 0],
            dpi: 96,
            scale_factor: 1.0,
        };
        let mut reg = WindowLifecycleRegistry::new(
            HostWorkerNonce::new(0xaaaa_bbbb_cccc_dddd_eeee_ffff_0000_1111).unwrap(),
            1,
            16,
        )
        .unwrap();
        let snap_stamp = reg
            .issue_or_reuse(WindowOsTuple::new(65552, 100, 12345).unwrap())
            .unwrap()
            .stamp;
        let snap = SnapshotRecord {
            state_token: "st_1".to_string(),
            observation_id: "obs_1".to_string(),
            task_session_id: "t1".to_string(),
            window: WindowRef {
                worker_instance: snap_stamp.host_nonce.worker_instance_label(),
                window_key: snap_stamp.window_key.clone(),
                hwnd: "65552".to_string(),
                pid: 100,
                process_started: "12345".to_string(),
                generation: snap_stamp.issue_generation,
                app_id: None,
            },
            lifecycle_stamp: snap_stamp,
            geometry: geom.clone(),
            elements: vec![pwd_el],
            tree_hash: "h".to_string(),
            window_mutation_epoch: 0,
        };
        let pwd_type_err = resolve_action_plan(
            fake_hwnd,
            &snap,
            &geom,
            &ActionSpec::TypeText {
                text: "abc".to_string(),
                target: None,
                element: Some(ElementTarget {
                    index: 3,
                    runtime_id: Some("42.999".to_string()),
                }),
            },
        )
        .expect_err("TypeText on password element must be rejected");
        assert_eq!(pwd_type_err.code, ErrorCode::Unsupported);
        assert_eq!(
            pwd_type_err.details.as_ref().and_then(|d| d.reason),
            Some(DiagnosticReason::PasswordRedacted)
        );

        // Monotonic mutation merge: None error upgraded to FocusOnly -> wire mutation Possible
        let raw_err = ryn_computer_use::errors::ProtocolError::observe(
            ErrorCode::WindowNotFound,
            DiagnosticReason::IsWindowFalse,
        );
        assert_eq!(raw_err.mutation, MutationState::None);
        let merged_err = raw_err.with_monotonic_mutation(InternalMutationDetail::FocusOnly);
        assert_eq!(merged_err.mutation, MutationState::Possible);
        assert_eq!(
            merged_err.details.as_ref().and_then(|d| d.internal_mutation),
            Some(InternalMutationDetail::FocusOnly)
        );

        // Zero processStarted in WindowRef is rejected fail-closed by WindowIdentityTracker
        let tracker = WindowIdentityTracker::new_inert_for_test(
            HostWorkerNonce::new(0x9999_8888_7777_6666_5555_4444_3333_2222).unwrap(),
        );
        let zero_ref = WindowRef {
            worker_instance: tracker.worker_instance().to_string(),
            window_key: "win_1".to_string(),
            hwnd: "65552".to_string(),
            pid: 100,
            process_started: "0".to_string(),
            generation: 1,
            app_id: None,
        };
        let zero_err = tracker
            .validate_window_ref(&zero_ref)
            .expect_err("Zero processStarted must be rejected fail-closed");
        assert_eq!(zero_err.code, ErrorCode::WindowStale);
        assert_eq!(
            zero_err.details.as_ref().and_then(|d| d.reason),
            Some(DiagnosticReason::ProcessCreationTimeMismatch)
        );
    }
}
