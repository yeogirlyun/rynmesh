use ryn_computer_use::engine::{OperationControlTable, PhysicalActorEngine};
use ryn_computer_use::errors::{DiagnosticReason, ErrorCode, ErrorStage, MutationState};
use ryn_computer_use::input::{resolve_action_plan, ResolvedActionPlan, ResolvedExecutionOp};
use ryn_computer_use::lifecycle::{
    map_lifecycle_error_to_protocol, InjectedLifecycleSource, LifecycleService,
};
use ryn_computer_use::lifecycle_model::{
    CoverageFaultReason, CoverageState, HostWorkerNonce, LifecycleError, LifecycleEventKind,
    RetirementReason, WindowOsTuple,
};
use ryn_computer_use::protocol::{
    parse_and_validate_request_line, ActionSpec, RectPx, ResolvedTargetSummary, WindowGeometry,
    WindowRef,
};
use ryn_computer_use::windows::{u64_to_hwnd, WindowIdentityTracker};
use std::sync::atomic::AtomicBool;


fn sample_geometry() -> WindowGeometry {
    WindowGeometry {
        window_rect: RectPx::from_ltrb(100, 100, 600, 500),
        client_rect: RectPx::from_ltrb(108, 132, 592, 492),
        screen_origin: [100, 100],
        dpi: 96,
        scale_factor: 1.0,
    }
}

#[test]
fn unhealthy_hook_is_an_error_not_an_empty_desktop() {
    use ryn_computer_use::windows::{handle_apps_list, handle_windows_list};
    for fault in [CoverageFaultReason::HeartbeatExpired, CoverageFaultReason::QueueOverflow] {
        let nonce = HostWorkerNonce::new(12345).unwrap();
        let inj = InjectedLifecycleSource::new_healthy(1, 32);
        let svc = LifecycleService::new_with_injected_source_for_test(nonce, 1, 16, inj.clone()).unwrap();
        inj.inject_fault(fault);
        let tracker = WindowIdentityTracker::from_service(svc);
        let error = handle_windows_list(&tracker, serde_json::from_str("{}").unwrap()).unwrap_err();
        assert_eq!(error.code, ErrorCode::ObservationUnavailable);
        assert_eq!(error.stage, ErrorStage::Observe);
        assert_eq!(error.details.unwrap().reason, Some(DiagnosticReason::GenerationMismatch));
        let error = handle_apps_list(&tracker, serde_json::from_str("{}").unwrap()).unwrap_err();
        assert_eq!(error.code, ErrorCode::ObservationUnavailable);
    }
}

#[test]
fn test_adapter_registration_and_health_failures_fail_closed() {
    let nonce = HostWorkerNonce::new(0x0102_0304_0506_0708_090a_0b0c_0d0e_0f10).unwrap();
    let inert_tracker = WindowIdentityTracker::new_inert_for_test(nonce);
    assert_eq!(
        inert_tracker.worker_instance(),
        "wrk_0102030405060708090a0b0c0d0e0f10"
    );
    assert_eq!(
        inert_tracker.service().coverage_state(),
        CoverageState::Unhealthy(CoverageFaultReason::HookRegistrationFailed)
    );

    let live_tuple = WindowOsTuple::new(0x2001, 4242, 133_000_000_000_000_000).unwrap();
    let issue_err = inert_tracker
        .build_window_ref_for_tuple(live_tuple, Some("fixture".to_string()), ErrorStage::Observe)
        .expect_err("inert tracker must never issue live WindowRef");
    assert_eq!(issue_err.code, ErrorCode::ObservationUnavailable);
    assert_eq!(
        issue_err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::GenerationMismatch)
    );

    let mut inert_engine =
        PhysicalActorEngine::new_inert_for_protocol_test(OperationControlTable::new()).unwrap();
    assert_eq!(
        inert_engine.lifecycle_service().coverage_state(),
        CoverageState::Unhealthy(CoverageFaultReason::HookRegistrationFailed)
    );
    let init_req = parse_and_validate_request_line(
        r#"{"v":1,"id":"init-inert","method":"initialize","params":{"protocolVersion":"1.0.0","client":{"name":"test","version":"1.0"}}}"#,
    )
    .unwrap();
    let init_resp = inert_engine.handle_request(init_req, &mut |_| {});
    assert!(init_resp.error.is_none());

    // Heartbeat expiry and unexpected pump exit on an InjectedLifecycleSource transition coverage
    // permanently to Unhealthy, retire issued windows, and clear all snapshots in PhysicalActorEngine.
    let inj = InjectedLifecycleSource::new_healthy(1, 32);
    let svc = LifecycleService::new_with_injected_source_for_test(nonce, 1, 16, inj.clone())
        .expect("healthy injected service");
    let mut engine =
        PhysicalActorEngine::new_with_lifecycle_service_for_test(OperationControlTable::new(), svc)
            .unwrap();

    let (wref, issued) = engine
        .window_tracker()
        .build_window_ref_for_tuple(live_tuple, Some("fixture".to_string()), ErrorStage::Observe)
        .expect("healthy injected service should issue WindowRef");
    let geom = sample_geometry();
    let snap = engine.snapshots().insert_snapshot(
        "task-hb".to_string(),
        wref.clone(),
        issued.stamp.clone(),
        geom.clone(),
        Vec::new(),
        "tree-hb".to_string(),
    );
    assert!(engine
        .snapshots()
        .validate_state_token(&snap.state_token, "task-hb", &wref, &issued.stamp, &geom)
        .is_ok());

    inj.inject_heartbeat_expired();
    engine.drain_and_route_lifecycle_events();
    assert_eq!(
        engine.lifecycle_service().coverage_state(),
        CoverageState::Unhealthy(CoverageFaultReason::HeartbeatExpired)
    );
    let token_err = engine
        .snapshots()
        .validate_state_token(&snap.state_token, "task-hb", &wref, &issued.stamp, &geom)
        .expect_err("coverage fault must clear all snapshots");
    assert_eq!(token_err.code, ErrorCode::StateTokenInvalid);

    // Simulated stop timeout quarantines cleanup rather than claiming clean shutdown.
    inj.set_simulate_stop_timeout(true);
    assert!(engine.lifecycle_service().shutdown_bounded().is_err());
    assert!(engine.lifecycle_service().is_cleanup_quarantined());
}

#[test]
fn test_adapter_delivered_event_retirement_and_shared_tracker_clones() {
    let nonce = HostWorkerNonce::new(0x1122_3344_5566_7788_99aa_bbcc_ddee_ff00).unwrap();
    let inj = InjectedLifecycleSource::new_healthy(1, 64);
    let svc = LifecycleService::new_with_injected_source_for_test(nonce, 1, 32, inj.clone())
        .expect("healthy injected service");
    let mut engine = PhysicalActorEngine::new_with_lifecycle_service_for_test(
        OperationControlTable::new(),
        svc.clone(),
    )
    .unwrap();

    // Tracker clones share the exact same underlying LifecycleService registry.
    let tracker_a = engine.window_tracker().clone();
    let tracker_b = WindowIdentityTracker::from_service(svc);

    let tuple = WindowOsTuple::new(0x1005, 777, 900_000).unwrap();
    let (wref_1, outcome_1) = tracker_a
        .build_window_ref_for_tuple(tuple, Some("app".to_string()), ErrorStage::Observe)
        .unwrap();
    assert_eq!(wref_1.generation, 1);
    assert_eq!(wref_1.window_key, outcome_1.identity.window_key);
    assert_eq!(wref_1.window_key, outcome_1.stamp.window_key);
    assert!(wref_1.window_key.starts_with("win_"));
    assert!(!outcome_1.reused_existing);

    let (wref_1_reused, outcome_1_reused) = tracker_b
        .build_window_ref_for_tuple(tuple, Some("app".to_string()), ErrorStage::Observe)
        .unwrap();
    assert_eq!(wref_1_reused, wref_1);
    assert!(outcome_1_reused.reused_existing);

    let geom = sample_geometry();
    let rec = engine.snapshots().insert_snapshot(
        "task-1".to_string(),
        wref_1.clone(),
        outcome_1.stamp.clone(),
        geom.clone(),
        Vec::new(),
        "hash-1".to_string(),
    );

    // Deliver EVENT_OBJECT_DESTROY for hwnd 0x1005 even though OS tuple (0x1005, 777, 900_000) is unchanged.
    inj.push_event(0x1005, LifecycleEventKind::ObjectDestroy, 12, 500);
    engine.drain_and_route_lifecycle_events();

    let stale_err = tracker_b
        .validate_window_ref_with_live_tuple(
            &wref_1,
            tuple,
            Some(&outcome_1.stamp),
            ErrorStage::Validate,
        )
        .expect_err("delivered ObjectDestroy must retire generation 1");
    assert_eq!(stale_err.code, ErrorCode::WindowStale);
    assert_eq!(
        stale_err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::GenerationMismatch)
    );

    // Snapshot for the retired window_key was removed by drain_and_route_lifecycle_events().
    let snap_err = engine
        .snapshots()
        .validate_state_token(&rec.state_token, "task-1", &wref_1, &outcome_1.stamp, &geom)
        .expect_err("retired window snapshot must be removed");
    assert_eq!(snap_err.code, ErrorCode::StateTokenInvalid);

    // Re-issuing for the exact same OS tuple allocates generation 2; generation 1 never revives.
    let (wref_2, outcome_2) = tracker_a
        .build_window_ref_for_tuple(tuple, Some("app".to_string()), ErrorStage::Observe)
        .unwrap();
    assert_eq!(wref_2.generation, 2);
    assert_eq!(wref_2.window_key, outcome_2.identity.window_key);
    assert_eq!(wref_2.window_key, outcome_2.stamp.window_key);
    assert!(wref_2.window_key.starts_with("win_"));
    assert_ne!(wref_2.window_key, wref_1.window_key);
    assert!(!outcome_2.reused_existing);

    assert!(tracker_b
        .validate_window_ref_with_live_tuple(&wref_1, tuple, None, ErrorStage::Validate)
        .is_err());
    assert!(tracker_b
        .validate_window_ref_with_live_tuple(
            &wref_2,
            tuple,
            Some(&outcome_2.stamp),
            ErrorStage::Validate
        )
        .is_ok());
}

#[test]
fn test_adapter_bounded_queue_overflow_event_storm_and_capacity_eviction() {
    let nonce = HostWorkerNonce::new(0x2233_4455_6677_8899_aabb_ccdd_eeff_0011).unwrap();

    // 1. Bounded callback queue overflow latches QueueOverflow fault.
    let small_inj = InjectedLifecycleSource::new_healthy(1, 4);
    let small_svc =
        LifecycleService::new_with_injected_source_for_test(nonce, 1, 16, small_inj.clone())
            .unwrap();
    let tuple_a = WindowOsTuple::new(0x3001, 100, 50_000).unwrap();
    let issued_a = small_svc.issue_or_reuse_window(tuple_a).unwrap();
    assert_eq!(issued_a.identity.issue_generation, 1);

    for i in 0..5u32 {
        small_inj.push_event(0x3999, LifecycleEventKind::ObjectCreate, 1, i);
    }
    let drain_err = small_svc
        .drain_and_take_retirements(16)
        .expect_err("5th event into capacity-4 queue must latch QueueOverflow");
    assert_eq!(
        drain_err,
        LifecycleError::CoverageUnhealthy(CoverageFaultReason::QueueOverflow)
    );
    assert_eq!(
        small_svc.coverage_state(),
        CoverageState::Unhealthy(CoverageFaultReason::QueueOverflow)
    );

    // 2. Event storm exceeding drain budget returns EventStormBudgetExceeded.
    let storm_inj = InjectedLifecycleSource::new_healthy(1, 16);
    let storm_svc =
        LifecycleService::new_with_injected_source_for_test(nonce, 1, 16, storm_inj.clone())
            .unwrap();
    for i in 0..6u32 {
        storm_inj.push_event(0x4000 + u64::from(i), LifecycleEventKind::ObjectCreate, 1, i);
    }
    let storm_err = storm_svc
        .drain_and_take_retirements(3)
        .expect_err("6 pending events with budget 3 must return EventStormBudgetExceeded");
    assert_eq!(
        storm_err,
        LifecycleError::EventStormBudgetExceeded {
            pending: 6,
            max_batch: 3,
        }
    );
    let mapped_obs = map_lifecycle_error_to_protocol(storm_err, ErrorStage::Observe, Some(0x4000));
    assert_eq!(mapped_obs.code, ErrorCode::ObservationUnavailable);

    // 3. Capacity eviction queues retirement in LifecycleService and removes evicted snapshot.
    let cap_inj = InjectedLifecycleSource::new_healthy(1, 32);
    let cap_svc =
        LifecycleService::new_with_injected_source_for_test(nonce, 1, 2, cap_inj).unwrap();
    let mut engine = PhysicalActorEngine::new_with_lifecycle_service_for_test(
        OperationControlTable::new(),
        cap_svc,
    )
    .unwrap();
    let geom = sample_geometry();

    let t1 = WindowOsTuple::new(0x4101, 100, 60_000).unwrap();
    let t2 = WindowOsTuple::new(0x4102, 100, 60_000).unwrap();
    let t3 = WindowOsTuple::new(0x4103, 100, 60_000).unwrap();

    let (wref_1, out_1) = engine
        .window_tracker()
        .build_window_ref_for_tuple(t1, None, ErrorStage::Observe)
        .unwrap();
    let snap_1 = engine.snapshots().insert_snapshot(
        "task-cap".to_string(),
        wref_1.clone(),
        out_1.stamp.clone(),
        geom.clone(),
        Vec::new(),
        "h1".to_string(),
    );

    let (_wref_2, _out_2) = engine
        .window_tracker()
        .build_window_ref_for_tuple(t2, None, ErrorStage::Observe)
        .unwrap();

    // Issuing a 3rd window in capacity-2 registry evicts 0x4101 (wref_1).
    let (_wref_3, out_3) = engine
        .window_tracker()
        .build_window_ref_for_tuple(t3, None, ErrorStage::Observe)
        .unwrap();
    assert_eq!(out_3.retirement_batch.retired.len(), 1);
    assert_eq!(
        out_3.retirement_batch.retired[0].reason,
        RetirementReason::CapacityEvicted
    );
    assert_eq!(out_3.retirement_batch.retired[0].window_key, wref_1.window_key);

    engine.drain_and_route_lifecycle_events();
    let evicted_snap_err = engine
        .snapshots()
        .validate_state_token(&snap_1.state_token, "task-cap", &wref_1, &out_1.stamp, &geom)
        .expect_err("capacity-evicted window snapshot must be removed");
    assert_eq!(evicted_snap_err.code, ErrorCode::StateTokenInvalid);
}

#[test]
fn test_adapter_original_ref_observation_race_never_mints_or_publishes_stale_token() {
    let nonce = HostWorkerNonce::new(0x3344_5566_7788_99aa_bbcc_ddee_ff00_1122).unwrap();
    let inj = InjectedLifecycleSource::new_healthy(1, 64);
    let svc = LifecycleService::new_with_injected_source_for_test(nonce, 1, 16, inj.clone())
        .expect("healthy injected service");

    let target_tuple = WindowOsTuple::new(0x5001, 321, 777_000).unwrap();
    let forged_unissued_ref = WindowRef {
        worker_instance: svc.worker_instance().to_string(),
        window_key: "win_0000000000005001_g1".to_string(),
        hwnd: "20481".to_string(),
        pid: 321,
        process_started: "777000".to_string(),
        generation: 1,
        app_id: None,
    };

    // 1. Calling begin_observation_for_window_ref on an unissued WindowRef must fail closed
    // without minting generation 1.
    let unissued_err = svc
        .begin_observation_for_window_ref(&forged_unissued_ref, target_tuple)
        .expect_err("unissued WindowRef must be rejected by begin_observation_for_window_ref");
    assert_eq!(
        unissued_err,
        LifecycleError::WindowGenerationRetiredOrEvicted {
            hwnd: 0x5001,
            expected_generation: 1,
        }
    );

    // Proof that begin_observation_for_window_ref did NOT mint generation 1:
    // issuing now allocates generation 1 fresh.
    let tracker = WindowIdentityTracker::from_service(svc.clone());
    let (wref_1, out_1) = tracker
        .build_window_ref_for_tuple(target_tuple, None, ErrorStage::Observe)
        .unwrap();
    assert_eq!(wref_1.generation, 1);
    assert!(!out_1.reused_existing);

    // 2. Unrelated window lifecycle event during observation does NOT invalidate target observation.
    let checkpoint_ok = svc
        .begin_observation_for_window_ref(&wref_1, target_tuple)
        .expect("active WindowRef should begin observation");
    inj.push_event(0x9999, LifecycleEventKind::ObjectDestroy, 7, 1000);
    let stamp_after_unrelated = svc
        .finish_observation_for_window_ref(&checkpoint_ok, target_tuple)
        .expect("unrelated HWND event must not invalidate target observation");
    assert_eq!(stamp_after_unrelated.issue_generation, 1);

    // 3. Target window lifecycle event during observation causes finish_observation_for_window_ref
    // to fail closed so state.get never publishes a stateToken.
    let checkpoint_race = svc
        .begin_observation_for_window_ref(&wref_1, target_tuple)
        .expect("active WindowRef should begin second observation");
    inj.push_event(0x5001, LifecycleEventKind::ObjectCreate, 7, 1010);
    let race_err = svc
        .finish_observation_for_window_ref(&checkpoint_race, target_tuple)
        .expect_err("target window event during observation must fail closed");
    let proto_err = map_lifecycle_error_to_protocol(race_err, ErrorStage::Observe, Some(0x5001));
    assert_eq!(proto_err.code, ErrorCode::ObservationUnavailable);
    assert_eq!(
        proto_err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::GenerationMismatch)
    );
}

#[test]
fn test_adapter_actor_owned_ready_ticket_retirement_releases_lock_and_records_terminal_failure() {
    let nonce = HostWorkerNonce::new(0x4455_6677_8899_aabb_ccdd_eeff_0011_2233).unwrap();
    let inj = InjectedLifecycleSource::new_healthy(1, 64);
    let svc = LifecycleService::new_with_injected_source_for_test(nonce, 1, 16, inj.clone())
        .expect("healthy injected service");
    let mut engine = PhysicalActorEngine::new_with_lifecycle_service_for_test(
        OperationControlTable::new(),
        svc,
    )
    .unwrap();

    let init_req = parse_and_validate_request_line(
        r#"{"v":1,"id":"init-ready","method":"initialize","params":{"protocolVersion":"1.0.0","client":{"name":"test","version":"1.0"}}}"#,
    )
    .unwrap();
    assert!(engine.handle_request(init_req, &mut |_| {}).error.is_none());

    let tuple = WindowOsTuple::new(0x6001, 888, 123_456_789).unwrap();
    let (wref, issued) = engine
        .window_tracker()
        .build_window_ref_for_tuple(tuple, Some("fixture".to_string()), ErrorStage::Observe)
        .unwrap();
    let geom = sample_geometry();
    let snap = engine.snapshots().insert_snapshot(
        "task-ready".to_string(),
        wref.clone(),
        issued.stamp.clone(),
        geom.clone(),
        Vec::new(),
        "tree-ready".to_string(),
    );

    let cancel_flag = AtomicBool::new(false);
    let lock_guard = engine
        .input_lock()
        .acquire_for_current_thread("op-ready-1".to_string(), 1_000, &cancel_flag)
        .expect("actor thread should acquire desktop input lock");

    let action = ActionSpec::FocusWindow {
        restore_if_minimized: Some(true),
    };
    let plan: ResolvedActionPlan =
        resolve_action_plan(u64_to_hwnd(tuple.hwnd()), &snap, &geom, &action)
            .expect("pure FocusWindow resolve_action_plan should succeed");
    assert!(matches!(plan.op, ResolvedExecutionOp::FocusWindow));
    let resolved_summary: &ResolvedTargetSummary = &plan.summary;
    assert_eq!(resolved_summary.window_hwnd, tuple.hwnd().to_string());

    engine.seed_ready_ticket_for_test(
        "task-ready".to_string(),
        "op-ready-1".to_string(),
        "pt_ready_test_1".to_string(),
        wref.clone(),
        issued.stamp.clone(),
        snap.state_token.clone(),
        action,
        plan,
        5_000,
        lock_guard,
    );

    // Deliver target ObjectDestroy while the ticket is waiting in Ready state.
    inj.push_event(0x6001, LifecycleEventKind::ObjectDestroy, 9, 2_000);
    engine.poll_maintenance();

    // Verify the actor thread released the DesktopInputLockGuard during poll_maintenance().
    let mut probe_guard = engine
        .input_lock()
        .acquire_for_current_thread("op-probe-after-retire".to_string(), 200, &cancel_flag)
        .expect("DesktopInputLockGuard must be released when ready ticket is retired");
    probe_guard
        .release()
        .expect("probe guard release on owning thread should succeed");

    // Subsequent action.commit returns the recorded terminal WindowStale failure with mutation=none.
    let commit_req = parse_and_validate_request_line(
        r#"{"v":1,"id":"commit-retired","method":"action.commit","params":{"taskSessionId":"task-ready","operationId":"op-ready-1","prepareTicket":"pt_ready_test_1"}}"#,
    )
    .unwrap();
    let commit_resp = engine.handle_request(commit_req, &mut |_| {});
    let commit_err = commit_resp
        .error
        .expect("commit of lifecycle-retired ready ticket must fail closed");
    assert_eq!(commit_err.code, ErrorCode::WindowStale);
    assert_eq!(commit_err.stage, ErrorStage::Ready);
    assert_eq!(commit_err.mutation, MutationState::None);
    assert_eq!(
        commit_err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::GenerationMismatch)
    );

    // Subsequent action.cancel reports the terminal failed state without claiming mutation.
    let cancel_req = parse_and_validate_request_line(
        r#"{"v":1,"id":"cancel-retired","method":"action.cancel","params":{"taskSessionId":"task-ready","operationId":"op-ready-1"}}"#,
    )
    .unwrap();
    let cancel_resp = engine.handle_request(cancel_req, &mut |_| {});
    assert!(cancel_resp.error.is_none());
    let cancel_val = cancel_resp.result.unwrap();
    assert_eq!(cancel_val.get("cancelled").and_then(|v| v.as_bool()), Some(false));
    assert_eq!(cancel_val.get("stage").and_then(|v| v.as_str()), Some("ready"));
    assert_eq!(cancel_val.get("mutation").and_then(|v| v.as_str()), Some("none"));
    assert_eq!(
        cancel_val.get("inputLockReleased").and_then(|v| v.as_bool()),
        Some(true)
    );
}
