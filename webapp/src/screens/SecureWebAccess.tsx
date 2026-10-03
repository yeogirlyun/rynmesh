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
      notify("ok", "Secure browser launched");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not launch the browser."); }
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
      <button type="button" className={styles.back} onClick={() => navigate(client.mode === "fixture" ? "/services?client=fixture" : "/services")}><ArrowLeft size={15} /> All services</button>
      <header className={styles.hero}>
        <div className={styles.heroTitle}><span className={styles.heroIcon}><ShieldCheck size={25} /></span><div><h1>Secure web access</h1><p>Browse through an encrypted route selected by Ryn.</p></div></div>
        <span className={styles.status}>{connected ? "Connected" : "Ready to connect"}</span>
      </header>
      <div className={styles.layout}>
        <section className={styles.panel}>
          <div className={styles.connectedHero}><span className={styles.connectedIcon}>{connected ? <ShieldCheck size={31} /> : <Globe2 size={31} />}</span><h2>{connected ? "Your secure route is active" : "Connect when you are ready"}</h2><p>{connected ? "Open a protected browser window using this route." : "You will see the price and route status before browsing."}</p></div>
          {visibleError ? <p role="alert" className={styles.error}>{visibleError}</p> : null}
          <div className={styles.actions}>
            {!connected ? <button type="button" className={styles.primary} disabled={Boolean(busy) || route.busy || route.loading} onClick={() => void connect()}><ShieldCheck size={16} /> {busy === "connect" ? "Connecting…" : "Connect securely"}</button> : <><button type="button" className={styles.primary} disabled={Boolean(busy) || route.busy || route.loading} onClick={() => void launch()}><ExternalLink size={16} /> {busy === "launch" ? "Opening…" : "Open secure browser"}</button><button type="button" className={styles.danger} disabled={Boolean(busy) || route.busy || route.loading} onClick={() => void disconnect()}><Unplug size={15} /> {busy === "disconnect" ? "Disconnecting…" : "Disconnect"}</button></>}
          </div>
        </section>
        <aside className={styles.panel}>
          <h2>Connection</h2><p className={styles.panelLead}>Ryn handles the provider and encrypted route automatically.</p>
          <div className={styles.summary}><div className={styles.summaryRow}><span>Status</span><strong>{connected ? "Protected" : "Not connected"}</strong></div><div className={styles.summaryRow}><span>Region</span><strong>{descriptor.region.label}</strong></div><div className={styles.summaryRow}><span>{descriptor.pricing.label}</span><strong>{status?.priceCredits ? `${status.priceCredits} credit` : "Shown when connected"}</strong></div>{connected ? <><div className={styles.summaryRow}><span>Exit location</span><strong>{status?.loc || "Checking"}{status?.locVerified ? " · verified" : ""}</strong></div><div className={styles.summaryRow}><span>Expires</span><strong>{status?.ttlExpiresAt ? new Date(status.ttlExpiresAt).toLocaleTimeString() : "—"}</strong></div></> : null}</div>
          {status?.providerPeerId ? <details className={styles.details}><summary>Route details</summary><div className={styles.detailBody}><span>{status.providerNodeName}</span><span>{status.exitIp}</span><span>{status.providerPeerId}</span></div></details> : null}
        </aside>
      </div>
    </div>
  );
}
