import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { StudioError, imageStudio, type StudioJob, type StudioStatus, type StudioUsage } from "../domain/imageStudio";
import { nodeControlUrl } from "../domain/nodeUrl";
import { peerProviders } from "../domain/peerProviders";
import "./nodeSites.css";

const POLL_MS = 5000;
type Channel = "publisher" | "not_publisher" | "not_connected" | "unknown";

/** Whether colleagues can reach pages here at all: the company decides who may host. */
async function companyChannel(): Promise<Channel> {
  try {
    const response = await fetch(nodeControlUrl("/peer-providers"), { credentials: "include", cache: "no-store" });
    if (!response.ok) return "unknown";
    const raw = await response.json() as { providers?: Array<Record<string, unknown>> };
    const connected = (raw.providers || []).filter(p => p?.status === "connected" && typeof p?.plugin_id === "string");
    if (!connected.length) return "not_connected";
    for (const row of connected) {
      try {
        if ((await peerProviders.workspace(row.plugin_id as string)).can_publish_models) return "publisher";
      } catch { /* try the next provider */ }
    }
    return "not_publisher";
  } catch {
    return "unknown";
  }
}

function seconds(value: number): string {
  const s = Math.max(0, Math.round(value));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** The image studio's controls within the shared app panel. */
export default function ImageStudioOwner({ audienceControl, sharingEnabled }: {
  audienceControl?: ReactNode;
  sharingEnabled: boolean;
}) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<StudioStatus | null>(null);
  const [queue, setQueue] = useState<StudioJob[]>([]);
  const [usage, setUsage] = useState<StudioUsage[]>([]);
  const [channel, setChannel] = useState<Channel>("unknown");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [, setTick] = useState(0);

  const load = useCallback(async () => {
    try {
      const [s, q, u] = await Promise.all([imageStudio.status(), imageStudio.queue(), imageStudio.usage()]);
      setStatus(s); setQueue(q); setUsage(u); setError("");
    } catch (err) {
      setError(err instanceof StudioError ? err.code : "image_unavailable");
    }
  }, []);

  useEffect(() => {
    void load();
    void companyChannel().then(setChannel);
    const timer = window.setInterval(() => { void load(); setTick(n => n + 1); }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const run = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key);
    try { await fn(); await load(); }
    catch (err) { setError(err instanceof StudioError ? err.code : "image_unavailable"); }
    finally { setBusy(""); }
  };

  const model = status?.model;
  const modelText = !model ? "" : model.ready ? t("sites.studio.modelReady")
    : t(`sites.studio.model.${model.code === "codex_missing" ? "codex_missing" : "codex_signed_out"}`);
  const now = Date.now() / 1000;
  const firstQueued = queue.findIndex(job => job.state === "queued");

  return <>
    {error && <p className="ns-error" role="alert">{t(`sites.studio.errors.${error}`, { defaultValue: t("sites.errors.site_unavailable") })}</p>}
    {sharingEnabled && channel === "not_publisher" && <p className="ns-error" role="status">{t("sites.studio.notPublisher")}</p>}
    {sharingEnabled && channel === "not_connected" && <p className="ns-error" role="status">{t("sites.studio.notConnected")}</p>}

    <div className="ns-stats">
      {audienceControl}
      <div className="ns-card ns-stat">
        <p className="ns-muted">{t("sites.studio.modelLabel")}</p>
        <strong>{t("sites.studio.modelName")}</strong>
        {model && <p className={model.ready ? "ns-ok" : "ns-error"} role="status">{modelText}</p>}
      </div>
      <div className="ns-card ns-stat">
        <p className="ns-muted">{t("sites.studio.today")}</p>
        <strong className="ns-big">{t("sites.studio.images", { count: status?.today ?? 0 })}</strong>
      </div>
      <div className="ns-card ns-stat">
        <p className="ns-muted">{t("sites.studio.queueing")}</p>
        <strong className="ns-big">{t("sites.studio.jobs", { count: status?.queue_length ?? 0 })}</strong>
      </div>
    </div>

    <div className="ns-card ns-mine">
      <div className="ns-mine-row">
        <h2 style={{ flex: 1, margin: 0 }}>{t("sites.studio.queue")}</h2>
        <p className="ns-muted">{t("sites.studio.queueHelp")}</p>
      </div>
      {queue.length === 0 ? <p className="ns-muted">{t("sites.studio.queueEmpty")}</p> : <div className="ns-table-wrap">
        <table className="ns-table">
          <thead><tr>
            <th>{t("sites.studio.col.order")}</th><th>{t("sites.studio.col.who")}</th><th>{t("sites.studio.col.brief")}</th>
            <th>{t("sites.studio.col.size")}</th><th>{t("sites.studio.col.state")}</th><th>{t("sites.studio.col.actions")}</th>
          </tr></thead>
          <tbody>{queue.map((job, index) => <tr key={job.id}>
            <td>{index + 1}</td>
            <td>{job.caller_name}</td>
            <td className="ns-brief" title={job.brief}>{job.brief}</td>
            <td className="ns-nowrap">{job.width} × {job.height}</td>
            <td className="ns-nowrap"><span className="ns-pill" style={job.state === "running"
              ? { color: "var(--accent-blue)", background: "var(--accent-blue-soft)" }
              : { color: "var(--accent-amber)", background: "var(--accent-amber-soft)" }}>
              {job.state === "running" ? t("sites.studio.running", { time: seconds(now - (job.started_at || now)) })
                : t("sites.studio.queued")}</span></td>
            <td><div className="ns-actions">
              {job.state === "queued" && index !== firstQueued && <button className="ns-link" disabled={Boolean(busy)}
                onClick={() => void run(`p/${job.id}`, () => imageStudio.prioritize(job.id))}>{t("sites.studio.prioritize")}</button>}
              <button className="ns-link is-danger" disabled={Boolean(busy)}
                onClick={() => void run(`c/${job.id}`, () => imageStudio.cancel(job.id))}>
                {job.state === "running" ? t("sites.studio.stop") : t("sites.studio.cancel")}</button>
            </div></td>
          </tr>)}</tbody>
        </table>
      </div>}
    </div>

    <div className="ns-card ns-mine">
      <h2 style={{ margin: 0 }}>{t("sites.studio.usage")}</h2>
      {usage.length === 0 ? <p className="ns-muted">{t("sites.studio.usageEmpty")}</p> : <div className="ns-table-wrap">
        <table className="ns-table">
          <thead><tr><th>{t("sites.studio.col.who")}</th><th>{t("sites.studio.col.today")}</th>
            <th>{t("sites.studio.col.month")}</th><th>{t("sites.studio.col.failed")}</th></tr></thead>
          <tbody>{usage.map(row => <tr key={row.caller}>
            <td>{row.name}</td><td>{t("sites.studio.images", { count: row.today })}</td>
            <td>{t("sites.studio.images", { count: row.last_30_days })}</td><td>{row.failed}</td>
          </tr>)}</tbody>
        </table>
      </div>}
      <p className="ns-muted">{t("sites.studio.usageNote")}</p>
    </div>
  </>;
}
