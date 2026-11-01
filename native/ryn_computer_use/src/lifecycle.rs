//! Real WinEvent (`EVENT_OBJECT_CREATE` / `EVENT_OBJECT_DESTROY`) pump thread and service-backed
//! window lifecycle adapter for N05B (`ryn_computer_use::lifecycle`).
//!
//! # Authority & Limitations
//! - Starts a dedicated pump thread per production worker, creates its thread message queue
//!   (`PeekMessageW(..., PM_NOREMOVE)`), and registers `SetWinEventHook(EVENT_OBJECT_CREATE,
//!   EVENT_OBJECT_DESTROY, None, Some(win_event_proc), 0, 0, WINEVENT_OUTOFCONTEXT)` without
//!   `WINEVENT_SKIPOWNPROCESS` so same-process fixture windows are also observed.
//! - Uses thread-local per-pump callback context (never a global last-engine pointer).
//! - The callback filters `hwnd != 0 && id_object == OBJID_WINDOW.0 && id_child == CHILDID_SELF as i32`
//!   and only enqueues fixed-capacity `DeliveredWindowEvent` items with checked `arrival_seq` and
//!   fault state under `catch_unwind`; it never performs PID/UIA lookup, desktop mutex waits,
//!   stdout writes, or input injection.
//! - Win32 `WINEVENT_OUTOFCONTEXT` is asynchronous and has no public flush API. Draining the local
//!   queue and verifying pump heartbeat proves continuity of observed hook coverage, NOT that every
//!   external event has already been delivered or that all Win32 TOCTOU windows are eliminated.
//! - Does NOT automatically restart a faulted service in N05B; any fault permanently transitions
//!   coverage to `CoverageState::Unhealthy` for the worker's lifetime.

use crate::errors::{DiagnosticReason, ErrorCode, ErrorStage, ProtocolError};
use crate::lifecycle_model::{
    CoverageFaultReason, CoverageState, DEFAULT_EVENT_DRAIN_BUDGET, DEFAULT_MAX_TRACKED_WINDOWS,
    DeliveredWindowEvent, HostWorkerNonce, IssueOutcome, LifecycleError, LifecycleEventKind,
    LifecycleStamp, ObservationCheckpoint, RetiredWindowIdentity, RetirementBatch,
    WindowLifecycleRegistry, WindowOsTuple,
};
#[cfg(feature = "lifecycle-test-support")]
use crate::lifecycle_model::RetirementReason;
use crate::protocol::WindowRef;
use crate::windows::{hwnd_to_u64, parse_decimal_u64};
use std::cell::Cell;
use std::collections::VecDeque;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::{Arc, Mutex, TryLockError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use windows::Win32::Foundation::{
    CloseHandle, DuplicateHandle, DUPLICATE_SAME_ACCESS, FILETIME, HANDLE, HWND, LPARAM,
    WAIT_OBJECT_0, WPARAM,
};
use windows::Win32::Security::Cryptography::{BCryptGenRandom, BCRYPT_USE_SYSTEM_PREFERRED_RNG};
use windows::Win32::System::Threading::{
    GetCurrentProcess, GetCurrentThread, GetCurrentThreadId, GetThreadTimes, WaitForSingleObject,
};
use windows::Win32::UI::Accessibility::{SetWinEventHook, UnhookWinEvent, HWINEVENTHOOK};
use windows::Win32::UI::WindowsAndMessaging::{
    DispatchMessageW, KillTimer, MsgWaitForMultipleObjectsEx, PeekMessageW, PostThreadMessageW,
    SetTimer, TranslateMessage, CHILDID_SELF, EVENT_OBJECT_CREATE, EVENT_OBJECT_DESTROY, MSG,
    MWMO_INPUTAVAILABLE, OBJID_WINDOW, PM_NOREMOVE, PM_REMOVE, QS_ALLINPUT,
    WINEVENT_OUTOFCONTEXT, WM_QUIT, WM_TIMER,
};

pub const DEFAULT_HOOK_QUEUE_CAPACITY: usize = 512;
pub const MAX_RETIREMENT_BACKLOG: usize = 512;
#[cfg(feature = "lifecycle-test-support")]
pub const CALLBACK_TRACE_CAPACITY: usize = 2048;
#[cfg(feature = "lifecycle-test-support")]
const MAX_MATCHED_TARGET_TRACES: usize = 32;
pub const HOOK_STARTUP_TIMEOUT_MS: u64 = 2_000;
pub const HOOK_STOP_TIMEOUT_MS: u64 = 1_500;
pub const PUMP_HEARTBEAT_SLICE_MS: u32 = 100;
pub const PUMP_HEARTBEAT_EXPIRY_MS: u64 = 2_500;
const PUMP_TIMER_ID: usize = 0x5259_4E35;

#[cfg(feature = "lifecycle-test-support")]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HookRegistrationDiagnostics {
    pub pump_thread_id: u32,
    pub pump_thread_creation_time: u64,
    pub pinned_thread_handle_valid: bool,
    pub message_queue_initialized: bool,
    pub hook_handle_nonzero: bool,
    pub hook_generation: u64,
    pub event_min: u32,
    pub event_max: u32,
    pub hook_flags: u32,
}

#[cfg(feature = "lifecycle-test-support")]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CallbackTraceRecord {
    pub raw_event: u32,
    pub kind: LifecycleEventKind,
    pub hwnd: u64,
    pub id_object: i32,
    pub id_child: i32,
    pub arrival_seq: u64,
    pub hook_generation: u64,
    pub source_event_thread: u32,
    pub actual_callback_tid: u32,
    pub event_time_ms: u32,
    pub monotonic_timestamp_ns: u64,
    pub applied_to_registry: bool,
    pub retired_issue_generation: Option<u64>,
}

#[cfg(feature = "lifecycle-test-support")]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HookShutdownDiagnostics {
    pub stop_requested: bool,
    pub stop_acknowledged: bool,
    pub unhook_called: bool,
    pub unhook_succeeded: bool,
    pub unhook_thread_id: u32,
    pub pinned_thread_signaled_before_join: bool,
    pub join_completed_within_budget: bool,
    pub stop_elapsed_ms: u64,
    pub stop_budget_ms: u64,
    pub cleanup_quarantined: bool,
}

#[cfg(feature = "lifecycle-test-support")]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LifecycleProducerDiagnostics {
    pub coverage_state: CoverageState,
    pub coverage_generation: u64,
    pub last_heartbeat_ns: u64,
    pub heartbeat_count: u64,
    pub registration: HookRegistrationDiagnostics,
    pub shutdown: HookShutdownDiagnostics,
    pub total_accepted_callbacks: u64,
    pub trace_overflowed: bool,
    pub matched_target_traces: Vec<CallbackTraceRecord>,
}

/// Generates a non-zero 128-bit `HostWorkerNonce` from the Windows CNG system RNG (`BCryptGenRandom`).
pub fn generate_host_worker_nonce() -> Result<HostWorkerNonce, ProtocolError> {
    for _ in 0..4 {
        let mut buf = [0u8; 16];
        let status = unsafe { BCryptGenRandom(None, &mut buf, BCRYPT_USE_SYSTEM_PREFERRED_RNG) };
        if status.is_err() {
            return Err(ProtocolError::observe(
                ErrorCode::InternalError,
                DiagnosticReason::DesktopIdentityLookupFailed,
            )
            .with_hresult(status.0));
        }
        let raw = u128::from_le_bytes(buf);
        if let Ok(nonce) = HostWorkerNonce::new(raw) {
            return Ok(nonce);
        }
    }
    Err(ProtocolError::observe(
        ErrorCode::InternalError,
        DiagnosticReason::DesktopIdentityLookupFailed,
    ))
}

/// Owned duplicated OS thread handle (`DuplicateHandle` on `GetCurrentThread()`) captured while
/// the pump thread is live at startup. Used solely to query creation time (`GetThreadTimes`) and
/// verify kernel thread object termination (`WaitForSingleObject`) before `JoinHandle::join`,
/// avoiding raw TID-lookup races after thread exit.
struct PinnedThreadHandle(usize);

impl PinnedThreadHandle {
    fn duplicate_current_thread_and_creation_time() -> Option<(Self, u64)> {
        let mut dup_handle = HANDLE::default();
        let dup_ok = unsafe {
            DuplicateHandle(
                GetCurrentProcess(),
                GetCurrentThread(),
                GetCurrentProcess(),
                &mut dup_handle,
                0,
                false,
                DUPLICATE_SAME_ACCESS,
            )
            .is_ok()
        };
        if !dup_ok || dup_handle.0.is_null() || dup_handle.is_invalid() {
            return None;
        }
        let mut creation = FILETIME::default();
        let mut exit = FILETIME::default();
        let mut kernel = FILETIME::default();
        let mut user = FILETIME::default();
        let times_ok = unsafe {
            GetThreadTimes(dup_handle, &mut creation, &mut exit, &mut kernel, &mut user).is_ok()
        };
        let ticks = ((creation.dwHighDateTime as u64) << 32) | (creation.dwLowDateTime as u64);
        if !times_ok || ticks == 0 {
            unsafe {
                let _ = CloseHandle(dup_handle);
            }
            return None;
        }
        Some((Self(dup_handle.0 as usize), ticks))
    }

    fn raw_handle(&self) -> HANDLE {
        HANDLE(self.0 as *mut std::ffi::c_void)
    }

    fn is_valid(&self) -> bool {
        self.0 != 0 && !self.raw_handle().is_invalid()
    }
}

impl Drop for PinnedThreadHandle {
    fn drop(&mut self) {
        if self.is_valid() {
            unsafe {
                let _ = CloseHandle(self.raw_handle());
            }
            self.0 = 0;
        }
    }
}

#[cfg(feature = "lifecycle-test-support")]
#[derive(Clone, Copy, Debug)]
struct CallbackTraceEntry {
    raw_event: u32,
    kind: LifecycleEventKind,
    hwnd: u64,
    id_object: i32,
    id_child: i32,
    arrival_seq: u64,
    hook_generation: u64,
    source_event_thread: u32,
    actual_callback_tid: u32,
    event_time_ms: u32,
    monotonic_timestamp_ns: u64,
    applied_to_registry: bool,
    retired_issue_generation: Option<u64>,
}

/// Separate feature-only preallocated diagnostic buffer.
/// Never shares the safety-critical `SharedCallbackQueue` mutex, never allocates in callback
/// context, and never faults real worker coverage if full or contended.
#[cfg(feature = "lifecycle-test-support")]
struct DiagnosticTraceBuffer {
    entries: Vec<CallbackTraceEntry>,
    trace_overflowed: bool,
    total_accepted_callbacks: u64,
}

#[cfg(feature = "lifecycle-test-support")]
impl DiagnosticTraceBuffer {
    fn new() -> Self {
        Self {
            entries: Vec::with_capacity(CALLBACK_TRACE_CAPACITY),
            trace_overflowed: false,
            total_accepted_callbacks: 0,
        }
    }

    fn record_callback(&mut self, entry: CallbackTraceEntry) {
        self.total_accepted_callbacks = self.total_accepted_callbacks.saturating_add(1);
        if self.trace_overflowed || self.entries.len() >= CALLBACK_TRACE_CAPACITY {
            self.trace_overflowed = true;
            return;
        }
        self.entries.push(entry);
    }
}

struct SharedCallbackQueue {
    events: VecDeque<DeliveredWindowEvent>,
    max_queue_capacity: usize,
    next_arrival_seq: u64,
    arrival_seq_exhausted: bool,
    hook_generation: u64,
    fault: Option<CoverageFaultReason>,
}

impl SharedCallbackQueue {
    fn new(hook_generation: u64, max_queue_capacity: usize) -> Self {
        let cap = max_queue_capacity.clamp(1, 4096);
        Self {
            events: VecDeque::with_capacity(cap.min(512)),
            max_queue_capacity: cap,
            next_arrival_seq: 1,
            arrival_seq_exhausted: false,
            hook_generation,
            fault: None,
        }
    }

    fn latch_fault(&mut self, reason: CoverageFaultReason) {
        if self.fault.is_none() {
            self.fault = Some(reason);
        }
        self.events.clear();
    }

    fn push_raw_event(
        &mut self,
        hwnd: u64,
        kind: LifecycleEventKind,
        event_thread: u32,
        event_time_ms: u32,
    ) -> Option<u64> {
        if self.fault.is_some() || hwnd == 0 {
            return None;
        }
        if self.events.len() >= self.max_queue_capacity {
            self.latch_fault(CoverageFaultReason::QueueOverflow);
            return None;
        }
        if self.arrival_seq_exhausted {
            self.latch_fault(CoverageFaultReason::ArrivalSeqOverflow);
            return None;
        }
        let seq = self.next_arrival_seq;
        match self.next_arrival_seq.checked_add(1) {
            Some(next) => {
                self.next_arrival_seq = next;
            }
            None => {
                self.arrival_seq_exhausted = true;
            }
        }
        self.events.push_back(DeliveredWindowEvent {
            hwnd,
            kind,
            event_thread,
            event_time_ms,
            arrival_seq: seq,
            hook_generation: self.hook_generation,
        });
        Some(seq)
    }
}

/// Shared state between the dedicated WinEvent pump thread and the `LifecycleService`.
struct PumpSharedState {
    epoch_instant: Instant,
    last_heartbeat_ns: AtomicU64,
    #[cfg(feature = "lifecycle-test-support")]
    heartbeat_count: AtomicU64,
    heartbeat_expiry_ns: u64,
    stop_requested: AtomicBool,
    pump_exited_unexpectedly: AtomicBool,
    callback_fault_code: AtomicU32,
    pump_thread_id: AtomicU32,
    #[cfg(feature = "lifecycle-test-support")]
    pump_thread_creation_time: AtomicU64,
    #[cfg(feature = "lifecycle-test-support")]
    pinned_thread_handle_valid: AtomicBool,
    #[cfg(feature = "lifecycle-test-support")]
    message_queue_initialized: AtomicBool,
    #[cfg(feature = "lifecycle-test-support")]
    hook_handle_nonzero: AtomicBool,
    #[cfg(feature = "lifecycle-test-support")]
    hook_generation: u64,
    #[cfg(feature = "lifecycle-test-support")]
    event_min: AtomicU32,
    #[cfg(feature = "lifecycle-test-support")]
    event_max: AtomicU32,
    #[cfg(feature = "lifecycle-test-support")]
    hook_flags: AtomicU32,
    unhook_called: AtomicBool,
    unhook_succeeded: AtomicBool,
    unhook_thread_id: AtomicU32,
    stop_acknowledged: AtomicBool,
    #[cfg(feature = "lifecycle-test-support")]
    pinned_thread_signaled_before_join: AtomicBool,
    #[cfg(feature = "lifecycle-test-support")]
    join_completed_within_budget: AtomicBool,
    #[cfg(feature = "lifecycle-test-support")]
    stop_elapsed_ms: AtomicU64,
    #[cfg(feature = "lifecycle-test-support")]
    stop_budget_ms: AtomicU64,
    queue: Mutex<SharedCallbackQueue>,
    #[cfg(feature = "lifecycle-test-support")]
    diag_trace_incomplete: AtomicBool,
    #[cfg(feature = "lifecycle-test-support")]
    diag_trace: Mutex<DiagnosticTraceBuffer>,
}

const CALLBACK_FAULT_NONE: u32 = 0;
const CALLBACK_FAULT_CONTENTION_OR_OVERFLOW: u32 = 1;
const CALLBACK_FAULT_POISONED: u32 = 2;
const CALLBACK_FAULT_PUMP_EXITED: u32 = 3;

fn encode_fault_code(reason: CoverageFaultReason) -> u32 {
    match reason {
        CoverageFaultReason::LockPoisoned => CALLBACK_FAULT_POISONED,
        CoverageFaultReason::PumpExited => CALLBACK_FAULT_PUMP_EXITED,
        _ => CALLBACK_FAULT_CONTENTION_OR_OVERFLOW,
    }
}

fn decode_fault_code(code: u32) -> Option<CoverageFaultReason> {
    match code {
        CALLBACK_FAULT_NONE => None,
        CALLBACK_FAULT_POISONED => Some(CoverageFaultReason::LockPoisoned),
        CALLBACK_FAULT_PUMP_EXITED => Some(CoverageFaultReason::PumpExited),
        _ => Some(CoverageFaultReason::QueueOverflow),
    }
}

impl PumpSharedState {
    fn new(hook_generation: u64, max_queue_capacity: usize, heartbeat_expiry_ms: u64) -> Self {
        let expiry_ns = heartbeat_expiry_ms
            .max(50)
            .saturating_mul(1_000_000);
        Self {
            epoch_instant: Instant::now(),
            last_heartbeat_ns: AtomicU64::new(1),
            #[cfg(feature = "lifecycle-test-support")]
            heartbeat_count: AtomicU64::new(0),
            heartbeat_expiry_ns: expiry_ns,
            stop_requested: AtomicBool::new(false),
            pump_exited_unexpectedly: AtomicBool::new(false),
            callback_fault_code: AtomicU32::new(CALLBACK_FAULT_NONE),
            pump_thread_id: AtomicU32::new(0),
            #[cfg(feature = "lifecycle-test-support")]
            pump_thread_creation_time: AtomicU64::new(0),
            #[cfg(feature = "lifecycle-test-support")]
            pinned_thread_handle_valid: AtomicBool::new(false),
            #[cfg(feature = "lifecycle-test-support")]
            message_queue_initialized: AtomicBool::new(false),
            #[cfg(feature = "lifecycle-test-support")]
            hook_handle_nonzero: AtomicBool::new(false),
            #[cfg(feature = "lifecycle-test-support")]
            hook_generation,
            #[cfg(feature = "lifecycle-test-support")]
            event_min: AtomicU32::new(0),
            #[cfg(feature = "lifecycle-test-support")]
            event_max: AtomicU32::new(0),
            #[cfg(feature = "lifecycle-test-support")]
            hook_flags: AtomicU32::new(0),
            unhook_called: AtomicBool::new(false),
            unhook_succeeded: AtomicBool::new(false),
            unhook_thread_id: AtomicU32::new(0),
            stop_acknowledged: AtomicBool::new(false),
            #[cfg(feature = "lifecycle-test-support")]
            pinned_thread_signaled_before_join: AtomicBool::new(false),
            #[cfg(feature = "lifecycle-test-support")]
            join_completed_within_budget: AtomicBool::new(false),
            #[cfg(feature = "lifecycle-test-support")]
            stop_elapsed_ms: AtomicU64::new(0),
            #[cfg(feature = "lifecycle-test-support")]
            stop_budget_ms: AtomicU64::new(0),
            queue: Mutex::new(SharedCallbackQueue::new(
                hook_generation,
                max_queue_capacity,
            )),
            #[cfg(feature = "lifecycle-test-support")]
            diag_trace_incomplete: AtomicBool::new(false),
            #[cfg(feature = "lifecycle-test-support")]
            diag_trace: Mutex::new(DiagnosticTraceBuffer::new()),
        }
    }

    fn elapsed_monotonic_ns(&self) -> u64 {
        self.epoch_instant
            .elapsed()
            .as_nanos()
            .min(u128::from(u64::MAX - 1)) as u64
    }

    fn record_heartbeat(&self) {
        let elapsed_ns = self.elapsed_monotonic_ns().max(1);
        self.last_heartbeat_ns.store(elapsed_ns, Ordering::SeqCst);
        #[cfg(feature = "lifecycle-test-support")]
        {
            self.heartbeat_count.fetch_add(1, Ordering::SeqCst);
        }
    }

    fn is_heartbeat_expired(&self) -> bool {
        let last = self.last_heartbeat_ns.load(Ordering::SeqCst);
        if last == 0 {
            return true;
        }
        let now = self.elapsed_monotonic_ns();
        now.saturating_sub(last) > self.heartbeat_expiry_ns
    }

    fn record_callback_fault(&self, reason: CoverageFaultReason) {
        let _ = self.callback_fault_code.compare_exchange(
            CALLBACK_FAULT_NONE,
            encode_fault_code(reason),
            Ordering::SeqCst,
            Ordering::SeqCst,
        );
    }

    fn on_raw_win_event(
        &self,
        event: u32,
        hwnd: HWND,
        id_object: i32,
        id_child: i32,
        id_event_thread: u32,
        dwms_event_time: u32,
    ) {
        let hwnd_val = hwnd_to_u64(hwnd);
        if hwnd_val == 0 || id_object != OBJID_WINDOW.0 || id_child != (CHILDID_SELF as i32) {
            return;
        }
        let kind = if event == EVENT_OBJECT_CREATE {
            LifecycleEventKind::ObjectCreate
        } else if event == EVENT_OBJECT_DESTROY {
            LifecycleEventKind::ObjectDestroy
        } else {
            return;
        };

        self.record_heartbeat();

        if self.callback_fault_code.load(Ordering::SeqCst) != CALLBACK_FAULT_NONE {
            return;
        }

        #[cfg_attr(not(feature = "lifecycle-test-support"), allow(unused_variables))]
        let accepted_seq = match self.queue.try_lock() {
            Ok(mut guard) => {
                let seq = guard.push_raw_event(hwnd_val, kind, id_event_thread, dwms_event_time);
                if let Some(f) = guard.fault {
                    self.record_callback_fault(f);
                }
                seq
            }
            Err(TryLockError::WouldBlock) => {
                // Bounded callback contract: never block the WinEvent pump callback on lock contention
                // and never silently drop a lifecycle event while remaining healthy.
                self.record_callback_fault(CoverageFaultReason::QueueOverflow);
                None
            }
            Err(TryLockError::Poisoned(_)) => {
                self.record_callback_fault(CoverageFaultReason::LockPoisoned);
                None
            }
        };

        #[cfg(feature = "lifecycle-test-support")]
        if let Some(seq) = accepted_seq {
            let actual_callback_tid = unsafe { GetCurrentThreadId() };
            let monotonic_ns = self.elapsed_monotonic_ns().max(1);
            match self.diag_trace.try_lock() {
                Ok(mut t_guard) => {
                    t_guard.record_callback(CallbackTraceEntry {
                        raw_event: event,
                        kind,
                        hwnd: hwnd_val,
                        id_object,
                        id_child,
                        arrival_seq: seq,
                        hook_generation: self.hook_generation,
                        source_event_thread: id_event_thread,
                        actual_callback_tid,
                        event_time_ms: dwms_event_time,
                        monotonic_timestamp_ns: monotonic_ns,
                        applied_to_registry: false,
                        retired_issue_generation: None,
                    });
                }
                Err(TryLockError::WouldBlock) | Err(TryLockError::Poisoned(_)) => {
                    // Diagnostic contention only marks diagnostic evidence incomplete;
                    // never faults the real callback queue or worker coverage health.
                    self.diag_trace_incomplete.store(true, Ordering::SeqCst);
                }
            }
        }
    }
}

thread_local! {
    static CURRENT_PUMP_STATE: Cell<*const PumpSharedState> = const { Cell::new(std::ptr::null()) };
}

struct ThreadLocalPumpContextGuard;

impl ThreadLocalPumpContextGuard {
    fn install(state: &Arc<PumpSharedState>) -> Self {
        CURRENT_PUMP_STATE.with(|cell| {
            cell.set(Arc::as_ptr(state));
        });
        Self
    }
}

impl Drop for ThreadLocalPumpContextGuard {
    fn drop(&mut self) {
        CURRENT_PUMP_STATE.with(|cell| {
            cell.set(std::ptr::null());
        });
    }
}

unsafe extern "system" fn win_event_proc(
    _h_win_event_hook: HWINEVENTHOOK,
    event: u32,
    hwnd: HWND,
    id_object: i32,
    id_child: i32,
    id_event_thread: u32,
    dwms_event_time: u32,
) {
    let _ = catch_unwind(AssertUnwindSafe(|| {
        CURRENT_PUMP_STATE.with(|cell| {
            let ptr = cell.get();
            if !ptr.is_null() {
                let state = unsafe { &*ptr };
                state.on_raw_win_event(
                    event,
                    hwnd,
                    id_object,
                    id_child,
                    id_event_thread,
                    dwms_event_time,
                );
            }
        });
    }));
}

struct PumpThreadHandle {
    shared: Arc<PumpSharedState>,
    pinned_thread: PinnedThreadHandle,
    stop_ack_rx: Mutex<mpsc::Receiver<bool>>,
    join_handle: Mutex<Option<JoinHandle<()>>>,
    stop_called: AtomicBool,
    cleanup_quarantined: AtomicBool,
}

impl PumpThreadHandle {
    fn spawn_started(
        hook_generation: u64,
        max_queue_capacity: usize,
        heartbeat_expiry_ms: u64,
        startup_timeout_ms: u64,
    ) -> Result<Self, CoverageFaultReason> {
        let shared = Arc::new(PumpSharedState::new(
            hook_generation,
            max_queue_capacity,
            heartbeat_expiry_ms,
        ));
        let (startup_tx, startup_rx) =
            mpsc::sync_channel::<Result<(u32, PinnedThreadHandle), CoverageFaultReason>>(1);
        let (stop_ack_tx, stop_ack_rx) = mpsc::sync_channel::<bool>(1);
        let pump_shared = shared.clone();

        let join_handle = thread::Builder::new()
            .name("ryn-winevent-pump".to_string())
            .spawn(move || {
                run_win_event_pump_thread(pump_shared, startup_tx, stop_ack_tx);
            })
            .map_err(|_| CoverageFaultReason::HookRegistrationFailed)?;

        let timeout = Duration::from_millis(startup_timeout_ms.clamp(50, 5_000));
        match startup_rx.recv_timeout(timeout) {
            Ok(Ok((_tid, pinned_thread))) => Ok(Self {
                shared,
                pinned_thread,
                stop_ack_rx: Mutex::new(stop_ack_rx),
                join_handle: Mutex::new(Some(join_handle)),
                stop_called: AtomicBool::new(false),
                cleanup_quarantined: AtomicBool::new(false),
            }),
            Ok(Err(fault)) => {
                shared.stop_requested.store(true, Ordering::SeqCst);
                let _ = join_handle.join();
                Err(fault)
            }
            Err(_) => {
                shared.stop_requested.store(true, Ordering::SeqCst);
                let tid = shared.pump_thread_id.load(Ordering::SeqCst);
                if tid != 0 {
                    unsafe {
                        let _ = PostThreadMessageW(tid, WM_QUIT, WPARAM(0), LPARAM(0));
                    }
                }
                Err(CoverageFaultReason::HookRegistrationFailed)
            }
        }
    }

    fn record_stop_elapsed(&self, stop_start: Instant) -> Duration {
        let elapsed = stop_start.elapsed();
        #[cfg(feature = "lifecycle-test-support")]
        {
            let elapsed_ms = elapsed.as_millis().min(u128::from(u64::MAX)) as u64;
            self.shared
                .stop_elapsed_ms
                .store(elapsed_ms, Ordering::SeqCst);
        }
        elapsed
    }

    fn stop_bounded(&self, timeout_ms: u64) -> Result<(), CoverageFaultReason> {
        if self.stop_called.swap(true, Ordering::SeqCst) {
            if self.cleanup_quarantined.load(Ordering::SeqCst) {
                return Err(CoverageFaultReason::PumpExited);
            }
            return Ok(());
        }

        let budget_ms = timeout_ms.clamp(50, HOOK_STOP_TIMEOUT_MS);
        let total_budget = Duration::from_millis(budget_ms);
        #[cfg(feature = "lifecycle-test-support")]
        {
            self.shared
                .stop_budget_ms
                .store(budget_ms, Ordering::SeqCst);
        }
        let stop_start = Instant::now();

        self.shared.stop_requested.store(true, Ordering::SeqCst);
        let tid = self.shared.pump_thread_id.load(Ordering::SeqCst);
        if tid != 0 {
            unsafe {
                let _ = PostThreadMessageW(tid, WM_QUIT, WPARAM(0), LPARAM(0));
            }
        }

        let ack_ok = match self.stop_ack_rx.lock() {
            Ok(rx) => match rx.recv_timeout(total_budget) {
                Ok(unhooked) => {
                    unhooked
                        && self.shared.stop_acknowledged.load(Ordering::SeqCst)
                        && self.shared.unhook_called.load(Ordering::SeqCst)
                        && self.shared.unhook_succeeded.load(Ordering::SeqCst)
                        && self.shared.unhook_thread_id.load(Ordering::SeqCst) == tid
                        && tid != 0
                }
                Err(RecvTimeoutError::Timeout) | Err(RecvTimeoutError::Disconnected) => {
                    self.shared.stop_acknowledged.load(Ordering::SeqCst)
                        && self.shared.unhook_called.load(Ordering::SeqCst)
                        && self.shared.unhook_succeeded.load(Ordering::SeqCst)
                        && self.shared.unhook_thread_id.load(Ordering::SeqCst) == tid
                        && tid != 0
                }
            },
            Err(_) => false,
        };

        if !ack_ok {
            let _ = self.record_stop_elapsed(stop_start);
            self.cleanup_quarantined.store(true, Ordering::SeqCst);
            return Err(CoverageFaultReason::PumpExited);
        }

        // Enforce a single bounded stop deadline: never grant extra wait time after `total_budget`
        // has expired, and verify the pinned duplicated OS thread handle signals before `join()`.
        let Some(remaining_budget) = total_budget.checked_sub(stop_start.elapsed()) else {
            let _ = self.record_stop_elapsed(stop_start);
            self.cleanup_quarantined.store(true, Ordering::SeqCst);
            return Err(CoverageFaultReason::PumpExited);
        };
        if !self.pinned_thread.is_valid() {
            let _ = self.record_stop_elapsed(stop_start);
            self.cleanup_quarantined.store(true, Ordering::SeqCst);
            return Err(CoverageFaultReason::PumpExited);
        }
        let remaining_ms = remaining_budget
            .as_millis()
            .min(u128::from(HOOK_STOP_TIMEOUT_MS)) as u32;
        let wait_status =
            unsafe { WaitForSingleObject(self.pinned_thread.raw_handle(), remaining_ms) };
        if wait_status != WAIT_OBJECT_0 {
            let _ = self.record_stop_elapsed(stop_start);
            self.cleanup_quarantined.store(true, Ordering::SeqCst);
            return Err(CoverageFaultReason::PumpExited);
        }
        #[cfg(feature = "lifecycle-test-support")]
        {
            self.shared
                .pinned_thread_signaled_before_join
                .store(true, Ordering::SeqCst);
        }

        if let Ok(mut jg) = self.join_handle.lock() {
            if let Some(handle) = jg.take() {
                if handle.join().is_err() {
                    let _ = self.record_stop_elapsed(stop_start);
                    self.cleanup_quarantined.store(true, Ordering::SeqCst);
                    return Err(CoverageFaultReason::PumpExited);
                }
            } else {
                let _ = self.record_stop_elapsed(stop_start);
                self.cleanup_quarantined.store(true, Ordering::SeqCst);
                return Err(CoverageFaultReason::PumpExited);
            }
        } else {
            let _ = self.record_stop_elapsed(stop_start);
            self.cleanup_quarantined.store(true, Ordering::SeqCst);
            return Err(CoverageFaultReason::LockPoisoned);
        }

        let total_elapsed = self.record_stop_elapsed(stop_start);
        if total_elapsed > total_budget {
            self.cleanup_quarantined.store(true, Ordering::SeqCst);
            return Err(CoverageFaultReason::PumpExited);
        }
        #[cfg(feature = "lifecycle-test-support")]
        {
            self.shared
                .join_completed_within_budget
                .store(true, Ordering::SeqCst);
        }

        Ok(())
    }
}

fn run_win_event_pump_thread(
    shared: Arc<PumpSharedState>,
    startup_tx: mpsc::SyncSender<Result<(u32, PinnedThreadHandle), CoverageFaultReason>>,
    stop_ack_tx: mpsc::SyncSender<bool>,
) {
    let _tls_guard = ThreadLocalPumpContextGuard::install(&shared);
    let tid = unsafe { GetCurrentThreadId() };
    shared.pump_thread_id.store(tid, Ordering::SeqCst);

    // 0. Pin a duplicated OS thread handle and record creation time while the pump thread is live.
    #[cfg_attr(not(feature = "lifecycle-test-support"), allow(unused_variables))]
    let Some((pinned_thread, creation_ticks)) =
        PinnedThreadHandle::duplicate_current_thread_and_creation_time()
    else {
        shared.record_callback_fault(CoverageFaultReason::HookRegistrationFailed);
        let _ = startup_tx.send(Err(CoverageFaultReason::HookRegistrationFailed));
        return;
    };
    #[cfg(feature = "lifecycle-test-support")]
    {
        shared
            .pump_thread_creation_time
            .store(creation_ticks, Ordering::SeqCst);
        shared
            .pinned_thread_handle_valid
            .store(true, Ordering::SeqCst);
    }

    // 1. Force creation of the thread message queue BEFORE registering the WinEvent hook.
    unsafe {
        let mut init_msg = MSG::default();
        let _ = PeekMessageW(&mut init_msg, None, 0, 0, PM_NOREMOVE);
    }
    #[cfg(feature = "lifecycle-test-support")]
    {
        shared
            .message_queue_initialized
            .store(true, Ordering::SeqCst);
    }

    if shared.stop_requested.load(Ordering::SeqCst) {
        let _ = startup_tx.send(Err(CoverageFaultReason::HookRegistrationFailed));
        return;
    }

    // 2. Register out-of-context WinEvent hook for top-level/child window CREATE and DESTROY
    // across the desktop (keeping own-process events: flags == WINEVENT_OUTOFCONTEXT).
    #[cfg(feature = "lifecycle-test-support")]
    {
        shared
            .event_min
            .store(EVENT_OBJECT_CREATE, Ordering::SeqCst);
        shared
            .event_max
            .store(EVENT_OBJECT_DESTROY, Ordering::SeqCst);
        shared
            .hook_flags
            .store(WINEVENT_OUTOFCONTEXT, Ordering::SeqCst);
    }

    let hook = unsafe {
        SetWinEventHook(
            EVENT_OBJECT_CREATE,
            EVENT_OBJECT_DESTROY,
            None,
            Some(win_event_proc),
            0,
            0,
            WINEVENT_OUTOFCONTEXT,
        )
    };
    if hook.0.is_null() {
        shared.record_callback_fault(CoverageFaultReason::HookRegistrationFailed);
        let _ = startup_tx.send(Err(CoverageFaultReason::HookRegistrationFailed));
        return;
    }
    #[cfg(feature = "lifecycle-test-support")]
    {
        shared.hook_handle_nonzero.store(true, Ordering::SeqCst);
    }

    let timer_id = unsafe { SetTimer(None, PUMP_TIMER_ID, PUMP_HEARTBEAT_SLICE_MS, None) };
    shared.record_heartbeat();

    if startup_tx.send(Ok((tid, pinned_thread))).is_err() {
        unsafe {
            if timer_id != 0 {
                let _ = KillTimer(None, timer_id);
            }
            let unhook_tid = GetCurrentThreadId();
            shared.unhook_thread_id.store(unhook_tid, Ordering::SeqCst);
            shared.unhook_called.store(true, Ordering::SeqCst);
            let unhooked = UnhookWinEvent(hook).as_bool();
            shared.unhook_succeeded.store(unhooked, Ordering::SeqCst);
            shared.stop_acknowledged.store(true, Ordering::SeqCst);
        }
        let _ = stop_ack_tx.send(shared.unhook_succeeded.load(Ordering::SeqCst));
        return;
    }

    let mut exited_cleanly = false;
    loop {
        shared.record_heartbeat();
        if shared.stop_requested.load(Ordering::SeqCst) {
            exited_cleanly = true;
            break;
        }

        let mut msg = MSG::default();
        let mut quit_received = false;
        while unsafe { PeekMessageW(&mut msg, None, 0, 0, PM_REMOVE).as_bool() } {
            shared.record_heartbeat();
            if msg.message == WM_QUIT || shared.stop_requested.load(Ordering::SeqCst) {
                quit_received = true;
                break;
            }
            if msg.message != WM_TIMER {
                unsafe {
                    let _ = TranslateMessage(&msg);
                    DispatchMessageW(&msg);
                }
            }
        }

        if quit_received || shared.stop_requested.load(Ordering::SeqCst) {
            exited_cleanly = true;
            break;
        }

        let wait_res = unsafe {
            MsgWaitForMultipleObjectsEx(
                None,
                PUMP_HEARTBEAT_SLICE_MS,
                QS_ALLINPUT,
                MWMO_INPUTAVAILABLE,
            )
        };
        // WAIT_FAILED == 0xFFFF_FFFF
        if wait_res.0 == u32::MAX {
            shared.record_callback_fault(CoverageFaultReason::PumpExited);
            break;
        }
    }

    if !exited_cleanly && !shared.stop_requested.load(Ordering::SeqCst) {
        shared.pump_exited_unexpectedly.store(true, Ordering::SeqCst);
        shared.record_callback_fault(CoverageFaultReason::PumpExited);
    }

    unsafe {
        if timer_id != 0 {
            let _ = KillTimer(None, timer_id);
        }
        let unhook_tid = GetCurrentThreadId();
        shared.unhook_thread_id.store(unhook_tid, Ordering::SeqCst);
        shared.unhook_called.store(true, Ordering::SeqCst);
        let unhooked = UnhookWinEvent(hook).as_bool();
        shared.unhook_succeeded.store(unhooked, Ordering::SeqCst);
        shared.stop_acknowledged.store(true, Ordering::SeqCst);
    }
    let _ = stop_ack_tx.send(shared.unhook_succeeded.load(Ordering::SeqCst));
}

#[derive(Clone)]
enum LifecycleSource {
    Inert(CoverageFaultReason),
    Injected(Arc<InjectedLifecycleSource>),
    RealPump(Arc<PumpThreadHandle>),
}

/// Deterministic, hook-free injectable source for pure adapter and engine tests.
///
/// Never calls `SetWinEventHook` or spawns an OS thread.
pub struct InjectedLifecycleSource {
    shared: Arc<PumpSharedState>,
    expired_flag: AtomicBool,
    simulated_stop_timeout: AtomicBool,
    stop_called: AtomicBool,
}

impl InjectedLifecycleSource {
    pub fn new_healthy(hook_generation: u64, max_queue_capacity: usize) -> Arc<Self> {
        Arc::new(Self {
            shared: Arc::new(PumpSharedState::new(
                hook_generation,
                max_queue_capacity,
                PUMP_HEARTBEAT_EXPIRY_MS,
            )),
            expired_flag: AtomicBool::new(false),
            simulated_stop_timeout: AtomicBool::new(false),
            stop_called: AtomicBool::new(false),
        })
    }

    /// Enqueues a raw `(hwnd, kind, event_thread, event_time_ms)` event through the exact same
    /// bounded `SharedCallbackQueue` used by the real WinEvent callback.
    pub fn push_event(
        &self,
        hwnd: u64,
        kind: LifecycleEventKind,
        event_thread: u32,
        event_time_ms: u32,
    ) {
        let raw_event = match kind {
            LifecycleEventKind::ObjectCreate => EVENT_OBJECT_CREATE,
            LifecycleEventKind::ObjectDestroy => EVENT_OBJECT_DESTROY,
        };
        self.shared.on_raw_win_event(
            raw_event,
            HWND(hwnd as usize as *mut std::ffi::c_void),
            OBJID_WINDOW.0,
            CHILDID_SELF as i32,
            event_thread,
            event_time_ms,
        );
    }

    /// Pushes a pre-built `DeliveredWindowEvent` directly (e.g., to test arrivalSeq gaps or hookGeneration mismatches).
    pub fn push_raw_delivered_event(&self, event: DeliveredWindowEvent) {
        if let Ok(mut guard) = self.shared.queue.lock() {
            if guard.events.len() >= guard.max_queue_capacity {
                guard.latch_fault(CoverageFaultReason::QueueOverflow);
                self.shared
                    .record_callback_fault(CoverageFaultReason::QueueOverflow);
                return;
            }
            guard.events.push_back(event);
        } else {
            self.shared
                .record_callback_fault(CoverageFaultReason::LockPoisoned);
        }
    }

    pub fn inject_fault(&self, reason: CoverageFaultReason) {
        if let Ok(mut guard) = self.shared.queue.lock() {
            guard.latch_fault(reason);
        }
        self.shared.record_callback_fault(reason);
    }

    pub fn inject_heartbeat_expired(&self) {
        self.expired_flag.store(true, Ordering::SeqCst);
    }

    pub fn inject_unexpected_pump_exit(&self) {
        self.shared
            .pump_exited_unexpectedly
            .store(true, Ordering::SeqCst);
        self.shared
            .record_callback_fault(CoverageFaultReason::PumpExited);
    }

    pub fn set_simulate_stop_timeout(&self, timeout: bool) {
        self.simulated_stop_timeout.store(timeout, Ordering::SeqCst);
    }

    pub fn was_stopped(&self) -> bool {
        self.stop_called.load(Ordering::SeqCst)
    }
}

struct LifecycleServiceInner {
    registry: WindowLifecycleRegistry,
    pending_retirements: VecDeque<RetiredWindowIdentity>,
    cleanup_quarantined: bool,
}

impl LifecycleServiceInner {
    fn enqueue_retirements(&mut self, batch: RetirementBatch) {
        for item in batch.retired {
            if self.pending_retirements.len() >= MAX_RETIREMENT_BACKLOG {
                let _ = self
                    .registry
                    .mark_coverage_unhealthy(CoverageFaultReason::QueueOverflow);
                self.pending_retirements.clear();
                return;
            }
            self.pending_retirements.push_back(item);
        }
    }
}

/// Service-backed window lifecycle authority shared across `PhysicalActorEngine` and all
/// `WindowIdentityTracker` clones.
#[derive(Clone)]
pub struct LifecycleService {
    host_nonce: HostWorkerNonce,
    worker_instance: String,
    source: LifecycleSource,
    inner: Arc<Mutex<LifecycleServiceInner>>,
}

impl LifecycleService {
    /// Starts the real dedicated WinEvent pump thread (`EVENT_OBJECT_CREATE` / `EVENT_OBJECT_DESTROY`)
    /// and returns an error unless the pump's message queue and WinEvent registration are ready.
    pub fn start_real() -> Result<Self, ProtocolError> {
        Self::start_real_with_capacity(
            DEFAULT_MAX_TRACKED_WINDOWS,
            DEFAULT_HOOK_QUEUE_CAPACITY,
            PUMP_HEARTBEAT_EXPIRY_MS,
            HOOK_STARTUP_TIMEOUT_MS,
        )
    }

    pub fn start_real_with_capacity(
        max_tracked_windows: usize,
        max_queue_capacity: usize,
        heartbeat_expiry_ms: u64,
        startup_timeout_ms: u64,
    ) -> Result<Self, ProtocolError> {
        let host_nonce = generate_host_worker_nonce()?;
        let worker_instance = host_nonce.worker_instance_label();
        let hook_generation = 1u64;
        let registry =
            WindowLifecycleRegistry::new(host_nonce, hook_generation, max_tracked_windows)
                .map_err(|_| {
                    ProtocolError::observe(
                        ErrorCode::InternalError,
                        DiagnosticReason::DesktopIdentityLookupFailed,
                    )
                })?;

        let pump = PumpThreadHandle::spawn_started(
            hook_generation,
            max_queue_capacity,
            heartbeat_expiry_ms,
            startup_timeout_ms,
        )
        .map_err(|_| {
            ProtocolError::observe(
                ErrorCode::ObservationUnavailable,
                DiagnosticReason::DesktopIdentityLookupFailed,
            )
        })?;

        Ok(Self {
            host_nonce,
            worker_instance,
            source: LifecycleSource::RealPump(Arc::new(pump)),
            inner: Arc::new(Mutex::new(LifecycleServiceInner {
                registry,
                pending_retirements: VecDeque::with_capacity(64),
                cleanup_quarantined: false,
            })),
        })
    }

    /// Constructs an explicitly inert, hook-free lifecycle service for pure protocol/validation tests.
    ///
    /// Never starts a WinEvent hook or pump thread, and immediately marks coverage unhealthy
    /// (`HookRegistrationFailed`) so it can never issue valid live `WindowRef`s, `stateToken`s, or actions.
    pub fn new_inert_for_test(host_nonce: HostWorkerNonce) -> Self {
        Self::new_inert_with_reason_for_test(
            host_nonce,
            CoverageFaultReason::HookRegistrationFailed,
        )
    }

    pub fn new_inert_with_reason_for_test(
        host_nonce: HostWorkerNonce,
        reason: CoverageFaultReason,
    ) -> Self {
        let worker_instance = host_nonce.worker_instance_label();
        let mut registry =
            WindowLifecycleRegistry::new(host_nonce, 1, DEFAULT_MAX_TRACKED_WINDOWS)
                .expect("valid default capacity");
        let _ = registry.mark_coverage_unhealthy(reason);
        Self {
            host_nonce,
            worker_instance,
            source: LifecycleSource::Inert(reason),
            inner: Arc::new(Mutex::new(LifecycleServiceInner {
                registry,
                pending_retirements: VecDeque::new(),
                cleanup_quarantined: false,
            })),
        }
    }

    /// Constructs a deterministic, hook-free lifecycle service backed by an `InjectedLifecycleSource`
    /// for adapter/engine unit tests.
    pub fn new_with_injected_source_for_test(
        host_nonce: HostWorkerNonce,
        hook_generation: u64,
        max_tracked_windows: usize,
        injected: Arc<InjectedLifecycleSource>,
    ) -> Result<Self, LifecycleError> {
        let worker_instance = host_nonce.worker_instance_label();
        let registry =
            WindowLifecycleRegistry::new(host_nonce, hook_generation, max_tracked_windows)?;
        Ok(Self {
            host_nonce,
            worker_instance,
            source: LifecycleSource::Injected(injected),
            inner: Arc::new(Mutex::new(LifecycleServiceInner {
                registry,
                pending_retirements: VecDeque::with_capacity(64),
                cleanup_quarantined: false,
            })),
        })
    }

    pub fn host_nonce(&self) -> HostWorkerNonce {
        self.host_nonce
    }

    pub fn worker_instance(&self) -> &str {
        &self.worker_instance
    }

    pub fn coverage_state(&self) -> CoverageState {
        match self.inner.lock() {
            Ok(guard) => guard.registry.coverage_state(),
            Err(_) => CoverageState::Unhealthy(CoverageFaultReason::LockPoisoned),
        }
    }

    /// Check live hook health without consuming retirement notifications needed by the actor.
    pub fn ensure_healthy(&self) -> Result<(), LifecycleError> {
        let mut inner = self.inner.lock().map_err(|_| {
            LifecycleError::CoverageUnhealthy(CoverageFaultReason::LockPoisoned)
        })?;
        self.sync_pump_into_inner(&mut inner, DEFAULT_EVENT_DRAIN_BUDGET)?;
        if let CoverageState::Unhealthy(reason) = inner.registry.coverage_state() {
            return Err(LifecycleError::CoverageUnhealthy(reason));
        }
        Ok(())
    }

    pub fn is_cleanup_quarantined(&self) -> bool {
        match self.inner.lock() {
            Ok(guard) => guard.cleanup_quarantined,
            Err(_) => true,
        }
    }

    pub fn pump_thread_id(&self) -> Option<u32> {
        match &self.source {
            LifecycleSource::RealPump(pump) => {
                let tid = pump.shared.pump_thread_id.load(Ordering::SeqCst);
                if tid != 0 {
                    Some(tid)
                } else {
                    None
                }
            }
            _ => None,
        }
    }

    /// Synchronizes pump health and drains up to `max_batch` delivered WinEvent records into the
    /// underlying `WindowLifecycleRegistry`, returning all newly + previously pending retired
    /// window identities in a single bounded `RetirementBatch`.
    pub fn drain_and_take_retirements(
        &self,
        max_batch: usize,
    ) -> Result<RetirementBatch, LifecycleError> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| LifecycleError::CoverageUnhealthy(CoverageFaultReason::LockPoisoned))?;

        let sync_res = self.sync_pump_into_inner(&mut inner, max_batch);
        let drained_retired: Vec<RetiredWindowIdentity> =
            inner.pending_retirements.drain(..).collect();
        let batch = RetirementBatch {
            retired: drained_retired,
            coverage_state: inner.registry.coverage_state(),
            coverage_generation: inner.registry.coverage_generation(),
            last_delivered_arrival_seq: inner.registry.last_delivered_arrival_seq(),
        };

        match sync_res {
            Ok(()) => Ok(batch),
            Err(err) => Err(err),
        }
    }

    /// Drains pending events and issues or reuses a window identity for `live_os_tuple`.
    /// Any capacity eviction or tuple-change retirement is queued into `pending_retirements`
    /// and also included in `IssueOutcome.retirement_batch`.
    pub fn issue_or_reuse_window(
        &self,
        live_os_tuple: WindowOsTuple,
    ) -> Result<IssueOutcome, LifecycleError> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| LifecycleError::CoverageUnhealthy(CoverageFaultReason::LockPoisoned))?;

        self.sync_pump_into_inner(&mut inner, DEFAULT_EVENT_DRAIN_BUDGET)?;
        let outcome = inner.registry.issue_or_reuse(live_os_tuple)?;
        if !outcome.retirement_batch.is_empty() {
            inner.enqueue_retirements(outcome.retirement_batch.clone());
        }
        Ok(outcome)
    }

    /// Drains pending events and validates a `WindowRef` (plus optional explicit `expected_stamp`)
    /// against current coverage health, registry state, and the caller-verified `live_os_tuple`.
    ///
    /// Never mints a new identity when validating an existing `WindowRef`.
    pub fn validate_window_ref_and_stamp(
        &self,
        wref: &WindowRef,
        live_os_tuple: WindowOsTuple,
        expected_stamp: Option<&LifecycleStamp>,
    ) -> Result<LifecycleStamp, LifecycleError> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| LifecycleError::CoverageUnhealthy(CoverageFaultReason::LockPoisoned))?;

        self.sync_pump_into_inner(&mut inner, DEFAULT_EVENT_DRAIN_BUDGET)?;

        if let CoverageState::Unhealthy(reason) = inner.registry.coverage_state() {
            return Err(LifecycleError::CoverageUnhealthy(reason));
        }

        if let Some(stamp) = expected_stamp {
            if stamp.issue_generation != wref.generation {
                return Err(LifecycleError::WindowGenerationMismatch {
                    hwnd: live_os_tuple.hwnd(),
                    expected_generation: wref.generation,
                    actual_generation: stamp.issue_generation,
                });
            }
            if stamp.window_key != wref.window_key {
                return Err(LifecycleError::WindowKeyMismatch {
                    hwnd: live_os_tuple.hwnd(),
                });
            }
            if stamp.os_tuple != live_os_tuple {
                return Err(LifecycleError::LiveOsTupleMismatch {
                    hwnd: live_os_tuple.hwnd(),
                    expected: stamp.os_tuple,
                    actual: live_os_tuple,
                });
            }
            return inner.registry.validate_stamp(stamp, live_os_tuple);
        }

        // When validating a client `WindowRef` without a snapshot stamp, construct the probe stamp
        // using the active slot's `target_event_revision` only if the slot's active identity still
        // matches `wref.generation` and `wref.window_key`. If the slot was retired, evicted, or moved
        // to a newer generation, `validate_stamp` with `target_event_revision: 0` deterministically
        // rejects it without ever minting a new generation.
        let synthetic_stamp = LifecycleStamp {
            host_nonce: self.host_nonce,
            coverage_generation: inner.registry.coverage_generation(),
            os_tuple: live_os_tuple,
            issue_generation: wref.generation,
            window_key: wref.window_key.clone(),
            target_event_revision: 0,
            observed_arrival_seq: inner.registry.last_delivered_arrival_seq(),
        };

        // Check if the window is currently active with the exact same generation and key; if so,
        // use its issued `target_event_revision` (which is still equal to slot.target_event_revision
        // because any delivered event since issuance would have cleared `slot.active`).
        match inner.registry.validate_stamp(&synthetic_stamp, live_os_tuple) {
            Ok(valid) => Ok(valid),
            Err(LifecycleError::TargetEventRevisionAdvanced {
                hwnd,
                current_revision,
                ..
            }) => {
                let rev_probe = LifecycleStamp {
                    target_event_revision: current_revision,
                    ..synthetic_stamp
                };
                inner.registry.validate_stamp(&rev_probe, live_os_tuple).map_err(|e| match e {
                    LifecycleError::WindowGenerationRetiredOrEvicted { .. } => {
                        LifecycleError::TargetEventRevisionAdvanced {
                            hwnd,
                            stamp_revision: 0,
                            current_revision,
                        }
                    }
                    other => other,
                })
            }
            Err(other) => Err(other),
        }
    }

    /// Begins a `state.get` observation for an existing caller-supplied `WindowRef` and its verified
    /// `live_os_tuple`.
    ///
    /// First verifies that `wref` matches the currently active issued identity (never silently
    /// minting a new generation for a stale `WindowRef`), then records an `ObservationCheckpoint`.
    pub fn begin_observation_for_window_ref(
        &self,
        wref: &WindowRef,
        live_os_tuple: WindowOsTuple,
    ) -> Result<ObservationCheckpoint, LifecycleError> {
        let verified_stamp = self.validate_window_ref_and_stamp(wref, live_os_tuple, None)?;
        Ok(ObservationCheckpoint {
            stamp: verified_stamp,
        })
    }

    /// Completes a `state.get` observation window by draining any newly arrived WinEvent records
    /// and verifying that coverage and the target window's `ObservationCheckpoint` remain valid.
    pub fn finish_observation_for_window_ref(
        &self,
        checkpoint: &ObservationCheckpoint,
        live_os_tuple: WindowOsTuple,
    ) -> Result<LifecycleStamp, LifecycleError> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| LifecycleError::CoverageUnhealthy(CoverageFaultReason::LockPoisoned))?;

        self.sync_pump_into_inner(&mut inner, DEFAULT_EVENT_DRAIN_BUDGET)?;
        inner.registry.finish_observation(checkpoint, live_os_tuple)
    }

    /// Explicitly faults coverage with `reason` and queues all retired window identities for
    /// adapter invalidation.
    pub fn fault_coverage(&self, reason: CoverageFaultReason) -> RetirementBatch {
        match self.inner.lock() {
            Ok(mut inner) => {
                let batch = inner.registry.mark_coverage_unhealthy(reason);
                inner.enqueue_retirements(batch.clone());
                batch
            }
            Err(_) => RetirementBatch {
                retired: Vec::new(),
                coverage_state: CoverageState::Unhealthy(CoverageFaultReason::LockPoisoned),
                coverage_generation: 1,
                last_delivered_arrival_seq: 0,
            },
        }
    }

    /// Signals the WinEvent pump thread to stop, unhook `HWINEVENTHOOK` on its owning thread,
    /// and waits up to `HOOK_STOP_TIMEOUT_MS` for explicit acknowledgement and thread exit.
    ///
    /// Idempotent across repeated calls (`session.end`, `shutdown`, `handle_eof`, `Drop`).
    /// If the pump fails to acknowledge stop within the bounded timeout, marks coverage unhealthy
    /// and sets `cleanup_quarantined = true` (never claiming clean detached-thread shutdown).
    pub fn shutdown_bounded(&self) -> Result<(), ProtocolError> {
        let stop_res = match &self.source {
            LifecycleSource::Inert(_) => Ok(()),
            LifecycleSource::Injected(inj) => {
                inj.stop_called.store(true, Ordering::SeqCst);
                if inj.simulated_stop_timeout.load(Ordering::SeqCst) {
                    Err(CoverageFaultReason::PumpExited)
                } else {
                    Ok(())
                }
            }
            LifecycleSource::RealPump(pump) => pump.stop_bounded(HOOK_STOP_TIMEOUT_MS),
        };

        if let Ok(mut inner) = self.inner.lock() {
            match stop_res {
                Ok(()) => {
                    let batch = inner
                        .registry
                        .mark_coverage_unhealthy(CoverageFaultReason::PumpExited);
                    inner.enqueue_retirements(batch);
                    Ok(())
                }
                Err(fault) => {
                    inner.cleanup_quarantined = true;
                    let batch = inner.registry.mark_coverage_unhealthy(fault);
                    inner.enqueue_retirements(batch);
                    Err(ProtocolError::session(
                        ErrorCode::InternalError,
                        DiagnosticReason::DesktopIdentityLookupFailed,
                    ))
                }
            }
        } else {
            Err(ProtocolError::session(
                ErrorCode::InternalError,
                DiagnosticReason::DesktopIdentityLookupFailed,
            ))
        }
    }

    fn sync_pump_into_inner(
        &self,
        inner: &mut LifecycleServiceInner,
        max_batch: usize,
    ) -> Result<(), LifecycleError> {
        let shared = match &self.source {
            LifecycleSource::Inert(reason) => {
                let batch = inner.registry.mark_coverage_unhealthy(*reason);
                inner.enqueue_retirements(batch);
                return Err(LifecycleError::CoverageUnhealthy(*reason));
            }
            LifecycleSource::Injected(inj) => {
                if inj.expired_flag.load(Ordering::SeqCst) {
                    let batch = inner
                        .registry
                        .mark_coverage_unhealthy(CoverageFaultReason::HeartbeatExpired);
                    inner.enqueue_retirements(batch);
                    return Err(LifecycleError::CoverageUnhealthy(
                        CoverageFaultReason::HeartbeatExpired,
                    ));
                }
                &inj.shared
            }
            LifecycleSource::RealPump(pump) => &pump.shared,
        };

        if shared.pump_exited_unexpectedly.load(Ordering::SeqCst) {
            let batch = inner
                .registry
                .mark_coverage_unhealthy(CoverageFaultReason::PumpExited);
            inner.enqueue_retirements(batch);
            return Err(LifecycleError::CoverageUnhealthy(
                CoverageFaultReason::PumpExited,
            ));
        }

        if let Some(fault) = decode_fault_code(shared.callback_fault_code.load(Ordering::SeqCst)) {
            let batch = inner.registry.mark_coverage_unhealthy(fault);
            inner.enqueue_retirements(batch);
            return Err(LifecycleError::CoverageUnhealthy(fault));
        }

        if shared.is_heartbeat_expired() {
            let batch = inner
                .registry
                .mark_coverage_unhealthy(CoverageFaultReason::HeartbeatExpired);
            inner.enqueue_retirements(batch);
            return Err(LifecycleError::CoverageUnhealthy(
                CoverageFaultReason::HeartbeatExpired,
            ));
        }

        let drained_events: Vec<DeliveredWindowEvent> = {
            let mut q_guard = match shared.queue.lock() {
                Ok(g) => g,
                Err(_) => {
                    let batch = inner
                        .registry
                        .mark_coverage_unhealthy(CoverageFaultReason::LockPoisoned);
                    inner.enqueue_retirements(batch);
                    return Err(LifecycleError::CoverageUnhealthy(
                        CoverageFaultReason::LockPoisoned,
                    ));
                }
            };

            if let Some(fault) = q_guard.fault {
                let batch = inner.registry.mark_coverage_unhealthy(fault);
                inner.enqueue_retirements(batch);
                return Err(LifecycleError::CoverageUnhealthy(fault));
            }

            if q_guard.events.len() > max_batch {
                let pending = q_guard.events.len();
                return Err(LifecycleError::EventStormBudgetExceeded {
                    pending,
                    max_batch,
                });
            }

            q_guard.events.drain(..).collect()
        };

        if !drained_events.is_empty() {
            match inner.registry.drain_events_bounded(&drained_events, max_batch) {
                Ok(outcome) => {
                    #[cfg(feature = "lifecycle-test-support")]
                    {
                        match shared.diag_trace.lock() {
                            Ok(mut t_guard) => {
                                for (ev_idx, ev) in drained_events.iter().enumerate() {
                                    let is_first_for_hwnd = !drained_events[..ev_idx]
                                        .iter()
                                        .any(|prev| prev.hwnd == ev.hwnd);
                                    let retired_gen = if is_first_for_hwnd {
                                        outcome
                                            .retirement_batch
                                            .retired
                                            .iter()
                                            .find(|r| {
                                                r.hwnd == ev.hwnd
                                                    && r.reason
                                                        == RetirementReason::DeliveredEvent(ev.kind)
                                            })
                                            .map(|r| r.issue_generation)
                                    } else {
                                        None
                                    };
                                    if let Ok(pos) = t_guard
                                        .entries
                                        .binary_search_by_key(&ev.arrival_seq, |t| t.arrival_seq)
                                    {
                                        t_guard.entries[pos].applied_to_registry = true;
                                        if retired_gen.is_some() {
                                            t_guard.entries[pos].retired_issue_generation =
                                                retired_gen;
                                        }
                                    } else {
                                        t_guard.trace_overflowed = true;
                                    }
                                }
                            }
                            Err(_) => {
                                shared.diag_trace_incomplete.store(true, Ordering::SeqCst);
                            }
                        }
                    }
                    if !outcome.retirement_batch.is_empty() {
                        inner.enqueue_retirements(outcome.retirement_batch);
                    }
                }
                Err(err) => {
                    if let LifecycleError::CoverageUnhealthy(reason) = err {
                        let batch = inner.registry.mark_coverage_unhealthy(reason);
                        inner.enqueue_retirements(batch);
                    }
                    return Err(err);
                }
            }
        }

        if let CoverageState::Unhealthy(reason) = inner.registry.coverage_state() {
            return Err(LifecycleError::CoverageUnhealthy(reason));
        }

        Ok(())
    }

    #[cfg(feature = "lifecycle-test-support")]
    pub fn snapshot_producer_diagnostics_for_hwnd(
        &self,
        target_hwnd: u64,
    ) -> Result<LifecycleProducerDiagnostics, LifecycleError> {
        let (coverage_state, coverage_generation, inner_quarantined) = {
            let inner = self.inner.lock().map_err(|_| {
                LifecycleError::CoverageUnhealthy(CoverageFaultReason::LockPoisoned)
            })?;
            (
                inner.registry.coverage_state(),
                inner.registry.coverage_generation(),
                inner.cleanup_quarantined,
            )
        };

        let (shared, pump_quarantined, pinned_valid) = match &self.source {
            LifecycleSource::RealPump(pump) => (
                Some(&pump.shared),
                pump.cleanup_quarantined.load(Ordering::SeqCst),
                pump.pinned_thread.is_valid(),
            ),
            LifecycleSource::Injected(inj) => (Some(&inj.shared), false, false),
            LifecycleSource::Inert(_) => (None, false, false),
        };

        let Some(shared) = shared else {
            return Ok(LifecycleProducerDiagnostics {
                coverage_state,
                coverage_generation,
                last_heartbeat_ns: 0,
                heartbeat_count: 0,
                registration: HookRegistrationDiagnostics {
                    pump_thread_id: 0,
                    pump_thread_creation_time: 0,
                    pinned_thread_handle_valid: false,
                    message_queue_initialized: false,
                    hook_handle_nonzero: false,
                    hook_generation: coverage_generation,
                    event_min: 0,
                    event_max: 0,
                    hook_flags: 0,
                },
                shutdown: HookShutdownDiagnostics {
                    stop_requested: false,
                    stop_acknowledged: false,
                    unhook_called: false,
                    unhook_succeeded: false,
                    unhook_thread_id: 0,
                    pinned_thread_signaled_before_join: false,
                    join_completed_within_budget: false,
                    stop_elapsed_ms: 0,
                    stop_budget_ms: 0,
                    cleanup_quarantined: inner_quarantined,
                },
                total_accepted_callbacks: 0,
                trace_overflowed: false,
                matched_target_traces: Vec::new(),
            });
        };

        // Copy matching fixed-size entries into a stack buffer while briefly holding the separate
        // diagnostic mutex (never the callback queue mutex), then allocate `Vec` outside all locks.
        let mut stack_matches: [Option<CallbackTraceEntry>; MAX_MATCHED_TARGET_TRACES] =
            [None; MAX_MATCHED_TARGET_TRACES];
        let mut matched_len = 0usize;
        let mut target_match_overflowed = false;
        let (total_accepted_callbacks, raw_trace_overflowed) = match shared.diag_trace.lock() {
            Ok(t_guard) => {
                if target_hwnd != 0 {
                    for entry in &t_guard.entries {
                        if entry.hwnd == target_hwnd {
                            if matched_len < MAX_MATCHED_TARGET_TRACES {
                                stack_matches[matched_len] = Some(*entry);
                                matched_len += 1;
                            } else {
                                target_match_overflowed = true;
                                break;
                            }
                        }
                    }
                }
                (t_guard.total_accepted_callbacks, t_guard.trace_overflowed)
            }
            Err(_) => (0, true),
        };

        let mut matched_target_traces = Vec::with_capacity(matched_len);
        for entry in stack_matches.into_iter().take(matched_len).flatten() {
            matched_target_traces.push(CallbackTraceRecord {
                raw_event: entry.raw_event,
                kind: entry.kind,
                hwnd: entry.hwnd,
                id_object: entry.id_object,
                id_child: entry.id_child,
                arrival_seq: entry.arrival_seq,
                hook_generation: entry.hook_generation,
                source_event_thread: entry.source_event_thread,
                actual_callback_tid: entry.actual_callback_tid,
                event_time_ms: entry.event_time_ms,
                monotonic_timestamp_ns: entry.monotonic_timestamp_ns,
                applied_to_registry: entry.applied_to_registry,
                retired_issue_generation: entry.retired_issue_generation,
            });
        }

        let trace_overflowed = raw_trace_overflowed
            || target_match_overflowed
            || shared.diag_trace_incomplete.load(Ordering::SeqCst);

        Ok(LifecycleProducerDiagnostics {
            coverage_state,
            coverage_generation,
            last_heartbeat_ns: shared.last_heartbeat_ns.load(Ordering::SeqCst),
            heartbeat_count: shared.heartbeat_count.load(Ordering::SeqCst),
            registration: HookRegistrationDiagnostics {
                pump_thread_id: shared.pump_thread_id.load(Ordering::SeqCst),
                pump_thread_creation_time: shared.pump_thread_creation_time.load(Ordering::SeqCst),
                pinned_thread_handle_valid: pinned_valid
                    && shared.pinned_thread_handle_valid.load(Ordering::SeqCst),
                message_queue_initialized: shared.message_queue_initialized.load(Ordering::SeqCst),
                hook_handle_nonzero: shared.hook_handle_nonzero.load(Ordering::SeqCst),
                hook_generation: shared.hook_generation,
                event_min: shared.event_min.load(Ordering::SeqCst),
                event_max: shared.event_max.load(Ordering::SeqCst),
                hook_flags: shared.hook_flags.load(Ordering::SeqCst),
            },
            shutdown: HookShutdownDiagnostics {
                stop_requested: shared.stop_requested.load(Ordering::SeqCst),
                stop_acknowledged: shared.stop_acknowledged.load(Ordering::SeqCst),
                unhook_called: shared.unhook_called.load(Ordering::SeqCst),
                unhook_succeeded: shared.unhook_succeeded.load(Ordering::SeqCst),
                unhook_thread_id: shared.unhook_thread_id.load(Ordering::SeqCst),
                pinned_thread_signaled_before_join: shared
                    .pinned_thread_signaled_before_join
                    .load(Ordering::SeqCst),
                join_completed_within_budget: shared
                    .join_completed_within_budget
                    .load(Ordering::SeqCst),
                stop_elapsed_ms: shared.stop_elapsed_ms.load(Ordering::SeqCst),
                stop_budget_ms: shared.stop_budget_ms.load(Ordering::SeqCst),
                cleanup_quarantined: inner_quarantined || pump_quarantined,
            },
            total_accepted_callbacks,
            trace_overflowed,
            matched_target_traces,
        })
    }
}

impl Drop for LifecycleService {
    fn drop(&mut self) {
        if let LifecycleSource::RealPump(ref pump) = self.source {
            // Only stop the real pump when the last `LifecycleService` clone is being dropped,
            // or if shutdown was already requested.
            if Arc::strong_count(pump) == 1 || pump.shared.stop_requested.load(Ordering::SeqCst) {
                let _ = pump.stop_bounded(HOOK_STOP_TIMEOUT_MS);
            }
        }
    }
}

/// Maps a `LifecycleError` onto the frozen wire-v1 `ProtocolError` codes and `DiagnosticReason`s
/// for `stage` (`ErrorStage::Observe` or `ErrorStage::Validate`).
pub fn map_lifecycle_error_to_protocol(
    err: LifecycleError,
    stage: ErrorStage,
    target_hwnd: Option<u64>,
) -> ProtocolError {
    let make = |code: ErrorCode, reason: DiagnosticReason| -> ProtocolError {
        let base = match stage {
            ErrorStage::Observe => ProtocolError::observe(code, reason),
            ErrorStage::Prepare => ProtocolError::prepare(code, reason),
            _ => ProtocolError::validate(code, reason),
        };
        base.with_hwnds(target_hwnd, None)
    };

    match err {
        LifecycleError::InvalidOsTuple(
            crate::lifecycle_model::InvalidOsTupleReason::ZeroHwnd
            | crate::lifecycle_model::InvalidOsTupleReason::ZeroPid,
        )
        | LifecycleError::InvalidEventZeroHwnd => {
            make(ErrorCode::WindowNotFound, DiagnosticReason::IsWindowFalse)
        }
        LifecycleError::InvalidOsTuple(
            crate::lifecycle_model::InvalidOsTupleReason::ZeroProcessCreationTime,
        ) => make(
            ErrorCode::WindowStale,
            DiagnosticReason::ProcessCreationTimeMismatch,
        ),
        LifecycleError::HostWorkerNonceMismatch { .. } => make(
            ErrorCode::WindowStale,
            DiagnosticReason::WorkerInstanceMismatch,
        ),
        LifecycleError::LiveOsTupleMismatch {
            expected, actual, ..
        } => {
            if expected.pid() != actual.pid() {
                make(ErrorCode::WindowStale, DiagnosticReason::PidMismatch)
            } else {
                make(
                    ErrorCode::WindowStale,
                    DiagnosticReason::ProcessCreationTimeMismatch,
                )
            }
        }
        LifecycleError::CoverageGenerationMismatch { .. }
        | LifecycleError::WindowGenerationRetiredOrEvicted { .. }
        | LifecycleError::WindowGenerationMismatch { .. }
        | LifecycleError::WindowKeyMismatch { .. }
        | LifecycleError::TargetEventRevisionAdvanced { .. }
        | LifecycleError::CoverageUnhealthy(_)
        | LifecycleError::EventStormBudgetExceeded { .. }
        | LifecycleError::IssueGenerationOverflow
        | LifecycleError::HostNonceHistoryExhausted { .. } => {
            if stage == ErrorStage::Observe {
                make(
                    ErrorCode::ObservationUnavailable,
                    DiagnosticReason::GenerationMismatch,
                )
            } else {
                make(ErrorCode::WindowStale, DiagnosticReason::GenerationMismatch)
            }
        }
        LifecycleError::InvalidCapacity { .. }
        | LifecycleError::InvalidHostWorkerNonce
        | LifecycleError::InvalidCoverageGeneration
        | LifecycleError::RestartRequiresNewCoverageGeneration { .. }
        | LifecycleError::RestartRequiresFreshHostNonce => make(
            ErrorCode::InternalError,
            DiagnosticReason::DesktopIdentityLookupFailed,
        ),
    }
}

/// Validates wire-v1 `WindowRef` fields and parses `(hwnd, pid, process_started)` into a
/// `WindowOsTuple` before querying live Win32 state.
pub fn parse_window_ref_os_tuple(
    wref: &WindowRef,
    expected_worker_instance: &str,
) -> Result<WindowOsTuple, ProtocolError> {
    if wref.worker_instance != expected_worker_instance {
        return Err(ProtocolError::validate(
            ErrorCode::WindowStale,
            DiagnosticReason::WorkerInstanceMismatch,
        ));
    }
    let hwnd_val = parse_decimal_u64(&wref.hwnd)?;
    let expected_started = parse_decimal_u64(&wref.process_started)?;
    if expected_started == 0 {
        return Err(ProtocolError::validate(
            ErrorCode::WindowStale,
            DiagnosticReason::ProcessCreationTimeMismatch,
        )
        .with_hwnds(Some(hwnd_val), None));
    }
    if hwnd_val == 0 {
        return Err(ProtocolError::validate(
            ErrorCode::WindowNotFound,
            DiagnosticReason::IsWindowFalse,
        )
        .with_hwnds(Some(0), None));
    }
    if wref.pid == 0 {
        return Err(ProtocolError::validate(
            ErrorCode::WindowStale,
            DiagnosticReason::PidMismatch,
        )
        .with_hwnds(Some(hwnd_val), None));
    }
    WindowOsTuple::new(hwnd_val, wref.pid, expected_started).map_err(|e| {
        map_lifecycle_error_to_protocol(e, ErrorStage::Validate, Some(hwnd_val))
    })
}
