import { nodeControlUrl } from "./nodeUrl";

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
export interface AgentSettings {
  enabled: boolean;
  model_id: string;
  permission_mode: "read_only" | "confirm_changes" | "automatic";
  allowed_write_tools: string[];
  max_steps: number;
  max_seconds: number;
  max_context_chars: number;
  max_output_chars: number;
}
export interface AgentModel {
  id: string;
  name: string;
  source: string;
  available: boolean;
  agent_supported?: boolean;
  tool_support?: "supported" | "unknown" | "unsupported";
  description?: string;
  destination_revision?: string | null;
  source_id?: string;
  source_name?: string;
  peer_id?: string;
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
  role: string;
  context_epoch?: number;
  content: string | null;
  tool_call_id?: string;
  tool_calls?: Array<{ id: string; function: { name: string; arguments: string } }>;
  ui?: { kind: "input_answer" | "source_credential" | "verification_feedback" | "steer" | "information" | "message"; text?: string; prompt?: string; pending_input_id?: string; fields?: Array<{ label: string; value: string }>; endpoint?: string; has_credential?: boolean };
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
  budget?: AgentBudget;
  run_timing?: AgentRunTiming;
  skills?: AgentSkill[];
}
export interface AgentResultRef { id: string; task_id: string; tool: string; operation_id: string; goal_revision: number; scope_revision: number; context_epoch: number; sha256: string; bytes: number; }
export interface AgentResultPage { id: string; sha256: string; bytes: number; characters: number; offset: number; next_offset: number | null; content: string; }
export interface AgentArtifact { id: string; name: string; bytes: number; sha256: string; created_at?: string; }
export type AgentRecordOperation = "archive" | "restore" | "delete";
export interface AgentDeleteResult { id: string; deleted: true; cleanup_pending: boolean; }
export interface AgentTask extends AgentTaskSummary {
  messages: AgentMessage[];
  events: AgentEvent[];
  limits: Partial<AgentSettings>;
}
export type AgentAction = "pause" | "resume" | "cancel" | "approve" | "deny";

export class AgentAPIError extends Error {
  constructor(public code: string, public status: number) { super(code); }
}
async function request<T>(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(nodeControlUrl(`/agent${path}`), {
    method, credentials: "include", cache: "no-store", signal,
    headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const code = typeof data.detail === "string" ? data.detail : typeof data.error === "string" ? data.error : "agent_request_failed";
    throw new AgentAPIError(code, response.status);
  }
  return response.json() as Promise<T>;
}

/** Mutations are sent once. An uncertain response is reconciled by reading server state. */
export const rynAgent = {
  settings: (signal?: AbortSignal) => request<AgentSettings>("/settings", "GET", undefined, signal),
  updateSettings: (settings: Partial<AgentSettings>) => request<AgentSettings>("/settings", "PUT", settings),
  models: (signal?: AbortSignal) => request<{ models: AgentModel[] }>("/models", "GET", undefined, signal),
  tools: (signal?: AbortSignal) => request<{ tools: AgentTool[] }>("/tools", "GET", undefined, signal),
  tasks: (signal?: AbortSignal) => request<{ tasks: AgentTaskSummary[] }>("/tasks", "GET", undefined, signal),
  archivedTasks: (signal?: AbortSignal) => request<{ tasks: AgentTaskSummary[] }>("/tasks?archived=true", "GET", undefined, signal),
  record: (id: string, operation: AgentRecordOperation, body: Record<string, unknown>) => request<AgentTask | AgentDeleteResult>(`/tasks/${encodeURIComponent(id)}${operation === "delete" ? "" : `/${operation}`}`, operation === "delete" ? "DELETE" : "POST", body),
  result: (taskID: string, resultID: string, offset = 0, signal?: AbortSignal) => request<AgentResultPage>(`/tasks/${encodeURIComponent(taskID)}/results/${encodeURIComponent(resultID)}?offset=${offset}&limit=8192`, "GET", undefined, signal),
  resources: (signal?: AbortSignal) => request<AgentResourceCatalog>("/resources", "GET", undefined, signal),
  scope: (id: string, signal?: AbortSignal) => request<AgentScopeSnapshot>(`/tasks/${encodeURIComponent(id)}/scope`, "GET", undefined, signal),
  artifacts: (id: string, signal?: AbortSignal) => request<{ artifacts: AgentArtifact[] }>(`/tasks/${encodeURIComponent(id)}/artifacts`, "GET", undefined, signal),
  artifact: (id: string, operation: string, signal?: AbortSignal) => request<AgentArtifact & { text: string }>(`/tasks/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(operation)}`, "GET", undefined, signal),
  task: (id: string, signal?: AbortSignal) => request<AgentTask>(`/tasks/${encodeURIComponent(id)}`, "GET", undefined, signal),
  create: (message: string, model_id: string, client_request_id: string, scope?: AgentScopeSelection, expected_destination_revision?: string, skill_ids?: string[]) => request<AgentTask>("/tasks", "POST", { message, model_id, client_request_id, ...(scope ? { scope, expected_destination_revision } : {}), ...(skill_ids?.length ? { skill_ids } : {}) }),
  control: (id: string, route: "messages" | "actions" | "scope", body: Record<string, unknown>) => request<AgentTask>(`/tasks/${encodeURIComponent(id)}/${route}`, route === "scope" ? "PUT" : "POST", body),
  message: (task: AgentTask, message: string, intent: AgentMessageIntent = "information", client_request_id = crypto.randomUUID()) => request<AgentTask>(`/tasks/${encodeURIComponent(task.id)}/messages`, "POST", task.allowed_actions ? {
    message, intent, expected_goal_revision: task.goal?.revision, expected_control_revision: task.control_revision, client_request_id,
  } : { message, expected_revision: task.revision }),
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
  updateBudget: (id: string, body: { limits: AgentBudgetLimits; expected_control_revision: number; client_request_id: string }) => request<AgentTask>(`/tasks/${encodeURIComponent(id)}/budget`, "PUT", body),
  previewModel: (id: string, modelID: string, signal?: AbortSignal) => request<AgentModelSwitchPreview>(`/tasks/${encodeURIComponent(id)}/model?model_id=${encodeURIComponent(modelID)}`, "GET", undefined, signal),
  switchModel: (id: string, body: AgentModelSwitchRequest) => request<AgentTask>(`/tasks/${encodeURIComponent(id)}/model`, "PUT", body),
};

export function agentMessageBody(task: AgentTask, message: string, intent: AgentMessageIntent): Record<string, unknown> {
  return { message, intent, expected_goal_revision: task.goal?.revision, expected_control_revision: task.control_revision, client_request_id: crypto.randomUUID() };
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
  return model.available && model.agent_supported === true;
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
export function pendingAgentText(task: Pick<AgentTask, "events">): string {
  const started = [...task.events].reverse().find(event => event.type === "model_started");
  if (!started || task.events.some(event => event.seq > started.seq && event.type === "assistant")) return "";
  return task.events.filter(event => event.seq > started.seq && event.type === "text_delta" && typeof event.text === "string").map(event => event.text).join("");
}

export function agentTurnToolCount(task: Pick<AgentTask, "events">): number {
  const turnStart = [...task.events].reverse().find(event => ["created", "message_added", "goal_changed", "information_added", "input_answered"].includes(event.type))?.seq || 0;
  return task.events.filter(event => event.seq > turnStart && event.type === "tool_result" && event.internal !== true && agentToolResultState(event) !== "not_executed").length;
}

/** Receipt fields are server evidence, not a guess based on model prose. */
export function agentToolResultState(event: AgentEvent): "not_executed" | "error" | "returned" {
  const result = event.result && typeof event.result === "object" ? event.result as Record<string, unknown> : {};
  return result.not_executed === true ? "not_executed" : result.error ? "error" : "returned";
}

/** Attribute owner-visible replies using runtime epochs, never the task's new model alone. */
export function agentMessageModelID(task: AgentTask, message: AgentMessage): string | null {
  const switches = task.events.filter(event => event.type === "model_changed").sort((a, b) => a.seq - b.seq);
  if (!switches.length) return task.model_id;
  if (typeof message.context_epoch !== "number") return null;
  for (const event of switches) {
    const previous = event.previous as { model_id?: unknown; context_epoch?: unknown } | undefined;
    if (typeof previous?.context_epoch === "number" && message.context_epoch <= previous.context_epoch)
      return typeof previous.model_id === "string" ? previous.model_id : null;
  }
  const last = switches[switches.length - 1];
  const destination = last.destination as { catalog_model_id?: unknown } | undefined;
  return typeof last.context_epoch === "number" && message.context_epoch >= last.context_epoch && typeof destination?.catalog_model_id === "string" ? destination.catalog_model_id : null;
}

export function isAgentLateToolResult(event: AgentEvent, events: AgentEvent[]): boolean {
  return event.type === "tool_result" && events.some(previous => previous.type === "cancelled" && previous.seq < event.seq);
}
