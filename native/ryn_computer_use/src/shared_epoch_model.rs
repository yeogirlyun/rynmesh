#![forbid(unsafe_code)]

//! Pure typed cross-worker shared mutation epoch model for N05 (`ryn_computer_use::shared_epoch_model`).
//!
//! # Authority & Limitations
//! - Models fixed-page (`4096`-byte) header validation, odd/even sequence transitions, double-read
//!   observation sampling, `prepare` / `ready` / `commit` state transitions, `WAIT_ABANDONED` / orphan-odd
//!   recovery, and preflight `u64` sequence overflow rotation.
//! - Does NOT perform Win32 `CreateMutexW`, `CreateFileMappingW`, `MapViewOfFile`, or `GetSecurityInfo`
//!   system calls directly, and does NOT manufacture trusted randomness (all 128-bit `EpochNonce` values
//!   must be injected by the host from a CSPRNG).
//! - Rejects raw `bool lock_held` parameters: every operation requires a borrowed thread-affine
//!   `DesktopMutexLease` (or sealed `DesktopInputLockProof`) bound to the matching `DesktopLockNamespace`
//!   and owning thread ID.

use std::fmt;
use std::marker::PhantomData;
use std::rc::Rc;

pub const SHARED_EPOCH_MAGIC: [u8; 8] = *b"RYNEPOCH";
pub const SHARED_EPOCH_SCHEMA_VERSION: u32 = 1;
pub const SHARED_EPOCH_HEADER_BYTES: usize = 128;
pub const SHARED_EPOCH_PAGE_BYTES: usize = 4096;
pub const INITIAL_EVEN_SEQUENCE: u64 = 2;

/// Non-zero 128-bit mapping nonce supplied by the host CSPRNG.
///
/// The pure model never generates randomness internally.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct EpochNonce(u128);

impl EpochNonce {
    pub fn from_host_csprng(raw: u128) -> Result<Self, SharedEpochError> {
        if raw == 0 {
            return Err(SharedEpochError::InvalidZeroEpochNonce);
        }
        Ok(Self(raw))
    }

    pub fn get(self) -> u128 {
        self.0
    }
}

impl fmt::Display for EpochNonce {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "epn_{:032x}", self.0)
    }
}

/// Bounded writer identity stored in the shared header while `sequence` is odd.
///
/// Never stores window titles, screenshots, secrets, or arbitrary task/operation text.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ActiveWriterIdentity {
    writer_pid: u32,
    writer_process_started: u64,
    worker_nonce: u128,
    operation_digest: [u8; 32],
}

impl ActiveWriterIdentity {
    pub fn new(
        writer_pid: u32,
        writer_process_started: u64,
        worker_nonce: u128,
        operation_digest: [u8; 32],
    ) -> Result<Self, SharedEpochError> {
        if writer_pid == 0
            || writer_process_started == 0
            || worker_nonce == 0
            || operation_digest == [0u8; 32]
        {
            return Err(SharedEpochError::InvalidWriterIdentity);
        }
        Ok(Self {
            writer_pid,
            writer_process_started,
            worker_nonce,
            operation_digest,
        })
    }

    pub fn writer_pid(&self) -> u32 {
        self.writer_pid
    }

    pub fn writer_process_started(&self) -> u64 {
        self.writer_process_started
    }

    pub fn worker_nonce(&self) -> u128 {
        self.worker_nonce
    }

    pub fn operation_digest(&self) -> &[u8; 32] {
        &self.operation_digest
    }
}

/// Scoped namespace identity shared between the desktop physical input mutex and the mutation
/// epoch file mapping.
///
/// Enforces distinct kernel object names (`Local\RynComputerUseInputLock_v1_...` vs
/// `Local\RynComputerUseMutationEpoch_v1_...`) derived from the exact same user/session/desktop scope.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct DesktopLockNamespace {
    user_sid: String,
    session_id: u32,
    desktop_hash_hex16: String,
    mutex_name: String,
    mapping_name: String,
}

impl DesktopLockNamespace {
    pub fn new(
        user_sid: &str,
        session_id: u32,
        desktop_hash_hex16: &str,
    ) -> Result<Self, SharedEpochError> {
        if user_sid.trim().is_empty()
            || desktop_hash_hex16.len() != 16
            || !desktop_hash_hex16
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(SharedEpochError::InvalidLockNamespace);
        }
        let sanitized_sid: String = user_sid
            .chars()
            .map(|c| {
                if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                    c
                } else {
                    '_'
                }
            })
            .collect();

        let mutex_name = format!(
            "Local\\RynComputerUseInputLock_v1_{}_s{}_{}",
            sanitized_sid, session_id, desktop_hash_hex16
        );
        let mapping_name = format!(
            "Local\\RynComputerUseMutationEpoch_v1_{}_s{}_{}",
            sanitized_sid, session_id, desktop_hash_hex16
        );
        Ok(Self {
            user_sid: user_sid.to_string(),
            session_id,
            desktop_hash_hex16: desktop_hash_hex16.to_string(),
            mutex_name,
            mapping_name,
        })
    }

    pub fn user_sid(&self) -> &str {
        &self.user_sid
    }

    pub fn session_id(&self) -> u32 {
        self.session_id
    }

    pub fn desktop_hash_hex16(&self) -> &str {
        &self.desktop_hash_hex16
    }

    pub fn mutex_name(&self) -> &str {
        &self.mutex_name
    }

    pub fn mapping_name(&self) -> &str {
        &self.mapping_name
    }
}

/// Disposition of acquiring the physical desktop input mutex.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AcquisitionDisposition {
    Normal,
    Abandoned,
}

mod private {
    pub trait Sealed {}
}

/// Sealed capability proof representing a borrowed, thread-affine desktop input lock guard.
///
/// Callers cannot construct a proof from a raw `bool`; it must come from an active
/// `DesktopMutexLease` (or, in N05C, the real `DesktopInputLockGuard`).
pub trait DesktopInputLockProof: private::Sealed {
    fn namespace(&self) -> &DesktopLockNamespace;
    fn owning_thread_id(&self) -> u32;
    fn owner_operation_id(&self) -> &str;
    fn lease_id(&self) -> u64;
    fn acquisition_disposition(&self) -> AcquisitionDisposition;
    fn is_held(&self) -> bool;
}

/// Thread-affine (`!Send`, `!Sync`) mutex lease issued by `DesktopMutexArbiter`.
#[derive(Debug)]
pub struct DesktopMutexLease {
    namespace: DesktopLockNamespace,
    owning_thread_id: u32,
    owner_operation_id: String,
    lease_id: u64,
    disposition: AcquisitionDisposition,
    held: bool,
    holds_ready_ticket: bool,
    _not_send_sync: PhantomData<Rc<()>>,
}

impl private::Sealed for DesktopMutexLease {}

impl DesktopInputLockProof for DesktopMutexLease {
    fn namespace(&self) -> &DesktopLockNamespace {
        &self.namespace
    }

    fn owning_thread_id(&self) -> u32 {
        self.owning_thread_id
    }

    fn owner_operation_id(&self) -> &str {
        &self.owner_operation_id
    }

    fn lease_id(&self) -> u64 {
        self.lease_id
    }

    fn acquisition_disposition(&self) -> AcquisitionDisposition {
        self.disposition
    }

    fn is_held(&self) -> bool {
        self.held
    }
}

impl DesktopMutexLease {
    pub fn holds_ready_ticket(&self) -> bool {
        self.holds_ready_ticket
    }

    fn mark_abandoned_recovered(&mut self) {
        self.disposition = AcquisitionDisposition::Normal;
    }
}

/// Pure arbiter modeling the single OS `NamedMutex` domain, thread-affine ownership,
/// same-actor `ready` ticket conflict detection, hard-kill abandonment, and release quarantine.
#[derive(Debug, Clone)]
pub struct DesktopMutexArbiter {
    namespace: DesktopLockNamespace,
    next_lease_id: u64,
    active_holder: Option<(u64, u32, String, bool)>,
    pending_abandoned: bool,
    quarantined: bool,
}

impl DesktopMutexArbiter {
    pub fn new(namespace: DesktopLockNamespace) -> Self {
        Self {
            namespace,
            next_lease_id: 1,
            active_holder: None,
            pending_abandoned: false,
            quarantined: false,
        }
    }

    pub fn namespace(&self) -> &DesktopLockNamespace {
        &self.namespace
    }

    pub fn is_quarantined(&self) -> bool {
        self.quarantined
    }

    /// Acquires the mutex for a short observation header read (`state.get`).
    ///
    /// If the same actor thread already holds a `ready` ticket lease, immediately returns
    /// `LockBusySameActorReadyTicket` rather than deadlocking on recursive acquisition.
    pub fn acquire_for_observation(
        &mut self,
        thread_id: u32,
        operation_id: &str,
    ) -> Result<DesktopMutexLease, SharedEpochError> {
        self.acquire_internal(thread_id, operation_id, false)
    }

    /// Acquires the mutex for `action.prepare` / `action.commit` / recovery.
    pub fn acquire_for_mutation(
        &mut self,
        thread_id: u32,
        operation_id: &str,
    ) -> Result<DesktopMutexLease, SharedEpochError> {
        self.acquire_internal(thread_id, operation_id, true)
    }

    fn acquire_internal(
        &mut self,
        thread_id: u32,
        operation_id: &str,
        for_ready_or_mutation: bool,
    ) -> Result<DesktopMutexLease, SharedEpochError> {
        if self.quarantined {
            return Err(SharedEpochError::LockQuarantined);
        }
        if thread_id == 0 || operation_id.trim().is_empty() {
            return Err(SharedEpochError::InvalidLockParameters);
        }
        if let Some((_, holder_tid, _, holder_is_ready)) = self.active_holder {
            if holder_tid == thread_id && holder_is_ready {
                return Err(SharedEpochError::LockBusySameActorReadyTicket);
            }
            return Err(SharedEpochError::LockBusy);
        }

        let lease_id = self.next_lease_id;
        self.next_lease_id = self
            .next_lease_id
            .checked_add(1)
            .ok_or(SharedEpochError::LockQuarantined)?;

        let disposition = if self.pending_abandoned {
            AcquisitionDisposition::Abandoned
        } else {
            AcquisitionDisposition::Normal
        };

        self.active_holder = Some((
            lease_id,
            thread_id,
            operation_id.to_string(),
            for_ready_or_mutation,
        ));

        Ok(DesktopMutexLease {
            namespace: self.namespace.clone(),
            owning_thread_id: thread_id,
            owner_operation_id: operation_id.to_string(),
            lease_id,
            disposition,
            held: true,
            holds_ready_ticket: for_ready_or_mutation,
            _not_send_sync: PhantomData,
        })
    }

    /// Simulates a worker process dying (e.g. `TerminateProcess` / hard-kill) while holding the OS mutex.
    /// The next acquirer will observe `AcquisitionDisposition::Abandoned` (`WAIT_ABANDONED`).
    pub fn simulate_holder_hard_kill(
        &mut self,
        mut lease: DesktopMutexLease,
    ) -> Result<(), SharedEpochError> {
        if !lease.held {
            return Err(SharedEpochError::LeaseNotHeld);
        }
        match self.active_holder {
            Some((active_id, _, _, _)) if active_id == lease.lease_id => {
                lease.held = false;
                self.active_holder = None;
                self.pending_abandoned = true;
                Ok(())
            }
            _ => Err(SharedEpochError::LeaseNotHeld),
        }
    }

    /// Releases the mutex lease on its owning thread.
    ///
    /// Releasing from the wrong thread or releasing an unrecovered `WAIT_ABANDONED` lease
    /// quarantines the lock arbiter and does NOT clear the active owner slot.
    pub fn release_lease(
        &mut self,
        lease: &mut DesktopMutexLease,
        current_thread_id: u32,
    ) -> Result<(), SharedEpochError> {
        if !lease.held {
            return Err(SharedEpochError::LeaseNotHeld);
        }
        if current_thread_id != lease.owning_thread_id {
            self.quarantined = true;
            lease.held = false;
            return Err(SharedEpochError::LockQuarantinedWrongThread {
                owning_thread_id: lease.owning_thread_id,
                attempted_thread_id: current_thread_id,
            });
        }
        let Some((active_id, _, _, _)) = self.active_holder else {
            self.quarantined = true;
            lease.held = false;
            return Err(SharedEpochError::LockQuarantined);
        };
        if active_id != lease.lease_id {
            self.quarantined = true;
            lease.held = false;
            return Err(SharedEpochError::LockQuarantined);
        }
        if lease.disposition == AcquisitionDisposition::Abandoned {
            self.quarantined = true;
            lease.held = false;
            return Err(SharedEpochError::AbandonedRecoveryRequired);
        }

        lease.held = false;
        self.pending_abandoned = false;
        self.active_holder = None;
        Ok(())
    }
}

/// Validated even-epoch stamp bound into internal `SnapshotRecord`s and `action.prepare` tickets.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct SharedEpochStamp {
    pub mapping_nonce: EpochNonce,
    pub sequence: u64,
}

impl SharedEpochStamp {
    pub fn is_even(&self) -> bool {
        self.sequence >= INITIAL_EVEN_SEQUENCE && self.sequence % 2 == 0
    }
}

/// First-read sample captured before unlocked UIA/screenshot collection.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ObservationEpochSample {
    pub pre_stamp: SharedEpochStamp,
}

/// Validated logical representation of the 128-byte fixed header in the 4096-byte shared page.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SharedEpochHeader {
    pub schema_version: u32,
    pub struct_bytes: u32,
    pub mapping_nonce: EpochNonce,
    pub sequence: u64,
    pub active_writer: Option<ActiveWriterIdentity>,
}

impl SharedEpochHeader {
    pub fn is_even(&self) -> bool {
        self.sequence % 2 == 0
    }

    pub fn is_odd(&self) -> bool {
        self.sequence % 2 == 1
    }

    pub fn encode(&self) -> Result<[u8; SHARED_EPOCH_HEADER_BYTES], SharedEpochError> {
        if self.schema_version != SHARED_EPOCH_SCHEMA_VERSION {
            return Err(SharedEpochError::UnsupportedSchemaVersion(
                self.schema_version,
            ));
        }
        if self.struct_bytes != SHARED_EPOCH_HEADER_BYTES as u32 {
            return Err(SharedEpochError::CorruptedHeader(
                CorruptedHeaderReason::BadStructBytes,
            ));
        }
        if self.sequence < INITIAL_EVEN_SEQUENCE {
            return Err(SharedEpochError::CorruptedHeader(
                CorruptedHeaderReason::ZeroOrInvalidSequence,
            ));
        }
        if self.is_even() && self.active_writer.is_some() {
            return Err(SharedEpochError::CorruptedHeader(
                CorruptedHeaderReason::EvenSequenceHasDirtyWriterFields,
            ));
        }
        if self.is_odd() && self.active_writer.is_none() {
            return Err(SharedEpochError::CorruptedHeader(
                CorruptedHeaderReason::OddSequenceMissingWriterIdentity,
            ));
        }

        let mut buf = [0u8; SHARED_EPOCH_HEADER_BYTES];
        buf[0..8].copy_from_slice(&SHARED_EPOCH_MAGIC);
        buf[8..12].copy_from_slice(&self.schema_version.to_le_bytes());
        buf[12..16].copy_from_slice(&self.struct_bytes.to_le_bytes());
        // 16..24: header_reserved = 0
        buf[24..40].copy_from_slice(&self.mapping_nonce.get().to_le_bytes());
        buf[40..48].copy_from_slice(&self.sequence.to_le_bytes());

        if let Some(writer) = self.active_writer {
            buf[48..52].copy_from_slice(&writer.writer_pid().to_le_bytes());
            // 52..56: writer_reserved = 0
            buf[56..64].copy_from_slice(&writer.writer_process_started().to_le_bytes());
            buf[64..80].copy_from_slice(&writer.worker_nonce().to_le_bytes());
            buf[80..112].copy_from_slice(writer.operation_digest());
        }
        // 112..128: tail_reserved = 0
        Ok(buf)
    }

    pub fn decode_and_validate(raw: &[u8]) -> Result<Self, SharedEpochError> {
        if raw.len() < SHARED_EPOCH_HEADER_BYTES {
            return Err(SharedEpochError::InvalidMappingSize {
                actual: raw.len(),
                expected: SHARED_EPOCH_HEADER_BYTES,
            });
        }
        if raw[0..8] != SHARED_EPOCH_MAGIC {
            return Err(SharedEpochError::CorruptedHeader(
                CorruptedHeaderReason::BadMagic,
            ));
        }
        let schema_version = u32::from_le_bytes([raw[8], raw[9], raw[10], raw[11]]);
        if schema_version != SHARED_EPOCH_SCHEMA_VERSION {
            return Err(SharedEpochError::UnsupportedSchemaVersion(schema_version));
        }
        let struct_bytes = u32::from_le_bytes([raw[12], raw[13], raw[14], raw[15]]);
        if struct_bytes != SHARED_EPOCH_HEADER_BYTES as u32 {
            return Err(SharedEpochError::CorruptedHeader(
                CorruptedHeaderReason::BadStructBytes,
            ));
        }

        let header_reserved = u64::from_le_bytes([
            raw[16], raw[17], raw[18], raw[19], raw[20], raw[21], raw[22], raw[23],
        ]);
        let writer_reserved = u32::from_le_bytes([raw[52], raw[53], raw[54], raw[55]]);
        if header_reserved != 0 || writer_reserved != 0 || raw[112..128].iter().any(|&b| b != 0) {
            return Err(SharedEpochError::CorruptedHeader(
                CorruptedHeaderReason::NonZeroReservedFields,
            ));
        }

        let mut nonce_bytes = [0u8; 16];
        nonce_bytes.copy_from_slice(&raw[24..40]);
        let raw_nonce = u128::from_le_bytes(nonce_bytes);
        if raw_nonce == 0 {
            return Err(SharedEpochError::CorruptedHeader(
                CorruptedHeaderReason::ZeroMappingNonce,
            ));
        }
        let mapping_nonce = EpochNonce(raw_nonce);

        let sequence = u64::from_le_bytes([
            raw[40], raw[41], raw[42], raw[43], raw[44], raw[45], raw[46], raw[47],
        ]);
        if sequence < INITIAL_EVEN_SEQUENCE {
            return Err(SharedEpochError::CorruptedHeader(
                CorruptedHeaderReason::ZeroOrInvalidSequence,
            ));
        }

        let writer_pid = u32::from_le_bytes([raw[48], raw[49], raw[50], raw[51]]);
        let writer_process_started = u64::from_le_bytes([
            raw[56], raw[57], raw[58], raw[59], raw[60], raw[61], raw[62], raw[63],
        ]);
        let mut worker_nonce_bytes = [0u8; 16];
        worker_nonce_bytes.copy_from_slice(&raw[64..80]);
        let worker_nonce = u128::from_le_bytes(worker_nonce_bytes);
        let mut operation_digest = [0u8; 32];
        operation_digest.copy_from_slice(&raw[80..112]);

        let all_writer_zero = writer_pid == 0
            && writer_process_started == 0
            && worker_nonce == 0
            && operation_digest == [0u8; 32];

        if sequence % 2 == 0 {
            if !all_writer_zero {
                return Err(SharedEpochError::CorruptedHeader(
                    CorruptedHeaderReason::EvenSequenceHasDirtyWriterFields,
                ));
            }
            Ok(Self {
                schema_version,
                struct_bytes,
                mapping_nonce,
                sequence,
                active_writer: None,
            })
        } else {
            let writer = ActiveWriterIdentity::new(
                writer_pid,
                writer_process_started,
                worker_nonce,
                operation_digest,
            )
            .map_err(|_| {
                SharedEpochError::CorruptedHeader(
                    CorruptedHeaderReason::OddSequenceMissingWriterIdentity,
                )
            })?;
            Ok(Self {
                schema_version,
                struct_bytes,
                mapping_nonce,
                sequence,
                active_writer: Some(writer),
            })
        }
    }
}

/// Non-cloneable ticket produced by `prepare_write` while holding the mutex lease before any
/// physical activation.
///
/// Bound to a unique model-issued `ticket_id`, the active `lease_id`, `owning_thread_id`,
/// `expected_stamp`, and `writer_identity`. Cannot be cloned, copied, or replayed after
/// cancellation/expiry or `begin_write`.
#[derive(Debug, PartialEq, Eq)]
pub struct PreparedEpochTicket {
    ticket_id: u64,
    lease_id: u64,
    owning_thread_id: u32,
    expected_stamp: SharedEpochStamp,
    writer_identity: ActiveWriterIdentity,
}

impl PreparedEpochTicket {
    pub fn ticket_id(&self) -> u64 {
        self.ticket_id
    }

    pub fn lease_id(&self) -> u64 {
        self.lease_id
    }

    pub fn owning_thread_id(&self) -> u32 {
        self.owning_thread_id
    }

    pub fn expected_stamp(&self) -> SharedEpochStamp {
        self.expected_stamp
    }

    pub fn writer_identity(&self) -> &ActiveWriterIdentity {
        &self.writer_identity
    }
}

/// Non-cloneable active write handle produced by `begin_write` after consuming the single active
/// `PreparedEpochTicket` and publishing the odd sequence and writer identity.
#[derive(Debug, PartialEq, Eq)]
pub struct ActiveWriteHandle {
    ticket_id: u64,
    lease_id: u64,
    mapping_nonce: EpochNonce,
    odd_sequence: u64,
    reserved_next_even: u64,
    writer_identity: ActiveWriterIdentity,
    finished: bool,
}

impl ActiveWriteHandle {
    pub fn ticket_id(&self) -> u64 {
        self.ticket_id
    }

    pub fn mapping_nonce(&self) -> EpochNonce {
        self.mapping_nonce
    }

    pub fn odd_sequence(&self) -> u64 {
        self.odd_sequence
    }

    pub fn reserved_next_even(&self) -> u64 {
        self.reserved_next_even
    }

    pub fn writer_identity(&self) -> &ActiveWriterIdentity {
        &self.writer_identity
    }

    pub fn is_finished(&self) -> bool {
        self.finished
    }
}

/// Proof token that the caller has completed its own release-only input cleanup before advancing
/// the shared epoch from odd back to even.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReleaseOnlyCleanupProof {
    _priv: (),
}

impl ReleaseOnlyCleanupProof {
    pub fn completed() -> Self {
        Self { _priv: () }
    }
}

/// Terminal disposition of a commit execution after `begin_write` published the odd sequence.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WriteTerminalKind {
    Succeeded,
    FailedAfterActivation,
    CancelledAfterActivation,
}

/// Host ledger accounting classification after `finish_write` advances to the next even sequence.
///
/// Advancing to `even` only signals that no Ryn write is currently executing; any failure or
/// cancellation after activation remains `UnknownOrPartial` and must NOT be replayed or treated as clean.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MutationAccounting {
    Confirmed,
    UnknownOrPartial,
}

/// Outcome returned when `finish_write` transitions the header from `odd` to `reserved_next_even`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FinishWriteOutcome {
    pub new_stamp: SharedEpochStamp,
    pub terminal_kind: WriteTerminalKind,
    pub mutation_accounting: MutationAccounting,
}

/// Explicit outcome code when recovering an abandoned mutex or orphan odd header.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RecoveryDecision {
    RecoveredButRejectOldRequest,
}

/// Detailed report produced when recovering from `WAIT_ABANDONED` or an orphan odd sequence.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RecoveryReport {
    pub decision: RecoveryDecision,
    pub previous_nonce: EpochNonce,
    pub previous_sequence: u64,
    pub interrupted_writer: Option<ActiveWriterIdentity>,
    pub new_stamp: SharedEpochStamp,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CorruptedHeaderReason {
    BadMagic,
    BadStructBytes,
    NonZeroReservedFields,
    ZeroMappingNonce,
    ZeroOrInvalidSequence,
    EvenSequenceHasDirtyWriterFields,
    OddSequenceMissingWriterIdentity,
}

/// Fixed bounded error enum for all shared epoch model operations.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SharedEpochError {
    InvalidLockNamespace,
    InvalidLockParameters,
    InvalidZeroEpochNonce,
    InvalidWriterIdentity,
    InvalidOddExpectedStamp {
        sequence: u64,
    },
    InvalidMappingSize {
        actual: usize,
        expected: usize,
    },
    CorruptedHeader(CorruptedHeaderReason),
    UnsupportedSchemaVersion(u32),
    LockBusy,
    LockBusySameActorReadyTicket,
    LockQuarantined,
    LockQuarantinedWrongThread {
        owning_thread_id: u32,
        attempted_thread_id: u32,
    },
    LeaseNotHeld,
    LockIdentityMismatch,
    WrongOwningThread {
        expected_thread_id: u32,
        actual_thread_id: u32,
    },
    EpochBusyOddSequence {
        sequence: u64,
    },
    EpochNonceRotated {
        expected: EpochNonce,
        actual: EpochNonce,
    },
    EpochAdvanced {
        expected_sequence: u64,
        actual_sequence: u64,
    },
    EpochAdvancedDuringObservation {
        before_sequence: u64,
        after_sequence: u64,
    },
    AbandonedRecoveryRequired,
    RecoveryNotRequired,
    NonceReuseDisallowed,
    RecoveredButRejectOldRequest(RecoveryReport),
    SequenceOverflowRotationRequired {
        current_sequence: u64,
    },
    SequenceOverflowRotatedRejectOldStamp {
        previous_nonce: EpochNonce,
        previous_sequence: u64,
        new_stamp: SharedEpochStamp,
    },
    ReadyTicketAlreadyActive {
        active_ticket_id: u64,
    },
    StaleOrInactivePreparedTicket,
    PreparedTicketIdOverflow,
    ForeignOrStaleWriteHandle,
    ModelQuarantined,
}

impl fmt::Display for SharedEpochError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:?}", self)
    }
}

impl std::error::Error for SharedEpochError {}

/// In-memory 4096-byte page backing the shared mutation epoch state machine.
///
/// Models both `create_new` (first creator initializes header) and `open_existing` (validates
/// existing 4096-byte view without ever resetting a corrupted/unknown header).
#[derive(Clone)]
pub struct SharedEpochPageBuffer {
    bytes: [u8; SHARED_EPOCH_PAGE_BYTES],
}

impl fmt::Debug for SharedEpochPageBuffer {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("SharedEpochPageBuffer")
            .field("page_bytes", &SHARED_EPOCH_PAGE_BYTES)
            .finish()
    }
}

impl SharedEpochPageBuffer {
    pub fn zeroed_uninitialized() -> Self {
        Self {
            bytes: [0u8; SHARED_EPOCH_PAGE_BYTES],
        }
    }

    pub fn from_raw_page(bytes: [u8; SHARED_EPOCH_PAGE_BYTES]) -> Self {
        Self { bytes }
    }

    pub fn as_bytes(&self) -> &[u8; SHARED_EPOCH_PAGE_BYTES] {
        &self.bytes
    }

    pub fn as_bytes_mut(&mut self) -> &mut [u8; SHARED_EPOCH_PAGE_BYTES] {
        &mut self.bytes
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct ActivePreparedSlot {
    ticket_id: u64,
    lease_id: u64,
    owning_thread_id: u32,
    expected_stamp: SharedEpochStamp,
    writer_identity: ActiveWriterIdentity,
}

/// Pure state machine managing a fixed-page shared mutation epoch under a `DesktopLockNamespace`.
///
/// Owns the single active prepared (`ready`) ticket slot so that at most one `PreparedEpochTicket`
/// can be live at any time, and any cancellation/expiry immediately invalidates the prepared slot
/// before a subsequent `begin_write` or new `prepare_write`.
#[derive(Debug, Clone)]
pub struct SharedEpochModel {
    namespace: DesktopLockNamespace,
    page: SharedEpochPageBuffer,
    next_prepared_ticket_id: u64,
    prepared_ticket_id_exhausted: bool,
    active_prepared_slot: Option<ActivePreparedSlot>,
    quarantined: bool,
}

impl SharedEpochModel {
    /// Initializes a brand-new shared epoch mapping (`ERROR_ALREADY_EXISTS == false`) while holding
    /// the desktop mutex lease.
    pub fn create_new<G: DesktopInputLockProof>(
        namespace: DesktopLockNamespace,
        guard: &G,
        current_thread_id: u32,
        initial_nonce: EpochNonce,
    ) -> Result<Self, SharedEpochError> {
        Self::verify_guard_static(&namespace, guard, current_thread_id)?;
        let header = SharedEpochHeader {
            schema_version: SHARED_EPOCH_SCHEMA_VERSION,
            struct_bytes: SHARED_EPOCH_HEADER_BYTES as u32,
            mapping_nonce: initial_nonce,
            sequence: INITIAL_EVEN_SEQUENCE,
            active_writer: None,
        };
        let encoded = header.encode()?;
        let mut page = SharedEpochPageBuffer::zeroed_uninitialized();
        page.as_bytes_mut()[0..SHARED_EPOCH_HEADER_BYTES].copy_from_slice(&encoded);

        Ok(Self {
            namespace,
            page,
            next_prepared_ticket_id: 1,
            prepared_ticket_id_exhausted: false,
            active_prepared_slot: None,
            quarantined: false,
        })
    }

    /// Test and overflow-proof helper to create a new mapping seeded at a specific valid even sequence.
    pub fn create_new_with_even_sequence<G: DesktopInputLockProof>(
        namespace: DesktopLockNamespace,
        guard: &G,
        current_thread_id: u32,
        initial_nonce: EpochNonce,
        initial_even_sequence: u64,
    ) -> Result<Self, SharedEpochError> {
        Self::verify_guard_static(&namespace, guard, current_thread_id)?;
        if initial_even_sequence < INITIAL_EVEN_SEQUENCE || initial_even_sequence % 2 != 0 {
            return Err(SharedEpochError::CorruptedHeader(
                CorruptedHeaderReason::ZeroOrInvalidSequence,
            ));
        }
        let header = SharedEpochHeader {
            schema_version: SHARED_EPOCH_SCHEMA_VERSION,
            struct_bytes: SHARED_EPOCH_HEADER_BYTES as u32,
            mapping_nonce: initial_nonce,
            sequence: initial_even_sequence,
            active_writer: None,
        };
        let encoded = header.encode()?;
        let mut page = SharedEpochPageBuffer::zeroed_uninitialized();
        page.as_bytes_mut()[0..SHARED_EPOCH_HEADER_BYTES].copy_from_slice(&encoded);

        Ok(Self {
            namespace,
            page,
            next_prepared_ticket_id: 1,
            prepared_ticket_id_exhausted: false,
            active_prepared_slot: None,
            quarantined: false,
        })
    }

    /// Opens an existing shared epoch mapping (`ERROR_ALREADY_EXISTS == true`) while holding the
    /// desktop mutex lease.
    ///
    /// Strictly validates the existing 4096-byte page and header; NEVER resets or overwrites an
    /// all-zero, corrupted, or unknown-version existing mapping.
    pub fn open_existing<G: DesktopInputLockProof>(
        namespace: DesktopLockNamespace,
        guard: &G,
        current_thread_id: u32,
        existing_bytes: &[u8],
    ) -> Result<Self, SharedEpochError> {
        Self::verify_guard_static(&namespace, guard, current_thread_id)?;
        if existing_bytes.len() != SHARED_EPOCH_PAGE_BYTES {
            return Err(SharedEpochError::InvalidMappingSize {
                actual: existing_bytes.len(),
                expected: SHARED_EPOCH_PAGE_BYTES,
            });
        }
        let _validated = SharedEpochHeader::decode_and_validate(existing_bytes)?;
        let mut raw = [0u8; SHARED_EPOCH_PAGE_BYTES];
        raw.copy_from_slice(existing_bytes);
        Ok(Self {
            namespace,
            page: SharedEpochPageBuffer::from_raw_page(raw),
            next_prepared_ticket_id: 1,
            prepared_ticket_id_exhausted: false,
            active_prepared_slot: None,
            quarantined: false,
        })
    }

    pub fn namespace(&self) -> &DesktopLockNamespace {
        &self.namespace
    }

    pub fn is_quarantined(&self) -> bool {
        self.quarantined
    }

    pub fn has_active_prepared_ticket(&self) -> bool {
        self.active_prepared_slot.is_some()
    }

    pub fn active_prepared_ticket_id(&self) -> Option<u64> {
        self.active_prepared_slot.map(|s| s.ticket_id)
    }

    pub fn raw_page_bytes(&self) -> &[u8; SHARED_EPOCH_PAGE_BYTES] {
        self.page.as_bytes()
    }

    /// Injects raw bytes into the underlying page buffer to simulate external corruption in pure tests.
    pub fn overwrite_raw_header_for_test(&mut self, offset: usize, data: &[u8]) {
        let end = (offset + data.len()).min(SHARED_EPOCH_PAGE_BYTES);
        if offset < end {
            self.page.as_bytes_mut()[offset..end].copy_from_slice(&data[..(end - offset)]);
        }
    }

    /// Reads the current validated header under a borrowed desktop mutex proof.
    pub fn inspect_header<G: DesktopInputLockProof>(
        &mut self,
        guard: &G,
        current_thread_id: u32,
    ) -> Result<SharedEpochHeader, SharedEpochError> {
        self.verify_guard(guard, current_thread_id)?;
        self.read_validated_header()
    }

    /// Step 1 of `state.get` observation: short mutex-guarded read before UIA/screenshot capture.
    ///
    /// Refuses if the mutex was acquired via `WAIT_ABANDONED` or if `sequence` is odd (write in progress).
    pub fn sample_before_observation<G: DesktopInputLockProof>(
        &mut self,
        guard: &G,
        current_thread_id: u32,
    ) -> Result<ObservationEpochSample, SharedEpochError> {
        self.verify_guard(guard, current_thread_id)?;
        if guard.acquisition_disposition() == AcquisitionDisposition::Abandoned {
            return Err(SharedEpochError::AbandonedRecoveryRequired);
        }
        let header = self.read_validated_header()?;
        if header.is_odd() {
            return Err(SharedEpochError::EpochBusyOddSequence {
                sequence: header.sequence,
            });
        }
        Ok(ObservationEpochSample {
            pre_stamp: SharedEpochStamp {
                mapping_nonce: header.mapping_nonce,
                sequence: header.sequence,
            },
        })
    }

    /// Step 2 of `state.get` observation: short mutex-guarded re-read after UIA/screenshot capture.
    ///
    /// Succeeds if and only if the header is still healthy/even and `(mapping_nonce, sequence)`
    /// match `pre_sample.pre_stamp`.
    pub fn sample_after_observation<G: DesktopInputLockProof>(
        &mut self,
        guard: &G,
        current_thread_id: u32,
        pre_sample: &ObservationEpochSample,
    ) -> Result<SharedEpochStamp, SharedEpochError> {
        self.verify_guard(guard, current_thread_id)?;
        if guard.acquisition_disposition() == AcquisitionDisposition::Abandoned {
            return Err(SharedEpochError::AbandonedRecoveryRequired);
        }
        let header = self.read_validated_header()?;
        if header.is_odd() {
            return Err(SharedEpochError::EpochBusyOddSequence {
                sequence: header.sequence,
            });
        }
        if header.mapping_nonce != pre_sample.pre_stamp.mapping_nonce {
            return Err(SharedEpochError::EpochNonceRotated {
                expected: pre_sample.pre_stamp.mapping_nonce,
                actual: header.mapping_nonce,
            });
        }
        if header.sequence != pre_sample.pre_stamp.sequence {
            return Err(SharedEpochError::EpochAdvancedDuringObservation {
                before_sequence: pre_sample.pre_stamp.sequence,
                after_sequence: header.sequence,
            });
        }
        Ok(pre_sample.pre_stamp)
    }

    /// Explicitly recovers from `WAIT_ABANDONED` or an orphan odd sequence while holding the
    /// exclusive desktop mutex lease.
    ///
    /// Rotates `mapping_nonce` to a fresh distinct CSPRNG nonce, resets `sequence` to
    /// `INITIAL_EVEN_SEQUENCE` (`2`), clears `active_writer` and any active prepared ticket slot,
    /// and returns `RecoveryReport` with `RecoveryDecision::RecoveredButRejectOldRequest`.
    pub fn recover_abandoned_or_orphan(
        &mut self,
        guard: &mut DesktopMutexLease,
        current_thread_id: u32,
        fresh_nonce: EpochNonce,
    ) -> Result<RecoveryReport, SharedEpochError> {
        self.verify_guard(guard, current_thread_id)?;
        let header = self.read_validated_header()?;

        let is_abandoned = guard.acquisition_disposition() == AcquisitionDisposition::Abandoned;
        let is_orphan_odd = header.is_odd();
        if !is_abandoned && !is_orphan_odd {
            return Err(SharedEpochError::RecoveryNotRequired);
        }
        if fresh_nonce == header.mapping_nonce {
            return Err(SharedEpochError::NonceReuseDisallowed);
        }

        self.active_prepared_slot = None;

        let new_header = SharedEpochHeader {
            schema_version: SHARED_EPOCH_SCHEMA_VERSION,
            struct_bytes: SHARED_EPOCH_HEADER_BYTES as u32,
            mapping_nonce: fresh_nonce,
            sequence: INITIAL_EVEN_SEQUENCE,
            active_writer: None,
        };
        self.write_validated_header(&new_header)?;
        guard.mark_abandoned_recovered();

        let new_stamp = SharedEpochStamp {
            mapping_nonce: fresh_nonce,
            sequence: INITIAL_EVEN_SEQUENCE,
        };
        Ok(RecoveryReport {
            decision: RecoveryDecision::RecoveredButRejectOldRequest,
            previous_nonce: header.mapping_nonce,
            previous_sequence: header.sequence,
            interrupted_writer: header.active_writer,
            new_stamp,
        })
    }

    /// Validates `expected_stamp` during `action.prepare` while holding the desktop mutex lease,
    /// and reserves the model's single active prepared ticket slot.
    ///
    /// - If the mutex acquisition was `WAIT_ABANDONED` or the header is currently odd, and
    ///   `recovery_fresh_nonce` is supplied, rotates the epoch, clears any prepared slot, and returns
    ///   `Err(SharedEpochError::RecoveredButRejectOldRequest(..))` (never recovers and continues!).
    /// - If a `PreparedEpochTicket` is already active (`has_active_prepared_ticket() == true`),
    ///   refuses with `Err(SharedEpochError::ReadyTicketAlreadyActive { .. })` before mutating state.
    /// - While `PreparedEpochTicket` (`ready`) is held, the header remains even (`sequence` does not advance).
    pub fn prepare_write(
        &mut self,
        guard: &mut DesktopMutexLease,
        current_thread_id: u32,
        expected_stamp: SharedEpochStamp,
        writer_identity: ActiveWriterIdentity,
        recovery_fresh_nonce: Option<EpochNonce>,
    ) -> Result<PreparedEpochTicket, SharedEpochError> {
        self.verify_guard(guard, current_thread_id)?;
        if !expected_stamp.is_even() {
            return Err(SharedEpochError::InvalidOddExpectedStamp {
                sequence: expected_stamp.sequence,
            });
        }

        let header = self.read_validated_header()?;
        if guard.acquisition_disposition() == AcquisitionDisposition::Abandoned || header.is_odd() {
            self.active_prepared_slot = None;
            let Some(fresh_nonce) = recovery_fresh_nonce else {
                return Err(SharedEpochError::AbandonedRecoveryRequired);
            };
            let report = self.recover_abandoned_or_orphan(guard, current_thread_id, fresh_nonce)?;
            return Err(SharedEpochError::RecoveredButRejectOldRequest(report));
        }

        if let Some(active) = self.active_prepared_slot {
            return Err(SharedEpochError::ReadyTicketAlreadyActive {
                active_ticket_id: active.ticket_id,
            });
        }

        if header.mapping_nonce != expected_stamp.mapping_nonce {
            return Err(SharedEpochError::EpochNonceRotated {
                expected: expected_stamp.mapping_nonce,
                actual: header.mapping_nonce,
            });
        }
        if header.sequence != expected_stamp.sequence {
            return Err(SharedEpochError::EpochAdvanced {
                expected_sequence: expected_stamp.sequence,
                actual_sequence: header.sequence,
            });
        }

        if self.prepared_ticket_id_exhausted {
            return Err(SharedEpochError::PreparedTicketIdOverflow);
        }
        let ticket_id = self.next_prepared_ticket_id;
        match self.next_prepared_ticket_id.checked_add(1) {
            Some(next) => {
                self.next_prepared_ticket_id = next;
            }
            None => {
                self.prepared_ticket_id_exhausted = true;
            }
        }

        let slot = ActivePreparedSlot {
            ticket_id,
            lease_id: guard.lease_id(),
            owning_thread_id: current_thread_id,
            expected_stamp,
            writer_identity,
        };
        self.active_prepared_slot = Some(slot);

        Ok(PreparedEpochTicket {
            ticket_id,
            lease_id: guard.lease_id(),
            owning_thread_id: current_thread_id,
            expected_stamp,
            writer_identity,
        })
    }

    /// Cancels or expires a `ready` ticket by value before `begin_write`.
    ///
    /// Requires `ticket` to match the model's currently active prepared slot. A stale ticket
    /// from an earlier prepare cannot cancel or interfere with a newly prepared writer.
    /// Leaves the header untouched at its original even `sequence` (zero odd publication, zero advance).
    pub fn cancel_or_expire_ready<G: DesktopInputLockProof>(
        &mut self,
        guard: &G,
        current_thread_id: u32,
        ticket: PreparedEpochTicket,
    ) -> Result<SharedEpochStamp, SharedEpochError> {
        self.verify_guard(guard, current_thread_id)?;
        self.verify_active_prepared_ticket(guard, current_thread_id, &ticket)?;

        // Invalidate the model-owned active prepared slot before checking header state.
        self.active_prepared_slot = None;

        let header = self.read_validated_header()?;
        if header.mapping_nonce != ticket.expected_stamp.mapping_nonce {
            return Err(SharedEpochError::EpochNonceRotated {
                expected: ticket.expected_stamp.mapping_nonce,
                actual: header.mapping_nonce,
            });
        }
        if header.sequence != ticket.expected_stamp.sequence {
            return Err(SharedEpochError::EpochAdvanced {
                expected_sequence: ticket.expected_stamp.sequence,
                actual_sequence: header.sequence,
            });
        }
        Ok(ticket.expected_stamp)
    }

    /// Invalidates the currently active prepared (`ready`) slot for `guard`'s lease without
    /// requiring ownership of the `PreparedEpochTicket` value (e.g., on host-driven cancellation
    /// or ready expiry while the ticket value is still held in caller scope).
    ///
    /// Any previously issued `PreparedEpochTicket` for that slot immediately becomes stale and
    /// will be rejected with `StaleOrInactivePreparedTicket` on both `begin_write` and
    /// `cancel_or_expire_ready`. Leaves the shared header untouched at its even `sequence`.
    pub fn cancel_active_ready_slot<G: DesktopInputLockProof>(
        &mut self,
        guard: &G,
        current_thread_id: u32,
    ) -> Result<SharedEpochStamp, SharedEpochError> {
        self.verify_guard(guard, current_thread_id)?;
        let Some(active) = self.active_prepared_slot else {
            return Err(SharedEpochError::StaleOrInactivePreparedTicket);
        };
        if active.lease_id != guard.lease_id() || active.owning_thread_id != current_thread_id {
            return Err(SharedEpochError::StaleOrInactivePreparedTicket);
        }

        self.active_prepared_slot = None;

        let header = self.read_validated_header()?;
        if header.mapping_nonce != active.expected_stamp.mapping_nonce {
            return Err(SharedEpochError::EpochNonceRotated {
                expected: active.expected_stamp.mapping_nonce,
                actual: header.mapping_nonce,
            });
        }
        if header.sequence != active.expected_stamp.sequence {
            return Err(SharedEpochError::EpochAdvanced {
                expected_sequence: active.expected_stamp.sequence,
                actual_sequence: header.sequence,
            });
        }
        Ok(active.expected_stamp)
    }

    /// Preflights and begins a write (`action.commit`) prior to the first window activation or input,
    /// consuming the single active `PreparedEpochTicket`.
    ///
    /// - Verifies that `ticket` matches the model's active prepared slot (`ticket_id`, `lease_id`,
    ///   `owning_thread_id`, `expected_stamp`, and `writer_identity`), then consumes the slot.
    /// - Verifies that BOTH `odd_sequence = sequence + 1` and `reserved_next_even = sequence + 2`
    ///   can be represented without `u64` overflow.
    /// - If `sequence >= u64::MAX - 1` (impending overflow), refuses to publish `odd_sequence`;
    ///   if `overflow_fresh_nonce` is provided, rotates to `overflow_fresh_nonce` at even sequence `2`
    ///   and returns `Err(SharedEpochError::SequenceOverflowRotatedRejectOldStamp { .. })`.
    /// - Otherwise, writes `odd_sequence` and `active_writer` to the header and returns `ActiveWriteHandle`.
    pub fn begin_write<G: DesktopInputLockProof>(
        &mut self,
        guard: &G,
        current_thread_id: u32,
        ticket: PreparedEpochTicket,
        overflow_fresh_nonce: Option<EpochNonce>,
    ) -> Result<ActiveWriteHandle, SharedEpochError> {
        self.verify_guard(guard, current_thread_id)?;
        if guard.acquisition_disposition() == AcquisitionDisposition::Abandoned {
            self.active_prepared_slot = None;
            return Err(SharedEpochError::AbandonedRecoveryRequired);
        }
        self.verify_active_prepared_ticket(guard, current_thread_id, &ticket)?;

        // Consume the single active prepared slot so `ticket` can never be replayed.
        self.active_prepared_slot = None;

        let header = self.read_validated_header()?;
        if header.is_odd() {
            return Err(SharedEpochError::EpochBusyOddSequence {
                sequence: header.sequence,
            });
        }
        if header.mapping_nonce != ticket.expected_stamp.mapping_nonce {
            return Err(SharedEpochError::EpochNonceRotated {
                expected: ticket.expected_stamp.mapping_nonce,
                actual: header.mapping_nonce,
            });
        }
        if header.sequence != ticket.expected_stamp.sequence {
            return Err(SharedEpochError::EpochAdvanced {
                expected_sequence: ticket.expected_stamp.sequence,
                actual_sequence: header.sequence,
            });
        }

        let odd_and_even = header
            .sequence
            .checked_add(1)
            .and_then(|odd| odd.checked_add(1).map(|next_even| (odd, next_even)));

        let Some((odd_sequence, reserved_next_even)) = odd_and_even else {
            let Some(fresh_nonce) = overflow_fresh_nonce else {
                return Err(SharedEpochError::SequenceOverflowRotationRequired {
                    current_sequence: header.sequence,
                });
            };
            if fresh_nonce == header.mapping_nonce {
                return Err(SharedEpochError::NonceReuseDisallowed);
            }
            let rotated_header = SharedEpochHeader {
                schema_version: SHARED_EPOCH_SCHEMA_VERSION,
                struct_bytes: SHARED_EPOCH_HEADER_BYTES as u32,
                mapping_nonce: fresh_nonce,
                sequence: INITIAL_EVEN_SEQUENCE,
                active_writer: None,
            };
            self.write_validated_header(&rotated_header)?;
            return Err(SharedEpochError::SequenceOverflowRotatedRejectOldStamp {
                previous_nonce: header.mapping_nonce,
                previous_sequence: header.sequence,
                new_stamp: SharedEpochStamp {
                    mapping_nonce: fresh_nonce,
                    sequence: INITIAL_EVEN_SEQUENCE,
                },
            });
        };

        let odd_header = SharedEpochHeader {
            schema_version: SHARED_EPOCH_SCHEMA_VERSION,
            struct_bytes: SHARED_EPOCH_HEADER_BYTES as u32,
            mapping_nonce: header.mapping_nonce,
            sequence: odd_sequence,
            active_writer: Some(ticket.writer_identity),
        };
        self.write_validated_header(&odd_header)?;

        Ok(ActiveWriteHandle {
            ticket_id: ticket.ticket_id,
            lease_id: ticket.lease_id,
            mapping_nonce: header.mapping_nonce,
            odd_sequence,
            reserved_next_even,
            writer_identity: ticket.writer_identity,
            finished: false,
        })
    }

    /// Completes an active write on ALL terminal paths (`Succeeded`, `FailedAfterActivation`,
    /// `CancelledAfterActivation`) after the caller's own release-only input cleanup has finished.
    ///
    /// Strictly verifies that `handle` matches the current active odd header and lease; a stale or
    /// foreign handle is rejected with `ForeignOrStaleWriteHandle` and cannot clear a newer writer.
    pub fn finish_write<G: DesktopInputLockProof>(
        &mut self,
        guard: &G,
        current_thread_id: u32,
        handle: &mut ActiveWriteHandle,
        _cleanup_proof: ReleaseOnlyCleanupProof,
        terminal_kind: WriteTerminalKind,
    ) -> Result<FinishWriteOutcome, SharedEpochError> {
        self.verify_guard(guard, current_thread_id)?;
        if handle.finished || guard.lease_id() != handle.lease_id {
            return Err(SharedEpochError::ForeignOrStaleWriteHandle);
        }

        let header = self.read_validated_header()?;
        if header.mapping_nonce != handle.mapping_nonce
            || header.sequence != handle.odd_sequence
            || header.active_writer != Some(handle.writer_identity)
        {
            return Err(SharedEpochError::ForeignOrStaleWriteHandle);
        }

        let even_header = SharedEpochHeader {
            schema_version: SHARED_EPOCH_SCHEMA_VERSION,
            struct_bytes: SHARED_EPOCH_HEADER_BYTES as u32,
            mapping_nonce: handle.mapping_nonce,
            sequence: handle.reserved_next_even,
            active_writer: None,
        };
        self.write_validated_header(&even_header)?;
        handle.finished = true;

        let mutation_accounting = match terminal_kind {
            WriteTerminalKind::Succeeded => MutationAccounting::Confirmed,
            WriteTerminalKind::FailedAfterActivation
            | WriteTerminalKind::CancelledAfterActivation => MutationAccounting::UnknownOrPartial,
        };

        Ok(FinishWriteOutcome {
            new_stamp: SharedEpochStamp {
                mapping_nonce: handle.mapping_nonce,
                sequence: handle.reserved_next_even,
            },
            terminal_kind,
            mutation_accounting,
        })
    }

    fn verify_active_prepared_ticket<G: DesktopInputLockProof>(
        &self,
        guard: &G,
        current_thread_id: u32,
        ticket: &PreparedEpochTicket,
    ) -> Result<(), SharedEpochError> {
        let Some(active) = self.active_prepared_slot else {
            return Err(SharedEpochError::StaleOrInactivePreparedTicket);
        };
        if guard.lease_id() != ticket.lease_id
            || ticket.owning_thread_id != current_thread_id
            || active.ticket_id != ticket.ticket_id
            || active.lease_id != ticket.lease_id
            || active.owning_thread_id != ticket.owning_thread_id
            || active.expected_stamp != ticket.expected_stamp
            || active.writer_identity != ticket.writer_identity
        {
            return Err(SharedEpochError::StaleOrInactivePreparedTicket);
        }
        Ok(())
    }

    fn verify_guard<G: DesktopInputLockProof>(
        &self,
        guard: &G,
        current_thread_id: u32,
    ) -> Result<(), SharedEpochError> {
        if self.quarantined {
            return Err(SharedEpochError::ModelQuarantined);
        }
        Self::verify_guard_static(&self.namespace, guard, current_thread_id)
    }

    fn verify_guard_static<G: DesktopInputLockProof>(
        namespace: &DesktopLockNamespace,
        guard: &G,
        current_thread_id: u32,
    ) -> Result<(), SharedEpochError> {
        if !guard.is_held() {
            return Err(SharedEpochError::LeaseNotHeld);
        }
        if guard.namespace() != namespace {
            return Err(SharedEpochError::LockIdentityMismatch);
        }
        if guard.owning_thread_id() != current_thread_id {
            return Err(SharedEpochError::WrongOwningThread {
                expected_thread_id: guard.owning_thread_id(),
                actual_thread_id: current_thread_id,
            });
        }
        Ok(())
    }

    fn read_validated_header(&mut self) -> Result<SharedEpochHeader, SharedEpochError> {
        match SharedEpochHeader::decode_and_validate(self.page.as_bytes()) {
            Ok(h) => Ok(h),
            Err(err) => {
                self.quarantined = true;
                Err(err)
            }
        }
    }

    fn write_validated_header(&mut self, header: &SharedEpochHeader) -> Result<(), SharedEpochError> {
        match header.encode() {
            Ok(encoded) => {
                self.page.as_bytes_mut()[0..SHARED_EPOCH_HEADER_BYTES].copy_from_slice(&encoded);
                Ok(())
            }
            Err(err) => {
                self.quarantined = true;
                Err(err)
            }
        }
    }
}
