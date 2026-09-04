export type AssistantPrivacyBoundary = "device" | "friend" | "cloud";
export type AssistantRunState = "queued" | "running" | "completed" | "failed" | "cancelling" | "cancelled";

export interface AssistantTarget {
  key: string;
  label: string;
  privacy_boundary: AssistantPrivacyBoundary;
  provider_id?: string;
  provider_peer_id?: string;
  package_id?: string;
  model_id?: string;
  online: boolean;
  route_modes: Array<"local_chat" | "local_search" | "local_context" | "remote_provider">;
}

export interface AssistantContextRef {
  version: "ryn.assistant-context.v1";
  id: string;
  local_ref: string;
  kind: string;
  title: string;
  source_url?: string | null;
  source_ref?: string | null;
  content_hash: string;
  size_bytes: number;
  expires_at: number;
}

export interface AssistantCitation {
  citation_id: string;
  context_id: string;
  title: string;
  source_url?: string | null;
  source_ref?: string | null;
  locator: { kind: string; start: number; end: number };
  quote: string;
  content_hash: string;
}

export interface AssistantRun {
  version: "ryn.assistant-run.v1";
  run_id: string;
  conversation_id: string;
  target_key: string;
  privacy_boundary: AssistantPrivacyBoundary;
  mode: string;
  state: AssistantRunState;
  attempt: number;
  created_at: string;
  updated_at: string;
  retryable: boolean;
  error_code?: string | null;
  safe_error?: string | null;
  output?: string;
  citations?: AssistantCitation[];
  context_budget?: {
    original_bytes: number;
    included_bytes: number;
    estimated_tokens_upper_bound: number;
    truncated: boolean;
  };
  compute?: { target_key: string; privacy_boundary: AssistantPrivacyBoundary };
}

export interface AssistantMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: "complete" | "running" | "failed" | "cancelled";
  createdAt: string;
  runId?: string;
  errorCode?: string;
  citations?: AssistantCitation[];
  contextBudget?: AssistantRun["context_budget"];
}

export interface AssistantConversationV2 {
  version: "ryn.assistant-conversation.v2";
  id: string;
  title: string;
  archived: boolean;
  computeTarget: {
    key: string;
    label: string;
    privacyBoundary: AssistantPrivacyBoundary;
    providerPeerId?: string;
    packageId?: string;
    modelId?: string;
  };
  contexts: AssistantContextRef[];
  messages: AssistantMessage[];
  createdAt: string;
  updatedAt: string;
  migratedFrom?: string;
}

export interface AssistantRunRequest {
  version: "ryn.assistant-run.v1";
  conversation_id: string;
  idempotency_key: string;
  target_key: string;
  privacy_boundary: AssistantPrivacyBoundary;
  mode: "local_chat" | "local_search" | "local_context" | "remote_provider";
  messages: AssistantMessage[];
  contexts: Array<{ local_ref: string; kind: string }>;
  max_output_tokens: number;
  attempt?: number;
}

export interface AssistantContextPrepareRequest {
  conversation_id?: string;
  kind?: "text" | "markdown" | "pdf";
  title?: string;
  text?: string;
  data_base64?: string;
  source_kind?: "content-item" | "reader-article";
  content_id?: string;
  url?: string;
}
