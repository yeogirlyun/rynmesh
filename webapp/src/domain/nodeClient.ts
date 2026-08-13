import type {
  ContentFilters,
  ContentBody,
  ContentItem,
  EgressStatus,
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
  discoverPeers(req?: { network?: string }): Promise<Peer[]>;
  listPeers(filters?: PeerFilters): Promise<Peer[]>;
  listContent(filters?: ContentFilters): Promise<ContentItem[]>;
  getContentItem(contentId: string): Promise<ContentItem>;
  getContentBody(contentId: string): Promise<ContentBody>;
  getProvenance(headHash: string | null): Promise<ProvenanceEvent[]>;
  fetchPreview(contentId: string, providerPeerId: string): Promise<{ ok: boolean; size: string }>;
  fetchFullContent(contentId: string, providerPeerId: string): Promise<{ ok: boolean; size: string }>;
  requestRecommendations(req?: { query?: string; limit?: number }): Promise<Recommendation[]>;
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
