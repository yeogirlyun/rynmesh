use crate::errors::{DiagnosticReason, ErrorCode, ProtocolError};
use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const WIRE_VERSION: u32 = 1;
pub const PROTOCOL_SEMVER: &str = "1.0.0";
pub const MAX_COMMAND_LINE_BYTES: usize = 64 * 1024;
pub const MAX_RESPONSE_FRAME_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_PNG_BYTES: usize = 8 * 1024 * 1024;
pub const MAX_ID_CHARS: usize = 128;
pub const MAX_METHOD_CHARS: usize = 64;
pub const MAX_WINDOWS_DEFAULT: usize = 64;
pub const MAX_WINDOWS_CAP: usize = 256;
pub const MAX_UIA_ELEMENTS_DEFAULT: usize = 300;
pub const MAX_UIA_ELEMENTS_CAP: usize = 1000;
pub const MAX_UIA_DEPTH_DEFAULT: u32 = 8;
pub const MAX_UIA_DEPTH_CAP: u32 = 16;
pub const MAX_TEXT_CHARS_DEFAULT: usize = 200;
pub const MAX_TEXT_CHARS_CAP: usize = 1000;

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(untagged)]
pub enum RequestId {
    String(String),
    Int(i64),
}

impl RequestId {
    pub fn as_correlation_string(&self) -> String {
        match self {
            Self::String(s) => s.clone(),
            Self::Int(n) => n.to_string(),
        }
    }

    pub fn validate(&self) -> Result<(), ProtocolError> {
        match self {
            Self::String(s) => {
                if s.trim().is_empty()
                    || s.len() > MAX_ID_CHARS
                    || s.chars().any(|c| c.is_control())
                {
                    return Err(ProtocolError::protocol(
                        ErrorCode::InvalidRequest,
                        DiagnosticReason::MissingRequestId,
                    ));
                }
                Ok(())
            }
            Self::Int(_) => Ok(()),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct RequestEnvelope {
    pub v: u32,
    pub id: RequestId,
    pub method: String,
    #[serde(default = "empty_json_object")]
    pub params: Value,
}

fn empty_json_object() -> Value {
    Value::Object(serde_json::Map::new())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ResponseEnvelope {
    pub v: u32,
    pub id: RequestId,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<ProtocolError>,
}

impl ResponseEnvelope {
    pub fn ok<T: Serialize>(id: RequestId, payload: T) -> Result<Self, serde_json::Error> {
        Ok(Self {
            v: WIRE_VERSION,
            id,
            result: Some(serde_json::to_value(payload)?),
            error: None,
        })
    }

    pub fn err(id: RequestId, error: ProtocolError) -> Self {
        let bound_err = if error.request_id.is_empty() {
            error.bind_context(id.as_correlation_string(), None)
        } else {
            error
        };
        Self {
            v: WIRE_VERSION,
            id,
            result: None,
            error: Some(bound_err),
        }
    }

    pub fn validate_invariant(&self) -> bool {
        self.v == WIRE_VERSION && (self.result.is_some() ^ self.error.is_some())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct EventEnvelope {
    pub v: u32,
    pub event: String,
    pub request_id: RequestId,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub operation_id: Option<String>,
    pub data: Value,
}

impl EventEnvelope {
    pub fn new<T: Serialize>(
        event: impl Into<String>,
        request_id: RequestId,
        operation_id: Option<String>,
        data: T,
    ) -> Result<Self, serde_json::Error> {
        Ok(Self {
            v: WIRE_VERSION,
            event: event.into(),
            request_id,
            operation_id,
            data: serde_json::to_value(data)?,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BoundedFrameRead {
    /// Clean EOF at frame boundary (zero bytes read before EOF).
    Eof,
    /// A newline-delimited UTF-8 command frame whose content length (excluding `\r\n` / `\n`)
    /// is at most `max_line_bytes`.
    Line(String),
    /// Frame exceeded `max_line_bytes` (stopped reading at `max_line_bytes + 1` content bytes
    /// without allocating the rest of the stream).
    Oversized,
    /// Non-empty bytes arrived before EOF with no terminating `\n`.
    UnterminatedEof,
    /// Frame bytes were within `max_line_bytes` but not valid UTF-8.
    InvalidUtf8,
}

/// Reads at most one JSONL frame from `reader`, enforcing `max_line_bytes` at the stream read boundary
/// (`cap + 1` content bytes max) so an oversized or unterminated input can never allocate unbounded memory
/// or block indefinitely after exceeding `max_line_bytes`.
pub fn read_bounded_jsonl_frame<R: std::io::BufRead>(
    reader: &mut R,
    max_line_bytes: usize,
    scratch: &mut Vec<u8>,
) -> std::io::Result<BoundedFrameRead> {
    scratch.clear();
    if scratch.capacity() > max_line_bytes.saturating_add(2) {
        scratch.shrink_to(max_line_bytes.saturating_add(2));
    }

    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            if scratch.is_empty() {
                return Ok(BoundedFrameRead::Eof);
            } else {
                scratch.clear();
                return Ok(BoundedFrameRead::UnterminatedEof);
            }
        }

        if let Some(pos) = available.iter().position(|&b| b == b'\n') {
            let chunk = &available[..pos];
            let raw_total = scratch.len().saturating_add(chunk.len());
            let ends_with_cr = if !chunk.is_empty() {
                chunk[chunk.len() - 1] == b'\r'
            } else {
                scratch.last() == Some(&b'\r')
            };
            let content_len = if ends_with_cr {
                raw_total.saturating_sub(1)
            } else {
                raw_total
            };

            if content_len > max_line_bytes {
                let consume_cap = (max_line_bytes.saturating_add(1))
                    .saturating_sub(scratch.len())
                    .max(1)
                    .min(pos + 1);
                reader.consume(consume_cap);
                scratch.clear();
                return Ok(BoundedFrameRead::Oversized);
            }

            scratch.extend_from_slice(chunk);
            reader.consume(pos + 1);
            if scratch.last() == Some(&b'\r') {
                scratch.pop();
            }

            let res = match std::str::from_utf8(scratch) {
                Ok(s) => BoundedFrameRead::Line(s.to_string()),
                Err(_) => BoundedFrameRead::InvalidUtf8,
            };
            scratch.clear();
            return Ok(res);
        }

        // No newline in `available`: copy at most up to `max_line_bytes + 2` total bytes into `scratch`
        let max_scratch = max_line_bytes.saturating_add(2);
        let can_take = max_scratch
            .saturating_sub(scratch.len())
            .min(available.len());
        scratch.extend_from_slice(&available[..can_take]);
        reader.consume(can_take);

        if scratch.len() == max_line_bytes.saturating_add(1) && scratch[max_line_bytes] != b'\r' {
            scratch.clear();
            return Ok(BoundedFrameRead::Oversized);
        }
        if scratch.len() >= max_scratch {
            scratch.clear();
            return Ok(BoundedFrameRead::Oversized);
        }
    }
}

/// Strictly parses a raw JSONL command line, verifying:
/// 1. Byte length <= 64 KiB
/// 2. No duplicate keys in any JSON object across the entire payload
/// 3. `v` field present and equal to integer `1`
/// 4. Valid bounded `id` and `method`
/// 5. No unknown top-level envelope fields
pub fn parse_and_validate_request_line(
    line: &str,
) -> Result<RequestEnvelope, (Option<RequestId>, ProtocolError)> {
    if line.len() > MAX_COMMAND_LINE_BYTES {
        return Err((
            None,
            ProtocolError::protocol(ErrorCode::InvalidRequest, DiagnosticReason::FrameTooLarge),
        ));
    }

    if let Err(err) = reject_duplicate_json_keys(line) {
        return Err((None, err));
    }

    let raw_val: Value = serde_json::from_str(line).map_err(|_| {
        (
            None,
            ProtocolError::protocol(ErrorCode::InvalidRequest, DiagnosticReason::MalformedJson),
        )
    })?;

    let obj = raw_val.as_object().ok_or_else(|| {
        (
            None,
            ProtocolError::protocol(ErrorCode::InvalidRequest, DiagnosticReason::MalformedJson),
        )
    })?;

    let extracted_id = obj
        .get("id")
        .and_then(|v| serde_json::from_value::<RequestId>(v.clone()).ok());

    match obj.get("v") {
        None => {
            return Err((
                extracted_id,
                ProtocolError::protocol(
                    ErrorCode::InvalidRequest,
                    DiagnosticReason::MissingVersion,
                ),
            ))
        }
        Some(Value::Number(n)) if n.as_u64() == Some(WIRE_VERSION as u64) => {}
        Some(_) => {
            return Err((
                extracted_id,
                ProtocolError::protocol(
                    ErrorCode::UnsupportedProtocolVersion,
                    DiagnosticReason::VersionMismatch,
                ),
            ))
        }
    }

    let env: RequestEnvelope = serde_json::from_value(raw_val).map_err(|_| {
        (
            extracted_id.clone(),
            ProtocolError::protocol(ErrorCode::InvalidRequest, DiagnosticReason::MalformedJson),
        )
    })?;

    if let Err(e) = env.id.validate() {
        return Err((Some(env.id), e));
    }

    if env.method.trim().is_empty()
        || env.method.len() > MAX_METHOD_CHARS
        || env.method.chars().any(|c| c.is_control() || c.is_whitespace())
    {
        return Err((
            Some(env.id),
            ProtocolError::protocol(ErrorCode::InvalidRequest, DiagnosticReason::MissingMethod),
        ));
    }

    if !env.params.is_object() {
        return Err((
            Some(env.id),
            ProtocolError::validate(ErrorCode::InvalidParams, DiagnosticReason::MalformedJson),
        ));
    }

    Ok(env)
}

/// Scans a raw JSON string and rejects any JSON object that contains duplicate keys at the same nesting level.
pub fn reject_duplicate_json_keys(input: &str) -> Result<(), ProtocolError> {
    use std::collections::HashSet;

    #[derive(Debug)]
    enum Scope {
        Object {
            keys: HashSet<String>,
            expecting_key: bool,
            after_colon: bool,
        },
        Array,
    }

    let bytes = input.as_bytes();
    let mut i = 0usize;
    let mut stack: Vec<Scope> = Vec::new();

    while i < bytes.len() {
        let b = bytes[i];
        match b {
            b' ' | b'\t' | b'\r' | b'\n' => {
                i += 1;
            }
            b'{' => {
                if let Some(Scope::Object {
                    expecting_key,
                    after_colon,
                    ..
                }) = stack.last_mut()
                {
                    if *expecting_key && !*after_colon {
                        return Err(ProtocolError::protocol(
                            ErrorCode::InvalidRequest,
                            DiagnosticReason::MalformedJson,
                        ));
                    }
                    *after_colon = false;
                }
                stack.push(Scope::Object {
                    keys: HashSet::new(),
                    expecting_key: true,
                    after_colon: false,
                });
                i += 1;
            }
            b'}' => {
                match stack.pop() {
                    Some(Scope::Object { .. }) => {}
                    _ => {
                        return Err(ProtocolError::protocol(
                            ErrorCode::InvalidRequest,
                            DiagnosticReason::MalformedJson,
                        ))
                    }
                }
                i += 1;
            }
            b'[' => {
                if let Some(Scope::Object { after_colon, .. }) = stack.last_mut() {
                    *after_colon = false;
                }
                stack.push(Scope::Array);
                i += 1;
            }
            b']' => {
                match stack.pop() {
                    Some(Scope::Array) => {}
                    _ => {
                        return Err(ProtocolError::protocol(
                            ErrorCode::InvalidRequest,
                            DiagnosticReason::MalformedJson,
                        ))
                    }
                }
                i += 1;
            }
            b':' => {
                if let Some(Scope::Object {
                    expecting_key,
                    after_colon,
                    ..
                }) = stack.last_mut()
                {
                    *expecting_key = false;
                    *after_colon = true;
                }
                i += 1;
            }
            b',' => {
                if let Some(Scope::Object {
                    expecting_key,
                    after_colon,
                    ..
                }) = stack.last_mut()
                {
                    *expecting_key = true;
                    *after_colon = false;
                }
                i += 1;
            }
            b'"' => {
                let (parsed_str, next_idx) = parse_json_string_literal(input, i)?;
                i = next_idx;
                if let Some(Scope::Object {
                    keys,
                    expecting_key,
                    after_colon,
                }) = stack.last_mut()
                {
                    if *expecting_key && !*after_colon {
                        if !keys.insert(parsed_str) {
                            return Err(ProtocolError::protocol(
                                ErrorCode::InvalidRequest,
                                DiagnosticReason::DuplicateJsonKey,
                            ));
                        }
                    } else if *after_colon {
                        *after_colon = false;
                    }
                }
            }
            _ => {
                if let Some(Scope::Object { after_colon, .. }) = stack.last_mut() {
                    *after_colon = false;
                }
                i += 1;
            }
        }
    }

    Ok(())
}

fn parse_json_string_literal(input: &str, start_quote: usize) -> Result<(String, usize), ProtocolError> {
    let rest = &input[start_quote..];
    let mut de = serde_json::Deserializer::from_str(rest).into_iter::<String>();
    match de.next() {
        Some(Ok(s)) => {
            let offset = de.byte_offset();
            Ok((s, start_quote + offset))
        }
        _ => Err(ProtocolError::protocol(
            ErrorCode::InvalidRequest,
            DiagnosticReason::MalformedJson,
        )),
    }
}

// ============================================================================
// Initialize
// ============================================================================

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ClientInfo {
    pub name: String,
    pub version: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct InitializeCapabilitiesRequest {
    #[serde(default)]
    pub high_dpi: Option<bool>,
    #[serde(default)]
    pub uia: Option<bool>,
    #[serde(default)]
    pub window_capture: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct InitializeParams {
    pub protocol_version: String,
    pub client: ClientInfo,
    #[serde(default)]
    pub capabilities: InitializeCapabilitiesRequest,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct WorkerInfo {
    pub name: String,
    pub version: String,
    pub pid: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ServerCapabilities {
    pub dpi_awareness: String,
    pub uia: bool,
    pub window_capture: Vec<String>,
    pub window_capture_limitations: Vec<String>,
    pub input_lock: String,
    pub actions: Vec<String>,
    pub unsupported_actions: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ServerLimits {
    pub max_command_line_bytes: usize,
    pub max_response_frame_bytes: usize,
    pub max_png_bytes: usize,
    pub max_windows: usize,
    pub max_uia_elements: usize,
    pub max_uia_depth: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct InitializeResult {
    pub protocol_version: String,
    pub worker_instance: String,
    pub worker: WorkerInfo,
    pub capabilities: ServerCapabilities,
    pub limits: ServerLimits,
}

// ============================================================================
// Window Identity & Discovery
// ============================================================================

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct WindowRef {
    pub worker_instance: String,
    pub window_key: String,
    pub hwnd: String,
    pub pid: u32,
    pub process_started: String,
    pub generation: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub app_id: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct RectPx {
    pub left: i32,
    pub top: i32,
    pub right: i32,
    pub bottom: i32,
    pub width: i32,
    pub height: i32,
}

impl RectPx {
    pub fn from_ltrb(left: i32, top: i32, right: i32, bottom: i32) -> Self {
        Self {
            left,
            top,
            right,
            bottom,
            width: (right - left).max(0),
            height: (bottom - top).max(0),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct WindowGeometry {
    pub window_rect: RectPx,
    pub client_rect: RectPx,
    pub screen_origin: [i32; 2],
    pub dpi: u32,
    pub scale_factor: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct WindowEntry {
    pub window: WindowRef,
    pub title: String,
    pub class_name: String,
    pub process_name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub executable_path: Option<String>,
    pub visible: bool,
    pub enabled: bool,
    pub minimized: bool,
    pub maximized: bool,
    pub foreground: bool,
    pub cloaked: bool,
    pub capture_protected: bool,
    pub bounds: RectPx,
    pub client_bounds: RectPx,
    pub dpi: u32,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct WindowsListParams {
    #[serde(default)]
    pub task_session_id: Option<String>,
    #[serde(default)]
    pub include_minimized: Option<bool>,
    #[serde(default)]
    pub include_invisible: Option<bool>,
    #[serde(default)]
    pub include_cloaked: Option<bool>,
    #[serde(default)]
    pub app_query: Option<String>,
    #[serde(default)]
    pub title_query: Option<String>,
    #[serde(default)]
    pub pid: Option<u32>,
    #[serde(default)]
    pub max_windows: Option<usize>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct WindowsListResult {
    pub captured_at: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub foreground_window: Option<WindowRef>,
    pub windows: Vec<WindowEntry>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct WindowsGetParams {
    #[serde(default)]
    pub task_session_id: Option<String>,
    #[serde(default)]
    pub window: Option<WindowRef>,
    #[serde(default)]
    pub hwnd: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct WindowsGetResult {
    pub captured_at: String,
    pub entry: WindowEntry,
}

// ============================================================================
// Apps
// ============================================================================

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AppsListParams {
    #[serde(default)]
    pub query: Option<String>,
    #[serde(default)]
    pub max_apps: Option<usize>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AppEntry {
    pub app_id: String,
    pub name: String,
    pub source: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub executable_path: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub aumid: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AppsListResult {
    pub apps: Vec<AppEntry>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum AppLaunchTarget {
    #[serde(rename_all = "camelCase")]
    AppId { app_id: String },
    #[serde(rename_all = "camelCase")]
    Aumid { aumid: String },
    #[serde(rename_all = "camelCase")]
    Executable {
        executable_path: String,
        #[serde(default)]
        args: Vec<String>,
        #[serde(default)]
        working_directory: Option<String>,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AppsLaunchParams {
    pub task_session_id: String,
    pub operation_id: String,
    pub target: AppLaunchTarget,
    #[serde(default)]
    pub wait_for_window_ms: Option<u64>,
}

// ============================================================================
// State Observation (UIA + Capture)
// ============================================================================

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ScreenshotOptions {
    #[serde(default)]
    pub enabled: Option<bool>,
    #[serde(default)]
    pub mode: Option<String>,
    #[serde(default)]
    pub format: Option<String>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AccessibilityOptions {
    #[serde(default)]
    pub enabled: Option<bool>,
    #[serde(default)]
    pub max_depth: Option<u32>,
    #[serde(default)]
    pub max_elements: Option<usize>,
    #[serde(default)]
    pub include_offscreen: Option<bool>,
    #[serde(default)]
    pub max_text_chars: Option<usize>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct StateGetParams {
    pub task_session_id: String,
    pub window: WindowRef,
    #[serde(default)]
    pub include_screenshot: Option<bool>,
    #[serde(default)]
    pub include_text: Option<bool>,
    #[serde(default)]
    pub screenshot: Option<ScreenshotOptions>,
    #[serde(default)]
    pub accessibility: Option<AccessibilityOptions>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ScreenshotArtifact {
    pub id: String,
    pub status: String,
    pub capture_method: String,
    pub mime_type: String,
    pub width: u32,
    pub height: u32,
    pub origin_x: i32,
    pub origin_y: i32,
    pub z_index: u32,
    pub dpi: u32,
    pub scale_factor: f64,
    pub sha256: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data_base64: Option<String>,
    pub minimized: bool,
    pub occluded: bool,
    pub capture_protected: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct UiaElement {
    pub index: u32,
    pub runtime_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub automation_id: Option<String>,
    pub name: String,
    pub control_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub class_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub value: Option<String>,
    pub password: bool,
    pub enabled: bool,
    pub offscreen: bool,
    pub focusable: bool,
    pub focused: bool,
    pub bounds: RectPx,
    pub depth: u32,
    pub patterns: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AccessibilityObservation {
    pub status: String,
    pub tree_hash: String,
    pub truncated: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub focused_element_index: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub focused_runtime_id: Option<String>,
    pub selected_element_indices: Vec<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub selected_text: Option<String>,
    pub elements: Vec<UiaElement>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ForegroundStatus {
    pub is_target_window: bool,
    pub is_related_to_target: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub foreground_hwnd: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub foreground_pid: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Observation {
    pub task_session_id: String,
    pub window: WindowRef,
    pub observation_id: String,
    pub state_token: String,
    pub captured_at: String,
    pub geometry: WindowGeometry,
    pub screenshots: Vec<ScreenshotArtifact>,
    pub accessibility: AccessibilityObservation,
    pub foreground: ForegroundStatus,
}

// ============================================================================
// Actions (Prepare / Commit / Cancel)
// ============================================================================

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct PointTarget {
    pub coordinate_space: String,
    pub x: i32,
    pub y: i32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ElementTarget {
    pub index: u32,
    #[serde(default)]
    pub runtime_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum ActionSpec {
    #[serde(rename_all = "camelCase")]
    FocusWindow {
        #[serde(default)]
        restore_if_minimized: Option<bool>,
    },
    #[serde(rename_all = "camelCase")]
    Click {
        #[serde(default)]
        button: Option<String>,
        #[serde(default)]
        target: Option<PointTarget>,
        #[serde(default)]
        element: Option<ElementTarget>,
    },
    #[serde(rename_all = "camelCase")]
    DoubleClick {
        #[serde(default)]
        button: Option<String>,
        #[serde(default)]
        target: Option<PointTarget>,
        #[serde(default)]
        element: Option<ElementTarget>,
    },
    #[serde(rename_all = "camelCase")]
    ContextClick {
        #[serde(default)]
        target: Option<PointTarget>,
        #[serde(default)]
        element: Option<ElementTarget>,
    },
    #[serde(rename_all = "camelCase")]
    MovePointer { target: PointTarget },
    #[serde(rename_all = "camelCase")]
    Drag {
        from: PointTarget,
        to: PointTarget,
        #[serde(default)]
        button: Option<String>,
        #[serde(default)]
        steps: Option<u32>,
    },
    #[serde(rename_all = "camelCase")]
    Scroll {
        target: PointTarget,
        delta_x: i32,
        delta_y: i32,
    },
    #[serde(rename_all = "camelCase")]
    TypeText {
        text: String,
        #[serde(default)]
        target: Option<PointTarget>,
        #[serde(default)]
        element: Option<ElementTarget>,
    },
    #[serde(rename_all = "camelCase")]
    KeyPress {
        key: String,
        #[serde(default)]
        repeat: Option<u32>,
    },
    #[serde(rename_all = "camelCase")]
    KeyChord { keys: Vec<String> },
    #[serde(rename_all = "camelCase")]
    UiaInvoke { element: ElementTarget },
    #[serde(rename_all = "camelCase")]
    UiaSetValue {
        element: ElementTarget,
        value: String,
    },
    #[serde(rename_all = "camelCase")]
    UiaSelect { element: ElementTarget },
    #[serde(rename_all = "camelCase")]
    UiaFocus { element: ElementTarget },
    #[serde(rename_all = "camelCase")]
    LaunchApp { target: AppLaunchTarget },
}

impl ActionSpec {
    pub const fn kind_name(&self) -> &'static str {
        match self {
            Self::FocusWindow { .. } => "focus_window",
            Self::Click { .. } => "click",
            Self::DoubleClick { .. } => "double_click",
            Self::ContextClick { .. } => "context_click",
            Self::MovePointer { .. } => "move_pointer",
            Self::Drag { .. } => "drag",
            Self::Scroll { .. } => "scroll",
            Self::TypeText { .. } => "type_text",
            Self::KeyPress { .. } => "key_press",
            Self::KeyChord { .. } => "key_chord",
            Self::UiaInvoke { .. } => "uia_invoke",
            Self::UiaSetValue { .. } => "uia_set_value",
            Self::UiaSelect { .. } => "uia_select",
            Self::UiaFocus { .. } => "uia_focus",
            Self::LaunchApp { .. } => "launch_app",
        }
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ActionPrepareConstraints {
    #[serde(default)]
    pub ready_timeout_ms: Option<u64>,
    #[serde(default)]
    pub lock_wait_timeout_ms: Option<u64>,
    #[serde(default)]
    pub focus_deadline_ms: Option<u64>,
    #[serde(default)]
    pub restore_if_minimized: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ActionPrepareParams {
    pub task_session_id: String,
    pub operation_id: String,
    pub window: WindowRef,
    pub expected_state_token: String,
    pub action: ActionSpec,
    #[serde(default)]
    pub timeout_ms: Option<u64>,
    #[serde(default)]
    pub authority_epoch: Option<u64>,
    #[serde(default)]
    pub constraints: Option<ActionPrepareConstraints>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ResolvedTargetSummary {
    pub window_hwnd: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub screen_point: Option<[i32; 2]>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub secondary_screen_point: Option<[i32; 2]>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub element_index: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub element_runtime_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ActionReadyEventData {
    pub task_session_id: String,
    pub operation_id: String,
    pub prepare_ticket: String,
    pub window: WindowRef,
    pub expected_state_token: String,
    pub action_kind: String,
    pub action_fingerprint: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub authority_epoch: Option<u64>,
    pub input_lock_held: bool,
    pub lock_was_abandoned: bool,
    pub expires_in_ms: u64,
    pub mutation: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ActionPrepareResult {
    pub status: String,
    pub task_session_id: String,
    pub operation_id: String,
    pub prepare_ticket: String,
    pub window: WindowRef,
    pub expected_state_token: String,
    pub action_kind: String,
    pub action_fingerprint: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub authority_epoch: Option<u64>,
    pub expires_in_ms: u64,
    pub input_lock_held: bool,
    pub lock_was_abandoned: bool,
    pub mutation: String,
    pub resolved_target: ResolvedTargetSummary,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct PostObservationOptions {
    #[serde(default)]
    pub enabled: Option<bool>,
    #[serde(default)]
    pub settle_delay_ms: Option<u64>,
    #[serde(default)]
    pub screenshot: Option<ScreenshotOptions>,
    #[serde(default)]
    pub accessibility: Option<AccessibilityOptions>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ActionCommitParams {
    pub task_session_id: String,
    pub operation_id: String,
    pub prepare_ticket: String,
    #[serde(default)]
    pub post_observation: Option<PostObservationOptions>,
    #[serde(default)]
    pub deadline_ms: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct FocusRecoverySummary {
    pub attempted: bool,
    pub succeeded: bool,
    pub method_used: String,
    pub duration_ms: u64,
    pub final_foreground_verified: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ActionCommitResult {
    pub status: String,
    pub task_session_id: String,
    pub operation_id: String,
    pub action_fingerprint: String,
    pub mutation: String,
    pub focus_recovery: FocusRecoverySummary,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub observation: Option<Observation>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ActionCancelParams {
    pub task_session_id: String,
    pub operation_id: String,
    #[serde(default)]
    pub prepare_ticket: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ActionCancelResult {
    pub cancelled: bool,
    pub operation_id: String,
    pub stage: String,
    pub mutation: String,
    pub input_lock_released: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SessionEndParams {
    pub task_session_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SessionEndResult {
    pub ended: bool,
    pub cancelled_operations: usize,
    pub invalidated_state_tokens: usize,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ShutdownParams {}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ShutdownResult {
    pub shutting_down: bool,
}
