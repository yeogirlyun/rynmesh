import type { ReactNode } from "react";
import { Tile, DeviceGlyph, type Hue, type TileSize } from "../personal/drop/FamilyParts";

// Presentational pieces shared by the redesigned service sub-pages. Styles live in service-pages-refresh.css (and the
// per-page refresh files) under each page's root; the tile, pill and button primitives come from personal/drop/family.css.

export type ServiceHue = Hue;

/** Page title block: `<h1>`, one-line lead and optional actions on the right. */
export function SvHead({ title, lead, children }: { title: ReactNode; lead?: ReactNode; children?: ReactNode }) {
  return (
    <header className="sv-head">
      <div className="sv-head-text"><h1 className="sv-title">{title}</h1>{lead ? <p className="sv-lead">{lead}</p> : null}</div>
      {children ? <div className="sv-head-actions">{children}</div> : null}
    </header>
  );
}

/* ---- drawn glyphs: a white body with details in the hue's deep shade ---- */

export function ChatGlyph({ size }: { size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H11l-4.2 3.6c-.5.4-1.3.1-1.3-.6V16A2.5 2.5 0 0 1 4 13.5z" fill="#fff" />
      <path className="rd-deep" d="M12 6.2l1 2.9 2.9 1-2.9 1-1 2.9-1-2.9-2.9-1 2.9-1z" fillOpacity=".55" />
    </svg>
  );
}

export function SparkGlyph({ size }: { size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="#fff" aria-hidden="true">
      <path d="M12 2.5l1.9 5.6 5.6 1.9-5.6 1.9L12 17.5l-1.9-5.6L4.5 10l5.6-1.9z" />
      <path d="M18.5 15l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z" fillOpacity=".75" />
    </svg>
  );
}

export function VideoGlyph({ size }: { size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <rect x="2.5" y="5" width="13.5" height="14" rx="2.6" fill="#fff" />
      <path d="M17 10.2l3.9-2.3c.5-.3 1.1.1 1.1.6v7c0 .6-.6.9-1.1.6L17 13.8z" fill="#fff" fillOpacity=".8" />
      <path className="rd-deep" d="M7.6 9.4v5.2c0 .4.4.6.7.4l4-2.6c.3-.2.3-.6 0-.8l-4-2.6c-.3-.2-.7 0-.7.4z" fillOpacity=".55" />
    </svg>
  );
}

export function ShieldGlyph({ size }: { size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 2.5l7.5 3v6c0 4.8-3.2 8.4-7.5 10-4.3-1.6-7.5-5.2-7.5-10v-6z" fill="#fff" />
      <path d="M8.8 12.2l2.2 2.2 4.4-4.6" fill="none" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ stroke: "var(--hd)" }} strokeOpacity=".7" />
    </svg>
  );
}

export function TermGlyph({ size, width = 2.2 }: { size: number; width?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth={width} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="m4 17 6-6-6-6M12 19h8" />
    </svg>
  );
}

export function GlobeGlyph({ size }: { size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
    </svg>
  );
}

export function LaptopGlyph({ size }: { size: number }) {
  return <DeviceGlyph kind="laptop" size={size} />;
}

export function DesktopGlyph({ size }: { size: number }) {
  return <DeviceGlyph kind="desktop" size={size} />;
}

/** A hue tile with a drawn glyph. */
export function GlyphTile({ hue, size = "md", off, children }: { hue: Hue; size?: TileSize; off?: boolean; children: (px: number) => ReactNode }) {
  return <Tile hue={hue} size={size} off={off}>{children}</Tile>;
}

/** Letter mark for a model or provider (one hue per source). */
export function MarkTile({ hue, size = "sm", children }: { hue: Hue; size?: "sm" | "xs" | "md"; children: ReactNode }) {
  return <span className={`rf-tile rf-tile-${size} rf-h-${hue} sv-mark`} aria-hidden="true">{children}</span>;
}
