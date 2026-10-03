import { ArrowLeft, Film, Play, RotateCcw } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAppContext } from "../appContext";
import type { JobCapacity, WorkOrder, WorkResult } from "../domain/types";
import { useProviderDiscovery, useServiceOrder } from "../domain/serviceExperience";
import { providerIdentity, serviceDescriptors } from "../domain/serviceDescriptors";
import { NodeClientError } from "../domain/nodeClient";
import styles from "./ServiceExperience.module.css";

const descriptor = serviceDescriptors.video;
const CAPABILITY = descriptor.capability;
const OPERATION = descriptor.operation;

export default function VideoRendering() {
  const { client, notify } = useAppContext();
  const navigate = useNavigate();
  const discovery = useProviderDiscovery<JobCapacity>({ scope: client, key: descriptor.id,
    intervalMs: descriptor.discoveryIntervalMs,
    load: async () => (await client.listJobCapacities({ capability: CAPABILITY })).filter((item) => item.capabilities.includes(CAPABILITY)),
    identity: (item) => providerIdentity(item.network_id, item.peer_id, CAPABILITY),
  });
  const [projectId, setProjectId] = useState("");
  const [maxScenes, setMaxScenes] = useState("0");
  const [skipExisting, setSkipExisting] = useState(true);
  const [busy, setBusy] = useState(false);
  const [order, setOrder] = useState<WorkOrder | null>(null);
  const [orderProvider, setOrderProvider] = useState<JobCapacity | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState("");

  const orderId = order?.work_order_id ?? "";
  const result = useServiceOrder<WorkResult[]>({ scope: client,
    key: JSON.stringify([order?.network_id, order?.provider_peer_id, orderId]), enabled: Boolean(orderId),
    intervalMs: descriptor.orderIntervalMs,
    load: async () => (await client.listWorkResults({ work_order_id: orderId, network_id: order?.network_id })).filter((row) =>
      row.work_order_id === orderId && row.provider_peer_id === order?.provider_peer_id && row.network_id === order?.network_id),
    isTerminal: (rows) => Boolean(rows[0] && ["completed", "failed", "cancelled"].includes(rows[0].status)),
  });
  const latest = result.data?.[0];
  const unfinished = Boolean(order && (!latest || !["completed", "failed", "cancelled"].includes(latest.status)));
  const provider = orderProvider ?? discovery.providers[0];
  const price = Number(provider?.price_credits[CAPABILITY] ?? 0);
  const visibleError = error || result.error?.message || discovery.error?.message || "";
  const refreshResult = () => result.refresh();

  const submit = async () => {
    if (!provider || !projectId.trim() || busy || result.busy || unfinished || uncertain) return;
    setBusy(true);
    setError("");
    try {
      const order = await result.execute(() => client.submitWorkOrder({
        provider_peer_id: provider.peer_id,
        capability: CAPABILITY,
        operation: OPERATION,
        max_credit_cost: price,
        network_id: provider.network_id,
        params: { video_id: projectId.trim(), max_scenes: Number(maxScenes || 0), skip_existing: skipExisting },
      }));
      setOrder(order.order);
      setOrderProvider(provider);
      notify("ok", "Video render request submitted");
    } catch (reason) {
      const rejected = reason instanceof NodeClientError && [400, 401, 403, 422].includes(reason.status ?? 0);
      setUncertain(!rejected);
      setError((rejected ? "The node rejected the request. Review the fields and try again. " : "The node did not confirm submission. Check the original request with the provider before starting another render; it may already be running. ") + (reason instanceof Error ? reason.message : ""));
    } finally {
      setBusy(false);
    }
  };


  return (
    <div className={styles.page}>
      <button type="button" className={styles.back} onClick={() => navigate(client.mode === "fixture" ? "/services?client=fixture" : "/services")}><ArrowLeft size={15} /> All services</button>
      <header className={styles.hero}>
        <div className={styles.heroTitle}><span className={styles.heroIcon}><Film size={25} /></span><div><h1>Video rendering</h1><p>Create motion clips through an available rendering service.</p></div></div>
        <span className={styles.status}>{provider ? "Renderer ready" : "Finding renderer"}</span>
      </header>
      <div className={styles.layout}>
        <section className={styles.panel}>
          <h2>Create a render</h2><p className={styles.panelLead}>Choose the project to render. Ryn selects the provider and route for you.</p>
          <div className={styles.form}>
            <label className={styles.field}>Video project ID<input aria-label="Video project ID" value={projectId} onChange={(event) => setProjectId(event.target.value)} placeholder="Enter a project ID" /></label>
            <label className={styles.field}>Maximum scenes<input aria-label="Maximum scenes" type="number" min="0" value={maxScenes} onChange={(event) => setMaxScenes(event.target.value)} /></label>
            <label className={styles.check}><input type="checkbox" checked={skipExisting} onChange={(event) => setSkipExisting(event.target.checked)} /> Skip clips that are already rendered</label>
            {visibleError ? <span role="alert" className={styles.error}>{visibleError}</span> : null}
            {uncertain ? <button type="button" className={styles.secondary} onClick={() => { setUncertain(false); setError(""); }}>I checked the original request; allow a new render</button> : null}
            <button type="button" className={styles.primary} disabled={!provider || !projectId.trim() || busy || result.busy || unfinished || uncertain} onClick={() => void submit()}><Play size={16} /> {busy ? "Submitting…" : "Start rendering"}</button>
            {orderId ? <div className={styles.result}><strong>Render request submitted</strong><p>{latest ? `${latest.status}: ${latest.message}` : "The node recorded the request. Waiting for provider progress."}</p><button type="button" className={styles.secondary} onClick={() => void refreshResult()}><RotateCcw size={14} /> Check progress</button></div> : null}
          </div>
        </section>
        <aside className={styles.panel}>
          <h2>Before you start</h2><p className={styles.panelLead}>The final cost will not exceed the amount shown here.</p>
          <div className={styles.summary}><div className={styles.summaryRow}><span>Availability</span><strong>{provider ? "Ready" : "Unavailable"}</strong></div><div className={styles.summaryRow}><span>{descriptor.pricing.label}</span><strong>{provider ? `${price} credits` : "—"}</strong></div><div className={styles.summaryRow}><span>Capacity</span><strong>{provider ? `${provider.capacity_units} slot${provider.capacity_units === 1 ? "" : "s"}` : "—"}</strong></div></div>
          {provider ? <details className={styles.details}><summary>Provider details</summary><div className={styles.detailBody}><span>{provider.provider_name || provider.node_name}</span><span>{provider.network_id}</span><span>{provider.peer_id}</span></div></details> : null}
        </aside>
      </div>
    </div>
  );
}
