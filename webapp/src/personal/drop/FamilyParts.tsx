import type { ReactNode } from "react";
import "./family.css";

// Presentational pieces shared by the device-family pages and dialogs (device sync, connection details, send / incoming /
// connection / edit / share dialogs, add device). Styles live in family.css under `.rf`. Colour carries identity and state only.

export type Hue = "indigo" | "violet" | "teal" | "sky" | "rose" | "amber" | "slate";
export type Tone = "ok" | "off" | "warn" | "self" | "sky" | "bad";

const PALETTE: Hue[] = ["teal", "violet", "sky", "rose", "amber"];
function hash(value: string) {
  let h = 0;
  for (const ch of value) h = (h * 43 + ch.charCodeAt(0)) | 0;
  return Math.abs(h);
}

/** One hue per device, stable for its id. This device is indigo, offline ones are slate. */
export function deviceHue(id: string, options: { self?: boolean; online?: boolean | null } = {}): Hue {
  if (options.self) return "indigo";
  if (options.online === false) return "slate";
  return PALETTE[hash(id) % PALETTE.length];
}

export const initialsOf = (name: string) => {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  const text = parts.length > 1 ? parts[0][0] + parts[1][0] : Array.from(parts[0] || "?").slice(0, 2).join("");
  return text.toUpperCase();
};

/** `actor:5d0c…41b7`-style shortening for long identities; short values stay whole. */
export function shortIdentity(value: string, prefix = "") {
  const text = value.replace(/^actor:/, "");
  const short = text.length > 12 ? `${text.slice(0, 4)}…${text.slice(-4)}` : text;
  return `${prefix}${short}`;
}

/** "2 min ago", localised; null when the time is unknown. */
export function relativeAgo(thenMs: number | null | undefined, nowMs: number, locale: string): string | null {
  if (!thenMs) return null;
  const seconds = Math.round((thenMs - nowMs) / 1000);
  const formatter = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  const abs = Math.abs(seconds);
  if (abs < 45) return formatter.format(0, "second");
  if (abs < 3600) return formatter.format(Math.round(seconds / 60), "minute");
  if (abs < 86400) return formatter.format(Math.round(seconds / 3600), "hour");
  return formatter.format(Math.round(seconds / 86400), "day");
}

/* ---- drawn glyphs: a white body with details in the hue's deep shade ---- */

export function DeviceGlyph({ kind, size }: { kind: "desktop" | "laptop" | "mesh"; size: number }) {
  if (kind === "mesh") {
    return (
      <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="3.4" fill="#fff" />
        <circle cx="4.5" cy="6" r="2" fill="#fff" fillOpacity=".7" />
        <circle cx="19.5" cy="6" r="2" fill="#fff" fillOpacity=".7" />
        <circle cx="12" cy="20.5" r="2" fill="#fff" fillOpacity=".7" />
        <path d="M6.2 7.1 9.4 10M17.8 7.1 14.6 10M12 15.4v3.1" stroke="#fff" strokeOpacity=".7" strokeWidth="1.4" />
      </svg>
    );
  }
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
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

/** Outline device glyph for chips (connection strip). */
export function DeviceLine({ kind, size = 18 }: { kind: "desktop" | "laptop"; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
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

export type FileKind = "folder" | "image" | "video" | "doc" | "files";
export function fileKindOf(name: string, kind?: string): FileKind {
  if (kind === "folder") return "folder";
  if (kind === "files") return "files";
  const ext = /\.([a-z0-9]+)$/i.exec(name)?.[1]?.toLowerCase() ?? "";
  if (["png", "jpg", "jpeg", "gif", "webp", "bmp", "heic", "svg", "avif"].includes(ext)) return "image";
  if (["mov", "mp4", "mkv", "avi", "webm", "m4v"].includes(ext)) return "video";
  return "doc";
}
export const fileHueOf = (kind: FileKind): Hue => ({ folder: "amber", image: "rose", video: "violet", doc: "teal", files: "sky" } as const)[kind];

export function FileGlyph({ kind, size }: { kind: FileKind; size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      {kind === "folder" ? (
        <>
          <path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2z" fill="#fff" />
          <rect className="rd-deep" x="2" y="9" width="20" height="1.6" fillOpacity=".3" />
        </>
      ) : kind === "image" ? (
        <>
          <rect x="3" y="4.5" width="18" height="15" rx="3" fill="#fff" />
          <circle cx="8.6" cy="9.4" r="1.9" style={{ fill: "var(--h2)" }} />
          <path className="rd-deep" d="M3 16.4l4.6-4.5a1.5 1.5 0 0 1 2.1 0l3.3 3.3 1.8-1.8a1.5 1.5 0 0 1 2.1 0L21 16.4V17a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17z" fillOpacity=".6" />
        </>
      ) : kind === "video" || kind === "files" ? (
        <>
          <rect x="6" y="2.5" width="13" height="16" rx="2.2" fill="#fff" fillOpacity=".55" />
          <rect x="3.5" y="5.5" width="13" height="16" rx="2.2" fill="#fff" />
          <path className="rd-deep" d="M8 11.2v6l5-3z" fillOpacity=".55" />
        </>
      ) : (
        <>
          <path d="M7 3h6.6a2 2 0 0 1 1.4.6l3.4 3.4a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z" fill="#fff" />
          <path className="rd-deep" d="M14 3.3V7a1 1 0 0 0 1 1h3.7z" fillOpacity=".28" />
          <rect className="rd-deep" x="8" y="12" width="8" height="1.7" rx=".85" fillOpacity=".6" />
          <rect className="rd-deep" x="8" y="15.5" width="5.4" height="1.7" rx=".85" fillOpacity=".36" />
        </>
      )}
    </svg>
  );
}

const TILE_ICON = { xs: 19, sm: 21, md: 26, lg: 30, xl: 32 } as const;
export type TileSize = keyof typeof TILE_ICON;
export function Tile({ hue, size = "md", off, className = "", children }: { hue: Hue; size?: TileSize; off?: boolean; className?: string; children: (px: number) => ReactNode }) {
  return <span className={`rf-tile${size === "md" ? "" : ` rf-tile-${size}`} rf-h-${hue}${off ? " off" : ""} ${className}`.trim()} aria-hidden="true">{children(TILE_ICON[size])}</span>;
}
export function DeviceTile({ kind, hue, size = "md", off }: { kind: "desktop" | "laptop" | "mesh"; hue: Hue; size?: TileSize; off?: boolean }) {
  return <Tile hue={hue} size={size} off={off}>{(px) => <DeviceGlyph kind={kind} size={px} />}</Tile>;
}
export function FileTile({ kind, size = "sm" }: { kind: FileKind; size?: TileSize }) {
  return <Tile hue={fileHueOf(kind)} size={size}>{(px) => <FileGlyph kind={kind} size={px} />}</Tile>;
}

export function Pill({ tone, small, line, dot = true, children }: { tone: Tone; small?: boolean; line?: boolean; dot?: boolean; children: ReactNode }) {
  return <span className={`rf-pill rf-t-${tone}${small ? " rf-pill-sm" : ""}${line ? " rf-pill-line" : ""}`}>{dot ? <i aria-hidden="true" /> : null}{children}</span>;
}

/** A stroke icon from a single path (lucide-style 24 grid), for glyphs lucide does not ship. */
export function PathIcon({ d, size = 18, width = 1.8 }: { d: string; size?: number; width?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={width} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={d} /></svg>;
}
export const ADD_DEVICE_PATH = "M12 17v4M8 21h8M14 3H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-4M19 2v6M16 5h6";

export type ServiceKind = "ai" | "document" | "audio" | "network" | "video";
const SERVICE_HUE: Record<ServiceKind, Hue> = { ai: "violet", document: "teal", audio: "sky", network: "amber", video: "rose" };
export const serviceHueOf = (kind: ServiceKind): Hue => SERVICE_HUE[kind] ?? "slate";

export function ServiceGlyph({ kind, size }: { kind: ServiceKind; size: number }) {
  if (kind === "network") return <DeviceGlyph kind="mesh" size={size} />;
  if (kind === "document" || kind === "video") return <FileGlyph kind={kind === "video" ? "video" : "doc"} size={size} />;
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      {kind === "ai" ? (
        <>
          <path d="M10 4.5c.7 4.6 2.9 6.8 7.5 7.5-4.6.7-6.8 2.9-7.5 7.5-.7-4.6-2.9-6.8-7.5-7.5 4.6-.7 6.8-2.9 7.5-7.5z" fill="#fff" />
          <path d="M18.5 2.8c.3 2 1 2.7 3 3-2 .3-2.7 1-3 3-.3-2-1-2.7-3-3 2-.3 2.7-1 3-3z" fill="#fff" fillOpacity=".72" />
          <circle cx="19" cy="18.5" r="1.5" fill="#fff" fillOpacity=".55" />
        </>
      ) : (
        <>
          <rect x="4" y="9" width="3" height="6" rx="1.5" fill="#fff" />
          <rect x="9" y="5" width="3" height="14" rx="1.5" fill="#fff" />
          <rect x="14" y="8" width="3" height="8" rx="1.5" fill="#fff" fillOpacity=".8" />
          <rect x="19" y="10.5" width="2" height="3" rx="1" fill="#fff" fillOpacity=".6" />
        </>
      )}
    </svg>
  );
}
export function ServiceTile({ kind, size = "sm" }: { kind: ServiceKind; size?: TileSize }) {
  return <Tile hue={serviceHueOf(kind)} size={size}>{(px) => <ServiceGlyph kind={kind} size={px} />}</Tile>;
}
