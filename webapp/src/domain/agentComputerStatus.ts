import { nodeControlUrl } from "./nodeUrl";

export const COMPUTER_STATUS_SCHEMA_VERSION = 1 as const;

export const COMPUTER_STATUS_EVIDENCE_SOURCES = [
  "none",
  "extension_store",
  "agent_store",
  "native_probe_cache",
  "live_session",
  "model_route_contract",
  "model_image_probe",
  "runtime_receipt",
  "client_render_receipt",
] as const;

export type ComputerStatusEvidenceSource = (typeof COMPUTER_STATUS_EVIDENCE_SOURCES)[number];

export const COMPUTER_STATUS_REASON_CODES = [
  "plugin_missing",
  "plugin_disabled",
  "agent_disabled",
  "native_not_verified",
  "native_missing",
  "native_platform_unsupported",
  "native_protocol_incompatible",
  "native_evidence_stale",
  "native_failed",
  "global_desktop_access_disabled",
  "task_local_execution_disabled",
  "task_desktop_access_disabled",
  "session_not_started",
  "session_lost",
  "model_unavailable",
  "image_route_unsupported",
  "image_capability_unknown",
  "image_verification_pending",
  "image_verification_failed",
  "image_evidence_stale",
  "status_unavailable",
] as const;

export type ComputerStatusReasonCode = (typeof COMPUTER_STATUS_REASON_CODES)[number];

export type ComputerStatusSelectionSource = "settings" | "task";
export type ComputerStatusNativePlatform = "windows" | "unsupported" | "unknown";
export type ComputerStatusExecutablePresence = "present" | "missing" | "unknown";
export type ComputerStatusNativeState =
  | "unknown"
  | "missing"
  | "unverified"
  | "ready"
  | "incompatible"
  | "error";

export type ComputerStatusTaskPermissionScope = "not_requested" | "present";
export type ComputerStatusPermissionMode =
  | "workspace"
  | "read_only"
  | "confirm_changes"
  | "automatic"
  | "unknown";

export type ComputerStatusSessionState =
  | "not_applicable"
  | "none"
  | "starting"
  | "connected"
  | "waiting_approval"
  | "busy"
  | "cancelled"
  | "lost"
  | "unknown";

export type ComputerStatusModelAdapter =
  | "codex_cli"
  | "antigravity_cli"
  | "openai_chat"
  | "openai_responses"
  | "anthropic_messages"
  | "other"
  | "unknown";

export type ComputerStatusModelAvailability = "available" | "unavailable" | "unknown";
export type ComputerStatusImageInputState =
  | "unsupported"
  | "carrier_available"
  | "verified"
  | "unknown";
export type ComputerStatusImageCarrier =
  | "none"
  | "codex_turn_input"
  | "api_image_blocks"
  | "unknown";

export type ComputerStatusObservationStageState =
  | "recorded"
  | "not_recorded"
  | "failed"
  | "unknown";

export interface ComputerStatusEvidence {
  source: ComputerStatusEvidenceSource;
  observed_at: string | null;
}

export interface ComputerStatusContext {
  task_id: string | null;
  task_revision: number | null;
  settings_revision: number | null;
  selection_source: ComputerStatusSelectionSource;
  model_id: string | null;
  selection_epoch: string;
}

export interface ComputerStatusPlugin {
  installed: boolean | null;
  enabled: boolean | null;
  revision: number | null;
  evidence: ComputerStatusEvidence;
  reason_code: ComputerStatusReasonCode | null;
}

export interface ComputerStatusNative {
  platform: ComputerStatusNativePlatform;
  executable_presence: ComputerStatusExecutablePresence;
  status: ComputerStatusNativeState;
  version: string | null;
  protocol_version: string | null;
  evidence: ComputerStatusEvidence;
  reason_code: ComputerStatusReasonCode | null;
}

export interface ComputerStatusPermissions {
  agent_enabled: boolean | null;
  global_desktop_access: boolean | null;
  task_scope: ComputerStatusTaskPermissionScope;
  task_local_execution: boolean | null;
  task_desktop_access: boolean | null;
  global_permission_mode: ComputerStatusPermissionMode | null;
  task_permission_mode: ComputerStatusPermissionMode | null;
  evidence: ComputerStatusEvidence;
  reason_code: ComputerStatusReasonCode | null;
}

export interface ComputerStatusSession {
  state: ComputerStatusSessionState;
  generation: number | null;
  evidence: ComputerStatusEvidence;
  reason_code: ComputerStatusReasonCode | null;
}

export interface ComputerStatusModelImageInput {
  status: ComputerStatusImageInputState;
  carrier: ComputerStatusImageCarrier;
  evidence: ComputerStatusEvidence;
  reason_code: ComputerStatusReasonCode | null;
}

export interface ComputerStatusModel {
  id: string | null;
  display_name: string | null;
  adapter: ComputerStatusModelAdapter | null;
  availability: ComputerStatusModelAvailability;
  image_input: ComputerStatusModelImageInput;
}

export interface ComputerStatusObservationStage {
  state: ComputerStatusObservationStageState;
  evidence: ComputerStatusEvidence;
}

export interface ComputerStatusObservationStages {
  saved: ComputerStatusObservationStage;
  displayed: ComputerStatusObservationStage;
  submitted: ComputerStatusObservationStage;
  semantic_observed: ComputerStatusObservationStage;
}

export interface ComputerStatusObservation {
  id: string;
  task_revision: number | null;
  selection_epoch: string;
  model_id: string | null;
  matches_current_selection: boolean;
  width: number | null;
  height: number | null;
  stages: ComputerStatusObservationStages;
}

export interface ComputerStatusDtoV1 {
  schema_version: 1;
  snapshot_id: string;
  generated_at: string;
  context: ComputerStatusContext;
  plugin: ComputerStatusPlugin;
  native: ComputerStatusNative;
  permissions: ComputerStatusPermissions;
  session: ComputerStatusSession;
  model: ComputerStatusModel;
  latest_observation: ComputerStatusObservation | null;
}

export type AgentComputerStatusErrorCode =
  | "endpoint_not_found"
  | "agent_task_not_found"
  | "agent_task_deleted"
  | "agent_invalid_task_id"
  | "agent_invalid_request"
  | "unauthorized"
  | "agent_computer_status_unavailable"
  | "invalid_response"
  | "network_error"
  | "status_unavailable";

export class AgentComputerStatusError extends Error {
  readonly code: AgentComputerStatusErrorCode;
  readonly status: number;

  constructor(code: AgentComputerStatusErrorCode, status = 0) {
    super(code);
    this.name = "AgentComputerStatusError";
    this.code = code;
    this.status = status;
  }
}

const EVIDENCE_SOURCE_SET = new Set<string>(COMPUTER_STATUS_EVIDENCE_SOURCES);
const REASON_CODE_SET = new Set<string>(COMPUTER_STATUS_REASON_CODES);
const PERMISSION_MODE_SET = new Set<string>([
  "workspace",
  "read_only",
  "confirm_changes",
  "automatic",
  "unknown",
]);
const SESSION_STATE_SET = new Set<string>([
  "not_applicable",
  "none",
  "starting",
  "connected",
  "waiting_approval",
  "busy",
  "cancelled",
  "lost",
  "unknown",
]);
const MODEL_ADAPTER_SET = new Set<string>([
  "codex_cli",
  "antigravity_cli",
  "openai_chat",
  "openai_responses",
  "anthropic_messages",
  "other",
  "unknown",
]);
const MODEL_AVAILABILITY_SET = new Set<string>(["available", "unavailable", "unknown"]);
const IMAGE_INPUT_STATUS_SET = new Set<string>([
  "unsupported",
  "carrier_available",
  "verified",
  "unknown",
]);
const IMAGE_CARRIER_SET = new Set<string>([
  "none",
  "codex_turn_input",
  "api_image_blocks",
  "unknown",
]);
const STAGE_STATE_SET = new Set<string>(["recorded", "not_recorded", "failed", "unknown"]);

const MAX_ID_LENGTH = 160;
const MAX_MODEL_ID_LENGTH = 1024;
const MAX_DISPLAY_NAME_LENGTH = 160;
const MAX_VERSION_LENGTH = 64;

const CONTROL_CHAR_RE = /[\u0000-\u001f\u007f]/;
const SAFE_VERSION_RE = /^[A-Za-z0-9._+-]{1,64}$/;

/**
 * Detects strings that look like developer file paths, CLI invocations, URLs,
 * stack traces, or credential/secret material so they are never accepted or rendered.
 */
export function looksLikeUnsafeDiagnosticOrSecret(value: string): boolean {
  if (!value || CONTROL_CHAR_RE.test(value)) {
    return true;
  }
  // Windows drive path or UNC path
  if (/[A-Za-z]:[\\/]/.test(value) || /^\\\\/.test(value) || value.includes("\\")) {
    return true;
  }
  // Common Unix/system paths, dotfile config dirs, or executable/config file extensions
  if (
    /(?:^|\s)\/(?:Users|home|root|etc|var|usr|bin|opt|tmp|private|Windows|Program Files)\b/i.test(
      value,
    ) ||
    /(?:^|[\\/])\.(?:codex|env|ssh|aws|config|gemini)\b/i.test(value) ||
    /\.(?:exe|dll|bat|cmd|ps1|sh|py|whl|json|toml|yaml|yml|ini|pem|key|crt|log)\b/i.test(value)
  ) {
    return true;
  }
  // URLs
  if (/(?:https?|wss?|file):\/\//i.test(value)) {
    return true;
  }
  // Secret / token / auth patterns
  if (
    /\b(?:sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|AIza[A-Za-z0-9_-]{10,}|Bearer\s+\S+)\b/i.test(
      value,
    ) ||
    /\b(?:api[_-]?key|secret|password|passwd|access[_-]?token|refresh[_-]?token|authorization|private[_-]?key)\s*[:=]/i.test(
      value,
    )
  ) {
    return true;
  }
  // Exception / traceback / CLI flag patterns
  if (
    /(?:Traceback \(most recent call last\)|Exception:|Error:\s|--version\b|STDERR|STDOUT|ENOENT|EACCES|ECONNREFUSED)/i.test(
      value,
    )
  ) {
    return true;
  }
  return false;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 && Number.isSafeInteger(value);
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0 && Number.isSafeInteger(value);
}

export function isValidIsoTimestamp(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const trimmed = value.trim();
  if (!trimmed || trimmed.length > 64 || CONTROL_CHAR_RE.test(trimmed)) return false;
  // Require standard ISO-8601 date-time pattern with timezone (Z or +/-offset)
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})$/i.test(trimmed)) {
    return false;
  }
  const parsed = Date.parse(trimmed);
  return !Number.isNaN(parsed);
}

/**
 * Tri-state boolean normalization: ONLY literal true or false are preserved.
 * Null, undefined, numbers, strings, and objects normalize to null (Unknown).
 * Boolean(raw.foo) is strictly forbidden.
 */
export function normalizeTriStateBoolean(value: unknown): boolean | null {
  if (value === true) return true;
  if (value === false) return false;
  return null;
}

function normalizeBoundedRequiredId(value: unknown, maxLength = MAX_ID_LENGTH): string {
  if (typeof value !== "string") {
    throw new AgentComputerStatusError("invalid_response", 200);
  }
  const trimmed = value.trim();
  if (
    !trimmed ||
    trimmed.length > maxLength ||
    CONTROL_CHAR_RE.test(trimmed) ||
    looksLikeUnsafeDiagnosticOrSecret(trimmed)
  ) {
    throw new AgentComputerStatusError("invalid_response", 200);
  }
  return trimmed;
}

function normalizeBoundedNullableId(value: unknown, maxLength = MAX_ID_LENGTH): string | null {
  if (value === null) return null;
  if (typeof value !== "string") {
    throw new AgentComputerStatusError("invalid_response", 200);
  }
  const trimmed = value.trim();
  if (!trimmed) return null;
  if (
    trimmed.length > maxLength ||
    CONTROL_CHAR_RE.test(trimmed) ||
    looksLikeUnsafeDiagnosticOrSecret(trimmed)
  ) {
    throw new AgentComputerStatusError("invalid_response", 200);
  }
  return trimmed;
}

function normalizeSafeDisplayName(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (
    !trimmed ||
    trimmed.length > MAX_DISPLAY_NAME_LENGTH ||
    looksLikeUnsafeDiagnosticOrSecret(trimmed)
  ) {
    return null;
  }
  return trimmed;
}

function normalizeSafeVersionString(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (
    !trimmed ||
    trimmed.length > MAX_VERSION_LENGTH ||
    !SAFE_VERSION_RE.test(trimmed) ||
    looksLikeUnsafeDiagnosticOrSecret(trimmed)
  ) {
    return null;
  }
  return trimmed;
}

function normalizeNullableRevision(value: unknown): number | null {
  if (value === null) return null;
  if (isNonNegativeInteger(value)) return value;
  throw new AgentComputerStatusError("invalid_response", 200);
}

export function normalizeEvidence(raw: unknown): ComputerStatusEvidence {
  if (!isPlainObject(raw)) {
    throw new AgentComputerStatusError("invalid_response", 200);
  }
  const rawSource = typeof raw.source === "string" ? raw.source : "";
  const source: ComputerStatusEvidenceSource = EVIDENCE_SOURCE_SET.has(rawSource)
    ? (rawSource as ComputerStatusEvidenceSource)
    : "none";
  if (source === "none") {
    return {
      source: "none",
      observed_at: isValidIsoTimestamp(raw.observed_at) && rawSource === "none" ? raw.observed_at : null,
    };
  }
  return {
    source,
    observed_at: isValidIsoTimestamp(raw.observed_at) ? raw.observed_at : null,
  };
}

export function normalizeReasonCode(raw: unknown): ComputerStatusReasonCode | null {
  if (raw === null || raw === undefined) return null;
  if (typeof raw !== "string") return "status_unavailable";
  const trimmed = raw.trim();
  if (!trimmed) return null;
  if (REASON_CODE_SET.has(trimmed)) {
    return trimmed as ComputerStatusReasonCode;
  }
  return "status_unavailable";
}

function normalizeNativeSection(raw: unknown): ComputerStatusNative {
  if (!isPlainObject(raw)) {
    throw new AgentComputerStatusError("invalid_response", 200);
  }
  const platform: ComputerStatusNativePlatform =
    raw.platform === "windows" || raw.platform === "unsupported" || raw.platform === "unknown"
      ? raw.platform
      : "unknown";

  const executable_presence: ComputerStatusExecutablePresence =
    raw.executable_presence === "present" ||
    raw.executable_presence === "missing" ||
    raw.executable_presence === "unknown"
      ? raw.executable_presence
      : "unknown";

  const rawStatus =
    raw.status === "unknown" ||
    raw.status === "missing" ||
    raw.status === "unverified" ||
    raw.status === "ready" ||
    raw.status === "incompatible" ||
    raw.status === "error"
      ? raw.status
      : "unknown";

  const version = normalizeSafeVersionString(raw.version);
  const protocol_version = normalizeSafeVersionString(raw.protocol_version);
  const evidence = normalizeEvidence(raw.evidence);
  let reason_code = normalizeReasonCode(raw.reason_code);

  let status: ComputerStatusNativeState = rawStatus;

  // Enforce Section 3 Native ready rules:
  // If prior verified record is stale, must show unverified, never ready.
  if (reason_code === "native_evidence_stale" && status === "ready") {
    status = "unverified";
  }

  // Known missing executable cannot be ready
  if (executable_presence === "missing" && status === "ready") {
    status = "missing";
    reason_code = reason_code ?? "native_missing";
  }

  // Unsupported or unknown platform cannot be ready
  if (platform !== "windows" && status === "ready") {
    status = platform === "unsupported" ? "error" : "unknown";
    reason_code =
      reason_code ?? (platform === "unsupported" ? "native_platform_unsupported" : "native_not_verified");
  }

  // Present executable without verified version, protocol_version, and handshake/probe evidence is unverified, never ready.
  const hasVerifiedNativeEvidence =
    (evidence.source === "native_probe_cache" || evidence.source === "live_session") &&
    evidence.observed_at !== null;
  const hasNegativeNativeReason =
    reason_code === "native_not_verified" ||
    reason_code === "native_missing" ||
    reason_code === "native_platform_unsupported" ||
    reason_code === "native_protocol_incompatible" ||
    reason_code === "native_evidence_stale" ||
    reason_code === "native_failed";

  if (status === "ready") {
    if (
      platform !== "windows" ||
      executable_presence !== "present" ||
      version === null ||
      protocol_version === null ||
      !hasVerifiedNativeEvidence ||
      hasNegativeNativeReason
    ) {
      status = executable_presence === "present" ? "unverified" : "unknown";
      if (!reason_code) {
        reason_code = "native_not_verified";
      }
    }
  }

  return {
    platform,
    executable_presence,
    status,
    version,
    protocol_version,
    evidence,
    reason_code,
  };
}

function normalizePermissionMode(raw: unknown): ComputerStatusPermissionMode | null {
  if (raw === null || raw === undefined) return null;
  if (typeof raw === "string" && PERMISSION_MODE_SET.has(raw)) {
    return raw as ComputerStatusPermissionMode;
  }
  return "unknown";
}

function normalizePermissionsSection(
  raw: unknown,
  hasTaskContext: boolean,
): ComputerStatusPermissions {
  if (!isPlainObject(raw)) {
    throw new AgentComputerStatusError("invalid_response", 200);
  }
  const agent_enabled = normalizeTriStateBoolean(raw.agent_enabled);
  const global_desktop_access = normalizeTriStateBoolean(raw.global_desktop_access);
  const task_scope: ComputerStatusTaskPermissionScope =
    hasTaskContext && raw.task_scope === "present" ? "present" : "not_requested";

  // Do not infer task permissions from global permissions.
  const task_local_execution = hasTaskContext
    ? normalizeTriStateBoolean(raw.task_local_execution)
    : null;
  const task_desktop_access = hasTaskContext
    ? normalizeTriStateBoolean(raw.task_desktop_access)
    : null;

  const global_permission_mode = normalizePermissionMode(raw.global_permission_mode);
  const task_permission_mode = hasTaskContext
    ? normalizePermissionMode(raw.task_permission_mode)
    : null;

  const evidence = normalizeEvidence(raw.evidence);
  const reason_code = normalizeReasonCode(raw.reason_code);

  return {
    agent_enabled,
    global_desktop_access,
    task_scope,
    task_local_execution,
    task_desktop_access,
    global_permission_mode,
    task_permission_mode,
    evidence,
    reason_code,
  };
}

function normalizeSessionSection(raw: unknown, hasTaskContext: boolean): ComputerStatusSession {
  if (!isPlainObject(raw)) {
    throw new AgentComputerStatusError("invalid_response", 200);
  }
  const rawState = typeof raw.state === "string" ? raw.state : "";
  const state: ComputerStatusSessionState = !hasTaskContext
    ? "not_applicable"
    : SESSION_STATE_SET.has(rawState)
      ? (rawState as ComputerStatusSessionState)
      : "unknown";

  const generation = !hasTaskContext ? null : normalizeNullableRevision(raw.generation ?? null);
  const evidence = normalizeEvidence(raw.evidence);
  const reason_code = normalizeReasonCode(raw.reason_code);

  return {
    state,
    generation,
    evidence,
    reason_code,
  };
}

function normalizeModelSection(
  raw: unknown,
  contextModelId: string | null,
): ComputerStatusModel {
  if (!isPlainObject(raw) || !isPlainObject(raw.image_input)) {
    throw new AgentComputerStatusError("invalid_response", 200);
  }

  const id = normalizeBoundedNullableId(raw.id, MAX_MODEL_ID_LENGTH);

  // Contract lines 120 & 228: model.id must equal context.model_id.
  if (id !== contextModelId) {
    throw new AgentComputerStatusError("invalid_response", 200);
  }

  const display_name = normalizeSafeDisplayName(raw.display_name);

  const adapter: ComputerStatusModelAdapter | null =
    raw.adapter === null || raw.adapter === undefined
      ? null
      : typeof raw.adapter === "string" && MODEL_ADAPTER_SET.has(raw.adapter)
        ? (raw.adapter as ComputerStatusModelAdapter)
        : "unknown";

  const availability: ComputerStatusModelAvailability =
    typeof raw.availability === "string" && MODEL_AVAILABILITY_SET.has(raw.availability)
      ? (raw.availability as ComputerStatusModelAvailability)
      : "unknown";

  const rawImage = raw.image_input;
  let status: ComputerStatusImageInputState =
    typeof rawImage.status === "string" && IMAGE_INPUT_STATUS_SET.has(rawImage.status)
      ? (rawImage.status as ComputerStatusImageInputState)
      : "unknown";

  let carrier: ComputerStatusImageCarrier =
    typeof rawImage.carrier === "string" && IMAGE_CARRIER_SET.has(rawImage.carrier)
      ? (rawImage.carrier as ComputerStatusImageCarrier)
      : "unknown";

  const evidence = normalizeEvidence(rawImage.evidence);
  let reason_code = normalizeReasonCode(rawImage.reason_code);

  // Antigravity CLI is unsupported with carrier:none under the v1 headless contract.
  if (adapter === "antigravity_cli") {
    status = "unsupported";
    carrier = "none";
    if (!reason_code) {
      reason_code = "image_route_unsupported";
    }
  } else if (status === "verified") {
    const hasValidCarrier = carrier === "codex_turn_input" || carrier === "api_image_blocks";
    const hasBackendEvidence =
      (evidence.source === "model_image_probe" || evidence.source === "runtime_receipt") &&
      evidence.observed_at !== null;
    const hasInvalidatingReason =
      reason_code === "image_evidence_stale" ||
      reason_code === "image_verification_pending" ||
      reason_code === "image_verification_failed" ||
      reason_code === "image_route_unsupported" ||
      reason_code === "image_capability_unknown";

    if (!hasValidCarrier || !hasBackendEvidence || hasInvalidatingReason) {
      status = hasValidCarrier ? "carrier_available" : "unknown";
      if (!reason_code) {
        reason_code = hasValidCarrier ? "image_verification_pending" : "image_capability_unknown";
      }
    }
  }

  return {
    id,
    display_name,
    adapter,
    availability,
    image_input: {
      status,
      carrier,
      evidence,
      reason_code,
    },
  };
}

function normalizeObservationStage(raw: unknown): ComputerStatusObservationStage {
  if (!isPlainObject(raw)) {
    throw new AgentComputerStatusError("invalid_response", 200);
  }
  const state: ComputerStatusObservationStageState =
    typeof raw.state === "string" && STAGE_STATE_SET.has(raw.state)
      ? (raw.state as ComputerStatusObservationStageState)
      : "unknown";
  const evidence = normalizeEvidence(raw.evidence);
  return { state, evidence };
}

function normalizeLatestObservation(
  raw: unknown,
  context: ComputerStatusContext,
): ComputerStatusObservation | null {
  if (raw === null) return null;
  if (!isPlainObject(raw) || !isPlainObject(raw.stages)) {
    throw new AgentComputerStatusError("invalid_response", 200);
  }

  const id = normalizeBoundedRequiredId(raw.id, MAX_ID_LENGTH);
  const task_revision = normalizeNullableRevision(raw.task_revision ?? null);
  const selection_epoch = normalizeBoundedRequiredId(raw.selection_epoch, MAX_ID_LENGTH);
  const model_id = normalizeBoundedNullableId(raw.model_id ?? null, MAX_MODEL_ID_LENGTH);

  const epochMatches = selection_epoch === context.selection_epoch;
  const modelMatches =
    model_id === null || context.model_id === null || model_id === context.model_id;
  const matches_current_selection =
    raw.matches_current_selection === true && epochMatches && modelMatches;

  const width =
    raw.width === null || raw.width === undefined
      ? null
      : isPositiveInteger(raw.width)
        ? raw.width
        : null;
  const height =
    raw.height === null || raw.height === undefined
      ? null
      : isPositiveInteger(raw.height)
        ? raw.height
        : null;

  const rawStages = raw.stages;
  const saved = normalizeObservationStage(rawStages.saved);
  const displayed = normalizeObservationStage(rawStages.displayed);
  const submitted = normalizeObservationStage(rawStages.submitted);
  const semantic_observed = normalizeObservationStage(rawStages.semantic_observed);

  return {
    id,
    task_revision,
    selection_epoch,
    model_id,
    matches_current_selection,
    width,
    height,
    stages: {
      saved,
      displayed,
      submitted,
      semantic_observed,
    },
  };
}

export interface ComputerStatusExpectedPermissions {
  agentEnabled?: boolean | null;
  globalDesktopAccess?: boolean | null;
  taskDesktopAccess?: boolean | null;
  taskLocalExecution?: boolean | null;
  globalPermissionMode?: string | null;
  taskPermissionMode?: string | null;
}

export interface ComputerStatusValidationExpectations {
  expectedTaskId?: string | null;
  /**
   * When !== undefined, requires context.model_id and model.id to match expectedModelId.
   * Distinguishes undefined (caller did not specify) from null (explicitly no model selected).
   */
  expectedModelId?: string | null;
  minTaskRevision?: number | null;
  minSettingsRevision?: number | null;
  expectedPermissions?: ComputerStatusExpectedPermissions;
}

/**
 * Strictly validates and normalizes raw unknown JSON into ComputerStatusDtoV1.
 * Throws AgentComputerStatusError("invalid_response") if schema_version !== 1,
 * required structural fields are absent, or task/model/permission context mismatches.
 */
export function validateAndNormalizeComputerStatus(
  raw: unknown,
  options?: ComputerStatusValidationExpectations,
): ComputerStatusDtoV1 {
  if (!isPlainObject(raw)) {
    throw new AgentComputerStatusError("invalid_response", 200);
  }
  if (raw.schema_version !== COMPUTER_STATUS_SCHEMA_VERSION) {
    throw new AgentComputerStatusError("invalid_response", 200);
  }

  const snapshot_id = normalizeBoundedRequiredId(raw.snapshot_id, MAX_ID_LENGTH);
  if (!isValidIsoTimestamp(raw.generated_at)) {
    throw new AgentComputerStatusError("invalid_response", 200);
  }
  const generated_at = raw.generated_at.trim();

  if (
    !isPlainObject(raw.context) ||
    !isPlainObject(raw.plugin) ||
    !isPlainObject(raw.native) ||
    !isPlainObject(raw.permissions) ||
    !isPlainObject(raw.session) ||
    !isPlainObject(raw.model) ||
    raw.latest_observation === undefined
  ) {
    throw new AgentComputerStatusError("invalid_response", 200);
  }

  const rawContext = raw.context;
  const task_id = normalizeBoundedNullableId(rawContext.task_id, MAX_ID_LENGTH);
  const task_revision = normalizeNullableRevision(rawContext.task_revision);
  const settings_revision = normalizeNullableRevision(rawContext.settings_revision);

  if (rawContext.selection_source !== "settings" && rawContext.selection_source !== "task") {
    throw new AgentComputerStatusError("invalid_response", 200);
  }
  const selection_source: ComputerStatusSelectionSource = rawContext.selection_source;
  const model_id = normalizeBoundedNullableId(rawContext.model_id, MAX_MODEL_ID_LENGTH);
  const selection_epoch = normalizeBoundedRequiredId(rawContext.selection_epoch, MAX_ID_LENGTH);

  const expectedTaskId =
    options?.expectedTaskId === undefined ? undefined : options.expectedTaskId;
  if (expectedTaskId !== undefined) {
    if (expectedTaskId === null) {
      if (task_id !== null || selection_source !== "settings") {
        throw new AgentComputerStatusError("invalid_response", 200);
      }
    } else {
      if (task_id !== expectedTaskId || selection_source !== "task") {
        throw new AgentComputerStatusError("invalid_response", 200);
      }
    }
  } else {
    if (task_id === null && selection_source !== "settings") {
      throw new AgentComputerStatusError("invalid_response", 200);
    }
    if (task_id !== null && selection_source !== "task") {
      throw new AgentComputerStatusError("invalid_response", 200);
    }
  }

  if (options?.expectedModelId !== undefined) {
    const normalizedExpectedModelId =
      options.expectedModelId === null
        ? null
        : typeof options.expectedModelId === "string" && options.expectedModelId.trim().length > 0
          ? options.expectedModelId.trim()
          : null;
    if (model_id !== normalizedExpectedModelId) {
      throw new AgentComputerStatusError("invalid_response", 200);
    }
  }

  if (
    typeof options?.minTaskRevision === "number" &&
    Number.isInteger(options.minTaskRevision) &&
    options.minTaskRevision >= 0 &&
    (task_revision === null || task_revision < options.minTaskRevision)
  ) {
    throw new AgentComputerStatusError("invalid_response", 200);
  }

  if (
    typeof options?.minSettingsRevision === "number" &&
    Number.isInteger(options.minSettingsRevision) &&
    options.minSettingsRevision >= 0 &&
    (settings_revision === null || settings_revision < options.minSettingsRevision)
  ) {
    throw new AgentComputerStatusError("invalid_response", 200);
  }

  const context: ComputerStatusContext = {
    task_id,
    task_revision,
    settings_revision,
    selection_source,
    model_id,
    selection_epoch,
  };

  const rawPlugin = raw.plugin;
  const plugin: ComputerStatusPlugin = {
    installed: normalizeTriStateBoolean(rawPlugin.installed),
    enabled: normalizeTriStateBoolean(rawPlugin.enabled),
    revision: normalizeNullableRevision(rawPlugin.revision),
    evidence: normalizeEvidence(rawPlugin.evidence),
    reason_code: normalizeReasonCode(rawPlugin.reason_code),
  };

  const hasTaskContext = task_id !== null;
  const native = normalizeNativeSection(raw.native);
  const permissions = normalizePermissionsSection(raw.permissions, hasTaskContext);

  const expectedPerms = options?.expectedPermissions;
  if (expectedPerms) {
    if (
      typeof expectedPerms.agentEnabled === "boolean" &&
      permissions.agent_enabled !== null &&
      permissions.agent_enabled !== expectedPerms.agentEnabled
    ) {
      throw new AgentComputerStatusError("invalid_response", 200);
    }
    if (
      typeof expectedPerms.globalDesktopAccess === "boolean" &&
      permissions.global_desktop_access !== null &&
      permissions.global_desktop_access !== expectedPerms.globalDesktopAccess
    ) {
      throw new AgentComputerStatusError("invalid_response", 200);
    }
    if (
      typeof expectedPerms.taskDesktopAccess === "boolean" &&
      permissions.task_desktop_access !== null &&
      permissions.task_desktop_access !== expectedPerms.taskDesktopAccess
    ) {
      throw new AgentComputerStatusError("invalid_response", 200);
    }
    if (
      typeof expectedPerms.taskLocalExecution === "boolean" &&
      permissions.task_local_execution !== null &&
      permissions.task_local_execution !== expectedPerms.taskLocalExecution
    ) {
      throw new AgentComputerStatusError("invalid_response", 200);
    }
    if (
      typeof expectedPerms.globalPermissionMode === "string" &&
      PERMISSION_MODE_SET.has(expectedPerms.globalPermissionMode.trim()) &&
      permissions.global_permission_mode !== null &&
      permissions.global_permission_mode !== "unknown" &&
      permissions.global_permission_mode !== expectedPerms.globalPermissionMode.trim()
    ) {
      throw new AgentComputerStatusError("invalid_response", 200);
    }
    if (
      typeof expectedPerms.taskPermissionMode === "string" &&
      PERMISSION_MODE_SET.has(expectedPerms.taskPermissionMode.trim()) &&
      permissions.task_permission_mode !== null &&
      permissions.task_permission_mode !== "unknown" &&
      permissions.task_permission_mode !== expectedPerms.taskPermissionMode.trim()
    ) {
      throw new AgentComputerStatusError("invalid_response", 200);
    }
  }

  const session = normalizeSessionSection(raw.session, hasTaskContext);
  const model = normalizeModelSection(raw.model, model_id);
  const latest_observation = normalizeLatestObservation(raw.latest_observation, context);

  return {
    schema_version: 1,
    snapshot_id,
    generated_at,
    context,
    plugin,
    native,
    permissions,
    session,
    model,
    latest_observation,
  };
}

/**
 * Maps public reason codes to authored Chinese/English descriptions.
 * Unknown codes return generic "状态暂不可用 / Status unavailable", never raw error strings.
 */
export function describeComputerStatusReason(
  code: string | null | undefined,
  tx: (zh: string, en: string) => string,
): string | null {
  if (code === null || code === undefined) return null;
  const trimmed = typeof code === "string" ? code.trim() : "";
  if (!trimmed) return null;

  switch (trimmed) {
    case "plugin_missing":
      return tx("未安装 Computer Use 插件", "Computer Use plugin is not installed");
    case "plugin_disabled":
      return tx("Computer Use 插件已停用", "Computer Use plugin is disabled");
    case "agent_disabled":
      return tx("Agent 功能未启用", "Agent is disabled");
    case "native_not_verified":
      return tx(
        "原生引擎尚未完成版本与协议握手验证",
        "Native engine version and protocol handshake are not verified",
      );
    case "native_missing":
      return tx(
        "未找到受信任的 Ryn 原生引擎可执行文件",
        "Ryn native engine executable is missing",
      );
    case "native_platform_unsupported":
      return tx(
        "当前系统平台不支持 Windows 原生桌面控制",
        "Current platform does not support Windows native desktop control",
      );
    case "native_protocol_incompatible":
      return tx("原生引擎协议版本不兼容", "Native engine protocol version is incompatible");
    case "native_evidence_stale":
      return tx(
        "原生引擎验证记录已过期，需重新验证",
        "Native engine verification evidence is stale",
      );
    case "native_failed":
      return tx("原生引擎检查或运行失败", "Native engine check or execution failed");
    case "global_desktop_access_disabled":
      return tx(
        "全局桌面操作权限 (desktop_access) 未开启",
        "Global desktop access (desktop_access) is disabled",
      );
    case "task_local_execution_disabled":
      return tx("当前任务未开启本地执行权限", "Task local execution permission is disabled");
    case "task_desktop_access_disabled":
      return tx("当前任务未开启桌面操作权限", "Task desktop access permission is disabled");
    case "session_not_started":
      return tx("任务桌面会话尚未启动", "Task desktop session has not started");
    case "session_lost":
      return tx("任务桌面会话连接已断开", "Task desktop session connection was lost");
    case "model_unavailable":
      return tx("当前所选模型不可用", "Selected model is unavailable");
    case "image_route_unsupported":
      return tx(
        "当前模型通道不支持图像输入",
        "Selected model route does not support image input",
      );
    case "image_capability_unknown":
      return tx("当前模型图像输入能力未知", "Model image input capability is unknown");
    case "image_verification_pending":
      return tx(
        "图像通道可传图，待实际像素验证",
        "Image carrier available; pixel verification pending",
      );
    case "image_verification_failed":
      return tx("模型图像输入验证失败", "Model image input verification failed");
    case "image_evidence_stale":
      return tx("模型图像验证记录已过期", "Model image verification evidence is stale");
    case "status_unavailable":
    default:
      return tx("状态暂不可用", "Status unavailable");
  }
}

function mapErrorResponse(
  status: number,
  rawCode: unknown,
  requestedTaskId: string | null,
): AgentComputerStatusError {
  const code = typeof rawCode === "string" ? rawCode.trim() : "";

  if (status === 400) {
    if (code === "agent_invalid_task_id") {
      return new AgentComputerStatusError("agent_invalid_task_id", 400);
    }
    return new AgentComputerStatusError("agent_invalid_request", 400);
  }
  if (status === 401 || status === 403) {
    return new AgentComputerStatusError("unauthorized", status);
  }
  if (status === 404) {
    if (requestedTaskId !== null && code === "agent_task_not_found") {
      return new AgentComputerStatusError("agent_task_not_found", 404);
    }
    return new AgentComputerStatusError("endpoint_not_found", 404);
  }
  if (status === 410 || code === "agent_task_deleted") {
    return new AgentComputerStatusError("agent_task_deleted", status);
  }
  if (status === 503 || code === "agent_computer_status_unavailable") {
    return new AgentComputerStatusError("agent_computer_status_unavailable", status);
  }
  return new AgentComputerStatusError("status_unavailable", status);
}

export interface GetComputerStatusOptions {
  taskId?: string;
  signal?: AbortSignal;
}

export async function getAgentComputerStatus(
  options?: GetComputerStatusOptions,
): Promise<ComputerStatusDtoV1> {
  let requestedTaskId: string | null = null;
  if (options?.taskId !== undefined) {
    if (typeof options.taskId !== "string") {
      throw new AgentComputerStatusError("agent_invalid_task_id", 400);
    }
    const trimmed = options.taskId.trim();
    if (
      !trimmed ||
      trimmed.length > MAX_ID_LENGTH ||
      CONTROL_CHAR_RE.test(trimmed) ||
      looksLikeUnsafeDiagnosticOrSecret(trimmed)
    ) {
      throw new AgentComputerStatusError("agent_invalid_task_id", 400);
    }
    requestedTaskId = trimmed;
  }

  const path =
    requestedTaskId !== null
      ? `/agent/computer/status?task_id=${encodeURIComponent(requestedTaskId)}`
      : "/agent/computer/status";

  let response: Response;
  try {
    response = await fetch(nodeControlUrl(path), {
      method: "GET",
      credentials: "include",
      cache: "no-store",
      signal: options?.signal,
    });
  } catch (error) {
    if (
      (error instanceof DOMException && error.name === "AbortError") ||
      (error instanceof Error && error.name === "AbortError")
    ) {
      throw error;
    }
    throw new AgentComputerStatusError("network_error", 0);
  }

  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as Record<string, unknown>;
    const rawCode =
      typeof body?.detail === "string"
        ? body.detail
        : typeof body?.error === "string"
          ? body.error
          : typeof body?.code === "string"
            ? body.code
            : "";
    throw mapErrorResponse(response.status, rawCode, requestedTaskId);
  }

  let data: unknown;
  try {
    data = await response.json();
  } catch {
    throw new AgentComputerStatusError("invalid_response", response.status);
  }

  return validateAndNormalizeComputerStatus(data, { expectedTaskId: requestedTaskId });
}

export const agentComputerStatus = {
  getStatus: getAgentComputerStatus,
  validateAndNormalize: validateAndNormalizeComputerStatus,
  describeReason: describeComputerStatusReason,
};
