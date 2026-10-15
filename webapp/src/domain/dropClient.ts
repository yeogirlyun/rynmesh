// Device drop API (docs/DEVICE_DROP.md). Byte values may be decimal strings
// above 2^53-1, so every size goes through bytesValue() and BigInt maths.
import { nodeControlBaseUrl } from "./nodeUrl";

export type Bytes = number | string;
export type Route = "lan" | "public" | "relay";
export type Relationship = "own" | "friend" | "nearby" | "other";

export type DropCapabilities = {
  direct_size_limit: null;
  routes: Route[];
  nat_traversal: boolean;
  relay: { available: boolean; reason: string; max_bytes: Bytes | null; quota_bytes: Bytes | null };
  folders: boolean;
  path_send: boolean;
  staged_upload: boolean;
  receive_dir: string;
  receive_free_bytes: Bytes | null;
};

export type PeerMeta = {
  peer_id: string;
  relationship: Relationship;
  name: string;
  online: boolean | null;
  route: Route | null;
  address: string;
  latency_ms: number | null;
  sendable: boolean;
  send_mode: "send" | "queue" | "verify" | "unavailable";
  reason: string;
  verification: { required: boolean; code: string | null; verified: boolean };
  auto_accept: boolean;
  unread: number;
  last: { kind: "text" | "link" | "system" | "transfer"; text: string; at: string } | null;
  /** Optional extras; rendered only when the daemon reports them. */
  last_seen?: string | null;
  platform?: string;
};

export type MessageStatus = "waiting_online" | "sending" | "delivered" | "failed";
export type DropMessage = {
  message_id: string;
  peer_id: string;
  direction: "out" | "in";
  kind: "text" | "link" | "system";
  text: string;
  created_at: string;
  status: MessageStatus;
  delivered_at: string | null;
  error: string;
  version: number;
  client_id?: string;
};

export type TransferStatus =
  | "connecting" | "waiting_online" | "awaiting_accept" | "transferring" | "paused"
  | "reconnecting" | "verifying" | "completed" | "insufficient_space" | "rejected"
  | "cancelled" | "source_changed" | "verify_failed" | "failed" | "relay_confirmation";

export type TransferAction =
  | "pause" | "resume" | "cancel" | "retry" | "accept" | "reject" | "open" | "reveal" | "send_again" | "reselect";

export type Transfer = {
  transfer_id: string;
  offer_id: string;
  peer_id: string;
  direction: "out" | "in";
  kind: "file" | "files" | "folder";
  name: string;
  file_count: number;
  total_bytes: Bytes;
  confirmed_bytes: Bytes;
  status: TransferStatus;
  route: Route | null;
  speed_bps: number | null;
  eta_s: number | null;
  error: { code: string; message: string; need_bytes?: Bytes; free_bytes?: Bytes } | null;
  note: string;
  save_dir: string;
  final_path: string;
  verification_code: string | null;
  actions: TransferAction[];
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  version: number;
  files_preview: { rel_path: string; size: Bytes }[];
  files?: { rel_path: string; size: Bytes }[];
};

export type ConversationItem =
  | { type: "message"; at: string; message: DropMessage }
  | { type: "transfer"; at: string; transfer: Transfer };

export type DropEvent =
  | { seq: number; type: "transfer"; transfer: Transfer }
  | { seq: number; type: "message"; message: DropMessage }
  | { seq: number; type: "peer"; peer: PeerMeta }
  | { seq: number; type: "resync" };

export type DropSnapshot = { seq: number; transfers: Transfer[]; peers: PeerMeta[]; incoming: number; boot_id?: string };

/** Proposed contract (sent to backend owner); unknown fields are tolerated. */
export type Preflight = {
  ok: boolean;
  route?: Route | null;
  send_mode?: PeerMeta["send_mode"];
  file_count?: number;
  total_bytes?: Bytes;
  receiver_free_bytes?: Bytes | null;
  issues?: { code: string; message: string; path?: string; need_bytes?: Bytes; free_bytes?: Bytes }[];
  items?: { name: string; kind: "file" | "folder"; file_count: number; size: Bytes }[];
};

export type DropPrefs = {
  receive_dir: string;
  receive_free_bytes?: Bytes | null;
  peers?: Record<string, { auto_accept: boolean }>;
};

export type UploadResult = { upload_id: string; name: string; rel_path: string; size: Bytes };

export class DropApiError extends Error {
  constructor(message: string, readonly status: number, readonly code = "") {
    super(message);
  }
}

// ---- Byte helpers ---------------------------------------------------------

export function bytesValue(value: Bytes | bigint | null | undefined): bigint {
  if (value === null || value === undefined || value === "") return 0n;
  if (typeof value === "bigint") return value;
  if (typeof value === "number") return Number.isFinite(value) ? BigInt(Math.max(0, Math.trunc(value))) : 0n;
  return /^\d+$/.test(value.trim()) ? BigInt(value.trim()) : 0n;
}

const UNITS = ["B", "KB", "MB", "GB", "TB", "PB", "EB"];

/** Decimal units, one decimal place below 100 (64.8 GB, 100 GB, 2.4 MB). */
export function formatBytes(value: Bytes | bigint | null | undefined): string {
  const bytes = bytesValue(value);
  let unit = 0;
  let scale = 1n;
  while (unit < UNITS.length - 1 && bytes >= scale * 1000n) { scale *= 1000n; unit += 1; }
  if (unit === 0) return `${bytes} B`;
  const tenths = Number((bytes * 10n + scale / 2n) / scale) / 10;
  const text = tenths >= 100 ? String(Math.round(tenths)) : tenths.toFixed(1).replace(/\.0$/, "");
  return `${text} ${UNITS[unit]}`;
}

/** "64.8 / 100 GB" — both values in the unit of the total. */
export function formatProgressBytes(done: Bytes, total: Bytes): string {
  const totalBytes = bytesValue(total);
  const doneBytes = bytesValue(done);
  let unit = 0;
  let scale = 1n;
  while (unit < UNITS.length - 1 && totalBytes >= scale * 1000n) { scale *= 1000n; unit += 1; }
  if (unit === 0) return `${doneBytes} / ${totalBytes} B`;
  const fmt = (b: bigint) => {
    const tenths = Number((b * 10n + scale / 2n) / scale) / 10;
    return tenths >= 100 ? String(Math.round(tenths)) : tenths.toFixed(1).replace(/\.0$/, "");
  };
  return `${fmt(doneBytes)} / ${fmt(totalBytes)} ${UNITS[unit]}`;
}

export function formatSpeed(bps: number | null | undefined): string {
  if (bps === null || bps === undefined || !Number.isFinite(bps) || bps <= 0) return "";
  return `${formatBytes(Math.round(bps))}/s`;
}

/** Percentage (0–100) with two decimals of precision, never above 100. */
export function progressPercent(done: Bytes, total: Bytes): number {
  const t = bytesValue(total);
  if (t === 0n) return 0;
  const d = bytesValue(done);
  const basis = Number((d * 10000n) / t) / 100;
  return Math.min(100, Math.max(0, basis));
}

export function sumBytes(values: (Bytes | bigint | null | undefined)[]): bigint {
  return values.reduce<bigint>((total, value) => total + bytesValue(value), 0n);
}

// ---- HTTP -----------------------------------------------------------------

export const dropBaseUrl = () => `${nodeControlBaseUrl()}/drop`;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${dropBaseUrl()}${path}`, {
    credentials: "include",
    ...init,
    headers: init?.body && !(init.body instanceof Blob) ? { "Content-Type": "application/json", ...(init.headers ?? {}) } : init?.headers,
  });
  if (!response.ok) {
    let detail = "";
    let code = "";
    try {
      const payload = (await response.json()) as { detail?: unknown; code?: unknown; error?: unknown };
      if (typeof payload.detail === "string") detail = payload.detail;
      else if (payload.detail && typeof payload.detail === "object") {
        const d = payload.detail as { message?: unknown; code?: unknown };
        if (typeof d.message === "string") detail = d.message;
        if (typeof d.code === "string") code = d.code;
      }
      if (typeof payload.code === "string") code = payload.code;
    } catch { /* Status code is still meaningful. */ }
    throw new DropApiError(detail || `HTTP ${response.status}`, response.status, code);
  }
  return (await response.json()) as T;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
const q = (params: Record<string, string | number | undefined>) => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value !== undefined && value !== "") search.set(key, String(value));
  const text = search.toString();
  return text ? `?${text}` : "";
};

export const dropApi = {
  capabilities: () => request<DropCapabilities>("/capabilities"),
  peers: () => request<PeerMeta[]>("/peers"),
  snapshot: () => request<DropSnapshot>("/snapshot"),
  eventsUrl: (since: number) => `${dropBaseUrl()}/events${q({ since })}`,
  conversation: (peerId: string, before?: string, limit?: number) =>
    request<{ peer_id: string; items: ConversationItem[] }>(`/conversation${q({ peer_id: peerId, before, limit })}`),
  markRead: (peerId: string) => post<{ ok: boolean }>("/conversation/read", { peer_id: peerId }),
  sendMessage: (peerId: string, text: string, clientId: string) =>
    post<DropMessage>("/messages", { peer_id: peerId, text, client_id: clientId }),
  retryMessage: (messageId: string) => post<DropMessage>("/messages/retry", { message_id: messageId }),
  preflight: (body: { peer_id: string; paths?: string[]; upload_ids?: string[] }) => post<Preflight>("/preflight", body),
  createTransfer: (body: { peer_id: string; paths?: string[]; upload_ids?: string[]; note?: string; client_id: string }) =>
    post<Transfer>("/transfers", body),
  transfers: (peerId?: string, filter: "all" | "active" | "done" = "all") =>
    request<Transfer[]>(`/transfers${q({ peer_id: peerId, filter })}`),
  transfer: (id: string) => request<Transfer>(`/transfers/${encodeURIComponent(id)}`),
  transferAction: (id: string, action: "pause" | "resume" | "cancel" | "retry" | "reject", body?: unknown) =>
    post<Transfer>(`/transfers/${encodeURIComponent(id)}/${action}`, body),
  accept: (id: string, body: { save_dir?: string; auto_accept?: boolean }) =>
    post<Transfer>(`/transfers/${encodeURIComponent(id)}/accept`, body),
  /** `delegate`: resolve the path only; the desktop shell opens it itself. */
  openFile: (id: string, how: "open" | "reveal", delegate = false) =>
    post<OpenResult>(`/transfers/${encodeURIComponent(id)}/${how}`, delegate ? { delegate: true } : undefined),
  prefs: () => request<DropPrefs>("/prefs"),
  savePrefs: (body: Partial<DropPrefs>) => request<DropPrefs>("/prefs", { method: "PUT", body: JSON.stringify(body) }),
  verifyPeer: (peerId: string, accept: boolean) => post<PeerMeta>("/peers/verify", { peer_id: peerId, accept }),
  /** Streams the File as the request body; never buffers or base64-encodes it. */
  upload: (file: File, relPath: string, batch: string, signal?: AbortSignal) =>
    request<UploadResult>(`/uploads${q({ name: file.name, rel: relPath, batch })}`, {
      method: "PUT", body: file, signal, headers: { "Content-Type": "application/octet-stream" },
    }),
};

export function newClientId(): string {
  // Also used as a staging batch directory: the daemon requires hex only.
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join("");
}

// ---- Status helpers -------------------------------------------------------

export const ACTIVE_STATUSES: TransferStatus[] = [
  "connecting", "waiting_online", "awaiting_accept", "transferring", "paused", "reconnecting", "verifying",
  "insufficient_space", "relay_confirmation",
];
export const isActiveTransfer = (t: Transfer) => ACTIVE_STATUSES.includes(t.status);
export const isDoneTransfer = (t: Transfer) => t.status === "completed";
export type OpenResult = { ok: boolean; path: string; how: "open" | "reveal" };

export const isIncomingRequest = (t: Transfer) => t.direction === "in" && ["awaiting_accept", "insufficient_space"].includes(t.status);
