import { useCallback, useEffect, useRef, useState } from "react";
import { askHistory, recoveryConversationId, type AskSyncChoice, type AskSyncConflict } from "../domain/askHistory";
import type { LLMConversation } from "../domain/llmConversationStore";
import { Button, Panel } from "./ui";
import styles from "./AskSyncConflicts.module.css";

export default function AskSyncConflicts({ refreshKey, onRestored }: {
  refreshKey?: unknown; onRestored: (conversation: LLMConversation) => void | Promise<void>;
}) {
  const [issues, setIssues] = useState<AskSyncConflict[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [expanded, setExpanded] = useState("");
  const alive = useRef(true);
  const busy = useRef(false);
  const generation = useRef(0);
  const load = useCallback(async () => {
    const current = ++generation.current;
    setLoading(true); setError("");
    try {
      const rows = await askHistory.syncConflicts();
      if (alive.current && current === generation.current) { setIssues(rows); setExpanded(""); }
    } catch (cause) {
      if (alive.current && current === generation.current) setError(cause instanceof Error ? cause.message : "Could not load conversation branches.");
    } finally { if (alive.current && current === generation.current) setLoading(false); }
  }, []);
  useEffect(() => { alive.current = true; return () => { alive.current = false; generation.current += 1; }; }, []);
  useEffect(() => { void load(); }, [load, refreshKey]);
  useEffect(() => { const refresh = () => { if (!busy.current) void load(); }; window.addEventListener("focus", refresh); return () => window.removeEventListener("focus", refresh); }, [load]);
  const restore = async (issue: AskSyncConflict, choice: AskSyncChoice) => {
    if (busy.current) return;
    busy.current = true; setSaving(true); setError("");
    try {
      // Stable across retries and page restarts: a lost response cannot create
      // another copy of the same reviewed branch.
      const id = await recoveryConversationId(issue, choice);
      const saved = await askHistory.restoreBranch(issue.id, choice.choice_id, id, issue.revision);
      if (alive.current) await onRestored(saved);
    } catch (cause) { if (alive.current) setError(cause instanceof Error ? cause.message : "The node did not confirm keeping this branch. Retry to check the same copy."); }
    finally { busy.current = false; if (alive.current) setSaving(false); }
  };
  if (!loading && !error && !issues.length) return null;
  return <Panel title="Conversation branches and recovery">
    {error ? <p role="alert">{error}</p> : null}
    {loading ? <p role="status">Loading conversation branches…</p> : null}
    <Button disabled={loading || saving} onClick={() => void load()}>Refresh branches</Button>
    <div className={styles.issues}>{issues.map((issue) => <section key={issue.id} aria-label={`Branches for ${issue.id}`}>
      <h3>{issue.deleted ? "Deleted conversation with pending recovery" : "Conversation changed on different devices"}</h3>
      <p>{issue.deleted ? "The original conversation stays out of your history. You can keep a concurrent branch as a separate conversation." : `Shared history: ${issue.common_messages.length} messages. Each branch retains its own continuation.`}</p>
      {issue.deferred ? <p role="status">This node is still checking the original task. Its outcome has not been confirmed; refresh after it finishes.</p> : null}
      {issue.local_draft ? <article>
        <h4>Unsent draft from this device</h4>
        <p>Original recipient: {issue.local_draft.providerPeerId} · {issue.local_draft.serviceName}</p>
        <p>This draft stayed on this device and has not been sent.</p>
        <textarea aria-label="Recovered unsent draft" readOnly value={issue.local_draft.draft ?? ""} rows={4} />
        <Button disabled={saving || loading} onClick={() => void restore(issue, { choice_id: "local-draft", value: issue.local_draft! })}>Keep draft as a separate conversation</Button>
      </article> : null}
      {(issue.deleted ? issue.recovery : issue.branches).map((choice, index) => {
        const key = `${issue.id}-${choice.choice_id}`;
        return <article key={key}>
          <h4>Branch {index + 1}: {choice.value.title}</h4>
          <p>Original recipient: {choice.value.providerPeerId} · {choice.value.serviceName} · {choice.value.networkId}</p>
          <small>{choice.value.messages.length} messages · {choice.value.serviceKey}</small>
          <button type="button" className="btn btn-standard" aria-expanded={expanded === key} aria-controls={`branch-${key}`} onClick={() => setExpanded(expanded === key ? "" : key)}>Read branch {index + 1}</button>
          {expanded === key ? <div id={`branch-${key}`} className={styles.transcript} tabIndex={0} aria-label={`Branch ${index + 1} messages`}>
            {choice.value.messages.map((message) => <article key={message.id}><strong>{message.role === "user" ? "You" : "Ryn"}</strong><p>{message.content}</p><small>{message.status}</small></article>)}
            {!choice.value.messages.length ? <p>No completed messages in this branch.</p> : null}
          </div> : null}
          <p>Keeping a branch saves a separate history with the original service. It does not send a question. Referenced articles may need to be saved on this device.</p>
          <Button disabled={saving || loading} onClick={() => void restore(issue, choice)}>Keep branch {index + 1} as a separate conversation</Button>
        </article>;
      })}
    </section>)}</div>
  </Panel>;
}
