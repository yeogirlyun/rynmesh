import { CloudCog, RefreshCw, SendHorizontal } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useAppContext } from "../appContext";
import { Button, Chip, Hash, LoadingPanel, PageHeader, Panel, PeerPill } from "../components/ui";
import type { LLMOrderResult, LLMProviderStatus, LLMServiceRecord, TaskBalanceSummary } from "../domain/nodeClient";
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
  const [llmNetwork, setLlmNetwork] = useState("rynmesh-llm-e2e");
  const [llmServices, setLlmServices] = useState<LLMServiceRecord[]>([]);
  const [selectedLlmPeerId, setSelectedLlmPeerId] = useState("");
  const [llmPrompt, setLlmPrompt] = useState("Explain in one sentence why this request travelled through Rynmesh.");
  const [llmMaxTokens, setLlmMaxTokens] = useState("64");
  const [llmResult, setLlmResult] = useState<LLMOrderResult | null>(null);
  const [llmBalance, setLlmBalance] = useState<TaskBalanceSummary | null>(null);
  const [llmProvider, setLlmProvider] = useState<LLMProviderStatus | null>(null);
  const [llmSubmitting, setLlmSubmitting] = useState(false);
  const [llmProgress, setLlmProgress] = useState<{ tone: "info" | "ok" | "danger"; text: string } | null>(null);
  const [llmPublishing, setLlmPublishing] = useState(false);
  const [loading, setLoading] = useState(true);

  const veoServices = useMemo(
    () => capacities.filter((item) => item.capabilities.includes(VEO_CAPABILITY)),
    [capacities],
  );
  const selectedVeo = veoServices.find((item) => item.peer_id === selectedPeerId) ?? veoServices[0];
  const selectedLlm = llmServices.find((item) => item.peer_id === selectedLlmPeerId) ?? llmServices[0];
  const peerById = new Map(peers.map((peer) => [peer.id, peer]));

  const refresh = async () => {
    setLoading(true);
    const [next, nextLlm, nextBalance, nextProvider] = await Promise.all([
      client.listJobCapacities({ capability: VEO_CAPABILITY }),
      client.listLLMServices(llmNetwork),
      client.getTaskBalance(),
      client.getLLMServiceStatus(),
    ]);
    setCapacities(next);
    setLlmServices(nextLlm);
    setLlmBalance(nextBalance);
    setLlmProvider(nextProvider);
    if (!selectedPeerId && next[0]) setSelectedPeerId(next[0].peer_id);
    if (!selectedLlmPeerId && nextLlm[0]) setSelectedLlmPeerId(nextLlm[0].peer_id);
    if (lastOrderId) setResults(await client.listWorkResults({ work_order_id: lastOrderId }));
    setLoading(false);
  };

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client]);

  const submit = async () => {
    const provider = selectedVeo?.peer_id ?? "";
    if (!provider || !videoId.trim()) return;
    const order = await client.submitWorkOrder({
      provider_peer_id: provider,
      capability: VEO_CAPABILITY,
      operation: VEO_OPERATION,
      max_credit_cost: Number(selectedVeo?.price_credits?.[VEO_CAPABILITY] ?? 20),
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

  const publishLlm = async () => {
    setLlmPublishing(true);
    try {
      await client.publishLLMService({ network_id: llmNetwork, benchmark: false });
      notify("ok", "Local LLM service published to the Rynmesh discovery network");
      await refresh();
    } catch (error) {
      notify("danger", error instanceof Error ? error.message : "LLM service publication failed");
    } finally {
      setLlmPublishing(false);
    }
  };

  const submitLlm = async () => {
    if (!selectedLlm || !llmPrompt.trim()) return;
    setLlmSubmitting(true);
    setLlmResult(null);
    setLlmProgress({
      tone: "info",
      text: "Order accepted locally. Strict public P2P hole punching is in progress; relay fallback is disabled.",
    });
    try {
      const result = await client.submitLLMOrder({
        network_id: llmNetwork,
        provider_peer_id: selectedLlm.peer_id,
        service_id: selectedLlm.service.package_id,
        prompt: llmPrompt.trim(),
        max_tokens: Math.max(1, Number(llmMaxTokens || 64)),
      });
      setLlmResult(result);
      setLlmBalance(await client.getTaskBalance());
      setLlmProgress({
        tone: result.state === "succeeded" ? "ok" : "danger",
        text: `Order ${result.state}${result.error_code ? `: ${result.error_code}` : ""}`,
      });
      notify(result.state === "succeeded" ? "ok" : "warn", `LLM task ${result.state}`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "LLM task failed";
      setLlmProgress({ tone: "danger", text: `Order failed: ${message}` });
      setLlmBalance(await client.getTaskBalance());
      notify("danger", message);
    } finally {
      setLlmSubmitting(false);
    }
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

      <Panel>
        <div className="panel-head">
          <div>
            <span className="eyebrow">Provider control</span>
            <h2>Local LLM service</h2>
          </div>
          <Chip tone={llmProvider?.online ? "ok" : llmProvider?.configured === false ? "info" : "danger"}>
            {llmProvider?.online ? "online" : llmProvider?.configured === false ? "not configured" : "offline"}
          </Chip>
        </div>
        {llmProvider?.service ? (
          <div className="form-stack">
            <div className="service-result">
              <span>{llmProvider.service.model_alias}</span>
              <small>
                {llmProvider.service.package_id} · context {llmProvider.service.context_window} ·
                {` ${llmProvider.capacity?.available ?? 0}/${llmProvider.capacity?.max_concurrent ?? 0} slots`}
              </small>
            </div>
            <div className="button-row">
              <Button
                variant="primary"
                icon={CloudCog}
                disabled={!llmProvider.online || llmPublishing}
                onClick={() => void publishLlm()}
              >
                {llmPublishing ? "Publishing…" : "Publish / refresh service"}
              </Button>
              <Chip tone="info">Publishes metadata only — never prompts or model files</Chip>
            </div>
          </div>
        ) : (
          <div className="empty-state">
            <h3>No local provider configured</h3>
            <p>This node can still discover and consume services from another Rynmesh node.</p>
          </div>
        )}
      </Panel>

      <Panel>
        <div className="panel-head">
          <div>
            <span className="eyebrow">Private local inference</span>
            <h2>LLM service order</h2>
          </div>
          <Chip tone={llmServices.length ? "ok" : "warn"}>
            {llmServices.length} available · {llmBalance?.available.toFixed(3) ?? "—"} DEV balance
          </Chip>
        </div>
        <div className="form-stack">
          <label className="field">
            <span>Discovery network</span>
            <input value={llmNetwork} onChange={(event) => setLlmNetwork(event.target.value)} />
          </label>
          <div className="button-row">
            <Button onClick={() => void refresh()} icon={RefreshCw}>Discover services</Button>
            <Chip tone="info">Ryn-to-Ryn encrypted path</Chip>
          </div>
          <label className="field">
            <span>Provider service</span>
            <select
              value={selectedLlmPeerId || selectedLlm?.peer_id || ""}
              onChange={(event) => setSelectedLlmPeerId(event.target.value)}
            >
              {llmServices.map((service) => (
                <option key={`${service.peer_id}-${service.service.package_id}`} value={service.peer_id}>
                  {service.service.model_alias} · {service.service.pricing.minimum} {service.service.pricing.currency}
                </option>
              ))}
            </select>
          </label>
          {selectedLlm ? (
            <div className="service-result">
              <Chip tone={selectedLlm.online ? "ok" : "danger"}>{selectedLlm.online ? "online" : "offline"}</Chip>
              <span>
                Context {selectedLlm.service.context_window} · max output {selectedLlm.service.max_output_tokens}
                {selectedLlm.capacity ? ` · ${selectedLlm.capacity.available ?? 0}/${selectedLlm.capacity.max_concurrent ?? 0} slots` : ""}
              </span>
              <small>{selectedLlm.service.privacy.policy_text || "Provider compute node sees plaintext."}</small>
            </div>
          ) : (
            <div className="empty-state">
              <h3>No LLM service discovered</h3>
              <p>Check the network name, then choose Discover services.</p>
            </div>
          )}
          <label className="field">
            <span>Prompt</span>
            <textarea rows={5} value={llmPrompt} onChange={(event) => setLlmPrompt(event.target.value)} />
          </label>
          <label className="field">
            <span>Maximum output tokens</span>
            <input value={llmMaxTokens} onChange={(event) => setLlmMaxTokens(event.target.value)} inputMode="numeric" />
          </label>
          <div className="button-row">
            <Button
              variant="primary"
              icon={SendHorizontal}
              disabled={!selectedLlm || !llmPrompt.trim() || llmSubmitting}
              onClick={() => void submitLlm()}
            >
              {llmSubmitting ? "Connecting P2P…" : "Place encrypted order"}
            </Button>
            {llmBalance ? <Chip mono>held {llmBalance.held.toFixed(3)}</Chip> : null}
          </div>
          {llmProgress ? (
            <div className="service-result" role="status" aria-live="polite">
              <Chip tone={llmProgress.tone}>{llmSubmitting ? "connecting" : llmProgress.tone === "ok" ? "complete" : "failed"}</Chip>
              <span>{llmProgress.text}</span>
            </div>
          ) : null}
        </div>
      </Panel>

      {llmResult ? (
        <Panel>
          <div className="panel-head">
            <div>
              <span className="eyebrow">Latest private LLM result</span>
              <h2>{llmResult.model_alias || "Local model"}</h2>
            </div>
            <Chip tone={llmResult.state === "succeeded" ? "ok" : "danger"}>{llmResult.state}</Chip>
          </div>
          <div className="form-stack">
            <div className="service-result"><span>{llmResult.output || llmResult.error_code || "No output"}</span></div>
            <div className="button-row">
              <Chip mono>{llmResult.task_id}</Chip>
              <Chip mono>{llmResult.input_tokens ?? 0} in / {llmResult.output_tokens ?? 0} out</Chip>
              <Chip mono>{llmResult.duration_ms ?? 0} ms</Chip>
              <Chip mono>{llmResult.amount ?? 0} DEV_TASK_BALANCE</Chip>
            </div>
          </div>
        </Panel>
      ) : null}

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
            <select value={selectedPeerId || selectedVeo?.peer_id || ""} onChange={(event) => setSelectedPeerId(event.target.value)}>
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
