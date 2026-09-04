import { describe, expect, it } from "vitest";
import { createAssistantConversation, listAssistantConversations, saveAssistantConversation } from "./assistantStore";
import { createConversation, listConversations, saveConversation } from "./llmConversationStore";

function rawRecord(id: string): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("ryn-private-ai-chat", 1);
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
      const transaction = request.result.transaction("conversations", "readonly");
      const read = transaction.objectStore("conversations").get(id);
      read.onsuccess = () => resolve(read.result);
      read.onerror = () => reject(read.error);
    };
  });
}

describe("assistant conversation v2 store", () => {
  it("encrypts v2 messages without plaintext Web Storage", async () => {
    const conversation = createAssistantConversation({
      key: "local:test:model", label: "This device · test", privacy_boundary: "device",
      online: true, route_modes: ["local_chat"],
    });
    const marker = "ASSISTANT_PRIVATE_MARKER";
    conversation.messages.push({
      id: "message", role: "user", content: marker, status: "complete", createdAt: new Date().toISOString(),
    });
    await saveAssistantConversation(conversation);
    expect(JSON.stringify(await rawRecord(conversation.id))).not.toContain(marker);
    expect(localStorage.getItem(marker)).toBeNull();
    expect(sessionStorage.getItem(marker)).toBeNull();
  });

  it("migrates v1 Private AI history into an isolated friend-boundary conversation", async () => {
    const legacy = createConversation({
      serviceKey: "peer:legacy::package:model", serviceName: "Legacy friend model",
      providerPeerId: "peer:legacy", networkId: "rynmesh-main",
    });
    legacy.title = "Legacy private conversation";
    legacy.messages.push({
      id: "old", role: "user", content: "Old private prompt", status: "complete", createdAt: new Date().toISOString(),
    });
    await saveConversation(legacy);
    const assistant = await listAssistantConversations();
    const migrated = assistant.find((item) => item.migratedFrom === legacy.id);
    expect(migrated?.computeTarget.privacyBoundary).toBe("friend");
    expect(migrated?.computeTarget.key).toBe(legacy.serviceKey);
    expect(migrated?.messages[0].content).toBe("Old private prompt");
    expect((await listConversations(legacy.serviceKey)).some((item) => item.id === legacy.id)).toBe(true);
  });
});

