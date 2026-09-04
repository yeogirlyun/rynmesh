import type {
  ContentFilters,
  ContentBody,
  ContentItem,
  EgressStatus,
  FirstSuccessStatus,
  JobCapacity,
  NodeSettings,
  NodeStatus,
  Peer,
  PeerFilters,
  PeerHealth,
  PersonalDataExport,
  PrivacyEraseScope,
  PrivacyStatus,
  ProvenanceEvent,
  PublishDraft,
  PublishPrepResult,
  Recommendation,
  RecommendationFeedbackAction,
  RecommendationProfile,
  RegistryStatus,
  SearchAskResponse,
  UpdateStatus,
  WorkOrder,
  WorkResult,
} from "./types";
import type {
  AssistantContextPrepareRequest,
  AssistantContextRef,
  AssistantRun,
  AssistantRunRequest,
  AssistantTarget,
} from "./assistantTypes";
import type { LibraryAction, LibraryItem, LibraryPage, LibrarySource, LibraryView, LibraryWatcher } from "./libraryTypes";

export interface LLMServiceRecord {
  peer_id: string;
  node_name?: string;
  online: boolean;
  capacity?: { available?: number; max_concurrent?: number; running?: number };
  benchmark?: { latency_ms?: number; tokens_per_second?: number };
  service: {
    package_id: string;
    model_alias: string;
    capabilities: string[];
    context_window: number;
    max_output_tokens: number;
    pricing: {
      currency: string;
      input_per_1k: number;
      output_per_1k: number;
      minimum: number;
      maximum_per_task: number;
    };
    privacy: { policy_text?: string; compute_node_sees_plaintext?: boolean };
    risk_labels?: string[];
  };
}

export interface LLMOrderResult {
  task_id: string;
  state: string;
  output?: string;
  model_alias?: string;
  input_tokens?: number;
  output_tokens?: number;
  duration_ms?: number;
  amount?: number;
  error_code?: string;
  created_at?: string;
  updated_at?: string;
  transport?: "peer_http_direct" | "ice_udp_direct" | "encrypted_relay" | "unknown";
  transport_evidence?: {
    relay_used?: boolean;
    public_nat_traversal_required?: boolean;
    distinct_public_egress_required?: boolean;
    peer_public_mapping_nominated?: boolean;
  };
}

export interface LLMPrivacySettings {
  result_retention_seconds: 0 | 3600 | 86400 | 604800;
  plaintext_persisted: boolean;
  stored_results_encrypted: boolean;
  compute_node_sees_plaintext: boolean;
}

export interface TaskBalanceSummary {
  currency: string;
  available: number;
  held: number;
  earned: number;
}

export interface LLMProviderStatus {
  configured?: boolean;
  online: boolean;
  service?: LLMServiceRecord["service"];
  capacity?: { available?: number; max_concurrent?: number; running?: number; queue_limit?: number };
  health?: Record<string, unknown>;
  publication_enabled?: boolean;
  accepting_orders?: boolean;
  network_id?: string;
  lifecycle?: {
    package_id?: string;
    mode?: string;
    runtime?: { managed?: boolean; installed?: boolean; running?: boolean; status?: string };
    health?: Record<string, unknown>;
    error?: string;
  };
}

export interface LLMSetupJob {
  version?: "ryn.local-ai-install.v2";
  job_id?: string;
  state: "idle" | "queued" | "running" | "cancelling" | "cancelled" | "failed" | "succeeded";
  stage: string;
  progress: number;
  message?: string;
  error_code?: string;
  retryable?: boolean;
  resumable?: boolean;
  cancelable?: boolean;
  bytes_done?: number;
  bytes_total?: number;
  selected_profile?: string | null;
  attempt?: number;
  safe_error?: string | null;
  started_at?: string;
  updated_at?: string;
  configured?: boolean;
  publication_enabled?: boolean;
}

export interface LocalAIProfile {
  id: "light" | "balanced" | "quality";
  model_id: string;
  alias: string;
  version: string;
  download_mib: number;
  estimated_memory_mib: number;
  estimated_disk_mib: number;
  context_window: number;
  backend: string;
  supported: boolean;
  unsupported_reason: string | null;
  recommendation_reason: string;
}

export interface LocalAIRecommendations {
  version: "ryn.local-ai-hardware.v1";
  platform: string;
  memory_available_mib: number;
  disk_available_mib: number;
  accelerators: Array<{ kind: string; name?: string; memory_mib: number }>;
  runtime_support: { managed: boolean; ollama: boolean; docker_preview: boolean };
  recommended_profile: LocalAIProfile["id"] | null;
  profiles: LocalAIProfile[];
  warnings: string[];
}

export interface LocalAIReadiness {
  version: "ryn.local-ai-readiness.v1";
  configured: boolean;
  ready: boolean;
  state: "not_configured" | "ready" | "unavailable";
  model_alias: string | null;
  runtime?: string;
  loopback_only?: boolean;
  last_setup_state: string;
  safe_error: string | null;
}

export interface LocalAISetupRequest {
  profile: LocalAIProfile["id"];
  accept_license: boolean;
  package_id?: string;
  port?: number;
}

export interface LLMSetupRequest {
  mode: "managed" | "import-gguf" | "openai-compatible" | "ollama";
  package_id: string;
  alias: string;
  port?: number;
  model_path?: string;
  base_url?: string;
  model?: string;
  api_key_env?: string;
  allow_non_loopback?: boolean;
  accept_risk?: boolean;
}

export interface NodeClient {
  mode: "live" | "fixture";
  getNodeStatus(): Promise<NodeStatus>;
  getRegistryStatus(): Promise<RegistryStatus>;
  listJobCapacities(filters?: { capability?: string; network_id?: string }): Promise<JobCapacity[]>;
  submitWorkOrder(req: {
    provider_peer_id: string;
    capability: string;
    operation: string;
    params: Record<string, unknown>;
    max_credit_cost?: number;
    network_id?: string;
  }): Promise<{ work_order_id: string; order: WorkOrder }>;
  listWorkResults(filters?: { work_order_id?: string; status?: string; network_id?: string }): Promise<WorkResult[]>;
  listLLMServices(networkId?: string): Promise<LLMServiceRecord[]>;
  getLLMServiceStatus(): Promise<LLMProviderStatus>;
  publishLLMService(req?: { network_id?: string; benchmark?: boolean }): Promise<Record<string, unknown>>;
  pauseLLMService(): Promise<LLMProviderStatus>;
  setupLLMService(req: LLMSetupRequest): Promise<Record<string, unknown>>;
  startLLMSetup(req: LLMSetupRequest | LocalAISetupRequest): Promise<LLMSetupJob>;
  getLLMSetupStatus(): Promise<LLMSetupJob>;
  cancelLLMSetup(jobId: string): Promise<LLMSetupJob>;
  retryLLMSetup(jobId: string): Promise<LLMSetupJob>;
  getLocalAIRecommendations(): Promise<LocalAIRecommendations>;
  getLocalAIReadiness(): Promise<LocalAIReadiness>;
  testLocalAI(): Promise<{ ok: boolean; answer: string; model_alias: string; duration_ms: number }>;
  listAssistantTargets(): Promise<AssistantTarget[]>;
  prepareAssistantContext(req: AssistantContextPrepareRequest): Promise<AssistantContextRef>;
  createAssistantRun(req: AssistantRunRequest): Promise<AssistantRun>;
  getAssistantRun(runId: string): Promise<AssistantRun>;
  cancelAssistantRun(runId: string): Promise<AssistantRun>;
  listLibrary(filters?: { view?: LibraryView; query?: string; kind?: string; source?: string; cursor?: string; limit?: number }): Promise<LibraryPage>;
  getLibraryItem(libraryId: string): Promise<LibraryItem>;
  importLibraryFile(req: { filename: string; mime: string; data_base64: string }): Promise<LibraryItem>;
  getLibraryImportPreview(importId: string): Promise<{ import_id: string; title: string; text: string; truncated: boolean }>;
  deleteLibraryImport(importId: string): Promise<{ ok: boolean }>;
  runLibraryAction(req: { action: LibraryAction; library_ids: string[]; tag?: string }): Promise<{ ok: boolean; results: Array<{ library_id: string; ok: boolean; error?: string }> }>;
  listLibrarySources(): Promise<{ sources: LibrarySource[]; watchers: LibraryWatcher[] }>;
  addLibrarySource(url: string): Promise<LibrarySource>;
  removeLibrarySource(sourceId: string): Promise<{ ok: boolean }>;
  addLibraryWatcher(url: string, note?: string): Promise<LibraryWatcher>;
  removeLibraryWatcher(watcherId: string): Promise<{ ok: boolean }>;
  setLibrarySourcePaused(sourceId: string, paused: boolean): Promise<LibrarySource>;
  setLibraryWatcherPaused(watcherId: string, paused: boolean): Promise<LibraryWatcher>;
  runLLMServiceAction(
    action: "start" | "stop" | "restart" | "update" | "self-test" | "uninstall",
    options?: { delete_environment?: boolean; delete_model?: boolean; confirm_model_delete?: boolean },
  ): Promise<Record<string, unknown>>;
  getTaskBalance(): Promise<TaskBalanceSummary>;
  submitLLMOrder(req: {
    network_id?: string;
    provider_peer_id: string;
    service_id: string;
    prompt: string;
    max_tokens: number;
    transport?: "auto" | "direct" | "p2p" | "relay";
  }): Promise<LLMOrderResult>;
  getLLMOrder(taskId: string): Promise<LLMOrderResult>;
  cancelLLMOrder(taskId: string): Promise<LLMOrderResult>;
  listLLMOrders(): Promise<LLMOrderResult[]>;
  getLLMPrivacy(): Promise<LLMPrivacySettings>;
  updateLLMPrivacy(retentionSeconds: LLMPrivacySettings["result_retention_seconds"]): Promise<LLMPrivacySettings>;
  clearLLMOrders(): Promise<{ ok: boolean; removed: number }>;
  discoverPeers(req?: { network?: string }): Promise<Peer[]>;
  listPeers(filters?: PeerFilters): Promise<Peer[]>;
  listContent(filters?: ContentFilters): Promise<ContentItem[]>;
  getContentItem(contentId: string): Promise<ContentItem>;
  getContentBody(contentId: string): Promise<ContentBody>;
  getProvenance(headHash: string | null): Promise<ProvenanceEvent[]>;
  fetchPreview(contentId: string, providerPeerId: string): Promise<{ ok: boolean; size: string }>;
  fetchFullContent(contentId: string, providerPeerId: string): Promise<{ ok: boolean; size: string }>;
  requestRecommendations(req?: { query?: string; limit?: number }): Promise<Recommendation[]>;
  getFirstSuccess(): Promise<FirstSuccessStatus>;
  dismissFirstSuccess(): Promise<FirstSuccessStatus>;
  resetFirstSuccess(): Promise<FirstSuccessStatus>;
  recordContentConsumption(
    item: ContentItem,
    action: "opened" | "bookmark" | "unbookmark" | "completed",
  ): Promise<void>;
  getRecommendationProfile(): Promise<RecommendationProfile>;
  updateRecommendationProfile(
    patch: Partial<Pick<RecommendationProfile, "direction" | "topics" | "platforms">>,
  ): Promise<RecommendationProfile>;
  submitRecommendationFeedback(
    contentId: string,
    action: RecommendationFeedbackAction,
  ): Promise<RecommendationProfile>;
  submitSearchAsk(req: { text: string }): Promise<SearchAskResponse>;
  preparePublish(req: PublishDraft): Promise<PublishPrepResult>;
  confirmPublish(draftId: string): Promise<{ ok: boolean; content_id: string }>;
  getCreditScoreboard(
    filters?: { peerId?: string },
  ): Promise<{ peers: Array<Pick<Peer, "id" | "name" | "credits" | "weight">> }>;
  getSettings(): Promise<NodeSettings>;
  updateSettings(patch: Partial<NodeSettings>): Promise<NodeSettings>;
  getActivity(): Promise<import("./types").ActivityEvent[]>;
  getPrivacyStatus(): Promise<PrivacyStatus>;
  exportPersonalData(): Promise<PersonalDataExport>;
  erasePersonalData(scopes: PrivacyEraseScope[]): Promise<{ ok: boolean; erased: string[] }>;
  updatesStatus(): Promise<UpdateStatus>;
  updatesCheck(): Promise<UpdateStatus>;
  updatesApply(): Promise<{ applied: boolean; version?: string; error?: string }>;
  setAutoUpdate(on: boolean): Promise<void>;
  peersHealth(): Promise<PeerHealth[]>;
  egressStatus(region?: string): Promise<EgressStatus>;
  egressConnect(req?: { region?: string; provider_peer_id?: string }): Promise<EgressStatus>;
  egressLaunch(
    req?: { region?: string; urls?: string[] },
  ): Promise<{ launched?: boolean; count?: number; lastError?: string | null }>;
  egressDisconnect(req?: { region?: string }): Promise<EgressStatus>;
  listMessages(peerId: string): Promise<any[]>;
  sendMessage(req: { peer_id: string; text?: string; attachment?: { filename: string; mime: string; bytes_b64: string } }): Promise<any>;
  messagesStreamUrl(): string;
  listFriends(): Promise<import("./friendTypes").FriendRecord[]>;
  createFriendInvite(ttlMinutes?: number): Promise<import("./friendTypes").FriendInviteResult>;
  cancelFriendInvite(inviteId: string): Promise<{ status: string }>;
  inspectFriendInvite(inviteUri: string): Promise<import("./friendTypes").FriendInvitePreview>;
  joinFriend(inviteUri: string): Promise<import("./friendTypes").FriendRecord>;
  revokeFriend(relationshipId: string): Promise<import("./friendTypes").FriendRecord>;
  retryFriendRevocation(relationshipId: string): Promise<{ delivered: boolean }>;
  listFriendMessages(peerId: string): Promise<import("./friendTypes").FriendMessage[]>;
  sendFriendMessage(req: { peer_id: string; text?: string; attachment?: { filename: string; mime: string; bytes_b64: string } }): Promise<import("./friendTypes").FriendMessage>;
  retryFriendMessages(peerId: string): Promise<{ attempted: number; delivered: number }>;
  listFriendContentCards(): Promise<import("./friendTypes").FriendContentCard[]>;
  sendFriendContentCard(req: { peer_id: string; card: Record<string, string> }): Promise<{ delivered: boolean }>;
}

export class NodeClientError extends Error {
  constructor(
    message: string,
    public readonly status?: number,
  ) {
    super(message);
    this.name = "NodeClientError";
  }
}
