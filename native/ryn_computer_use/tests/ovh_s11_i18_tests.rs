use ryn_computer_use::capture::{
    try_capture_window_screenshot_with_probe, ScreenshotCaptureProbe,
};
use ryn_computer_use::engine::{OperationControlTable, PhysicalActorEngine};
use ryn_computer_use::errors::{
    DiagnosticReason, ErrorCode, ErrorStage, ErrorWindowSummary, InternalMutationDetail,
    MutationState, ProtocolError,
};
use ryn_computer_use::focus::{ensure_window_focused_with_backend, FocusExecutionBackend};
use ryn_computer_use::input::{
    execute_resolved_action_with_backend, resolve_action_plan, InputExecutionBackend,
    MouseButtonKind, ResolvedActionPlan, ResolvedExecutionOp, SnapshotElementBinding,
};
use ryn_computer_use::input_lock::verify_active_desktop_matches_lock;
use ryn_computer_use::lifecycle::{InjectedLifecycleSource, LifecycleService};
use ryn_computer_use::lifecycle_model::{
    HostWorkerNonce, WindowLifecycleRegistry, WindowOsTuple,
};
use ryn_computer_use::protocol::{
    parse_and_validate_request_line, ActionSpec, ElementTarget, RectPx,
    ScreenshotOptions, ScreenshotUnavailableReason, UiaElement, UiaObservationReason,
    WindowEntry, WindowGeometry, WindowRef,
};
use ryn_computer_use::snapshot::SnapshotRecord;
use ryn_computer_use::uia::{
    classify_uia_com_hresult, observe_window_accessibility_from_stage_error,
    verify_live_element_state_matches_snapshot,
};
use ryn_computer_use::windows::{
    compare_integrity_rids_is_elevated, format_unix_millis_rfc3339, now_iso8601_utc, u64_to_hwnd,
};
use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use std::time::{Duration, Instant};
use windows::Win32::Foundation::HWND;
use windows::Win32::UI::Input::KeyboardAndMouse::INPUT;

fn sample_geometry(w: i32, h: i32) -> WindowGeometry {
    WindowGeometry {
        window_rect: RectPx::from_ltrb(100, 100, 100 + w, 100 + h),
        client_rect: RectPx::from_ltrb(100, 100, 100 + w, 100 + h),
        screen_origin: [100, 100],
        dpi: 96,
        scale_factor: 1.0,
    }
}

#[test]
fn t1_screenshot_failure_paths_carry_reason_and_capture_protected_error() {
    let geom = sample_geometry(200, 120);
    let default_opts = ScreenshotOptions {
        enabled: Some(true),
        mode: None,
        format: None,
    };

    // 1. disabled
    let disabled_art = try_capture_window_screenshot_with_probe(
        &geom,
        &ScreenshotOptions {
            enabled: Some(false),
            mode: None,
            format: None,
        },
        "art_disabled".to_string(),
        ScreenshotCaptureProbe::healthy_non_blank(200, 120),
    )
    .expect("disabled screenshot returns unavailable artifact");
    assert_eq!(disabled_art.status, "unavailable");
    assert_eq!(disabled_art.reason, Some(ScreenshotUnavailableReason::Disabled));

    // 2. not_visible
    let mut probe_not_vis = ScreenshotCaptureProbe::healthy_non_blank(200, 120);
    probe_not_vis.visible = false;
    let not_vis_art = try_capture_window_screenshot_with_probe(
        &geom,
        &default_opts,
        "art_not_vis".to_string(),
        probe_not_vis,
    )
    .unwrap();
    assert_eq!(not_vis_art.status, "unavailable");
    assert_eq!(not_vis_art.reason, Some(ScreenshotUnavailableReason::NotVisible));

    // 3. cloaked
    let mut probe_cloaked = ScreenshotCaptureProbe::healthy_non_blank(200, 120);
    probe_cloaked.cloaked = true;
    let cloaked_art = try_capture_window_screenshot_with_probe(
        &geom,
        &default_opts,
        "art_cloaked".to_string(),
        probe_cloaked,
    )
    .unwrap();
    assert_eq!(cloaked_art.status, "unavailable");
    assert_eq!(cloaked_art.reason, Some(ScreenshotUnavailableReason::Cloaked));

    // 4. too_large (> 8192 px)
    let huge_geom = sample_geometry(9000, 120);
    let too_large_art = try_capture_window_screenshot_with_probe(
        &huge_geom,
        &default_opts,
        "art_too_large".to_string(),
        ScreenshotCaptureProbe::healthy_non_blank(16, 16),
    )
    .unwrap();
    assert_eq!(too_large_art.status, "unavailable");
    assert_eq!(too_large_art.reason, Some(ScreenshotUnavailableReason::TooLarge));

    // 5. print_failed
    let mut probe_print_fail = ScreenshotCaptureProbe::healthy_non_blank(200, 120);
    probe_print_fail.print_result = Err(());
    let print_fail_art = try_capture_window_screenshot_with_probe(
        &geom,
        &default_opts,
        "art_print_fail".to_string(),
        probe_print_fail,
    )
    .unwrap();
    assert_eq!(print_fail_art.status, "unavailable");
    assert_eq!(print_fail_art.reason, Some(ScreenshotUnavailableReason::PrintFailed));

    // 6. blank (without display-affinity protection)
    let mut probe_blank = ScreenshotCaptureProbe::healthy_non_blank(16, 16);
    probe_blank.print_result = Ok((vec![0u8; 16 * 16 * 4], "print_window_full_content"));
    probe_blank.display_affinity_protected = false;
    let small_geom = sample_geometry(16, 16);
    let blank_art = try_capture_window_screenshot_with_probe(
        &small_geom,
        &default_opts,
        "art_blank".to_string(),
        probe_blank,
    )
    .unwrap();
    assert_eq!(blank_art.status, "unavailable");
    assert_eq!(blank_art.reason, Some(ScreenshotUnavailableReason::Blank));

    // 7. png_too_large
    let mut probe_png_big = ScreenshotCaptureProbe::healthy_non_blank(16, 16);
    probe_png_big.max_png_bytes = 8;
    let png_big_art = try_capture_window_screenshot_with_probe(
        &small_geom,
        &default_opts,
        "art_png_big".to_string(),
        probe_png_big,
    )
    .unwrap();
    assert_eq!(png_big_art.status, "unavailable");
    assert_eq!(png_big_art.reason, Some(ScreenshotUnavailableReason::PngTooLarge));

    // 8. Change 15: blank + display-affinity protected produces ErrorCode::WindowCaptureProtected
    let mut probe_protected = ScreenshotCaptureProbe::healthy_non_blank(16, 16);
    probe_protected.print_result = Ok((vec![0u8; 16 * 16 * 4], "print_window_full_content"));
    probe_protected.display_affinity_protected = true;
    let prot_err = try_capture_window_screenshot_with_probe(
        &small_geom,
        &default_opts,
        "art_protected".to_string(),
        probe_protected,
    )
    .expect_err("blank capture with display affinity protection must emit WindowCaptureProtected");
    assert_eq!(prot_err.code, ErrorCode::WindowCaptureProtected);
}

#[test]
fn t2_rfc3339_utc_timestamp_formatting() {
    // 2026-11-14T10:00:00.123Z -> 1794650400_123 ms since Unix epoch
    assert_eq!(
        format_unix_millis_rfc3339(1_794_650_400_123),
        "2026-11-14T10:00:00.123Z"
    );
    assert_eq!(
        format_unix_millis_rfc3339(0),
        "1970-01-01T00:00:00.000Z"
    );
    // Leap day 2024-02-29T23:59:59.999Z -> 1709251199_999
    assert_eq!(
        format_unix_millis_rfc3339(1_709_251_199_999),
        "2024-02-29T23:59:59.999Z"
    );
    let live = now_iso8601_utc();
    assert_eq!(live.len(), 24, "unexpected timestamp format: {}", live);
    assert!(live.contains('T') && live.ends_with('Z'));
}

#[test]
fn t8_window_entry_elevation_zorder_and_monitor_fields() {
    // Medium (0x2000) vs High/Admin (0x3000) and System (0x4000)
    assert!(compare_integrity_rids_is_elevated(Some(0x3000), Some(0x2000)));
    assert!(compare_integrity_rids_is_elevated(Some(0x4000), Some(0x2000)));
    assert!(!compare_integrity_rids_is_elevated(Some(0x2000), Some(0x2000)));
    assert!(!compare_integrity_rids_is_elevated(Some(0x1000), Some(0x2000)));

    let entry = WindowEntry {
        window: WindowRef {
            worker_instance: "wrk_1".to_string(),
            window_key: "win_1".to_string(),
            hwnd: "1001".to_string(),
            pid: 42,
            process_started: "9999".to_string(),
            generation: 1,
            app_id: Some("notepad.exe".to_string()),
        },
        title: "管理员: 记事本".to_string(),
        class_name: "Notepad".to_string(),
        process_name: "notepad.exe".to_string(),
        executable_path: Some("C:\\Windows\\System32\\notepad.exe".to_string()),
        visible: true,
        enabled: true,
        minimized: false,
        maximized: false,
        foreground: true,
        cloaked: false,
        capture_protected: false,
        bounds: RectPx::from_ltrb(0, 0, 800, 600),
        client_bounds: RectPx::from_ltrb(0, 0, 800, 600),
        dpi: 96,
        z_order: 0,
        monitor: "\\\\.\\DISPLAY1".to_string(),
        is_elevated: true,
    };
    let json_val = serde_json::to_value(&entry).unwrap();
    assert_eq!(json_val.get("zOrder").and_then(|v| v.as_u64()), Some(0));
    assert_eq!(
        json_val.get("monitor").and_then(|v| v.as_str()),
        Some("\\\\.\\DISPLAY1")
    );
    assert_eq!(json_val.get("isElevated").and_then(|v| v.as_bool()), Some(true));
}

#[test]
fn t10_prepare_rejects_locked_or_secure_desktop_with_desktop_locked() {
    // Pure verification helper
    let mismatch_err = verify_active_desktop_matches_lock("Default", Ok("Winlogon"))
        .expect_err("Winlogon secure desktop must fail prepare");
    assert_eq!(mismatch_err.code, ErrorCode::DesktopLocked);
    assert_eq!(mismatch_err.stage, ErrorStage::Prepare);
    assert_eq!(mismatch_err.mutation, MutationState::None);
    assert_eq!(
        mismatch_err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::DesktopLocked)
    );

    let denied_err = verify_active_desktop_matches_lock("Default", Err(5))
        .expect_err("ERROR_ACCESS_DENIED on OpenInputDesktop must report DesktopLocked");
    assert_eq!(denied_err.code, ErrorCode::DesktopLocked);

    // Engine integration check via handle_request("action.prepare")
    let nonce = HostWorkerNonce::new(0x5566_7788_99aa_bbcc_ddee_ff00_1122_3344).unwrap();
    let inj = InjectedLifecycleSource::new_healthy(1, 32);
    let svc = LifecycleService::new_with_injected_source_for_test(nonce, 1, 16, inj).unwrap();
    let mut engine = PhysicalActorEngine::new_with_lifecycle_service_for_test(
        OperationControlTable::new(),
        svc,
    )
    .unwrap();
    engine.set_active_desktop_probe_for_test(Arc::new(|| Ok("Winlogon".to_string())));

    let init_req = parse_and_validate_request_line(
        r#"{"v":1,"id":"init-lock","method":"initialize","params":{"protocolVersion":"1.0.0","client":{"name":"test","version":"1.0"}}}"#,
    )
    .unwrap();
    assert!(engine.handle_request(init_req, &mut |_| {}).error.is_none());

    let prep_req = parse_and_validate_request_line(
        r#"{"v":1,"id":"prep-locked","method":"action.prepare","params":{"taskSessionId":"t-lock","operationId":"op-lock","window":{"workerInstance":"wrk_1","windowKey":"win_1","hwnd":"123","pid":1,"processStarted":"1","generation":1},"expectedStateToken":"st_1","action":{"type":"focus_window"}}}"#,
    )
    .unwrap();
    let resp = engine.handle_request(prep_req, &mut |_| {});
    let err = resp.error.expect("action.prepare must fail on locked desktop");
    assert_eq!(err.code, ErrorCode::DesktopLocked);
    assert_eq!(err.stage, ErrorStage::Prepare);
    assert_eq!(err.mutation, MutationState::None);
}

#[test]
fn t11_protocol_error_display_uses_snake_case_stage_and_mutation() {
    let err = ProtocolError::new(
        ErrorCode::InputLockBusy,
        ErrorStage::WaitingLock,
        InternalMutationDetail::FocusOnly,
    );
    let rendered = format!("{}", err);
    assert!(
        rendered.contains("stage=waiting_lock"),
        "expected snake_case stage=waiting_lock, got: {}",
        rendered
    );
    assert!(
        rendered.contains("mutation=possible"),
        "expected snake_case mutation=possible, got: {}",
        rendered
    );
    assert!(!rendered.contains("WaitingLock"));
    assert!(!rendered.contains("Possible"));
}

#[test]
fn t12_disabled_and_offscreen_elements_and_uia_diagnostic_splits() {
    let bounds = RectPx::from_ltrb(10, 20, 110, 60);

    // 1. Live UIA disabled -> ElementDisabled (retryable == false)
    let dis_err = verify_live_element_state_matches_snapshot(
        "42.1",
        &bounds,
        false,
        "42.1",
        &bounds,
        Some(false),
        Some(false),
        Some(false),
    )
    .expect_err("disabled element must fail");
    assert_eq!(dis_err.code, ErrorCode::ElementDisabled);
    assert!(!dis_err.retryable);
    assert_eq!(
        dis_err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::ElementDisabled)
    );

    // 2. Live UIA offscreen -> ElementOffscreen
    let off_err = verify_live_element_state_matches_snapshot(
        "42.1",
        &bounds,
        false,
        "42.1",
        &bounds,
        Some(true),
        Some(true),
        Some(false),
    )
    .expect_err("offscreen element must fail");
    assert_eq!(off_err.code, ErrorCode::ElementOffscreen);
    assert_eq!(
        off_err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::ElementOffscreen)
    );

    // 3. Snapshot resolution disabled / offscreen / missing element
    let geom = sample_geometry(400, 300);
    let mut reg = WindowLifecycleRegistry::new(
        HostWorkerNonce::new(0x1111_2222_3333_4444_5555_6666_7777_9999).unwrap(),
        1,
        16,
    )
    .unwrap();
    let stamp = reg
        .issue_or_reuse(WindowOsTuple::new(5001, 200, 99999).unwrap())
        .unwrap()
        .stamp;
    let disabled_btn = UiaElement {
        index: 1,
        runtime_id: "7.1".to_string(),
        automation_id: None,
        name: "Submit".to_string(),
        control_type: "button".to_string(),
        class_name: None,
        value: None,
        password: false,
        enabled: false,
        offscreen: false,
        focusable: true,
        focused: false,
        bounds: RectPx::from_ltrb(120, 120, 200, 150),
        depth: 1,
        patterns: vec!["invoke".to_string()],
    };
    let offscreen_item = UiaElement {
        index: 2,
        runtime_id: "7.2".to_string(),
        enabled: true,
        offscreen: true,
        ..disabled_btn.clone()
    };
    let snap = SnapshotRecord {
        state_token: "st_elem".to_string(),
        observation_id: "obs_elem".to_string(),
        task_session_id: "t1".to_string(),
        window: WindowRef {
            worker_instance: stamp.host_nonce.worker_instance_label(),
            window_key: stamp.window_key.clone(),
            hwnd: "5001".to_string(),
            pid: 200,
            process_started: "99999".to_string(),
            generation: stamp.issue_generation,
            app_id: None,
        },
        lifecycle_stamp: stamp,
        geometry: geom.clone(),
        elements: vec![disabled_btn, offscreen_item],
        tree_hash: "h".to_string(),
        window_mutation_epoch: 0,
    };

    let snap_dis_err = resolve_action_plan(
        u64_to_hwnd(5001),
        &snap,
        &geom,
        &ActionSpec::UiaInvoke {
            element: ElementTarget {
                index: 1,
                runtime_id: Some("7.1".to_string()),
            },
        },
    )
    .unwrap_err();
    assert_eq!(snap_dis_err.code, ErrorCode::ElementDisabled);
    assert!(!snap_dis_err.retryable);

    let snap_off_err = resolve_action_plan(
        u64_to_hwnd(5001),
        &snap,
        &geom,
        &ActionSpec::UiaInvoke {
            element: ElementTarget {
                index: 2,
                runtime_id: Some("7.2".to_string()),
            },
        },
    )
    .unwrap_err();
    assert_eq!(snap_off_err.code, ErrorCode::ElementOffscreen);

    let snap_missing_err = resolve_action_plan(
        u64_to_hwnd(5001),
        &snap,
        &geom,
        &ActionSpec::UiaInvoke {
            element: ElementTarget {
                index: 99,
                runtime_id: None,
            },
        },
    )
    .unwrap_err();
    assert_eq!(snap_missing_err.code, ErrorCode::UiaElementStale);
    assert_eq!(
        snap_missing_err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::UiaElementNotFound)
    );

    // 4. UIA COM HRESULT classification & AccessibilityObservation failure reason + HRESULT
    assert_eq!(
        classify_uia_com_hresult(0x80040201u32 as i32),
        DiagnosticReason::UiaAppNotResponding
    );
    assert_eq!(
        classify_uia_com_hresult(0x800705B4u32 as i32),
        DiagnosticReason::UiaAppNotResponding
    );
    assert_eq!(
        classify_uia_com_hresult(0x80040200u32 as i32),
        DiagnosticReason::UiaElementNotFound
    );
    assert_eq!(
        classify_uia_com_hresult(0x80004005u32 as i32),
        DiagnosticReason::UiaComCallFailed
    );

    let acc_obs = observe_window_accessibility_from_stage_error(
        UiaObservationReason::RootFailed,
        0x80040201u32 as i32,
    );
    assert_eq!(acc_obs.status, "unavailable");
    assert_eq!(acc_obs.reason, Some(UiaObservationReason::RootFailed));
    assert_eq!(acc_obs.hresult_hex.as_deref(), Some("0x80040201"));
}

struct UipiAndCancelMockBackend {
    win32_err: Option<u32>,
    target_elevated: bool,
    fg_ok: bool,
    fg_summary: Option<ErrorWindowSummary>,
}

impl InputExecutionBackend for UipiAndCancelMockBackend {
    fn check_foreground(&mut self, target_hwnd: HWND) -> (bool, HWND) {
        if self.fg_ok {
            (true, target_hwnd)
        } else {
            (false, u64_to_hwnd(0xDEAD))
        }
    }

    fn check_hit_test(
        &mut self,
        _target_hwnd: HWND,
        _screen_pt: [i32; 2],
        _stage: ErrorStage,
        _internal_mutation: InternalMutationDetail,
    ) -> Result<(), ProtocolError> {
        Ok(())
    }

    fn verify_live_element(
        &mut self,
        _target_hwnd: HWND,
        binding: &SnapshotElementBinding,
        _allow_password_target: bool,
        _cancel_flag: &AtomicBool,
    ) -> Result<RectPx, ProtocolError> {
        Ok(binding.snapshot_bounds)
    }

    fn send_input_batch(&mut self, _inputs: &[INPUT]) -> usize {
        0
    }

    fn last_send_input_error(&mut self) -> Option<u32> {
        self.win32_err
    }

    fn is_target_elevated_above_self(&mut self, _target_hwnd: HWND) -> bool {
        self.target_elevated
    }

    fn describe_window(&mut self, _hwnd: HWND) -> Option<ErrorWindowSummary> {
        self.fg_summary.clone()
    }

    fn uia_focus(
        &mut self,
        _target_hwnd: HWND,
        _binding: &SnapshotElementBinding,
        _allow_password_target: bool,
        _cancel_flag: &AtomicBool,
    ) -> Result<(), ProtocolError> {
        Ok(())
    }

    fn uia_invoke(
        &mut self,
        _target_hwnd: HWND,
        _binding: &SnapshotElementBinding,
        _cancel_flag: &AtomicBool,
    ) -> Result<(), ProtocolError> {
        Ok(())
    }

    fn uia_set_value(
        &mut self,
        _target_hwnd: HWND,
        _binding: &SnapshotElementBinding,
        _value: &str,
        _cancel_flag: &AtomicBool,
    ) -> Result<(), ProtocolError> {
        Ok(())
    }

    fn uia_select(
        &mut self,
        _target_hwnd: HWND,
        _binding: &SnapshotElementBinding,
        _cancel_flag: &AtomicBool,
    ) -> Result<(), ProtocolError> {
        Ok(())
    }

    fn sleep_ms(&mut self, _ms: u64) {}
}

struct StubFocusFailBackend {
    now_instant: Instant,
    fg_summary: ErrorWindowSummary,
}

impl FocusExecutionBackend for StubFocusFailBackend {
    fn now(&mut self) -> Instant {
        self.now_instant
    }
    fn sleep(&mut self, duration: Duration) {
        self.now_instant += duration;
    }
    fn is_window(&mut self, _hwnd: HWND) -> bool {
        true
    }
    fn get_root_window(&mut self, hwnd: HWND) -> HWND {
        hwnd
    }
    fn is_minimized(&mut self, _hwnd: HWND) -> bool {
        false
    }
    fn verify_target_identity(&mut self, _root_hwnd: HWND, _target_hwnd: HWND) -> bool {
        true
    }
    fn verify_foreground_relation(&mut self, _target_hwnd: HWND) -> (bool, HWND) {
        (false, u64_to_hwnd(0xBEEF))
    }
    fn current_thread_id(&mut self) -> u32 {
        10
    }
    fn window_pid_tid(&mut self, _hwnd: HWND) -> (u32, u32) {
        (20, 21)
    }
    fn show_window_restore(&mut self, _root_hwnd: HWND) -> bool {
        false
    }
    fn bring_window_to_top(&mut self, _root_hwnd: HWND) -> bool {
        false
    }
    fn set_foreground_window(&mut self, _root_hwnd: HWND) -> bool {
        false
    }
    fn set_focus(&mut self, _root_hwnd: HWND) -> bool {
        false
    }
    fn switch_to_this_window(&mut self, _root_hwnd: HWND) {}
    fn attach_thread_input(&mut self, _from_tid: u32, _to_tid: u32, _attach: bool) -> bool {
        false
    }
    fn describe_window(&mut self, _hwnd: HWND) -> Option<ErrorWindowSummary> {
        Some(self.fg_summary.clone())
    }
}

#[test]
fn t13_cancelled_after_focus_reports_cancelled_while_ready_and_uipi_and_window_summaries() {
    let hwnd = u64_to_hwnd(0x2222);
    let click_plan = ResolvedActionPlan {
        summary: ryn_computer_use::protocol::ResolvedTargetSummary {
            window_hwnd: "8738".to_string(),
            screen_point: Some([150, 150]),
            secondary_screen_point: None,
            element_index: None,
            element_runtime_id: None,
        },
        op: ResolvedExecutionOp::MouseClick {
            screen_pt: [150, 150],
            element_binding: None,
            button: MouseButtonKind::Left,
            double: false,
        },
    };

    // 1. T13: Cancelled at Validating stage when only focused -> CancelledWhileReady
    let pre_cancelled = AtomicBool::new(true);
    let mut backend = UipiAndCancelMockBackend {
        win32_err: None,
        target_elevated: false,
        fg_ok: true,
        fg_summary: None,
    };
    let cancel_err =
        execute_resolved_action_with_backend(hwnd, &click_plan, &pre_cancelled, &mut backend)
            .expect_err("cancelled before SendInput must abort at Validating");
    assert_eq!(cancel_err.code, ErrorCode::Cancelled);
    assert_eq!(cancel_err.stage, ErrorStage::Validating);
    assert_eq!(
        cancel_err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::CancelledWhileReady)
    );

    // 2. Change 1: SendInput sent == 0 with ERROR_ACCESS_DENIED (5) -> InputBlockedByUipi
    let active_cancel = AtomicBool::new(false);
    let mut uipi_err_backend = UipiAndCancelMockBackend {
        win32_err: Some(5),
        target_elevated: false,
        fg_ok: true,
        fg_summary: None,
    };
    let uipi_err =
        execute_resolved_action_with_backend(hwnd, &click_plan, &active_cancel, &mut uipi_err_backend)
            .expect_err("SendInput sent==0 + ERROR_ACCESS_DENIED must return InputBlockedByUipi");
    assert_eq!(uipi_err.code, ErrorCode::InputBlockedByUipi);
    assert!(!uipi_err.retryable);

    // 3. Change 1: SendInput sent == 0 when target process integrity > current process -> InputBlockedByUipi
    let mut uipi_elev_backend = UipiAndCancelMockBackend {
        win32_err: Some(0),
        target_elevated: true,
        fg_ok: true,
        fg_summary: None,
    };
    let uipi_elev_err = execute_resolved_action_with_backend(
        hwnd,
        &click_plan,
        &active_cancel,
        &mut uipi_elev_backend,
    )
    .expect_err("SendInput sent==0 on elevated target must return InputBlockedByUipi");
    assert_eq!(uipi_elev_err.code, ErrorCode::InputBlockedByUipi);
    assert!(!uipi_elev_err.retryable);

    // 4. Change 2: ErrorWindowSummary truncates title to 80 chars, strips path to exe basename, never enters message
    let long_title = format!("PROMPT_INJECTION_{}", "中".repeat(100));
    let sanitized = ErrorWindowSummary::sanitized(
        &long_title,
        "C:\\Program Files\\SecretDir\\BlockingApp.exe",
    );
    assert_eq!(sanitized.title.chars().count(), 80);
    assert_eq!(sanitized.exe, "BlockingApp.exe");

    let mut focus_backend = StubFocusFailBackend {
        now_instant: Instant::now(),
        fg_summary: sanitized.clone(),
    };
    let focus_err = ensure_window_focused_with_backend(
        &mut focus_backend,
        hwnd,
        false,
        120,
        &active_cancel,
    )
    .expect_err("focus must fail when foreground never matches");
    assert_eq!(focus_err.code, ErrorCode::FocusFailed);
    assert!(!focus_err.message.contains("PROMPT_INJECTION_"));
    assert!(!focus_err.message.contains("SecretDir"));
    assert_eq!(
        focus_err
            .details
            .as_ref()
            .and_then(|d| d.foreground_window.as_ref()),
        Some(&sanitized)
    );
}
