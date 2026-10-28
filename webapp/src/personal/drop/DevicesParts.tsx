import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { ChevronRight, Globe, House, MessageCircle, ArrowRightLeft, Waypoints, Wifi, WifiOff } from "lucide-react";
import type { Service } from "../model";
import { personalHref, usePersonal } from "../model";
import { personalLabelKeys } from "../labels";
import type { Transfer } from "../../domain/dropClient";
import { formatRelativeTime, routeLabel, statusLabel, type DropDevice } from "./dropModel";
import "./devices-refresh.css";

// Presentational pieces of the Devices page. Colour is identity and status only: one indigo brand colour,
// jewel hues for a device or a file kind, and ok / warn / off for state.

export type Hue = "indigo" | "violet" | "teal" | "sky" | "rose" | "amber" | "slate";
export type RouteKind = "self" | "lan" | "public" | "relay" | "unknown" | "off";

const PALETTE: Hue[] = ["teal", "violet", "sky", "rose", "amber"];
function hash(value: string) {
  let h = 0;
  for (const ch of value) h = (h * 43 + ch.charCodeAt(0)) | 0;
  return Math.abs(h);
}

/** This device is indigo, offline ones are slate; every other device keeps one hue picked from its id. */
export function deviceHue(device: Pick<DropDevice, "id" | "self" | "online">): Hue {
  if (device.self) return "indigo";
  if (device.online === false) return "slate";
  return PALETTE[hash(device.id) % PALETTE.length];
}

export function routeKind(device: Pick<DropDevice, "self" | "online" | "route">): RouteKind {
  if (device.self) return "self";
  if (device.online === false) return "off";
  return device.route === "lan" ? "lan" : device.route === "public" ? "public" : device.route === "relay" ? "relay" : "unknown";
}

const initials = (name: string) => {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  const text = parts.length > 1 ? parts[0][0] + parts[1][0] : Array.from(parts[0] || "?").slice(0, 2).join("");
  return text.toUpperCase();
};

const translateLabel = (t: TFunction, value: string) => (personalLabelKeys[value] ? t(`personal.${personalLabelKeys[value]}`) : value);

/** What the Connection column says: route and latency, the current network, or when it was last seen. */
export function connectionOf(t: TFunction, language: string, device: DropDevice): { kind: RouteKind; label: string; sub: string } {
  const kind = routeKind(device);
  if (kind === "self") {
    const network = device.device?.network?.label;
    const named = network && network !== "Current network";
    return { kind, label: named ? translateLabel(t, network) : t("personal.currentNetwork"), sub: named ? t("personal.currentNetwork") : "" };
  }
  if (kind === "off") {
    const seen = formatRelativeTime(device.meta?.last_seen, language);
    return { kind, label: t("drop.notConnected"), sub: seen ? t("drop.lastSeen", { time: seen }) : "" };
  }
  if (device.online === null && !device.route) return { kind, label: t("drop.statusUnknown"), sub: "" };
  const latency = device.latency !== null && device.latency !== undefined ? t("drop.latency", { ms: Math.round(device.latency) }) : "";
  return { kind, label: routeLabel(t, device.route), sub: latency };
}

export function sendLabel(t: TFunction, device: DropDevice) {
  const mode = device.meta?.send_mode;
  return mode === "queue" ? t("drop.queueButton") : mode === "verify" ? t("drop.verifyButton") : t("drop.sendFile");
}

/* ---- drawn glyphs: a white body with details in the hue's deep shade ---- */

export function DeviceGlyph({ kind, size }: { kind: "desktop" | "laptop"; size: number }) {
  return (
    <svg className="rd-glyph" width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      {kind === "laptop" ? (
        <>
          <rect x="4" y="4.5" width="16" height="11" rx="1.8" fill="#fff" />
          <rect className="rd-deep" x="5.9" y="6.4" width="12.2" height="7.2" rx="1" fillOpacity=".3" />
          <path d="M1.8 17.2h20.4a.6.6 0 0 1 .6.6 1.8 1.8 0 0 1-1.8 1.8H3a1.8 1.8 0 0 1-1.8-1.8.6.6 0 0 1 .6-.6z" fill="#fff" />
        </>
      ) : (
        <>
          <rect x="2.5" y="3.5" width="19" height="13" rx="2.4" fill="#fff" />
          <rect className="rd-deep" x="4.6" y="5.6" width="14.8" height="8.8" rx="1.2" fillOpacity=".3" />
          <rect x="10.8" y="16.4" width="2.4" height="3" fill="#fff" fillOpacity=".85" />
          <rect x="7" y="19.2" width="10" height="2" rx="1" fill="#fff" />
        </>
      )}
    </svg>
  );
}

/** Outline version used on small chips (connection strip, this-device box). */
export function DeviceLine({ kind }: { kind: "desktop" | "laptop" }) {
  return (
    <svg className="rd-ico" width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">
      {kind === "laptop" ? (
        <>
          <path d="M6 5h12a1.5 1.5 0 0 1 1.5 1.5V15h-15V6.5A1.5 1.5 0 0 1 6 5z" fill="currentColor" fillOpacity=".16" stroke="currentColor" strokeWidth="1.6" />
          <path d="M2.8 18.5h18.4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        </>
      ) : (
        <>
          <path d="M5.2 4h13.6A2.2 2.2 0 0 1 21 6.2v7.6a2.2 2.2 0 0 1-2.2 2.2H5.2A2.2 2.2 0 0 1 3 13.8V6.2A2.2 2.2 0 0 1 5.2 4z" fill="currentColor" fillOpacity=".16" stroke="currentColor" strokeWidth="1.6" />
          <path d="M8.5 20h7M12 16v4" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
        </>
      )}
    </svg>
  );
}

export function DeviceTile({ device, size = "md" }: { device: Pick<DropDevice, "id" | "kind" | "self" | "online">; size?: "sm" | "md" | "xl" }) {
  const px = { sm: 23, md: 26, xl: 32 }[size];
  return (
    <span className={`rd-tile ${size} rd-h-${deviceHue(device)}${device.online === false && !device.self ? " off" : ""}`} aria-hidden="true">
      <DeviceGlyph kind={device.kind} size={px} />
    </span>
  );
}

export function RouteIcon({ kind, size = 16 }: { kind: RouteKind; size?: number }) {
  const props = { size, strokeWidth: 1.8, "aria-hidden": true as const };
  return kind === "lan" ? <Wifi {...props} /> : kind === "public" ? <Globe {...props} /> : kind === "relay" ? <Waypoints {...props} />
    : kind === "off" ? <WifiOff {...props} /> : kind === "self" ? <House {...props} /> : <Wifi {...props} />;
}

type FileCategory = "folder" | "files" | "audio" | "archive" | "sheet" | "doc";
function fileCategory(transfer: Pick<Transfer, "kind" | "name">): FileCategory {
  if (transfer.kind === "folder") return "folder";
  if (transfer.kind === "files") return "files";
  const ext = /\.([a-z0-9]+)$/i.exec(transfer.name)?.[1]?.toLowerCase() ?? "";
  if (["mp3", "m4a", "wav", "flac", "ogg", "aac", "opus"].includes(ext)) return "audio";
  if (["zip", "7z", "rar", "tar", "gz", "tgz", "bz2", "xz"].includes(ext)) return "archive";
  if (["xlsx", "xls", "csv", "ods", "tsv"].includes(ext)) return "sheet";
  return "doc";
}
export function fileHue(transfer: Pick<Transfer, "kind" | "name">): Hue {
  const category = fileCategory(transfer);
  if (category === "doc") return /\.pdf$/i.test(transfer.name) ? "rose" : "indigo";
  return { folder: "amber", files: "sky", audio: "sky", archive: "violet", sheet: "teal" }[category] as Hue;
}

function FileGlyph({ category, size }: { category: FileCategory; size: number }) {
  const body = {
    folder: (
      <>
        <path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2z" fill="#fff" />
        <rect className="rd-deep" x="2" y="9" width="20" height="1.6" fillOpacity=".3" />
      </>
    ),
    doc: (
      <>
        <path d="M7 3h6.6a2 2 0 0 1 1.4.6l3.4 3.4a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z" fill="#fff" />
        <path className="rd-deep" d="M14 3.3V7a1 1 0 0 0 1 1h3.7z" fillOpacity=".28" />
        <rect className="rd-deep" x="8" y="12" width="8" height="1.7" rx=".85" fillOpacity=".6" />
        <rect className="rd-deep" x="8" y="15.5" width="5.4" height="1.7" rx=".85" fillOpacity=".36" />
      </>
    ),
    audio: (
      <>
        <rect x="3.2" y="9" width="2.6" height="6" rx="1.3" fill="#fff" fillOpacity=".7" />
        <rect x="7.2" y="6" width="2.6" height="12" rx="1.3" fill="#fff" fillOpacity=".9" />
        <rect x="11.2" y="3.5" width="2.6" height="17" rx="1.3" fill="#fff" />
        <rect x="15.2" y="7" width="2.6" height="10" rx="1.3" fill="#fff" fillOpacity=".9" />
        <rect x="19.2" y="9.5" width="2.6" height="5" rx="1.3" fill="#fff" fillOpacity=".7" />
      </>
    ),
    archive: (
      <>
        <rect x="3.5" y="3.5" width="17" height="17" rx="3" fill="#fff" />
        <rect className="rd-deep" x="11" y="3.5" width="2" height="2.2" fillOpacity=".5" />
        <rect className="rd-deep" x="11" y="7" width="2" height="2.2" fillOpacity=".5" />
        <rect className="rd-deep" x="10" y="10.5" width="4" height="4.5" rx="1.2" fillOpacity=".7" />
      </>
    ),
    sheet: (
      <>
        <rect x="4" y="3" width="16" height="18" rx="2.5" fill="#fff" />
        <path className="rd-deep-line" d="M4 9h16M4 15h16M10 9v12" fill="none" strokeWidth="1.5" strokeOpacity=".45" />
      </>
    ),
    files: (
      <>
        <rect x="7" y="2.5" width="13" height="16" rx="2.2" fill="#fff" fillOpacity=".6" />
        <rect x="4" y="5.5" width="13" height="16" rx="2.2" fill="#fff" />
        <rect className="rd-deep" x="6.5" y="10" width="8" height="1.6" rx=".8" fillOpacity=".55" />
        <rect className="rd-deep" x="6.5" y="13.4" width="5.5" height="1.6" rx=".8" fillOpacity=".35" />
      </>
    ),
  }[category];
  return <svg className="rd-glyph" width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">{body}</svg>;
}

/** A file or folder tile. `badge` draws the direction mark (sent or received) on its corner. */
export function FileTile({ transfer, size = 42, badge }: { transfer: Pick<Transfer, "kind" | "name" | "direction">; size?: number; badge?: ReactNode }) {
  return (
    <span className={`rd-ftile rd-h-${fileHue(transfer)}`} style={{ width: size, height: size }} aria-hidden="true">
      <FileGlyph category={fileCategory(transfer)} size={Math.round(size * 0.52)} />
      {badge}
    </span>
  );
}

const serviceHue: Record<Service["kind"], Hue> = { ai: "violet", document: "teal", audio: "sky", video: "rose", network: "indigo" };
export const hueOfService = (kind: Service["kind"]): Hue => serviceHue[kind];

export function ServiceTile({ kind }: { kind: Service["kind"] }) {
  const body = {
    ai: (
      <>
        <path d="M10 4.5c.7 4.6 2.9 6.8 7.5 7.5-4.6.7-6.8 2.9-7.5 7.5-.7-4.6-2.9-6.8-7.5-7.5 4.6-.7 6.8-2.9 7.5-7.5z" fill="#fff" />
        <path d="M18.5 2.8c.3 2 1 2.7 3 3-2 .3-2.7 1-3 3-.3-2-1-2.7-3-3 2-.3 2.7-1 3-3z" fill="#fff" fillOpacity=".72" />
        <circle cx="19" cy="18.5" r="1.5" fill="#fff" fillOpacity=".55" />
      </>
    ),
    document: (
      <>
        <path d="M7 3h6.6a2 2 0 0 1 1.4.6l3.4 3.4a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z" fill="#fff" />
        <path className="rd-deep" d="M14 3.3V7a1 1 0 0 0 1 1h3.7z" fillOpacity=".28" />
        <rect className="rd-deep" x="8" y="12" width="8" height="1.7" rx=".85" fillOpacity=".6" />
        <rect className="rd-deep" x="8" y="15.5" width="5.4" height="1.7" rx=".85" fillOpacity=".36" />
      </>
    ),
    audio: (
      <>
        <rect x="3.2" y="9" width="2.6" height="6" rx="1.3" fill="#fff" fillOpacity=".7" />
        <rect x="7.2" y="6" width="2.6" height="12" rx="1.3" fill="#fff" fillOpacity=".9" />
        <rect x="11.2" y="3.5" width="2.6" height="17" rx="1.3" fill="#fff" />
        <rect x="15.2" y="7" width="2.6" height="10" rx="1.3" fill="#fff" fillOpacity=".9" />
        <rect x="19.2" y="9.5" width="2.6" height="5" rx="1.3" fill="#fff" fillOpacity=".7" />
      </>
    ),
    video: (
      <>
        <rect x="3" y="5" width="18" height="14" rx="3.2" fill="#fff" />
        <path className="rd-deep" d="M10 9.2v5.6a.6.6 0 0 0 .9.5l4.6-2.8a.6.6 0 0 0 0-1l-4.6-2.8a.6.6 0 0 0-.9.5z" fillOpacity=".7" />
      </>
    ),
    network: (
      <>
        <circle cx="12" cy="12" r="9" fill="#fff" />
        <path className="rd-deep-line" d="M3.2 12h17.6M12 3c2.4 2.5 3.6 5.5 3.6 9S14.4 18.5 12 21M12 3C9.6 5.5 8.4 8.5 8.4 12S9.6 18.5 12 21" fill="none" strokeWidth="1.4" strokeLinecap="round" strokeOpacity=".55" />
      </>
    ),
  }[kind];
  return (
    <span className={`rd-tile xs rd-h-${hueOfService(kind)}`} aria-hidden="true">
      <svg className="rd-glyph" width="21" height="21" viewBox="0 0 24 24">{body}</svg>
    </span>
  );
}

/* ---- status ---- */

export function StatusPill({ device }: { device: Pick<DropDevice, "self" | "online"> }) {
  const { t } = useTranslation();
  const tone = device.self ? "self" : device.online === true ? "ok" : "off";
  return (
    <span className={`rd-pill ${tone}`}>
      <i />
      {device.self ? t("drop.thisDevice") : statusLabel(t, device.online)}
    </span>
  );
}

export function Pill({ tone, children }: { tone: "ok" | "off" | "warn" | "self"; children: ReactNode }) {
  return (
    <span className={`rd-pill ${tone}`}>
      <i />
      {children}
    </span>
  );
}

/** Segmented filter. The counts are decoration, so button names stay the plain filter names. */
export function Seg<T extends string>({ options, value, onChange, label }: {
  options: { value: T; label: string; count?: number }[];
  value: T;
  onChange: (value: T) => void;
  label: string;
}) {
  return (
    <div className="rd-seg" role="group" aria-label={label}>
      {options.map(option => (
        <button key={option.value} type="button" aria-pressed={value === option.value} className={value === option.value ? "on" : ""} onClick={() => onChange(option.value)}>
          {option.label}
          {option.count !== undefined && <em aria-hidden="true">{option.count}</em>}
        </button>
      ))}
    </div>
  );
}

export function EmptyState({ icon, title, children, action }: { icon: ReactNode; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="rd-empty">
      <span className="rd-empty-ico" aria-hidden="true">{icon}</span>
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

export function Callout({ tone = "plain", icon, children, role, action }: { tone?: "plain" | "warn" | "err"; icon: ReactNode; children: ReactNode; role?: "status" | "alert"; action?: ReactNode }) {
  return (
    <div className={`rd-callout ${tone}`} role={role}>
      <span className="rd-callout-ico" aria-hidden="true">{icon}</span>
      <span className="rd-callout-text">{children}</span>
      {action && <span className="rd-callout-action">{action}</span>}
    </div>
  );
}

/* ---- summary band ---- */

const ROUTE_ORDER: RouteKind[] = ["lan", "public", "relay", "unknown", "off"];

/** How many devices there are, which way they are reached from this one, and what is waiting for the user. */
export function DevicesBand({ devices, activeCount, unread }: { devices: DropDevice[]; activeCount: number; unread: number }) {
  const { t } = useTranslation();
  const { demo } = usePersonal();
  const online = devices.filter(device => device.online === true).length;
  const counts: Record<RouteKind, number> = { self: 0, lan: 0, public: 0, relay: 0, unknown: 0, off: 0 };
  for (const device of devices) counts[routeKind(device)] += 1;
  const names: Record<RouteKind, string> = {
    self: t("drop.thisDevice"), lan: t("drop.routeLan"), public: t("drop.routePublic"), relay: t("drop.routeRelay"),
    unknown: t("drop.legendUnknown"), off: t("drop.offline"),
  };
  const shown = ROUTE_ORDER.filter(kind => counts[kind] > 0);
  const summary = shown.map(kind => t("drop.bandRouteCount", { count: counts[kind], label: names[kind] })).join(t("drop.listSeparator"));
  const stats: { to: string; icon: ReactNode; count: number; text: string }[] = [
    { to: "/devices?tab=transfers", icon: <ArrowRightLeft size={15} strokeWidth={1.7} />, count: activeCount, text: t("drop.statTransfers", { count: activeCount }) },
    { to: "/devices?tab=conversations", icon: <MessageCircle size={15} strokeWidth={1.7} />, count: unread, text: t("drop.statUnread", { count: unread }) },
  ];
  return (
    <section className="rd-band" aria-label={t("drop.bandLabel")}>
      <div className="rd-band-left">
        <div>
          <div className="rd-band-num">{devices.length}</div>
          <div className="rd-band-cap">{t("drop.bandCaption", { count: devices.length, online })}</div>
        </div>
        <div className="rd-avs" aria-hidden="true">
          {devices.slice(0, 6).map(device => (
            <span key={device.id} className={`rd-h-${deviceHue(device)}`}>{initials(device.name)}</span>
          ))}
        </div>
      </div>
      {shown.length > 0 && (
        <div className="rd-band-mid">
          <div className="rd-band-title">{t("drop.bandRoutes")}</div>
          <div className="rd-meter" role="img" aria-label={summary}>
            {shown.map(kind => <i key={kind} className={`rd-k-${kind}`} style={{ flex: counts[kind] }} />)}
          </div>
          <div className="rd-legend" aria-hidden="true">
            {shown.map(kind => <span key={kind}><i className={`rd-dot rd-k-${kind}`} /><b>{counts[kind]}</b>{names[kind]}</span>)}
          </div>
        </div>
      )}
      <div className="rd-band-stats">
        {stats.map(stat => (
          <Link key={stat.to} className="rd-stat" to={personalHref(stat.to, demo)}>
            <span className="rd-stat-ico" aria-hidden="true">{stat.icon}</span>
            <span className="rd-stat-text"><b>{stat.count}</b> {stat.text}</span>
            <ChevronRight size={15} strokeWidth={1.6} aria-hidden="true" />
          </Link>
        ))}
      </div>
    </section>
  );
}
