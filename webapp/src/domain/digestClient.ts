// Client for the node's Daily Digest API (/api/local/sources, /api/local/digest).
// Kept separate from NodeClient on purpose: the digest surface is live-node-only
// (no fixture variant), and nodeClient.ts carries in-flight owner edits.

export interface DigestSource {
  id: string;
  kind: string;
  feed_url: string;
  title: string;
  tags: string[];
  weight: number;
  builtin?: boolean;
  content_kind?: string;
}

export interface DigestSourceHealth {
  id: string;
  title: string;
  ok: boolean;
  error: string;
  item_count: number;
}

export interface DigestItem {
  item_id: string;
  source_id: string;
  source_title: string;
  source_kind: string;
  title: string;
  link: string;
  summary: string;
  ai_summary: string;
  author: string;
  thumbnail: string;
  media_url: string;
  content_kind: string;
  content_type: string;
  tags: string[];
  published_unix: number;
  score: number;
  reasons: string[];
}

export interface DiscoveryStatus {
  phase: "waiting" | "refreshing" | "ready" | "error";
  message: string;
  last_started_unix: number;
  last_completed_unix: number;
  next_refresh_unix: number;
  new_items: number;
  unread_count: number;
  item_count: number;
  source_count: number;
  formats: string[];
}

export interface Digest {
  generated_at_unix: number;
  brief: string;
  ai: { provider: string; model: string } | null;
  items: DigestItem[];
  sources: DigestSourceHealth[];
}

export interface Watcher {
  id: string;
  url: string;
  note: string;
  title: string;
}

export interface ReaderBlock {
  tag: string;
  text: string;
}

export interface ReaderArticle {
  url: string;
  title: string;
  byline: string;
  lead_image: string;
  blocks: ReaderBlock[];
  word_count: number;
  cached: boolean;
}

export interface Steering {
  text: string;
  interests: string[];
  avoids: string[];
}

export interface AiStatus {
  provider: string | null;
  model: string | null;
}

export interface InstalledModel {
  name: string;
  size_bytes: number;
  modified: string;
}

export interface RecommendedModel {
  name: string;
  size_hint: string;
  tier: string;
  note: string;
  installed: boolean;
}

export interface LocalModelCatalog {
  ollama_running: boolean;
  installed: InstalledModel[];
  recommended: RecommendedModel[];
  /** what the node is using right now */
  current: string;
  /** the owner's explicit pick ("" = automatic) */
  selected: string;
  provider: string | null;
  anthropic_key_present: boolean;
}

export class DigestClientError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

// Same base-URL rule as App.tsx resolveNodeBaseUrl: env override > Tauri
// loopback default > Vite dev proxy.
function baseUrl(): string {
  const explicit = import.meta.env.VITE_RYN_NODE_BASE_URL;
  if (explicit) return explicit;
  const isTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
  return isTauri ? "http://127.0.0.1:8791/api/local" : "/api/local";
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${baseUrl()}${path}`, {
    // Carry the session cookie when the node is reached through a tunnel.
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    let detail = `Local Ryn node returned ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // keep the generic message
    }
    throw new DigestClientError(detail, response.status);
  }
  return (await response.json()) as T;
}

export const digestApi = {
  listSources: () => requestJson<DigestSource[]>("/sources"),
  addSource: (url: string) =>
    requestJson<DigestSource>("/sources", { method: "POST", body: JSON.stringify({ url }) }),
  removeSource: (sourceId: string) =>
    requestJson<{ ok: boolean }>(`/sources/${sourceId}`, { method: "DELETE" }),
  getDigest: () => requestJson<Digest>("/digest"),
  getDiscoveryStatus: () => requestJson<DiscoveryStatus>("/discovery/status"),
  markDiscoverySeen: () =>
    requestJson<DiscoveryStatus>("/discovery/seen", { method: "POST" }),
  refreshDigest: () =>
    requestJson<{ refresh: { new_items: number }; digest: Digest; status: DiscoveryStatus }>("/digest/refresh", {
      method: "POST",
    }),
  readArticle: (url: string) =>
    requestJson<ReaderArticle>(`/reader?url=${encodeURIComponent(url)}`),
  getSteering: () => requestJson<Steering>("/digest/steer"),
  steer: (text: string) =>
    requestJson<Steering>("/digest/steer", { method: "POST", body: JSON.stringify({ text }) }),
  sendFeedback: (itemId: string, action: "up" | "down" | "opened" | "more_like_this") =>
    requestJson<{ ok: boolean }>("/digest/feedback", {
      method: "POST",
      body: JSON.stringify({ item_id: itemId, action }),
    }),
  saveReadLater: (url: string) =>
    requestJson<{ ok: boolean; title: string }>("/readlater", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),
  listWatchers: () => requestJson<Watcher[]>("/watchers"),
  addWatcher: (url: string, note: string) =>
    requestJson<Watcher>("/watchers", { method: "POST", body: JSON.stringify({ url, note }) }),
  removeWatcher: (watcherId: string) =>
    requestJson<{ ok: boolean }>(`/watchers/${watcherId}`, { method: "DELETE" }),
  aiStatus: () => requestJson<AiStatus>("/ai/status"),
  listModels: () => requestJson<LocalModelCatalog>("/ai/models"),
  selectModel: (model: string) =>
    requestJson<{ ok: boolean; selected: string; model: string | null }>("/ai/model", {
      method: "POST",
      body: JSON.stringify({ model }),
    }),
};
