import { tr, useUILanguage } from "../uiI18n";
import type { CSSProperties } from "react";

export function RynMark({ size = 28 }: {
  size?: number;
  tone?: "green" | "blue" | "amber" | "paper" | "ink";
  flat?: boolean;
  shadow?: boolean;
}) {
  useUILanguage();
  return <svg className="ryn-mark" width={size} height={size * 1.13} viewBox="0 0 30 34" aria-label={tr("Ryn mark")}>
    <path d="M2 3h15c15 0 16 18 3 21l9 10H18L8 22v12H2V15h14c5 0 5-5 0-5H2V3Z" fill="currentColor" />
    <path d="M2 15h14c3 0 5-1 6-3 2 4-1 8-6 8H2v-5Z" fill="#98a6bf" />
  </svg>;
}

export function RynWordmark({
  product = "",
  size = 21,
  muted = false,
}: {
  product?: string;
  size?: number;
  muted?: boolean;
}) {
  useUILanguage();
  return (
    <span className="ryn-wordmark" style={{ "--wordmark-size": `${size}px` } as CSSProperties}>
      <span className="ryn-wordmark-head">Ryn</span>
      {product ? <span className={muted ? "ryn-product-tag muted-tag" : "ryn-product-tag"}>{product}</span> : null}
    </span>
  );
}

export function RynLockup({
  tagline = tr("Your devices, connected"),
  product = "",
}: {
  tagline?: string;
  product?: string;
}) {
  useUILanguage();
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
