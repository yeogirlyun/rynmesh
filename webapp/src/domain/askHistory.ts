import * as legacy from "./llmConversationStore";
import type { LLMConversation } from "./llmConversationStore";
import { nodeControlUrl } from "./nodeUrl";

const errors: Record<string, string> = {
  ask_run_busy: "This conversation already has an active task. Wait for it or cancel it before sending another question.",
  ask_preview_changed: "The prepared input changed after review. Review the current question and sources again before sending.",
  ask_run_identity_conflict: "This task ID belongs to a different request. Check its original result before continuing.",
  ask_run_not_found: "The node has no saved receipt for this task. Retry the same reviewed request to confirm it; do not create a different task.",
  ask_context_unavailable: "This source copy is missing, damaged or no longer readable. Reopen the article to prepare it again, or remove it from this conversation.",
  ask_context_changed: "This offline version changed or was cleared. Reopen the current copy and review it before asking.",
  ask_provider_unavailable: "The original provider is unavailable. Wait for it or start a separate conversation with another service.",
  ask_context_budget_unavailable: "This service has no usable context budget. Choose a service with a larger context window.",
  ask_question_too_large: "This question does not fit with the required prompt framing. Shorten it or choose a larger context window.",
  ask_revision_conflict: "This conversation changed in another view. Reload its history before continuing; your input has been kept.",
  ask_conversation_deleted: "This conversation was deleted. Start a new conversation; it will not be restored automatically.",
  ask_service_binding_mismatch: "This history belongs to its original provider and service. Start a separate conversation to switch.",
  ask_history_version_unsupported: "Saved conversations require a newer version of Ryn. Your files have been kept unchanged.",
  ask_history_unreadable: "Saved conversations could not be decrypted. Check this node's identity and restore its backup.",
  ask_history_limit: "Conversation storage is full or this conversation is too large. Export and remove older history before retrying.",
  ask_migration_conflict: "This older conversation differs from the copy already on this node. Both copies have been kept for review.",
};

export interface AskSource {
  library_id: string; title: string; source_url: string; sha256: string; extraction_truncated: boolean; text_bytes: number;
  source_number?: number; included_bytes?: number; budget_truncated?: boolean; text?: string;
}
export interface AskPreview {
  ai_permission?: { relationship_id: string; revision: number };
  conversation_id: string; revision: number; provider_peer_id: string; service_id: string;
  prompt: string; prompt_sha256: string; context_window: number; input_token_upper_estimate: number;
  framing_reserve: number; max_output_tokens: number; history_messages_omitted: number; sources: AskSource[];
}
export interface AskRun {
  task_id: string; conversation_id: string; state: string; cancel_requested: boolean; error_code?: string;
}
export interface AskRunRequest {
  ai_permission?: { relationship_id: string; revision: number };
  task_id: string; conversation_id: string; expected_revision: number; question: string; prompt_sha256: string;
}
export class AskRequestError extends Error {
  constructor(readonly status: number, message: string) { super(message); }
}

async function request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(nodeControlUrl(`/ask${path}`), { method, credentials: "include", headers: { "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  } catch {
    throw new Error("The node did not confirm saving this conversation. Reconnect and retry; keep this page open to retain your input.");
  }
  if (!response.ok) {
    const value = await response.json().catch(() => ({})) as { detail?: string };
    throw new AskRequestError(response.status, errors[value.detail ?? ""] ?? "Conversation history is unavailable. Reconnect to your node and reload history.");
  }
  return response.json() as Promise<T>;
}

export const askHistory = {
  beginRun: (body: AskRunRequest) => request<AskRun>("/runs", "POST", body),
  run: (taskId: string) => request<AskRun>(`/runs/${encodeURIComponent(taskId)}`),
  cancelRun: (taskId: string) => request<AskRun>(`/runs/${encodeURIComponent(taskId)}/cancel`, "POST"),
  prepareContext: (item_id: string, offline_job_id?: string) => request<AskSource>("/contexts", "POST", { item_id, offline_job_id, prefer_source: !offline_job_id }),
  context: (libraryId: string) => request<AskSource>(`/contexts/${encodeURIComponent(libraryId)}`),
  preview: (conversation: LLMConversation, question: string) => request<AskPreview>("/preview", "POST", { conversation_id: conversation.id, expected_revision: conversation.revision, question }),
  draft: () => request<{ text: string; revision: number }>("/draft"),
  saveDraft: (text: string, revision: number) => request<{ text: string; revision: number }>("/draft", "PUT", { text, expected_revision: revision }),
  list: async (serviceKey?: string) => (await request<{ conversations: LLMConversation[] }>(`/conversations${serviceKey ? `?service_key=${encodeURIComponent(serviceKey)}` : ""}`)).conversations,
  get: (id: string) => request<LLMConversation>(`/conversations/${encodeURIComponent(id)}`),
  save: (conversation: LLMConversation) => request<LLMConversation>(`/conversations/${encodeURIComponent(conversation.id)}`, "PUT", { conversation, expected_revision: conversation.revision ?? 0 }),
  remove: (conversation: LLMConversation) => request<{ removed: number }>(`/conversations/${encodeURIComponent(conversation.id)}`, "DELETE", { expected_revision: conversation.revision }),
  export: () => request<{ version: string; conversations: LLMConversation[] }>("/export"),
  async importLegacy() {
    const source = await legacy.readLegacyConversations();
    let imported = 0, retained = source.unreadable;
    for (const conversation of source.conversations) {
      try {
        await request("/migrate", "POST", { source: "ryn-private-ai-chat-v1", conversation });
        imported += 1;
      } catch { retained += 1; }
    }
    // The encrypted browser originals remain recovery copies, never live history.
    return { imported, retained };
  },
};

export function conversationRepository(mode: "live" | "fixture") {
  if (mode === "live") return {
    list: askHistory.list,
    save: askHistory.save,
    remove: askHistory.remove,
    async clear(serviceKey: string, networkId: string) {
      for (const conversation of await askHistory.list(serviceKey)) if (conversation.networkId === networkId) await askHistory.remove(conversation);
    },
    storageMode: async () => "node-encrypted" as const,
  };
  return {
    list: legacy.listConversations,
    async save(conversation: LLMConversation) { await legacy.saveConversation(conversation); return conversation; },
    async remove(conversation: LLMConversation) { await legacy.deleteConversation(conversation.id); return { removed: 1 }; },
    async clear(serviceKey: string, networkId: string) {
      for (const conversation of await legacy.listConversations(serviceKey)) if (conversation.networkId === networkId) await legacy.deleteConversation(conversation.id);
    },
    storageMode: legacy.conversationStorageMode,
  };
}
