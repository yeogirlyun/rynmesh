import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { runThenReload } from "../domain/actThenReload";
import { deviceSyncApi } from "../domain/deviceSync";
import type { DevicePair, ReadingConflict } from "../domain/deviceSync";
import { FileTile } from "../personal/drop/FamilyParts";
import "../screens/device-sync-refresh.css";

function Choice({ issue, localActor, devices, busy, resolve }: { issue: ReadingConflict; localActor: string; devices: DevicePair[];
  busy: boolean; resolve: (issue: ReadingConflict, choice: string) => void }) {
  const [selected, setSelected] = useState("");
  return <article className="rf-conf-item">
    <div className="rf-conf-who"><FileTile kind={issue.scope === "reading" ? "doc" : "files"} size="sm" />
      <div><h3>{issue.item?.title || "Reading item"}</h3><p>{issue.item?.source_title || "Source unavailable"}</p></div></div>
    <fieldset disabled={busy} className="rf-conf-set">
      <legend className="rf-legend">{issue.scope === "reading" ? "Choose a reading position" : "Choose whether to keep this saved"}</legend>
      <div className="rf-conf-opts">{issue.candidates.map((candidate) => {
        const actor = candidate.choice_id.split(":")[0];
        const name = actor === localActor ? "This device" : devices.find((pair) => pair.device.actor === actor)?.device.name || "Another device";
        const value = candidate.value;
        const description = issue.scope === "bookmarks" ? value?.bookmarked ? "Saved" : "Not saved"
          : value === null ? "Remove synced reading position" : `${Math.round((value.progress ?? 0) * 100)}% read${value.completed ? " · completed" : ""}`;
        const pct = issue.scope === "reading" && value ? Math.max(0, Math.min(100, Math.round((value.progress ?? 0) * 100))) : null;
        return <label key={candidate.choice_id} className="rf-opt rf-conf-opt">
          <span className="rf-conf-r"><input type="radio" name={`choice-${issue.scope}-${issue.id}`} value={candidate.choice_id} checked={selected === candidate.choice_id}
            onChange={() => setSelected(candidate.choice_id)} /><b>{name}</b></span>
          <span className="rf-conf-what">{description}</span>
          {pct !== null ? <span className="rf-prog" aria-hidden="true"><i style={{ width: `${pct}%` }} /></span> : null}
          <span className="rf-conf-meta"><small className="rf-mono">{actor.slice(0, 12)}</small>
            {issue.scope === "reading" && value ? <small>Content version: {value.content_version || "unknown"}</small> : null}</span>
        </label>;
      })}</div>
    </fieldset>
    <div className="rf-conf-foot">
      <p>Choosing updates this device now. The choice can reach other paired devices when this category is allowed. Article text is not copied.</p>
      <button type="button" className="rf-btn rf-btn-p rf-btn-sm" disabled={busy || !selected} onClick={() => resolve(issue, selected)}>Use this choice</button>
    </div>
  </article>;
}

export default function ReadingSyncConflicts({ devices, onResolved }: { devices: DevicePair[]; onResolved: () => Promise<void> }) {
  const [issues, setIssues] = useState<ReadingConflict[]>([]);
  const [actor, setActor] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const active = useRef(true), generation = useRef(0);
  const load = useCallback(async () => {
    const version = ++generation.current;
    const result = await deviceSyncApi.readingConflicts();
    if (!active.current || version !== generation.current) return;
    setIssues(result.conflicts); setActor(result.local_actor); setLoaded(true);
  }, []);
  useEffect(() => {
    active.current = true;
    let running = false;
    const poll = async () => {
      if (running) return;
      running = true;
      try { await load(); }
      catch { if (active.current) setError("Reading changes could not be loaded. Refresh to retry."); }
      finally { running = false; }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 5000);
    return () => { active.current = false; window.clearInterval(timer); };
  }, [load]);
  const resolve = async (issue: ReadingConflict, choice: string) => {
    setBusy(true); setError(""); setNotice("");
    await runThenReload(
      async () => {
        await deviceSyncApi.resolveReading(issue, choice);
        if (active.current) setNotice("Choice saved on this device. Check the device status for sync confirmation.");
      },
      async () => { await load(); await onResolved(); },
      {
        onError: (cause) => { if (active.current) setError(cause instanceof Error ? cause.message : "This choice could not be confirmed. Refresh and review again."); },
        onNotice: (message) => { if (active.current) setNotice(message); },
      },
    );
    if (active.current) setBusy(false);
  };
  return <section className="rf-card rd-conf" aria-labelledby="reading-sync-conflicts">
    <div className="rd-conf-head">
      <div className="rd-conf-title"><h2 id="reading-sync-conflicts">Review reading changes{issues.length ? <span className="rf-count rf-t-warn">{issues.length}</span> : null}</h2>
        <p>Different devices may keep different positions or saved choices. Review the candidates; a larger percentage or a newer computer clock does not decide for you.</p></div>
      <button type="button" className="rf-btn rf-btn-sm" disabled={busy} onClick={() => { setError(""); void load().catch(() => setError("Reading changes could not be loaded. Refresh to retry.")); }}><RefreshCw size={14} strokeWidth={1.7} aria-hidden="true" />Refresh reading changes</button>
    </div>
    {error ? <p role="alert" className="rf-callout rf-callout-err rd-conf-msg">{error}</p> : null}{notice ? <p role="status" className="rf-callout rf-callout-ok rd-conf-msg">{notice}</p> : null}
    {!loaded ? <p className="rd-conf-empty">Loading reading changes…</p> : issues.length === 0 ? <p className="rd-conf-empty">No reading conflicts to resolve.</p> : issues.map((issue) =>
      <Choice key={`${issue.scope}:${issue.id}:${issue.revision}`} issue={issue} localActor={actor} devices={devices} busy={busy}
        resolve={(value, choice) => void resolve(value, choice)} />)}
  </section>;
}
