import { useEffect, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { Archive, MessageSquarePlus, RefreshCw, Search } from "lucide-react";
import { useAppContext } from "../appContext";
import { askHistory } from "../domain/askHistory";
import { readLegacyConversations, type LLMConversation } from "../domain/llmConversationStore";
import { personalHref } from "../personal/model";
import { tr, uiLocale, useUILanguage } from "../uiI18n";
import styles from "../screens/PrivateAIChat.module.css";

export function workspaceConversationUrl(row: LLMConversation, storage: "node" | "browser") {
  return `/ask?${new URLSearchParams({ conversation: row.id, peer: row.providerPeerId,
    service: row.serviceKey.slice(row.providerPeerId.length + 2), network: row.networkId, storage })}`;
}

export default function UnifiedAIHistory({ selectedId, storage, rows, onNew, actions, serviceScope }: {
  selectedId?: string; storage: "node" | "browser"; rows: LLMConversation[]; onNew: () => void; actions?: ReactNode; serviceScope?: { key: string; network: string };
}) {
  useUILanguage();
  const { client } = useAppContext();
  const navigate = useNavigate();
  const [all, setAll] = useState<{ row: LLMConversation; storage: "node" | "browser" }[]>([]);
  const [query, setQuery] = useState("");
  const [errors, setErrors] = useState<string[]>([]);
  const [retry, setRetry] = useState(0);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let active = true;
    setLoading(true);
    const read = async () => {
      const sources = await Promise.allSettled([client.mode === "live" ? askHistory.list() : Promise.resolve([]), readLegacyConversations().then(result => {
        if (result.unreadable) throw new Error("browser_history_unreadable");
        return result.conversations;
      })]);
      if (!active) return;
      setAll(sources.flatMap((result, index) => result.status === "fulfilled"
        ? result.value.map(row => ({ row, storage: index === 0 ? "node" as const : "browser" as const })) : []));
      setErrors(sources.flatMap((result, index) => result.status === "rejected" ? [tr(index === 0 ? "Node history could not be loaded." : "Browser history could not be loaded.")] : []));
      setLoading(false);
    };
    void read();
    return () => { active = false; };
  }, [client, storage, selectedId, retry]);
  // Storage is part of identity: equal IDs in two stores never overwrite each other.
  const entries = all.filter(entry => entry.storage !== storage || (serviceScope
    ? entry.row.serviceKey !== serviceScope.key || entry.row.networkId !== serviceScope.network
    : !rows.some(row => row.id === entry.row.id)));
  entries.push(...rows.map(row => ({ row, storage })));
  entries.sort((a, b) => b.row.updatedAt.localeCompare(a.row.updatedAt));
  const visible = entries.filter(({ row }) => `${row.title} ${row.serviceName}`.toLowerCase().includes(query.toLowerCase()));
  return <aside className={`${styles.history} sv-ws-history`} aria-label={tr("AI conversations")}>
    <button className={`${styles.newButton} sv-ws-newButton`} type="button" onClick={onNew}><MessageSquarePlus size={17} />{tr("新建对话")}</button>
    <label className={`${styles.historySearch} sv-ws-historySearch`}><Search size={16} /><input aria-label={tr("Search conversations")} placeholder={tr("搜索对话…")} value={query} onChange={event => setQuery(event.target.value)} /></label>
    <div className={`${styles.historyScroll} sv-ws-historyScroll`}>
      <section className={`${styles.historyGroup} sv-ws-historyGroup`}><h2>{tr("All conversations")}</h2>
        {visible.map(({ row, storage: place }) => <div key={`${place}:${row.id}`} className={`${styles.conversationRow} sv-ws-conversationRow${place === storage && row.id === selectedId ? ` ${styles.conversationRowSelected} sv-ws-conversationRowSelected` : ""}`}>
          <button type="button" className={`${styles.conversationButton} sv-ws-conversationButton`} onClick={() => navigate(personalHref(workspaceConversationUrl(row, place), client.mode === "fixture"))}>
            <strong>{row.title === "New conversation" ? tr("新对话") : row.title}</strong>
            <small>{tr(place === "node" ? "Saved on node" : "Saved in browser")} · {new Date(row.updatedAt).toLocaleDateString(uiLocale())}</small>
            <small>{row.serviceName}</small>
          </button>
        </div>)}
      </section>
      {!visible.length ? <p className={`${styles.historyEmpty} sv-ws-historyEmpty`} role="status">{tr(loading ? "Loading conversations…" : "No matching conversations")}</p> : null}
      {errors.map(error => <p role="alert" key={error}>{error}</p>)}
      {errors.length ? <button type="button" className={`${styles.clearButton} sv-ws-clearButton`} onClick={() => setRetry(value => value + 1)}><RefreshCw size={14} />{tr("Retry history")}</button> : null}
    </div>
    {actions}
    <button className={`${styles.clearButton} sv-ws-clearButton`} type="button" onClick={() => navigate(personalHref("/ask/history", client.mode === "fixture"))}><Archive size={14} />{tr("Manage node history")}</button>
    <small className="ai-history-location">{tr("Original histories keep their storage location. Import and sync require a separate choice.")}</small>
  </aside>;
}
