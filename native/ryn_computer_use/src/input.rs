use crate::errors::{
    DiagnosticReason, ErrorCode, ErrorStage, InternalMutationDetail, ProtocolError,
};
use crate::focus::verify_foreground_relation;
use crate::protocol::{
    ActionSpec, ElementTarget, PointTarget, RectPx, ResolvedTargetSummary, UiaElement,
    WindowGeometry,
};
use crate::snapshot::SnapshotRecord;
use crate::uia::{
    execute_uia_focus, execute_uia_invoke, execute_uia_select, execute_uia_set_value,
    verify_live_element_for_commit_target,
};
use crate::windows::{hwnd_to_u64, is_hwnd_related_to_target};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;
use windows::Win32::Foundation::{HWND, POINT};
use windows::Win32::UI::Input::KeyboardAndMouse::{
    SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, INPUT_MOUSE, KEYBDINPUT, KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_HWHEEL, MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP, MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, MOUSEEVENTF_MOVE,
    MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_WHEEL,
    MOUSEINPUT, VIRTUAL_KEY, VK_BACK, VK_CONTROL, VK_DELETE, VK_DOWN, VK_END, VK_ESCAPE, VK_F1,
    VK_F10, VK_F11, VK_F12, VK_F2, VK_F3, VK_F4, VK_F5, VK_F6, VK_F7, VK_F8, VK_F9, VK_HOME,
    VK_INSERT, VK_LEFT, VK_LMENU, VK_LWIN, VK_MENU, VK_NEXT, VK_PRIOR, VK_RETURN, VK_RIGHT,
    VK_SHIFT, VK_SPACE, VK_TAB, VK_UP,
};
use windows::Win32::UI::WindowsAndMessaging::{
    GetSystemMetrics, WindowFromPoint, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN, SM_XVIRTUALSCREEN,
    SM_YVIRTUALSCREEN,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MouseButtonKind {
    Left,
    Right,
    Middle,
}

impl MouseButtonKind {
    pub fn parse_optional(s: Option<&str>) -> Result<Self, ProtocolError> {
        match s.unwrap_or("left") {
            "left" => Ok(Self::Left),
            "right" => Ok(Self::Right),
            "middle" => Ok(Self::Middle),
            _ => Err(ProtocolError::validate(
                ErrorCode::InvalidParams,
                DiagnosticReason::MalformedJson,
            )),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SnapshotElementBinding {
    pub index: u32,
    pub runtime_id: String,
    pub snapshot_bounds: RectPx,
    pub snapshot_password: bool,
}

#[derive(Debug, Clone)]
pub enum ResolvedExecutionOp {
    FocusWindow,
    MouseClick {
        screen_pt: [i32; 2],
        element_binding: Option<SnapshotElementBinding>,
        button: MouseButtonKind,
        double: bool,
    },
    MovePointer {
        screen_pt: [i32; 2],
    },
    Scroll {
        screen_pt: [i32; 2],
        delta_x: i32,
        delta_y: i32,
    },
    TypeText {
        click_first_pt: Option<[i32; 2]>,
        element_binding: Option<SnapshotElementBinding>,
        text: String,
    },
    KeyPress {
        vk: VIRTUAL_KEY,
        repeat: u32,
    },
    KeyChord {
        vks: Vec<VIRTUAL_KEY>,
    },
    UiaInvoke {
        binding: SnapshotElementBinding,
    },
    UiaSetValue {
        binding: SnapshotElementBinding,
        value: String,
    },
    UiaSelect {
        binding: SnapshotElementBinding,
    },
    UiaFocus {
        binding: SnapshotElementBinding,
    },
}

#[derive(Debug, Clone)]
pub struct ResolvedActionPlan {
    pub summary: ResolvedTargetSummary,
    pub op: ResolvedExecutionOp,
}

/// Validates and resolves an `ActionSpec` against `snapshot` and `live_geometry` during `action.prepare`.
/// Performs zero OS focus/input side effects. Explicitly rejects unsupported actions (`drag`, `launch_app`).
pub fn resolve_action_plan(
    hwnd: HWND,
    snapshot: &SnapshotRecord,
    live_geometry: &WindowGeometry,
    action: &ActionSpec,
) -> Result<ResolvedActionPlan, ProtocolError> {
    let window_hwnd = hwnd_to_u64(hwnd).to_string();

    match action {
        ActionSpec::FocusWindow { .. } => Ok(ResolvedActionPlan {
            summary: ResolvedTargetSummary {
                window_hwnd,
                screen_point: None,
                secondary_screen_point: None,
                element_index: None,
                element_runtime_id: None,
            },
            op: ResolvedExecutionOp::FocusWindow,
        }),

        ActionSpec::Click {
            button,
            target,
            element,
        } => {
            let btn = MouseButtonKind::parse_optional(button.as_deref())?;
            let (pt, binding) = resolve_point_or_element(
                snapshot,
                live_geometry,
                target.as_ref(),
                element.as_ref(),
                true,
            )?;
            Ok(ResolvedActionPlan {
                summary: ResolvedTargetSummary {
                    window_hwnd,
                    screen_point: Some(pt),
                    secondary_screen_point: None,
                    element_index: binding.as_ref().map(|b| b.index),
                    element_runtime_id: binding.as_ref().map(|b| b.runtime_id.clone()),
                },
                op: ResolvedExecutionOp::MouseClick {
                    screen_pt: pt,
                    element_binding: binding,
                    button: btn,
                    double: false,
                },
            })
        }

        ActionSpec::DoubleClick {
            button,
            target,
            element,
        } => {
            let btn = MouseButtonKind::parse_optional(button.as_deref())?;
            let (pt, binding) = resolve_point_or_element(
                snapshot,
                live_geometry,
                target.as_ref(),
                element.as_ref(),
                true,
            )?;
            Ok(ResolvedActionPlan {
                summary: ResolvedTargetSummary {
                    window_hwnd,
                    screen_point: Some(pt),
                    secondary_screen_point: None,
                    element_index: binding.as_ref().map(|b| b.index),
                    element_runtime_id: binding.as_ref().map(|b| b.runtime_id.clone()),
                },
                op: ResolvedExecutionOp::MouseClick {
                    screen_pt: pt,
                    element_binding: binding,
                    button: btn,
                    double: true,
                },
            })
        }

        ActionSpec::ContextClick { target, element } => {
            let (pt, binding) = resolve_point_or_element(
                snapshot,
                live_geometry,
                target.as_ref(),
                element.as_ref(),
                true,
            )?;
            Ok(ResolvedActionPlan {
                summary: ResolvedTargetSummary {
                    window_hwnd,
                    screen_point: Some(pt),
                    secondary_screen_point: None,
                    element_index: binding.as_ref().map(|b| b.index),
                    element_runtime_id: binding.as_ref().map(|b| b.runtime_id.clone()),
                },
                op: ResolvedExecutionOp::MouseClick {
                    screen_pt: pt,
                    element_binding: binding,
                    button: MouseButtonKind::Right,
                    double: false,
                },
            })
        }

        ActionSpec::MovePointer { target } => {
            let pt = resolve_point_target(live_geometry, target)?;
            Ok(ResolvedActionPlan {
                summary: ResolvedTargetSummary {
                    window_hwnd,
                    screen_point: Some(pt),
                    secondary_screen_point: None,
                    element_index: None,
                    element_runtime_id: None,
                },
                op: ResolvedExecutionOp::MovePointer { screen_pt: pt },
            })
        }

        ActionSpec::Scroll {
            target,
            delta_x,
            delta_y,
        } => {
            if *delta_x == 0 && *delta_y == 0 {
                return Err(ProtocolError::validate(
                    ErrorCode::InvalidParams,
                    DiagnosticReason::MalformedJson,
                ));
            }
            let pt = resolve_point_target(live_geometry, target)?;
            Ok(ResolvedActionPlan {
                summary: ResolvedTargetSummary {
                    window_hwnd,
                    screen_point: Some(pt),
                    secondary_screen_point: None,
                    element_index: None,
                    element_runtime_id: None,
                },
                op: ResolvedExecutionOp::Scroll {
                    screen_pt: pt,
                    delta_x: *delta_x,
                    delta_y: *delta_y,
                },
            })
        }

        ActionSpec::TypeText {
            text,
            target,
            element,
        } => {
            if text.is_empty() || text.chars().count() > 4096 {
                return Err(ProtocolError::validate(
                    ErrorCode::InvalidParams,
                    DiagnosticReason::MalformedJson,
                ));
            }
            let mut click_first_pt = None;
            let mut elem_binding: Option<SnapshotElementBinding> = None;

            if target.is_some() && element.is_some() {
                return Err(ProtocolError::validate(
                    ErrorCode::InvalidParams,
                    DiagnosticReason::MalformedJson,
                ));
            }

            if let Some(t) = target {
                click_first_pt = Some(resolve_point_target(live_geometry, t)?);
            } else if let Some(el_ref) = element {
                let uia_el = resolve_snapshot_element(snapshot, el_ref, false)?;
                let binding = SnapshotElementBinding {
                    index: uia_el.index,
                    runtime_id: uia_el.runtime_id.clone(),
                    snapshot_bounds: uia_el.bounds,
                    snapshot_password: uia_el.password,
                };
                elem_binding = Some(binding);
            }

            let summary_pt = click_first_pt.or_else(|| {
                elem_binding.as_ref().and_then(|b| {
                    if b.snapshot_bounds.width > 0 && b.snapshot_bounds.height > 0 {
                        Some([
                            b.snapshot_bounds.left + b.snapshot_bounds.width / 2,
                            b.snapshot_bounds.top + b.snapshot_bounds.height / 2,
                        ])
                    } else {
                        None
                    }
                })
            });

            Ok(ResolvedActionPlan {
                summary: ResolvedTargetSummary {
                    window_hwnd,
                    screen_point: summary_pt,
                    secondary_screen_point: None,
                    element_index: elem_binding.as_ref().map(|b| b.index),
                    element_runtime_id: elem_binding.as_ref().map(|b| b.runtime_id.clone()),
                },
                op: ResolvedExecutionOp::TypeText {
                    click_first_pt,
                    element_binding: elem_binding,
                    text: text.clone(),
                },
            })
        }

        ActionSpec::KeyPress { key, repeat } => {
            let vk = parse_virtual_key(key)?;
            let rep = repeat.unwrap_or(1).clamp(1, 32);
            Ok(ResolvedActionPlan {
                summary: ResolvedTargetSummary {
                    window_hwnd,
                    screen_point: None,
                    secondary_screen_point: None,
                    element_index: None,
                    element_runtime_id: None,
                },
                op: ResolvedExecutionOp::KeyPress { vk, repeat: rep },
            })
        }

        ActionSpec::KeyChord { keys } => {
            if keys.is_empty() || keys.len() > 6 {
                return Err(ProtocolError::validate(
                    ErrorCode::InvalidParams,
                    DiagnosticReason::MalformedJson,
                ));
            }
            let mut vks = Vec::with_capacity(keys.len());
            for k in keys {
                vks.push(parse_virtual_key(k)?);
            }
            Ok(ResolvedActionPlan {
                summary: ResolvedTargetSummary {
                    window_hwnd,
                    screen_point: None,
                    secondary_screen_point: None,
                    element_index: None,
                    element_runtime_id: None,
                },
                op: ResolvedExecutionOp::KeyChord { vks },
            })
        }

        ActionSpec::UiaInvoke { element } => {
            let uia_el = resolve_snapshot_element(snapshot, element, false)?;
            let binding = SnapshotElementBinding {
                index: uia_el.index,
                runtime_id: uia_el.runtime_id.clone(),
                snapshot_bounds: uia_el.bounds,
                snapshot_password: uia_el.password,
            };
            Ok(ResolvedActionPlan {
                summary: ResolvedTargetSummary {
                    window_hwnd,
                    screen_point: None,
                    secondary_screen_point: None,
                    element_index: Some(uia_el.index),
                    element_runtime_id: Some(uia_el.runtime_id),
                },
                op: ResolvedExecutionOp::UiaInvoke { binding },
            })
        }

        ActionSpec::UiaSetValue { element, value } => {
            if value.chars().count() > 4096 {
                return Err(ProtocolError::validate(
                    ErrorCode::InvalidParams,
                    DiagnosticReason::MalformedJson,
                ));
            }
            let uia_el = resolve_snapshot_element(snapshot, element, false)?;
            let binding = SnapshotElementBinding {
                index: uia_el.index,
                runtime_id: uia_el.runtime_id.clone(),
                snapshot_bounds: uia_el.bounds,
                snapshot_password: uia_el.password,
            };
            Ok(ResolvedActionPlan {
                summary: ResolvedTargetSummary {
                    window_hwnd,
                    screen_point: None,
                    secondary_screen_point: None,
                    element_index: Some(uia_el.index),
                    element_runtime_id: Some(uia_el.runtime_id),
                },
                op: ResolvedExecutionOp::UiaSetValue {
                    binding,
                    value: value.clone(),
                },
            })
        }

        ActionSpec::UiaSelect { element } => {
            let uia_el = resolve_snapshot_element(snapshot, element, false)?;
            let binding = SnapshotElementBinding {
                index: uia_el.index,
                runtime_id: uia_el.runtime_id.clone(),
                snapshot_bounds: uia_el.bounds,
                snapshot_password: uia_el.password,
            };
            Ok(ResolvedActionPlan {
                summary: ResolvedTargetSummary {
                    window_hwnd,
                    screen_point: None,
                    secondary_screen_point: None,
                    element_index: Some(uia_el.index),
                    element_runtime_id: Some(uia_el.runtime_id),
                },
                op: ResolvedExecutionOp::UiaSelect { binding },
            })
        }

        ActionSpec::UiaFocus { element } => {
            let uia_el = resolve_snapshot_element(snapshot, element, true)?;
            let binding = SnapshotElementBinding {
                index: uia_el.index,
                runtime_id: uia_el.runtime_id.clone(),
                snapshot_bounds: uia_el.bounds,
                snapshot_password: uia_el.password,
            };
            Ok(ResolvedActionPlan {
                summary: ResolvedTargetSummary {
                    window_hwnd,
                    screen_point: None,
                    secondary_screen_point: None,
                    element_index: Some(uia_el.index),
                    element_runtime_id: Some(uia_el.runtime_id),
                },
                op: ResolvedExecutionOp::UiaFocus { binding },
            })
        }

        ActionSpec::Drag { .. } => Err(ProtocolError::prepare(
            ErrorCode::Unsupported,
            DiagnosticReason::UiaPatternMissing,
        )),

        ActionSpec::LaunchApp { .. } => Err(ProtocolError::prepare(
            ErrorCode::Unsupported,
            DiagnosticReason::DirectLaunchDisallowed,
        )),
    }
}

fn resolve_point_or_element(
    snapshot: &SnapshotRecord,
    live_geometry: &WindowGeometry,
    target: Option<&PointTarget>,
    element: Option<&ElementTarget>,
    allow_password_target: bool,
) -> Result<([i32; 2], Option<SnapshotElementBinding>), ProtocolError> {
    match (target, element) {
        (Some(pt_target), None) => {
            let pt = resolve_point_target(live_geometry, pt_target)?;
            Ok((pt, None))
        }
        (None, Some(el_target)) => {
            let uia_el = resolve_snapshot_element(snapshot, el_target, allow_password_target)?;
            if uia_el.bounds.width <= 0 || uia_el.bounds.height <= 0 {
                return Err(ProtocolError::validate(
                    ErrorCode::InvalidParams,
                    DiagnosticReason::PointOutOfBounds,
                ));
            }
            let cx = uia_el.bounds.left + uia_el.bounds.width / 2;
            let cy = uia_el.bounds.top + uia_el.bounds.height / 2;
            verify_screen_point_within_rect(cx, cy, &live_geometry.window_rect)?;
            let binding = SnapshotElementBinding {
                index: uia_el.index,
                runtime_id: uia_el.runtime_id,
                snapshot_bounds: uia_el.bounds,
                snapshot_password: uia_el.password,
            };
            Ok(([cx, cy], Some(binding)))
        }
        _ => Err(ProtocolError::validate(
            ErrorCode::InvalidParams,
            DiagnosticReason::MalformedJson,
        )),
    }
}

pub fn resolve_point_target(
    live_geometry: &WindowGeometry,
    target: &PointTarget,
) -> Result<[i32; 2], ProtocolError> {
    let rect = match target.coordinate_space.as_str() {
        "window_physical_px" => &live_geometry.window_rect,
        "client_physical_px" => &live_geometry.client_rect,
        _ => {
            return Err(ProtocolError::validate(
                ErrorCode::InvalidParams,
                DiagnosticReason::PointOutOfBounds,
            ))
        }
    };

    if target.x < 0 || target.y < 0 || target.x >= rect.width || target.y >= rect.height {
        return Err(ProtocolError::validate(
            ErrorCode::InvalidParams,
            DiagnosticReason::PointOutOfBounds,
        ));
    }

    let sx = rect.left + target.x;
    let sy = rect.top + target.y;
    verify_screen_point_within_rect(sx, sy, &live_geometry.window_rect)?;
    Ok([sx, sy])
}

fn verify_screen_point_within_rect(
    sx: i32,
    sy: i32,
    rect: &crate::protocol::RectPx,
) -> Result<(), ProtocolError> {
    if sx < rect.left || sy < rect.top || sx >= rect.right || sy >= rect.bottom {
        return Err(ProtocolError::validate(
            ErrorCode::InvalidParams,
            DiagnosticReason::PointOutOfBounds,
        ));
    }
    Ok(())
}

fn resolve_snapshot_element(
    snapshot: &SnapshotRecord,
    el_target: &ElementTarget,
    allow_password_target: bool,
) -> Result<UiaElement, ProtocolError> {
    let found = snapshot
        .elements
        .iter()
        .find(|e| e.index == el_target.index)
        .ok_or_else(|| {
            ProtocolError::validate(
                ErrorCode::UiaElementStale,
                DiagnosticReason::UiaPatternMissing,
            )
        })?;

    if let Some(ref expected_rid) = el_target.runtime_id {
        if &found.runtime_id != expected_rid {
            return Err(ProtocolError::validate(
                ErrorCode::UiaElementStale,
                DiagnosticReason::UiaPatternMissing,
            ));
        }
    }

    if found.password && !allow_password_target {
        return Err(ProtocolError::validate(
            ErrorCode::Unsupported,
            DiagnosticReason::PasswordRedacted,
        ));
    }

    if !found.enabled || found.offscreen {
        return Err(ProtocolError::validate(
            ErrorCode::StateTokenStale,
            DiagnosticReason::UiaPatternMissing,
        ));
    }

    Ok(found.clone())
}

/// Verifies via `WindowFromPoint` that `screen_pt` hits `target_hwnd`, a child/descendant,
/// or a directionally owned popup window of `target_hwnd`. Rejects with `WindowOccluded` if an unrelated window
/// covers the target coordinate.
pub fn verify_hit_test_target(
    target_hwnd: HWND,
    screen_pt: [i32; 2],
    stage: ErrorStage,
    internal_mutation: InternalMutationDetail,
) -> Result<(), ProtocolError> {
    unsafe {
        let pt = POINT {
            x: screen_pt[0],
            y: screen_pt[1],
        };
        let hit = WindowFromPoint(pt);
        if hit.0.is_null() || !is_hwnd_related_to_target(hit, target_hwnd) {
            let hit_u64 = if hit.0.is_null() {
                None
            } else {
                Some(hwnd_to_u64(hit))
            };
            return Err(ProtocolError::commit(
                ErrorCode::WindowOccluded,
                stage,
                internal_mutation,
                DiagnosticReason::HitTestUnrelatedWindow,
            )
            .with_hwnds(Some(hwnd_to_u64(target_hwnd)), hit_u64));
        }
        Ok(())
    }
}

/// Injectable execution backend used by `execute_resolved_action_with_backend` so pure unit tests
/// can deterministically inject `SendInput` short counts, cancellation barriers, foreground loss,
/// hit-test occlusion, and live element bounds drift without touching OS windows or adding any
/// production safety bypass.
pub trait InputExecutionBackend {
    fn check_foreground(&mut self, target_hwnd: HWND) -> (bool, HWND);
    fn check_hit_test(
        &mut self,
        target_hwnd: HWND,
        screen_pt: [i32; 2],
        stage: ErrorStage,
        internal_mutation: InternalMutationDetail,
    ) -> Result<(), ProtocolError>;
    fn verify_live_element(
        &mut self,
        target_hwnd: HWND,
        binding: &SnapshotElementBinding,
        allow_password_target: bool,
        cancel_flag: &AtomicBool,
    ) -> Result<RectPx, ProtocolError>;
    fn send_input_batch(&mut self, inputs: &[INPUT]) -> usize;
    fn uia_focus(
        &mut self,
        target_hwnd: HWND,
        binding: &SnapshotElementBinding,
        allow_password_target: bool,
        cancel_flag: &AtomicBool,
    ) -> Result<(), ProtocolError>;
    fn uia_invoke(
        &mut self,
        target_hwnd: HWND,
        binding: &SnapshotElementBinding,
        cancel_flag: &AtomicBool,
    ) -> Result<(), ProtocolError>;
    fn uia_set_value(
        &mut self,
        target_hwnd: HWND,
        binding: &SnapshotElementBinding,
        value: &str,
        cancel_flag: &AtomicBool,
    ) -> Result<(), ProtocolError>;
    fn uia_select(
        &mut self,
        target_hwnd: HWND,
        binding: &SnapshotElementBinding,
        cancel_flag: &AtomicBool,
    ) -> Result<(), ProtocolError>;
    fn sleep_ms(&mut self, ms: u64);
}

pub struct OsWin32InputBackend;

impl InputExecutionBackend for OsWin32InputBackend {
    fn check_foreground(&mut self, target_hwnd: HWND) -> (bool, HWND) {
        verify_foreground_relation(target_hwnd)
    }

    fn check_hit_test(
        &mut self,
        target_hwnd: HWND,
        screen_pt: [i32; 2],
        stage: ErrorStage,
        internal_mutation: InternalMutationDetail,
    ) -> Result<(), ProtocolError> {
        verify_hit_test_target(target_hwnd, screen_pt, stage, internal_mutation)
    }

    fn verify_live_element(
        &mut self,
        target_hwnd: HWND,
        binding: &SnapshotElementBinding,
        allow_password_target: bool,
        cancel_flag: &AtomicBool,
    ) -> Result<RectPx, ProtocolError> {
        verify_live_element_for_commit_target(
            target_hwnd,
            &binding.runtime_id,
            &binding.snapshot_bounds,
            allow_password_target,
            cancel_flag,
        )
    }

    fn send_input_batch(&mut self, inputs: &[INPUT]) -> usize {
        if inputs.is_empty() {
            return 0;
        }
        unsafe { SendInput(inputs, std::mem::size_of::<INPUT>() as i32) as usize }
    }

    fn uia_focus(
        &mut self,
        target_hwnd: HWND,
        binding: &SnapshotElementBinding,
        allow_password_target: bool,
        cancel_flag: &AtomicBool,
    ) -> Result<(), ProtocolError> {
        execute_uia_focus(
            target_hwnd,
            &binding.runtime_id,
            Some(&binding.snapshot_bounds),
            allow_password_target,
            cancel_flag,
        )
    }

    fn uia_invoke(
        &mut self,
        target_hwnd: HWND,
        binding: &SnapshotElementBinding,
        cancel_flag: &AtomicBool,
    ) -> Result<(), ProtocolError> {
        execute_uia_invoke(
            target_hwnd,
            &binding.runtime_id,
            Some(&binding.snapshot_bounds),
            cancel_flag,
        )
    }

    fn uia_set_value(
        &mut self,
        target_hwnd: HWND,
        binding: &SnapshotElementBinding,
        value: &str,
        cancel_flag: &AtomicBool,
    ) -> Result<(), ProtocolError> {
        execute_uia_set_value(
            target_hwnd,
            &binding.runtime_id,
            Some(&binding.snapshot_bounds),
            value,
            cancel_flag,
        )
    }

    fn uia_select(
        &mut self,
        target_hwnd: HWND,
        binding: &SnapshotElementBinding,
        cancel_flag: &AtomicBool,
    ) -> Result<(), ProtocolError> {
        execute_uia_select(
            target_hwnd,
            &binding.runtime_id,
            Some(&binding.snapshot_bounds),
            cancel_flag,
        )
    }

    fn sleep_ms(&mut self, ms: u64) {
        std::thread::sleep(Duration::from_millis(ms));
    }
}

fn gate_cancel_and_foreground<B: InputExecutionBackend>(
    backend: &mut B,
    target_hwnd: HWND,
    cancel_flag: &AtomicBool,
    stage: ErrorStage,
    current_mutation: InternalMutationDetail,
) -> Result<(), ProtocolError> {
    if cancel_flag.load(Ordering::SeqCst) {
        let cancel_reason = if stage == ErrorStage::Validating
            && current_mutation == InternalMutationDetail::FocusOnly
        {
            DiagnosticReason::CancelledDuringExecution
        } else {
            DiagnosticReason::CancelledDuringExecution
        };
        return Err(ProtocolError::commit(
            ErrorCode::Cancelled,
            stage,
            current_mutation,
            cancel_reason,
        ));
    }

    let (fg_ok, fg_hwnd) = backend.check_foreground(target_hwnd);
    if !fg_ok {
        return Err(ProtocolError::commit(
            ErrorCode::FocusFailed,
            stage,
            current_mutation,
            DiagnosticReason::ForegroundVerificationFailed,
        )
        .with_hwnds(
            Some(hwnd_to_u64(target_hwnd)),
            if fg_hwnd.0.is_null() {
                None
            } else {
                Some(hwnd_to_u64(fg_hwnd))
            },
        ));
    }
    Ok(())
}

/// Executes a resolved action plan during `action.commit` after window identity, state token,
/// and foreground focus have been verified.
pub fn execute_resolved_action(
    target_hwnd: HWND,
    plan: &ResolvedActionPlan,
    cancel_flag: &AtomicBool,
) -> Result<(), ProtocolError> {
    let mut backend = OsWin32InputBackend;
    execute_resolved_action_with_backend(target_hwnd, plan, cancel_flag, &mut backend)
}

pub fn execute_resolved_action_with_backend<B: InputExecutionBackend>(
    target_hwnd: HWND,
    plan: &ResolvedActionPlan,
    cancel_flag: &AtomicBool,
    backend: &mut B,
) -> Result<(), ProtocolError> {
    gate_cancel_and_foreground(
        backend,
        target_hwnd,
        cancel_flag,
        ErrorStage::Validating,
        InternalMutationDetail::FocusOnly,
    )?;

    match &plan.op {
        ResolvedExecutionOp::FocusWindow => Ok(()),

        ResolvedExecutionOp::MouseClick {
            screen_pt,
            element_binding,
            button,
            double,
        } => {
            let mut actual_pt = *screen_pt;
            if let Some(ref binding) = element_binding {
                let live_bounds =
                    backend.verify_live_element(target_hwnd, binding, true, cancel_flag)?;
                actual_pt = [
                    live_bounds.left + live_bounds.width / 2,
                    live_bounds.top + live_bounds.height / 2,
                ];
            }

            gate_cancel_and_foreground(
                backend,
                target_hwnd,
                cancel_flag,
                ErrorStage::Validating,
                InternalMutationDetail::FocusOnly,
            )?;
            backend.check_hit_test(
                target_hwnd,
                actual_pt,
                ErrorStage::Validating,
                InternalMutationDetail::FocusOnly,
            )?;

            send_mouse_move(backend, actual_pt, InternalMutationDetail::FocusOnly)?;

            gate_cancel_and_foreground(
                backend,
                target_hwnd,
                cancel_flag,
                ErrorStage::InputStarted,
                InternalMutationDetail::FocusOnly,
            )?;
            send_mouse_button_click(backend, actual_pt, *button)?;

            if *double {
                backend.sleep_ms(35);
                // Re-verify cancel, foreground, live element (if element-bound), and hit-test
                // before dispatching the second click of a double-click.
                gate_cancel_and_foreground(
                    backend,
                    target_hwnd,
                    cancel_flag,
                    ErrorStage::InputStarted,
                    InternalMutationDetail::UnknownOrPartial,
                )?;
                if let Some(ref binding) = element_binding {
                    let live_bounds = backend
                        .verify_live_element(target_hwnd, binding, true, cancel_flag)
                        .map_err(|e| {
                            e.with_monotonic_mutation(InternalMutationDetail::UnknownOrPartial)
                        })?;
                    actual_pt = [
                        live_bounds.left + live_bounds.width / 2,
                        live_bounds.top + live_bounds.height / 2,
                    ];
                }
                backend.check_hit_test(
                    target_hwnd,
                    actual_pt,
                    ErrorStage::InputStarted,
                    InternalMutationDetail::UnknownOrPartial,
                )?;
                send_mouse_button_click(backend, actual_pt, *button)?;
            }
            Ok(())
        }

        ResolvedExecutionOp::MovePointer { screen_pt } => {
            backend.check_hit_test(
                target_hwnd,
                *screen_pt,
                ErrorStage::Validating,
                InternalMutationDetail::FocusOnly,
            )?;
            send_mouse_move(backend, *screen_pt, InternalMutationDetail::FocusOnly)
        }

        ResolvedExecutionOp::Scroll {
            screen_pt,
            delta_x,
            delta_y,
        } => {
            backend.check_hit_test(
                target_hwnd,
                *screen_pt,
                ErrorStage::Validating,
                InternalMutationDetail::FocusOnly,
            )?;
            send_mouse_move(backend, *screen_pt, InternalMutationDetail::FocusOnly)?;

            let mut scroll_started = false;
            if *delta_y != 0 {
                gate_cancel_and_foreground(
                    backend,
                    target_hwnd,
                    cancel_flag,
                    ErrorStage::InputStarted,
                    InternalMutationDetail::FocusOnly,
                )?;
                backend.check_hit_test(
                    target_hwnd,
                    *screen_pt,
                    ErrorStage::InputStarted,
                    InternalMutationDetail::FocusOnly,
                )?;
                send_mouse_wheel(backend, MOUSEEVENTF_WHEEL, *delta_y)?;
                scroll_started = true;
            }
            if *delta_x != 0 {
                let mut_detail = if scroll_started {
                    InternalMutationDetail::UnknownOrPartial
                } else {
                    InternalMutationDetail::FocusOnly
                };
                gate_cancel_and_foreground(
                    backend,
                    target_hwnd,
                    cancel_flag,
                    ErrorStage::InputStarted,
                    mut_detail,
                )?;
                backend.check_hit_test(
                    target_hwnd,
                    *screen_pt,
                    ErrorStage::InputStarted,
                    mut_detail,
                )?;
                send_mouse_wheel(backend, MOUSEEVENTF_HWHEEL, *delta_x)?;
            }
            Ok(())
        }

        ResolvedExecutionOp::TypeText {
            click_first_pt,
            element_binding,
            text,
        } => {
            let mut pre_type_mutation = InternalMutationDetail::FocusOnly;
            if let Some(ref binding) = element_binding {
                // Element-targeted type_text MUST succeed in live re-resolution and SetFocus;
                // never ignore SetFocus failure and never silently fall back to clicking an old center!
                backend.uia_focus(target_hwnd, binding, false, cancel_flag)?;
            } else if let Some(pt) = click_first_pt {
                backend.check_hit_test(
                    target_hwnd,
                    *pt,
                    ErrorStage::Validating,
                    InternalMutationDetail::FocusOnly,
                )?;
                send_mouse_move(backend, *pt, InternalMutationDetail::FocusOnly)?;
                gate_cancel_and_foreground(
                    backend,
                    target_hwnd,
                    cancel_flag,
                    ErrorStage::InputStarted,
                    InternalMutationDetail::FocusOnly,
                )?;
                send_mouse_button_click(backend, *pt, MouseButtonKind::Left)?;
                pre_type_mutation = InternalMutationDetail::UnknownOrPartial;
                backend.sleep_ms(20);
            }
            send_unicode_text(
                backend,
                target_hwnd,
                element_binding.as_ref(),
                text,
                cancel_flag,
                pre_type_mutation,
            )
        }

        ResolvedExecutionOp::KeyPress { vk, repeat } => {
            for idx in 0..*repeat {
                let mut_detail = if idx == 0 {
                    InternalMutationDetail::FocusOnly
                } else {
                    InternalMutationDetail::UnknownOrPartial
                };
                gate_cancel_and_foreground(
                    backend,
                    target_hwnd,
                    cancel_flag,
                    ErrorStage::InputStarted,
                    mut_detail,
                )?;
                send_vk_tap(backend, *vk)?;
            }
            Ok(())
        }

        ResolvedExecutionOp::KeyChord { vks } => {
            gate_cancel_and_foreground(
                backend,
                target_hwnd,
                cancel_flag,
                ErrorStage::InputStarted,
                InternalMutationDetail::FocusOnly,
            )?;
            send_vk_chord(backend, vks)
        }

        ResolvedExecutionOp::UiaInvoke { binding } => {
            backend.uia_invoke(target_hwnd, binding, cancel_flag)
        }

        ResolvedExecutionOp::UiaSetValue { binding, value } => {
            backend.uia_set_value(target_hwnd, binding, value, cancel_flag)
        }

        ResolvedExecutionOp::UiaSelect { binding } => {
            backend.uia_select(target_hwnd, binding, cancel_flag)
        }

        ResolvedExecutionOp::UiaFocus { binding } => {
            backend.uia_focus(target_hwnd, binding, true, cancel_flag)
        }
    }
}

fn screen_to_virtual_desktop_normalized(screen_pt: [i32; 2]) -> (i32, i32) {
    unsafe {
        let vx = GetSystemMetrics(SM_XVIRTUALSCREEN);
        let vy = GetSystemMetrics(SM_YVIRTUALSCREEN);
        let vw = GetSystemMetrics(SM_CXVIRTUALSCREEN).max(1);
        let vh = GetSystemMetrics(SM_CYVIRTUALSCREEN).max(1);

        let nx = (((screen_pt[0] - vx) as i64 * 65535) / ((vw - 1).max(1) as i64)).clamp(0, 65535)
            as i32;
        let ny = (((screen_pt[1] - vy) as i64 * 65535) / ((vh - 1).max(1) as i64)).clamp(0, 65535)
            as i32;
        (nx, ny)
    }
}

fn send_mouse_move<B: InputExecutionBackend>(
    backend: &mut B,
    screen_pt: [i32; 2],
    failure_mutation: InternalMutationDetail,
) -> Result<(), ProtocolError> {
    let (nx, ny) = screen_to_virtual_desktop_normalized(screen_pt);
    let input = INPUT {
        r#type: INPUT_MOUSE,
        Anonymous: INPUT_0 {
            mi: MOUSEINPUT {
                dx: nx,
                dy: ny,
                mouseData: 0,
                dwFlags: MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                time: 0,
                dwExtraInfo: 0,
            },
        },
    };
    let sent = backend.send_input_batch(&[input]);
    if sent != 1 {
        return Err(ProtocolError::commit(
            ErrorCode::InputFailed,
            ErrorStage::InputStarted,
            failure_mutation,
            DiagnosticReason::SendInputShortCount,
        ));
    }
    Ok(())
}

/// Dispatches a mouse button down+up pair (`[down, up]`).
/// If `SendInput` returns `1` (only the button-down event was accepted), dispatches a release-only
/// `[up]` compensation. If the release-only cleanup fails, returns `InputLockQuarantined` with
/// `UnknownOrPartial` so the worker quarantines the lock and reports honest unknown mutation.
/// Note: In-process compensation cannot protect against hard-kill (`TerminateProcess`) mid-batch.
fn send_mouse_button_click<B: InputExecutionBackend>(
    backend: &mut B,
    screen_pt: [i32; 2],
    button: MouseButtonKind,
) -> Result<(), ProtocolError> {
    let (nx, ny) = screen_to_virtual_desktop_normalized(screen_pt);
    let (down_flag, up_flag) = match button {
        MouseButtonKind::Left => (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        MouseButtonKind::Right => (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        MouseButtonKind::Middle => (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    };
    let base_flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK;
    let down = INPUT {
        r#type: INPUT_MOUSE,
        Anonymous: INPUT_0 {
            mi: MOUSEINPUT {
                dx: nx,
                dy: ny,
                mouseData: 0,
                dwFlags: base_flags | down_flag,
                time: 0,
                dwExtraInfo: 0,
            },
        },
    };
    let up = INPUT {
        r#type: INPUT_MOUSE,
        Anonymous: INPUT_0 {
            mi: MOUSEINPUT {
                dx: nx,
                dy: ny,
                mouseData: 0,
                dwFlags: base_flags | up_flag,
                time: 0,
                dwExtraInfo: 0,
            },
        },
    };
    let pair = [down, up];
    let sent = backend.send_input_batch(&pair);
    if sent == 2 {
        return Ok(());
    }
    if sent == 1 {
        let rel_sent = backend.send_input_batch(&[up]);
        if rel_sent != 1 {
            return Err(ProtocolError::commit(
                ErrorCode::InputLockQuarantined,
                ErrorStage::InputStarted,
                InternalMutationDetail::UnknownOrPartial,
                DiagnosticReason::SendInputShortCount,
            ));
        }
    }
    Err(ProtocolError::commit(
        ErrorCode::InputFailed,
        ErrorStage::InputStarted,
        InternalMutationDetail::UnknownOrPartial,
        DiagnosticReason::SendInputShortCount,
    ))
}

fn send_mouse_wheel<B: InputExecutionBackend>(
    backend: &mut B,
    flag: windows::Win32::UI::Input::KeyboardAndMouse::MOUSE_EVENT_FLAGS,
    delta: i32,
) -> Result<(), ProtocolError> {
    let input = INPUT {
        r#type: INPUT_MOUSE,
        Anonymous: INPUT_0 {
            mi: MOUSEINPUT {
                dx: 0,
                dy: 0,
                mouseData: delta as u32,
                dwFlags: flag,
                time: 0,
                dwExtraInfo: 0,
            },
        },
    };
    let sent = backend.send_input_batch(&[input]);
    if sent != 1 {
        return Err(ProtocolError::commit(
            ErrorCode::InputFailed,
            ErrorStage::InputStarted,
            InternalMutationDetail::UnknownOrPartial,
            DiagnosticReason::SendInputShortCount,
        ));
    }
    Ok(())
}

fn send_unicode_text<B: InputExecutionBackend>(
    backend: &mut B,
    target_hwnd: HWND,
    element_binding: Option<&SnapshotElementBinding>,
    text: &str,
    cancel_flag: &AtomicBool,
    initial_mutation: InternalMutationDetail,
) -> Result<(), ProtocolError> {
    let utf16: Vec<u16> = text.encode_utf16().collect();
    for (idx, code_unit) in utf16.into_iter().enumerate() {
        let mut_state = if idx == 0 {
            initial_mutation
        } else {
            InternalMutationDetail::UnknownOrPartial
        };

        gate_cancel_and_foreground(
            backend,
            target_hwnd,
            cancel_flag,
            ErrorStage::InputStarted,
            mut_state,
        )?;

        if let Some(binding) = element_binding {
            backend
                .verify_live_element(target_hwnd, binding, false, cancel_flag)
                .map_err(|e| e.with_monotonic_mutation(mut_state))?;
        }

        let down = INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: VIRTUAL_KEY(0),
                    wScan: code_unit,
                    dwFlags: KEYEVENTF_UNICODE,
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        };
        let up = INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: VIRTUAL_KEY(0),
                    wScan: code_unit,
                    dwFlags: KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        };
        let pair = [down, up];
        let sent = backend.send_input_batch(&pair);
        if sent == 2 {
            continue;
        }
        if sent == 1 {
            let rel_sent = backend.send_input_batch(&[up]);
            if rel_sent != 1 {
                return Err(ProtocolError::commit(
                    ErrorCode::InputLockQuarantined,
                    ErrorStage::InputStarted,
                    InternalMutationDetail::UnknownOrPartial,
                    DiagnosticReason::SendInputShortCount,
                ));
            }
        }
        return Err(ProtocolError::commit(
            ErrorCode::InputFailed,
            ErrorStage::InputStarted,
            InternalMutationDetail::UnknownOrPartial,
            DiagnosticReason::SendInputShortCount,
        ));
    }
    Ok(())
}

fn send_vk_tap<B: InputExecutionBackend>(
    backend: &mut B,
    vk: VIRTUAL_KEY,
) -> Result<(), ProtocolError> {
    let down = INPUT {
        r#type: INPUT_KEYBOARD,
        Anonymous: INPUT_0 {
            ki: KEYBDINPUT {
                wVk: vk,
                wScan: 0,
                dwFlags: Default::default(),
                time: 0,
                dwExtraInfo: 0,
            },
        },
    };
    let up = INPUT {
        r#type: INPUT_KEYBOARD,
        Anonymous: INPUT_0 {
            ki: KEYBDINPUT {
                wVk: vk,
                wScan: 0,
                dwFlags: KEYEVENTF_KEYUP,
                time: 0,
                dwExtraInfo: 0,
            },
        },
    };
    let pair = [down, up];
    let sent = backend.send_input_batch(&pair);
    if sent == 2 {
        return Ok(());
    }
    if sent == 1 {
        let rel_sent = backend.send_input_batch(&[up]);
        if rel_sent != 1 {
            return Err(ProtocolError::commit(
                ErrorCode::InputLockQuarantined,
                ErrorStage::InputStarted,
                InternalMutationDetail::UnknownOrPartial,
                DiagnosticReason::SendInputShortCount,
            ));
        }
    }
    Err(ProtocolError::commit(
        ErrorCode::InputFailed,
        ErrorStage::InputStarted,
        InternalMutationDetail::UnknownOrPartial,
        DiagnosticReason::SendInputShortCount,
    ))
}

/// Dispatches a key chord (`N` key-downs followed by `N` reverse-order key-ups).
/// If `SendInput` returns a short count `sent < 2 * N`:
/// - Never replays the business chord.
/// - Computes which keys from this operation are still held down (`sent < N` => `0..sent`;
///   `N <= sent < 2*N` => the remaining unreleased prefix `0..(2*N - sent)`), and dispatches
///   ONLY their corresponding `KEYEVENTF_KEYUP` events in reverse order.
/// - If the release-only cleanup fails, returns `InputLockQuarantined`.
fn send_vk_chord<B: InputExecutionBackend>(
    backend: &mut B,
    vks: &[VIRTUAL_KEY],
) -> Result<(), ProtocolError> {
    let n = vks.len();
    if n == 0 {
        return Ok(());
    }
    let mut seq = Vec::with_capacity(n * 2);
    for &vk in vks {
        seq.push(INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: vk,
                    wScan: 0,
                    dwFlags: Default::default(),
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        });
    }
    for &vk in vks.iter().rev() {
        seq.push(INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: vk,
                    wScan: 0,
                    dwFlags: KEYEVENTF_KEYUP,
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        });
    }

    let total = seq.len();
    let sent = backend.send_input_batch(&seq);
    if sent == total {
        return Ok(());
    }

    // Determine which key-downs from this operation remain un-released:
    // Down order: vks[0], vks[1], ..., vks[n-1]
    // Up order:   vks[n-1], vks[n-2], ..., vks[0]
    let held_count = if sent == 0 {
        0
    } else if sent <= n {
        sent
    } else if sent < total {
        total - sent
    } else {
        0
    };

    if held_count > 0 {
        let mut release_ups = Vec::with_capacity(held_count);
        for &vk in vks[..held_count].iter().rev() {
            release_ups.push(INPUT {
                r#type: INPUT_KEYBOARD,
                Anonymous: INPUT_0 {
                    ki: KEYBDINPUT {
                        wVk: vk,
                        wScan: 0,
                        dwFlags: KEYEVENTF_KEYUP,
                        time: 0,
                        dwExtraInfo: 0,
                    },
                },
            });
        }
        let rel_sent = backend.send_input_batch(&release_ups);
        if rel_sent != release_ups.len() {
            return Err(ProtocolError::commit(
                ErrorCode::InputLockQuarantined,
                ErrorStage::InputStarted,
                InternalMutationDetail::UnknownOrPartial,
                DiagnosticReason::SendInputShortCount,
            ));
        }
    }

    Err(ProtocolError::commit(
        ErrorCode::InputFailed,
        ErrorStage::InputStarted,
        InternalMutationDetail::UnknownOrPartial,
        DiagnosticReason::SendInputShortCount,
    ))
}

pub fn parse_virtual_key(key: &str) -> Result<VIRTUAL_KEY, ProtocolError> {
    let normalized = key.trim().to_lowercase();
    match normalized.as_str() {
        "enter" | "return" => Ok(VK_RETURN),
        "tab" => Ok(VK_TAB),
        "escape" | "esc" => Ok(VK_ESCAPE),
        "space" | "spacebar" => Ok(VK_SPACE),
        "backspace" | "back" => Ok(VK_BACK),
        "delete" | "del" => Ok(VK_DELETE),
        "insert" | "ins" => Ok(VK_INSERT),
        "home" => Ok(VK_HOME),
        "end" => Ok(VK_END),
        "pageup" | "page_up" | "pgup" => Ok(VK_PRIOR),
        "pagedown" | "page_down" | "pgdn" => Ok(VK_NEXT),
        "left" | "arrowleft" | "arrow_left" => Ok(VK_LEFT),
        "right" | "arrowright" | "arrow_right" => Ok(VK_RIGHT),
        "up" | "arrowup" | "arrow_up" => Ok(VK_UP),
        "down" | "arrowdown" | "arrow_down" => Ok(VK_DOWN),
        "shift" => Ok(VK_SHIFT),
        "ctrl" | "control" => Ok(VK_CONTROL),
        "alt" | "menu" => Ok(VK_MENU),
        "lalt" => Ok(VK_LMENU),
        "win" | "meta" | "super" | "lwin" => Ok(VK_LWIN),
        "f1" => Ok(VK_F1),
        "f2" => Ok(VK_F2),
        "f3" => Ok(VK_F3),
        "f4" => Ok(VK_F4),
        "f5" => Ok(VK_F5),
        "f6" => Ok(VK_F6),
        "f7" => Ok(VK_F7),
        "f8" => Ok(VK_F8),
        "f9" => Ok(VK_F9),
        "f10" => Ok(VK_F10),
        "f11" => Ok(VK_F11),
        "f12" => Ok(VK_F12),
        s if s.len() == 1 => {
            let b = s.as_bytes()[0];
            if b.is_ascii_lowercase() {
                Ok(VIRTUAL_KEY((b - b'a' + b'A') as u16))
            } else if b.is_ascii_digit() {
                Ok(VIRTUAL_KEY(b as u16))
            } else {
                Err(ProtocolError::validate(
                    ErrorCode::InvalidParams,
                    DiagnosticReason::MalformedJson,
                ))
            }
        }
        _ => Err(ProtocolError::validate(
            ErrorCode::InvalidParams,
            DiagnosticReason::MalformedJson,
        )),
    }
}
