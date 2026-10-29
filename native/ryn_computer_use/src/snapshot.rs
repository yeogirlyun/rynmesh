use crate::errors::{DiagnosticReason, ErrorCode, ProtocolError};
use crate::lifecycle_model::LifecycleStamp;
use crate::protocol::{ActionSpec, UiaElement, WindowGeometry, WindowRef};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use windows::Win32::Security::Cryptography::{BCryptGenRandom, BCRYPT_USE_SYSTEM_PREFERRED_RNG};

/// Generates cryptographically random hex bytes using the Windows CNG system RNG (`BCryptGenRandom`).
pub fn csprng_hex(num_bytes: usize) -> String {
    let mut buf = vec![0u8; num_bytes];
    let status = unsafe { BCryptGenRandom(None, &mut buf, BCRYPT_USE_SYSTEM_PREFERRED_RNG) };
    assert!(
        status.is_ok(),
        "BCryptGenRandom failed with NTSTATUS 0x{:08X}",
        status.0 as u32
    );
    hex::encode(buf)
}

pub fn generate_worker_instance_id() -> String {
    format!("wrk_{}", csprng_hex(16))
}

pub fn generate_state_token() -> String {
    format!("st_{}", csprng_hex(16))
}

pub fn generate_prepare_ticket() -> String {
    format!("pt_{}", csprng_hex(16))
}

pub fn generate_observation_id() -> String {
    format!("obs_{}", csprng_hex(12))
}

pub fn generate_artifact_id() -> String {
    format!("art_{}", csprng_hex(12))
}

/// Computes a deterministic SHA-256 hex digest binding task, window identity, state token, and action spec.
pub fn compute_action_fingerprint(
    task_session_id: &str,
    window: &WindowRef,
    expected_state_token: &str,
    action: &ActionSpec,
) -> String {
    let mut hasher = Sha256::new();
    hasher.update(task_session_id.as_bytes());
    hasher.update(b"|");
    hasher.update(window.worker_instance.as_bytes());
    hasher.update(b"|");
    hasher.update(window.window_key.as_bytes());
    hasher.update(b"|");
    hasher.update(window.hwnd.as_bytes());
    hasher.update(b"|");
    hasher.update(window.pid.to_string().as_bytes());
    hasher.update(b"|");
    hasher.update(window.process_started.as_bytes());
    hasher.update(b"|");
    hasher.update(window.generation.to_string().as_bytes());
    hasher.update(b"|");
    hasher.update(expected_state_token.as_bytes());
    hasher.update(b"|");
    if let Ok(action_json) = serde_json::to_vec(action) {
        hasher.update(&action_json);
    }
    hex::encode(hasher.finalize())
}

#[derive(Debug, Clone)]
pub struct SnapshotRecord {
    pub state_token: String,
    pub observation_id: String,
    pub task_session_id: String,
    pub window: WindowRef,
    pub lifecycle_stamp: LifecycleStamp,
    pub geometry: WindowGeometry,
    pub elements: Vec<UiaElement>,
    pub tree_hash: String,
    pub window_mutation_epoch: u64,
}

#[derive(Clone)]
pub struct SnapshotStore {
    inner: Arc<Mutex<SnapshotStoreInner>>,
}

struct SnapshotStoreInner {
    by_token: HashMap<String, SnapshotRecord>,
    window_epochs: HashMap<String, u64>,
    retired_tasks: HashMap<String, bool>,
}

impl SnapshotStore {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(SnapshotStoreInner {
                by_token: HashMap::new(),
                window_epochs: HashMap::new(),
                retired_tasks: HashMap::new(),
            })),
        }
    }

    pub fn is_task_retired(&self, task_session_id: &str) -> bool {
        let guard = self.inner.lock().unwrap();
        guard
            .retired_tasks
            .get(task_session_id)
            .copied()
            .unwrap_or(false)
    }

    pub fn insert_snapshot(
        &self,
        task_session_id: String,
        window: WindowRef,
        lifecycle_stamp: LifecycleStamp,
        geometry: WindowGeometry,
        elements: Vec<UiaElement>,
        tree_hash: String,
    ) -> SnapshotRecord {
        let mut guard = self.inner.lock().unwrap();
        guard.retired_tasks.remove(&task_session_id);
        let epoch = *guard.window_epochs.get(&window.window_key).unwrap_or(&0);
        let state_token = generate_state_token();
        let observation_id = generate_observation_id();
        let record = SnapshotRecord {
            state_token: state_token.clone(),
            observation_id,
            task_session_id,
            window,
            lifecycle_stamp,
            geometry,
            elements,
            tree_hash,
            window_mutation_epoch: epoch,
        };
        guard.by_token.insert(state_token, record.clone());
        record
    }

    /// Revalidates that `state_token` exists, belongs to `task_session_id`, matches `window`,
    /// matches `verified_lifecycle_stamp` (host nonce, coverage generation, OS tuple, issue generation,
    /// window key, and per-target event revision), has not been superseded by a newer mutation epoch
    /// on that window, and matches live geometry + DPI.
    pub fn validate_state_token(
        &self,
        state_token: &str,
        task_session_id: &str,
        window: &WindowRef,
        verified_lifecycle_stamp: &LifecycleStamp,
        live_geometry: &WindowGeometry,
    ) -> Result<SnapshotRecord, ProtocolError> {
        let guard = self.inner.lock().unwrap();
        let record = guard.by_token.get(state_token).ok_or_else(|| {
            ProtocolError::validate(
                ErrorCode::StateTokenInvalid,
                DiagnosticReason::TokenWindowMismatch,
            )
        })?;

        if record.task_session_id != task_session_id {
            return Err(ProtocolError::validate(
                ErrorCode::TaskConflict,
                DiagnosticReason::TokenTaskMismatch,
            ));
        }

        if record.window.worker_instance != window.worker_instance
            || record.window.window_key != window.window_key
            || record.window.hwnd != window.hwnd
            || record.window.pid != window.pid
            || record.window.process_started != window.process_started
            || record.window.generation != window.generation
        {
            return Err(ProtocolError::validate(
                ErrorCode::StateTokenStale,
                DiagnosticReason::TokenWindowMismatch,
            ));
        }

        if record.lifecycle_stamp.host_nonce != verified_lifecycle_stamp.host_nonce
            || record.lifecycle_stamp.coverage_generation
                != verified_lifecycle_stamp.coverage_generation
            || record.lifecycle_stamp.os_tuple != verified_lifecycle_stamp.os_tuple
            || record.lifecycle_stamp.issue_generation != verified_lifecycle_stamp.issue_generation
            || record.lifecycle_stamp.window_key != verified_lifecycle_stamp.window_key
            || record.lifecycle_stamp.target_event_revision
                != verified_lifecycle_stamp.target_event_revision
        {
            return Err(ProtocolError::validate(
                ErrorCode::WindowStale,
                DiagnosticReason::GenerationMismatch,
            ));
        }

        let current_epoch = *guard.window_epochs.get(&window.window_key).unwrap_or(&0);
        if record.window_mutation_epoch != current_epoch {
            return Err(ProtocolError::validate(
                ErrorCode::StateTokenStale,
                DiagnosticReason::EpochAdvanced,
            ));
        }

        if record.geometry.dpi != live_geometry.dpi {
            return Err(ProtocolError::validate(
                ErrorCode::StateTokenStale,
                DiagnosticReason::DpiChanged,
            ));
        }

        if record.geometry.window_rect != live_geometry.window_rect
            || record.geometry.client_rect != live_geometry.client_rect
        {
            return Err(ProtocolError::validate(
                ErrorCode::StateTokenStale,
                DiagnosticReason::GeometryChanged,
            ));
        }

        Ok(record.clone())
    }

    /// Looks up a `SnapshotRecord`'s internal `LifecycleStamp` by `state_token` without mutating state.
    pub fn lookup_lifecycle_stamp(&self, state_token: &str) -> Option<LifecycleStamp> {
        let guard = self.inner.lock().unwrap();
        guard
            .by_token
            .get(state_token)
            .map(|r| r.lifecycle_stamp.clone())
    }

    /// Removes all snapshots belonging to a retired/evicted `window_key` and advances its local epoch.
    pub fn remove_snapshots_for_window_key(&self, window_key: &str) -> usize {
        let mut guard = self.inner.lock().unwrap();
        let next = guard.window_epochs.get(window_key).copied().unwrap_or(0) + 1;
        guard.window_epochs.insert(window_key.to_string(), next);
        let before = guard.by_token.len();
        guard
            .by_token
            .retain(|_, rec| rec.window.window_key != window_key);
        before.saturating_sub(guard.by_token.len())
    }

    /// Removes all snapshots in the store (e.g. on global coverage loss).
    pub fn clear_all_snapshots(&self) -> usize {
        let mut guard = self.inner.lock().unwrap();
        let count = guard.by_token.len();
        guard.by_token.clear();
        count
    }

    /// Advances the mutation epoch for `window_key`, immediately rendering all prior `stateToken`s
    /// for that window stale.
    pub fn advance_window_epoch(&self, window_key: &str) {
        let mut guard = self.inner.lock().unwrap();
        let next = guard.window_epochs.get(window_key).copied().unwrap_or(0) + 1;
        guard.window_epochs.insert(window_key.to_string(), next);
    }

    /// Retires all snapshots belonging to `task_session_id` without touching OS windows.
    pub fn retire_task_session(&self, task_session_id: &str) -> usize {
        let mut guard = self.inner.lock().unwrap();
        guard.retired_tasks.insert(task_session_id.to_string(), true);
        let before = guard.by_token.len();
        guard
            .by_token
            .retain(|_, rec| rec.task_session_id != task_session_id);
        before.saturating_sub(guard.by_token.len())
    }
}

impl Default for SnapshotStore {
    fn default() -> Self {
        Self::new()
    }
}
