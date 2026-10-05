import { tr, useUILanguage, uiLocale } from "../uiI18n";
import { Radar, ShieldAlert, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { useAppContext } from "../appContext";
import { Button, Chip, Hash, KV, LoadingPanel, PageHeader, Panel, PeerPill, TierBadge, WeightBar } from "../components/ui";
import type { IdentityTier, JobCapacity, Peer, PeerHealth } from "../domain/types";
import { joinPeerMeta, type PeerMeta } from "../domain/peerMeta";

const tierValues: Array<IdentityTier | "all"> = ["all", "unverified", "attested", "staked", "proven"];

export default function Peers() {
  useUILanguage();
  const { client, confirm, notify } = useAppContext();
  const [peers, setPeers] = useState<Peer[]>([]);
  const [capacities, setCapacities] = useState<JobCapacity[]>([]);
  const [health, setHealth] = useState<PeerHealth[]>([]);
  const [query, setQuery] = useState("");
  const [tier, setTier] = useState<IdentityTier | "all">("all");
  const [selected, setSelected] = useState<Peer | null>(null);
  const [loading, setLoading] = useState(true);

  const meta: Map<string, PeerMeta> = joinPeerMeta(peers.map((p) => p.id), health, capacities);

  const refresh = async () => {
    setLoading(true);
    const [result, caps] = await Promise.all([
      client.listPeers({ tier, search: query }),
      client.listJobCapacities(),
    ]);
    setPeers(result);
    setCapacities(caps);
    setLoading(false);
  };

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, tier]);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const h = await client.peersHealth();
        if (alive) setHealth(h);
      } catch {
        /* keep last-known health; never block the list */
      }
    };
    void tick();
    const id = window.setInterval(tick, 10000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [client]);

  if (loading) return <LoadingPanel />;

  return (
    <div className="screen-stack">
      <PageHeader
        eyebrow={tr("Peers")}
        title={tr("Connection details")}
        context={tr("Inspect device identities, connection status, and trust.")}
        actions={
          <Button
            icon={Radar}
            onClick={async () => {
              await client.discoverPeers();
              notify("ok", tr("Peer discovery requested through local node"));
            }}
          >
            {tr("Discover")}
          </Button>
        }
      />
      <Panel className="filter-panel">
        <div className="source-chips">
          <label className="field inline-field">
            <span>{tr("Search")}</span>
            <input value={query} onChange={(event) => setQuery(event.target.value)} onBlur={() => void refresh()} />
          </label>
          {tierValues.map((candidate) => (
            <button key={candidate} className={tier === candidate ? "filter-chip active" : "filter-chip"} type="button" onClick={() => setTier(candidate)}>
              {tr(candidate)}
            </button>
          ))}
        </div>
      </Panel>
      <Panel className="table-panel">
        <div className="table-wrap">
          <table className="peer-table">
            <thead>
              <tr>
                <th>{tr("Peer")}</th>
                <th>{tr("Tier")}</th>
                <th>{tr("Credits")}</th>
                <th>{tr("Weight")}</th>
                <th>{tr("Last seen")}</th>
                <th>{tr("Served/Fetched")}</th>
                <th>{tr("Trust status")}</th>
                <th>{tr("Actions")}</th>
              </tr>
            </thead>
            <tbody>
              {peers.map((peer) => (
                <tr key={peer.id}>
                  <td>
                    <span
                      className={`status-dot ${meta.get(peer.id)?.online === true ? "online" : meta.get(peer.id)?.online === false ? "offline" : "unknown"}`}
                      title={tr(meta.get(peer.id)?.online === true ? "online" : meta.get(peer.id)?.online === false ? "offline" : "Status unknown")}
                      aria-label={tr(meta.get(peer.id)?.online === true ? "online" : meta.get(peer.id)?.online === false ? "offline" : "Status unknown")}
                    />
                    <PeerPill peer={peer} />
                    <small className="mono">{peer.endpoint}</small>
                    {(meta.get(peer.id)?.capabilities.length ?? 0) > 0 && (
                      <div className="service-chip-row">
                        {meta.get(peer.id)!.capabilities.map((cap) => (
                          <Chip key={cap} tone="muted">{cap}</Chip>
                        ))}
                      </div>
                    )}
                  </td>
                  <td>
                    <TierBadge tier={peer.tier} />
                  </td>
                  <td className="mono number-cell">{peer.credits.toLocaleString(uiLocale())}</td>
                  <td>
                    <WeightBar value={peer.weight} />
                  </td>
                  <td>{peer.lastSeen}</td>
                  <td className="mono">
                    {peer.served}/{peer.fetched}
                  </td>
                  <td>
                    {peer.quarantined ? <Chip tone="danger">{tr("quarantined")}</Chip> : peer.trustedRoot ? <Chip tone="ok">{tr("trusted root")}</Chip> : <Chip tone="muted">{tr("local default")}</Chip>}
                  </td>
                  <td>
                    <Button onClick={() => setSelected(peer)}>{tr("Inspect")}</Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      {selected ? (
        <div className="drawer-backdrop" role="presentation">
          <aside className="trust-drawer" role="dialog" aria-modal="true" aria-labelledby="peer-drawer-title">
            <h2 id="peer-drawer-title">{selected.name}</h2>
            <KV
              rows={[
                { label: tr("Peer ID"), value: <Hash value={selected.id} /> },
                { label: tr("Endpoint"), value: <span className="mono">{selected.endpoint}</span> },
                {
                  label: tr("Status"),
                  value: (
                    <span>
                      <span className={`status-dot ${meta.get(selected.id)?.online === true ? "online" : meta.get(selected.id)?.online === false ? "offline" : "unknown"}`} />
                      {tr(meta.get(selected.id)?.online === true ? "online" : meta.get(selected.id)?.online === false ? "offline" : "Status unknown")}
                    </span>
                  ),
                },
                {
                  label: tr("Services"),
                  value: (meta.get(selected.id)?.capabilities.length ?? 0) > 0 ? (
                    <div className="service-chip-row">
                      {meta.get(selected.id)!.capabilities.map((cap) => (
                        <Chip key={cap} tone="muted">{cap}</Chip>
                      ))}
                    </div>
                  ) : (
                    <span className="muted">{tr("none")}</span>
                  ),
                },
                { label: tr("Tier"), value: <TierBadge tier={selected.tier} /> },
                { label: tr("Credits"), value: <span className="mono">{selected.credits.toLocaleString(uiLocale())}</span> },
                { label: tr("Weight"), value: <WeightBar value={selected.weight} /> },
              ]}
            />
            <div className="button-column">
              <Button
                variant="primary"
                icon={ShieldCheck}
                onClick={() =>
                  confirm({
                    title: tr("Trust this peer as a root?"),
                    body: tr("This changes local identity policy. The node will treat signed evidence from this peer as trusted root evidence."),
                    risk: "high",
                    confirmLabel: tr("Trust root"),
                    onConfirm: () => notify("ok", tr("Trust root change sent to local node")),
                  })
                }
              >
                {tr("Trust as root")}
              </Button>
              <Button>{tr("Downrank locally")}</Button>
              <Button
                variant="danger"
                icon={ShieldAlert}
                onClick={() =>
                  confirm({
                    title: tr("Quarantine this peer?"),
                    body: tr("The local node will downrank this peer and prepare a signed report if configured."),
                    risk: "high",
                    confirmLabel: tr("Quarantine"),
                    onConfirm: () => notify("warn", tr("Quarantine request sent to local node")),
                  })
                }
              >
                {tr("Quarantine")}
              </Button>
              <Button variant="ghost" onClick={() => setSelected(null)}>
                {tr("Close")}
              </Button>
            </div>
          </aside>
        </div>
      ) : null}
    </div>
  );
}
