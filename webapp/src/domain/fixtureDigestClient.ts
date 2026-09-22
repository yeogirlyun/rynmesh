import type { digestApi, Digest, DiscoveryStatus } from "./digestClient";

// The design preview must never read or modify the user's live feed.
const unavailable = async (): Promise<never> => { throw new Error("This action is available when connected to your live device."); };
const digest: Digest = { generated_at_unix: 0, brief: "", ai: null, items: [], sources: [] };
const status: DiscoveryStatus = {
  phase: "waiting", message: "Design preview — connect your live device to load your feed.",
  last_started_unix: 0, last_completed_unix: 0, next_refresh_unix: 0,
  new_items: 0, unread_count: 0, item_count: 0, source_count: 0, formats: [],
  healthy_sources: 0, failed_sources: 0, cached_sources: 0, degraded: false,
  offline_ready: false, source_health: [],
};
export const fixtureDigestApi: typeof digestApi = {
  listSources: async () => [],
  listWatchers: async () => [],
  listConsumption: async () => [],
  getDigest: async () => digest,
  getDiscoveryStatus: async () => status,
  markDiscoverySeen: async () => status,
  refreshDigest: async () => ({ refresh: { new_items: 0 }, digest, status }),
  aiStatus: async () => ({ provider: null, model: null }),
  getSteering: async () => ({ text: "", interests: [], avoids: [] }),
  addSource: unavailable, removeSource: unavailable, readArticle: unavailable,
  recordConsumption: unavailable, clearConsumption: unavailable, steer: unavailable,
  sendFeedback: unavailable, saveReadLater: unavailable, addWatcher: unavailable,
  removeWatcher: unavailable, listModels: unavailable, selectModel: unavailable,
};
