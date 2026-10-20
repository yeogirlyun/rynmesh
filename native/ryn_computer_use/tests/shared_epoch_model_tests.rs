#![forbid(unsafe_code)]

use ryn_computer_use::shared_epoch_model::{
    AcquisitionDisposition, ActiveWriterIdentity, CorruptedHeaderReason, DesktopInputLockProof,
    DesktopLockNamespace, DesktopMutexArbiter, EpochNonce, INITIAL_EVEN_SEQUENCE,
    MutationAccounting, RecoveryDecision, ReleaseOnlyCleanupProof, SHARED_EPOCH_HEADER_BYTES,
    SHARED_EPOCH_PAGE_BYTES, SharedEpochError, SharedEpochHeader, SharedEpochModel,
    SharedEpochStamp, WriteTerminalKind,
};

fn test_namespace() -> DesktopLockNamespace {
    DesktopLockNamespace::new("S-1-5-21-1000-2000-3000-1001", 1, "0123456789abcdef").unwrap()
}

fn writer_identity(pid: u32, started: u64, worker_nonce: u128, tag: u8) -> ActiveWriterIdentity {
    ActiveWriterIdentity::new(pid, started, worker_nonce, [tag; 32]).unwrap()
}

#[test]
fn epoch2_read_interleaved_with_b_odd3_even4_causes_a_second_read_to_refuse() {
    let ns = test_namespace();
    assert_ne!(ns.mutex_name(), ns.mapping_name());

    let mut arbiter = DesktopMutexArbiter::new(ns.clone());
    let nonce_1 = EpochNonce::from_host_csprng(0x1111_2222_3333_4444_5555_6666_7777_8888).unwrap();

    // Bootstrap shared epoch at even sequence 2.
    let mut init_lease = arbiter.acquire_for_mutation(101, "bootstrap").unwrap();
    let mut model = SharedEpochModel::create_new(ns.clone(), &init_lease, 101, nonce_1).unwrap();
    arbiter.release_lease(&mut init_lease, 101).unwrap();

    // Step 1: Worker A (thread 101) takes short mutex lease and samples before UIA/screenshot capture.
    let mut lease_a1 = arbiter.acquire_for_observation(101, "a_obs_before").unwrap();
    let pre_sample_a = model.sample_before_observation(&lease_a1, 101).unwrap();
    assert_eq!(pre_sample_a.pre_stamp.sequence, 2);
    assert_eq!(pre_sample_a.pre_stamp.mapping_nonce, nonce_1);
    arbiter.release_lease(&mut lease_a1, 101).unwrap();

    // Step 2: While Worker A is capturing unlocked UIA/screenshot, Worker B (thread 202) prepares and commits.
    let mut lease_b = arbiter.acquire_for_mutation(202, "b_commit").unwrap();
    let writer_b = writer_identity(2002, 88_000_002, 0xb0b0_b0b0, 0xbb);
    let ticket_b = model
        .prepare_write(&mut lease_b, 202, pre_sample_a.pre_stamp, writer_b, None)
        .unwrap();

    let mut write_handle_b = model.begin_write(&lease_b, 202, ticket_b, None).unwrap();
    assert_eq!(write_handle_b.odd_sequence(), 3);
    assert_eq!(write_handle_b.reserved_next_even(), 4);

    // Header during active write is odd 3 and carries Worker B's identity.
    let mid_header = model.inspect_header(&lease_b, 202).unwrap();
    assert_eq!(mid_header.sequence, 3);
    assert_eq!(mid_header.active_writer, Some(writer_b));

    let finish_b = model
        .finish_write(
            &lease_b,
            202,
            &mut write_handle_b,
            ReleaseOnlyCleanupProof::completed(),
            WriteTerminalKind::Succeeded,
        )
        .unwrap();
    assert_eq!(finish_b.new_stamp.sequence, 4);
    assert_eq!(finish_b.mutation_accounting, MutationAccounting::Confirmed);
    arbiter.release_lease(&mut lease_b, 202).unwrap();

    // Step 3: Worker A (thread 101) re-acquires short mutex lease after capture and sees sequence 4 != 2.
    let mut lease_a2 = arbiter.acquire_for_observation(101, "a_obs_after").unwrap();
    let after_err = model
        .sample_after_observation(&lease_a2, 101, &pre_sample_a)
        .unwrap_err();
    assert_eq!(
        after_err,
        SharedEpochError::EpochAdvancedDuringObservation {
            before_sequence: 2,
            after_sequence: 4,
        }
    );
    arbiter.release_lease(&mut lease_a2, 101).unwrap();
}

#[test]
fn odd_read_busy_and_same_actor_ready_ticket_conflict() {
    let ns = test_namespace();
    let mut arbiter = DesktopMutexArbiter::new(ns.clone());
    let nonce = EpochNonce::from_host_csprng(0x2222_3333_4444_5555_6666_7777_8888_9999).unwrap();

    let mut lease_a = arbiter.acquire_for_mutation(101, "a_prepare").unwrap();
    let mut model = SharedEpochModel::create_new(ns, &lease_a, 101, nonce).unwrap();

    let stamp_2 = SharedEpochStamp {
        mapping_nonce: nonce,
        sequence: 2,
    };
    let writer_a = writer_identity(1001, 77_000_001, 0xa0a0_a0a0, 0xaa);
    let ticket_a = model
        .prepare_write(&mut lease_a, 101, stamp_2, writer_a, None)
        .unwrap();

    // While Worker A holds the ready ticket lease on thread 101, a state.get on the same actor thread
    // must immediately return LockBusySameActorReadyTicket rather than waiting on itself.
    assert_eq!(
        arbiter
            .acquire_for_observation(101, "nested_state_get")
            .unwrap_err(),
        SharedEpochError::LockBusySameActorReadyTicket
    );
    // Another worker/thread sees LockBusy.
    assert_eq!(
        arbiter
            .acquire_for_mutation(202, "competing_prepare")
            .unwrap_err(),
        SharedEpochError::LockBusy
    );

    // Publish odd sequence 3.
    let mut handle_a = model.begin_write(&lease_a, 101, ticket_a, None).unwrap();
    assert_eq!(handle_a.odd_sequence(), 3);

    // Any observation sample against an odd sequence must refuse with EpochBusyOddSequence.
    assert_eq!(
        model.sample_before_observation(&lease_a, 101).unwrap_err(),
        SharedEpochError::EpochBusyOddSequence { sequence: 3 }
    );

    model
        .finish_write(
            &lease_a,
            101,
            &mut handle_a,
            ReleaseOnlyCleanupProof::completed(),
            WriteTerminalKind::Succeeded,
        )
        .unwrap();
    arbiter.release_lease(&mut lease_a, 101).unwrap();
}

#[test]
fn ready_cancel_or_expire_keeps_even_sequence_2_without_advancing() {
    let ns = test_namespace();
    let mut arbiter = DesktopMutexArbiter::new(ns.clone());
    let nonce = EpochNonce::from_host_csprng(0x3333_4444_5555_6666_7777_8888_9999_aaaa).unwrap();

    let mut lease_a = arbiter.acquire_for_mutation(101, "a_prepare_cancel").unwrap();
    let mut model = SharedEpochModel::create_new(ns, &lease_a, 101, nonce).unwrap();

    let stamp_2 = SharedEpochStamp {
        mapping_nonce: nonce,
        sequence: 2,
    };
    let writer_a = writer_identity(1001, 77_000_001, 0xa1a1, 0x11);
    let ticket_a = model
        .prepare_write(&mut lease_a, 101, stamp_2, writer_a, None)
        .unwrap();

    // Header remains at even sequence 2 during ready.
    let ready_header = model.inspect_header(&lease_a, 101).unwrap();
    assert_eq!(ready_header.sequence, 2);
    assert_eq!(ready_header.active_writer, None);

    // Cancel ready ticket before commit -> sequence stays 2, zero odd state published.
    let unchanged_stamp = model
        .cancel_or_expire_ready(&lease_a, 101, ticket_a)
        .unwrap();
    assert_eq!(unchanged_stamp, stamp_2);
    arbiter.release_lease(&mut lease_a, 101).unwrap();

    // Worker B can now prepare and commit using the original sequence 2 stamp.
    let mut lease_b = arbiter.acquire_for_mutation(202, "b_commit").unwrap();
    let writer_b = writer_identity(2002, 77_000_002, 0xb2b2, 0x22);
    let ticket_b = model
        .prepare_write(&mut lease_b, 202, stamp_2, writer_b, None)
        .unwrap();
    let mut handle_b = model.begin_write(&lease_b, 202, ticket_b, None).unwrap();
    let outcome_b = model
        .finish_write(
            &lease_b,
            202,
            &mut handle_b,
            ReleaseOnlyCleanupProof::completed(),
            WriteTerminalKind::Succeeded,
        )
        .unwrap();
    assert_eq!(outcome_b.new_stamp.sequence, 4);
    arbiter.release_lease(&mut lease_b, 202).unwrap();
}

#[test]
fn matching_commit_publishes_odd_and_even_across_normal_error_and_cancel_terminals() {
    let ns = test_namespace();
    let mut arbiter = DesktopMutexArbiter::new(ns.clone());
    let nonce = EpochNonce::from_host_csprng(0x4444_5555_6666_7777_8888_9999_aaaa_bbbb).unwrap();

    let mut lease = arbiter.acquire_for_mutation(101, "commit_terminals").unwrap();
    let mut model = SharedEpochModel::create_new(ns, &lease, 101, nonce).unwrap();
    let writer = writer_identity(1001, 90_000_001, 0xc3c3, 0x33);

    // 1. Succeeded: 2 -> 3 -> 4 (Confirmed)
    let stamp_2 = SharedEpochStamp {
        mapping_nonce: nonce,
        sequence: 2,
    };
    let ticket_1 = model
        .prepare_write(&mut lease, 101, stamp_2, writer, None)
        .unwrap();
    let mut handle_1 = model.begin_write(&lease, 101, ticket_1, None).unwrap();
    assert_eq!(model.inspect_header(&lease, 101).unwrap().sequence, 3);
    let out_1 = model
        .finish_write(
            &lease,
            101,
            &mut handle_1,
            ReleaseOnlyCleanupProof::completed(),
            WriteTerminalKind::Succeeded,
        )
        .unwrap();
    assert_eq!(out_1.new_stamp.sequence, 4);
    assert_eq!(out_1.mutation_accounting, MutationAccounting::Confirmed);

    // 2. FailedAfterActivation: 4 -> 5 -> 6 (UnknownOrPartial, old stamp 2/4 invalidated)
    let ticket_2 = model
        .prepare_write(&mut lease, 101, out_1.new_stamp, writer, None)
        .unwrap();
    let mut handle_2 = model.begin_write(&lease, 101, ticket_2, None).unwrap();
    assert_eq!(model.inspect_header(&lease, 101).unwrap().sequence, 5);
    let out_2 = model
        .finish_write(
            &lease,
            101,
            &mut handle_2,
            ReleaseOnlyCleanupProof::completed(),
            WriteTerminalKind::FailedAfterActivation,
        )
        .unwrap();
    assert_eq!(out_2.new_stamp.sequence, 6);
    assert_eq!(
        out_2.mutation_accounting,
        MutationAccounting::UnknownOrPartial
    );
    assert_eq!(model.inspect_header(&lease, 101).unwrap().active_writer, None);

    // Old stamp 4 is now stale.
    assert_eq!(
        model
            .prepare_write(&mut lease, 101, out_1.new_stamp, writer, None)
            .unwrap_err(),
        SharedEpochError::EpochAdvanced {
            expected_sequence: 4,
            actual_sequence: 6,
        }
    );

    // 3. CancelledAfterActivation: 6 -> 7 -> 8 (UnknownOrPartial)
    let ticket_3 = model
        .prepare_write(&mut lease, 101, out_2.new_stamp, writer, None)
        .unwrap();
    let mut handle_3 = model.begin_write(&lease, 101, ticket_3, None).unwrap();
    assert_eq!(model.inspect_header(&lease, 101).unwrap().sequence, 7);
    let out_3 = model
        .finish_write(
            &lease,
            101,
            &mut handle_3,
            ReleaseOnlyCleanupProof::completed(),
            WriteTerminalKind::CancelledAfterActivation,
        )
        .unwrap();
    assert_eq!(out_3.new_stamp.sequence, 8);
    assert_eq!(
        out_3.mutation_accounting,
        MutationAccounting::UnknownOrPartial
    );

    arbiter.release_lease(&mut lease, 101).unwrap();
}

#[test]
fn foreign_or_stale_write_handle_cannot_finish_or_clear_new_writer() {
    let ns = test_namespace();
    let mut arbiter = DesktopMutexArbiter::new(ns.clone());
    let nonce = EpochNonce::from_host_csprng(0x5555_6666_7777_8888_9999_aaaa_bbbb_cccc).unwrap();

    // Worker A runs write 2 -> 3 -> 4; a second local view captures an unfinished handle at odd=3
    // without requiring Clone on ActiveWriteHandle.
    let mut lease_a = arbiter.acquire_for_mutation(101, "a_write").unwrap();
    let mut model = SharedEpochModel::create_new(ns.clone(), &lease_a, 101, nonce).unwrap();
    let mut stale_view_a =
        SharedEpochModel::open_existing(ns, &lease_a, 101, model.raw_page_bytes()).unwrap();
    let writer_a = writer_identity(1001, 50_000_001, 0x1111, 0xa1);
    let stamp_2 = SharedEpochStamp {
        mapping_nonce: nonce,
        sequence: 2,
    };
    let stale_ticket_a = stale_view_a
        .prepare_write(&mut lease_a, 101, stamp_2, writer_a, None)
        .unwrap();
    let mut stale_unmarked_handle_a = stale_view_a
        .begin_write(&lease_a, 101, stale_ticket_a, None)
        .unwrap();

    let ticket_a = model
        .prepare_write(&mut lease_a, 101, stamp_2, writer_a, None)
        .unwrap();
    let mut handle_a = model.begin_write(&lease_a, 101, ticket_a, None).unwrap();
    let out_a = model
        .finish_write(
            &lease_a,
            101,
            &mut handle_a,
            ReleaseOnlyCleanupProof::completed(),
            WriteTerminalKind::Succeeded,
        )
        .unwrap();
    arbiter.release_lease(&mut lease_a, 101).unwrap();

    // Worker B starts write 4 -> 5.
    let mut lease_b = arbiter.acquire_for_mutation(202, "b_write").unwrap();
    let writer_b = writer_identity(2002, 50_000_002, 0x2222, 0xb2);
    let ticket_b = model
        .prepare_write(&mut lease_b, 202, out_a.new_stamp, writer_b, None)
        .unwrap();
    let mut handle_b = model.begin_write(&lease_b, 202, ticket_b, None).unwrap();
    assert_eq!(handle_b.odd_sequence(), 5);

    // Unfinished stale handle from Worker A must be rejected and MUST NOT clear Worker B's odd state.
    assert_eq!(
        model
            .finish_write(
                &lease_b,
                202,
                &mut stale_unmarked_handle_a,
                ReleaseOnlyCleanupProof::completed(),
                WriteTerminalKind::Succeeded,
            )
            .unwrap_err(),
        SharedEpochError::ForeignOrStaleWriteHandle
    );

    // Already-finished handle_a is also rejected.
    assert_eq!(
        model
            .finish_write(
                &lease_b,
                202,
                &mut handle_a,
                ReleaseOnlyCleanupProof::completed(),
                WriteTerminalKind::Succeeded,
            )
            .unwrap_err(),
        SharedEpochError::ForeignOrStaleWriteHandle
    );

    // Even if another writer C overwrote the raw header with odd=5 but C's identity, Worker B's handle
    // detects the writer identity mismatch and refuses to clear C's active writer.
    let writer_c = writer_identity(3003, 50_000_003, 0x3333, 0xc3);
    let forged_c_header = SharedEpochHeader {
        schema_version: 1,
        struct_bytes: SHARED_EPOCH_HEADER_BYTES as u32,
        mapping_nonce: nonce,
        sequence: 5,
        active_writer: Some(writer_c),
    }
    .encode()
    .unwrap();
    model.overwrite_raw_header_for_test(0, &forged_c_header);
    assert_eq!(
        model
            .finish_write(
                &lease_b,
                202,
                &mut handle_b,
                ReleaseOnlyCleanupProof::completed(),
                WriteTerminalKind::Succeeded,
            )
            .unwrap_err(),
        SharedEpochError::ForeignOrStaleWriteHandle
    );
    assert_eq!(
        model.inspect_header(&lease_b, 202).unwrap().active_writer,
        Some(writer_c)
    );

    // Restore Worker B's genuine header and finish cleanly to even=6.
    let genuine_b_header = SharedEpochHeader {
        schema_version: 1,
        struct_bytes: SHARED_EPOCH_HEADER_BYTES as u32,
        mapping_nonce: nonce,
        sequence: 5,
        active_writer: Some(writer_b),
    }
    .encode()
    .unwrap();
    model.overwrite_raw_header_for_test(0, &genuine_b_header);

    let out_b = model
        .finish_write(
            &lease_b,
            202,
            &mut handle_b,
            ReleaseOnlyCleanupProof::completed(),
            WriteTerminalKind::Succeeded,
        )
        .unwrap();
    assert_eq!(out_b.new_stamp.sequence, 6);

    // Calling finish_write a second time on already-finished handle_b is rejected.
    assert_eq!(
        model
            .finish_write(
                &lease_b,
                202,
                &mut handle_b,
                ReleaseOnlyCleanupProof::completed(),
                WriteTerminalKind::Succeeded,
            )
            .unwrap_err(),
        SharedEpochError::ForeignOrStaleWriteHandle
    );
    arbiter.release_lease(&mut lease_b, 202).unwrap();
}

#[test]
fn duplicate_prepare_and_cancelled_stale_ticket_cannot_resurrect_or_control_new_writer() {
    let ns = test_namespace();
    let mut arbiter = DesktopMutexArbiter::new(ns.clone());
    let nonce = EpochNonce::from_host_csprng(0x1234_5678_9abc_def0_1357_9bdf_2468_ace0).unwrap();

    let mut lease = arbiter
        .acquire_for_mutation(101, "single_ready_slot")
        .unwrap();
    let mut model = SharedEpochModel::create_new(ns, &lease, 101, nonce).unwrap();
    let stamp_2 = SharedEpochStamp {
        mapping_nonce: nonce,
        sequence: 2,
    };
    let writer_1 = writer_identity(1001, 70_000_001, 0x1010, 0x10);
    let writer_2 = writer_identity(1001, 70_000_001, 0x2020, 0x20);

    // 1. Prepare ticket_1 -> active prepared slot is occupied.
    let ticket_1 = model
        .prepare_write(&mut lease, 101, stamp_2, writer_1, None)
        .unwrap();
    assert!(model.has_active_prepared_ticket());
    assert_eq!(model.active_prepared_ticket_id(), Some(ticket_1.ticket_id()));

    // 2. Attempting a second prepare_write while ticket_1 is still ready under the same lease
    // MUST be rejected with ReadyTicketAlreadyActive (cannot mint two concurrent ready tickets).
    assert_eq!(
        model
            .prepare_write(&mut lease, 101, stamp_2, writer_2, None)
            .unwrap_err(),
        SharedEpochError::ReadyTicketAlreadyActive {
            active_ticket_id: ticket_1.ticket_id(),
        }
    );

    // 3. Cancel active ready slot without consuming ticket_1 value (simulating adapter timeout/cancel
    // before a caller attempts to use a retained ticket_1): sequence remains 2 and ticket_1 is dead.
    let cancelled_stamp = model.cancel_active_ready_slot(&lease, 101).unwrap();
    assert_eq!(cancelled_stamp, stamp_2);
    assert!(!model.has_active_prepared_ticket());
    assert_eq!(model.inspect_header(&lease, 101).unwrap().sequence, 2);

    // 4. Prepare a new ticket_2 under the same lease; it gets a fresh monotonic ticket_id.
    let ticket_2 = model
        .prepare_write(&mut lease, 101, stamp_2, writer_2, None)
        .unwrap();
    assert_ne!(ticket_2.ticket_id(), ticket_1.ticket_id());
    assert_eq!(model.active_prepared_ticket_id(), Some(ticket_2.ticket_id()));

    // 5. Stale ticket_1 cannot cancel ticket_2 and cannot begin_write!
    assert_eq!(
        model
            .cancel_or_expire_ready(&lease, 101, ticket_1)
            .unwrap_err(),
        SharedEpochError::StaleOrInactivePreparedTicket
    );
    // ticket_2 remains the active ready ticket after stale cancel refusal.
    assert_eq!(model.active_prepared_ticket_id(), Some(ticket_2.ticket_id()));

    // Now cancel ticket_2 via cancel_or_expire_ready, then prepare ticket_3 and verify stale ticket
    // cannot begin_write while ticket_3 is active or after cancellation.
    let cancelled_2 = model
        .cancel_or_expire_ready(&lease, 101, ticket_2)
        .unwrap();
    assert_eq!(cancelled_2, stamp_2);
    assert!(!model.has_active_prepared_ticket());

    let ticket_3 = model
        .prepare_write(&mut lease, 101, stamp_2, writer_1, None)
        .unwrap();
    let _ = model.cancel_active_ready_slot(&lease, 101).unwrap();
    assert_eq!(
        model.begin_write(&lease, 101, ticket_3, None).unwrap_err(),
        SharedEpochError::StaleOrInactivePreparedTicket
    );
    assert_eq!(model.inspect_header(&lease, 101).unwrap().sequence, 2);

    // 6. Fresh ticket_4 still commits cleanly 2 -> 3 -> 4.
    let ticket_4 = model
        .prepare_write(&mut lease, 101, stamp_2, writer_2, None)
        .unwrap();
    let mut handle_4 = model.begin_write(&lease, 101, ticket_4, None).unwrap();
    assert!(!model.has_active_prepared_ticket());
    assert_eq!(handle_4.odd_sequence(), 3);
    let out_4 = model
        .finish_write(
            &lease,
            101,
            &mut handle_4,
            ReleaseOnlyCleanupProof::completed(),
            WriteTerminalKind::Succeeded,
        )
        .unwrap();
    assert_eq!(out_4.new_stamp.sequence, 4);

    arbiter.release_lease(&mut lease, 101).unwrap();
}

#[test]
fn abandoned_mutex_and_orphan_odd_recovery_rotate_fresh_nonce_and_reject_old_stamp() {
    let ns = test_namespace();
    let mut arbiter = DesktopMutexArbiter::new(ns.clone());
    let nonce_1 = EpochNonce::from_host_csprng(0x6666_7777_8888_9999_aaaa_bbbb_cccc_dddd).unwrap();
    let nonce_2 = EpochNonce::from_host_csprng(0x7777_8888_9999_aaaa_bbbb_cccc_dddd_eeee).unwrap();
    let nonce_3 = EpochNonce::from_host_csprng(0x8888_9999_aaaa_bbbb_cccc_dddd_eeee_ffff).unwrap();

    // Worker A starts write at odd=3 and is hard-killed while holding the mutex.
    let mut lease_a = arbiter.acquire_for_mutation(101, "a_crash").unwrap();
    let mut model = SharedEpochModel::create_new(ns.clone(), &lease_a, 101, nonce_1).unwrap();
    let stamp_2_v1 = SharedEpochStamp {
        mapping_nonce: nonce_1,
        sequence: 2,
    };
    let writer_a = writer_identity(1001, 60_000_001, 0xaaaa, 0x5a);
    let ticket_a = model
        .prepare_write(&mut lease_a, 101, stamp_2_v1, writer_a, None)
        .unwrap();
    let _handle_a = model.begin_write(&lease_a, 101, ticket_a, None).unwrap();
    assert_eq!(model.inspect_header(&lease_a, 101).unwrap().sequence, 3);

    arbiter.simulate_holder_hard_kill(lease_a).unwrap();

    // Worker B acquires mutex and observes WAIT_ABANDONED.
    let mut lease_b = arbiter.acquire_for_mutation(202, "b_prepare_after_crash").unwrap();
    assert_eq!(
        lease_b.acquisition_disposition(),
        AcquisitionDisposition::Abandoned
    );

    // Observation on abandoned lease is refused.
    assert_eq!(
        model.sample_before_observation(&lease_b, 202).unwrap_err(),
        SharedEpochError::AbandonedRecoveryRequired
    );

    // Recovery reusing the same nonce_1 is refused.
    let writer_b = writer_identity(2002, 60_000_002, 0xbbbb, 0x5b);
    assert_eq!(
        model
            .prepare_write(&mut lease_b, 202, stamp_2_v1, writer_b, Some(nonce_1))
            .unwrap_err(),
        SharedEpochError::NonceReuseDisallowed
    );

    // Valid recovery rotates to nonce_2 (even sequence 2), clears writer_a, AND rejects Worker B's old request!
    let recovery_err = model
        .prepare_write(&mut lease_b, 202, stamp_2_v1, writer_b, Some(nonce_2))
        .unwrap_err();
    match recovery_err {
        SharedEpochError::RecoveredButRejectOldRequest(report) => {
            assert_eq!(report.decision, RecoveryDecision::RecoveredButRejectOldRequest);
            assert_eq!(report.previous_nonce, nonce_1);
            assert_eq!(report.previous_sequence, 3);
            assert_eq!(report.interrupted_writer, Some(writer_a));
            assert_eq!(report.new_stamp.mapping_nonce, nonce_2);
            assert_eq!(report.new_stamp.sequence, INITIAL_EVEN_SEQUENCE);
        }
        other => panic!("unexpected error: {:?}", other),
    }

    // Header is now healthy/even under nonce_2, and old stamp_2_v1 is rejected with EpochNonceRotated.
    let recovered_header = model.inspect_header(&lease_b, 202).unwrap();
    assert_eq!(recovered_header.mapping_nonce, nonce_2);
    assert_eq!(recovered_header.sequence, 2);
    assert_eq!(recovered_header.active_writer, None);

    assert_eq!(
        model
            .prepare_write(&mut lease_b, 202, stamp_2_v1, writer_b, None)
            .unwrap_err(),
        SharedEpochError::EpochNonceRotated {
            expected: nonce_1,
            actual: nonce_2,
        }
    );
    arbiter.release_lease(&mut lease_b, 202).unwrap();

    // Simulate all workers exiting and a brand-new mapping created later with nonce_3.
    let mut lease_c = arbiter.acquire_for_mutation(303, "c_new_mapping").unwrap();
    let mut new_mapping_model =
        SharedEpochModel::create_new(ns, &lease_c, 303, nonce_3).unwrap();
    let stamp_2_v2 = SharedEpochStamp {
        mapping_nonce: nonce_2,
        sequence: 2,
    };
    assert_eq!(
        new_mapping_model
            .prepare_write(&mut lease_c, 303, stamp_2_v2, writer_b, None)
            .unwrap_err(),
        SharedEpochError::EpochNonceRotated {
            expected: nonce_2,
            actual: nonce_3,
        }
    );
    arbiter.release_lease(&mut lease_c, 303).unwrap();
}

#[test]
fn corrupted_or_unknown_existing_header_is_refused_and_never_reset_as_new() {
    let ns = test_namespace();
    let mut arbiter = DesktopMutexArbiter::new(ns.clone());
    let nonce = EpochNonce::from_host_csprng(0x9999_aaaa_bbbb_cccc_dddd_eeee_ffff_1111).unwrap();

    let mut lease = arbiter.acquire_for_mutation(101, "corrupt_checks").unwrap();

    // 1. All-zero existing mapping page must NOT be treated as initializable; fails with BadMagic.
    let zero_page = [0u8; SHARED_EPOCH_PAGE_BYTES];
    assert_eq!(
        SharedEpochModel::open_existing(ns.clone(), &lease, 101, &zero_page).unwrap_err(),
        SharedEpochError::CorruptedHeader(CorruptedHeaderReason::BadMagic)
    );

    // 2. Wrong mapping size is rejected.
    assert_eq!(
        SharedEpochModel::open_existing(ns.clone(), &lease, 101, &[0u8; 256]).unwrap_err(),
        SharedEpochError::InvalidMappingSize {
            actual: 256,
            expected: SHARED_EPOCH_PAGE_BYTES,
        }
    );

    // Start from a valid page image and test each corruption vector.
    let valid_model = SharedEpochModel::create_new(ns.clone(), &lease, 101, nonce).unwrap();
    let valid_page = *valid_model.raw_page_bytes();

    // 3. Unknown schema version -> UnsupportedSchemaVersion.
    let mut bad_schema = valid_page;
    bad_schema[8..12].copy_from_slice(&99u32.to_le_bytes());
    assert_eq!(
        SharedEpochModel::open_existing(ns.clone(), &lease, 101, &bad_schema).unwrap_err(),
        SharedEpochError::UnsupportedSchemaVersion(99)
    );

    // 4. Non-zero header_reserved -> NonZeroReservedFields.
    let mut bad_reserved = valid_page;
    bad_reserved[16] = 1;
    assert_eq!(
        SharedEpochModel::open_existing(ns.clone(), &lease, 101, &bad_reserved).unwrap_err(),
        SharedEpochError::CorruptedHeader(CorruptedHeaderReason::NonZeroReservedFields)
    );

    // 5. Even sequence with non-zero writer PID -> EvenSequenceHasDirtyWriterFields.
    let mut dirty_even = valid_page;
    dirty_even[48..52].copy_from_slice(&1234u32.to_le_bytes());
    assert_eq!(
        SharedEpochModel::open_existing(ns.clone(), &lease, 101, &dirty_even).unwrap_err(),
        SharedEpochError::CorruptedHeader(
            CorruptedHeaderReason::EvenSequenceHasDirtyWriterFields
        )
    );

    // 6. Odd sequence with missing writer identity -> OddSequenceMissingWriterIdentity.
    let mut bare_odd = valid_page;
    bare_odd[40..48].copy_from_slice(&3u64.to_le_bytes());
    assert_eq!(
        SharedEpochModel::open_existing(ns.clone(), &lease, 101, &bare_odd).unwrap_err(),
        SharedEpochError::CorruptedHeader(
            CorruptedHeaderReason::OddSequenceMissingWriterIdentity
        )
    );

    // 7. In-flight header corruption quarantines the model and refuses recovery.
    let mut live_model = SharedEpochModel::open_existing(ns, &lease, 101, &valid_page).unwrap();
    live_model.overwrite_raw_header_for_test(0, b"CORRUPT!");
    let fresh_nonce =
        EpochNonce::from_host_csprng(0xaaaa_bbbb_cccc_dddd_eeee_ffff_0000_1111).unwrap();
    assert_eq!(
        live_model
            .recover_abandoned_or_orphan(&mut lease, 101, fresh_nonce)
            .unwrap_err(),
        SharedEpochError::CorruptedHeader(CorruptedHeaderReason::BadMagic)
    );
    assert!(live_model.is_quarantined());

    arbiter.release_lease(&mut lease, 101).unwrap();
}

#[test]
fn sequence_overflow_preflight_rotates_nonce_and_never_publishes_odd_without_next_even() {
    let ns = test_namespace();
    let mut arbiter = DesktopMutexArbiter::new(ns.clone());
    let nonce_initial =
        EpochNonce::from_host_csprng(0xaaaa_1111_bbbb_2222_cccc_3333_dddd_4444).unwrap();
    let nonce_rotated =
        EpochNonce::from_host_csprng(0xeeee_5555_ffff_6666_1111_7777_2222_8888).unwrap();

    let mut lease = arbiter.acquire_for_mutation(101, "overflow_preflight").unwrap();
    // Seed at u64::MAX - 1 (even = 18446744073709551614). Next odd would be u64::MAX, which has NO next even!
    let near_max_even = u64::MAX - 1;
    let mut model = SharedEpochModel::create_new_with_even_sequence(
        ns.clone(),
        &lease,
        101,
        nonce_initial,
        near_max_even,
    )
    .unwrap();

    let old_stamp = SharedEpochStamp {
        mapping_nonce: nonce_initial,
        sequence: near_max_even,
    };
    let writer = writer_identity(1001, 40_000_001, 0x9999, 0x7f);

    let ticket_without_nonce = model
        .prepare_write(&mut lease, 101, old_stamp, writer, None)
        .unwrap();
    // Without a fresh rotation nonce, begin_write refuses and leaves sequence at near_max_even (never odd!).
    assert_eq!(
        model
            .begin_write(&lease, 101, ticket_without_nonce, None)
            .unwrap_err(),
        SharedEpochError::SequenceOverflowRotationRequired {
            current_sequence: near_max_even,
        }
    );
    assert_eq!(
        model.inspect_header(&lease, 101).unwrap().sequence,
        near_max_even
    );

    // With a fresh rotation nonce, begin_write rotates to (nonce_rotated, sequence = 2) and rejects old_stamp.
    let ticket_with_rotation = model
        .prepare_write(&mut lease, 101, old_stamp, writer, None)
        .unwrap();
    let rot_err = model
        .begin_write(&lease, 101, ticket_with_rotation, Some(nonce_rotated))
        .unwrap_err();
    assert_eq!(
        rot_err,
        SharedEpochError::SequenceOverflowRotatedRejectOldStamp {
            previous_nonce: nonce_initial,
            previous_sequence: near_max_even,
            new_stamp: SharedEpochStamp {
                mapping_nonce: nonce_rotated,
                sequence: INITIAL_EVEN_SEQUENCE,
            },
        }
    );

    let header_after = model.inspect_header(&lease, 101).unwrap();
    assert_eq!(header_after.mapping_nonce, nonce_rotated);
    assert_eq!(header_after.sequence, INITIAL_EVEN_SEQUENCE);
    assert_eq!(header_after.active_writer, None);

    // Also verify lock identity mismatch and wrong owning thread checks fail closed.
    assert_eq!(
        model.inspect_header(&lease, 999).unwrap_err(),
        SharedEpochError::WrongOwningThread {
            expected_thread_id: 101,
            actual_thread_id: 999,
        }
    );

    let other_ns =
        DesktopLockNamespace::new("S-1-5-21-1000-2000-3000-1001", 2, "fedcba9876543210").unwrap();
    let mut other_arbiter = DesktopMutexArbiter::new(other_ns);
    let mut other_lease = other_arbiter.acquire_for_observation(101, "other_ns").unwrap();
    assert_eq!(
        model.inspect_header(&other_lease, 101).unwrap_err(),
        SharedEpochError::LockIdentityMismatch
    );
    other_arbiter.release_lease(&mut other_lease, 101).unwrap();

    arbiter.release_lease(&mut lease, 101).unwrap();
}
