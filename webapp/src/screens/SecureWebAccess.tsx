import { tr, useUILanguage, uiLocale } from "../uiI18n";
import { personalHref } from "../personal/model";
import { ArrowLeft, ExternalLink, Globe2, ShieldCheck, Unplug } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAppContext } from "../appContext";
import type { EgressStatus } from "../domain/types";
import { useServiceOrder } from "../domain/serviceExperience";
import { serviceDescriptors } from "../domain/serviceDescriptors";
import styles from "./ServiceExperience.module.css";

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
  return (
    <div className={styles.page}>
      <button type="button" className={styles.back} onClick={() => navigate(personalHref("/services", client.mode === "fixture"))}><ArrowLeft size={15} /> {tr("All services")}</button>
      <header className={styles.hero}>
        <div className={styles.heroTitle}><span className={styles.heroIcon}><ShieldCheck size={25} /></span><div><h1>{tr("Secure web access")}</h1><p>{tr("Browse through an encrypted route selected by Ryn.")}</p></div></div>
        <span className={styles.status}>{connected ? tr("Connected") : tr("Ready to connect")}</span>
      </header>
      <div className={styles.layout}>
        <section className={styles.panel}>
          <div className={styles.connectedHero}><span className={styles.connectedIcon}>{connected ? <ShieldCheck size={31} /> : <Globe2 size={31} />}</span><h2>{connected ? tr("Your secure route is active") : tr("Connect when you are ready")}</h2><p>{connected ? tr("Open a protected browser window using this route.") : tr("You will see the price and route status before browsing.")}</p></div>
          {visibleError ? <p role="alert" className={styles.error}>{visibleError}</p> : null}
          <div className={styles.actions}>
            {!connected ? <button type="button" className={styles.primary} disabled={Boolean(busy) || route.busy || route.loading} onClick={() => void connect()}><ShieldCheck size={16} /> {busy === "connect" ? tr("Connecting…") : tr("Connect securely")}</button> : <><button type="button" className={styles.primary} disabled={Boolean(busy) || route.busy || route.loading} onClick={() => void launch()}><ExternalLink size={16} /> {busy === "launch" ? tr("Opening…") : tr("Open secure browser")}</button><button type="button" className={styles.danger} disabled={Boolean(busy) || route.busy || route.loading} onClick={() => void disconnect()}><Unplug size={15} /> {busy === "disconnect" ? tr("Disconnecting…") : tr("Disconnect")}</button></>}
          </div>
        </section>
        <aside className={styles.panel}>
          <h2>{tr("Connection")}</h2><p className={styles.panelLead}>{tr("Ryn handles the provider and encrypted route automatically.")}</p>
          <div className={styles.summary}><div className={styles.summaryRow}><span>{tr("Status")}</span><strong>{connected ? tr("Protected") : tr("Not connected")}</strong></div><div className={styles.summaryRow}><span>{tr("Region")}</span><strong>{tr(descriptor.region.label)}</strong></div><div className={styles.summaryRow}><span>{tr(descriptor.pricing.label)}</span><strong>{status?.priceCredits ? tr("{{count}} credits", { count: status.priceCredits }) : tr("Shown when connected")}</strong></div>{connected ? <><div className={styles.summaryRow}><span>{tr("Exit location")}</span><strong>{status?.loc || tr("Checking")}{status?.locVerified ? tr(" · verified") : ""}</strong></div><div className={styles.summaryRow}><span>{tr("Expires")}</span><strong>{status?.ttlExpiresAt ? new Date(status.ttlExpiresAt).toLocaleTimeString(uiLocale()) : "—"}</strong></div></> : null}</div>
          {status?.providerPeerId ? <details className={styles.details}><summary>{tr("Route details")}</summary><div className={styles.detailBody}><span>{status.providerNodeName}</span><span>{status.exitIp}</span><span>{status.providerPeerId}</span></div></details> : null}
        </aside>
      </div>
    </div>
  );
}
