import type { CSSProperties } from "react";
import { AudioLines, Clapperboard, DraftingCompass, FileUser, HardDrive, Headset, Image, LayoutTemplate, MousePointer2, PenTool, SlidersHorizontal, UsersRound, WandSparkles } from "lucide-react";
import type { CraftPlugin } from "../domain/craftPlugins";

const tones: Record<string, string> = {
  pdfcraft: "#bd5957", wordcraft: "#4d72b6", gridcraft: "#41846b", deckcraft: "#c0844f",
  cadcraft: "#547b8f", designcraft: "#b4718c", figcraft: "#8970b0", photocraft: "#587fb0",
  lightcraft: "#78875e", vectorcraft: "#bd9353", soundcraft: "#9978b0", effectcraft: "#7d77b2",
  filmcraft: "#617d8e", resume: "#647ab0", recruit: "#9672a5", desk: "#b08c5d", nas: "#558d85",
};
const letters: Record<string, string> = { pdfcraft: "PDF", wordcraft: "W", gridcraft: "X", deckcraft: "P" };

export default function CraftPluginIcon({ app }: { app: CraftPlugin }) {
  const Icon = {
    cadcraft: DraftingCompass, designcraft: LayoutTemplate, figcraft: MousePointer2,
    photocraft: Image, lightcraft: SlidersHorizontal, vectorcraft: PenTool,
    soundcraft: AudioLines, effectcraft: WandSparkles, filmcraft: Clapperboard,
    resume: FileUser, recruit: UsersRound, desk: Headset, nas: HardDrive,
  }[app.id] || LayoutTemplate;
  const letter = letters[app.id];
  return <span className={`craft-mark craft-icon ${letter ? "craft-icon-office" : ""}`} data-tool={app.id}
    style={{ "--craft-icon-tone": tones[app.id] || "#647ab0" } as CSSProperties} aria-hidden="true">
    {letter ? <svg viewBox="0 0 48 48" fill="none">
      <rect x="19" y="6" width="24" height="35" rx="4" fill="currentColor" opacity=".16" />
      {app.id === "gridcraft" ? <path d="M24 13h14M24 20h14M24 27h14M24 34h14M29 13v21M35 13v21" stroke="currentColor" strokeWidth="1.3" opacity=".42" />
        : app.id === "deckcraft" ? <><path d="M31 14a8 8 0 1 0 8 8h-8v-8Z" fill="currentColor" opacity=".4" /><path d="M33 12a8 8 0 0 1 8 8h-8v-8Z" fill="currentColor" opacity=".7" /></>
          : <path d="M25 14h12M25 20h12M25 26h12M25 32h8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" opacity=".42" />}
      <rect x="5" y="14" width="27" height="28" rx="5" fill="currentColor" />
      <text x="18.5" y="32.5" textAnchor="middle" fill="white" fontFamily="Segoe UI, sans-serif" fontSize={letter === "PDF" ? 9 : 18} fontWeight="650">{letter}</text>
    </svg> : <Icon size={27} strokeWidth={1.65} />}
  </span>;
}
