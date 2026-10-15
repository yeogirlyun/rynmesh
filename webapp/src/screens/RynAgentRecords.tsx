import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { AgentAPIError, canUseAgentAction, rynAgent, sortAgentTasks, type AgentRecordOperation, type AgentResultPage, type AgentTask, type AgentTaskSummary } from "../domain/rynAgent";

const PENDING_KEY = "ryn.agent.pending-record";
interface PendingRecord { task_id: string; operation: AgentRecordOperation; body: Record<string, unknown>; }
function pendingRecord(): PendingRecord | null {
  try { return JSON.parse(sessionStorage.getItem(PENDING_KEY) || "null"); } catch { return null; }
}
function persist(value: PendingRecord | null) {
  try { if (value) sessionStorage.setItem(PENDING_KEY, JSON.stringify(value)); else sessionStorage.removeItem(PENDING_KEY); } catch { /* Server remains authoritative when browser storage is unavailable. */ }
}

export function ArchivedTasks({ enabled, selected, onChoose, revision }: { enabled: boolean; selected: string; onChoose: (id: string) => void; revision: number }) {
  const { t } = useTranslation("rynAgent");
  const [open, setOpen] = useState(false);
  const [tasks, setTasks] = useState<AgentTaskSummary[]>([]);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!enabled || !open) return;
    const controller = new AbortController(); setLoading(true); setError(false);
    void rynAgent.archivedTasks(controller.signal).then(value => { if (!controller.signal.aborted) setTasks(sortAgentTasks(value.tasks)); })
      .catch(() => { if (!controller.signal.aborted) setError(true); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [enabled, open, revision, retry]);
  if (!enabled) return null;
  return <details className="ra-archives" onToggle={event => setOpen(event.currentTarget.open)}><summary>{t("archivedTasks")}</summary>{open && <>
    {loading && <p role="status">{t("loading")}</p>}
    {error ? <p role="alert">{t("archiveLoadError")} <button onClick={() => setRetry(value => value + 1)}>{t("retry")}</button></p> : tasks.length ? tasks.map(item => <button key={item.id} className={`ra-task-item${selected === item.id ? " selected" : ""}`} aria-pressed={selected === item.id} onClick={() => onChoose(item.id)}><span>{item.title}</span><small>{t("archived")}</small></button>) : !loading && <p>{t("emptyArchives")}</p>}
  </>}</details>;
}

export function TaskRecords({ task, busy, onChange, onDelete }: { task: AgentTask | null; busy: boolean; onChange: (task: AgentTask) => void; onDelete: (id: string) => void }) {
  const { t } = useTranslation("rynAgent");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [working, setWorking] = useState(false);
  const [pending, setPending] = useState(pendingRecord);
  const [error, setError] = useState("");
  const [cleanup, setCleanup] = useState(false);
  const lock = useRef(false);
  useEffect(() => { setConfirmDelete(false); }, [task?.id]);
  const run = async (request: PendingRecord) => {
    if (busy || lock.current) return;
    lock.current = true; setWorking(true); setError(""); setPending(request); persist(request);
    try {
      const result = await rynAgent.record(request.task_id, request.operation, request.body);
      if ("deleted" in result) {
        onDelete(result.id);
        setConfirmDelete(false); setCleanup(result.cleanup_pending);
        if (!result.cleanup_pending) { setPending(null); persist(null); }
      } else {
        setPending(null); persist(null); setCleanup(false); onChange(result);
      }
    } catch (reason) {
      if (reason instanceof AgentAPIError && reason.status < 500) { setPending(null); persist(null); }
      setError(reason instanceof AgentAPIError && reason.status === 409 ? "recordConflict" : "recordError");
    } finally { lock.current = false; setWorking(false); }
  };
  const act = (operation: AgentRecordOperation) => {
    if (!task || pending || !canUseAgentAction(task, operation)) return;
    void run({ task_id: task.id, operation, body: { expected_control_revision: task.control_revision, client_request_id: crypto.randomUUID() } });
  };
  const supported = task && ["archive", "restore", "delete"].some(action => task.allowed_actions?.[action]);
  if (!supported && !pending) return null;
  return <section className="ra-records" aria-label={t("taskRecords")}>
    {task?.archived && <p>{t("archivedHelp")}</p>}
    {error && <p role="alert">{t(error)}</p>}
    {pending && <p role="status">{t(cleanup ? "cleanupPending" : "recordUncertain")} <button disabled={busy || working} onClick={() => void run(pending)}>{t(cleanup ? "retryCleanup" : "checkRecord")}</button></p>}
    {supported && <div className="ra-scope-actions">
      {!task.archived && task.allowed_actions?.archive && <button disabled={busy || working || Boolean(pending) || !canUseAgentAction(task, "archive")} onClick={() => act("archive")}>{t("archiveTask")}</button>}
      {task.archived && task.allowed_actions?.restore && <button disabled={busy || working || Boolean(pending) || !canUseAgentAction(task, "restore")} onClick={() => act("restore")}>{t("restoreTask")}</button>}
      {task.allowed_actions?.delete && <button disabled={busy || working || Boolean(pending) || !canUseAgentAction(task, "delete")} onClick={() => setConfirmDelete(true)}>{t("deleteTask")}</button>}
    </div>}
    {confirmDelete && <section className="ra-delete-confirm" role="alertdialog" aria-label={t("deleteTask")}><p>{t("deleteTaskHelp")}</p><div className="ra-scope-actions"><button disabled={working} onClick={() => setConfirmDelete(false)}>{t("keepTask")}</button><button disabled={busy || working || Boolean(pending)} onClick={() => act("delete")}>{t("confirmDeleteTask")}</button></div></section>}
  </section>;
}

export function TaskResultFiles({ task }: { task: AgentTask }) {
  const { t } = useTranslation("rynAgent");
  const [open, setOpen] = useState(false);
  const [chosen, setChosen] = useState("");
  const [page, setPage] = useState<AgentResultPage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [offset, setOffset] = useState(0);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    if (!open || !chosen) return;
    const controller = new AbortController(); setLoading(true); setError(false); setPage(null);
    void rynAgent.result(task.id, chosen, offset, controller.signal).then(next => { if (!controller.signal.aborted) setPage(next); })
      .catch(() => { if (!controller.signal.aborted) setError(true); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [task.id, chosen, offset, open, reload]);
  if (!task.result_refs?.length) return null;
  return <details className="ra-result-files ra-task-context" onToggle={event => setOpen(event.currentTarget.open)}><summary>{t("savedResults")}</summary>{open && <>
    <p>{t("savedResultsHelp")}</p>
    {task.result_refs.map(result => <button key={result.id} aria-pressed={chosen === result.id} onClick={() => { setChosen(result.id); setOffset(0); }}>{t(`tool_${result.tool}`, { defaultValue: t("savedOutput") })} · {result.bytes} {t("bytes")}</button>)}
    {loading && <p role="status">{t("loading")}</p>}
    {error && <p role="alert">{t("resultLoadError")} <button onClick={() => setReload(value => value + 1)}>{t("retry")}</button></p>}
    {page && <><p>{t("resultPage", { start: page.offset, end: page.next_offset ?? page.characters, total: page.characters })}</p><pre>{page.content}</pre><div className="ra-scope-actions">{offset > 0 && <button onClick={() => setOffset(Math.max(0, offset - 8192))}>{t("previousPage")}</button>}{page.next_offset !== null && <button onClick={() => setOffset(page.next_offset!)}>{t("nextPage")}</button>}</div><details><summary>{t("resultFingerprint")}</summary><code>{page.sha256}</code></details></>}
  </>}</details>;
}
