import { CloudCog, RefreshCw, SendHorizontal } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useAppContext } from "../appContext";
import { Button, Chip, Hash, LoadingPanel, PageHeader, Panel, PeerPill } from "../components/ui";
import type { JobCapacity, WorkResult } from "../domain/types";

const VEO_CAPABILITY = "signal50.veo_motion.v1";
const VEO_OPERATION = "signal50.remote_action.complete_flow_video_veo_motion_clips";

export default function Services() {
  const { client, peers, notify } = useAppContext();
  const [capacities, setCapacities] = useState<JobCapacity[]>([]);
  const [selectedPeerId, setSelectedPeerId] = useState("");
  const [videoId, setVideoId] = useState("");
  const [maxScenes, setMaxScenes] = useState("0");
  const [skipExisting, setSkipExisting] = useState(true);
  const [lastOrderId, setLastOrderId] = useState("");
  const [results, setResults] = useState<WorkResult[]>([]);
  const [loading, setLoading] = useState(true);

  const veoServices = useMemo(
    () => capacities.filter((item) => item.capabilities.includes(VEO_CAPABILITY)),
    [capacities],
  );
  const selected = veoServices.find((item) => item.peer_id === selectedPeerId) ?? veoServices[0];
  const peerById = new Map(peers.map((peer) => [peer.id, peer]));

  const refresh = async () => {
    setLoading(true);
    const next = await client.listJobCapacities({ capability: VEO_CAPABILITY });
    setCapacities(next);
    if (!selectedPeerId && next[0]) setSelectedPeerId(next[0].peer_id);
    if (lastOrderId) setResults(await client.listWorkResults({ work_order_id: lastOrderId }));
    setLoading(false);
  };

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client]);

  const submit = async () => {
    const provider = selected?.peer_id ?? "";
    if (!provider || !videoId.trim()) return;
    const order = await client.submitWorkOrder({
      provider_peer_id: provider,
      capability: VEO_CAPABILITY,
      operation: VEO_OPERATION,
      max_credit_cost: Number(selected?.price_credits?.[VEO_CAPABILITY] ?? 20),
      params: {
        video_id: videoId.trim(),
        skip_existing: skipExisting,
        max_scenes: Number(maxScenes || 0),
      },
    });
    setLastOrderId(order.work_order_id);
    setResults([]);
    notify("ok", "Veo render request submitted through Rynmesh");
  };

  if (loading) return <LoadingPanel />;

  return (
    <div className="screen-stack">
      <PageHeader
        eyebrow="Services"
        title="Ryn job capacity"
        context="Discover provider nodes, submit signed work orders, and watch provider results through the relay mailbox."
        actions={<Button icon={RefreshCw} onClick={() => void refresh()}>Refresh</Button>}
      />

      <Panel className="table-panel">
        <div className="panel-head">
          <div>
            <span className="eyebrow">Published capacity</span>
            <h2>Signal50 Veo providers</h2>
          </div>
          <Chip tone={veoServices.length ? "ok" : "warn"}>{veoServices.length} available</Chip>
        </div>
        <div className="table-wrap">
          <table className="peer-table">
            <thead>
              <tr>
                <th>Provider</th>
                <th>Capability</th>
                <th>Price</th>
                <th>Route</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {veoServices.map((service) => (
                <tr key={service.peer_id}>
                  <td>
                    {peerById.get(service.peer_id) ? (
                      <PeerPill peer={peerById.get(service.peer_id)!} />
                    ) : (
                      <span>{service.provider_name || service.node_name}</span>
                    )}
                    <Hash value={service.peer_id} />
                  </td>
                  <td className="mono">{VEO_CAPABILITY}</td>
                  <td className="mono">{service.price_credits[VEO_CAPABILITY] ?? 0}</td>
                  <td>{String(service.metadata.route ?? service.metadata.service ?? "")}</td>
                  <td>{service.updated_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel>
        <div className="panel-head">
          <div>
            <span className="eyebrow">Request form</span>
            <h2>Signal50 Veo render</h2>
          </div>
          <Chip tone="info" icon={CloudCog}>polling relay</Chip>
        </div>
        <div className="form-stack">
          <label className="field">
            <span>Provider</span>
            <select value={selectedPeerId || selected?.peer_id || ""} onChange={(event) => setSelectedPeerId(event.target.value)}>
              {veoServices.map((service) => (
                <option key={service.peer_id} value={service.peer_id}>
                  {service.provider_name || service.node_name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Signal50 video ID</span>
            <input value={videoId} onChange={(event) => setVideoId(event.target.value)} placeholder="20260518__casefile__example-tt-deep-veo" />
          </label>
          <label className="field">
            <span>Max scenes</span>
            <input value={maxScenes} onChange={(event) => setMaxScenes(event.target.value)} inputMode="numeric" />
          </label>
          <label className="checkbox-row">
            <input type="checkbox" checked={skipExisting} onChange={(event) => setSkipExisting(event.target.checked)} />
            Skip existing clips
          </label>
          <div className="button-row">
            <Button variant="primary" icon={SendHorizontal} onClick={() => void submit()}>
              Submit request
            </Button>
            {lastOrderId ? <Chip mono>{lastOrderId}</Chip> : null}
          </div>
        </div>
      </Panel>

      {lastOrderId ? (
        <Panel>
          <div className="panel-head">
            <h2>Latest result</h2>
            <Button onClick={() => void refresh()}>Refresh result</Button>
          </div>
          {results.length ? (
            <div className="form-stack">
              {results.map((result) => (
                <div className="service-result" key={`${result.work_order_id}-${result.created_at}`}>
                  <Chip tone={result.status === "completed" ? "ok" : result.status === "failed" ? "danger" : "info"}>
                    {result.status}
                  </Chip>
                  <span>{result.message || "No message"}</span>
                  <small className="mono">{result.created_at}</small>
                </div>
              ))}
            </div>
          ) : (
            <div className="empty-state">
              <h3>No result yet</h3>
              <p>The provider will post accepted, running, completed, or failed messages as it polls.</p>
            </div>
          )}
        </Panel>
      ) : null}
    </div>
  );
}
