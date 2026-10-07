import {
  FileText,
  ArrowUp,
  Paperclip,
  Sparkles,
  Check,
  ChevronDown,
  Copy,
  RotateCcw,
  ShieldCheck,
  Square,
  ThumbsUp,
  Trash2,
} from "lucide-react";
import { useContext, useEffect, useMemo, useRef, useState } from "react";
import { tr, uiLocale, useUILanguage } from "../uiI18n";
import { DeviceArt, ServiceArt } from "../personal/components";
import { PersonalContext, personalHref } from "../personal/model";
import WorkspaceSelect from "./WorkspaceSelect";
import ChatMarkdown from "./ChatMarkdown";
import UnifiedAIHistory from "../components/UnifiedAIHistory";
import AIWorkspaceMaterials from "../components/AIWorkspaceMaterials";
import { getNasHandoff, setNasHandoff, nasRequest, type NasStatus } from "../domain/nas";
import { useProviderDiscovery, useServiceOrder } from "../domain/serviceExperience";
import { providerIdentity, serviceDescriptors } from "../domain/serviceDescriptors";
import { LLM_TERMINAL_STATES, llmServiceAvailability, llmServiceRecordKey } from "../domain/llmOrders";
import { Link, useSearchParams } from "react-router-dom";
import { useAppContext } from "../appContext";
import { LoadingPanel } from "../components/ui";
import {
  buildConversationPrompt,
  createConversation,
  titleFromPrompt,
  type LLMChatMessage,
  type LLMConversation,
} from "../domain/llmConversationStore";
import { askHistory, AskRequestError, conversationRepository, type AskPreview, type AskRunRequest } from "../domain/askHistory";
import AskMaterials, { AskAnswerSources } from "../components/AskMaterials";
import type { LLMOrderResult, LLMServiceRecord } from "../domain/nodeClient";
import { useLLMStream } from "../domain/useLLMStream";
import styles from "./PrivateAIChat.module.css";

const TERMINAL_STATES = LLM_TERMINAL_STATES;
const SUGGESTIONS = ["Summarize a document", "Draft a professional email", "Explain a difficult topic"];
const NODE_UNREACHABLE_ERROR = "The node could not be reached. Saved tasks continue on the node; no new request was submitted.";
const ASK_RUN_NOT_FOUND_ERROR = "The node has no record of this task, so nothing is running there. The request was not confirmed; you can send it again.";
const RECOVERY_UNCONFIRMED_ERROR = "The node could not confirm clearing this task. Reload history and try again.";
const RUNNING_MESSAGE_STATUSES = new Set(["queued", "running", "cancel_requested"]);

function serviceKey(service: LLMServiceRecord) {
  // Aliases are display names and are not unique. Scope history by both the
  // provider identity and package ID to prevent cross-provider conversation
  // mixups. Shared with Services so the two screens can never diverge on the
  // identity format (conversation-store keys persist in IndexedDB).
  return llmServiceRecordKey(service);
}

function messageId() {
  return typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : `message_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

function formatTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString(uiLocale(), { hour: "2-digit", minute: "2-digit" });
}

function resultMessage(result: LLMOrderResult) {
  if (result.state === "cancelled") return "Cancellation was recorded. The provider may still be finishing computation.";
  if (result.state === "timed_out") return "The request timed out. Check its original task before submitting another request.";
  if (result.error_code === "runtime_busy" || result.error_code === "capacity_exhausted") return "The provider is busy. Wait for its current request to finish or choose another service.";
  if (result.error_code === "p2p_capacity_exhausted") return "The connection has no free session capacity. Wait for the active session to close, then retry.";
  if (result.error_code === "insufficient_balance") return "There are not enough credits to run this request.";
  if (result.error_code === "p2p_distinct_public_egress_required") return "The provider needs a different public network. Change networks and try again.";
  return result.output || (result.error_code ? `The request failed: ${result.error_code.replaceAll("_", " ")}.` : "The model did not return a response.");
}

export default function PrivateAIChat() {
  useUILanguage();
  const personal = useContext(PersonalContext);
  const [nasHandoff, updateNasHandoff] = useState(getNasHandoff);
  const [materialsOpen, setMaterialsOpen] = useState(false);
  const [materialBusy, setMaterialBusy] = useState(false);
  const [cliModels, setCLIModels] = useState<{ id: string; name: string; default: boolean }[]>([]);
  const [cliModelsError, setCLIModelsError] = useState("");
  const { client, confirm, notify } = useAppContext();
  const history = useMemo(() => conversationRepository(client.mode), [client.mode]);
  const [searchParams, setSearchParams] = useSearchParams();
  const configuredNetwork = useServiceOrder({ scope: client, key: "service-network", intervalMs: 15_000,
    load: async () => (await client.getSettings().catch(() => null))?.network_id?.trim() || "rynmesh-main",
    isTerminal: () => true,
  });
  const discoveryNetwork = searchParams.get("network") || configuredNetwork.data;
  const discovery = useProviderDiscovery<LLMServiceRecord>({ scope: client, key: discoveryNetwork ?? "",
    enabled: Boolean(discoveryNetwork), intervalMs: serviceDescriptors.privateAI.discoveryIntervalMs,
    load: () => client.listLLMServices(discoveryNetwork!),
    identity: (item) => providerIdentity(discoveryNetwork!, item.peer_id, item.service.package_id),
  });
  const [selectedService, setSelectedService] = useState<LLMServiceRecord | null>(null);
  const selectedServiceKeyRef = useRef("");
  selectedServiceKeyRef.current = selectedService ? serviceKey(selectedService) : "";
  const [networkId, setNetworkId] = useState(searchParams.get("network") || "rynmesh-main");
  const activeNetworkRef = useRef(networkId);
  activeNetworkRef.current = networkId;
  const [conversations, setConversations] = useState<LLMConversation[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(true);
  const [historyReady, setHistoryReady] = useState(false);
  const [sending, setSending] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [activeTaskId, setActiveTaskId] = useState("");
  const [unconfirmed, setUnconfirmed] = useState<AskRunRequest | null>(null);
  const [error, setError] = useState("");
  const [storageMode, setStorageMode] = useState<"node-encrypted" | "encrypted" | "session-only">("node-encrypted");
  const [helpfulMessages, setHelpfulMessages] = useState<Set<string>>(new Set());
  const messageScrollRef = useRef<HTMLDivElement>(null);
  const mountedRef = useRef(true);
  // Conversations removed while a generation is in flight: the completion
  // callback must not resurrect them into state or encrypted storage.
  const deletedIdsRef = useRef<Set<string>>(new Set());
  // Stop pressed before submitLLMOrder returned a task id.
  const cancelRequestedRef = useRef(false);

  const fixtureOrder = useServiceOrder<LLMOrderResult>({ scope: client,
    key: providerIdentity(networkId, selectedService?.peer_id ?? "", selectedService?.service.package_id ?? ""),
    enabled: false, load: () => client.getLLMOrder(activeTaskId),
    intervalMs: serviceDescriptors.privateAI.orderIntervalMs,
    isTerminal: (result) => TERMINAL_STATES.has(result.state),
  });

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const selectedConversation = (selectedId ? conversations.find((conversation) => conversation.id === selectedId) : conversations[0]) ?? null;
  const selectedConversationRef = useRef(selectedConversation?.id);
  selectedConversationRef.current = selectedConversation?.id;
  const conversationRef = useRef(selectedConversation);
  conversationRef.current = selectedConversation;
  const nodeTask = client.mode === "live" ? selectedConversation?.messages.find((message) => message.role === "assistant" && RUNNING_MESSAGE_STATUSES.has(message.status))?.taskId : undefined;
  const isSending = sending || Boolean(nodeTask);
  const stream = useLLMStream(nodeTask);
  const localCodex = Boolean(selectedService?.local_only && selectedService.service.adapter === "codex_cli");
  const cliModel = selectedConversation?.cliModel || cliModels.find(row => row.default)?.id || cliModels[0]?.id || "";
  const [nasOrigins, setNasOrigins] = useState<Record<string, NonNullable<import("../domain/askHistory").AskSource["nas"]>>>({});
  const sourceIds = JSON.stringify([...new Set(selectedConversation?.messages.flatMap(message => message.contextIds ?? []) ?? [])]);
  useEffect(() => {
    let active = true;
    setNasOrigins({});
    void Promise.all((JSON.parse(sourceIds) as string[]).map(async id => {
      try { return [id, (await askHistory.context(id)).nas] as const; } catch { return [id, undefined] as const; }
    })).then(rows => { if (active) setNasOrigins(Object.fromEntries(rows.filter(row => row[1])) as typeof nasOrigins); });
    return () => { active = false; };
  }, [sourceIds]);
  useEffect(() => {
    let active = true; setCLIModels([]); setCLIModelsError("");
    if (localCodex) void client.getCLIModels("codex_cli").then(result => { if (active) setCLIModels(result.models); })
      .catch(cause => { if (active) setCLIModelsError(cause instanceof Error ? cause.message : tr("无法读取 Codex 模型")); });
    return () => { active = false; };
  }, [client, localCodex]);


  const refreshNodeHistory = async () => {
    const rows = await history.list(selectedServiceKeyRef.current);
    const filtered = rows.filter((row) => row.serviceKey === selectedServiceKeyRef.current && row.networkId === activeNetworkRef.current);
    if (mountedRef.current) setConversations(filtered);
    return filtered;
  };

  useServiceOrder({ scope: client, key: providerIdentity(networkId, selectedService?.peer_id ?? "", selectedService?.service.package_id ?? ""),
    enabled: client.mode === "live" && historyReady && Boolean(selectedService),
    intervalMs: serviceDescriptors.privateAI.orderIntervalMs,
    load: async () => {
      const key = selectedService ? serviceKey(selectedService) : "";
      return (await history.list(key)).filter((row) => row.serviceKey === key && row.networkId === networkId);
    },
    onData: (rows) => { setConversations(rows); setError((value) => value === NODE_UNREACHABLE_ERROR ? "" : value); },
    onError: () => setError(NODE_UNREACHABLE_ERROR),
  });

  useEffect(() => {
    if (!discoveryNetwork) return;
    let active = true;
    let redirecting = false;
    setLoading(true);
    setHistoryReady(false);
    setConversations([]);
    setSelectedId("");
    void (async () => {
      try {
      const network = discoveryNetwork;
      const discovered = await discovery.refresh() ?? [];
      if (!active) return;
      setNetworkId(network);
      const requestedId = searchParams.get("conversation");
      const requestedRow = requestedId ? (await history.list()).find(row => row.id === requestedId) : null;
      if (!active) return;
      const requestedPeer = searchParams.get("peer") || requestedRow?.providerPeerId;
      const requestedService = searchParams.get("service") || (requestedRow ? requestedRow.serviceKey.slice(requestedRow.providerPeerId.length + 2) : null);
      const selected = requestedPeer && requestedService
        ? discovered.find((item) => item.peer_id === requestedPeer && item.service.package_id === requestedService) ?? null
        : discovered.find((item) => item.online) ?? discovered[0] ?? null;
      setSelectedService(selected);
      setStorageMode(await history.storageMode());
      if (!selected && requestedRow) { setConversations([requestedRow]); setSelectedId(requestedRow.id); setInput(requestedRow.draft ?? ""); }
      if (selected) {
        const key = serviceKey(selected);
        let stored = (await history.list(key)).filter((row) => row.networkId === network);
        if (requestedId && !stored.some((row) => row.id === requestedId)) throw new Error("This conversation is unavailable for this provider and network. It has not been replaced by another history.");
        if ((!stored.length && !requestedId) || searchParams.get("new") === "1" || (!requestedId && (nasHandoff || searchParams.get("material")))) {
          const material = searchParams.get("material");
          if (material && client.mode === "live") await askHistory.context(material);
          const fresh = createConversation({
            serviceKey: key,
            serviceName: selected.service.model_alias,
            providerPeerId: selected.peer_id,
            networkId: network,
          });
          if (material) fresh.contextIds = [material];
          stored = [await history.save(fresh), ...stored];
        }
        if (active) {
          setConversations(stored);
          const opened = searchParams.get("new") === "1" ? stored[0] : stored.find((row) => row.id === requestedId) ?? stored[0];
          setSelectedId(opened.id);
          setInput(opened.draft ?? "");
          setHistoryReady(true);
          if (!requestedId || searchParams.get("new") === "1") { redirecting = true; setSearchParams((prior) => { const next = new URLSearchParams(prior); next.set("conversation", opened.id); next.delete("new"); next.delete("material"); return next; }, { replace: true }); }
        }
      }
      } catch (cause) {
        if (active) setError(cause instanceof Error ? cause.message : "Could not load conversation history.");
      } finally { if (active && !redirecting) setLoading(false); }
    })();
    return () => { active = false; };
  }, [client, history, searchParams, setSearchParams, discoveryNetwork, discovery.refresh]);

  useEffect(() => {
    const element = messageScrollRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [selectedConversation?.messages.length, sending, stream.text]);

  const replaceConversation = async (conversation: LLMConversation, select = true) => {
    const saved = await history.save(conversation);
    if (saved.serviceKey !== selectedServiceKeyRef.current || saved.networkId !== activeNetworkRef.current) return saved;
    setConversations((current) => [
      saved,
      ...current.filter((item) => item.id !== saved.id),
    ].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)));
    if (select) {
      setSelectedId(conversation.id);
      if (searchParams.get("conversation") !== conversation.id) setSearchParams((prior) => { const next = new URLSearchParams(prior); next.set("conversation", conversation.id); return next; });
    }
    return saved;
  };

  const attachSource = async (source: import("../domain/askHistory").AskSource) => {
    if (!selectedConversation || isSending || unconfirmed) throw new Error(tr("Wait for the current task before adding materials."));
    const ids = selectedConversation.contextIds ?? [];
    if (!ids.includes(source.library_id) && ids.length >= 8) throw new Error(tr("You can attach up to eight materials."));
    await replaceConversation({ ...selectedConversation, contextIds: [...new Set([...ids, source.library_id])], updatedAt: new Date().toISOString() }, false);
  };
  const reviewNasHandoff = () => {
    const file = nasHandoff;
    if (!file || !selectedConversation || materialBusy) return;
    confirm({ title: tr("Add this NAS document?"), risk: "low", confirmLabel: tr("Add materials"),
      body: tr("Adding saves a source copy on this node for history and citations. Nothing is sent to AI until you review and confirm."),
      details: [{ label: tr("NAS files"), value: `${file.sourceName} / ${file.path}` }],
      onConfirm: async () => {
        setMaterialBusy(true);
        try { await attachSource(await askHistory.prepareNas(file.sourceId, file.path)); updateNasHandoff(null); setNasHandoff(null); setInput(tr("Summarize this document.")); }
        catch (cause) { setError(cause instanceof Error ? cause.message : tr("Could not attach this material.")); }
        finally { setMaterialBusy(false); }
      } });
  };
  const saveToNas = async (message: LLMChatMessage) => {
    try {
      const id = message.contextIds?.find(sourceId => nasOrigins[sourceId]);
      if (!id) return;
      const origin = (await askHistory.context(id)).nas;
      if (!origin) { setError(tr("This source is not a NAS document.")); return; }
      const status = await (await nasRequest()).json() as NasStatus;
      const connection = status.sources.find(row => row.id === origin.source_id);
      if (!status.enabled || !connection?.writable) { setError(tr("This NAS connection is unavailable or read-only.")); return; }
      const folder = origin.path.split("/").slice(0, -1).join("/");
      const path = `${folder ? folder + "/" : ""}ryn-response-${crypto.randomUUID()}.txt`;
      confirm({ title: tr("Save response to NAS?"), risk: "low", confirmLabel: tr("Save"),
        body: `${connection.name} / ${path}`,
        onConfirm: async () => { await nasRequest(`/sources/${encodeURIComponent(origin.source_id)}/content?path=${encodeURIComponent(path)}`, { method: "PUT", body: message.content }); notify("ok", tr("Response saved to NAS")); } });
    } catch (cause) { setError(cause instanceof Error ? cause.message : tr("Could not save response to NAS.")); }
  };

  const newConversation = async () => {
    if (!selectedService) return;
    setLoading(true);
    const fresh = createConversation({
      serviceKey: serviceKey(selectedService),
      serviceName: selectedService.service.model_alias,
      providerPeerId: selectedService.peer_id,
      networkId,
    });
    try { await replaceConversation(fresh); }
    catch (cause) { setLoading(false); throw cause; }
    setInput("");
    setError("");
  };

  const removeConversation = async (conversationId: string) => {
    const row = conversations.find((item) => item.id === conversationId);
    if (!row) return;
    await history.remove(row);
    deletedIdsRef.current.add(conversationId);
    const remaining = conversations.filter((conversation) => conversation.id !== conversationId);
    if (remaining.length) {
      setConversations(remaining);
      if (selectedId === conversationId) setSearchParams((prior) => { const next = new URLSearchParams(prior); next.set("conversation", remaining[0].id); return next; });
    } else {
      setConversations([]);
      setSelectedId("");
      await newConversation();
    }
  };

  const clearHistory = () => {
    if (!selectedService) return;
    confirm({
      title: "Clear Private AI conversation history?",
      body: `This removes conversation history for this provider, service and network from the node. ${conversations.some((row) => row.sync) ? "Approved devices receive synchronized deletions when connected; concurrent replies may remain in recovery. " : ""}Retained order results and older browser recovery copies are separate. Running requests may continue.`,
      risk: "high",
      confirmLabel: "Clear history",
      onConfirm: async () => {
        await history.clear(serviceKey(selectedService), networkId);
        conversations.forEach((row) => deletedIdsRef.current.add(row.id));
        setConversations([]);
        setSelectedId("");
        await newConversation();
        notify("ok", "Private AI history cleared");
      },
    });
  };

  const submitReviewed = async (request: AskRunRequest) => {
    setSending(true); setActiveTaskId(request.task_id); setError("");
    setUnconfirmed(request);
    try {
      await askHistory.beginRun(request);
      if (mountedRef.current) { setUnconfirmed(null); setInput((current) => current.trim() === request.question ? "" : current); }
      if (cancelRequestedRef.current) { await askHistory.cancelRun(request.task_id); cancelRequestedRef.current = false; }
      await refreshNodeHistory();
    } catch (cause) {
      if (mountedRef.current && cause instanceof AskRequestError && [400, 409].includes(cause.status)) setUnconfirmed(null);
      if (mountedRef.current) setError(cause instanceof Error ? cause.message : "The node did not confirm this request. Check the original task or retry the same reviewed request.");
    } finally {
      if (mountedRef.current) { setSending(false); setActiveTaskId(""); }
    }
  };

  const checkOriginal = async (taskId: string) => {
    if (client.mode === "live") {
      try {
        const run = await askHistory.run(taskId);
        await refreshNodeHistory();
        if (unconfirmed?.task_id === taskId) setInput((current) => current.trim() === unconfirmed.question ? "" : current);
        setUnconfirmed((prior) => prior?.task_id === taskId ? null : prior);
        setError(`Original task: ${run.state}. No new request was submitted.`);
      } catch (cause) { setError(cause instanceof Error ? cause.message : "The original task could not be verified. No new request was submitted."); }
    } else {
      void client.getLLMOrder(taskId).then((result) => setError(`Original task ${result.state}. ${resultMessage(result)} No new request was submitted.`))
        .catch(() => setError("The original task could not be verified. No new request was submitted."));
    }
  };

  const runPrompt = async (promptText: string, preview?: AskPreview) => {
    const text = promptText.trim();
    if (!text || !selectedService || isSending || unconfirmed || !historyReady) return;
    let conversation = conversationRef.current;
    if (client.mode === "live" && (!preview || preview.conversation_id !== selectedConversationRef.current || preview.provider_peer_id + "::" + preview.service_id !== selectedServiceKeyRef.current || preview.revision !== conversation?.revision)) {
      setError("This send review is out of date. Review the current conversation before sending.");
      return;
    }
    if (conversation && (conversation.serviceKey !== serviceKey(selectedService) || conversation.networkId !== networkId)) {
      setError("This conversation belongs to another provider. Open a separate conversation for the selected service.");
      return;
    }
    if (client.mode === "live" && preview) {
      cancelRequestedRef.current = false;
      return submitReviewed({ task_id: "task_" + crypto.randomUUID().replaceAll("-", ""), conversation_id: preview.conversation_id,
        expected_revision: preview.revision, question: text, prompt_sha256: preview.prompt_sha256,
        ...(preview.ai_permission ? { ai_permission: preview.ai_permission } : {}) });
    }
    if (!conversation) {
      conversation = createConversation({
        serviceKey: serviceKey(selectedService),
        serviceName: selectedService.service.model_alias,
        providerPeerId: selectedService.peer_id,
        networkId,
      });
    }
    const now = new Date().toISOString();
    const taskId = "task_" + messageId().replaceAll("-", "");
    const userMessage: LLMChatMessage = { id: messageId(), role: "user", content: text, createdAt: now, status: "complete", taskId };
    let withUser: LLMConversation = {
      ...conversation, draft: "",
      title: conversation.messages.length ? conversation.title : titleFromPrompt(text),
      updatedAt: now,
      messages: [...conversation.messages, userMessage],
    };
    setError("");
    setSending(true);
    cancelRequestedRef.current = false;
    let userSaved = false;
    try {
      withUser = await replaceConversation(withUser);
      userSaved = true;
      setInput("");
      let result = await client.submitLLMOrder({
        ...(localCodex ? { cli_model: cliModel } : {}),
        task_id: taskId, idempotency_key: taskId,
        network_id: networkId,
        provider_peer_id: selectedService.peer_id,
        service_id: selectedService.service.package_id,
        prompt: preview?.prompt ?? buildConversationPrompt(withUser.messages),
        max_tokens: preview?.max_output_tokens ?? Math.min(selectedService.service.max_output_tokens || 256, 256),
        transport: "auto",
      });
      setActiveTaskId(result.task_id);
      if (cancelRequestedRef.current) {
        // Stop was pressed while the submit call was still in flight; the
        // task id only just became known, so deliver the cancellation now.
        cancelRequestedRef.current = false;
        await client.cancelLLMOrder(result.task_id).catch(() => null);
      }
      // Orders are asynchronous at the node boundary. Keep polling centralized
      // here so the UI never bypasses node transport, settlement, or cancellation.
      const originalTaskId = result.task_id;
      result = await fixtureOrder.awaitTerminal(result, () => client.getLLMOrder(originalTaskId));
      if (!mountedRef.current) return;
      const success = result.state === "succeeded";
      const assistantMessage: LLMChatMessage = {
        id: messageId(),
        role: "assistant",
        content: resultMessage(result),
        createdAt: new Date().toISOString(),
        status: success ? "complete" : result.state === "cancelled" ? "cancelled" : "failed",
        taskId: result.task_id,
        inputTokens: result.input_tokens,
        outputTokens: result.output_tokens,
        cost: result.amount,
        ...(preview ? { contextIds: preview.sources.map((source) => source.library_id), contextBytes: preview.sources.map((source) => source.included_bytes ?? 0), promptSha256: preview.prompt_sha256 } : {}),
      };
      const completed = {
        ...withUser,
        updatedAt: assistantMessage.createdAt,
        messages: [...withUser.messages, assistantMessage],
      };
      if (deletedIdsRef.current.has(withUser.id)) return;
      await replaceConversation(completed, false);
      notify(success ? "ok" : "warn", success ? "Private AI response complete" : `Private AI request ${result.state}`);
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : "Private AI request failed";
      const failedMessage: LLMChatMessage = {
        id: messageId(), role: "assistant", content: message, createdAt: new Date().toISOString(), status: "failed", taskId,
      };
      if (mountedRef.current && !deletedIdsRef.current.has(withUser.id)) {
        if (userSaved) {
          try { await replaceConversation({ ...withUser, updatedAt: failedMessage.createdAt, messages: [...withUser.messages, failedMessage] }, false); }
          catch { setInput(text); }
        } else setInput(text);
        setError(message);
        notify("danger", message);
      }
    } finally {
      if (mountedRef.current) {
        setSending(false);
        setActiveTaskId("");
      }
    }
  };

  // A resumed-from-history running message keeps `isSending` true (`nodeTask`
  // derives from message status) even after `sending`/`activeTaskId` are
  // cleared, so once the node reports ask_run_not_found the stale message
  // itself must be rewritten to `interrupted` — through the normal save
  // path, against the node's current revision, so a reload does not
  // resurrect it as still running and a stale local copy does not clobber a
  // newer one from another device. Every await here is guarded: nothing may
  // escape into an unhandled rejection from the click handler that calls this.
  const recoverInterruptedTask = async (taskId: string) => {
    try {
      let rows = await refreshNodeHistory();
      for (let attempt = 0; attempt < 2; attempt += 1) {
        if (!mountedRef.current) return;
        const conversation = rows.find((row) => row.messages.some((message) => message.taskId === taskId));
        const stale = conversation?.messages.find((message) => message.taskId === taskId && RUNNING_MESSAGE_STATUSES.has(message.status));
        if (!conversation || !stale || deletedIdsRef.current.has(conversation.id)) {
          // Already resolved (by this recovery, another device, or deletion).
          setError(ASK_RUN_NOT_FOUND_ERROR);
          return;
        }
        const messages = conversation.messages.map((message) => message === stale
          ? { ...message, status: "interrupted" as const, content: "The node has no record of this task; nothing is running there." }
          : message);
        try {
          await replaceConversation({ ...conversation, messages }, false);
          setError(ASK_RUN_NOT_FOUND_ERROR);
          return;
        } catch (saveError) {
          if (attempt === 0 && saveError instanceof AskRequestError && saveError.code === "ask_revision_conflict") {
            rows = await refreshNodeHistory();
            continue;
          }
          if (mountedRef.current) setError(RECOVERY_UNCONFIRMED_ERROR);
          return;
        }
      }
    } catch {
      if (mountedRef.current) setError(RECOVERY_UNCONFIRMED_ERROR);
    }
  };

  const stopGeneration = async () => {
    if (client.mode === "live") {
      cancelRequestedRef.current = true;
      const taskId = nodeTask || activeTaskId;
      if (taskId) {
        try { await askHistory.cancelRun(taskId); cancelRequestedRef.current = false; await refreshNodeHistory(); }
        catch (cause) {
          if (cause instanceof AskRequestError && cause.code === "ask_run_not_found") {
            setSending(false);
            setActiveTaskId("");
            await recoverInterruptedTask(taskId);
          } else {
            setError("Cancellation has not been confirmed. The task may still be running; check it again.");
          }
        }
      }
      return;
    }
    if (!activeTaskId) {
      // The submit call has not returned a task id yet. Record the intent so
      // the cancellation is delivered the moment the id exists — otherwise
      // Stop during a slow submit was a silent no-op and the user paid for a
      // generation they explicitly stopped.
      if (sending) cancelRequestedRef.current = true;
      return;
    }
    await client.cancelLLMOrder(activeTaskId).catch(() => null);
  };

  const retryLast = () => {
    const messages = selectedConversation?.messages ?? [];
    const lastUser = [...messages].reverse().find((message) => message.role === "user");
    if (lastUser?.taskId) {
      void checkOriginal(lastUser.taskId);
    } else if (lastUser) setInput(lastUser.content);
  };

  const reviewAndSend = async () => {
    if (!selectedConversation || !selectedService || !input.trim() || isSending || unconfirmed || reviewing || !historyReady || materialBusy) return;
    if (localCodex && !cliModels.some(row => row.id === cliModel)) { setError(tr("Choose an available CLI model before sending.")); return; }
    if (client.mode !== "live") return runPrompt(input);
    const question = input.trim();
    setReviewing(true); setError("");
    try {
      const row = localCodex && selectedConversation.cliModel !== cliModel ? await replaceConversation({ ...selectedConversation, cliModel }, false) : selectedConversation;
      const preview = await askHistory.preview(row, question);
      if (row.id !== selectedConversationRef.current) return;
      confirm({ title: tr("Send to {{service}}?", { service: selectedService.service.model_alias }), risk: "medium", confirmLabel: tr("Send reviewed question"),
        body: tr("Recipient: {{peer}}. Conservative input estimate {{input}}, framing reserve {{framing}}, output reserve {{output}}, context window {{window}}. {{omitted}} older history messages omitted.", { peer: preview.provider_peer_id, input: preview.input_token_upper_estimate, framing: preview.framing_reserve, output: preview.max_output_tokens, window: preview.context_window, omitted: preview.history_messages_omitted }) + tr(preview.sources.length ? " Article text is treated as untrusted material." : " No article material is attached."),
        details: [{ label: tr("Question"), value: question }, ...(preview.cli_model ? [{ label: tr("模型"), value: preview.cli_model }] : []), ...preview.sources.map((source) => ({ label: tr("Source {{number}}: {{title}}", { number: source.source_number, title: source.title }), value: `${source.nas ? `${source.nas.source_name} / ${source.nas.path}` : source.source_url || tr("Private local document")} · ${source.included_bytes}/${source.text_bytes} ${tr("bytes")}${source.budget_truncated || source.extraction_truncated ? ` · ${tr("TRUNCATED")}` : ""}` }))],
        onConfirm: () => runPrompt(question, preview),
      });
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not prepare the send review."); }
    finally { setReviewing(false); }
  };

  const providerName = personal?.resolveName(selectedService?.peer_id ?? "", selectedService?.node_name) || selectedService?.node_name || selectedService?.peer_id || "";
  const changeService = (service: LLMServiceRecord) => {
    if (isSending || reviewing || unconfirmed) return;
    confirm({ title: tr("Start a separate conversation?"), risk: "low", confirmLabel: tr("新建对话"),
      body: tr("The new conversation starts with this service. Existing messages and materials stay with their original provider."),
      onConfirm: () => { const next = new URLSearchParams(searchParams); next.set("peer", service.peer_id); next.set("service", service.service.package_id); next.set("new", "1"); next.delete("conversation"); next.delete("material"); setSearchParams(next); } });
  };
  const services = discovery.data ?? [];
  const blocked = isSending || reviewing || Boolean(unconfirmed) || materialBusy;
  const selectedStorage = client.mode === "live" ? "node" : "browser";
  const toolbar = <header className={styles.pageHeading}>
    <div className={styles.modelLockup}><ServiceArt kind="ai" /><div><h1>{tr("AI 工作台")}</h1><p>{tr("连接你的设备，与熟悉的 AI 一起工作。")}</p></div></div>
    <details className={styles.details}><summary className={styles.detailsButton}><ShieldCheck size={15} />{tr("会话详情")}<ChevronDown size={14} /></summary><div className={styles.detailsPanel}>
      <strong>{providerName}</strong><p>{tr("Requests go to the selected service. Node history and browser originals keep their separate storage locations.")}</p>
      <Link to={personalHref("/ask/history", client.mode === "fixture")}>{tr("Manage node history")}</Link>
      {selectedConversation ? <button type="button" onClick={() => confirm({ title: "Delete this conversation?", body: `${selectedConversation.sync ? "Approved devices receive synchronized deletions. " : ""}This removes this node's conversation history. Browser originals and retained orders are separate.`, risk: "high", confirmLabel: "Delete conversation", onConfirm: () => removeConversation(selectedConversation.id) })} aria-label={`Delete ${selectedConversation.title}`}><Trash2 size={14} />{tr("Delete conversation")}</button> : null}
    </div></details>
  </header>;

  if (loading) return <LoadingPanel label={tr("Opening Private AI")} />;
  if (!selectedService) return <div className={`${styles.page} ai-unified-workspace`}>{toolbar}
    <UnifiedAIHistory rows={conversations} selectedId={selectedId} storage={selectedStorage} onNew={() => setSearchParams(new URLSearchParams({ network: networkId, new: "1" }))} />
    <main className={styles.workspace}><div className={styles.messages}>
      <h2>{tr("The selected provider is unavailable")}</h2><p>{tr("Your conversation remains bound to its original provider. Choose another service to start a separate conversation.")}</p>
      <Link to={personalHref("/services/sources", client.mode === "fixture")}>{tr("AI 来源")}</Link>{selectedConversation?.messages.map(message => <div className={styles.messageBlock} key={message.id}><div className={styles.messageBubble}><ChatMarkdown>{message.content}</ChatMarkdown></div>{message.contextIds?.length ? <AskAnswerSources ids={message.contextIds} byteLimits={message.contextBytes} /> : null}</div>)}
      {error ? <p role="alert">{error}</p> : null}
    </div></main></div>;

  return <div className={`${styles.page} ai-unified-workspace`}>
    {toolbar}
    <div className={styles.sourceBar}>
      <WorkspaceSelect grow caption={tr("运行设备")} label={tr("Processing device")} value={selectedService.peer_id} disabled={blocked} icon={<DeviceArt kind={personal?.devices.find(device => device.id === selectedService.peer_id)?.kind || "desktop"} small />}
        options={[selectedService, ...services].filter((item, index, all) => all.findIndex(other => other.peer_id === item.peer_id) === index).map(item => ({ value: item.peer_id, label: personal?.resolveName(item.peer_id, item.node_name) || item.node_name || item.peer_id, detail: tr(item.access === "friend" ? "Friend AI · authorized" : item.local_only ? "This device" : item.online ? "Online" : "Offline") }))}
        onChange={value => { const next = services.find(item => item.peer_id === value); if (next) changeService(next); }} />
      <WorkspaceSelect caption={tr("AI 服务")} label={tr("AI 服务")} value={serviceKey(selectedService)} disabled={blocked} icon={<Sparkles size={23} />}
        options={[selectedService, ...services].filter((item, index, all) => item.peer_id === selectedService.peer_id && all.findIndex(other => serviceKey(other) === serviceKey(item)) === index).map(item => ({ value: serviceKey(item), label: item.service.adapter === "codex_cli" ? "ChatGPT" : item.service.model_alias }))}
        onChange={key => { const next = services.find(item => serviceKey(item) === key); if (next) changeService(next); }} />
      {localCodex ? <WorkspaceSelect caption={tr("模型")} label={tr("Codex 模型")} value={cliModel} disabled={blocked || !cliModels.length} placeholder={tr("正在读取模型…")} options={cliModels.map(model => ({ value: model.id, label: model.name }))} onChange={value => { if (selectedConversation) void replaceConversation({ ...selectedConversation, cliModel: value }, false).catch((cause: Error) => setError(cause.message)); }} /> : null}
      <Link className={styles.apiLink} to={personalHref("/services/api", client.mode === "fixture")}>{tr("API 接入")}</Link>
      {cliModelsError ? <p role="alert">{cliModelsError}</p> : null}
    </div>
    <UnifiedAIHistory selectedId={selectedConversation?.id} storage={selectedStorage} rows={conversations} serviceScope={{ key: serviceKey(selectedService), network: networkId }} onNew={() => void newConversation().catch((cause: Error) => setError(cause.message))}
      actions={<button className={styles.clearButton} type="button" onClick={clearHistory}><Trash2 size={14} />{tr("Clear history")}</button>} />
    <main className={styles.workspace}>
      <header className={styles.workspaceHeading}><div><span>{tr("当前对话")}</span><strong>{selectedConversation?.title === "New conversation" ? tr("开启一段新对话") : selectedConversation?.title}</strong></div><span className={styles.connectionBadge} data-failed={!selectedService.online}>{tr(llmServiceAvailability(selectedService))}</span></header>
      {selectedConversation?.sync && (selectedConversation.sync.conflict || selectedConversation.sync.deferred) ? <p role="status">{tr("Other-device changes are saved for review.")} <Link to={`/ask/history?${new URLSearchParams({ conversation: selectedConversation.id, network: selectedConversation.networkId })}`}>{tr("Review conversation branches")}</Link></p> : null}
      <div className={styles.messages} ref={messageScrollRef}>
        {!selectedConversation?.messages.length ? <div className={styles.welcome}><h2>{tr("今天，想一起做些什么？")}</h2><p>{tr("想法、问题，或一段需要打磨的文字，都可以从这里开始。")}</p><div className={styles.suggestions}>{SUGGESTIONS.map(suggestion => <button type="button" key={suggestion} onClick={() => setInput(tr(suggestion))}>{tr(suggestion)}</button>)}</div></div> : selectedConversation.messages.map(message => <div className={`${styles.messageRow}${message.role === "user" ? ` ${styles.messageRowUser}` : ""}`} key={message.id}>
          {message.role === "assistant" ? <span className={styles.assistantAvatar}><ServiceArt kind="ai" small /></span> : null}
          <div className={styles.messageBlock}><div className={`${styles.messageBubble}${message.status === "failed" ? ` ${styles.messageFailed}` : ""}`}>{message.role === "assistant" ? <ChatMarkdown>{message.taskId === nodeTask && stream.text ? stream.text : message.content}</ChatMarkdown> : message.content}</div>
            {message.role === "assistant" && message.taskId === nodeTask && stream.text ? <span className={styles.messageMeta} role="status">{stream.interrupted ? "Live updates paused. This is a partial answer; checking the original task." : "Generating · partial answer"}</span> : null}
            <span className={styles.messageMeta}>{formatTime(message.createdAt)}{message.cost !== undefined ? ` · ${message.cost} credits` : ""}</span>
            {message.role === "assistant" ? <div className={styles.messageActions}><button type="button" onClick={() => void navigator.clipboard?.writeText(message.taskId === nodeTask && stream.text ? stream.text : message.content)}><Copy size={12} />{tr("Copy")}</button><button type="button" onClick={() => setHelpfulMessages(current => new Set(current).add(message.id))}>{helpfulMessages.has(message.id) ? <Check size={12} /> : <ThumbsUp size={12} />}{tr(helpfulMessages.has(message.id) ? "Helpful" : "Good response")}</button>{message.status !== "complete" ? <button type="button" onClick={() => message.taskId ? void checkOriginal(message.taskId) : retryLast()}><RotateCcw size={12} />{tr("Check original task")}</button> : null}{message.status === "complete" && message.contextIds?.some(id => nasOrigins[id]) ? <button type="button" onClick={() => void saveToNas(message)}>{tr("Save to NAS")}</button> : null}</div> : null}
            {message.contextIds?.length ? <AskAnswerSources ids={message.contextIds} byteLimits={message.contextBytes} /> : null}
          </div>
        </div>)}
        {sending ? <div className={styles.messageRow}><span className={styles.assistantAvatar}><ServiceArt kind="ai" small /></span><div className={styles.thinking} aria-label="Private AI is thinking"><span /><span /><span /></div></div> : null}
      </div>
      <div className={styles.composerWrap}>
        {nasHandoff ? <div className="ai-pending-nas"><FileText size={15} /><span>{nasHandoff.sourceName} / {nasHandoff.path}</span><button type="button" disabled={blocked} onClick={reviewNasHandoff}>{tr("Add materials")}</button><button type="button" onClick={() => { updateNasHandoff(null); setNasHandoff(null); }}>{tr("Remove document")}</button></div> : null}
        {selectedConversation?.contextIds?.length ? <AskMaterials ids={selectedConversation.contextIds} onRemove={async id => { if (blocked) throw new Error(tr("Wait for the current task before adding materials.")); await replaceConversation({ ...selectedConversation, contextIds: selectedConversation.contextIds?.filter(value => value !== id) }, false); }} /> : null}
        {error ? <div className={styles.error} role="alert">{error}</div> : null}
        {historyReady && !selectedConversation ? <div role="alert">This conversation is no longer available. Your input has been kept; open its history or create a new conversation.</div> : null}
        {unconfirmed ? <div role="status">The node has not confirmed this reviewed request for conversation {unconfirmed.conversation_id}.<button type="button" disabled={sending} onClick={() => void checkOriginal(unconfirmed.task_id)}>Check original task</button><button type="button" disabled={sending} onClick={() => void submitReviewed(unconfirmed)}>Retry same reviewed request</button></div> : null}
        <div className={styles.composer}><textarea aria-label={tr("Message Private AI")} value={input} onChange={event => setInput(event.target.value)} placeholder={tr("输入你的问题，让想法继续…")} rows={2} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void reviewAndSend(); } }} />
          <div className={styles.composerControls}><button className="ai-attach-button" type="button" disabled={blocked || !historyReady || client.mode !== "live"} onClick={() => setMaterialsOpen(true)}><Paperclip size={15} />{tr("Add materials")}</button><span className={styles.composerModel}>{localCodex ? cliModels.find(model => model.id === cliModel)?.name : selectedService.service.model_alias}</span><span className={styles.keyboardHint}>{tr("Enter 发送 · Shift + Enter 换行")}</span>{isSending ? <button className={styles.stopButton} type="button" aria-label={tr("Stop generating")} onClick={() => void stopGeneration()}><Square size={15} /></button> : <button className={styles.sendButton} type="button" aria-label={tr("Send message")} disabled={!input.trim() || !historyReady || blocked || (localCodex && !cliModels.some(model => model.id === cliModel))} onClick={() => void reviewAndSend()}><ArrowUp size={19} /></button>}</div>
        </div>
        <div className={styles.composerMeta}><span><ShieldCheck size={12} />{tr(storageMode === "node-encrypted" ? "Encrypted history on your node" : storageMode === "encrypted" ? "Encrypted in this browser" : "History kept for this session")}</span><span>{tr("Processed by {{device}}", { device: providerName })}</span></div>
      </div>
    </main>
    {materialsOpen ? <AIWorkspaceMaterials onSelect={attachSource} onClose={() => setMaterialsOpen(false)} /> : null}
  </div>;
}
