import { tr, useUILanguage, uiLocale } from "../uiI18n";
import { personalHref } from "../personal/model";
import { ArrowLeft, ChevronDown, ExternalLink, Globe2, ShieldCheck, Unplug } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAppContext } from "../appContext";
import type { EgressStatus } from "../domain/types";
import { useServiceOrder } from "../domain/serviceExperience";
import { serviceDescriptors } from "../domain/serviceDescriptors";
import { GlyphTile, ShieldGlyph } from "./ServiceParts";
import "./service-pages-refresh.css";

const descriptor = serviceDescriptors.secureWeb;
const REGION = descriptor.region.code;

export default function SecureWebAccess() {
  useUILanguage();
  const { client, notify } = useAppContext();
  const navigate = useNavigate();
  const route = useServiceOrder<EgressStatus>({ scope: client, key: descriptor.id + REGION,
    load: () => client.egressStatus(REGION), intervalMs: descriptor.orderIntervalMs,
  });
  const status = route.data;
  const [busy, setBusy] = useState<"connect" | "launch" | "disconnect" | "">("");
  const [error, setError] = useState("");

  const connect = async () => {
    setBusy("connect"); setError("");
    try { await route.execute(() => client.egressConnect({ region: REGION }), (next) => next); notify("ok", "Secure route connected"); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not connect."); }
    finally { setBusy(""); }
  };

  const launch = async () => {
    setBusy("launch"); setError("");
    try {
      const result = await route.execute(() => client.egressLaunch({ region: REGION }));
      if (result.lastError) throw new Error(result.lastError);
      notify("ok", tr("Secure browser launched"));
    } catch (reason) { setError(reason instanceof Error ? reason.message : tr("Could not launch the browser.")); }
    finally { setBusy(""); }
  };

  const disconnect = async () => {
    setBusy("disconnect"); setError("");
    try { await route.execute(() => client.egressDisconnect({ region: REGION }), (next) => next); notify("info", "Secure route disconnected"); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not disconnect."); }
    finally { setBusy(""); }
  };

  const connected = Boolean(status?.connected);
  const visibleError = error || route.error?.message || "";
  const idle = Boolean(busy) || route.busy || route.loading;
  return (
    <div className="rf rd-svc rd-secure sv-narrow">
      <button type="button" className="rf-lnk sv-back" onClick={() => navigate(personalHref("/services", client.mode === "fixture"))}><ArrowLeft size={14} aria-hidden="true" /> {tr("All services")}</button>
      <header className="sv-head-ident rf-h-teal">
        <GlyphTile hue="teal" size="xl">{(px) => <ShieldGlyph size={px} />}</GlyphTile>
        <div className="sv-head-text"><h1 className="sv-title">{tr("Secure web access")}</h1><p className="sv-lead">{tr("Browse through an encrypted route selected by Ryn.")}</p></div>
        <span className={`rf-pill rf-t-${connected ? "ok" : "off"}`}><i aria-hidden="true" />{connected ? tr("Connected") : tr("Ready to connect")}</span>
      </header>
      <div className="sv-cols">
        <section className="sv-card sv-main">
          <div className="sv-hero rf-h-teal"><span className="sv-hero-ico" aria-hidden="true">{connected ? <ShieldCheck size={28} strokeWidth={1.7} /> : <Globe2 size={28} strokeWidth={1.7} />}</span><h2>{connected ? tr("Your secure route is active") : tr("Connect when you are ready")}</h2><p>{connected ? tr("Open a protected browser window using this route.") : tr("You will see the price and route status before browsing.")}</p>
            {visibleError ? <p role="alert" className="sv-alert sv-alert-bad">{visibleError}</p> : null}
            <div className="sv-hero-actions">
              {!connected ? <button type="button" className="rf-btn rf-btn-p rf-btn-lg" disabled={idle} onClick={() => void connect()}><ShieldCheck size={15} aria-hidden="true" /> {busy === "connect" ? tr("Connecting…") : tr("Connect securely")}</button> : <><button type="button" className="rf-btn rf-btn-p rf-btn-lg" disabled={idle} onClick={() => void launch()}><ExternalLink size={15} aria-hidden="true" /> {busy === "launch" ? tr("Opening…") : tr("Open secure browser")}</button><button type="button" className="rf-btn rf-btn-d rf-btn-lg" disabled={idle} onClick={() => void disconnect()}><Unplug size={14} aria-hidden="true" /> {busy === "disconnect" ? tr("Disconnecting…") : tr("Disconnect")}</button></>}
            </div>
          </div>
        </section>
        <aside className="sv-card sv-aside" aria-labelledby="sv-conn-title">
          <h2 id="sv-conn-title" className="sv-h2">{tr("Connection")}</h2><p className="sv-note">{tr("Ryn handles the provider and encrypted route automatically.")}</p>
          <dl className="sv-rows"><div><dt>{tr("Status")}</dt><dd>{connected ? tr("Protected") : tr("Not connected")}</dd></div><div><dt>{tr("Region")}</dt><dd>{tr(descriptor.region.label)}</dd></div><div><dt>{tr(descriptor.pricing.label)}</dt><dd>{status?.priceCredits ? tr("{{count}} credits", { count: status.priceCredits }) : tr("Shown when connected")}</dd></div>{connected ? <><div><dt>{tr("Exit location")}</dt><dd>{status?.loc || tr("Checking")}{status?.locVerified ? tr(" · verified") : ""}</dd></div><div><dt>{tr("Expires")}</dt><dd>{status?.ttlExpiresAt ? new Date(status.ttlExpiresAt).toLocaleTimeString(uiLocale()) : "—"}</dd></div></> : null}</dl>
          {status?.providerPeerId ? <details className="sv-details"><summary><span>{tr("Route details")}</span><ChevronDown size={14} aria-hidden="true" /></summary><div className="sv-details-body"><span>{status.providerNodeName}</span><span>{status.exitIp}</span><span>{status.providerPeerId}</span></div></details> : null}
        </aside>
      </div>
    </div>
  );
}
