use crate::capture::capture_window_screenshot;
use crate::errors::{
    DiagnosticReason, ErrorCode, ErrorStage, InternalMutationDetail, ProtocolError,
};
use crate::focus::{ensure_window_focused, DEFAULT_FOCUS_DEADLINE_MS};
use crate::input::{execute_resolved_action, resolve_action_plan, ResolvedActionPlan};
use crate::input_lock::{
    DesktopInputLock, DesktopInputLockGuard, DEFAULT_LOCK_WAIT_TIMEOUT_MS,
    DEFAULT_READY_TICKET_TTL_MS, MAX_READY_TICKET_TTL_MS,
};
use crate::lifecycle::{generate_host_worker_nonce, LifecycleService};
use crate::lifecycle_model::{
    DEFAULT_EVENT_DRAIN_BUDGET, LifecycleStamp, RetirementBatch,
};
use crate::protocol::{
    AccessibilityOptions, ActionCancelParams, ActionCancelResult, ActionCommitParams,
    ActionCommitResult, ActionPrepareParams, ActionPrepareResult, ActionReadyEventData, ActionSpec,
    AppsLaunchParams, AppsListParams, EventEnvelope, InitializeParams, InitializeResult,
    Observation, RequestEnvelope, RequestId, ResponseEnvelope, ScreenshotOptions,
    ServerCapabilities, ServerLimits, SessionEndParams, SessionEndResult, ShutdownParams,
    ShutdownResult, StateGetParams, WindowRef, WindowsGetParams, WindowsListParams, WorkerInfo,
    MAX_COMMAND_LINE_BYTES, MAX_PNG_BYTES, MAX_RESPONSE_FRAME_BYTES, MAX_UIA_DEPTH_CAP,
    MAX_UIA_ELEMENTS_CAP, MAX_WINDOWS_CAP, PROTOCOL_SEMVER,
};
use crate::snapshot::{
    compute_action_fingerprint, generate_artifact_id, generate_prepare_ticket, SnapshotStore,
};
use crate::uia::observe_window_accessibility;
use crate::windows::{
    current_worker_pid, get_foreground_status, get_window_geometry, handle_apps_launch,
    handle_apps_list, handle_windows_get, handle_windows_list, init_process_dpi_awareness,
    now_iso8601_utc, WindowIdentityTracker,
};
use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use windows::Win32::UI::WindowsAndMessaging::IsIconic;

/// Thread-safe control table shared between the independent stdin reader thread
/// and the physical actor thread so cancellation signals take effect immediately
/// even while the actor thread is waiting on the OS mutex or executing focus/input.
#[derive(Clone)]
pub struct OperationControlTable {
    inner: Arc<Mutex<OperationControlInner>>,
}

struct OperationControlEntry {
    pub task_session_id: String,
    pub cancel_flag: Arc<AtomicBool>,
    pub stage: ErrorStage,
    pub internal_mutation: InternalMutationDetail,
}

struct OperationControlInner {
    by_operation_id: HashMap<String, OperationControlEntry>,
    retired_tasks: HashSet<String>,
    shutdown_requested: bool,
}

impl OperationControlTable {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(OperationControlInner {
                by_operation_id: HashMap::new(),
                retired_tasks: HashSet::new(),
                shutdown_requested: false,
            })),
        }
    }

    pub fn register_operation(
        &self,
        task_session_id: &str,
        operation_id: &str,
    ) -> Arc<AtomicBool> {
        let mut guard = self.inner.lock().unwrap();
        if let Some(existing) = guard.by_operation_id.get(operation_id) {
            return existing.cancel_flag.clone();
        }
        let cancelled = guard.shutdown_requested || guard.retired_tasks.contains(task_session_id);
        let flag = Arc::new(AtomicBool::new(cancelled));
        guard.by_operation_id.insert(
            operation_id.to_string(),
            OperationControlEntry {
                task_session_id: task_session_id.to_string(),
                cancel_flag: flag.clone(),
                stage: ErrorStage::WaitingLock,
                internal_mutation: InternalMutationDetail::None,
            },
        );
        flag
    }

    pub fn clear_task_retired(&self, task_session_id: &str) {
        let mut guard = self.inner.lock().unwrap();
        guard.retired_tasks.remove(task_session_id);
    }

    pub fn update_stage(
        &self,
        operation_id: &str,
        stage: ErrorStage,
        internal_mutation: InternalMutationDetail,
    ) {
        let mut guard = self.inner.lock().unwrap();
        if let Some(entry) = guard.by_operation_id.get_mut(operation_id) {
            entry.stage = stage;
            entry.internal_mutation = internal_mutation;
        }
    }

    pub fn signal_cancel_operation(&self, task_session_id: &str, operation_id: &str) -> bool {
        let guard = self.inner.lock().unwrap();
        if let Some(entry) = guard.by_operation_id.get(operation_id) {
            if entry.task_session_id == task_session_id {
                entry.cancel_flag.store(true, Ordering::SeqCst);
                return true;
            }
        }
        false
    }

    pub fn signal_cancel_task(&self, task_session_id: &str) -> usize {
        let mut guard = self.inner.lock().unwrap();
        guard.retired_tasks.insert(task_session_id.to_string());
        let mut count = 0;
        for entry in guard.by_operation_id.values() {
            if entry.task_session_id == task_session_id {
                if !entry.cancel_flag.swap(true, Ordering::SeqCst) {
                    count += 1;
                }
            }
        }
        count
    }

    pub fn signal_shutdown_all(&self) {
        let mut guard = self.inner.lock().unwrap();
        guard.shutdown_requested = true;
        for entry in guard.by_operation_id.values() {
            entry.cancel_flag.store(true, Ordering::SeqCst);
        }
    }
}

impl Default for OperationControlTable {
    fn default() -> Self {
        Self::new()
    }
}

/// Holds a prepared two-phase action along with the thread-bound `!Send` `DesktopInputLockGuard`.
/// Lives exclusively on the physical actor thread.
struct ActivePreparedTicket {
    pub task_session_id: String,
    pub operation_id: String,
    pub prepare_ticket: String,
    pub window: WindowRef,
    pub lifecycle_stamp: LifecycleStamp,
    pub lifecycle_invalidated: bool,
    pub expected_state_token: String,
    pub action: ActionSpec,
    pub action_fingerprint: String,
    pub plan: ResolvedActionPlan,
    pub restore_if_minimized: bool,
    pub focus_deadline_ms: u64,
    pub expires_at: Instant,
    pub cancel_flag: Arc<AtomicBool>,
    pub lock_guard: DesktopInputLockGuard,
}

#[derive(Debug, Clone)]
enum TerminalOperationOutcome {
    Committed(ActionCommitResult),
    Failed(ProtocolError),
    Cancelled(ActionCancelResult),
}

#[derive(Debug, Clone)]
struct CompletedOperationRecord {
    pub task_session_id: String,
    pub operation_id: String,
    pub prepare_ticket: Option<String>,
    pub action_fingerprint: String,
    pub outcome: TerminalOperationOutcome,
}

/// Single-threaded physical actor engine that owns the `!Send` `DesktopInputLockGuard`
/// across `action.prepare` -> `action.ready` -> `action.commit` / `action.cancel` / expiry,
/// and owns the worker's `LifecycleService` (dedicated WinEvent pump + bounded lifecycle registry).
pub struct PhysicalActorEngine {
    initialized: bool,
    shutting_down: bool,
    dpi_awareness: String,
    worker_instance: String,
    lifecycle_service: LifecycleService,
    window_tracker: WindowIdentityTracker,
    snapshots: SnapshotStore,
    input_lock: DesktopInputLock,
    control: OperationControlTable,
    active_ticket: Option<ActivePreparedTicket>,
    expired_tickets: HashSet<String>,
    completed_operations: HashMap<String, CompletedOperationRecord>,
}

impl PhysicalActorEngine {
    /// Production constructor: starts a real `LifecycleService` (dedicated WinEvent pump thread
    /// with message queue + `EVENT_OBJECT_CREATE` / `EVENT_OBJECT_DESTROY` registration) and
    /// fails closed unless the pump and hook registration are ready.
    pub fn new(control: OperationControlTable) -> Result<Self, ProtocolError> {
        let dpi_awareness = init_process_dpi_awareness();
        let lifecycle_service = LifecycleService::start_real()?;
        let worker_instance = lifecycle_service.worker_instance().to_string();
        let window_tracker = WindowIdentityTracker::from_service(lifecycle_service.clone());
        let snapshots = SnapshotStore::new();
        let input_lock = DesktopInputLock::new()?;

        Ok(Self {
            initialized: false,
            shutting_down: false,
            dpi_awareness,
            worker_instance,
            lifecycle_service,
            window_tracker,
            snapshots,
            input_lock,
            control,
            active_ticket: None,
            expired_tickets: HashSet::new(),
            completed_operations: HashMap::new(),
        })
    }

    /// Explicit inert, hook-free constructor for pure protocol/validation unit tests.
    ///
    /// Never registers a WinEvent hook or spawns a pump thread, and marks lifecycle coverage
    /// permanently unhealthy (`HookRegistrationFailed`) so it can never issue valid live
    /// `WindowRef`s, `stateToken`s, or actions.
    pub fn new_inert_for_protocol_test(
        control: OperationControlTable,
    ) -> Result<Self, ProtocolError> {
        let host_nonce = generate_host_worker_nonce()?;
        let lifecycle_service = LifecycleService::new_inert_for_test(host_nonce);
        Self::new_with_lifecycle_service_for_test(control, lifecycle_service)
    }

    /// Test-only constructor allowing deterministic adapter/service tests to supply an
    /// `InjectedLifecycleSource`-backed `LifecycleService` without starting OS hooks.
    pub fn new_with_lifecycle_service_for_test(
        control: OperationControlTable,
        lifecycle_service: LifecycleService,
    ) -> Result<Self, ProtocolError> {
        let dpi_awareness = init_process_dpi_awareness();
        let worker_instance = lifecycle_service.worker_instance().to_string();
        let window_tracker = WindowIdentityTracker::from_service(lifecycle_service.clone());
        let snapshots = SnapshotStore::new();
        let input_lock = DesktopInputLock::new()?;

        Ok(Self {
            initialized: false,
            shutting_down: false,
            dpi_awareness,
            worker_instance,
            lifecycle_service,
            window_tracker,
            snapshots,
            input_lock,
            control,
            active_ticket: None,
            expired_tickets: HashSet::new(),
            completed_operations: HashMap::new(),
        })
    }

    pub fn is_shutting_down(&self) -> bool {
        self.shutting_down
    }

    pub fn worker_instance(&self) -> &str {
        &self.worker_instance
    }

    pub fn lifecycle_service(&self) -> &LifecycleService {
        &self.lifecycle_service
    }

    pub fn window_tracker(&self) -> &WindowIdentityTracker {
        &self.window_tracker
    }

    pub fn snapshots(&self) -> &SnapshotStore {
        &self.snapshots
    }

    pub fn input_lock(&self) -> &DesktopInputLock {
        &self.input_lock
    }

    /// Routes a `RetirementBatch` from the `LifecycleService` through the single bounded adapter
    /// invalidation path: removes retired-key snapshots (or all snapshots on coverage fault)
    /// and marks any matching `active_ticket` as lifecycle-invalidated.
    pub fn apply_retirement_batch(&mut self, batch: &RetirementBatch) {
        if !batch.coverage_state.is_healthy() {
            self.snapshots.clear_all_snapshots();
            if let Some(ref mut ticket) = self.active_ticket {
                ticket.lifecycle_invalidated = true;
            }
        }
        for retired in &batch.retired {
            self.snapshots
                .remove_snapshots_for_window_key(&retired.window_key);
            if let Some(ref mut ticket) = self.active_ticket {
                if ticket.window.window_key == retired.window_key
                    || (ticket.lifecycle_stamp.os_tuple.hwnd() == retired.hwnd
                        && ticket.lifecycle_stamp.issue_generation == retired.issue_generation)
                {
                    ticket.lifecycle_invalidated = true;
                }
            }
        }
    }

    /// Drains up to `DEFAULT_EVENT_DRAIN_BUDGET` pending WinEvent records from `LifecycleService`
    /// and routes all resulting retirements (including earlier issue-time evictions or coverage
    /// faults) through `apply_retirement_batch`.
    pub fn drain_and_route_lifecycle_events(&mut self) {
        match self
            .lifecycle_service
            .drain_and_take_retirements(DEFAULT_EVENT_DRAIN_BUDGET)
        {
            Ok(batch) => {
                self.apply_retirement_batch(&batch);
            }
            Err(_) => {
                if !self.lifecycle_service.coverage_state().is_healthy() {
                    self.snapshots.clear_all_snapshots();
                    if let Some(ref mut ticket) = self.active_ticket {
                        ticket.lifecycle_invalidated = true;
                    }
                }
            }
        }
    }

    /// Test-only helper to seed an `ActivePreparedTicket` on the current physical actor thread
    /// so deterministic pure adapter tests can verify `poll_maintenance` retirement of a ready
    /// ticket without requiring a real OS window.
    pub fn seed_ready_ticket_for_test(
        &mut self,
        task_session_id: String,
        operation_id: String,
        prepare_ticket: String,
        window: WindowRef,
        lifecycle_stamp: LifecycleStamp,
        expected_state_token: String,
        action: ActionSpec,
        plan: ResolvedActionPlan,
        expires_in_ms: u64,
        lock_guard: DesktopInputLockGuard,
    ) {
        let cancel_flag = self
            .control
            .register_operation(&task_session_id, &operation_id);
        let action_fingerprint = compute_action_fingerprint(
            &task_session_id,
            &window,
            &expected_state_token,
            &action,
        );
        self.control.update_stage(
            &operation_id,
            ErrorStage::Ready,
            InternalMutationDetail::None,
        );
        self.active_ticket = Some(ActivePreparedTicket {
            task_session_id,
            operation_id,
            prepare_ticket,
            window,
            lifecycle_stamp,
            lifecycle_invalidated: false,
            expected_state_token,
            action,
            action_fingerprint,
            plan,
            restore_if_minimized: true,
            focus_deadline_ms: DEFAULT_FOCUS_DEADLINE_MS,
            expires_at: Instant::now() + Duration::from_millis(expires_in_ms),
            cancel_flag,
            lock_guard,
        });
    }

    /// Releases the held `DesktopInputLockGuard` on the owning actor thread if the active
    /// `prepareTicket` has been invalidated by a lifecycle event / coverage loss, has been
    /// flagged for cancellation, or has expired.
    pub fn poll_maintenance(&mut self) {
        self.drain_and_route_lifecycle_events();

        let should_release = match self.active_ticket.as_ref() {
            Some(t) => {
                t.lifecycle_invalidated
                    || !self.lifecycle_service.coverage_state().is_healthy()
                    || t.cancel_flag.load(Ordering::SeqCst)
                    || Instant::now() >= t.expires_at
            }
            None => false,
        };
        if !should_release {
            return;
        }

        if let Some(mut ticket) = self.active_ticket.take() {
            let is_cancelled = ticket.cancel_flag.load(Ordering::SeqCst);
            let is_lifecycle_stale = ticket.lifecycle_invalidated
                || !self.lifecycle_service.coverage_state().is_healthy();
            let released_ok = ticket.lock_guard.release().is_ok();
            if is_cancelled {
                let cancel_res = ActionCancelResult {
                    cancelled: true,
                    operation_id: ticket.operation_id.clone(),
                    stage: "ready".to_string(),
                    mutation: "none".to_string(),
                    input_lock_released: released_ok,
                };
                self.completed_operations.insert(
                    ticket.operation_id.clone(),
                    CompletedOperationRecord {
                        task_session_id: ticket.task_session_id,
                        operation_id: ticket.operation_id,
                        prepare_ticket: Some(ticket.prepare_ticket),
                        action_fingerprint: ticket.action_fingerprint,
                        outcome: TerminalOperationOutcome::Cancelled(cancel_res),
                    },
                );
            } else if is_lifecycle_stale {
                let target_hwnd = Some(ticket.lifecycle_stamp.os_tuple.hwnd());
                let err = ProtocolError::commit(
                    ErrorCode::WindowStale,
                    ErrorStage::Ready,
                    InternalMutationDetail::None,
                    DiagnosticReason::GenerationMismatch,
                )
                .with_hwnds(target_hwnd, None)
                .with_operation_id(ticket.operation_id.clone());
                self.completed_operations.insert(
                    ticket.operation_id.clone(),
                    CompletedOperationRecord {
                        task_session_id: ticket.task_session_id,
                        operation_id: ticket.operation_id,
                        prepare_ticket: Some(ticket.prepare_ticket),
                        action_fingerprint: ticket.action_fingerprint,
                        outcome: TerminalOperationOutcome::Failed(err),
                    },
                );
            } else {
                self.expired_tickets.insert(ticket.prepare_ticket.clone());
                let err = ProtocolError::commit(
                    ErrorCode::PrepareTicketExpired,
                    ErrorStage::Ready,
                    InternalMutationDetail::None,
                    DiagnosticReason::MutexWaitTimeout,
                )
                .with_operation_id(ticket.operation_id.clone());
                self.completed_operations.insert(
                    ticket.operation_id.clone(),
                    CompletedOperationRecord {
                        task_session_id: ticket.task_session_id,
                        operation_id: ticket.operation_id,
                        prepare_ticket: Some(ticket.prepare_ticket),
                        action_fingerprint: ticket.action_fingerprint,
                        outcome: TerminalOperationOutcome::Failed(err),
                    },
                );
            }
        }
    }

    /// Invoked on stdin EOF / parent process disconnect to immediately cancel any active
    /// prepared ticket, release the OS desktop mutex on the owning thread, and stop/unhook
    /// the WinEvent pump thread with bounded acknowledgement.
    pub fn handle_eof(&mut self) {
        self.shutting_down = true;
        self.control.signal_shutdown_all();
        if let Some(mut ticket) = self.active_ticket.take() {
            let _ = ticket.lock_guard.release();
        }
        let _ = self.lifecycle_service.shutdown_bounded();
        self.drain_and_route_lifecycle_events();
    }

    pub fn handle_request(
        &mut self,
        req: RequestEnvelope,
        emit_event: &mut dyn FnMut(EventEnvelope),
    ) -> ResponseEnvelope {
        let req_id = req.id.clone();
        let req_id_str = req_id.as_correlation_string();

        let outcome = self.dispatch_method(&req_id, &req.method, req.params, emit_event);
        match outcome {
            Ok(val) => ResponseEnvelope {
                v: crate::protocol::WIRE_VERSION,
                id: req_id,
                result: Some(val),
                error: None,
            },
            Err(err) => {
                let bound = err.bind_context(req_id_str, None);
                ResponseEnvelope::err(req_id, bound)
            }
        }
    }

    fn dispatch_method(
        &mut self,
        req_id: &RequestId,
        method: &str,
        params: serde_json::Value,
        emit_event: &mut dyn FnMut(EventEnvelope),
    ) -> Result<serde_json::Value, ProtocolError> {
        if method == "initialize" {
            let init_params: InitializeParams = serde_json::from_value(params).map_err(|_| {
                ProtocolError::validate(ErrorCode::InvalidParams, DiagnosticReason::MalformedJson)
            })?;
            let res = self.handle_initialize(init_params)?;
            return serde_json::to_value(res).map_err(|_| {
                ProtocolError::protocol(ErrorCode::InternalError, DiagnosticReason::MalformedJson)
            });
        }

        if method == "shutdown" {
            let _shutdown_params: ShutdownParams =
                serde_json::from_value(params).map_err(|_| {
                    ProtocolError::validate(
                        ErrorCode::InvalidParams,
                        DiagnosticReason::MalformedJson,
                    )
                })?;
            let res = self.handle_shutdown()?;
            return serde_json::to_value(res).map_err(|_| {
                ProtocolError::protocol(ErrorCode::InternalError, DiagnosticReason::MalformedJson)
            });
        }

        if !self.initialized {
            return Err(ProtocolError::protocol(
                ErrorCode::NotInitialized,
                DiagnosticReason::MissingMethod,
            ));
        }

        match method {
            "windows.list" => {
                self.poll_maintenance();
                let p: WindowsListParams = serde_json::from_value(params).map_err(|_| {
                    ProtocolError::validate(
                        ErrorCode::InvalidParams,
                        DiagnosticReason::MalformedJson,
                    )
                })?;
                let res = handle_windows_list(&self.window_tracker, p);
                self.drain_and_route_lifecycle_events();
                let res = res?;
                serde_json::to_value(res).map_err(|_| {
                    ProtocolError::protocol(
                        ErrorCode::InternalError,
                        DiagnosticReason::MalformedJson,
                    )
                })
            }

            "windows.get" => {
                self.poll_maintenance();
                let p: WindowsGetParams = serde_json::from_value(params).map_err(|_| {
                    ProtocolError::validate(
                        ErrorCode::InvalidParams,
                        DiagnosticReason::MalformedJson,
                    )
                })?;
                let res = handle_windows_get(&self.window_tracker, p);
                self.drain_and_route_lifecycle_events();
                let res = res?;
                serde_json::to_value(res).map_err(|_| {
                    ProtocolError::protocol(
                        ErrorCode::InternalError,
                        DiagnosticReason::MalformedJson,
                    )
                })
            }

            "apps.list" => {
                self.poll_maintenance();
                let p: AppsListParams = serde_json::from_value(params).map_err(|_| {
                    ProtocolError::validate(
                        ErrorCode::InvalidParams,
                        DiagnosticReason::MalformedJson,
                    )
                })?;
                let res = handle_apps_list(&self.window_tracker, p);
                self.drain_and_route_lifecycle_events();
                let res = res?;
                serde_json::to_value(res).map_err(|_| {
                    ProtocolError::protocol(
                        ErrorCode::InternalError,
                        DiagnosticReason::MalformedJson,
                    )
                })
            }

            "apps.launch" => {
                self.poll_maintenance();
                let p: AppsLaunchParams = serde_json::from_value(params).map_err(|_| {
                    ProtocolError::validate(
                        ErrorCode::InvalidParams,
                        DiagnosticReason::MalformedJson,
                    )
                })?;
                handle_apps_launch(&p)
                    .map_err(|e| e.with_operation_id(p.operation_id))
            }

            "state.get" => {
                self.poll_maintenance();
                let p: StateGetParams = serde_json::from_value(params).map_err(|_| {
                    ProtocolError::validate(
                        ErrorCode::InvalidParams,
                        DiagnosticReason::MalformedJson,
                    )
                })?;
                let res = self.handle_state_get(p)?;
                serde_json::to_value(res).map_err(|_| {
                    ProtocolError::protocol(
                        ErrorCode::InternalError,
                        DiagnosticReason::MalformedJson,
                    )
                })
            }

            "action.prepare" => {
                self.poll_maintenance();
                let p: ActionPrepareParams = serde_json::from_value(params).map_err(|_| {
                    ProtocolError::validate(
                        ErrorCode::InvalidParams,
                        DiagnosticReason::MalformedJson,
                    )
                })?;
                let op_id = p.operation_id.clone();
                let res = self
                    .handle_action_prepare(req_id, p, emit_event)
                    .map_err(|e| e.with_operation_id(op_id))?;
                serde_json::to_value(res).map_err(|_| {
                    ProtocolError::protocol(
                        ErrorCode::InternalError,
                        DiagnosticReason::MalformedJson,
                    )
                })
            }

            "action.commit" => {
                let p: ActionCommitParams = serde_json::from_value(params).map_err(|_| {
                    ProtocolError::validate(
                        ErrorCode::InvalidParams,
                        DiagnosticReason::MalformedJson,
                    )
                })?;
                let op_id = p.operation_id.clone();
                let res = self
                    .handle_action_commit(p)
                    .map_err(|e| e.with_operation_id(op_id))?;
                serde_json::to_value(res).map_err(|_| {
                    ProtocolError::protocol(
                        ErrorCode::InternalError,
                        DiagnosticReason::MalformedJson,
                    )
                })
            }

            "action.cancel" => {
                let p: ActionCancelParams = serde_json::from_value(params).map_err(|_| {
                    ProtocolError::validate(
                        ErrorCode::InvalidParams,
                        DiagnosticReason::MalformedJson,
                    )
                })?;
                let op_id = p.operation_id.clone();
                let res = self
                    .handle_action_cancel(p)
                    .map_err(|e| e.with_operation_id(op_id))?;
                serde_json::to_value(res).map_err(|_| {
                    ProtocolError::protocol(
                        ErrorCode::InternalError,
                        DiagnosticReason::MalformedJson,
                    )
                })
            }

            "session.end" => {
                let p: SessionEndParams = serde_json::from_value(params).map_err(|_| {
                    ProtocolError::validate(
                        ErrorCode::InvalidParams,
                        DiagnosticReason::MalformedJson,
                    )
                })?;
                let res = self.handle_session_end(req_id, p, emit_event)?;
                serde_json::to_value(res).map_err(|_| {
                    ProtocolError::protocol(
                        ErrorCode::InternalError,
                        DiagnosticReason::MalformedJson,
                    )
                })
            }

            _ => Err(ProtocolError::protocol(
                ErrorCode::MethodNotFound,
                DiagnosticReason::MissingMethod,
            )),
        }
    }

    fn handle_initialize(
        &mut self,
        params: InitializeParams,
    ) -> Result<InitializeResult, ProtocolError> {
        if self.initialized {
            return Err(ProtocolError::protocol(
                ErrorCode::InvalidRequest,
                DiagnosticReason::AlreadyInitialized,
            ));
        }

        if params.protocol_version.trim() != PROTOCOL_SEMVER {
            return Err(ProtocolError::protocol(
                ErrorCode::UnsupportedProtocolVersion,
                DiagnosticReason::VersionMismatch,
            ));
        }

        if params.client.name.trim().is_empty() || params.client.version.trim().is_empty() {
            return Err(ProtocolError::validate(
                ErrorCode::InvalidParams,
                DiagnosticReason::MalformedJson,
            ));
        }

        self.initialized = true;

        Ok(InitializeResult {
            protocol_version: PROTOCOL_SEMVER.to_string(),
            worker_instance: self.worker_instance.clone(),
            worker: WorkerInfo {
                name: "ryn_computer_use".to_string(),
                version: env!("CARGO_PKG_VERSION").to_string(),
                pid: current_worker_pid(),
            },
            capabilities: ServerCapabilities {
                dpi_awareness: self.dpi_awareness.clone(),
                uia: true,
                window_capture: vec![
                    "print_window_full_content".to_string(),
                    "print_window".to_string(),
                ],
                window_capture_limitations: vec![
                    "phase1_gdi_print_window_candidate_only".to_string(),
                    "no_wgc_or_occluded_layered_parity".to_string(),
                    "minimized_and_display_affinity_protected_windows_not_capturable".to_string(),
                ],
                input_lock: "win32_named_mutex_dacl_current_user".to_string(),
                actions: vec![
                    "focus_window".to_string(),
                    "click".to_string(),
                    "double_click".to_string(),
                    "context_click".to_string(),
                    "move_pointer".to_string(),
                    "scroll".to_string(),
                    "type_text".to_string(),
                    "key_press".to_string(),
                    "key_chord".to_string(),
                    "uia_invoke".to_string(),
                    "uia_set_value".to_string(),
                    "uia_select".to_string(),
                    "uia_focus".to_string(),
                ],
                unsupported_actions: vec!["drag".to_string(), "launch_app".to_string()],
            },
            limits: ServerLimits {
                max_command_line_bytes: MAX_COMMAND_LINE_BYTES,
                max_response_frame_bytes: MAX_RESPONSE_FRAME_BYTES,
                max_png_bytes: MAX_PNG_BYTES,
                max_windows: MAX_WINDOWS_CAP,
                max_uia_elements: MAX_UIA_ELEMENTS_CAP,
                max_uia_depth: MAX_UIA_DEPTH_CAP,
            },
        })
    }

    fn handle_state_get(&mut self, params: StateGetParams) -> Result<Observation, ProtocolError> {
        if params.task_session_id.trim().is_empty() {
            return Err(ProtocolError::validate(
                ErrorCode::InvalidParams,
                DiagnosticReason::MalformedJson,
            ));
        }

        self.control.clear_task_retired(&params.task_session_id);

        // Pre-observation lifecycle checkpoint: verify original WindowRef without minting
        // a new generation, and snapshot coverage generation + target event_revision.
        let (hwnd, pre_checkpoint) = match self.window_tracker.begin_live_observation(&params.window)
        {
            Ok(pair) => {
                self.drain_and_route_lifecycle_events();
                pair
            }
            Err(err) => {
                self.drain_and_route_lifecycle_events();
                return Err(err);
            }
        };
        let pre_geometry = get_window_geometry(hwnd)?;

        let mut acc_opts = params.accessibility.unwrap_or_default();
        if let Some(inc_text) = params.include_text {
            if acc_opts.enabled.is_none() {
                acc_opts.enabled = Some(inc_text);
            }
        }
        let accessibility = observe_window_accessibility(hwnd, &acc_opts);

        let mut shot_opts = params.screenshot.unwrap_or_default();
        if let Some(inc_shot) = params.include_screenshot {
            if shot_opts.enabled.is_none() {
                shot_opts.enabled = Some(inc_shot);
            }
        }
        let screenshot =
            capture_window_screenshot(hwnd, &pre_geometry, &shot_opts, generate_artifact_id());
        let foreground = get_foreground_status(hwnd);

        // Post-observation re-verification: drain WinEvents, verify live OS tuple + geometry,
        // and confirm coverage generation and target event_revision did not change during sampling.
        let post_geometry = get_window_geometry(hwnd)?;
        let final_stamp = match self
            .window_tracker
            .finish_live_observation(&params.window, &pre_checkpoint)
        {
            Ok(stamp) => {
                self.drain_and_route_lifecycle_events();
                stamp
            }
            Err(err) => {
                self.drain_and_route_lifecycle_events();
                return Err(err);
            }
        };

        if post_geometry.dpi != pre_geometry.dpi
            || post_geometry.window_rect != pre_geometry.window_rect
            || post_geometry.client_rect != pre_geometry.client_rect
        {
            return Err(ProtocolError::observe(
                ErrorCode::ObservationUnavailable,
                DiagnosticReason::GeometryChanged,
            )
            .with_hwnds(Some(crate::windows::hwnd_to_u64(hwnd)), None));
        }

        let record = self.snapshots.insert_snapshot(
            params.task_session_id.clone(),
            params.window.clone(),
            final_stamp,
            post_geometry.clone(),
            accessibility.elements.clone(),
            accessibility.tree_hash.clone(),
        );

        Ok(Observation {
            task_session_id: params.task_session_id,
            window: params.window,
            observation_id: record.observation_id,
            state_token: record.state_token,
            captured_at: now_iso8601_utc(),
            geometry: post_geometry,
            screenshots: vec![screenshot],
            accessibility,
            foreground,
        })
    }

    fn handle_action_prepare(
        &mut self,
        req_id: &RequestId,
        params: ActionPrepareParams,
        emit_event: &mut dyn FnMut(EventEnvelope),
    ) -> Result<ActionPrepareResult, ProtocolError> {
        if params.task_session_id.trim().is_empty() || params.operation_id.trim().is_empty() {
            return Err(ProtocolError::validate(
                ErrorCode::InvalidParams,
                DiagnosticReason::MalformedJson,
            ));
        }

        if self.snapshots.is_task_retired(&params.task_session_id) {
            return Err(ProtocolError::validate(
                ErrorCode::TaskNotFound,
                DiagnosticReason::TokenTaskMismatch,
            ));
        }

        if self.completed_operations.contains_key(&params.operation_id)
            || self
                .active_ticket
                .as_ref()
                .map(|t| t.operation_id == params.operation_id)
                .unwrap_or(false)
        {
            return Err(ProtocolError::validate(
                ErrorCode::DuplicateOperationId,
                DiagnosticReason::TokenWindowMismatch,
            ));
        }

        if self.input_lock.is_quarantined() {
            return Err(ProtocolError::waiting_lock(
                ErrorCode::InputLockQuarantined,
                DiagnosticReason::MutexReleaseFailed,
            ));
        }

        let cancel_flag = self
            .control
            .register_operation(&params.task_session_id, &params.operation_id);
        if cancel_flag.load(Ordering::SeqCst) {
            return Err(ProtocolError::cancel(
                ErrorCode::Cancelled,
                ErrorStage::Validate,
                InternalMutationDetail::None,
                DiagnosticReason::CancelledBeforeLock,
            ));
        }

        // Pre-lock side-effect-free validation of WindowRef, LifecycleStamp, stateToken, and ActionSpec
        let expected_stamp = self
            .snapshots
            .lookup_lifecycle_stamp(&params.expected_state_token);
        let (hwnd, pre_stamp) = match self
            .window_tracker
            .validate_window_ref_and_stamp(&params.window, expected_stamp.as_ref())
        {
            Ok(pair) => {
                self.drain_and_route_lifecycle_events();
                pair
            }
            Err(err) => {
                self.drain_and_route_lifecycle_events();
                return Err(err);
            }
        };
        let pre_geometry = get_window_geometry(hwnd)?;
        let snapshot = self.snapshots.validate_state_token(
            &params.expected_state_token,
            &params.task_session_id,
            &params.window,
            &pre_stamp,
            &pre_geometry,
        )?;

        let plan = resolve_action_plan(hwnd, &snapshot, &pre_geometry, &params.action)?;

        let constraints = params.constraints.unwrap_or_default();
        let restore_if_minimized = match &params.action {
            ActionSpec::FocusWindow {
                restore_if_minimized: Some(r),
            } => *r,
            _ => constraints.restore_if_minimized.unwrap_or(true),
        };

        if unsafe { IsIconic(hwnd).as_bool() } && !restore_if_minimized {
            return Err(ProtocolError::prepare(
                ErrorCode::WindowMinimized,
                DiagnosticReason::MinimizedRestoreDisallowed,
            ));
        }

        if self.active_ticket.is_some() {
            return Err(ProtocolError::waiting_lock(
                ErrorCode::InputLockBusy,
                DiagnosticReason::MutexWaitTimeout,
            ));
        }

        let lock_wait_ms = constraints
            .lock_wait_timeout_ms
            .or(params.timeout_ms)
            .unwrap_or(DEFAULT_LOCK_WAIT_TIMEOUT_MS);

        self.control.update_stage(
            &params.operation_id,
            ErrorStage::WaitingLock,
            InternalMutationDetail::None,
        );

        let mut lock_guard = self.input_lock.acquire_for_current_thread(
            params.operation_id.clone(),
            lock_wait_ms,
            &cancel_flag,
        )?;
        let lock_was_abandoned = lock_guard.was_abandoned();

        // Post-lock revalidation: desktop state or WinEvent coverage may have changed while
        // waiting for the lock or if the previous lock holder abandoned the mutex.
        self.control.update_stage(
            &params.operation_id,
            ErrorStage::Prepare,
            InternalMutationDetail::None,
        );

        if cancel_flag.load(Ordering::SeqCst) {
            let _ = lock_guard.release();
            return Err(ProtocolError::cancel(
                ErrorCode::Cancelled,
                ErrorStage::Prepare,
                InternalMutationDetail::None,
                DiagnosticReason::CancelledBeforeLock,
            ));
        }

        let (hwnd_after, post_lock_stamp) = match self
            .window_tracker
            .validate_window_ref_and_stamp(&params.window, Some(&snapshot.lifecycle_stamp))
        {
            Ok(pair) => {
                self.drain_and_route_lifecycle_events();
                pair
            }
            Err(e) => {
                self.drain_and_route_lifecycle_events();
                let _ = lock_guard.release();
                return Err(e);
            }
        };
        let post_lock_geometry = match get_window_geometry(hwnd_after) {
            Ok(g) => g,
            Err(e) => {
                let _ = lock_guard.release();
                return Err(e);
            }
        };
        if let Err(e) = self.snapshots.validate_state_token(
            &params.expected_state_token,
            &params.task_session_id,
            &params.window,
            &post_lock_stamp,
            &post_lock_geometry,
        ) {
            let _ = lock_guard.release();
            return Err(e);
        }

        let ready_ttl_ms = constraints
            .ready_timeout_ms
            .unwrap_or(DEFAULT_READY_TICKET_TTL_MS)
            .clamp(50, MAX_READY_TICKET_TTL_MS);
        let focus_deadline_ms = constraints
            .focus_deadline_ms
            .unwrap_or(DEFAULT_FOCUS_DEADLINE_MS);

        let prepare_ticket = generate_prepare_ticket();
        let action_kind = params.action.kind_name().to_string();
        let action_fingerprint = compute_action_fingerprint(
            &params.task_session_id,
            &params.window,
            &params.expected_state_token,
            &params.action,
        );

        let summary = plan.summary.clone();
        let expires_at = Instant::now() + Duration::from_millis(ready_ttl_ms);

        self.control.update_stage(
            &params.operation_id,
            ErrorStage::Ready,
            InternalMutationDetail::None,
        );

        self.active_ticket = Some(ActivePreparedTicket {
            task_session_id: params.task_session_id.clone(),
            operation_id: params.operation_id.clone(),
            prepare_ticket: prepare_ticket.clone(),
            window: params.window.clone(),
            expected_state_token: params.expected_state_token.clone(),
            lifecycle_stamp: post_lock_stamp,
            lifecycle_invalidated: false,
            action: params.action,
            action_fingerprint: action_fingerprint.clone(),
            plan,
            restore_if_minimized,
            focus_deadline_ms,
            expires_at,
            cancel_flag,
            lock_guard,
        });

        let ready_event = ActionReadyEventData {
            task_session_id: params.task_session_id.clone(),
            operation_id: params.operation_id.clone(),
            prepare_ticket: prepare_ticket.clone(),
            window: params.window.clone(),
            expected_state_token: params.expected_state_token.clone(),
            action_kind: action_kind.clone(),
            action_fingerprint: action_fingerprint.clone(),
            authority_epoch: params.authority_epoch,
            input_lock_held: true,
            lock_was_abandoned,
            expires_in_ms: ready_ttl_ms,
            mutation: "none".to_string(),
        };

        if let Ok(ev) = EventEnvelope::new(
            "action.ready",
            req_id.clone(),
            Some(params.operation_id.clone()),
            ready_event,
        ) {
            emit_event(ev);
        }

        Ok(ActionPrepareResult {
            status: "ready".to_string(),
            task_session_id: params.task_session_id,
            operation_id: params.operation_id,
            prepare_ticket,
            window: params.window,
            expected_state_token: params.expected_state_token,
            action_kind,
            action_fingerprint,
            authority_epoch: params.authority_epoch,
            expires_in_ms: ready_ttl_ms,
            input_lock_held: true,
            lock_was_abandoned,
            mutation: "none".to_string(),
            resolved_target: summary,
        })
    }

    fn handle_action_commit(
        &mut self,
        params: ActionCommitParams,
    ) -> Result<ActionCommitResult, ProtocolError> {
        if params.task_session_id.trim().is_empty()
            || params.operation_id.trim().is_empty()
            || params.prepare_ticket.trim().is_empty()
        {
            return Err(ProtocolError::validate(
                ErrorCode::InvalidParams,
                DiagnosticReason::MalformedJson,
            ));
        }

        self.drain_and_route_lifecycle_events();

        // Duplicate commit protection: never repeat physical side effects for the same operationId.
        if let Some(existing) = self.completed_operations.get(&params.operation_id).cloned() {
            if existing.operation_id != params.operation_id
                || existing.task_session_id != params.task_session_id
                || existing.prepare_ticket.as_deref() != Some(params.prepare_ticket.as_str())
                || existing.action_fingerprint.is_empty()
            {
                return Err(ProtocolError::validate(
                    ErrorCode::DuplicateOperationId,
                    DiagnosticReason::TokenWindowMismatch,
                ));
            }
            return match existing.outcome {
                TerminalOperationOutcome::Committed(res) => Ok(res),
                TerminalOperationOutcome::Failed(err) => Err(err),
                TerminalOperationOutcome::Cancelled(_) => Err(ProtocolError::cancel(
                    ErrorCode::Cancelled,
                    ErrorStage::Ready,
                    InternalMutationDetail::None,
                    DiagnosticReason::CancelledWhileReady,
                )),
            };
        }

        let mut ticket = match self.active_ticket.take() {
            Some(t) => t,
            None => {
                if self.expired_tickets.contains(&params.prepare_ticket) {
                    return Err(ProtocolError::commit(
                        ErrorCode::PrepareTicketExpired,
                        ErrorStage::Ready,
                        InternalMutationDetail::None,
                        DiagnosticReason::MutexWaitTimeout,
                    ));
                }
                return Err(ProtocolError::commit(
                    ErrorCode::PrepareTicketInvalid,
                    ErrorStage::Commit,
                    InternalMutationDetail::None,
                    DiagnosticReason::TokenWindowMismatch,
                ));
            }
        };

        if ticket.operation_id != params.operation_id
            || ticket.prepare_ticket != params.prepare_ticket
        {
            // Keep the active ticket if it belongs to a different operation; if it's the same operation
            // with a forged ticket, release the lock and reject.
            if ticket.operation_id == params.operation_id {
                let _ = ticket.lock_guard.release();
            } else {
                self.active_ticket = Some(ticket);
            }
            return Err(ProtocolError::commit(
                ErrorCode::PrepareTicketInvalid,
                ErrorStage::Commit,
                InternalMutationDetail::None,
                DiagnosticReason::TokenWindowMismatch,
            ));
        }

        if ticket.task_session_id != params.task_session_id {
            let _ = ticket.lock_guard.release();
            return Err(ProtocolError::commit(
                ErrorCode::TaskConflict,
                ErrorStage::Commit,
                InternalMutationDetail::None,
                DiagnosticReason::TokenTaskMismatch,
            ));
        }

        if ticket.cancel_flag.load(Ordering::SeqCst) {
            let released_ok = ticket.lock_guard.release().is_ok();
            let cancel_res = ActionCancelResult {
                cancelled: true,
                operation_id: ticket.operation_id.clone(),
                stage: "ready".to_string(),
                mutation: "none".to_string(),
                input_lock_released: released_ok,
            };
            self.completed_operations.insert(
                ticket.operation_id.clone(),
                CompletedOperationRecord {
                    task_session_id: ticket.task_session_id,
                    operation_id: ticket.operation_id,
                    prepare_ticket: Some(ticket.prepare_ticket),
                    action_fingerprint: ticket.action_fingerprint,
                    outcome: TerminalOperationOutcome::Cancelled(cancel_res),
                },
            );
            return Err(ProtocolError::cancel(
                ErrorCode::Cancelled,
                ErrorStage::Ready,
                InternalMutationDetail::None,
                DiagnosticReason::CancelledWhileReady,
            ));
        }

        if ticket.lifecycle_invalidated || !self.lifecycle_service.coverage_state().is_healthy() {
            let _ = ticket.lock_guard.release();
            let target_hwnd = Some(ticket.lifecycle_stamp.os_tuple.hwnd());
            let err = ProtocolError::commit(
                ErrorCode::WindowStale,
                ErrorStage::Ready,
                InternalMutationDetail::None,
                DiagnosticReason::GenerationMismatch,
            )
            .with_hwnds(target_hwnd, None);
            self.completed_operations.insert(
                ticket.operation_id.clone(),
                CompletedOperationRecord {
                    task_session_id: ticket.task_session_id,
                    operation_id: ticket.operation_id,
                    prepare_ticket: Some(ticket.prepare_ticket),
                    action_fingerprint: ticket.action_fingerprint,
                    outcome: TerminalOperationOutcome::Failed(err.clone()),
                },
            );
            return Err(err);
        }

        if Instant::now() >= ticket.expires_at {
            let _ = ticket.lock_guard.release();
            self.expired_tickets.insert(ticket.prepare_ticket.clone());
            let err = ProtocolError::commit(
                ErrorCode::PrepareTicketExpired,
                ErrorStage::Ready,
                InternalMutationDetail::None,
                DiagnosticReason::MutexWaitTimeout,
            );
            self.completed_operations.insert(
                ticket.operation_id.clone(),
                CompletedOperationRecord {
                    task_session_id: ticket.task_session_id,
                    operation_id: ticket.operation_id,
                    prepare_ticket: Some(ticket.prepare_ticket),
                    action_fingerprint: ticket.action_fingerprint,
                    outcome: TerminalOperationOutcome::Failed(err.clone()),
                },
            );
            return Err(err);
        }

        // Revalidate WindowRef, LifecycleStamp, and stateToken immediately before activation
        let (hwnd, commit_stamp) = match self
            .window_tracker
            .validate_window_ref_and_stamp(&ticket.window, Some(&ticket.lifecycle_stamp))
        {
            Ok(pair) => {
                self.drain_and_route_lifecycle_events();
                pair
            }
            Err(mut err) => {
                self.drain_and_route_lifecycle_events();
                err.stage = ErrorStage::Commit;
                let _ = ticket.lock_guard.release();
                self.completed_operations.insert(
                    ticket.operation_id.clone(),
                    CompletedOperationRecord {
                        task_session_id: ticket.task_session_id,
                        operation_id: ticket.operation_id,
                        prepare_ticket: Some(ticket.prepare_ticket),
                        action_fingerprint: ticket.action_fingerprint,
                        outcome: TerminalOperationOutcome::Failed(err.clone()),
                    },
                );
                return Err(err);
            }
        };

        let pre_focus_geometry = match get_window_geometry(hwnd) {
            Ok(g) => g,
            Err(err) => {
                let _ = ticket.lock_guard.release();
                self.completed_operations.insert(
                    ticket.operation_id.clone(),
                    CompletedOperationRecord {
                        task_session_id: ticket.task_session_id,
                        operation_id: ticket.operation_id,
                        prepare_ticket: Some(ticket.prepare_ticket),
                        action_fingerprint: ticket.action_fingerprint,
                        outcome: TerminalOperationOutcome::Failed(err.clone()),
                    },
                );
                return Err(err);
            }
        };

        if let Err(mut err) = self.snapshots.validate_state_token(
            &ticket.expected_state_token,
            &ticket.task_session_id,
            &ticket.window,
            &commit_stamp,
            &pre_focus_geometry,
        ) {
            err.stage = ErrorStage::Commit;
            let _ = ticket.lock_guard.release();
            self.completed_operations.insert(
                ticket.operation_id.clone(),
                CompletedOperationRecord {
                    task_session_id: ticket.task_session_id,
                    operation_id: ticket.operation_id,
                    prepare_ticket: Some(ticket.prepare_ticket),
                    action_fingerprint: ticket.action_fingerprint,
                    outcome: TerminalOperationOutcome::Failed(err.clone()),
                },
            );
            return Err(err);
        }

        // Advance window mutation epoch before physical activation/input so older state tokens
        // for this window cannot be reused after physical mutation begins.
        self.snapshots
            .advance_window_epoch(&ticket.window.window_key);

        self.control.update_stage(
            &ticket.operation_id,
            ErrorStage::Activating,
            InternalMutationDetail::FocusOnly,
        );

        let effective_focus_deadline = match params.deadline_ms {
            Some(d) => ticket.focus_deadline_ms.min(d.max(50)),
            None => ticket.focus_deadline_ms,
        };

        let focus_summary = match ensure_window_focused(
            hwnd,
            ticket.restore_if_minimized,
            effective_focus_deadline,
            &ticket.cancel_flag,
        ) {
            Ok(s) => s,
            Err(err) => {
                if err.code == ErrorCode::InputLockQuarantined {
                    self.input_lock.force_quarantine();
                }
                let _ = ticket.lock_guard.release();
                self.completed_operations.insert(
                    ticket.operation_id.clone(),
                    CompletedOperationRecord {
                        task_session_id: ticket.task_session_id,
                        operation_id: ticket.operation_id,
                        prepare_ticket: Some(ticket.prepare_ticket),
                        action_fingerprint: ticket.action_fingerprint,
                        outcome: TerminalOperationOutcome::Failed(err.clone()),
                    },
                );
                return Err(err);
            }
        };

        // Any failure after `ensure_window_focused` succeeds must monotonically carry at least
        // `FocusOnly` (`mutation: possible`) whenever activation was attempted (`focus_summary.attempted`),
        // or if the window is now foreground/related.
        let post_focus_min_mutation = if focus_summary.attempted {
            InternalMutationDetail::FocusOnly
        } else {
            InternalMutationDetail::None
        };

        // Re-verify geometry after focus/restore before delivering pointer/keyboard/UIA input
        if !matches!(ticket.action, ActionSpec::FocusWindow { .. }) {
            let post_focus_geometry = match get_window_geometry(hwnd) {
                Ok(g) => g,
                Err(err) => {
                    let _ = ticket.lock_guard.release();
                    let mut final_err = err.with_monotonic_mutation(
                        post_focus_min_mutation.max_monotonic(InternalMutationDetail::FocusOnly),
                    );
                    final_err.stage = ErrorStage::Validating;
                    self.completed_operations.insert(
                        ticket.operation_id.clone(),
                        CompletedOperationRecord {
                            task_session_id: ticket.task_session_id,
                            operation_id: ticket.operation_id,
                            prepare_ticket: Some(ticket.prepare_ticket),
                            action_fingerprint: ticket.action_fingerprint,
                            outcome: TerminalOperationOutcome::Failed(final_err.clone()),
                        },
                    );
                    return Err(final_err);
                }
            };
            if post_focus_geometry.window_rect != pre_focus_geometry.window_rect
                || post_focus_geometry.client_rect != pre_focus_geometry.client_rect
                || post_focus_geometry.dpi != pre_focus_geometry.dpi
            {
                let _ = ticket.lock_guard.release();
                let err = ProtocolError::commit(
                    ErrorCode::StateTokenStale,
                    ErrorStage::Validating,
                    post_focus_min_mutation.max_monotonic(InternalMutationDetail::FocusOnly),
                    DiagnosticReason::GeometryChanged,
                );
                self.completed_operations.insert(
                    ticket.operation_id.clone(),
                    CompletedOperationRecord {
                        task_session_id: ticket.task_session_id,
                        operation_id: ticket.operation_id,
                        prepare_ticket: Some(ticket.prepare_ticket),
                        action_fingerprint: ticket.action_fingerprint,
                        outcome: TerminalOperationOutcome::Failed(err.clone()),
                    },
                );
                return Err(err);
            }
        }

        self.control.update_stage(
            &ticket.operation_id,
            ErrorStage::InputStarted,
            InternalMutationDetail::UnknownOrPartial,
        );

        if let Err(err) = execute_resolved_action(hwnd, &ticket.plan, &ticket.cancel_flag) {
            if err.code == ErrorCode::InputLockQuarantined {
                self.input_lock.force_quarantine();
            }
            let _ = ticket.lock_guard.release();
            let final_err = err.with_monotonic_mutation(
                post_focus_min_mutation.max_monotonic(InternalMutationDetail::FocusOnly),
            );
            self.completed_operations.insert(
                ticket.operation_id.clone(),
                CompletedOperationRecord {
                    task_session_id: ticket.task_session_id,
                    operation_id: ticket.operation_id,
                    prepare_ticket: Some(ticket.prepare_ticket),
                    action_fingerprint: ticket.action_fingerprint,
                    outcome: TerminalOperationOutcome::Failed(final_err.clone()),
                },
            );
            return Err(final_err);
        }

        self.control.update_stage(
            &ticket.operation_id,
            ErrorStage::Commit,
            InternalMutationDetail::Committed,
        );

        // Release the desktop input mutex on the owning physical actor thread.
        let release_res = ticket.lock_guard.release();

        let mut post_obs: Option<Observation> = None;
        if let Some(ref post_opts) = params.post_observation {
            if post_opts.enabled.unwrap_or(true) {
                let settle_ms = post_opts.settle_delay_ms.unwrap_or(40).min(1000);
                if settle_ms > 0 {
                    std::thread::sleep(Duration::from_millis(settle_ms));
                }
                let state_params = StateGetParams {
                    task_session_id: ticket.task_session_id.clone(),
                    window: ticket.window.clone(),
                    include_screenshot: None,
                    include_text: None,
                    screenshot: post_opts.screenshot.clone().or(Some(ScreenshotOptions {
                        enabled: Some(true),
                        mode: None,
                        format: None,
                    })),
                    accessibility: post_opts
                        .accessibility
                        .clone()
                        .or(Some(AccessibilityOptions::default())),
                };
                post_obs = self.handle_state_get(state_params).ok();
            }
        }

        if let Err(mut release_err) = release_res {
            release_err.mutation = InternalMutationDetail::Committed.to_wire_mutation();
            if let Some(ref mut d) = release_err.details {
                d.internal_mutation = Some(InternalMutationDetail::Committed);
            }
            self.completed_operations.insert(
                ticket.operation_id.clone(),
                CompletedOperationRecord {
                    task_session_id: ticket.task_session_id,
                    operation_id: ticket.operation_id,
                    prepare_ticket: Some(ticket.prepare_ticket),
                    action_fingerprint: ticket.action_fingerprint,
                    outcome: TerminalOperationOutcome::Failed(release_err.clone()),
                },
            );
            return Err(release_err);
        }

        let commit_result = ActionCommitResult {
            status: "committed".to_string(),
            task_session_id: ticket.task_session_id.clone(),
            operation_id: ticket.operation_id.clone(),
            action_fingerprint: ticket.action_fingerprint.clone(),
            mutation: "delivered".to_string(),
            focus_recovery: focus_summary,
            observation: post_obs,
        };

        self.completed_operations.insert(
            ticket.operation_id.clone(),
            CompletedOperationRecord {
                task_session_id: ticket.task_session_id,
                operation_id: ticket.operation_id,
                prepare_ticket: Some(ticket.prepare_ticket),
                action_fingerprint: ticket.action_fingerprint,
                outcome: TerminalOperationOutcome::Committed(commit_result.clone()),
            },
        );

        Ok(commit_result)
    }

    fn handle_action_cancel(
        &mut self,
        params: ActionCancelParams,
    ) -> Result<ActionCancelResult, ProtocolError> {
        if params.task_session_id.trim().is_empty() || params.operation_id.trim().is_empty() {
            return Err(ProtocolError::validate(
                ErrorCode::InvalidParams,
                DiagnosticReason::MalformedJson,
            ));
        }

        self.control
            .signal_cancel_operation(&params.task_session_id, &params.operation_id);

        if self
            .active_ticket
            .as_ref()
            .map(|t| t.operation_id == params.operation_id)
            .unwrap_or(false)
        {
            let mut ticket = self.active_ticket.take().unwrap();
            if ticket.task_session_id != params.task_session_id {
                self.active_ticket = Some(ticket);
                return Err(ProtocolError::cancel(
                    ErrorCode::TaskConflict,
                    ErrorStage::Cancel,
                    InternalMutationDetail::None,
                    DiagnosticReason::TokenTaskMismatch,
                ));
            }
            let released_ok = ticket.lock_guard.release().is_ok();
            let res = ActionCancelResult {
                cancelled: true,
                operation_id: ticket.operation_id.clone(),
                stage: "ready".to_string(),
                mutation: "none".to_string(),
                input_lock_released: released_ok,
            };
            self.completed_operations.insert(
                ticket.operation_id.clone(),
                CompletedOperationRecord {
                    task_session_id: ticket.task_session_id,
                    operation_id: ticket.operation_id,
                    prepare_ticket: Some(ticket.prepare_ticket),
                    action_fingerprint: ticket.action_fingerprint,
                    outcome: TerminalOperationOutcome::Cancelled(res.clone()),
                },
            );
            return Ok(res);
        }

        if let Some(existing) = self.completed_operations.get(&params.operation_id) {
            if existing.task_session_id != params.task_session_id {
                return Err(ProtocolError::cancel(
                    ErrorCode::TaskConflict,
                    ErrorStage::Cancel,
                    InternalMutationDetail::None,
                    DiagnosticReason::TokenTaskMismatch,
                ));
            }
            return match &existing.outcome {
                TerminalOperationOutcome::Cancelled(c) => Ok(c.clone()),
                TerminalOperationOutcome::Committed(_) => Ok(ActionCancelResult {
                    cancelled: false,
                    operation_id: params.operation_id,
                    stage: "commit".to_string(),
                    mutation: "delivered".to_string(),
                    input_lock_released: true,
                }),
                TerminalOperationOutcome::Failed(err) => Ok(ActionCancelResult {
                    cancelled: false,
                    operation_id: params.operation_id,
                    stage: format!("{:?}", err.stage).to_lowercase(),
                    mutation: match err.mutation {
                        crate::errors::MutationState::None => "none".to_string(),
                        crate::errors::MutationState::Possible => "possible".to_string(),
                        crate::errors::MutationState::Delivered => "delivered".to_string(),
                    },
                    input_lock_released: !self.input_lock.is_quarantined(),
                }),
            };
        }

        Ok(ActionCancelResult {
            cancelled: true,
            operation_id: params.operation_id,
            stage: "validate".to_string(),
            mutation: "none".to_string(),
            input_lock_released: true,
        })
    }

    /// Retires task-scoped snapshots, state tokens, and pending ready tickets for `task_session_id`
    /// without closing or modifying any user windows.
    fn handle_session_end(
        &mut self,
        req_id: &RequestId,
        params: SessionEndParams,
        emit_event: &mut dyn FnMut(EventEnvelope),
    ) -> Result<SessionEndResult, ProtocolError> {
        if params.task_session_id.trim().is_empty() {
            return Err(ProtocolError::validate(
                ErrorCode::InvalidParams,
                DiagnosticReason::MalformedJson,
            ));
        }

        let mut cancelled_operations = self.control.signal_cancel_task(&params.task_session_id);

        if self
            .active_ticket
            .as_ref()
            .map(|t| t.task_session_id == params.task_session_id)
            .unwrap_or(false)
        {
            if let Some(mut ticket) = self.active_ticket.take() {
                let released_ok = ticket.lock_guard.release().is_ok();
                cancelled_operations = cancelled_operations.max(1);
                let cancel_res = ActionCancelResult {
                    cancelled: true,
                    operation_id: ticket.operation_id.clone(),
                    stage: "ready".to_string(),
                    mutation: "none".to_string(),
                    input_lock_released: released_ok,
                };
                self.completed_operations.insert(
                    ticket.operation_id.clone(),
                    CompletedOperationRecord {
                        task_session_id: ticket.task_session_id,
                        operation_id: ticket.operation_id,
                        prepare_ticket: Some(ticket.prepare_ticket),
                        action_fingerprint: ticket.action_fingerprint,
                        outcome: TerminalOperationOutcome::Cancelled(cancel_res),
                    },
                );
            }
        }

        let invalidated_state_tokens = self.snapshots.retire_task_session(&params.task_session_id);

        if let Ok(ev) = EventEnvelope::new(
            "session.retired",
            req_id.clone(),
            None,
            serde_json::json!({
                "taskSessionId": params.task_session_id,
                "cancelledOperations": cancelled_operations,
                "invalidatedStateTokens": invalidated_state_tokens,
            }),
        ) {
            emit_event(ev);
        }

        Ok(SessionEndResult {
            ended: true,
            cancelled_operations,
            invalidated_state_tokens,
        })
    }

    fn handle_shutdown(&mut self) -> Result<ShutdownResult, ProtocolError> {
        self.shutting_down = true;
        self.control.signal_shutdown_all();
        if let Some(mut ticket) = self.active_ticket.take() {
            let _ = ticket.lock_guard.release();
        }
        let _ = self.lifecycle_service.shutdown_bounded();
        self.drain_and_route_lifecycle_events();
        Ok(ShutdownResult {
            shutting_down: true,
        })
    }
}

impl Drop for PhysicalActorEngine {
    fn drop(&mut self) {
        if let Some(mut ticket) = self.active_ticket.take() {
            let _ = ticket.lock_guard.release();
        }
        let _ = self.lifecycle_service.shutdown_bounded();
    }
}
