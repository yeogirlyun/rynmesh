import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Archive, ArchiveRestore, MoreHorizontal, Pencil, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { agentTaskState, canUseAgentAction, isAgentActive, rynAgent, type AgentTask, type AgentTaskSummary } from "../domain/rynAgent";
import RynAgentContextMenu from "./RynAgentContextMenu";
import RynAgentTaskIndicator from "./RynAgentTaskIndicator";
import { useAgentRecordMutation, type PendingAgentRecord } from "./RynAgentRecords";
import "./RynAgentTaskMenu.css";

interface TaskMenuOptions {
  selected: string;
  busy: boolean;
  onChoose: (id: string) => void;
  onChange: (task: AgentTask) => void;
  onDelete: (id: string) => void;
  isUnread?: (task: AgentTaskSummary) => boolean;
}
type TaskDialogState = { kind: "rename" | "delete"; task: AgentTaskSummary };

function TaskDialog({ title, children, onClose, danger = false }: { title: string; children: ReactNode; onClose: () => void; danger?: boolean }) {
  const ref = useRef<HTMLElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  const id = useId();
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const input = ref.current?.querySelector<HTMLInputElement>("input");
    (input || ref.current?.querySelector<HTMLButtonElement>("button") || ref.current)?.focus();
    input?.select();
    const trap = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close.current(); return; }
      if (event.key !== "Tab" || !ref.current) return;
      const controls = Array.from(ref.current.querySelectorAll<HTMLElement>('input:not(:disabled), button:not(:disabled)'));
      const first = controls[0]; const last = controls[controls.length - 1];
      if (!first) { event.preventDefault(); ref.current.focus(); return; }
      if (!controls.includes(document.activeElement as HTMLElement)) { event.preventDefault(); (event.shiftKey ? last : first).focus(); return; }
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", trap, true);
    return () => { document.removeEventListener("keydown", trap, true); if (previous?.isConnected) previous.focus(); };
  }, []);
  return <div className="ra-task-dialog-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><section ref={ref} className="ra-task-dialog" role={danger ? "alertdialog" : "dialog"} aria-modal="true" aria-labelledby={id} tabIndex={-1}><h2 id={id}>{title}</h2>{children}</section></div>;
}

/** One controller serves project, recent and archive rows without changing selection. */
export function useAgentTaskMenu({ selected, busy, onChoose, onChange, onDelete, isUnread = () => false }: TaskMenuOptions) {
  const { t } = useTranslation("rynAgent");
  const [menu, setMenu] = useState<{ task: AgentTaskSummary; anchor: { x: number; y: number } } | null>(null);
  const [dialog, setDialog] = useState<TaskDialogState | null>(null);
  const [name, setName] = useState("");
  const [preparing, setPreparing] = useState(false);
  const [notice, setNotice] = useState("");
  const prepareLock = useRef(false);
  const preflight = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const latest = useRef({ busy, onChange });
  latest.current = { busy, onChange };
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; preflight.current?.abort(); };
  }, []);
  const mutation = useAgentRecordMutation({ busy, onChange, onDelete });
  const unavailable = busy || preparing || mutation.working || Boolean(mutation.pending);
  const error = notice || mutation.error;
  const closeDialog = () => { if (!preparing && !mutation.working) setDialog(null); };
  const beginDialog = (kind: TaskDialogState["kind"], task: AgentTaskSummary) => {
    setNotice(""); mutation.clearError(); setName(task.title); setDialog({ kind, task });
  };
  const act = async (operation: PendingAgentRecord["operation"], item: AgentTaskSummary, title?: string) => {
    if (unavailable || prepareLock.current) return;
    prepareLock.current = true; setPreparing(true); setNotice(""); mutation.clearError();
    const controller = new AbortController(); preflight.current = controller;
    try {
      // Sidebar summaries can lag behind a running task. Always obtain its latest grant/version.
      const task = await rynAgent.task(item.id, controller.signal);
      if (controller.signal.aborted || !mounted.current || latest.current.busy) return;
      if (!canUseAgentAction(task, operation)) {
        latest.current.onChange(task);
        setNotice(operation !== "rename" && isAgentActive(agentTaskState(task)) ? "taskBusyArchiveHelp" : "recordConflict");
        return;
      }
      const completed = await mutation.run({ task_id: task.id, operation, title: task.title, body: {
        expected_control_revision: task.control_revision,
        client_request_id: crypto.randomUUID(),
        ...(operation === "rename" ? { title } : {}),
      } });
      if (completed && mounted.current) setDialog(null);
    } catch {
      if (!controller.signal.aborted && mounted.current) setNotice(operation === "rename" ? "renameError" : "recordError");
    } finally { if (preflight.current === controller) preflight.current = null; prepareLock.current = false; if (mounted.current) setPreparing(false); }
  };
  const disabledHelp = (task: AgentTaskSummary, operation: "archive" | "delete") =>
    !canUseAgentAction(task, operation) && isAgentActive(agentTaskState(task)) ? t("taskBusyArchiveHelp") : undefined;
  const renderTask = (item: AgentTaskSummary, project = false): ReactNode => {
    const openMenu = (x: number, y: number) => { setNotice(""); mutation.clearError(); setMenu({ task: item, anchor: { x, y } }); };
    return <div key={item.id} className={`ra-sidebar-task${selected === item.id ? " selected" : ""}`} onContextMenu={event => {
      event.preventDefault(); event.stopPropagation();
      event.currentTarget.querySelector<HTMLButtonElement>("button")?.focus();
      const bounds = event.currentTarget.getBoundingClientRect();
      openMenu(event.clientX || bounds.left, event.clientY || bounds.bottom);
    }} onKeyDown={event => {
      if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
        event.preventDefault(); event.stopPropagation(); const bounds = event.currentTarget.getBoundingClientRect(); openMenu(bounds.left, bounds.bottom);
      }
    }}><button type="button" className={`${project ? "ra-project-task" : "ra-task-item"}${selected === item.id ? " selected" : ""}`} aria-pressed={selected === item.id} aria-label={item.title} aria-description={t(item.archived ? "archived" : item.pending_approval || agentTaskState(item) === "waiting_approval" ? "sidebarNeedsApproval" : item.pending_input || agentTaskState(item) === "waiting_input" ? "sidebarNeedsInput" : isUnread(item) ? "sidebarUnreadCompletion" : agentTaskState(item))} title={`${item.title} · ${t(item.archived ? "archived" : agentTaskState(item))}`} onClick={() => onChoose(item.id)}><span className="ra-task-title">{item.title}</span><RynAgentTaskIndicator task={item} unread={isUnread(item)} /></button><button type="button" className="ra-task-overflow ra-icon" aria-label={t("taskMenu", { title: item.title })} title={t("taskMenu", { title: item.title })} aria-haspopup="menu" aria-expanded={menu?.task.id === item.id} onClick={event => {
      const bounds = event.currentTarget.getBoundingClientRect(); openMenu(bounds.right, bounds.bottom);
    }}><MoreHorizontal size={16} /></button></div>;
  };
  const dialogs = <>
    {menu && <RynAgentContextMenu anchor={menu.anchor} label={t("taskMenu", { title: menu.task.title })} onClose={() => setMenu(null)} items={[
      { id: "rename", label: t("renameTask"), icon: <Pencil size={15} />, disabled: unavailable || !canUseAgentAction(menu.task, "rename"), onSelect: () => beginDialog("rename", menu.task) },
      menu.task.archived
        ? { id: "restore", label: t("restoreTask"), icon: <ArchiveRestore size={15} />, disabled: unavailable || !canUseAgentAction(menu.task, "restore"), onSelect: () => { void act("restore", menu.task); } }
        : { id: "archive", label: t("archiveTask"), icon: <Archive size={15} />, disabled: unavailable || !canUseAgentAction(menu.task, "archive"), description: disabledHelp(menu.task, "archive"), onSelect: () => { void act("archive", menu.task); } },
      { id: "delete", label: t("deleteTask"), icon: <Trash2 size={15} />, danger: true, disabled: unavailable || !canUseAgentAction(menu.task, "delete"), description: disabledHelp(menu.task, "delete"), onSelect: () => beginDialog("delete", menu.task) },
    ]} />}
    {dialog && <TaskDialog title={t(dialog.kind === "rename" ? "renameTask" : "deleteTask")} danger={dialog.kind === "delete"} onClose={closeDialog}>
      {dialog.kind === "rename" ? <form onSubmit={event => { event.preventDefault(); if (name.trim() && !/[\x00-\x1f\x7f]/.test(name)) void act("rename", dialog.task, name.trim()); }}><label>{t("renameTitle")}<input value={name} maxLength={160} required disabled={preparing || mutation.working} onChange={event => setName(event.target.value)} /></label>{error && <p role="alert">{t(error)}</p>}<div className="ra-task-dialog-actions"><button type="button" disabled={preparing || mutation.working} onClick={closeDialog}>{t("cancelRename")}</button><button type="submit" className="ra-primary" disabled={unavailable || !name.trim() || /[\x00-\x1f\x7f]/.test(name)}>{t("saveName")}</button></div></form> : <><p className="ra-task-dialog-name">{dialog.task.title}</p><p>{t("deleteTaskHelp")}</p>{error && <p role="alert">{t(error)}</p>}<div className="ra-task-dialog-actions"><button disabled={preparing || mutation.working} onClick={closeDialog}>{t("keepTask")}</button><button className="ra-task-danger" disabled={unavailable} onClick={() => { void act("delete", dialog.task); }}>{t("confirmDeleteTask")}</button></div></>}
    </TaskDialog>}
    {(mutation.pending || (!dialog && error)) && <section className="ra-task-record-notice" role={error ? "alert" : "status"}>
      {mutation.pending?.title && <strong>{mutation.pending.title}</strong>}
      {error && <p>{t(error)}</p>}
      {mutation.pending && <><p>{t(mutation.pending.cleanup_pending ? "cleanupPending" : "recordUncertain")}</p><button disabled={busy || preparing || mutation.working} onClick={() => { const original = mutation.pending; if (original) void mutation.run(original).then(done => { if (done) setDialog(null); }); }}>{t(mutation.pending.cleanup_pending ? "retryCleanup" : "checkRecord")}</button></>}
      {!mutation.pending && <button onClick={() => { setNotice(""); mutation.clearError(); }}>{t("cancelRename")}</button>}
    </section>}
  </>;
  return { renderTask, dialogs };
}
