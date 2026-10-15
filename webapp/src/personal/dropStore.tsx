import { useTranslation } from "react-i18next";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  dropApi, isIncomingRequest, newClientId,
  type ConversationItem, type DropCapabilities, type DropEvent, type DropMessage, type DropPrefs,
  type DropSnapshot, type PeerMeta, type Preflight, type Transfer,
} from "../domain/dropClient";
import { canPickPaths, openDropPath } from "../domain/dropDesktop";
import { requestDesktopNotificationPermission } from "../domain/notifications";

// One store for every drop surface: device strip, conversation cards, the
// transfers tab and the shell's incoming count all read the same objects.

export type DropData = {
  transfers: Record<string, Transfer>;
  messages: Record<string, DropMessage>;
  peers: Record<string, PeerMeta>;
  /** Conversation timelines per peer: "m:<message_id>" | "t:<transfer_id>". */
  convs: Record<string, { loaded: boolean; refs: string[] }>;
  seq: number;
  bootId?: string;
};

export const emptyDropData = (): DropData => ({ transfers: {}, messages: {}, peers: {}, convs: {}, seq: 0 });

const localId = (clientId: string) => `local:${clientId}`;

function addRef(data: DropData, peerId: string, ref: string, replace?: string) {
  const conv = data.convs[peerId];
  if (!conv?.loaded) return;
  if (conv.refs.includes(ref)) {
    if (replace && conv.refs.includes(replace)) data.convs[peerId] = { ...conv, refs: conv.refs.filter(r => r !== replace) };
    return;
  }
  const refs = replace && conv.refs.includes(replace) ? conv.refs.map(r => (r === replace ? ref : r)) : [...conv.refs, ref];
  data.convs[peerId] = { ...conv, refs };
}

/** Never applies an older version over a newer one (duplicate/out-of-order events). */
export function mergeTransfer(data: DropData, transfer: Transfer): boolean {
  const current = data.transfers[transfer.transfer_id];
  if (current && current.version > transfer.version) return false;
  if (current && current.version === transfer.version && !transfer.files) return false;
  data.transfers[transfer.transfer_id] = current?.files && !transfer.files ? { ...transfer, files: current.files } : transfer;
  addRef(data, transfer.peer_id, `t:${transfer.transfer_id}`);
  return true;
}

export function mergeMessage(data: DropData, message: DropMessage): boolean {
  const current = data.messages[message.message_id];
  if (current && current.version > message.version) return false;
  if (current && current.version === message.version) return false;
  data.messages[message.message_id] = message;
  const local = message.client_id ? localId(message.client_id) : "";
  const replace = local && data.messages[local] ? `m:${local}` : undefined;
  if (local && data.messages[local]) delete data.messages[local];
  addRef(data, message.peer_id, `m:${message.message_id}`, replace);
  return true;
}

export function applyEvent(data: DropData, event: DropEvent): DropData {
  if (event.type === "transfer") mergeTransfer(data, event.transfer);
  else if (event.type === "message") mergeMessage(data, event.message);
  else if (event.type === "peer") data.peers[event.peer.peer_id] = event.peer;
  if (typeof event.seq === "number" && event.seq > data.seq) data.seq = event.seq;
  return data;
}

export function applyEvents(previous: DropData, events: DropEvent[]): DropData {
  const next: DropData = {
    ...previous, transfers: { ...previous.transfers }, messages: { ...previous.messages },
    peers: { ...previous.peers }, convs: { ...previous.convs },
  };
  for (const event of events) applyEvent(next, event);
  return next;
}

export function applySnapshot(previous: DropData, snapshot: DropSnapshot): DropData {
  const fresh = previous.bootId !== undefined && snapshot.boot_id !== undefined && previous.bootId !== snapshot.boot_id;
  const base = fresh ? emptyDropData() : previous;
  const next: DropData = {
    ...base, transfers: { ...base.transfers }, messages: { ...base.messages },
    peers: {}, convs: { ...base.convs }, seq: snapshot.seq, bootId: snapshot.boot_id,
  };
  for (const peer of snapshot.peers || []) next.peers[peer.peer_id] = peer;
  for (const transfer of snapshot.transfers || []) mergeTransfer(next, transfer);
  return next;
}

export function applyConversation(previous: DropData, peerId: string, items: ConversationItem[]): DropData {
  const next: DropData = { ...previous, transfers: { ...previous.transfers }, messages: { ...previous.messages }, convs: { ...previous.convs } };
  // A history request can finish after a live event; keep those newer entries.
  const pendingLocal = previous.convs[peerId]?.refs || [];
  next.convs[peerId] = { loaded: true, refs: [] };
  for (const item of items) {
    if (item.type === "message") mergeMessage(next, item.message);
    else mergeTransfer(next, item.transfer);
    const ref = item.type === "message" ? `m:${item.message.message_id}` : `t:${item.transfer.transfer_id}`;
    if (!next.convs[peerId].refs.includes(ref)) next.convs[peerId] = { loaded: true, refs: [...next.convs[peerId].refs, ref] };
  }
  next.convs[peerId] = { loaded: true, refs: [...next.convs[peerId].refs, ...pendingLocal.filter(r => !next.convs[peerId].refs.includes(r)
    && (r.startsWith("m:") ? Boolean(next.messages[r.slice(2)]) : Boolean(next.transfers[r.slice(2)])))] };
  return next;
}

/** One card per peer and kind; `transferIds` holds everything it covers. */
export type DropNotice = { id: string; kind: "incoming" | "received"; peerId: string; transferIds: string[]; at: number };

const RECEIVED_NOTICE_MS = 12000;
const MAX_NOTICES = 4;

export type DropStatus = "disabled" | "loading" | "ready" | "unavailable";

export type DropState = {
  status: DropStatus;
  capabilities: DropCapabilities | null;
  data: DropData;
  transfers: Transfer[];
  incoming: Transfer[];
  isPending: (key: string) => boolean;
  /** Runs an action once per key; repeated clicks while pending are ignored. */
  run: <T>(key: string, action: () => Promise<T>) => Promise<T | undefined>;
  refresh: () => Promise<void>;
  loadConversation: (peerId: string) => Promise<void>;
  markRead: (peerId: string) => Promise<void>;
  sendMessage: (peerId: string, text: string) => Promise<void>;
  retryMessage: (messageId: string) => Promise<void>;
  transferAction: (transfer: Transfer, action: "pause" | "resume" | "cancel" | "retry" | "reject") => Promise<void>;
  acceptTransfer: (transfer: Transfer, body: { save_dir?: string; auto_accept?: boolean }) => Promise<void>;
  openTransfer: (transfer: Transfer, how: "open" | "reveal") => Promise<void>;
  preflight: (body: { peer_id: string; paths?: string[]; upload_ids?: string[] }) => Promise<Preflight>;
  createTransfer: (body: { peer_id: string; paths?: string[]; upload_ids?: string[]; note?: string; client_id: string }) => Promise<Transfer>;
  verifyPeer: (peerId: string, accept: boolean) => Promise<void>;
  loadPrefs: () => Promise<DropPrefs>;
  setAutoAccept: (peerId: string, value: boolean) => Promise<void>;
  notify: (tone: "ok" | "info" | "warn", text: string) => void;
  /** In-app cards for new requests and finished receives (newest last). */
  notices: DropNotice[];
  dismissNotice: (id: string) => void;
  /** false = closed, true = queue of requests, string = a specific transfer. */
  incomingOpen: boolean | string;
  setIncomingOpen: (open: boolean | string) => void;
};

const noop = async () => undefined;
const DISABLED: DropState = {
  status: "disabled", capabilities: null, data: emptyDropData(), transfers: [], incoming: [],
  isPending: () => false, run: async () => undefined, refresh: noop, loadConversation: noop, markRead: noop,
  sendMessage: noop, retryMessage: noop, transferAction: noop, acceptTransfer: noop, openTransfer: noop,
  preflight: async () => ({ ok: false, issues: [{ code: "unavailable", message: "" }] }),
  createTransfer: async () => { throw new Error("Device transfer is not available."); },
  verifyPeer: noop, loadPrefs: async () => ({ receive_dir: "" }), setAutoAccept: noop,
  notify: () => undefined, notices: [], dismissNotice: () => undefined,
  incomingOpen: false, setIncomingOpen: () => undefined,
};

export const DropContext = createContext<DropState | null>(null);
export function useDrop(): DropState {
  return useContext(DropContext) ?? DISABLED;
}

const isTauri = () => typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
/** The user is looking at Ryn: in-app cards are enough, no system notification. */
const appInFocus = () => typeof document !== "undefined" && document.visibilityState === "visible" && document.hasFocus();

let permission: Promise<boolean> | null = null;
/** Asked at most once per session, the first time something actually arrives. */
function notificationsAllowed(): Promise<boolean> {
  permission ??= requestDesktopNotificationPermission().then(result => result === "granted").catch(() => false);
  return permission;
}

async function systemNotify(title: string, body: string, tag: string, onOpen: () => void) {
  try {
    if (!(await notificationsAllowed())) return;
    if (isTauri()) {
      const plugin = await import("@tauri-apps/plugin-notification");
      plugin.sendNotification({ title, body, autoCancel: true, group: "rynmesh-drop" });
      // Desktop toasts cannot report clicks; flash the taskbar so returning to
      // Ryn (which opens the request) is one click away.
      const { getCurrentWindow, UserAttentionType } = await import("@tauri-apps/api/window");
      await getCurrentWindow().requestUserAttention(UserAttentionType.Informational).catch(() => undefined);
      return;
    }
    // Same tag replaces the previous card instead of stacking one per file.
    const notification = new Notification(title, { body, tag });
    notification.onclick = () => { window.focus(); onOpen(); notification.close(); };
  } catch { /* Notifications are best effort; in-app cards and the shell count stay visible. */ }
}

/** Groups transfers by peer, keeping arrival order. */
function byPeer(items: Transfer[]): Transfer[][] {
  const groups = new Map<string, Transfer[]>();
  for (const item of items) groups.set(item.peer_id, [...(groups.get(item.peer_id) || []), item]);
  return [...groups.values()];
}

export function DropProvider({ mode, children, notify }: {
  mode: string;
  notify?: (tone: "ok" | "info" | "warn", text: string) => void;
  children: ReactNode;
}) {
  const live = mode !== "fixture";
  const [data, setData] = useState<DropData>(emptyDropData);
  const [status, setStatus] = useState<DropStatus>(live ? "loading" : "disabled");
  const [capabilities, setCapabilities] = useState<DropCapabilities | null>(null);
  const [pending, setPending] = useState<Record<string, true>>({});
  const [incomingOpen, setIncomingOpen] = useState<boolean | string>(false);
  const pendingRef = useRef(new Set<string>());
  const queue = useRef<DropEvent[]>([]);
  const frame = useRef<number | null>(null);
  const dataRef = useRef(data);
  dataRef.current = data;
  const seen = useRef<{ incoming: Set<string>; received: Set<string> } | null>(null);
  const [notices, setNotices] = useState<DropNotice[]>([]);
  // Requests that arrived while Ryn was in the background open when it returns.
  const openOnReturn = useRef(false);
  const { t } = useTranslation();
  const tRef = useRef(t);
  tRef.current = t;
  const notifyRef2 = useRef(notify);
  notifyRef2.current = notify;

  const flush = useCallback(() => {
    frame.current = null;
    const events = queue.current.splice(0);
    if (events.length) setData(previous => applyEvents(previous, events));
  }, []);
  const enqueue = useCallback((event: DropEvent) => {
    queue.current.push(event);
    if (frame.current === null) frame.current = window.requestAnimationFrame(flush);
  }, [flush]);
  const mergeNow = useCallback((events: DropEvent[]) => setData(previous => applyEvents(previous, events)), []);

  const refresh = useCallback(async () => {
    if (!live) return;
    try {
      const [snapshot, caps] = await Promise.all([dropApi.snapshot(), dropApi.capabilities().catch(() => null)]);
      setData(previous => applySnapshot(previous, snapshot));
      if (caps) setCapabilities(caps);
      setStatus("ready");
    } catch {
      setStatus(current => (current === "ready" ? "ready" : "unavailable"));
      throw new Error("snapshot");
    }
  }, [live]);

  // Snapshot first, then events since its seq. On error or resync: refetch.
  useEffect(() => {
    if (!live) return;
    let disposed = false;
    let source: EventSource | null = null;
    let timer: number | undefined;
    let backoff = 1500;
    const connect = async () => {
      if (disposed) return;
      try {
        await refresh();
        backoff = 1500;
      } catch {
        timer = window.setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, 30000);
        return;
      }
      if (disposed || typeof EventSource === "undefined") return;
      source = new EventSource(dropApi.eventsUrl(dataRef.current.seq), { withCredentials: true });
      source.onmessage = message => {
        let event: DropEvent;
        try { event = JSON.parse(message.data) as DropEvent; } catch { return; }
        if (event.type === "resync") { source?.close(); source = null; void connect(); return; }
        enqueue(event);
      };
      source.onerror = () => {
        source?.close();
        source = null;
        if (disposed) return;
        timer = window.setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, 30000);
      };
    };
    void connect();
    return () => {
      disposed = true;
      source?.close();
      if (timer) window.clearTimeout(timer);
      if (frame.current !== null) window.cancelAnimationFrame(frame.current);
      frame.current = null;
    };
  }, [live, refresh, enqueue]);

  const transfers = useMemo(() => Object.values(data.transfers)
    .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || "")), [data.transfers]);
  const incoming = useMemo(() => transfers.filter(isIncomingRequest), [transfers]);

  // Announce each new request and each finished receive once (nothing from the
  // first snapshot). One card / system notification per peer, not per file.
  useEffect(() => {
    if (status !== "ready") return;
    const received = transfers.filter(item => item.direction === "in" && item.status === "completed");
    if (seen.current === null) {
      seen.current = { incoming: new Set(incoming.map(item => item.transfer_id)), received: new Set(received.map(item => item.transfer_id)) };
      return;
    }
    const known = seen.current;
    const newIncoming = incoming.filter(item => !known.incoming.has(item.transfer_id));
    const newReceived = received.filter(item => !known.received.has(item.transfer_id));
    if (!newIncoming.length && !newReceived.length) return;
    newIncoming.forEach(item => known.incoming.add(item.transfer_id));
    newReceived.forEach(item => known.received.add(item.transfer_id));
    const t = tRef.current;
    const away = !appInFocus();
    const describe = (group: Transfer[]) => {
      const device = dataRef.current.peers[group[0].peer_id]?.name || t("drop.unknownDevice");
      return { device, what: group.length === 1 ? group[0].name : t("drop.itemsCount", { count: group.length }) };
    };
    const now = Date.now();
    const cards: DropNotice[] = [];
    for (const group of byPeer(newIncoming)) {
      cards.push({ id: `incoming:${group[0].transfer_id}`, kind: "incoming", peerId: group[0].peer_id, transferIds: group.map(i => i.transfer_id), at: now });
      if (!away) continue;
      const { device, what } = describe(group);
      openOnReturn.current = true;
      void systemNotify(t("drop.notifyTitle"), t("drop.notifyBody", { device, name: what }), `rynmesh-drop-in:${group[0].peer_id}`,
        () => setIncomingOpen(group.length === 1 ? group[0].transfer_id : true));
    }
    for (const group of byPeer(newReceived)) {
      cards.push({ id: `received:${group[0].transfer_id}`, kind: "received", peerId: group[0].peer_id, transferIds: group.map(i => i.transfer_id), at: now });
      if (!away) continue;
      const { device, what } = describe(group);
      void systemNotify(t("drop.notifyReceivedTitle"), t("drop.notifyReceivedBody", { device, name: what }), `rynmesh-drop-done:${group[0].peer_id}`,
        () => undefined);
    }
    setNotices(current => [...current, ...cards].slice(-MAX_NOTICES));
  }, [incoming, transfers, status]);

  // Drop request cards once every transfer they cover was answered (here or elsewhere).
  useEffect(() => {
    setNotices(current => {
      const pending = new Set(incoming.map(item => item.transfer_id));
      const next = current.filter(notice => notice.kind !== "incoming" || notice.transferIds.some(id => pending.has(id)));
      return next.length === current.length ? current : next;
    });
  }, [incoming]);

  // Finished-receive cards fade on their own.
  useEffect(() => {
    const timed = notices.filter(notice => notice.kind === "received");
    if (!timed.length) return;
    const soonest = Math.min(...timed.map(notice => notice.at + RECEIVED_NOTICE_MS));
    const timer = window.setTimeout(() => setNotices(current => current.filter(notice =>
      notice.kind !== "received" || notice.at + RECEIVED_NOTICE_MS > Date.now())), Math.max(0, soonest - Date.now()) + 50);
    return () => window.clearTimeout(timer);
  }, [notices]);

  const incomingRef = useRef(incoming);
  incomingRef.current = incoming;
  useEffect(() => {
    const onReturn = () => {
      if (!openOnReturn.current) return;
      openOnReturn.current = false;
      if (incomingRef.current.length) setIncomingOpen(current => current || true);
    };
    window.addEventListener("focus", onReturn);
    return () => window.removeEventListener("focus", onReturn);
  }, []);

  const dismissNotice = useCallback((id: string) => setNotices(current => current.filter(notice => notice.id !== id)), []);

  const run = useCallback(async <T,>(key: string, action: () => Promise<T>) => {
    if (pendingRef.current.has(key)) return undefined;
    pendingRef.current.add(key);
    setPending(current => ({ ...current, [key]: true }));
    try {
      return await action();
    } finally {
      pendingRef.current.delete(key);
      setPending(current => { const next = { ...current }; delete next[key]; return next; });
    }
  }, []);

  const value = useMemo<DropState>(() => {
    const transferEvent = (transfer: Transfer): DropEvent => ({ seq: 0, type: "transfer", transfer });
    return {
      status, capabilities, data, transfers, incoming,
      isPending: key => Boolean(pending[key]),
      run, refresh: async () => { try { await refresh(); } catch { /* status reflects it */ } },
      loadConversation: async peerId => {
        const result = await dropApi.conversation(peerId);
        setData(previous => applyConversation(previous, peerId, result.items || []));
      },
      markRead: async peerId => { await dropApi.markRead(peerId).catch(() => undefined); },
      sendMessage: async (peerId, text) => {
        const clientId = newClientId();
        const local: DropMessage = {
          message_id: localId(clientId), client_id: clientId, peer_id: peerId, direction: "out", kind: "text",
          text, created_at: new Date().toISOString(), status: "sending", delivered_at: null, error: "", version: -1,
        };
        setData(previous => {
          const next = { ...previous, messages: { ...previous.messages, [local.message_id]: local }, convs: { ...previous.convs } };
          const conv = next.convs[peerId] || { loaded: true, refs: [] };
          next.convs[peerId] = { loaded: true, refs: [...conv.refs, `m:${local.message_id}`] };
          return next;
        });
        try {
          const message = await dropApi.sendMessage(peerId, text, clientId);
          mergeNow([{ seq: 0, type: "message", message: { ...message, client_id: message.client_id || clientId } }]);
        } catch (error) {
          setData(previous => !previous.messages[local.message_id] ? previous : ({
            ...previous,
            messages: { ...previous.messages, [local.message_id]: { ...local, status: "failed", error: error instanceof Error ? error.message : "" } },
          }));
          throw error;
        }
      },
      retryMessage: async messageId => {
        const current = dataRef.current.messages[messageId];
        if (messageId.startsWith("local:") && current) {
          // The POST may have succeeded before the response was lost. Reuse its
          // idempotency key so retry cannot deliver a second copy to the peer.
          const message = await dropApi.sendMessage(current.peer_id, current.text, current.client_id!);
          mergeNow([{ seq: 0, type: "message", message: { ...message, client_id: current.client_id } }]);
          return;
        }
        const message = await dropApi.retryMessage(messageId);
        mergeNow([{ seq: 0, type: "message", message }]);
      },
      transferAction: async (transfer, action) => {
        const result = await dropApi.transferAction(transfer.transfer_id, action);
        mergeNow([transferEvent(result)]);
      },
      acceptTransfer: async (transfer, body) => {
        const result = await dropApi.accept(transfer.transfer_id, body);
        mergeNow([transferEvent(result)]);
        if (result.status === "insufficient_space") throw new Error(result.error?.message || tRef.current("drop.st_insufficient_space_unknown"));
      },
      openTransfer: async (transfer, how) => {
        let done: "open" | "reveal";
        if (canPickPaths()) {
          const target = await dropApi.openFile(transfer.transfer_id, how, true);
          done = await openDropPath(target.path, target.how === "reveal");
        } else {
          done = (await dropApi.openFile(transfer.transfer_id, how)).how;
        }
        if (how === "open" && done === "reveal") notifyRef2.current?.("info", tRef.current("drop.openRevealedInstead"));
      },
      preflight: body => dropApi.preflight(body),
      createTransfer: async body => {
        const transfer = await dropApi.createTransfer(body);
        mergeNow([transferEvent(transfer)]);
        return transfer;
      },
      verifyPeer: async (peerId, accept) => {
        const peer = await dropApi.verifyPeer(peerId, accept);
        mergeNow([{ seq: 0, type: "peer", peer }]);
      },
      loadPrefs: () => dropApi.prefs(),
      setAutoAccept: async (peerId, auto) => {
        await dropApi.savePrefs({ peers: { [peerId]: { auto_accept: auto } } });
        const peer = dataRef.current.peers[peerId];
        if (peer) mergeNow([{ seq: 0, type: "peer", peer: { ...peer, auto_accept: auto } }]);
      },
      notify: (tone, text) => notifyRef2.current?.(tone, text),
      notices, dismissNotice,
      incomingOpen, setIncomingOpen,
    };
  }, [status, capabilities, data, transfers, incoming, pending, run, refresh, mergeNow, incomingOpen, notices, dismissNotice]);

  return <DropContext.Provider value={live ? value : { ...DISABLED, notify: value.notify, incomingOpen, setIncomingOpen }}>{children}</DropContext.Provider>;
}
