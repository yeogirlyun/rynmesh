import { CloudCog, RefreshCw, SendHorizontal } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useAppContext } from "../appContext";
import { Button, Chip, Hash, LoadingPanel, PageHeader, Panel, PeerPill } from "../components/ui";
import type {
  LLMOrderResult,
  LLMPrivacySettings,
  LLMProviderStatus,
  LLMServiceRecord,
  LLMSetupRequest,
  TaskBalanceSummary,
} from "../domain/nodeClient";
import type { JobCapacity, WorkResult } from "../domain/types";

const VEO_CAPABILITY = "signal50.veo_motion.v1";
const VEO_OPERATION = "signal50.remote_action.complete_flow_video_veo_motion_clips";
const LLM_TERMINAL_STATES = new Set(["succeeded", "failed", "timed_out", "cancelled", "rejected"]);

export default function Services() {
  const { client, peers, notify, confirm } = useAppContext();
  const [capacities, setCapacities] = useState<JobCapacity[]>([]);
  const [selectedPeerId, setSelectedPeerId] = useState("");
  const [videoId, setVideoId] = useState("");
  const [maxScenes, setMaxScenes] = useState("0");
  const [skipExisting, setSkipExisting] = useState(true);
  const [lastOrderId, setLastOrderId] = useState("");
  const [results, setResults] = useState<WorkResult[]>([]);
  const [llmNetwork, setLlmNetwork] = useState("rynmesh-main");
  const [llmServices, setLlmServices] = useState<LLMServiceRecord[]>([]);
  const [selectedLlmPeerId, setSelectedLlmPeerId] = useState("");
  const [llmPrompt, setLlmPrompt] = useState("Explain in one sentence why this request travelled through Rynmesh.");
  const [llmMaxTokens, setLlmMaxTokens] = useState("64");
  const [llmTransport, setLlmTransport] = useState<"auto" | "direct" | "p2p" | "relay">("auto");
  const [llmResult, setLlmResult] = useState<LLMOrderResult | null>(null);
  const [llmBalance, setLlmBalance] = useState<TaskBalanceSummary | null>(null);
  const [llmProvider, setLlmProvider] = useState<LLMProviderStatus | null>(null);
  const [llmSubmitting, setLlmSubmitting] = useState(false);
  const [llmProgress, setLlmProgress] = useState<{ tone: "info" | "ok" | "danger"; text: string } | null>(null);
  const [llmPublishing, setLlmPublishing] = useState(false);
  const [llmActiveTaskId, setLlmActiveTaskId] = useState("");
  const [llmCancelling, setLlmCancelling] = useState(false);
  const [llmOrders, setLlmOrders] = useState<LLMOrderResult[]>([]);
  const [llmPrivacy, setLlmPrivacy] = useState<LLMPrivacySettings | null>(null);
  const [llmConfiguring, setLlmConfiguring] = useState(false);
  const [llmSetupMode, setLlmSetupMode] = useState<LLMSetupRequest["mode"]>("openai-compatible");
  const [llmPackageId, setLlmPackageId] = useState("local-small");
  const [llmAlias, setLlmAlias] = useState("rynmesh-local");
  const [llmBaseUrl, setLlmBaseUrl] = useState("http://127.0.0.1:8080");
  const [llmModel, setLlmModel] = useState("");
  const [llmModelPath, setLlmModelPath] = useState("");
  const [llmSetupConfirmed, setLlmSetupConfirmed] = useState(false);
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
    try {
      const [capacityResult, serviceResult, balanceResult, providerResult, ordersResult, privacyResult] = await Promise.allSettled([
        client.listJobCapacities({ capability: VEO_CAPABILITY }),
        client.listLLMServices(llmNetwork),
        client.getTaskBalance(),
        client.getLLMServiceStatus(),
        client.listLLMOrders(),
        client.getLLMPrivacy(),
      ]);
      if (capacityResult.status === "fulfilled") {
        setCapacities(capacityResult.value);
        if (!selectedPeerId && capacityResult.value[0]) setSelectedPeerId(capacityResult.value[0].peer_id);
      }
      if (serviceResult.status === "fulfilled") {
        setLlmServices(serviceResult.value);
        if (!selectedLlmPeerId && serviceResult.value[0]) setSelectedLlmPeerId(serviceResult.value[0].peer_id);
      } else {
        setLlmProgress({ tone: "danger", text: `Service discovery failed: ${serviceResult.reason instanceof Error ? serviceResult.reason.message : "unknown error"}` });
      }
      if (balanceResult.status === "fulfilled") setLlmBalance(balanceResult.value);
      if (providerResult.status === "fulfilled") setLlmProvider(providerResult.value);
      if (ordersResult.status === "fulfilled") setLlmOrders(ordersResult.value);
      if (privacyResult.status === "fulfilled") setLlmPrivacy(privacyResult.value);
      if (lastOrderId) setResults(await client.listWorkResults({ work_order_id: lastOrderId }));
    } finally {
      setLoading(false);
    }
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

  const pauseLlm = async () => {
    setLlmPublishing(true);
    try {
      setLlmProvider(await client.pauseLLMService());
      notify("ok", "Local LLM service paused; new orders will be rejected");
    } catch (error) {
      notify("danger", error instanceof Error ? error.message : "LLM service pause failed");
    } finally {
      setLlmPublishing(false);
    }
  };

  const setupLlm = async () => {
    setLlmConfiguring(true);
    try {
      await client.setupLLMService({
        mode: llmSetupMode,
        package_id: llmPackageId.trim(),
        alias: llmAlias.trim(),
        base_url: llmBaseUrl.trim(),
        model: llmModel.trim(),
        model_path: llmModelPath.trim(),
        accept_risk: llmSetupConfirmed,
      });
      notify("ok", "Local model configured and self-tested; publishing remains off until you enable it");
      await refresh();
    } catch (error) {
      notify("danger", error instanceof Error ? error.message : "Local model setup failed");
    } finally {
      setLlmConfiguring(false);
    }
  };

  const submitLlm = async () => {
    if (!selectedLlm || !llmPrompt.trim()) return;
    setLlmSubmitting(true);
    setLlmResult(null);
    setLlmProgress({
      tone: "info",
      text: llmTransport === "p2p"
        ? "Order accepted locally. Strict P2P connection is in progress; relay fallback is disabled."
        : llmTransport === "relay"
          ? "Order accepted locally. End-to-end ciphertext relay delivery is in progress."
          : llmTransport === "direct"
            ? "Order accepted locally. A direct Provider node connection is in progress."
            : "Order accepted locally. The node is selecting an available encrypted transport.",
    });
    try {
      let result = await client.submitLLMOrder({
        network_id: llmNetwork,
        provider_peer_id: selectedLlm.peer_id,
        service_id: selectedLlm.service.package_id,
        prompt: llmPrompt.trim(),
        max_tokens: Math.max(1, Number(llmMaxTokens || 64)),
        transport: llmTransport,
      });
      setLlmActiveTaskId(result.task_id);
      setLlmPrompt("");
      while (!LLM_TERMINAL_STATES.has(result.state)) {
        setLlmProgress({
          tone: "info",
          text: `Order ${result.task_id} is ${result.state}; waiting for the Provider node…`,
        });
        await new Promise((resolve) => window.setTimeout(resolve, 500));
        result = await client.getLLMOrder(result.task_id);
      }
      setLlmResult(result);
      setLlmBalance(await client.getTaskBalance());
      setLlmOrders(await client.listLLMOrders());
      setLlmProgress({
        tone: result.state === "succeeded" ? "ok" : "danger",
        text: `Order ${result.state}${result.transport ? ` via ${result.transport}` : ""}${result.error_code ? `: ${result.error_code}` : ""}`,
      });
      notify(result.state === "succeeded" ? "ok" : "warn", `LLM task ${result.state}`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "LLM task failed";
      setLlmProgress({ tone: "danger", text: `Order failed: ${message}` });
      setLlmBalance(await client.getTaskBalance());
      notify("danger", message);
    } finally {
      setLlmSubmitting(false);
      setLlmActiveTaskId("");
    }
  };

  const cancelLlm = async () => {
    if (!llmActiveTaskId) return;
    setLlmCancelling(true);
    try {
      const result = await client.cancelLLMOrder(llmActiveTaskId);
      setLlmResult(result);
      setLlmProgress({ tone: "info", text: `Order ${result.task_id} cancellation requested` });
      notify("ok", "Cancellation sent to the Provider node; the reserved balance was released");
    } catch (error) {
      notify("danger", error instanceof Error ? error.message : "LLM task cancellation failed");
    } finally {
      setLlmCancelling(false);
    }
  };

  const updateLlmRetention = async (value: LLMPrivacySettings["result_retention_seconds"]) => {
    try {
      setLlmPrivacy(await client.updateLLMPrivacy(value));
      notify("ok", value ? "Encrypted result retention updated" : "Stored encrypted results purged");
    } catch (error) {
      notify("danger", error instanceof Error ? error.message : "Result retention update failed");
    }
  };

  const clearLlmHistory = () => {
    confirm({
      title: "Clear completed LLM task history?",
      body: "This permanently removes local task metadata and any retained encrypted results. Running tasks are preserved.",
      risk: "high",
      confirmLabel: "Clear task history",
      onConfirm: async () => {
        const result = await client.clearLLMOrders();
        setLlmOrders(await client.listLLMOrders());
        notify("ok", `${result.removed} local LLM task records removed`);
      },
    });
  };

  const viewLlmOrder = async (taskId: string) => {
    try {
      setLlmResult(await client.getLLMOrder(taskId));
    } catch (error) {
      notify("danger", error instanceof Error ? error.message : "Task result is unavailable");
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
                disabled={llmPublishing}
                onClick={() => void publishLlm()}
              >
                {llmPublishing ? "Publishing…" : "Publish / refresh service"}
              </Button>
              {llmProvider.publication_enabled ? (
                <Button disabled={llmPublishing} onClick={() => void pauseLlm()}>
                  {llmPublishing ? "Pausing…" : "Pause new orders"}
                </Button>
              ) : null}
              <Chip tone="info">Publishes metadata only — never prompts or model files</Chip>
            </div>
          </div>
        ) : (
          <div className="empty-state">
            <h3>No local provider configured</h3>
            <p>This node can still discover and consume services from another Rynmesh node.</p>
          </div>
        )}
        <div className="form-stack">
          <div className="panel-head">
            <div>
              <span className="eyebrow">Model setup</span>
              <h3>{llmProvider?.configured ? "Change local model connection" : "Add a local model"}</h3>
            </div>
            <Chip tone="info">Publishing stays off after setup</Chip>
          </div>
          <label className="field">
            <span>Setup mode</span>
            <select value={llmSetupMode} onChange={(event) => setLlmSetupMode(event.target.value as LLMSetupRequest["mode"])}>
              <option value="openai-compatible">OpenAI-compatible local API</option>
              <option value="ollama">Ollama</option>
              <option value="import-gguf">Import a GGUF file read-only</option>
              <option value="managed">Automatically install a recommended model</option>
            </select>
          </label>
          <label className="field">
            <span>Package ID</span>
            <input value={llmPackageId} onChange={(event) => setLlmPackageId(event.target.value)} />
          </label>
          <label className="field">
            <span>Public model alias</span>
            <input value={llmAlias} onChange={(event) => setLlmAlias(event.target.value)} />
          </label>
          {llmSetupMode === "openai-compatible" || llmSetupMode === "ollama" ? (
            <>
              <label className="field">
                <span>Local API URL</span>
                <input value={llmBaseUrl} onChange={(event) => setLlmBaseUrl(event.target.value)} />
              </label>
              <label className="field">
                <span>Model name (optional)</span>
                <input value={llmModel} onChange={(event) => setLlmModel(event.target.value)} />
              </label>
            </>
          ) : null}
          {llmSetupMode === "import-gguf" ? (
            <label className="field">
              <span>GGUF file path</span>
              <input value={llmModelPath} onChange={(event) => setLlmModelPath(event.target.value)} />
            </label>
          ) : null}
          {llmSetupMode === "managed" || llmSetupMode === "import-gguf" ? (
            <label className="checkbox-row">
              <input type="checkbox" checked={llmSetupConfirmed} onChange={(event) => setLlmSetupConfirmed(event.target.checked)} />
              I understand this prepares a local runtime and may download software or model data.
            </label>
          ) : null}
          <div className="button-row">
            <Button
              variant="primary"
              disabled={llmConfiguring || !llmPackageId.trim() || !llmAlias.trim()
                || (llmSetupMode === "import-gguf" && !llmModelPath.trim())
                || ((llmSetupMode === "managed" || llmSetupMode === "import-gguf") && !llmSetupConfirmed)}
              onClick={() => void setupLlm()}
            >
              {llmConfiguring ? "Configuring and self-testing…" : "Configure and run self-test"}
            </Button>
            <Chip tone="info">The compute node sees plaintext during inference</Chip>
          </div>
        </div>
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
          <label className="field">
            <span>Transport policy</span>
            <select value={llmTransport} onChange={(event) => setLlmTransport(event.target.value as typeof llmTransport)}>
              <option value="auto">Automatic — direct first, encrypted relay if configured</option>
              <option value="direct">Direct Provider HTTP only</option>
              <option value="p2p">Strict ICE/UDP P2P — never relay</option>
              <option value="relay">End-to-end ciphertext relay</option>
            </select>
          </label>
          <div className="service-result">
            <small>The Provider necessarily sees plaintext during inference. Registry coordination never receives the prompt or response body.</small>
          </div>
          <div className="button-row">
            <Button
              variant="primary"
              icon={SendHorizontal}
              disabled={!selectedLlm || !llmPrompt.trim() || llmSubmitting}
              onClick={() => void submitLlm()}
            >
              {llmSubmitting ? "Task running…" : "Place encrypted order"}
            </Button>
            {llmActiveTaskId ? (
              <Button disabled={llmCancelling} onClick={() => void cancelLlm()}>
                {llmCancelling ? "Cancelling…" : "Cancel task"}
              </Button>
            ) : null}
            {llmBalance ? <Chip mono>held {llmBalance.held.toFixed(3)}</Chip> : null}
          </div>
          {llmProgress ? (
            <div className="service-result" role="status" aria-live="polite">
              <Chip tone={llmProgress.tone}>{llmSubmitting ? "running" : llmProgress.tone === "ok" ? "complete" : "failed"}</Chip>
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
              {llmResult.transport ? <Chip mono>{llmResult.transport}</Chip> : null}
            </div>
          </div>
        </Panel>
      ) : null}

      <Panel>
        <div className="panel-head">
          <div>
            <span className="eyebrow">Local task history & privacy</span>
            <h2>Private LLM orders</h2>
          </div>
          <Chip tone="info">Prompt text is never written to task history</Chip>
        </div>
        <div className="form-stack">
          <label className="field">
            <span>Retain encrypted result bodies</span>
            <select
              aria-label="Encrypted result retention"
              value={llmPrivacy?.result_retention_seconds ?? 3600}
              onChange={(event) => void updateLlmRetention(Number(event.target.value) as LLMPrivacySettings["result_retention_seconds"])}
            >
              <option value={0}>Do not retain after first delivery</option>
              <option value={3600}>1 hour</option>
              <option value={86400}>24 hours</option>
              <option value={604800}>7 days</option>
            </select>
          </label>
          <div className="service-result">
            <small>
              Stored result bodies remain end-to-end encrypted. The selected Provider necessarily sees plaintext while computing;
              this node never persists prompt text.
            </small>
          </div>
          {llmOrders.length ? llmOrders.slice(0, 20).map((order) => (
            <div className="service-result" key={order.task_id}>
              <span>{order.state} · {order.task_id}</span>
              <small>
                {order.transport || "transport pending"}
                {order.amount !== undefined ? ` · ${order.amount} DEV_TASK_BALANCE` : ""}
                {order.updated_at ? ` · ${new Date(order.updated_at).toLocaleString()}` : ""}
              </small>
              <Button onClick={() => void viewLlmOrder(order.task_id)}>View retained result</Button>
            </div>
          )) : (
            <div className="empty-state"><p>No local LLM task history yet.</p></div>
          )}
          <div className="button-row">
            <Button variant="danger" disabled={!llmOrders.length} onClick={clearLlmHistory}>
              Clear completed task history
            </Button>
          </div>
        </div>
      </Panel>

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
