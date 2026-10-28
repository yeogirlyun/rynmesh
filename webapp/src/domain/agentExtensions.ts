import { nodeControlUrl } from "./nodeUrl";

export type ExtensionSourceFormat =
  | "builtin"
  | "ryn_plugin"
  | "codex_plugin"
  | "claude_plugin"
  | "skill_md"
  | "mcp_json"
  | "codex_toml"
  | string;

export type ExtensionMcpTransport = "stdio" | "streamable_http";
export type ExtensionMcpOauthClientRegistration = "auto" | "cimd" | "dcr";
export type ExtensionMcpStatus =
  | "disabled"
  | "ready"
  | "NeedsTest"
  | "NeedsAuth"
  | "NeedsPermission"
  | "Error"
  | "connected"
  | "error"
  | string;
export type ExtensionImportKind = "plugin" | "skill" | "mcp";

export const ENV_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;
export const HEADER_KEY_RE = /^[A-Za-z0-9-]{1,128}$/;

export interface ExtensionMaskedSecret {
  has_value: boolean;
  masked: string;
}

export interface ExtensionMcpToolAnnotations {
  readOnlyHint?: boolean;
  destructiveHint?: boolean;
  idempotentHint?: boolean;
  openWorldHint?: boolean;
}

export type ExtensionDataPolicyRule = { on_device: true } | { api_base_url_prefix: string } | { catalog_model_id: string };
export interface ExtensionDataPolicy {
  models: { mode: "any" } | { mode: "allowlist"; allow: ExtensionDataPolicyRule[] };
  memory: "allow" | "deny";
}
export interface ExtensionServerHintCandidate {
  name: string;
  title: string;
  schema_sha256: string;
}
export interface ExtensionServerHintsInput {
  expected_revision: number;
  instance_id: string;
  discovery_revision: number;
  tools: Array<{ name: string; schema_sha256: string }>;
}

export interface ExtensionMcpTool {
  name: string;
  model_tool_name?: string;
  title: string;
  description: string;
  enabled: boolean;
  component_enabled: boolean;
  owner_read_only: boolean;
  effective_effect: "read" | "write";
  effective_risk?: "low" | "high";
  annotations?: ExtensionMcpToolAnnotations;
  schema_sha256?: string;
  input_schema?: Record<string, unknown>;
}

export interface ExtensionMcpServer {
  auth?: { mode: "auto" | "oauth" | "none"; client_id: string | null; scopes: string[]; callback_port: number | null };
  oauth?: McpOauthStatus;
  data_policy?: ExtensionDataPolicy;
  discovery_revision?: number;
  read_only_sources?: Record<string, "owner" | "server_hint">;
  server_hint_candidates?: ExtensionServerHintCandidate[];
  id: string;
  instance_id?: string;
  revision: number;
  config_generation?: number;
  name: string;
  title?: string;
  description?: string;
  transport: ExtensionMcpTransport;
  builtin: boolean;
  deletable: boolean;
  plugin_id: string | null;
  computer_access: boolean;
  /** Effective state: true only when both component_enabled and parent_enabled are true. */
  enabled: boolean;
  /** Individual server enabled preference (retained even when parent plugin is disabled). */
  component_enabled: boolean;
  /** True when the server is standalone or its parent plugin is enabled. */
  parent_enabled: boolean;
  command: string;
  args: string[];
  cwd: string;
  env_keys: string[];
  masked_env: Record<string, ExtensionMaskedSecret>;
  env_vars?: string[];
  url: string;
  bearer_token_env: string;
  bearer_token_env_present?: boolean;
  header_keys: string[];
  masked_headers: Record<string, ExtensionMaskedSecret>;
  env_http_headers?: Record<string, string>;
  oauth_client_registration?: ExtensionMcpOauthClientRegistration;
  has_secrets?: boolean;
  startup_timeout_ms: number;
  tool_timeout_ms: number;
  /** null means unrestricted (all discovered tools allowed unless in disabled_tools); [] means explicit empty allowlist. */
  enabled_tools: string[] | null;
  disabled_tools: string[];
  read_only_tools: string[];
  tools: ExtensionMcpTool[];
  tools_count: number;
  enabled_tools_count?: number;
  status: ExtensionMcpStatus;
  status_message: string;
  last_tested_at: string | null;
  updated_at?: string | null;
}

export interface McpOauthStatus {
  state: "not_required" | "logged_out" | "pending" | "logged_in" | "error";
  expires_at?: number | null;
  scopes?: string[];
  issuer_host?: string | null;
  error?: string | null;
}

export interface McpOauthLogin {
  auth_id: string;
  authorization_url: string;
  expires_in: number;
}

export interface McpOauthProgress {
  state: "waiting" | "exchanging" | "connected" | "failed" | "cancelled" | "expired";
  error?: string;
}

export const startMcpOauthLogin = (id: string, expected_revision: number, language: string) =>
  extensionRequest<McpOauthLogin>(`/mcp/servers/${encodeURIComponent(id)}/oauth/login`, "POST", { expected_revision, language });
export const getMcpOauthLogin = (id: string, authId: string, signal?: AbortSignal) =>
  extensionRequest<McpOauthProgress>(`/mcp/servers/${encodeURIComponent(id)}/oauth/login/${encodeURIComponent(authId)}`, "GET", undefined, signal);
export const cancelMcpOauthLogin = (id: string, authId: string) =>
  extensionRequest<McpOauthProgress>(`/mcp/servers/${encodeURIComponent(id)}/oauth/login/${encodeURIComponent(authId)}/cancel`, "POST", {});
export const logoutMcpOauth = (id: string, expected_revision: number) =>
  extensionRequest<McpOauthStatus>(`/mcp/servers/${encodeURIComponent(id)}/oauth/logout`, "POST", { expected_revision });
export const clearLocalMcpAuthorizations = () =>
  extensionRequest<{ cleared: boolean }>("/mcp/oauth/clear-local-authorizations", "POST", { confirm: true });
export const applyMcpServerHints = async (id: string, input: ExtensionServerHintsInput) =>
  normalizeMcpServer(await extensionRequest<Record<string, unknown>>(`/mcp/servers/${encodeURIComponent(id)}/apply-server-hints`, "POST", input));

export interface ExtensionSkillFile {
  path: string;
  size: number;
}

export type ExtensionSkillScope = "user" | "repo" | "system" | "plugin" | "curated" | string;

export interface ExtensionSkillInterface {
  display_name?: string;
  short_description?: string;
  icon_small?: string | null;
  icon_large?: string | null;
  brand_color?: string | null;
  default_prompt?: string | null;
}

export interface ExtensionSkillPolicy {
  allow_implicit_invocation?: boolean;
}

export interface ExtensionSkill {
  id: string;
  instance_id?: string;
  revision: number;
  name: string;
  title: string;
  description: string;
  short_description?: string;
  summary?: string;
  version: string;
  install_generation?: number;
  content: string;
  manifest_sha256?: string | null;
  builtin: boolean;
  deletable: boolean;
  /** Effective state: true only when both component_enabled and parent_enabled are true. */
  enabled: boolean;
  /** Individual skill enabled preference (retained even when parent plugin is disabled). */
  component_enabled: boolean;
  /** True when the skill is standalone or its parent plugin is enabled. */
  parent_enabled: boolean;
  source_format: ExtensionSourceFormat;
  scope?: ExtensionSkillScope;
  path?: string;
  package_dir?: string;
  interface?: ExtensionSkillInterface;
  policy?: ExtensionSkillPolicy;
  plugin_id: string | null;
  files: ExtensionSkillFile[];
  warnings: string[];
  updated_at?: string | null;
}

export interface RecommendedSkill {
  id: string;
  name: string;
  title: string;
  description: string;
  short_description: string;
  summary?: string;
  version: string;
  repo_path: string;
  source_dir?: string | null;
  path: string;
  scope: ExtensionSkillScope;
  content: string;
  installed: boolean;
  interface?: ExtensionSkillInterface;
  policy?: ExtensionSkillPolicy;
}

export interface ExtensionPluginCapabilities {
  platform?: string;
  windows_supported: boolean;
  desktop_access_enabled?: boolean;
  observable: string[];
  controllable: string[];
  permission_note?: string;
}

export type ExtensionViewFormat = "text" | "number" | "money";
export interface ExtensionPluginView {
  id: string;
  title: string;
  description?: string;
  mcpServer: string;
  tool: string;
  inputs: Array<{ name: string; label: string; type: "text" | "date"; required?: boolean; default?: "today" | "month_start" }>;
  columns: Array<{ key: string; label: string; format?: ExtensionViewFormat }>;
  rowsPath: string;
  summaryFields: Array<{ path: string; label: string; format?: ExtensionViewFormat }>;
  pagination: { pageArgument: string; sizeArgument: string; path: string; pageSize: number };
}

export const queryPluginView = (plugin: ExtensionPlugin, viewId: string, args: Record<string, string>, page: number, signal?: AbortSignal) =>
  extensionRequest<{ view_id: string; result: Record<string, unknown> }>(
    `/plugins/${encodeURIComponent(plugin.id)}/views/${encodeURIComponent(viewId)}/query`, "POST",
    { arguments: args, page, expected_instance_id: plugin.instance_id, expected_revision: plugin.revision }, signal,
  );

export interface ExtensionPlugin {
  views?: ExtensionPluginView[];
  data_policy?: ExtensionDataPolicy;
  peer_provider?: { url: string; oauth: { clientId: string; scopes: string[] } } | null;
  id: string;
  instance_id?: string;
  revision: number;
  name: string;
  title: string;
  description: string;
  version: string;
  kind?: string;
  source_format: ExtensionSourceFormat;
  builtin: boolean;
  deletable: boolean;
  enabled: boolean;
  component_enabled: boolean;
  parent_enabled: boolean;
  status: string;
  status_reason: string;
  computer_access: boolean;
  skill_ids: string[];
  mcp_server_ids: string[];
  skills_count?: number;
  mcp_servers_count?: number;
  skills?: ExtensionSkill[];
  mcp_servers?: ExtensionMcpServer[];
  unsupported_components: string[];
  warnings: string[];
  capabilities?: ExtensionPluginCapabilities;
  updated_at?: string | null;
}

export interface ExtensionOverview {
  revision: number;
  updated_at?: string | null;
  desktop_access: boolean;
  windows_supported?: boolean;
  plugins: ExtensionPlugin[];
  skills: ExtensionSkill[];
  mcp_servers: ExtensionMcpServer[];
}

export interface ExtensionTestResult {
  status: "connected" | "error" | string;
  tools_count: number;
  error?: string | null;
}

export interface ExtensionMcpServerActionResponse {
  server: ExtensionMcpServer;
  test_result: ExtensionTestResult;
}

export interface ExtensionDeleteResponse {
  deleted: boolean | string;
  id?: string;
  removed_mcp_server_ids?: string[];
  revision?: number;
}

export interface ExtensionMcpServerCreateInput {
  auth?: ExtensionMcpServer["auth"];
  name: string;
  description?: string;
  transport: ExtensionMcpTransport;
  enabled?: boolean;
  command?: string;
  args?: string[];
  cwd?: string;
  env?: Record<string, string>;
  env_vars?: string[];
  url?: string;
  bearer_token_env?: string;
  headers?: Record<string, string>;
  env_http_headers?: Record<string, string>;
  oauth_client_registration?: ExtensionMcpOauthClientRegistration;
  startup_timeout_ms?: number;
  tool_timeout_ms?: number;
  computer_access?: boolean;
  enabled_tools?: string[] | null;
  disabled_tools?: string[];
  read_only_tools?: string[];
  expected_revision: number;
}

export interface ExtensionMcpServerUpdateInput {
  auth?: ExtensionMcpServer["auth"];
  name?: string;
  description?: string;
  transport?: ExtensionMcpTransport;
  enabled?: boolean;
  command?: string;
  args?: string[];
  cwd?: string;
  env?: Record<string, string | null>;
  env_vars?: string[];
  url?: string;
  bearer_token_env?: string;
  headers?: Record<string, string | null>;
  env_http_headers?: Record<string, string | null>;
  oauth_client_registration?: ExtensionMcpOauthClientRegistration;
  startup_timeout_ms?: number;
  tool_timeout_ms?: number;
  computer_access?: boolean;
  enabled_tools?: string[] | null;
  disabled_tools?: string[];
  read_only_tools?: string[];
  expected_revision: number;
}

export interface ExtensionImportPreviewRequest {
  kind: ExtensionImportKind;
  path?: string;
  config_text?: string;
  skill_text?: string;
  filename?: string;
  zip_base64?: string;
}

export interface ExtensionImportPreview {
  preview_id: string;
  name: string;
  version: string;
  source_format: ExtensionSourceFormat;
  plugins: Array<{
    data_policy?: ExtensionDataPolicy;
    id: string;
    name: string;
    title: string;
    description: string;
    version: string;
    source_format: string;
    skill_ids: string[];
    mcp_server_ids: string[];
  }>;
  skills: Array<{
    id: string;
    name: string;
    title: string;
    description: string;
    version: string;
    files?: ExtensionSkillFile[];
  }>;
  mcp_servers: Array<{
    data_policy?: ExtensionDataPolicy;
    id: string;
    name: string;
    title?: string;
    description?: string;
    transport: string;
    command?: string;
    args?: string[];
    url?: string;
    bearer_token_env?: string;
    env_keys?: string[];
    header_keys?: string[];
    has_secrets?: boolean;
    auth?: ExtensionMcpServer["auth"];
    login_required?: boolean;
    oauth_issuer?: string | null;
  }>;
  warnings: string[];
  unsupported_components: string[];
  can_import: boolean;
  expected_revision: number;
}

export class AgentExtensionError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, status: number) {
    super(code);
    this.name = "AgentExtensionError";
    this.code = code;
    this.status = status;
  }
}

export function isRevisionConflictError(error: unknown): boolean {
  if (error instanceof AgentExtensionError) {
    return error.status === 409 || error.code.includes("revision_conflict");
  }
  return false;
}

export interface SecretEntryDraft {
  id: string;
  key: string;
  value: string;
  savedKey: string | null;
  isSavedMasked: boolean;
}

export type SecretValidationErrorCode =
  | "duplicate_key"
  | "invalid_key"
  | "renamed_masked_key"
  | "empty_secret_value";

export interface SecretValidationError {
  code: SecretValidationErrorCode;
  key: string;
}

export const MASKED_SECRET_PLACEHOLDER = "••••••••";

export function createInitialSecretEntries(
  maskedRecord: Record<string, ExtensionMaskedSecret> | undefined,
  keysFallback: string[] = [],
): SecretEntryDraft[] {
  const keys = maskedRecord ? Object.keys(maskedRecord) : [];
  const allKeys = keys.length > 0 ? keys : keysFallback;
  return allKeys.map((key, index) => ({
    id: `saved-${index}-${key}`,
    key,
    value: "",
    savedKey: key,
    isSavedMasked: Boolean(
      maskedRecord && Object.hasOwn(maskedRecord, key) ? maskedRecord[key]?.has_value : true,
    ),
  }));
}

export function validateSecretEntries(
  entries: SecretEntryDraft[],
  kind: "env" | "header",
): SecretValidationError | null {
  const seen = new Set<string>();
  const keyPattern = kind === "env" ? ENV_KEY_RE : HEADER_KEY_RE;

  for (const entry of entries) {
    const trimmedKey = entry.key.trim();
    const hasValue = entry.value.length > 0;
    if (!trimmedKey && !hasValue && !entry.savedKey) {
      continue;
    }
    if (!trimmedKey) {
      return { code: "invalid_key", key: "" };
    }
    if (!keyPattern.test(trimmedKey)) {
      return { code: "invalid_key", key: trimmedKey };
    }
    if (seen.has(trimmedKey)) {
      return { code: "duplicate_key", key: trimmedKey };
    }
    seen.add(trimmedKey);

    if (entry.savedKey && entry.isSavedMasked && trimmedKey !== entry.savedKey) {
      return { code: "renamed_masked_key", key: trimmedKey };
    }
    if (!entry.isSavedMasked && (!hasValue || entry.value === MASKED_SECRET_PLACEHOLDER)) {
      return { code: "empty_secret_value", key: trimmedKey };
    }
  }
  return null;
}

/**
 * Builds the secret patch dictionary for PATCH /mcp/servers/{id}:
 * - Uses a null-prototype object and Object.hasOwn so special keys (__proto__, constructor) work safely.
 * - Unchanged saved keys (isSavedMasked === true and key === savedKey) are omitted.
 * - Removed or renamed saved keys emit `{ [savedKey]: null }`.
 * - Newly entered values emit `{ [key]: value }` and never send the mask placeholder.
 */
export function buildSecretPatch(
  entries: SecretEntryDraft[],
  originalSavedKeys: string[],
): Record<string, string | null> | undefined {
  const patch: Record<string, string | null> = Object.create(null) as Record<string, string | null>;
  const retainedSavedKeys = new Set<string>();

  for (const entry of entries) {
    const trimmedKey = entry.key.trim();
    if (entry.savedKey !== null) {
      if (entry.isSavedMasked && trimmedKey === entry.savedKey) {
        retainedSavedKeys.add(entry.savedKey);
        continue;
      }
      if (trimmedKey !== entry.savedKey) {
        patch[entry.savedKey] = null;
      } else {
        retainedSavedKeys.add(entry.savedKey);
      }
    }
    if (!trimmedKey) continue;
    if (entry.isSavedMasked) continue;
    if (entry.value === MASKED_SECRET_PLACEHOLDER) continue;
    patch[trimmedKey] = entry.value;
  }

  for (const oldKey of originalSavedKeys) {
    if (!retainedSavedKeys.has(oldKey) && !Object.hasOwn(patch, oldKey)) {
      patch[oldKey] = null;
    }
  }

  return Object.keys(patch).length > 0 ? patch : undefined;
}

/**
 * Builds the secret map for POST /mcp/servers (new server creation).
 * Uses a null-prototype object so keys like `__proto__` or `constructor` are preserved as own data properties.
 */
export function buildNewSecretMap(entries: SecretEntryDraft[]): Record<string, string> | undefined {
  const out: Record<string, string> = Object.create(null) as Record<string, string>;
  for (const entry of entries) {
    const trimmedKey = entry.key.trim();
    if (!trimmedKey || entry.value === MASKED_SECRET_PLACEHOLDER || !entry.value) continue;
    out[trimmedKey] = entry.value;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

/**
 * Built-in and imported skills each maintain their own item revision in store.py.
 * Always submit the skill's own revision.
 */
export function expectedSkillRevision(skill: Pick<ExtensionSkill, "revision">): number {
  return skill.revision;
}

export function formatMcpArgsInput(args: readonly string[]): string {
  return JSON.stringify(args ?? []);
}

export function parseMcpArgsInput(
  raw: string,
): { ok: true; args: string[] } | { ok: false; error: "invalid_json_array" } {
  const trimmed = raw.trim();
  if (!trimmed) {
    return { ok: true, args: [] };
  }
  if (trimmed.startsWith("[")) {
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (!Array.isArray(parsed) || !parsed.every(item => typeof item === "string")) {
        return { ok: false, error: "invalid_json_array" };
      }
      return { ok: true, args: [...parsed] };
    } catch {
      return { ok: false, error: "invalid_json_array" };
    }
  }
  return {
    ok: true,
    args: raw.replace(/\r\n/g, "\n").split("\n"),
  };
}

export function reconcileSecretEntriesOnConflict(
  currentEntries: SecretEntryDraft[],
  baselineSavedKeys: string[],
  latestMaskedRecord: Record<string, ExtensionMaskedSecret> | undefined,
  latestKeysFallback: string[] = [],
): SecretEntryDraft[] {
  const latestKeys =
    latestMaskedRecord && Object.keys(latestMaskedRecord).length > 0
      ? Object.keys(latestMaskedRecord)
      : latestKeysFallback;
  const latestKeySet = new Set(latestKeys);
  const baselineKeySet = new Set(baselineSavedKeys);

  // Determine which baseline keys the user explicitly removed in the current draft
  const retainedBaselineKeys = new Set<string>();
  for (const entry of currentEntries) {
    if (entry.savedKey !== null) {
      retainedBaselineKeys.add(entry.savedKey);
    }
  }
  const userDeletedKeys = new Set<string>();
  for (const baseKey of baselineSavedKeys) {
    if (!retainedBaselineKeys.has(baseKey)) {
      userDeletedKeys.add(baseKey);
    }
  }

  const nextEntries: SecretEntryDraft[] = [];
  const presentKeys = new Set<string>();

  for (const entry of currentEntries) {
    // If this was an untouched saved masked row that was removed remotely, drop it
    if (entry.savedKey !== null && entry.isSavedMasked && !latestKeySet.has(entry.savedKey)) {
      continue;
    }
    const updatedEntry: SecretEntryDraft =
      entry.savedKey !== null && !latestKeySet.has(entry.savedKey)
        ? { ...entry, savedKey: null, isSavedMasked: false }
        : entry;
    nextEntries.push(updatedEntry);
    if (updatedEntry.savedKey) presentKeys.add(updatedEntry.savedKey);
    if (updatedEntry.key.trim()) presentKeys.add(updatedEntry.key.trim());
  }

  // Append any newly added remote saved keys that were not in baseline and not touched by user
  for (const remoteKey of latestKeys) {
    if (
      !baselineKeySet.has(remoteKey) &&
      !userDeletedKeys.has(remoteKey) &&
      !presentKeys.has(remoteKey)
    ) {
      nextEntries.push({
        id: `saved-rebased-${remoteKey}`,
        key: remoteKey,
        value: "",
        savedKey: remoteKey,
        isSavedMasked: Boolean(
          latestMaskedRecord && Object.hasOwn(latestMaskedRecord, remoteKey)
            ? latestMaskedRecord[remoteKey]?.has_value
            : true,
        ),
      });
    }
  }

  return nextEntries;
}

function normalizeMcpTool(raw: Record<string, unknown>): ExtensionMcpTool {
  const name = String(raw.name ?? "");
  const ownerReadOnly = Boolean(raw.owner_read_only);
  const componentEnabled = raw.component_enabled !== undefined ? Boolean(raw.component_enabled) : Boolean(raw.enabled ?? true);
  const enabled = raw.enabled !== undefined ? Boolean(raw.enabled) : componentEnabled;
  return {
    name,
    model_tool_name: typeof raw.model_tool_name === "string" ? raw.model_tool_name : undefined,
    title: typeof raw.title === "string" && raw.title ? raw.title : name,
    description: typeof raw.description === "string" ? raw.description : "",
    enabled,
    component_enabled: componentEnabled,
    owner_read_only: ownerReadOnly,
    effective_effect: raw.effective_effect === "read" || ownerReadOnly ? "read" : "write",
    effective_risk: raw.effective_risk === "low" || ownerReadOnly ? "low" : "high",
    annotations: raw.annotations && typeof raw.annotations === "object" ? (raw.annotations as ExtensionMcpToolAnnotations) : undefined,
    schema_sha256: typeof raw.schema_sha256 === "string" ? raw.schema_sha256 : undefined,
    input_schema: raw.input_schema && typeof raw.input_schema === "object" ? (raw.input_schema as Record<string, unknown>) : undefined,
  };
}

export function normalizeMcpServer(raw: Record<string, unknown>): ExtensionMcpServer {
  const id = String(raw.id ?? "");
  const name = typeof raw.name === "string" && raw.name ? raw.name : id;
  const parentEnabled = raw.parent_enabled !== undefined ? Boolean(raw.parent_enabled) : true;
  const componentEnabled = raw.component_enabled !== undefined ? Boolean(raw.component_enabled) : Boolean(raw.enabled);
  const enabled = raw.enabled !== undefined ? Boolean(raw.enabled) : componentEnabled && parentEnabled;
  const maskedEnvRaw = raw.masked_env && typeof raw.masked_env === "object" ? (raw.masked_env as Record<string, ExtensionMaskedSecret>) : {};
  const maskedHeadersRaw = raw.masked_headers && typeof raw.masked_headers === "object" ? (raw.masked_headers as Record<string, ExtensionMaskedSecret>) : {};
  const envHttpHeadersRaw: Record<string, string> = {};
  if (raw.env_http_headers && typeof raw.env_http_headers === "object") {
    for (const [k, v] of Object.entries(raw.env_http_headers as Record<string, unknown>)) {
      if (typeof k === "string" && k.trim() && typeof v === "string" && v.trim()) {
        envHttpHeadersRaw[k.trim()] = v.trim();
      }
    }
  }
  const oauthRegRaw = typeof raw.oauth_client_registration === "string" ? raw.oauth_client_registration.toLowerCase() : "auto";
  const oauthClientRegistration: ExtensionMcpOauthClientRegistration =
    oauthRegRaw === "cimd" || oauthRegRaw === "dcr" ? oauthRegRaw : "auto";
  const tools = Array.isArray(raw.tools)
    ? raw.tools.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object")).map(normalizeMcpTool)
    : [];
  const enabledTools = Array.isArray(raw.enabled_tools) ? raw.enabled_tools.map(String) : null;
  const status = typeof raw.status === "string" && raw.status ? raw.status : enabled ? "ready" : "disabled";
  return {
    id,
    instance_id: typeof raw.instance_id === "string" ? raw.instance_id : undefined,
    revision: typeof raw.revision === "number" ? raw.revision : 1,
    config_generation: typeof raw.config_generation === "number" ? raw.config_generation : undefined,
    name,
    title: typeof raw.title === "string" && raw.title ? raw.title : name,
    description: typeof raw.description === "string" ? raw.description : "",
    transport: raw.transport === "streamable_http" ? "streamable_http" : "stdio",
    builtin: Boolean(raw.builtin),
    deletable: raw.deletable !== undefined ? Boolean(raw.deletable) : !raw.builtin,
    plugin_id: typeof raw.plugin_id === "string" && raw.plugin_id ? raw.plugin_id : null,
    computer_access: Boolean(raw.computer_access),
    enabled,
    component_enabled: componentEnabled,
    parent_enabled: parentEnabled,
    command: typeof raw.command === "string" ? raw.command : "",
    args: Array.isArray(raw.args) ? raw.args.map(String) : [],
    cwd: typeof raw.cwd === "string" ? raw.cwd : "",
    env_keys: Array.isArray(raw.env_keys) ? raw.env_keys.map(String) : Object.keys(maskedEnvRaw),
    masked_env: maskedEnvRaw,
    env_vars: Array.isArray(raw.env_vars) ? raw.env_vars.map(String).filter(Boolean) : [],
    url: typeof raw.url === "string" ? raw.url : "",
    bearer_token_env: typeof raw.bearer_token_env === "string" ? raw.bearer_token_env : "",
    bearer_token_env_present: typeof raw.bearer_token_env_present === "boolean" ? raw.bearer_token_env_present : undefined,
    header_keys: Array.isArray(raw.header_keys) ? raw.header_keys.map(String) : Object.keys(maskedHeadersRaw),
    masked_headers: maskedHeadersRaw,
    env_http_headers: envHttpHeadersRaw,
    oauth_client_registration: oauthClientRegistration,
    auth: raw.auth && typeof raw.auth === "object" ? raw.auth as ExtensionMcpServer["auth"] : undefined,
    oauth: raw.oauth && typeof raw.oauth === "object" ? raw.oauth as McpOauthStatus : undefined,
    data_policy: raw.data_policy && typeof raw.data_policy === "object" ? raw.data_policy as ExtensionDataPolicy : undefined,
    discovery_revision: typeof raw.discovery_revision === "number" ? raw.discovery_revision : undefined,
    read_only_sources: raw.read_only_sources && typeof raw.read_only_sources === "object"
      ? Object.fromEntries(Object.entries(raw.read_only_sources).filter(([, value]) => value === "owner" || value === "server_hint")) as Record<string, "owner" | "server_hint"> : {},
    server_hint_candidates: Array.isArray(raw.server_hint_candidates) ? raw.server_hint_candidates
      .filter((value): value is ExtensionServerHintCandidate => Boolean(value && typeof value === "object" && typeof value.name === "string"
        && typeof value.schema_sha256 === "string" && /^[0-9a-f]{64}$/.test(value.schema_sha256)))
      .map(value => ({ name: value.name, title: typeof value.title === "string" ? value.title : value.name, schema_sha256: value.schema_sha256 })) : [],
    has_secrets: typeof raw.has_secrets === "boolean" ? raw.has_secrets : Boolean(Object.keys(maskedEnvRaw).length || Object.keys(maskedHeadersRaw).length),
    startup_timeout_ms: typeof raw.startup_timeout_ms === "number" ? raw.startup_timeout_ms : 10000,
    tool_timeout_ms: typeof raw.tool_timeout_ms === "number" ? raw.tool_timeout_ms : 30000,
    enabled_tools: enabledTools,
    disabled_tools: Array.isArray(raw.disabled_tools) ? raw.disabled_tools.map(String) : [],
    read_only_tools: Array.isArray(raw.read_only_tools) ? raw.read_only_tools.map(String) : [],
    tools,
    tools_count: typeof raw.tools_count === "number" ? raw.tools_count : tools.length,
    enabled_tools_count: typeof raw.enabled_tools_count === "number" ? raw.enabled_tools_count : tools.filter(t => t.enabled).length,
    status,
    status_message: typeof raw.status_message === "string" ? raw.status_message : "",
    last_tested_at: typeof raw.last_tested_at === "string" ? raw.last_tested_at : null,
    updated_at: typeof raw.updated_at === "string" ? raw.updated_at : null,
  };
}

function normalizeSkillInterface(
  raw: unknown,
  fallbackTitle: string,
  fallbackShortDesc: string,
): ExtensionSkillInterface {
  const obj = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  return {
    display_name:
      typeof obj.display_name === "string" && obj.display_name
        ? obj.display_name
        : typeof obj.displayName === "string" && obj.displayName
          ? obj.displayName
          : fallbackTitle,
    short_description:
      typeof obj.short_description === "string" && obj.short_description
        ? obj.short_description
        : typeof obj.shortDescription === "string" && obj.shortDescription
          ? obj.shortDescription
          : fallbackShortDesc,
    icon_small:
      typeof obj.icon_small === "string" && obj.icon_small
        ? obj.icon_small
        : typeof obj.iconSmall === "string" && obj.iconSmall
          ? obj.iconSmall
          : null,
    icon_large:
      typeof obj.icon_large === "string" && obj.icon_large
        ? obj.icon_large
        : typeof obj.iconLarge === "string" && obj.iconLarge
          ? obj.iconLarge
          : null,
    brand_color:
      typeof obj.brand_color === "string" && obj.brand_color
        ? obj.brand_color
        : typeof obj.brandColor === "string" && obj.brandColor
          ? obj.brandColor
          : null,
    default_prompt:
      typeof obj.default_prompt === "string" && obj.default_prompt
        ? obj.default_prompt
        : typeof obj.defaultPrompt === "string" && obj.defaultPrompt
          ? obj.defaultPrompt
          : null,
  };
}

export function normalizeSkill(raw: Record<string, unknown>): ExtensionSkill {
  const id = String(raw.id ?? "");
  const name = typeof raw.name === "string" && raw.name ? raw.name : id;
  const title = typeof raw.title === "string" && raw.title ? raw.title : name;
  const description = typeof raw.description === "string" ? raw.description : typeof raw.summary === "string" ? raw.summary : "";
  const shortDescription =
    typeof raw.short_description === "string" && raw.short_description
      ? raw.short_description
      : typeof raw.summary === "string" && raw.summary
        ? raw.summary
        : description;
  const parentEnabled = raw.parent_enabled !== undefined ? Boolean(raw.parent_enabled) : true;
  const componentEnabled = raw.component_enabled !== undefined ? Boolean(raw.component_enabled) : Boolean(raw.enabled);
  const enabled = raw.enabled !== undefined ? Boolean(raw.enabled) : componentEnabled && parentEnabled;
  const files = Array.isArray(raw.files)
    ? raw.files
        .filter((f): f is Record<string, unknown> => Boolean(f && typeof f === "object" && typeof f.path === "string"))
        .map(f => ({ path: String(f.path), size: typeof f.size === "number" ? f.size : 0 }))
    : [];
  const scope: ExtensionSkillScope =
    typeof raw.scope === "string" && raw.scope
      ? raw.scope
      : raw.builtin
        ? "system"
        : raw.plugin_id
          ? "plugin"
          : "user";
  const policyObj = raw.policy && typeof raw.policy === "object" ? (raw.policy as Record<string, unknown>) : {};
  return {
    id,
    instance_id: typeof raw.instance_id === "string" ? raw.instance_id : undefined,
    revision: typeof raw.revision === "number" ? raw.revision : 1,
    name,
    title,
    description,
    short_description: shortDescription,
    summary: typeof raw.summary === "string" ? raw.summary : description,
    version: typeof raw.version === "string" ? raw.version : "1.0.0",
    install_generation: typeof raw.install_generation === "number" ? raw.install_generation : undefined,
    content: typeof raw.content === "string" ? raw.content : "",
    manifest_sha256: typeof raw.manifest_sha256 === "string" ? raw.manifest_sha256 : null,
    builtin: Boolean(raw.builtin),
    deletable: raw.deletable !== undefined ? Boolean(raw.deletable) : !raw.builtin && scope !== "system",
    enabled,
    component_enabled: componentEnabled,
    parent_enabled: parentEnabled,
    source_format: typeof raw.source_format === "string" && raw.source_format ? raw.source_format : raw.builtin ? "builtin" : "skill_md",
    scope,
    path: typeof raw.path === "string" ? raw.path : "",
    package_dir: typeof raw.package_dir === "string" ? raw.package_dir : "",
    interface: normalizeSkillInterface(raw.interface, title, shortDescription),
    policy: {
      allow_implicit_invocation:
        policyObj.allow_implicit_invocation !== undefined
          ? Boolean(policyObj.allow_implicit_invocation)
          : true,
    },
    plugin_id: typeof raw.plugin_id === "string" && raw.plugin_id ? raw.plugin_id : null,
    files,
    warnings: Array.isArray(raw.warnings) ? raw.warnings.map(String) : [],
    updated_at: typeof raw.updated_at === "string" ? raw.updated_at : null,
  };
}

export function normalizeRecommendedSkill(raw: Record<string, unknown>): RecommendedSkill {
  const id = String(raw.id ?? "");
  const name = typeof raw.name === "string" && raw.name ? raw.name : id;
  const title = typeof raw.title === "string" && raw.title ? raw.title : name;
  const description = typeof raw.description === "string" ? raw.description : "";
  const shortDescription =
    typeof raw.short_description === "string" && raw.short_description
      ? raw.short_description
      : typeof raw.summary === "string" && raw.summary
        ? raw.summary
        : description;
  const policyObj = raw.policy && typeof raw.policy === "object" ? (raw.policy as Record<string, unknown>) : {};
  return {
    id,
    name,
    title,
    description,
    short_description: shortDescription,
    summary: typeof raw.summary === "string" ? raw.summary : shortDescription,
    version: typeof raw.version === "string" ? raw.version : "1.0.0",
    repo_path: typeof raw.repo_path === "string" ? raw.repo_path : `skills/.curated/${id}`,
    source_dir: typeof raw.source_dir === "string" ? raw.source_dir : null,
    path: typeof raw.path === "string" ? raw.path : `skills/.curated/${id}`,
    scope: typeof raw.scope === "string" && raw.scope ? raw.scope : "curated",
    content: typeof raw.content === "string" ? raw.content : "",
    installed: Boolean(raw.installed),
    interface: normalizeSkillInterface(raw.interface, title, shortDescription),
    policy: {
      allow_implicit_invocation:
        policyObj.allow_implicit_invocation !== undefined
          ? Boolean(policyObj.allow_implicit_invocation)
          : true,
    },
  };
}

export function normalizePlugin(raw: Record<string, unknown>): ExtensionPlugin {
  const id = String(raw.id ?? "");
  const name = typeof raw.name === "string" && raw.name ? raw.name : id;
  const title = typeof raw.title === "string" && raw.title ? raw.title : name;
  const enabled = Boolean(raw.enabled);
  const componentEnabled = raw.component_enabled !== undefined ? Boolean(raw.component_enabled) : enabled;
  const parentEnabled = raw.parent_enabled !== undefined ? Boolean(raw.parent_enabled) : true;
  const skills = Array.isArray(raw.skills)
    ? raw.skills.filter((s): s is Record<string, unknown> => Boolean(s && typeof s === "object")).map(normalizeSkill)
    : undefined;
  const mcpServers = Array.isArray(raw.mcp_servers)
    ? raw.mcp_servers.filter((m): m is Record<string, unknown> => Boolean(m && typeof m === "object")).map(normalizeMcpServer)
    : undefined;
  return {
    id,
    peer_provider: normalizePeerProvider(raw.peer_provider),
    views: normalizePluginViews(raw.views),
    data_policy: raw.data_policy && typeof raw.data_policy === "object" ? raw.data_policy as ExtensionDataPolicy : undefined,
    instance_id: typeof raw.instance_id === "string" ? raw.instance_id : undefined,
    revision: typeof raw.revision === "number" ? raw.revision : 1,
    name,
    title,
    description: typeof raw.description === "string" ? raw.description : "",
    version: typeof raw.version === "string" ? raw.version : "1.0.0",
    kind: typeof raw.kind === "string" ? raw.kind : undefined,
    source_format: typeof raw.source_format === "string" && raw.source_format ? raw.source_format : "ryn_plugin",
    builtin: Boolean(raw.builtin),
    deletable: raw.deletable !== undefined ? Boolean(raw.deletable) : !raw.builtin,
    enabled,
    component_enabled: componentEnabled,
    parent_enabled: parentEnabled,
    status: typeof raw.status === "string" && raw.status ? raw.status : enabled ? "enabled" : "disabled",
    status_reason: typeof raw.status_reason === "string" ? raw.status_reason : "",
    computer_access: Boolean(raw.computer_access),
    skill_ids: Array.isArray(raw.skill_ids) ? raw.skill_ids.map(String) : [],
    mcp_server_ids: Array.isArray(raw.mcp_server_ids) ? raw.mcp_server_ids.map(String) : [],
    skills_count: typeof raw.skills_count === "number" ? raw.skills_count : skills?.length,
    mcp_servers_count: typeof raw.mcp_servers_count === "number" ? raw.mcp_servers_count : mcpServers?.length,
    skills,
    mcp_servers: mcpServers,
    unsupported_components: Array.isArray(raw.unsupported_components) ? raw.unsupported_components.map(String) : [],
    warnings: Array.isArray(raw.warnings) ? raw.warnings.map(String) : [],
    capabilities: raw.capabilities && typeof raw.capabilities === "object" ? (raw.capabilities as ExtensionPluginCapabilities) : undefined,
    updated_at: typeof raw.updated_at === "string" ? raw.updated_at : null,
  };
}

function normalizePluginViews(raw: unknown): ExtensionPluginView[] {
  if (!Array.isArray(raw) || raw.length > 8) return [];
  return raw.filter((item): item is ExtensionPluginView => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return false;
    const pagination = item.pagination;
    return [item.id, item.title, item.mcpServer, item.tool, item.rowsPath].every(x => typeof x === "string" && x.length > 0 && x.length <= 256)
      && (item.description === undefined || typeof item.description === "string")
      && Array.isArray(item.inputs) && item.inputs.length <= 20 && item.inputs.every((x: Record<string, unknown>) =>
        x && typeof x.name === "string" && typeof x.label === "string" && ["text", "date"].includes(String(x.type)))
      && Array.isArray(item.columns) && item.columns.length > 0 && item.columns.length <= 32 && item.columns.every((x: Record<string, unknown>) =>
        x && typeof x.key === "string" && typeof x.label === "string")
      && Array.isArray(item.summaryFields) && item.summaryFields.length <= 16 && item.summaryFields.every((x: Record<string, unknown>) =>
        x && typeof x.path === "string" && typeof x.label === "string")
      && pagination && typeof pagination.path === "string" && Number.isInteger(pagination.pageSize) && pagination.pageSize > 0 && pagination.pageSize <= 20;
  });
}

function normalizePeerProvider(raw: unknown): ExtensionPlugin["peer_provider"] {
  if (!raw || typeof raw !== "object") return null;
  const value = raw as Record<string, unknown>;
  const oauth = value.oauth as Record<string, unknown> | undefined;
  if (typeof value.url !== "string" || !oauth || typeof oauth.clientId !== "string" ||
      !Array.isArray(oauth.scopes) || !oauth.scopes.every(s => typeof s === "string")) return null;
  // Copy only public declaration fields; credentials never belong in plugin views.
  return { url: value.url, oauth: { clientId: oauth.clientId, scopes: oauth.scopes as string[] } };
}

export function normalizeOverview(raw: Record<string, unknown>): ExtensionOverview {
  return {
    revision: typeof raw.revision === "number" ? raw.revision : 1,
    updated_at: typeof raw.updated_at === "string" ? raw.updated_at : null,
    desktop_access: Boolean(raw.desktop_access),
    windows_supported: typeof raw.windows_supported === "boolean" ? raw.windows_supported : undefined,
    plugins: Array.isArray(raw.plugins)
      ? raw.plugins.filter((p): p is Record<string, unknown> => Boolean(p && typeof p === "object")).map(normalizePlugin)
      : [],
    skills: Array.isArray(raw.skills)
      ? raw.skills.filter((s): s is Record<string, unknown> => Boolean(s && typeof s === "object")).map(normalizeSkill)
      : [],
    mcp_servers: Array.isArray(raw.mcp_servers)
      ? raw.mcp_servers.filter((m): m is Record<string, unknown> => Boolean(m && typeof m === "object")).map(normalizeMcpServer)
      : [],
  };
}

async function extensionRequest<T>(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(nodeControlUrl(`/agent/extensions${path}`), {
    method,
    credentials: "include",
    cache: "no-store",
    signal,
    headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const code =
      typeof data.detail === "string"
        ? data.detail
        : typeof data.error === "string"
          ? data.error
          : "extension_request_failed";
    throw new AgentExtensionError(code, response.status);
  }
  return response.json() as Promise<T>;
}

export const agentExtensions = {
  overview: async (signal?: AbortSignal): Promise<ExtensionOverview> =>
    normalizeOverview(await extensionRequest<Record<string, unknown>>("", "GET", undefined, signal)),

  createMcpServer: async (body: ExtensionMcpServerCreateInput): Promise<ExtensionMcpServer> =>
    normalizeMcpServer(await extensionRequest<Record<string, unknown>>("/mcp/servers", "POST", body)),

  updateMcpServer: async (id: string, body: ExtensionMcpServerUpdateInput): Promise<ExtensionMcpServer> =>
    normalizeMcpServer(
      await extensionRequest<Record<string, unknown>>(`/mcp/servers/${encodeURIComponent(id)}`, "PATCH", body),
    ),

  deleteMcpServer: (id: string, expectedRevision: number) =>
    extensionRequest<ExtensionDeleteResponse>(
      `/mcp/servers/${encodeURIComponent(id)}?expected_revision=${encodeURIComponent(String(expectedRevision))}`,
      "DELETE",
    ),

  testMcpServer: async (id: string, expectedRevision: number): Promise<ExtensionMcpServerActionResponse> => {
    const raw = await extensionRequest<{ server: Record<string, unknown>; test_result: ExtensionTestResult }>(
      `/mcp/servers/${encodeURIComponent(id)}/test`,
      "POST",
      { expected_revision: expectedRevision },
    );
    return {
      server: normalizeMcpServer(raw.server),
      test_result: raw.test_result,
    };
  },

  restartMcpServer: async (id: string, expectedRevision: number): Promise<ExtensionMcpServerActionResponse> => {
    const raw = await extensionRequest<{ server: Record<string, unknown>; test_result: ExtensionTestResult }>(
      `/mcp/servers/${encodeURIComponent(id)}/restart`,
      "POST",
      { expected_revision: expectedRevision },
    );
    return {
      server: normalizeMcpServer(raw.server),
      test_result: raw.test_result,
    };
  },

  restartAllMcpServers: async (): Promise<ExtensionOverview> => {
    const raw = await extensionRequest<Record<string, unknown>>("/mcp/restart", "POST", {});
    const overviewPayload =
      raw.overview && typeof raw.overview === "object" ? (raw.overview as Record<string, unknown>) : raw;
    return normalizeOverview(overviewPayload);
  },

  clearMcpAppData: async (): Promise<ExtensionOverview> => {
    const raw = await extensionRequest<Record<string, unknown>>("/mcp/apps/clear-data", "POST", {});
    const overviewPayload =
      raw.overview && typeof raw.overview === "object" ? (raw.overview as Record<string, unknown>) : raw;
    return normalizeOverview(overviewPayload);
  },

  updatePlugin: async (id: string, body: { enabled: boolean; expected_revision: number }): Promise<ExtensionPlugin> =>
    normalizePlugin(
      await extensionRequest<Record<string, unknown>>(`/plugins/${encodeURIComponent(id)}`, "PATCH", body),
    ),

  deletePlugin: (id: string, expectedRevision: number) =>
    extensionRequest<ExtensionDeleteResponse>(
      `/plugins/${encodeURIComponent(id)}?expected_revision=${encodeURIComponent(String(expectedRevision))}`,
      "DELETE",
    ),

  updateSkill: async (id: string, body: { enabled: boolean; expected_revision: number }): Promise<ExtensionSkill> =>
    normalizeSkill(
      await extensionRequest<Record<string, unknown>>(`/skills/${encodeURIComponent(id)}`, "PATCH", body),
    ),

  deleteSkill: (id: string, expectedRevision: number) =>
    extensionRequest<ExtensionDeleteResponse>(
      `/skills/${encodeURIComponent(id)}?expected_revision=${encodeURIComponent(String(expectedRevision))}`,
      "DELETE",
    ),

  recommendedSkills: async (refresh = false, signal?: AbortSignal): Promise<RecommendedSkill[]> => {
    const query = refresh ? "?refresh=1" : "";
    const raw = await extensionRequest<Record<string, unknown>>(`/skills/recommended${query}`, "GET", undefined, signal);
    const list = Array.isArray(raw.skills) ? raw.skills : [];
    return list
      .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"))
      .map(normalizeRecommendedSkill);
  },

  installRecommendedSkill: async (body: {
    skill_id: string;
    repo_path?: string;
  }): Promise<{ skill: ExtensionSkill; overview: ExtensionOverview }> => {
    const raw = await extensionRequest<{ skill: Record<string, unknown>; overview: Record<string, unknown> }>(
      "/skills/recommended/install",
      "POST",
      body,
    );
    return {
      skill: normalizeSkill(raw.skill ?? {}),
      overview: normalizeOverview(raw.overview ?? {}),
    };
  },

  refreshSkills: async (body?: { reset_uninstalled?: boolean }): Promise<ExtensionOverview> => {
    const raw = await extensionRequest<Record<string, unknown>>("/skills/refresh", "POST", body ?? {});
    const overviewPayload =
      raw.overview && typeof raw.overview === "object" ? (raw.overview as Record<string, unknown>) : raw;
    return normalizeOverview(overviewPayload);
  },

  openSkillPath: (id: string, action: "open" | "reveal" = "open") =>
    extensionRequest<{ ok: boolean; action: string; path: string }>(
      `/skills/${encodeURIComponent(id)}/open`,
      "POST",
      { action },
    ),

  readSkillFile: (id: string, relativePath = "SKILL.md") =>
    extensionRequest<{
      skill_id: string;
      version: string;
      path: string;
      content: string;
      truncated: boolean;
      files: string[];
    }>(`/skills/${encodeURIComponent(id)}/files?path=${encodeURIComponent(relativePath)}`, "GET"),

  previewImport: (body: ExtensionImportPreviewRequest) =>
    extensionRequest<ExtensionImportPreview>("/import/preview", "POST", body),

  commitImport: async (body: { preview_id: string; expected_revision: number }): Promise<ExtensionOverview> => {
    const raw = await extensionRequest<Record<string, unknown>>("/import/commit", "POST", body);
    const overviewPayload =
      raw.overview && typeof raw.overview === "object" ? (raw.overview as Record<string, unknown>) : raw;
    return normalizeOverview(overviewPayload);
  },
};
