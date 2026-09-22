import {
  Bot,
  Check,
  ChevronDown,
  Copy,
  LockKeyhole,
  MessageSquarePlus,
  RotateCcw,
  Search,
  SendHorizontal,
  ShieldCheck,
  Square,
  ThumbsUp,
  Trash2,
} from "lucide-react";
import { useContext, useEffect, useMemo, useRef, useState } from "react";
import { LLM_TERMINAL_STATES, llmServiceRecordKey } from "../domain/llmOrders";
import { Link, useSearchParams } from "react-router-dom";
import { useAppContext } from "../appContext";
import { LoadingPanel } from "../components/ui";
import {
  buildConversationPrompt,
  clearConversations,
  conversationStorageMode,
  createConversation,
  deleteConversation,
  listConversations,
  saveConversation,
  titleFromPrompt,
  type LLMChatMessage,
  type LLMConversation,
} from "../domain/llmConversationStore";
import type { LLMOrderResult, LLMServiceRecord } from "../domain/nodeClient";
import styles from "./PrivateAIChat.module.css";
import { personalHref, PersonalContext } from "../personal/model";
import { DeviceArt, ServiceArt } from "../personal/components";

const TERMINAL_STATES = LLM_TERMINAL_STATES;
const SUGGESTIONS = [
  "Summarize a document",
  "Draft a professional email",
  "Explain a difficult topic",
];

function serviceKey(service: LLMServiceRecord) {
  // Aliases are display names and are not unique. Scope history by both the
  // provider identity and package ID to prevent cross-provider conversation
  // mixups. Shared with Services so the two screens can never diverge on the
  // identity format (conversation-store keys persist in IndexedDB).
  return llmServiceRecordKey(service);
}

function messageId() {
  return typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `message_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

function formatTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function historyBucket(value: string) {
  const date = new Date(value);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const itemDay = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const days = Math.round((today.getTime() - itemDay.getTime()) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days <= 7) return "Previous 7 days";
  return "Older";
}

function resultMessage(result: LLMOrderResult) {
  if (result.state === "cancelled") return "Generation stopped.";
  if (result.state === "timed_out")
    return "The model took too long to respond. Try again.";
  if (result.error_code === "insufficient_balance")
    return "There are not enough credits to run this request.";
  if (result.error_code === "p2p_distinct_public_egress_required")
    return "Public-network test mode requires different internet connections. Turn off that test setting for normal use.";
  if (result.error_code === "p2p_connection_timed_out")
    return "Could not connect directly to this device. Keep Ryn open on both computers and check that the networks allow UDP. Relay is not enabled in this build's default configuration.";
  if (result.error_code === "p2p_public_mapping_unavailable")
    return "Could not obtain a public connection address. Check the STUN server and whether the network allows UDP.";
  if (result.error_code === "p2p_transport_failed")
    return "The peer connection failed. Check the other device and its network, then retry. Retrying discovers its current address.";
  return (
    result.output ||
    (result.error_code
      ? `The request failed: ${result.error_code.replaceAll("_", " ")}.`
      : "The model did not return a response.")
  );
}

export default function PrivateAIChat() {
  const { client, confirm, notify } = useAppContext();
  const [searchParams, setSearchParams] = useSearchParams();
  const personal = useContext(PersonalContext);
  const [services, setServices] = useState<LLMServiceRecord[]>([]);
  const [selectedService, setSelectedService] =
    useState<LLMServiceRecord | null>(null);
  const [networkId, setNetworkId] = useState(
    searchParams.get("network") || "rynmesh-main",
  );
  const [conversations, setConversations] = useState<LLMConversation[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [query, setQuery] = useState("");
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [activeTaskId, setActiveTaskId] = useState("");
  const [connectionStatus, setConnectionStatus] = useState("");
  const [error, setError] = useState("");
  const [storageMode, setStorageMode] = useState<"encrypted" | "session-only">(
    "encrypted",
  );
  const [helpfulMessages, setHelpfulMessages] = useState<Set<string>>(
    new Set(),
  );
  const messageScrollRef = useRef<HTMLDivElement>(null);
  const mountedRef = useRef(true);
  // Conversations removed while a generation is in flight: the completion
  // callback must not resurrect them into state or encrypted storage.
  const deletedIdsRef = useRef<Set<string>>(new Set());
  // Stop pressed before submitLLMOrder returned a task id.
  const cancelRequestedRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const selectedConversation =
    conversations.find((conversation) => conversation.id === selectedId) ??
    conversations[0] ??
    null;

  useEffect(() => {
    let active = true;
    setLoading(true);
    void (async () => {
      const settings = await client.getSettings().catch(() => null);
      const network =
        searchParams.get("network") ||
        settings?.network_id?.trim() ||
        "rynmesh-main";
      const discovered = await client.listLLMServices(network).catch(() => []);
      if (!active) return;
      setNetworkId(network);
      setServices(discovered);
      const requestedPeer = searchParams.get("peer");
      const requestedService = searchParams.get("service");
      const selected =
        requestedPeer || requestedService
          ? (discovered.find(
              (item) =>
                item.peer_id === requestedPeer &&
                item.service.package_id === requestedService,
            ) ?? null)
          : (discovered.find((item) => item.online) ?? discovered[0] ?? null);
      setSelectedService(selected);
      setStorageMode(await conversationStorageMode());
      if (selected) {
        const key = serviceKey(selected);
        let stored = await listConversations(key);
        if (!stored.length) {
          const fresh = createConversation({
            serviceKey: key,
            serviceName: selected.service.model_alias,
            providerPeerId: selected.peer_id,
            networkId: network,
          });
          await saveConversation(fresh);
          stored = [fresh];
        }
        if (active) {
          setConversations(stored);
          setSelectedId(stored[0].id);
        }
      }
      if (active) setLoading(false);
    })();
    return () => {
      active = false;
    };
  }, [client, searchParams]);

  useEffect(() => {
    const element = messageScrollRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [selectedConversation?.messages.length, sending]);

  useEffect(() => {
    if (!selectedService) return;
    let active = true;
    const timer = window.setInterval(() => {
      void client
        .listLLMServices(networkId)
        .then((discovered) => {
          if (!active) return;
          setServices(discovered);
          setSelectedService((current) =>
            current
              ? discovered.find(
                  (item) => serviceKey(item) === serviceKey(current),
                ) || { ...current, online: false }
              : null,
          );
        })
        .catch(() => {
          if (active)
            setSelectedService((current) =>
              current ? { ...current, online: false } : null,
            );
        });
    }, 15000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [
    client,
    networkId,
    selectedService?.peer_id,
    selectedService?.service.package_id,
  ]);

  const grouped = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const visible = conversations.filter(
      (conversation) =>
        !needle || conversation.title.toLowerCase().includes(needle),
    );
    return visible.reduce<Record<string, LLMConversation[]>>(
      (groups, conversation) => {
        const bucket = historyBucket(conversation.updatedAt);
        (groups[bucket] ??= []).push(conversation);
        return groups;
      },
      {},
    );
  }, [conversations, query]);

  const replaceConversation = async (conversation: LLMConversation) => {
    setConversations((current) =>
      [
        conversation,
        ...current.filter((item) => item.id !== conversation.id),
      ].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)),
    );
    setSelectedId(conversation.id);
    await saveConversation(conversation);
  };

  const newConversation = async () => {
    if (!selectedService) return;
    const fresh = createConversation({
      serviceKey: serviceKey(selectedService),
      serviceName: selectedService.service.model_alias,
      providerPeerId: selectedService.peer_id,
      networkId,
    });
    await replaceConversation(fresh);
    setInput("");
    setError("");
  };

  const removeConversation = async (conversationId: string) => {
    deletedIdsRef.current.add(conversationId);
    await deleteConversation(conversationId);
    const remaining = conversations.filter(
      (conversation) => conversation.id !== conversationId,
    );
    if (remaining.length) {
      setConversations(remaining);
      if (selectedId === conversationId) setSelectedId(remaining[0].id);
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
      body: "This permanently removes locally encrypted conversations and retained LLM results. Running requests are not affected.",
      risk: "high",
      confirmLabel: "Clear history",
      onConfirm: async () => {
        await clearConversations(serviceKey(selectedService));
        await client.clearLLMOrders().catch(() => ({ ok: false, removed: 0 }));
        setConversations([]);
        setSelectedId("");
        await newConversation();
        notify("ok", "Private AI history cleared");
      },
    });
  };

  const runPrompt = async (promptText: string) => {
    const text = promptText.trim();
    if (!text || !selectedService || !selectedService.online || sending) return;
    let conversation = selectedConversation;
    if (!conversation) {
      conversation = createConversation({
        serviceKey: serviceKey(selectedService),
        serviceName: selectedService.service.model_alias,
        providerPeerId: selectedService.peer_id,
        networkId,
      });
    }
    const now = new Date().toISOString();
    const userMessage: LLMChatMessage = {
      id: messageId(),
      role: "user",
      content: text,
      createdAt: now,
      status: "complete",
    };
    const withUser: LLMConversation = {
      ...conversation,
      title: conversation.messages.length
        ? conversation.title
        : titleFromPrompt(text),
      updatedAt: now,
      messages: [...conversation.messages, userMessage],
    };
    setInput("");
    setError("");
    setSending(true);
    setConnectionStatus("Finding device…");
    cancelRequestedRef.current = false;
    await replaceConversation(withUser);

    try {
      let result = await client.submitLLMOrder({
        network_id: networkId,
        provider_peer_id: selectedService.peer_id,
        service_id: selectedService.service.package_id,
        prompt: buildConversationPrompt(withUser.messages),
        max_tokens: Math.min(
          selectedService.service.max_output_tokens || 256,
          256,
        ),
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
      while (mountedRef.current && !TERMINAL_STATES.has(result.state)) {
        await new Promise((resolve) => window.setTimeout(resolve, 650));
        result = await client.getLLMOrder(result.task_id);
        if (mountedRef.current) {
          setConnectionStatus(
            result.connection_phase === "connecting_p2p" ? "Establishing peer connection…"
              : result.connection_phase === "connecting_direct" ? "Connecting directly / waiting for response…"
              : result.connection_phase === "connecting_relay" ? "Connecting through configured relay…"
              : result.transport === "ice_udp_direct" ? "Connected peer to peer · generating…"
              : "Waiting for response…",
          );
        }
      }
      if (!mountedRef.current) return;
      const success = result.state === "succeeded";
      const assistantMessage: LLMChatMessage = {
        id: messageId(),
        role: "assistant",
        content: resultMessage(result),
        createdAt: new Date().toISOString(),
        status: success
          ? "complete"
          : result.state === "cancelled"
            ? "cancelled"
            : "failed",
        taskId: result.task_id,
        inputTokens: result.input_tokens,
        outputTokens: result.output_tokens,
        cost: result.amount,
        transport: result.transport,
      };
      const completed = {
        ...withUser,
        updatedAt: assistantMessage.createdAt,
        messages: [...withUser.messages, assistantMessage],
      };
      if (deletedIdsRef.current.has(withUser.id)) return;
      await replaceConversation(completed);
      notify(
        success ? "ok" : "warn",
        success ? "AI response complete" : `Private AI request ${result.state}`,
      );
    } catch (requestError) {
      const message =
        requestError instanceof Error
          ? requestError.message
          : "Private AI request failed";
      const failedMessage: LLMChatMessage = {
        id: messageId(),
        role: "assistant",
        content: message,
        createdAt: new Date().toISOString(),
        status: "failed",
      };
      if (mountedRef.current && !deletedIdsRef.current.has(withUser.id)) {
        await replaceConversation({
          ...withUser,
          updatedAt: failedMessage.createdAt,
          messages: [...withUser.messages, failedMessage],
        });
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

  const stopGeneration = async () => {
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
    const lastUser = [...messages]
      .reverse()
      .find((message) => message.role === "user");
    if (lastUser) void runPrompt(lastUser.content);
  };

  if (loading) return <LoadingPanel label="Opening AI chat" />;

  if (!selectedService) {
    return (
      <div className="empty-state">
        <Bot size={28} />
        <h3>The selected AI service is unavailable</h3>
        <p>Return to Services to choose a device or set up a model.</p>
        <Link
          to={
            client.mode === "fixture" ? "/services?client=fixture" : "/services"
          }
        >
          View services
        </Link>
      </div>
    );
  }

  const providerName =
    personal?.resolveName(selectedService.peer_id, selectedService.node_name) ||
    selectedService.node_name ||
    selectedService.peer_id;
  const providerKind =
    personal?.devices.find((device) => device.id === selectedService.peer_id)
      ?.kind || "desktop";
  const changeService = (key: string) => {
    const service = services.find((item) => serviceKey(item) === key);
    if (!service || sending) return;
    const next = new URLSearchParams(searchParams);
    next.set("peer", service.peer_id);
    next.set("service", service.service.package_id);
    setSearchParams(next);
  };
  return (
    <div className={styles.page}>
      <header className={styles.pageHeading}>
        <div className={styles.modelLockup}>
          <ServiceArt kind="ai" />
          <div>
            <h1>AI chat</h1>
            <p>Chat with a model on a device you choose.</p>
          </div>
        </div>
        <details className={styles.details}>
          <summary className={styles.detailsButton}>
            Source &amp; access <ChevronDown size={14} />
          </summary>
          <div className={styles.detailsPanel}>
            <strong>{providerName}</strong>
            <p>
              The selected provider sees your request while generating a
              response. History is stored on this device.
            </p>
            <p>Service: {selectedService.service.package_id}</p>
            <p>Network: {networkId}</p>
          </div>
        </details>
      </header>
      <div className={styles.sourceBar}>
        <label className={styles.sourceSelect}>
          <DeviceArt kind={providerKind} small />
          <select
            aria-label="Processing device"
            value={selectedService.peer_id}
            disabled={sending}
            onChange={(event) => {
              const next = services.find(
                (item) => item.peer_id === event.target.value,
              );
              if (next) changeService(serviceKey(next));
            }}
          >
            {[selectedService, ...services]
              .filter(
                (item, index, all) =>
                  all.findIndex((other) => other.peer_id === item.peer_id) ===
                  index,
              )
              .map((item) => (
                <option key={item.peer_id} value={item.peer_id}>
                  {personal?.resolveName(item.peer_id, item.node_name) ||
                    item.node_name ||
                    item.peer_id}{" "}
                  · {item.online ? "Online" : "Offline"}
                </option>
              ))}
          </select>
        </label>
        <select
          className={styles.modelLabel}
          aria-label="Model"
          value={serviceKey(selectedService)}
          disabled={sending}
          onChange={(event) => changeService(event.target.value)}
        >
          {[selectedService, ...services]
            .filter(
              (item, index, all) =>
                item.peer_id === selectedService.peer_id &&
                all.findIndex(
                  (other) => serviceKey(other) === serviceKey(item),
                ) === index,
            )
            .map((item) => (
              <option key={serviceKey(item)} value={serviceKey(item)}>
                {item.service.model_alias}
              </option>
            ))}
        </select>
        <Link
          className="pf-button"
          to={personalHref(
            "/services/manage#inference-api",
            client.mode === "fixture",
          )}
        >
          API access
        </Link>
      </div>
      <aside className={styles.history} aria-label="AI conversations">
        <button
          className={styles.newButton}
          type="button"
          onClick={() => void newConversation()}
        >
          <MessageSquarePlus size={17} /> New chat
        </button>
        <label className={styles.historySearch}>
          <Search size={16} />
          <input
            aria-label="Search conversations"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search conversations"
          />
        </label>
        <div className={styles.historyScroll}>
          {["Today", "Yesterday", "Previous 7 days", "Older"].map((bucket) =>
            grouped[bucket]?.length ? (
              <section className={styles.historyGroup} key={bucket}>
                <h2>{bucket}</h2>
                {grouped[bucket].map((conversation) => (
                  <div
                    className={`${styles.conversationRow}${selectedConversation?.id === conversation.id ? ` ${styles.conversationRowSelected}` : ""}`}
                    key={conversation.id}
                  >
                    <button
                      className={styles.conversationButton}
                      type="button"
                      onClick={() => setSelectedId(conversation.id)}
                    >
                      <strong>{conversation.title}</strong>
                      <small>{formatTime(conversation.updatedAt)}</small>
                    </button>
                    <button
                      className={styles.deleteButton}
                      type="button"
                      aria-label={`Delete ${conversation.title}`}
                      onClick={() => void removeConversation(conversation.id)}
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                ))}
              </section>
            ) : null,
          )}
          {!Object.keys(grouped).length ? (
            <div className={styles.historyEmpty}>No matching conversations</div>
          ) : null}
        </div>
        <button
          className={styles.clearButton}
          type="button"
          onClick={clearHistory}
        >
          <Trash2 size={14} /> Clear history
        </button>
      </aside>

      <main className={styles.workspace}>
        <div className={styles.messages} ref={messageScrollRef}>
          {!selectedConversation?.messages.length ? (
            <div className={styles.welcome}>
              <span className={styles.welcomeIcon}>
                <ServiceArt kind="ai" />
              </span>
              <h2>Start a conversation</h2>
              <p>
                Choose a device above, then send your first message. Ryn will
                keep using that device for this conversation.
              </p>
              <div className={styles.suggestions}>
                {SUGGESTIONS.map((suggestion) => (
                  <button
                    type="button"
                    key={suggestion}
                    onClick={() => setInput(suggestion)}
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            selectedConversation.messages.map((message) => (
              <div
                className={`${styles.messageRow}${message.role === "user" ? ` ${styles.messageRowUser}` : ""}`}
                key={message.id}
              >
                {message.role === "assistant" ? (
                  <span className={styles.assistantAvatar}>
                    <ServiceArt kind="ai" small />
                  </span>
                ) : null}
                <div className={styles.messageBlock}>
                  <div
                    className={`${styles.messageBubble}${message.status === "failed" ? ` ${styles.messageFailed}` : ""}`}
                  >
                    {message.content}
                  </div>
                  <span className={styles.messageMeta}>
                    {formatTime(message.createdAt)}
                    {message.transport === "ice_udp_direct" ? " · Peer-to-peer connection"
                      : message.transport === "peer_http_direct" ? " · Direct connection"
                      : message.transport === "encrypted_relay" ? " · Encrypted relay" : ""}
                    {message.cost !== undefined
                      ? ` · ${message.cost} credits`
                      : ""}
                  </span>
                  {message.role === "assistant" ? (
                    <div className={styles.messageActions}>
                      <button
                        type="button"
                        onClick={() =>
                          void navigator.clipboard?.writeText(message.content)
                        }
                      >
                        <Copy size={12} /> Copy
                      </button>
                      <button
                        type="button"
                        onClick={() =>
                          setHelpfulMessages((current) =>
                            new Set(current).add(message.id),
                          )
                        }
                      >
                        {helpfulMessages.has(message.id) ? (
                          <Check size={12} />
                        ) : (
                          <ThumbsUp size={12} />
                        )}{" "}
                        {helpfulMessages.has(message.id)
                          ? "Helpful"
                          : "Good response"}
                      </button>
                      {message.status !== "complete" ? (
                        <button type="button" onClick={retryLast}>
                          <RotateCcw size={12} /> Try again
                        </button>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              </div>
            ))
          )}
          {sending ? (
            <div className={styles.messageRow}>
              <span className={styles.assistantAvatar}>
                <ServiceArt kind="ai" small />
              </span>
              <div className={styles.thinking} aria-label="AI is thinking">
                <span />
                <span />
                <span />
              </div>
              <small role="status" className={styles.connectionStatus}>{connectionStatus}</small>
            </div>
          ) : null}
        </div>

        <div className={styles.composerWrap}>
          {error ? (
            <div className={styles.error} role="alert">
              {error}
            </div>
          ) : null}
          <div className={styles.composer}>
            <textarea
              aria-label="Message AI chat"
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder={`Message the model on ${providerName}…`}
              rows={1}
              onKeyDown={(event) => {
                if (
                  event.key === "Enter" &&
                  !event.shiftKey &&
                  !event.nativeEvent.isComposing
                ) {
                  event.preventDefault();
                  void runPrompt(input);
                }
              }}
            />
            {sending ? (
              <button
                className={styles.stopButton}
                type="button"
                aria-label="Stop generating"
                onClick={() => void stopGeneration()}
              >
                <Square size={15} />
              </button>
            ) : (
              <button
                className={styles.sendButton}
                type="button"
                aria-label="Send message"
                disabled={!input.trim() || !selectedService.online}
                onClick={() => void runPrompt(input)}
              >
                <SendHorizontal size={17} />
              </button>
            )}
          </div>
          <div className={styles.composerMeta}>
            <span>
              <ShieldCheck size={12} />{" "}
              {storageMode === "encrypted"
                ? "Encrypted on this device"
                : "History kept for this session"}
            </span>
            <span>
              Processed on {providerName}. No automatic device switching.
            </span>
          </div>
        </div>
      </main>
    </div>
  );
}
