use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;
use ryn_computer_use::engine::{OperationControlTable, PhysicalActorEngine};
use ryn_computer_use::errors::{DiagnosticReason, ErrorCode, MutationState};
use ryn_computer_use::protocol::{
    ElementTarget, EventEnvelope, PointTarget, RequestEnvelope, RequestId, UiaElement, WindowRef,
    WIRE_VERSION,
};
use ryn_computer_use::snapshot::csprng_hex;
use ryn_computer_use::windows::{
    get_window_class_name, get_window_pid_tid, get_window_title, query_process_creation_time,
    query_process_executable_path, u64_to_hwnd,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::time::Duration;
use windows::Win32::UI::WindowsAndMessaging::{GetAncestor, GetParent, IsChild, IsWindow, GA_ROOT};

const ID_PUBLIC_EDIT: usize = 102;
const ID_PASSWORD_EDIT: usize = 103;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum FixtureLifecyclePhase {
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

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct FixturePointPx {
    pub x: i32,
    pub y: i32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct FixtureRectPx {
    pub left: i32,
    pub top: i32,
    pub right: i32,
    pub bottom: i32,
    pub width: i32,
    pub height: i32,
}

impl FixtureRectPx {
    const fn is_valid_non_empty(&self) -> bool {
        let w = (self.right as i64) - (self.left as i64);
        let h = (self.bottom as i64) - (self.top as i64);
        self.width > 0
            && self.height > 0
            && w == (self.width as i64)
            && h == (self.height as i64)
    }

    const fn has_exact_size(&self, expected_width: i32, expected_height: i32) -> bool {
        expected_width > 0
            && expected_height > 0
            && self.is_valid_non_empty()
            && self.width == expected_width
            && self.height == expected_height
    }

    const fn contains_point(&self, pt: FixturePointPx) -> bool {
        if !self.is_valid_non_empty() {
            return false;
        }
        pt.x >= self.left && pt.x <= self.right && pt.y >= self.top && pt.y <= self.bottom
    }

    const fn contains_rect(&self, inner: &FixtureRectPx) -> bool {
        self.is_valid_non_empty()
            && inner.is_valid_non_empty()
            && inner.left >= self.left
            && inner.top >= self.top
            && inner.right <= self.right
            && inner.bottom <= self.bottom
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct FixturePlacementDiagnostics {
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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct FixtureMouseMoveTraceDetail {
    pub wparam_flags: usize,
    pub lparam_raw: isize,
    pub client_x: i32,
    pub client_y: i32,
    pub screen_x: Option<i32>,
    pub screen_y: Option<i32>,
    pub cursor_x: Option<i32>,
    pub cursor_y: Option<i32>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct FixtureTraceEntry {
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

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct FixtureCounters {
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
    pub public_edit_change_count: u64,
    pub setup_public_edit_change_count: u64,
    pub runtime_public_edit_change_count: u64,
    pub public_edit_settext_count: u64,
    pub public_edit_text: String,
    pub public_edit_chars: Vec<u32>,
    pub public_edit_last_vk: u32,
    pub mouse_move_count: u64,
    pub mouse_wheel_vertical_count: u64,
    pub mouse_wheel_vertical_delta_sum: i64,
    #[serde(default)]
    pub raw_mouse_wheel_vertical_message_count: u64,
    #[serde(default)]
    pub raw_mouse_wheel_vertical_delta_sum: i64,
    #[serde(default)]
    pub forwarded_mouse_wheel_vertical_count: u64,
    #[serde(default)]
    pub forwarded_mouse_wheel_vertical_delta_sum: i64,
    pub mouse_wheel_horizontal_count: u64,
    pub mouse_wheel_horizontal_delta_sum: i64,
    #[serde(default)]
    pub raw_mouse_wheel_horizontal_message_count: u64,
    #[serde(default)]
    pub raw_mouse_wheel_horizontal_delta_sum: i64,
    #[serde(default)]
    pub forwarded_mouse_wheel_horizontal_count: u64,
    #[serde(default)]
    pub forwarded_mouse_wheel_horizontal_delta_sum: i64,
    pub password_edit_focus_gained_count: u64,
    pub password_edit_focus_lost_count: u64,
    pub password_edit_lbutton_down_count: u64,
    pub password_edit_lbutton_up_count: u64,
    pub password_edit_keydown_count: u64,
    pub password_edit_keyup_count: u64,
    pub password_edit_char_count: u64,
    pub password_edit_change_count: u64,
    pub setup_password_edit_change_count: u64,
    pub runtime_password_edit_change_count: u64,
    pub password_edit_settext_count: u64,
}

impl FixtureCounters {
    /// Sum of all physical key/char/button/wheel/mouse-move/focus and WM_SETTEXT counters
    /// from process start. Must be strictly 0 in all no-input tests (both during setup and runtime).
    fn total_physical_focus_mouse_key_wheel_and_settext_mutations(&self) -> u64 {
        self.main_focus_gained_count
            + self.main_focus_lost_count
            + self.button_click_count
            + self.button_lbutton_down_count
            + self.button_lbutton_up_count
            + self.button_lbutton_dblclk_count
            + self.button_rbutton_down_count
            + self.button_rbutton_up_count
            + self.button_focus_gained_count
            + self.button_focus_lost_count
            + self.public_edit_focus_gained_count
            + self.public_edit_focus_lost_count
            + self.public_edit_lbutton_down_count
            + self.public_edit_lbutton_up_count
            + self.public_edit_keydown_count
            + self.public_edit_keyup_count
            + self.public_edit_char_count
            + self.public_edit_settext_count
            + self.mouse_move_count
            + self.mouse_wheel_vertical_count
            + self.mouse_wheel_horizontal_count
            + self.password_edit_focus_gained_count
            + self.password_edit_focus_lost_count
            + self.password_edit_lbutton_down_count
            + self.password_edit_lbutton_up_count
            + self.password_edit_keydown_count
            + self.password_edit_keyup_count
            + self.password_edit_char_count
            + self.password_edit_settext_count
    }

    /// Sum of all post-ready runtime mutations (all physical/focus/mouse/key/wheel/settext counters
    /// plus any runtime EN_CHANGE on public or password edit). Must be strictly 0 in all no-input tests.
    fn total_runtime_mutations_after_setup(&self) -> u64 {
        self.total_physical_focus_mouse_key_wheel_and_settext_mutations()
            + self.runtime_public_edit_change_count
            + self.runtime_password_edit_change_count
    }

    /// Raw all-lifetime event sum including setup initial-text EN_CHANGE notifications
    /// and any child->parent forwarded wheel messages.
    fn total_all_lifetime_raw_events(&self) -> u64 {
        self.total_physical_focus_mouse_key_wheel_and_settext_mutations()
            + self.forwarded_mouse_wheel_vertical_count
            + self.forwarded_mouse_wheel_horizontal_count
            + self.public_edit_change_count
            + self.password_edit_change_count
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct FixtureCounterSnapshot {
    pub phase: FixtureLifecyclePhase,
    pub setup_counters: FixtureCounters,
    pub counters: FixtureCounters,
    pub trace: Vec<FixtureTraceEntry>,
    pub trace_dropped_count: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct FixtureChildInfo {
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
struct FixtureReadyReport {
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

impl FixtureReadyReport {
    fn to_snapshot(&self) -> FixtureCounterSnapshot {
        FixtureCounterSnapshot {
            phase: self.phase,
            setup_counters: self.setup_counters.clone(),
            counters: self.counters.clone(),
            trace: self.trace.clone(),
            trace_dropped_count: self.trace_dropped_count,
        }
    }
}

fn verify_ready_placement_invariants(
    stage_label: &str,
    placement: &FixturePlacementDiagnostics,
) -> Result<(), String> {
    if !matches!(
        placement.dpi_awareness.as_str(),
        "per_monitor_aware_v2" | "per_monitor_aware"
    ) || placement.pre_show_window_dpi == 0
        || placement.window_dpi == 0
        || placement.pre_show_window_dpi != placement.window_dpi
    {
        return Err(format!(
            "[{}] invalid DPI diagnostics: dpi_awareness='{}', pre_show_window_dpi={}, window_dpi={}",
            stage_label,
            placement.dpi_awareness,
            placement.pre_show_window_dpi,
            placement.window_dpi
        ));
    }
    if placement.placement_candidate.trim().is_empty() {
        return Err(format!(
            "[{}] placement_candidate must not be empty",
            stage_label
        ));
    }
    let expected_original = FixtureRectPx {
        left: 120,
        top: 120,
        right: 580,
        bottom: 440,
        width: 460,
        height: 320,
    };
    if placement.original_preferred_rect != expected_original {
        return Err(format!(
            "[{}] original_preferred_rect {:?} != expected {:?}",
            stage_label, placement.original_preferred_rect, expected_original
        ));
    }
    if !placement.selected_monitor_rect.is_valid_non_empty()
        || !placement.selected_monitor_work_area.is_valid_non_empty()
        || !placement
            .selected_monitor_rect
            .contains_rect(&placement.selected_monitor_work_area)
    {
        return Err(format!(
            "[{}] selected_monitor_work_area {:?} must be valid and inside selected_monitor_rect {:?}",
            stage_label, placement.selected_monitor_work_area, placement.selected_monitor_rect
        ));
    }
    if !placement.requested_outer_rect.has_exact_size(460, 320) {
        return Err(format!(
            "[{}] requested_outer_rect {:?} must preserve valid 460x320 outer size",
            stage_label, placement.requested_outer_rect
        ));
    }
    if !placement.pre_show_window_rect.has_exact_size(460, 320) {
        return Err(format!(
            "[{}] pre_show_window_rect {:?} must preserve valid 460x320 outer size",
            stage_label, placement.pre_show_window_rect
        ));
    }
    if !placement.actual_window_rect.has_exact_size(460, 320) {
        return Err(format!(
            "[{}] actual_window_rect {:?} must preserve valid 460x320 outer size",
            stage_label, placement.actual_window_rect
        ));
    }
    if !placement
        .selected_monitor_work_area
        .contains_rect(&placement.requested_outer_rect)
    {
        return Err(format!(
            "[{}] requested_outer_rect {:?} is not contained in selected_monitor_work_area {:?}",
            stage_label, placement.requested_outer_rect, placement.selected_monitor_work_area
        ));
    }
    if !placement
        .selected_monitor_work_area
        .contains_rect(&placement.pre_show_window_rect)
    {
        return Err(format!(
            "[{}] pre_show_window_rect {:?} is not contained in selected_monitor_work_area {:?}",
            stage_label, placement.pre_show_window_rect, placement.selected_monitor_work_area
        ));
    }
    if !placement
        .selected_monitor_work_area
        .contains_rect(&placement.actual_window_rect)
    {
        return Err(format!(
            "[{}] actual_window_rect {:?} is not contained in selected_monitor_work_area {:?}",
            stage_label, placement.actual_window_rect, placement.selected_monitor_work_area
        ));
    }
    if placement
        .requested_outer_rect
        .contains_point(placement.creation_cursor)
    {
        return Err(format!(
            "[{}] requested_outer_rect {:?} must exclude creation_cursor {:?}",
            stage_label, placement.requested_outer_rect, placement.creation_cursor
        ));
    }
    if placement
        .requested_outer_rect
        .contains_point(placement.pre_show_cursor)
        || placement
            .pre_show_window_rect
            .contains_point(placement.pre_show_cursor)
    {
        return Err(format!(
            "[{}] pre_show_cursor {:?} overlapped requested_outer_rect {:?} or pre_show_window_rect {:?}",
            stage_label,
            placement.pre_show_cursor,
            placement.requested_outer_rect,
            placement.pre_show_window_rect
        ));
    }
    if placement
        .requested_outer_rect
        .contains_point(placement.ready_cursor)
        || placement
            .actual_window_rect
            .contains_point(placement.ready_cursor)
    {
        return Err(format!(
            "[{}] ready_cursor {:?} overlapped requested_outer_rect {:?} or actual_window_rect {:?}",
            stage_label,
            placement.ready_cursor,
            placement.requested_outer_rect,
            placement.actual_window_rect
        ));
    }
    if !placement
        .pre_show_window_rect
        .contains_rect(&placement.pre_show_client_bounds)
    {
        return Err(format!(
            "[{}] pre_show_client_bounds {:?} must be valid and inside pre_show_window_rect {:?}",
            stage_label, placement.pre_show_client_bounds, placement.pre_show_window_rect
        ));
    }
    if !placement
        .actual_window_rect
        .contains_rect(&placement.actual_client_bounds)
    {
        return Err(format!(
            "[{}] actual_client_bounds {:?} must be valid and inside actual_window_rect {:?}",
            stage_label, placement.actual_client_bounds, placement.actual_window_rect
        ));
    }
    Ok(())
}

/// Pure, strict verifier for the fixture's setup baseline and zero-input invariants.
/// - Requires all physical key/char/button/wheel/mouse-move/focus/settext counters to be 0 from process start.
/// - Requires all raw and forwarded wheel message counts and deltas to be 0.
/// - Requires all runtime (post-ready) EN_CHANGE counters to be 0.
/// - Requires raw all-lifetime EN_CHANGE counters (`public_edit_change_count == 1`, `password_edit_change_count == 1`)
///   to match `setup_public_edit_change_count == 1` and `setup_password_edit_change_count == 1`.
/// - Requires the bounded message trace to contain the exact 2 synchronous setup `WM_COMMAND(EN_CHANGE)`
///   notifications in order (`seq 1` in `create_public` for `public_edit` control 102, `seq 2` in
///   `create_password` for `password_edit` control 103) with zero dropped trace entries.
/// - Requires strict equality of all counter fields and trace entries against the frozen ready baseline.
fn verify_setup_and_zero_input_invariants(
    stage_label: &str,
    ready_baseline: &FixtureCounterSnapshot,
    current: &FixtureCounterSnapshot,
) -> Result<(), String> {
    if current.phase != FixtureLifecyclePhase::Ready {
        return Err(format!(
            "[{}] expected fixture phase Ready, got {:?}",
            stage_label, current.phase
        ));
    }

    let c = &current.counters;
    let phys_total = c.total_physical_focus_mouse_key_wheel_and_settext_mutations();
    if phys_total != 0 {
        return Err(format!(
            "[{}] physical/focus/mouse/key/wheel/settext counter sum is {} (expected 0); counters={} trace={}",
            stage_label,
            phys_total,
            serde_json::to_string(c).unwrap_or_default(),
            serde_json::to_string(&current.trace).unwrap_or_default()
        ));
    }

    if c.raw_mouse_wheel_vertical_message_count != 0
        || c.raw_mouse_wheel_vertical_delta_sum != 0
        || c.forwarded_mouse_wheel_vertical_count != 0
        || c.forwarded_mouse_wheel_vertical_delta_sum != 0
        || c.raw_mouse_wheel_horizontal_message_count != 0
        || c.raw_mouse_wheel_horizontal_delta_sum != 0
        || c.forwarded_mouse_wheel_horizontal_count != 0
        || c.forwarded_mouse_wheel_horizontal_delta_sum != 0
    {
        return Err(format!(
            "[{}] unexpected raw/forwarded wheel activity in zero-input check; counters={}",
            stage_label,
            serde_json::to_string(c).unwrap_or_default()
        ));
    }

    if c.runtime_public_edit_change_count != 0 || c.runtime_password_edit_change_count != 0 {
        return Err(format!(
            "[{}] unexpected runtime EN_CHANGE after ready (runtimePublicEditChangeCount={}, runtimePasswordEditChangeCount={}); counters={} trace={}",
            stage_label,
            c.runtime_public_edit_change_count,
            c.runtime_password_edit_change_count,
            serde_json::to_string(c).unwrap_or_default(),
            serde_json::to_string(&current.trace).unwrap_or_default()
        ));
    }

    if c.setup_public_edit_change_count != 1
        || c.public_edit_change_count != 1
        || c.setup_password_edit_change_count != 1
        || c.password_edit_change_count != 1
    {
        return Err(format!(
            "[{}] unexpected setup/raw EN_CHANGE counts (setupPublic={}, rawPublic={}, setupPassword={}, rawPassword={}); counters={} trace={}",
            stage_label,
            c.setup_public_edit_change_count,
            c.public_edit_change_count,
            c.setup_password_edit_change_count,
            c.password_edit_change_count,
            serde_json::to_string(c).unwrap_or_default(),
            serde_json::to_string(&current.trace).unwrap_or_default()
        ));
    }

    if c.total_runtime_mutations_after_setup() != 0 {
        return Err(format!(
            "[{}] total_runtime_mutations_after_setup is {} (expected 0)",
            stage_label,
            c.total_runtime_mutations_after_setup()
        ));
    }

    if c.total_all_lifetime_raw_events() != 2 {
        return Err(format!(
            "[{}] total_all_lifetime_raw_events is {} (expected 2 from the 2 proven CreateWindowExW setup EN_CHANGE notifications)",
            stage_label,
            c.total_all_lifetime_raw_events()
        ));
    }

    if c.public_edit_text != "InitialPublicValue"
        || !c.public_edit_chars.is_empty()
        || c.public_edit_last_vk != 0
    {
        return Err(format!(
            "[{}] public edit state mutated unexpectedly: text='{}', chars={:?}, last_vk={}",
            stage_label, c.public_edit_text, c.public_edit_chars, c.public_edit_last_vk
        ));
    }

    if current.trace_dropped_count != 0 {
        return Err(format!(
            "[{}] trace_dropped_count is {} (expected 0)",
            stage_label, current.trace_dropped_count
        ));
    }

    let expected_trace = vec![
        FixtureTraceEntry {
            seq: 1,
            phase: FixtureLifecyclePhase::CreatePublic,
            is_setup_phase: true,
            source: "main_wnd_proc".to_string(),
            control_role: "public_edit".to_string(),
            category: "setup_initial_text_notify".to_string(),
            message: "WM_COMMAND".to_string(),
            control_id: ID_PUBLIC_EDIT,
            notify_code: Some("EN_CHANGE".to_string()),
            field_incremented: "publicEditChangeCount".to_string(),
            new_field_count: 1,
            mouse_move_detail: None,
        },
        FixtureTraceEntry {
            seq: 2,
            phase: FixtureLifecyclePhase::CreatePassword,
            is_setup_phase: true,
            source: "main_wnd_proc".to_string(),
            control_role: "password_edit".to_string(),
            category: "setup_initial_text_notify".to_string(),
            message: "WM_COMMAND".to_string(),
            control_id: ID_PASSWORD_EDIT,
            notify_code: Some("EN_CHANGE".to_string()),
            field_incremented: "passwordEditChangeCount".to_string(),
            new_field_count: 1,
            mouse_move_detail: None,
        },
    ];

    if current.trace != expected_trace {
        return Err(format!(
            "[{}] trace does not match exact expected CreateWindowExW setup provenance: actual={} expected={}",
            stage_label,
            serde_json::to_string(&current.trace).unwrap_or_default(),
            serde_json::to_string(&expected_trace).unwrap_or_default()
        ));
    }

    if &current.setup_counters != &ready_baseline.setup_counters {
        return Err(format!(
            "[{}] setup_counters drifted from frozen ready baseline",
            stage_label
        ));
    }

    if &current.counters != &ready_baseline.counters {
        return Err(format!(
            "[{}] counters drifted from immutable ready baseline: current={} baseline={}",
            stage_label,
            serde_json::to_string(&current.counters).unwrap_or_default(),
            serde_json::to_string(&ready_baseline.counters).unwrap_or_default()
        ));
    }

    if &current.trace != &ready_baseline.trace {
        return Err(format!(
            "[{}] trace drifted from immutable ready baseline: current={} baseline={}",
            stage_label,
            serde_json::to_string(&current.trace).unwrap_or_default(),
            serde_json::to_string(&ready_baseline.trace).unwrap_or_default()
        ));
    }

    Ok(())
}

/// Pure self-test verifying that `verify_setup_and_zero_input_invariants` accepts ONLY the exact
/// two-entry `CreateWindowExW` initial-text `EN_CHANGE` setup provenance and rejects any runtime
/// `EN_CHANGE`, `WM_SETTEXT`, focus, mouse movement, mouse click, wheel, or key/char mutation,
/// as well as any miscategorized setup event.
fn verify_pure_counter_and_trace_classifier_invariants() {
    let valid_counters = FixtureCounters {
        public_edit_change_count: 1,
        setup_public_edit_change_count: 1,
        runtime_public_edit_change_count: 0,
        password_edit_change_count: 1,
        setup_password_edit_change_count: 1,
        runtime_password_edit_change_count: 0,
        public_edit_text: "InitialPublicValue".to_string(),
        ..Default::default()
    };
    let valid_trace = vec![
        FixtureTraceEntry {
            seq: 1,
            phase: FixtureLifecyclePhase::CreatePublic,
            is_setup_phase: true,
            source: "main_wnd_proc".to_string(),
            control_role: "public_edit".to_string(),
            category: "setup_initial_text_notify".to_string(),
            message: "WM_COMMAND".to_string(),
            control_id: ID_PUBLIC_EDIT,
            notify_code: Some("EN_CHANGE".to_string()),
            field_incremented: "publicEditChangeCount".to_string(),
            new_field_count: 1,
            mouse_move_detail: None,
        },
        FixtureTraceEntry {
            seq: 2,
            phase: FixtureLifecyclePhase::CreatePassword,
            is_setup_phase: true,
            source: "main_wnd_proc".to_string(),
            control_role: "password_edit".to_string(),
            category: "setup_initial_text_notify".to_string(),
            message: "WM_COMMAND".to_string(),
            control_id: ID_PASSWORD_EDIT,
            notify_code: Some("EN_CHANGE".to_string()),
            field_incremented: "passwordEditChangeCount".to_string(),
            new_field_count: 1,
            mouse_move_detail: None,
        },
    ];
    let baseline = FixtureCounterSnapshot {
        phase: FixtureLifecyclePhase::Ready,
        setup_counters: valid_counters.clone(),
        counters: valid_counters,
        trace: valid_trace,
        trace_dropped_count: 0,
    };

    // 1. Valid baseline passes
    verify_setup_and_zero_input_invariants("pure_valid", &baseline, &baseline)
        .expect("Exact proven setup baseline must pass");

    // 2. Any runtime EN_CHANGE on public edit must fail
    let mut rt_pub = baseline.clone();
    rt_pub.counters.public_edit_change_count = 2;
    rt_pub.counters.runtime_public_edit_change_count = 1;
    assert!(verify_setup_and_zero_input_invariants("pure_rt_pub", &baseline, &rt_pub).is_err());

    // 3. Any runtime EN_CHANGE on password edit must fail
    let mut rt_pwd = baseline.clone();
    rt_pwd.counters.password_edit_change_count = 2;
    rt_pwd.counters.runtime_password_edit_change_count = 1;
    assert!(verify_setup_and_zero_input_invariants("pure_rt_pwd", &baseline, &rt_pwd).is_err());

    // 4. Any WM_SETTEXT on public or password edit must fail
    let mut settext_snap = baseline.clone();
    settext_snap.counters.public_edit_settext_count = 1;
    assert!(
        verify_setup_and_zero_input_invariants("pure_settext", &baseline, &settext_snap).is_err()
    );

    // 5. Any focus gain (even during setup) on main, button, public edit, or password edit must fail
    let mut focus_snap = baseline.clone();
    focus_snap.counters.main_focus_gained_count = 1;
    assert!(verify_setup_and_zero_input_invariants("pure_focus", &baseline, &focus_snap).is_err());

    // 6. Any mouse movement must fail (mouse_move_count is strictly included in the physical safety sum)
    let mut move_snap = baseline.clone();
    move_snap.counters.mouse_move_count = 1;
    assert!(verify_setup_and_zero_input_invariants("pure_mousemove", &baseline, &move_snap).is_err());

    // 7. Any button click or key/char event must fail
    let mut key_snap = baseline.clone();
    key_snap.counters.public_edit_keydown_count = 1;
    assert!(verify_setup_and_zero_input_invariants("pure_keydown", &baseline, &key_snap).is_err());

    // 8. Wrong phase or wrong role/order in setup trace must fail
    let mut wrong_trace = baseline.clone();
    wrong_trace.trace[0].phase = FixtureLifecyclePhase::Show;
    assert!(
        verify_setup_and_zero_input_invariants("pure_wrong_phase", &baseline, &wrong_trace)
            .is_err()
    );

    // 9. Any raw/forwarded wheel count drift must fail zero-input check
    let mut raw_wheel_snap = baseline.clone();
    raw_wheel_snap.counters.raw_mouse_wheel_vertical_message_count = 1;
    assert!(
        verify_setup_and_zero_input_invariants("pure_raw_wheel", &baseline, &raw_wheel_snap)
            .is_err()
    );

    // 10. Placement invariant verifier checks (confirmed per-monitor DPI, 460x320 size,
    //     containment in selected_monitor_work_area, inclusive cursor exclusion, and i64 overflow rejection):
    let valid_outer = FixtureRectPx {
        left: 120,
        top: 120,
        right: 580,
        bottom: 440,
        width: 460,
        height: 320,
    };
    let valid_client = FixtureRectPx {
        left: 128,
        top: 151,
        right: 572,
        bottom: 432,
        width: 444,
        height: 281,
    };
    let valid_placement = FixturePlacementDiagnostics {
        dpi_awareness: "per_monitor_aware_v2".to_string(),
        pre_show_window_dpi: 96,
        window_dpi: 96,
        placement_candidate: "preferred_original".to_string(),
        original_preferred_rect: valid_outer,
        creation_cursor: FixturePointPx { x: 900, y: 700 },
        selected_monitor_rect: FixtureRectPx {
            left: 0,
            top: 0,
            right: 1920,
            bottom: 1080,
            width: 1920,
            height: 1080,
        },
        selected_monitor_work_area: FixtureRectPx {
            left: 0,
            top: 0,
            right: 1920,
            bottom: 1040,
            width: 1920,
            height: 1040,
        },
        requested_outer_rect: valid_outer,
        pre_show_window_rect: valid_outer,
        pre_show_client_bounds: valid_client,
        pre_show_cursor: FixturePointPx { x: 900, y: 700 },
        actual_window_rect: valid_outer,
        actual_client_bounds: valid_client,
        ready_cursor: FixturePointPx { x: 900, y: 700 },
    };
    verify_ready_placement_invariants("pure_placement_valid", &valid_placement)
        .expect("Valid per-monitor placement must pass");

    let mut unconfirmed_dpi = valid_placement.clone();
    unconfirmed_dpi.dpi_awareness = "system_aware".to_string();
    assert!(verify_ready_placement_invariants("pure_unconfirmed_dpi", &unconfirmed_dpi).is_err());

    let mut boundary_ready_cursor = valid_placement.clone();
    boundary_ready_cursor.ready_cursor = FixturePointPx { x: 580, y: 440 };
    assert!(
        verify_ready_placement_invariants("pure_boundary_cursor", &boundary_ready_cursor).is_err()
    );

    let mut overflow_rect_placement = valid_placement.clone();
    overflow_rect_placement.selected_monitor_work_area = FixtureRectPx {
        left: i32::MIN,
        top: 0,
        right: i32::MAX,
        bottom: 1040,
        width: i32::MAX,
        height: 1040,
    };
    assert!(
        verify_ready_placement_invariants("pure_overflow_work_area", &overflow_rect_placement)
            .is_err()
    );
}

#[test]
fn test_pure_fixture_counter_and_trace_phase_classification() {
    verify_pure_counter_and_trace_classifier_invariants();
}

/// Emits a bounded, safe JSON diagnostic line to stdout so root logs capture per-field counters
/// (including zero fields) and trace provenance at ready and before/after each stage.
fn emit_stage_counter_diagnostic(
    test_name: &str,
    stage: &str,
    ready: &FixtureReadyReport,
    snapshot: &FixtureCounterSnapshot,
) {
    let safe_diag = serde_json::json!({
        "diagnostic": "fixture_stage_snapshot",
        "test": test_name,
        "stage": stage,
        "nonce": ready.nonce,
        "pid": ready.pid,
        "processStarted": ready.process_started,
        "hwnd": ready.hwnd,
        "phase": snapshot.phase,
        "placement": ready.placement,
        "physicalFocusMouseKeyWheelSettextSum": snapshot.counters.total_physical_focus_mouse_key_wheel_and_settext_mutations(),
        "runtimeMutationsAfterSetupSum": snapshot.counters.total_runtime_mutations_after_setup(),
        "allLifetimeRawEventsSum": snapshot.counters.total_all_lifetime_raw_events(),
        "setupCounters": snapshot.setup_counters,
        "counters": snapshot.counters,
        "trace": snapshot.trace,
        "traceDroppedCount": snapshot.trace_dropped_count,
    });
    println!("{}", serde_json::to_string(&safe_diag).unwrap());
}

/// Manages a self-spawned `ryn_computer_use_fixture` child process with strict identity proof
/// (`PID`, `ProcessCreationTime`, executable path, unique `nonce` in title/class, and child HWND
/// parent/root ownership), an immutable ready counter/trace baseline, and a private JSONL
/// stdin/stdout control channel.
/// Cleans up ONLY its own spawned fixture process and window handles on Drop.
struct ControlledFixtureProcess {
    test_name: String,
    child: Child,
    stdin: Option<ChildStdin>,
    stdout: BufReader<ChildStdout>,
    ready: FixtureReadyReport,
    ready_baseline: FixtureCounterSnapshot,
    hwnd_val: u64,
}

impl ControlledFixtureProcess {
    fn spawn_for_test(test_name: &str) -> Self {
        let bin_path = env!("CARGO_BIN_EXE_ryn_computer_use_fixture");
        let nonce = format!("n_{}", csprng_hex(8));

        let mut child = Command::new(bin_path)
            .args(["--timeout-ms", "60000", "--nonce", &nonce])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("Failed to spawn ryn_computer_use_fixture binary");

        let spawned_pid = child.id();
        let stdin = child.stdin.take().expect("Fixture stdin must be piped");
        let stdout_raw = child.stdout.take().expect("Fixture stdout must be piped");
        let mut stdout = BufReader::new(stdout_raw);

        let mut ready_line = String::new();
        stdout
            .read_line(&mut ready_line)
            .expect("Failed to read ready JSON line from owned fixture");
        let trimmed_ready = ready_line.trim();
        let raw_ready_val: serde_json::Value =
            serde_json::from_str(trimmed_ready).expect("Malformed fixture ready JSON");
        if raw_ready_val.get("status").and_then(|v| v.as_str()) == Some("fixture_unavailable") {
            let _ = child.kill();
            let _ = child.wait();
            panic!(
                "[fixture_ready] owned fixture reported status=fixture_unavailable for test '{}': {}",
                test_name, trimmed_ready
            );
        }
        let ready: FixtureReadyReport =
            serde_json::from_str(trimmed_ready).expect("Malformed FixtureReadyReport JSON");

        assert_eq!(ready.status, "ready");
        assert_eq!(ready.phase, FixtureLifecyclePhase::Ready);
        assert_eq!(ready.nonce, nonce, "Fixture nonce must match per-run nonce");
        assert_eq!(
            ready.pid, spawned_pid,
            "Fixture reported PID must match spawned child PID"
        );

        let hwnd_val: u64 = ready
            .hwnd
            .parse()
            .expect("Fixture HWND must be decimal u64");
        assert!(hwnd_val != 0);

        let ready_baseline = ready.to_snapshot();
        emit_stage_counter_diagnostic(test_name, "fixture_ready", &ready, &ready_baseline);

        let inst = Self {
            test_name: test_name.to_string(),
            child,
            stdin: Some(stdin),
            stdout,
            ready,
            ready_baseline,
            hwnd_val,
        };
        inst.assert_live_identity_proof();
        if let Err(msg) = verify_ready_placement_invariants("fixture_ready", &inst.ready.placement)
        {
            panic!(
                "{} | placement={}",
                msg,
                serde_json::to_string(&inst.ready.placement).unwrap_or_default()
            );
        }
        if let Err(msg) = verify_setup_and_zero_input_invariants(
            "fixture_ready",
            &inst.ready_baseline,
            &inst.ready_baseline,
        ) {
            panic!(
                "{} | placement={}",
                msg,
                serde_json::to_string(&inst.ready.placement).unwrap_or_default()
            );
        }
        inst
    }

    #[allow(dead_code)]
    fn spawn() -> Self {
        Self::spawn_for_test("owned_window_acceptance")
    }

    /// Revalidates that the live OS window and all reported child controls still belong to our
    /// exact spawned fixture process (`PID`, `ProcessCreationTime`, `IsWindow`, root/parent,
    /// `nonce` in title & class, and child control relationships).
    fn assert_live_identity_proof(&self) {
        let hwnd = u64_to_hwnd(self.hwnd_val);
        unsafe {
            assert!(
                IsWindow(Some(hwnd)).as_bool(),
                "Owned fixture HWND is no longer a valid window"
            );
            let (live_pid, _) = get_window_pid_tid(hwnd);
            assert_eq!(
                live_pid, self.ready.pid,
                "Live HWND PID does not match owned fixture PID"
            );

            let live_started = query_process_creation_time(live_pid)
                .expect("Failed to query live fixture process creation time")
                .to_string();
            assert_eq!(
                live_started, self.ready.process_started,
                "Live process creation time does not match owned fixture creation time"
            );

            let live_exe = query_process_executable_path(live_pid);
            assert_eq!(
                live_exe, self.ready.executable_path,
                "Live executable path does not match owned fixture executable path"
            );

            let root = GetAncestor(hwnd, GA_ROOT);
            assert_eq!(root, hwnd, "Owned fixture main HWND must be a root window");

            let live_title = get_window_title(hwnd);
            let live_class = get_window_class_name(hwnd);
            assert!(
                live_title.contains(&self.ready.nonce),
                "Live window title '{}' missing per-run nonce '{}'",
                live_title,
                self.ready.nonce
            );
            assert!(
                live_class.contains(&self.ready.nonce),
                "Live window class '{}' missing per-run nonce '{}'",
                live_class,
                self.ready.nonce
            );

            assert_eq!(self.ready.children.len(), 3);
            for child_info in &self.ready.children {
                let ch_val: u64 = child_info.hwnd.parse().unwrap();
                let ch_hwnd = u64_to_hwnd(ch_val);
                assert!(
                    IsWindow(Some(ch_hwnd)).as_bool(),
                    "Child control '{}' is not a valid window",
                    child_info.role
                );
                let (ch_pid, _) = get_window_pid_tid(ch_hwnd);
                assert_eq!(ch_pid, self.ready.pid);
                assert!(
                    IsChild(hwnd, ch_hwnd).as_bool(),
                    "Child '{}' must satisfy IsChild(main_hwnd)",
                    child_info.role
                );
                let parent = GetParent(ch_hwnd).unwrap_or_default();
                assert_eq!(parent, hwnd);
                assert!(child_info.is_child_of_main);
            }
        }
    }

    fn send_control_cmd(&mut self, cmd_json: serde_json::Value) -> serde_json::Value {
        let stdin = self.stdin.as_mut().expect("Fixture stdin already closed");
        let line = format!("{}\n", serde_json::to_string(&cmd_json).unwrap());
        stdin
            .write_all(line.as_bytes())
            .expect("Failed to write control command to fixture stdin");
        stdin.flush().expect("Failed to flush fixture stdin");

        let mut resp_line = String::new();
        self.stdout
            .read_line(&mut resp_line)
            .expect("Failed to read control response from fixture stdout");
        serde_json::from_str(resp_line.trim()).expect("Malformed control response from fixture")
    }

    fn read_counter_snapshot(&mut self, stage_label: &str) -> FixtureCounterSnapshot {
        self.assert_live_identity_proof();
        let val = self.send_control_cmd(serde_json::json!({
            "cmd": "read_counters",
            "req_id": stage_label
        }));
        assert_eq!(val["status"], "counters");
        assert_eq!(val["pid"].as_u64(), Some(self.ready.pid as u64));
        assert_eq!(val["hwnd"].as_str(), Some(self.ready.hwnd.as_str()));
        let snap = FixtureCounterSnapshot {
            phase: serde_json::from_value(val["phase"].clone())
                .expect("Failed to deserialize fixture phase"),
            setup_counters: serde_json::from_value(val["setupCounters"].clone())
                .expect("Failed to deserialize setupCounters"),
            counters: serde_json::from_value(val["counters"].clone())
                .expect("Failed to deserialize counters"),
            trace: serde_json::from_value(val["trace"].clone())
                .expect("Failed to deserialize trace"),
            trace_dropped_count: val["traceDroppedCount"].as_u64().unwrap_or(0),
        };
        emit_stage_counter_diagnostic(&self.test_name, stage_label, &self.ready, &snap);
        snap
    }

    fn read_counters(&mut self) -> FixtureCounters {
        self.read_counter_snapshot("read_counters").counters
    }

    fn assert_stage_matches_ready_baseline(&mut self, stage_label: &str) -> FixtureCounterSnapshot {
        let snap = self.read_counter_snapshot(stage_label);
        if let Err(err_msg) =
            verify_setup_and_zero_input_invariants(stage_label, &self.ready_baseline, &snap)
        {
            panic!("{}", err_msg);
        }
        snap
    }

    fn move_child(&mut self, role: &str, x: i32, y: i32) {
        self.assert_live_identity_proof();
        let val = self.send_control_cmd(serde_json::json!({
            "cmd": "move_child",
            "req_id": "mv-ch",
            "role": role,
            "x": x,
            "y": y,
        }));
        assert_eq!(val["status"], "move_child_ok");
        assert_eq!(val["moved"], true);
    }

    fn move_main_window(&mut self, x: i32, y: i32) {
        self.assert_live_identity_proof();
        let val = self.send_control_cmd(serde_json::json!({
            "cmd": "move_main_window",
            "req_id": "mv-main",
            "x": x,
            "y": y,
        }));
        assert_eq!(val["status"], "move_main_ok");
        assert_eq!(val["moved"], true);
    }

    fn verify_window_ref_matches_fixture(&self, wref: &WindowRef) {
        assert_eq!(wref.hwnd, self.ready.hwnd);
        assert_eq!(wref.pid, self.ready.pid);
        assert_eq!(wref.process_started, self.ready.process_started);
        assert!(wref.generation >= 1);
    }
}

impl Drop for ControlledFixtureProcess {
    fn drop(&mut self) {
        if let Some(mut stdin) = self.stdin.take() {
            let _ = stdin.write_all(b"{\"cmd\":\"shutdown\",\"req_id\":\"drop\"}\n");
            let _ = stdin.flush();
        }
        // Wait briefly for clean self-destruction, then terminate ONLY our spawned child process if still alive
        for _ in 0..25 {
            if let Ok(Some(_)) = self.child.try_wait() {
                return;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn init_engine() -> (PhysicalActorEngine, OperationControlTable) {
    let control = OperationControlTable::new();
    let mut engine = PhysicalActorEngine::new(control.clone()).unwrap();
    let init_req = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-init".to_string()),
        method: "initialize".to_string(),
        params: serde_json::json!({
            "protocolVersion": "1.0.0",
            "client": { "name": "ryn-owned-acceptance", "version": "1.0.0" }
        }),
    };
    let resp = engine.handle_request(init_req, &mut |_| {});
    assert!(resp.error.is_none(), "initialize failed: {:?}", resp.error);
    (engine, control)
}

fn discover_and_prove_owned_window(
    engine: &mut PhysicalActorEngine,
    fixture: &ControlledFixtureProcess,
) -> WindowRef {
    fixture.assert_live_identity_proof();
    let get_req = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-win-get".to_string()),
        method: "windows.get".to_string(),
        params: serde_json::json!({
            "hwnd": fixture.ready.hwnd
        }),
    };
    let get_resp = engine.handle_request(get_req, &mut |_| {});
    assert!(
        get_resp.error.is_none(),
        "windows.get failed: {:?}",
        get_resp.error
    );
    let entry = &get_resp.result.as_ref().unwrap()["entry"];
    assert_eq!(entry["title"].as_str(), Some(fixture.ready.title.as_str()));
    assert_eq!(
        entry["className"].as_str(),
        Some(fixture.ready.class_name.as_str())
    );
    let win_ref: WindowRef = serde_json::from_value(entry["window"].clone()).unwrap();
    fixture.verify_window_ref_matches_fixture(&win_ref);
    win_ref
}

fn observe_owned_window(
    engine: &mut PhysicalActorEngine,
    fixture: &ControlledFixtureProcess,
    task_session_id: &str,
    win_ref: &WindowRef,
    include_screenshot: bool,
) -> (String, Vec<UiaElement>, serde_json::Value) {
    fixture.assert_live_identity_proof();
    fixture.verify_window_ref_matches_fixture(win_ref);

    let state_req = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-state".to_string()),
        method: "state.get".to_string(),
        params: serde_json::json!({
            "taskSessionId": task_session_id,
            "window": win_ref,
            "includeScreenshot": include_screenshot,
            "includeText": true
        }),
    };
    let resp = engine.handle_request(state_req, &mut |_| {});
    assert!(resp.error.is_none(), "state.get failed: {:?}", resp.error);
    let val = resp.result.unwrap();
    let serialized = serde_json::to_string(&val).unwrap();
    assert!(
        !serialized.contains("TopSecretPasswordValue999"),
        "Password control contents must never appear in state.get output"
    );

    let state_token = val["stateToken"].as_str().unwrap().to_string();
    let elements: Vec<UiaElement> =
        serde_json::from_value(val["accessibility"]["elements"].clone()).unwrap();
    (state_token, elements, val)
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hex::encode(hasher.finalize())
}

// ============================================================================
// Scenario 1: Read-only observe UIA + screenshot on owned window (zero focus/input)
// ============================================================================
#[test]
fn test_owned_window_readonly_observe_uia_and_screenshot_zero_input() {
    verify_pure_counter_and_trace_classifier_invariants();

    let mut fixture = ControlledFixtureProcess::spawn_for_test(
        "test_owned_window_readonly_observe_uia_and_screenshot_zero_input",
    );
    // First read BEFORE engine initialization
    fixture.assert_stage_matches_ready_baseline("pre_engine_init");

    let (mut engine, _control) = init_engine();
    // Second read AFTER engine initialization, BEFORE any window/UIA/screenshot query
    fixture.assert_stage_matches_ready_baseline("post_engine_init");

    let win_ref = discover_and_prove_owned_window(&mut engine, &fixture);
    fixture.assert_stage_matches_ready_baseline("post_windows_get");

    let (state_token, elements, obs_val) =
        observe_owned_window(&mut engine, &fixture, "task-obs-1", &win_ref, true);
    assert!(state_token.starts_with("st_"));
    fixture.assert_stage_matches_ready_baseline("post_state_get_with_screenshot");

    // Verify screenshot metadata and PNG cryptographic/header integrity on our owned window
    let shots = obs_val["screenshots"].as_array().unwrap();
    assert_eq!(shots.len(), 1);
    let shot = &shots[0];
    assert_eq!(shot["status"], "ok");
    let width = shot["width"].as_u64().unwrap() as u32;
    let height = shot["height"].as_u64().unwrap() as u32;
    assert!(width > 0);
    assert!(height > 0);
    let b64_str = shot["dataBase64"]
        .as_str()
        .expect("dataBase64 must be present when status == ok");
    assert!(!b64_str.is_empty());

    let png_bytes = BASE64_STANDARD
        .decode(b64_str.as_bytes())
        .expect("Screenshot dataBase64 must be valid base64");
    assert!(
        png_bytes.len() > 24,
        "Decoded PNG must contain signature + IHDR"
    );
    assert_eq!(
        &png_bytes[0..8],
        b"\x89PNG\r\n\x1a\n",
        "Decoded screenshot bytes must start with PNG magic signature"
    );
    assert_eq!(&png_bytes[12..16], b"IHDR", "First PNG chunk must be IHDR");
    let ihdr_width = u32::from_be_bytes([
        png_bytes[16],
        png_bytes[17],
        png_bytes[18],
        png_bytes[19],
    ]);
    let ihdr_height = u32::from_be_bytes([
        png_bytes[20],
        png_bytes[21],
        png_bytes[22],
        png_bytes[23],
    ]);
    assert_eq!(ihdr_width, width, "PNG IHDR width must match reported width");
    assert_eq!(
        ihdr_height, height,
        "PNG IHDR height must match reported height"
    );

    let reported_png_sha256 = shot["sha256"].as_str().unwrap();
    let verified_png_sha256 = sha256_hex(&png_bytes);
    assert_eq!(
        verified_png_sha256, reported_png_sha256,
        "Decoded PNG SHA-256 must match ScreenshotArtifact.sha256"
    );
    let base64_data_sha256 = sha256_hex(b64_str.as_bytes());
    let fixture_source_sha256 =
        sha256_hex(include_bytes!("../src/bin/ryn_computer_use_fixture.rs"));
    let test_source_sha256 = sha256_hex(include_bytes!("owned_window_acceptance_tests.rs"));

    // Export non-pixel, non-base64 screenshot metadata for root verification
    let shot_meta = serde_json::json!({
        "diagnostic": "readonly_screenshot_metadata",
        "test": "test_owned_window_readonly_observe_uia_and_screenshot_zero_input",
        "nonce": fixture.ready.nonce,
        "pid": fixture.ready.pid,
        "processStarted": fixture.ready.process_started,
        "hwnd": fixture.ready.hwnd,
        "stateToken": state_token,
        "uiaTreeHash": obs_val["accessibility"]["treeHash"],
        "captureStatus": shot["status"],
        "captureMethod": shot["captureMethod"],
        "mimeType": shot["mimeType"],
        "width": width,
        "height": height,
        "ihdrWidth": ihdr_width,
        "ihdrHeight": ihdr_height,
        "pngByteLength": png_bytes.len(),
        "reportedPngSha256": reported_png_sha256,
        "verifiedPngSha256": verified_png_sha256,
        "base64DataSha256": base64_data_sha256,
        "fixtureSourceSha256": fixture_source_sha256,
        "testSourceSha256": test_source_sha256,
        "dpi": shot["dpi"],
        "scaleFactor": shot["scaleFactor"],
        "minimized": shot["minimized"],
        "occluded": shot["occluded"],
        "captureProtected": shot["captureProtected"],
    });
    println!("{}", serde_json::to_string(&shot_meta).unwrap());

    // Verify UIA tree discovered our button, non-password edit (with InitialPublicValue),
    // and password edit (marked password=true with value=None)
    let btn_el = elements
        .iter()
        .find(|e| e.control_type == "button" && e.name == "InvokeButton")
        .expect("Owned button must be present in UIA elements");
    assert!(btn_el.patterns.contains(&"invoke".to_string()));

    let pub_edit = elements
        .iter()
        .find(|e| e.control_type == "edit" && !e.password)
        .expect("Owned non-password edit must be present in UIA elements");
    assert_eq!(pub_edit.value.as_deref(), Some("InitialPublicValue"));

    let pwd_edit = elements
        .iter()
        .find(|e| e.control_type == "edit" && e.password)
        .expect("Owned password edit must be present and flagged password=true");
    assert!(
        pwd_edit.value.is_none(),
        "Password edit value must be omitted"
    );

    // Confirm zero focus or input events were delivered to the fixture during read-only observation
    fixture.assert_stage_matches_ready_baseline("final_after_readonly_observe");
}

// ============================================================================
// Scenario 2: Prepare + cancel produces zero focus/input and blocks subsequent commit
// ============================================================================
#[test]
fn test_owned_window_prepare_and_cancel_zero_focus_and_zero_input() {
    verify_pure_counter_and_trace_classifier_invariants();

    let mut fixture = ControlledFixtureProcess::spawn_for_test(
        "test_owned_window_prepare_and_cancel_zero_focus_and_zero_input",
    );
    fixture.assert_stage_matches_ready_baseline("pre_engine_init");

    let (mut engine, _control) = init_engine();
    fixture.assert_stage_matches_ready_baseline("post_engine_init");

    let win_ref = discover_and_prove_owned_window(&mut engine, &fixture);
    fixture.assert_stage_matches_ready_baseline("post_windows_get");

    let (state_token, elements, _) =
        observe_owned_window(&mut engine, &fixture, "task-cancel-1", &win_ref, false);
    fixture.assert_stage_matches_ready_baseline("post_state_get");

    let btn_el = elements
        .iter()
        .find(|e| e.control_type == "button" && e.name == "InvokeButton")
        .unwrap();

    let mut events: Vec<EventEnvelope> = Vec::new();
    let prep_req = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-prep-cancel".to_string()),
        method: "action.prepare".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-cancel-1",
            "operationId": "op-cancel-1",
            "window": win_ref,
            "expectedStateToken": state_token,
            "authorityEpoch": 11,
            "action": {
                "type": "click",
                "button": "left",
                "element": {
                    "index": btn_el.index,
                    "runtimeId": btn_el.runtime_id,
                }
            }
        }),
    };
    let prep_resp = engine.handle_request(prep_req, &mut |ev| events.push(ev));
    assert!(prep_resp.error.is_none());
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].event, "action.ready");
    let prepare_ticket = prep_resp.result.unwrap()["prepareTicket"]
        .as_str()
        .unwrap()
        .to_string();

    // Verify zero focus/input occurred during action.prepare
    fixture.assert_stage_matches_ready_baseline("post_action_prepare");

    // Cancel while in ready state
    let cancel_req = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-do-cancel".to_string()),
        method: "action.cancel".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-cancel-1",
            "operationId": "op-cancel-1",
            "prepareTicket": prepare_ticket,
        }),
    };
    let cancel_resp = engine.handle_request(cancel_req, &mut |_| {});
    assert!(cancel_resp.error.is_none());
    let cancel_val = cancel_resp.result.unwrap();
    assert_eq!(cancel_val["cancelled"], true);
    assert_eq!(cancel_val["mutation"], "none");
    assert_eq!(cancel_val["inputLockReleased"], true);
    fixture.assert_stage_matches_ready_baseline("post_action_cancel");

    // Attempting commit after cancel must fail with Cancelled and zero fixture events
    let commit_req = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-commit-after-cancel".to_string()),
        method: "action.commit".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-cancel-1",
            "operationId": "op-cancel-1",
            "prepareTicket": prepare_ticket,
        }),
    };
    let commit_resp = engine.handle_request(commit_req, &mut |_| {});
    let err = commit_resp.error.expect("Commit after cancel must fail");
    assert_eq!(err.code, ErrorCode::Cancelled);
    assert_eq!(err.mutation, MutationState::None);

    let final_snap = fixture.assert_stage_matches_ready_baseline("post_commit_after_cancel");
    assert_eq!(final_snap.counters.button_click_count, 0);
}

// ============================================================================
// Scenario 3: Fresh ready revoked / expired without commit produces zero input
// ============================================================================
#[test]
fn test_owned_window_ready_revoked_or_expired_without_commit_zero_input() {
    verify_pure_counter_and_trace_classifier_invariants();

    let mut fixture = ControlledFixtureProcess::spawn_for_test(
        "test_owned_window_ready_revoked_or_expired_without_commit_zero_input",
    );
    fixture.assert_stage_matches_ready_baseline("pre_engine_init");

    let (mut engine, control) = init_engine();
    fixture.assert_stage_matches_ready_baseline("post_engine_init");

    let win_ref = discover_and_prove_owned_window(&mut engine, &fixture);
    fixture.assert_stage_matches_ready_baseline("post_windows_get");

    let (state_token, _, _) =
        observe_owned_window(&mut engine, &fixture, "task-expire-1", &win_ref, false);
    fixture.assert_stage_matches_ready_baseline("post_state_get");

    // 3a. Prepare with a short 60ms readyTimeoutMs, let it expire without commit, then attempt commit
    let prep_req = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-prep-exp".to_string()),
        method: "action.prepare".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-expire-1",
            "operationId": "op-exp-1",
            "window": win_ref,
            "expectedStateToken": state_token,
            "constraints": {
                "readyTimeoutMs": 60
            },
            "action": {
                "type": "focus_window"
            }
        }),
    };
    let prep_resp = engine.handle_request(prep_req, &mut |_| {});
    assert!(prep_resp.error.is_none());
    let ticket_exp = prep_resp.result.unwrap()["prepareTicket"]
        .as_str()
        .unwrap()
        .to_string();
    fixture.assert_stage_matches_ready_baseline("post_prepare_expire_ticket");

    std::thread::sleep(Duration::from_millis(85));
    engine.poll_maintenance();
    fixture.assert_stage_matches_ready_baseline("post_expiry_maintenance");

    let commit_exp = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-commit-exp".to_string()),
        method: "action.commit".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-expire-1",
            "operationId": "op-exp-1",
            "prepareTicket": ticket_exp,
        }),
    };
    let exp_resp = engine.handle_request(commit_exp, &mut |_| {});
    let exp_err = exp_resp.error.expect("Expired ticket must fail commit");
    assert_eq!(exp_err.code, ErrorCode::PrepareTicketExpired);
    assert_eq!(exp_err.mutation, MutationState::None);
    fixture.assert_stage_matches_ready_baseline("post_commit_expired_ticket");

    // 3b. Prepare a second operation, revoke via reader-side OperationControlTable signal before commit
    let prep_req2 = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-prep-revoke".to_string()),
        method: "action.prepare".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-expire-1",
            "operationId": "op-revoke-2",
            "window": win_ref,
            "expectedStateToken": state_token,
            "action": {
                "type": "key_press",
                "key": "enter"
            }
        }),
    };
    let prep_resp2 = engine.handle_request(prep_req2, &mut |_| {});
    assert!(prep_resp2.error.is_none());
    let ticket_rev = prep_resp2.result.unwrap()["prepareTicket"]
        .as_str()
        .unwrap()
        .to_string();
    fixture.assert_stage_matches_ready_baseline("post_prepare_revoke_ticket");

    assert!(control.signal_cancel_operation("task-expire-1", "op-revoke-2"));

    let commit_rev = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-commit-revoke".to_string()),
        method: "action.commit".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-expire-1",
            "operationId": "op-revoke-2",
            "prepareTicket": ticket_rev,
        }),
    };
    let rev_resp = engine.handle_request(commit_rev, &mut |_| {});
    let rev_err = rev_resp.error.expect("Revoked ready ticket must fail");
    assert_eq!(rev_err.code, ErrorCode::Cancelled);
    assert_eq!(rev_err.mutation, MutationState::None);

    fixture.assert_stage_matches_ready_baseline("post_commit_revoked_ticket");
}

// ============================================================================
// Scenario 4: Positive focus, Unicode + Chinese text into owned non-password edit,
// and click unique owned button with exact-once counter verification
// ============================================================================
#[test]
fn test_owned_window_positive_focus_unicode_chinese_type_and_button_click_exact_once() {
    let mut fixture = ControlledFixtureProcess::spawn_for_test(
        "test_owned_window_positive_focus_unicode_chinese_type_and_button_click_exact_once",
    );
    let (mut engine, _control) = init_engine();

    let win_ref = discover_and_prove_owned_window(&mut engine, &fixture);
    let (state_token_1, _, _) =
        observe_owned_window(&mut engine, &fixture, "task-pos-1", &win_ref, false);

    // Step 1: Positive `focus_window` on our owned fixture window
    let prep_focus = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-prep-focus".to_string()),
        method: "action.prepare".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-pos-1",
            "operationId": "op-pos-focus-1",
            "window": win_ref,
            "expectedStateToken": state_token_1,
            "action": {
                "type": "focus_window"
            }
        }),
    };
    let prep_focus_resp = engine.handle_request(prep_focus, &mut |_| {});
    assert!(prep_focus_resp.error.is_none());
    let focus_ticket = prep_focus_resp.result.unwrap()["prepareTicket"]
        .as_str()
        .unwrap()
        .to_string();

    fixture.assert_live_identity_proof();
    let commit_focus = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-commit-focus".to_string()),
        method: "action.commit".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-pos-1",
            "operationId": "op-pos-focus-1",
            "prepareTicket": focus_ticket,
        }),
    };
    let commit_focus_resp = engine.handle_request(commit_focus, &mut |_| {});
    assert!(
        commit_focus_resp.error.is_none(),
        "focus_window commit failed: {:?}",
        commit_focus_resp.error
    );
    let focus_res = commit_focus_resp.result.unwrap();
    assert_eq!(focus_res["status"], "committed");
    assert_eq!(focus_res["mutation"], "delivered");
    assert_eq!(focus_res["focusRecovery"]["succeeded"], true);
    assert_eq!(focus_res["focusRecovery"]["finalForegroundVerified"], true);

    // Step 2: Re-observe, clear the public edit via `uia_set_value` to "", and type Unicode + Chinese
    // ("Ryn测试🌟42") into the owned non-password edit element without resetting any lifetime counters
    let (state_token_clear, elements_clear, _) =
        observe_owned_window(&mut engine, &fixture, "task-pos-1", &win_ref, false);
    let pub_edit_clear = elements_clear
        .iter()
        .find(|e| e.control_type == "edit" && !e.password)
        .expect("Public edit must be found");

    let prep_clear = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-prep-clear".to_string()),
        method: "action.prepare".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-pos-1",
            "operationId": "op-pos-clear-1",
            "window": win_ref,
            "expectedStateToken": state_token_clear,
            "action": {
                "type": "uia_set_value",
                "element": {
                    "index": pub_edit_clear.index,
                    "runtimeId": pub_edit_clear.runtime_id,
                },
                "value": ""
            }
        }),
    };
    let clear_ticket = engine
        .handle_request(prep_clear, &mut |_| {})
        .result
        .unwrap()["prepareTicket"]
        .as_str()
        .unwrap()
        .to_string();
    let commit_clear = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-commit-clear".to_string()),
        method: "action.commit".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-pos-1",
            "operationId": "op-pos-clear-1",
            "prepareTicket": clear_ticket,
        }),
    };
    assert!(engine.handle_request(commit_clear, &mut |_| {}).error.is_none());

    let before_type_counters = fixture.read_counters();
    let (state_token_2, elements_2, _) =
        observe_owned_window(&mut engine, &fixture, "task-pos-1", &win_ref, false);
    let pub_edit = elements_2
        .iter()
        .find(|e| e.control_type == "edit" && !e.password)
        .expect("Public edit must be found");

    let target_text = "Ryn测试🌟42";
    let expected_utf16_len = target_text.encode_utf16().count() as u64;

    let prep_type = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-prep-type".to_string()),
        method: "action.prepare".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-pos-1",
            "operationId": "op-pos-type-1",
            "window": win_ref,
            "expectedStateToken": state_token_2,
            "action": {
                "type": "type_text",
                "text": target_text,
                "element": ElementTarget {
                    index: pub_edit.index,
                    runtime_id: Some(pub_edit.runtime_id.clone()),
                }
            }
        }),
    };
    let prep_type_resp = engine.handle_request(prep_type, &mut |_| {});
    assert!(prep_type_resp.error.is_none());
    let type_ticket = prep_type_resp.result.unwrap()["prepareTicket"]
        .as_str()
        .unwrap()
        .to_string();

    fixture.assert_live_identity_proof();
    let commit_type = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-commit-type".to_string()),
        method: "action.commit".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-pos-1",
            "operationId": "op-pos-type-1",
            "prepareTicket": type_ticket,
        }),
    };
    let commit_type_resp = engine.handle_request(commit_type, &mut |_| {});
    assert!(
        commit_type_resp.error.is_none(),
        "type_text commit failed: {:?}",
        commit_type_resp.error
    );

    // Verify independent fixture counters and actual Edit control text + password untouched
    let after_type_counters = fixture.read_counters();
    assert_eq!(after_type_counters.public_edit_text, target_text);
    assert_eq!(
        after_type_counters.public_edit_char_count - before_type_counters.public_edit_char_count,
        expected_utf16_len
    );
    assert!(
        after_type_counters.runtime_public_edit_change_count
            > before_type_counters.runtime_public_edit_change_count
    );
    assert_eq!(after_type_counters.button_click_count, 0);
    assert_eq!(after_type_counters.password_edit_char_count, 0);
    assert_eq!(after_type_counters.runtime_password_edit_change_count, 0);
    assert_eq!(after_type_counters.setup_password_edit_change_count, 1);

    // Step 3: Re-observe and click the unique owned button (`InvokeButton`) -> verify exact-once click counter
    let (state_token_3, elements_3, _) =
        observe_owned_window(&mut engine, &fixture, "task-pos-1", &win_ref, false);
    let btn_el = elements_3
        .iter()
        .find(|e| e.control_type == "button" && e.name == "InvokeButton")
        .expect("Owned button must be found");

    let prep_click = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-prep-click".to_string()),
        method: "action.prepare".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-pos-1",
            "operationId": "op-pos-click-1",
            "window": win_ref,
            "expectedStateToken": state_token_3,
            "action": {
                "type": "click",
                "button": "left",
                "element": ElementTarget {
                    index: btn_el.index,
                    runtime_id: Some(btn_el.runtime_id.clone()),
                }
            }
        }),
    };
    let prep_click_resp = engine.handle_request(prep_click, &mut |_| {});
    assert!(prep_click_resp.error.is_none());
    let click_ticket = prep_click_resp.result.unwrap()["prepareTicket"]
        .as_str()
        .unwrap()
        .to_string();

    fixture.assert_live_identity_proof();
    let commit_click = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-commit-click".to_string()),
        method: "action.commit".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-pos-1",
            "operationId": "op-pos-click-1",
            "prepareTicket": click_ticket,
        }),
    };
    let commit_click_resp = engine.handle_request(commit_click, &mut |_| {});
    assert!(
        commit_click_resp.error.is_none(),
        "click commit failed: {:?}",
        commit_click_resp.error
    );

    let after_click_counters = fixture.read_counters();
    assert_eq!(
        after_click_counters.button_click_count, 1,
        "Owned button must be clicked exactly once"
    );
    assert_eq!(after_click_counters.button_lbutton_down_count, 1);
    assert_eq!(after_click_counters.button_lbutton_up_count, 1);
    assert_eq!(after_click_counters.public_edit_text, target_text);
    assert_eq!(after_click_counters.runtime_password_edit_change_count, 0);
}

// ============================================================================
// Scenario 5: Positive UIA actions (`uia_focus`, `uia_set_value`, `uia_invoke`),
// `key_press`, and `scroll` on owned window with independent counter verification
// ============================================================================
#[test]
fn test_owned_window_positive_uia_actions_key_press_and_scroll_verified_by_counters() {
    let mut fixture = ControlledFixtureProcess::spawn_for_test(
        "test_owned_window_positive_uia_actions_key_press_and_scroll_verified_by_counters",
    );
    let (mut engine, _control) = init_engine();

    let win_ref = discover_and_prove_owned_window(&mut engine, &fixture);

    // 1. `uia_set_value` on owned non-password edit
    let (state_token_1, elements_1, _) =
        observe_owned_window(&mut engine, &fixture, "task-uia-1", &win_ref, false);
    let pub_edit = elements_1
        .iter()
        .find(|e| e.control_type == "edit" && !e.password)
        .unwrap();

    let prep_set = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-prep-setval".to_string()),
        method: "action.prepare".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-uia-1",
            "operationId": "op-uia-setval-1",
            "window": win_ref,
            "expectedStateToken": state_token_1,
            "action": {
                "type": "uia_set_value",
                "element": {
                    "index": pub_edit.index,
                    "runtimeId": pub_edit.runtime_id,
                },
                "value": "UiaValue_中文_99"
            }
        }),
    };
    let prep_set_resp = engine.handle_request(prep_set, &mut |_| {});
    assert!(prep_set_resp.error.is_none());
    let set_ticket = prep_set_resp.result.unwrap()["prepareTicket"]
        .as_str()
        .unwrap()
        .to_string();

    fixture.assert_live_identity_proof();
    let commit_set = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-commit-setval".to_string()),
        method: "action.commit".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-uia-1",
            "operationId": "op-uia-setval-1",
            "prepareTicket": set_ticket,
        }),
    };
    let commit_set_resp = engine.handle_request(commit_set, &mut |_| {});
    assert!(
        commit_set_resp.error.is_none(),
        "uia_set_value failed: {:?}",
        commit_set_resp.error
    );

    let c1 = fixture.read_counters();
    assert_eq!(c1.public_edit_text, "UiaValue_中文_99");
    assert!(c1.public_edit_settext_count >= 1);

    // 2. `uia_invoke` on owned button -> increments button_click_count by 1 (in the Windows UIA proxy for this tested standard BUTTON control, InvokePattern also observes button mouse down/up messages; UIA invoke is not universally guaranteed to synthesize mouse messages across all providers)
    let (state_token_2, elements_2, _) =
        observe_owned_window(&mut engine, &fixture, "task-uia-1", &win_ref, false);
    let btn_el = elements_2
        .iter()
        .find(|e| e.control_type == "button" && e.name == "InvokeButton")
        .unwrap();

    let prep_inv = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-prep-invoke".to_string()),
        method: "action.prepare".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-uia-1",
            "operationId": "op-uia-invoke-1",
            "window": win_ref,
            "expectedStateToken": state_token_2,
            "action": {
                "type": "uia_invoke",
                "element": {
                    "index": btn_el.index,
                    "runtimeId": btn_el.runtime_id,
                }
            }
        }),
    };
    let prep_inv_resp = engine.handle_request(prep_inv, &mut |_| {});
    assert!(prep_inv_resp.error.is_none());
    let inv_ticket = prep_inv_resp.result.unwrap()["prepareTicket"]
        .as_str()
        .unwrap()
        .to_string();

    fixture.assert_live_identity_proof();
    let commit_inv = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-commit-invoke".to_string()),
        method: "action.commit".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-uia-1",
            "operationId": "op-uia-invoke-1",
            "prepareTicket": inv_ticket,
        }),
    };
    let commit_inv_resp = engine.handle_request(commit_inv, &mut |_| {});
    assert!(
        commit_inv_resp.error.is_none(),
        "uia_invoke failed: {:?}",
        commit_inv_resp.error
    );

    let c2 = fixture.read_counters();
    assert_eq!(c2.button_click_count, 1);

    // 3. `uia_focus` on public edit followed by `key_press` ("a") -> verified only by keydown/keyup counters, not WM_CHAR (under an active IME, virtual key 'A' may be intercepted as VK_PROCESSKEY=229 without producing WM_CHAR)
    let (state_token_3, elements_3, _) =
        observe_owned_window(&mut engine, &fixture, "task-uia-1", &win_ref, false);
    let pub_edit_3 = elements_3
        .iter()
        .find(|e| e.control_type == "edit" && !e.password)
        .unwrap();

    let prep_ufocus = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-prep-ufocus".to_string()),
        method: "action.prepare".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-uia-1",
            "operationId": "op-uia-focus-1",
            "window": win_ref,
            "expectedStateToken": state_token_3,
            "action": {
                "type": "uia_focus",
                "element": {
                    "index": pub_edit_3.index,
                    "runtimeId": pub_edit_3.runtime_id,
                }
            }
        }),
    };
    let ufocus_ticket = engine
        .handle_request(prep_ufocus, &mut |_| {})
        .result
        .unwrap()["prepareTicket"]
        .as_str()
        .unwrap()
        .to_string();
    let commit_ufocus = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-commit-ufocus".to_string()),
        method: "action.commit".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-uia-1",
            "operationId": "op-uia-focus-1",
            "prepareTicket": ufocus_ticket,
        }),
    };
    assert!(engine.handle_request(commit_ufocus, &mut |_| {}).error.is_none());

    let (state_token_4, _, _) =
        observe_owned_window(&mut engine, &fixture, "task-uia-1", &win_ref, false);
    let prep_key = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-prep-key".to_string()),
        method: "action.prepare".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-uia-1",
            "operationId": "op-key-press-1",
            "window": win_ref,
            "expectedStateToken": state_token_4,
            "action": {
                "type": "key_press",
                "key": "a",
                "repeat": 1
            }
        }),
    };
    let key_ticket = engine
        .handle_request(prep_key, &mut |_| {})
        .result
        .unwrap()["prepareTicket"]
        .as_str()
        .unwrap()
        .to_string();
    let commit_key = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-commit-key".to_string()),
        method: "action.commit".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-uia-1",
            "operationId": "op-key-press-1",
            "prepareTicket": key_ticket,
        }),
    };
    assert!(engine.handle_request(commit_key, &mut |_| {}).error.is_none());

    // 4. `scroll` inside owned window client rect -> verified by canonical mouse_wheel_vertical_count
    // and raw/forwarded message accounting (child WM_MOUSEWHEEL at (80, 80) on public_edit forwards
    // via DefSubclassProc to main_wnd_proc).
    let (state_token_5, _, _) =
        observe_owned_window(&mut engine, &fixture, "task-uia-1", &win_ref, false);
    let prep_scroll = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-prep-scroll".to_string()),
        method: "action.prepare".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-uia-1",
            "operationId": "op-scroll-1",
            "window": win_ref,
            "expectedStateToken": state_token_5,
            "action": {
                "type": "scroll",
                "target": PointTarget {
                    coordinate_space: "client_physical_px".to_string(),
                    x: 80,
                    y: 80,
                },
                "deltaX": 0,
                "deltaY": -120
            }
        }),
    };
    let scroll_ticket = engine
        .handle_request(prep_scroll, &mut |_| {})
        .result
        .unwrap()["prepareTicket"]
        .as_str()
        .unwrap()
        .to_string();
    let commit_scroll = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-commit-scroll".to_string()),
        method: "action.commit".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-uia-1",
            "operationId": "op-scroll-1",
            "prepareTicket": scroll_ticket,
        }),
    };
    assert!(engine.handle_request(commit_scroll, &mut |_| {}).error.is_none());

    let snap_final = fixture.read_counter_snapshot("post_scroll");
    let c_final = &snap_final.counters;
    assert_eq!(c_final.public_edit_keydown_count, 1);
    assert_eq!(c_final.public_edit_keyup_count, 1);
    assert_eq!(c_final.mouse_wheel_vertical_count, 1);
    assert_eq!(c_final.mouse_wheel_vertical_delta_sum, -120);
    assert_eq!(c_final.raw_mouse_wheel_vertical_message_count, 2);
    assert_eq!(c_final.raw_mouse_wheel_vertical_delta_sum, -240);
    assert_eq!(c_final.forwarded_mouse_wheel_vertical_count, 1);
    assert_eq!(c_final.forwarded_mouse_wheel_vertical_delta_sum, -120);
    assert_eq!(c_final.mouse_wheel_horizontal_count, 0);
    assert_eq!(c_final.raw_mouse_wheel_horizontal_message_count, 0);
    assert_eq!(c_final.forwarded_mouse_wheel_horizontal_count, 0);
    assert_eq!(snap_final.trace_dropped_count, 0);

    let wheel_trace: Vec<&FixtureTraceEntry> = snap_final
        .trace
        .iter()
        .filter(|e| e.category == "mouse_wheel")
        .collect();
    assert_eq!(wheel_trace.len(), 2);
    assert_eq!(wheel_trace[0].source, "child_subclass");
    assert_eq!(wheel_trace[0].control_role, "public_edit");
    assert_eq!(wheel_trace[0].notify_code, None);
    assert_eq!(wheel_trace[0].field_incremented, "mouseWheelVerticalCount");
    assert_eq!(wheel_trace[1].source, "main_wnd_proc");
    assert_eq!(wheel_trace[1].control_role, "main_window");
    assert_eq!(
        wheel_trace[1].notify_code.as_deref(),
        Some("forwarded_from_child")
    );
    assert_eq!(
        wheel_trace[1].field_incremented,
        "rawMouseWheelVerticalMessageCount"
    );
}

// ============================================================================
// Scenario 6: Malformed/stale token, top-level geometry move, child element move
// inside unchanged top-level rect, and PID mismatch rejection with zero input
// ============================================================================
#[test]
fn test_owned_window_malformed_stale_token_geometry_child_move_and_pid_rejection_no_input() {
    let mut fixture = ControlledFixtureProcess::spawn_for_test(
        "test_owned_window_malformed_stale_token_geometry_child_move_and_pid_rejection_no_input",
    );
    let (mut engine, _control) = init_engine();

    let win_ref = discover_and_prove_owned_window(&mut engine, &fixture);
    let (state_token, elements, _) =
        observe_owned_window(&mut engine, &fixture, "task-stale-1", &win_ref, false);
    let btn_el = elements
        .iter()
        .find(|e| e.control_type == "button" && e.name == "InvokeButton")
        .unwrap();

    // 6a. Malformed / unknown stateToken rejected with StateTokenInvalid and 0 input
    let bad_token_prep = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-bad-tok".to_string()),
        method: "action.prepare".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-stale-1",
            "operationId": "op-bad-tok",
            "window": win_ref,
            "expectedStateToken": "st_00000000000000000000000000000000",
            "action": {
                "type": "click",
                "element": { "index": btn_el.index, "runtimeId": btn_el.runtime_id }
            }
        }),
    };
    let bad_tok_err = engine
        .handle_request(bad_token_prep, &mut |_| {})
        .error
        .unwrap();
    assert_eq!(bad_tok_err.code, ErrorCode::StateTokenInvalid);

    // 6b. Wrong PID in WindowRef rejected with WindowStale + PidMismatch and 0 input
    let mut wrong_pid_ref = win_ref.clone();
    wrong_pid_ref.pid = win_ref.pid + 9999;
    let bad_pid_prep = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-bad-pid".to_string()),
        method: "action.prepare".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-stale-1",
            "operationId": "op-bad-pid",
            "window": wrong_pid_ref,
            "expectedStateToken": state_token,
            "action": {
                "type": "focus_window"
            }
        }),
    };
    let bad_pid_err = engine
        .handle_request(bad_pid_prep, &mut |_| {})
        .error
        .unwrap();
    assert_eq!(bad_pid_err.code, ErrorCode::WindowStale);
    assert_eq!(
        bad_pid_err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::PidMismatch)
    );

    // 6c. Child element moved inside unchanged top-level window between prepare and commit
    // MUST be rejected at commit with StateTokenStale (GeometryChanged) and 0 button clicks!
    let prep_child_move = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-prep-chmove".to_string()),
        method: "action.prepare".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-stale-1",
            "operationId": "op-chmove-1",
            "window": win_ref,
            "expectedStateToken": state_token,
            "action": {
                "type": "click",
                "button": "left",
                "element": {
                    "index": btn_el.index,
                    "runtimeId": btn_el.runtime_id,
                }
            }
        }),
    };
    let chmove_ticket = engine
        .handle_request(prep_child_move, &mut |_| {})
        .result
        .unwrap()["prepareTicket"]
        .as_str()
        .unwrap()
        .to_string();

    // Move the child button by +120px horizontally while keeping top-level window rect identical
    fixture.move_child("button", 140, 20);

    let commit_child_move = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-commit-chmove".to_string()),
        method: "action.commit".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-stale-1",
            "operationId": "op-chmove-1",
            "prepareTicket": chmove_ticket,
        }),
    };
    let chmove_err = engine
        .handle_request(commit_child_move, &mut |_| {})
        .error
        .expect("Commit after child element moved must fail");
    assert_eq!(chmove_err.code, ErrorCode::StateTokenStale);
    assert_eq!(
        chmove_err.details.as_ref().and_then(|d| d.reason),
        Some(DiagnosticReason::GeometryChanged)
    );
    // Focus happened prior to element bounds check, so mutation is monotonically Possible (never None)
    assert_eq!(chmove_err.mutation, MutationState::Possible);

    let c_after_chmove = fixture.read_counters();
    assert_eq!(
        c_after_chmove.button_click_count, 0,
        "Moved child button must NOT be clicked"
    );
    assert_eq!(c_after_chmove.button_lbutton_down_count, 0);

    // 6d. Top-level window moved after observation -> rejected in prepare with StateTokenStale
    let (state_token_2, _, _) =
        observe_owned_window(&mut engine, &fixture, "task-stale-1", &win_ref, false);
    fixture.move_main_window(180, 160);

    let prep_win_move = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-prep-winmove".to_string()),
        method: "action.prepare".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-stale-1",
            "operationId": "op-winmove-1",
            "window": win_ref,
            "expectedStateToken": state_token_2,
            "action": {
                "type": "focus_window"
            }
        }),
    };
    let winmove_err = engine
        .handle_request(prep_win_move, &mut |_| {})
        .error
        .expect("Prepare after top-level window moved must fail");
    assert_eq!(winmove_err.code, ErrorCode::StateTokenStale);
    assert_eq!(winmove_err.mutation, MutationState::None);
}

// ============================================================================
// Scenario 7: Duplicate commit returns cached terminal result without repeating
// physical side effects, and session.end / shutdown invalidate tokens without
// closing the owned window
// ============================================================================
#[test]
fn test_owned_window_commit_duplicate_idempotent_and_session_end_preserves_window() {
    let mut fixture = ControlledFixtureProcess::spawn_for_test(
        "test_owned_window_commit_duplicate_idempotent_and_session_end_preserves_window",
    );
    let (mut engine, _control) = init_engine();

    let win_ref = discover_and_prove_owned_window(&mut engine, &fixture);
    let (state_token, elements, _) =
        observe_owned_window(&mut engine, &fixture, "task-dup-1", &win_ref, false);
    let btn_el = elements
        .iter()
        .find(|e| e.control_type == "button" && e.name == "InvokeButton")
        .unwrap();

    let prep_req = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-prep-dup".to_string()),
        method: "action.prepare".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-dup-1",
            "operationId": "op-dup-click-1",
            "window": win_ref,
            "expectedStateToken": state_token,
            "action": {
                "type": "click",
                "button": "left",
                "element": {
                    "index": btn_el.index,
                    "runtimeId": btn_el.runtime_id,
                }
            }
        }),
    };
    let prep_resp = engine.handle_request(prep_req, &mut |_| {});
    assert!(prep_resp.error.is_none());
    let ticket = prep_resp.result.unwrap()["prepareTicket"]
        .as_str()
        .unwrap()
        .to_string();

    // First commit executes the click exactly once
    let commit_1 = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-commit-dup-1".to_string()),
        method: "action.commit".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-dup-1",
            "operationId": "op-dup-click-1",
            "prepareTicket": ticket,
        }),
    };
    let resp_1 = engine.handle_request(commit_1, &mut |_| {});
    assert!(resp_1.error.is_none());
    let c1 = fixture.read_counters();
    assert_eq!(c1.button_click_count, 1);

    // Second commit with the same (taskSessionId, operationId, prepareTicket) returns the cached
    // committed receipt and does NOT click the button a second time!
    let commit_2 = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-commit-dup-2".to_string()),
        method: "action.commit".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-dup-1",
            "operationId": "op-dup-click-1",
            "prepareTicket": ticket,
        }),
    };
    let resp_2 = engine.handle_request(commit_2, &mut |_| {});
    assert!(resp_2.error.is_none());
    assert_eq!(resp_1.result, resp_2.result);

    let c2 = fixture.read_counters();
    assert_eq!(
        c2.button_click_count, 1,
        "Duplicate commit must never re-execute physical click"
    );
    assert_eq!(c2.button_lbutton_down_count, 1);

    // Re-observe to get a fresh state token, then call `session.end` and verify the token is invalidated
    // while our owned fixture window remains open and untouched.
    let (state_token_after, _, _) =
        observe_owned_window(&mut engine, &fixture, "task-dup-1", &win_ref, false);

    let end_req = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-end-dup".to_string()),
        method: "session.end".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-dup-1"
        }),
    };
    let end_resp = engine.handle_request(end_req, &mut |_| {});
    assert!(end_resp.error.is_none());
    assert_eq!(end_resp.result.unwrap()["ended"], true);

    // Owned window is still alive and button click count is still 1
    fixture.assert_live_identity_proof();
    let c3 = fixture.read_counters();
    assert_eq!(c3.button_click_count, 1);

    // Attempting `action.prepare` on the retired task/token is rejected with TaskNotFound
    let prep_retired = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-prep-retired".to_string()),
        method: "action.prepare".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-dup-1",
            "operationId": "op-retired-1",
            "window": win_ref,
            "expectedStateToken": state_token_after,
            "action": {
                "type": "focus_window"
            }
        }),
    };
    let retired_err = engine
        .handle_request(prep_retired, &mut |_| {})
        .error
        .expect("Prepare on retired task session must fail");
    assert_eq!(retired_err.code, ErrorCode::TaskNotFound);

    // Shutdown worker engine and verify owned window is still open and untouched
    let shutdown_req = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-shutdown".to_string()),
        method: "shutdown".to_string(),
        params: serde_json::json!({}),
    };
    let shutdown_resp = engine.handle_request(shutdown_req, &mut |_| {});
    assert!(shutdown_resp.error.is_none());
    fixture.assert_live_identity_proof();
}

#[cfg(feature = "lifecycle-test-support")]
impl ControlledFixtureProcess {
    /// Captures final zero-input counters and triggers deterministic self-destruction of the owned
    /// fixture window via its private JSONL control channel (`{"cmd": "shutdown"}`), then waits up
    /// to `timeout` for the child process to exit cleanly.
    fn shutdown_privately_and_wait_exit(
        &mut self,
        stage_label: &str,
        timeout: Duration,
    ) -> (FixtureCounterSnapshot, std::process::ExitStatus) {
        self.assert_live_identity_proof();
        let val = self.send_control_cmd(serde_json::json!({
            "cmd": "shutdown",
            "req_id": stage_label
        }));
        assert_eq!(val["status"].as_str(), Some("shutting_down"));
        assert_eq!(val["reqId"].as_str(), Some(stage_label));
        let shutdown_placement: FixturePlacementDiagnostics =
            serde_json::from_value(val["placement"].clone())
                .expect("Failed to deserialize placement on shutdown");
        assert_eq!(shutdown_placement, self.ready.placement);

        let snap = FixtureCounterSnapshot {
            phase: serde_json::from_value(val["phase"].clone())
                .expect("Failed to deserialize fixture phase on shutdown"),
            setup_counters: serde_json::from_value(val["setupCounters"].clone())
                .expect("Failed to deserialize setupCounters on shutdown"),
            counters: serde_json::from_value(val["counters"].clone())
                .expect("Failed to deserialize counters on shutdown"),
            trace: serde_json::from_value(val["trace"].clone())
                .expect("Failed to deserialize trace on shutdown"),
            trace_dropped_count: val["traceDroppedCount"].as_u64().unwrap_or(0),
        };
        emit_stage_counter_diagnostic(&self.test_name, stage_label, &self.ready, &snap);
        if let Err(err_msg) =
            verify_setup_and_zero_input_invariants(stage_label, &self.ready_baseline, &snap)
        {
            panic!("{}", err_msg);
        }

        // Close stdin handle so Drop does not re-send shutdown after child exit.
        let _ = self.stdin.take();

        let deadline = std::time::Instant::now() + timeout;
        loop {
            match self.child.try_wait() {
                Ok(Some(status)) => return (snap, status),
                Ok(None) => {
                    if std::time::Instant::now() >= deadline {
                        let _ = self.child.kill();
                        let _ = self.child.wait();
                        panic!(
                            "Owned fixture child process (pid={}) did not exit within {:?} after private shutdown",
                            self.ready.pid, timeout
                        );
                    }
                    std::thread::sleep(Duration::from_millis(10));
                }
                Err(e) => panic!("try_wait failed on owned fixture child: {}", e),
            }
        }
    }
}

// ============================================================================
// Scenario 5 (N05B04/05 Real-Hook Candidate): Real WinEvent hook registration,
// actual win_event_proc CREATE/DESTROY producer delivery on owned fixture
// window, generation retirement, owner-thread UnhookWinEvent, and pinned
// thread handle signaled verification before join (zero business input)
// ============================================================================
#[cfg(feature = "lifecycle-test-support")]
#[test]
#[ignore = "requires root explicit real-hook owned-window whitelist"]
fn test_owned_window_real_winevent_create_destroy_owner_thread_shutdown_zero_business_input() {
    use ryn_computer_use::lifecycle_model::{
        CoverageFaultReason, CoverageState, LifecycleError, LifecycleEventKind, WindowOsTuple,
    };
    use std::time::Instant;
    use windows::Win32::System::Threading::GetCurrentThreadId;
    use windows::Win32::UI::WindowsAndMessaging::{
        CHILDID_SELF, EVENT_OBJECT_CREATE, EVENT_OBJECT_DESTROY, OBJID_WINDOW,
        WINEVENT_OUTOFCONTEXT, WINEVENT_SKIPOWNPROCESS,
    };

    verify_pure_counter_and_trace_classifier_invariants();

    // ------------------------------------------------------------------------
    // Stage 1: Real Hook Startup & Registration Proof (prior to fixture spawn)
    // ------------------------------------------------------------------------
    let (mut engine, _control) = init_engine();
    let caller_tid = unsafe { GetCurrentThreadId() };
    assert_eq!(
        engine.lifecycle_service().coverage_state(),
        CoverageState::Healthy
    );

    let startup_diag = engine
        .lifecycle_service()
        .snapshot_producer_diagnostics_for_hwnd(0)
        .expect("startup producer diagnostics must succeed");
    assert_eq!(startup_diag.coverage_state, CoverageState::Healthy);
    assert_eq!(startup_diag.coverage_generation, 1);
    let pump_tid = startup_diag.registration.pump_thread_id;
    assert_ne!(pump_tid, 0, "pump_thread_id must be non-zero");
    assert_ne!(
        pump_tid, caller_tid,
        "WinEvent pump must run on a dedicated thread distinct from engine/test thread"
    );
    assert!(
        startup_diag.registration.pump_thread_creation_time > 0,
        "pinned pump thread creation time from GetThreadTimes must be > 0"
    );
    assert!(
        startup_diag.registration.pinned_thread_handle_valid,
        "duplicated OS thread handle must be valid while pump is active"
    );
    assert!(
        startup_diag.registration.message_queue_initialized,
        "pump thread must initialize its Win32 message queue before SetWinEventHook"
    );
    assert!(
        startup_diag.registration.hook_handle_nonzero,
        "SetWinEventHook must return a non-null HWINEVENTHOOK"
    );
    assert_eq!(startup_diag.registration.hook_generation, 1);
    assert_eq!(startup_diag.registration.event_min, EVENT_OBJECT_CREATE);
    assert_eq!(startup_diag.registration.event_max, EVENT_OBJECT_DESTROY);
    assert_eq!(startup_diag.registration.hook_flags, WINEVENT_OUTOFCONTEXT);
    assert_eq!(
        startup_diag.registration.hook_flags & WINEVENT_SKIPOWNPROCESS,
        0,
        "WINEVENT_SKIPOWNPROCESS must NOT be set"
    );
    assert!(startup_diag.last_heartbeat_ns > 0);
    assert!(startup_diag.heartbeat_count >= 1);
    assert!(!startup_diag.trace_overflowed);

    // ------------------------------------------------------------------------
    // Stage 2: Owned Fixture Creation & Actual EVENT_OBJECT_CREATE Producer Proof
    // ------------------------------------------------------------------------
    let mut fixture = ControlledFixtureProcess::spawn_for_test(
        "test_owned_window_real_winevent_create_destroy_owner_thread_shutdown_zero_business_input",
    );
    fixture.assert_stage_matches_ready_baseline("post_fixture_spawn");

    let target_hwnd = fixture.hwnd_val;
    let (fixture_pid, fixture_tid) = get_window_pid_tid(u64_to_hwnd(target_hwnd));
    assert_eq!(fixture_pid, fixture.ready.pid);
    assert_ne!(fixture_tid, 0, "fixture main window thread ID must be non-zero");
    let fixture_started_ticks: u64 = fixture
        .ready
        .process_started
        .parse()
        .expect("fixture process_started must be decimal u64");
    let live_tuple =
        WindowOsTuple::new(target_hwnd, fixture_pid, fixture_started_ticks).unwrap();

    let create_deadline = Instant::now() + Duration::from_millis(1_500);
    let mut observed_create_trace = None;
    while Instant::now() < create_deadline {
        engine.drain_and_route_lifecycle_events();
        let diag = engine
            .lifecycle_service()
            .snapshot_producer_diagnostics_for_hwnd(target_hwnd)
            .expect("producer diagnostics during create wait must succeed");
        assert!(
            !diag.trace_overflowed,
            "preallocated callback trace must not overflow"
        );
        if let Some(rec) = diag
            .matched_target_traces
            .iter()
            .find(|r| r.kind == LifecycleEventKind::ObjectCreate && r.applied_to_registry)
        {
            observed_create_trace = Some(rec.clone());
            break;
        }
        std::thread::sleep(Duration::from_millis(15));
    }

    let create_trace = observed_create_trace.expect(
        "real win_event_proc must receive EVENT_OBJECT_CREATE for owned fixture main HWND and apply it to registry",
    );
    assert_eq!(create_trace.raw_event, EVENT_OBJECT_CREATE);
    assert_eq!(create_trace.kind, LifecycleEventKind::ObjectCreate);
    assert_eq!(create_trace.hwnd, target_hwnd);
    assert_eq!(create_trace.id_object, OBJID_WINDOW.0);
    assert_eq!(create_trace.id_child, CHILDID_SELF as i32);
    assert_eq!(create_trace.hook_generation, 1);
    assert_eq!(
        create_trace.actual_callback_tid, pump_tid,
        "win_event_proc for EVENT_OBJECT_CREATE must execute on the dedicated pump thread"
    );
    assert_eq!(
        create_trace.source_event_thread, fixture_tid,
        "EVENT_OBJECT_CREATE idEventThread must match owned fixture window thread ID"
    );
    assert!(create_trace.arrival_seq >= 1);
    assert!(create_trace.monotonic_timestamp_ns > 0);
    assert!(create_trace.applied_to_registry);

    // ------------------------------------------------------------------------
    // Stage 3: Issuance, Original Stamp Capture, Snapshot Binding & Zero-Input Observe
    // ------------------------------------------------------------------------
    let win_ref = discover_and_prove_owned_window(&mut engine, &fixture);
    assert!(win_ref.generation >= 1);
    fixture.assert_stage_matches_ready_baseline("post_windows_get");

    // Capture the actual issued LifecycleStamp after CREATE was applied and verify it validates.
    let original_stamp = engine
        .lifecycle_service()
        .validate_window_ref_and_stamp(&win_ref, live_tuple, None)
        .expect("issued WindowRef must validate and return its active LifecycleStamp");
    assert_eq!(original_stamp.os_tuple, live_tuple);
    assert_eq!(original_stamp.issue_generation, win_ref.generation);
    assert_eq!(original_stamp.window_key, win_ref.window_key);
    engine
        .lifecycle_service()
        .validate_window_ref_and_stamp(&win_ref, live_tuple, Some(&original_stamp))
        .expect("issued WindowRef + original LifecycleStamp must validate before DESTROY");

    let (state_token, _elements, _obs_val) =
        observe_owned_window(&mut engine, &fixture, "task-real-hook-1", &win_ref, false);
    assert!(state_token.starts_with("st_"));
    fixture.assert_stage_matches_ready_baseline("post_state_get_zero_input");

    // ------------------------------------------------------------------------
    // Stage 4: Controlled Owned Window Destruction & Actual EVENT_OBJECT_DESTROY Proof
    // ------------------------------------------------------------------------
    let (final_fixture_snap, child_exit_status) = fixture
        .shutdown_privately_and_wait_exit("pre_fixture_shutdown", Duration::from_millis(1_500));
    assert!(
        child_exit_status.success(),
        "owned fixture process must exit with code 0 after private shutdown"
    );
    assert!(
        unsafe { !IsWindow(Some(u64_to_hwnd(target_hwnd))).as_bool() },
        "owned fixture HWND must no longer be a live window after private shutdown and child exit"
    );

    let destroy_deadline = Instant::now() + Duration::from_millis(1_500);
    let mut observed_destroy_trace = None;
    while Instant::now() < destroy_deadline {
        engine.drain_and_route_lifecycle_events();
        let diag = engine
            .lifecycle_service()
            .snapshot_producer_diagnostics_for_hwnd(target_hwnd)
            .expect("producer diagnostics during destroy wait must succeed");
        assert!(
            !diag.trace_overflowed,
            "preallocated callback trace must not overflow"
        );
        if let Some(rec) = diag
            .matched_target_traces
            .iter()
            .find(|r| r.kind == LifecycleEventKind::ObjectDestroy && r.applied_to_registry)
        {
            observed_destroy_trace = Some(rec.clone());
            break;
        }
        std::thread::sleep(Duration::from_millis(15));
    }

    let destroy_trace = observed_destroy_trace.expect(
        "real win_event_proc must receive EVENT_OBJECT_DESTROY for owned fixture main HWND and retire issued generation",
    );
    assert_eq!(destroy_trace.raw_event, EVENT_OBJECT_DESTROY);
    assert_eq!(destroy_trace.kind, LifecycleEventKind::ObjectDestroy);
    assert_eq!(destroy_trace.hwnd, target_hwnd);
    assert_eq!(destroy_trace.id_object, OBJID_WINDOW.0);
    assert_eq!(destroy_trace.id_child, CHILDID_SELF as i32);
    assert_eq!(destroy_trace.hook_generation, 1);
    assert_eq!(
        destroy_trace.actual_callback_tid, pump_tid,
        "win_event_proc for EVENT_OBJECT_DESTROY must execute on the dedicated pump thread"
    );
    assert_eq!(
        destroy_trace.source_event_thread, fixture_tid,
        "EVENT_OBJECT_DESTROY idEventThread must match owned fixture window thread ID"
    );
    assert!(
        destroy_trace.arrival_seq > create_trace.arrival_seq,
        "EVENT_OBJECT_DESTROY arrival_seq ({}) must be strictly greater than EVENT_OBJECT_CREATE arrival_seq ({})",
        destroy_trace.arrival_seq,
        create_trace.arrival_seq
    );
    assert!(
        destroy_trace.monotonic_timestamp_ns >= create_trace.monotonic_timestamp_ns,
        "EVENT_OBJECT_DESTROY monotonic_timestamp_ns must be >= EVENT_OBJECT_CREATE monotonic_timestamp_ns"
    );
    assert!(destroy_trace.applied_to_registry);
    assert_eq!(
        destroy_trace.retired_issue_generation,
        Some(win_ref.generation),
        "real EVENT_OBJECT_DESTROY delivery must retire the exact issued WindowRef generation"
    );

    // Reject both the original WindowRef and the saved original LifecycleStamp in the registry
    // after the real EVENT_OBJECT_DESTROY was applied.
    let reg_ref_err = engine
        .lifecycle_service()
        .validate_window_ref_and_stamp(&win_ref, live_tuple, None)
        .expect_err("retired WindowRef generation must fail registry validation");
    assert!(
        matches!(
            reg_ref_err,
            LifecycleError::WindowGenerationRetiredOrEvicted {
                hwnd,
                expected_generation,
            } if hwnd == target_hwnd && expected_generation == win_ref.generation
        ) || matches!(
            reg_ref_err,
            LifecycleError::TargetEventRevisionAdvanced { hwnd, .. } if hwnd == target_hwnd
        ),
        "expected WindowGenerationRetiredOrEvicted or TargetEventRevisionAdvanced for WindowRef, got: {:?}",
        reg_ref_err
    );

    let reg_stamp_err = engine
        .lifecycle_service()
        .validate_window_ref_and_stamp(&win_ref, live_tuple, Some(&original_stamp))
        .expect_err("saved original LifecycleStamp must fail registry validation after real EVENT_OBJECT_DESTROY");
    assert!(
        matches!(
            reg_stamp_err,
            LifecycleError::WindowGenerationRetiredOrEvicted {
                hwnd,
                expected_generation,
            } if hwnd == target_hwnd && expected_generation == win_ref.generation
        ) || matches!(
            reg_stamp_err,
            LifecycleError::TargetEventRevisionAdvanced { hwnd, .. } if hwnd == target_hwnd
        ),
        "expected WindowGenerationRetiredOrEvicted or TargetEventRevisionAdvanced for original LifecycleStamp, got: {:?}",
        reg_stamp_err
    );

    // Single negative `action.prepare` after confirmed DESTROY and registry rejection:
    // must fail closed with `MutationState::None` without issuing any ticket or executing input.
    let prep_req = RequestEnvelope {
        v: WIRE_VERSION,
        id: RequestId::String("req-prep-after-destroy".to_string()),
        method: "action.prepare".to_string(),
        params: serde_json::json!({
            "taskSessionId": "task-real-hook-1",
            "operationId": "op-after-destroy-1",
            "window": win_ref,
            "expectedStateToken": state_token,
            "action": {
                "type": "focus_window"
            }
        }),
    };
    let prep_err = engine
        .handle_request(prep_req, &mut |_| {})
        .error
        .expect("negative action.prepare after real EVENT_OBJECT_DESTROY retirement must fail closed");
    assert_eq!(prep_err.mutation, MutationState::None);
    assert!(
        matches!(
            prep_err.code,
            ErrorCode::StateTokenInvalid | ErrorCode::WindowNotFound | ErrorCode::WindowStale
        ),
        "unexpected error code after destroy retirement: {:?}",
        prep_err.code
    );

    // ------------------------------------------------------------------------
    // Stage 5: Zero-Business-Input Invariant Proof (actual FixtureCounters + setup/runtime)
    // ------------------------------------------------------------------------
    assert!(
        verify_setup_and_zero_input_invariants(
            "final_zero_input_proof",
            &fixture.ready_baseline,
            &final_fixture_snap,
        )
        .is_ok()
    );
    let c = &final_fixture_snap.counters;
    assert_eq!(
        c.total_physical_focus_mouse_key_wheel_and_settext_mutations(),
        0
    );
    assert_eq!(c.total_runtime_mutations_after_setup(), 0);
    assert_eq!(c.total_all_lifetime_raw_events(), 2);
    assert_eq!(c.main_focus_gained_count, 0);
    assert_eq!(c.main_focus_lost_count, 0);
    assert_eq!(c.button_focus_gained_count, 0);
    assert_eq!(c.button_focus_lost_count, 0);
    assert_eq!(c.public_edit_focus_gained_count, 0);
    assert_eq!(c.public_edit_focus_lost_count, 0);
    assert_eq!(c.password_edit_focus_gained_count, 0);
    assert_eq!(c.password_edit_focus_lost_count, 0);
    assert_eq!(c.button_click_count, 0);
    assert_eq!(c.button_lbutton_down_count, 0);
    assert_eq!(c.button_lbutton_up_count, 0);
    assert_eq!(c.public_edit_char_count, 0);
    assert_eq!(c.public_edit_keydown_count, 0);
    assert_eq!(c.public_edit_keyup_count, 0);
    assert_eq!(c.public_edit_settext_count, 0);
    assert_eq!(c.password_edit_char_count, 0);
    assert_eq!(c.password_edit_keydown_count, 0);
    assert_eq!(c.password_edit_keyup_count, 0);
    assert_eq!(c.password_edit_settext_count, 0);
    assert_eq!(c.setup_public_edit_change_count, 1);
    assert_eq!(c.public_edit_change_count, 1);
    assert_eq!(c.runtime_public_edit_change_count, 0);
    assert_eq!(c.setup_password_edit_change_count, 1);
    assert_eq!(c.password_edit_change_count, 1);
    assert_eq!(c.runtime_password_edit_change_count, 0);
    assert_eq!(final_fixture_snap, fixture.ready_baseline);

    // ------------------------------------------------------------------------
    // Stage 6: Bounded Owner-Thread UnhookWinEvent & Pinned Thread Exit Proof
    // ------------------------------------------------------------------------
    engine
        .lifecycle_service()
        .shutdown_bounded()
        .expect("bounded real-hook shutdown must succeed within HOOK_STOP_TIMEOUT_MS");

    let post_shutdown_diag = engine
        .lifecycle_service()
        .snapshot_producer_diagnostics_for_hwnd(target_hwnd)
        .expect("post-shutdown producer diagnostics must remain readable");

    assert!(
        post_shutdown_diag.last_heartbeat_ns >= startup_diag.last_heartbeat_ns,
        "pump heartbeat timestamp must progress monotonically"
    );
    assert!(
        post_shutdown_diag.heartbeat_count >= startup_diag.heartbeat_count,
        "pump heartbeat count must progress monotonically"
    );
    assert!(post_shutdown_diag.shutdown.stop_requested);
    assert!(post_shutdown_diag.shutdown.stop_acknowledged);
    assert!(post_shutdown_diag.shutdown.unhook_called);
    assert!(post_shutdown_diag.shutdown.unhook_succeeded);
    assert_eq!(
        post_shutdown_diag.shutdown.unhook_thread_id, pump_tid,
        "UnhookWinEvent must execute on the exact pump thread that called SetWinEventHook"
    );
    assert!(
        post_shutdown_diag.shutdown.pinned_thread_signaled_before_join,
        "pinned duplicated OS thread handle must reach WAIT_OBJECT_0 before JoinHandle::join"
    );
    assert!(
        post_shutdown_diag.shutdown.join_completed_within_budget,
        "pump thread JoinHandle::join must complete within single bounded stop budget"
    );
    assert!(post_shutdown_diag.shutdown.stop_budget_ms > 0);
    assert!(
        post_shutdown_diag.shutdown.stop_elapsed_ms <= post_shutdown_diag.shutdown.stop_budget_ms,
        "stop_elapsed_ms ({}) must not exceed stop_budget_ms ({})",
        post_shutdown_diag.shutdown.stop_elapsed_ms,
        post_shutdown_diag.shutdown.stop_budget_ms
    );
    assert!(!post_shutdown_diag.shutdown.cleanup_quarantined);
    assert!(!engine.lifecycle_service().is_cleanup_quarantined());
    assert_eq!(
        post_shutdown_diag.coverage_state,
        CoverageState::Unhealthy(CoverageFaultReason::PumpExited)
    );
    assert!(!post_shutdown_diag.trace_overflowed);

    let evidence_summary = serde_json::json!({
        "test": "test_owned_window_real_winevent_create_destroy_owner_thread_shutdown_zero_business_input",
        "targetHwnd": target_hwnd,
        "fixturePid": fixture_pid,
        "fixtureTid": fixture_tid,
        "fixtureProcessStarted": fixture.ready.process_started,
        "fixtureNonce": fixture.ready.nonce,
        "numericHwndReuseObserved": false,
        "registration": {
            "pumpThreadId": pump_tid,
            "pumpThreadCreationTime": post_shutdown_diag.registration.pump_thread_creation_time,
            "pinnedThreadHandleValid": startup_diag.registration.pinned_thread_handle_valid,
            "messageQueueInitialized": post_shutdown_diag.registration.message_queue_initialized,
            "hookHandleNonzero": post_shutdown_diag.registration.hook_handle_nonzero,
            "hookGeneration": post_shutdown_diag.registration.hook_generation,
            "eventMin": post_shutdown_diag.registration.event_min,
            "eventMax": post_shutdown_diag.registration.event_max,
            "hookFlags": post_shutdown_diag.registration.hook_flags,
        },
        "heartbeat": {
            "startupHeartbeatNs": startup_diag.last_heartbeat_ns,
            "finalHeartbeatNs": post_shutdown_diag.last_heartbeat_ns,
            "startupHeartbeatCount": startup_diag.heartbeat_count,
            "finalHeartbeatCount": post_shutdown_diag.heartbeat_count,
        },
        "totalAcceptedCallbacks": post_shutdown_diag.total_accepted_callbacks,
        "traceOverflowed": post_shutdown_diag.trace_overflowed,
        "createTrace": {
            "rawEvent": create_trace.raw_event,
            "kind": format!("{:?}", create_trace.kind),
            "hwnd": create_trace.hwnd,
            "idObject": create_trace.id_object,
            "idChild": create_trace.id_child,
            "hookGeneration": create_trace.hook_generation,
            "arrivalSeq": create_trace.arrival_seq,
            "actualCallbackTid": create_trace.actual_callback_tid,
            "sourceEventThread": create_trace.source_event_thread,
            "eventTimeMs": create_trace.event_time_ms,
            "monotonicTimestampNs": create_trace.monotonic_timestamp_ns,
            "appliedToRegistry": create_trace.applied_to_registry,
            "retiredIssueGeneration": create_trace.retired_issue_generation,
        },
        "destroyTrace": {
            "rawEvent": destroy_trace.raw_event,
            "kind": format!("{:?}", destroy_trace.kind),
            "hwnd": destroy_trace.hwnd,
            "idObject": destroy_trace.id_object,
            "idChild": destroy_trace.id_child,
            "hookGeneration": destroy_trace.hook_generation,
            "arrivalSeq": destroy_trace.arrival_seq,
            "actualCallbackTid": destroy_trace.actual_callback_tid,
            "sourceEventThread": destroy_trace.source_event_thread,
            "eventTimeMs": destroy_trace.event_time_ms,
            "monotonicTimestampNs": destroy_trace.monotonic_timestamp_ns,
            "appliedToRegistry": destroy_trace.applied_to_registry,
            "retiredIssueGeneration": destroy_trace.retired_issue_generation,
        },
        "shutdown": {
            "stopRequested": post_shutdown_diag.shutdown.stop_requested,
            "stopAcknowledged": post_shutdown_diag.shutdown.stop_acknowledged,
            "unhookCalled": post_shutdown_diag.shutdown.unhook_called,
            "unhookSucceeded": post_shutdown_diag.shutdown.unhook_succeeded,
            "unhookThreadId": post_shutdown_diag.shutdown.unhook_thread_id,
            "pinnedThreadSignaledBeforeJoin": post_shutdown_diag.shutdown.pinned_thread_signaled_before_join,
            "joinCompletedWithinBudget": post_shutdown_diag.shutdown.join_completed_within_budget,
            "stopElapsedMs": post_shutdown_diag.shutdown.stop_elapsed_ms,
            "stopBudgetMs": post_shutdown_diag.shutdown.stop_budget_ms,
            "cleanupQuarantined": post_shutdown_diag.shutdown.cleanup_quarantined,
        },
        "zeroBusinessInputProof": {
            "totalPhysicalFocusMouseKeyWheelAndSettextMutations": c.total_physical_focus_mouse_key_wheel_and_settext_mutations(),
            "totalRuntimeMutationsAfterSetup": c.total_runtime_mutations_after_setup(),
            "totalAllLifetimeRawEvents": c.total_all_lifetime_raw_events(),
            "setupPublicEditChangeCount": c.setup_public_edit_change_count,
            "runtimePublicEditChangeCount": c.runtime_public_edit_change_count,
            "setupPasswordEditChangeCount": c.setup_password_edit_change_count,
            "runtimePasswordEditChangeCount": c.runtime_password_edit_change_count,
            "traceDroppedCount": final_fixture_snap.trace_dropped_count,
            "traceLen": final_fixture_snap.trace.len(),
            "counters": c,
        }
    });
    eprintln!(
        "[real_hook_candidate_evidence] {}",
        serde_json::to_string(&evidence_summary).unwrap_or_default()
    );
}


