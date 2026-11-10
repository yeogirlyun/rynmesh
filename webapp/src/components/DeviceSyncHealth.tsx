import { useState } from "react";
import { ChevronDown, Clock, Info } from "lucide-react";
import type { DevicePair, DeviceStatus } from "../domain/deviceSync";
import { pairLabels } from "../domain/deviceSync";
import { tr, uiLocale, useUILanguage } from "../uiI18n";
import { deviceHue, initialsOf, relativeAgo } from "../personal/drop/FamilyParts";
import "../screens/device-sync-refresh.css";

function summary(pair: DevicePair, available: boolean): string {
  if (pair.status !== "active") return pairLabels[pair.status] ?? "Pairing status unavailable";
  if (!available) return "Content transfer unavailable";
  if (pair.paused || pair.remote_paused) return "Paused";
  if (!pair.effective_scopes.length) return "No shared categories";
  const sync = pair.sync;
  if (!sync) return "Sync status unavailable";
  if (sync.rejected_details_truncated) return "Rejected records · incomplete counts";
  if (Object.values(sync.rejected_by_peer).some((count) => (count ?? 0) > 0)) return "Rejected records need attention";
  if (sync.conflicts > 0) return "Conflicts need review";
  if (sync.state === "confirmed" && sync.pending === 0) return "Selected local changes acknowledged";
  return ({ pending: "Awaiting acknowledgement", waiting: "Transfer not confirmed · retry connection",
    failed: "Local sync storage unavailable", conflict: "Conflicts need review", paused: "Paused",
    no_scope: "No shared categories" } as Record<string, string>)[sync.state] ?? "Sync status unavailable";
}

export default function DeviceSyncHealth({ status, stale, readAt }: {
  status: DeviceStatus | null; stale: boolean; readAt: number | null;
}) {
  useUILanguage();
  const [open, setOpen] = useState(false);
  const devices = status?.devices ?? [];
  const live = status?.data_transfer_available ? devices.filter((pair) => pair.status === "active" && pair.sync) : [];
  // Only values the node reported: unknown stays unknown instead of becoming zero.
  const pending = live.some((pair) => pair.sync!.pending !== null) ? live.reduce((total, pair) => total + (pair.sync!.pending ?? 0), 0) : null;
  const conflicts = live.reduce((total, pair) => total + pair.sync!.conflicts, 0);
  const last = live.reduce<{ at: number; name: string } | null>((best, pair) => {
    const at = pair.sync!.last_success_at;
    return at != null && (!best || at > best.at) ? { at, name: pair.device.name } : best;
  }, null);
  const paired = devices.filter((pair) => pair.status === "active").length;
  const review = devices.filter((pair) => pair.status === "awaiting_owner").length;
  const ago = last ? relativeAgo(last.at * 1000, Date.now(), uiLocale()) : null;
  return <section aria-labelledby="sync-health-title" className="rd-health">
    <h2 id="sync-health-title" className="rf-sr">Sync health</h2>
    {stale ? <div className="rf-callout rf-callout-warn"><Clock size={16} strokeWidth={1.7} aria-hidden="true" /><p>Live status unavailable. Any values below are from the last update; refresh or reconnect before relying on them.</p></div> : null}
    {!status ? <p className="rf-muted">Waiting for device status.</p> : !devices.length ? null : <>
      <div className="rf-band" data-stale={stale ? "true" : "false"}>
        <div className="rf-band-left">
          <div>
            <div className="rf-band-num">{paired}</div>
            <div className="rf-band-cap">{tr("paired computers")}{review > 0 ? ` · ${tr("{{count}} needs your review", { count: review })}` : ""}</div>
          </div>
          <div className="rf-avs" aria-hidden="true">{devices.slice(0, 4).map((pair) => <span key={pair.id} className={`rf-h-${deviceHue(pair.device.peer_id || pair.id)}`}>{initialsOf(pair.device.name)}</span>)}</div>
        </div>
        <div className="rf-band-stats">
          <div className="rf-stat"><div className="rf-stat-label">{tr("Waiting for acknowledgement")}</div>
            <div className="rf-stat-val"><b>{pending ?? "–"}</b><span>{tr("changes")}</span></div></div>
          <a className={`rf-stat rf-stat-warn${conflicts > 0 ? " on" : ""}`} href="#reading-sync-conflicts"><div className="rf-stat-label">{tr("Conflicts to review")}</div>
            <div className="rf-stat-val"><b>{live.length ? conflicts : "–"}</b>{conflicts > 0 ? <span className="rf-lnk">{tr("Review below")}</span> : null}</div></a>
          <div className="rf-stat"><div className="rf-stat-label">{tr("Last acknowledgement")}</div>
            <div className="rf-stat-val"><b>{ago ?? "–"}</b>{last ? <span>· {last.name}</span> : null}</div></div>
        </div>
        <p className="rf-band-note"><Info size={14} strokeWidth={1.7} aria-hidden="true" /><span>These are acknowledgements of changes sent from this computer. They do not prove another device is online or that all its changes have arrived here.{readAt !== null ? ` ${tr("Last status update:")} ${new Date(readAt).toLocaleString()}.` : ""}</span></p>
      </div>
      {status.capture_failures.count > 0 || status.quarantined_count > 0 ? <p className="rf-note">On this computer: {status.capture_failures.count} changes not queued; {status.quarantined_count} records held outside the sync replica. Review the details below. These counts are not attributed to any one remote device.</p> : null}
      <div className="rf-health-more">
        <button type="button" className="rf-lnk" aria-expanded={open} onClick={() => setOpen((value) => !value)}>{tr("Per-device progress")}<ChevronDown size={14} strokeWidth={1.8} aria-hidden="true" /></button>
        <div className="rf-health-scroll" data-open={open ? "true" : "false"} tabIndex={0} role="region" aria-label="Per-device sync progress">
          <table><thead><tr><th scope="col">Device</th><th scope="col">State</th><th scope="col">Waiting for acknowledgement</th><th scope="col">Rejected by device</th><th scope="col">Last acknowledgement</th></tr></thead>
            <tbody>{devices.map((pair) => {
              const sync = pair.status === "active" && status.data_transfer_available ? pair.sync : undefined;
              const rejected = sync ? Object.values(sync.rejected_by_peer).reduce<number>((total, count) => total + (count ?? 0), 0) : null;
              return <tr key={pair.id}><th scope="row"><a href={`#device-${pair.id}`}>{pair.device.name}</a></th>
                <td>{stale ? "Status out of date" : summary(pair, status.data_transfer_available)}</td>
                <td>{sync?.pending ?? "Unknown"}</td>
                <td>{rejected === null ? "Unknown" : sync?.rejected_details_truncated ? `At least ${rejected}` : rejected}</td>
                <td>{sync?.last_success_at == null ? "Not recorded" : new Date(sync.last_success_at * 1000).toLocaleString()}</td></tr>;
            })}</tbody>
          </table>
        </div>
      </div>
    </>}
  </section>;
}
