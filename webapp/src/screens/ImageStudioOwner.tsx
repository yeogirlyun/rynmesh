import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { IMAGE_STUDIO_SITE_ID, StudioError, imageStudio, type StudioJob, type StudioStatus, type StudioUsage } from "../domain/imageStudio";
import { nodeSites, type SiteMember } from "../domain/nodeSites";
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

/** The owner's view of the image studio: open it, choose who, watch and manage the queue. */
export default function ImageStudioOwner() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [status, setStatus] = useState<StudioStatus | null>(null);
  const [queue, setQueue] = useState<StudioJob[]>([]);
  const [usage, setUsage] = useState<StudioUsage[]>([]);
  const [members, setMembers] = useState<SiteMember[]>([]);
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
    nodeSites.mine().then(value => setMembers(value.members)).catch(() => {});
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

  const setOpen = (enabled: boolean) => run("open", () => nodeSites.update(IMAGE_STUDIO_SITE_ID, { enabled }));
  const setAudience = (mode: "all" | "members", list: string[]) =>
    run("audience", () => nodeSites.update(IMAGE_STUDIO_SITE_ID, { audience: { mode, members: list } }));

  const model = status?.model;
  const modelText = !model ? "" : model.ready ? t("sites.studio.modelReady")
    : t(`sites.studio.model.${model.code === "codex_missing" ? "codex_missing" : "codex_signed_out"}`);
  const audience = status?.audience || { mode: "all" as const, members: [] };
  const now = Date.now() / 1000;
  const firstQueued = queue.findIndex(job => job.state === "queued");

  return <section className="ns-page">
    <header>
      <div>
        <p className="ns-muted">{t("sites.studio.eyebrow")}</p>
        <h1>{t("sites.studio.title")}</h1>
      </div>
      <button className="ns-button" onClick={() => navigate("/apps/sites")}>{t("sites.back")}</button>
    </header>
    {error && <p className="ns-error" role="alert">{t(`sites.studio.errors.${error}`, { defaultValue: t("sites.errors.site_unavailable") })}</p>}

    <div className="ns-card ns-mine">
      <div className="ns-mine-row">
        <div style={{ flex: "1 1 320px", minWidth: 0 }}>
          <strong>{t("sites.openToColleagues")}</strong>
          <p className="ns-muted">{t("sites.studio.openHelp")}</p>
        </div>
        <label>
          <input type="checkbox" checked={Boolean(status?.enabled)} disabled={!status || Boolean(busy)}
            onChange={event => void setOpen(event.target.checked)} />
          {status?.enabled ? t("sites.openState") : t("sites.closedState")}
        </label>
      </div>
      {status?.enabled && channel === "not_publisher" && <p className="ns-error" role="status">{t("sites.studio.notPublisher")}</p>}
      {status?.enabled && channel === "not_connected" && <p className="ns-error" role="status">{t("sites.studio.notConnected")}</p>}
      {model && !model.ready && <p className="ns-error" role="status">{modelText}</p>}
    </div>

    <div className="ns-stats">
      <div className="ns-card ns-stat">
        <p className="ns-muted">{t("sites.studio.modelLabel")}</p>
        <strong>{t("sites.studio.modelName")}</strong>
        <p className={model?.ready ? "ns-ok" : "ns-error"}>{modelText}</p>
      </div>
      <div className="ns-card ns-stat">
        <p className="ns-muted">{t("sites.studio.today")}</p>
        <strong className="ns-big">{t("sites.studio.images", { count: status?.today ?? 0 })}</strong>
      </div>
      <div className="ns-card ns-stat">
        <p className="ns-muted">{t("sites.studio.queueing")}</p>
        <strong className="ns-big">{t("sites.studio.jobs", { count: status?.queue_length ?? 0 })}</strong>
      </div>
      <div className="ns-card ns-stat" role="radiogroup" aria-labelledby="studio-who">
        <p className="ns-muted" id="studio-who">{t("sites.studio.who")}</p>
        <label><input type="radio" name="studio-audience" checked={audience.mode === "all"} disabled={!status || Boolean(busy)}
          onChange={() => void setAudience("all", [])} />{t("sites.audienceAll")}</label>
        <label><input type="radio" name="studio-audience" checked={audience.mode === "members"} disabled={!status || Boolean(busy)}
          onChange={() => void setAudience("members", audience.members)} />{t("sites.audienceMembers")}</label>
      </div>
    </div>
    {audience.mode === "members" && <fieldset className="ns-card ns-mine" disabled={Boolean(busy)}>
      <legend className="ns-muted">{t("sites.chooseMembers")}</legend>
      <div className="ns-members">
        {members.length === 0 && <p className="ns-muted">{t("sites.studio.noMembers")}</p>}
        {members.map(member => <label key={`${member.provider_resource}/${member.member_id}`}>
          <input type="checkbox" checked={audience.members.includes(member.member_id)} onChange={event => {
            const next = new Set(audience.members);
            if (event.target.checked) next.add(member.member_id); else next.delete(member.member_id);
            void setAudience("members", [...next]);
          }} />{member.member_name || member.member_id}
        </label>)}
      </div>
    </fieldset>}

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
            <td className="ns-nowrap"><span className={`ns-pill${job.state === "running" ? " is-online" : ""}`}>
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
  </section>;
}
