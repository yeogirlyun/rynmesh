import { nodeControlUrl } from "./nodeUrl";
import type { AgentAttachmentInput, AgentMessageAttachment } from "./agentAttachments";
import type { ModelTokenUsage } from "./aiSources";

export type AgentStatus = "queued" | "running" | "waiting_approval" | "waiting_input" | "pausing" | "reconciling" | "paused" | "completed" | "cancelled" | "failed" | "interrupted";
export type AgentRunState = "queued" | "running" | "pausing" | "paused" | "waiting_input" | "waiting_approval" | "reconciling" | "ended";
export interface AgentGoal {
  revision: number;
  kind: "question" | "action" | "mixed" | "unclassified";
  original_message_id: string;
  text: string;
  constraints: Array<{ id: string; text: string; source: "user" | "system" | "tool"; message_id: string; required: boolean }>;
}
export interface AgentCriterion {
  id: string; goal_revision: number; source: "user" | "system" | "tool"; source_ref: string;
  required: boolean; kind: "mechanical" | "semantic"; description: string;
  verifier: unknown; status: "pending" | "passed" | "failed" | "unknown"; evidence_refs: string[];
}
export interface AgentPlan {
  revision: number; goal_revision: number;
  steps: Array<{ id: string; text: string; criterion_ids: string[]; depends_on: string[]; status: "pending" | "running" | "blocked" | "done" }>;
}
export interface AgentOutcome {
  assessment_requested?: boolean;
  status: "unverified" | "verified" | "partial" | "not_achieved" | "unknown";
  goal_revision: number; criteria_results: unknown[]; summary: string; evidence_refs: string[]; uncertainties: string[];
}
export interface AgentAllowedAction { enabled: boolean; reason: string | null; requires_control_revision: boolean; }
export type AgentMessageIntent = "steer" | "information";
export interface AgentPendingInput {
  id: string; kind: "information" | "choice" | "credential_ref"; prompt: string;
  fields: Array<{ id: string; label: string; type: "text" | "choice" | "credential_ref"; required: boolean; options?: Array<{ value: string; label: string }> }>;
  goal_revision: number; control_revision: number; scope_revision: number; business_revision: unknown; created_at: number | string;
}
export interface AgentResourceSelection { connection_id: string; root_path: string; root_kind?: "file" | "directory"; recursive: boolean; actions: Array<"list" | "read_ai" | "create_file" | "create_directory">; }
export interface AgentScopeSelection { resources: AgentResourceSelection[]; allow_model_data: boolean; }
export interface AgentResourceCatalog {
  connections: Array<{ connection_id: string; name: string; protocol: string; enabled: boolean; allow_ai: boolean; agent_supported: boolean; reason: string; writable?: boolean; create_supported?: boolean }>;
  supported_formats: string[];
  create_scope_supported?: boolean;
  file_scope_supported?: boolean;
}
export interface AgentModelDestination { catalog_model_id: string; provider_peer_id?: string | null; source_id?: string | null; destination_revision?: string | null; }
export interface AgentModelSwitchPreview {
  model: Pick<AgentModel, "id" | "name" | "source" | "source_name" | "source_id" | "peer_id">;
  current_model_id: string; destination: AgentModelDestination; control_revision: number; scope_revision: number;
  current_scope_selection: AgentScopeSelection; current_scope_authorized?: boolean;
  current_project?: { id: string; name: string; path: string } | null;
  data_policy: { sent_after_resume: string[]; excluded: string[]; resources_require_explicit_transfer_or_new_authorization: boolean; automatic_resume: boolean };
  confirmation_required: true;
}
export interface AgentModelSwitchRequest {
  model_id: string; expected_control_revision: number; expected_scope_revision: number;
  expected_destination: AgentModelDestination; confirm_data_destination: true; transfer_resource_scope: boolean; client_request_id: string;
}
export interface AgentScopeSnapshot {
  scope: { resource_grants: unknown[]; egress_grants: unknown[]; revision?: number; selection?: AgentScopeSelection };
  scope_revision: number; control_revision: number;
  model_destination?: AgentModelDestination;
  current_model_destination?: AgentModelDestination | null;
}
export interface AgentContextBudgetPolicy {
  mode: string;
  provenance: string;
  effective_max_context_bytes: number;
  context_window_tokens: number;
  context_window_known: boolean;
  context_window_basis: string;
  reserved_output_tokens: number;
  framing_reserve_tokens: number;
  estimate_basis: string;
  units?: Record<string, string>;
  token_count_is_estimate?: boolean;
  manual_max_context_bytes?: number | null;
}
export interface AgentSettings {
  enabled: boolean;
  model_id: string;
  permission_mode: "workspace" | "read_only" | "confirm_changes" | "automatic";
  allowed_write_tools: string[];
  workspace_root?: string;
  desktop_access?: boolean;
  max_steps: number;
  max_seconds: number;
  max_context_chars: number | null;
  context_budget_policy?: AgentContextBudgetPolicy;
  max_output_chars: number;
}
export interface AgentModel {
  context_window?: number;
  context_window_known?: boolean;
  context_window_basis?: string;
  supported_reasoning_efforts?: string[];
  default_reasoning_effort?: string;
  cli_model?: string;
  cli_source?: string;
  service_id?: string;
  id: string;
  name: string;
  source: string;
  available: boolean;
  /** Whether this connection implements the assistant tool protocol; not a probe result. */
  agent_supported?: boolean;
  /** Historical tool probe result for diagnostics only. */
  tool_support?: "supported" | "unknown" | "unsupported";
  description?: string;
  destination_revision?: string | null;
  destination_binding_supported?: boolean;
  source_id?: string;
  source_name?: string;
  peer_id?: string;
  usage?: ModelTokenUsage;
}
export interface AgentTool { name: string; title: string; effect: "read" | "write"; description?: string; }
export interface AgentSkill { id: string; version: string; title: string; description: string; }
export interface AgentPreferences { revision: number; items: Array<{ id: string; text: string }>; }
export interface AgentBudgetLimits { max_total_model_calls: number; max_total_tool_calls: number; max_total_tokens: number; max_total_seconds: number; }
export interface AgentBudgetUsage { model_calls: number | null; tool_calls: number | null; charged_model_calls?: number; charged_tool_calls?: number; legacy_usage_unknown?: boolean; total_tokens: number | null; charged_tokens: number; charged_seconds: number; active_seconds: number | null; cost: number | null; usage_complete: boolean; unsettled_operations: number; }
export interface AgentBudget { limits: AgentBudgetLimits; round: number; lifetime: AgentBudgetUsage; current_round: AgentBudgetUsage; }
export interface AgentRunTiming {
  recorded_seconds: number | null;
  elapsed_seconds: number | null;
  max_seconds: number | null;
  measurement: "live" | "recorded" | "pending" | "unknown";
}
export interface AgentApproval {
  id: string;
  tool: string;
  arguments: Record<string, unknown>;
  call_id: string;
  arguments_sha256: string;
  preview?: unknown;
}
export interface AgentMessage {
  attachments?: AgentMessageAttachment[];
  message_id?: string;
  original_context_epoch?: number;
  role: string;
  context_epoch?: number;
  content: string | null;
  tool_call_id?: string;
  tool_calls?: Array<{ id: string; function: { name: string; arguments: string } }>;
  ui?: { kind: "input_answer" | "source_credential" | "verification_feedback" | "change_review" | "steer" | "information" | "message" | "heartbeat" | "silent_heartbeat"; text?: string; prompt?: string; pending_input_id?: string; fields?: Array<{ label: string; value: string }>; endpoint?: string; has_credential?: boolean; hidden?: boolean; automation_id?: string; automation_name?: string };
}
export interface AgentEvent {
  seq: number;
  type: string;
  at: number | string;
  tool?: string;
  result?: unknown;
  [key: string]: unknown;
}
export interface AgentTaskSummary {
  conversation_revision?: number;
  conversation_event_cursor?: number;
  reasoning_effort?: string;
  project?: AgentProject;
  local_execution?: { workspace_root: string; desktop_access?: boolean };
  id: string;
  title: string;
  model_id: string;
  status: AgentStatus;
  revision: number;
  created_at: number | string;
  updated_at: number | string;
  pending_approval: AgentApproval | null;
  steps: number;
  result: string;
  error: string | null;
  client_request_id?: string;
  permission_mode?: AgentSettings["permission_mode"];
  allowed_write_tools?: string[];
  schema_version?: number;
  control_revision?: number;
  event_cursor?: number;
  completion_event_seq?: number;
  run_state?: AgentRunState;
  goal?: AgentGoal;
  criteria?: AgentCriterion[];
  plan?: AgentPlan;
  outcome?: AgentOutcome;
  allowed_actions?: Record<string, AgentAllowedAction>;
  pending_input?: AgentPendingInput | null;
  scope_revision?: number;
  model_destination?: AgentModelDestination;
  archived?: boolean;
  result_refs?: AgentResultRef[];
  file_artifact_revision?: number;
  budget?: AgentBudget;
  run_timing?: AgentRunTiming;
  skills?: AgentSkill[];
  source?: string;
  automation_id?: string | null;
  automation_run_id?: string | null;
  automation_kind?: "cron" | "heartbeat";
  latest_turn_is_silent_heartbeat?: boolean;
  automation_notification?: { id: string; automation_id: string; automation_name: string; kind: "cron" | "heartbeat"; decision: string; title: string; message: string; task_id: string; status?: string; created_at: string } | null;
}
export interface AgentResultRef { id: string; task_id: string; tool: string; operation_id: string; goal_revision: number; scope_revision: number; context_epoch: number; sha256: string; bytes: number; }
export interface AgentProject { id: string; name: string; path: string; generation: number; entry_files: string[]; manifests: string[]; directories: string[]; }
export interface AgentResultPage { id: string; sha256: string; bytes: number; characters: number; offset: number; next_offset: number | null; content: string; }
export interface AgentArtifact { id: string; name: string; bytes: number; sha256: string; created_at?: string; }
/** A workspace file proven by this task's own confirmed file operations, re-checked against the disk. */
export interface AgentFileArtifact {
  id: string; name: string; path: string; absolute_path?: string; operation_id?: string; source?: "write" | "command" | "read" | "computer_observation"; media_type: string; preview: "image" | "text" | "download";
  role: "deliverable" | "supporting"; status: "current" | "changed_outside_task" | "missing" | "too_large" | "unavailable" | "unverified";
  bytes?: number; sha256?: string | null; receipt_sha256?: string | null; created_by_task: boolean; revisions: number;
  attribution?: "observed_during_command"; turn_index?: number;
  goal_revisions: number[]; created_at?: string; updated_at?: string; mentioned_in_answer: boolean;
  render_inspected?: { at?: string; findings: number };
}
export interface AgentArtifactListing { artifacts: AgentArtifact[]; files?: AgentFileArtifact[]; unbacked_mentions?: string[]; pending_files?: boolean; }
export type AgentRecordOperation = "archive" | "restore" | "delete";
export interface AgentDeleteResult { id: string; deleted: true; cleanup_pending: boolean; }
export interface AgentTask extends AgentTaskSummary {
  /** Live model text is projected from memory independently of durable checkpoints. */
  streaming?: { step: number; context_epoch: number; text: string } | null;
  messages: AgentMessage[];
  events: AgentEvent[];
  limits: Partial<AgentSettings>;
}
export type AgentAction = "pause" | "resume" | "stop" | "cancel" | "approve" | "deny";

export class AgentAPIError extends Error {
  constructor(public code: string, public status: number) { super(code); }
}

const READ_TIMEOUT_MS = 10000;
const READ_RETRY_DELAY_MS = 250;

/** Only connection failures and server outages are safe candidates for automatic GET recovery. */
export function isTransientAgentReadError(reason: unknown): boolean {
  return reason instanceof AgentAPIError ? reason.status === 0 || reason.status >= 500 && reason.status < 600
    : reason instanceof TypeError || reason instanceof DOMException && reason.name === "NetworkError";
}

export function agentReadBackoff(failures: number): number {
  return Math.min(15000, 1000 * 2 ** Math.min(Math.max(0, failures - 1), 4));
}

async function readAttempt<T>(run: (signal: AbortSignal) => Promise<T>, signal?: AbortSignal): Promise<T> {
  signal?.throwIfAborted();
  const controller = new AbortController();
  const cancel = () => controller.abort(signal?.reason);
  signal?.addEventListener("abort", cancel, { once: true });
  const timer = setTimeout(() => controller.abort(new AgentAPIError("agent_request_timeout", 0)), READ_TIMEOUT_MS);
  let abort!: () => void;
  const interrupted = new Promise<never>((_, reject) => {
    abort = () => reject(controller.signal.reason);
    controller.signal.addEventListener("abort", abort, { once: true });
  });
  try {
    // Bound decoding too: receiving headers does not prove the response body will finish.
    return await Promise.race([run(controller.signal), interrupted]);
  } catch (reason) {
    if (controller.signal.aborted) throw controller.signal.reason;
    throw reason;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", cancel);
    controller.signal.removeEventListener("abort", abort);
  }
}

async function readWithRecovery<T>(run: (signal: AbortSignal) => Promise<T>, signal?: AbortSignal): Promise<T> {
  try {
    return await readAttempt(run, signal);
  } catch (reason) {
    if (signal?.aborted || !isTransientAgentReadError(reason)) throw reason;
    await new Promise<void>((resolve, reject) => {
      const finish = () => { signal?.removeEventListener("abort", cancel); resolve(); };
      const timer = setTimeout(finish, READ_RETRY_DELAY_MS);
      const cancel = () => { clearTimeout(timer); reject(signal?.reason); };
      signal?.addEventListener("abort", cancel, { once: true });
      if (signal?.aborted) cancel();
    });
    // One bounded retry per read. Mutations never enter this path.
    return readAttempt(run, signal);
  }
}
export interface AgentTaskUnchanged extends Partial<Pick<AgentTask, "run_timing" | "budget" | "allowed_actions" | "run_state" | "streaming">> {
  id: string;
  revision: number;
  unchanged: true;
}

export function mergeUnchangedTask(cached: AgentTask, value: AgentTask | AgentTaskUnchanged): AgentTask {
  if (!("unchanged" in value) || value.unchanged !== true || value.id !== cached.id || value.revision !== cached.revision) return value as AgentTask;
  const merged: AgentTask = { ...cached };
  for (const key of ["run_timing", "budget", "allowed_actions", "run_state", "streaming"] as const) {
    if (value[key] !== undefined) Object.assign(merged, { [key]: value[key] });
  }
  return merged;
}

/** Focus/selection reads may overlap a live poll at the same durable revision. */
export function mergeAgentTaskSnapshot(previous: AgentTask | undefined, incoming: AgentTask): AgentTask {
  if (!previous || previous.id !== incoming.id || previous.revision < incoming.revision) return incoming;
  if (previous.revision > incoming.revision) return previous;
  let next = incoming;
  const before = previous.streaming, after = incoming.streaming;
  // A completed/retired model step always advances the durable revision. Until
  // then live text only grows, even if an earlier GET finishes out of order.
  if (before && (!after || (before.step === after.step && before.context_epoch === after.context_epoch
      && before.text.startsWith(after.text)))) next = { ...next, streaming: before };
  const elapsed = previous.run_timing?.elapsed_seconds;
  if (typeof elapsed === "number" && incoming.run_timing && typeof incoming.run_timing.elapsed_seconds === "number"
      && elapsed > incoming.run_timing.elapsed_seconds) next = { ...next, run_timing: { ...incoming.run_timing, elapsed_seconds: elapsed } };
  return next;
}

async function request<T>(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  const send = async (requestSignal?: AbortSignal): Promise<T> => {
    const response = await fetch(nodeControlUrl(`/agent${path}`), {
      method, credentials: "include", cache: "no-store", signal: requestSignal,
      headers: { "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const code = typeof data.detail === "string" ? data.detail : typeof data.error === "string" ? data.error : "agent_request_failed";
      throw new AgentAPIError(code, response.status);
    }
    return response.json() as Promise<T>;
  };
  return method === "GET" ? readWithRecovery(send, signal) : send(signal);
}
export function agentFileUrl(taskID: string, file: Pick<AgentFileArtifact, "id" | "sha256">): string {
  // The version in the URL makes every revision a distinct resource; nothing stale is reused.
  return nodeControlUrl(`/agent/tasks/${encodeURIComponent(taskID)}/files/${encodeURIComponent(file.id)}?v=${encodeURIComponent(file.sha256 || "")}`);
}
async function fileContent(taskID: string, file: Pick<AgentFileArtifact, "id" | "sha256" | "media_type">, signal?: AbortSignal): Promise<Blob> {
  return readWithRecovery(async requestSignal => {
    const response = await fetch(agentFileUrl(taskID, file), { credentials: "include", cache: "no-store", signal: requestSignal });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new AgentAPIError(typeof data.detail === "string" ? data.detail : "agent_request_failed", response.status);
    }
    // The listing's type, not a sniffed one, decides how the bytes are drawn.
    return new Blob([await response.arrayBuffer()], { type: file.media_type });
  }, signal);
}

/** Mutations are sent once. An uncertain response is reconciled by reading server state. */
export const rynAgent = {
  projects: (signal?: AbortSignal) => request<{ projects: AgentProject[]; selected_project_id?: string }>("/projects", "GET", undefined, signal),
  selectProject: (project_id: string) => request<{ selected_project_id: string }>("/projects/selection", "POST", { project_id }),
  loadProject: (path: string) => request<AgentProject>("/projects", "POST", { path }),
  renameProject: (id: string, name: string) => request<AgentProject>(`/projects/${encodeURIComponent(id)}`, "PATCH", { name }),
  removeProject: (id: string) => request<{ removed: boolean }>(`/projects/${encodeURIComponent(id)}`, "DELETE"),
  settings: (signal?: AbortSignal) => request<AgentSettings>("/settings", "GET", undefined, signal),
  updateSettings: (settings: Partial<AgentSettings>) => request<AgentSettings>("/settings", "PUT", settings),
  models: (signal?: AbortSignal) => request<{ models: AgentModel[] }>("/models", "GET", undefined, signal),
  tools: (signal?: AbortSignal) => request<{ tools: AgentTool[] }>("/tools", "GET", undefined, signal),
  tasks: (signal?: AbortSignal) => request<{ tasks: AgentTaskSummary[] }>("/tasks", "GET", undefined, signal),
  archivedTasks: (signal?: AbortSignal) => request<{ tasks: AgentTaskSummary[] }>("/tasks?archived=true", "GET", undefined, signal),
  record: (id: string, operation: AgentRecordOperation, body: Record<string, unknown>) => request<AgentTask | AgentDeleteResult>(`/tasks/${encodeURIComponent(id)}${operation === "delete" ? "" : `/${operation}`}`, operation === "delete" ? "DELETE" : "POST", body),
  renameTask: (id: string, title: string, expected_control_revision: number, client_request_id: string) => request<AgentTask>(`/tasks/${encodeURIComponent(id)}`, "PATCH", { title, expected_control_revision, client_request_id }),
  result: (taskID: string, resultID: string, offset = 0, signal?: AbortSignal) => request<AgentResultPage>(`/tasks/${encodeURIComponent(taskID)}/results/${encodeURIComponent(resultID)}?offset=${offset}&limit=8192`, "GET", undefined, signal),
  resources: (signal?: AbortSignal) => request<AgentResourceCatalog>("/resources", "GET", undefined, signal),
  scope: (id: string, signal?: AbortSignal) => request<AgentScopeSnapshot>(`/tasks/${encodeURIComponent(id)}/scope`, "GET", undefined, signal),
  artifacts: (id: string, signal?: AbortSignal) => request<AgentArtifactListing>(`/tasks/${encodeURIComponent(id)}/artifacts`, "GET", undefined, signal),
  fileContent,
  openFile: (id: string, file: string, action: "open" | "reveal") => request<{ opened: boolean }>(`/tasks/${encodeURIComponent(id)}/files/${encodeURIComponent(file)}/actions`, "POST", { action }),
  artifact: (id: string, operation: string, signal?: AbortSignal) => request<AgentArtifact & { text: string }>(`/tasks/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(operation)}`, "GET", undefined, signal),
  task: (id: string, signal?: AbortSignal) => request<AgentTask>(`/tasks/${encodeURIComponent(id)}`, "GET", undefined, signal),
  // Polling with a cached copy: an unchanged task returns only its live fields.
  taskSince: (cached: AgentTask, signal?: AbortSignal) => request<AgentTask | AgentTaskUnchanged>(
    `/tasks/${encodeURIComponent(cached.id)}?since_revision=${cached.revision}`, "GET", undefined, signal)
    .then(value => mergeUnchangedTask(cached, value)),
  create: (message: string, model_id: string, client_request_id: string, scope?: AgentScopeSelection, expected_destination_revision?: string, skill_ids?: string[], response_language?: "zh-CN" | "en", project_id?: string, reasoning_effort?: string, attachments?: AgentAttachmentInput[]) => request<AgentTask>("/tasks", "POST", { message, model_id, client_request_id, ...(attachments?.length ? { attachments } : {}), ...(project_id ? { project_id } : {}), ...(response_language ? { response_language } : {}), ...(scope ? { scope, expected_destination_revision } : {}), ...(skill_ids?.length ? { skill_ids } : {}), ...(reasoning_effort ? { reasoning_effort } : {}) }),
  control: (id: string, route: "messages" | "actions" | "scope" | "workspace", body: Record<string, unknown>) => request<AgentTask>(`/tasks/${encodeURIComponent(id)}/${route}`, route === "scope" ? "PUT" : "POST", body),
  message: (task: AgentTask, message: string, intent: AgentMessageIntent = "information", client_request_id: string = crypto.randomUUID(), reasoning_effort?: string, attachments?: AgentAttachmentInput[]) => request<AgentTask>(`/tasks/${encodeURIComponent(task.id)}/messages`, "POST", task.allowed_actions ? {
    message, intent, expected_goal_revision: task.goal?.revision, expected_control_revision: task.control_revision, client_request_id, ...(reasoning_effort ? { reasoning_effort } : {}), ...(attachments?.length ? { attachments } : {}),
  } : { message, expected_revision: task.revision, ...(reasoning_effort ? { reasoning_effort } : {}), ...(attachments?.length ? { attachments } : {}) }),
  action: (task: AgentTask, action: AgentAction) => request<AgentTask>(`/tasks/${encodeURIComponent(task.id)}/actions`, "POST", {
    action, ...(task.allowed_actions ? { expected_control_revision: task.control_revision, client_request_id: crypto.randomUUID() } : { expected_revision: task.revision }),
    ...((action === "approve" || action === "deny") && task.pending_approval ? { approval_id: task.pending_approval.id } : {}),
  }),
  answer: (task: AgentTask, answers: Record<string, string>, client_request_id: string) => request<AgentTask>(`/tasks/${encodeURIComponent(task.id)}/actions`, "POST", {
    action: "answer", pending_input_id: task.pending_input?.id, expected_goal_revision: task.pending_input?.goal_revision,
    expected_control_revision: task.control_revision, answers, client_request_id,
  }),
  sourceCredential: (id: string, body: Record<string, unknown>) => request<{ credential: { has_credential: boolean; base_url: string; source_id: string }; task: AgentTask }>(`/tasks/${encodeURIComponent(id)}/source-credential`, "POST", body),
  skills: () => request<{ skills: AgentSkill[] }>("/skills"),
  preferences: () => request<AgentPreferences>("/preferences"),
  savePreferences: (body: { expected_revision: number; items: AgentPreferences["items"] }) => request<AgentPreferences>("/preferences", "PUT", body),
  updateBudget: (id: string, body: { limits: AgentBudgetLimits; run_max_seconds?: number; expected_control_revision: number; client_request_id: string }) => request<AgentTask>(`/tasks/${encodeURIComponent(id)}/budget`, "PUT", body),
  previewModel: (id: string, modelID: string, signal?: AbortSignal) => request<AgentModelSwitchPreview>(`/tasks/${encodeURIComponent(id)}/model?model_id=${encodeURIComponent(modelID)}`, "GET", undefined, signal),
  switchModel: (id: string, body: AgentModelSwitchRequest) => request<AgentTask>(`/tasks/${encodeURIComponent(id)}/model`, "PUT", body),
  automations: (signal?: AbortSignal) => request<AgentAutomationsSnapshot>("/automations", "GET", undefined, signal),
  createAutomation: (body: Record<string, unknown>) => request<{ automation: AgentAutomation }>("/automations", "POST", body),
  updateAutomation: (id: string, body: Record<string, unknown>) => request<{ automation: AgentAutomation }>(`/automations/${encodeURIComponent(id)}`, "PATCH", body),
  deleteAutomation: (id: string) => request<{ deleted: boolean; id: string }>(`/automations/${encodeURIComponent(id)}`, "DELETE"),
  runAutomationNow: (id: string) => request<{ triggered: boolean; deferred: boolean; task_id?: string; automation: AgentAutomation }>(`/automations/${encodeURIComponent(id)}/run-now`, "POST", {}),
};

export function agentMessageBody(task: AgentTask, message: string, intent: AgentMessageIntent, reasoning_effort?: string, attachments?: AgentAttachmentInput[]): Record<string, unknown> {
  return { message, intent, expected_goal_revision: task.goal?.revision, expected_control_revision: task.control_revision, client_request_id: crypto.randomUUID(), ...(reasoning_effort ? { reasoning_effort } : {}), ...(attachments?.length ? { attachments } : {}) };
}
export function agentActionBody(task: AgentTask, action: AgentAction): Record<string, unknown> {
  return { action, expected_control_revision: task.control_revision, client_request_id: crypto.randomUUID(),
    ...((action === "approve" || action === "deny") && task.pending_approval ? { approval_id: task.pending_approval.id } : {}) };
}
export function agentAnswerBody(task: AgentTask, answers: Record<string, string>): Record<string, unknown> {
  return { action: "answer", pending_input_id: task.pending_input?.id, expected_goal_revision: task.pending_input?.goal_revision,
    expected_control_revision: task.control_revision, answers, client_request_id: crypto.randomUUID() };
}
export function agentScopeBody(snapshot: AgentScopeSnapshot, scope: AgentScopeSelection): Record<string, unknown> {
  return { scope, expected_control_revision: snapshot.control_revision, expected_scope_revision: snapshot.scope_revision, client_request_id: crypto.randomUUID(), ...(scope.resources.length ? { expected_destination_revision: snapshot.current_model_destination?.destination_revision } : {}) };
}

export function canContinueAgentTask(task: AgentTaskSummary): boolean {
  if (task.allowed_actions) return canUseAgentAction(task, "information");
  return ["completed", "failed", "paused"].includes(task.status) && !task.pending_approval && !task.pending_input;
}
export function isAgentActive(status: AgentStatus): boolean {
  return ["queued", "running", "waiting_approval", "waiting_input", "pausing", "reconciling"].includes(status);
}

export function canUseAgentModel(model: AgentModel): boolean {
  return model.available && model.agent_supported === true && !(model.source === "peer" && model.destination_binding_supported === false);
}

export function canResumeAgentTask(task: AgentTaskSummary): boolean {
  if (task.allowed_actions) return canUseAgentAction(task, "resume");
  return task.status === "paused" || (task.status === "interrupted" && task.error === "agent_node_restarted");
}

/** New controls require an explicit server grant and its own version, never a text inference. */
export function canUseAgentAction(task: Pick<AgentTaskSummary, "allowed_actions" | "control_revision" | "goal">, action: string, legacyFallback = false): boolean {
  if (!task.allowed_actions) return legacyFallback;
  if (typeof task.control_revision !== "number") return false;
  if (["steer", "information"].includes(action) && !task.goal) return false;
  return task.allowed_actions[action]?.enabled === true;
}

export function agentTaskState(task: AgentTaskSummary): AgentStatus {
  return task.run_state && task.run_state !== "ended" ? task.run_state : task.status;
}

export function sortAgentTasks<T extends AgentTaskSummary>(tasks: T[]): T[] {
  const timestamp = (value: number | string) => typeof value === "number" ? value : Date.parse(value) || 0;
  return [...tasks].sort((left, right) => timestamp(right.updated_at) - timestamp(left.updated_at) || timestamp(right.created_at) - timestamp(left.created_at));
}

/** A finalized assistant event replaces its streamed draft; snapshots never append twice. */
export function pendingAgentText(task: Pick<AgentTask, "events" | "streaming">): string {
  const started = [...task.events].reverse().find(event => event.type === "model_started");
  if (!started || task.events.some(event => event.seq > started.seq && (["assistant", "message_edited"].includes(event.type) || (event.type === "paused" && event.reason === "owner_stopped")))) return "";
  if (task.streaming && task.streaming.step === started.step && task.streaming.context_epoch === started.context_epoch) return task.streaming.text;
  return task.events.filter(event => event.seq > started.seq && event.type === "text_delta" && typeof event.text === "string").map(event => event.text).join("");
}

export function agentTurnToolCount(task: Pick<AgentTask, "events">): number {
  const turnStart = [...task.events].reverse().find(event => ["created", "message_added", "goal_changed", "information_added", "input_answered", "message_edited"].includes(event.type))?.seq || 0;
  return task.events.filter(event => event.seq > turnStart && event.type === "tool_result" && event.internal !== true && agentToolResultState(event) !== "not_executed").length;
}

/** Receipt fields are server evidence, not a guess based on model prose. */
export function agentToolResultState(event: AgentEvent): "not_executed" | "error" | "returned" {
  const result = event.result && typeof event.result === "object" ? event.result as Record<string, unknown> : {};
  return result.not_executed === true ? "not_executed" : result.error ? "error" : "returned";
}

/** Attribute owner-visible replies using runtime epochs, never the task's new model alone. */
export function agentMessageModelID(task: Pick<AgentTask, "model_id" | "events">, message: AgentMessage): string | null {
  const switches = task.events.filter(event => event.type === "model_changed").sort((a, b) => a.seq - b.seq);
  if (!switches.length) return task.model_id;
  const epoch = message.original_context_epoch ?? message.context_epoch;
  if (typeof epoch !== "number") return null;
  for (const event of switches) {
    const previous = event.previous as { model_id?: unknown; context_epoch?: unknown } | undefined;
    if (typeof previous?.context_epoch === "number" && epoch <= previous.context_epoch)
      return typeof previous.model_id === "string" ? previous.model_id : null;
  }
  const last = switches[switches.length - 1];
  const destination = last.destination as { catalog_model_id?: unknown } | undefined;
  return typeof last.context_epoch === "number" && epoch >= last.context_epoch && typeof destination?.catalog_model_id === "string" ? destination.catalog_model_id : null;
}

export function isAgentLateToolResult(event: AgentEvent, events: AgentEvent[]): boolean {
  return event.type === "tool_result" && events.some(previous => previous.type === "cancelled" && previous.seq < event.seq);
}

export interface AgentAutomation {
  id: string;
  name: string;
  prompt: string;
  status: "ACTIVE" | "PAUSED";
  rrule: string;
  kind: "cron" | "heartbeat";
  repeat: "daily" | "weekdays" | "weekly" | "hourly" | "minutely" | "once";
  time: string;
  weekday: string;
  weekdays: string[];
  interval_hours: number;
  interval_minutes?: number;
  schedule_label: string;
  schedule_label_zh: string;
  schedule_label_en: string;
  desktop_access: boolean;
  workspace_root: string;
  target_task_id: string;
  execution_environment: string;
  notification_policy?: "all_runs" | "failed_runs_only" | "never";
  auto_archive?: boolean;
  next_run_at: number | null;
  next_run_at_iso: string;
  last_run_at: number | null;
  last_run_at_iso: string;
  last_run_status: string;
  last_run_summary: string;
  last_task_id: string;
  run_count: number;
  memory_path: string;
  created_at: string;
  updated_at: string;
}

export interface AgentAutomationRun {
  id: string;
  automation_id: string;
  automation_name: string;
  task_id: string;
  status: string;
  kind?: "cron" | "heartbeat";
  decision?: string;
  inbox_title: string;
  inbox_summary: string;
  created_at: string;
  updated_at: string;
  completed_at?: string;
}

export interface AgentAutomationsSnapshot {
  items: AgentAutomation[];
  runs: AgentAutomationRun[];
}
