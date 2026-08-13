import { Radar, ShieldAlert, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { useAppContext } from "../appContext";
import { Button, Chip, Hash, KV, LoadingPanel, PageHeader, Panel, PeerPill, TierBadge, WeightBar } from "../components/ui";
import type { IdentityTier, JobCapacity, Peer, PeerHealth } from "../domain/types";
import { joinPeerMeta, type PeerMeta } from "../domain/peerMeta";

const tierValues: Array<IdentityTier | "all"> = ["all", "unverified", "attested", "staked", "proven"];

export default function Peers() {
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
        eyebrow="Peers"
        title="Discovered Ryn nodes"
        context="Inspect identity tiers, credits, distribution weight, and local trust decisions."
        actions={
          <Button
            icon={Radar}
            onClick={async () => {
              await client.discoverPeers();
              notify("ok", "Peer discovery requested through local node");
            }}
          >
            Discover
          </Button>
        }
      />
      <Panel className="filter-panel">
        <div className="source-chips">
          <label className="field inline-field">
            <span>Search</span>
            <input value={query} onChange={(event) => setQuery(event.target.value)} onBlur={() => void refresh()} />
          </label>
          {tierValues.map((candidate) => (
            <button key={candidate} className={tier === candidate ? "filter-chip active" : "filter-chip"} type="button" onClick={() => setTier(candidate)}>
              {candidate}
            </button>
          ))}
        </div>
      </Panel>
      <Panel className="table-panel">
        <div className="table-wrap">
          <table className="peer-table">
            <thead>
              <tr>
                <th>Peer</th>
                <th>Tier</th>
                <th>Credits</th>
                <th>Weight</th>
                <th>Last seen</th>
                <th>Served/Fetched</th>
                <th>Trust status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {peers.map((peer) => (
                <tr key={peer.id}>
                  <td>
                    <span
                      className={`status-dot ${meta.get(peer.id)?.online ? "online" : "offline"}`}
                      title={meta.get(peer.id)?.online ? "online" : "offline"}
                      aria-label={meta.get(peer.id)?.online ? "online" : "offline"}
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
                  <td className="mono number-cell">{peer.credits.toLocaleString()}</td>
                  <td>
                    <WeightBar value={peer.weight} />
                  </td>
                  <td>{peer.lastSeen}</td>
                  <td className="mono">
                    {peer.served}/{peer.fetched}
                  </td>
                  <td>
                    {peer.quarantined ? <Chip tone="danger">quarantined</Chip> : peer.trustedRoot ? <Chip tone="ok">trusted root</Chip> : <Chip tone="muted">local default</Chip>}
                  </td>
                  <td>
                    <Button onClick={() => setSelected(peer)}>Inspect</Button>
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
                { label: "Peer ID", value: <Hash value={selected.id} /> },
                { label: "Endpoint", value: <span className="mono">{selected.endpoint}</span> },
                {
                  label: "Status",
                  value: (
                    <span>
                      <span className={`status-dot ${meta.get(selected.id)?.online ? "online" : "offline"}`} />
                      {meta.get(selected.id)?.online ? "online" : "offline"}
                    </span>
                  ),
                },
                {
                  label: "Services",
                  value: (meta.get(selected.id)?.capabilities.length ?? 0) > 0 ? (
                    <div className="service-chip-row">
                      {meta.get(selected.id)!.capabilities.map((cap) => (
                        <Chip key={cap} tone="muted">{cap}</Chip>
                      ))}
                    </div>
                  ) : (
                    <span className="muted">none</span>
                  ),
                },
                { label: "Tier", value: <TierBadge tier={selected.tier} /> },
                { label: "Credits", value: <span className="mono">{selected.credits.toLocaleString()}</span> },
                { label: "Weight", value: <WeightBar value={selected.weight} /> },
              ]}
            />
            <div className="button-column">
              <Button
                variant="primary"
                icon={ShieldCheck}
                onClick={() =>
                  confirm({
                    title: "Trust this peer as a root?",
                    body: "This changes local identity policy. The node will treat signed evidence from this peer as trusted root evidence.",
                    risk: "high",
                    confirmLabel: "Trust root",
                    onConfirm: () => notify("ok", "Trust root change sent to local node"),
                  })
                }
              >
                Trust as root
              </Button>
              <Button>Downrank locally</Button>
              <Button
                variant="danger"
                icon={ShieldAlert}
                onClick={() =>
                  confirm({
                    title: "Quarantine this peer?",
                    body: "The local node will downrank this peer and prepare a signed report if configured.",
                    risk: "high",
                    confirmLabel: "Quarantine",
                    onConfirm: () => notify("warn", "Quarantine request sent to local node"),
                  })
                }
              >
                Quarantine
              </Button>
              <Button variant="ghost" onClick={() => setSelected(null)}>
                Close
              </Button>
            </div>
          </aside>
        </div>
      ) : null}
    </div>
  );
}
