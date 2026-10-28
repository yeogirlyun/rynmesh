use ryn_computer_use::snapshot::csprng_hex;
use ryn_computer_use::windows::{
    current_worker_pid, get_window_class_name, get_window_pid_tid, get_window_title, hwnd_to_u64,
    init_process_dpi_awareness, query_process_creation_time, query_process_executable_path,
    u64_to_hwnd,
};
use serde::{Deserialize, Serialize};
use std::io::{self, BufRead, Write};
use std::sync::mpsc::{self, TryRecvError};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};
use windows::core::{w, PCWSTR};
use windows::Win32::Foundation::{HINSTANCE, HWND, LPARAM, LRESULT, POINT, RECT, WPARAM};
use windows::Win32::Graphics::Gdi::{
    ClientToScreen, GetMonitorInfoW, MonitorFromRect, UpdateWindow, COLOR_WINDOW, HBRUSH,
    MONITORINFO, MONITOR_DEFAULTTONEAREST,
};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::System::Threading::GetCurrentThreadId;
use windows::Win32::UI::HiDpi::GetDpiForWindow;
use windows::Win32::UI::Shell::{DefSubclassProc, RemoveWindowSubclass, SetWindowSubclass};
use windows::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, DestroyWindow, DispatchMessageW, GetClientRect, GetCursorPos,
    GetParent, GetWindowRect, IsChild, IsWindow, PeekMessageW, RegisterClassW, SetWindowPos,
    ShowWindow, TranslateMessage, HMENU, MSG, PM_REMOVE, SWP_NOACTIVATE, SWP_NOSIZE, SWP_NOZORDER,
    SW_HIDE, SW_SHOWNOACTIVATE, WINDOW_EX_STYLE, WINDOW_STYLE, WM_CHAR, WM_COMMAND, WM_DESTROY,
    WM_KEYDOWN, WM_KEYUP, WM_KILLFOCUS, WM_LBUTTONDBLCLK, WM_LBUTTONDOWN, WM_LBUTTONUP,
    WM_MOUSEHWHEEL, WM_MOUSEMOVE, WM_MOUSEWHEEL, WM_RBUTTONDOWN, WM_RBUTTONUP, WM_SETFOCUS,
    WM_SETTEXT, WNDCLASSW, WS_BORDER, WS_CHILD, WS_OVERLAPPEDWINDOW, WS_TABSTOP, WS_VISIBLE,
};

const ES_AUTOHSCROLL_STYLE: u32 = 0x0080;
const ES_PASSWORD_STYLE: u32 = 0x0020;
const BS_PUSHBUTTON_STYLE: u32 = 0x0000;
const BS_NOTIFY_STYLE: u32 = 0x4000;

const ID_BUTTON: usize = 101;
const ID_PUBLIC_EDIT: usize = 102;
const ID_PASSWORD_EDIT: usize = 103;

const SUBCLASS_ID_BUTTON: usize = 1;
const SUBCLASS_ID_PUBLIC_EDIT: usize = 2;
const SUBCLASS_ID_PASSWORD_EDIT: usize = 3;

const BN_CLICKED_NOTIFY: u16 = 0;
const EN_SETFOCUS_NOTIFY: u16 = 0x0100;
const EN_KILLFOCUS_NOTIFY: u16 = 0x0200;
const EN_CHANGE_NOTIFY: u16 = 0x0300;

const MAX_TRACE_ENTRIES: usize = 256;

pub const ORIGINAL_PREFERRED_OUTER_X: i32 = 120;
pub const ORIGINAL_PREFERRED_OUTER_Y: i32 = 120;
pub const ORIGINAL_PREFERRED_OUTER_WIDTH: i32 = 460;
pub const ORIGINAL_PREFERRED_OUTER_HEIGHT: i32 = 320;
pub const DEFAULT_PLACEMENT_EDGE_MARGIN_PX: i32 = 24;
pub const DEFAULT_PLACEMENT_CURSOR_CLEARANCE_PX: i32 = 24;

pub fn is_confirmed_per_monitor_dpi_awareness(mode: &str) -> bool {
    matches!(mode, "per_monitor_aware_v2" | "per_monitor_aware")
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct FixturePointPx {
    pub x: i32,
    pub y: i32,
}

impl FixturePointPx {
    pub const fn new(x: i32, y: i32) -> Self {
        Self { x, y }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct FixtureRectPx {
    pub left: i32,
    pub top: i32,
    pub right: i32,
    pub bottom: i32,
    pub width: i32,
    pub height: i32,
}

impl FixtureRectPx {
    pub const fn from_xywh(x: i32, y: i32, width: i32, height: i32) -> Self {
        if width <= 0 || height <= 0 {
            return Self {
                left: x,
                top: y,
                right: x,
                bottom: y,
                width,
                height,
            };
        }
        let right64 = (x as i64) + (width as i64);
        let bottom64 = (y as i64) + (height as i64);
        if right64 < (i32::MIN as i64)
            || right64 > (i32::MAX as i64)
            || bottom64 < (i32::MIN as i64)
            || bottom64 > (i32::MAX as i64)
        {
            return Self {
                left: x,
                top: y,
                right: x,
                bottom: y,
                width: -1,
                height: -1,
            };
        }
        Self {
            left: x,
            top: y,
            right: right64 as i32,
            bottom: bottom64 as i32,
            width,
            height,
        }
    }

    pub const fn from_ltrb(left: i32, top: i32, right: i32, bottom: i32) -> Self {
        let span_x = (right as i64) - (left as i64);
        let span_y = (bottom as i64) - (top as i64);
        if span_x <= 0
            || span_y <= 0
            || span_x > (i32::MAX as i64)
            || span_y > (i32::MAX as i64)
        {
            return Self {
                left,
                top,
                right,
                bottom,
                width: -1,
                height: -1,
            };
        }
        Self {
            left,
            top,
            right,
            bottom,
            width: span_x as i32,
            height: span_y as i32,
        }
    }

    pub const fn is_valid_non_empty(&self) -> bool {
        let span_x = (self.right as i64) - (self.left as i64);
        let span_y = (self.bottom as i64) - (self.top as i64);
        self.width > 0
            && self.height > 0
            && self.right > self.left
            && self.bottom > self.top
            && span_x == (self.width as i64)
            && span_y == (self.height as i64)
    }

    pub const fn has_exact_size(&self, expected_width: i32, expected_height: i32) -> bool {
        expected_width > 0
            && expected_height > 0
            && self.is_valid_non_empty()
            && self.width == expected_width
            && self.height == expected_height
            && ((self.right as i64) - (self.left as i64)) == (expected_width as i64)
            && ((self.bottom as i64) - (self.top as i64)) == (expected_height as i64)
    }

    pub const fn is_fully_inside(&self, outer: &Self) -> bool {
        self.is_valid_non_empty()
            && outer.is_valid_non_empty()
            && self.left >= outer.left
            && self.top >= outer.top
            && self.right <= outer.right
            && self.bottom <= outer.bottom
    }

    pub const fn contains_rect(&self, inner: &Self) -> bool {
        inner.is_fully_inside(self)
    }

    pub const fn contains_point_inclusive(&self, pt: FixturePointPx) -> bool {
        self.is_valid_non_empty()
            && pt.x >= self.left
            && pt.x <= self.right
            && pt.y >= self.top
            && pt.y <= self.bottom
    }

    pub const fn contains_point(&self, pt: FixturePointPx) -> bool {
        self.contains_point_inclusive(pt)
    }

    pub const fn contains_point_with_margin(&self, pt: FixturePointPx, margin: i32) -> bool {
        if !self.is_valid_non_empty() {
            return false;
        }
        let m = if margin > 0 { margin as i64 } else { 0 };
        let px = pt.x as i64;
        let py = pt.y as i64;
        px >= (self.left as i64) - m
            && px <= (self.right as i64) + m
            && py >= (self.top as i64) - m
            && py <= (self.bottom as i64) + m
    }

    pub const fn contains_point_with_clearance(&self, pt: FixturePointPx, clearance: i32) -> bool {
        self.contains_point_with_margin(pt, clearance)
    }
}

/// Pure, deterministic cursor-avoiding initial window placement selector (no Win32 calls).
/// Given `preferred_outer` (`(120, 120, 460, 320)`), the target monitor's `work_area`
/// (resolved from the original preferred rect's monitor so monitor/DPI stay unchanged), and
/// the current read-only `cursor` position:
/// - Rejects any malformed, empty, reversed, inconsistent, or non-`460x320` `preferred_outer`
///   and any malformed, empty, reversed, or overflowing `work_area` using widened `i64` math.
/// - Preserves the exact `460x320` mathematical outer dimensions of `preferred_outer`.
/// - Requires the selected outer rect to lie 100% inside `work_area` (`is_fully_inside`).
/// - Requires the selected outer rect to exclude `cursor` (preferring candidates with at least
///   `cursor_clearance` margin around the outer rect, falling back to strict inclusive exclusion
///   only when `work_area` is too tight for the extra clearance).
/// - Evaluates a bounded list of candidate positions in `work_area` (starting with `preferred_outer`
///   itself, followed by inset corners/midpoints and exact work-area corners).
/// - Returns `Err` if dimensions are invalid, `work_area` is smaller than `preferred_outer`, or
///   no candidate inside `work_area` can exclude `cursor`.
pub fn select_cursor_avoiding_window_placement(
    preferred_outer: FixtureRectPx,
    work_area: FixtureRectPx,
    cursor: FixturePointPx,
    edge_margin: i32,
    cursor_clearance: i32,
) -> Result<(FixtureRectPx, &'static str), &'static str> {
    if !preferred_outer.is_valid_non_empty() {
        return Err("invalid_preferred_outer_rect");
    }
    if !preferred_outer.has_exact_size(
        ORIGINAL_PREFERRED_OUTER_WIDTH,
        ORIGINAL_PREFERRED_OUTER_HEIGHT,
    ) {
        return Err("unexpected_preferred_outer_dimensions");
    }
    if !work_area.is_valid_non_empty() {
        return Err("invalid_monitor_work_area");
    }
    let w = preferred_outer.width;
    let h = preferred_outer.height;
    let w64 = w as i64;
    let h64 = h as i64;
    let wa_left = work_area.left as i64;
    let wa_top = work_area.top as i64;
    let wa_right = work_area.right as i64;
    let wa_bottom = work_area.bottom as i64;
    let wa_w = work_area.width as i64;
    let wa_h = work_area.height as i64;

    if wa_w < w64 || wa_h < h64 {
        return Err("work_area_smaller_than_window");
    }

    let m64 = if edge_margin > 0 {
        edge_margin as i64
    } else {
        0
    };
    let can_inset_x = wa_w >= w64 + m64 * 2;
    let can_inset_y = wa_h >= h64 + m64 * 2;

    let inset_left = if can_inset_x { wa_left + m64 } else { wa_left };
    let inset_top = if can_inset_y { wa_top + m64 } else { wa_top };
    let inset_right_x = if can_inset_x {
        wa_right - w64 - m64
    } else {
        wa_right - w64
    };
    let inset_bottom_y = if can_inset_y {
        wa_bottom - h64 - m64
    } else {
        wa_bottom - h64
    };

    let mid_x = wa_left + (wa_w - w64) / 2;
    let mid_y = wa_top + (wa_h - h64) / 2;

    let exact_left = wa_left;
    let exact_top = wa_top;
    let exact_right_x = wa_right - w64;
    let exact_bottom_y = wa_bottom - h64;

    let candidates: [(i64, i64, &'static str); 14] = [
        (
            preferred_outer.left as i64,
            preferred_outer.top as i64,
            "preferred_original",
        ),
        (inset_right_x, inset_top, "work_area_top_right_margin"),
        (inset_left, inset_bottom_y, "work_area_bottom_left_margin"),
        (
            inset_right_x,
            inset_bottom_y,
            "work_area_bottom_right_margin",
        ),
        (inset_left, inset_top, "work_area_top_left_margin"),
        (mid_x, inset_top, "work_area_top_center_margin"),
        (mid_x, inset_bottom_y, "work_area_bottom_center_margin"),
        (inset_left, mid_y, "work_area_mid_left_margin"),
        (inset_right_x, mid_y, "work_area_mid_right_margin"),
        (mid_x, mid_y, "work_area_center"),
        (exact_right_x, exact_top, "work_area_top_right_exact"),
        (exact_left, exact_bottom_y, "work_area_bottom_left_exact"),
        (exact_right_x, exact_bottom_y, "work_area_bottom_right_exact"),
        (exact_left, exact_top, "work_area_top_left_exact"),
    ];

    let clearance = cursor_clearance.max(0);
    for (cx64, cy64, label) in candidates {
        let (Ok(cx), Ok(cy), Ok(_), Ok(_)) = (
            i32::try_from(cx64),
            i32::try_from(cy64),
            i32::try_from(cx64 + w64),
            i32::try_from(cy64 + h64),
        ) else {
            continue;
        };
        let rect = FixtureRectPx::from_xywh(cx, cy, w, h);
        if rect.has_exact_size(
            ORIGINAL_PREFERRED_OUTER_WIDTH,
            ORIGINAL_PREFERRED_OUTER_HEIGHT,
        ) && rect.is_fully_inside(&work_area)
            && !rect.contains_point_with_margin(cursor, clearance)
        {
            return Ok((rect, label));
        }
    }

    for (cx64, cy64, label) in candidates {
        let (Ok(cx), Ok(cy), Ok(_), Ok(_)) = (
            i32::try_from(cx64),
            i32::try_from(cy64),
            i32::try_from(cx64 + w64),
            i32::try_from(cy64 + h64),
        ) else {
            continue;
        };
        let rect = FixtureRectPx::from_xywh(cx, cy, w, h);
        if rect.has_exact_size(
            ORIGINAL_PREFERRED_OUTER_WIDTH,
            ORIGINAL_PREFERRED_OUTER_HEIGHT,
        ) && rect.is_fully_inside(&work_area)
            && !rect.contains_point_inclusive(cursor)
        {
            return Ok((rect, label));
        }
    }

    Err("no_cursor_free_visible_placement_in_work_area")
}

pub fn validate_stage_window_and_cursor_invariants(
    stage: &str,
    dpi_awareness: &str,
    window_dpi: u32,
    selected_monitor_work_area: FixtureRectPx,
    requested_outer_rect: FixtureRectPx,
    actual_window_rect: FixtureRectPx,
    actual_client_bounds: FixtureRectPx,
    cursor: FixturePointPx,
) -> Result<(), String> {
    if !is_confirmed_per_monitor_dpi_awareness(dpi_awareness) {
        return Err(format!(
            "{stage}_unconfirmed_dpi_awareness_mode: {dpi_awareness}"
        ));
    }
    if window_dpi == 0 {
        return Err(format!("{stage}_window_dpi_zero"));
    }
    if !selected_monitor_work_area.is_valid_non_empty() {
        return Err(format!("{stage}_invalid_selected_monitor_work_area"));
    }
    if !requested_outer_rect.has_exact_size(
        ORIGINAL_PREFERRED_OUTER_WIDTH,
        ORIGINAL_PREFERRED_OUTER_HEIGHT,
    ) {
        return Err(format!(
            "{stage}_requested_outer_rect_size_not_460x320: {:?}",
            requested_outer_rect
        ));
    }
    if !selected_monitor_work_area.contains_rect(&requested_outer_rect) {
        return Err(format!(
            "{stage}_requested_outer_rect_outside_work_area"
        ));
    }
    if !actual_window_rect.has_exact_size(
        ORIGINAL_PREFERRED_OUTER_WIDTH,
        ORIGINAL_PREFERRED_OUTER_HEIGHT,
    ) {
        return Err(format!(
            "{stage}_actual_window_rect_size_not_460x320: {:?}",
            actual_window_rect
        ));
    }
    if !selected_monitor_work_area.contains_rect(&actual_window_rect) {
        return Err(format!(
            "{stage}_actual_window_rect_outside_work_area"
        ));
    }
    if !actual_client_bounds.is_valid_non_empty()
        || !actual_window_rect.contains_rect(&actual_client_bounds)
    {
        return Err(format!(
            "{stage}_actual_client_bounds_invalid_or_outside_window"
        ));
    }
    if requested_outer_rect.contains_point_inclusive(cursor) {
        return Err(format!(
            "{stage}_cursor_inside_requested_outer_rect"
        ));
    }
    if actual_window_rect.contains_point_inclusive(cursor) {
        return Err(format!(
            "{stage}_cursor_inside_actual_window_rect"
        ));
    }
    Ok(())
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct FixturePlacementDiagnostics {
    pub dpi_awareness: String,
    pub pre_show_window_dpi: u32,
    pub window_dpi: u32,
    pub placement_candidate: String,
    pub original_preferred_rect: FixtureRectPx,
    pub creation_cursor: FixturePointPx,
    pub selected_monitor_rect: FixtureRectPx,
    pub selected_monitor_work_area: FixtureRectPx,
    pub requested_outer_rect: FixtureRectPx,
    pub pre_show_window_rect: FixtureRectPx,
    pub pre_show_client_bounds: FixtureRectPx,
    pub pre_show_cursor: FixturePointPx,
    pub actual_window_rect: FixtureRectPx,
    pub actual_client_bounds: FixtureRectPx,
    pub ready_cursor: FixturePointPx,
}

/// Optional raw diagnostic payload recorded on `WM_MOUSEMOVE` trace entries (`lParam` client/screen
/// coordinates, `wParam` virtual key flags, and read-only `GetCursorPos` at receipt).
/// Strictly observational; never used to infer synthetic `SendInput` vs OS/user pointer messages.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct FixtureMouseMoveTraceDetail {
    pub wparam_flags: usize,
    pub lparam_raw: isize,
    pub client_x: i32,
    pub client_y: i32,
    pub screen_x: Option<i32>,
    pub screen_y: Option<i32>,
    pub cursor_x: Option<i32>,
    pub cursor_y: Option<i32>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FixtureLifecyclePhase {
    CreateMain,
    CreateButton,
    CreatePublic,
    CreatePassword,
    Subclass,
    Show,
    Pump,
    Ready,
}

impl Default for FixtureLifecyclePhase {
    fn default() -> Self {
        Self::CreateMain
    }
}

impl FixtureLifecyclePhase {
    pub const fn is_setup_phase(self) -> bool {
        !matches!(self, Self::Ready)
    }
}

/// Safe bounded message/phase trace entry.
/// Records ONLY sequence number, explicit lifecycle phase, source hook, control role, category,
/// Win32 message name, control ID, notification code, counter field name, new count, and
/// optional `WM_MOUSEMOVE` coordinate diagnostics.
/// NEVER records password values, characters, key codes, or private process/user contents.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct FixtureTraceEntry {
    pub seq: u64,
    pub phase: FixtureLifecyclePhase,
    pub is_setup_phase: bool,
    pub source: String,
    pub control_role: String,
    pub category: String,
    pub message: String,
    pub control_id: usize,
    pub notify_code: Option<String>,
    pub field_incremented: String,
    pub new_field_count: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mouse_move_detail: Option<FixtureMouseMoveTraceDetail>,
}

/// All-lifetime counters from process start.
/// Counters are strictly monotonic for the entire lifetime of the fixture process and are
/// NEVER reset, cleared, or subtracted.
/// Distinguishes canonical physical wheel inputs (`mouse_wheel_*_count` / `mouse_wheel_*_delta_sum`)
/// from raw WndProc/SubclassProc message deliveries (`raw_mouse_wheel_*_message_count` / `raw_mouse_wheel_*_delta_sum`)
/// and child->parent `DefSubclassProc` forwarded deliveries (`forwarded_mouse_wheel_*_count` / `forwarded_mouse_wheel_*_delta_sum`).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct FixtureCounters {
    pub main_focus_gained_count: u64,
    pub main_focus_lost_count: u64,
    pub button_click_count: u64,
    pub button_lbutton_down_count: u64,
    pub button_lbutton_up_count: u64,
    pub button_lbutton_dblclk_count: u64,
    pub button_rbutton_down_count: u64,
    pub button_rbutton_up_count: u64,
    pub button_focus_gained_count: u64,
    pub button_focus_lost_count: u64,
    pub public_edit_focus_gained_count: u64,
    pub public_edit_focus_lost_count: u64,
    pub public_edit_lbutton_down_count: u64,
    pub public_edit_lbutton_up_count: u64,
    pub public_edit_keydown_count: u64,
    pub public_edit_keyup_count: u64,
    pub public_edit_char_count: u64,
    /// Raw all-lifetime EN_CHANGE count on public edit (`setup_public_edit_change_count + runtime_public_edit_change_count`).
    pub public_edit_change_count: u64,
    /// Synchronous EN_CHANGE notifications received during window/control setup before `Ready`.
    pub setup_public_edit_change_count: u64,
    /// EN_CHANGE notifications received after `Ready` was entered.
    pub runtime_public_edit_change_count: u64,
    pub public_edit_settext_count: u64,
    pub public_edit_text: String,
    pub public_edit_chars: Vec<u32>,
    pub public_edit_last_vk: u32,
    pub mouse_move_count: u64,
    /// Canonical physical vertical wheel count (each distinct wheel input counted once, even when
    /// a child control forwards `WM_MOUSEWHEEL` to `main_wnd_proc` via `DefSubclassProc`).
    pub mouse_wheel_vertical_count: u64,
    /// Canonical physical vertical wheel delta sum.
    pub mouse_wheel_vertical_delta_sum: i64,
    /// Raw `WM_MOUSEWHEEL` message deliveries observed across child subclass and `main_wnd_proc`.
    #[serde(default)]
    pub raw_mouse_wheel_vertical_message_count: u64,
    /// Raw `WM_MOUSEWHEEL` delta sum across all child and parent message deliveries.
    #[serde(default)]
    pub raw_mouse_wheel_vertical_delta_sum: i64,
    /// `WM_MOUSEWHEEL` deliveries at `main_wnd_proc` proven by scoped same-thread forwarding context
    /// to be synchronously forwarded from a child `DefSubclassProc`.
    #[serde(default)]
    pub forwarded_mouse_wheel_vertical_count: u64,
    /// Delta sum of child->parent forwarded `WM_MOUSEWHEEL` deliveries at `main_wnd_proc`.
    #[serde(default)]
    pub forwarded_mouse_wheel_vertical_delta_sum: i64,
    /// Canonical physical horizontal wheel count.
    pub mouse_wheel_horizontal_count: u64,
    /// Canonical physical horizontal wheel delta sum.
    pub mouse_wheel_horizontal_delta_sum: i64,
    /// Raw `WM_MOUSEHWHEEL` message deliveries observed across child subclass and `main_wnd_proc`.
    #[serde(default)]
    pub raw_mouse_wheel_horizontal_message_count: u64,
    /// Raw `WM_MOUSEHWHEEL` delta sum across all child and parent message deliveries.
    #[serde(default)]
    pub raw_mouse_wheel_horizontal_delta_sum: i64,
    /// `WM_MOUSEHWHEEL` deliveries at `main_wnd_proc` proven by scoped same-thread forwarding context
    /// to be synchronously forwarded from a child `DefSubclassProc`.
    #[serde(default)]
    pub forwarded_mouse_wheel_horizontal_count: u64,
    /// Delta sum of child->parent forwarded `WM_MOUSEHWHEEL` deliveries at `main_wnd_proc`.
    #[serde(default)]
    pub forwarded_mouse_wheel_horizontal_delta_sum: i64,
    // Password edit only records event counts, NEVER any character/text contents!
    pub password_edit_focus_gained_count: u64,
    pub password_edit_focus_lost_count: u64,
    pub password_edit_lbutton_down_count: u64,
    pub password_edit_lbutton_up_count: u64,
    pub password_edit_keydown_count: u64,
    pub password_edit_keyup_count: u64,
    pub password_edit_char_count: u64,
    /// Raw all-lifetime EN_CHANGE count on password edit (`setup_password_edit_change_count + runtime_password_edit_change_count`).
    pub password_edit_change_count: u64,
    /// Synchronous EN_CHANGE notifications received on password edit during setup before `Ready`.
    pub setup_password_edit_change_count: u64,
    /// EN_CHANGE notifications received on password edit after `Ready` was entered.
    pub runtime_password_edit_change_count: u64,
    pub password_edit_settext_count: u64,
}

/// Active same-thread child-to-parent wheel forwarding frame pushed around `DefSubclassProc`.
/// Matches at most one synchronous parent `main_wnd_proc` delivery with identical
/// `(thread_id, msg, wparam, lparam)` while `DefSubclassProc` is on the call stack.
#[derive(Debug, Clone, PartialEq, Eq)]
struct ActiveWheelForwardFrame {
    token: u64,
    thread_id: u32,
    child_control_id: usize,
    msg: u32,
    wparam: usize,
    lparam: isize,
    parent_forwarded_consumed: bool,
}

#[derive(Debug, Clone)]
struct FixtureRuntimeState {
    phase: FixtureLifecyclePhase,
    next_seq: u64,
    next_forwarding_token: u64,
    active_wheel_forwarding_stack: Vec<ActiveWheelForwardFrame>,
    counters: FixtureCounters,
    setup_counters_frozen: FixtureCounters,
    trace: Vec<FixtureTraceEntry>,
    trace_dropped_count: u64,
}

impl FixtureRuntimeState {
    fn new() -> Self {
        let initial_counters = FixtureCounters {
            public_edit_text: "InitialPublicValue".to_string(),
            ..Default::default()
        };
        Self {
            phase: FixtureLifecyclePhase::CreateMain,
            next_seq: 1,
            next_forwarding_token: 1,
            active_wheel_forwarding_stack: Vec::with_capacity(4),
            setup_counters_frozen: initial_counters.clone(),
            counters: initial_counters,
            trace: Vec::with_capacity(32),
            trace_dropped_count: 0,
        }
    }

    fn record_trace(
        &mut self,
        source: &str,
        control_role: &str,
        category: &str,
        message: &str,
        control_id: usize,
        notify_code: Option<&str>,
        field_incremented: &str,
        new_field_count: u64,
    ) {
        self.record_trace_with_mouse_move_detail(
            source,
            control_role,
            category,
            message,
            control_id,
            notify_code,
            field_incremented,
            new_field_count,
            None,
        );
    }

    fn record_trace_with_mouse_move_detail(
        &mut self,
        source: &str,
        control_role: &str,
        category: &str,
        message: &str,
        control_id: usize,
        notify_code: Option<&str>,
        field_incremented: &str,
        new_field_count: u64,
        mouse_move_detail: Option<FixtureMouseMoveTraceDetail>,
    ) {
        let seq = self.next_seq;
        self.next_seq = self.next_seq.saturating_add(1);
        if self.trace.len() < MAX_TRACE_ENTRIES {
            self.trace.push(FixtureTraceEntry {
                seq,
                phase: self.phase,
                is_setup_phase: self.phase.is_setup_phase(),
                source: source.to_string(),
                control_role: control_role.to_string(),
                category: category.to_string(),
                message: message.to_string(),
                control_id,
                notify_code: notify_code.map(|s| s.to_string()),
                field_incremented: field_incremented.to_string(),
                new_field_count,
                mouse_move_detail,
            });
        } else {
            self.trace_dropped_count = self.trace_dropped_count.saturating_add(1);
        }
    }

    fn record_mouse_move_trace(
        &mut self,
        source: &str,
        control_role: &str,
        control_id: usize,
        detail: Option<FixtureMouseMoveTraceDetail>,
    ) {
        self.counters.mouse_move_count += 1;
        let v = self.counters.mouse_move_count;
        self.record_trace_with_mouse_move_detail(
            source,
            control_role,
            "mouse_move",
            "WM_MOUSEMOVE",
            control_id,
            None,
            "mouseMoveCount",
            v,
            detail,
        );
    }

    fn on_child_wheel_message(
        &mut self,
        thread_id: u32,
        control_role: &str,
        control_id: usize,
        msg: u32,
        wparam: usize,
        lparam: isize,
    ) -> u64 {
        let delta = ((wparam >> 16) & 0xFFFF) as i16 as i64;
        if msg == WM_MOUSEWHEEL {
            self.counters.raw_mouse_wheel_vertical_message_count += 1;
            self.counters.raw_mouse_wheel_vertical_delta_sum += delta;
            self.counters.mouse_wheel_vertical_count += 1;
            self.counters.mouse_wheel_vertical_delta_sum += delta;
            let v = self.counters.mouse_wheel_vertical_count;
            self.record_trace(
                "child_subclass",
                control_role,
                "mouse_wheel",
                "WM_MOUSEWHEEL",
                control_id,
                None,
                "mouseWheelVerticalCount",
                v,
            );
        } else if msg == WM_MOUSEHWHEEL {
            self.counters.raw_mouse_wheel_horizontal_message_count += 1;
            self.counters.raw_mouse_wheel_horizontal_delta_sum += delta;
            self.counters.mouse_wheel_horizontal_count += 1;
            self.counters.mouse_wheel_horizontal_delta_sum += delta;
            let v = self.counters.mouse_wheel_horizontal_count;
            self.record_trace(
                "child_subclass",
                control_role,
                "mouse_wheel",
                "WM_MOUSEHWHEEL",
                control_id,
                None,
                "mouseWheelHorizontalCount",
                v,
            );
        }

        let token = self.next_forwarding_token;
        self.next_forwarding_token = self.next_forwarding_token.saturating_add(1);
        self.active_wheel_forwarding_stack
            .push(ActiveWheelForwardFrame {
                token,
                thread_id,
                child_control_id: control_id,
                msg,
                wparam,
                lparam,
                parent_forwarded_consumed: false,
            });
        token
    }

    fn pop_wheel_forwarding_token(&mut self, token: u64) {
        if let Some(pos) = self
            .active_wheel_forwarding_stack
            .iter()
            .rposition(|f| f.token == token)
        {
            self.active_wheel_forwarding_stack.remove(pos);
        }
    }

    fn on_main_wheel_message(&mut self, thread_id: u32, msg: u32, wparam: usize, lparam: isize) {
        let delta = ((wparam >> 16) & 0xFFFF) as i16 as i64;
        // Only the active TOP frame on the forwarding stack can classify its own synchronous
        // child->parent wheel forward. An older unconsumed frame underneath a nested different
        // top frame must never be matched or suppress an independent parent wheel message.
        let is_forwarded = match self.active_wheel_forwarding_stack.last_mut() {
            Some(top_frame)
                if top_frame.thread_id == thread_id
                    && top_frame.msg == msg
                    && top_frame.wparam == wparam
                    && top_frame.lparam == lparam
                    && !top_frame.parent_forwarded_consumed =>
            {
                let _ = top_frame.child_control_id;
                top_frame.parent_forwarded_consumed = true;
                true
            }
            _ => false,
        };

        if msg == WM_MOUSEWHEEL {
            self.counters.raw_mouse_wheel_vertical_message_count += 1;
            self.counters.raw_mouse_wheel_vertical_delta_sum += delta;
            if is_forwarded {
                self.counters.forwarded_mouse_wheel_vertical_count += 1;
                self.counters.forwarded_mouse_wheel_vertical_delta_sum += delta;
                let raw_v = self.counters.raw_mouse_wheel_vertical_message_count;
                self.record_trace(
                    "main_wnd_proc",
                    "main_window",
                    "mouse_wheel",
                    "WM_MOUSEWHEEL",
                    0,
                    Some("forwarded_from_child"),
                    "rawMouseWheelVerticalMessageCount",
                    raw_v,
                );
            } else {
                self.counters.mouse_wheel_vertical_count += 1;
                self.counters.mouse_wheel_vertical_delta_sum += delta;
                let v = self.counters.mouse_wheel_vertical_count;
                self.record_trace(
                    "main_wnd_proc",
                    "main_window",
                    "mouse_wheel",
                    "WM_MOUSEWHEEL",
                    0,
                    None,
                    "mouseWheelVerticalCount",
                    v,
                );
            }
        } else if msg == WM_MOUSEHWHEEL {
            self.counters.raw_mouse_wheel_horizontal_message_count += 1;
            self.counters.raw_mouse_wheel_horizontal_delta_sum += delta;
            if is_forwarded {
                self.counters.forwarded_mouse_wheel_horizontal_count += 1;
                self.counters.forwarded_mouse_wheel_horizontal_delta_sum += delta;
                let raw_h = self.counters.raw_mouse_wheel_horizontal_message_count;
                self.record_trace(
                    "main_wnd_proc",
                    "main_window",
                    "mouse_wheel",
                    "WM_MOUSEHWHEEL",
                    0,
                    Some("forwarded_from_child"),
                    "rawMouseWheelHorizontalMessageCount",
                    raw_h,
                );
            } else {
                self.counters.mouse_wheel_horizontal_count += 1;
                self.counters.mouse_wheel_horizontal_delta_sum += delta;
                let v = self.counters.mouse_wheel_horizontal_count;
                self.record_trace(
                    "main_wnd_proc",
                    "main_window",
                    "mouse_wheel",
                    "WM_MOUSEHWHEEL",
                    0,
                    None,
                    "mouseWheelHorizontalCount",
                    v,
                );
            }
        }
    }

    fn on_main_mousemove(&mut self) {
        self.record_mouse_move_trace("main_wnd_proc", "main_window", 0, None);
    }
}

static FIXTURE_STATE: OnceLock<Mutex<FixtureRuntimeState>> = OnceLock::new();

fn state_lock() -> &'static Mutex<FixtureRuntimeState> {
    FIXTURE_STATE.get_or_init(|| Mutex::new(FixtureRuntimeState::new()))
}

struct ScopedWheelForwardingGuard<'a> {
    state_mutex: &'a Mutex<FixtureRuntimeState>,
    token: u64,
}

impl<'a> ScopedWheelForwardingGuard<'a> {
    const fn new(state_mutex: &'a Mutex<FixtureRuntimeState>, token: u64) -> Self {
        Self { state_mutex, token }
    }
}

impl Drop for ScopedWheelForwardingGuard<'_> {
    fn drop(&mut self) {
        let mut st = self.state_mutex.lock().unwrap_or_else(|e| e.into_inner());
        st.pop_wheel_forwarding_token(self.token);
    }
}

fn with_scoped_child_wheel_forwarding<F, R>(
    state_mutex: &Mutex<FixtureRuntimeState>,
    thread_id: u32,
    control_role: &str,
    control_id: usize,
    msg: u32,
    wparam: usize,
    lparam: isize,
    forward_fn: F,
) -> R
where
    F: FnOnce() -> R,
{
    let token = if let Ok(mut st) = state_mutex.lock() {
        Some(st.on_child_wheel_message(
            thread_id,
            control_role,
            control_id,
            msg,
            wparam,
            lparam,
        ))
    } else {
        None
    };
    let _guard = token.map(|t| ScopedWheelForwardingGuard::new(state_mutex, t));
    forward_fn()
}

unsafe fn handle_child_wheel_and_forward(
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
    control_role: &str,
    control_id: usize,
) -> LRESULT {
    let tid = GetCurrentThreadId();
    with_scoped_child_wheel_forwarding(
        state_lock(),
        tid,
        control_role,
        control_id,
        msg,
        wparam.0,
        lparam.0,
        || DefSubclassProc(hwnd, msg, wparam, lparam),
    )
}

fn set_lifecycle_phase(phase: FixtureLifecyclePhase) {
    if let Ok(mut st) = state_lock().lock() {
        st.phase = phase;
        if phase == FixtureLifecyclePhase::Ready {
            st.setup_counters_frozen = st.counters.clone();
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct FixtureChildInfo {
    pub role: String,
    pub hwnd: String,
    pub parent_hwnd: String,
    pub control_id: usize,
    pub class_name: String,
    pub control_type: String,
    pub is_child_of_main: bool,
    pub password: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct FixtureReadyReport {
    pub status: String,
    pub phase: FixtureLifecyclePhase,
    pub nonce: String,
    pub pid: u32,
    pub process_started: String,
    pub executable_path: Option<String>,
    pub hwnd: String,
    pub title: String,
    pub class_name: String,
    pub placement: FixturePlacementDiagnostics,
    pub children: Vec<FixtureChildInfo>,
    pub setup_counters: FixtureCounters,
    pub counters: FixtureCounters,
    pub trace: Vec<FixtureTraceEntry>,
    pub trace_dropped_count: u64,
}

unsafe fn capture_mouse_move_trace_detail(
    hwnd: HWND,
    wparam: WPARAM,
    lparam: LPARAM,
) -> FixtureMouseMoveTraceDetail {
    let client_x = (lparam.0 & 0xFFFF) as i16 as i32;
    let client_y = ((lparam.0 >> 16) & 0xFFFF) as i16 as i32;
    let mut screen_pt = POINT {
        x: client_x,
        y: client_y,
    };
    let (screen_x, screen_y) = if ClientToScreen(hwnd, &mut screen_pt).as_bool() {
        (Some(screen_pt.x), Some(screen_pt.y))
    } else {
        (None, None)
    };
    let mut cursor_pt = POINT::default();
    let (cursor_x, cursor_y) = if GetCursorPos(&mut cursor_pt).is_ok() {
        (Some(cursor_pt.x), Some(cursor_pt.y))
    } else {
        (None, None)
    };
    FixtureMouseMoveTraceDetail {
        wparam_flags: wparam.0,
        lparam_raw: lparam.0,
        client_x,
        client_y,
        screen_x,
        screen_y,
        cursor_x,
        cursor_y,
    }
}

#[derive(Debug, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
enum FixtureControlCommand {
    ReadCounters {
        #[serde(default)]
        req_id: Option<String>,
    },
    MoveChild {
        #[serde(default)]
        req_id: Option<String>,
        role: String,
        x: i32,
        y: i32,
    },
    MoveMainWindow {
        #[serde(default)]
        req_id: Option<String>,
        x: i32,
        y: i32,
    },
    SpawnOwnedModal {
        #[serde(default)]
        req_id: Option<String>,
        slot: String,
        x: i32,
        y: i32,
        width: i32,
        height: i32,
    },
    CloseOwnedModal {
        #[serde(default)]
        req_id: Option<String>,
        slot: String,
    },
    Shutdown {
        #[serde(default)]
        req_id: Option<String>,
    },
}

unsafe extern "system" fn main_wnd_proc(
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    match msg {
        WM_SETFOCUS => {
            if let Ok(mut st) = state_lock().lock() {
                st.counters.main_focus_gained_count += 1;
                let v = st.counters.main_focus_gained_count;
                st.record_trace(
                    "main_wnd_proc",
                    "main_window",
                    "focus",
                    "WM_SETFOCUS",
                    0,
                    None,
                    "mainFocusGainedCount",
                    v,
                );
            }
        }
        WM_KILLFOCUS => {
            if let Ok(mut st) = state_lock().lock() {
                st.counters.main_focus_lost_count += 1;
                let v = st.counters.main_focus_lost_count;
                st.record_trace(
                    "main_wnd_proc",
                    "main_window",
                    "focus",
                    "WM_KILLFOCUS",
                    0,
                    None,
                    "mainFocusLostCount",
                    v,
                );
            }
        }
        WM_MOUSEMOVE => {
            let detail = capture_mouse_move_trace_detail(hwnd, wparam, lparam);
            if let Ok(mut st) = state_lock().lock() {
                st.record_mouse_move_trace("main_wnd_proc", "main_window", 0, Some(detail));
            }
        }
        WM_MOUSEWHEEL | WM_MOUSEHWHEEL => {
            let tid = GetCurrentThreadId();
            if let Ok(mut st) = state_lock().lock() {
                st.on_main_wheel_message(tid, msg, wparam.0, lparam.0);
            }
        }
        WM_COMMAND => {
            let control_id = wparam.0 & 0xFFFF;
            let notify_code = ((wparam.0 >> 16) & 0xFFFF) as u16;
            let child_hwnd = HWND(lparam.0 as *mut core::ffi::c_void);
            if control_id == ID_BUTTON && notify_code == BN_CLICKED_NOTIFY {
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.button_click_count += 1;
                    let v = st.counters.button_click_count;
                    st.record_trace(
                        "main_wnd_proc",
                        "button",
                        "command_notify",
                        "WM_COMMAND",
                        ID_BUTTON,
                        Some("BN_CLICKED"),
                        "buttonClickCount",
                        v,
                    );
                }
            } else if control_id == ID_PUBLIC_EDIT {
                match notify_code {
                    EN_CHANGE_NOTIFY => {
                        let current_text = get_window_title(child_hwnd);
                        if let Ok(mut st) = state_lock().lock() {
                            st.counters.public_edit_change_count += 1;
                            let is_setup = st.phase.is_setup_phase();
                            if is_setup {
                                st.counters.setup_public_edit_change_count += 1;
                            } else {
                                st.counters.runtime_public_edit_change_count += 1;
                            }
                            st.counters.public_edit_text = current_text;
                            let v = st.counters.public_edit_change_count;
                            let category = if is_setup {
                                "setup_initial_text_notify"
                            } else {
                                "text_mutation"
                            };
                            st.record_trace(
                                "main_wnd_proc",
                                "public_edit",
                                category,
                                "WM_COMMAND",
                                ID_PUBLIC_EDIT,
                                Some("EN_CHANGE"),
                                "publicEditChangeCount",
                                v,
                            );
                        }
                    }
                    EN_SETFOCUS_NOTIFY => {
                        if let Ok(mut st) = state_lock().lock() {
                            st.counters.public_edit_focus_gained_count += 1;
                            let v = st.counters.public_edit_focus_gained_count;
                            st.record_trace(
                                "main_wnd_proc",
                                "public_edit",
                                "focus",
                                "WM_COMMAND",
                                ID_PUBLIC_EDIT,
                                Some("EN_SETFOCUS"),
                                "publicEditFocusGainedCount",
                                v,
                            );
                        }
                    }
                    EN_KILLFOCUS_NOTIFY => {
                        if let Ok(mut st) = state_lock().lock() {
                            st.counters.public_edit_focus_lost_count += 1;
                            let v = st.counters.public_edit_focus_lost_count;
                            st.record_trace(
                                "main_wnd_proc",
                                "public_edit",
                                "focus",
                                "WM_COMMAND",
                                ID_PUBLIC_EDIT,
                                Some("EN_KILLFOCUS"),
                                "publicEditFocusLostCount",
                                v,
                            );
                        }
                    }
                    _ => {}
                }
            } else if control_id == ID_PASSWORD_EDIT {
                // Strictly record counts and safe trace metadata ONLY; never read password control text!
                match notify_code {
                    EN_CHANGE_NOTIFY => {
                        if let Ok(mut st) = state_lock().lock() {
                            st.counters.password_edit_change_count += 1;
                            let is_setup = st.phase.is_setup_phase();
                            if is_setup {
                                st.counters.setup_password_edit_change_count += 1;
                            } else {
                                st.counters.runtime_password_edit_change_count += 1;
                            }
                            let v = st.counters.password_edit_change_count;
                            let category = if is_setup {
                                "setup_initial_text_notify"
                            } else {
                                "text_mutation"
                            };
                            st.record_trace(
                                "main_wnd_proc",
                                "password_edit",
                                category,
                                "WM_COMMAND",
                                ID_PASSWORD_EDIT,
                                Some("EN_CHANGE"),
                                "passwordEditChangeCount",
                                v,
                            );
                        }
                    }
                    EN_SETFOCUS_NOTIFY => {
                        if let Ok(mut st) = state_lock().lock() {
                            st.counters.password_edit_focus_gained_count += 1;
                            let v = st.counters.password_edit_focus_gained_count;
                            st.record_trace(
                                "main_wnd_proc",
                                "password_edit",
                                "focus",
                                "WM_COMMAND",
                                ID_PASSWORD_EDIT,
                                Some("EN_SETFOCUS"),
                                "passwordEditFocusGainedCount",
                                v,
                            );
                        }
                    }
                    EN_KILLFOCUS_NOTIFY => {
                        if let Ok(mut st) = state_lock().lock() {
                            st.counters.password_edit_focus_lost_count += 1;
                            let v = st.counters.password_edit_focus_lost_count;
                            st.record_trace(
                                "main_wnd_proc",
                                "password_edit",
                                "focus",
                                "WM_COMMAND",
                                ID_PASSWORD_EDIT,
                                Some("EN_KILLFOCUS"),
                                "passwordEditFocusLostCount",
                                v,
                            );
                        }
                    }
                    _ => {}
                }
            }
        }
        _ => {}
    }
    DefWindowProcW(hwnd, msg, wparam, lparam)
}

unsafe extern "system" fn child_subclass_proc(
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
    uidsubclass: usize,
    _dwrefdata: usize,
) -> LRESULT {
    match uidsubclass {
        SUBCLASS_ID_BUTTON => match msg {
            WM_SETFOCUS => {
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.button_focus_gained_count += 1;
                    let v = st.counters.button_focus_gained_count;
                    st.record_trace(
                        "child_subclass",
                        "button",
                        "focus",
                        "WM_SETFOCUS",
                        ID_BUTTON,
                        None,
                        "buttonFocusGainedCount",
                        v,
                    );
                }
            }
            WM_KILLFOCUS => {
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.button_focus_lost_count += 1;
                    let v = st.counters.button_focus_lost_count;
                    st.record_trace(
                        "child_subclass",
                        "button",
                        "focus",
                        "WM_KILLFOCUS",
                        ID_BUTTON,
                        None,
                        "buttonFocusLostCount",
                        v,
                    );
                }
            }
            WM_MOUSEMOVE => {
                let detail = capture_mouse_move_trace_detail(hwnd, wparam, lparam);
                if let Ok(mut st) = state_lock().lock() {
                    st.record_mouse_move_trace("child_subclass", "button", ID_BUTTON, Some(detail));
                }
            }
            WM_LBUTTONDOWN => {
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.button_lbutton_down_count += 1;
                    let v = st.counters.button_lbutton_down_count;
                    st.record_trace(
                        "child_subclass",
                        "button",
                        "mouse_button",
                        "WM_LBUTTONDOWN",
                        ID_BUTTON,
                        None,
                        "buttonLbuttonDownCount",
                        v,
                    );
                }
            }
            WM_LBUTTONUP => {
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.button_lbutton_up_count += 1;
                    let v = st.counters.button_lbutton_up_count;
                    st.record_trace(
                        "child_subclass",
                        "button",
                        "mouse_button",
                        "WM_LBUTTONUP",
                        ID_BUTTON,
                        None,
                        "buttonLbuttonUpCount",
                        v,
                    );
                }
            }
            WM_LBUTTONDBLCLK => {
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.button_lbutton_dblclk_count += 1;
                    let v = st.counters.button_lbutton_dblclk_count;
                    st.record_trace(
                        "child_subclass",
                        "button",
                        "mouse_button",
                        "WM_LBUTTONDBLCLK",
                        ID_BUTTON,
                        None,
                        "buttonLbuttonDblclkCount",
                        v,
                    );
                }
            }
            WM_RBUTTONDOWN => {
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.button_rbutton_down_count += 1;
                    let v = st.counters.button_rbutton_down_count;
                    st.record_trace(
                        "child_subclass",
                        "button",
                        "mouse_button",
                        "WM_RBUTTONDOWN",
                        ID_BUTTON,
                        None,
                        "buttonRbuttonDownCount",
                        v,
                    );
                }
            }
            WM_RBUTTONUP => {
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.button_rbutton_up_count += 1;
                    let v = st.counters.button_rbutton_up_count;
                    st.record_trace(
                        "child_subclass",
                        "button",
                        "mouse_button",
                        "WM_RBUTTONUP",
                        ID_BUTTON,
                        None,
                        "buttonRbuttonUpCount",
                        v,
                    );
                }
            }
            WM_MOUSEWHEEL | WM_MOUSEHWHEEL => {
                return handle_child_wheel_and_forward(
                    hwnd, msg, wparam, lparam, "button", ID_BUTTON,
                );
            }
            WM_DESTROY => {
                let _ = RemoveWindowSubclass(hwnd, Some(child_subclass_proc), uidsubclass);
            }
            _ => {}
        },
        SUBCLASS_ID_PUBLIC_EDIT => match msg {
            WM_MOUSEMOVE => {
                let detail = capture_mouse_move_trace_detail(hwnd, wparam, lparam);
                if let Ok(mut st) = state_lock().lock() {
                    st.record_mouse_move_trace(
                        "child_subclass",
                        "public_edit",
                        ID_PUBLIC_EDIT,
                        Some(detail),
                    );
                }
            }
            WM_LBUTTONDOWN => {
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.public_edit_lbutton_down_count += 1;
                    let v = st.counters.public_edit_lbutton_down_count;
                    st.record_trace(
                        "child_subclass",
                        "public_edit",
                        "mouse_button",
                        "WM_LBUTTONDOWN",
                        ID_PUBLIC_EDIT,
                        None,
                        "publicEditLbuttonDownCount",
                        v,
                    );
                }
            }
            WM_LBUTTONUP => {
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.public_edit_lbutton_up_count += 1;
                    let v = st.counters.public_edit_lbutton_up_count;
                    st.record_trace(
                        "child_subclass",
                        "public_edit",
                        "mouse_button",
                        "WM_LBUTTONUP",
                        ID_PUBLIC_EDIT,
                        None,
                        "publicEditLbuttonUpCount",
                        v,
                    );
                }
            }
            WM_KEYDOWN => {
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.public_edit_keydown_count += 1;
                    st.counters.public_edit_last_vk = (wparam.0 & 0xFFFF) as u32;
                    let v = st.counters.public_edit_keydown_count;
                    st.record_trace(
                        "child_subclass",
                        "public_edit",
                        "keyboard",
                        "WM_KEYDOWN",
                        ID_PUBLIC_EDIT,
                        None,
                        "publicEditKeydownCount",
                        v,
                    );
                }
            }
            WM_KEYUP => {
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.public_edit_keyup_count += 1;
                    let v = st.counters.public_edit_keyup_count;
                    st.record_trace(
                        "child_subclass",
                        "public_edit",
                        "keyboard",
                        "WM_KEYUP",
                        ID_PUBLIC_EDIT,
                        None,
                        "publicEditKeyupCount",
                        v,
                    );
                }
            }
            WM_CHAR => {
                let ch = (wparam.0 & 0xFFFF) as u32;
                let res = DefSubclassProc(hwnd, msg, wparam, lparam);
                let updated_text = get_window_title(hwnd);
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.public_edit_char_count += 1;
                    st.counters.public_edit_chars.push(ch);
                    st.counters.public_edit_text = updated_text;
                    let v = st.counters.public_edit_char_count;
                    st.record_trace(
                        "child_subclass",
                        "public_edit",
                        "keyboard",
                        "WM_CHAR",
                        ID_PUBLIC_EDIT,
                        None,
                        "publicEditCharCount",
                        v,
                    );
                }
                return res;
            }
            WM_SETTEXT => {
                let res = DefSubclassProc(hwnd, msg, wparam, lparam);
                let updated_text = get_window_title(hwnd);
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.public_edit_settext_count += 1;
                    st.counters.public_edit_text = updated_text;
                    let v = st.counters.public_edit_settext_count;
                    st.record_trace(
                        "child_subclass",
                        "public_edit",
                        "text_mutation",
                        "WM_SETTEXT",
                        ID_PUBLIC_EDIT,
                        None,
                        "publicEditSettextCount",
                        v,
                    );
                }
                return res;
            }
            WM_MOUSEWHEEL | WM_MOUSEHWHEEL => {
                return handle_child_wheel_and_forward(
                    hwnd,
                    msg,
                    wparam,
                    lparam,
                    "public_edit",
                    ID_PUBLIC_EDIT,
                );
            }
            WM_DESTROY => {
                let _ = RemoveWindowSubclass(hwnd, Some(child_subclass_proc), uidsubclass);
            }
            _ => {}
        },
        SUBCLASS_ID_PASSWORD_EDIT => match msg {
            // Focus is counted via parent WM_COMMAND (EN_SETFOCUS / EN_KILLFOCUS) so setup + runtime
            // share a single non-duplicated source; here we count mouse, key, char, wheel, and settext
            // without EVER reading or recording password text or characters.
            WM_MOUSEMOVE => {
                let detail = capture_mouse_move_trace_detail(hwnd, wparam, lparam);
                if let Ok(mut st) = state_lock().lock() {
                    st.record_mouse_move_trace(
                        "child_subclass",
                        "password_edit",
                        ID_PASSWORD_EDIT,
                        Some(detail),
                    );
                }
            }
            WM_LBUTTONDOWN => {
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.password_edit_lbutton_down_count += 1;
                    let v = st.counters.password_edit_lbutton_down_count;
                    st.record_trace(
                        "child_subclass",
                        "password_edit",
                        "mouse_button",
                        "WM_LBUTTONDOWN",
                        ID_PASSWORD_EDIT,
                        None,
                        "passwordEditLbuttonDownCount",
                        v,
                    );
                }
            }
            WM_LBUTTONUP => {
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.password_edit_lbutton_up_count += 1;
                    let v = st.counters.password_edit_lbutton_up_count;
                    st.record_trace(
                        "child_subclass",
                        "password_edit",
                        "mouse_button",
                        "WM_LBUTTONUP",
                        ID_PASSWORD_EDIT,
                        None,
                        "passwordEditLbuttonUpCount",
                        v,
                    );
                }
            }
            WM_KEYDOWN => {
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.password_edit_keydown_count += 1;
                    let v = st.counters.password_edit_keydown_count;
                    st.record_trace(
                        "child_subclass",
                        "password_edit",
                        "keyboard",
                        "WM_KEYDOWN",
                        ID_PASSWORD_EDIT,
                        None,
                        "passwordEditKeydownCount",
                        v,
                    );
                }
            }
            WM_KEYUP => {
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.password_edit_keyup_count += 1;
                    let v = st.counters.password_edit_keyup_count;
                    st.record_trace(
                        "child_subclass",
                        "password_edit",
                        "keyboard",
                        "WM_KEYUP",
                        ID_PASSWORD_EDIT,
                        None,
                        "passwordEditKeyupCount",
                        v,
                    );
                }
            }
            WM_CHAR => {
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.password_edit_char_count += 1;
                    let v = st.counters.password_edit_char_count;
                    st.record_trace(
                        "child_subclass",
                        "password_edit",
                        "keyboard",
                        "WM_CHAR",
                        ID_PASSWORD_EDIT,
                        None,
                        "passwordEditCharCount",
                        v,
                    );
                }
            }
            WM_SETTEXT => {
                let res = DefSubclassProc(hwnd, msg, wparam, lparam);
                if let Ok(mut st) = state_lock().lock() {
                    st.counters.password_edit_settext_count += 1;
                    let v = st.counters.password_edit_settext_count;
                    st.record_trace(
                        "child_subclass",
                        "password_edit",
                        "text_mutation",
                        "WM_SETTEXT",
                        ID_PASSWORD_EDIT,
                        None,
                        "passwordEditSettextCount",
                        v,
                    );
                }
                return res;
            }
            WM_MOUSEWHEEL | WM_MOUSEHWHEEL => {
                return handle_child_wheel_and_forward(
                    hwnd,
                    msg,
                    wparam,
                    lparam,
                    "password_edit",
                    ID_PASSWORD_EDIT,
                );
            }
            WM_DESTROY => {
                let _ = RemoveWindowSubclass(hwnd, Some(child_subclass_proc), uidsubclass);
            }
            _ => {}
        },
        _ => {}
    }
    DefSubclassProc(hwnd, msg, wparam, lparam)
}

fn to_wide_null(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

fn parse_args() -> (u64, String) {
    let mut timeout_ms = 15_000u64;
    let mut nonce = csprng_hex(8);
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--timeout-ms" => {
                if let Some(val) = args.next() {
                    if let Ok(ms) = val.parse::<u64>() {
                        timeout_ms = ms.clamp(100, 120_000);
                    }
                }
            }
            "--nonce" => {
                if let Some(val) = args.next() {
                    let trimmed = val.trim();
                    if !trimmed.is_empty() && trimmed.len() <= 64 {
                        nonce = trimmed.to_string();
                    }
                }
            }
            _ => {}
        }
    }
    (timeout_ms, nonce)
}

unsafe fn build_child_info(
    role: &str,
    child_hwnd: HWND,
    main_hwnd: HWND,
    control_id: usize,
    control_type: &str,
    password: bool,
) -> FixtureChildInfo {
    let parent = GetParent(child_hwnd).unwrap_or_default();
    let is_child = IsChild(main_hwnd, child_hwnd).as_bool() && parent == main_hwnd;
    FixtureChildInfo {
        role: role.to_string(),
        hwnd: hwnd_to_u64(child_hwnd).to_string(),
        parent_hwnd: hwnd_to_u64(parent).to_string(),
        control_id,
        class_name: get_window_class_name(child_hwnd),
        control_type: control_type.to_string(),
        is_child_of_main: is_child,
        password,
    }
}

fn emit_json_line<T: Serialize>(payload: &T) {
    if let Ok(s) = serde_json::to_string(payload) {
        let mut out = io::stdout().lock();
        let _ = out.write_all(s.as_bytes());
        let _ = out.write_all(b"\n");
        let _ = out.flush();
    }
}

unsafe fn query_readonly_cursor_pos() -> Result<FixturePointPx, String> {
    let mut pt = POINT::default();
    GetCursorPos(&mut pt)
        .map_err(|e| format!("GetCursorPos failed: {e}"))?;
    Ok(FixturePointPx::new(pt.x, pt.y))
}

unsafe fn query_monitor_and_work_area_for_rect(
    preferred_rect: FixtureRectPx,
) -> Result<(FixtureRectPx, FixtureRectPx), String> {
    if !preferred_rect.is_valid_non_empty() {
        return Err("invalid preferred_rect passed to query_monitor_and_work_area_for_rect".to_string());
    }
    let win_rect = RECT {
        left: preferred_rect.left,
        top: preferred_rect.top,
        right: preferred_rect.right,
        bottom: preferred_rect.bottom,
    };
    let hmon = MonitorFromRect(&win_rect, MONITOR_DEFAULTTONEAREST);
    if hmon.0.is_null() {
        return Err("MonitorFromRect returned null HMONITOR".to_string());
    }
    let mut mi = MONITORINFO {
        cbSize: std::mem::size_of::<MONITORINFO>() as u32,
        ..Default::default()
    };
    if !GetMonitorInfoW(hmon, &mut mi).as_bool() {
        return Err("GetMonitorInfoW failed for MonitorFromRect handle".to_string());
    }
    let monitor_rect = FixtureRectPx::from_ltrb(
        mi.rcMonitor.left,
        mi.rcMonitor.top,
        mi.rcMonitor.right,
        mi.rcMonitor.bottom,
    );
    let work_area = FixtureRectPx::from_ltrb(
        mi.rcWork.left,
        mi.rcWork.top,
        mi.rcWork.right,
        mi.rcWork.bottom,
    );
    if !monitor_rect.is_valid_non_empty() || !work_area.is_valid_non_empty() {
        return Err(format!(
            "Monitor or work area rectangle is invalid: monitor={:?} work_area={:?}",
            monitor_rect, work_area
        ));
    }
    if !monitor_rect.contains_rect(&work_area) {
        return Err(format!(
            "Monitor work area {:?} is not contained in monitor rect {:?}",
            work_area, monitor_rect
        ));
    }
    Ok((monitor_rect, work_area))
}

unsafe fn query_actual_window_and_client_bounds(
    hwnd: HWND,
) -> Result<(FixtureRectPx, FixtureRectPx), String> {
    let mut wrect = RECT::default();
    GetWindowRect(hwnd, &mut wrect)
        .map_err(|e| format!("GetWindowRect failed: {e}"))?;
    let actual_window = FixtureRectPx::from_ltrb(wrect.left, wrect.top, wrect.right, wrect.bottom);
    if !actual_window.is_valid_non_empty() {
        return Err(format!("GetWindowRect returned invalid rect: {:?}", actual_window));
    }

    let mut crect = RECT::default();
    GetClientRect(hwnd, &mut crect)
        .map_err(|e| format!("GetClientRect failed: {e}"))?;
    let mut tl = POINT {
        x: crect.left,
        y: crect.top,
    };
    let mut br = POINT {
        x: crect.right,
        y: crect.bottom,
    };
    if !ClientToScreen(hwnd, &mut tl).as_bool() || !ClientToScreen(hwnd, &mut br).as_bool() {
        return Err("ClientToScreen failed for client bounds".to_string());
    }
    let actual_client = FixtureRectPx::from_ltrb(tl.x, tl.y, br.x, br.y);
    if !actual_client.is_valid_non_empty() {
        return Err(format!(
            "ClientToScreen produced invalid client bounds: {:?}",
            actual_client
        ));
    }
    Ok((actual_window, actual_client))
}

unsafe fn emit_fixture_unavailable_and_exit(
    stage: &str,
    reason: &str,
    nonce: &str,
    pid: u32,
    process_started: &str,
    executable_path: &Option<String>,
    main_hwnd: Option<HWND>,
    placement: serde_json::Value,
) -> ! {
    if let Some(hwnd) = main_hwnd {
        if IsWindow(Some(hwnd)).as_bool() {
            let _ = DestroyWindow(hwnd);
        }
    }
    let (phase_now, setup_counters_snapshot, counters_snapshot, trace_snapshot, dropped_now) = {
        let st = state_lock().lock().unwrap();
        (
            st.phase,
            st.setup_counters_frozen.clone(),
            st.counters.clone(),
            st.trace.clone(),
            st.trace_dropped_count,
        )
    };
    emit_json_line(&serde_json::json!({
        "status": "fixture_unavailable",
        "stage": stage,
        "reason": reason,
        "phase": phase_now,
        "nonce": nonce,
        "pid": pid,
        "processStarted": process_started,
        "executablePath": executable_path,
        "placement": placement,
        "setupCounters": setup_counters_snapshot,
        "counters": counters_snapshot,
        "trace": trace_snapshot,
        "traceDroppedCount": dropped_now,
    }));
    std::process::exit(2);
}

fn main() {
    let dpi_awareness = init_process_dpi_awareness().to_string();
    let (timeout_ms, nonce) = parse_args();
    let pid = current_worker_pid();
    let process_started = query_process_creation_time(pid)
        .expect("Failed to query own process creation time")
        .to_string();
    let executable_path = query_process_executable_path(pid);

    let class_name_str = format!("RynOwnedFixtureClass_{}", nonce);
    let window_title_str = format!("RynComputerUseFixture_{}", nonce);
    let class_wide = to_wide_null(&class_name_str);
    let title_wide = to_wide_null(&window_title_str);

    // Spawn bounded stdin reader for fixture control commands
    let (cmd_tx, cmd_rx) = mpsc::channel::<FixtureControlCommand>();
    std::thread::Builder::new()
        .name("fixture-stdin-control".to_string())
        .spawn(move || {
            let stdin = io::stdin();
            for line_res in stdin.lock().lines() {
                match line_res {
                    Ok(line) => {
                        let trimmed = line.trim();
                        if trimmed.is_empty() {
                            continue;
                        }
                        if let Ok(cmd) = serde_json::from_str::<FixtureControlCommand>(trimmed) {
                            let is_shutdown = matches!(cmd, FixtureControlCommand::Shutdown { .. });
                            if cmd_tx.send(cmd).is_err() || is_shutdown {
                                break;
                            }
                        }
                    }
                    Err(_) => break,
                }
            }
        })
        .expect("Failed to spawn fixture control reader thread");

    unsafe {
        set_lifecycle_phase(FixtureLifecyclePhase::CreateMain);
        let original_preferred_rect = FixtureRectPx::from_xywh(
            ORIGINAL_PREFERRED_OUTER_X,
            ORIGINAL_PREFERRED_OUTER_Y,
            ORIGINAL_PREFERRED_OUTER_WIDTH,
            ORIGINAL_PREFERRED_OUTER_HEIGHT,
        );

        if !is_confirmed_per_monitor_dpi_awareness(&dpi_awareness) {
            emit_fixture_unavailable_and_exit(
                "create_main_dpi_gate",
                &format!("unconfirmed_dpi_awareness_mode: {dpi_awareness}"),
                &nonce,
                pid,
                &process_started,
                &executable_path,
                None,
                serde_json::json!({
                    "dpiAwareness": dpi_awareness,
                    "originalPreferredRect": original_preferred_rect,
                }),
            );
        }

        let creation_cursor = match query_readonly_cursor_pos() {
            Ok(pt) => pt,
            Err(err) => {
                emit_fixture_unavailable_and_exit(
                    "create_main_cursor_query",
                    &format!("creation_cursor_unavailable: {err}"),
                    &nonce,
                    pid,
                    &process_started,
                    &executable_path,
                    None,
                    serde_json::json!({
                        "dpiAwareness": dpi_awareness,
                        "originalPreferredRect": original_preferred_rect,
                    }),
                );
            }
        };

        let (selected_monitor_rect, selected_monitor_work_area) =
            match query_monitor_and_work_area_for_rect(original_preferred_rect) {
                Ok(pair) => pair,
                Err(err) => {
                    emit_fixture_unavailable_and_exit(
                        "create_main_monitor_query",
                        &format!("monitor_work_area_unavailable: {err}"),
                        &nonce,
                        pid,
                        &process_started,
                        &executable_path,
                        None,
                        serde_json::json!({
                            "dpiAwareness": dpi_awareness,
                            "originalPreferredRect": original_preferred_rect,
                            "creationCursor": creation_cursor,
                        }),
                    );
                }
            };

        let (requested_outer_rect, placement_candidate_name) =
            match select_cursor_avoiding_window_placement(
                original_preferred_rect,
                selected_monitor_work_area,
                creation_cursor,
                DEFAULT_PLACEMENT_EDGE_MARGIN_PX,
                DEFAULT_PLACEMENT_CURSOR_CLEARANCE_PX,
            ) {
                Ok(sel) => sel,
                Err(err) => {
                    emit_fixture_unavailable_and_exit(
                        "create_main_placement_select",
                        &format!("cursor_avoiding_placement_unavailable: {err}"),
                        &nonce,
                        pid,
                        &process_started,
                        &executable_path,
                        None,
                        serde_json::json!({
                            "dpiAwareness": dpi_awareness,
                            "originalPreferredRect": original_preferred_rect,
                            "creationCursor": creation_cursor,
                            "selectedMonitorRect": selected_monitor_rect,
                            "selectedMonitorWorkArea": selected_monitor_work_area,
                        }),
                    );
                }
            };

        let hinstance: HINSTANCE = GetModuleHandleW(None).unwrap_or_default().into();
        let wc = WNDCLASSW {
            lpfnWndProc: Some(main_wnd_proc),
            hInstance: hinstance,
            hbrBackground: HBRUSH((COLOR_WINDOW.0 + 1) as *mut core::ffi::c_void),
            lpszClassName: PCWSTR(class_wide.as_ptr()),
            ..Default::default()
        };
        let atom = RegisterClassW(&wc);
        assert!(atom != 0, "RegisterClassW failed for owned fixture window");

        let main_hwnd = CreateWindowExW(
            WINDOW_EX_STYLE(0),
            PCWSTR(class_wide.as_ptr()),
            PCWSTR(title_wide.as_ptr()),
            WS_OVERLAPPEDWINDOW,
            requested_outer_rect.left,
            requested_outer_rect.top,
            requested_outer_rect.width,
            requested_outer_rect.height,
            None,
            None,
            Some(hinstance),
            None,
        )
        .expect("Failed to create RynComputerUseFixture top-level window");

        let (win_pid, _) = get_window_pid_tid(main_hwnd);
        assert_eq!(win_pid, pid, "Created window PID must match own process PID");

        set_lifecycle_phase(FixtureLifecyclePhase::CreateButton);
        let btn_hwnd = CreateWindowExW(
            WINDOW_EX_STYLE(0),
            w!("BUTTON"),
            w!("InvokeButton"),
            WS_CHILD
                | WS_VISIBLE
                | WS_TABSTOP
                | WINDOW_STYLE(BS_PUSHBUTTON_STYLE | BS_NOTIFY_STYLE),
            20,
            20,
            150,
            36,
            Some(main_hwnd),
            Some(HMENU(ID_BUTTON as *mut core::ffi::c_void)),
            Some(hinstance),
            None,
        )
        .expect("Failed to create child button");

        set_lifecycle_phase(FixtureLifecyclePhase::CreatePublic);
        let edit_hwnd = CreateWindowExW(
            WINDOW_EX_STYLE(0),
            w!("EDIT"),
            w!("InitialPublicValue"),
            WS_CHILD | WS_VISIBLE | WS_BORDER | WS_TABSTOP | WINDOW_STYLE(ES_AUTOHSCROLL_STYLE),
            20,
            72,
            260,
            30,
            Some(main_hwnd),
            Some(HMENU(ID_PUBLIC_EDIT as *mut core::ffi::c_void)),
            Some(hinstance),
            None,
        )
        .expect("Failed to create normal edit control");

        set_lifecycle_phase(FixtureLifecyclePhase::CreatePassword);
        let pwd_hwnd = CreateWindowExW(
            WINDOW_EX_STYLE(0),
            w!("EDIT"),
            w!("TopSecretPasswordValue999"),
            WS_CHILD
                | WS_VISIBLE
                | WS_BORDER
                | WS_TABSTOP
                | WINDOW_STYLE(ES_PASSWORD_STYLE | ES_AUTOHSCROLL_STYLE),
            20,
            120,
            260,
            30,
            Some(main_hwnd),
            Some(HMENU(ID_PASSWORD_EDIT as *mut core::ffi::c_void)),
            Some(hinstance),
            None,
        )
        .expect("Failed to create password edit control");

        set_lifecycle_phase(FixtureLifecyclePhase::Subclass);
        let _ = SetWindowSubclass(
            btn_hwnd,
            Some(child_subclass_proc),
            SUBCLASS_ID_BUTTON,
            0,
        );
        let _ = SetWindowSubclass(
            edit_hwnd,
            Some(child_subclass_proc),
            SUBCLASS_ID_PUBLIC_EDIT,
            0,
        );
        let _ = SetWindowSubclass(
            pwd_hwnd,
            Some(child_subclass_proc),
            SUBCLASS_ID_PASSWORD_EDIT,
            0,
        );

        let pre_show_window_dpi = GetDpiForWindow(main_hwnd);
        let (pre_show_window_rect, pre_show_client_bounds) =
            match query_actual_window_and_client_bounds(main_hwnd) {
                Ok(pair) => pair,
                Err(err) => {
                    emit_fixture_unavailable_and_exit(
                        "pre_show_bounds_query",
                        &format!("pre_show_window_bounds_unavailable: {err}"),
                        &nonce,
                        pid,
                        &process_started,
                        &executable_path,
                        Some(main_hwnd),
                        serde_json::json!({
                            "dpiAwareness": dpi_awareness,
                            "preShowWindowDpi": pre_show_window_dpi,
                            "placementCandidate": placement_candidate_name,
                            "originalPreferredRect": original_preferred_rect,
                            "creationCursor": creation_cursor,
                            "selectedMonitorRect": selected_monitor_rect,
                            "selectedMonitorWorkArea": selected_monitor_work_area,
                            "requestedOuterRect": requested_outer_rect,
                        }),
                    );
                }
            };

        let pre_show_cursor = match query_readonly_cursor_pos() {
            Ok(pt) => pt,
            Err(err) => {
                emit_fixture_unavailable_and_exit(
                    "pre_show_cursor_query",
                    &format!("pre_show_cursor_unavailable: {err}"),
                    &nonce,
                    pid,
                    &process_started,
                    &executable_path,
                    Some(main_hwnd),
                    serde_json::json!({
                        "dpiAwareness": dpi_awareness,
                        "preShowWindowDpi": pre_show_window_dpi,
                        "placementCandidate": placement_candidate_name,
                        "originalPreferredRect": original_preferred_rect,
                        "creationCursor": creation_cursor,
                        "selectedMonitorRect": selected_monitor_rect,
                        "selectedMonitorWorkArea": selected_monitor_work_area,
                        "requestedOuterRect": requested_outer_rect,
                        "preShowWindowRect": pre_show_window_rect,
                        "preShowClientBounds": pre_show_client_bounds,
                    }),
                );
            }
        };

        if let Err(reason) = validate_stage_window_and_cursor_invariants(
            "pre_show",
            &dpi_awareness,
            pre_show_window_dpi,
            selected_monitor_work_area,
            requested_outer_rect,
            pre_show_window_rect,
            pre_show_client_bounds,
            pre_show_cursor,
        ) {
            emit_fixture_unavailable_and_exit(
                "pre_show_validation",
                &reason,
                &nonce,
                pid,
                &process_started,
                &executable_path,
                Some(main_hwnd),
                serde_json::json!({
                    "dpiAwareness": dpi_awareness,
                    "preShowWindowDpi": pre_show_window_dpi,
                    "placementCandidate": placement_candidate_name,
                    "originalPreferredRect": original_preferred_rect,
                    "creationCursor": creation_cursor,
                    "selectedMonitorRect": selected_monitor_rect,
                    "selectedMonitorWorkArea": selected_monitor_work_area,
                    "requestedOuterRect": requested_outer_rect,
                    "preShowWindowRect": pre_show_window_rect,
                    "preShowClientBounds": pre_show_client_bounds,
                    "preShowCursor": pre_show_cursor,
                }),
            );
        }

        // Show without activating or stealing user focus
        set_lifecycle_phase(FixtureLifecyclePhase::Show);
        let _ = ShowWindow(main_hwnd, SW_SHOWNOACTIVATE);
        let _ = UpdateWindow(main_hwnd);

        // Pump initial creation/paint messages before entering Ready
        set_lifecycle_phase(FixtureLifecyclePhase::Pump);
        let mut msg = MSG::default();
        while PeekMessageW(&mut msg, None, 0, 0, PM_REMOVE).as_bool() {
            let _ = TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }

        // Transition to Ready and freeze setup baseline snapshot (without resetting any counter)
        set_lifecycle_phase(FixtureLifecyclePhase::Ready);

        let window_dpi = GetDpiForWindow(main_hwnd);
        let (actual_window_rect, actual_client_bounds) =
            match query_actual_window_and_client_bounds(main_hwnd) {
                Ok(pair) => pair,
                Err(err) => {
                    emit_fixture_unavailable_and_exit(
                        "post_pump_ready_bounds_query",
                        &format!("ready_window_bounds_unavailable: {err}"),
                        &nonce,
                        pid,
                        &process_started,
                        &executable_path,
                        Some(main_hwnd),
                        serde_json::json!({
                            "dpiAwareness": dpi_awareness,
                            "preShowWindowDpi": pre_show_window_dpi,
                            "windowDpi": window_dpi,
                            "placementCandidate": placement_candidate_name,
                            "originalPreferredRect": original_preferred_rect,
                            "creationCursor": creation_cursor,
                            "selectedMonitorRect": selected_monitor_rect,
                            "selectedMonitorWorkArea": selected_monitor_work_area,
                            "requestedOuterRect": requested_outer_rect,
                            "preShowWindowRect": pre_show_window_rect,
                            "preShowClientBounds": pre_show_client_bounds,
                            "preShowCursor": pre_show_cursor,
                        }),
                    );
                }
            };

        let ready_cursor = match query_readonly_cursor_pos() {
            Ok(pt) => pt,
            Err(err) => {
                emit_fixture_unavailable_and_exit(
                    "post_pump_ready_cursor_query",
                    &format!("ready_cursor_unavailable: {err}"),
                    &nonce,
                    pid,
                    &process_started,
                    &executable_path,
                    Some(main_hwnd),
                    serde_json::json!({
                        "dpiAwareness": dpi_awareness,
                        "preShowWindowDpi": pre_show_window_dpi,
                        "windowDpi": window_dpi,
                        "placementCandidate": placement_candidate_name,
                        "originalPreferredRect": original_preferred_rect,
                        "creationCursor": creation_cursor,
                        "selectedMonitorRect": selected_monitor_rect,
                        "selectedMonitorWorkArea": selected_monitor_work_area,
                        "requestedOuterRect": requested_outer_rect,
                        "preShowWindowRect": pre_show_window_rect,
                        "preShowClientBounds": pre_show_client_bounds,
                        "preShowCursor": pre_show_cursor,
                        "actualWindowRect": actual_window_rect,
                        "actualClientBounds": actual_client_bounds,
                    }),
                );
            }
        };

        if window_dpi != pre_show_window_dpi {
            emit_fixture_unavailable_and_exit(
                "post_pump_ready_dpi_validation",
                &format!(
                    "post_pump_ready_window_dpi_changed: pre_show={} ready={}",
                    pre_show_window_dpi, window_dpi
                ),
                &nonce,
                pid,
                &process_started,
                &executable_path,
                Some(main_hwnd),
                serde_json::json!({
                    "dpiAwareness": dpi_awareness,
                    "preShowWindowDpi": pre_show_window_dpi,
                    "windowDpi": window_dpi,
                    "placementCandidate": placement_candidate_name,
                    "originalPreferredRect": original_preferred_rect,
                    "creationCursor": creation_cursor,
                    "selectedMonitorRect": selected_monitor_rect,
                    "selectedMonitorWorkArea": selected_monitor_work_area,
                    "requestedOuterRect": requested_outer_rect,
                    "preShowWindowRect": pre_show_window_rect,
                    "preShowClientBounds": pre_show_client_bounds,
                    "preShowCursor": pre_show_cursor,
                    "actualWindowRect": actual_window_rect,
                    "actualClientBounds": actual_client_bounds,
                    "readyCursor": ready_cursor,
                }),
            );
        }

        if let Err(reason) = validate_stage_window_and_cursor_invariants(
            "post_pump_ready",
            &dpi_awareness,
            window_dpi,
            selected_monitor_work_area,
            requested_outer_rect,
            actual_window_rect,
            actual_client_bounds,
            ready_cursor,
        ) {
            emit_fixture_unavailable_and_exit(
                "post_pump_ready_validation",
                &reason,
                &nonce,
                pid,
                &process_started,
                &executable_path,
                Some(main_hwnd),
                serde_json::json!({
                    "dpiAwareness": dpi_awareness,
                    "preShowWindowDpi": pre_show_window_dpi,
                    "windowDpi": window_dpi,
                    "placementCandidate": placement_candidate_name,
                    "originalPreferredRect": original_preferred_rect,
                    "creationCursor": creation_cursor,
                    "selectedMonitorRect": selected_monitor_rect,
                    "selectedMonitorWorkArea": selected_monitor_work_area,
                    "requestedOuterRect": requested_outer_rect,
                    "preShowWindowRect": pre_show_window_rect,
                    "preShowClientBounds": pre_show_client_bounds,
                    "preShowCursor": pre_show_cursor,
                    "actualWindowRect": actual_window_rect,
                    "actualClientBounds": actual_client_bounds,
                    "readyCursor": ready_cursor,
                }),
            );
        }

        let placement_diagnostics = FixturePlacementDiagnostics {
            dpi_awareness: dpi_awareness.clone(),
            pre_show_window_dpi,
            window_dpi,
            placement_candidate: placement_candidate_name.to_string(),
            original_preferred_rect,
            creation_cursor,
            selected_monitor_rect,
            selected_monitor_work_area,
            requested_outer_rect,
            pre_show_window_rect,
            pre_show_client_bounds,
            pre_show_cursor,
            actual_window_rect,
            actual_client_bounds,
            ready_cursor,
        };

        let children = vec![
            build_child_info("button", btn_hwnd, main_hwnd, ID_BUTTON, "button", false),
            build_child_info(
                "public_edit",
                edit_hwnd,
                main_hwnd,
                ID_PUBLIC_EDIT,
                "edit",
                false,
            ),
            build_child_info(
                "password_edit",
                pwd_hwnd,
                main_hwnd,
                ID_PASSWORD_EDIT,
                "edit",
                true,
            ),
        ];

        let (phase_now, setup_counters_snapshot, counters_snapshot, trace_snapshot, dropped_now) = {
            let st = state_lock().lock().unwrap();
            (
                st.phase,
                st.setup_counters_frozen.clone(),
                st.counters.clone(),
                st.trace.clone(),
                st.trace_dropped_count,
            )
        };
        let ready_report = FixtureReadyReport {
            status: "ready".to_string(),
            phase: phase_now,
            nonce: nonce.clone(),
            pid,
            process_started,
            executable_path,
            hwnd: hwnd_to_u64(main_hwnd).to_string(),
            title: get_window_title(main_hwnd),
            class_name: get_window_class_name(main_hwnd),
            placement: placement_diagnostics.clone(),
            children,
            setup_counters: setup_counters_snapshot,
            counters: counters_snapshot,
            trace: trace_snapshot,
            trace_dropped_count: dropped_now,
        };
        emit_json_line(&ready_report);

        let mut modal_a_hwnd: Option<HWND> = None;
        let mut modal_b_hwnd: Option<HWND> = None;
        let deadline = Instant::now() + Duration::from_millis(timeout_ms);
        let mut running = true;

        while running && Instant::now() < deadline && IsWindow(Some(main_hwnd)).as_bool() {
            while PeekMessageW(&mut msg, None, 0, 0, PM_REMOVE).as_bool() {
                let _ = TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }

            loop {
                match cmd_rx.try_recv() {
                    Ok(FixtureControlCommand::ReadCounters { req_id }) => {
                        while PeekMessageW(&mut msg, None, 0, 0, PM_REMOVE).as_bool() {
                            let _ = TranslateMessage(&msg);
                            DispatchMessageW(&msg);
                        }
                        let live_text = if IsWindow(Some(edit_hwnd)).as_bool() {
                            Some(get_window_title(edit_hwnd))
                        } else {
                            None
                        };
                        let (phase_snap, setup_snap, counters_snap, trace_snap, dropped_snap) = {
                            let mut st = state_lock().lock().unwrap();
                            if let Some(txt) = live_text {
                                st.counters.public_edit_text = txt;
                            }
                            (
                                st.phase,
                                st.setup_counters_frozen.clone(),
                                st.counters.clone(),
                                st.trace.clone(),
                                st.trace_dropped_count,
                            )
                        };
                        emit_json_line(&serde_json::json!({
                            "status": "counters",
                            "reqId": req_id,
                            "phase": phase_snap,
                            "hwnd": hwnd_to_u64(main_hwnd).to_string(),
                            "pid": pid,
                            "placement": placement_diagnostics,
                            "setupCounters": setup_snap,
                            "counters": counters_snap,
                            "trace": trace_snap,
                            "traceDroppedCount": dropped_snap,
                        }));
                    }
                    Ok(FixtureControlCommand::MoveChild { req_id, role, x, y }) => {
                        let target_child = match role.as_str() {
                            "button" => Some(btn_hwnd),
                            "public_edit" => Some(edit_hwnd),
                            "password_edit" => Some(pwd_hwnd),
                            _ => None,
                        };
                        let mut moved = false;
                        if let Some(ch) = target_child {
                            moved = SetWindowPos(
                                ch,
                                None,
                                x,
                                y,
                                0,
                                0,
                                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
                            )
                            .is_ok();
                        }
                        while PeekMessageW(&mut msg, None, 0, 0, PM_REMOVE).as_bool() {
                            let _ = TranslateMessage(&msg);
                            DispatchMessageW(&msg);
                        }
                        emit_json_line(&serde_json::json!({
                            "status": "move_child_ok",
                            "reqId": req_id,
                            "role": role,
                            "moved": moved,
                        }));
                    }
                    Ok(FixtureControlCommand::MoveMainWindow { req_id, x, y }) => {
                        let moved = SetWindowPos(
                            main_hwnd,
                            None,
                            x,
                            y,
                            0,
                            0,
                            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
                        )
                        .is_ok();
                        while PeekMessageW(&mut msg, None, 0, 0, PM_REMOVE).as_bool() {
                            let _ = TranslateMessage(&msg);
                            DispatchMessageW(&msg);
                        }
                        emit_json_line(&serde_json::json!({
                            "status": "move_main_ok",
                            "reqId": req_id,
                            "moved": moved,
                        }));
                    }
                    Ok(FixtureControlCommand::SpawnOwnedModal {
                        req_id,
                        slot,
                        x,
                        y,
                        width,
                        height,
                    }) => {
                        let modal_title = format!("RynOwnedModal_{}_{}", slot, nonce);
                        let modal_wide = to_wide_null(&modal_title);
                        let modal_hwnd = CreateWindowExW(
                            WINDOW_EX_STYLE(0),
                            PCWSTR(class_wide.as_ptr()),
                            PCWSTR(modal_wide.as_ptr()),
                            WS_OVERLAPPEDWINDOW,
                            x,
                            y,
                            width.max(120),
                            height.max(90),
                            Some(main_hwnd),
                            None,
                            Some(hinstance),
                            None,
                        )
                        .expect("Failed to create owned modal popup");
                        let _ = ShowWindow(modal_hwnd, SW_SHOWNOACTIVATE);
                        while PeekMessageW(&mut msg, None, 0, 0, PM_REMOVE).as_bool() {
                            let _ = TranslateMessage(&msg);
                            DispatchMessageW(&msg);
                        }
                        if slot == "B" {
                            if let Some(old) = modal_b_hwnd.replace(modal_hwnd) {
                                if IsWindow(Some(old)).as_bool() {
                                    let _ = DestroyWindow(old);
                                }
                            }
                        } else if let Some(old) = modal_a_hwnd.replace(modal_hwnd) {
                            if IsWindow(Some(old)).as_bool() {
                                let _ = DestroyWindow(old);
                            }
                        }
                        emit_json_line(&serde_json::json!({
                            "status": "modal_ready",
                            "reqId": req_id,
                            "slot": slot,
                            "modalHwnd": hwnd_to_u64(modal_hwnd).to_string(),
                            "ownerHwnd": hwnd_to_u64(main_hwnd).to_string(),
                            "pid": pid,
                            "title": modal_title,
                        }));
                    }
                    Ok(FixtureControlCommand::CloseOwnedModal { req_id, slot }) => {
                        let target_opt = if slot == "B" {
                            modal_b_hwnd.take()
                        } else {
                            modal_a_hwnd.take()
                        };
                        if let Some(mh) = target_opt {
                            if IsWindow(Some(mh)).as_bool() {
                                let _ = ShowWindow(mh, SW_HIDE);
                                let _ = DestroyWindow(mh);
                            }
                        }
                        emit_json_line(&serde_json::json!({
                            "status": "modal_closed",
                            "reqId": req_id,
                            "slot": slot,
                        }));
                    }
                    Ok(FixtureControlCommand::Shutdown { req_id }) => {
                        let (phase_snap, setup_snap, counters_snap, trace_snap, dropped_snap) = {
                            let st = state_lock().lock().unwrap();
                            (
                                st.phase,
                                st.setup_counters_frozen.clone(),
                                st.counters.clone(),
                                st.trace.clone(),
                                st.trace_dropped_count,
                            )
                        };
                        emit_json_line(&serde_json::json!({
                            "status": "shutting_down",
                            "reqId": req_id,
                            "phase": phase_snap,
                            "placement": placement_diagnostics,
                            "setupCounters": setup_snap,
                            "counters": counters_snap,
                            "trace": trace_snap,
                            "traceDroppedCount": dropped_snap,
                        }));
                        running = false;
                        break;
                    }
                    Err(TryRecvError::Empty) => break,
                    Err(TryRecvError::Disconnected) => break,
                }
            }

            std::thread::sleep(Duration::from_millis(2));
        }

        // Clean up ONLY our own fixture-created handles
        if let Some(mh) = modal_b_hwnd.take() {
            if IsWindow(Some(mh)).as_bool() {
                let _ = DestroyWindow(mh);
            }
        }
        if let Some(mh) = modal_a_hwnd.take() {
            if IsWindow(Some(mh)).as_bool() {
                let _ = DestroyWindow(mh);
            }
        }
        if IsWindow(Some(main_hwnd)).as_bool() {
            let _ = DestroyWindow(main_hwnd);
        }
    }
}

#[allow(dead_code)]
fn _keep_u64_to_hwnd_linked(v: u64) -> HWND {
    u64_to_hwnd(v)
}

#[cfg(test)]
fn new_ready_test_state_mutex() -> Mutex<FixtureRuntimeState> {
    let mut st = FixtureRuntimeState::new();
    st.phase = FixtureLifecyclePhase::Ready;
    st.setup_counters_frozen = st.counters.clone();
    Mutex::new(st)
}

#[cfg(test)]
fn make_wheel_wparam(delta: i16) -> usize {
    ((delta as u16 as usize) << 16) & 0xFFFF_0000
}

#[cfg(test)]
#[test]
fn test_fixture_wheel_child_to_main_forward_counts_canonical_once_and_retains_raw_trace() {
    let state_mutex = new_ready_test_state_mutex();
    let tid = 1001u32;
    let wp_neg120 = make_wheel_wparam(-120);
    let lp_pt: isize = (80 << 16) | 80;

    with_scoped_child_wheel_forwarding(
        &state_mutex,
        tid,
        "public_edit",
        ID_PUBLIC_EDIT,
        WM_MOUSEWHEEL,
        wp_neg120,
        lp_pt,
        || {
            let mut st = state_mutex.lock().unwrap();
            st.on_main_wheel_message(tid, WM_MOUSEWHEEL, wp_neg120, lp_pt);
        },
    );

    let st = state_mutex.lock().unwrap();
    assert!(
        st.active_wheel_forwarding_stack.is_empty(),
        "ScopedWheelForwardingGuard::drop must pop the frame on scope exit"
    );
    assert_eq!(st.counters.mouse_wheel_vertical_count, 1);
    assert_eq!(st.counters.mouse_wheel_vertical_delta_sum, -120);
    assert_eq!(st.counters.raw_mouse_wheel_vertical_message_count, 2);
    assert_eq!(st.counters.raw_mouse_wheel_vertical_delta_sum, -240);
    assert_eq!(st.counters.forwarded_mouse_wheel_vertical_count, 1);
    assert_eq!(st.counters.forwarded_mouse_wheel_vertical_delta_sum, -120);
    assert_eq!(st.trace_dropped_count, 0);
    assert_eq!(st.trace.len(), 2);

    assert_eq!(st.trace[0].seq, 1);
    assert_eq!(st.trace[0].phase, FixtureLifecyclePhase::Ready);
    assert!(!st.trace[0].is_setup_phase);
    assert_eq!(st.trace[0].source, "child_subclass");
    assert_eq!(st.trace[0].control_role, "public_edit");
    assert_eq!(st.trace[0].category, "mouse_wheel");
    assert_eq!(st.trace[0].message, "WM_MOUSEWHEEL");
    assert_eq!(st.trace[0].control_id, ID_PUBLIC_EDIT);
    assert_eq!(st.trace[0].notify_code, None);
    assert_eq!(st.trace[0].field_incremented, "mouseWheelVerticalCount");
    assert_eq!(st.trace[0].new_field_count, 1);

    assert_eq!(st.trace[1].seq, 2);
    assert_eq!(st.trace[1].phase, FixtureLifecyclePhase::Ready);
    assert!(!st.trace[1].is_setup_phase);
    assert_eq!(st.trace[1].source, "main_wnd_proc");
    assert_eq!(st.trace[1].control_role, "main_window");
    assert_eq!(st.trace[1].category, "mouse_wheel");
    assert_eq!(st.trace[1].message, "WM_MOUSEWHEEL");
    assert_eq!(st.trace[1].control_id, 0);
    assert_eq!(
        st.trace[1].notify_code.as_deref(),
        Some("forwarded_from_child")
    );
    assert_eq!(
        st.trace[1].field_incremented,
        "rawMouseWheelVerticalMessageCount"
    );
    assert_eq!(st.trace[1].new_field_count, 2);
}

#[cfg(test)]
#[test]
fn test_fixture_wheel_consecutive_equal_child_wheels_and_parent_only_wheel() {
    let state_mutex = new_ready_test_state_mutex();
    let tid = 1002u32;
    let wp_neg120 = make_wheel_wparam(-120);
    let wp_pos120 = make_wheel_wparam(120);
    let lp_pt: isize = (80 << 16) | 80;

    // Two consecutive genuine child wheel inputs with identical (tid, msg, wparam, lparam)
    // that each forward to main_wnd_proc must count 2 canonical and 4 raw.
    for _ in 0..2 {
        with_scoped_child_wheel_forwarding(
            &state_mutex,
            tid,
            "public_edit",
            ID_PUBLIC_EDIT,
            WM_MOUSEWHEEL,
            wp_neg120,
            lp_pt,
            || {
                let mut st = state_mutex.lock().unwrap();
                st.on_main_wheel_message(tid, WM_MOUSEWHEEL, wp_neg120, lp_pt);
            },
        );
    }

    {
        let st = state_mutex.lock().unwrap();
        assert!(st.active_wheel_forwarding_stack.is_empty());
        assert_eq!(st.counters.mouse_wheel_vertical_count, 2);
        assert_eq!(st.counters.mouse_wheel_vertical_delta_sum, -240);
        assert_eq!(st.counters.raw_mouse_wheel_vertical_message_count, 4);
        assert_eq!(st.counters.raw_mouse_wheel_vertical_delta_sum, -480);
        assert_eq!(st.counters.forwarded_mouse_wheel_vertical_count, 2);
        assert_eq!(st.counters.forwarded_mouse_wheel_vertical_delta_sum, -240);
        assert_eq!(st.trace.len(), 4);
    }

    // Direct parent-only wheel event (no active child forwarding scope) counts once in both canonical and raw.
    {
        let mut st = state_mutex.lock().unwrap();
        st.on_main_wheel_message(tid, WM_MOUSEWHEEL, wp_pos120, lp_pt);
        assert_eq!(st.counters.mouse_wheel_vertical_count, 3);
        assert_eq!(st.counters.mouse_wheel_vertical_delta_sum, -120);
        assert_eq!(st.counters.raw_mouse_wheel_vertical_message_count, 5);
        assert_eq!(st.counters.raw_mouse_wheel_vertical_delta_sum, -360);
        assert_eq!(st.counters.forwarded_mouse_wheel_vertical_count, 2);
        assert_eq!(st.counters.forwarded_mouse_wheel_vertical_delta_sum, -240);
        assert_eq!(st.trace.len(), 5);
        assert_eq!(st.trace[4].source, "main_wnd_proc");
        assert_eq!(st.trace[4].notify_code, None);
        assert_eq!(st.trace[4].field_incremented, "mouseWheelVerticalCount");
        assert_eq!(st.trace[4].new_field_count, 3);
    }
}

#[cfg(test)]
#[test]
fn test_fixture_wheel_nested_different_top_frame_does_not_consume_older_matching_frame() {
    let state_mutex = new_ready_test_state_mutex();
    let tid = 1003u32;
    let wp_outer_neg120 = make_wheel_wparam(-120);
    let wp_inner_pos120 = make_wheel_wparam(120);
    let lp_outer: isize = (80 << 16) | 80;
    let lp_inner: isize = (140 << 16) | 40;

    with_scoped_child_wheel_forwarding(
        &state_mutex,
        tid,
        "public_edit",
        ID_PUBLIC_EDIT,
        WM_MOUSEWHEEL,
        wp_outer_neg120,
        lp_outer,
        || {
            // Nested child wheel scope with DIFFERENT parameters is now the active TOP frame.
            with_scoped_child_wheel_forwarding(
                &state_mutex,
                tid,
                "button",
                ID_BUTTON,
                WM_MOUSEWHEEL,
                wp_inner_pos120,
                lp_inner,
                || {
                    let mut st = state_mutex.lock().unwrap();
                    assert_eq!(st.counters.mouse_wheel_vertical_count, 2);
                    assert_eq!(st.counters.raw_mouse_wheel_vertical_message_count, 2);
                    assert_eq!(st.counters.forwarded_mouse_wheel_vertical_count, 0);

                    // While the nested inner frame is on TOP, a parent wheel arrives with parameters
                    // matching the OLDER outer frame (wp_outer_neg120, lp_outer).
                    // Because only the active TOP frame can classify its own synchronous forward,
                    // this MUST NOT consume the older frame and MUST count as a canonical parent wheel.
                    st.on_main_wheel_message(tid, WM_MOUSEWHEEL, wp_outer_neg120, lp_outer);
                    assert_eq!(
                        st.counters.mouse_wheel_vertical_count, 3,
                        "Older unconsumed frame beneath a nested different top frame must not suppress parent wheel"
                    );
                    assert_eq!(st.counters.mouse_wheel_vertical_delta_sum, -120);
                    assert_eq!(st.counters.raw_mouse_wheel_vertical_message_count, 3);
                    assert_eq!(
                        st.counters.forwarded_mouse_wheel_vertical_count, 0,
                        "Older frame must not be marked consumed while nested top frame is active"
                    );
                    assert_eq!(st.trace.last().unwrap().notify_code, None);
                    assert_eq!(
                        st.trace.last().unwrap().field_incremented,
                        "mouseWheelVerticalCount"
                    );

                    // Now the nested TOP frame's own forwarded wheel arrives -> classified as forwarded.
                    st.on_main_wheel_message(tid, WM_MOUSEWHEEL, wp_inner_pos120, lp_inner);
                    assert_eq!(st.counters.mouse_wheel_vertical_count, 3);
                    assert_eq!(st.counters.raw_mouse_wheel_vertical_message_count, 4);
                    assert_eq!(st.counters.forwarded_mouse_wheel_vertical_count, 1);
                    assert_eq!(st.counters.forwarded_mouse_wheel_vertical_delta_sum, 120);
                },
            );

            // Inner guard has dropped; outer frame is once again the active TOP frame and still unconsumed.
            // Its synchronous forwarded parent wheel now arrives -> classified as forwarded.
            let mut st = state_mutex.lock().unwrap();
            st.on_main_wheel_message(tid, WM_MOUSEWHEEL, wp_outer_neg120, lp_outer);
            assert_eq!(st.counters.mouse_wheel_vertical_count, 3);
            assert_eq!(st.counters.mouse_wheel_vertical_delta_sum, -120);
            assert_eq!(st.counters.raw_mouse_wheel_vertical_message_count, 5);
            assert_eq!(st.counters.raw_mouse_wheel_vertical_delta_sum, -120);
            assert_eq!(st.counters.forwarded_mouse_wheel_vertical_count, 2);
            assert_eq!(st.counters.forwarded_mouse_wheel_vertical_delta_sum, 0);
        },
    );

    let st = state_mutex.lock().unwrap();
    assert!(st.active_wheel_forwarding_stack.is_empty());
}

#[cfg(test)]
#[test]
fn test_fixture_wheel_second_parent_in_same_scope_axes_and_guard_drop_restoration() {
    let state_mutex = new_ready_test_state_mutex();
    let tid = 1004u32;
    let wp_neg120 = make_wheel_wparam(-120);
    let wp_pos120 = make_wheel_wparam(120);
    let lp_pt: isize = (80 << 16) | 80;

    // 1. Inside a vertical child wheel scope:
    //    - unrelated nested message (WM_MOUSEMOVE) does not disturb forwarding frame
    //    - horizontal parent wheel (WM_MOUSEHWHEEL) does not match vertical frame and counts as canonical horizontal
    //    - first matching vertical parent wheel is classified as forwarded
    //    - second identical vertical parent wheel in the SAME scope is NOT suppressed (counts as canonical)
    with_scoped_child_wheel_forwarding(
        &state_mutex,
        tid,
        "public_edit",
        ID_PUBLIC_EDIT,
        WM_MOUSEWHEEL,
        wp_neg120,
        lp_pt,
        || {
            let mut st = state_mutex.lock().unwrap();
            st.on_main_mousemove();
            assert_eq!(st.counters.mouse_move_count, 1);

            st.on_main_wheel_message(tid, WM_MOUSEHWHEEL, wp_pos120, lp_pt);
            assert_eq!(st.counters.mouse_wheel_horizontal_count, 1);
            assert_eq!(st.counters.mouse_wheel_horizontal_delta_sum, 120);
            assert_eq!(st.counters.raw_mouse_wheel_horizontal_message_count, 1);
            assert_eq!(st.counters.forwarded_mouse_wheel_horizontal_count, 0);

            st.on_main_wheel_message(tid, WM_MOUSEWHEEL, wp_neg120, lp_pt);
            assert_eq!(st.counters.mouse_wheel_vertical_count, 1);
            assert_eq!(st.counters.forwarded_mouse_wheel_vertical_count, 1);
            assert_eq!(st.counters.raw_mouse_wheel_vertical_message_count, 2);

            // Second parent wheel with same params before scope exits must count as canonical
            st.on_main_wheel_message(tid, WM_MOUSEWHEEL, wp_neg120, lp_pt);
            assert_eq!(st.counters.mouse_wheel_vertical_count, 2);
            assert_eq!(st.counters.mouse_wheel_vertical_delta_sum, -240);
            assert_eq!(st.counters.forwarded_mouse_wheel_vertical_count, 1);
            assert_eq!(st.counters.raw_mouse_wheel_vertical_message_count, 3);
            assert_eq!(st.counters.raw_mouse_wheel_vertical_delta_sum, -360);
        },
    );

    // 2. Horizontal child->parent forwarded wheel
    with_scoped_child_wheel_forwarding(
        &state_mutex,
        tid,
        "password_edit",
        ID_PASSWORD_EDIT,
        WM_MOUSEHWHEEL,
        wp_neg120,
        lp_pt,
        || {
            let mut st = state_mutex.lock().unwrap();
            st.on_main_wheel_message(tid, WM_MOUSEHWHEEL, wp_neg120, lp_pt);
        },
    );

    {
        let st = state_mutex.lock().unwrap();
        assert_eq!(st.counters.mouse_wheel_horizontal_count, 2);
        assert_eq!(st.counters.mouse_wheel_horizontal_delta_sum, 0);
        assert_eq!(st.counters.raw_mouse_wheel_horizontal_message_count, 3);
        assert_eq!(st.counters.raw_mouse_wheel_horizontal_delta_sum, -120);
        assert_eq!(st.counters.forwarded_mouse_wheel_horizontal_count, 1);
        assert_eq!(st.counters.forwarded_mouse_wheel_horizontal_delta_sum, -120);
    }

    // 3. Guard Drop context restoration when child scope returns early without forwarding to parent:
    //    subsequent direct parent wheel with identical parameters must count as canonical.
    let early_res: Result<(), &'static str> = with_scoped_child_wheel_forwarding(
        &state_mutex,
        tid,
        "public_edit",
        ID_PUBLIC_EDIT,
        WM_MOUSEWHEEL,
        wp_neg120,
        lp_pt,
        || Err("early_return_without_parent_forward"),
    );
    assert_eq!(early_res, Err("early_return_without_parent_forward"));

    {
        let mut st = state_mutex.lock().unwrap();
        assert!(
            st.active_wheel_forwarding_stack.is_empty(),
            "Guard Drop must pop unconsumed frame on early return"
        );
        assert_eq!(st.counters.mouse_wheel_vertical_count, 3);
        st.on_main_wheel_message(tid, WM_MOUSEWHEEL, wp_neg120, lp_pt);
        assert_eq!(
            st.counters.mouse_wheel_vertical_count, 4,
            "Direct parent wheel after early-returned child scope must not be suppressed"
        );
        assert_eq!(st.counters.forwarded_mouse_wheel_vertical_count, 1);
        assert_eq!(st.counters.raw_mouse_wheel_vertical_message_count, 5);
    }
}

#[cfg(test)]
#[test]
fn test_fixture_cursor_aware_initial_placement_pure() {
    let preferred = FixtureRectPx::from_xywh(
        ORIGINAL_PREFERRED_OUTER_X,
        ORIGINAL_PREFERRED_OUTER_Y,
        ORIGINAL_PREFERRED_OUTER_WIDTH,
        ORIGINAL_PREFERRED_OUTER_HEIGHT,
    );
    let work_area = FixtureRectPx::from_xywh(0, 0, 1920, 1040);
    let margin = DEFAULT_PLACEMENT_EDGE_MARGIN_PX;
    let clearance = DEFAULT_PLACEMENT_CURSOR_CLEARANCE_PX;

    // 1. Cursor far away -> preserves original preferred rect (120, 120, 460, 320).
    let far_cursor = FixturePointPx::new(900, 700);
    let (rect1, name1) =
        select_cursor_avoiding_window_placement(preferred, work_area, far_cursor, margin, clearance)
            .expect("far cursor should preserve preferred_original");
    assert_eq!(name1, "preferred_original");
    assert_eq!(rect1, preferred);
    assert!(work_area.contains_rect(&rect1));
    assert!(!rect1.contains_point(far_cursor));
    assert!(!rect1.contains_point_with_clearance(far_cursor, clearance));

    // 2. Cursor directly inside public_edit region of original rect (e.g., 200, 200) ->
    //    relocates to work_area_top_right_margin on the SAME monitor work area with identical 460x320 size.
    let overlapping_cursor = FixturePointPx::new(200, 200);
    assert!(preferred.contains_point(overlapping_cursor));
    let (rect2, name2) = select_cursor_avoiding_window_placement(
        preferred,
        work_area,
        overlapping_cursor,
        margin,
        clearance,
    )
    .expect("cursor overlapping preferred rect must select safe work-area fallback");
    assert_eq!(name2, "work_area_top_right_margin");
    assert_eq!(rect2.width, ORIGINAL_PREFERRED_OUTER_WIDTH);
    assert_eq!(rect2.height, ORIGINAL_PREFERRED_OUTER_HEIGHT);
    assert_eq!(rect2, FixtureRectPx::from_xywh(1920 - 24 - 460, 24, 460, 320));
    assert!(work_area.contains_rect(&rect2));
    assert!(!rect2.contains_point(overlapping_cursor));
    assert!(!rect2.contains_point_with_clearance(overlapping_cursor, clearance));

    // 3. Cursor outside preferred rect but within clearance buffer (e.g. 105, 105) ->
    //    also avoids preferred_original and selects work_area_top_right_margin.
    let near_cursor = FixturePointPx::new(105, 105);
    assert!(!preferred.contains_point(near_cursor));
    assert!(preferred.contains_point_with_clearance(near_cursor, clearance));
    let (rect3, name3) = select_cursor_avoiding_window_placement(
        preferred,
        work_area,
        near_cursor,
        margin,
        clearance,
    )
    .expect("cursor within clearance must relocate");
    assert_eq!(name3, "work_area_top_right_margin");
    assert!(!rect3.contains_point_with_clearance(near_cursor, clearance));

    // 4. Shifted work_area where preferred_original is outside work_area and cursor is at top-left ->
    //    selects work_area_top_right_margin inside shifted_work_area.
    let shifted_work_area = FixtureRectPx::from_xywh(200, 200, 1600, 900);
    let cursor_at_top_left_inset = FixturePointPx::new(250, 250);
    let (rect4, name4) = select_cursor_avoiding_window_placement(
        preferred,
        shifted_work_area,
        cursor_at_top_left_inset,
        margin,
        clearance,
    )
    .expect("shifted work area with top-left cursor should select work_area_top_right_margin");
    assert_eq!(name4, "work_area_top_right_margin");
    assert!(shifted_work_area.contains_rect(&rect4));
    assert!(!rect4.contains_point_with_clearance(cursor_at_top_left_inset, clearance));

    // 5. Tight work area (480x340) where inset margins clamp together and cursor is at (10, 10):
    //    exact bottom-right corner without clearance still fits inside work_area and excludes cursor.
    let tight_work_area = FixtureRectPx::from_xywh(0, 0, 480, 340);
    let tight_cursor = FixturePointPx::new(10, 10);
    let (rect5, _name5) = select_cursor_avoiding_window_placement(
        preferred,
        tight_work_area,
        tight_cursor,
        margin,
        clearance,
    )
    .expect("tight work area should still find cursor-excluding corner");
    assert_eq!(rect5.width, 460);
    assert_eq!(rect5.height, 320);
    assert!(tight_work_area.contains_rect(&rect5));
    assert!(!rect5.contains_point(tight_cursor));

    // 6. Impossible placement: work_area exactly 460x320 and cursor is inside it -> returns Err.
    let exact_work_area = FixtureRectPx::from_xywh(0, 0, 460, 320);
    let center_cursor = FixturePointPx::new(230, 160);
    let err_same = select_cursor_avoiding_window_placement(
        preferred,
        exact_work_area,
        center_cursor,
        margin,
        clearance,
    );
    assert!(err_same.is_err());

    // 7. Impossible placement: work_area smaller than 460x320 -> returns Err.
    let tiny_work_area = FixtureRectPx::from_xywh(0, 0, 400, 300);
    let err_tiny = select_cursor_avoiding_window_placement(
        preferred,
        tiny_work_area,
        far_cursor,
        margin,
        clearance,
    );
    assert!(err_tiny.is_err());

    // 8. Negative monitor coordinates (multi-monitor virtual screen left/above primary):
    //    both preferred_original inside negative work_area and fallback when cursor overlaps in negative space.
    let neg_work_area = FixtureRectPx::from_ltrb(-1920, -1080, 0, -40);
    assert!(neg_work_area.is_valid_non_empty());
    let neg_preferred = FixtureRectPx::from_xywh(-1600, -900, 460, 320);
    let neg_far_cursor = FixturePointPx::new(-200, -200);
    let (neg_rect1, neg_name1) = select_cursor_avoiding_window_placement(
        neg_preferred,
        neg_work_area,
        neg_far_cursor,
        margin,
        clearance,
    )
    .expect("negative monitor work_area with far cursor must preserve neg_preferred");
    assert_eq!(neg_name1, "preferred_original");
    assert_eq!(neg_rect1, neg_preferred);
    assert!(neg_rect1.has_exact_size(460, 320));
    assert!(neg_work_area.contains_rect(&neg_rect1));

    let neg_overlap_cursor = FixturePointPx::new(-1500, -800);
    let (neg_rect2, neg_name2) = select_cursor_avoiding_window_placement(
        neg_preferred,
        neg_work_area,
        neg_overlap_cursor,
        margin,
        clearance,
    )
    .expect("negative monitor work_area with overlapping cursor must select safe candidate");
    assert_eq!(neg_name2, "work_area_top_right_margin");
    assert!(neg_rect2.has_exact_size(460, 320));
    assert!(neg_work_area.contains_rect(&neg_rect2));
    assert!(!neg_rect2.contains_point_with_clearance(neg_overlap_cursor, clearance));

    // 9. Invalid, empty, reversed, inconsistent, and overflow rectangles:
    //    saturated i32::MAX must never validate a huge/overflowing rectangle.
    let overflow_ltrb = FixtureRectPx::from_ltrb(i32::MIN, 0, i32::MAX, 320);
    assert!(!overflow_ltrb.is_valid_non_empty());
    assert!(!overflow_ltrb.has_exact_size(i32::MAX, 320));
    assert!(!overflow_ltrb.contains_point(FixturePointPx::new(0, 100)));
    assert!(
        select_cursor_avoiding_window_placement(
            preferred,
            overflow_ltrb,
            far_cursor,
            margin,
            clearance
        )
        .is_err()
    );

    let overflow_xywh = FixtureRectPx::from_xywh(i32::MAX - 100, 0, 460, 320);
    assert!(!overflow_xywh.is_valid_non_empty());
    assert!(!overflow_xywh.has_exact_size(460, 320));

    let empty_rect = FixtureRectPx::from_ltrb(100, 100, 100, 420);
    assert!(!empty_rect.is_valid_non_empty());
    let reversed_rect = FixtureRectPx::from_ltrb(500, 400, 100, 100);
    assert!(!reversed_rect.is_valid_non_empty());
    let inconsistent_manual = FixtureRectPx {
        left: 0,
        top: 0,
        right: 500,
        bottom: 320,
        width: 460,
        height: 320,
    };
    assert!(!inconsistent_manual.is_valid_non_empty());
    assert!(!inconsistent_manual.has_exact_size(460, 320));
    assert!(!work_area.contains_rect(&inconsistent_manual));

    // 10. All 4 edges and 4 corners on exact inclusive boundary vs 1px outside,
    //     and on clearance boundary vs 1px outside clearance.
    //     preferred is left=120, top=120, right=580, bottom=440.
    let exact_boundary_points = [
        FixturePointPx::new(120, 120), // top-left corner
        FixturePointPx::new(580, 120), // top-right corner
        FixturePointPx::new(120, 440), // bottom-left corner
        FixturePointPx::new(580, 440), // bottom-right corner
        FixturePointPx::new(350, 120), // top edge
        FixturePointPx::new(350, 440), // bottom edge
        FixturePointPx::new(120, 280), // left edge
        FixturePointPx::new(580, 280), // right edge
    ];
    for pt in exact_boundary_points {
        assert!(
            preferred.contains_point(pt),
            "inclusive boundary must contain {:?}",
            pt
        );
        let (relocated, cand_name) = select_cursor_avoiding_window_placement(
            preferred,
            work_area,
            pt,
            margin,
            0,
        )
        .expect("exact boundary point in large work_area must relocate away from preferred_original");
        assert_ne!(cand_name, "preferred_original");
        assert!(relocated.has_exact_size(460, 320));
        assert!(work_area.contains_rect(&relocated));
        assert!(!relocated.contains_point(pt));
    }

    let one_px_outside_exact = [
        FixturePointPx::new(119, 120),
        FixturePointPx::new(581, 120),
        FixturePointPx::new(120, 119),
        FixturePointPx::new(120, 441),
        FixturePointPx::new(119, 119),
        FixturePointPx::new(581, 441),
    ];
    for pt in one_px_outside_exact {
        assert!(
            !preferred.contains_point(pt),
            "1px outside exact rect must not be contained in exact rect: {:?}",
            pt
        );
    }

    // With clearance = 24, expanded inclusive box is [96..=604, 96..=464].
    let clearance_boundary_points = [
        FixturePointPx::new(96, 96),   // expanded top-left corner
        FixturePointPx::new(604, 96),  // expanded top-right corner
        FixturePointPx::new(96, 464),  // expanded bottom-left corner
        FixturePointPx::new(604, 464), // expanded bottom-right corner
        FixturePointPx::new(350, 96),  // expanded top edge
        FixturePointPx::new(350, 464), // expanded bottom edge
        FixturePointPx::new(96, 280),  // expanded left edge
        FixturePointPx::new(604, 280), // expanded right edge
    ];
    for pt in clearance_boundary_points {
        assert!(
            preferred.contains_point_with_clearance(pt, clearance),
            "inclusive clearance boundary must contain {:?}",
            pt
        );
        let (_rect_c, name_c) = select_cursor_avoiding_window_placement(
            preferred, work_area, pt, margin, clearance,
        )
        .expect("clearance boundary point should relocate to fallback");
        assert_ne!(name_c, "preferred_original");
    }

    let one_px_outside_clearance = [
        FixturePointPx::new(95, 96),
        FixturePointPx::new(605, 96),
        FixturePointPx::new(96, 95),
        FixturePointPx::new(96, 465),
        FixturePointPx::new(95, 95),
        FixturePointPx::new(605, 465),
    ];
    for pt in one_px_outside_clearance {
        assert!(
            !preferred.contains_point_with_clearance(pt, clearance),
            "1px outside clearance boundary must not be contained: {:?}",
            pt
        );
        let (rect_ok, name_ok) = select_cursor_avoiding_window_placement(
            preferred, work_area, pt, margin, clearance,
        )
        .expect("1px outside clearance should keep preferred_original");
        assert_eq!(name_ok, "preferred_original");
        assert_eq!(rect_ok, preferred);
    }

    // 11. Near-i32 extreme valid work_area and cursor coordinates without overflow:
    let extreme_work_area =
        FixtureRectPx::from_ltrb(i32::MAX - 2000, i32::MIN + 100, i32::MAX - 100, i32::MIN + 1500);
    assert!(extreme_work_area.is_valid_non_empty());
    let extreme_cursor = FixturePointPx::new(i32::MAX - 1900, i32::MIN + 200);
    let (ext_rect, ext_name) = select_cursor_avoiding_window_placement(
        preferred,
        extreme_work_area,
        extreme_cursor,
        i32::MAX, // huge margin to test i64 clamping without overflow
        i32::MAX, // huge clearance falls back cleanly to Tier 2 exact exclusion
    )
    .expect("extreme coordinates and margins must not overflow and must find Tier 2 candidate");
    assert!(!ext_name.is_empty());
    assert_ne!(ext_name, "preferred_original");
    assert!(ext_rect.has_exact_size(460, 320));
    assert!(extreme_work_area.contains_rect(&ext_rect));
    assert!(!ext_rect.contains_point(extreme_cursor));

    // 12. Confirmed per-monitor DPI awareness and stage validation helper checks:
    assert!(is_confirmed_per_monitor_dpi_awareness("per_monitor_aware_v2"));
    assert!(is_confirmed_per_monitor_dpi_awareness("per_monitor_aware"));
    assert!(!is_confirmed_per_monitor_dpi_awareness("system_aware"));
    assert!(!is_confirmed_per_monitor_dpi_awareness("system_dpi_aware"));
    assert!(!is_confirmed_per_monitor_dpi_awareness("unaware"));
    assert!(!is_confirmed_per_monitor_dpi_awareness(""));

    let valid_client = FixtureRectPx::from_xywh(128, 151, 444, 281);
    assert!(
        validate_stage_window_and_cursor_invariants(
            "pre_show",
            "per_monitor_aware_v2",
            96,
            work_area,
            preferred,
            preferred,
            valid_client,
            far_cursor,
        )
        .is_ok()
    );
    // Reject wrong outer size at post_pump_ready
    let wrong_size_actual = FixtureRectPx::from_xywh(120, 120, 459, 320);
    assert!(
        validate_stage_window_and_cursor_invariants(
            "post_pump_ready",
            "per_monitor_aware_v2",
            96,
            work_area,
            preferred,
            wrong_size_actual,
            valid_client,
            far_cursor,
        )
        .is_err()
    );
    // Reject cursor on boundary of actual_window_rect at post_pump_ready
    assert!(
        validate_stage_window_and_cursor_invariants(
            "post_pump_ready",
            "per_monitor_aware_v2",
            96,
            work_area,
            preferred,
            preferred,
            valid_client,
            FixturePointPx::new(580, 440),
        )
        .is_err()
    );
}

