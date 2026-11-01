use crate::errors::{
    DiagnosticReason, ErrorCode, ErrorStage, InternalMutationDetail, ProtocolError,
};
use crate::protocol::{
    AccessibilityObservation, AccessibilityOptions, RectPx, UiaElement, MAX_TEXT_CHARS_CAP,
    MAX_TEXT_CHARS_DEFAULT, MAX_UIA_DEPTH_CAP, MAX_UIA_DEPTH_DEFAULT, MAX_UIA_ELEMENTS_CAP,
    MAX_UIA_ELEMENTS_DEFAULT,
};
use sha2::{Digest, Sha256};
use std::ffi::c_void;
use windows::core::{Interface, BSTR};
use windows::Win32::Foundation::{HWND, RECT};
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED,
    COINIT_MULTITHREADED, SAFEARRAY,
};
use windows::Win32::System::Ole::{
    SafeArrayDestroy, SafeArrayGetElement, SafeArrayGetLBound, SafeArrayGetUBound,
};
use windows::Win32::UI::Accessibility::*;

/// Ensures COM is initialized on the calling thread and creates a native `IUIAutomation` instance.
pub fn create_uia_instance() -> Result<IUIAutomation, ProtocolError> {
    unsafe {
        let hr = CoInitializeEx(None, COINIT_MULTITHREADED);
        if hr.is_err() {
            let _ = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
        }
        CoCreateInstance(&CUIAutomation, None, CLSCTX_INPROC_SERVER).map_err(|e| {
            ProtocolError::observe(ErrorCode::UiaUnavailable, DiagnosticReason::UiaPatternMissing)
                .with_hresult(e.code().0)
        })
    }
}

/// Observes the accessibility tree of `hwnd` using native COM UIA (`IUIAutomation`).
/// Never invokes PowerShell or external processes. Excludes password field values (`CurrentIsPassword`).
pub fn observe_window_accessibility(
    hwnd: HWND,
    options: &AccessibilityOptions,
) -> AccessibilityObservation {
    let enabled = options.enabled.unwrap_or(true);
    if !enabled {
        return AccessibilityObservation {
            status: "skipped".to_string(),
            tree_hash: empty_tree_hash(),
            truncated: false,
            focused_element_index: None,
            focused_runtime_id: None,
            selected_element_indices: Vec::new(),
            selected_text: None,
            elements: Vec::new(),
        };
    }

    let max_depth = options
        .max_depth
        .unwrap_or(MAX_UIA_DEPTH_DEFAULT)
        .clamp(1, MAX_UIA_DEPTH_CAP);
    let max_elements = options
        .max_elements
        .unwrap_or(MAX_UIA_ELEMENTS_DEFAULT)
        .clamp(1, MAX_UIA_ELEMENTS_CAP);
    let include_offscreen = options.include_offscreen.unwrap_or(false);
    let max_text_chars = options
        .max_text_chars
        .unwrap_or(MAX_TEXT_CHARS_DEFAULT)
        .clamp(16, MAX_TEXT_CHARS_CAP);

    let uia = match create_uia_instance() {
        Ok(u) => u,
        Err(_) => {
            return AccessibilityObservation {
                status: "unavailable".to_string(),
                tree_hash: empty_tree_hash(),
                truncated: false,
                focused_element_index: None,
                focused_runtime_id: None,
                selected_element_indices: Vec::new(),
                selected_text: None,
                elements: Vec::new(),
            }
        }
    };

    let root = match unsafe { uia.ElementFromHandle(hwnd) } {
        Ok(r) => r,
        Err(_) => {
            return AccessibilityObservation {
                status: "unavailable".to_string(),
                tree_hash: empty_tree_hash(),
                truncated: false,
                focused_element_index: None,
                focused_runtime_id: None,
                selected_element_indices: Vec::new(),
                selected_text: None,
                elements: Vec::new(),
            }
        }
    };

    let walker = match unsafe { uia.ControlViewWalker() } {
        Ok(w) => w,
        Err(_) => {
            return AccessibilityObservation {
                status: "unavailable".to_string(),
                tree_hash: empty_tree_hash(),
                truncated: false,
                focused_element_index: None,
                focused_runtime_id: None,
                selected_element_indices: Vec::new(),
                selected_text: None,
                elements: Vec::new(),
            }
        }
    };

    let global_focused_runtime_id = unsafe {
        uia.GetFocusedElement()
            .ok()
            .and_then(|el| extract_runtime_id(&el))
    };

    let mut elements = Vec::new();
    let mut truncated = false;
    let mut focused_element_index: Option<u32> = None;
    let mut selected_element_indices: Vec<u32> = Vec::new();
    let mut selected_text: Option<String> = None;

    walk_uia_subtree(
        &walker,
        &root,
        0,
        max_depth,
        max_elements,
        include_offscreen,
        max_text_chars,
        global_focused_runtime_id.as_deref(),
        &mut elements,
        &mut truncated,
        &mut focused_element_index,
        &mut selected_element_indices,
        &mut selected_text,
    );

    let tree_hash = compute_uia_tree_hash(&elements);

    AccessibilityObservation {
        status: "ok".to_string(),
        tree_hash,
        truncated,
        focused_element_index,
        focused_runtime_id: global_focused_runtime_id,
        selected_element_indices,
        selected_text,
        elements,
    }
}

#[allow(clippy::too_many_arguments)]
fn walk_uia_subtree(
    walker: &IUIAutomationTreeWalker,
    element: &IUIAutomationElement,
    depth: u32,
    max_depth: u32,
    max_elements: usize,
    include_offscreen: bool,
    max_text_chars: usize,
    global_focused_runtime_id: Option<&str>,
    elements: &mut Vec<UiaElement>,
    truncated: &mut bool,
    focused_element_index: &mut Option<u32>,
    selected_element_indices: &mut Vec<u32>,
    selected_text: &mut Option<String>,
) {
    if elements.len() >= max_elements {
        *truncated = true;
        return;
    }

    let offscreen = unsafe {
        element
            .CurrentIsOffscreen()
            .map(|b| b.as_bool())
            .unwrap_or(false)
    };

    if include_offscreen || !offscreen || depth == 0 {
        let index = (elements.len() + 1) as u32;
        let (uia_elem, is_selected, elem_selected_text) =
            extract_uia_element(element, index, depth, offscreen, max_text_chars);

        let is_focused = uia_elem.focused
            || global_focused_runtime_id
                .map(|fid| !fid.is_empty() && fid == uia_elem.runtime_id)
                .unwrap_or(false);

        let mut uia_elem = uia_elem;
        uia_elem.focused = is_focused;

        if is_focused && focused_element_index.is_none() {
            *focused_element_index = Some(index);
        }
        if is_selected {
            selected_element_indices.push(index);
        }
        if selected_text.is_none() {
            if let Some(st) = elem_selected_text {
                if !st.is_empty() {
                    *selected_text = Some(st);
                }
            }
        }

        elements.push(uia_elem);
    }

    if depth >= max_depth {
        return;
    }

    let mut child_opt = unsafe { walker.GetFirstChildElement(element).ok() };
    while let Some(child) = child_opt {
        if elements.len() >= max_elements {
            *truncated = true;
            break;
        }
        walk_uia_subtree(
            walker,
            &child,
            depth + 1,
            max_depth,
            max_elements,
            include_offscreen,
            max_text_chars,
            global_focused_runtime_id,
            elements,
            truncated,
            focused_element_index,
            selected_element_indices,
            selected_text,
        );
        child_opt = unsafe { walker.GetNextSiblingElement(&child).ok() };
    }
}

fn extract_uia_element(
    element: &IUIAutomationElement,
    index: u32,
    depth: u32,
    offscreen: bool,
    max_text_chars: usize,
) -> (UiaElement, bool, Option<String>) {
    unsafe {
        let runtime_id = extract_runtime_id(element).unwrap_or_else(|| format!("idx_{}", index));
        let automation_id = element
            .CurrentAutomationId()
            .ok()
            .map(|b| truncate_str(&b.to_string(), max_text_chars))
            .filter(|s| !s.is_empty());

        let name = element
            .CurrentName()
            .ok()
            .map(|b| truncate_str(&b.to_string(), max_text_chars))
            .unwrap_or_default();

        let class_name = element
            .CurrentClassName()
            .ok()
            .map(|b| truncate_str(&b.to_string(), max_text_chars))
            .filter(|s| !s.is_empty());

        let control_type_id = element.CurrentControlType().unwrap_or(UIA_CustomControlTypeId);
        let control_type = map_control_type_name(control_type_id).to_string();

        let password_query = element.CurrentIsPassword().ok().map(|b| b.as_bool());
        let password = evaluate_password_protection(
            password_query,
            control_type_id == UIA_EditControlTypeId,
            &name,
        );
        let password_indeterminate = password_query.is_none();

        let enabled = element
            .CurrentIsEnabled()
            .map(|b| b.as_bool())
            .unwrap_or(false);

        let focusable = element
            .CurrentIsKeyboardFocusable()
            .map(|b| b.as_bool())
            .unwrap_or(false);

        let focused = element
            .CurrentHasKeyboardFocus()
            .map(|b| b.as_bool())
            .unwrap_or(false);

        let rect = element.CurrentBoundingRectangle().unwrap_or(RECT {
            left: 0,
            top: 0,
            right: 0,
            bottom: 0,
        });
        let bounds = RectPx::from_ltrb(rect.left, rect.top, rect.right, rect.bottom);

        let mut patterns = Vec::new();
        if password_indeterminate {
            patterns.push("password_status_unknown".to_string());
        } else if password {
            patterns.push("password_restricted".to_string());
        }
        let mut value: Option<String> = None;
        let mut is_selected = false;
        let mut selected_text: Option<String> = None;

        // Check InvokePattern
        if let Ok(unk) = element.GetCurrentPattern(UIA_InvokePatternId) {
            if unk.cast::<IUIAutomationInvokePattern>().is_ok() {
                patterns.push("invoke".to_string());
            }
        }

        // Check ValuePattern (strictly omit value if password is true or indeterminate)
        if let Ok(unk) = element.GetCurrentPattern(UIA_ValuePatternId) {
            if let Ok(val_pat) = unk.cast::<IUIAutomationValuePattern>() {
                patterns.push("value".to_string());
                if !password {
                    if let Ok(v) = val_pat.CurrentValue() {
                        let s = truncate_str(&v.to_string(), max_text_chars);
                        if !s.is_empty() {
                            value = Some(s);
                        }
                    }
                }
            }
        }

        // Check TogglePattern
        if let Ok(unk) = element.GetCurrentPattern(UIA_TogglePatternId) {
            if let Ok(tog_pat) = unk.cast::<IUIAutomationTogglePattern>() {
                patterns.push("toggle".to_string());
                if value.is_none() && !password {
                    if let Ok(state) = tog_pat.CurrentToggleState() {
                        let state_str = match state {
                            ToggleState_Off => "off",
                            ToggleState_On => "on",
                            _ => "indeterminate",
                        };
                        value = Some(state_str.to_string());
                    }
                }
            }
        }

        // Check SelectionItemPattern
        if let Ok(unk) = element.GetCurrentPattern(UIA_SelectionItemPatternId) {
            if let Ok(sel_pat) = unk.cast::<IUIAutomationSelectionItemPattern>() {
                patterns.push("selection_item".to_string());
                if let Ok(sel) = sel_pat.CurrentIsSelected() {
                    is_selected = sel.as_bool();
                }
            }
        }

        // Check ExpandCollapsePattern
        if let Ok(unk) = element.GetCurrentPattern(UIA_ExpandCollapsePatternId) {
            if unk.cast::<IUIAutomationExpandCollapsePattern>().is_ok() {
                patterns.push("expand_collapse".to_string());
            }
        }

        // Check ScrollPattern
        if let Ok(unk) = element.GetCurrentPattern(UIA_ScrollPatternId) {
            if unk.cast::<IUIAutomationScrollPattern>().is_ok() {
                patterns.push("scroll".to_string());
            }
        }

        // Check TextPattern (strictly omit if password is true or indeterminate)
        if !password {
            if let Ok(unk) = element.GetCurrentPattern(UIA_TextPatternId) {
                if let Ok(text_pat) = unk.cast::<IUIAutomationTextPattern>() {
                    patterns.push("text".to_string());
                    if let Ok(ranges) = text_pat.GetSelection() {
                        if let Ok(len) = ranges.Length() {
                            if len > 0 {
                                if let Ok(range) = ranges.GetElement(0) {
                                    if let Ok(bstr) = range.GetText(max_text_chars as i32) {
                                        let s = truncate_str(&bstr.to_string(), max_text_chars);
                                        if !s.is_empty() {
                                            selected_text = Some(s);
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        (
            UiaElement {
                index,
                runtime_id,
                automation_id,
                name,
                control_type,
                class_name,
                value,
                password,
                enabled,
                offscreen,
                focusable,
                focused,
                bounds,
                depth,
                patterns,
            },
            is_selected,
            selected_text,
        )
    }
}

/// Pure fail-closed password classifier:
/// - If `current_is_password` is `None` (COM query failed) or `Some(true)`, fails closed (`true`).
/// - If `is_edit_control` and `name` contains password indicators, returns `true`.
pub fn evaluate_password_protection(
    current_is_password: Option<bool>,
    is_edit_control: bool,
    name: &str,
) -> bool {
    match current_is_password {
        None | Some(true) => true,
        Some(false) => {
            if is_edit_control {
                let lower = name.to_lowercase();
                lower.contains("password") || lower.contains("密码") || lower.contains("口令")
            } else {
                false
            }
        }
    }
}

/// Pure verifier that checks whether a live element's runtimeId, bounds, enabled, offscreen,
/// and password flags still match the expected snapshot element within tolerance.
pub fn verify_live_element_state_matches_snapshot(
    expected_runtime_id: &str,
    expected_bounds: &RectPx,
    allow_password_target: bool,
    live_runtime_id: &str,
    live_bounds: &RectPx,
    live_enabled: Option<bool>,
    live_offscreen: Option<bool>,
    live_is_password: Option<bool>,
) -> Result<(), ProtocolError> {
    if live_runtime_id.is_empty() || live_runtime_id != expected_runtime_id {
        return Err(ProtocolError::commit(
            ErrorCode::UiaElementStale,
            ErrorStage::Validating,
            InternalMutationDetail::FocusOnly,
            DiagnosticReason::UiaPatternMissing,
        ));
    }

    match live_is_password {
        None => {
            return Err(ProtocolError::commit(
                ErrorCode::Unsupported,
                ErrorStage::Validating,
                InternalMutationDetail::FocusOnly,
                DiagnosticReason::PasswordRedacted,
            ));
        }
        Some(true) if !allow_password_target => {
            return Err(ProtocolError::commit(
                ErrorCode::Unsupported,
                ErrorStage::Validating,
                InternalMutationDetail::FocusOnly,
                DiagnosticReason::PasswordRedacted,
            ));
        }
        _ => {}
    }

    if live_enabled != Some(true) || live_offscreen != Some(false) {
        return Err(ProtocolError::commit(
            ErrorCode::StateTokenStale,
            ErrorStage::Validating,
            InternalMutationDetail::FocusOnly,
            DiagnosticReason::UiaPatternMissing,
        ));
    }

    if live_bounds.width <= 0 || live_bounds.height <= 0 {
        return Err(ProtocolError::commit(
            ErrorCode::StateTokenStale,
            ErrorStage::Validating,
            InternalMutationDetail::FocusOnly,
            DiagnosticReason::GeometryChanged,
        ));
    }

    let dx = (live_bounds.left - expected_bounds.left).abs();
    let dy = (live_bounds.top - expected_bounds.top).abs();
    let dw = (live_bounds.width - expected_bounds.width).abs();
    let dh = (live_bounds.height - expected_bounds.height).abs();
    if dx > 2 || dy > 2 || dw > 2 || dh > 2 {
        return Err(ProtocolError::commit(
            ErrorCode::StateTokenStale,
            ErrorStage::Validating,
            InternalMutationDetail::FocusOnly,
            DiagnosticReason::GeometryChanged,
        ));
    }

    Ok(())
}

pub unsafe fn extract_runtime_id(element: &IUIAutomationElement) -> Option<String> {
    let psa: *mut SAFEARRAY = element.GetRuntimeId().ok()?;
    if psa.is_null() {
        return None;
    }
    let lbound = SafeArrayGetLBound(psa, 1).ok()?;
    let ubound = SafeArrayGetUBound(psa, 1).ok()?;
    if ubound < lbound {
        let _ = SafeArrayDestroy(psa);
        return None;
    }
    let mut parts = Vec::with_capacity((ubound - lbound + 1) as usize);
    for idx in lbound..=ubound {
        let mut val: i32 = 0;
        if SafeArrayGetElement(psa, &idx, &mut val as *mut i32 as *mut c_void).is_ok() {
            parts.push(val.to_string());
        }
    }
    let _ = SafeArrayDestroy(psa);
    if parts.is_empty() {
        None
    } else {
        Some(parts.join("."))
    }
}

/// Finds a live `IUIAutomationElement` inside `hwnd` matching `expected_runtime_id`.
pub fn find_live_element_by_runtime_id(
    hwnd: HWND,
    expected_runtime_id: &str,
    max_depth: u32,
    cancel_flag: & std::sync::atomic::AtomicBool,
) -> Result<IUIAutomationElement, ProtocolError> {
    use std::sync::atomic::Ordering;
    if cancel_flag.load(Ordering::SeqCst) {
        return Err(ProtocolError::commit(
            ErrorCode::Cancelled,
            ErrorStage::Validating,
            InternalMutationDetail::FocusOnly,
            DiagnosticReason::CancelledDuringExecution,
        ));
    }

    let uia = create_uia_instance()?;
    let root = unsafe {
        uia.ElementFromHandle(hwnd).map_err(|e| {
            ProtocolError::commit(
                ErrorCode::UiaUnavailable,
                ErrorStage::Validating,
                InternalMutationDetail::FocusOnly,
                DiagnosticReason::UiaPatternMissing,
            )
            .with_hresult(e.code().0)
        })?
    };
    let walker = unsafe {
        uia.ControlViewWalker().map_err(|e| {
            ProtocolError::commit(
                ErrorCode::UiaUnavailable,
                ErrorStage::Validating,
                InternalMutationDetail::FocusOnly,
                DiagnosticReason::UiaPatternMissing,
            )
            .with_hresult(e.code().0)
        })?
    };

    let mut visited = 0usize;
    let mut cancelled = false;
    if let Some(found) = search_element_recursive(
        &walker,
        &root,
        expected_runtime_id,
        0,
        max_depth.clamp(1, MAX_UIA_DEPTH_CAP),
        &mut visited,
        cancel_flag,
        &mut cancelled,
    ) {
        if cancel_flag.load(Ordering::SeqCst) {
            return Err(ProtocolError::commit(
                ErrorCode::Cancelled,
                ErrorStage::Validating,
                InternalMutationDetail::FocusOnly,
                DiagnosticReason::CancelledDuringExecution,
            ));
        }
        Ok(found)
    } else if cancelled || cancel_flag.load(Ordering::SeqCst) {
        Err(ProtocolError::commit(
            ErrorCode::Cancelled,
            ErrorStage::Validating,
            InternalMutationDetail::FocusOnly,
            DiagnosticReason::CancelledDuringExecution,
        ))
    } else {
        Err(ProtocolError::commit(
            ErrorCode::UiaElementStale,
            ErrorStage::Validating,
            InternalMutationDetail::FocusOnly,
            DiagnosticReason::UiaPatternMissing,
        ))
    }
}

#[allow(clippy::too_many_arguments)]
fn search_element_recursive(
    walker: &IUIAutomationTreeWalker,
    current: &IUIAutomationElement,
    expected_runtime_id: &str,
    depth: u32,
    max_depth: u32,
    visited: &mut usize,
    cancel_flag: &std::sync::atomic::AtomicBool,
    cancelled: &mut bool,
) -> Option<IUIAutomationElement> {
    if cancel_flag.load(std::sync::atomic::Ordering::SeqCst) {
        *cancelled = true;
        return None;
    }
    if *visited >= MAX_UIA_ELEMENTS_CAP {
        return None;
    }
    *visited += 1;

    if let Some(rid) = unsafe { extract_runtime_id(current) } {
        if rid == expected_runtime_id {
            return Some(current.clone());
        }
    }

    if depth >= max_depth {
        return None;
    }

    let mut child_opt = unsafe { walker.GetFirstChildElement(current).ok() };
    while let Some(child) = child_opt {
        if cancel_flag.load(std::sync::atomic::Ordering::SeqCst) {
            *cancelled = true;
            return None;
        }
        if let Some(found) = search_element_recursive(
            walker,
            &child,
            expected_runtime_id,
            depth + 1,
            max_depth,
            visited,
            cancel_flag,
            cancelled,
        ) {
            return Some(found);
        }
        if *cancelled {
            return None;
        }
        child_opt = unsafe { walker.GetNextSiblingElement(&child).ok() };
    }
    None
}

fn verify_live_uia_element_before_write(
    hwnd: HWND,
    elem: &IUIAutomationElement,
    expected_runtime_id: &str,
    expected_bounds: Option<&RectPx>,
    allow_password_target: bool,
    cancel_flag: &std::sync::atomic::AtomicBool,
) -> Result<RectPx, ProtocolError> {
    if cancel_flag.load(std::sync::atomic::Ordering::SeqCst) {
        return Err(ProtocolError::commit(
            ErrorCode::Cancelled,
            ErrorStage::Validating,
            InternalMutationDetail::FocusOnly,
            DiagnosticReason::CancelledDuringExecution,
        ));
    }

    let (fg_ok, fg_hwnd) = crate::focus::verify_foreground_relation(hwnd);
    if !fg_ok {
        return Err(ProtocolError::commit(
            ErrorCode::FocusFailed,
            ErrorStage::Validating,
            InternalMutationDetail::FocusOnly,
            DiagnosticReason::ForegroundVerificationFailed,
        )
        .with_hwnds(
            Some(crate::windows::hwnd_to_u64(hwnd)),
            if fg_hwnd.0.is_null() {
                None
            } else {
                Some(crate::windows::hwnd_to_u64(fg_hwnd))
            },
        ));
    }

    unsafe {
        let live_rid = extract_runtime_id(elem).unwrap_or_default();
        let live_enabled = elem.CurrentIsEnabled().ok().map(|b| b.as_bool());
        let live_offscreen = elem.CurrentIsOffscreen().ok().map(|b| b.as_bool());
        let live_pwd_raw = elem.CurrentIsPassword().ok().map(|b| b.as_bool());
        let control_type_id = elem
            .CurrentControlType()
            .unwrap_or(UIA_CustomControlTypeId);
        let name = elem
            .CurrentName()
            .ok()
            .map(|b| b.to_string())
            .unwrap_or_default();
        let effective_pwd = live_pwd_raw.map(|is_pwd| {
            evaluate_password_protection(
                Some(is_pwd),
                control_type_id == UIA_EditControlTypeId,
                &name,
            )
        });

        let rect = elem.CurrentBoundingRectangle().map_err(|e| {
            ProtocolError::commit(
                ErrorCode::StateTokenStale,
                ErrorStage::Validating,
                InternalMutationDetail::FocusOnly,
                DiagnosticReason::GeometryChanged,
            )
            .with_hresult(e.code().0)
        })?;
        let live_bounds = RectPx::from_ltrb(rect.left, rect.top, rect.right, rect.bottom);

        let ref_bounds = expected_bounds.unwrap_or(&live_bounds);
        verify_live_element_state_matches_snapshot(
            expected_runtime_id,
            ref_bounds,
            allow_password_target,
            &live_rid,
            &live_bounds,
            live_enabled,
            live_offscreen,
            effective_pwd,
        )?;

        if cancel_flag.load(std::sync::atomic::Ordering::SeqCst) {
            return Err(ProtocolError::commit(
                ErrorCode::Cancelled,
                ErrorStage::Validating,
                InternalMutationDetail::FocusOnly,
                DiagnosticReason::CancelledDuringExecution,
            ));
        }

        Ok(live_bounds)
    }
}

/// Resolves a live UIA element by `runtime_id` and verifies its current bounds/enabled/offscreen/password
/// state against the snapshot `expected_bounds` before commit-time pointer/keyboard delivery.
pub fn verify_live_element_for_commit_target(
    hwnd: HWND,
    runtime_id: &str,
    expected_bounds: &RectPx,
    allow_password_target: bool,
    cancel_flag: &std::sync::atomic::AtomicBool,
) -> Result<RectPx, ProtocolError> {
    let elem = find_live_element_by_runtime_id(hwnd, runtime_id, MAX_UIA_DEPTH_CAP, cancel_flag)?;
    verify_live_uia_element_before_write(
        hwnd,
        &elem,
        runtime_id,
        Some(expected_bounds),
        allow_password_target,
        cancel_flag,
    )
}

pub fn execute_uia_invoke(
    hwnd: HWND,
    runtime_id: &str,
    expected_bounds: Option<&RectPx>,
    cancel_flag: &std::sync::atomic::AtomicBool,
) -> Result<(), ProtocolError> {
    let elem = find_live_element_by_runtime_id(hwnd, runtime_id, MAX_UIA_DEPTH_CAP, cancel_flag)?;
    verify_live_uia_element_before_write(
        hwnd,
        &elem,
        runtime_id,
        expected_bounds,
        false,
        cancel_flag,
    )?;
    unsafe {
        if let Ok(unk) = elem.GetCurrentPattern(UIA_InvokePatternId) {
            if let Ok(inv) = unk.cast::<IUIAutomationInvokePattern>() {
                if cancel_flag.load(std::sync::atomic::Ordering::SeqCst) {
                    return Err(ProtocolError::commit(
                        ErrorCode::Cancelled,
                        ErrorStage::Validating,
                        InternalMutationDetail::FocusOnly,
                        DiagnosticReason::CancelledDuringExecution,
                    ));
                }
                inv.Invoke().map_err(|e| {
                    ProtocolError::commit(
                        ErrorCode::InputFailed,
                        ErrorStage::InputStarted,
                        InternalMutationDetail::UnknownOrPartial,
                        DiagnosticReason::UiaPatternMissing,
                    )
                    .with_hresult(e.code().0)
                })?;
                return Ok(());
            }
        }
        if let Ok(unk) = elem.GetCurrentPattern(UIA_TogglePatternId) {
            if let Ok(tog) = unk.cast::<IUIAutomationTogglePattern>() {
                if cancel_flag.load(std::sync::atomic::Ordering::SeqCst) {
                    return Err(ProtocolError::commit(
                        ErrorCode::Cancelled,
                        ErrorStage::Validating,
                        InternalMutationDetail::FocusOnly,
                        DiagnosticReason::CancelledDuringExecution,
                    ));
                }
                tog.Toggle().map_err(|e| {
                    ProtocolError::commit(
                        ErrorCode::InputFailed,
                        ErrorStage::InputStarted,
                        InternalMutationDetail::UnknownOrPartial,
                        DiagnosticReason::UiaPatternMissing,
                    )
                    .with_hresult(e.code().0)
                })?;
                return Ok(());
            }
        }
    }
    Err(ProtocolError::commit(
        ErrorCode::Unsupported,
        ErrorStage::Validating,
        InternalMutationDetail::FocusOnly,
        DiagnosticReason::UiaPatternMissing,
    ))
}

pub fn execute_uia_set_value(
    hwnd: HWND,
    runtime_id: &str,
    expected_bounds: Option<&RectPx>,
    value: &str,
    cancel_flag: &std::sync::atomic::AtomicBool,
) -> Result<(), ProtocolError> {
    let elem = find_live_element_by_runtime_id(hwnd, runtime_id, MAX_UIA_DEPTH_CAP, cancel_flag)?;
    verify_live_uia_element_before_write(
        hwnd,
        &elem,
        runtime_id,
        expected_bounds,
        false,
        cancel_flag,
    )?;
    unsafe {
        if let Ok(unk) = elem.GetCurrentPattern(UIA_ValuePatternId) {
            if let Ok(val_pat) = unk.cast::<IUIAutomationValuePattern>() {
                if cancel_flag.load(std::sync::atomic::Ordering::SeqCst) {
                    return Err(ProtocolError::commit(
                        ErrorCode::Cancelled,
                        ErrorStage::Validating,
                        InternalMutationDetail::FocusOnly,
                        DiagnosticReason::CancelledDuringExecution,
                    ));
                }
                let bstr = BSTR::from(value);
                val_pat.SetValue(&bstr).map_err(|e| {
                    ProtocolError::commit(
                        ErrorCode::InputFailed,
                        ErrorStage::InputStarted,
                        InternalMutationDetail::UnknownOrPartial,
                        DiagnosticReason::UiaPatternMissing,
                    )
                    .with_hresult(e.code().0)
                })?;
                return Ok(());
            }
        }
    }
    Err(ProtocolError::commit(
        ErrorCode::Unsupported,
        ErrorStage::Validating,
        InternalMutationDetail::FocusOnly,
        DiagnosticReason::UiaPatternMissing,
    ))
}

pub fn execute_uia_select(
    hwnd: HWND,
    runtime_id: &str,
    expected_bounds: Option<&RectPx>,
    cancel_flag: &std::sync::atomic::AtomicBool,
) -> Result<(), ProtocolError> {
    let elem = find_live_element_by_runtime_id(hwnd, runtime_id, MAX_UIA_DEPTH_CAP, cancel_flag)?;
    verify_live_uia_element_before_write(
        hwnd,
        &elem,
        runtime_id,
        expected_bounds,
        false,
        cancel_flag,
    )?;
    unsafe {
        if let Ok(unk) = elem.GetCurrentPattern(UIA_SelectionItemPatternId) {
            if let Ok(sel_pat) = unk.cast::<IUIAutomationSelectionItemPattern>() {
                if cancel_flag.load(std::sync::atomic::Ordering::SeqCst) {
                    return Err(ProtocolError::commit(
                        ErrorCode::Cancelled,
                        ErrorStage::Validating,
                        InternalMutationDetail::FocusOnly,
                        DiagnosticReason::CancelledDuringExecution,
                    ));
                }
                sel_pat.Select().map_err(|e| {
                    ProtocolError::commit(
                        ErrorCode::InputFailed,
                        ErrorStage::InputStarted,
                        InternalMutationDetail::UnknownOrPartial,
                        DiagnosticReason::UiaPatternMissing,
                    )
                    .with_hresult(e.code().0)
                })?;
                return Ok(());
            }
        }
    }
    Err(ProtocolError::commit(
        ErrorCode::Unsupported,
        ErrorStage::Validating,
        InternalMutationDetail::FocusOnly,
        DiagnosticReason::UiaPatternMissing,
    ))
}

pub fn execute_uia_focus(
    hwnd: HWND,
    runtime_id: &str,
    expected_bounds: Option<&RectPx>,
    allow_password_target: bool,
    cancel_flag: &std::sync::atomic::AtomicBool,
) -> Result<(), ProtocolError> {
    let elem = find_live_element_by_runtime_id(hwnd, runtime_id, MAX_UIA_DEPTH_CAP, cancel_flag)?;
    verify_live_uia_element_before_write(
        hwnd,
        &elem,
        runtime_id,
        expected_bounds,
        allow_password_target,
        cancel_flag,
    )?;
    unsafe {
        if cancel_flag.load(std::sync::atomic::Ordering::SeqCst) {
            return Err(ProtocolError::commit(
                ErrorCode::Cancelled,
                ErrorStage::Validating,
                InternalMutationDetail::FocusOnly,
                DiagnosticReason::CancelledDuringExecution,
            ));
        }
        elem.SetFocus().map_err(|e| {
            ProtocolError::commit(
                ErrorCode::FocusFailed,
                ErrorStage::Activating,
                InternalMutationDetail::FocusOnly,
                DiagnosticReason::ForegroundVerificationFailed,
            )
            .with_hresult(e.code().0)
        })?;
        // Verify that the element actually acquired keyboard focus or that the focused element matches runtime_id
        let focused_ok = elem
            .CurrentHasKeyboardFocus()
            .ok()
            .map(|b| b.as_bool())
            .unwrap_or(false)
            || create_uia_instance()
                .ok()
                .and_then(|u| u.GetFocusedElement().ok())
                .and_then(|fe| extract_runtime_id(&fe))
                .map(|fid| fid == runtime_id)
                .unwrap_or(false);
        if !focused_ok {
            return Err(ProtocolError::commit(
                ErrorCode::FocusFailed,
                ErrorStage::Activating,
                InternalMutationDetail::FocusOnly,
                DiagnosticReason::ForegroundVerificationFailed,
            ));
        }
        Ok(())
    }
}

pub fn compute_uia_tree_hash(elements: &[UiaElement]) -> String {
    let mut hasher = Sha256::new();
    for el in elements {
        hasher.update(el.index.to_string().as_bytes());
        hasher.update(b"|");
        hasher.update(el.runtime_id.as_bytes());
        hasher.update(b"|");
        hasher.update(el.control_type.as_bytes());
        hasher.update(b"|");
        hasher.update(el.name.as_bytes());
        hasher.update(b"|");
        hasher.update(
            format!(
                "{},{},{},{}",
                el.bounds.left, el.bounds.top, el.bounds.width, el.bounds.height
            )
            .as_bytes(),
        );
        hasher.update(b"\n");
    }
    hex::encode(hasher.finalize())
}

fn empty_tree_hash() -> String {
    compute_uia_tree_hash(&[])
}

fn truncate_str(s: &str, max_chars: usize) -> String {
    if s.chars().count() <= max_chars {
        s.to_string()
    } else {
        s.chars().take(max_chars).collect()
    }
}

fn map_control_type_name(id: UIA_CONTROLTYPE_ID) -> &'static str {
    match id {
        UIA_ButtonControlTypeId => "button",
        UIA_CalendarControlTypeId => "calendar",
        UIA_CheckBoxControlTypeId => "check_box",
        UIA_ComboBoxControlTypeId => "combo_box",
        UIA_EditControlTypeId => "edit",
        UIA_HyperlinkControlTypeId => "hyperlink",
        UIA_ImageControlTypeId => "image",
        UIA_ListItemControlTypeId => "list_item",
        UIA_ListControlTypeId => "list",
        UIA_MenuControlTypeId => "menu",
        UIA_MenuBarControlTypeId => "menu_bar",
        UIA_MenuItemControlTypeId => "menu_item",
        UIA_ProgressBarControlTypeId => "progress_bar",
        UIA_RadioButtonControlTypeId => "radio_button",
        UIA_ScrollBarControlTypeId => "scroll_bar",
        UIA_SliderControlTypeId => "slider",
        UIA_SpinnerControlTypeId => "spinner",
        UIA_StatusBarControlTypeId => "status_bar",
        UIA_TabControlTypeId => "tab",
        UIA_TabItemControlTypeId => "tab_item",
        UIA_TextControlTypeId => "text",
        UIA_ToolBarControlTypeId => "tool_bar",
        UIA_ToolTipControlTypeId => "tool_tip",
        UIA_TreeControlTypeId => "tree",
        UIA_TreeItemControlTypeId => "tree_item",
        UIA_CustomControlTypeId => "custom",
        UIA_GroupControlTypeId => "group",
        UIA_ThumbControlTypeId => "thumb",
        UIA_DataGridControlTypeId => "data_grid",
        UIA_DataItemControlTypeId => "data_item",
        UIA_DocumentControlTypeId => "document",
        UIA_SplitButtonControlTypeId => "split_button",
        UIA_WindowControlTypeId => "window",
        UIA_PaneControlTypeId => "pane",
        UIA_HeaderControlTypeId => "header",
        UIA_HeaderItemControlTypeId => "header_item",
        UIA_TableControlTypeId => "table",
        UIA_TitleBarControlTypeId => "title_bar",
        UIA_SeparatorControlTypeId => "separator",
        UIA_SemanticZoomControlTypeId => "semantic_zoom",
        UIA_AppBarControlTypeId => "app_bar",
        _ => "unknown",
    }
}
