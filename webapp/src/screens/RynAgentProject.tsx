import { Fragment, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronRight, Folder, FolderOpen, LoaderCircle, MoreHorizontal, Pencil, Plus, RefreshCw, SquarePen, Trash2, X } from "lucide-react";
import { agentTaskState, rynAgent, type AgentProject, type AgentTaskSummary } from "../domain/rynAgent";
import { canPickPaths, chooseDropDir } from "../domain/dropDesktop";
import "./rynAgentText";
import RynAgentContextMenu from "./RynAgentContextMenu";
import RynAgentTaskIndicator from "./RynAgentTaskIndicator";
import "./RynAgentProject.css";

export interface RynAgentProjectProps {
  value: string;
  onChange: (id: string) => void;
  disabled?: boolean;
  tasks?: AgentTaskSummary[];
  activeTaskID?: string;
  navigationRevision?: number;
  onSelectTask?: (id: string) => void;
  onNewTask?: (projectID: string) => void;
  onProjectsChange?: (projects: AgentProject[]) => void;
  restoreSelection?: boolean;
  renderTask?: (task: AgentTaskSummary) => ReactNode;
}

type ProjectNavigation = Pick<RynAgentProjectProps, "value" | "activeTaskID" | "navigationRevision">;

/** Saved projects stay visible while their conversations are opened. */
export default function RynAgentProject({ value, onChange, disabled = false, tasks = [], activeTaskID, navigationRevision = 0, onSelectTask, onNewTask, onProjectsChange, restoreSelection = true, renderTask }: RynAgentProjectProps) {
  const { t } = useTranslation("rynAgent");
  const [projects, setProjects] = useState<AgentProject[]>(() => [...new Map(tasks.filter(task => task.project).map(task => [task.project!.id, task.project!])).values()]);
  const [path, setPath] = useState("");
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busyID, setBusyID] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [menu, setMenu] = useState<{ project: AgentProject; anchor: { x: number; y: number } } | null>(null);
  const [dialog, setDialog] = useState<{ project: AgentProject; kind: "rename" | "remove" } | null>(null);
  const [projectName, setProjectName] = useState("");
  const [dialogError, setDialogError] = useState("");
  const removed = useRef(new Set<string>());
  const touched = useRef(false);
  const mounted = useRef(true);
  const projectsRef = useRef(projects);
  const callbacks = useRef({ onChange, onProjectsChange, activeTaskID, value, navigationRevision, restoreSelection });
  callbacks.current = { onChange, onProjectsChange, activeTaskID, value, navigationRevision, restoreSelection };
  const blocked = disabled || busyID !== null;
  const navigation = (): ProjectNavigation => ({ value: callbacks.current.value, activeTaskID: callbacks.current.activeTaskID, navigationRevision: callbacks.current.navigationRevision });
  const isCurrentNavigation = (snapshot: ProjectNavigation) => snapshot.value === callbacks.current.value && snapshot.activeTaskID === callbacks.current.activeTaskID && snapshot.navigationRevision === callbacks.current.navigationRevision;

  const publish = (next: AgentProject[]) => {
    const visible = next.filter(project => !removed.current.has(project.id));
    projectsRef.current = visible;
    setProjects(visible);
    callbacks.current.onProjectsChange?.(visible);
  };

  useEffect(() => {
    mounted.current = true;
    if (projectsRef.current.length) callbacks.current.onProjectsChange?.(projectsRef.current);
    const abort = new AbortController();
    void rynAgent.projects(abort.signal).then(result => {
      if (abort.signal.aborted) return;
      const current = projectsRef.current;
      publish(touched.current ? [...result.projects.filter(project => !current.some(item => item.id === project.id)), ...current] : result.projects);
      if (callbacks.current.restoreSelection && !callbacks.current.value && !callbacks.current.activeTaskID && !touched.current && result.selected_project_id) callbacks.current.onChange(result.selected_project_id);
    }).catch(() => { if (!abort.signal.aborted) setError("projectListError"); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => { mounted.current = false; abort.abort(); };
  }, []);

  useEffect(() => {
    // Opening a conversation reveals its project even if it was collapsed earlier.
    const projectID = tasks.find(task => task.id === activeTaskID)?.project?.id;
    if (projectID) setCollapsed(current => {
      if (!current.has(projectID)) return current;
      const next = new Set(current); next.delete(projectID); return next;
    });
  }, [activeTaskID]);

  const retry = async () => {
    if (blocked || loading) return;
    setLoading(true); setError("");
    try {
      const result = await rynAgent.projects();
      if (mounted.current) publish(result.projects);
    } catch { if (mounted.current) setError("projectListError"); }
    finally { if (mounted.current) setLoading(false); }
  };

  const select = async (id: string, startTask = false) => {
    if (blocked) return;
    const originalNavigation = navigation();
    touched.current = true;
    setError("");
    if (id === value) {
      if (startTask && onNewTask) onNewTask(id); else callbacks.current.onChange(id);
      return;
    }
    setBusyID(id);
    try {
      await rynAgent.selectProject(id);
      if (!mounted.current) return;
      setCollapsed(current => { const next = new Set(current); next.delete(id); return next; });
      if (isCurrentNavigation(originalNavigation)) {
        if (startTask && onNewTask) onNewTask(id); else callbacks.current.onChange(id);
      }
    } catch { if (mounted.current) setError("projectSelectionError"); }
    finally { if (mounted.current) setBusyID(null); }
  };

  const load = async (folder: string, reloadID?: string, originalNavigation = navigation()) => {
    if (!folder.trim() || blocked) return;
    touched.current = true;
    setBusyID(reloadID || "new"); setError("");
    try {
      const project = await rynAgent.loadProject(folder.trim());
      if (!mounted.current) return;
      removed.current.delete(project.id);
      const current = projectsRef.current;
      publish(current.some(item => item.id === project.id) ? current.map(item => item.id === project.id ? project : item) : [...current, project]);
      setCollapsed(current => { const next = new Set(current); next.delete(project.id); return next; });
      if (isCurrentNavigation(originalNavigation)) callbacks.current.onChange(project.id);
      setOpen(false); setPath("");
    } catch { if (mounted.current) { setError("projectLoadError"); if (!reloadID) setOpen(true); } }
    finally { if (mounted.current) setBusyID(null); }
  };

  const browse = async () => {
    if (blocked) return;
    const originalNavigation = navigation();
    setBusyID("picker"); setError("");
    let folder: string | null = null;
    try { folder = await chooseDropDir(); }
    catch { if (mounted.current) { setOpen(true); setError("projectLoadError"); } }
    finally { if (mounted.current) setBusyID(null); }
    if (folder && mounted.current) { setPath(folder); await load(folder, undefined, originalNavigation); }
  };

  const openDialog = (project: AgentProject, kind: "rename" | "remove") => {
    setDialog({ project, kind }); setProjectName(project.name); setDialogError("");
  };

  const submitDialog = async () => {
    if (!dialog || blocked || (dialog.kind === "rename" && !projectName.trim())) return;
    const originalNavigation = navigation();
    const { project, kind } = dialog;
    const wasSelected = (tasks.find(task => task.id === originalNavigation.activeTaskID)?.project?.id || originalNavigation.value) === project.id;
    touched.current = true;
    setBusyID(project.id); setDialogError(""); setError("");
    try {
      if (kind === "rename") {
        const updated = await rynAgent.renameProject(project.id, projectName.trim());
        if (!mounted.current) return;
        publish(projectsRef.current.map(item => item.id === project.id ? updated : item));
      } else {
        await rynAgent.removeProject(project.id);
        if (!mounted.current) return;
        removed.current.add(project.id);
        publish(projectsRef.current.filter(item => item.id !== project.id));
        if (wasSelected && isCurrentNavigation(originalNavigation)) callbacks.current.onChange("");
      }
      setDialog(null);
    } catch { if (mounted.current) setDialogError(kind === "rename" ? "projectRenameError" : "projectRemoveError"); }
    finally { if (mounted.current) setBusyID(null); }
  };

  const activeProjectID = tasks.find(task => task.id === activeTaskID)?.project?.id;
  const selectedProjectID = activeProjectID || value;
  return <section className="ra-projects" aria-label={t("projects", { defaultValue: "Projects" })}>
    <div className="ra-projects-heading"><h3>{t("projects", { defaultValue: "Projects" })}</h3><button type="button" className="ra-project-icon" title={t("projectAdd", { defaultValue: "Add project" })} aria-label={t("loadProject")} aria-expanded={open} disabled={blocked} onClick={() => { setOpen(current => !current); setError(""); }}><Plus size={15} /></button></div>
    {open && <form className="ra-project-add" onSubmit={event => { event.preventDefault(); void load(path); }}>
      <div className="ra-project-add-heading"><strong>{t("projectAdd", { defaultValue: "Add project" })}</strong><button type="button" className="ra-project-icon" aria-label={t("close")} disabled={blocked} onClick={() => setOpen(false)}><X size={14} /></button></div>
      {canPickPaths() && <button type="button" className="ra-project-browse" disabled={blocked} onClick={() => void browse()}><FolderOpen size={15} />{t("chooseProjectFolder", { defaultValue: "Choose folder" })}</button>}
      <label>{t("projectPath")}<input autoFocus value={path} placeholder={t("projectPath")} disabled={blocked} onChange={event => setPath(event.target.value)} /></label>
      <p>{t("projectHelp")}</p>
      <button type="submit" className="ra-project-load-button" disabled={!path.trim() || blocked}>{busyID === "new" || busyID === "picker" ? <LoaderCircle size={14} className="ra-project-spinner" /> : <Plus size={14} />}{t(busyID === "new" ? "loading" : "loadProject")}</button>
    </form>}
    {loading && <p className="ra-projects-note" role="status">{t("loading")}</p>}
    {!loading && !projects.length && !error && <p className="ra-projects-note">{t("emptyProjects", { defaultValue: "Add a local folder to start a project." })}</p>}
    {projects.length > 0 && <ul className="ra-project-list">{projects.map(project => {
      const projectTasks = tasks.filter(task => task.project?.id === project.id && !task.archived);
      const expanded = !collapsed.has(project.id);
      const isSelected = project.id === selectedProjectID;
      return <li key={project.id} className={`ra-project-group${isSelected ? " selected" : ""}`}>
        <div className="ra-project-row" onContextMenu={event => {
          event.preventDefault();
          if (blocked) return;
          event.currentTarget.querySelector<HTMLButtonElement>(".ra-project-select")?.focus();
          setMenu({ project, anchor: { x: event.clientX, y: event.clientY } });
        }}>
          <button type="button" className="ra-project-chevron" aria-label={t(expanded ? "projectCollapse" : "projectExpand", { name: project.name, defaultValue: `${expanded ? "Hide" : "Show"} tasks in {{name}}` })} aria-expanded={expanded} onClick={() => setCollapsed(current => { const next = new Set(current); if (next.has(project.id)) next.delete(project.id); else next.add(project.id); return next; })}>{expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}</button>
          <button type="button" className="ra-project-select" title={project.path} aria-label={`${project.name} · ${project.path}`} aria-pressed={isSelected} disabled={blocked} onClick={() => void select(project.id)}>{busyID === project.id ? <LoaderCircle size={15} className="ra-project-spinner" /> : isSelected ? <FolderOpen size={15} /> : <Folder size={15} />}<span>{project.name}</span></button>
          <div className="ra-project-actions">{onNewTask && <button type="button" className="ra-project-icon" title={t("projectNewTask", { name: project.name, defaultValue: "New task in {{name}}" })} aria-label={t("projectNewTask", { name: project.name, defaultValue: "New task in {{name}}" })} disabled={blocked} onClick={() => void select(project.id, true)}><SquarePen size={14} /></button>}<button type="button" className="ra-project-icon" aria-label={t("projectMenu", { name: project.name, defaultValue: "Project options for {{name}}" })} aria-haspopup="menu" aria-expanded={menu?.project.id === project.id} disabled={blocked} onClick={event => { const bounds = event.currentTarget.getBoundingClientRect(); setMenu({ project, anchor: { x: bounds.right, y: bounds.bottom + 4 } }); }}><MoreHorizontal size={15} /></button></div>
        </div>
        {expanded && <div className="ra-project-task-list">{projectTasks.map(task => <Fragment key={task.id}>{renderTask ? renderTask(task) : <button type="button" className={`ra-project-task${task.id === activeTaskID ? " selected" : ""}`} aria-pressed={task.id === activeTaskID} aria-label={task.title} aria-description={t(agentTaskState(task))} disabled={disabled || !onSelectTask} title={`${task.title} · ${t(agentTaskState(task))}`} onClick={() => onSelectTask?.(task.id)}><span className="ra-task-title">{task.title}</span><RynAgentTaskIndicator task={task} /></button>}</Fragment>)}{!projectTasks.length && isSelected && <p className="ra-projects-note">{t("projectTasksEmpty", { defaultValue: "No tasks yet" })}</p>}</div>}
      </li>;
    })}</ul>}
    {!loading && value && !projects.some(project => project.id === value) && <p className="ra-projects-note">{t("projectUnavailable")}</p>}
    {error && <div className="ra-project-error" role="alert"><p>{t(error === "projectListError" ? "projectLoadError" : error)}</p>{error === "projectListError" && <button type="button" disabled={blocked || loading} onClick={() => void retry()}><RefreshCw size={13} />{t("projectListRetry", { defaultValue: "Reload projects" })}</button>}</div>}
    {menu && <RynAgentContextMenu anchor={menu.anchor} label={t("projectMenu", { name: menu.project.name, defaultValue: "Project options for {{name}}" })} onClose={() => setMenu(null)} items={[
      ...(onNewTask ? [{ id: "new", label: t("projectNewTask", { name: menu.project.name, defaultValue: "New task in {{name}}" }), icon: <SquarePen size={15} />, disabled: blocked, onSelect: () => void select(menu.project.id, true) }] : []),
      { id: "reload", label: t("projectReload", { name: menu.project.name, defaultValue: "Reload {{name}}" }), icon: <RefreshCw size={15} />, disabled: blocked, onSelect: () => void load(menu.project.path, menu.project.id) },
      { id: "rename", label: t("renameProject", { defaultValue: "Rename project" }), icon: <Pencil size={15} />, disabled: blocked, onSelect: () => openDialog(menu.project, "rename") },
      { id: "remove", label: t("projectRemove", { defaultValue: "Remove project" }), icon: <Trash2 size={15} />, disabled: blocked, danger: true, onSelect: () => openDialog(menu.project, "remove") },
    ]} />}
    {dialog && <ProjectDialog title={t(dialog.kind === "rename" ? "renameProject" : "projectRemove", { defaultValue: dialog.kind === "rename" ? "Rename project" : "Remove project" })} busy={busyID !== null} onClose={() => { if (!busyID) setDialog(null); }} onSubmit={() => void submitDialog()}>
      {dialog.kind === "rename" ? <label className="ra-project-name-field">{t("renameTitle", { defaultValue: "Name" })}<input autoFocus maxLength={160} value={projectName} disabled={busyID !== null} onChange={event => setProjectName(event.target.value)} /></label> : <><strong className="ra-project-remove-name">{dialog.project.name}</strong><p>{t("projectRemoveHelp", { defaultValue: "Remove this project from the sidebar and revoke its folder access. Unfinished project tasks will be paused. Your local folder and conversation records will be kept." })}</p></>}
      {dialogError && <p className="ra-project-dialog-error" role="alert">{t(dialogError, { defaultValue: dialog.kind === "rename" ? "Could not rename the project. Try again." : "Could not remove the project. Try again." })}</p>}
      <div className="ra-project-dialog-actions"><button type="button" disabled={busyID !== null} onClick={() => setDialog(null)}>{t("cancelRename", { defaultValue: "Cancel" })}</button><button type="submit" className={dialog.kind === "remove" ? "ra-project-danger" : "ra-project-primary"} disabled={busyID !== null || (dialog.kind === "rename" && !projectName.trim())}>{busyID !== null && <LoaderCircle size={14} className="ra-project-spinner" />}{t(dialog.kind === "rename" ? "saveName" : "projectRemoveConfirm", { defaultValue: dialog.kind === "rename" ? "Save" : "Remove project" })}</button></div>
    </ProjectDialog>}
  </section>;
}

function ProjectDialog({ title, busy, onClose, onSubmit, children }: { title: string; busy: boolean; onClose: () => void; onSubmit: () => void; children: ReactNode }) {
  const heading = useId();
  const dialog = useRef<HTMLFormElement>(null);
  useLayoutEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const input = dialog.current?.querySelector<HTMLInputElement>("input");
    if (input) { input.focus(); input.select(); } else dialog.current?.querySelector<HTMLButtonElement>("button")?.focus();
    return () => { if (previousFocus?.isConnected) previousFocus.focus(); };
  }, []);
  return createPortal(<div className="ra-project-dialog-backdrop" onClick={event => { if (event.target === event.currentTarget && !busy) onClose(); }}><form ref={dialog} className="ra-project-dialog" role="dialog" aria-modal="true" aria-labelledby={heading} tabIndex={-1} onSubmit={event => { event.preventDefault(); onSubmit(); }} onKeyDown={event => {
    if (event.key === "Escape") { event.preventDefault(); if (!busy) onClose(); }
    if (event.key !== "Tab") return;
    const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled)'));
    if (!controls.length) { event.preventDefault(); event.currentTarget.focus(); return; }
    const first = controls[0], last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }}><h2 id={heading}>{title}</h2>{children}</form></div>, document.body);
}
