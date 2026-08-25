export type LLMChatRole = "user" | "assistant";
export type LLMChatMessageStatus = "complete" | "failed" | "cancelled";

export interface LLMChatMessage {
  id: string;
  role: LLMChatRole;
  content: string;
  createdAt: string;
  status: LLMChatMessageStatus;
  taskId?: string;
  inputTokens?: number;
  outputTokens?: number;
  cost?: number;
}

export interface LLMConversation {
  id: string;
  title: string;
  serviceKey: string;
  serviceName: string;
  providerPeerId: string;
  networkId: string;
  createdAt: string;
  updatedAt: string;
  messages: LLMChatMessage[];
}

interface EncryptedConversationRecord {
  id: string;
  serviceKey: string;
  updatedAt: string;
  iv: string;
  ciphertext: string;
}

interface StoredKey {
  id: "primary";
  key: CryptoKey;
}

const DB_NAME = "ryn-private-ai-chat";
const DB_VERSION = 1;
const KEY_STORE = "keys";
const CONVERSATION_STORE = "conversations";
const memoryFallback = new Map<string, LLMConversation>();
let databasePromise: Promise<IDBDatabase> | null = null;
let keyPromise: Promise<CryptoKey> | null = null;

function cloneConversation(conversation: LLMConversation): LLMConversation {
  return JSON.parse(JSON.stringify(conversation)) as LLMConversation;
}

function requestResult<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed"));
  });
}

function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB transaction failed"));
    transaction.onabort = () => reject(transaction.error ?? new Error("IndexedDB transaction aborted"));
  });
}

function supportsEncryptedPersistence() {
  return typeof indexedDB !== "undefined" && typeof crypto !== "undefined" && Boolean(crypto.subtle);
}

function openDatabase(): Promise<IDBDatabase> {
  if (!supportsEncryptedPersistence()) return Promise.reject(new Error("Encrypted persistence is unavailable"));
  if (databasePromise) return databasePromise;
  databasePromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(KEY_STORE)) database.createObjectStore(KEY_STORE, { keyPath: "id" });
      if (!database.objectStoreNames.contains(CONVERSATION_STORE)) database.createObjectStore(CONVERSATION_STORE, { keyPath: "id" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("Unable to open encrypted conversation storage"));
  });
  return databasePromise;
}

async function getEncryptionKey(): Promise<CryptoKey> {
  if (keyPromise) return keyPromise;
  keyPromise = (async () => {
    const database = await openDatabase();
    const readTransaction = database.transaction(KEY_STORE, "readonly");
    const readDone = transactionDone(readTransaction);
    const stored = await requestResult(readTransaction.objectStore(KEY_STORE).get("primary")) as StoredKey | undefined;
    await readDone;
    if (stored?.key) return stored.key;

    const key = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
    const writeTransaction = database.transaction(KEY_STORE, "readwrite");
    const writeDone = transactionDone(writeTransaction);
    writeTransaction.objectStore(KEY_STORE).put({ id: "primary", key } satisfies StoredKey);
    await writeDone;
    return key;
  })();
  return keyPromise;
}

function bytesToBase64(bytes: Uint8Array) {
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return btoa(binary);
}

function base64ToBytes(value: string) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

async function encryptConversation(conversation: LLMConversation): Promise<EncryptedConversationRecord> {
  const key = await getEncryptionKey();
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const plaintext = new TextEncoder().encode(JSON.stringify(conversation));
  const ciphertext = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, plaintext);
  return {
    id: conversation.id,
    serviceKey: conversation.serviceKey,
    updatedAt: conversation.updatedAt,
    iv: bytesToBase64(iv),
    ciphertext: bytesToBase64(new Uint8Array(ciphertext)),
  };
}

async function decryptConversation(record: EncryptedConversationRecord): Promise<LLMConversation> {
  const key = await getEncryptionKey();
  const plaintext = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: base64ToBytes(record.iv) },
    key,
    base64ToBytes(record.ciphertext),
  );
  return JSON.parse(new TextDecoder().decode(plaintext)) as LLMConversation;
}

export function createConversation(input: {
  serviceKey: string;
  serviceName: string;
  providerPeerId: string;
  networkId: string;
}): LLMConversation {
  const now = new Date().toISOString();
  return {
    id: typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : `conversation_${Date.now()}_${Math.random().toString(16).slice(2)}`,
    title: "New conversation",
    serviceKey: input.serviceKey,
    serviceName: input.serviceName,
    providerPeerId: input.providerPeerId,
    networkId: input.networkId,
    createdAt: now,
    updatedAt: now,
    messages: [],
  };
}

export function titleFromPrompt(prompt: string) {
  const compact = prompt.trim().replace(/\s+/g, " ");
  if (!compact) return "New conversation";
  return compact.length > 46 ? `${compact.slice(0, 46).trimEnd()}…` : compact;
}

export function buildConversationPrompt(messages: LLMChatMessage[]) {
  const complete = messages.filter((message) => message.status === "complete" && message.content.trim());
  if (complete.length <= 1 && complete[0]?.role === "user") return complete[0].content;
  const transcript = complete.map((message) => `${message.role === "user" ? "User" : "Assistant"}: ${message.content}`).join("\n\n");
  return `Continue the conversation below. Answer the latest user message directly.\n\n${transcript}\n\nAssistant:`;
}

export async function saveConversation(conversation: LLMConversation) {
  const snapshot = cloneConversation(conversation);
  try {
    const database = await openDatabase();
    const record = await encryptConversation(snapshot);
    const transaction = database.transaction(CONVERSATION_STORE, "readwrite");
    const done = transactionDone(transaction);
    transaction.objectStore(CONVERSATION_STORE).put(record);
    await done;
  } catch {
    memoryFallback.set(snapshot.id, snapshot);
  }
}

export async function listConversations(serviceKey: string) {
  try {
    const database = await openDatabase();
    const transaction = database.transaction(CONVERSATION_STORE, "readonly");
    const done = transactionDone(transaction);
    const records = await requestResult(transaction.objectStore(CONVERSATION_STORE).getAll()) as EncryptedConversationRecord[];
    await done;
    const decrypted = await Promise.all(records.filter((record) => record.serviceKey === serviceKey).map(decryptConversation));
    return decrypted.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  } catch {
    return [...memoryFallback.values()]
      .filter((conversation) => conversation.serviceKey === serviceKey)
      .map(cloneConversation)
      .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  }
}

export async function deleteConversation(conversationId: string) {
  memoryFallback.delete(conversationId);
  try {
    const database = await openDatabase();
    const transaction = database.transaction(CONVERSATION_STORE, "readwrite");
    const done = transactionDone(transaction);
    transaction.objectStore(CONVERSATION_STORE).delete(conversationId);
    await done;
  } catch {
    // The in-memory record was already removed.
  }
}

export async function clearConversations(serviceKey: string) {
  [...memoryFallback.values()].forEach((conversation) => {
    if (conversation.serviceKey === serviceKey) memoryFallback.delete(conversation.id);
  });
  try {
    const database = await openDatabase();
    const readTransaction = database.transaction(CONVERSATION_STORE, "readonly");
    const readDone = transactionDone(readTransaction);
    const records = await requestResult(readTransaction.objectStore(CONVERSATION_STORE).getAll()) as EncryptedConversationRecord[];
    await readDone;
    const writeTransaction = database.transaction(CONVERSATION_STORE, "readwrite");
    const writeDone = transactionDone(writeTransaction);
    const store = writeTransaction.objectStore(CONVERSATION_STORE);
    records.filter((record) => record.serviceKey === serviceKey).forEach((record) => store.delete(record.id));
    await writeDone;
  } catch {
    // Nothing persisted outside the session fallback.
  }
}

export async function conversationStorageMode(): Promise<"encrypted" | "session-only"> {
  try {
    await getEncryptionKey();
    return "encrypted";
  } catch {
    return "session-only";
  }
}
