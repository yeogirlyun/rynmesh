#![forbid(unsafe_code)]

//! Pure typed window lifecycle state machine for N05 (`ryn_computer_use::lifecycle_model`).
//!
//! # Authority & Limitations
//! - Models only **KNOWN delivered** `EVENT_OBJECT_CREATE` / `EVENT_OBJECT_DESTROY` window events
//!   ordered by local callback `arrival_seq`, combined with caller-supplied live OS identity tuples
//!   `(hwnd, pid, process_started)`.
//! - Win32 `SetWinEventHook(..., WINEVENT_OUTOFCONTEXT)` is asynchronous and queued on the hook
//!   thread's message loop. There is no OS synchronous flush API; draining the local queue,
//!   self-pinging, or checking heartbeats proves continuity of observed hook coverage, NOT that
//!   all external kernel/user32 events have been delivered, and does NOT claim mathematical
//!   elimination of all TOCTOU windows between a Win32 check and the next system call.
//! - `event_time_ms` (`DWORD` 32-bit tick count from WinEvent) is stored purely as auxiliary
//!   diagnostic evidence and is NEVER used as an ordering authority; 32-bit wraparound or
//!   out-of-order timestamps never revive a retired generation.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

pub const DEFAULT_MAX_TRACKED_WINDOWS: usize = 256;
pub const MAX_ALLOWED_CAPACITY: usize = 4096;
pub const DEFAULT_EVENT_DRAIN_BUDGET: usize = 256;
pub const MAX_HOST_NONCE_HISTORY: usize = 64;

/// Non-zero 128-bit host worker instance nonce bound to every issued window identity and stamp.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct HostWorkerNonce(u128);

impl HostWorkerNonce {
    pub fn new(raw: u128) -> Result<Self, LifecycleError> {
        if raw == 0 {
            return Err(LifecycleError::InvalidHostWorkerNonce);
        }
        Ok(Self(raw))
    }

    pub fn get(self) -> u128 {
        self.0
    }

    pub fn worker_instance_label(self) -> String {
        format!("wrk_{:032x}", self.0)
    }
}

impl fmt::Display for HostWorkerNonce {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "wrk_{:032x}", self.0)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InvalidOsTupleReason {
    ZeroHwnd,
    ZeroPid,
    ZeroProcessCreationTime,
}

/// Immutable OS window identity tuple `(hwnd, pid, process_started)`.
///
/// Every component must be strictly non-zero. Unqueryable or zero `process_started`
/// is rejected rather than falling back to `0` or guessing by window title/app name.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct WindowOsTuple {
    hwnd: u64,
    pid: u32,
    process_started: u64,
}

impl WindowOsTuple {
    pub fn new(hwnd: u64, pid: u32, process_started: u64) -> Result<Self, LifecycleError> {
        if hwnd == 0 {
            return Err(LifecycleError::InvalidOsTuple(InvalidOsTupleReason::ZeroHwnd));
        }
        if pid == 0 {
            return Err(LifecycleError::InvalidOsTuple(InvalidOsTupleReason::ZeroPid));
        }
        if process_started == 0 {
            return Err(LifecycleError::InvalidOsTuple(
                InvalidOsTupleReason::ZeroProcessCreationTime,
            ));
        }
        Ok(Self {
            hwnd,
            pid,
            process_started,
        })
    }

    pub fn hwnd(self) -> u64 {
        self.hwnd
    }

    pub fn pid(self) -> u32 {
        self.pid
    }

    pub fn process_started(self) -> u64 {
        self.process_started
    }
}

/// Window-level WinEvent kinds tracked by the lifecycle service (`OBJID_WINDOW` / `CHILDID_SELF`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum LifecycleEventKind {
    ObjectCreate,
    ObjectDestroy,
}

/// A delivered window lifecycle event recorded by the WinEvent hook callback.
///
/// `event_time_ms` is a 32-bit `DWORD` timestamp retained only as auxiliary diagnostic data;
/// local `arrival_seq` is the sole ordering authority.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DeliveredWindowEvent {
    pub hwnd: u64,
    pub kind: LifecycleEventKind,
    pub event_thread: u32,
    pub event_time_ms: u32,
    pub arrival_seq: u64,
    pub hook_generation: u64,
}

/// Reason why WinEvent hook coverage transitioned to unhealthy (fail-closed).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum CoverageFaultReason {
    HookRegistrationFailed,
    QueueOverflow,
    ArrivalSeqOverflow,
    ArrivalSeqGapOrOutOfOrder,
    HookGenerationMismatch,
    LockPoisoned,
    PumpExited,
    HeartbeatExpired,
    IssueGenerationOverflow,
    PerTargetRevisionOverflow,
    HostNonceHistoryExhausted,
}

/// Current health of the WinEvent lifecycle coverage.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CoverageState {
    Healthy,
    Unhealthy(CoverageFaultReason),
}

impl CoverageState {
    pub fn is_healthy(self) -> bool {
        matches!(self, CoverageState::Healthy)
    }
}

/// Specific cause for retiring or evicting a previously issued window identity.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RetirementReason {
    DeliveredEvent(LifecycleEventKind),
    OsTupleChanged,
    CapacityEvicted,
    CoverageFault(CoverageFaultReason),
    CoverageRestarted,
}

/// Explicit retirement record emitted whenever an issued window identity is retired or evicted,
/// enabling the future adapter to invalidate matching `SnapshotRecord`s and `ready` tickets.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RetiredWindowIdentity {
    pub hwnd: u64,
    pub os_tuple: WindowOsTuple,
    pub issue_generation: u64,
    pub window_key: String,
    pub coverage_generation: u64,
    pub host_nonce: HostWorkerNonce,
    pub target_event_revision_at_retirement: u64,
    pub reason: RetirementReason,
}

/// Batch of retired/evicted window identities returned by registry transitions.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RetirementBatch {
    pub retired: Vec<RetiredWindowIdentity>,
    pub coverage_state: CoverageState,
    pub coverage_generation: u64,
    pub last_delivered_arrival_seq: u64,
}

impl RetirementBatch {
    pub fn is_empty(&self) -> bool {
        self.retired.is_empty()
    }

    pub fn contains_hwnd(&self, hwnd: u64) -> bool {
        self.retired.iter().any(|r| r.hwnd == hwnd)
    }

    pub fn contains_window_key(&self, window_key: &str) -> bool {
        self.retired.iter().any(|r| r.window_key == window_key)
    }

    pub fn contains_generation(&self, issue_generation: u64) -> bool {
        self.retired
            .iter()
            .any(|r| r.issue_generation == issue_generation)
    }
}

/// Immutable issued window identity bound to host worker nonce, coverage generation,
/// OS tuple, global checked issue generation, and per-target event revision.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IssuedWindowIdentity {
    pub host_nonce: HostWorkerNonce,
    pub coverage_generation: u64,
    pub os_tuple: WindowOsTuple,
    pub issue_generation: u64,
    pub window_key: String,
    pub target_event_revision: u64,
    pub issued_at_arrival_seq: u64,
    pub last_event_time_ms: Option<u32>,
    pub last_event_kind: Option<LifecycleEventKind>,
}

impl IssuedWindowIdentity {
    pub fn to_stamp(&self, observed_arrival_seq: u64) -> LifecycleStamp {
        LifecycleStamp {
            host_nonce: self.host_nonce,
            coverage_generation: self.coverage_generation,
            os_tuple: self.os_tuple,
            issue_generation: self.issue_generation,
            window_key: self.window_key.clone(),
            target_event_revision: self.target_event_revision,
            observed_arrival_seq,
        }
    }
}

/// Internal snapshot/execution stamp binding a window identity to a specific coverage generation
/// and per-target delivered lifecycle event revision.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LifecycleStamp {
    pub host_nonce: HostWorkerNonce,
    pub coverage_generation: u64,
    pub os_tuple: WindowOsTuple,
    pub issue_generation: u64,
    pub window_key: String,
    pub target_event_revision: u64,
    pub observed_arrival_seq: u64,
}

/// Pre-observation checkpoint recorded before sampling a target window's UIA tree / screenshot.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ObservationCheckpoint {
    pub stamp: LifecycleStamp,
}

/// Result of issuing or reusing a window identity for a live OS tuple.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IssueOutcome {
    pub identity: IssuedWindowIdentity,
    pub stamp: LifecycleStamp,
    pub reused_existing: bool,
    pub retirement_batch: RetirementBatch,
}

/// Result of draining a bounded batch of delivered WinEvent records.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DrainOutcome {
    pub processed_events: usize,
    pub retirement_batch: RetirementBatch,
}

/// Fixed bounded error enum for all lifecycle model operations.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LifecycleError {
    InvalidCapacity {
        requested: usize,
        max_allowed: usize,
    },
    InvalidHostWorkerNonce,
    InvalidCoverageGeneration,
    InvalidOsTuple(InvalidOsTupleReason),
    InvalidEventZeroHwnd,
    CoverageUnhealthy(CoverageFaultReason),
    HostWorkerNonceMismatch {
        expected: HostWorkerNonce,
        actual: HostWorkerNonce,
    },
    CoverageGenerationMismatch {
        expected: u64,
        actual: u64,
    },
    WindowGenerationRetiredOrEvicted {
        hwnd: u64,
        expected_generation: u64,
    },
    WindowGenerationMismatch {
        hwnd: u64,
        expected_generation: u64,
        actual_generation: u64,
    },
    WindowKeyMismatch {
        hwnd: u64,
    },
    TargetEventRevisionAdvanced {
        hwnd: u64,
        stamp_revision: u64,
        current_revision: u64,
    },
    LiveOsTupleMismatch {
        hwnd: u64,
        expected: WindowOsTuple,
        actual: WindowOsTuple,
    },
    EventStormBudgetExceeded {
        pending: usize,
        max_batch: usize,
    },
    RestartRequiresNewCoverageGeneration {
        current: u64,
        supplied: u64,
    },
    RestartRequiresFreshHostNonce,
    HostNonceHistoryExhausted {
        max_history: usize,
    },
    IssueGenerationOverflow,
}

impl fmt::Display for LifecycleError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:?}", self)
    }
}

impl std::error::Error for LifecycleError {}

#[derive(Debug, Clone)]
struct HwndLifecycleSlot {
    hwnd: u64,
    target_event_revision: u64,
    last_event_time_ms: Option<u32>,
    last_event_kind: Option<LifecycleEventKind>,
    touch_order: u64,
    active: Option<IssuedWindowIdentity>,
}

/// Bounded pure state machine tracking delivered window lifecycle events, coverage health,
/// and globally monotonic window generations.
#[derive(Debug, Clone)]
pub struct WindowLifecycleRegistry {
    max_capacity: usize,
    host_nonce: HostWorkerNonce,
    used_host_nonces: BTreeSet<u128>,
    coverage_generation: u64,
    coverage_state: CoverageState,
    last_delivered_arrival_seq: u64,
    next_issue_generation: u64,
    issue_generation_exhausted: bool,
    touch_counter: u64,
    slots: BTreeMap<u64, HwndLifecycleSlot>,
}

impl WindowLifecycleRegistry {
    /// Creates a new bounded lifecycle registry starting in `CoverageState::Healthy`.
    pub fn new(
        host_nonce: HostWorkerNonce,
        coverage_generation: u64,
        max_capacity: usize,
    ) -> Result<Self, LifecycleError> {
        if coverage_generation == 0 {
            return Err(LifecycleError::InvalidCoverageGeneration);
        }
        if max_capacity == 0 || max_capacity > MAX_ALLOWED_CAPACITY {
            return Err(LifecycleError::InvalidCapacity {
                requested: max_capacity,
                max_allowed: MAX_ALLOWED_CAPACITY,
            });
        }
        let mut used_host_nonces = BTreeSet::new();
        used_host_nonces.insert(host_nonce.get());
        Ok(Self {
            max_capacity,
            host_nonce,
            used_host_nonces,
            coverage_generation,
            coverage_state: CoverageState::Healthy,
            last_delivered_arrival_seq: 0,
            next_issue_generation: 1,
            issue_generation_exhausted: false,
            touch_counter: 0,
            slots: BTreeMap::new(),
        })
    }

    /// Test and overflow-verification helper to seed `next_issue_generation` near `u64::MAX`.
    pub fn with_initial_issue_generation(
        host_nonce: HostWorkerNonce,
        coverage_generation: u64,
        max_capacity: usize,
        initial_issue_generation: u64,
    ) -> Result<Self, LifecycleError> {
        if initial_issue_generation == 0 {
            return Err(LifecycleError::IssueGenerationOverflow);
        }
        let mut reg = Self::new(host_nonce, coverage_generation, max_capacity)?;
        reg.next_issue_generation = initial_issue_generation;
        Ok(reg)
    }

    pub fn max_capacity(&self) -> usize {
        self.max_capacity
    }

    pub fn max_host_nonce_history(&self) -> usize {
        MAX_HOST_NONCE_HISTORY
    }

    pub fn used_host_nonce_count(&self) -> usize {
        self.used_host_nonces.len()
    }

    pub fn tracked_slot_count(&self) -> usize {
        self.slots.len()
    }

    pub fn active_identity_count(&self) -> usize {
        self.slots.values().filter(|s| s.active.is_some()).count()
    }

    pub fn host_nonce(&self) -> HostWorkerNonce {
        self.host_nonce
    }

    pub fn coverage_generation(&self) -> u64 {
        self.coverage_generation
    }

    pub fn coverage_state(&self) -> CoverageState {
        self.coverage_state
    }

    pub fn last_delivered_arrival_seq(&self) -> u64 {
        self.last_delivered_arrival_seq
    }

    pub fn next_issue_generation(&self) -> Option<u64> {
        if self.issue_generation_exhausted {
            None
        } else {
            Some(self.next_issue_generation)
        }
    }

    /// Explicitly marks hook coverage as unhealthy (e.g. queue overflow, pump exit, heartbeat expiry)
    /// and immediately retires all active window identities across the registry.
    pub fn mark_coverage_unhealthy(&mut self, reason: CoverageFaultReason) -> RetirementBatch {
        if self.coverage_state.is_healthy() {
            self.coverage_state = CoverageState::Unhealthy(reason);
        }
        let retired = self.retire_all_active(RetirementReason::CoverageFault(reason));
        RetirementBatch {
            retired,
            coverage_state: self.coverage_state,
            coverage_generation: self.coverage_generation,
            last_delivered_arrival_seq: self.last_delivered_arrival_seq,
        }
    }

    /// Restarts lifecycle coverage under a strictly newer `new_coverage_generation` and a fresh,
    /// never-before-used `new_host_nonce`.
    ///
    /// Retires all remaining state and resets `last_delivered_arrival_seq` for the new hook pump
    /// while preserving the global monotonic `next_issue_generation` counter.
    /// Bounded nonce history (`MAX_HOST_NONCE_HISTORY`) is never silently evicted; once full,
    /// restart fails closed before adding or mutating identity state and transitions coverage to
    /// `CoverageState::Unhealthy(CoverageFaultReason::HostNonceHistoryExhausted)`, requiring a new host worker.
    pub fn restart_coverage(
        &mut self,
        new_host_nonce: HostWorkerNonce,
        new_coverage_generation: u64,
    ) -> Result<RetirementBatch, LifecycleError> {
        if self.issue_generation_exhausted {
            return Err(LifecycleError::IssueGenerationOverflow);
        }
        if matches!(
            self.coverage_state,
            CoverageState::Unhealthy(CoverageFaultReason::HostNonceHistoryExhausted)
        ) {
            return Err(LifecycleError::HostNonceHistoryExhausted {
                max_history: MAX_HOST_NONCE_HISTORY,
            });
        }
        if new_coverage_generation <= self.coverage_generation {
            return Err(LifecycleError::RestartRequiresNewCoverageGeneration {
                current: self.coverage_generation,
                supplied: new_coverage_generation,
            });
        }
        if self.used_host_nonces.contains(&new_host_nonce.get()) {
            return Err(LifecycleError::RestartRequiresFreshHostNonce);
        }
        if self.used_host_nonces.len() >= MAX_HOST_NONCE_HISTORY {
            self.coverage_state =
                CoverageState::Unhealthy(CoverageFaultReason::HostNonceHistoryExhausted);
            let _ = self.retire_all_active(RetirementReason::CoverageFault(
                CoverageFaultReason::HostNonceHistoryExhausted,
            ));
            self.slots.clear();
            return Err(LifecycleError::HostNonceHistoryExhausted {
                max_history: MAX_HOST_NONCE_HISTORY,
            });
        }

        let retired = self.retire_all_active(RetirementReason::CoverageRestarted);
        self.slots.clear();
        self.used_host_nonces.insert(new_host_nonce.get());
        self.host_nonce = new_host_nonce;
        self.coverage_generation = new_coverage_generation;
        self.coverage_state = CoverageState::Healthy;
        self.last_delivered_arrival_seq = 0;

        Ok(RetirementBatch {
            retired,
            coverage_state: self.coverage_state,
            coverage_generation: self.coverage_generation,
            last_delivered_arrival_seq: self.last_delivered_arrival_seq,
        })
    }

    /// Ingests a single delivered window lifecycle event (`ObjectCreate` or `ObjectDestroy`).
    ///
    /// Invariants:
    /// - Any delivered `ObjectCreate` or `ObjectDestroy` for `hwnd` retires any currently issued
    ///   generation for that `hwnd` and increments its `target_event_revision`.
    /// - Duplicate or late events, or events whose 32-bit `event_time_ms` wrapped around or moved
    ///   backward, still retire and advance revision; they NEVER revive a retired generation.
    /// - Non-consecutive `arrival_seq`, `arrival_seq == u64::MAX` overflow, or mismatched
    ///   `hook_generation` immediately faults coverage and retires all active identities.
    pub fn ingest_event(
        &mut self,
        event: DeliveredWindowEvent,
    ) -> Result<RetirementBatch, LifecycleError> {
        if let CoverageState::Unhealthy(reason) = self.coverage_state {
            return Err(LifecycleError::CoverageUnhealthy(reason));
        }
        if event.hwnd == 0 {
            return Err(LifecycleError::InvalidEventZeroHwnd);
        }
        if event.hook_generation != self.coverage_generation {
            let batch = self.mark_coverage_unhealthy(CoverageFaultReason::HookGenerationMismatch);
            return Err(LifecycleError::CoverageUnhealthy(
                match batch.coverage_state {
                    CoverageState::Unhealthy(r) => r,
                    CoverageState::Healthy => CoverageFaultReason::HookGenerationMismatch,
                },
            ));
        }

        let Some(expected_seq) = self.last_delivered_arrival_seq.checked_add(1) else {
            let _ = self.mark_coverage_unhealthy(CoverageFaultReason::ArrivalSeqOverflow);
            return Err(LifecycleError::CoverageUnhealthy(
                CoverageFaultReason::ArrivalSeqOverflow,
            ));
        };
        if event.arrival_seq != expected_seq {
            let fault = if event.arrival_seq == u64::MAX {
                CoverageFaultReason::ArrivalSeqOverflow
            } else {
                CoverageFaultReason::ArrivalSeqGapOrOutOfOrder
            };
            let _ = self.mark_coverage_unhealthy(fault);
            return Err(LifecycleError::CoverageUnhealthy(fault));
        }

        self.last_delivered_arrival_seq = event.arrival_seq;
        let mut retired = Vec::new();

        if !self.slots.contains_key(&event.hwnd) && self.slots.len() >= self.max_capacity {
            if let Some(evicted) = self.evict_one_slot() {
                retired.push(evicted);
            }
        }

        let touch = self.next_touch_order();
        let slot = self
            .slots
            .entry(event.hwnd)
            .or_insert_with(|| HwndLifecycleSlot {
                hwnd: event.hwnd,
                target_event_revision: 0,
                last_event_time_ms: None,
                last_event_kind: None,
                touch_order: touch,
                active: None,
            });

        let Some(next_rev) = slot.target_event_revision.checked_add(1) else {
            let mut fault_batch =
                self.mark_coverage_unhealthy(CoverageFaultReason::PerTargetRevisionOverflow);
            retired.append(&mut fault_batch.retired);
            return Err(LifecycleError::CoverageUnhealthy(
                CoverageFaultReason::PerTargetRevisionOverflow,
            ));
        };

        slot.target_event_revision = next_rev;
        slot.last_event_time_ms = Some(event.event_time_ms);
        slot.last_event_kind = Some(event.kind);
        slot.touch_order = touch;

        if let Some(active_id) = slot.active.take() {
            retired.push(RetiredWindowIdentity {
                hwnd: active_id.os_tuple.hwnd(),
                os_tuple: active_id.os_tuple,
                issue_generation: active_id.issue_generation,
                window_key: active_id.window_key,
                coverage_generation: active_id.coverage_generation,
                host_nonce: active_id.host_nonce,
                target_event_revision_at_retirement: next_rev,
                reason: RetirementReason::DeliveredEvent(event.kind),
            });
        }

        Ok(RetirementBatch {
            retired,
            coverage_state: self.coverage_state,
            coverage_generation: self.coverage_generation,
            last_delivered_arrival_seq: self.last_delivered_arrival_seq,
        })
    }

    /// Consumes a bounded batch of delivered events. If `events.len() > max_batch` (or `max_batch == 0`
    /// when `events` is non-empty), rejects with `EventStormBudgetExceeded` without unbounded spinning.
    pub fn drain_events_bounded(
        &mut self,
        events: &[DeliveredWindowEvent],
        max_batch: usize,
    ) -> Result<DrainOutcome, LifecycleError> {
        if let CoverageState::Unhealthy(reason) = self.coverage_state {
            return Err(LifecycleError::CoverageUnhealthy(reason));
        }
        if events.len() > max_batch {
            return Err(LifecycleError::EventStormBudgetExceeded {
                pending: events.len(),
                max_batch,
            });
        }

        let mut all_retired = Vec::new();
        for ev in events {
            let mut batch = self.ingest_event(*ev)?;
            all_retired.append(&mut batch.retired);
        }

        Ok(DrainOutcome {
            processed_events: events.len(),
            retirement_batch: RetirementBatch {
                retired: all_retired,
                coverage_state: self.coverage_state,
                coverage_generation: self.coverage_generation,
                last_delivered_arrival_seq: self.last_delivered_arrival_seq,
            },
        })
    }

    /// Issues a new `IssuedWindowIdentity` (or reuses the active one if and only if coverage is
    /// healthy, no lifecycle event retired the HWND, and the live `WindowOsTuple` is unchanged).
    pub fn issue_or_reuse(&mut self, live_os_tuple: WindowOsTuple) -> Result<IssueOutcome, LifecycleError> {
        if let CoverageState::Unhealthy(reason) = self.coverage_state {
            return Err(LifecycleError::CoverageUnhealthy(reason));
        }

        let hwnd = live_os_tuple.hwnd();
        let mut retired = Vec::new();

        if let Some(existing_slot) = self.slots.get(&hwnd) {
            if let Some(ref active) = existing_slot.active {
                if active.os_tuple == live_os_tuple
                    && active.coverage_generation == self.coverage_generation
                    && active.host_nonce == self.host_nonce
                {
                    let cloned_active = active.clone();
                    let stamp = cloned_active.to_stamp(self.last_delivered_arrival_seq);
                    let touch = self.next_touch_order();
                    if let Some(slot_mut) = self.slots.get_mut(&hwnd) {
                        slot_mut.touch_order = touch;
                    }
                    return Ok(IssueOutcome {
                        identity: cloned_active,
                        stamp,
                        reused_existing: true,
                        retirement_batch: RetirementBatch {
                            retired,
                            coverage_state: self.coverage_state,
                            coverage_generation: self.coverage_generation,
                            last_delivered_arrival_seq: self.last_delivered_arrival_seq,
                        },
                    });
                }
            }
        }

        // Need to issue a new generation. Check global issue_generation availability first.
        if self.issue_generation_exhausted {
            let _ = self.mark_coverage_unhealthy(CoverageFaultReason::IssueGenerationOverflow);
            return Err(LifecycleError::IssueGenerationOverflow);
        }
        let issued_gen = self.next_issue_generation;
        match self.next_issue_generation.checked_add(1) {
            Some(next) => {
                self.next_issue_generation = next;
            }
            None => {
                // `u64::MAX` is being issued now; mark exhausted so any subsequent issuance fails closed.
                self.issue_generation_exhausted = true;
            }
        }

        if !self.slots.contains_key(&hwnd) && self.slots.len() >= self.max_capacity {
            if let Some(evicted) = self.evict_one_slot() {
                retired.push(evicted);
            }
        }

        let touch = self.next_touch_order();
        let slot = self.slots.entry(hwnd).or_insert_with(|| HwndLifecycleSlot {
            hwnd,
            target_event_revision: 0,
            last_event_time_ms: None,
            last_event_kind: None,
            touch_order: touch,
            active: None,
        });
        slot.touch_order = touch;

        if let Some(old_active) = slot.active.take() {
            retired.push(RetiredWindowIdentity {
                hwnd: old_active.os_tuple.hwnd(),
                os_tuple: old_active.os_tuple,
                issue_generation: old_active.issue_generation,
                window_key: old_active.window_key,
                coverage_generation: old_active.coverage_generation,
                host_nonce: old_active.host_nonce,
                target_event_revision_at_retirement: slot.target_event_revision,
                reason: RetirementReason::OsTupleChanged,
            });
        }

        let window_key = derive_window_key(
            self.host_nonce,
            self.coverage_generation,
            live_os_tuple,
            issued_gen,
        );
        let identity = IssuedWindowIdentity {
            host_nonce: self.host_nonce,
            coverage_generation: self.coverage_generation,
            os_tuple: live_os_tuple,
            issue_generation: issued_gen,
            window_key,
            target_event_revision: slot.target_event_revision,
            issued_at_arrival_seq: self.last_delivered_arrival_seq,
            last_event_time_ms: slot.last_event_time_ms,
            last_event_kind: slot.last_event_kind,
        };
        let stamp = identity.to_stamp(self.last_delivered_arrival_seq);
        slot.active = Some(identity.clone());

        Ok(IssueOutcome {
            identity,
            stamp,
            reused_existing: false,
            retirement_batch: RetirementBatch {
                retired,
                coverage_state: self.coverage_state,
                coverage_generation: self.coverage_generation,
                last_delivered_arrival_seq: self.last_delivered_arrival_seq,
            },
        })
    }

    /// Validates an existing `LifecycleStamp` against current coverage health, registry state,
    /// and a fresh caller-verified live `WindowOsTuple`.
    ///
    /// Unrelated window events that arrived after `stamp.observed_arrival_seq` do NOT invalidate
    /// `stamp`, whereas any event on `stamp.os_tuple.hwnd()` or any coverage fault/restart does.
    pub fn validate_stamp(
        &self,
        stamp: &LifecycleStamp,
        live_os_tuple: WindowOsTuple,
    ) -> Result<LifecycleStamp, LifecycleError> {
        if let CoverageState::Unhealthy(reason) = self.coverage_state {
            return Err(LifecycleError::CoverageUnhealthy(reason));
        }
        if stamp.host_nonce != self.host_nonce {
            return Err(LifecycleError::HostWorkerNonceMismatch {
                expected: self.host_nonce,
                actual: stamp.host_nonce,
            });
        }
        if stamp.coverage_generation != self.coverage_generation {
            return Err(LifecycleError::CoverageGenerationMismatch {
                expected: self.coverage_generation,
                actual: stamp.coverage_generation,
            });
        }

        let hwnd = stamp.os_tuple.hwnd();
        if live_os_tuple != stamp.os_tuple {
            return Err(LifecycleError::LiveOsTupleMismatch {
                hwnd,
                expected: stamp.os_tuple,
                actual: live_os_tuple,
            });
        }

        let Some(slot) = self.slots.get(&hwnd) else {
            return Err(LifecycleError::WindowGenerationRetiredOrEvicted {
                hwnd,
                expected_generation: stamp.issue_generation,
            });
        };

        if slot.target_event_revision != stamp.target_event_revision {
            return Err(LifecycleError::TargetEventRevisionAdvanced {
                hwnd,
                stamp_revision: stamp.target_event_revision,
                current_revision: slot.target_event_revision,
            });
        }

        let Some(ref active) = slot.active else {
            return Err(LifecycleError::WindowGenerationRetiredOrEvicted {
                hwnd,
                expected_generation: stamp.issue_generation,
            });
        };

        if active.issue_generation != stamp.issue_generation {
            return Err(LifecycleError::WindowGenerationMismatch {
                hwnd,
                expected_generation: stamp.issue_generation,
                actual_generation: active.issue_generation,
            });
        }
        if active.window_key != stamp.window_key {
            return Err(LifecycleError::WindowKeyMismatch { hwnd });
        }
        if active.os_tuple != live_os_tuple {
            return Err(LifecycleError::LiveOsTupleMismatch {
                hwnd,
                expected: active.os_tuple,
                actual: live_os_tuple,
            });
        }

        Ok(active.to_stamp(self.last_delivered_arrival_seq))
    }

    /// Begins a snapshot observation window for `live_os_tuple`, returning an `ObservationCheckpoint`.
    pub fn begin_observation(
        &mut self,
        live_os_tuple: WindowOsTuple,
    ) -> Result<(ObservationCheckpoint, RetirementBatch), LifecycleError> {
        let outcome = self.issue_or_reuse(live_os_tuple)?;
        Ok((
            ObservationCheckpoint {
                stamp: outcome.stamp,
            },
            outcome.retirement_batch,
        ))
    }

    /// Completes a snapshot observation window by verifying that neither coverage nor the target
    /// window's lifecycle stamp changed while UIA/screenshot capture was in flight.
    pub fn finish_observation(
        &self,
        checkpoint: &ObservationCheckpoint,
        live_os_tuple: WindowOsTuple,
    ) -> Result<LifecycleStamp, LifecycleError> {
        self.validate_stamp(&checkpoint.stamp, live_os_tuple)
    }

    fn next_touch_order(&mut self) -> u64 {
        self.touch_counter = self.touch_counter.saturating_add(1);
        self.touch_counter
    }

    fn retire_all_active(&mut self, reason: RetirementReason) -> Vec<RetiredWindowIdentity> {
        let mut retired = Vec::new();
        for slot in self.slots.values_mut() {
            if let Some(active_id) = slot.active.take() {
                retired.push(RetiredWindowIdentity {
                    hwnd: active_id.os_tuple.hwnd(),
                    os_tuple: active_id.os_tuple,
                    issue_generation: active_id.issue_generation,
                    window_key: active_id.window_key,
                    coverage_generation: active_id.coverage_generation,
                    host_nonce: active_id.host_nonce,
                    target_event_revision_at_retirement: slot.target_event_revision,
                    reason,
                });
            }
        }
        retired
    }

    fn evict_one_slot(&mut self) -> Option<RetiredWindowIdentity> {
        // Prefer evicting the oldest inactive (retired) slot first so active windows stay resident;
        // if all slots are active, evict the oldest active slot and emit its retirement record.
        let candidate_hwnd = self
            .slots
            .values()
            .filter(|s| s.active.is_none())
            .min_by_key(|s| (s.touch_order, s.hwnd))
            .map(|s| s.hwnd)
            .or_else(|| {
                self.slots
                    .values()
                    .min_by_key(|s| (s.touch_order, s.hwnd))
                    .map(|s| s.hwnd)
            })?;

        let removed = self.slots.remove(&candidate_hwnd)?;
        removed.active.map(|active_id| RetiredWindowIdentity {
            hwnd: active_id.os_tuple.hwnd(),
            os_tuple: active_id.os_tuple,
            issue_generation: active_id.issue_generation,
            window_key: active_id.window_key,
            coverage_generation: active_id.coverage_generation,
            host_nonce: active_id.host_nonce,
            target_event_revision_at_retirement: removed.target_event_revision,
            reason: RetirementReason::CapacityEvicted,
        })
    }
}

/// Deterministic, dependency-free key derivation binding `(host_nonce, coverage_generation,
/// hwnd, pid, process_started, issue_generation)` into a stable `win_<24-hex>` identifier.
fn derive_window_key(
    host_nonce: HostWorkerNonce,
    coverage_generation: u64,
    os_tuple: WindowOsTuple,
    issue_generation: u64,
) -> String {
    let mut h1: u64 = 0xcbf2_9ce4_8422_2325;
    let mut h2: u64 = 0x8422_2325_cbf2_9ce4;

    let mut mix = |val: u64| {
        for b in val.to_le_bytes() {
            h1 ^= u64::from(b);
            h1 = h1.wrapping_mul(0x0000_0100_0000_01b3);
            h2 ^= u64::from(b.rotate_left(3));
            h2 = h2.wrapping_mul(0x0000_0100_0000_01b3).rotate_left(7);
        }
    };

    mix((host_nonce.get() & 0xffff_ffff_ffff_ffff) as u64);
    mix((host_nonce.get() >> 64) as u64);
    mix(coverage_generation);
    mix(os_tuple.hwnd());
    mix(u64::from(os_tuple.pid()));
    mix(os_tuple.process_started());
    mix(issue_generation);

    format!(
        "win_{:016x}{:08x}",
        h1 ^ issue_generation.rotate_left(17),
        (h2 as u32) ^ (coverage_generation as u32)
    )
}
