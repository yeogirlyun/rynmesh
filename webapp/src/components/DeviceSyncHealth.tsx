import type { DevicePair, DeviceStatus } from "../domain/deviceSync";
import { pairLabels } from "../domain/deviceSync";
import styles from "./DeviceSyncHealth.module.css";

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
  return <section aria-labelledby="sync-health-title" className={styles.health}>
    <h2 id="sync-health-title">Sync health</h2>
    <p>These are acknowledgements of changes sent from this computer. They do not prove another device is online or that all its changes have arrived here.</p>
    {readAt !== null ? <p>Last status update: {new Date(readAt).toLocaleString()}.</p> : null}
    {stale ? <p>Live status unavailable. Any values below are from the last update; refresh or reconnect before relying on them.</p> : null}
    {!status ? <p>Waiting for device status.</p> : <>
      {status.capture_failures.count > 0 || status.quarantined_count > 0 ? <p>On this computer: {status.capture_failures.count} changes not queued; {status.quarantined_count} records held outside the sync replica. Review the details below. These counts are not attributed to any one remote device.</p> : null}
      {!status.devices.length ? <p>Pair a computer to see its sync progress here.</p> : <div className={styles.scroll} tabIndex={0} role="region" aria-label="Per-device sync progress">
        <table><thead><tr><th scope="col">Device</th><th scope="col">State</th><th scope="col">Waiting for acknowledgement</th><th scope="col">Rejected by device</th><th scope="col">Last acknowledgement</th></tr></thead>
          <tbody>{status.devices.map((pair) => {
            const sync = pair.status === "active" && status.data_transfer_available ? pair.sync : undefined;
            const rejected = sync ? Object.values(sync.rejected_by_peer).reduce<number>((total, count) => total + (count ?? 0), 0) : null;
            return <tr key={pair.id}><th scope="row"><a href={`#device-${pair.id}`}>{pair.device.name}</a></th>
              <td>{stale ? "Status out of date" : summary(pair, status.data_transfer_available)}</td>
              <td>{sync?.pending ?? "Unknown"}</td>
              <td>{rejected === null ? "Unknown" : sync?.rejected_details_truncated ? `At least ${rejected}` : rejected}</td>
              <td>{sync?.last_success_at == null ? "Not recorded" : new Date(sync.last_success_at * 1000).toLocaleString()}</td></tr>;
          })}</tbody>
        </table>
      </div>}
    </>}
  </section>;
}
