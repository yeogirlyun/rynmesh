import type { AssistantConversationV2, AssistantTarget } from "./assistantTypes";
import {
  deleteConversation,
  listAllConversations,
  listConversations,
  saveConversation as saveLegacyConversation,
  titleFromPrompt,
  type LLMConversation,
} from "./llmConversationStore";

const ASSISTANT_BUCKET = "assistant";

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

export function createAssistantConversation(target: AssistantTarget): AssistantConversationV2 {
  const now = new Date().toISOString();
  return {
    version: "ryn.assistant-conversation.v2",
    id: crypto.randomUUID(),
    title: "New conversation",
    archived: false,
    computeTarget: {
      key: target.key,
      label: target.label,
      privacyBoundary: target.privacy_boundary,
      providerPeerId: target.provider_peer_id,
      packageId: target.package_id,
      modelId: target.model_id,
    },
    contexts: [],
    messages: [],
    createdAt: now,
    updatedAt: now,
  };
}

function persistedShape(conversation: AssistantConversationV2): LLMConversation {
  return {
    ...clone(conversation),
    serviceKey: ASSISTANT_BUCKET,
    serviceName: conversation.computeTarget.label,
    providerPeerId: conversation.computeTarget.providerPeerId ?? "local",
    networkId: "rynmesh-main",
  } as unknown as LLMConversation;
}

export async function saveAssistantConversation(conversation: AssistantConversationV2) {
  await saveLegacyConversation(persistedShape(conversation));
}

function migrateLegacy(conversation: LLMConversation): AssistantConversationV2 {
  return {
    version: "ryn.assistant-conversation.v2",
    id: `migrated_${conversation.id}`,
    title: conversation.title,
    archived: false,
    computeTarget: {
      key: conversation.serviceKey,
      label: conversation.serviceName,
      privacyBoundary: "friend",
      providerPeerId: conversation.providerPeerId,
    },
    contexts: [],
    messages: conversation.messages.map((message) => ({
      id: message.id,
      role: message.role,
      content: message.content,
      status: message.status === "complete" ? "complete" : message.status,
      createdAt: message.createdAt,
      runId: message.taskId,
    })),
    createdAt: conversation.createdAt,
    updatedAt: conversation.updatedAt,
    migratedFrom: conversation.id,
  };
}

export async function listAssistantConversations() {
  const stored = (await listConversations(ASSISTANT_BUCKET)) as unknown as AssistantConversationV2[];
  const valid = stored.filter((item) => item.version === "ryn.assistant-conversation.v2");
  const migratedIds = new Set(valid.map((item) => item.migratedFrom).filter(Boolean));
  const legacy = (await listAllConversations()).filter((item) => (
    item.serviceKey !== ASSISTANT_BUCKET && !migratedIds.has(item.id)
  ));
  for (const item of legacy) {
    const migrated = migrateLegacy(item);
    await saveAssistantConversation(migrated);
    valid.push(migrated);
  }
  return valid.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
}

export async function deleteAssistantConversation(id: string) {
  await deleteConversation(id);
}

export { titleFromPrompt };

// The quick panel and full page deliberately share only opaque IDs and drafts.
interface SessionSnapshot {
  activeConversationId: string | null;
  drafts: Record<string, string>;
  revision: number;
  dataRevision: number;
}

let snapshot: SessionSnapshot = { activeConversationId: null, drafts: {}, revision: 0, dataRevision: 0 };
const listeners = new Set<() => void>();

function publish(next: Omit<SessionSnapshot, "revision" | "dataRevision">, touchData = false) {
  snapshot = {
    ...next,
    revision: snapshot.revision + 1,
    dataRevision: snapshot.dataRevision + (touchData ? 1 : 0),
  };
  listeners.forEach((listener) => listener());
}

export const assistantSession = {
  getSnapshot: () => snapshot,
  subscribe(listener: () => void) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
  setActive(id: string | null) {
    publish({ activeConversationId: id, drafts: snapshot.drafts });
  },
  setDraft(id: string, draft: string) {
    publish({ activeConversationId: snapshot.activeConversationId, drafts: { ...snapshot.drafts, [id]: draft } });
  },
  touchData() {
    publish({ activeConversationId: snapshot.activeConversationId, drafts: snapshot.drafts }, true);
  },
};
