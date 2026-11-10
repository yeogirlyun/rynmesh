use crate::errors::{DiagnosticReason, ErrorCode, ErrorStage, InternalMutationDetail, ProtocolError};
use sha2::{Digest, Sha256};
use std::ffi::c_void;
use std::marker::PhantomData;
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use windows::core::{PCWSTR, PWSTR};
use windows::Win32::Foundation::{
    CloseHandle, GetLastError, LocalFree, HANDLE, HLOCAL, WAIT_ABANDONED, WAIT_OBJECT_0,
    WAIT_TIMEOUT,
};
use windows::Win32::Security::Authorization::{
    ConvertSidToStringSidW, ConvertStringSecurityDescriptorToSecurityDescriptorW,
    SDDL_REVISION_1,
};
use windows::Win32::Security::{
    GetTokenInformation, TokenSessionId, TokenUser, PSECURITY_DESCRIPTOR, SECURITY_ATTRIBUTES,
    TOKEN_QUERY, TOKEN_USER,
};
use windows::Win32::System::StationsAndDesktops::{
    GetThreadDesktop, GetUserObjectInformationW, UOI_NAME,
};
use windows::Win32::System::Threading::{
    CreateMutexW, GetCurrentProcess, GetCurrentThreadId, OpenProcessToken, ReleaseMutex,
    WaitForSingleObject,
};

pub const DEFAULT_LOCK_WAIT_TIMEOUT_MS: u64 = 1500;
pub const MAX_LOCK_WAIT_TIMEOUT_MS: u64 = 10_000;
pub const DEFAULT_READY_HOLD_TIMEOUT_MS: u64 = 1500;
pub const MAX_READY_HOLD_TIMEOUT_MS: u64 = 2000;
pub const DEFAULT_READY_TICKET_TTL_MS: u64 = DEFAULT_READY_HOLD_TIMEOUT_MS;
pub const MAX_READY_TICKET_TTL_MS: u64 = MAX_READY_HOLD_TIMEOUT_MS;
const LOCK_POLL_SLICE_MS: u32 = 25;

pub type DesktopInputLock = DesktopInputLockManager;

/// Represents the current user SID, session ID, and desktop name used to scope
/// the cross-process physical desktop input mutex.
/// Fails closed if any lookup fails; never guesses `"Default"`.
#[derive(Debug, Clone)]
pub struct DesktopLockIdentity {
    pub user_sid: String,
    pub session_id: u32,
    pub desktop_name: String,
    pub mutex_name: String,
}

impl DesktopLockIdentity {
    pub fn resolve_current() -> Result<Self, ProtocolError> {
        let (user_sid, session_id) = query_current_token_sid_and_session()?;
        let desktop_name = query_current_desktop_name()?;

        let mut hasher = Sha256::new();
        hasher.update(user_sid.as_bytes());
        hasher.update(b":");
        hasher.update(session_id.to_string().as_bytes());
        hasher.update(b":");
        hasher.update(desktop_name.to_lowercase().as_bytes());
        let desktop_hash = hex::encode(&hasher.finalize()[..16]);

        let mutex_name = format!(
            "Local\\RynComputerUseInputLock_v1_{}_s{}_{}",
            sanitize_sid_for_object_name(&user_sid),
            session_id,
            desktop_hash
        );

        Ok(Self {
            user_sid,
            session_id,
            desktop_name,
            mutex_name,
        })
    }
}

fn sanitize_sid_for_object_name(sid: &str) -> String {
    sid.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '_'
            }
        })
        .collect()
}

fn query_current_token_sid_and_session() -> Result<(String, u32), ProtocolError> {
    unsafe {
        let mut token = HANDLE::default();
        OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token).map_err(|_| {
            ProtocolError::waiting_lock(
                ErrorCode::InternalError,
                DiagnosticReason::DesktopIdentityLookupFailed,
            )
            .with_win32_error(GetLastError().0)
        })?;
        let _token_guard = HandleCloseGuard(token);

        let mut needed = 0u32;
        let _ = GetTokenInformation(token, TokenUser, None, 0, &mut needed);
        if needed == 0 {
            return Err(ProtocolError::waiting_lock(
                ErrorCode::InternalError,
                DiagnosticReason::DesktopIdentityLookupFailed,
            ));
        }

        let mut buffer = vec![0u8; needed as usize];
        GetTokenInformation(
            token,
            TokenUser,
            Some(buffer.as_mut_ptr() as *mut c_void),
            needed,
            &mut needed,
        )
        .map_err(|_| {
            ProtocolError::waiting_lock(
                ErrorCode::InternalError,
                DiagnosticReason::DesktopIdentityLookupFailed,
            )
            .with_win32_error(GetLastError().0)
        })?;

        let token_user = &*(buffer.as_ptr() as *const TOKEN_USER);
        let mut sid_pwstr = PWSTR::null();
        ConvertSidToStringSidW(token_user.User.Sid, &mut sid_pwstr).map_err(|_| {
            ProtocolError::waiting_lock(
                ErrorCode::InternalError,
                DiagnosticReason::DesktopIdentityLookupFailed,
            )
            .with_win32_error(GetLastError().0)
        })?;
        let sid_string = pwstr_to_string(sid_pwstr);
        let _ = LocalFree(Some(HLOCAL(sid_pwstr.0 as *mut c_void)));
        if sid_string.is_empty() {
            return Err(ProtocolError::waiting_lock(
                ErrorCode::InternalError,
                DiagnosticReason::DesktopIdentityLookupFailed,
            ));
        }

        let mut session_id = 0u32;
        let mut session_len = 0u32;
        GetTokenInformation(
            token,
            TokenSessionId,
            Some(&mut session_id as *mut u32 as *mut c_void),
            std::mem::size_of::<u32>() as u32,
            &mut session_len,
        )
        .map_err(|_| {
            ProtocolError::waiting_lock(
                ErrorCode::InternalError,
                DiagnosticReason::DesktopIdentityLookupFailed,
            )
            .with_win32_error(GetLastError().0)
        })?;

        Ok((sid_string, session_id))
    }
}

fn query_current_desktop_name() -> Result<String, ProtocolError> {
    unsafe {
        let hdesk = GetThreadDesktop(GetCurrentThreadId()).map_err(|_| {
            ProtocolError::waiting_lock(
                ErrorCode::InternalError,
                DiagnosticReason::DesktopIdentityLookupFailed,
            )
            .with_win32_error(GetLastError().0)
        })?;

        let mut needed = 0u32;
        let _ = GetUserObjectInformationW(
            HANDLE(hdesk.0),
            UOI_NAME,
            None,
            0,
            Some(&mut needed),
        );
        if needed == 0 {
            return Err(ProtocolError::waiting_lock(
                ErrorCode::InternalError,
                DiagnosticReason::DesktopIdentityLookupFailed,
            )
            .with_win32_error(GetLastError().0));
        }

        let mut buf = vec![0u16; (needed as usize / 2) + 2];
        GetUserObjectInformationW(
            HANDLE(hdesk.0),
            UOI_NAME,
            Some(buf.as_mut_ptr() as *mut c_void),
            needed,
            Some(&mut needed),
        )
        .map_err(|_| {
            ProtocolError::waiting_lock(
                ErrorCode::InternalError,
                DiagnosticReason::DesktopIdentityLookupFailed,
            )
            .with_win32_error(GetLastError().0)
        })?;

        let len = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
        let name = String::from_utf16_lossy(&buf[..len]).trim().to_string();
        if name.is_empty() {
            return Err(ProtocolError::waiting_lock(
                ErrorCode::InternalError,
                DiagnosticReason::DesktopIdentityLookupFailed,
            ));
        }
        Ok(name)
    }
}

unsafe fn pwstr_to_string(ptr: PWSTR) -> String {
    if ptr.is_null() {
        return String::new();
    }
    let mut len = 0usize;
    while *ptr.0.add(len) != 0 {
        len += 1;
    }
    let slice = std::slice::from_raw_parts(ptr.0, len);
    String::from_utf16_lossy(slice)
}

struct HandleCloseGuard(HANDLE);

impl Drop for HandleCloseGuard {
    fn drop(&mut self) {
        if !self.0.is_invalid() {
            unsafe {
                let _ = CloseHandle(self.0);
            }
        }
    }
}

/// Manages acquisition and release of the cross-process named mutex and in-process gate.
/// Enforces single-operation ownership and quarantines on any Win32 `ReleaseMutex` failure.
#[derive(Clone)]
pub struct DesktopInputLockManager {
    identity: DesktopLockIdentity,
    in_process_owner: Arc<Mutex<Option<String>>>,
    quarantined: Arc<AtomicBool>,
}

impl DesktopInputLockManager {
    pub fn new() -> Result<Self, ProtocolError> {
        let identity = DesktopLockIdentity::resolve_current()?;
        Ok(Self {
            identity,
            in_process_owner: Arc::new(Mutex::new(None)),
            quarantined: Arc::new(AtomicBool::new(false)),
        })
    }

    pub fn identity(&self) -> &DesktopLockIdentity {
        &self.identity
    }

    pub fn mutex_name(&self) -> &str {
        &self.identity.mutex_name
    }

    pub fn is_quarantined(&self) -> bool {
        self.quarantined.load(Ordering::SeqCst)
    }

    pub fn force_quarantine(&self) {
        self.quarantined.store(true, Ordering::SeqCst);
    }

    /// Acquires both the in-process owner slot and the OS named mutex on the current physical actor thread.
    /// Uses bounded polling with `cancel_flag` and clamped deadline so the control/reader thread can cancel
    /// a blocked acquisition immediately.
    pub fn acquire_for_current_thread(
        &self,
        owner_operation_id: impl Into<String>,
        timeout_ms: u64,
        cancel_flag: &AtomicBool,
    ) -> Result<DesktopInputLockGuard, ProtocolError> {
        let owner_operation_id = owner_operation_id.into();
        if self.is_quarantined() {
            return Err(ProtocolError::waiting_lock(
                ErrorCode::InputLockQuarantined,
                DiagnosticReason::MutexReleaseFailed,
            ));
        }

        let bounded_timeout_ms = timeout_ms.clamp(1, MAX_LOCK_WAIT_TIMEOUT_MS);
        let deadline = Instant::now() + Duration::from_millis(bounded_timeout_ms);

        // Step 1: Acquire in-process slot (prevents same-thread recursive Win32 mutex acquisition)
        loop {
            if cancel_flag.load(Ordering::SeqCst) {
                return Err(ProtocolError::waiting_lock(
                    ErrorCode::Cancelled,
                    DiagnosticReason::CancelledBeforeLock,
                ));
            }
            if self.is_quarantined() {
                return Err(ProtocolError::waiting_lock(
                    ErrorCode::InputLockQuarantined,
                    DiagnosticReason::MutexReleaseFailed,
                ));
            }
            {
                let mut slot = self.in_process_owner.lock().unwrap();
                if slot.is_none() {
                    *slot = Some(owner_operation_id.clone());
                    break;
                }
            }
            if Instant::now() >= deadline {
                return Err(ProtocolError::waiting_lock(
                    ErrorCode::InputLockBusy,
                    DiagnosticReason::MutexWaitTimeout,
                ));
            }
            std::thread::sleep(Duration::from_millis(10));
        }

        // Step 2: Acquire OS named mutex with bounded polling slices and cancellation checks
        match acquire_os_named_mutex_polled(&self.identity, deadline, cancel_flag) {
            Ok((handle, was_abandoned)) => Ok(DesktopInputLockGuard {
                handle: Some(handle),
                owner_operation_id,
                in_process_owner: Arc::clone(&self.in_process_owner),
                quarantined: Arc::clone(&self.quarantined),
                owning_thread_id: unsafe { GetCurrentThreadId() },
                was_abandoned,
                _not_send_sync: PhantomData,
            }),
            Err(err) => {
                let mut slot = self.in_process_owner.lock().unwrap();
                if slot.as_deref() == Some(owner_operation_id.as_str()) {
                    *slot = None;
                }
                Err(err)
            }
        }
    }
}

fn acquire_os_named_mutex_polled(
    identity: &DesktopLockIdentity,
    deadline: Instant,
    cancel_flag: &AtomicBool,
) -> Result<(HANDLE, bool), ProtocolError> {
    unsafe {
        let sddl = format!("D:P(A;;GA;;;{})(A;;GA;;;SY)", identity.user_sid);
        let sddl_utf16: Vec<u16> = sddl.encode_utf16().chain(std::iter::once(0)).collect();
        let mut sd = PSECURITY_DESCRIPTOR::default();

        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            PCWSTR(sddl_utf16.as_ptr()),
            SDDL_REVISION_1,
            &mut sd,
            None,
        )
        .map_err(|_| {
            ProtocolError::waiting_lock(
                ErrorCode::InternalError,
                DiagnosticReason::DesktopIdentityLookupFailed,
            )
            .with_win32_error(GetLastError().0)
        })?;

        let sa = SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: sd.0,
            bInheritHandle: false.into(),
        };

        let name_utf16: Vec<u16> = identity
            .mutex_name
            .encode_utf16()
            .chain(std::iter::once(0))
            .collect();

        let mutex_res = CreateMutexW(Some(&sa), false, PCWSTR(name_utf16.as_ptr()));
        let _ = LocalFree(Some(HLOCAL(sd.0)));

        let handle = mutex_res.map_err(|_| {
            ProtocolError::waiting_lock(
                ErrorCode::InternalError,
                DiagnosticReason::DesktopIdentityLookupFailed,
            )
            .with_win32_error(GetLastError().0)
        })?;

        loop {
            if cancel_flag.load(Ordering::SeqCst) {
                let _ = CloseHandle(handle);
                return Err(ProtocolError::waiting_lock(
                    ErrorCode::Cancelled,
                    DiagnosticReason::CancelledBeforeLock,
                ));
            }

            let now = Instant::now();
            let remaining_ms = match deadline.checked_duration_since(now) {
                Some(rem) => (rem.as_millis() as u64).min(LOCK_POLL_SLICE_MS as u64) as u32,
                None => 0,
            };

            let wait_res = WaitForSingleObject(handle, remaining_ms);
            if wait_res == WAIT_OBJECT_0 {
                if cancel_flag.load(Ordering::SeqCst) {
                    let _ = ReleaseMutex(handle);
                    let _ = CloseHandle(handle);
                    return Err(ProtocolError::waiting_lock(
                        ErrorCode::Cancelled,
                        DiagnosticReason::CancelledBeforeLock,
                    ));
                }
                return Ok((handle, false));
            } else if wait_res == WAIT_ABANDONED {
                if cancel_flag.load(Ordering::SeqCst) {
                    let _ = ReleaseMutex(handle);
                    let _ = CloseHandle(handle);
                    return Err(ProtocolError::waiting_lock(
                        ErrorCode::Cancelled,
                        DiagnosticReason::CancelledBeforeLock,
                    ));
                }
                return Ok((handle, true));
            } else if wait_res == WAIT_TIMEOUT {
                if Instant::now() >= deadline {
                    let _ = CloseHandle(handle);
                    return Err(ProtocolError::waiting_lock(
                        ErrorCode::InputLockBusy,
                        DiagnosticReason::MutexWaitTimeout,
                    ));
                }
            } else {
                let win32_err = GetLastError().0;
                let _ = CloseHandle(handle);
                return Err(ProtocolError::waiting_lock(
                    ErrorCode::InternalError,
                    DiagnosticReason::MutexWaitTimeout,
                )
                .with_win32_error(win32_err));
            }
        }
    }
}

/// Thread-affine RAII guard for the physical desktop input mutex.
/// Strictly `!Send` and `!Sync` via `PhantomData<Rc<()>>` because Win32 mutex ownership
/// belongs to the acquiring OS thread.
pub struct DesktopInputLockGuard {
    handle: Option<HANDLE>,
    owner_operation_id: String,
    in_process_owner: Arc<Mutex<Option<String>>>,
    quarantined: Arc<AtomicBool>,
    owning_thread_id: u32,
    was_abandoned: bool,
    _not_send_sync: PhantomData<Rc<()>>,
}

impl DesktopInputLockGuard {
    pub fn owner_operation_id(&self) -> &str {
        &self.owner_operation_id
    }

    pub fn owning_thread_id(&self) -> u32 {
        self.owning_thread_id
    }

    pub fn was_abandoned(&self) -> bool {
        self.was_abandoned
    }

    /// Explicitly releases the Win32 mutex on the owning thread and verifies `ReleaseMutex` succeeds.
    /// If called from the wrong thread or if `ReleaseMutex` fails, quarantines the lock manager
    /// and does NOT clear `in_process_owner`.
    pub fn release(&mut self) -> Result<(), ProtocolError> {
        let Some(handle) = self.handle.take() else {
            return Ok(());
        };

        let current_tid = unsafe { GetCurrentThreadId() };
        if current_tid != self.owning_thread_id {
            self.quarantined.store(true, Ordering::SeqCst);
            let _ = unsafe { CloseHandle(handle) };
            return Err(ProtocolError::commit(
                ErrorCode::InputLockQuarantined,
                ErrorStage::Commit,
                InternalMutationDetail::UnknownOrPartial,
                DiagnosticReason::MutexReleaseFailed,
            ));
        }

        let release_res = unsafe { ReleaseMutex(handle) };
        let win32_err = unsafe { GetLastError().0 };
        let _ = unsafe { CloseHandle(handle) };

        if release_res.is_err() {
            self.quarantined.store(true, Ordering::SeqCst);
            return Err(ProtocolError::commit(
                ErrorCode::InputLockQuarantined,
                ErrorStage::Commit,
                InternalMutationDetail::UnknownOrPartial,
                DiagnosticReason::MutexReleaseFailed,
            )
            .with_win32_error(win32_err));
        }

        let mut slot = self.in_process_owner.lock().unwrap();
        if slot.as_deref() == Some(self.owner_operation_id.as_str()) {
            *slot = None;
        }
        Ok(())
    }
}

impl std::fmt::Debug for DesktopInputLockGuard {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DesktopInputLockGuard")
            .field("owning_thread_id", &self.owning_thread_id)
            .field("was_abandoned", &self.was_abandoned)
            .field("held", &self.handle.is_some())
            .finish()
    }
}

impl Drop for DesktopInputLockGuard {
    fn drop(&mut self) {
        let _ = self.release();
    }
}
