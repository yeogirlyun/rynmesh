import { nodeControlUrl } from "./nodeUrl";
import { AgentAPIError, isTransientAgentReadError } from "./rynAgent";

export type AgentMemoryScopeKind = "personal" | "project";
export type AgentMemoryExposureMode = "local_only" | "destinations" | "all_selected";
export type AgentMemoryDefaultExposureMode = "destination" | "selected_models" | "restricted";
export type AgentMemoryActivationPolicy = "owner_review" | "automatic";

export interface AgentMemoryDestinationTuple {
  catalog_model_id: string;
  provider_peer_id: string;
  source_id: string;
  destination_revision: string;
}

export interface AgentMemoryDestinationOption {
  label: string;
  destination: AgentMemoryDestinationTuple;
  available: boolean;
}

export interface AgentMemoryDestinationsResponse {
  destinations: AgentMemoryDestinationOption[];
  default_destination: AgentMemoryDestinationTuple | null;
}

export interface AgentMemoryScope {
  kind: AgentMemoryScopeKind;
  owner_id: string;
  project_id: string | null;
  project_generation: number | null;
}

export interface AgentMemoryScopeInput {
  kind: AgentMemoryScopeKind;
  project_id: string | null;
}

export interface AgentMemoryExposurePolicy {
  mode: AgentMemoryExposureMode;
  allowed_destinations: AgentMemoryDestinationTuple[];
  revision?: number;
}

export interface AgentMemoryExposurePolicyInput {
  mode: AgentMemoryExposureMode;
  allowed_destinations: AgentMemoryDestinationTuple[];
}

export interface AgentMemoryDefaultExposure {
  mode: AgentMemoryDefaultExposureMode;
  allowed_destinations: AgentMemoryDestinationTuple[];
}

export interface AgentMemorySourceRecord {
  source_type: "manual_owner" | "task_message";
  task_id: string | null;
  message_id: string | null;
  conversation_revision?: number | null;
  context_epoch?: number | null;
  source_revision?: number | null;
  content_sha256?: string;
  author_role?: "owner" | string;
  project_id: string | null;
  excerpt: string;
  [key: string]: unknown;
}

export interface AgentMemorySettings {
  capture_enabled: boolean;
  use_enabled: boolean;
  activation_policy: AgentMemoryActivationPolicy;
  extraction_model_id: string;
  extraction_destination: AgentMemoryDestinationTuple | null;
  default_exposure: AgentMemoryDefaultExposure;
  budgets?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface AgentMemoryEntry {
  id: string;
  scope: AgentMemoryScope;
  category: string;
  fact_key: string;
  text: string;
  kind: string;
  status: "active";
  revision: number;
  origin: string;
  sources: AgentMemorySourceRecord[];
  supersedes: string[];
  superseded_by: string | null;
  exposure_policy: AgentMemoryExposurePolicy;
  created_at: number | string | null;
  updated_at: number | string | null;
  last_used_at: number | string | null;
  reviewed_by_owner_at: number | string | null;
  [key: string]: unknown;
}

export interface AgentMemoryCandidate {
  id: string;
  scope: AgentMemoryScope;
  category: string;
  fact_key: string;
  text: string;
  kind: string;
  status: "pending";
  revision: number;
  origin: "background_extraction" | string;
  job_id: string | null;
  reset_epoch: number;
  sources: AgentMemorySourceRecord[];
  target_entry_id: string | null;
  expected_entry_revision: number | null;
  exposure_policy: AgentMemoryExposurePolicy;
  created_at: number | string | null;
  updated_at: number | string | null;
  [key: string]: unknown;
}

export interface AgentMemorySnapshot {
  schema_version: number;
  generation: number;
  reset_epoch: number;
  policy_revision: number;
  settings: AgentMemorySettings;
  entries: AgentMemoryEntry[];
  candidates: AgentMemoryCandidate[];
  tombstone_count: number;
  [key: string]: unknown;
}

export interface AgentMemoryQuery {
  scope_kind?: AgentMemoryScopeKind;
  project_id?: string;
  status?: string;
}

export interface AgentMemorySettingsPatch {
  expected_generation: number;
  client_request_id: string;
  capture_enabled?: boolean;
  use_enabled?: boolean;
  activation_policy?: AgentMemoryActivationPolicy;
  extraction_destination?: AgentMemoryDestinationTuple | null;
  default_exposure?: AgentMemoryDefaultExposure;
  budgets?: Record<string, unknown>;
}

export interface AgentMemoryCreateEntryInput {
  expected_generation: number;
  client_request_id: string;
  text: string;
  category?: string;
  fact_key?: string;
  kind?: string;
  scope: AgentMemoryScopeInput;
  exposure_policy: AgentMemoryExposurePolicyInput;
}

export interface AgentMemoryUpdateEntryInput {
  expected_generation: number;
  expected_entry_revision: number;
  client_request_id: string;
  text: string;
  category?: string;
  fact_key?: string;
  kind?: string;
  scope: AgentMemoryScopeInput;
  exposure_policy: AgentMemoryExposurePolicyInput;
}

export interface AgentMemoryDeleteEntryInput {
  expected_generation: number;
  expected_entry_revision: number;
  client_request_id: string;
}

export interface AgentMemoryCandidateDecisionInput {
  expected_generation: number;
  expected_candidate_revision: number;
  client_request_id: string;
  decision: "accept" | "reject" | "edit_and_accept";
  edited_text?: string;
  category?: string;
  fact_key?: string;
  scope?: AgentMemoryScopeInput;
  exposure_policy?: AgentMemoryExposurePolicyInput;
}

export interface AgentMemorySearchQuery {
  q?: string;
  scope_kind?: AgentMemoryScopeKind;
  project_id?: string;
  category?: string;
  limit?: number;
}

export interface AgentMemorySearchResponse {
  entries: AgentMemoryEntry[];
  total: number;
  generation: number;
  reset_epoch: number;
  policy_revision: number;
}

export interface AgentMemoryResetInput {
  expected_generation: number;
  client_request_id: string;
  scope: AgentMemoryScopeInput | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function invalidContract(detail: string): never {
  throw new AgentAPIError(detail, 502);
}

export function sameDestinationTuple(
  left: AgentMemoryDestinationTuple | null | undefined,
  right: AgentMemoryDestinationTuple | null | undefined,
): boolean {
  if (!left || !right) return false;
  return (
    left.catalog_model_id === right.catalog_model_id &&
    left.provider_peer_id === right.provider_peer_id &&
    left.source_id === right.source_id &&
    left.destination_revision === right.destination_revision
  );
}

export function destinationTupleKey(tuple: AgentMemoryDestinationTuple): string {
  return JSON.stringify([
    tuple.catalog_model_id,
    tuple.provider_peer_id,
    tuple.source_id,
    tuple.destination_revision,
  ]);
}

export function validateDestinationTuple(raw: unknown): AgentMemoryDestinationTuple {
  if (!isRecord(raw)) invalidContract("agent_memory_invalid_destination");
  const { catalog_model_id, provider_peer_id, source_id, destination_revision } = raw;
  // source_id must be a string (may be "" for native CLI destinations); catalog_model_id, provider_peer_id, and destination_revision must be non-empty strings.
  if (
    typeof catalog_model_id !== "string" || !catalog_model_id.trim() ||
    typeof provider_peer_id !== "string" || !provider_peer_id.trim() ||
    typeof source_id !== "string" ||
    typeof destination_revision !== "string" || !destination_revision.trim()
  ) {
    invalidContract("agent_memory_invalid_destination");
  }
  return {
    catalog_model_id,
    provider_peer_id,
    source_id,
    destination_revision,
  };
}

export function validateMemoryScope(raw: unknown): AgentMemoryScope {
  if (!isRecord(raw)) invalidContract("agent_memory_invalid_scope");
  const kind = raw.kind;
  if (kind !== "personal" && kind !== "project") {
    invalidContract("agent_memory_invalid_scope");
  }
  const owner_id = typeof raw.owner_id === "string" && raw.owner_id ? raw.owner_id : "local_owner";
  const project_id = raw.project_id === null || raw.project_id === undefined ? null : typeof raw.project_id === "string" ? raw.project_id : invalidContract("agent_memory_invalid_scope");
  if (kind === "project" && (!project_id || !project_id.trim())) {
    invalidContract("agent_memory_invalid_scope_project");
  }
  if (kind === "personal" && project_id !== null) {
    invalidContract("agent_memory_invalid_scope_personal");
  }
  const project_generation = raw.project_generation === null || raw.project_generation === undefined
    ? null
    : typeof raw.project_generation === "number" && Number.isInteger(raw.project_generation)
      ? raw.project_generation
      : invalidContract("agent_memory_invalid_scope_generation");
  return {
    kind,
    owner_id,
    project_id,
    project_generation,
  };
}

export function validateExposurePolicy(raw: unknown): AgentMemoryExposurePolicy {
  if (!isRecord(raw)) invalidContract("agent_memory_invalid_exposure_policy");
  const mode = raw.mode;
  if (mode !== "local_only" && mode !== "destinations" && mode !== "all_selected") {
    invalidContract("agent_memory_invalid_exposure_policy");
  }
  if (!Array.isArray(raw.allowed_destinations)) {
    invalidContract("agent_memory_invalid_exposure_policy");
  }
  const allowed_destinations = raw.allowed_destinations.map(validateDestinationTuple);
  const revision = typeof raw.revision === "number" ? raw.revision : undefined;
  return {
    mode,
    allowed_destinations,
    ...(revision !== undefined ? { revision } : {}),
  };
}

export function validateDefaultExposure(raw: unknown): AgentMemoryDefaultExposure {
  if (!isRecord(raw)) invalidContract("agent_memory_invalid_default_exposure");
  const mode = raw.mode;
  if (mode !== "destination" && mode !== "selected_models" && mode !== "restricted") {
    invalidContract("agent_memory_invalid_default_exposure");
  }
  if (!Array.isArray(raw.allowed_destinations)) {
    invalidContract("agent_memory_invalid_default_exposure");
  }
  const allowed_destinations = raw.allowed_destinations.map(validateDestinationTuple);
  return {
    mode,
    allowed_destinations,
  };
}

export function validateSourceRecord(raw: unknown): AgentMemorySourceRecord {
  if (!isRecord(raw)) invalidContract("agent_memory_invalid_source");
  const source_type = raw.source_type;
  if (source_type !== "manual_owner" && source_type !== "task_message") {
    invalidContract("agent_memory_invalid_source_type");
  }
  const task_id = raw.task_id === null || raw.task_id === undefined ? null : typeof raw.task_id === "string" ? raw.task_id : invalidContract("agent_memory_invalid_source");
  const message_id = raw.message_id === null || raw.message_id === undefined ? null : typeof raw.message_id === "string" ? raw.message_id : invalidContract("agent_memory_invalid_source");
  const project_id = raw.project_id === null || raw.project_id === undefined ? null : typeof raw.project_id === "string" ? raw.project_id : invalidContract("agent_memory_invalid_source");
  const excerpt = typeof raw.excerpt === "string" ? raw.excerpt : "";
  return {
    ...raw,
    source_type,
    task_id,
    message_id,
    project_id,
    excerpt,
  };
}

export function validateMemoryEntry(raw: unknown): AgentMemoryEntry {
  if (!isRecord(raw)) invalidContract("agent_memory_invalid_entry");
  if (typeof raw.id !== "string" || !raw.id) invalidContract("agent_memory_invalid_entry");
  if (typeof raw.text !== "string") invalidContract("agent_memory_invalid_entry");
  if (typeof raw.revision !== "number" || !Number.isInteger(raw.revision)) invalidContract("agent_memory_invalid_entry");
  if (raw.status !== "active") invalidContract("agent_memory_invalid_entry_status");
  if (!Array.isArray(raw.sources)) invalidContract("agent_memory_invalid_entry_sources");
  const scope = validateMemoryScope(raw.scope);
  const exposure_policy = validateExposurePolicy(raw.exposure_policy);
  const sources = raw.sources.map(validateSourceRecord);
  return {
    ...raw,
    id: raw.id,
    scope,
    category: typeof raw.category === "string" ? raw.category : "general",
    fact_key: typeof raw.fact_key === "string" ? raw.fact_key : "",
    text: raw.text,
    kind: typeof raw.kind === "string" ? raw.kind : "fact",
    status: "active",
    revision: raw.revision,
    origin: typeof raw.origin === "string" ? raw.origin : "manual_owner",
    sources,
    supersedes: Array.isArray(raw.supersedes) ? raw.supersedes.filter((id): id is string => typeof id === "string") : [],
    superseded_by: typeof raw.superseded_by === "string" ? raw.superseded_by : null,
    exposure_policy,
    created_at: (raw.created_at as number | string | null) ?? null,
    updated_at: (raw.updated_at as number | string | null) ?? null,
    last_used_at: (raw.last_used_at as number | string | null) ?? null,
    reviewed_by_owner_at: (raw.reviewed_by_owner_at as number | string | null) ?? null,
  };
}

export function validateMemoryCandidate(raw: unknown): AgentMemoryCandidate {
  if (!isRecord(raw)) invalidContract("agent_memory_invalid_candidate");
  if (typeof raw.id !== "string" || !raw.id) invalidContract("agent_memory_invalid_candidate");
  if (typeof raw.text !== "string") invalidContract("agent_memory_invalid_candidate");
  if (typeof raw.revision !== "number" || !Number.isInteger(raw.revision)) invalidContract("agent_memory_invalid_candidate");
  if (raw.status !== "pending") invalidContract("agent_memory_invalid_candidate_status");
  if (!Array.isArray(raw.sources)) invalidContract("agent_memory_invalid_candidate_sources");
  const scope = validateMemoryScope(raw.scope);
  const exposure_policy = validateExposurePolicy(raw.exposure_policy);
  const sources = raw.sources.map(validateSourceRecord);
  return {
    ...raw,
    id: raw.id,
    scope,
    category: typeof raw.category === "string" ? raw.category : "general",
    fact_key: typeof raw.fact_key === "string" ? raw.fact_key : "",
    text: raw.text,
    kind: typeof raw.kind === "string" ? raw.kind : "fact",
    status: "pending",
    revision: raw.revision,
    origin: typeof raw.origin === "string" ? raw.origin : "background_extraction",
    job_id: typeof raw.job_id === "string" ? raw.job_id : null,
    reset_epoch: typeof raw.reset_epoch === "number" ? raw.reset_epoch : 0,
    sources,
    target_entry_id: typeof raw.target_entry_id === "string" ? raw.target_entry_id : null,
    expected_entry_revision: typeof raw.expected_entry_revision === "number" ? raw.expected_entry_revision : null,
    exposure_policy,
    created_at: (raw.created_at as number | string | null) ?? null,
    updated_at: (raw.updated_at as number | string | null) ?? null,
  };
}

export function validateMemorySettings(raw: unknown): AgentMemorySettings {
  if (!isRecord(raw)) invalidContract("agent_memory_invalid_settings");
  if (typeof raw.capture_enabled !== "boolean" || typeof raw.use_enabled !== "boolean") {
    invalidContract("agent_memory_invalid_settings");
  }
  const activation_policy = raw.activation_policy;
  if (activation_policy !== "owner_review" && activation_policy !== "automatic") {
    invalidContract("agent_memory_invalid_activation_policy");
  }
  const extraction_destination = raw.extraction_destination === null || raw.extraction_destination === undefined
    ? null
    : validateDestinationTuple(raw.extraction_destination);
  const default_exposure = validateDefaultExposure(raw.default_exposure);
  return {
    ...raw,
    capture_enabled: raw.capture_enabled,
    use_enabled: raw.use_enabled,
    activation_policy,
    extraction_model_id: typeof raw.extraction_model_id === "string" ? raw.extraction_model_id : "",
    extraction_destination,
    default_exposure,
    ...(isRecord(raw.budgets) ? { budgets: raw.budgets } : {}),
  };
}

export function validateMemorySnapshot(raw: unknown): AgentMemorySnapshot {
  if (!isRecord(raw)) invalidContract("agent_memory_invalid_snapshot");
  if (typeof raw.generation !== "number" || !Number.isInteger(raw.generation) || raw.generation < 0) {
    invalidContract("agent_memory_invalid_generation");
  }
  if (!Array.isArray(raw.entries) || !Array.isArray(raw.candidates)) {
    invalidContract("agent_memory_invalid_snapshot_lists");
  }
  const settings = validateMemorySettings(raw.settings);
  const entries = raw.entries.filter(item => isRecord(item) && item.status === "active").map(validateMemoryEntry);
  for (const item of raw.entries) {
    if (!isRecord(item) || typeof item.status !== "string") invalidContract("agent_memory_invalid_entry");
  }
  const candidates = raw.candidates.filter(item => isRecord(item) && item.status === "pending").map(validateMemoryCandidate);
  for (const item of raw.candidates) {
    if (!isRecord(item) || typeof item.status !== "string") invalidContract("agent_memory_invalid_candidate");
  }
  return {
    ...raw,
    schema_version: typeof raw.schema_version === "number" ? raw.schema_version : 1,
    generation: raw.generation,
    reset_epoch: typeof raw.reset_epoch === "number" ? raw.reset_epoch : 0,
    policy_revision: typeof raw.policy_revision === "number" ? raw.policy_revision : 0,
    settings,
    entries,
    candidates,
    tombstone_count: typeof raw.tombstone_count === "number" ? raw.tombstone_count : 0,
  };
}

export function validateDestinationsResponse(raw: unknown): AgentMemoryDestinationsResponse {
  if (!isRecord(raw) || !Array.isArray(raw.destinations)) {
    invalidContract("agent_memory_invalid_destinations");
  }
  const destinations: AgentMemoryDestinationOption[] = raw.destinations.map(item => {
    if (!isRecord(item) || typeof item.label !== "string" || !item.label.trim() || typeof item.available !== "boolean") {
      invalidContract("agent_memory_invalid_destination_option");
    }
    return {
      label: item.label,
      destination: validateDestinationTuple(item.destination),
      available: item.available,
    };
  });
  const default_destination = raw.default_destination === null || raw.default_destination === undefined
    ? null
    : validateDestinationTuple(raw.default_destination);
  return { destinations, default_destination };
}

export function validateSearchResponse(raw: unknown): AgentMemorySearchResponse {
  if (!isRecord(raw) || !Array.isArray(raw.entries) || typeof raw.generation !== "number" || !Number.isInteger(raw.generation)) {
    invalidContract("agent_memory_invalid_search_response");
  }
  const entries = raw.entries.filter(item => isRecord(item) && item.status === "active").map(validateMemoryEntry);
  for (const item of raw.entries) {
    if (!isRecord(item) || typeof item.status !== "string") invalidContract("agent_memory_invalid_entry");
  }
  return {
    entries,
    total: typeof raw.total === "number" ? raw.total : entries.length,
    generation: raw.generation,
    reset_epoch: typeof raw.reset_epoch === "number" ? raw.reset_epoch : 0,
    policy_revision: typeof raw.policy_revision === "number" ? raw.policy_revision : 0,
  };
}

const READ_TIMEOUT_MS = 10000;
const READ_RETRY_DELAY_MS = 250;

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
    return readAttempt(run, signal);
  }
}

async function memoryRequest<T>(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  const send = async (requestSignal?: AbortSignal): Promise<T> => {
    const response = await fetch(nodeControlUrl(`/agent/memory${path}`), {
      method,
      credentials: "include",
      cache: "no-store",
      signal: requestSignal,
      headers: { "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const code = typeof data.detail === "string" ? data.detail : typeof data.error === "string" ? data.error : "agent_memory_request_failed";
      throw new AgentAPIError(code, response.status);
    }
    return response.json() as Promise<T>;
  };
  return method === "GET" ? readWithRecovery(send, signal) : send(signal);
}

/** Centralized typed API adapter for Ryn Agent long-term memory (api-contract-v1). */
export const rynAgentMemory = {
  get: async (query?: AgentMemoryQuery, signal?: AbortSignal): Promise<AgentMemorySnapshot> => {
    const params = new URLSearchParams();
    if (query?.scope_kind) params.set("scope_kind", query.scope_kind);
    if (query?.project_id) params.set("project_id", query.project_id);
    if (query?.status) params.set("status", query.status);
    const qs = params.toString();
    return validateMemorySnapshot(await memoryRequest<unknown>(qs ? `?${qs}` : "", "GET", undefined, signal));
  },

  destinations: async (signal?: AbortSignal): Promise<AgentMemoryDestinationsResponse> =>
    validateDestinationsResponse(await memoryRequest<unknown>("/destinations", "GET", undefined, signal)),

  updateSettings: async (body: AgentMemorySettingsPatch): Promise<AgentMemorySnapshot> =>
    validateMemorySnapshot(await memoryRequest<unknown>("/settings", "PUT", body)),

  createEntry: async (body: AgentMemoryCreateEntryInput): Promise<AgentMemorySnapshot> =>
    validateMemorySnapshot(await memoryRequest<unknown>("/entries", "POST", body)),

  updateEntry: async (id: string, body: AgentMemoryUpdateEntryInput): Promise<AgentMemorySnapshot> =>
    validateMemorySnapshot(await memoryRequest<unknown>(`/entries/${encodeURIComponent(id)}`, "PUT", body)),

  deleteEntry: async (id: string, body: AgentMemoryDeleteEntryInput): Promise<AgentMemorySnapshot> =>
    validateMemorySnapshot(await memoryRequest<unknown>(`/entries/${encodeURIComponent(id)}`, "DELETE", body)),

  decideCandidate: async (id: string, body: AgentMemoryCandidateDecisionInput): Promise<AgentMemorySnapshot> =>
    validateMemorySnapshot(await memoryRequest<unknown>(`/candidates/${encodeURIComponent(id)}/decision`, "POST", body)),

  search: async ({ q, scope_kind, project_id, category, limit }: AgentMemorySearchQuery, signal?: AbortSignal): Promise<AgentMemorySearchResponse> => {
    const params = new URLSearchParams();
    if (q !== undefined && q !== "") params.set("q", q);
    if (scope_kind) params.set("scope_kind", scope_kind);
    if (project_id) params.set("project_id", project_id);
    if (category) params.set("category", category);
    if (typeof limit === "number") params.set("limit", String(limit));
    const qs = params.toString();
    return validateSearchResponse(await memoryRequest<unknown>(`/search${qs ? `?${qs}` : ""}`, "GET", undefined, signal));
  },

  reset: async (body: AgentMemoryResetInput): Promise<AgentMemorySnapshot> =>
    validateMemorySnapshot(await memoryRequest<unknown>("/reset", "POST", body)),
};
