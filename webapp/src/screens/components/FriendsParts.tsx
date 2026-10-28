import { Link } from "react-router-dom";
import { Check, Copy, Info, RefreshCw, Search, TriangleAlert, UserPlus } from "lucide-react";
import type { ReactNode } from "react";
import type { FriendRecord } from "../../domain/friendTypes";
import { deviceHue, initialsOf, type Hue, type Tone } from "../../personal/drop/FamilyParts";
import { tr, uiLocale } from "../../uiI18n";
import { linkInfo, type LinkInfo } from "./friendLinks";
import "../friends-refresh.css";

// Presentational pieces of the Friends page (summary band, friend list, detail header, connection facts). Styles live in
// friends-refresh.css under `.rd-friends`; primitives (buttons, pills, tiles) come from personal/drop/family.css via `.rf`.

export const friendHue = (friend: Pick<FriendRecord, "peer_id">): Hue => deviceHue(friend.peer_id);

export function formatBytes(bytes: number): string {
  const fmt = (value: number) => value.toLocaleString(uiLocale(), { maximumFractionDigits: value < 10 ? 1 : 0 });
  if (bytes < 1024) return `${fmt(bytes)} B`;
  if (bytes < 1024 * 1024) return `${fmt(bytes / 1024)} KB`;
  return `${fmt(bytes / (1024 * 1024))} MB`;
}

/** A moment in words a person scans quickly: just the time today, otherwise the date and time. */
export function formatWhen(value: number | string | Date): string {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  if (date.toDateString() === new Date().toDateString()) return date.toLocaleTimeString(uiLocale(), { hour: "2-digit", minute: "2-digit" });
  return date.toLocaleString(uiLocale(), { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export const shortPeerId = (id: string) => (id.length > 16 ? `${id.slice(0, 9)}…${id.slice(-5)}` : id);

export function formatClock(ts?: string): string {
  if (!ts) return "";
  const date = new Date(ts);
  return Number.isNaN(date.getTime()) ? ts : date.toLocaleTimeString(uiLocale(), { hour: "2-digit", minute: "2-digit" });
}

export function FriendAvatar({ friend, size = "md" }: { friend: Pick<FriendRecord, "peer_id" | "node_name">; size?: "md" | "lg" }) {
  return <span className={`rq-av rq-av-${size} rf-h-${friendHue(friend)}`} aria-hidden="true">{initialsOf(friend.node_name)}</span>;
}

export function LinkPill({ info, size = "md" }: { info: Pick<LinkInfo, "tone" | "label">; size?: "sm" | "md" }) {
  return <span className={`rf-pill rf-t-${info.tone}${size === "sm" ? " rf-pill-sm" : ""}`}><i aria-hidden="true" />{info.label}</span>;
}

export function Tinted({ tone, children, className = "" }: { tone: Tone; children: ReactNode; className?: string }) {
  return <span className={`rq-tint rf-t-${tone} ${className}`.trim()}>{children}</span>;
}

/** One cell per cheap, correct count. A cell whose number is not known yet shows a dash rather than a guess. */
export function FriendBand({ friends, verified, usingAi }: { friends: number; verified: number | null; usingAi: number | null }) {
  const num = (value: number | null) => (value === null ? "–" : value.toLocaleString(uiLocale()));
  return <section className="rq-band" aria-label={tr("Summary")}>
    <div><span>{tr("Friends")}</span><b>{num(friends)}</b></div>
    <div><span><i className="rf-t-ok" aria-hidden="true" />{tr("Verified direct link")}</span><b>{num(verified)}</b></div>
    <div><span><i className="rf-t-self" aria-hidden="true" />{tr("Using your AI")}</span><b>{num(usingAi)}</b></div>
  </section>;
}

export function FriendRow({ friend, info, usesAi, selected, onSelect }: { friend: FriendRecord; info: LinkInfo; usesAi: boolean; selected: boolean; onSelect: () => void }) {
  return <button type="button" className={`rq-row rf-h-${friendHue(friend)}`} data-on={selected} aria-pressed={selected} onClick={onSelect}>
    <FriendAvatar friend={friend} />
    <span className="rq-row-text">
      <b className="rf-ell">{friend.node_name}</b>
      <small className="rf-ell">{friend.endpoint}</small>
      <span className="rq-row-chips">
        <span className={`rq-chip rf-t-${info.tone}`}><i aria-hidden="true" />{info.label}</span>
        {usesAi ? <span className="rq-chip rf-t-self">{tr("Uses your AI")}</span> : null}
      </span>
    </span>
  </button>;
}

export function EmptyFriends({ onAdd }: { onAdd: () => void }) {
  return <div className="rq-empty rf-card">
    <span className="rq-empty-ico rf-h-sky" aria-hidden="true"><UserPlus size={26} strokeWidth={1.8} /></span>
    <h3>{tr("No friends yet")}</h3>
    <p>{tr("Create an invite for someone you know, or paste one they sent you.")}</p>
    <button type="button" className="rf-btn rf-btn-p" onClick={onAdd}>{tr("Add a friend")}</button>
  </div>;
}

export function PageNote({ children }: { children: ReactNode }) {
  return <p className="rq-pagenote rf-note"><Info size={13} strokeWidth={1.8} aria-hidden="true" /><span>{children}</span></p>;
}

/** Text with one `[link]…[/link]` span turned into a router link, so a translated sentence keeps its own word order. */
export function LinkedText({ text, to }: { text: string; to: string }) {
  const match = /^(.*?)\[link\](.*?)\[\/link\](.*)$/s.exec(text);
  if (!match) return <>{text}</>;
  return <>{match[1]}<Link className="rf-lnk" to={to}>{match[2]}</Link>{match[3]}</>;
}

export function Banner({ tone, role, children }: { tone: "warn" | "bad" | "ok"; role?: "alert" | "status"; children: ReactNode }) {
  return <div className={`rq-banner rq-banner-${tone}`} role={role}>
    {tone === "bad" || tone === "warn" ? <TriangleAlert size={17} strokeWidth={1.8} aria-hidden="true" /> : <Check size={17} strokeWidth={1.8} aria-hidden="true" />}
    {children}
  </div>;
}

export function SearchField({ value, onChange, label, placeholder }: { value: string; onChange: (value: string) => void; label: string; placeholder: string }) {
  return <label className="rq-search">
    <Search size={15} strokeWidth={1.6} aria-hidden="true" />
    <input value={value} aria-label={label} placeholder={placeholder} onChange={(event) => onChange(event.target.value)} />
  </label>;
}

export function CopyButton({ value, label, copied, onCopied }: { value: string; label: string; copied: boolean; onCopied: () => void }) {
  return <button type="button" className="rf-ib rf-ib-sm" aria-label={label} onClick={() => {
    if (!navigator.clipboard) return;
    void navigator.clipboard.writeText(value).then(onCopied).catch(() => undefined);
  }}>{copied ? <Check size={13} strokeWidth={2} aria-hidden="true" /> : <Copy size={13} strokeWidth={1.8} aria-hidden="true" />}</button>;
}

export function RefreshIcon({ spinning }: { spinning?: boolean }) {
  return <RefreshCw size={15} strokeWidth={1.7} aria-hidden="true" className={spinning ? "rq-spin" : undefined} />;
}

export { linkInfo };
