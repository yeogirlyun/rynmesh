import * as legacy from "./llmConversationStore";
import type { LLMConversation } from "./llmConversationStore";
import { nodeControlUrl } from "./nodeUrl";

const errors: Record<string, string> = {
  ask_revision_conflict: "This conversation changed in another view. Reload its history before continuing; your input has been kept.",
  ask_conversation_deleted: "This conversation was deleted. Start a new conversation; it will not be restored automatically.",
  ask_service_binding_mismatch: "This history belongs to its original provider and service. Start a separate conversation to switch.",
  ask_history_version_unsupported: "Saved conversations require a newer version of Ryn. Your files have been kept unchanged.",
  ask_history_unreadable: "Saved conversations could not be decrypted. Check this node's identity and restore its backup.",
  ask_history_limit: "Conversation storage is full or this conversation is too large. Export and remove older history before retrying.",
  ask_migration_conflict: "This older conversation differs from the copy already on this node. Both copies have been kept for review.",
};

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
    throw new Error(errors[value.detail ?? ""] ?? "Conversation history is unavailable. Reconnect to your node and reload history.");
  }
  return response.json() as Promise<T>;
}

export const askHistory = {
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
