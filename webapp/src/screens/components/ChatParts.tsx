import { Lock, MessageSquare, Paperclip } from "lucide-react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { uiLocale } from "../../uiI18n";
import { DeviceTile, deviceHue, type Hue } from "../../personal/drop/FamilyParts";
import type { Device } from "../../personal/model";
import type { Peer } from "../../domain/types";
import { CopyButton, SearchField, formatClock, shortPeerId } from "./FriendsParts";
import "../friends-refresh.css";
import "../chat-refresh.css";

// Presentational pieces of the Messages page (/chat). Chat.tsx keeps the node calls, the message stream and the send logic.

export interface ChatRecord {
  msg_id: string;
  dir: "in" | "out";
  from: string;
  to: string;
  text?: string;
  ts?: string;
  kind?: string;
  delivered?: boolean;
  // "direct" when the peer took it on the wire, "mailbox" when the node handed
  // it to the network mailbox instead, absent when neither route accepted it.
  via?: "direct" | "mailbox";
  attachment?: { filename: string; mime: string; size?: number };
}

/** "Today", "Yesterday" or a short date, in the interface language. */
export function dayLabel(ts: string | undefined, now = new Date()): string {
  if (!ts) return "";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return "";
  const startOf = (value: Date) => new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime();
  const days = Math.round((startOf(date) - startOf(now)) / 86400000);
  if (days === 0 || days === -1) return new Intl.RelativeTimeFormat(uiLocale(), { numeric: "auto" }).format(days, "day");
  return date.toLocaleDateString(uiLocale(), { month: "short", day: "numeric", ...(date.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }) });
}

export const dayKey = (ts?: string) => {
  const date = ts ? new Date(ts) : null;
  return date && !Number.isNaN(date.getTime()) ? `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}` : "";
};

export function peerView(peer: Peer, devices: Device[], name: string) {
  const device = devices.find((candidate) => candidate.id === peer.id);
  const online = device?.online ?? null;
  const hue: Hue = deviceHue(peer.id, { online });
  return { name, online, hue, kind: device?.kind ?? "desktop" } as const;
}

export function PeerRow({ view, selected, onSelect }: { view: ReturnType<typeof peerView>; selected: boolean; onSelect: () => void }) {
  const { t } = useTranslation();
  const tone = view.online === true ? "ok" : view.online === false ? "off" : "unknown";
  return <button type="button" className={`rq-peer rf-h-${view.hue}`} data-on={selected} data-off={view.online === false} aria-pressed={selected} onClick={onSelect}>
    <DeviceTile kind={view.kind} hue={view.hue} size="sm" off={view.online === false} />
    <span className="rq-peer-text">
      <b className="rf-ell">{view.name}</b>
      <small><i className={`rq-dot rq-dot-${tone}`} aria-hidden="true" /><span className="rf-ell">{view.online === false ? t("personal.offline") : t("personal.messagesConnectedDevice")}</span></small>
    </span>
  </button>;
}

export function PeerList({ views, selectedId, onSelect, query, onQuery }: {
  views: Array<{ id: string; view: ReturnType<typeof peerView> }>; selectedId: string | undefined; onSelect: (id: string) => void; query: string; onQuery: (value: string) => void;
}) {
  const { t } = useTranslation();
  const note = t("personal.messagesConversationsWithFriendsAreOnTheFriendsPage");
  const match = /^(.*?)\[link\](.*?)\[\/link\](.*)$/s.exec(note);
  return <div className="rq-plist">
    <div className="rq-plist-head">
      <h2>{t("personal.messagesConversations")}</h2>
      <SearchField value={query} onChange={onQuery} label={t("personal.messagesSearchDevices")} placeholder={t("personal.messagesSearchDevices")} />
    </div>
    <ul>
      {views.map(({ id, view }) => <li key={id}><PeerRow view={view} selected={id === selectedId} onSelect={() => onSelect(id)} /></li>)}
    </ul>
    <p className="rq-plist-note">{match ? <>{match[1]}<Link className="rf-lnk" to="/friends">{match[2]}</Link>{match[3]}</> : note}</p>
  </div>;
}

export function ConversationHead({ peerId, view }: { peerId: string; view: ReturnType<typeof peerView> }) {
  const { t } = useTranslation();
  const status = view.online === true ? t("personal.online") : view.online === false ? t("personal.offline") : "";
  return <div className="rq-conv-head">
    <DeviceTile kind={view.kind} hue={view.hue} size="md" off={view.online === false} />
    <div className="rq-conv-id">
      <h2>{view.name}</h2>
      <small><Lock size={12} strokeWidth={2} aria-hidden="true" />{t("personal.messagesEncryptedConversation")}{status ? ` · ${status}` : ""}</small>
    </div>
    <code className="rf-code-chip" title={peerId}>{shortPeerId(peerId)}</code>
    <CopyButton value={peerId} label={t("personal.messagesCopyDeviceId")} copied={false} onCopied={() => undefined} />
  </div>;
}

export function EmptyDevices() {
  const { t } = useTranslation();
  return <div className="rq-nodevices rf-card">
    <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M3 16h4a1 1 0 0 1 1 1v4a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1v-4a1 1 0 0 1 1-1zM17 16h4a1 1 0 0 1 1 1v4a1 1 0 0 1-1 1h-4a1 1 0 0 1-1-1v-4a1 1 0 0 1 1-1zM10 2h4a1 1 0 0 1 1 1v4a1 1 0 0 1-1 1h-4a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1zM5 16v-3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v3M12 12V8" /></svg>
    <h3>{t("personal.messagesNoDevicesDiscoveredYet")}</h3>
    <p>{t("personal.messagesAddAnotherComputerFromDevicesToStartAConversation")}</p>
    <Link className="rf-btn" to="/devices">{t("personal.messagesOpenDevices")}</Link>
  </div>;
}

export function NoMessages({ title, hint }: { title: string; hint: string }) {
  return <div className="rq-cempty"><MessageSquare size={32} strokeWidth={1.4} aria-hidden="true" /><b>{title}</b><span>{hint}</span></div>;
}

export function LoadingConversation({ label }: { label: string }) {
  return <div className="rq-cloading"><div className="rq-skel" aria-hidden="true"><i /><i /><i /></div><p role="status">{label}</p></div>;
}

export function MessageBubble({ record, url, peerName }: { record: ChatRecord; url: string; peerName: string }) {
  const { t } = useTranslation();
  const out = record.dir === "out";
  const att = record.attachment;
  return <div className={`rq-cmsg ${out ? "out" : "in"}`}>
    {record.text ? <div className="rq-cbubble">{record.text}</div> : null}
    {att ? (record.kind === "image"
      ? <img className="rq-cimg" src={url} alt={att.filename} />
      : <a className="rf-btn rq-cfile" href={url} download={att.filename}><Paperclip size={14} strokeWidth={1.8} aria-hidden="true" /><span className="rf-ell">{att.filename}</span></a>) : null}
    <small className="rq-cmeta">
      {formatClock(record.ts)}
      {out && record.delivered ? <span className="rq-cstate rq-cstate-ok"> · <span>✓</span> {t("personal.messagesDelivered")}</span> : null}
      {out && !record.delivered && record.via === "mailbox" ? <span className="rq-cstate rq-cstate-warn"> · <span className="rq-queued" title={t("personal.messagesQueuedViaTheNetworkMailboxDeliveredWhenThePeerComesOnline")}>{t("personal.messagesQueued")}</span> · {t("personal.messagesDeliveredWhenComesOnline", { name: peerName })}</span> : null}
    </small>
  </div>;
}
