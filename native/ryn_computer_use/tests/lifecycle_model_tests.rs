#![forbid(unsafe_code)]

use ryn_computer_use::lifecycle_model::{
    CoverageFaultReason, CoverageState, DeliveredWindowEvent, HostWorkerNonce,
    InvalidOsTupleReason, LifecycleError, LifecycleEventKind, MAX_HOST_NONCE_HISTORY,
    RetirementReason, WindowLifecycleRegistry, WindowOsTuple,
};

#[test]
fn same_hwnd_same_pid_creation_after_destroy_and_create_rejects_old() {
    let host_nonce = HostWorkerNonce::new(0x1111_2222_3333_4444_5555_6666_7777_8888).unwrap();
    let mut registry = WindowLifecycleRegistry::new(host_nonce, 1, 16).unwrap();

    let tuple = WindowOsTuple::new(0x0001_0040, 4242, 133_700_000_001).unwrap();
    let first = registry.issue_or_reuse(tuple).unwrap();
    assert!(!first.reused_existing);
    assert_eq!(first.identity.issue_generation, 1);
    assert_eq!(first.stamp.target_event_revision, 0);

    // Without intervening lifecycle events, re-observing the identical live tuple reuses generation 1.
    let reused = registry.issue_or_reuse(tuple).unwrap();
    assert!(reused.reused_existing);
    assert_eq!(reused.identity.issue_generation, 1);
    assert_eq!(reused.identity.window_key, first.identity.window_key);

    // Deliver DESTROY followed by CREATE for the same numeric HWND.
    let destroy_batch = registry
        .ingest_event(DeliveredWindowEvent {
            hwnd: tuple.hwnd(),
            kind: LifecycleEventKind::ObjectDestroy,
            event_thread: 99,
            event_time_ms: 1000,
            arrival_seq: 1,
            hook_generation: 1,
        })
        .unwrap();
    assert_eq!(destroy_batch.retired.len(), 1);
    assert_eq!(destroy_batch.retired[0].issue_generation, 1);
    assert_eq!(destroy_batch.retired[0].window_key, first.identity.window_key);
    assert_eq!(
        destroy_batch.retired[0].reason,
        RetirementReason::DeliveredEvent(LifecycleEventKind::ObjectDestroy)
    );

    let create_batch = registry
        .ingest_event(DeliveredWindowEvent {
            hwnd: tuple.hwnd(),
            kind: LifecycleEventKind::ObjectCreate,
            event_thread: 99,
            event_time_ms: 1005,
            arrival_seq: 2,
            hook_generation: 1,
        })
        .unwrap();
    assert!(create_batch.retired.is_empty());

    // Old stamp must be rejected even when live OS tuple is identical.
    let err = registry.validate_stamp(&first.stamp, tuple).unwrap_err();
    assert_eq!(
        err,
        LifecycleError::TargetEventRevisionAdvanced {
            hwnd: tuple.hwnd(),
            stamp_revision: 0,
            current_revision: 2,
        }
    );

    // Re-issuing for the exact same (hwnd, pid, process_started) must allocate generation 2 and a new key.
    let second = registry.issue_or_reuse(tuple).unwrap();
    assert!(!second.reused_existing);
    assert_eq!(second.identity.issue_generation, 2);
    assert_ne!(second.identity.window_key, first.identity.window_key);
    assert_eq!(second.stamp.target_event_revision, 2);

    // Old stamp is still rejected after new generation 2 is active.
    let err_after_reissue = registry.validate_stamp(&first.stamp, tuple).unwrap_err();
    assert_eq!(
        err_after_reissue,
        LifecycleError::TargetEventRevisionAdvanced {
            hwnd: tuple.hwnd(),
            stamp_revision: 0,
            current_revision: 2,
        }
    );

    // A single delivered CREATE alone (without prior DESTROY) also retires active generation 2.
    let create_only_batch = registry
        .ingest_event(DeliveredWindowEvent {
            hwnd: tuple.hwnd(),
            kind: LifecycleEventKind::ObjectCreate,
            event_thread: 99,
            event_time_ms: 1010,
            arrival_seq: 3,
            hook_generation: 1,
        })
        .unwrap();
    assert_eq!(create_only_batch.retired.len(), 1);
    assert_eq!(create_only_batch.retired[0].issue_generation, 2);
    assert!(registry.validate_stamp(&second.stamp, tuple).is_err());
}

#[test]
fn duplicate_late_and_32bit_time_wraparound_events_never_revive_old_generation() {
    let host_nonce = HostWorkerNonce::new(0xabcd_ef01_2345_6789_abcd_ef01_2345_6789).unwrap();
    let mut registry = WindowLifecycleRegistry::new(host_nonce, 1, 16).unwrap();
    let tuple = WindowOsTuple::new(0x0002_0080, 7777, 999_888_777).unwrap();

    let gen1 = registry.issue_or_reuse(tuple).unwrap();
    assert_eq!(gen1.identity.issue_generation, 1);

    // Event near u32::MAX tick count retires gen 1.
    registry
        .ingest_event(DeliveredWindowEvent {
            hwnd: tuple.hwnd(),
            kind: LifecycleEventKind::ObjectDestroy,
            event_thread: 12,
            event_time_ms: u32::MAX - 2,
            arrival_seq: 1,
            hook_generation: 1,
        })
        .unwrap();

    let gen2 = registry.issue_or_reuse(tuple).unwrap();
    assert_eq!(gen2.identity.issue_generation, 2);

    // 32-bit tick wraps around to 4ms on a delivered CREATE; retires gen 2.
    let batch_wrap = registry
        .ingest_event(DeliveredWindowEvent {
            hwnd: tuple.hwnd(),
            kind: LifecycleEventKind::ObjectCreate,
            event_thread: 12,
            event_time_ms: 4,
            arrival_seq: 2,
            hook_generation: 1,
        })
        .unwrap();
    assert_eq!(batch_wrap.retired.len(), 1);
    assert_eq!(batch_wrap.retired[0].issue_generation, 2);

    // Duplicate/late CREATE and DESTROY with backward 32-bit event_time_ms must never revive gen 1 or gen 2.
    registry
        .ingest_event(DeliveredWindowEvent {
            hwnd: tuple.hwnd(),
            kind: LifecycleEventKind::ObjectCreate,
            event_thread: 12,
            event_time_ms: u32::MAX - 500,
            arrival_seq: 3,
            hook_generation: 1,
        })
        .unwrap();
    registry
        .ingest_event(DeliveredWindowEvent {
            hwnd: tuple.hwnd(),
            kind: LifecycleEventKind::ObjectDestroy,
            event_thread: 12,
            event_time_ms: 1,
            arrival_seq: 4,
            hook_generation: 1,
        })
        .unwrap();

    assert!(registry.validate_stamp(&gen1.stamp, tuple).is_err());
    assert!(registry.validate_stamp(&gen2.stamp, tuple).is_err());

    let gen3 = registry.issue_or_reuse(tuple).unwrap();
    assert_eq!(gen3.identity.issue_generation, 3);
    assert_ne!(gen3.identity.window_key, gen1.identity.window_key);
    assert_ne!(gen3.identity.window_key, gen2.identity.window_key);
}

#[test]
fn bounded_capacity_eviction_emits_retirement_and_never_revives_old_key() {
    let host_nonce = HostWorkerNonce::new(0x9999_0000_1111_2222_3333_4444_5555_6666).unwrap();
    let mut registry = WindowLifecycleRegistry::new(host_nonce, 1, 2).unwrap();

    let tuple_a = WindowOsTuple::new(0x1001, 100, 5000).unwrap();
    let tuple_b = WindowOsTuple::new(0x1002, 100, 5000).unwrap();
    let tuple_c = WindowOsTuple::new(0x1003, 100, 5000).unwrap();

    let out_a = registry.issue_or_reuse(tuple_a).unwrap();
    let out_b = registry.issue_or_reuse(tuple_b).unwrap();
    assert_eq!(registry.tracked_slot_count(), 2);

    // Issuing tuple_c at capacity 2 evicts oldest active slot (tuple_a) and emits explicit retirement output.
    let out_c = registry.issue_or_reuse(tuple_c).unwrap();
    assert_eq!(registry.tracked_slot_count(), 2);
    assert_eq!(out_c.retirement_batch.retired.len(), 1);
    assert_eq!(out_c.retirement_batch.retired[0].hwnd, tuple_a.hwnd());
    assert_eq!(
        out_c.retirement_batch.retired[0].issue_generation,
        out_a.identity.issue_generation
    );
    assert_eq!(
        out_c.retirement_batch.retired[0].reason,
        RetirementReason::CapacityEvicted
    );

    // Evicted stamp_a is rejected.
    assert_eq!(
        registry.validate_stamp(&out_a.stamp, tuple_a).unwrap_err(),
        LifecycleError::WindowGenerationRetiredOrEvicted {
            hwnd: tuple_a.hwnd(),
            expected_generation: 1,
        }
    );
    // Resident stamp_b and stamp_c remain valid.
    assert!(registry.validate_stamp(&out_b.stamp, tuple_b).is_ok());
    assert!(registry.validate_stamp(&out_c.stamp, tuple_c).is_ok());

    // Re-observing tuple_a after eviction allocates global generation 4 (never reuses generation 1 or its key).
    let out_a_reissued = registry.issue_or_reuse(tuple_a).unwrap();
    assert_eq!(out_a_reissued.identity.issue_generation, 4);
    assert_ne!(out_a_reissued.identity.window_key, out_a.identity.window_key);
    assert_eq!(
        registry.validate_stamp(&out_a.stamp, tuple_a).unwrap_err(),
        LifecycleError::WindowGenerationMismatch {
            hwnd: tuple_a.hwnd(),
            expected_generation: 1,
            actual_generation: 4,
        }
    );
}

#[test]
fn coverage_overflow_and_restart_retire_all_old_stamps_without_resurrection() {
    let nonce_v1 = HostWorkerNonce::new(0x0101_0101_0101_0101_0101_0101_0101_0101).unwrap();
    let nonce_v2 = HostWorkerNonce::new(0x0202_0202_0202_0202_0202_0202_0202_0202).unwrap();
    let mut registry = WindowLifecycleRegistry::new(nonce_v1, 10, 8).unwrap();

    let tuple_1 = WindowOsTuple::new(0x3001, 200, 9000).unwrap();
    let tuple_2 = WindowOsTuple::new(0x3002, 201, 9001).unwrap();
    let issued_1 = registry.issue_or_reuse(tuple_1).unwrap();
    let issued_2 = registry.issue_or_reuse(tuple_2).unwrap();

    // Arrival sequence gap (expected 1, got 3) faults coverage and retires all active windows.
    let gap_err = registry
        .ingest_event(DeliveredWindowEvent {
            hwnd: tuple_1.hwnd(),
            kind: LifecycleEventKind::ObjectCreate,
            event_thread: 1,
            event_time_ms: 50,
            arrival_seq: 3,
            hook_generation: 10,
        })
        .unwrap_err();
    assert_eq!(
        gap_err,
        LifecycleError::CoverageUnhealthy(CoverageFaultReason::ArrivalSeqGapOrOutOfOrder)
    );
    assert_eq!(
        registry.coverage_state(),
        CoverageState::Unhealthy(CoverageFaultReason::ArrivalSeqGapOrOutOfOrder)
    );

    // All old stamps fail closed when coverage is unhealthy.
    assert_eq!(
        registry.validate_stamp(&issued_1.stamp, tuple_1).unwrap_err(),
        LifecycleError::CoverageUnhealthy(CoverageFaultReason::ArrivalSeqGapOrOutOfOrder)
    );
    assert_eq!(
        registry.validate_stamp(&issued_2.stamp, tuple_2).unwrap_err(),
        LifecycleError::CoverageUnhealthy(CoverageFaultReason::ArrivalSeqGapOrOutOfOrder)
    );

    // Restarting with same coverage generation or reused host nonce is refused.
    assert_eq!(
        registry.restart_coverage(nonce_v2, 10).unwrap_err(),
        LifecycleError::RestartRequiresNewCoverageGeneration {
            current: 10,
            supplied: 10,
        }
    );
    assert_eq!(
        registry.restart_coverage(nonce_v1, 11).unwrap_err(),
        LifecycleError::RestartRequiresFreshHostNonce
    );

    // Valid restart with new coverage generation and fresh host nonce succeeds, but never resurrects old stamps.
    let restart_batch = registry.restart_coverage(nonce_v2, 11).unwrap();
    assert_eq!(restart_batch.coverage_state, CoverageState::Healthy);
    assert_eq!(restart_batch.coverage_generation, 11);

    assert_eq!(
        registry.validate_stamp(&issued_1.stamp, tuple_1).unwrap_err(),
        LifecycleError::HostWorkerNonceMismatch {
            expected: nonce_v2,
            actual: nonce_v1,
        }
    );

    // Even if a caller forged the new host nonce onto the old stamp, coverage_generation mismatch still rejects.
    let mut forged_stamp = issued_1.stamp.clone();
    forged_stamp.host_nonce = nonce_v2;
    assert_eq!(
        registry.validate_stamp(&forged_stamp, tuple_1).unwrap_err(),
        LifecycleError::CoverageGenerationMismatch {
            expected: 11,
            actual: 10,
        }
    );

    // Fresh issuance after restart continues monotonic global issue_generation (3).
    let post_restart = registry.issue_or_reuse(tuple_1).unwrap();
    assert_eq!(post_restart.identity.issue_generation, 3);
    assert_eq!(post_restart.identity.coverage_generation, 11);
}

#[test]
fn snapshot_serial_race_and_unrelated_target_event_isolation() {
    let host_nonce = HostWorkerNonce::new(0x7777_8888_9999_aaaa_bbbb_cccc_dddd_eeee).unwrap();
    let mut registry = WindowLifecycleRegistry::new(host_nonce, 1, 16).unwrap();

    let target_tuple = WindowOsTuple::new(0x5001, 310, 70_000).unwrap();
    let other_tuple = WindowOsTuple::new(0x5002, 311, 70_001).unwrap();

    let _other_issued = registry.issue_or_reuse(other_tuple).unwrap();
    let (checkpoint, _) = registry.begin_observation(target_tuple).unwrap();

    // Unrelated window has DESTROY + CREATE during target's observation window.
    let drain_unrelated = registry
        .drain_events_bounded(
            &[
                DeliveredWindowEvent {
                    hwnd: other_tuple.hwnd(),
                    kind: LifecycleEventKind::ObjectDestroy,
                    event_thread: 7,
                    event_time_ms: 200,
                    arrival_seq: 1,
                    hook_generation: 1,
                },
                DeliveredWindowEvent {
                    hwnd: other_tuple.hwnd(),
                    kind: LifecycleEventKind::ObjectCreate,
                    event_thread: 7,
                    event_time_ms: 205,
                    arrival_seq: 2,
                    hook_generation: 1,
                },
            ],
            16,
        )
        .unwrap();
    assert_eq!(drain_unrelated.processed_events, 2);
    assert_eq!(drain_unrelated.retirement_batch.retired.len(), 1);
    assert_eq!(
        drain_unrelated.retirement_batch.retired[0].hwnd,
        other_tuple.hwnd()
    );

    // Target window observation still succeeds because target had zero lifecycle changes.
    let verified_stamp = registry
        .finish_observation(&checkpoint, target_tuple)
        .unwrap();
    assert_eq!(verified_stamp.issue_generation, checkpoint.stamp.issue_generation);
    assert_eq!(verified_stamp.observed_arrival_seq, 2);

    // Now begin a second observation on target, and inject a target DESTROY+CREATE race mid-sample.
    let (race_checkpoint, _) = registry.begin_observation(target_tuple).unwrap();
    registry
        .drain_events_bounded(
            &[
                DeliveredWindowEvent {
                    hwnd: target_tuple.hwnd(),
                    kind: LifecycleEventKind::ObjectDestroy,
                    event_thread: 8,
                    event_time_ms: 210,
                    arrival_seq: 3,
                    hook_generation: 1,
                },
                DeliveredWindowEvent {
                    hwnd: target_tuple.hwnd(),
                    kind: LifecycleEventKind::ObjectCreate,
                    event_thread: 8,
                    event_time_ms: 212,
                    arrival_seq: 4,
                    hook_generation: 1,
                },
            ],
            16,
        )
        .unwrap();

    // Finishing observation on target must fail closed even though live OS tuple matches.
    let race_err = registry
        .finish_observation(&race_checkpoint, target_tuple)
        .unwrap_err();
    assert_eq!(
        race_err,
        LifecycleError::TargetEventRevisionAdvanced {
            hwnd: target_tuple.hwnd(),
            stamp_revision: 0,
            current_revision: 2,
        }
    );

    // Bounded drain refuses event storm exceeding max_batch without spinning.
    let storm_events = [
        DeliveredWindowEvent {
            hwnd: other_tuple.hwnd(),
            kind: LifecycleEventKind::ObjectDestroy,
            event_thread: 8,
            event_time_ms: 220,
            arrival_seq: 5,
            hook_generation: 1,
        },
        DeliveredWindowEvent {
            hwnd: other_tuple.hwnd(),
            kind: LifecycleEventKind::ObjectCreate,
            event_thread: 8,
            event_time_ms: 221,
            arrival_seq: 6,
            hook_generation: 1,
        },
    ];
    assert_eq!(
        registry.drain_events_bounded(&storm_events, 1).unwrap_err(),
        LifecycleError::EventStormBudgetExceeded {
            pending: 2,
            max_batch: 1,
        }
    );
}

#[test]
fn checked_issue_generation_overflow_and_invalid_os_tuples_fail_closed() {
    assert_eq!(
        WindowOsTuple::new(0, 100, 200).unwrap_err(),
        LifecycleError::InvalidOsTuple(InvalidOsTupleReason::ZeroHwnd)
    );
    assert_eq!(
        WindowOsTuple::new(0x100, 0, 200).unwrap_err(),
        LifecycleError::InvalidOsTuple(InvalidOsTupleReason::ZeroPid)
    );
    assert_eq!(
        WindowOsTuple::new(0x100, 100, 0).unwrap_err(),
        LifecycleError::InvalidOsTuple(InvalidOsTupleReason::ZeroProcessCreationTime)
    );

    let host_nonce = HostWorkerNonce::new(0xffff_eeee_dddd_cccc_bbbb_aaaa_9999_8888).unwrap();
    let mut registry =
        WindowLifecycleRegistry::with_initial_issue_generation(host_nonce, 1, 8, u64::MAX).unwrap();

    let tuple_max = WindowOsTuple::new(0x9001, 500, 80_000).unwrap();
    let issued_max = registry.issue_or_reuse(tuple_max).unwrap();
    assert_eq!(issued_max.identity.issue_generation, u64::MAX);
    assert_eq!(registry.next_issue_generation(), None);

    // Attempting to issue one more window identity fails closed with IssueGenerationOverflow.
    let tuple_overflow = WindowOsTuple::new(0x9002, 500, 80_000).unwrap();
    assert_eq!(
        registry.issue_or_reuse(tuple_overflow).unwrap_err(),
        LifecycleError::IssueGenerationOverflow
    );
    assert_eq!(
        registry.coverage_state(),
        CoverageState::Unhealthy(CoverageFaultReason::IssueGenerationOverflow)
    );
}

#[test]
fn ten_thousand_attempted_restarts_stay_bounded_and_fail_closed_without_revival() {
    let initial_nonce = HostWorkerNonce::new(1).unwrap();
    let mut registry = WindowLifecycleRegistry::new(initial_nonce, 1, 1).unwrap();
    assert_eq!(registry.max_capacity(), 1);
    assert_eq!(registry.max_host_nonce_history(), MAX_HOST_NONCE_HISTORY);
    assert_eq!(registry.used_host_nonce_count(), 1);

    let tuple = WindowOsTuple::new(0x7001, 404, 123_456_789).unwrap();
    let initial_issued = registry.issue_or_reuse(tuple).unwrap();
    let mut all_issued_stamps = Vec::with_capacity(MAX_HOST_NONCE_HISTORY);
    all_issued_stamps.push(initial_issued.stamp);

    let mut last_accepted_nonce = initial_nonce;
    let mut last_accepted_cov_gen = 1u64;

    // Attempt 10,000 restarts with distinct candidate nonces and monotonic coverage generations.
    for attempt in 1..=10_000u64 {
        let candidate_nonce = HostWorkerNonce::new((attempt as u128) + 1).unwrap();
        let candidate_cov_gen = attempt + 1;

        if (attempt as usize) < MAX_HOST_NONCE_HISTORY {
            let batch = registry
                .restart_coverage(candidate_nonce, candidate_cov_gen)
                .unwrap();
            assert_eq!(batch.coverage_state, CoverageState::Healthy);
            assert_eq!(batch.coverage_generation, candidate_cov_gen);
            assert_eq!(registry.used_host_nonce_count(), (attempt as usize) + 1);
            assert!(registry.used_host_nonce_count() <= MAX_HOST_NONCE_HISTORY);
            assert_eq!(registry.tracked_slot_count(), 0);

            last_accepted_nonce = candidate_nonce;
            last_accepted_cov_gen = candidate_cov_gen;

            // Issue a window in each healthy restarted generation so we can verify both slot
            // retirement on the boundary transition and permanent non-revival of all prior stamps.
            let issued = registry.issue_or_reuse(tuple).unwrap();
            assert_eq!(registry.tracked_slot_count(), 1);
            all_issued_stamps.push(issued.stamp);
        } else {
            // Snapshot state BEFORE the refused restart attempt to prove no unbounded growth or
            // partial mutation of host_nonce / coverage_generation / next_issue_generation.
            let nonce_before = registry.host_nonce();
            let cov_gen_before = registry.coverage_generation();
            let used_count_before = registry.used_host_nonce_count();
            let next_issue_before = registry.next_issue_generation();

            let err = registry
                .restart_coverage(candidate_nonce, candidate_cov_gen)
                .unwrap_err();
            assert_eq!(
                err,
                LifecycleError::HostNonceHistoryExhausted {
                    max_history: MAX_HOST_NONCE_HISTORY,
                }
            );

            // Prove bounded state and before/after invariants:
            // - used_host_nonces never exceeds MAX_HOST_NONCE_HISTORY
            // - active slots are retired/cleared (0 slots retained)
            // - host_nonce, coverage_generation, and next_issue_generation are not mutated to the rejected candidate
            assert_eq!(registry.used_host_nonce_count(), MAX_HOST_NONCE_HISTORY);
            assert_eq!(registry.used_host_nonce_count(), used_count_before);
            assert_eq!(registry.tracked_slot_count(), 0);
            assert_eq!(registry.host_nonce(), nonce_before);
            assert_eq!(registry.host_nonce(), last_accepted_nonce);
            assert_eq!(registry.coverage_generation(), cov_gen_before);
            assert_eq!(registry.coverage_generation(), last_accepted_cov_gen);
            assert_eq!(registry.next_issue_generation(), next_issue_before);
            assert_eq!(
                registry.coverage_state(),
                CoverageState::Unhealthy(CoverageFaultReason::HostNonceHistoryExhausted)
            );
        }
    }

    // Every single previously issued stamp across all 64 generations must fail closed (no revival).
    assert_eq!(all_issued_stamps.len(), MAX_HOST_NONCE_HISTORY);
    for old_stamp in &all_issued_stamps {
        assert_eq!(
            registry.validate_stamp(old_stamp, tuple).unwrap_err(),
            LifecycleError::CoverageUnhealthy(CoverageFaultReason::HostNonceHistoryExhausted)
        );
    }

    // Further issuance or observation attempts also fail closed until a new host worker is created.
    assert_eq!(
        registry.issue_or_reuse(tuple).unwrap_err(),
        LifecycleError::CoverageUnhealthy(CoverageFaultReason::HostNonceHistoryExhausted)
    );
}
