/**
 * Device-local copy of Agent records so the task list and recently opened
 * conversations show at once after a page switch or an app restart. The node
 * stays the source of truth: every screen still refreshes from it.
 *
 * Records are encrypted with a non-extractable AES-GCM key kept in IndexedDB,
 * the same protection as Private AI chat. When IndexedDB or Web Crypto is
 * unavailable nothing is persisted; plaintext never reaches browser storage.
 */
import type { AgentTask, AgentTaskSummary } from "./rynAgent";

const DB_NAME = "ryn-agent-task-cache";
const DB_VERSION = 1;
const KEY_STORE = "keys";
const RECORD_STORE = "records";
/** Opened conversations kept per node; older ones fall back to the node. */
export const PERSISTED_AGENT_TASKS = 30;
/** Very large transcripts are left to the node instead of slowing every save. */
const MAX_RECORD_CHARS = 8_000_000;
const WRITE_DELAY_MS = 1500;

interface EncryptedRecord {
  id: string;
  scope: string;
  kind: "list" | "task";
  savedAt: number;
  iv: string;
  ciphertext: string;
}

export interface PersistedAgentRecords {
  tasks: AgentTaskSummary[] | null;
  /** Most recently saved first. */
  details: AgentTask[];
}

let databasePromise: Promise<IDBDatabase> | null = null;
let keyPromise: Promise<CryptoKey> | null = null;
let writer: Promise<void> = Promise.resolve();
const pending = new Map<string, { timer: ReturnType<typeof setTimeout>; run: () => Promise<void> }>();

function available() {
  // Component tests switch the device copy off so one test's records never
  // seed another's screen; the store's own tests switch it back on.
  if ((globalThis as { __rynAgentRecordsDisabled?: boolean }).__rynAgentRecordsDisabled) return false;
  return typeof indexedDB !== "undefined" && typeof crypto !== "undefined" && Boolean(crypto.subtle);
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

function openDatabase(): Promise<IDBDatabase> {
  if (!available()) return Promise.reject(new Error("agent_cache_unavailable"));
  if (databasePromise) return databasePromise;
  databasePromise = new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(KEY_STORE)) database.createObjectStore(KEY_STORE, { keyPath: "id" });
      if (!database.objectStoreNames.contains(RECORD_STORE)) {
        database.createObjectStore(RECORD_STORE, { keyPath: "id" }).createIndex("scope", "scope");
      }
    };
    request.onblocked = () => reject(new Error("agent_cache_blocked"));
    request.onsuccess = () => {
      request.result.onversionchange = () => { request.result.close(); databasePromise = null; keyPromise = null; };
      resolve(request.result);
    };
    request.onerror = () => reject(request.error ?? new Error("agent_cache_open_failed"));
  }).catch(cause => { databasePromise = null; throw cause; });
  return databasePromise;
}

function encryptionKey(): Promise<CryptoKey> {
  if (keyPromise) return keyPromise;
  keyPromise = (async () => {
    const database = await openDatabase();
    const read = database.transaction(KEY_STORE, "readonly");
    const readDone = transactionDone(read);
    const stored = await requestResult(read.objectStore(KEY_STORE).get("primary")) as { key?: CryptoKey } | undefined;
    await readDone;
    if (stored?.key) return stored.key;
    const key = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
    const write = database.transaction(KEY_STORE, "readwrite");
    const writeDone = transactionDone(write);
    // Two windows may race to create the key; keep whichever landed first.
    const raced = await requestResult(write.objectStore(KEY_STORE).get("primary")) as { key?: CryptoKey } | undefined;
    if (!raced?.key) write.objectStore(KEY_STORE).put({ id: "primary", key });
    await writeDone;
    return raced?.key || key;
  })().catch(cause => { keyPromise = null; throw cause; });
  return keyPromise;
}

function toBase64(bytes: Uint8Array) {
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  return btoa(binary);
}

function fromBase64(value: string) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

const recordID = (scope: string, kind: "list" | "task", taskID = "") => `${scope}\n${kind}\n${taskID}`;

function serialize(task: unknown) {
  const text = JSON.stringify(task);
  return text.length > MAX_RECORD_CHARS ? null : text;
}

async function seal(scope: string, kind: "list" | "task", taskID: string, text: string): Promise<EncryptedRecord> {
  const key = await encryptionKey();
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, new TextEncoder().encode(text));
  return { id: recordID(scope, kind, taskID), scope, kind, savedAt: Date.now(), iv: toBase64(iv), ciphertext: toBase64(new Uint8Array(ciphertext)) };
}

async function open<T>(record: EncryptedRecord): Promise<T> {
  const key = await encryptionKey();
  const plaintext = await crypto.subtle.decrypt({ name: "AES-GCM", iv: fromBase64(record.iv) }, key, fromBase64(record.ciphertext));
  return JSON.parse(new TextDecoder().decode(plaintext)) as T;
}

function serial(operation: () => Promise<void>): Promise<void> {
  const next = writer.then(operation, operation).catch(() => undefined);
  writer = next;
  return next;
}

async function scopeRecords(database: IDBDatabase, scope: string): Promise<EncryptedRecord[]> {
  const transaction = database.transaction(RECORD_STORE, "readonly");
  const done = transactionDone(transaction);
  const records = await requestResult(transaction.objectStore(RECORD_STORE).index("scope").getAll(scope)) as EncryptedRecord[];
  await done;
  return records;
}

/** Reads one node's records. Unreadable records are dropped, never surfaced. */
export async function loadAgentRecords(scope: string): Promise<PersistedAgentRecords> {
  const empty: PersistedAgentRecords = { tasks: null, details: [] };
  if (!available()) return empty;
  try {
    await writer;
    const records = await scopeRecords(await openDatabase(), scope);
    let tasks: AgentTaskSummary[] | null = null;
    const details: Array<{ savedAt: number; task: AgentTask }> = [];
    for (const record of records) {
      try {
        if (record.kind === "list") {
          const value = await open<unknown>(record);
          if (Array.isArray(value)) tasks = value.filter(isTaskLike) as AgentTaskSummary[];
        } else {
          const value = await open<unknown>(record);
          if (isTaskLike(value) && Array.isArray((value as AgentTask).messages) && Array.isArray((value as AgentTask).events)) {
            details.push({ savedAt: record.savedAt, task: value as AgentTask });
          }
        }
      } catch { /* A record from a replaced key or an older format is ignored. */ }
    }
    details.sort((left, right) => right.savedAt - left.savedAt);
    return { tasks, details: details.map(item => item.task) };
  } catch {
    return empty;
  }
}

function isTaskLike(value: unknown): boolean {
  const item = value as AgentTaskSummary | null;
  return Boolean(item && typeof item === "object" && typeof item.id === "string" && typeof item.revision === "number" && typeof item.title === "string");
}

let flushOnHide = false;
/**
 * Throttles each record: a running task changes every half second, but its
 * copy is serialized and encrypted at most once per WRITE_DELAY_MS, always
 * with the newest value.
 */
function schedule(id: string, run: () => Promise<void>) {
  if (!flushOnHide && typeof document !== "undefined") {
    flushOnHide = true;
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") void flushAgentRecords(); });
    window.addEventListener("pagehide", () => { void flushAgentRecords(); });
  }
  const queued = pending.get(id);
  if (queued) { queued.run = run; return; }
  const entry = { run, timer: setTimeout(() => { pending.delete(id); void serial(entry.run); }, WRITE_DELAY_MS) };
  pending.set(id, entry);
}

/** Writes every queued record now; used when the window is hidden or closed. */
export function flushAgentRecords(): Promise<void> {
  const runs = [...pending.values()];
  pending.clear();
  for (const item of runs) { clearTimeout(item.timer); void serial(item.run); }
  return writer;
}

export function saveAgentTaskList(scope: string, tasks: AgentTaskSummary[]) {
  if (!available()) return;
  schedule(recordID(scope, "list"), async () => {
    const text = serialize(tasks.map(agentTaskSummary));
    if (text === null) return;
    const record = await seal(scope, "list", "", text);
    const database = await openDatabase();
    const transaction = database.transaction(RECORD_STORE, "readwrite");
    const done = transactionDone(transaction);
    transaction.objectStore(RECORD_STORE).put(record);
    await done;
  });
}

export function saveAgentTask(scope: string, task: AgentTask) {
  if (!available()) return;
  const id = recordID(scope, "task", task.id);
  schedule(id, async () => {
    const text = serialize(task);
    if (text === null) {
      const database = await openDatabase();
      const transaction = database.transaction(RECORD_STORE, "readwrite");
      const done = transactionDone(transaction);
      transaction.objectStore(RECORD_STORE).delete(id);
      await done;
      return;
    }
    const record = await seal(scope, "task", task.id, text);
    const database = await openDatabase();
    const transaction = database.transaction(RECORD_STORE, "readwrite");
    const done = transactionDone(transaction);
    const store = transaction.objectStore(RECORD_STORE);
    store.put(record);
    const all = await requestResult(store.index("scope").getAll(scope)) as EncryptedRecord[];
    const opened = all.filter(item => item.kind === "task" && item.id !== id).sort((left, right) => right.savedAt - left.savedAt);
    for (const old of opened.slice(PERSISTED_AGENT_TASKS - 1)) store.delete(old.id);
    await done;
  });
}

export function removeAgentTask(scope: string, taskID: string) {
  const id = recordID(scope, "task", taskID);
  const queued = pending.get(id);
  if (queued) { clearTimeout(queued.timer); pending.delete(id); }
  if (!available()) return;
  void serial(async () => {
    const database = await openDatabase();
    const transaction = database.transaction(RECORD_STORE, "readwrite");
    const done = transactionDone(transaction);
    transaction.objectStore(RECORD_STORE).delete(id);
    await done;
  });
}

/** Removes every saved Agent record on this device. */
export async function clearAgentRecords(): Promise<void> {
  for (const item of pending.values()) clearTimeout(item.timer);
  pending.clear();
  if (!available()) return;
  await serial(async () => {
    const database = await openDatabase();
    const transaction = database.transaction(RECORD_STORE, "readwrite");
    const done = transactionDone(transaction);
    transaction.objectStore(RECORD_STORE).clear();
    await done;
  });
}

const DETAIL_FIELDS = ["streaming", "messages", "pending_user_messages", "events", "limits"] as const;
/** The sidebar only needs summary fields; transcripts stay in the per-task records. */
export function agentTaskSummary<T extends AgentTaskSummary>(task: T): AgentTaskSummary {
  if (!DETAIL_FIELDS.some(field => field in task)) return task;
  const copy = { ...task } as Record<string, unknown>;
  for (const field of DETAIL_FIELDS) delete copy[field];
  return copy as unknown as AgentTaskSummary;
}
