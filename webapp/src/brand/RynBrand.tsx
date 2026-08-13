import type { CSSProperties } from "react";

const HEX_PATH = "M50 8 L86 28 L86 42 L62 50 L86 58 L86 72 L50 92 L14 72 L14 28 Z";
const HEX_TOP_EDGE = "M14 28 L50 8 L86 28";

export function RynMark({
  size = 28,
  tone = "green",
  flat = false,
  shadow = false,
}: {
  size?: number;
  tone?: "green" | "blue" | "amber" | "paper" | "ink";
  flat?: boolean;
  shadow?: boolean;
}) {
  if (flat || size <= 24) {
    const fill = {
      green: "#00D084",
      blue: "#39A9FF",
      amber: "#F5C542",
      paper: "#F4F1EA",
      ink: "#0B0F14",
    }[tone];
    return (
      <svg className="ryn-mark" width={size} height={size} viewBox="0 0 100 100" aria-label="Ryn mark">
        <path d={HEX_PATH} fill={fill} />
      </svg>
    );
  }

  const palette = {
    green: {
      hi: "#5CECB1",
      body: "#00D084",
      deep: "#007048",
      edge: "#003E27",
    },
    blue: {
      hi: "#8FCDFF",
      body: "#39A9FF",
      deep: "#0E5394",
      edge: "#0A2D52",
    },
    amber: {
      hi: "#FFE08C",
      body: "#F5C542",
      deep: "#8C661A",
      edge: "#3F2E10",
    },
    paper: {
      hi: "#FFFFFF",
      body: "#F4F1EA",
      deep: "#9B937A",
      edge: "#3D372A",
    },
    ink: {
      hi: "#3A4655",
      body: "#1B2838",
      deep: "#05080C",
      edge: "#000000",
    },
  }[tone];

  const id = `ryn-${tone}-${size}`.replace(/\W/g, "");

  return (
    <svg
      className="ryn-mark"
      width={size}
      height={size * 1.05}
      viewBox="-2 -2 104 109"
      aria-label="Ryn mark"
    >
      <defs>
        <linearGradient id={`${id}-body`} x1="0.18" y1="0.05" x2="0.85" y2="0.95">
          <stop offset="0%" stopColor={palette.hi} stopOpacity="0.92" />
          <stop offset="22%" stopColor={palette.body} />
          <stop offset="65%" stopColor={palette.body} />
          <stop offset="100%" stopColor={palette.deep} />
        </linearGradient>
        <radialGradient id={`${id}-gloss`} cx="0.42" cy="0.22" r="0.55" fx="0.36" fy="0.18">
          <stop offset="0%" stopColor="#FFFFFF" stopOpacity="0.55" />
          <stop offset="55%" stopColor="#FFFFFF" stopOpacity="0.08" />
          <stop offset="100%" stopColor="#FFFFFF" stopOpacity="0" />
        </radialGradient>
        <linearGradient id={`${id}-bevel`} x1="0" y1="0.7" x2="0" y2="1">
          <stop offset="0%" stopColor={palette.edge} stopOpacity="0" />
          <stop offset="100%" stopColor={palette.edge} stopOpacity="0.42" />
        </linearGradient>
        <linearGradient id={`${id}-notch`} x1="0.5" y1="0" x2="0.5" y2="1">
          <stop offset="0%" stopColor={palette.edge} stopOpacity="0.5" />
          <stop offset="100%" stopColor={palette.edge} stopOpacity="0" />
        </linearGradient>
        <clipPath id={`${id}-clip`}>
          <path d={HEX_PATH} />
        </clipPath>
        <filter id={`${id}-cast`} x="-30%" y="-10%" width="160%" height="160%">
          <feGaussianBlur in="SourceAlpha" stdDeviation="3.4" />
          <feOffset dx="0" dy="3.2" />
          <feComponentTransfer>
            <feFuncA type="linear" slope="0.55" />
          </feComponentTransfer>
          <feMerge>
            <feMergeNode />
          </feMerge>
        </filter>
      </defs>
      {shadow ? <path d={HEX_PATH} fill={palette.edge} opacity="0.55" filter={`url(#${id}-cast)`} /> : null}
      <path d={HEX_PATH} fill={`url(#${id}-body)`} />
      <g clipPath={`url(#${id}-clip)`}>
        <ellipse cx="38" cy="22" rx="52" ry="22" fill={`url(#${id}-gloss)`} />
        <rect x="-4" y="58" width="108" height="40" fill={`url(#${id}-bevel)`} />
        <path d="M86 42 L62 50 L86 58 L86 60 L60 50 L86 40 Z" fill={`url(#${id}-notch)`} />
        <rect x="30" y="0" width="14" height="100" fill="#FFFFFF" opacity="0.04" />
      </g>
      <path
        d={HEX_TOP_EDGE}
        fill="none"
        stroke="#FFFFFF"
        strokeOpacity="0.55"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M86 42 L62 50"
        fill="none"
        stroke="#FFFFFF"
        strokeOpacity="0.32"
        strokeWidth="1.2"
        strokeLinecap="round"
      />
      <path
        d="M14 72 L50 92 L86 72"
        fill="none"
        stroke={palette.edge}
        strokeOpacity="0.55"
        strokeWidth="1.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function RynWordmark({
  product = "WEBAPP",
  size = 21,
  muted = false,
}: {
  product?: string;
  size?: number;
  muted?: boolean;
}) {
  return (
    <span className="ryn-wordmark" style={{ "--wordmark-size": `${size}px` } as CSSProperties}>
      <span className="ryn-wordmark-head">Ryn</span>
      {product ? <span className={muted ? "ryn-product-tag muted-tag" : "ryn-product-tag"}>{product}</span> : null}
    </span>
  );
}

export function RynLockup({
  tagline = "LOCAL CONSOLE",
  product = "WEBAPP",
}: {
  tagline?: string;
  product?: string;
}) {
  return (
    <span className="ryn-lockup">
      <RynMark size={52} shadow />
      <span>
        <RynWordmark product={product} size={28} />
        <span className="ryn-tagline">{tagline}</span>
      </span>
    </span>
  );
}
