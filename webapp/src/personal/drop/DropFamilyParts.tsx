import { useTranslation } from "react-i18next";
import { Lock } from "lucide-react";
import type { Route } from "../../domain/dropClient";
import { routeLabel } from "./dropModel";
import { Pill, type Tone } from "./FamilyParts";

// Drop-specific pieces shared by the send sheet, the incoming dialog and the connection details.

export const routeTone = (route: Route | null | undefined, online?: boolean | null): Tone =>
  online === false ? "off" : route === "lan" ? "ok" : route === "public" ? "self" : route === "relay" ? "warn" : "off";

/** "LAN direct · 3 ms", with a lock that says the link is end-to-end encrypted. */
export function RouteBadge({ route, latency, online }: { route: Route | null | undefined; latency?: number | null; online?: boolean | null }) {
  const { t } = useTranslation();
  const text = [routeLabel(t, route), latency !== null && latency !== undefined ? t("drop.latency", { ms: Math.round(latency) }) : ""].filter(Boolean).join(" · ");
  return (
    <span className={`rf-route rf-t-${routeTone(route, online)}`}>
      <Lock size={12} strokeWidth={2.2} role="img" aria-label={t("drop.e2eNote")} />{text}
    </span>
  );
}

/** A verification code drawn as boxed characters, split in the middle. The text is also available as one label. */
export function CodeDigits({ code, label }: { code: string; label: string }) {
  const chars = Array.from(code.replace(/[\s-]+/g, ""));
  if (!chars.length || chars.length > 10) return <strong className="rf-code-plain rf-mono">{code || "—"}</strong>;
  const mid = Math.ceil(chars.length / 2);
  return (
    <div className="rf-digits" role="group" aria-label={`${label} ${code}`}>
      {chars.map((char, index) => <span key={index} style={{ display: "contents" }}>
        {index === mid ? <span className="rf-digits-gap" aria-hidden="true" /> : null}
        <span className="rf-digit" aria-hidden="true">{char}</span>
      </span>)}
    </div>
  );
}

export function StatePill({ online, label }: { online: boolean | null; label: string }) {
  return <Pill tone={online === true ? "ok" : online === false ? "off" : "warn"} small>{label}</Pill>;
}
