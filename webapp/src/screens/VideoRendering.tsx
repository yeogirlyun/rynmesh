import { tr, useUILanguage } from "../uiI18n";
import { personalHref } from "../personal/model";
import { ArrowLeft, ChevronDown, Play, RotateCcw } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAppContext } from "../appContext";
import type { JobCapacity, WorkOrder, WorkResult } from "../domain/types";
import { useProviderDiscovery, useServiceOrder } from "../domain/serviceExperience";
import { providerIdentity, serviceDescriptors } from "../domain/serviceDescriptors";
import { NodeClientError } from "../domain/nodeClient";
import { GlyphTile, VideoGlyph } from "./ServiceParts";
import "./service-pages-refresh.css";

const descriptor = serviceDescriptors.video;
const CAPABILITY = descriptor.capability;
const OPERATION = descriptor.operation;

export default function VideoRendering() {
  useUILanguage();
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
    <div className="rf rd-svc rd-video sv-narrow">
      <button type="button" className="rf-lnk sv-back" onClick={() => navigate(personalHref("/services", client.mode === "fixture"))}><ArrowLeft size={14} aria-hidden="true" /> {tr("All services")}</button>
      <header className="sv-head-ident rf-h-rose">
        <GlyphTile hue="rose" size="xl">{(px) => <VideoGlyph size={px} />}</GlyphTile>
        <div className="sv-head-text"><h1 className="sv-title">{tr("Video rendering")}</h1><p className="sv-lead">{tr("Create motion clips through an available rendering service.")}</p></div>
        <span className={`rf-pill rf-t-${provider ? "ok" : "warn"}`}><i aria-hidden="true" />{provider ? tr("Renderer ready") : tr("Finding renderer")}</span>
      </header>
      <div className="sv-cols">
        <section className="sv-card sv-main" aria-labelledby="sv-create-title">
          <div><h2 id="sv-create-title" className="sv-h2 sv-h2-lg">{tr("Create a render")}</h2><p className="sv-sub">{tr("Choose the project to render. Ryn selects the provider and route for you.")}</p></div>
          <div className="sv-form">
            <div className="sv-grid2 sv-video-fields">
              <label className="sv-field"><span>Video project ID</span><input className="sv-inp sv-mono" aria-label="Video project ID" value={projectId} onChange={(event) => setProjectId(event.target.value)} placeholder="Enter a project ID" /></label>
              <label className="sv-field"><span>Maximum scenes</span><input className="sv-inp" aria-label="Maximum scenes" type="number" min="0" value={maxScenes} onChange={(event) => setMaxScenes(event.target.value)} /><small>{tr("0 renders all scenes")}</small></label>
            </div>
            <label className="sv-check"><input type="checkbox" checked={skipExisting} onChange={(event) => setSkipExisting(event.target.checked)} /> Skip clips that are already rendered</label>
            {visibleError ? <span role="alert" className="sv-alert sv-alert-bad">{visibleError}</span> : null}
            {uncertain ? <button type="button" className="rf-btn" style={{ alignSelf: "flex-start" }} onClick={() => { setUncertain(false); setError(""); }}>I checked the original request; allow a new render</button> : null}
            <button type="button" className="rf-btn rf-btn-p rf-btn-lg" style={{ alignSelf: "flex-start" }} disabled={!provider || !projectId.trim() || busy || result.busy || unfinished || uncertain} onClick={() => void submit()}><Play size={14} fill="currentColor" aria-hidden="true" /> {busy ? "Submitting…" : "Start rendering"}</button>
            {orderId ? <div className="sv-progress rf-t-sky"><div><b>Render request submitted</b><p>{latest ? `${latest.status}: ${latest.message}` : "The node recorded the request. Waiting for provider progress."}</p></div><button type="button" className="rf-btn rf-btn-sm" onClick={() => void refreshResult()}><RotateCcw size={13} aria-hidden="true" /> Check progress</button></div> : null}
          </div>
        </section>
        <aside className="sv-card sv-aside" aria-labelledby="sv-before-title">
          <h2 id="sv-before-title" className="sv-h2">Before you start</h2><p className="sv-note">The final cost will not exceed the amount shown here.</p>
          <dl className="sv-rows"><div><dt>Availability</dt><dd>{provider ? "Ready" : "Unavailable"}</dd></div><div><dt>{descriptor.pricing.label}</dt><dd>{provider ? `${price} credits` : "—"}</dd></div><div><dt>Capacity</dt><dd>{provider ? `${provider.capacity_units} slot${provider.capacity_units === 1 ? "" : "s"}` : "—"}</dd></div></dl>
          {provider ? <details className="sv-details"><summary><span>Provider details</span><ChevronDown size={14} aria-hidden="true" /></summary><div className="sv-details-body"><span>{provider.provider_name || provider.node_name}</span><span>{provider.network_id}</span><span>{provider.peer_id}</span></div></details> : null}
        </aside>
      </div>
    </div>
  );
}
