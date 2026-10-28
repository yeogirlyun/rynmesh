import { Fragment, useEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ArrowLeft, ArrowUp, Ellipsis, Info, Lock, MessageCircle, Paperclip, WifiOff } from "lucide-react";
import { useDrop } from "../dropStore";
import type { DropMessage, Transfer } from "../../domain/dropClient";
import type { MapAnchor } from "../DeviceTopology";
import { TransferCard, type TransferHandlers } from "./TransferCard";
import { DeviceConnection } from "./DeviceConnection";
import { Callout, DeviceLine, DeviceTile, EmptyState, deviceHue } from "./DevicesParts";
import { formatClock, relationshipLabel, statusLabel, type DropDevice } from "./dropModel";
import { useDropDevices } from "./useDropDevices";

const URL_PATTERN = /(https?:\/\/[^\s<>"'，。）)]+)/g;

/** Plain text with http(s) links; never interprets HTML. */
export function LinkifiedText({ text }: { text: string }) {
  const parts = text.split(URL_PATTERN);
  return <>{parts.map((part, index) => index % 2 === 1
    ? <a key={index} href={part} target="_blank" rel="noopener noreferrer">{part}</a>
    : <Fragment key={index}>{part}</Fragment>)}</>;
}

/** Enter sends unless Shift is held or an IME composition is in progress. */
export function isSendKey(event: Pick<KeyboardEvent, "key" | "shiftKey" | "nativeEvent"> & { keyCode?: number }, composing = false) {
  const native = event.nativeEvent as globalThis.KeyboardEvent | undefined;
  if (event.key !== "Enter" || event.shiftKey) return false;
  if (composing || native?.isComposing || event.keyCode === 229 || native?.keyCode === 229) return false;
  return true;
}

type TimelineEntry = { kind: "message"; at: string; message: DropMessage } | { kind: "transfer"; at: string; transfer: Transfer };

export function DeviceConversation({ activeId, onActiveChange, onMenu, onSendFiles, onDropFiles, dropTargetId, onDetails, handlers, mobileChat, onMobileBack }: {
  activeId: string;
  onActiveChange: (peerId: string) => void;
  onMenu: (device: DropDevice, anchor: MapAnchor) => void;
  onSendFiles: (device: DropDevice) => void;
  onDropFiles?: (device: DropDevice, event: DragEvent<HTMLElement>) => void;
  dropTargetId?: string;
  onDetails: (peerId: string) => void;
  handlers: TransferHandlers;
  mobileChat: boolean;
  onMobileBack: () => void;
}) {
  const { t, i18n } = useTranslation();
  const drop = useDrop();
  const { list, byId, self } = useDropDevices();
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [loadError, setLoadError] = useState("");
  const [dragId, setDragId] = useState("");
  const composing = useRef(false);
  const body = useRef<HTMLDivElement>(null);
  const { data } = drop;

  const threads = useMemo(() => {
    const ids = new Set<string>();
    for (const peer of Object.values(data.peers)) if (peer.relationship === "own" || peer.relationship === "friend" || peer.relationship === "external" || peer.last || peer.unread > 0) ids.add(peer.peer_id);
    for (const transfer of drop.transfers) ids.add(transfer.peer_id);
    if (activeId) ids.add(activeId);
    return Array.from(ids).map(id => byId.get(id)).filter((d): d is DropDevice => Boolean(d) && !d!.self)
      .sort((a, b) => Number(b.online === true) - Number(a.online === true) || (b.meta?.last?.at || "").localeCompare(a.meta?.last?.at || ""));
  }, [data.peers, drop.transfers, activeId, byId]);
  const active = activeId ? byId.get(activeId) : undefined;
  const conv = activeId ? data.convs[activeId] : undefined;

  useEffect(() => {
    if (!activeId || drop.status !== "ready") return;
    setLoadError("");
    drop.loadConversation(activeId).catch(() => setLoadError(t("drop.loadFailed")));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, drop.status]);
  const unread = active?.meta?.unread ?? 0;
  useEffect(() => {
    if (activeId && unread > 0 && conv?.loaded) void drop.markRead(activeId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, unread, conv?.loaded]);

  const timeline = useMemo<TimelineEntry[]>(() => (conv?.refs || []).flatMap((ref): TimelineEntry[] => {
    if (ref.startsWith("m:")) {
      const message = data.messages[ref.slice(2)];
      return message ? [{ kind: "message", at: message.created_at, message }] : [];
    }
    const transfer = data.transfers[ref.slice(2)];
    return transfer ? [{ kind: "transfer", at: transfer.created_at, transfer }] : [];
  }).sort((a, b) => a.at.localeCompare(b.at)), [conv?.refs, data.messages, data.transfers]);

  useEffect(() => { body.current?.scrollTo?.({ top: body.current.scrollHeight }); }, [timeline.length, activeId]);

  const canMessage = Boolean(active?.meta) && active!.relationship !== "other" && active!.meta!.send_mode !== "unavailable";
  const canFiles = canMessage && active?.relationship !== "external" && active?.meta?.file_sendable !== false;
  const needsVerify = active?.meta?.send_mode === "verify" && !active.meta.verification.verified;
  const draft = drafts[activeId] || "";
  const submit = async () => {
    const text = draft.trim();
    if (!text || !active || !canMessage || needsVerify) return;
    setDrafts(current => ({ ...current, [active.id]: "" }));
    try { await drop.sendMessage(active.id, text); } catch { /* The failed bubble offers retry. */ }
  };

  const contextMenu = (device: DropDevice) => (event: MouseEvent<HTMLElement>) => {
    event.preventDefault();
    // Only the menu target changes; the open conversation and its draft stay put.
    onMenu(device, event.clientX || event.clientY ? { x: event.clientX, y: event.clientY } : event.currentTarget);
  };
  const menuKey = (device: DropDevice) => (event: KeyboardEvent<HTMLElement>) => {
    if ((event.shiftKey && event.key === "F10") || event.key === "ContextMenu") { event.preventDefault(); onMenu(device, event.currentTarget); }
  };

  const statusText = (message: DropMessage) => message.status === "waiting_online" ? t("drop.msgWaiting")
    : message.status === "sending" ? t("drop.msgSending") : message.status === "delivered" ? t("drop.msgDelivered") : t("drop.msgFailed");
  const selfName = self?.name || t("drop.thisDevice");
  // The side a message sits on already says who sent it; the text is for screen readers.
  const senderLabel = (mine: boolean) => <span className="rd-sr">{mine ? selfName : active?.name} · {t(mine ? "drop.fromLocalDevice" : "drop.fromRemoteDevice")}</span>;
  let lastDay = "";
  const dayLabel = (iso: string) => {
    const date = new Date(iso);
    return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat(i18n.language, { month: "long", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date) : "";
  };
  const entry = (item: TimelineEntry): ReactNode => {
    const day = item.at.slice(0, 10);
    const separator = day !== lastDay ? <div className="rd-date" key={`d-${day}`}>{dayLabel(item.at)}</div> : null;
    lastDay = day;
    if (item.kind === "transfer") {
      const mine = item.transfer.direction === "out";
      return <Fragment key={`t-${item.transfer.transfer_id}`}>{separator}
        <div className={`rd-msg wide${mine ? " mine" : ""}`}>
          {senderLabel(mine)}
          <TransferCard transfer={item.transfer} handlers={handlers} />
          <div className="rd-msgmeta">{mine ? selfName : active?.name} · {formatClock(item.transfer.created_at, i18n.language)}</div>
        </div>
      </Fragment>;
    }
    const message = item.message;
    if (message.kind === "system") return <Fragment key={`m-${message.message_id}`}>{separator}
      <p className="rd-system"><LinkifiedText text={message.text} /> · {formatClock(message.created_at, i18n.language)}</p></Fragment>;
    const mine = message.direction === "out";
    return <Fragment key={`m-${message.message_id}`}>{separator}
      <div className={`rd-msg${mine ? " mine" : ""}`}>
        {senderLabel(mine)}
        <div className="rd-bubble"><LinkifiedText text={message.text} /></div>
        <div className={`rd-msgmeta${message.status === "failed" || message.status === "waiting_online" ? " warn" : ""}`}>
          {mine ? `${formatClock(message.created_at, i18n.language)} · ${statusText(message)}`
            : formatClock(message.created_at, i18n.language)}
          {mine && message.status === "failed" && <button type="button" className="rd-link sm" disabled={drop.isPending(`retry:${message.message_id}`)}
            onClick={() => void drop.run(`retry:${message.message_id}`, () => drop.retryMessage(message.message_id)).catch(() => undefined)}>{t("drop.retry")}</button>}
        </div>
      </div>
    </Fragment>;
  };

  return <div className={`rd-conv${mobileChat ? " show-chat" : ""}`}>
    <aside className="rd-rail" aria-label={t("drop.conversations")}>
      <div className="rd-self rd-h-indigo"><span className="rd-dg lg"><DeviceLine kind={self?.kind || "desktop"} /></span><div><small>{t("drop.thisDevice")}</small><b>{selfName}</b></div></div>
      <div className="rd-rail-head"><span>{t("drop.messageDevices")}</span><span>{threads.length}</span></div>
      {threads.map(device => {
        const last = device.meta?.last;
        const unreadCount = device.meta?.unread ?? 0;
        return <div key={device.id} className={`rd-thread rd-h-${deviceHue(device)}${device.id === activeId ? " active" : ""}`}>
          <button type="button" className="rd-thread-select" aria-current={device.id === activeId ? "true" : undefined}
            onClick={() => onActiveChange(device.id)} onContextMenu={contextMenu(device)} onKeyDown={menuKey(device)}>
            <span className="rd-thread-device"><DeviceTile device={device} size="sm" /><i className={`rd-sdot ${device.online === true ? "online" : device.online === false ? "offline" : "unknown"}`} /></span>
            <span className="rd-thread-text">
              <span className="rd-thread-top"><b>{device.name}</b>{last && <small>{formatClock(last.at, i18n.language)}</small>}</span>
              <small className="rd-thread-state">{statusLabel(t, device.online)} · {relationshipLabel(t, device)}</small>
              <span className="rd-thread-last">
                <small>{last?.text || t("drop.startConversation")}</small>
                {unreadCount > 0 && <span className="rd-badge" aria-label={String(unreadCount)}>{unreadCount}</span>}
              </span>
            </span>
          </button>
          <button type="button" className="rd-ib xs rd-thread-more" aria-haspopup="menu" aria-label={t("drop.moreActions", { name: device.name })}
            onClick={event => onMenu(device, event.currentTarget)}><Ellipsis size={15} /></button>
        </div>;
      })}
      {!threads.length && <EmptyState icon={<MessageCircle size={24} strokeWidth={1.7} />} title={t("drop.noConversations")}>{t("drop.noConversationsHint")}</EmptyState>}
      <p className="rd-rail-note"><Lock size={12} strokeWidth={1.9} aria-hidden="true" />{t("drop.e2eNote")}</p>
    </aside>
    <section className={`rd-chat${active ? ` rd-h-${deviceHue(active)}` : ""}`} aria-label={active?.name}>
      {active ? <>
        <header className="rd-chathead">
          <button type="button" className="rd-ib rd-back" aria-label={t("drop.back")} onClick={onMobileBack}><ArrowLeft size={18} /></button>
          <DeviceTile device={active} size="sm" />
          <div className="rd-chathead-id">
            <h3>{active.name}</h3>
            <DeviceConnection self={self} peer={active} onDetails={() => onDetails(active.id)} />
          </div>
          <span className="rd-sendwrap">
            <button type="button" className="rd-btn rd-header-send" disabled={!canFiles} onClick={() => onSendFiles(active)}><Paperclip size={15} strokeWidth={1.7} />{t("drop.sendFile")}</button>
            {!canFiles && <small>{t("drop.sendUnavailable")}</small>}
          </span>
          <button type="button" className="rd-ib" aria-haspopup="menu" aria-label={t("drop.moreActions", { name: active.name })}
            onClick={event => onMenu(active, event.currentTarget)}><Ellipsis size={18} /></button>
        </header>
        <div className="rd-chatbody" ref={body}>
          {active.relationship === "external" && <Callout role="status" icon={<Info size={15} strokeWidth={1.7} />}>{t("drop.externalMessageTarget", { defaultValue: "Text to this device only. Delivery requires a reachable device and an active connection." })} · {relationshipLabel(t, active)}</Callout>}
          {active.online === false && canMessage && active.relationship !== "external" && <Callout role="status" icon={<WifiOff size={15} strokeWidth={1.7} />}>{t("drop.offlineNotice")}</Callout>}
          {loadError && <Callout tone="err" role="alert" icon={<Info size={15} strokeWidth={1.7} />}>{loadError}</Callout>}
          {timeline.map(entry)}
          {conv?.loaded && !timeline.length && <EmptyState icon={<MessageCircle size={24} strokeWidth={1.7} />} title={t("drop.startConversation")}>{t("drop.startConversationHint")}</EmptyState>}
        </div>
        {canMessage ? <form data-drop-peer={active.id}
          className={`rd-form${dragId === active.id || dropTargetId === active.id ? " drop-target" : ""}`}
          onDragOver={event => {
            if (!event.dataTransfer.types.includes("Files")) return;
            event.preventDefault();
            if (!canFiles) { event.dataTransfer.dropEffect = "none"; return; }
            event.dataTransfer.dropEffect = "copy";
            setDragId(active.id);
          }}
          onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragId(""); }}
          onDrop={event => {
            if (!event.dataTransfer.types.includes("Files")) return;
            event.preventDefault();
            setDragId("");
            if (!canFiles) return;
            onDropFiles?.(active, event);
          }}
          onSubmit={event => { event.preventDefault(); void submit(); }}>
          {(dragId === active.id || dropTargetId === active.id) && <p className="rd-hint" role="status"><Paperclip size={14} aria-hidden="true" />{t("drop.orDropHere")}</p>}
          {needsVerify && <p className="rd-hint"><Info size={14} aria-hidden="true" />{t("drop.verifyRequired")} <button type="button" className="rd-link sm" onClick={() => onDetails(active.id)}>{t("drop.verifyNearby")}</button></p>}
          <div className="rd-composer">
            <textarea rows={2} value={draft} aria-label={t("drop.composerLabel", { name: active.name })}
              placeholder={t("drop.composerDevicePlaceholder", { name: active.name })}
              onChange={event => { const value = event.target.value; setDrafts(current => ({ ...current, [active.id]: value })); }}
              onCompositionStart={() => { composing.current = true; }}
              onCompositionEnd={() => { composing.current = false; }}
              onKeyDown={event => { if (isSendKey(event, composing.current)) { event.preventDefault(); void submit(); } }} />
            <div className="rd-composer-bottom">
              <div className="rd-composer-tools">
                <button type="button" className="rd-tb" disabled={!canFiles} onClick={() => onSendFiles(active)}><Paperclip size={15} strokeWidth={1.7} aria-hidden="true" />{t("drop.files")}</button>
                <small className="rd-composer-tip">{t("drop.composerTip")}</small>
              </div>
              <button type="submit" className="rd-btn primary sm" disabled={!draft.trim() || needsVerify}><ArrowUp size={15} strokeWidth={1.9} aria-hidden="true" />{t("drop.send")}</button>
            </div>
          </div>
        </form> : <p className="rd-readonly"><MessageCircle size={15} strokeWidth={1.7} aria-hidden="true" />{t("drop.readOnly")}</p>}
      </> : threads.length > 0 && <EmptyState icon={<MessageCircle size={24} strokeWidth={1.7} />} title={t("drop.conversations")}>{t("drop.noConversationsHint")}</EmptyState>}
    </section>
  </div>;
}

export const firstThreadId = (list: DropDevice[]) => list.find(device => !device.self && (device.meta?.last || (device.meta?.unread ?? 0) > 0))?.id
  || list.find(device => !device.self && device.online === true && (device.relationship === "own" || device.relationship === "friend"))?.id
  || list.find(device => !device.self && device.relationship === "external")?.id || "";
