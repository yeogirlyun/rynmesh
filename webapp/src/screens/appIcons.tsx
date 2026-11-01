// One drawn icon per app; the layers and tones live in appIconData.ts.
import { PAGE, PAGE_BACK, layers, tones } from "./appIconData";

/** Colour family for an app; unknown apps fall back to the brand blue. */
export const appTone = (id: string) => tones[id] || "blue";

export default function AppIcon({ id, name }: { id: string; name: string }) {
  // A plugin this release has no drawing for still gets a tidy sheet with its initial.
  const l = layers[id] || { back: PAGE_BACK, main: PAGE, label: name.slice(0, 1).toUpperCase(), size: 17, y: 34 };
  return (
    <svg className="ra-gl" viewBox="0 0 48 48" aria-hidden="true">
      {l.back && <path className="ra-g-back" d={l.back} />}
      {l.main && <path className="ra-g-main" d={l.main} />}
      {l.mark && <path className="ra-g-mark" d={l.mark} />}
      {l.line && <path className="ra-g-line" d={l.line} />}
      {l.glyph && <path className="ra-g-glyph" d={l.glyph} />}
      {l.label && (
        <text className="ra-g-text" x="24" y={l.y ?? 30} fontSize={l.size ?? 10} textAnchor="middle">
          {l.label}
        </text>
      )}
    </svg>
  );
}
