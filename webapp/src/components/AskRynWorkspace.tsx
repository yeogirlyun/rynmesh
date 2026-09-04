import {
  Archive,
  Bot,
  CornerDownLeft,
  FileText,
  FolderOpen,
  MessageSquarePlus,
  Pencil,
  RotateCcw,
  Search,
  ShieldCheck,
  Square,
  Trash2,
  User,
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useAppContext } from "../appContext";
import type { AppOutletContext } from "../appContext";
import type {
  AssistantContextPrepareRequest,
  AssistantContextRef,
  AssistantConversationV2,
  AssistantMessage,
  AssistantRun,
  AssistantTarget,
} from "../domain/assistantTypes";
import {
  assistantSession,
  createAssistantConversation,
  deleteAssistantConversation,
  listAssistantConversations,
  saveAssistantConversation,
  titleFromPrompt,
} from "../domain/assistantStore";
import { Button, Chip } from "./ui";
import styles from "./AskRynWorkspace.module.css";

const preparedHandoffs = new Set<string>();
export const ASSISTANT_CONVERSATION_PAGE_SIZE = 50;

export function pageAssistantConversations(
  conversations: AssistantConversationV2[],
  query: string,
  showArchived: boolean,
  limit: number,
) {
  const normalizedQuery = query.trim().toLowerCase();
  const matching = conversations.filter((conversation) => (
    (showArchived || !conversation.archived)
    && conversation.title.toLowerCase().includes(normalizedQuery)
  ));
  return { items: matching.slice(0, limit), remaining: Math.max(0, matching.length - limit), total: matching.length };
}

function bytesLabel(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

function fileBase64(buffer: ArrayBuffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 1) binary += String.fromCharCode(bytes[index]);
  return btoa(binary);
}

function updateConversation(
  conversations: AssistantConversationV2[],
  id: string,
  updater: (conversation: AssistantConversationV2) => AssistantConversationV2,
) {
  return conversations.map((conversation) => conversation.id === id ? updater(conversation) : conversation);
}

export default function AskRynWorkspace({ compact = false, appContext }: { compact?: boolean; appContext?: AppOutletContext }) {
  const outletContext = useAppContext();
  const { client, confirm, notify } = appContext ?? outletContext;
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const session = useSyncExternalStore(assistantSession.subscribe, assistantSession.getSnapshot);
  const [targets, setTargets] = useState<AssistantTarget[]>([]);
  const [conversations, setConversations] = useState<AssistantConversationV2[]>([]);
  const [loading, setLoading] = useState(true);
  const [showArchived, setShowArchived] = useState(false);
  const [conversationQuery, setConversationQuery] = useState("");
  const [conversationLimit, setConversationLimit] = useState(ASSISTANT_CONVERSATION_PAGE_SIZE);
  const [mode, setMode] = useState<"local_chat" | "local_search">(
    searchParams.get("mode") === "search" ? "local_search" : "local_chat",
  );
  const [preparing, setPreparing] = useState(false);
  const [readerUrl, setReaderUrl] = useState("");
  const mounted = useRef(true);
  const active = conversations.find((conversation) => conversation.id === session.activeConversationId) ?? null;
  const draft = active ? session.drafts[active.id] ?? "" : "";
  const running = active?.messages.find((message) => message.status === "running" && message.runId);

  const persist = useCallback(async (conversation: AssistantConversationV2) => {
    setConversations((current) => updateConversation(current, conversation.id, () => conversation));
    await saveAssistantConversation(conversation);
    assistantSession.touchData();
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    void Promise.all([client.listAssistantTargets(), listAssistantConversations()]).then(([nextTargets, stored]) => {
      if (cancelled) return;
      setTargets(nextTargets);
      setConversations(stored);
      const current = assistantSession.getSnapshot().activeConversationId;
      if (!current || !stored.some((conversation) => conversation.id === current)) {
        if (stored[0]) assistantSession.setActive(stored[0].id);
        else if (nextTargets[0]) {
          const created = createAssistantConversation(nextTargets[0]);
          setConversations([created]);
          assistantSession.setActive(created.id);
          void saveAssistantConversation(created);
        }
      }
      setLoading(false);
    }).catch(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [client, session.dataRevision]);

  const pollRun = useCallback(async (conversationId: string, runId: string) => {
    for (;;) {
      const run = await client.getAssistantRun(runId);
      if (!mounted.current) return;
      if (["queued", "running", "cancelling"].includes(run.state)) {
        await new Promise((resolve) => window.setTimeout(resolve, document.hidden ? 3000 : 700));
        continue;
      }
      const latest = (await listAssistantConversations()).find((item) => item.id === conversationId);
      if (!latest) return;
      const messages = latest.messages.map((message) => message.runId === runId ? {
        ...message,
        content: run.output ?? run.safe_error ?? "The assistant run did not complete.",
        status: run.state === "completed" ? "complete" as const
          : run.state === "cancelled" ? "cancelled" as const : "failed" as const,
        errorCode: run.error_code ?? undefined,
        citations: run.citations,
        contextBudget: run.context_budget,
      } : message);
      await persist({ ...latest, messages, updatedAt: new Date().toISOString() });
      return;
    }
  }, [client, persist]);

  useEffect(() => {
    if (running?.runId && active) void pollRun(active.id, running.runId).catch(() => undefined);
  }, [active, pollRun, running?.runId]);

  const addContext = useCallback(async (request: AssistantContextPrepareRequest) => {
    if (!active) return;
    setPreparing(true);
    try {
      const context = await client.prepareAssistantContext({ ...request, conversation_id: active.id });
      if (!active.contexts.some((item) => item.id === context.id)) {
        await persist({
          ...active,
          contexts: [...active.contexts, context],
          updatedAt: new Date().toISOString(),
        });
      }
      notify("ok", `${context.title} is ready as private context`);
    } catch {
      notify("danger", "Ryn could not prepare that local material");
    } finally {
      setPreparing(false);
    }
  }, [active, client, notify, persist]);

  useEffect(() => {
    const handoff = searchParams.get("context");
    if (!active || !handoff?.startsWith("content:")) return;
    const contentId = handoff.slice("content:".length);
    const key = `${active.id}:${contentId}`;
    if (preparedHandoffs.has(key)) return;
    preparedHandoffs.add(key);
    void addContext({ source_kind: "content-item", content_id: contentId });
  }, [active, addContext, searchParams]);

  const createNew = (target = targets[0]) => {
    if (!target) return;
    const conversation = createAssistantConversation(target);
    setConversations((current) => [conversation, ...current]);
    assistantSession.setActive(conversation.id);
    void saveAssistantConversation(conversation).then(() => assistantSession.touchData());
  };

  const selectTarget = (targetKey: string) => {
    if (!active || running || targetKey === active.computeTarget.key) return;
    const target = targets.find((item) => item.key === targetKey);
    if (!target) return;
    if (target.privacy_boundary !== active.computeTarget.privacyBoundary) {
      confirm({
        title: `Start a ${target.privacy_boundary} conversation?`,
        body: "Ryn will create a separate conversation. Existing history and selected materials will not be copied across the privacy boundary.",
        risk: "medium",
        confirmLabel: "Create separate conversation",
        onConfirm: () => createNew(target),
      });
      return;
    }
    void persist({
      ...active,
      computeTarget: {
        key: target.key, label: target.label, privacyBoundary: target.privacy_boundary,
        providerPeerId: target.provider_peer_id, packageId: target.package_id, modelId: target.model_id,
      },
      updatedAt: new Date().toISOString(),
    });
  };

  const rename = () => {
    if (!active) return;
    const title = window.prompt("Conversation name", active.title)?.trim();
    if (brig(title)) void persist({ ...active, title: title!, updatedAt: new Date().toISOString() });
  };

  const archive = () => {
    if (!active) return;
    void persist({ ...active, archived: !active.archived, updatedAt: new Date().toISOString() });
  };

  const remove = () => {
    if (!active) return;
    confirm({
      title: "Delete this assistant conversation?",
      body: "The encrypted conversation record will be removed from this browser profile.",
      risk: "high",
      confirmLabel: "Delete conversation",
      onConfirm: async () => {
        await deleteAssistantConversation(active.id);
        const remaining = conversations.filter((item) => item.id !== active.id);
        setConversations(remaining);
        assistantSession.setActive(remaining[0]?.id ?? null);
        assistantSession.touchData();
      },
    });
  };

  const send = async (event: FormEvent) => {
    event.preventDefault();
    if (!active || running || !draft.trim()) return;
    const text = draft.trim();
    const now = new Date().toISOString();
    const userMessage: AssistantMessage = {
      id: crypto.randomUUID(), role: "user", content: text, status: "complete", createdAt: now,
    };
    const pendingId = crypto.randomUUID();
    const requestedMode = active.computeTarget.privacyBoundary === "friend" ? "remote_provider"
      : active.contexts.length ? "local_context" : mode;
    const requestMessages = [...active.messages, userMessage];
    const next: AssistantConversationV2 = {
      ...active,
      title: active.messages.length ? active.title : titleFromPrompt(text),
      messages: [...requestMessages, {
        id: pendingId, role: "assistant", content: "", status: "running", createdAt: now,
      }],
      updatedAt: now,
    };
    assistantSession.setDraft(active.id, "");
    await persist(next);
    try {
      const run = await client.createAssistantRun({
        version: "ryn.assistant-run.v1",
        conversation_id: active.id,
        idempotency_key: pendingId,
        target_key: active.computeTarget.key,
        privacy_boundary: active.computeTarget.privacyBoundary,
        mode: requestedMode,
        messages: requestMessages,
        contexts: active.contexts.map((context) => ({ local_ref: context.local_ref, kind: context.kind })),
        max_output_tokens: 512,
      });
      const withRun = {
        ...next,
        messages: next.messages.map((message) => message.id === pendingId ? { ...message, runId: run.run_id } : message),
      };
      await persist(withRun);
    } catch {
      const failed = {
        ...next,
        messages: next.messages.map((message) => message.id === pendingId ? {
          ...message, status: "failed" as const, content: "The local node could not start this run.",
        } : message),
      };
      await persist(failed);
    }
  };

  const retry = (message: AssistantMessage) => {
    if (!active) return;
    const index = active.messages.findIndex((item) => item.id === message.id);
    const prompt = [...active.messages.slice(0, index)].reverse().find((item) => item.role === "user");
    if (prompt) assistantSession.setDraft(active.id, prompt.content);
  };

  const cancel = async () => {
    if (running?.runId) await client.cancelAssistantRun(running.runId);
  };

  const onFile = async (file: File | undefined) => {
    if (!file) return;
    const lower = file.name.toLowerCase();
    if (lower.endsWith(".pdf")) {
      await addContext({ kind: "pdf", title: file.name, data_base64: fileBase64(await file.arrayBuffer()) });
    } else {
      await addContext({ kind: lower.endsWith(".md") || lower.endsWith(".markdown") ? "markdown" : "text",
        title: file.name, text: await file.text() });
    }
  };

  const page = pageAssistantConversations(conversations, conversationQuery, showArchived, conversationLimit);
  const totalBytes = active?.contexts.reduce((sum, context) => sum + context.size_bytes, 0) ?? 0;
  const willTruncate = totalBytes > 12 * 1024;

  if (loading) return <div className={styles.loading}>Loading encrypted conversations…</div>;
  if (!targets.length && !active) {
    return (
      <div className={styles.noModel}>
        <Bot size={28} />
        <h2>Enable private AI to ask Ryn</h2>
        <p>Your draft and local materials stay on this device. Ryn will not silently switch to cloud AI.</p>
        <Button variant="primary" onClick={() => navigate("/local-ai")}>Enable local AI</Button>
      </div>
    );
  }

  return (
    <div className={`${styles.workspace} ${compact ? styles.compact : ""}`}>
      {!compact ? (
        <aside className={styles.conversations} aria-label="Assistant conversations">
          <Button variant="primary" icon={MessageSquarePlus} onClick={() => createNew()}>New conversation</Button>
          <label className={styles.archiveToggle}>
            <input type="checkbox" checked={showArchived} onChange={(event) => setShowArchived(event.target.checked)} />
            Show archived
          </label>
          <input className={styles.conversationSearch} value={conversationQuery}
            onChange={(event) => { setConversationQuery(event.target.value); setConversationLimit(ASSISTANT_CONVERSATION_PAGE_SIZE); }}
            placeholder="Search conversations" aria-label="Search conversations" />
          <div className={styles.conversationList}>
            {page.items.map((conversation) => (
              <button key={conversation.id} type="button"
                className={conversation.id === active?.id ? styles.activeConversation : ""}
                onClick={() => assistantSession.setActive(conversation.id)}>
                <strong>{conversation.title}</strong>
                <small>{conversation.computeTarget.privacyBoundary === "device" ? "This device" : conversation.computeTarget.label}</small>
              </button>
            ))}
            {page.remaining > 0 ? (
              <button type="button" onClick={() => setConversationLimit((value) => value + ASSISTANT_CONVERSATION_PAGE_SIZE)}>
                <strong>Load 50 more</strong><small>{page.remaining} remaining</small>
              </button>
            ) : null}
          </div>
        </aside>
      ) : null}

      <section className={styles.chat}>
        <header className={styles.chatHeader}>
          <div>
            <span className={styles.eyebrow}>Ask Ryn</span>
            <h2>{active?.title ?? "Private assistant"}</h2>
          </div>
          <div className={styles.headerActions}>
            <Chip tone={active?.computeTarget.privacyBoundary === "device" ? "ok" : "warn"}>
              <ShieldCheck size={13} /> {active?.computeTarget.privacyBoundary === "device" ? "Private on device" : "Leaves this device"}
            </Chip>
            {active ? (
              <select aria-label="Assistant compute target" value={active.computeTarget.key}
                onChange={(event) => selectTarget(event.target.value)} disabled={Boolean(running)}>
                {!targets.some((target) => target.key === active.computeTarget.key) ? (
                  <option value={active.computeTarget.key}>{active.computeTarget.label} · offline</option>
                ) : null}
                {targets.map((target) => <option key={target.key} value={target.key}>{target.label}</option>)}
              </select>
            ) : null}
            {compact ? <Button variant="ghost" onClick={() => navigate("/ask")}>Expand</Button> : (
              <>
                <button type="button" aria-label="Rename conversation" onClick={rename}><Pencil size={15} /></button>
                <button type="button" aria-label="Archive conversation" onClick={archive}><Archive size={15} /></button>
                <button type="button" aria-label="Delete conversation" onClick={remove}><Trash2 size={15} /></button>
              </>
            )}
          </div>
        </header>

        {active && active.computeTarget.privacyBoundary !== "device" && !targets.some((target) => target.key === active.computeTarget.key) ? (
          <div className={styles.warning} role="status">
            This migrated provider is offline. Its history remains isolated; start a new device conversation to continue.
            <Button onClick={() => createNew(targets[0])}>Start new device conversation</Button>
          </div>
        ) : null}

        <div className={styles.messages} aria-live="polite">
          {!active?.messages.length ? (
            <div className={styles.empty}>
              <Bot size={30} />
              <h3>One private place to ask</h3>
              <p>Chat normally, search your Ryn content, or add an article, Markdown, text, or PDF file.</p>
            </div>
          ) : active.messages.map((message) => (
            <article key={message.id} className={`${styles.message} ${styles[message.role]}`}>
              <div className={styles.messageLabel}>{message.role === "user" ? <User size={14} /> : <Bot size={14} />}
                {message.role === "user" ? "You" : "Ryn"}</div>
              {message.status === "running" ? <p>Thinking on this device…</p> : <p>{message.content}</p>}
              {message.contextBudget?.truncated ? <div className={styles.truncated}>Answer used a bounded excerpt of the selected material.</div> : null}
              {message.citations?.length ? (
                <div className={styles.citations}>
                  <b>Reference material</b>
                  {message.citations.map((citation) => citation.source_url ? (
                    <a key={citation.citation_id} href={citation.source_url} target="_blank" rel="noreferrer noopener">
                      {citation.title}<small>{citation.quote}</small>
                    </a>
                  ) : citation.source_ref ? (
                    <Link key={citation.citation_id} to={`/items/${encodeURIComponent(citation.source_ref)}`}>
                      {citation.title}<small>{citation.quote}</small>
                    </Link>
                  ) : <span key={citation.citation_id}>{citation.title}</span>)}
                </div>
              ) : null}
              {message.status === "failed" || message.status === "cancelled" ? (
                <Button icon={RotateCcw} onClick={() => retry(message)}>Retry</Button>
              ) : null}
            </article>
          ))}
        </div>

        {active ? (
          <div className={styles.contextArea}>
            <div className={styles.contexts}>
              {active.contexts.map((context) => (
                <span key={context.id}><FileText size={13} />{context.title}<small>{bytesLabel(context.size_bytes)}</small>
                  <button type="button" aria-label={`Remove ${context.title}`} onClick={() => void persist({
                    ...active, contexts: active.contexts.filter((item) => item.id !== context.id),
                    updatedAt: new Date().toISOString(),
                  })}>×</button>
                </span>
              ))}
            </div>
            {willTruncate ? <div className={styles.warning}>The first 12 KB will be sent; the remaining context will be truncated before the run.</div> : null}
            {!compact ? (
              <div className={styles.addContext}>
                <label className={styles.fileButton}><FolderOpen size={14} /> Add text, Markdown, or PDF
                  <input type="file" accept=".txt,.md,.markdown,.pdf,text/plain,text/markdown,application/pdf"
                    onChange={(event) => void onFile(event.target.files?.[0])} disabled={preparing || Boolean(running)} />
                </label>
                <input value={readerUrl} onChange={(event) => setReaderUrl(event.target.value)} placeholder="Article URL" />
                <Button disabled={!readerUrl.trim() || preparing || Boolean(running)} onClick={() => {
                  void addContext({ source_kind: "reader-article", url: readerUrl.trim() }); setReaderUrl("");
                }}>Add article</Button>
              </div>
            ) : null}
          </div>
        ) : null}

        {active && targets.some((target) => target.key === active.computeTarget.key) ? (
          <form className={styles.composer} onSubmit={send}>
            <div className={styles.modeRow}>
              <button type="button" className={mode === "local_chat" ? styles.selectedMode : ""} onClick={() => setMode("local_chat")}>
                <Bot size={13} /> Chat
              </button>
              <button type="button" className={mode === "local_search" ? styles.selectedMode : ""} onClick={() => setMode("local_search")}>
                <Search size={13} /> Search my Ryn
              </button>
              <span>Compute: {active.computeTarget.privacyBoundary === "device" ? "on this device" : active.computeTarget.label}</span>
            </div>
            <textarea value={draft} onChange={(event) => assistantSession.setDraft(active.id, event.target.value)}
              placeholder="Ask Ryn…" disabled={Boolean(running)} aria-label="Ask Ryn message" />
            {running ? <Button icon={Square} onClick={() => void cancel()}>Cancel</Button>
              : <Button type="submit" variant="primary" icon={CornerDownLeft} disabled={!draft.trim()}>Send</Button>}
          </form>
        ) : null}
      </section>
    </div>
  );
}

function brig(value: string | undefined): value is string {
  return Boolean(value);
}
