use crate::errors::{DiagnosticReason, ErrorCode, ErrorStage, ProtocolError};
use crate::lifecycle::{
    map_lifecycle_error_to_protocol, parse_window_ref_os_tuple, LifecycleService,
};
use crate::lifecycle_model::{
    HostWorkerNonce, IssueOutcome, LifecycleStamp, ObservationCheckpoint, WindowOsTuple,
};
use crate::protocol::{
    AppEntry, AppsLaunchParams, AppsListParams, AppsListResult, ForegroundStatus, RectPx,
    WindowEntry, WindowGeometry, WindowRef, WindowsGetParams, WindowsGetResult, WindowsListParams,
    WindowsListResult, MAX_WINDOWS_CAP, MAX_WINDOWS_DEFAULT,
};
use std::collections::HashMap;
use std::ffi::c_void;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};
use windows::core::{BOOL, PWSTR};
use windows::Win32::Foundation::{CloseHandle, FILETIME, HWND, LPARAM, POINT, RECT};
use windows::Win32::Graphics::Dwm::{DwmGetWindowAttribute, DWMWA_CLOAKED};
use windows::Win32::Graphics::Gdi::ClientToScreen;
use windows::Win32::System::Threading::{
    GetCurrentProcessId, GetProcessTimes, OpenProcess, QueryFullProcessImageNameW,
    PROCESS_NAME_WIN32, PROCESS_QUERY_LIMITED_INFORMATION,
};
use windows::Win32::UI::HiDpi::{
    GetDpiForWindow, SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE,
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, DPI_AWARENESS_CONTEXT_SYSTEM_AWARE,
};
use windows::Win32::UI::Input::KeyboardAndMouse::IsWindowEnabled;
use windows::Win32::UI::WindowsAndMessaging::{
    EnumWindows, GetAncestor, GetClassNameW, GetClientRect, GetForegroundWindow, GetWindow,
    GetWindowDisplayAffinity, GetWindowRect, GetWindowTextW, GetWindowThreadProcessId, IsIconic,
    IsWindow, IsWindowVisible, IsZoomed, SetProcessDPIAware, GA_ROOT, GW_OWNER, WDA_NONE,
};

/// Configures the current worker process for Per-Monitor V2 High-DPI awareness
/// and returns a human-readable descriptor of the active DPI awareness mode.
pub fn init_process_dpi_awareness() -> String {
    unsafe {
        if SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2).is_ok() {
            return "per_monitor_aware_v2".to_string();
        }
        if SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE).is_ok() {
            return "per_monitor_aware".to_string();
        }
        if SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_SYSTEM_AWARE).is_ok() {
            return "system_aware".to_string();
        }
        if SetProcessDPIAware().as_bool() {
            return "system_dpi_aware".to_string();
        }
        "unaware".to_string()
    }
}

pub fn now_iso8601_utc() -> String {
    let dur = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let secs = dur.as_secs();
    let millis = dur.subsec_millis();
    format!("{}.{:03}Z", secs, millis)
}

/// Service-backed window identity tracker that assigns monotonic checked `issue_generation`
/// counters and model-issued `windowKey` values to live `(HWND, PID, ProcessCreationTime)`
/// identities while consuming delivered WinEvent (`EVENT_OBJECT_CREATE` / `EVENT_OBJECT_DESTROY`)
/// lifecycle records from the shared `LifecycleService`.
#[derive(Clone)]
pub struct WindowIdentityTracker {
    service: LifecycleService,
}

impl WindowIdentityTracker {
    /// Creates a `WindowIdentityTracker` backed by an active `LifecycleService`.
    /// All clones share the exact same underlying bounded registry and WinEvent pump state.
    pub fn from_service(service: LifecycleService) -> Self {
        Self { service }
    }

    /// Creates an explicitly inert, hook-free `WindowIdentityTracker` for pure unit tests.
    /// Never starts a WinEvent hook and cannot issue live `WindowRef`s.
    pub fn new_inert_for_test(host_nonce: HostWorkerNonce) -> Self {
        Self {
            service: LifecycleService::new_inert_for_test(host_nonce),
        }
    }

    pub fn service(&self) -> &LifecycleService {
        &self.service
    }

    pub fn worker_instance(&self) -> &str {
        self.service.worker_instance()
    }

    /// Queries the live Win32 `(HWND, PID, ProcessCreationTime)` tuple for `hwnd`.
    pub fn query_live_window_os_tuple(
        hwnd: HWND,
        stage: ErrorStage,
    ) -> Result<WindowOsTuple, ProtocolError> {
        let hwnd_val = hwnd_to_u64(hwnd);
        if hwnd_val == 0 || !unsafe { IsWindow(Some(hwnd)).as_bool() } {
            let err = match stage {
                ErrorStage::Observe => ProtocolError::observe(
                    ErrorCode::WindowNotFound,
                    DiagnosticReason::IsWindowFalse,
                ),
                _ => ProtocolError::validate(
                    ErrorCode::WindowNotFound,
                    DiagnosticReason::IsWindowFalse,
                ),
            };
            return Err(err.with_hwnds(Some(hwnd_val), None));
        }

        let (pid, _) = get_window_pid_tid(hwnd);
        if pid == 0 {
            let err = match stage {
                ErrorStage::Observe => ProtocolError::observe(
                    ErrorCode::WindowNotFound,
                    DiagnosticReason::IsWindowFalse,
                ),
                _ => ProtocolError::validate(
                    ErrorCode::WindowNotFound,
                    DiagnosticReason::IsWindowFalse,
                ),
            };
            return Err(err.with_hwnds(Some(hwnd_val), None));
        }

        let process_started = match query_process_creation_time(pid) {
            Some(ticks) if ticks > 0 => ticks,
            _ => {
                let err = match stage {
                    ErrorStage::Observe => ProtocolError::observe(
                        ErrorCode::WindowStale,
                        DiagnosticReason::ProcessCreationTimeMismatch,
                    ),
                    _ => ProtocolError::validate(
                        ErrorCode::WindowStale,
                        DiagnosticReason::ProcessCreationTimeMismatch,
                    ),
                };
                return Err(err.with_hwnds(Some(hwnd_val), None));
            }
        };

        WindowOsTuple::new(hwnd_val, pid, process_started)
            .map_err(|e| map_lifecycle_error_to_protocol(e, stage, Some(hwnd_val)))
    }

    /// Drains delivered WinEvent records, verifies live `(HWND, PID, ProcessCreationTime)` against
    /// `live_tuple`, and issues or reuses the bounded model identity.
    pub fn build_window_ref_for_tuple(
        &self,
        live_tuple: WindowOsTuple,
        app_id: Option<String>,
        stage: ErrorStage,
    ) -> Result<(WindowRef, IssueOutcome), ProtocolError> {
        let outcome = self
            .service
            .issue_or_reuse_window(live_tuple)
            .map_err(|e| map_lifecycle_error_to_protocol(e, stage, Some(live_tuple.hwnd())))?;

        let wref = WindowRef {
            worker_instance: self.service.worker_instance().to_string(),
            window_key: outcome.identity.window_key.clone(),
            hwnd: live_tuple.hwnd().to_string(),
            pid: live_tuple.pid(),
            process_started: live_tuple.process_started().to_string(),
            generation: outcome.identity.issue_generation,
            app_id,
        };
        Ok((wref, outcome))
    }

    pub fn build_window_ref(
        &self,
        hwnd: HWND,
        app_id: Option<String>,
    ) -> Result<WindowRef, ProtocolError> {
        let live_tuple = Self::query_live_window_os_tuple(hwnd, ErrorStage::Observe)?;
        let (wref, _) = self.build_window_ref_for_tuple(live_tuple, app_id, ErrorStage::Observe)?;
        Ok(wref)
    }

    /// Revalidates a client-supplied `WindowRef` (and optional internal `expected_stamp`) against
    /// a caller-supplied `live_tuple` and the shared `LifecycleService`.
    pub fn validate_window_ref_with_live_tuple(
        &self,
        wref: &WindowRef,
        live_tuple: WindowOsTuple,
        expected_stamp: Option<&LifecycleStamp>,
        stage: ErrorStage,
    ) -> Result<LifecycleStamp, ProtocolError> {
        let parsed_tuple = parse_window_ref_os_tuple(wref, self.service.worker_instance())?;
        let hwnd_val = parsed_tuple.hwnd();

        if live_tuple.pid() != parsed_tuple.pid() {
            return Err(ProtocolError::validate(
                ErrorCode::WindowStale,
                DiagnosticReason::PidMismatch,
            )
            .with_hwnds(Some(hwnd_val), None));
        }
        if live_tuple.process_started() != parsed_tuple.process_started() {
            return Err(ProtocolError::validate(
                ErrorCode::WindowStale,
                DiagnosticReason::ProcessCreationTimeMismatch,
            )
            .with_hwnds(Some(hwnd_val), None));
        }

        self.service
            .validate_window_ref_and_stamp(wref, live_tuple, expected_stamp)
            .map_err(|e| map_lifecycle_error_to_protocol(e, stage, Some(hwnd_val)))
    }

    /// Revalidates a client-supplied `WindowRef` (plus optional internal `expected_stamp`) against
    /// the live OS window and the shared `LifecycleService`.
    pub fn validate_window_ref_and_stamp(
        &self,
        wref: &WindowRef,
        expected_stamp: Option<&LifecycleStamp>,
    ) -> Result<(HWND, LifecycleStamp), ProtocolError> {
        let parsed_tuple = parse_window_ref_os_tuple(wref, self.service.worker_instance())?;
        let hwnd_val = parsed_tuple.hwnd();
        let hwnd = u64_to_hwnd(hwnd_val);

        if !unsafe { IsWindow(Some(hwnd)).as_bool() } {
            return Err(ProtocolError::validate(
                ErrorCode::WindowNotFound,
                DiagnosticReason::IsWindowFalse,
            )
            .with_hwnds(Some(hwnd_val), None));
        }

        let (live_pid, _) = get_window_pid_tid(hwnd);
        if live_pid != parsed_tuple.pid() {
            return Err(ProtocolError::validate(
                ErrorCode::WindowStale,
                DiagnosticReason::PidMismatch,
            )
            .with_hwnds(Some(hwnd_val), None));
        }

        let live_started = match query_process_creation_time(live_pid) {
            Some(ticks) if ticks > 0 => ticks,
            _ => {
                return Err(ProtocolError::validate(
                    ErrorCode::WindowStale,
                    DiagnosticReason::ProcessCreationTimeMismatch,
                )
                .with_hwnds(Some(hwnd_val), None));
            }
        };
        if live_started != parsed_tuple.process_started() {
            return Err(ProtocolError::validate(
                ErrorCode::WindowStale,
                DiagnosticReason::ProcessCreationTimeMismatch,
            )
            .with_hwnds(Some(hwnd_val), None));
        }

        let live_tuple = WindowOsTuple::new(hwnd_val, live_pid, live_started)
            .map_err(|e| map_lifecycle_error_to_protocol(e, ErrorStage::Validate, Some(hwnd_val)))?;

        let stamp = self.validate_window_ref_with_live_tuple(
            wref,
            live_tuple,
            expected_stamp,
            ErrorStage::Validate,
        )?;
        Ok((hwnd, stamp))
    }

    /// Revalidates a client-supplied `WindowRef` against the live OS window and the shared
    /// `LifecycleService`.
    /// Rejects cross-worker refs, recycled HWNDs, PID changes, process creation time mismatches,
    /// zero/unprovable creation times, retired/evicted generations, or unhealthy hook coverage.
    pub fn validate_window_ref(&self, wref: &WindowRef) -> Result<HWND, ProtocolError> {
        let (hwnd, _) = self.validate_window_ref_and_stamp(wref, None)?;
        Ok(hwnd)
    }

    /// Begins a `state.get` observation for `wref` against the live OS window:
    /// drains pending WinEvent records, verifies `wref`'s immutable issued key/generation and live
    /// OS tuple without minting a new generation for a stale ref, and returns `(HWND, ObservationCheckpoint)`.
    pub fn begin_live_observation(
        &self,
        wref: &WindowRef,
    ) -> Result<(HWND, ObservationCheckpoint), ProtocolError> {
        let (hwnd, stamp) = self.validate_window_ref_and_stamp(wref, None)?;
        Ok((hwnd, ObservationCheckpoint { stamp }))
    }

    /// Completes a `state.get` observation for `wref` against the live OS window:
    /// drains pending WinEvent records, re-queries live `(HWND, PID, ProcessCreationTime)`, and
    /// verifies that neither coverage nor the target window's `ObservationCheckpoint` changed.
    pub fn finish_live_observation(
        &self,
        wref: &WindowRef,
        checkpoint: &ObservationCheckpoint,
    ) -> Result<LifecycleStamp, ProtocolError> {
        let (hwnd, _) = self.validate_window_ref_and_stamp(wref, Some(&checkpoint.stamp))?;
        let live_tuple = Self::query_live_window_os_tuple(hwnd, ErrorStage::Observe)?;
        self.service
            .finish_observation_for_window_ref(checkpoint, live_tuple)
            .map_err(|e| map_lifecycle_error_to_protocol(e, ErrorStage::Observe, Some(live_tuple.hwnd())))
    }
}

pub fn hwnd_to_u64(hwnd: HWND) -> u64 {
    hwnd.0 as usize as u64
}

pub fn u64_to_hwnd(val: u64) -> HWND {
    HWND(val as usize as *mut c_void)
}

pub fn parse_decimal_u64(s: &str) -> Result<u64, ProtocolError> {
    if s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()) {
        return Err(ProtocolError::validate(
            ErrorCode::InvalidParams,
            DiagnosticReason::HwndParseFailed,
        ));
    }
    s.parse::<u64>().map_err(|_| {
        ProtocolError::validate(ErrorCode::InvalidParams, DiagnosticReason::HwndParseFailed)
    })
}

pub fn get_window_pid_tid(hwnd: HWND) -> (u32, u32) {
    unsafe {
        let mut pid = 0u32;
        let tid = GetWindowThreadProcessId(hwnd, Some(&mut pid));
        (pid, tid)
    }
}

pub fn query_process_creation_time(pid: u32) -> Option<u64> {
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid).ok()?;
        let mut creation = FILETIME::default();
        let mut exit = FILETIME::default();
        let mut kernel = FILETIME::default();
        let mut user = FILETIME::default();
        let ok = GetProcessTimes(handle, &mut creation, &mut exit, &mut kernel, &mut user).is_ok();
        let _ = CloseHandle(handle);
        if !ok {
            return None;
        }
        let ticks = ((creation.dwHighDateTime as u64) << 32) | (creation.dwLowDateTime as u64);
        Some(ticks)
    }
}

pub fn query_process_executable_path(pid: u32) -> Option<String> {
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid).ok()?;
        let mut buf = vec![0u16; 1024];
        let mut size = buf.len() as u32;
        let res = QueryFullProcessImageNameW(
            handle,
            PROCESS_NAME_WIN32,
            PWSTR(buf.as_mut_ptr()),
            &mut size,
        );
        let _ = CloseHandle(handle);
        if res.is_ok() && size > 0 {
            Some(String::from_utf16_lossy(&buf[..size as usize]))
        } else {
            None
        }
    }
}

pub fn get_window_title(hwnd: HWND) -> String {
    unsafe {
        let mut buf = [0u16; 512];
        let len = GetWindowTextW(hwnd, &mut buf);
        if len > 0 {
            String::from_utf16_lossy(&buf[..len as usize])
        } else {
            String::new()
        }
    }
}

pub fn get_window_class_name(hwnd: HWND) -> String {
    unsafe {
        let mut buf = [0u16; 256];
        let len = GetClassNameW(hwnd, &mut buf);
        if len > 0 {
            String::from_utf16_lossy(&buf[..len as usize])
        } else {
            String::new()
        }
    }
}

pub fn is_window_cloaked(hwnd: HWND) -> bool {
    unsafe {
        let mut cloaked: u32 = 0;
        let res = DwmGetWindowAttribute(
            hwnd,
            DWMWA_CLOAKED,
            &mut cloaked as *mut u32 as *mut c_void,
            std::mem::size_of::<u32>() as u32,
        );
        res.is_ok() && cloaked != 0
    }
}

pub fn is_window_capture_protected(hwnd: HWND) -> bool {
    unsafe {
        let mut affinity: u32 = 0;
        if GetWindowDisplayAffinity(hwnd, &mut affinity).is_ok() {
            affinity != WDA_NONE.0
        } else {
            false
        }
    }
}

pub fn get_window_geometry(hwnd: HWND) -> Result<WindowGeometry, ProtocolError> {
    unsafe {
        let mut wrect = RECT::default();
        GetWindowRect(hwnd, &mut wrect).map_err(|e| {
            ProtocolError::observe(ErrorCode::WindowNotFound, DiagnosticReason::IsWindowFalse)
                .with_hresult(e.code().0)
                .with_hwnds(Some(hwnd_to_u64(hwnd)), None)
        })?;

        let mut crect = RECT::default();
        let client_rect = if GetClientRect(hwnd, &mut crect).is_ok() {
            let mut tl = POINT {
                x: crect.left,
                y: crect.top,
            };
            let mut br = POINT {
                x: crect.right,
                y: crect.bottom,
            };
            let ok_tl = ClientToScreen(hwnd, &mut tl).as_bool();
            let ok_br = ClientToScreen(hwnd, &mut br).as_bool();
            if ok_tl && ok_br {
                RectPx::from_ltrb(tl.x, tl.y, br.x, br.y)
            } else {
                RectPx::from_ltrb(wrect.left, wrect.top, wrect.right, wrect.bottom)
            }
        } else {
            RectPx::from_ltrb(wrect.left, wrect.top, wrect.right, wrect.bottom)
        };

        let raw_dpi = GetDpiForWindow(hwnd);
        let dpi = if raw_dpi == 0 { 96 } else { raw_dpi };
        let scale_factor = (dpi as f64) / 96.0;

        Ok(WindowGeometry {
            window_rect: RectPx::from_ltrb(wrect.left, wrect.top, wrect.right, wrect.bottom),
            client_rect,
            screen_origin: [wrect.left, wrect.top],
            dpi,
            scale_factor,
        })
    }
}

/// Outcome of evaluating the directional relationship between a `candidate` window
/// (e.g., foreground window or `WindowFromPoint` hit) and the authorized `target` window.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WindowRelationDecision {
    AllowedExactTarget,
    AllowedTargetChildDescendant,
    AllowedOwnedPopupChain,
    RejectedNullOrInvalid,
    RejectedCrossProcess,
    RejectedUnprovenProcessIdentity,
    RejectedSiblingOrUnownedPopup,
    RejectedOwnerChainCycleOrLimit,
}

impl WindowRelationDecision {
    pub const fn is_allowed(self) -> bool {
        matches!(
            self,
            Self::AllowedExactTarget
                | Self::AllowedTargetChildDescendant
                | Self::AllowedOwnedPopupChain
        )
    }
}

/// Snapshot node used for deterministic pure relationship graph evaluation and testing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WindowRelationNode {
    pub hwnd: u64,
    pub root_hwnd: u64,
    pub parent_hwnd: u64,
    pub owner_hwnd: u64,
    pub pid: u32,
    pub process_started: Option<u64>,
}

/// Pure, deterministic directional relation evaluator:
/// 1. Rejects null/missing nodes, cross-PID windows, and zero/unproven `process_started` identities.
/// 2. Allows `candidate == target`.
/// 3. Allows true `WS_CHILD` descendants of `target` (walking `parent_hwnd` up to `target`) when every hop
///    shares the exact same `(pid, process_started)`.
/// 4. Allows a directional `GW_OWNER` popup chain starting at `candidate.root_hwnd` ONLY if walking
///    `owner_hwnd` reaches `target_root` (`target` or `target.root_hwnd`), with every intermediate popup
///    sharing the exact same `(pid, process_started)`.
/// 5. Explicitly rejects sibling modal/popup dialogs that merely share a common root owner with `target`
///    (i.e. when `target` is dialog A owned by main window M, sibling dialog B owned by M does NOT have A
///    in its directional owner chain).
pub fn evaluate_directional_window_relation<F>(
    candidate_hwnd: u64,
    target_hwnd: u64,
    mut lookup: F,
) -> WindowRelationDecision
where
    F: FnMut(u64) -> Option<WindowRelationNode>,
{
    if candidate_hwnd == 0 || target_hwnd == 0 {
        return WindowRelationDecision::RejectedNullOrInvalid;
    }

    let Some(cand_node) = lookup(candidate_hwnd) else {
        return WindowRelationDecision::RejectedNullOrInvalid;
    };
    let Some(targ_node) = lookup(target_hwnd) else {
        return WindowRelationDecision::RejectedNullOrInvalid;
    };

    if cand_node.pid == 0 || targ_node.pid == 0 || cand_node.pid != targ_node.pid {
        return WindowRelationDecision::RejectedCrossProcess;
    }

    let (Some(cand_started), Some(targ_started)) =
        (cand_node.process_started, targ_node.process_started)
    else {
        return WindowRelationDecision::RejectedUnprovenProcessIdentity;
    };
    if cand_started == 0 || targ_started == 0 || cand_started != targ_started {
        return WindowRelationDecision::RejectedUnprovenProcessIdentity;
    }

    if candidate_hwnd == target_hwnd {
        return WindowRelationDecision::AllowedExactTarget;
    }

    let targ_root = if targ_node.root_hwnd != 0 {
        targ_node.root_hwnd
    } else {
        target_hwnd
    };
    let cand_root = if cand_node.root_hwnd != 0 {
        cand_node.root_hwnd
    } else {
        candidate_hwnd
    };

    // Case A: Candidate is inside the same top-level window as target
    if cand_root == targ_root {
        if target_hwnd == targ_root {
            return WindowRelationDecision::AllowedTargetChildDescendant;
        }
        // Target is a specific child/sub-window: candidate must be a descendant of target_hwnd
        let mut curr = candidate_hwnd;
        for _ in 0..32 {
            if curr == target_hwnd {
                return WindowRelationDecision::AllowedTargetChildDescendant;
            }
            let Some(node) = lookup(curr) else {
                return WindowRelationDecision::RejectedNullOrInvalid;
            };
            if node.pid != targ_node.pid || node.process_started != Some(targ_started) {
                return WindowRelationDecision::RejectedCrossProcess;
            }
            if node.parent_hwnd == 0 || node.parent_hwnd == curr {
                break;
            }
            curr = node.parent_hwnd;
        }
        return WindowRelationDecision::RejectedSiblingOrUnownedPopup;
    }

    // Case B: Candidate is in a top-level popup window. Walk directional GW_OWNER chain from cand_root
    // to see if it is directly or transitively owned by `target_hwnd` (or `targ_root` when target is root).
    // Note: if target is itself a modal/owned popup (so targ_root.owner_hwnd != 0), we require the owner
    // chain of cand_root to pass through `targ_root` itself—never accepting a sibling popup that only shares
    // `targ_root`'s owner!
    let mut curr_popup = cand_root;
    for _ in 0..16 {
        let Some(popup_node) = lookup(curr_popup) else {
            return WindowRelationDecision::RejectedNullOrInvalid;
        };
        if popup_node.pid != targ_node.pid {
            return WindowRelationDecision::RejectedCrossProcess;
        }
        if popup_node.process_started != Some(targ_started) {
            return WindowRelationDecision::RejectedUnprovenProcessIdentity;
        }

        let owner = popup_node.owner_hwnd;
        if owner == 0 {
            return WindowRelationDecision::RejectedSiblingOrUnownedPopup;
        }
        if owner == curr_popup {
            return WindowRelationDecision::RejectedOwnerChainCycleOrLimit;
        }

        let owner_root = lookup(owner)
            .map(|n| if n.root_hwnd != 0 { n.root_hwnd } else { owner })
            .unwrap_or(owner);

        if owner == target_hwnd || owner == targ_root || owner_root == targ_root {
            // Verify the owner node itself also has matching PID and process creation time
            if let Some(owner_node) = lookup(owner) {
                if owner_node.pid != targ_node.pid {
                    return WindowRelationDecision::RejectedCrossProcess;
                }
                if owner_node.process_started != Some(targ_started) {
                    return WindowRelationDecision::RejectedUnprovenProcessIdentity;
                }
                return WindowRelationDecision::AllowedOwnedPopupChain;
            } else {
                return WindowRelationDecision::RejectedNullOrInvalid;
            }
        }

        curr_popup = owner_root;
    }

    WindowRelationDecision::RejectedOwnerChainCycleOrLimit
}

fn query_live_relation_node(hwnd_val: u64) -> Option<WindowRelationNode> {
    if hwnd_val == 0 {
        return None;
    }
    let hwnd = u64_to_hwnd(hwnd_val);
    unsafe {
        if !IsWindow(Some(hwnd)).as_bool() {
            return None;
        }
        let (pid, _) = get_window_pid_tid(hwnd);
        let process_started = if pid != 0 {
            query_process_creation_time(pid)
        } else {
            None
        };
        let root = GetAncestor(hwnd, GA_ROOT);
        let parent = windows::Win32::UI::WindowsAndMessaging::GetParent(hwnd).unwrap_or_default();
        let owner = GetWindow(hwnd, GW_OWNER).unwrap_or_default();

        Some(WindowRelationNode {
            hwnd: hwnd_val,
            root_hwnd: hwnd_to_u64(root),
            parent_hwnd: hwnd_to_u64(parent),
            owner_hwnd: hwnd_to_u64(owner),
            pid,
            process_started,
        })
    }
}

/// Checks if `candidate` has a valid directional relationship to `target`:
/// - `candidate == target`,
/// - `candidate` is a child/descendant of `target`, or
/// - `candidate`'s top-level window has a directional `GW_OWNER` chain leading to `target`,
/// with every window in the chain belonging to the exact same `(PID, ProcessCreationTime)`.
/// Rejects sibling modals sharing a common owner and rejects cross-PID windows.
pub fn inspect_hwnd_relation_to_target(candidate: HWND, target: HWND) -> WindowRelationDecision {
    evaluate_directional_window_relation(
        hwnd_to_u64(candidate),
        hwnd_to_u64(target),
        query_live_relation_node,
    )
}

pub fn is_hwnd_related_to_target(candidate: HWND, target: HWND) -> bool {
    inspect_hwnd_relation_to_target(candidate, target).is_allowed()
}

pub fn get_foreground_status(target: HWND) -> ForegroundStatus {
    unsafe {
        let fg = GetForegroundWindow();
        if fg.0.is_null() {
            return ForegroundStatus {
                is_target_window: false,
                is_related_to_target: false,
                foreground_hwnd: None,
                foreground_pid: None,
            };
        }
        let (fg_pid, _) = get_window_pid_tid(fg);
        let is_target = fg == target || GetAncestor(fg, GA_ROOT) == target;
        let is_related = is_hwnd_related_to_target(fg, target);
        ForegroundStatus {
            is_target_window: is_target,
            is_related_to_target: is_related,
            foreground_hwnd: Some(hwnd_to_u64(fg).to_string()),
            foreground_pid: if fg_pid != 0 { Some(fg_pid) } else { None },
        }
    }
}

pub fn inspect_window_entry(
    tracker: &WindowIdentityTracker,
    hwnd: HWND,
    fg_hwnd: HWND,
) -> Result<WindowEntry, ProtocolError> {
    if hwnd.0.is_null() || !unsafe { IsWindow(Some(hwnd)).as_bool() } {
        return Err(
            ProtocolError::observe(ErrorCode::WindowNotFound, DiagnosticReason::IsWindowFalse)
                .with_hwnds(Some(hwnd_to_u64(hwnd)), None),
        );
    }

    let (pid, _) = get_window_pid_tid(hwnd);
    let exe_path = query_process_executable_path(pid);
    let process_name = exe_path
        .as_deref()
        .and_then(|p| Path::new(p).file_name())
        .and_then(|s| s.to_str())
        .unwrap_or("unknown")
        .to_string();

    let app_id = if process_name != "unknown" {
        Some(process_name.to_lowercase())
    } else {
        None
    };

    let wref = tracker.build_window_ref(hwnd, app_id)?;
    let title = get_window_title(hwnd);
    let class_name = get_window_class_name(hwnd);
    let geometry = get_window_geometry(hwnd)?;

    let visible = unsafe { IsWindowVisible(hwnd).as_bool() };
    let enabled = unsafe { IsWindowEnabled(hwnd).as_bool() };
    let minimized = unsafe { IsIconic(hwnd).as_bool() };
    let maximized = unsafe { IsZoomed(hwnd).as_bool() };
    let cloaked = is_window_cloaked(hwnd);
    let capture_protected = is_window_capture_protected(hwnd);
    let foreground = !fg_hwnd.0.is_null() && is_hwnd_related_to_target(fg_hwnd, hwnd);

    Ok(WindowEntry {
        window: wref,
        title,
        class_name,
        process_name,
        executable_path: exe_path,
        visible,
        enabled,
        minimized,
        maximized,
        foreground,
        cloaked,
        capture_protected,
        bounds: geometry.window_rect,
        client_bounds: geometry.client_rect,
        dpi: geometry.dpi,
    })
}

unsafe extern "system" fn enum_windows_proc(hwnd: HWND, lparam: LPARAM) -> BOOL {
    let vec_ptr = lparam.0 as *mut Vec<HWND>;
    if !vec_ptr.is_null() {
        (*vec_ptr).push(hwnd);
    }
    BOOL(1)
}

pub fn enumerate_top_level_hwnds() -> Vec<HWND> {
    let mut hwnds: Vec<HWND> = Vec::with_capacity(128);
    unsafe {
        let _ = EnumWindows(
            Some(enum_windows_proc),
            LPARAM(&mut hwnds as *mut Vec<HWND> as isize),
        );
    }
    hwnds
}

pub fn handle_windows_list(
    tracker: &WindowIdentityTracker,
    params: WindowsListParams,
) -> Result<WindowsListResult, ProtocolError> {
    tracker.service().ensure_healthy().map_err(|err| {
        map_lifecycle_error_to_protocol(err, ErrorStage::Observe, None)
    })?;
    let include_minimized = params.include_minimized.unwrap_or(true);
    let include_invisible = params.include_invisible.unwrap_or(false);
    let include_cloaked = params.include_cloaked.unwrap_or(false);
    let max_windows = params
        .max_windows
        .unwrap_or(MAX_WINDOWS_DEFAULT)
        .clamp(1, MAX_WINDOWS_CAP);

    let app_query = params.app_query.as_ref().map(|q| q.to_lowercase());
    let title_query = params.title_query.as_ref().map(|q| q.to_lowercase());

    let fg_hwnd = unsafe { GetForegroundWindow() };
    let foreground_window = if !fg_hwnd.0.is_null() && unsafe { IsWindow(Some(fg_hwnd)).as_bool() }
    {
        tracker.build_window_ref(fg_hwnd, None).ok()
    } else {
        None
    };

    let hwnds = enumerate_top_level_hwnds();
    let mut entries = Vec::new();

    for hwnd in hwnds {
        if entries.len() >= max_windows {
            break;
        }
        let visible = unsafe { IsWindowVisible(hwnd).as_bool() };
        if !include_invisible && !visible {
            continue;
        }
        let minimized = unsafe { IsIconic(hwnd).as_bool() };
        if !include_minimized && minimized {
            continue;
        }
        let cloaked = is_window_cloaked(hwnd);
        if !include_cloaked && cloaked {
            continue;
        }

        if let Some(filter_pid) = params.pid {
            let (pid, _) = get_window_pid_tid(hwnd);
            if pid != filter_pid {
                continue;
            }
        }

        let entry = match inspect_window_entry(tracker, hwnd, fg_hwnd) {
            Ok(e) => e,
            Err(_) => continue,
        };

        if !include_invisible
            && !entry.minimized
            && (entry.bounds.width <= 0 || entry.bounds.height <= 0)
        {
            continue;
        }

        if let Some(ref aq) = app_query {
            let proc_match = entry.process_name.to_lowercase().contains(aq);
            let class_match = entry.class_name.to_lowercase().contains(aq);
            let exe_match = entry
                .executable_path
                .as_ref()
                .map(|p| p.to_lowercase().contains(aq))
                .unwrap_or(false);
            if !proc_match && !class_match && !exe_match {
                continue;
            }
        }

        if let Some(ref tq) = title_query {
            if !entry.title.to_lowercase().contains(tq) {
                continue;
            }
        }

        entries.push(entry);
    }

    // Per-window failures (closed/protected windows) can be skipped, but a failed
    // global hook must never masquerade as a successful empty desktop.
    tracker.service().ensure_healthy().map_err(|err| {
        map_lifecycle_error_to_protocol(err, ErrorStage::Observe, None)
    })?;
    Ok(WindowsListResult {
        captured_at: now_iso8601_utc(),
        foreground_window,
        windows: entries,
    })
}

pub fn handle_windows_get(
    tracker: &WindowIdentityTracker,
    params: WindowsGetParams,
) -> Result<WindowsGetResult, ProtocolError> {
    let hwnd = if let Some(ref wref) = params.window {
        tracker.validate_window_ref(wref)?
    } else if let Some(ref hwnd_str) = params.hwnd {
        let val = parse_decimal_u64(hwnd_str)?;
        let h = u64_to_hwnd(val);
        if val == 0 || !unsafe { IsWindow(Some(h)).as_bool() } {
            return Err(
                ProtocolError::observe(ErrorCode::WindowNotFound, DiagnosticReason::IsWindowFalse)
                    .with_hwnds(Some(val), None),
            );
        }
        h
    } else {
        return Err(ProtocolError::validate(
            ErrorCode::InvalidParams,
            DiagnosticReason::HwndParseFailed,
        ));
    };

    let fg_hwnd = unsafe { GetForegroundWindow() };
    let entry = inspect_window_entry(tracker, hwnd, fg_hwnd)?;

    Ok(WindowsGetResult {
        captured_at: now_iso8601_utc(),
        entry,
    })
}

pub fn handle_apps_list(
    tracker: &WindowIdentityTracker,
    params: AppsListParams,
) -> Result<AppsListResult, ProtocolError> {
    tracker.service().ensure_healthy().map_err(|err| {
        map_lifecycle_error_to_protocol(err, ErrorStage::Observe, None)
    })?;
    let max_apps = params.max_apps.unwrap_or(64).clamp(1, 256);
    let query = params.query.as_ref().map(|q| q.to_lowercase());

    let mut apps_by_id: HashMap<String, AppEntry> = HashMap::new();

    let fg_hwnd = unsafe { GetForegroundWindow() };
    for hwnd in enumerate_top_level_hwnds() {
        if !unsafe { IsWindowVisible(hwnd).as_bool() } || is_window_cloaked(hwnd) {
            continue;
        }
        if let Ok(entry) = inspect_window_entry(tracker, hwnd, fg_hwnd) {
            if entry.process_name != "unknown" && !entry.process_name.is_empty() {
                let app_id = entry.process_name.to_lowercase();
                apps_by_id.entry(app_id.clone()).or_insert_with(|| AppEntry {
                    app_id,
                    name: if !entry.title.is_empty() {
                        entry.title
                    } else {
                        entry.process_name
                    },
                    source: "running_window".to_string(),
                    executable_path: entry.executable_path,
                    aumid: None,
                });
            }
        }
    }

    if let Ok(sysroot) = std::env::var("SystemRoot") {
        let candidates = [
            ("notepad.exe", "Notepad", "System32\\notepad.exe"),
            ("calc.exe", "Calculator", "System32\\calc.exe"),
            ("mspaint.exe", "Paint", "System32\\mspaint.exe"),
            ("explorer.exe", "File Explorer", "explorer.exe"),
        ];
        for (app_id, display_name, rel_path) in candidates {
            let full_path = Path::new(&sysroot).join(rel_path);
            if full_path.is_file() {
                apps_by_id
                    .entry(app_id.to_string())
                    .or_insert_with(|| AppEntry {
                        app_id: app_id.to_string(),
                        name: display_name.to_string(),
                        source: "system_catalog".to_string(),
                        executable_path: Some(full_path.to_string_lossy().into_owned()),
                        aumid: None,
                    });
            }
        }
    }

    let mut apps: Vec<AppEntry> = apps_by_id
        .into_values()
        .filter(|app| {
            if let Some(ref q) = query {
                app.app_id.to_lowercase().contains(q)
                    || app.name.to_lowercase().contains(q)
                    || app
                        .executable_path
                        .as_ref()
                        .map(|p| p.to_lowercase().contains(q))
                        .unwrap_or(false)
            } else {
                true
            }
        })
        .collect();

    apps.sort_by(|a, b| a.app_id.cmp(&b.app_id));
    apps.truncate(max_apps);

    tracker.service().ensure_healthy().map_err(|err| {
        map_lifecycle_error_to_protocol(err, ErrorStage::Observe, None)
    })?;
    Ok(AppsListResult { apps })
}

/// Direct `apps.launch` RPC is disallowed from bypassing `action.prepare` -> `action.ready` -> `action.commit`.
/// Always returns `unsupported` with `mutation = none` and no OS side effects.
pub fn handle_apps_launch(_params: &AppsLaunchParams) -> Result<serde_json::Value, ProtocolError> {
    Err(ProtocolError::validate(
        ErrorCode::Unsupported,
        DiagnosticReason::DirectLaunchDisallowed,
    ))
}

pub fn current_worker_pid() -> u32 {
    unsafe { GetCurrentProcessId() }
}
