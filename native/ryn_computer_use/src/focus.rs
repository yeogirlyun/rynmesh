use crate::errors::{DiagnosticReason, ErrorCode, InternalMutationDetail, ProtocolError};
use crate::protocol::FocusRecoverySummary;
use crate::windows::{get_window_pid_tid, hwnd_to_u64, is_hwnd_related_to_target};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};
use windows::Win32::Foundation::HWND;
use windows::Win32::System::Threading::{AttachThreadInput, GetCurrentThreadId};
use windows::Win32::UI::Input::KeyboardAndMouse::SetFocus;
use windows::Win32::UI::WindowsAndMessaging::{
    BringWindowToTop, GetAncestor, GetForegroundWindow, IsIconic, IsWindow, SetForegroundWindow,
    ShowWindow, SwitchToThisWindow, PeekMessageW, MSG, PM_NOREMOVE, GA_ROOT, SW_RESTORE,
};

pub const DEFAULT_FOCUS_DEADLINE_MS: u64 = 1200;
pub const MAX_FOCUS_DEADLINE_MS: u64 = 5000;

/// Injectable focus recovery backend for `ensure_window_focused_with_backend`.
/// Strictly limited to window/thread focus and foreground inspection/activation primitives;
/// never exposes or dispatches synthetic keyboard or mouse `SendInput` events.
pub trait FocusExecutionBackend {
    fn now(&mut self) -> Instant;
    fn sleep(&mut self, duration: Duration);
    fn is_window(&mut self, hwnd: HWND) -> bool;
    fn get_root_window(&mut self, hwnd: HWND) -> HWND;
    fn is_minimized(&mut self, hwnd: HWND) -> bool;
    fn verify_target_identity(&mut self, root_hwnd: HWND, target_hwnd: HWND) -> bool;
    fn verify_foreground_relation(&mut self, target_hwnd: HWND) -> (bool, HWND);
    fn current_thread_id(&mut self) -> u32;
    fn window_pid_tid(&mut self, hwnd: HWND) -> (u32, u32);
    fn show_window_restore(&mut self, root_hwnd: HWND) -> bool;
    fn bring_window_to_top(&mut self, root_hwnd: HWND) -> bool;
    fn set_foreground_window(&mut self, root_hwnd: HWND) -> bool;
    fn set_focus(&mut self, root_hwnd: HWND) -> bool;
    fn switch_to_this_window(&mut self, root_hwnd: HWND);
    fn attach_thread_input(&mut self, from_tid: u32, to_tid: u32, attach: bool) -> bool;
}

/// Production Win32 backend for window focus recovery.
pub struct OsWin32FocusBackend;

impl FocusExecutionBackend for OsWin32FocusBackend {
    fn now(&mut self) -> Instant {
        Instant::now()
    }

    fn sleep(&mut self, duration: Duration) {
        std::thread::sleep(duration);
    }

    fn is_window(&mut self, hwnd: HWND) -> bool {
        !hwnd.0.is_null() && unsafe { IsWindow(Some(hwnd)).as_bool() }
    }

    fn get_root_window(&mut self, hwnd: HWND) -> HWND {
        if hwnd.0.is_null() {
            return hwnd;
        }
        unsafe {
            let r = GetAncestor(hwnd, GA_ROOT);
            if r.0.is_null() {
                hwnd
            } else {
                r
            }
        }
    }

    fn is_minimized(&mut self, hwnd: HWND) -> bool {
        !hwnd.0.is_null() && unsafe { IsIconic(hwnd).as_bool() }
    }

    fn verify_target_identity(&mut self, root_hwnd: HWND, target_hwnd: HWND) -> bool {
        if target_hwnd.0.is_null() || root_hwnd.0.is_null() {
            return false;
        }
        if !self.is_window(target_hwnd) || !self.is_window(root_hwnd) {
            return false;
        }
        is_hwnd_related_to_target(root_hwnd, target_hwnd)
    }

    fn verify_foreground_relation(&mut self, target_hwnd: HWND) -> (bool, HWND) {
        verify_foreground_relation(target_hwnd)
    }

    fn current_thread_id(&mut self) -> u32 {
        // Physical actor threads do not otherwise pump GUI messages. Win32
        // refuses AttachThreadInput if either thread has no message queue.
        let mut message = MSG::default();
        unsafe { let _ = PeekMessageW(&mut message, None, 0, 0, PM_NOREMOVE); }
        unsafe { GetCurrentThreadId() }
    }

    fn window_pid_tid(&mut self, hwnd: HWND) -> (u32, u32) {
        get_window_pid_tid(hwnd)
    }

    fn show_window_restore(&mut self, root_hwnd: HWND) -> bool {
        unsafe { ShowWindow(root_hwnd, SW_RESTORE).as_bool() }
    }

    fn bring_window_to_top(&mut self, root_hwnd: HWND) -> bool {
        unsafe { BringWindowToTop(root_hwnd).is_ok() }
    }

    fn set_foreground_window(&mut self, root_hwnd: HWND) -> bool {
        unsafe { SetForegroundWindow(root_hwnd).as_bool() }
    }

    fn set_focus(&mut self, root_hwnd: HWND) -> bool {
        unsafe { SetFocus(Some(root_hwnd)).is_ok() }
    }

    fn switch_to_this_window(&mut self, root_hwnd: HWND) {
        unsafe {
            SwitchToThisWindow(root_hwnd, true);
        }
    }

    fn attach_thread_input(&mut self, from_tid: u32, to_tid: u32, attach: bool) -> bool {
        if from_tid == 0 || to_tid == 0 || from_tid == to_tid {
            return false;
        }
        unsafe { AttachThreadInput(from_tid, to_tid, attach).as_bool() }
    }
}

/// RAII guard that attaches `(worker_tid -> fg_tid)` and `(worker_tid -> target_tid)` input queues
/// and guarantees reverse-order detachment on drop so thread input state is never leaked on early
/// return, cancellation, target invalidation, partial attachment failure, or timeout.
struct ScopedThreadInputAttachment<'a, B: FocusExecutionBackend + ?Sized> {
    backend: &'a mut B,
    worker_tid: u32,
    fg_tid: u32,
    target_tid: u32,
    fg_attached: bool,
    target_attached: bool,
}

impl<'a, B: FocusExecutionBackend + ?Sized> ScopedThreadInputAttachment<'a, B> {
    fn attach(backend: &'a mut B, worker_tid: u32, fg_tid: u32, target_tid: u32) -> Self {
        let fg_attached = if worker_tid != 0 && fg_tid != 0 && worker_tid != fg_tid {
            backend.attach_thread_input(worker_tid, fg_tid, true)
        } else {
            false
        };
        let target_attached =
            if worker_tid != 0 && target_tid != 0 && worker_tid != target_tid && target_tid != fg_tid
            {
                backend.attach_thread_input(worker_tid, target_tid, true)
            } else {
                false
            };
        Self {
            backend,
            worker_tid,
            fg_tid,
            target_tid,
            fg_attached,
            target_attached,
        }
    }

    const fn any_attached(&self) -> bool {
        self.fg_attached || self.target_attached
    }
}

impl<'a, B: FocusExecutionBackend + ?Sized> Drop for ScopedThreadInputAttachment<'a, B> {
    fn drop(&mut self) {
        if self.target_attached {
            let _ = self
                .backend
                .attach_thread_input(self.worker_tid, self.target_tid, false);
            self.target_attached = false;
        }
        if self.fg_attached {
            let _ = self
                .backend
                .attach_thread_input(self.worker_tid, self.fg_tid, false);
            self.fg_attached = false;
        }
    }
}

#[inline]
const fn mutation_from_attempted(mutated: bool) -> InternalMutationDetail {
    if mutated {
        InternalMutationDetail::FocusOnly
    } else {
        InternalMutationDetail::None
    }
}

fn check_focus_step_gate<B: FocusExecutionBackend + ?Sized>(
    backend: &mut B,
    root_hwnd: HWND,
    target_hwnd: HWND,
    cancel_flag: &AtomicBool,
    mutated: bool,
) -> Result<(), ProtocolError> {
    let mutation = mutation_from_attempted(mutated);
    if cancel_flag.load(Ordering::SeqCst) {
        return Err(ProtocolError::focus(
            ErrorCode::Cancelled,
            mutation,
            DiagnosticReason::CancelledDuringExecution,
        )
        .with_hwnds(Some(hwnd_to_u64(root_hwnd)), None));
    }
    if target_hwnd.0.is_null()
        || root_hwnd.0.is_null()
        || !backend.is_window(target_hwnd)
        || !backend.is_window(root_hwnd)
    {
        return Err(ProtocolError::focus(
            ErrorCode::WindowNotFound,
            mutation,
            DiagnosticReason::IsWindowFalse,
        )
        .with_hwnds(Some(hwnd_to_u64(root_hwnd)), None));
    }
    let current_root = backend.get_root_window(target_hwnd);
    let effective_root = if current_root.0.is_null() {
        target_hwnd
    } else {
        current_root
    };
    if effective_root != root_hwnd || !backend.verify_target_identity(root_hwnd, target_hwnd) {
        return Err(ProtocolError::focus(
            ErrorCode::WindowStale,
            mutation,
            DiagnosticReason::GenerationMismatch,
        )
        .with_hwnds(Some(hwnd_to_u64(root_hwnd)), None));
    }
    Ok(())
}

/// Checks whether the current foreground window satisfies focus requirements for `target_hwnd`
/// (either exact match, child descendant match, or directional owned popup/dialog relation
/// within the same non-zero `(PID, ProcessCreationTime)`).
pub fn verify_foreground_relation(target_hwnd: HWND) -> (bool, HWND) {
    unsafe {
        let fg = GetForegroundWindow();
        if fg.0.is_null() {
            return (false, fg);
        }
        let related = is_hwnd_related_to_target(fg, target_hwnd);
        (related, fg)
    }
}

/// Executes bounded, multi-stage focus recovery for `target_hwnd` within `deadline_ms`
/// using the production Win32 backend.
/// Never dispatches synthetic keyboard/mouse `SendInput` (such as unscoped Alt key taps)
/// or claims focus success without verified target foreground relationship.
pub fn ensure_window_focused(
    target_hwnd: HWND,
    restore_if_minimized: bool,
    deadline_ms: u64,
    cancel_flag: &AtomicBool,
) -> Result<FocusRecoverySummary, ProtocolError> {
    let mut backend = OsWin32FocusBackend;
    ensure_window_focused_with_backend(
        &mut backend,
        target_hwnd,
        restore_if_minimized,
        deadline_ms,
        cancel_flag,
    )
}

/// Testable focus recovery state machine using `FocusExecutionBackend`.
/// Steps:
/// 1. `already_foreground` (if not minimized and foreground already satisfies target relation)
/// 2. `show_window_restore` (if minimized and `restore_if_minimized` is permitted)
/// 3. `set_foreground_window` (`BringWindowToTop` + `SetForegroundWindow`)
/// 4. `attach_thread_input` (scoped RAII `AttachThreadInput` + `BringWindowToTop` + `SetForegroundWindow` + `SetFocus`)
/// 5. `switch_to_this_window` (`SwitchToThisWindow` + `SetForegroundWindow`)
///
/// Guards cancellation and target identity before every step and during post-step foreground waits.
/// If target foreground cannot be verified within `deadline_ms`, fails closed with `ErrorCode::FocusFailed`
/// and monotonic `InternalMutationDetail::FocusOnly` (`mutation: possible`) whenever any activation step
/// was attempted.
pub fn ensure_window_focused_with_backend<B: FocusExecutionBackend + ?Sized>(
    backend: &mut B,
    target_hwnd: HWND,
    restore_if_minimized: bool,
    deadline_ms: u64,
    cancel_flag: &AtomicBool,
) -> Result<FocusRecoverySummary, ProtocolError> {
    let start = backend.now();
    let bounded_ms = deadline_ms.clamp(50, MAX_FOCUS_DEADLINE_MS);
    let deadline = start + Duration::from_millis(bounded_ms);

    if target_hwnd.0.is_null() || !backend.is_window(target_hwnd) {
        return Err(ProtocolError::focus(
            ErrorCode::WindowNotFound,
            InternalMutationDetail::None,
            DiagnosticReason::IsWindowFalse,
        )
        .with_hwnds(Some(hwnd_to_u64(target_hwnd)), None));
    }

    let root_hwnd = {
        let r = backend.get_root_window(target_hwnd);
        if r.0.is_null() {
            target_hwnd
        } else {
            r
        }
    };

    check_focus_step_gate(backend, root_hwnd, target_hwnd, cancel_flag, false)?;

    let mut mutated = false;

    // Step 1: Check if already foreground/related and not minimized
    let is_minimized = backend.is_minimized(root_hwnd);
    if !is_minimized {
        let (ok, _) = backend.verify_foreground_relation(root_hwnd);
        if ok {
            check_focus_step_gate(backend, root_hwnd, target_hwnd, cancel_flag, false)?;
            return Ok(FocusRecoverySummary {
                attempted: false,
                succeeded: true,
                method_used: "already_foreground".to_string(),
                duration_ms: backend.now().saturating_duration_since(start).as_millis() as u64,
                final_foreground_verified: true,
            });
        }
    }

    // Step 2: Restore minimized window if allowed
    if is_minimized {
        if !restore_if_minimized {
            return Err(ProtocolError::focus(
                ErrorCode::WindowMinimized,
                InternalMutationDetail::None,
                DiagnosticReason::MinimizedRestoreDisallowed,
            )
            .with_hwnds(Some(hwnd_to_u64(root_hwnd)), None));
        }
        check_focus_step_gate(backend, root_hwnd, target_hwnd, cancel_flag, mutated)?;
        let _ = backend.show_window_restore(root_hwnd);
        mutated = true;
        let step_limit = start + Duration::from_millis(80);
        if wait_for_foreground_until(
            backend,
            root_hwnd,
            target_hwnd,
            step_limit,
            deadline,
            cancel_flag,
            mutated,
        )? {
            return Ok(FocusRecoverySummary {
                attempted: true,
                succeeded: true,
                method_used: "show_window_restore".to_string(),
                duration_ms: backend.now().saturating_duration_since(start).as_millis() as u64,
                final_foreground_verified: true,
            });
        }
    }

    // Step 3: Direct BringWindowToTop + SetForegroundWindow
    check_focus_step_gate(backend, root_hwnd, target_hwnd, cancel_flag, mutated)?;
    if backend.now() < deadline {
        let _ = backend.bring_window_to_top(root_hwnd);
        let _ = backend.set_foreground_window(root_hwnd);
        mutated = true;
        let step_limit = backend.now() + Duration::from_millis(60);
        if wait_for_foreground_until(
            backend,
            root_hwnd,
            target_hwnd,
            step_limit,
            deadline,
            cancel_flag,
            mutated,
        )? {
            return Ok(FocusRecoverySummary {
                attempted: true,
                succeeded: true,
                method_used: "set_foreground_window".to_string(),
                duration_ms: backend.now().saturating_duration_since(start).as_millis() as u64,
                final_foreground_verified: true,
            });
        }
    }

    // Step 4: Scoped AttachThreadInput (worker -> foreground, worker -> target) + BringWindowToTop + SetForegroundWindow + SetFocus
    check_focus_step_gate(backend, root_hwnd, target_hwnd, cancel_flag, mutated)?;
    if backend.now() < deadline {
        let worker_tid = backend.current_thread_id();
        let (_, fg_hwnd) = backend.verify_foreground_relation(root_hwnd);
        let (_, fg_tid) = if !fg_hwnd.0.is_null() {
            backend.window_pid_tid(fg_hwnd)
        } else {
            (0, 0)
        };
        let (_, target_tid) = backend.window_pid_tid(root_hwnd);

        {
            let scope =
                ScopedThreadInputAttachment::attach(backend, worker_tid, fg_tid, target_tid);
            if scope.any_attached() {
                mutated = true;
            }
            check_focus_step_gate(scope.backend, root_hwnd, target_hwnd, cancel_flag, mutated)?;
            let _ = scope.backend.bring_window_to_top(root_hwnd);
            let _ = scope.backend.set_foreground_window(root_hwnd);
            let _ = scope.backend.set_focus(root_hwnd);
            mutated = true;
        }
        let step_limit = backend.now() + Duration::from_millis(80);
        if wait_for_foreground_until(
            backend,
            root_hwnd,
            target_hwnd,
            step_limit,
            deadline,
            cancel_flag,
            mutated,
        )? {
            return Ok(FocusRecoverySummary {
                attempted: true,
                succeeded: true,
                method_used: "attach_thread_input".to_string(),
                duration_ms: backend.now().saturating_duration_since(start).as_millis() as u64,
                final_foreground_verified: true,
            });
        }
    }

    // Step 5: SwitchToThisWindow + SetForegroundWindow
    check_focus_step_gate(backend, root_hwnd, target_hwnd, cancel_flag, mutated)?;
    if backend.now() < deadline {
        backend.switch_to_this_window(root_hwnd);
        let _ = backend.set_foreground_window(root_hwnd);
        mutated = true;
        let step_limit = backend.now() + Duration::from_millis(80);
        if wait_for_foreground_until(
            backend,
            root_hwnd,
            target_hwnd,
            step_limit,
            deadline,
            cancel_flag,
            mutated,
        )? {
            return Ok(FocusRecoverySummary {
                attempted: true,
                succeeded: true,
                method_used: "switch_to_this_window".to_string(),
                duration_ms: backend.now().saturating_duration_since(start).as_millis() as u64,
                final_foreground_verified: true,
            });
        }
    }

    // Final foreground relation check (only after re-verifying cancellation and target identity)
    check_focus_step_gate(backend, root_hwnd, target_hwnd, cancel_flag, mutated)?;
    let (final_ok, final_fg) = backend.verify_foreground_relation(root_hwnd);
    if final_ok {
        check_focus_step_gate(backend, root_hwnd, target_hwnd, cancel_flag, mutated)?;
        return Ok(FocusRecoverySummary {
            attempted: true,
            succeeded: true,
            method_used: "final_verification".to_string(),
            duration_ms: backend.now().saturating_duration_since(start).as_millis() as u64,
            final_foreground_verified: true,
        });
    }

    let internal_mutation = mutation_from_attempted(mutated);
    let fg_u64 = if final_fg.0.is_null() {
        None
    } else {
        Some(hwnd_to_u64(final_fg))
    };

    Err(ProtocolError::focus(
        ErrorCode::FocusFailed,
        internal_mutation,
        DiagnosticReason::ForegroundVerificationFailed,
    )
    .with_hwnds(Some(hwnd_to_u64(root_hwnd)), fg_u64))
}

fn wait_for_foreground_until<B: FocusExecutionBackend + ?Sized>(
    backend: &mut B,
    root_hwnd: HWND,
    target_hwnd: HWND,
    step_limit: Instant,
    hard_deadline: Instant,
    cancel_flag: &AtomicBool,
    mutated: bool,
) -> Result<bool, ProtocolError> {
    let limit = if step_limit < hard_deadline {
        step_limit
    } else {
        hard_deadline
    };
    loop {
        check_focus_step_gate(backend, root_hwnd, target_hwnd, cancel_flag, mutated)?;
        let (ok, _) = backend.verify_foreground_relation(root_hwnd);
        if ok {
            check_focus_step_gate(backend, root_hwnd, target_hwnd, cancel_flag, mutated)?;
            return Ok(true);
        }
        if backend.now() >= limit {
            return Ok(false);
        }
        backend.sleep(Duration::from_millis(15));
    }
}
