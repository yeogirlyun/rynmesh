use ryn_computer_use::errors::{
    DiagnosticReason, ErrorCode, ErrorStage, InternalMutationDetail, MutationState,
};
use ryn_computer_use::focus::{
    ensure_window_focused_with_backend, FocusExecutionBackend, DEFAULT_FOCUS_DEADLINE_MS,
};
use ryn_computer_use::windows::{
    evaluate_directional_window_relation, u64_to_hwnd, WindowRelationDecision, WindowRelationNode,
};
use std::collections::BTreeSet;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use windows::Win32::Foundation::HWND;

#[derive(Debug, Clone, PartialEq, Eq)]
enum RecordedFocusCall {
    ShowWindowRestore(u64),
    BringWindowToTop(u64),
    SetForegroundWindow(u64),
    SetFocus(u64),
    SwitchToThisWindow(u64),
    AttachThreadInput {
        from_tid: u32,
        to_tid: u32,
        attach: bool,
        result: bool,
    },
}

struct DeterministicFocusBackend {
    now_instant: Instant,
    worker_tid: u32,
    target_hwnd: u64,
    root_hwnd: u64,
    extra_valid_hwnds: BTreeSet<u64>,
    target_pid: u32,
    target_tid: u32,
    foreground_hwnd: u64,
    foreground_pid: u32,
    foreground_tid: u32,
    target_is_window: bool,
    root_is_window: bool,
    target_identity_valid: bool,
    minimized: bool,
    foreground_is_related: bool,
    relation_graph: Vec<WindowRelationNode>,
    allow_fg_attach: bool,
    allow_target_attach: bool,
    succeed_on_method: Option<&'static str>,
    foreground_on_step: Option<(&'static str, u64)>,
    change_root_on_step: Option<(&'static str, u64)>,
    cancel_on_step: Option<&'static str>,
    invalidate_window_on_step: Option<&'static str>,
    invalidate_identity_on_step: Option<&'static str>,
    cancel_flag: Arc<AtomicBool>,
    calls: Vec<RecordedFocusCall>,
    attach_history: Vec<(u32, u32, bool, bool)>,
    active_attachments: BTreeSet<(u32, u32)>,
}

impl DeterministicFocusBackend {
    fn new(cancel_flag: Arc<AtomicBool>) -> Self {
        Self {
            now_instant: Instant::now(),
            worker_tid: 1001,
            target_hwnd: 0x1100,
            root_hwnd: 0x1100,
            extra_valid_hwnds: BTreeSet::new(),
            target_pid: 4000,
            target_tid: 4001,
            foreground_hwnd: 0x9900,
            foreground_pid: 8000,
            foreground_tid: 8001,
            target_is_window: true,
            root_is_window: true,
            target_identity_valid: true,
            minimized: false,
            foreground_is_related: false,
            relation_graph: Vec::new(),
            allow_fg_attach: true,
            allow_target_attach: true,
            succeed_on_method: None,
            foreground_on_step: None,
            change_root_on_step: None,
            cancel_on_step: None,
            invalidate_window_on_step: None,
            invalidate_identity_on_step: None,
            cancel_flag,
            calls: Vec::new(),
            attach_history: Vec::new(),
            active_attachments: BTreeSet::new(),
        }
    }

    fn trigger_step_faults(&mut self, step_name: &str) {
        if self.cancel_on_step == Some(step_name) {
            self.cancel_flag.store(true, Ordering::SeqCst);
        }
        if self.invalidate_window_on_step == Some(step_name) {
            self.target_is_window = false;
            self.root_is_window = false;
        }
        if self.invalidate_identity_on_step == Some(step_name) {
            self.target_identity_valid = false;
        }
        if let Some((trigger_step, new_root)) = self.change_root_on_step {
            if trigger_step == step_name {
                self.extra_valid_hwnds.insert(self.root_hwnd);
                self.extra_valid_hwnds.insert(new_root);
                self.root_hwnd = new_root;
            }
        }
        if let Some((trigger_step, new_fg)) = self.foreground_on_step {
            if trigger_step == step_name {
                self.foreground_hwnd = new_fg;
            }
        }
        if self.succeed_on_method == Some(step_name) {
            self.foreground_hwnd = self.root_hwnd;
            self.foreground_pid = self.target_pid;
            self.foreground_tid = self.target_tid;
            self.foreground_is_related = true;
        }
    }
}

impl FocusExecutionBackend for DeterministicFocusBackend {
    fn now(&mut self) -> Instant {
        self.now_instant
    }

    fn sleep(&mut self, duration: Duration) {
        self.now_instant += duration;
        self.trigger_step_faults("during_wait_sleep");
    }

    fn is_window(&mut self, hwnd: HWND) -> bool {
        let val = hwnd.0 as usize as u64;
        if val == self.target_hwnd {
            self.target_is_window
        } else if val == self.root_hwnd {
            self.root_is_window
        } else {
            self.extra_valid_hwnds.contains(&val)
        }
    }

    fn get_root_window(&mut self, _hwnd: HWND) -> HWND {
        u64_to_hwnd(self.root_hwnd)
    }

    fn is_minimized(&mut self, _hwnd: HWND) -> bool {
        self.minimized
    }

    fn verify_target_identity(&mut self, root_hwnd: HWND, target_hwnd: HWND) -> bool {
        let r = root_hwnd.0 as usize as u64;
        let t = target_hwnd.0 as usize as u64;
        self.target_identity_valid && r == self.root_hwnd && t == self.target_hwnd
    }

    fn verify_foreground_relation(&mut self, target_hwnd: HWND) -> (bool, HWND) {
        if !self.relation_graph.is_empty() {
            let targ_u64 = target_hwnd.0 as usize as u64;
            let decision =
                evaluate_directional_window_relation(self.foreground_hwnd, targ_u64, |hwnd| {
                    self.relation_graph.iter().find(|n| n.hwnd == hwnd).copied()
                });
            return (decision.is_allowed(), u64_to_hwnd(self.foreground_hwnd));
        }
        (
            self.foreground_is_related,
            u64_to_hwnd(self.foreground_hwnd),
        )
    }

    fn current_thread_id(&mut self) -> u32 {
        self.worker_tid
    }

    fn window_pid_tid(&mut self, hwnd: HWND) -> (u32, u32) {
        let val = hwnd.0 as usize as u64;
        if val == self.root_hwnd || val == self.target_hwnd {
            (self.target_pid, self.target_tid)
        } else if val == self.foreground_hwnd {
            (self.foreground_pid, self.foreground_tid)
        } else {
            (0, 0)
        }
    }

    fn show_window_restore(&mut self, root_hwnd: HWND) -> bool {
        let val = root_hwnd.0 as usize as u64;
        self.minimized = false;
        self.calls.push(RecordedFocusCall::ShowWindowRestore(val));
        self.trigger_step_faults("show_window_restore");
        true
    }

    fn bring_window_to_top(&mut self, root_hwnd: HWND) -> bool {
        let val = root_hwnd.0 as usize as u64;
        self.calls.push(RecordedFocusCall::BringWindowToTop(val));
        true
    }

    fn set_foreground_window(&mut self, root_hwnd: HWND) -> bool {
        let val = root_hwnd.0 as usize as u64;
        self.calls.push(RecordedFocusCall::SetForegroundWindow(val));
        // Determine whether we are in Step 3 (`set_foreground_window`), Step 4 (`attach_thread_input`),
        // or Step 5 (`switch_to_this_window`) based on preceding call state.
        if !self.active_attachments.is_empty() {
            self.trigger_step_faults("attach_thread_input");
        } else if self
            .calls
            .iter()
            .any(|c| matches!(c, RecordedFocusCall::SwitchToThisWindow(_)))
        {
            self.trigger_step_faults("switch_to_this_window");
        } else {
            self.trigger_step_faults("set_foreground_window");
        }
        true
    }

    fn set_focus(&mut self, root_hwnd: HWND) -> bool {
        let val = root_hwnd.0 as usize as u64;
        self.calls.push(RecordedFocusCall::SetFocus(val));
        true
    }

    fn switch_to_this_window(&mut self, root_hwnd: HWND) {
        let val = root_hwnd.0 as usize as u64;
        self.calls.push(RecordedFocusCall::SwitchToThisWindow(val));
    }

    fn attach_thread_input(&mut self, from_tid: u32, to_tid: u32, attach: bool) -> bool {
        let result = if attach {
            let allowed = if to_tid == self.foreground_tid {
                self.allow_fg_attach
            } else if to_tid == self.target_tid {
                self.allow_target_attach
            } else {
                false
            };
            if allowed {
                self.active_attachments.insert((from_tid, to_tid));
            }
            if to_tid == self.target_tid || !self.allow_target_attach {
                self.trigger_step_faults("inside_attach_scope");
            }
            allowed
        } else {
            let was_present = self.active_attachments.remove(&(from_tid, to_tid));
            assert!(
                was_present,
                "Attempted to detach thread pair ({}, {}) that was not attached",
                from_tid, to_tid
            );
            true
        };
        self.attach_history
            .push((from_tid, to_tid, attach, result));
        self.calls.push(RecordedFocusCall::AttachThreadInput {
            from_tid,
            to_tid,
            attach,
            result,
        });
        result
    }
}

#[test]
fn test_focus_all_recovery_methods_fail_closed_without_synthetic_input_to_unrelated_foreground() {
    let cancel_flag = Arc::new(AtomicBool::new(false));
    let mut backend = DeterministicFocusBackend::new(cancel_flag.clone());
    backend.minimized = true;

    let target = u64_to_hwnd(backend.target_hwnd);
    let err = ensure_window_focused_with_backend(
        &mut backend,
        target,
        true,
        DEFAULT_FOCUS_DEADLINE_MS,
        &cancel_flag,
    )
    .expect_err("When unrelated window retains foreground, focus recovery must fail closed");

    assert_eq!(err.code, ErrorCode::FocusFailed);
    assert_eq!(err.stage, ErrorStage::Activating);
    assert_eq!(
        err.mutation,
        MutationState::Possible,
        "Attempted window activation must monotonically report mutation=possible"
    );
    assert!(
        !err.retryable,
        "ProtocolError must keep retryable=false under no-auto-retry policy"
    );
    let details = err
        .details
        .expect("FocusFailed must include diagnostic details");
    assert_eq!(
        details.internal_mutation,
        Some(InternalMutationDetail::FocusOnly)
    );
    assert_eq!(
        details.reason,
        Some(DiagnosticReason::ForegroundVerificationFailed)
    );
    assert_eq!(
        details.target_hwnd.as_deref(),
        Some("4352") // 0x1100
    );
    assert_eq!(
        details.foreground_hwnd.as_deref(),
        Some("39168") // 0x9900 (unrelated foreground)
    );

    // Every non-key focus step targeted ONLY our verified target HWND (0x1100), never 0x9900,
    // and no synthetic keyboard/mouse SendInput step exists.
    assert_eq!(
        backend.calls,
        vec![
            // Step 2: show_window_restore
            RecordedFocusCall::ShowWindowRestore(0x1100),
            // Step 3: set_foreground_window
            RecordedFocusCall::BringWindowToTop(0x1100),
            RecordedFocusCall::SetForegroundWindow(0x1100),
            // Step 4: attach_thread_input (attach -> bring/setfg/setfocus -> reverse-order detach)
            RecordedFocusCall::AttachThreadInput {
                from_tid: 1001,
                to_tid: 8001,
                attach: true,
                result: true,
            },
            RecordedFocusCall::AttachThreadInput {
                from_tid: 1001,
                to_tid: 4001,
                attach: true,
                result: true,
            },
            RecordedFocusCall::BringWindowToTop(0x1100),
            RecordedFocusCall::SetForegroundWindow(0x1100),
            RecordedFocusCall::SetFocus(0x1100),
            RecordedFocusCall::AttachThreadInput {
                from_tid: 1001,
                to_tid: 4001,
                attach: false,
                result: true,
            },
            RecordedFocusCall::AttachThreadInput {
                from_tid: 1001,
                to_tid: 8001,
                attach: false,
                result: true,
            },
            // Step 5: switch_to_this_window
            RecordedFocusCall::SwitchToThisWindow(0x1100),
            RecordedFocusCall::SetForegroundWindow(0x1100),
        ]
    );
    assert!(
        backend.active_attachments.is_empty(),
        "All thread attachments must be released before ensure_window_focused returns"
    );
}

#[test]
fn test_focus_partial_thread_attachment_and_mid_attach_cancellation_or_target_loss_always_detach() {
    // Case A: Partial attachment where fg_tid attach succeeds but target_tid attach fails
    {
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let mut backend = DeterministicFocusBackend::new(cancel_flag.clone());
        backend.allow_fg_attach = true;
        backend.allow_target_attach = false;

        let target = u64_to_hwnd(backend.target_hwnd);
        let err = ensure_window_focused_with_backend(
            &mut backend,
            target,
            false,
            DEFAULT_FOCUS_DEADLINE_MS,
            &cancel_flag,
        )
        .unwrap_err();
        assert_eq!(err.code, ErrorCode::FocusFailed);
        assert_eq!(err.stage, ErrorStage::Activating);
        assert_eq!(err.mutation, MutationState::Possible);
        assert!(!err.retryable);
        assert!(backend.active_attachments.is_empty());
        assert_eq!(
            backend.attach_history,
            vec![
                (1001, 8001, true, true),
                (1001, 4001, true, false),
                (1001, 8001, false, true),
            ]
        );
    }

    // Case B: Both attachments succeed, then cancellation triggers inside attached scope
    // before BringWindowToTop/SetForegroundWindow/SetFocus in Step 4
    {
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let mut backend = DeterministicFocusBackend::new(cancel_flag.clone());
        backend.cancel_on_step = Some("inside_attach_scope");

        let target = u64_to_hwnd(backend.target_hwnd);
        let err = ensure_window_focused_with_backend(
            &mut backend,
            target,
            false,
            DEFAULT_FOCUS_DEADLINE_MS,
            &cancel_flag,
        )
        .unwrap_err();
        assert_eq!(err.code, ErrorCode::Cancelled);
        assert_eq!(err.stage, ErrorStage::Activating);
        assert_eq!(
            err.mutation,
            MutationState::Possible,
            "Cancellation after Step 3 / thread attachment must preserve possible mutation"
        );
        assert!(!err.retryable);
        let details = err.details.expect("Cancelled error must carry details");
        assert_eq!(
            details.internal_mutation,
            Some(InternalMutationDetail::FocusOnly)
        );
        assert_eq!(
            details.reason,
            Some(DiagnosticReason::CancelledDuringExecution)
        );
        assert!(
            backend.active_attachments.is_empty(),
            "RAII guard must detach both threads on early cancellation return"
        );
        assert_eq!(
            backend.attach_history,
            vec![
                (1001, 8001, true, true),
                (1001, 4001, true, true),
                (1001, 4001, false, true),
                (1001, 8001, false, true),
            ]
        );
        // SetFocus in Step 4 and SwitchToThisWindow in Step 5 must NOT have been called
        assert!(!backend
            .calls
            .iter()
            .any(|c| matches!(c, RecordedFocusCall::SetFocus(_))));
        assert!(!backend
            .calls
            .iter()
            .any(|c| matches!(c, RecordedFocusCall::SwitchToThisWindow(_))));
    }

    // Case C: Target identity becomes stale inside attached scope -> detaches both and fails closed
    {
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let mut backend = DeterministicFocusBackend::new(cancel_flag.clone());
        backend.invalidate_identity_on_step = Some("inside_attach_scope");

        let target = u64_to_hwnd(backend.target_hwnd);
        let err = ensure_window_focused_with_backend(
            &mut backend,
            target,
            false,
            DEFAULT_FOCUS_DEADLINE_MS,
            &cancel_flag,
        )
        .unwrap_err();
        assert_eq!(err.code, ErrorCode::WindowStale);
        assert_eq!(err.stage, ErrorStage::Activating);
        assert_eq!(err.mutation, MutationState::Possible);
        assert!(!err.retryable);
        let details = err.details.expect("WindowStale error must carry details");
        assert_eq!(
            details.internal_mutation,
            Some(InternalMutationDetail::FocusOnly)
        );
        assert_eq!(details.reason, Some(DiagnosticReason::GenerationMismatch));
        assert!(backend.active_attachments.is_empty());
    }

    // Case D: fg_tid == target_tid (e.g. non-related sibling popup on same thread) -> attaches & detaches once
    {
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let mut backend = DeterministicFocusBackend::new(cancel_flag.clone());
        backend.foreground_tid = backend.target_tid;

        let target = u64_to_hwnd(backend.target_hwnd);
        let err = ensure_window_focused_with_backend(
            &mut backend,
            target,
            false,
            DEFAULT_FOCUS_DEADLINE_MS,
            &cancel_flag,
        )
        .unwrap_err();
        assert_eq!(err.code, ErrorCode::FocusFailed);
        assert_eq!(err.stage, ErrorStage::Activating);
        assert!(!err.retryable);
        assert!(backend.active_attachments.is_empty());
        assert_eq!(
            backend.attach_history,
            vec![(1001, 4001, true, true), (1001, 4001, false, true)]
        );
    }
}

#[test]
fn test_focus_cancellation_and_target_identity_gates_before_and_between_every_step() {
    // 1. Invalid target window before Step 1 -> WindowNotFound with MutationState::None (retryable remains false)
    {
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let mut backend = DeterministicFocusBackend::new(cancel_flag.clone());
        backend.target_is_window = false;
        let target = u64_to_hwnd(backend.target_hwnd);
        let err =
            ensure_window_focused_with_backend(&mut backend, target, true, 500, &cancel_flag)
                .unwrap_err();
        assert_eq!(err.code, ErrorCode::WindowNotFound);
        assert_eq!(err.stage, ErrorStage::Activating);
        assert_eq!(err.mutation, MutationState::None);
        assert!(!err.retryable);
        let details = err.details.expect("WindowNotFound must carry details");
        assert_eq!(details.internal_mutation, Some(InternalMutationDetail::None));
        assert_eq!(details.reason, Some(DiagnosticReason::IsWindowFalse));
        assert!(backend.calls.is_empty());
    }

    // 2. Target identity invalid before Step 1 -> WindowStale with MutationState::None (retryable remains false)
    {
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let mut backend = DeterministicFocusBackend::new(cancel_flag.clone());
        backend.target_identity_valid = false;
        let target = u64_to_hwnd(backend.target_hwnd);
        let err =
            ensure_window_focused_with_backend(&mut backend, target, true, 500, &cancel_flag)
                .unwrap_err();
        assert_eq!(err.code, ErrorCode::WindowStale);
        assert_eq!(err.stage, ErrorStage::Activating);
        assert_eq!(err.mutation, MutationState::None);
        assert!(!err.retryable);
        let details = err.details.expect("WindowStale must carry details");
        assert_eq!(details.internal_mutation, Some(InternalMutationDetail::None));
        assert_eq!(details.reason, Some(DiagnosticReason::GenerationMismatch));
        assert!(backend.calls.is_empty());
    }

    // 3. Minimized window with restore_if_minimized == false -> WindowMinimized with MutationState::None (retryable remains false)
    {
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let mut backend = DeterministicFocusBackend::new(cancel_flag.clone());
        backend.minimized = true;
        let target = u64_to_hwnd(backend.target_hwnd);
        let err =
            ensure_window_focused_with_backend(&mut backend, target, false, 500, &cancel_flag)
                .unwrap_err();
        assert_eq!(err.code, ErrorCode::WindowMinimized);
        assert_eq!(err.stage, ErrorStage::Activating);
        assert_eq!(err.mutation, MutationState::None);
        assert!(!err.retryable);
        let details = err.details.expect("WindowMinimized must carry details");
        assert_eq!(details.internal_mutation, Some(InternalMutationDetail::None));
        assert_eq!(
            details.reason,
            Some(DiagnosticReason::MinimizedRestoreDisallowed)
        );
        assert!(backend.calls.is_empty());
    }

    // 4. Pre-cancelled before any activation step -> Cancelled with MutationState::None (retryable remains false)
    {
        let cancel_flag = Arc::new(AtomicBool::new(true));
        let mut backend = DeterministicFocusBackend::new(cancel_flag.clone());
        let target = u64_to_hwnd(backend.target_hwnd);
        let err =
            ensure_window_focused_with_backend(&mut backend, target, true, 500, &cancel_flag)
                .unwrap_err();
        assert_eq!(err.code, ErrorCode::Cancelled);
        assert_eq!(err.stage, ErrorStage::Activating);
        assert_eq!(err.mutation, MutationState::None);
        assert!(!err.retryable);
        let details = err.details.expect("Cancelled must carry details");
        assert_eq!(details.internal_mutation, Some(InternalMutationDetail::None));
        assert_eq!(
            details.reason,
            Some(DiagnosticReason::CancelledDuringExecution)
        );
        assert!(backend.calls.is_empty());
    }

    // 5. Target window closes during Step 3 wait -> WindowNotFound with monotonic MutationState::Possible
    {
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let mut backend = DeterministicFocusBackend::new(cancel_flag.clone());
        backend.invalidate_window_on_step = Some("during_wait_sleep");
        let target = u64_to_hwnd(backend.target_hwnd);
        let err =
            ensure_window_focused_with_backend(&mut backend, target, false, 500, &cancel_flag)
                .unwrap_err();
        assert_eq!(err.code, ErrorCode::WindowNotFound);
        assert_eq!(err.stage, ErrorStage::Activating);
        assert_eq!(err.mutation, MutationState::Possible);
        assert!(!err.retryable);
        let details = err.details.expect("WindowNotFound must carry details");
        assert_eq!(
            details.internal_mutation,
            Some(InternalMutationDetail::FocusOnly)
        );
        assert_eq!(details.reason, Some(DiagnosticReason::IsWindowFalse));
        assert_eq!(
            backend.calls,
            vec![
                RecordedFocusCall::BringWindowToTop(0x1100),
                RecordedFocusCall::SetForegroundWindow(0x1100),
            ]
        );
    }

    // 6. Target root window changes mid-sequence (reparented/stale root) -> WindowStale with MutationState::Possible
    {
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let mut backend = DeterministicFocusBackend::new(cancel_flag.clone());
        backend.target_hwnd = 0x1105;
        backend.root_hwnd = 0x1100;
        backend.change_root_on_step = Some(("set_foreground_window", 0x2200));
        let target = u64_to_hwnd(backend.target_hwnd);
        let err =
            ensure_window_focused_with_backend(&mut backend, target, false, 500, &cancel_flag)
                .unwrap_err();
        assert_eq!(err.code, ErrorCode::WindowStale);
        assert_eq!(err.stage, ErrorStage::Activating);
        assert_eq!(err.mutation, MutationState::Possible);
        assert!(!err.retryable);
        let details = err.details.expect("WindowStale must carry details");
        assert_eq!(
            details.internal_mutation,
            Some(InternalMutationDetail::FocusOnly)
        );
        assert_eq!(details.reason, Some(DiagnosticReason::GenerationMismatch));
        assert_eq!(
            backend.calls,
            vec![
                RecordedFocusCall::BringWindowToTop(0x1100),
                RecordedFocusCall::SetForegroundWindow(0x1100),
            ]
        );
    }
}

#[test]
fn test_focus_deadline_exhaustion_and_step_by_step_success_verification() {
    // 1. Tight deadline (50ms) exhausted during Step 3 wait -> skips Steps 4 & 5, fails closed
    {
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let mut backend = DeterministicFocusBackend::new(cancel_flag.clone());
        let target = u64_to_hwnd(backend.target_hwnd);
        let err = ensure_window_focused_with_backend(&mut backend, target, false, 50, &cancel_flag)
            .unwrap_err();
        assert_eq!(err.code, ErrorCode::FocusFailed);
        assert_eq!(err.stage, ErrorStage::Activating);
        assert_eq!(err.mutation, MutationState::Possible);
        assert!(!err.retryable);
        // Only Step 3 ran before the 50ms deadline expired (4 * 15ms = 60ms >= 50ms)
        assert_eq!(
            backend.calls,
            vec![
                RecordedFocusCall::BringWindowToTop(0x1100),
                RecordedFocusCall::SetForegroundWindow(0x1100),
            ]
        );
    }

    // 2. Step 1: already_foreground succeeds with attempted == false and 0 activation calls
    {
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let mut backend = DeterministicFocusBackend::new(cancel_flag.clone());
        backend.foreground_is_related = true;
        backend.foreground_hwnd = backend.root_hwnd;
        let target = u64_to_hwnd(backend.target_hwnd);
        let summary =
            ensure_window_focused_with_backend(&mut backend, target, false, 500, &cancel_flag)
                .expect("already_foreground must succeed");
        assert!(!summary.attempted);
        assert!(summary.succeeded);
        assert_eq!(summary.method_used, "already_foreground");
        assert!(summary.final_foreground_verified);
        assert!(backend.calls.is_empty());
    }

    // 3. Step 2: show_window_restore succeeds when minimized window is restored to foreground
    {
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let mut backend = DeterministicFocusBackend::new(cancel_flag.clone());
        backend.minimized = true;
        backend.succeed_on_method = Some("show_window_restore");
        let target = u64_to_hwnd(backend.target_hwnd);
        let summary =
            ensure_window_focused_with_backend(&mut backend, target, true, 1200, &cancel_flag)
                .expect("show_window_restore must succeed when restore brings target to foreground");
        assert!(summary.attempted);
        assert!(summary.succeeded);
        assert_eq!(summary.method_used, "show_window_restore");
        assert!(summary.final_foreground_verified);
        assert_eq!(
            backend.calls,
            vec![RecordedFocusCall::ShowWindowRestore(0x1100)]
        );
    }

    // 4. Step 3: set_foreground_window succeeds directly and stops before AttachThreadInput
    {
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let mut backend = DeterministicFocusBackend::new(cancel_flag.clone());
        backend.succeed_on_method = Some("set_foreground_window");
        let target = u64_to_hwnd(backend.target_hwnd);
        let summary =
            ensure_window_focused_with_backend(&mut backend, target, false, 1200, &cancel_flag)
                .expect("set_foreground_window must succeed when direct activation brings target to foreground");
        assert!(summary.attempted);
        assert!(summary.succeeded);
        assert_eq!(summary.method_used, "set_foreground_window");
        assert!(summary.final_foreground_verified);
        assert_eq!(
            backend.calls,
            vec![
                RecordedFocusCall::BringWindowToTop(0x1100),
                RecordedFocusCall::SetForegroundWindow(0x1100),
            ]
        );
    }

    // 5. Step 4: attach_thread_input succeeds, detaches cleanly, and stops before Step 5
    {
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let mut backend = DeterministicFocusBackend::new(cancel_flag.clone());
        backend.succeed_on_method = Some("attach_thread_input");
        let target = u64_to_hwnd(backend.target_hwnd);
        let summary =
            ensure_window_focused_with_backend(&mut backend, target, false, 1200, &cancel_flag)
                .expect("attach_thread_input recovery must succeed when foreground becomes related");
        assert!(summary.attempted);
        assert!(summary.succeeded);
        assert_eq!(summary.method_used, "attach_thread_input");
        assert!(summary.final_foreground_verified);
        assert!(backend.active_attachments.is_empty());
        assert!(!backend
            .calls
            .iter()
            .any(|c| matches!(c, RecordedFocusCall::SwitchToThisWindow(_))));
    }

    // 6. Step 5: switch_to_this_window succeeds as final recovery method without any keyboard fallback
    {
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let mut backend = DeterministicFocusBackend::new(cancel_flag.clone());
        backend.succeed_on_method = Some("switch_to_this_window");
        let target = u64_to_hwnd(backend.target_hwnd);
        let summary =
            ensure_window_focused_with_backend(&mut backend, target, false, 1200, &cancel_flag)
                .expect("switch_to_this_window recovery must succeed when foreground becomes related");
        assert!(summary.attempted);
        assert!(summary.succeeded);
        assert_eq!(summary.method_used, "switch_to_this_window");
        assert!(summary.final_foreground_verified);
        assert!(backend.active_attachments.is_empty());
    }

    // 7. Injected directional relation graph: owned popup (GW_OWNER -> target) accepted,
    // while sibling popup sharing only a common owner is rejected
    {
        let shared_graph = vec![
            // Main root window 0x1100
            WindowRelationNode {
                hwnd: 0x1100,
                root_hwnd: 0x1100,
                parent_hwnd: 0,
                owner_hwnd: 0,
                pid: 4000,
                process_started: Some(900_000),
            },
            // Modal popup A (0x1101) owned by 0x1100
            WindowRelationNode {
                hwnd: 0x1101,
                root_hwnd: 0x1101,
                parent_hwnd: 0,
                owner_hwnd: 0x1100,
                pid: 4000,
                process_started: Some(900_000),
            },
            // Modal popup B (0x1102) also owned by 0x1100 (sibling of 0x1101)
            WindowRelationNode {
                hwnd: 0x1102,
                root_hwnd: 0x1102,
                parent_hwnd: 0,
                owner_hwnd: 0x1100,
                pid: 4000,
                process_started: Some(900_000),
            },
            // Unrelated foreground window 0x9900 in another process
            WindowRelationNode {
                hwnd: 0x9900,
                root_hwnd: 0x9900,
                parent_hwnd: 0,
                owner_hwnd: 0,
                pid: 8000,
                process_started: Some(500_000),
            },
        ];

        assert_eq!(
            evaluate_directional_window_relation(0x1102, 0x1100, |h| {
                shared_graph.iter().find(|n| n.hwnd == h).copied()
            }),
            WindowRelationDecision::AllowedOwnedPopupChain
        );
        assert_eq!(
            evaluate_directional_window_relation(0x1102, 0x1101, |h| {
                shared_graph.iter().find(|n| n.hwnd == h).copied()
            }),
            WindowRelationDecision::RejectedSiblingOrUnownedPopup
        );

        // 7a. Target is main window 0x1100; Step 3 brings owned popup 0x1102 to foreground -> accepted
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let mut backend_owned = DeterministicFocusBackend::new(cancel_flag.clone());
        backend_owned.relation_graph = shared_graph.clone();
        backend_owned.foreground_on_step = Some(("set_foreground_window", 0x1102));
        let summary = ensure_window_focused_with_backend(
            &mut backend_owned,
            u64_to_hwnd(0x1100),
            false,
            1200,
            &cancel_flag,
        )
        .expect("Directional owned popup of target root must satisfy foreground verification");
        assert!(summary.attempted);
        assert!(summary.succeeded);
        assert_eq!(summary.method_used, "set_foreground_window");
        assert!(summary.final_foreground_verified);

        // 7b. Target is popup A (0x1101); sibling popup B (0x1102) holds foreground -> rejected across all steps
        let mut backend_sibling = DeterministicFocusBackend::new(cancel_flag.clone());
        backend_sibling.target_hwnd = 0x1101;
        backend_sibling.root_hwnd = 0x1101;
        backend_sibling.foreground_hwnd = 0x1102;
        backend_sibling.foreground_pid = 4000;
        backend_sibling.foreground_tid = 4001;
        backend_sibling.relation_graph = shared_graph;
        let err = ensure_window_focused_with_backend(
            &mut backend_sibling,
            u64_to_hwnd(0x1101),
            false,
            1200,
            &cancel_flag,
        )
        .expect_err("Sibling popup sharing only a common owner must be rejected");
        assert_eq!(err.code, ErrorCode::FocusFailed);
        assert_eq!(err.stage, ErrorStage::Activating);
        assert_eq!(err.mutation, MutationState::Possible);
        assert!(!err.retryable);
        assert!(backend_sibling.active_attachments.is_empty());
    }
}

