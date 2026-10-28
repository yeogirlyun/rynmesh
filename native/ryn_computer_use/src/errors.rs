use serde::{Deserialize, Serialize};
use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCode {
    InvalidRequest,
    UnsupportedProtocolVersion,
    NotInitialized,
    MethodNotFound,
    InvalidParams,
    TaskNotFound,
    TaskConflict,
    WindowNotFound,
    WindowStale,
    WindowMinimized,
    WindowOccluded,
    WindowCaptureProtected,
    ObservationUnavailable,
    UiaUnavailable,
    UiaElementStale,
    StateTokenInvalid,
    StateTokenStale,
    PrepareTicketInvalid,
    PrepareTicketExpired,
    DuplicateOperationId,
    FocusFailed,
    InputLockBusy,
    InputLockQuarantined,
    InputBlockedByUipi,
    InputFailed,
    Cancelled,
    Timeout,
    Unsupported,
    InternalError,
}

impl ErrorCode {
    pub const fn as_str(&self) -> &'static str {
        match self {
            Self::InvalidRequest => "invalid_request",
            Self::UnsupportedProtocolVersion => "unsupported_protocol_version",
            Self::NotInitialized => "not_initialized",
            Self::MethodNotFound => "method_not_found",
            Self::InvalidParams => "invalid_params",
            Self::TaskNotFound => "task_not_found",
            Self::TaskConflict => "task_conflict",
            Self::WindowNotFound => "window_not_found",
            Self::WindowStale => "window_stale",
            Self::WindowMinimized => "window_minimized",
            Self::WindowOccluded => "window_occluded",
            Self::WindowCaptureProtected => "window_capture_protected",
            Self::ObservationUnavailable => "observation_unavailable",
            Self::UiaUnavailable => "uia_unavailable",
            Self::UiaElementStale => "uia_element_stale",
            Self::StateTokenInvalid => "state_token_invalid",
            Self::StateTokenStale => "state_token_stale",
            Self::PrepareTicketInvalid => "prepare_ticket_invalid",
            Self::PrepareTicketExpired => "prepare_ticket_expired",
            Self::DuplicateOperationId => "duplicate_operation_id",
            Self::FocusFailed => "focus_failed",
            Self::InputLockBusy => "input_lock_busy",
            Self::InputLockQuarantined => "input_lock_quarantined",
            Self::InputBlockedByUipi => "input_blocked_by_uipi",
            Self::InputFailed => "input_failed",
            Self::Cancelled => "cancelled",
            Self::Timeout => "timeout",
            Self::Unsupported => "unsupported",
            Self::InternalError => "internal_error",
        }
    }

    /// Fixed authored message template. Never interpolates caller strings, paths, titles, or OS messages.
    pub const fn static_message(&self) -> &'static str {
        match self {
            Self::InvalidRequest => "Request frame is malformed or violates wire-v1 envelope rules.",
            Self::UnsupportedProtocolVersion => "Unsupported wire protocol version; expected v=1.",
            Self::NotInitialized => "Worker session has not been initialized.",
            Self::MethodNotFound => "Requested method is not recognized by wire-v1.",
            Self::InvalidParams => "Request parameters failed schema or safety validation.",
            Self::TaskNotFound => "Referenced taskSessionId does not exist or has been retired.",
            Self::TaskConflict => "Referenced resource belongs to a different taskSessionId.",
            Self::WindowNotFound => "Target window handle no longer exists.",
            Self::WindowStale => "Target WindowRef identity, PID, process creation time, or generation is stale.",
            Self::WindowMinimized => "Target window is minimized and cannot be operated without restore.",
            Self::WindowOccluded => "Target point or window is occluded by an unrelated window.",
            Self::WindowCaptureProtected => "Target window is protected by display affinity and cannot be captured.",
            Self::ObservationUnavailable => "Window state observation could not be completed.",
            Self::UiaUnavailable => "Native COM UIA service is unavailable for the target window.",
            Self::UiaElementStale => "Target UIA element runtimeId is stale or not present in the window.",
            Self::StateTokenInvalid => "Provided stateToken is unknown or malformed.",
            Self::StateTokenStale => "Provided stateToken is stale due to window geometry, DPI, or epoch change.",
            Self::PrepareTicketInvalid => "Provided prepareTicket is unknown, already consumed, or mismatched.",
            Self::PrepareTicketExpired => "Provided prepareTicket expired before commit.",
            Self::DuplicateOperationId => "Operation ID has already been registered with conflicting parameters.",
            Self::FocusFailed => "Target window could not be brought to foreground and verified within deadline.",
            Self::InputLockBusy => "Global physical desktop input mutex could not be acquired within timeout.",
            Self::InputLockQuarantined => "Physical desktop input mutex is quarantined after a release failure.",
            Self::InputBlockedByUipi => "Physical input was blocked by User Interface Privilege Isolation (UIPI).",
            Self::InputFailed => "Physical or UIA input delivery failed.",
            Self::Cancelled => "Operation was cancelled before completion.",
            Self::Timeout => "Operation exceeded its execution deadline.",
            Self::Unsupported => "Requested action or capability is not supported in this worker build.",
            Self::InternalError => "Internal native worker error.",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorStage {
    Protocol,
    Validate,
    Observe,
    WaitingLock,
    Prepare,
    Ready,
    Activating,
    Validating,
    InputStarted,
    Commit,
    PostObserve,
    Cancel,
    Session,
}

/// Wire-level mutation state per frozen design: `none | possible | delivered`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MutationState {
    None,
    Possible,
    Delivered,
}

impl MutationState {
    pub const fn max_monotonic(self, other: Self) -> Self {
        match (self, other) {
            (Self::Delivered, _) | (_, Self::Delivered) => Self::Delivered,
            (Self::Possible, _) | (_, Self::Possible) => Self::Possible,
            _ => Self::None,
        }
    }
}

/// Finer-grained internal diagnostic classification mapped to `MutationState`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InternalMutationDetail {
    None,
    FocusOnly,
    UnknownOrPartial,
    Committed,
}

impl InternalMutationDetail {
    pub const fn to_wire_mutation(self) -> MutationState {
        match self {
            Self::None => MutationState::None,
            Self::FocusOnly | Self::UnknownOrPartial => MutationState::Possible,
            Self::Committed => MutationState::Delivered,
        }
    }

    pub const fn max_monotonic(self, other: Self) -> Self {
        match (self, other) {
            (Self::Committed, _) | (_, Self::Committed) => Self::Committed,
            (Self::UnknownOrPartial, _) | (_, Self::UnknownOrPartial) => Self::UnknownOrPartial,
            (Self::FocusOnly, _) | (_, Self::FocusOnly) => Self::FocusOnly,
            _ => Self::None,
        }
    }
}

/// Strictly enumerated diagnostic reason codes so arbitrary strings can never enter error payloads.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DiagnosticReason {
    FrameTooLarge,
    MalformedJson,
    DuplicateJsonKey,
    MissingVersion,
    VersionMismatch,
    MissingRequestId,
    MissingMethod,
    AlreadyInitialized,
    SafetyConstraintDisabled,
    WorkerInstanceMismatch,
    HwndParseFailed,
    IsWindowFalse,
    PidMismatch,
    ProcessCreationTimeMismatch,
    GenerationMismatch,
    GeometryChanged,
    DpiChanged,
    EpochAdvanced,
    TokenTaskMismatch,
    TokenWindowMismatch,
    PointOutOfBounds,
    HitTestUnrelatedWindow,
    ForegroundVerificationFailed,
    MinimizedRestoreDisallowed,
    MutexWaitTimeout,
    MutexAbandoned,
    MutexReleaseFailed,
    DesktopIdentityLookupFailed,
    SendInputShortCount,
    UiaPatternMissing,
    PasswordRedacted,
    DirectLaunchDisallowed,
    CancelledBeforeLock,
    CancelledWhileReady,
    CancelledDuringExecution,
    ParentStdinClosed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ErrorDetails {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub win32_error: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hresult_hex: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub target_hwnd: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub foreground_hwnd: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub internal_mutation: Option<InternalMutationDetail>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<DiagnosticReason>,
}

impl ErrorDetails {
    pub fn empty() -> Self {
        Self {
            win32_error: None,
            hresult_hex: None,
            target_hwnd: None,
            foreground_hwnd: None,
            internal_mutation: None,
            reason: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ProtocolError {
    pub code: ErrorCode,
    pub stage: ErrorStage,
    pub mutation: MutationState,
    pub request_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub operation_id: Option<String>,
    pub retryable: bool,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub details: Option<ErrorDetails>,
}

impl ProtocolError {
    pub fn new(
        code: ErrorCode,
        stage: ErrorStage,
        internal_mutation: InternalMutationDetail,
    ) -> Self {
        let mut details = ErrorDetails::empty();
        details.internal_mutation = Some(internal_mutation);
        Self {
            code,
            stage,
            mutation: internal_mutation.to_wire_mutation(),
            request_id: String::new(),
            operation_id: None,
            retryable: false,
            message: code.static_message().to_string(),
            details: Some(details),
        }
    }

    pub fn bind_context(
        mut self,
        request_id: impl Into<String>,
        operation_id: Option<String>,
    ) -> Self {
        self.request_id = request_id.into();
        if operation_id.is_some() {
            self.operation_id = operation_id;
        }
        self
    }

    pub fn with_operation_id(mut self, operation_id: impl Into<String>) -> Self {
        self.operation_id = Some(operation_id.into());
        self
    }

    pub fn with_reason(mut self, reason: DiagnosticReason) -> Self {
        let d = self.details.get_or_insert_with(ErrorDetails::empty);
        d.reason = Some(reason);
        self
    }

    pub fn with_win32_error(mut self, win32_error: u32) -> Self {
        let d = self.details.get_or_insert_with(ErrorDetails::empty);
        d.win32_error = Some(win32_error);
        self
    }

    pub fn with_hresult(mut self, hr: i32) -> Self {
        let d = self.details.get_or_insert_with(ErrorDetails::empty);
        d.hresult_hex = Some(format!("0x{:08X}", hr as u32));
        self
    }

    pub fn with_hwnds(mut self, target_hwnd: Option<u64>, foreground_hwnd: Option<u64>) -> Self {
        let d = self.details.get_or_insert_with(ErrorDetails::empty);
        d.target_hwnd = target_hwnd.map(|h| h.to_string());
        d.foreground_hwnd = foreground_hwnd.map(|h| h.to_string());
        self
    }

    /// Monotonically merges `min_detail` into this error so post-activation or post-input
    /// failures never regress to `mutation: none`.
    pub fn with_monotonic_mutation(mut self, min_detail: InternalMutationDetail) -> Self {
        let d = self.details.get_or_insert_with(ErrorDetails::empty);
        let merged = d
            .internal_mutation
            .unwrap_or(InternalMutationDetail::None)
            .max_monotonic(min_detail);
        d.internal_mutation = Some(merged);
        self.mutation = self.mutation.max_monotonic(merged.to_wire_mutation());
        self
    }

    pub fn protocol(code: ErrorCode, reason: DiagnosticReason) -> Self {
        Self::new(code, ErrorStage::Protocol, InternalMutationDetail::None).with_reason(reason)
    }

    pub fn validate(code: ErrorCode, reason: DiagnosticReason) -> Self {
        Self::new(code, ErrorStage::Validate, InternalMutationDetail::None).with_reason(reason)
    }

    pub fn observe(code: ErrorCode, reason: DiagnosticReason) -> Self {
        Self::new(code, ErrorStage::Observe, InternalMutationDetail::None).with_reason(reason)
    }

    pub fn waiting_lock(code: ErrorCode, reason: DiagnosticReason) -> Self {
        Self::new(code, ErrorStage::WaitingLock, InternalMutationDetail::None).with_reason(reason)
    }

    pub fn prepare(code: ErrorCode, reason: DiagnosticReason) -> Self {
        Self::new(code, ErrorStage::Prepare, InternalMutationDetail::None).with_reason(reason)
    }

    pub fn focus(
        code: ErrorCode,
        internal_mutation: InternalMutationDetail,
        reason: DiagnosticReason,
    ) -> Self {
        Self::new(code, ErrorStage::Activating, internal_mutation).with_reason(reason)
    }

    pub fn commit(
        code: ErrorCode,
        stage: ErrorStage,
        internal_mutation: InternalMutationDetail,
        reason: DiagnosticReason,
    ) -> Self {
        Self::new(code, stage, internal_mutation).with_reason(reason)
    }

    pub fn cancel(
        code: ErrorCode,
        actual_stage: ErrorStage,
        internal_mutation: InternalMutationDetail,
        reason: DiagnosticReason,
    ) -> Self {
        Self::new(code, actual_stage, internal_mutation).with_reason(reason)
    }

    pub fn session(code: ErrorCode, reason: DiagnosticReason) -> Self {
        Self::new(code, ErrorStage::Session, InternalMutationDetail::None).with_reason(reason)
    }
}

impl fmt::Display for ProtocolError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "[code={} stage={:?} mutation={:?}] {}",
            self.code.as_str(),
            self.stage,
            self.mutation,
            self.message
        )
    }
}

impl std::error::Error for ProtocolError {}
