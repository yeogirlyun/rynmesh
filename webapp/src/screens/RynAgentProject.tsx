import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronRight, Folder, FolderOpen, LoaderCircle, Plus, RefreshCw, SquarePen, X } from "lucide-react";
import { agentTaskState, rynAgent, type AgentProject, type AgentTaskSummary } from "../domain/rynAgent";
import { canPickPaths, chooseDropDir } from "../domain/dropDesktop";
import "./rynAgentText";
import "./RynAgentProject.css";

export interface RynAgentProjectProps {
  value: string;
  onChange: (id: string) => void;
  disabled?: boolean;
  tasks?: AgentTaskSummary[];
  activeTaskID?: string;
  onSelectTask?: (id: string) => void;
  onNewTask?: (projectID: string) => void;
  onProjectsChange?: (projects: AgentProject[]) => void;
  restoreSelection?: boolean;
}

type ProjectNavigation = Pick<RynAgentProjectProps, "value" | "activeTaskID">;

/** Saved projects stay visible while their conversations are opened. */
export default function RynAgentProject({ value, onChange, disabled = false, tasks = [], activeTaskID, onSelectTask, onNewTask, onProjectsChange, restoreSelection = true }: RynAgentProjectProps) {
  const { t } = useTranslation("rynAgent");
  const [projects, setProjects] = useState<AgentProject[]>(() => [...new Map(tasks.filter(task => task.project).map(task => [task.project!.id, task.project!])).values()]);
  const [path, setPath] = useState("");
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busyID, setBusyID] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const touched = useRef(false);
  const mounted = useRef(true);
  const projectsRef = useRef(projects);
  const callbacks = useRef({ onChange, onProjectsChange, activeTaskID, value, restoreSelection });
  callbacks.current = { onChange, onProjectsChange, activeTaskID, value, restoreSelection };
  const blocked = disabled || busyID !== null;
  const navigation = (): ProjectNavigation => ({ value: callbacks.current.value, activeTaskID: callbacks.current.activeTaskID });
  const isCurrentNavigation = (snapshot: ProjectNavigation) => snapshot.value === callbacks.current.value && snapshot.activeTaskID === callbacks.current.activeTaskID;

  const publish = (next: AgentProject[]) => {
    projectsRef.current = next;
    setProjects(next);
    callbacks.current.onProjectsChange?.(next);
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
        <div className="ra-project-row">
          <button type="button" className="ra-project-chevron" aria-label={t(expanded ? "projectCollapse" : "projectExpand", { name: project.name, defaultValue: `${expanded ? "Hide" : "Show"} tasks in {{name}}` })} aria-expanded={expanded} onClick={() => setCollapsed(current => { const next = new Set(current); if (next.has(project.id)) next.delete(project.id); else next.add(project.id); return next; })}>{expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}</button>
          <button type="button" className="ra-project-select" title={project.path} aria-label={`${project.name} · ${project.path}`} aria-pressed={isSelected} disabled={blocked} onClick={() => void select(project.id)}>{busyID === project.id ? <LoaderCircle size={15} className="ra-project-spinner" /> : isSelected ? <FolderOpen size={15} /> : <Folder size={15} />}<span>{project.name}</span></button>
          <div className="ra-project-actions"><button type="button" className="ra-project-icon" title={t("projectReload", { name: project.name, defaultValue: "Reload {{name}}" })} aria-label={t("projectReload", { name: project.name, defaultValue: "Reload {{name}}" })} disabled={blocked} onClick={() => void load(project.path, project.id)}><RefreshCw size={13} /></button>{onNewTask && <button type="button" className="ra-project-icon" title={t("projectNewTask", { name: project.name, defaultValue: "New task in {{name}}" })} aria-label={t("projectNewTask", { name: project.name, defaultValue: "New task in {{name}}" })} disabled={blocked} onClick={() => void select(project.id, true)}><SquarePen size={14} /></button>}</div>
        </div>
        {expanded && <div className="ra-project-task-list">{projectTasks.map(task => <button type="button" key={task.id} className={`ra-project-task${task.id === activeTaskID ? " selected" : ""}`} aria-pressed={task.id === activeTaskID} disabled={disabled || !onSelectTask} title={`${task.title} · ${t(agentTaskState(task))}`} onClick={() => onSelectTask?.(task.id)}><span>{task.title}</span><small>{t(agentTaskState(task))}</small></button>)}{!projectTasks.length && isSelected && <p className="ra-projects-note">{t("projectTasksEmpty", { defaultValue: "No tasks yet" })}</p>}</div>}
      </li>;
    })}</ul>}
    {!loading && value && !projects.some(project => project.id === value) && <p className="ra-projects-note">{t("projectUnavailable")}</p>}
    {error && <div className="ra-project-error" role="alert"><p>{t(error === "projectListError" ? "projectLoadError" : error)}</p>{error === "projectListError" && <button type="button" disabled={blocked || loading} onClick={() => void retry()}><RefreshCw size={13} />{t("projectListRetry", { defaultValue: "Reload projects" })}</button>}</div>}
  </section>;
}
