import { tr, useUILanguage } from "../uiI18n";
import { personalHref } from "../personal/model";
import { ArrowLeft, Film, Play, RotateCcw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAppContext } from "../appContext";
import type { JobCapacity, WorkResult } from "../domain/types";
import styles from "./ServiceExperience.module.css";

const CAPABILITY = "signal50.veo_motion.v1";
const OPERATION = "signal50.remote_action.complete_flow_video_veo_motion_clips";

export default function VideoRendering() {
  useUILanguage();
  const { client, notify } = useAppContext();
  const navigate = useNavigate();
  const [providers, setProviders] = useState<JobCapacity[]>([]);
  const [projectId, setProjectId] = useState("");
  const [maxScenes, setMaxScenes] = useState("0");
  const [skipExisting, setSkipExisting] = useState(true);
  const [busy, setBusy] = useState(false);
  const [orderId, setOrderId] = useState("");
  const [results, setResults] = useState<WorkResult[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void client.listJobCapacities({ capability: CAPABILITY })
      .then((items) => { if (active) setProviders(items.filter((item) => item.capabilities.includes(CAPABILITY))); })
      .catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : tr("Could not find a renderer.")); });
    return () => { active = false; };
  }, [client]);

  const provider = providers[0];
  const price = Number(provider?.price_credits[CAPABILITY] ?? 0);
  const refreshResult = async () => {
    if (!orderId) return;
    setResults(await client.listWorkResults({ work_order_id: orderId }));
  };

  const submit = async () => {
    if (!provider || !projectId.trim()) return;
    setBusy(true);
    setError("");
    try {
      const order = await client.submitWorkOrder({
        provider_peer_id: provider.peer_id,
        capability: CAPABILITY,
        operation: OPERATION,
        max_credit_cost: price,
        network_id: provider.network_id,
        params: { video_id: projectId.trim(), max_scenes: Number(maxScenes || 0), skip_existing: skipExisting },
      });
      setOrderId(order.work_order_id);
      setResults([]);
      notify("ok", tr("Video render request submitted"));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : tr("Could not submit the render request."));
    } finally {
      setBusy(false);
    }
  };

  const latest = useMemo(() => results[0], [results]);

  return (
    <div className={styles.page}>
      <button type="button" className={styles.back} onClick={() => navigate(personalHref("/services", client.mode === "fixture"))}><ArrowLeft size={15} /> {tr("All services")}</button>
      <header className={styles.hero}>
        <div className={styles.heroTitle}><span className={styles.heroIcon}><Film size={25} /></span><div><h1>{tr("Video rendering")}</h1><p>{tr("Create motion clips through an available rendering service.")}</p></div></div>
        <span className={styles.status}>{provider ? tr("Renderer ready") : tr("Finding renderer")}</span>
      </header>
      <div className={styles.layout}>
        <section className={styles.panel}>
          <h2>{tr("Create a render")}</h2><p className={styles.panelLead}>{tr("Choose the project to render. Ryn selects the provider and route for you.")}</p>
          <div className={styles.form}>
            <label className={styles.field}>{tr("Video project ID")}<input aria-label={tr("Video project ID")} value={projectId} onChange={(event) => setProjectId(event.target.value)} placeholder={tr("Enter a project ID")} /></label>
            <label className={styles.field}>{tr("Maximum scenes")}<input aria-label={tr("Maximum scenes")} type="number" min="0" value={maxScenes} onChange={(event) => setMaxScenes(event.target.value)} /></label>
            <label className={styles.check}><input type="checkbox" checked={skipExisting} onChange={(event) => setSkipExisting(event.target.checked)} /> {tr("Skip clips that are already rendered")}</label>
            {error ? <span role="alert" className={styles.error}>{error}</span> : null}
            <button type="button" className={styles.primary} disabled={!provider || !projectId.trim() || busy} onClick={() => void submit()}><Play size={16} /> {busy ? tr("Submitting…") : tr("Start rendering")}</button>
            {orderId ? <div className={styles.result}><strong>{tr("Render request submitted")}</strong><p>{latest ? `${tr(latest.status)}: ${latest.message}` : tr("Your provider has received the request. Check again for progress.")}</p><button type="button" className={styles.secondary} onClick={() => void refreshResult()}><RotateCcw size={14} /> {tr("Check progress")}</button></div> : null}
          </div>
        </section>
        <aside className={styles.panel}>
          <h2>{tr("Before you start")}</h2><p className={styles.panelLead}>{tr("The final cost will not exceed the amount shown here.")}</p>
          <div className={styles.summary}><div className={styles.summaryRow}><span>{tr("Availability")}</span><strong>{provider ? tr("Ready") : tr("Unavailable")}</strong></div><div className={styles.summaryRow}><span>{tr("Maximum cost")}</span><strong>{provider ? tr("{{count}} credits", { count: price }) : "—"}</strong></div><div className={styles.summaryRow}><span>{tr("Capacity")}</span><strong>{provider ? tr("{{count}} slots", { count: provider.capacity_units }) : "—"}</strong></div></div>
          {provider ? <details className={styles.details}><summary>{tr("Provider details")}</summary><div className={styles.detailBody}><span>{provider.provider_name || provider.node_name}</span><span>{provider.network_id}</span><span>{provider.peer_id}</span></div></details> : null}
        </aside>
      </div>
    </div>
  );
}
