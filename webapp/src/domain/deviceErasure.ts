import { nodeControlUrl } from "./nodeUrl";

export const erasureCategories = { conversations: "Ask Ryn history and retained results", reading: "Bookmarks and reading history", documents: "Private imported documents", friend_feed: "Friend publications and followed updates", friend_cards: "Friend sharing cards", offline: "Offline downloads" };
export type ErasureCategory = keyof typeof erasureCategories;
export const erasureCopies: Record<ErasureCategory, string> = {
  conversations: "Reviewed conversation sources, sync replicas, known backups, search entries and retained task results.",
  reading: "Reviewed bookmark and reading sources, sync replicas, known backups and search entries. Downloaded article bodies are a separate category.",
  documents: "The private import catalogue and reviewed import files. Reading metadata, search snapshots and offline downloads are separate copies.",
  friend_feed: "Publication and subscription metadata, received update entries and known backups. Separately saved article copies remain.",
  friend_cards: "Sharing-card metadata and reviewed legacy files. Conversations and separately saved article copies remain.",
  offline: "Download metadata, pending download jobs and reviewed offline files. Original imports and separately prepared Ask materials remain.",
};
export type ErasureStatus = { outgoing: { id: string; category: ErasureCategory; remote_confirmed: boolean;
  targets: { pair_id: string; name: string; state: string; completed_at?: number }[] }[];
  incoming: { id: string; name: string; category: ErasureCategory; state: string; available: boolean }[] };
export type ErasureReview = { id: string; category: ErasureCategory; review_token: string; counts: Record<string, number | boolean>; phases: string[] };
const messages: Record<string, string> = {
  erasure_local_incomplete: "Cleanup is incomplete. Review unfinished cleanup in Privacy settings, then continue this exact approved job. No completion receipt has been issued.",
  erasure_review_changed: "This review changed. Reject this proposal and request a new review; no broader cleanup will start automatically.",
  erasure_policy_changed: "Device permissions changed. This request cannot authorize cleanup; create a new request after reviewing the pairing.",
  erasure_device_inactive: "This device is no longer paired. Its cleanup cannot be confirmed.",
  erasure_capacity: "The durable cleanup journal is full. Existing requests and receipts are retained; no new request was created.",
};
async function request<T>(body?: object): Promise<T> {
  const response = await fetch(nodeControlUrl(`/device-erasure${body ? "/action" : ""}`), { method: body ? "POST" : "GET",
    credentials: "include", signal: AbortSignal.timeout(30000), headers: { "Content-Type": "application/json" },
    ...(body ? { body: JSON.stringify(body) } : {}) });
  if (!response.ok) { const value = await response.json().catch(() => ({})); throw new Error(messages[value.detail] ?? "Cleanup could not be confirmed. Refresh to check the saved request before retrying."); }
  return response.json();
}
export const deviceErasure = {
  status: () => request<ErasureStatus>(),
  action: (value: object) => request(value),
  preview: (id: string) => request<ErasureReview>({ action: "preview", id }),
};
