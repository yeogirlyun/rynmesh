// Draws the operating-system icon of every app from the layers the Apps page uses:
//   icons/apps/<id>.ico   Windows Start menu, taskbar and file types (full tile)
//   icons/apps/<id>.icns  macOS launcher bundles (tile on Apple's 824/1024 grid)
//   icons/apps/<id>.png   64px editor window icon
// Run from webapp/ after changing appIconData.ts or the tones in craft-apps-refresh.css:
//   node src-tauri/scripts/generate-app-icons.mjs
import { execFileSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { layers, tones } from "../../src/screens/appIconData.ts";

const webapp = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const out = join(webapp, "src-tauri", "icons", "apps");
const tauri = join(webapp, "node_modules", "@tauri-apps", "cli", "tauri.js");

// The tile gradient colours are the page's own: .ra-t-<tone> { --c1; --c2 }.
const css = readFileSync(join(webapp, "src", "screens", "craft-apps-refresh.css"), "utf8");
function tone(name) {
  const rule = css.match(new RegExp(`\\.ra-t-${name}\\s*\\{([^}]*)\\}`));
  const c1 = rule?.[1].match(/--c1:\s*(#[0-9a-f]{6})/i)?.[1];
  const c2 = rule?.[1].match(/--c2:\s*(#[0-9a-f]{6})/i)?.[1];
  if (!c1 || !c2) throw new Error(`tone ${name} has no --c1/--c2 in craft-apps-refresh.css`);
  return { c1, c2 };
}

// A CSS linear-gradient angle as SVG bounding-box coordinates whose 0% and 100% touch the corners.
function gradient(id, degrees, stops) {
  const a = (degrees * Math.PI) / 180;
  const dx = Math.sin(a), dy = -Math.cos(a);
  const half = (Math.abs(dx) + Math.abs(dy)) / 2;
  const f = (n) => n.toFixed(4);
  return `<linearGradient id="${id}" x1="${f(0.5 - dx * half)}" y1="${f(0.5 - dy * half)}" x2="${f(0.5 + dx * half)}" y2="${f(0.5 + dy * half)}">${stops}</linearGradient>`;
}

const escape = (s) => s.replace(/[<>&"]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" })[c]);

/** One icon as SVG. `inset` and `radius` are in 48-unit tile space; mac adds Apple's soft shadow. */
function svg(id, { inset, radius, shadow }) {
  const l = layers[id];
  const { c1, c2 } = tone(tones[id] || "blue");
  const size = 48 - inset * 2;
  const s = size / 48;
  // .ra-gl draws the 48-unit glyph inside 3px of padding with a soft drop shadow.
  const glyph = `translate(${inset + 3 * s} ${inset + 3 * s}) scale(${(42 / 48) * s})`;
  const parts = [
    l.back && `<path d="${l.back}" fill="#fff" fill-opacity=".4" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/>`,
    l.main && `<path d="${l.main}" fill="#fff" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/>`,
    l.mark && `<path d="${l.mark}" fill="${c1}" fill-opacity=".9"/>`,
    l.line && `<path d="${l.line}" fill="none" stroke="${c1}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" stroke-opacity=".55"/>`,
    l.glyph && `<path d="${l.glyph}" fill="none" stroke="#fff" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"/>`,
    l.label &&
      `<text x="24" y="${l.y ?? 30}" font-size="${l.size ?? 10}" text-anchor="middle" fill="${c1}" font-family="Segoe UI, Arial, sans-serif" font-weight="800" letter-spacing="-0.02em">${escape(l.label)}</text>`,
  ].filter(Boolean);
  return `<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 48 48">
<defs>
${gradient("tile", 145, `<stop offset="0" stop-color="${c1}"/><stop offset="1" stop-color="${c2}"/>`)}
${gradient("shine", 160, `<stop offset="0" stop-color="#fff" stop-opacity=".3"/><stop offset=".46" stop-color="#fff" stop-opacity="0"/>`)}
<filter id="glyph" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="0" dy="${1.5 * s}" stdDeviation="${0.75 * s}" flood-color="#0a1432" flood-opacity=".22"/></filter>
<filter id="tile-shadow" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="0" dy=".5" stdDeviation=".6" flood-color="#000" flood-opacity=".28"/></filter>
</defs>
<g${shadow ? ' filter="url(#tile-shadow)"' : ""}><rect x="${inset}" y="${inset}" width="${size}" height="${size}" rx="${radius}" fill="url(#tile)"/></g>
<rect x="${inset}" y="${inset}" width="${size}" height="${size}" rx="${radius}" fill="url(#shine)"/>
<g filter="url(#glyph)" transform="${glyph}">${parts.join("")}</g>
</svg>
`;
}

// The drawn tile is 48 units with a 14-unit corner; Windows keeps it nearly full bleed, macOS
// uses Apple's 100/1024 margin and 185/824 corner so it lines up with other Launchpad icons.
const windows = { inset: 1, radius: 14 * (46 / 48), shadow: false };
const mac = { inset: 4.6875, radius: 38.625 * (185 / 824), shadow: true };

function render(id, variant, pick) {
  const work = mkdtempSync(join(tmpdir(), `ryn-icon-${id}-`));
  try {
    const source = join(work, "source.svg");
    writeFileSync(source, svg(id, variant));
    execFileSync(process.execPath, [tauri, "icon", source, "-o", join(work, "out")], { stdio: "pipe" });
    for (const [from, to] of pick) copyFileSync(join(work, "out", from), join(out, to));
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
}

mkdirSync(out, { recursive: true });
for (const id of Object.keys(layers).sort()) {
  render(id, windows, [["icon.ico", `${id}.ico`], ["64x64.png", `${id}.png`]]);
  render(id, mac, [["icon.icns", `${id}.icns`]]);
  console.log(`icons/apps/${id}.{ico,icns,png}`);
}
