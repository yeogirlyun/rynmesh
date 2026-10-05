import { tr, useUILanguage, uiLocale } from "../uiI18n";
import { CloudCog, RefreshCw, SendHorizontal } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useAppContext } from "../appContext";
import { Button, Chip, Hash, LoadingPanel, PageHeader, Panel, PeerPill } from "../components/ui";
import type {
  LLMHardwareReport,
  LLMOrderResult,
  LLMPrivacySettings,
  LLMProviderStatus,
  LLMServiceRecord,
  LLMSetupJob,
  LLMSetupRequest,
  TaskBalanceSummary,
} from "../domain/nodeClient";
import { LLM_TERMINAL_STATES, llmServiceAvailability, llmServiceRecordKey } from "../domain/llmOrders";
import type { JobCapacity, WorkResult } from "../domain/types";
import { useProviderDiscovery, useServiceOrder } from "../domain/serviceExperience";
import { providerIdentity, serviceDescriptors } from "../domain/serviceDescriptors";
import InferenceAccess from "./InferenceAccess";

const VEO_CAPABILITY = serviceDescriptors.video.capability;
const VEO_OPERATION = serviceDescriptors.video.operation;
const LLM_ERROR_MESSAGES: Record<string, string> = {
  get p2p_distinct_public_egress_required() { return tr("This older strict-P2P package incorrectly requires different public exits. Update both nodes and retry on the current network."); },
  get p2p_public_mapping_unavailable() { return tr("A public UDP mapping could not be created. Check outbound UDP and the configured STUN server."); },
  get p2p_connection_timed_out() { return tr("The direct UDP connection timed out. Check whether private routing or NAT hairpin UDP is allowed between the two nodes."); },
  get p2p_transport_failed() { return tr("The strict peer-to-peer path failed, and relay fallback is disabled."); },
  get capacity_exhausted() { return tr("The Provider is busy. Wait for an available slot or choose another Provider."); },
  get insufficient_task_balance() { return tr("The available development Task Balance is too low for this order."); },
  get provider_unavailable() { return tr("The selected Provider is offline, unhealthy, or no longer advertised. Refresh services and choose an available Provider."); },
  get invalid_order() { return tr("The order contains an invalid or missing value. Review the prompt and output-token limit."); },
  get consumer_restarted_before_completion() { return tr("The Consumer node restarted before the task finished. The reserved balance was released; submit again."); },
};

function llmServiceKey(service: LLMServiceRecord): string {
  return llmServiceRecordKey(service);
}

function shortPeerId(peerId: string): string {
  return peerId.length > 16 ? `${peerId.slice(0, 8)}…${peerId.slice(-6)}` : peerId;
}

function llmErrorMessage(errorCode: string): string {
  return LLM_ERROR_MESSAGES[errorCode] || errorCode;
}

// Shared by friendlyError (thrown request errors) and the async setup-job
// status panel (a raw backend message reported through job polling) so both
// surfaces show the same mapped text for a given backend error string.
function mapKnownLlmErrorText(message: string): string | null {
  if (/local inference runtime dependency is missing/i.test(message)) return "A local runtime dependency is missing. Use Update runtime to repair the runtime files, then retry. Your model and conversations are kept.";
  if (/configured model file is missing/i.test(message)) return "The selected model file is missing. Restore the file or use Model setup to install or select it again, then start the runtime.";
  if (/local inference runtime is not installed/i.test(message)) return "The local runtime is missing. Use Update runtime to restore it, or use Model setup to install it again.";
  if (/configured model checksum no longer matches/i.test(message)) return "The model file failed verification. Use Model setup to download it again before starting the runtime.";
  if (/insufficient development task balance/i.test(message)) return LLM_ERROR_MESSAGES.insufficient_task_balance;
  if (/capacity[_ ]exhausted/i.test(message)) return LLM_ERROR_MESSAGES.capacity_exhausted;
  if (/docker is not installed/i.test(message)) return "Docker is required for managed or GGUF modes. Start Docker, or connect an existing local model API.";
  if (/engine is not running/i.test(message)) return "Docker is installed but not running. Start Docker Desktop and retry.";
  if (/no local inference runtime is available/i.test(message)) return "No local inference runtime is available on this device yet. Retry to download the bundled runtime, or connect an existing local model API.";
  if (/runtime archive checksum mismatch|model checksum mismatch/i.test(message)) return "A download failed verification and was discarded. Retry to download it again.";
  if (/download incomplete/i.test(message)) return "The download was interrupted. Downloaded data is kept; retry to continue. The model still needs verification.";
  if (/download response has invalid byte range or encoding/i.test(message)) return "The source returned an invalid download response. Your previous progress is kept; retry when the source is available.";
  if (/exited during startup/i.test(message)) return "The local model runtime stopped while starting. Retry with a smaller model profile.";
  if (/download exceeded the pinned size/i.test(message)) return "The download did not match the expected size and was discarded. Retry.";
  return null;
}

function friendlyError(error: unknown, fallback: string): string {
  const raw = error instanceof Error ? error.message : fallback;
  const message = raw.replace(/^Local Ryn node returned \d+:\s*/i, "").trim();
  return mapKnownLlmErrorText(message) || message || fallback;
}

export default function Services() {
  useUILanguage();
  const { client, node, peers, notify, confirm } = useAppContext();
  const [selectedPeerId, setSelectedPeerId] = useState("");
  const [videoId, setVideoId] = useState("");
  const [maxScenes, setMaxScenes] = useState("0");
  const [skipExisting, setSkipExisting] = useState(true);
  const [lastOrderId, setLastOrderId] = useState("");
  const [results, setResults] = useState<WorkResult[]>([]);
  const [llmNetwork, setLlmNetwork] = useState("");
  const [selectedLlmServiceKey, setSelectedLlmServiceKey] = useState("");
  const [llmPrompt, setLlmPrompt] = useState(tr("Explain in one sentence why this request travelled through Rynmesh."));
  const [llmMaxTokens, setLlmMaxTokens] = useState("64");
  const [llmTransport, setLlmTransport] = useState<"auto" | "direct" | "p2p" | "relay">("p2p");
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
  const [llmSetupMode, setLlmSetupMode] = useState<LLMSetupRequest["mode"] | "">("openai-compatible");
  const [llmResumeNotice, setLlmResumeNotice] = useState("");
  const setupFormInitialized = useRef(false);
  const [llmProfile, setLlmProfile] = useState<NonNullable<LLMSetupRequest["profile"]>>("auto");
  const [llmHardware, setLlmHardware] = useState<LLMHardwareReport | null>(null);
  const reviewedProfile = llmHardware?.recommendations.find((row) => row.can_run && (llmProfile === "auto" ? row.recommended : row.profile === llmProfile));
  const [llmPackageId, setLlmPackageId] = useState("local-small");
  const [llmAlias, setLlmAlias] = useState("rynmesh-local");
  const [llmBaseUrl, setLlmBaseUrl] = useState("http://127.0.0.1:8080");
  const [llmPort, setLlmPort] = useState("18080");
  const [llmModel, setLlmModel] = useState("");
  const [llmModelPath, setLlmModelPath] = useState("");
  const [llmApiKeyEnv, setLlmApiKeyEnv] = useState("");
  const [llmAllowNonLoopback, setLlmAllowNonLoopback] = useState(false);
  const [llmSetupConfirmed, setLlmSetupConfirmed] = useState(false);
  useEffect(() => { setLlmSetupConfirmed(false); }, [llmSetupMode, llmProfile, reviewedProfile?.profile]);
  const [llmSetupJob, setLlmSetupJob] = useState<LLMSetupJob | null>(null);
  const [llmLifecycleAction, setLlmLifecycleAction] = useState("");
  const [llmLifecycleError, setLlmLifecycleError] = useState("");
  const lifecycleErrorRef = useRef<HTMLParagraphElement>(null);
  useEffect(() => { if (llmLifecycleError) lifecycleErrorRef.current?.focus(); }, [llmLifecycleError]);
  const [llmHistoryQuery, setLlmHistoryQuery] = useState("");
  const [llmHistoryPage, setLlmHistoryPage] = useState(1);
  const llmNetworkInitialized = useRef(false);
  const mountedRef = useRef(true);
  const trackedTaskRef = useRef("");
  const trackedSetupRef = useRef("");

  const configuredNetwork = useServiceOrder({ scope: client, key: "service-network", intervalMs: 15_000,
    load: async () => (await client.getSettings().catch(() => null))?.network_id?.trim() || "rynmesh-main",
    isTerminal: () => true,
  });
  useEffect(() => {
    if (!llmNetworkInitialized.current && configuredNetwork.data) {
      llmNetworkInitialized.current = true;
      setLlmNetwork(configuredNetwork.data);
    }
  }, [configuredNetwork.data]);
  const capacityDiscovery = useProviderDiscovery<JobCapacity>({ scope: client, key: VEO_CAPABILITY,
    intervalMs: serviceDescriptors.video.discoveryIntervalMs,
    load: () => client.listJobCapacities({ capability: VEO_CAPABILITY }),
    identity: (row) => providerIdentity(row.network_id, row.peer_id, VEO_CAPABILITY),
    onData: (rows) => setSelectedPeerId((prior) => prior || rows[0]?.peer_id || ""),
  });
  const discoveryNetwork = llmNetwork.trim() || configuredNetwork.data || "";
  const serviceDiscovery = useProviderDiscovery<LLMServiceRecord>({ scope: client, key: discoveryNetwork,
    enabled: Boolean(discoveryNetwork), intervalMs: serviceDescriptors.privateAI.discoveryIntervalMs,
    load: () => client.listLLMServices(discoveryNetwork),
    identity: (row) => providerIdentity(discoveryNetwork, row.peer_id, row.service.package_id),
    onData: (rows) => setSelectedLlmServiceKey((prior) => prior || (rows[0] ? llmServiceKey(rows[0]) : "")),
    onError: (error) => setLlmProgress({ tone: "danger", text: `Service discovery failed: ${error.message}` }),
  });
  const capacities = capacityDiscovery.providers;
  const llmServices = serviceDiscovery.providers;
  const veoServices = useMemo(
    () => capacities.filter((item) => item.capabilities.includes(VEO_CAPABILITY)),
    [capacities],
  );
  const selectedVeo = veoServices.find((item) => item.peer_id === selectedPeerId) ?? veoServices[0];
  const selectedLlm = selectedLlmServiceKey ? llmServices.find((item) => llmServiceKey(item) === selectedLlmServiceKey) : llmServices[0];
  const peerById = new Map(peers.map((peer) => [peer.id, peer]));

  const parsedMaxTokens = Number(llmMaxTokens);
  const packageIdValid = /^[a-z0-9][a-z0-9._-]*$/.test(llmPackageId.trim());
  const maxTokensValid = Number.isInteger(parsedMaxTokens) && parsedMaxTokens > 0
    && (!selectedLlm || parsedMaxTokens <= selectedLlm.service.max_output_tokens);
  const estimatedInputTokens = Math.max(1, Math.ceil(llmPrompt.trim().length / 4));
  const estimatedAmount = selectedLlm
    ? Math.max(
        selectedLlm.service.pricing.minimum,
        (estimatedInputTokens / 1000) * selectedLlm.service.pricing.input_per_1k
          + ((Number.isFinite(parsedMaxTokens) ? parsedMaxTokens : 0) / 1000)
            * selectedLlm.service.pricing.output_per_1k,
      )
    : 0;
  const priceWithinProviderLimit = !selectedLlm
    || estimatedAmount <= selectedLlm.service.pricing.maximum_per_task;
  const contextWithinProviderLimit = !selectedLlm || !maxTokensValid
    || estimatedInputTokens + parsedMaxTokens <= selectedLlm.service.context_window;
  const providerAvailable = Boolean(selectedLlm?.online)
    && (selectedLlm?.capacity?.available === undefined || selectedLlm.capacity.available > 0);
  const balanceAvailable = !llmBalance || llmBalance.available >= estimatedAmount;
  const canSubmitLlm = Boolean(selectedLlm && llmPrompt.trim() && maxTokensValid
    && contextWithinProviderLimit && priceWithinProviderLimit
    && providerAvailable && balanceAvailable && !llmSubmitting);
  const filteredLlmOrders = useMemo(() => {
    const query = llmHistoryQuery.trim().toLowerCase();
    if (!query) return llmOrders;
    return llmOrders.filter((order) => [
      order.task_id, order.state, order.transport, order.model_alias, order.error_code,
    ].some((value) => String(value || "").toLowerCase().includes(query)));
  }, [llmHistoryQuery, llmOrders]);
  const llmHistoryPages = Math.max(1, Math.ceil(filteredLlmOrders.length / 10));
  const visibleLlmOrders = filteredLlmOrders.slice((llmHistoryPage - 1) * 10, llmHistoryPage * 10);

  const serviceStatus = useServiceOrder({ scope: client, key: "service-management",
    intervalMs: 15_000,
    load: () => Promise.allSettled([
      client.getTaskBalance(), client.getLLMServiceStatus(), client.listLLMOrders(),
      client.getLLMPrivacy(), client.getLLMSetupStatus(), client.getLLMHardware(),
    ] as const),
    onData: ([balanceResult, providerResult, ordersResult, privacyResult, setupResult, hardwareResult]) => {
      if (balanceResult.status === "fulfilled") setLlmBalance(balanceResult.value);
      if (providerResult.status === "fulfilled") setLlmProvider(providerResult.value);
      if (ordersResult.status === "fulfilled") {
        setLlmOrders(ordersResult.value);
        const active = ordersResult.value.find((order) => !LLM_TERMINAL_STATES.has(order.state));
        if (active) void trackLlmOrder(active.task_id);
      }
      if (privacyResult.status === "fulfilled") setLlmPrivacy(privacyResult.value);
      if (setupResult.status === "fulfilled") {
        setLlmSetupJob(setupResult.value);
        if (!setupFormInitialized.current) {
          setupFormInitialized.current = true;
          const previous = setupResult.value;
          if (previous.job_id && previous.state !== "succeeded" && previous.state !== "idle") {
            const choices = previous.resume_configuration;
            setLlmSetupConfirmed(false);
            if (choices?.mode === "managed" && ["light", "balanced", "quality"].includes(choices.profile)
              && typeof choices.package_id === "string" && /^[a-z0-9][a-z0-9._-]{0,254}$/.test(choices.package_id)
              && Number.isInteger(choices.port) && choices.port >= 1 && choices.port <= 65535) {
              setLlmSetupMode("managed");
              setLlmProfile(choices.profile);
              setLlmPackageId(choices.package_id);
              setLlmPort(String(choices.port));
              setLlmResumeNotice("Previous model choices restored. Review the source and confirm before continuing.");
            } else {
              setLlmSetupMode("");
              setLlmResumeNotice("Previous setup choices are unavailable. Choose the setup mode and review its settings before configuring again.");
            }
          }
        }
        if (setupResult.value.job_id && ["queued", "running", "cancelling"].includes(setupResult.value.state)) {
          void trackSetupJob(setupResult.value.job_id);
        }
      }
      if (hardwareResult.status === "fulfilled") setLlmHardware(hardwareResult.value);
    },
  });
  const workResults = useServiceOrder({ scope: client, key: lastOrderId, enabled: Boolean(lastOrderId),
    intervalMs: serviceDescriptors.video.orderIntervalMs,
    load: () => client.listWorkResults({ work_order_id: lastOrderId }),
    onData: setResults,
    isTerminal: (rows) => Boolean(rows[0] && ["completed", "failed", "cancelled"].includes(rows[0].status)),
  });
  const loading = configuredNetwork.loading || capacityDiscovery.loading || serviceDiscovery.loading || serviceStatus.loading;
  const refresh = async (_silent = false) => {
    await Promise.all([capacityDiscovery.refresh(), serviceDiscovery.refresh(), serviceStatus.refresh(), workResults.refresh()]);
  };

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; trackedTaskRef.current = ""; trackedSetupRef.current = ""; };
  }, [client]);

  useEffect(() => {
    setLlmHistoryPage((page) => Math.min(page, llmHistoryPages));
  }, [llmHistoryPages]);

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
    notify("ok", tr("Veo render request submitted through Rynmesh"));
  };

  async function trackSetupJob(jobId: string) {
    if (!jobId || trackedSetupRef.current === jobId) return;
    trackedSetupRef.current = jobId;
    setLlmConfiguring(true);
    try {
      while (mountedRef.current && trackedSetupRef.current === jobId) {
        const job = await client.getLLMSetupStatus();
        setLlmSetupJob(job);
        if (["succeeded", "failed", "cancelled"].includes(job.state)) {
          if (job.state === "succeeded") {
            notify("ok", tr("Local model configured and self-tested; publishing remains off"));
            await refresh(true);
          } else {
            notify(job.state === "cancelled" ? "warn" : "danger", job.message || tr("Local model setup did not complete"));
          }
          break;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 500));
      }
    } catch (error) {
      setLlmSetupJob({
        job_id: jobId,
        state: "failed",
        stage: "status",
        progress: 0,
        message: friendlyError(error, tr("Setup status is temporarily unavailable. Retry status refresh.")),
        retryable: true,
      });
    } finally {
      if (trackedSetupRef.current === jobId) trackedSetupRef.current = "";
      if (mountedRef.current) setLlmConfiguring(false);
    }
  }

  async function applyTerminalResult(result: LLMOrderResult) {
    setLlmResult(result);
    // The result panel renders the full mapped error text; the progress line
    // only carries the state so the message never appears twice.
    setLlmProgress({
      tone: result.state === "succeeded" ? "ok" : "danger",
      text: result.transport ? tr("Order {{state}} via {{transport}}", { state: tr(result.state), transport: tr(result.transport) }) : tr("Order {{state}}", { state: tr(result.state) }),
    });
    notify(result.state === "succeeded" ? "ok" : "warn", tr("LLM task {{v0}}", { v0: result.state }));
    try {
      setLlmBalance(await client.getTaskBalance());
      setLlmOrders(await client.listLLMOrders());
    } catch {
      // Best-effort refresh; the periodic refresh catches up. Throwing here
      // used to re-enter the polling loop on a finished order forever.
    }
  }

  useServiceOrder({ scope: client, key: llmActiveTaskId, enabled: Boolean(llmActiveTaskId),
    intervalMs: serviceDescriptors.privateAI.orderIntervalMs,
    load: () => client.getLLMOrder(llmActiveTaskId),
    isTerminal: (result) => LLM_TERMINAL_STATES.has(result.state),
    onData: (result) => {
      if (LLM_TERMINAL_STATES.has(result.state)) {
        trackedTaskRef.current = "";
        setLlmSubmitting(false);
        setLlmActiveTaskId("");
        void applyTerminalResult(result);
      } else setLlmProgress({ tone: "info", text: `Order ${result.task_id} is ${result.state}; waiting for the Provider node…` });
    },
    onError: () => setLlmProgress({ tone: "info", text: "Task status is temporarily unavailable; reconnecting…" }),
  });

  async function trackLlmOrder(taskId: string) {
    if (!taskId || trackedTaskRef.current) return;
    trackedTaskRef.current = taskId;
    setLlmActiveTaskId(taskId);
    setLlmSubmitting(true);
  }

  const publishLlm = async () => {
    setLlmPublishing(true);
    try {
      await client.publishLLMService({ network_id: llmNetwork, benchmark: false });
      notify("ok", tr("Local LLM service published to the Rynmesh discovery network"));
      await refresh();
    } catch (error) {
      notify("danger", error instanceof Error ? error.message : tr("LLM service publication failed"));
    } finally {
      setLlmPublishing(false);
    }
  };

  const pauseLlm = async () => {
    setLlmPublishing(true);
    try {
      setLlmProvider(await client.pauseLLMService());
      notify("ok", tr("Local LLM service paused; new orders will be rejected"));
    } catch (error) {
      notify("danger", error instanceof Error ? error.message : tr("LLM service pause failed"));
    } finally {
      setLlmPublishing(false);
    }
  };

  const setupLlm = async () => {
    if (!llmSetupMode) return;
    if (llmSetupMode === "managed" && !reviewedProfile) return;
    setLlmConfiguring(true);
    try {
      const job = await client.startLLMSetup({
        mode: llmSetupMode,
        package_id: llmPackageId.trim(),
        alias: llmAlias.trim(),
        port: Number(llmPort),
        base_url: llmBaseUrl.trim(),
        model: llmModel.trim(),
        model_path: llmModelPath.trim(),
        api_key_env: llmApiKeyEnv.trim(),
        allow_non_loopback: llmAllowNonLoopback,
        accept_risk: llmSetupConfirmed,
        profile: llmSetupMode === "managed" ? reviewedProfile!.profile : llmProfile,
      });
      setLlmSetupJob(job);
      await trackSetupJob(job.job_id || "");
    } catch (error) {
      notify("danger", friendlyError(error, tr("Local model setup failed")));
    } finally {
      setLlmConfiguring(false);
    }
  };

  const cancelLlmSetup = async () => {
    if (!llmSetupJob?.job_id) return;
    try {
      setLlmSetupJob(await client.cancelLLMSetup(llmSetupJob.job_id));
      notify("warn", "Setup cancellation requested. Wait for the final recovery status.");
    } catch (error) {
      notify("danger", friendlyError(error, tr("Unable to cancel local model setup")));
    }
  };

  const runLlmLifecycle = async (
    action: "start" | "stop" | "restart" | "update" | "self-test" | "uninstall",
    options?: { delete_environment?: boolean; delete_model?: boolean; confirm_model_delete?: boolean },
  ) => {
    setLlmLifecycleAction(action);
    setLlmLifecycleError("");
    try {
      const response = options ? await client.runLLMServiceAction(action, options) : await client.runLLMServiceAction(action);
      const result = response.result as { removed?: string[]; model_preserved?: boolean } | undefined;
      notify("ok", action === "uninstall"
        ? `${result?.removed?.includes("runtime_process") ? "Runtime process removed; shared runtime files remain installed." : "Runtime removal completed."} ${result?.model_preserved === false ? "Managed model file removed." : "Model data preserved."} Private configuration and conversations are preserved.`
        : `Local model ${action} completed; publishing remains paused until enabled`);
      await refresh(true);
    } catch (error) {
      setLlmLifecycleError(friendlyError(error, `Local model ${action} failed`));
      await refresh(true);
    } finally {
      setLlmLifecycleAction("");
    }
  };

  const submitLlm = async () => {
    if (!selectedLlm || !canSubmitLlm) return;
    setLlmSubmitting(true);
    setLlmResult(null);
    setLlmProgress({
      tone: "info",
      text: llmTransport === "p2p"
        ? tr("Order accepted locally. Strict P2P connection is in progress; relay fallback is disabled.")
        : llmTransport === "relay"
          ? tr("Order accepted locally. End-to-end ciphertext relay delivery is in progress.")
          : llmTransport === "direct"
            ? tr("Order accepted locally. A direct Provider node connection is in progress.")
            : tr("Order accepted locally. The node is selecting an available encrypted transport."),
    });
    try {
      const result = await client.submitLLMOrder({
        network_id: llmNetwork,
        provider_peer_id: selectedLlm.peer_id,
        service_id: selectedLlm.service.package_id,
        prompt: llmPrompt.trim(),
        max_tokens: parsedMaxTokens,
        transport: llmTransport,
      });
      setLlmActiveTaskId(result.task_id);
      setLlmPrompt("");
      if (LLM_TERMINAL_STATES.has(result.state)) {
        await applyTerminalResult(result);
      } else {
        setLlmSubmitting(false);
        await trackLlmOrder(result.task_id);
      }
    } catch (error) {
      const message = friendlyError(error, tr("LLM task failed"));
      setLlmProgress({ tone: "danger", text: tr("Order failed: {{v0}}", { v0: message }) });
      setLlmBalance(await client.getTaskBalance());
      notify("danger", message);
    } finally {
      if (!trackedTaskRef.current) {
        setLlmSubmitting(false);
        setLlmActiveTaskId("");
      }
    }
  };

  const cancelLlm = async () => {
    if (!llmActiveTaskId) return;
    setLlmCancelling(true);
    try {
      const result = await client.cancelLLMOrder(llmActiveTaskId);
      setLlmResult(result);
      setLlmProgress({ tone: "info", text: tr("Order {{v0}} cancellation requested", { v0: result.task_id }) });
      notify("ok", tr("Cancellation sent to the Provider node; the reserved balance was released"));
    } catch (error) {
      notify("danger", error instanceof Error ? error.message : tr("LLM task cancellation failed"));
    } finally {
      setLlmCancelling(false);
    }
  };

  const updateLlmRetention = async (value: LLMPrivacySettings["result_retention_seconds"]) => {
    try {
      setLlmPrivacy(await client.updateLLMPrivacy(value));
      notify("ok", value ? tr("Encrypted result retention updated") : tr("Stored encrypted results purged"));
    } catch (error) {
      notify("danger", error instanceof Error ? error.message : tr("Result retention update failed"));
    }
  };

  const clearLlmHistory = () => {
    confirm({
      title: tr("Clear completed LLM task history?"),
      body: tr("This permanently removes local task metadata and any retained encrypted results. Running tasks are preserved."),
      risk: "high",
      confirmLabel: tr("Clear task history"),
      onConfirm: async () => {
        const result = await client.clearLLMOrders();
        setLlmOrders(await client.listLLMOrders());
        notify("ok", tr("{{v0}} local LLM task records removed", { v0: result.removed }));
      },
    });
  };

  const viewLlmOrder = async (taskId: string) => {
    try {
      setLlmResult(await client.getLLMOrder(taskId));
    } catch (error) {
      notify("danger", error instanceof Error ? error.message : tr("Task result is unavailable"));
    }
  };

  const copyLlmResult = async () => {
    if (!llmResult?.output) return;
    try {
      await navigator.clipboard.writeText(llmResult.output);
      notify("ok", tr("Result copied to the clipboard"));
    } catch {
      notify("danger", tr("Clipboard access is unavailable; select the result text and copy it manually"));
    }
  };

  if (loading) return <LoadingPanel />;

  return (
    <div className="screen-stack">
      <PageHeader
        eyebrow={tr("Services")}
        title={tr("Service setup")}
        context={tr("Set up and manage the services running on this device.")}
        actions={<Button icon={RefreshCw} onClick={() => void refresh()}>{tr("Refresh")}</Button>}
      />

      <section id="inference-api"><InferenceAccess /></section>

      <Panel>
        <div className="panel-head">
          <div>
            <span className="eyebrow">{tr("Provider control")}</span>
            <h2>{tr("Local LLM service")}</h2>
          </div>
          <Chip tone={llmProvider?.ready || llmProvider?.online ? "ok" : llmProvider?.configured === false ? "info" : "danger"}>
            {llmProvider?.ready ? (llmProvider.capacity?.available === 0 ? "busy" : "ready on this device") : llmProvider?.online ? "online" : llmProvider?.configured === false ? "not configured" : "not ready"}
          </Chip>
        </div>
        {llmProvider?.service ? (
          <div className="form-stack">
            <div className="service-result">
              <span>{llmProvider.service.model_alias}</span>
              <small>
                {llmProvider.service.package_id} {tr("· context")} {llmProvider.service.context_window} ·
                {` ${tr("{{available}}/{{total}} slots", { available: llmProvider.capacity?.available ?? 0, total: llmProvider.capacity?.max_concurrent ?? 0 })}`}
              </small>
            </div>
            <div className="button-row">
              {llmProvider.ready ? <Link to={`/ask?peer=${encodeURIComponent(node.peer_id)}&service=${encodeURIComponent(llmProvider.service.package_id)}&network=${encodeURIComponent(llmNetwork)}`}>Ask using this device</Link> : null}
              {!llmProvider.publication_enabled ? <Chip tone="info">Remote sharing is off. Local Ask Ryn remains available when ready.</Chip> : null}
              <Button
                variant="primary"
                icon={CloudCog}
                disabled={llmPublishing}
                onClick={() => void publishLlm()}
              >
                {llmPublishing ? tr("Publishing…") : tr("Publish / refresh service")}
              </Button>
              {llmProvider.publication_enabled ? (
                <Button disabled={llmPublishing} onClick={() => void pauseLlm()}>
                  {llmPublishing ? tr("Pausing…") : tr("Pause new orders")}
                </Button>
              ) : null}
              <Chip tone="info">{tr("Publishes metadata only — never prompts or model files")}</Chip>
            </div>
            <div className="button-row">
              <Button disabled={Boolean(llmLifecycleAction)} onClick={() => void runLlmLifecycle("self-test")}>
                {llmLifecycleAction === "self-test" ? tr("Testing…") : tr("Run self-test")}
              </Button>
              {llmProvider.lifecycle?.runtime?.managed !== false ? (
                <>
                  <Button disabled={Boolean(llmLifecycleAction)} onClick={() => void runLlmLifecycle("start")}>{tr("Start runtime")}</Button>
                  <Button disabled={Boolean(llmLifecycleAction)} onClick={() => void runLlmLifecycle("stop")}>{tr("Stop runtime")}</Button>
                  <Button disabled={Boolean(llmLifecycleAction)} onClick={() => void runLlmLifecycle("restart")}>{tr("Restart")}</Button>
                  <Button disabled={Boolean(llmLifecycleAction)} onClick={() => void runLlmLifecycle("update")}>{tr("Update runtime")}</Button>
                  <Button
                    variant="danger"
                    disabled={Boolean(llmLifecycleAction)}
                    onClick={() => confirm({
                      title: "Remove this model's runtime instance?",
                      body: "This stops and removes this model's runtime instance. Shared native runtime files, model data, private configuration and conversations are preserved.",
                      risk: "high",
                      confirmLabel: tr("Uninstall runtime"),
                      onConfirm: () => runLlmLifecycle("uninstall"),
                    })}
                  >
                    {tr("Uninstall runtime")}
                  </Button>
                  {llmProvider.lifecycle?.mode === "managed" ? (
                    <Button
                      variant="danger"
                      disabled={Boolean(llmLifecycleAction)}
                      onClick={() => confirm({
                        title: "Delete the managed model too?",
                        body: "This removes the model's runtime instance and Rynmesh-owned model file. Shared native runtime files, private configuration and conversations are preserved. Imported or user-owned files are never deleted.",
                        risk: "high",
                        confirmLabel: tr("Delete managed model"),
                        onConfirm: () => runLlmLifecycle("uninstall", {
                          delete_environment: true,
                          delete_model: true,
                          confirm_model_delete: true,
                        }),
                      })}
                    >
                      {tr("Delete managed model")}
                    </Button>
                  ) : null}
                </>
              ) : <Chip tone="info">{tr("External runtime is owner-managed")}</Chip>}
              {llmProvider.lifecycle?.runtime?.status ? (
                <Chip mono>{tr(llmProvider.lifecycle.runtime.status)}</Chip>
              ) : null}
            </div>
            {llmLifecycleError ? <p ref={lifecycleErrorRef} tabIndex={-1} role="alert">{llmLifecycleError}</p> : null}
            {llmProvider.lifecycle?.storage ? <p role="status">
              Model file: {llmProvider.lifecycle.storage.model_present === false ? "missing · 0 MiB" : llmProvider.lifecycle.storage.model_bytes === null ? "size unavailable — refresh to check again" : `${(llmProvider.lifecycle.storage.model_bytes / 1024 / 1024).toFixed(1)} MiB`}
              {llmProvider.lifecycle.storage.model_owned ? " · managed by Rynmesh" : " · externally managed"}. Shared runtime files are separate.
            </p> : null}
          </div>
        ) : (
          <div className="empty-state">
            <h3>{tr("No local provider configured")}</h3>
            <p>{tr("This node can still discover and consume services from another Rynmesh node.")}</p>
          </div>
        )}
        <div className="form-stack">
          <div className="panel-head">
            <div>
              <span className="eyebrow">{tr("Model setup")}</span>
              <h3>{llmProvider?.configured ? tr("Change local model connection") : tr("Add a local model")}</h3>
            </div>
            <Chip tone="info">{tr("Publishing stays off after setup")}</Chip>
          </div>
          {llmResumeNotice ? <p>{llmResumeNotice}</p> : null}
          <label className="field">
            <span>{tr("Setup mode")}</span>
            <select value={llmSetupMode} onChange={(event) => setLlmSetupMode(event.target.value as LLMSetupRequest["mode"])}>
              <option value="" disabled>Choose setup mode</option>
              <option value="openai-compatible">OpenAI-compatible local API</option>
              <option value="ollama">Ollama</option>
              <option value="import-gguf">Import a GGUF file read-only</option>
              <option value="managed">Managed local model (bundled runtime)</option>
            </select>
          </label>
          {llmHardware ? (
            <div className="service-result">
              <small>
                {/* `native_runtime_present`, not `native_runtime_available`:
                    the latter is true wherever the pinned release *could* be
                    downloaded, so it would claim "available" on a device that
                    has nothing installed yet. */}
                {llmHardware.hardware.native_runtime_present
                  ? "Bundled runtime: available"
                  : "Bundled runtime: will be downloaded on first setup"}
              </small>
            </div>
          ) : null}
          <label className="field">
            <span>{tr("Package ID")}</span>
            <input value={llmPackageId} onChange={(event) => setLlmPackageId(event.target.value)} />
          </label>
          <label className="field">
            <span>{tr("Public model alias")}</span>
            <input value={llmAlias} onChange={(event) => setLlmAlias(event.target.value)} />
          </label>
          {llmSetupMode === "openai-compatible" || llmSetupMode === "ollama" ? (
            <>
              <label className="field">
                <span>{tr("Local API URL")}</span>
                <input value={llmBaseUrl} onChange={(event) => setLlmBaseUrl(event.target.value)} />
              </label>
              <label className="field">
                <span>{tr("Model name (optional)")}</span>
                <input value={llmModel} onChange={(event) => setLlmModel(event.target.value)} />
              </label>
              <label className="field">
                <span>{tr("API key environment variable (optional)")}</span>
                <input
                  aria-label={tr("API key environment variable (optional)")}
                  value={llmApiKeyEnv}
                  onChange={(event) => setLlmApiKeyEnv(event.target.value)}
                  placeholder={tr("For example: LOCAL_LLM_API_KEY")}
                />
                <small>{tr("Enter the environment-variable name, never the secret value.")}</small>
              </label>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={llmAllowNonLoopback}
                  onChange={(event) => setLlmAllowNonLoopback(event.target.checked)}
                />
                {tr("Allow a trusted non-loopback API address. Only enable this for a network you control.")}
              </label>
            </>
          ) : null}
          {llmSetupMode === "import-gguf" ? (
            <label className="field">
              <span>{tr("GGUF file path")}</span>
              <input value={llmModelPath} onChange={(event) => setLlmModelPath(event.target.value)} />
            </label>
          ) : null}
          {llmSetupMode === "managed" || llmSetupMode === "import-gguf" ? (
            <>
              <div className="service-result">
                <small>
                  {llmSetupMode === "managed"
                    ? "Downloads a verified model and runs it with the bundled llama.cpp runtime on this device. Docker is only used on server nodes that choose it."
                    : "Runs the imported GGUF file with the bundled llama.cpp runtime on this device. Docker is only used on server nodes that choose it."}
                </small>
              </div>
              {llmSetupMode === "managed" ? (
                <label className="field">
                  <span>Model profile</span>
                  <select
                    value={llmProfile}
                    onChange={(event) => setLlmProfile(event.target.value as typeof llmProfile)}
                  >
                    <option value="auto">Automatic — recommended for this device</option>
                    {/* A recommendation the device cannot run is not an
                        option, and the no-fit sentinel the node returns has
                        no profile name — either would render a blank entry. */}
                    {(llmHardware?.recommendations ?? [])
                      .filter((rec) => rec.can_run && rec.profile)
                      .map((rec) => (
                        <option key={rec.profile} value={rec.profile}>
                          {(rec.display_name || rec.profile)}
                          {rec.estimated_memory_mb ? ` · ~${rec.estimated_memory_mb} MB memory` : ""}
                          {rec.estimated_disk_mb ? ` · ~${rec.estimated_disk_mb} MB disk` : ""}
                          {rec.recommended ? " · recommended" : ""}
                        </option>
                      ))}
                  </select>
                </label>
              ) : null}
              {llmSetupMode === "managed" && reviewedProfile ? <div className="service-result">
                <span>{reviewedProfile.display_name || reviewedProfile.model_alias || reviewedProfile.profile}</span>
                <small>Model download: {reviewedProfile.download_bytes ? `${(reviewedProfile.download_bytes / 1024 / 1024).toFixed(0)} MiB` : "size unavailable"}; required disk: {reviewedProfile.estimated_disk_mb ?? "unknown"} MiB; memory: {reviewedProfile.estimated_memory_mb ?? "unknown"} MiB. The runtime may require an additional download.</small>
                {reviewedProfile.source_url?.startsWith("https://") ? <a href={reviewedProfile.source_url} target="_blank" rel="noreferrer">Pinned model source</a> : null}
                {reviewedProfile.license_url?.startsWith("https://") ? <a href={reviewedProfile.license_url} target="_blank" rel="noreferrer">License: {reviewedProfile.license_id}</a> : null}
                <small>{reviewedProfile.license_notice || "Review the model source and license before installation."}</small>
              </div> : null}
              {llmSetupMode === "managed" && !reviewedProfile ? <p role="status">No verified profile is currently recommended. Refresh device information or free memory and disk space.</p> : null}
              <label className="field">
                <span>{tr("Local runtime port")}</span>
                <input value={llmPort} onChange={(event) => setLlmPort(event.target.value)} inputMode="numeric" />
              </label>
              <label className="checkbox-row">
                <input type="checkbox" checked={llmSetupConfirmed} onChange={(event) => setLlmSetupConfirmed(event.target.checked)} />
                {tr("I understand this prepares a local runtime and may download software or model data.")}
              </label>
            </>
          ) : null}
          {llmSetupJob && llmSetupJob.state !== "idle" ? (
            <div className="service-result" role="status" aria-live="polite">
              <span>
                {llmSetupJob.state === "failed"
                  ? mapKnownLlmErrorText(llmSetupJob.message || "") || llmSetupJob.message || llmSetupJob.stage
                  : llmSetupJob.message || llmSetupJob.stage}
              </span>
              <progress value={llmSetupJob.progress} max={100} aria-label="Model setup progress" />
              <small>{llmSetupJob.progress}% · {llmSetupJob.state}</small>
            </div>
          ) : null}
          <div className="button-row">
            <Button
              variant="primary"
              disabled={llmConfiguring || !llmSetupMode || !packageIdValid || !llmAlias.trim()
                || (llmSetupMode === "managed" && !reviewedProfile)
                || (llmSetupMode === "import-gguf" && !llmModelPath.trim())
                || ((llmSetupMode === "managed" || llmSetupMode === "import-gguf")
                  && (!llmSetupConfirmed || !Number.isInteger(Number(llmPort)) || Number(llmPort) < 1 || Number(llmPort) > 65535))}
              onClick={() => void setupLlm()}
            >
              {llmConfiguring ? tr("Configuring and self-testing…")
                : llmSetupJob?.retryable ? tr("Retry configuration") : tr("Configure and run self-test")}
            </Button>
            {llmSetupJob?.job_id && ["queued", "running", "cancelling"].includes(llmSetupJob.state) ? (
              <Button disabled={llmSetupJob.state === "cancelling"} onClick={() => void cancelLlmSetup()}>
                {llmSetupJob.state === "cancelling" ? tr("Cancelling…") : tr("Cancel setup")}
              </Button>
            ) : null}
            <Chip tone="info">{tr("The compute node sees plaintext during inference")}</Chip>
            {!packageIdValid ? <Chip tone="danger">{tr("Package ID must be a lowercase slug")}</Chip> : null}
          </div>
        </div>
      </Panel>

      <Panel>
        <div className="panel-head">
          <div>
            <span className="eyebrow">{tr("Private local inference")}</span>
            <h2>{tr("LLM service order")}</h2>
          </div>
          <Chip tone={llmServices.length ? "ok" : "warn"}>
            {llmServices.length} {tr("available ·")} {llmBalance?.available.toFixed(3) ?? "—"} {tr("DEV balance")}
          </Chip>
        </div>
        <div className="form-stack">
          <label className="field">
            <span>{tr("Filter task history")}</span>
            <input
              value={llmHistoryQuery}
              onChange={(event) => {
                setLlmHistoryQuery(event.target.value);
                setLlmHistoryPage(1);
              }}
              placeholder={tr("Task ID, state, transport, model, or error")}
            />
          </label>
          <label className="field">
            <span>{tr("Discovery network")}</span>
            <input value={llmNetwork} onChange={(event) => setLlmNetwork(event.target.value)} />
          </label>
          <div className="button-row">
            <Button onClick={() => void refresh()} icon={RefreshCw}>{tr("Discover services")}</Button>
            <Chip tone="info">{tr("Ryn-to-Ryn encrypted path")}</Chip>
          </div>
          <label className="field">
            <span>{tr("Provider service")}</span>
            <select
              value={selectedLlmServiceKey || (selectedLlm ? llmServiceKey(selectedLlm) : "")}
              onChange={(event) => setSelectedLlmServiceKey(event.target.value)}
            >
              {llmServices.map((service) => (
                <option key={llmServiceKey(service)} value={llmServiceKey(service)}>
                  {service.service.model_alias} · {service.node_name || shortPeerId(service.peer_id)} · {service.service.package_id}
                  {` · ${service.service.pricing.minimum} ${tr(service.service.pricing.currency)}`}
                </option>
              ))}
            </select>
          </label>
          {selectedLlm ? (
            <div className="service-result">
              <Chip tone={selectedLlm.online ? "ok" : "warn"}>{llmServiceAvailability(selectedLlm)}</Chip>
              <span>
                {selectedLlm.node_name || shortPeerId(selectedLlm.peer_id)} · {selectedLlm.service.package_id}
                {tr(" · Context {{v0}} · max output {{v1}}", { v0: selectedLlm.service.context_window, v1: selectedLlm.service.max_output_tokens })}
                {selectedLlm.capacity ? ` · ${tr("{{available}}/{{total}} slots", { available: selectedLlm.capacity.available ?? 0, total: selectedLlm.capacity.max_concurrent ?? 0 })}` : ""}
              </span>
              <small>{selectedLlm.service.privacy.policy_text || tr("Provider compute node sees plaintext.")}</small>
            </div>
          ) : (
            <div className="empty-state">
              <h3>{tr("No LLM service discovered")}</h3>
              <p>{tr("Check the network name, then choose Discover services.")}</p>
            </div>
          )}
          <label className="field">
            <span>{tr("Prompt")}</span>
            <textarea rows={5} value={llmPrompt} onChange={(event) => setLlmPrompt(event.target.value)} />
          </label>
          <label className="field">
            <span>{tr("Maximum output tokens")}</span>
            <input value={llmMaxTokens} onChange={(event) => setLlmMaxTokens(event.target.value)} inputMode="numeric" />
          </label>
          <label className="field">
            <span>{tr("Transport policy")}</span>
            <select value={llmTransport} onChange={(event) => setLlmTransport(event.target.value as typeof llmTransport)}>
              <option value="auto">{tr("Automatic — direct first, encrypted relay only if configured")}</option>
              <option value="direct">{tr("Direct Provider HTTP only")}</option>
              <option value="p2p">{tr("Strict ICE/UDP P2P — never relay")}</option>
              <option value="relay">{tr("End-to-end ciphertext relay")}</option>
            </select>
          </label>
          <div className="service-result">
            <small>
              {tr("Strict P2P exchanges host and STUN candidates and may use a private route or NAT hairpin when both nodes share one public gateway. TURN and payload relay remain forbidden. The Provider sees plaintext during inference; Registry signaling never receives task bodies.")}
            </small>
          </div>
          <div className="service-result">
            <span>{tr("Estimated reservation:")} {estimatedAmount.toFixed(6)} {tr(selectedLlm?.service.pricing.currency || "DEV_TASK_BALANCE")}</span>
            <small>
              {tr("Based on approximately")} {estimatedInputTokens} {tr("input tokens and a")} {Number.isFinite(parsedMaxTokens) ? parsedMaxTokens : 0}{tr("-token output cap. Final settlement uses actual usage; unused reservation is released.")}
            </small>
            {!selectedLlm ? <Chip tone="warn">Choose a Provider</Chip> : null}
            {selectedLlm && !selectedLlm.online ? <Chip tone="warn">{llmServiceAvailability(selectedLlm)}</Chip> : null}
            {selectedLlm?.capacity?.available === 0 ? <Chip tone="warn">Provider busy</Chip> : null}
            {!maxTokensValid ? <Chip tone="danger">Enter 1–{selectedLlm?.service.max_output_tokens || "provider max"} whole tokens</Chip> : null}
            {!contextWithinProviderLimit ? <Chip tone="danger">Prompt plus output exceeds the Provider context window</Chip> : null}
            {!priceWithinProviderLimit ? <Chip tone="danger">Estimated cost exceeds the Provider task maximum</Chip> : null}
            {!balanceAvailable ? <Chip tone="danger">Insufficient DEV balance</Chip> : null}
          </div>
          <div className="button-row">
            <Button
              variant="primary"
              icon={SendHorizontal}
              disabled={!canSubmitLlm}
              onClick={() => void submitLlm()}
            >
              {llmSubmitting ? tr("Task running…") : tr("Place encrypted order")}
            </Button>
            {llmActiveTaskId ? (
              <Button disabled={llmCancelling} onClick={() => void cancelLlm()}>
                {llmCancelling ? tr("Cancelling…") : tr("Cancel task")}
              </Button>
            ) : null}
            {llmBalance ? <Chip mono>{tr("held")} {llmBalance.held.toFixed(3)}</Chip> : null}
            {llmActiveTaskId ? <Chip mono>{tr("resumed")} {llmActiveTaskId}</Chip> : null}
          </div>
          {llmProgress ? (
            <div className="service-result" role="status" aria-live="polite">
              <Chip tone={llmProgress.tone}>{tr(llmSubmitting ? "running" : llmProgress.tone === "ok" ? "complete" : "failed")}</Chip>
              <span>{llmProgress.text}</span>
            </div>
          ) : null}
        </div>
      </Panel>

      {llmResult ? (
        <Panel>
          <div className="panel-head">
            <div>
              <span className="eyebrow">{tr("Latest private LLM result")}</span>
              <h2>{llmResult.model_alias || tr("Local model")}</h2>
            </div>
            <Chip tone={llmResult.state === "succeeded" ? "ok" : "danger"}>{tr(llmResult.state)}</Chip>
          </div>
          <div className="form-stack">
            <div className="service-result">
              <pre className="llm-output">
                {llmResult.output || (llmResult.error_code ? llmErrorMessage(llmResult.error_code) : tr("No output"))}
              </pre>
            </div>
            <div className="button-row">
              <Chip mono>{llmResult.task_id}</Chip>
              <Chip mono>{llmResult.input_tokens ?? 0} {tr("in /")} {llmResult.output_tokens ?? 0} {tr("out")}</Chip>
              <Chip mono>{llmResult.duration_ms ?? 0} ms</Chip>
              <Chip mono>{llmResult.amount ?? 0} {tr("DEV_TASK_BALANCE")}</Chip>
              {llmResult.transport ? <Chip mono>{tr(llmResult.transport)}</Chip> : null}
              {llmResult.output ? <Button onClick={() => void copyLlmResult()}>{tr("Copy result")}</Button> : null}
            </div>
          </div>
        </Panel>
      ) : null}

      <Panel>
        <div className="panel-head">
          <div>
            <span className="eyebrow">{tr("Local task history & privacy")}</span>
            <h2>{tr("Private LLM orders")}</h2>
          </div>
          <Chip tone="info">{tr("Prompt text is never written to task history")}</Chip>
        </div>
        <div className="form-stack">
          <label className="field">
            <span>{tr("Retain encrypted result bodies")}</span>
            <select
              aria-label={tr("Encrypted result retention")}
              value={llmPrivacy?.result_retention_seconds ?? 3600}
              onChange={(event) => void updateLlmRetention(Number(event.target.value) as LLMPrivacySettings["result_retention_seconds"])}
            >
              <option value={0}>{tr("Do not retain after first delivery")}</option>
              <option value={3600}>{tr("1 hour")}</option>
              <option value={86400}>{tr("24 hours")}</option>
              <option value={604800}>{tr("7 days")}</option>
            </select>
          </label>
          <div className="service-result">
            <small>
              Stored result bodies remain end-to-end encrypted. The selected Provider necessarily sees plaintext while computing;
              task records exclude prompt text. Ask Ryn conversations are saved separately in encrypted node history.
            </small>
          </div>
          {visibleLlmOrders.length ? visibleLlmOrders.map((order) => (
            <div className="service-result" key={order.task_id}>
              <span>{tr(order.state)} · {order.task_id}</span>
              <small>
                {order.transport ? tr(order.transport) : tr("transport pending")}
                {order.amount !== undefined ? ` · ${order.amount} ${tr("DEV_TASK_BALANCE")}` : ""}
                {order.updated_at ? ` · ${new Date(order.updated_at).toLocaleString(uiLocale())}` : ""}
              </small>
              <Button onClick={() => void viewLlmOrder(order.task_id)}>{tr("View status / retained result")}</Button>
            </div>
          )) : (
            <div className="empty-state"><p>{llmOrders.length ? tr("No task history matches this filter.") : tr("No local LLM task history yet.")}</p></div>
          )}
          <div className="button-row">
            <Button
              disabled={llmHistoryPage <= 1}
              onClick={() => setLlmHistoryPage((page) => Math.max(1, page - 1))}
            >
              {tr("Previous")}
            </Button>
            <Chip mono>{tr("page")} {Math.min(llmHistoryPage, llmHistoryPages)} / {llmHistoryPages}</Chip>
            <Button
              disabled={llmHistoryPage >= llmHistoryPages}
              onClick={() => setLlmHistoryPage((page) => Math.min(llmHistoryPages, page + 1))}
            >
              {tr("Next")}
            </Button>
            <Button variant="danger" disabled={!llmOrders.length} onClick={clearLlmHistory}>
              {tr("Clear completed task history")}
            </Button>
          </div>
        </div>
      </Panel>

      <Panel className="table-panel">
        <div className="panel-head">
          <div>
            <span className="eyebrow">{tr("Published capacity")}</span>
            <h2>{tr("Signal50 Veo providers")}</h2>
          </div>
          <Chip tone={veoServices.length ? "ok" : "warn"}>{veoServices.length} {tr("available")}</Chip>
        </div>
        <div className="table-wrap">
          <table className="peer-table">
            <thead>
              <tr>
                <th>{tr("Provider")}</th>
                <th>{tr("Capability")}</th>
                <th>{tr("Price")}</th>
                <th>{tr("Route")}</th>
                <th>{tr("Updated")}</th>
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
            <span className="eyebrow">{tr("Request form")}</span>
            <h2>{tr("Signal50 Veo render")}</h2>
          </div>
          <Chip tone="info" icon={CloudCog}>{tr("polling relay")}</Chip>
        </div>
        <div className="form-stack">
          <label className="field">
            <span>{tr("Provider")}</span>
            <select value={selectedPeerId || selectedVeo?.peer_id || ""} onChange={(event) => setSelectedPeerId(event.target.value)}>
              {veoServices.map((service) => (
                <option key={service.peer_id} value={service.peer_id}>
                  {service.provider_name || service.node_name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>{tr("Signal50 video ID")}</span>
            <input value={videoId} onChange={(event) => setVideoId(event.target.value)} placeholder="20260518__casefile__example-tt-deep-veo" />
          </label>
          <label className="field">
            <span>{tr("Max scenes")}</span>
            <input value={maxScenes} onChange={(event) => setMaxScenes(event.target.value)} inputMode="numeric" />
          </label>
          <label className="checkbox-row">
            <input type="checkbox" checked={skipExisting} onChange={(event) => setSkipExisting(event.target.checked)} />
            {tr("Skip existing clips")}
          </label>
          <div className="button-row">
            <Button variant="primary" icon={SendHorizontal} onClick={() => void submit()}>
              {tr("Submit request")}
            </Button>
            {lastOrderId ? <Chip mono>{lastOrderId}</Chip> : null}
          </div>
        </div>
      </Panel>

      {lastOrderId ? (
        <Panel>
          <div className="panel-head">
            <h2>{tr("Latest result")}</h2>
            <Button onClick={() => void refresh()}>{tr("Refresh result")}</Button>
          </div>
          {results.length ? (
            <div className="form-stack">
              {results.map((result) => (
                <div className="service-result" key={`${result.work_order_id}-${result.created_at}`}>
                  <Chip tone={result.status === "completed" ? "ok" : result.status === "failed" ? "danger" : "info"}>
                    {tr(result.status)}
                  </Chip>
                  <span>{result.message || tr("No message")}</span>
                  <small className="mono">{result.created_at}</small>
                </div>
              ))}
            </div>
          ) : (
            <div className="empty-state">
              <h3>{tr("No result yet")}</h3>
              <p>{tr("The provider will post accepted, running, completed, or failed messages as it polls.")}</p>
            </div>
          )}
        </Panel>
      ) : null}
    </div>
  );
}
