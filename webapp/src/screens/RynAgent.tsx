import { useCallback, useContext, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowUp, MoreHorizontal, Square, Check, CircleAlert, ListChecks, Monitor, Network, PanelLeftClose, PanelLeftOpen, Pause, Play, Settings, SlidersHorizontal, Share2, Sparkles, SquarePen, X } from "lucide-react";
import { AgentAPIError, agentMessageModelID, agentActionBody, agentAnswerBody, agentMessageBody, agentScopeBody, agentToolResultState, canContinueAgentTask, canResumeAgentTask, canUseAgentModel, canUseAgentAction, agentTaskState, isAgentActive, isAgentLateToolResult, pendingAgentText, sortAgentTasks, rynAgent, type AgentAction, type AgentApproval, type AgentMessageIntent, type AgentModel, type AgentScopeSelection, type AgentScopeSnapshot, type AgentSettings, type AgentTask, type AgentTaskSummary, type AgentTool } from "../domain/rynAgent";
import { AgentNavigationContext } from "../personal/agentNavigation";
import ChatMarkdown from "./ChatMarkdown";
import RynAgentScope from "./RynAgentScope";
import RynAgentModel from "./RynAgentModel";
import RynAgentInputCard from "./RynAgentInputCard";
import RynAgentVerification from "./RynAgentVerification";
import { PreferencesEditor, SkillSelection, TaskSkills } from "./RynAgentGuidance";
import RynAgentBudget from "./RynAgentBudget";
import { ArchivedTasks, TaskRecords, TaskResultFiles } from "./RynAgentRecords";
import { TaskArtifacts } from "./RynAgentArtifacts";
import "./rynAgentText";
import "./RynAgent.css";

const SELECTED_KEY = "ryn.agent.selected";
const DRAFT_KEY = "ryn.agent.drafts";
const PENDING_KEY = "ryn.agent.pending-create";
const CONTROL_KEY = "ryn.agent.pending-control";
const SCOPE_DRAFT_KEY = "ryn.agent.scope-draft";
interface PendingControl { task_id: string; route: "messages" | "actions" | "scope"; body: Record<string, unknown>; }
interface ScopeDraft { scope: AgentScopeSelection; model_id: string; destination_revision: string | null; }
function readScopeDraft(): ScopeDraft | null { try { const value = JSON.parse(saved(SCOPE_DRAFT_KEY)); return Array.isArray(value?.scope?.resources) ? value : null; } catch { return null; } }
function saved(key: string): string { try { return sessionStorage.getItem(key) || ""; } catch { return ""; } }
function save(key: string, value: string) { try { if (value) sessionStorage.setItem(key, value); else sessionStorage.removeItem(key); } catch { /* Storage can be unavailable. */ } }
function readDrafts(): Record<string, string> { try { return JSON.parse(saved(DRAFT_KEY) || "{}"); } catch { return {}; } }
function json(value: unknown): string { return JSON.stringify(value, null, 2) ?? ""; }
const outcomeReasons: Record<string, string> = {
  "Independent goal verification is not available.": "outcomeVerificationUnavailable",
  "Current owner requirements have not been independently verified.": "outcomeVerificationUnavailable",
  "A dispatched write has no confirmed outcome.": "outcomeWriteUnconfirmed",
  "Resource permissions changed; prior results are not current verification.": "outcomeScopeChanged",
  "Verification did not establish the complete goal.": "outcomeGoalNotEstablished",
  "Registered mechanical checks passed; independent semantic assessment found the current goal covered.": "outcomeGoalEstablished",
  "Registered mechanical checks passed within their stated coverage; model semantic assessment found the current goal covered.": "outcomeGoalEstablished",
  "Only a semantic assessment is available; no registered mechanical goal check established completion.": "outcomeSemanticOnly",
  "One or more required conditions lack independent evidence.": "outcomeMissingEvidence",
  "agent_invalid_verification_response": "outcomeInvalidVerification",
  "Model changed; resume explicitly and reauthorize any resource access.": "outcomeModelChanged",
};

export default function RynAgent() {
  const { t } = useTranslation("rynAgent");
  const nav = useContext(AgentNavigationContext);
  const [settings, setSettings] = useState<AgentSettings | null>(null);
  const [models, setModels] = useState<AgentModel[]>([]);
  const [tasks, setTasks] = useState<AgentTaskSummary[]>([]);
  const [recordsSupported, setRecordsSupported] = useState(false);
  const [recordsRevision, setRecordsRevision] = useState(0);
  const [selected, setSelected] = useState(() => saved(SELECTED_KEY));
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const [task, setTask] = useState<AgentTask | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");
  const [resourceErrors, setResourceErrors] = useState({ settings: "", models: "", tasks: "", detail: "" });
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(() => Boolean(saved(PENDING_KEY)));
  const [controlUncertain, setControlUncertain] = useState(() => Boolean(saved(CONTROL_KEY)));
  const [tasksCollapsed, setTasksCollapsed] = useState(() => saved("ryn.agent.tasks") === "closed" || (window.innerWidth <= 760 && saved("ryn.agent.tasks") !== "open"));
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [drafts, setDrafts] = useState(readDrafts);
  const [model, setModel] = useState("");
  const [messageIntent, setMessageIntent] = useState<AgentMessageIntent>("steer");
  const [scopeDraft, setScopeDraft] = useState<ScopeDraft | null>(readScopeDraft);
  const [skillIDs, setSkillIDs] = useState<string[]>([]);
  const defaultModelRef = useRef("");
  const draft = drafts[selected || "new"] || "";
  const input = useRef<HTMLTextAreaElement>(null);
  const mutationRef = useRef(false);
  const deletedTaskIDs = useRef(new Set<string>());
  const refreshRevision = useRef(0);

  const acceptTask = useCallback((incoming: AgentTask) => {
    if (deletedTaskIDs.current.has(incoming.id)) return;
    if (incoming.archived !== undefined) setRecordsSupported(true);
    setTasks(current => sortAgentTasks([...(incoming.archived ? [] : [current.find(item => item.id === incoming.id && item.revision > incoming.revision) || incoming]), ...current.filter(item => item.id !== incoming.id)]));
    if (selectedRef.current === incoming.id) setTask(current => current?.id === incoming.id && current.revision > incoming.revision ? current : incoming);
  }, []);
  const acceptTaskList = useCallback((incoming: AgentTaskSummary[]) => {
    if (incoming.some(item => item.archived !== undefined)) setRecordsSupported(true);
    setTasks(current => sortAgentTasks(incoming.filter(item => !deletedTaskIDs.current.has(item.id)).map(item => current.find(savedTask => savedTask.id === item.id && savedTask.revision > item.revision) || item)));
  }, []);
  const recoverCreate = useCallback((summaries: AgentTaskSummary[]) => {
    let pendingID = "";
    try { pendingID = JSON.parse(saved(PENDING_KEY) || "{}").client_request_id || ""; } catch { /* Ignore old or unavailable storage. */ }
    const recovered = summaries.find(item => pendingID && item.client_request_id === pendingID && !deletedTaskIDs.current.has(item.id));
    if (recovered) {
      selectedRef.current = recovered.id; setSelected(recovered.id);
      save(PENDING_KEY, ""); setUncertain(false);
      setDrafts(current => ({ ...current, new: "" }));
    }
  }, []);
  const refresh = useCallback(async () => {
    const refreshID = ++refreshRevision.current;
    const [nextSettings, nextModels, nextTasks] = await Promise.allSettled([rynAgent.settings(), rynAgent.models(), rynAgent.tasks()]);
    if (refreshID !== refreshRevision.current) return;
    setResourceErrors(current => ({ ...current,
      settings: nextSettings.status === "rejected" ? nextSettings.reason instanceof AgentAPIError && [404, 501].includes(nextSettings.reason.status) ? "unavailable" : "settingsLoadError" : "",
      models: nextModels.status === "rejected" ? "modelsLoadError" : "",
      tasks: nextTasks.status === "rejected" ? "historyLoadError" : "",
    }));
    if (nextSettings.status === "fulfilled") {
      const previousDefault = defaultModelRef.current;
      const nextDefault = nextSettings.value.model_id;
      defaultModelRef.current = nextDefault;
      setSettings(nextSettings.value);
      setModel(current => !current || current === previousDefault ? nextDefault || (nextModels.status === "fulfilled" ? nextModels.value.models.find(canUseAgentModel)?.id : "") || "" : current);
    }
    // Do not offer a previously verified model while its current capability cannot be read.
    setModels(nextModels.status === "fulfilled" ? nextModels.value.models : []);
    if (nextTasks.status === "fulfilled") {
      acceptTaskList(nextTasks.value.tasks);
      recoverCreate(nextTasks.value.tasks);
    }
    // Detail remains independently readable even when the model catalog or task index fails.
    const currentID = selectedRef.current;
    if (currentID) {
      try {
        const detail = await rynAgent.task(currentID);
        if (refreshID !== refreshRevision.current || currentID !== selectedRef.current) return;
        acceptTask(detail); setResourceErrors(current => ({ ...current, detail: "" }));
      } catch {
        if (refreshID === refreshRevision.current && currentID === selectedRef.current) setResourceErrors(current => ({ ...current, detail: "detailLoadError" }));
      }
    }
  }, [acceptTask, acceptTaskList, recoverCreate]);

  const defaultWriteReceipt = task?.events.filter(item => item.type === "tool_result" && item.tool === "ryn_set_default_agent_model").at(-1)?.seq;
  useEffect(() => {
    if (defaultWriteReceipt !== undefined) void refresh().catch(() => setError("loadError"));
  }, [task?.id, defaultWriteReceipt, refresh]);

  useEffect(() => {
    let active = true;
    void refresh().then(() => { if (active) setError(""); }).catch(reason => {
      if (active) setError(reason instanceof AgentAPIError && [404, 501].includes(reason.status) ? "unavailable" : "loadError");
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; refreshRevision.current++; };
  }, [refresh]);
  useEffect(() => {
    const onFocus = () => { void refresh().catch(() => setError("loadError")); };
    const onVisible = () => { if (document.visibilityState === "visible") onFocus(); };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisible);
    return () => { window.removeEventListener("focus", onFocus); document.removeEventListener("visibilitychange", onVisible); };
  }, [refresh]);
  useEffect(() => { save(DRAFT_KEY, JSON.stringify(drafts)); }, [drafts]);
  useEffect(() => { save(SCOPE_DRAFT_KEY, scopeDraft ? JSON.stringify(scopeDraft) : ""); }, [scopeDraft]);
  useEffect(() => {
    save(SELECTED_KEY, selected);
    if (!selected) { setTask(null); setResourceErrors(current => ({ ...current, detail: "" })); return; }
    const abort = new AbortController();
    setDetailLoading(true);
    void rynAgent.task(selected, abort.signal).then(incoming => { if (!abort.signal.aborted) { acceptTask(incoming); setResourceErrors(current => ({ ...current, detail: "" })); } })
      .catch(() => { if (!abort.signal.aborted) setResourceErrors(current => ({ ...current, detail: "detailLoadError" })); })
      .finally(() => { if (!abort.signal.aborted) setDetailLoading(false); });
    return () => abort.abort();
  }, [selected, acceptTask]);
  useEffect(() => {
    if (loading) return;
    let stopped = false;
    let timer: number;
    const abort = new AbortController();
    const poll = async () => {
      let activeTasks = false;
      const id = selectedRef.current;
      const [summary, detail] = await Promise.allSettled([rynAgent.tasks(abort.signal), id ? rynAgent.task(id, abort.signal) : Promise.resolve(null)]);
      if (stopped) return;
      if (summary.status === "fulfilled") {
        acceptTaskList(summary.value.tasks);
        setResourceErrors(current => ({ ...current, tasks: "" }));
        recoverCreate(summary.value.tasks);
        activeTasks = summary.value.tasks.some(item => isAgentActive(agentTaskState(item)));
      } else setResourceErrors(current => ({ ...current, tasks: "historyLoadError" }));
      if (id && selectedRef.current === id) {
        if (detail.status === "fulfilled" && detail.value) {
          acceptTask(detail.value); setResourceErrors(current => ({ ...current, detail: "" }));
          activeTasks ||= isAgentActive(agentTaskState(detail.value));
        } else setResourceErrors(current => ({ ...current, detail: "detailLoadError" }));
      }
      if (!stopped) timer = window.setTimeout(poll, activeTasks ? 1200 : 5000);
    };
    timer = window.setTimeout(poll, 1200);
    return () => { stopped = true; abort.abort(); window.clearTimeout(timer); };
  // Poll server snapshots, never append repeated messages or events.
  }, [loading, selected, acceptTask, acceptTaskList, recoverCreate]);

  const changeDraft = (value: string) => setDrafts(current => ({ ...current, [selected || "new"]: value }));
  const newTask = async () => {
    if (mutationRef.current) return;
    mutationRef.current = true;
    selectedRef.current = ""; setSelected(""); setTask(null); setBusy(true);
    try { await refresh(); } catch { setError("loadError"); }
    finally { mutationRef.current = false; setBusy(false); input.current?.focus(); }
  };
  const chooseTask = (id: string) => { selectedRef.current = id; setSelected(id); setTask(null); };
  const changeRecord = (incoming: AgentTask) => { acceptTask(incoming); setRecordsRevision(current => current + 1); };
  const deleteRecord = (id: string) => {
    deletedTaskIDs.current.add(id);
    setTasks(current => current.filter(item => item.id !== id)); setRecordsRevision(current => current + 1);
    setDrafts(current => { const next = { ...current }; delete next[id]; return next; });
    if (selectedRef.current === id) { selectedRef.current = ""; setSelected(""); setTask(null); setResourceErrors(current => ({ ...current, detail: "" })); }
  };
  const reload = async () => {
    setBusy(true);
    try { await refresh(); setError(""); } catch { setError("loadError"); }
    finally { setBusy(false); }
  };
  const updateSettings = async (update: Partial<AgentSettings>) => {
    if (mutationRef.current || resourceErrors.settings) return false;
    mutationRef.current = true; setBusy(true); setError("");
    try { setSettings(await rynAgent.updateSettings(update)); await refresh(); return true; }
    catch { setError("mutationError"); await refresh().catch(() => undefined); return false; }
    finally { mutationRef.current = false; setBusy(false); }
  };
  const submitControl = async (pending: PendingControl) => {
    // Save the complete body before sending; an explicit retry must retain its original versions.
    save(CONTROL_KEY, JSON.stringify(pending));
    try {
      const next = await rynAgent.control(pending.task_id, pending.route, pending.body);
      save(CONTROL_KEY, ""); setControlUncertain(false);
      return next;
    } catch (reason) {
      if (reason instanceof AgentAPIError && reason.status < 500) { save(CONTROL_KEY, ""); setControlUncertain(false); }
      else setControlUncertain(true);
      throw reason;
    }
  };
  const action = async (kind: AgentAction) => {
    if (!task || mutationRef.current || (controlUncertain && !["pause", "cancel"].includes(kind)) || !canUseAgentAction(task, kind, true) || (resourceErrors.settings && !task.allowed_actions && !["pause", "cancel", "deny"].includes(kind))) return;
    mutationRef.current = true; setBusy(true); setError("");
    try { acceptTask(task.allowed_actions && !controlUncertain ? await submitControl({ task_id: task.id, route: "actions", body: agentActionBody(task, kind) }) : await rynAgent.action(task, kind)); }
    catch (reason) {
      setError(reason instanceof AgentAPIError && reason.status === 409 ? "conflict" : "mutationError");
      await rynAgent.task(task.id).then(acceptTask).catch(() => undefined);
    } finally { mutationRef.current = false; setBusy(false); }
  };
  const answer = async (answers: Record<string, string>) => {
    if (!task?.pending_input || mutationRef.current || controlUncertain || !canUseAgentAction(task, "answer")) return;
    mutationRef.current = true; setBusy(true); setError("");
    try { acceptTask(await submitControl({ task_id: task.id, route: "actions", body: agentAnswerBody(task, answers) })); }
    catch (reason) {
      setError(reason instanceof AgentAPIError && reason.status === 409 ? "inputConflict" : "mutationError");
      await rynAgent.task(task.id).then(acceptTask).catch(() => undefined);
    } finally { mutationRef.current = false; setBusy(false); }
  };
  const updateScope = async (snapshot: AgentScopeSnapshot, scope: AgentScopeSelection) => {
    if (!task || mutationRef.current || controlUncertain || !canUseAgentAction(task, "scope")) return false;
    mutationRef.current = true; setBusy(true); setError("");
    try { acceptTask(await submitControl({ task_id: task.id, route: "scope", body: agentScopeBody(snapshot, scope) })); return true; }
    catch (reason) { setError(reason instanceof AgentAPIError && reason.status === 409 ? "scopeConflict" : "mutationError"); await rynAgent.task(task.id).then(acceptTask).catch(() => undefined); return false; }
    finally { mutationRef.current = false; setBusy(false); }
  };
  const send = async () => {
    const effectiveModel = task?.model_id || model;
    const currentTask = task;
    const legacyReady = settings?.enabled && !resourceErrors.settings && models.some(item => item.id === effectiveModel && canUseAgentModel(item));
    if (!draft.trim() || mutationRef.current || uncertain || controlUncertain || (!selected && !newScopeReady)) return;
    if (selected ? !currentTask || (currentTask.allowed_actions ? !canUseAgentAction(currentTask, effectiveIntent) : !legacyReady || !canContinueAgentTask(currentTask)) : !legacyReady) return;
    mutationRef.current = true; setBusy(true); setError("");
    const submittedMessage = draft.trim();
    const requestID = crypto.randomUUID();
    const newScope = !currentTask && scopeDraft?.scope.resources.length ? scopeDraft.scope : undefined;
    const destinationRevision = newScope ? scopeDraft?.destination_revision || undefined : undefined;
    if (!currentTask) save(PENDING_KEY, JSON.stringify({ client_request_id: requestID, message: submittedMessage, model_id: model, ...(newScope ? { scope: newScope, expected_destination_revision: destinationRevision } : {}), ...(skillIDs.length ? { skill_ids: skillIDs } : {}), at: Date.now() }));
    try {
      const next = currentTask ? currentTask.allowed_actions ? await submitControl({ task_id: currentTask.id, route: "messages", body: agentMessageBody(currentTask, submittedMessage, effectiveIntent) }) : await rynAgent.message(currentTask, submittedMessage) : await rynAgent.create(submittedMessage, model, requestID, newScope, destinationRevision, skillIDs);
      if (!currentTask && !selectedRef.current) { selectedRef.current = next.id; setSelected(next.id); }
      acceptTask(next);
      setDrafts(current => current[selected || "new"]?.trim() === submittedMessage ? { ...current, [selected || "new"]: "" } : current);
      save(PENDING_KEY, ""); setUncertain(false);
      if (!currentTask) { setScopeDraft(null); setSkillIDs([]); }
    } catch (reason) {
      if (!currentTask && (!(reason instanceof AgentAPIError) || reason.status >= 500)) setUncertain(true);
      else save(PENDING_KEY, "");
      setError(reason instanceof AgentAPIError && reason.status === 409 ? "conflict" : "mutationError");
      await refresh().catch(() => undefined);
    } finally { mutationRef.current = false; setBusy(false); }
  };
  const recoverRequest = async () => {
    if (mutationRef.current) return;
    let pending: { client_request_id: string; message: string; model_id: string; scope?: AgentScopeSelection; expected_destination_revision?: string; skill_ids?: string[] };
    try { pending = JSON.parse(saved(PENDING_KEY)); if (!pending.client_request_id || !pending.message || !pending.model_id) return; }
    catch { return; }
    mutationRef.current = true; setBusy(true);
    try {
      const next = await rynAgent.create(pending.message, pending.model_id, pending.client_request_id, pending.scope, pending.expected_destination_revision, pending.skill_ids);
      selectedRef.current = next.id; setSelected(next.id); acceptTask(next);
      save(PENDING_KEY, ""); setUncertain(false); setError("");
      setDrafts(current => ({ ...current, new: "" }));
      setScopeDraft(null); setSkillIDs([]);
    } catch { setError("mutationError"); await refresh().catch(() => undefined); }
    finally { mutationRef.current = false; setBusy(false); }
  };
  const recoverControl = async () => {
    if (mutationRef.current) return;
    let pending: PendingControl;
    try { pending = JSON.parse(saved(CONTROL_KEY)); if (!pending.task_id || !["messages", "actions", "scope"].includes(pending.route) || !pending.body?.client_request_id) return; } catch { return; }
    mutationRef.current = true; setBusy(true); setError("");
    try {
      acceptTask(await submitControl(pending));
      if (pending.route === "messages") setDrafts(current => current[pending.task_id]?.trim() === pending.body.message ? { ...current, [pending.task_id]: "" } : current);
    } catch (reason) { setError(reason instanceof AgentAPIError && reason.status === 409 ? "conflict" : "mutationError"); await refresh().catch(() => undefined); }
    finally { mutationRef.current = false; setBusy(false); }
  };
  const settingsReady = Boolean(settings) && !resourceErrors.settings;
  const enabled = settingsReady && settings?.enabled;
  const messageIntents: AgentMessageIntent[] = task?.allowed_actions ? (["steer", "information"] as const).filter(intent => canUseAgentAction(task, intent)) : [];
  const effectiveIntent = messageIntents.includes(messageIntent) ? messageIntent : messageIntents[0] || "information";
  const backgroundTasks = tasks.filter(item => item.id !== selected && isAgentActive(agentTaskState(item)));
  const selectedModel = models.find(item => item.id === (task?.model_id || model));
  const newScopeReady = !scopeDraft?.scope.resources.length || (scopeDraft.scope.allow_model_data && scopeDraft.model_id === model && Boolean(scopeDraft.destination_revision) && scopeDraft.destination_revision === selectedModel?.destination_revision);
  const visibleMessages = task?.messages.filter((message, index, messages) =>
    (message.role === "user" || message.role === "assistant") && message.content
    && !(task.result && index === messages.length - 1 && message.role === "assistant" && message.content === task.result),
  ) || [];
  const legacySend = enabled && selectedModel && canUseAgentModel(selectedModel) && (!selected || (task && canContinueAgentTask(task)));
  const canSend = draft.trim() && !busy && !uncertain && !controlUncertain && (!selected ? newScopeReady : true) && (task?.allowed_actions ? canUseAgentAction(task, effectiveIntent) : legacySend);
  const sendLabel = task?.allowed_actions ? effectiveIntent === "steer" ? "sendSteer" : "sendInformation" : selected ? "followup" : "send";
  useEffect(() => { if (task) setMessageIntent(isAgentActive(agentTaskState(task)) ? "steer" : "information"); }, [task?.id, task ? isAgentActive(agentTaskState(task)) : false]);
  const modelSelect = (disabled = false) => <select aria-label={t("model")} title={t(task ? "taskModel" : "newTaskModel")} value={task?.model_id || model} disabled={disabled || busy} onChange={event => setModel(event.target.value)}>
    <option value="">{t("chooseModel")}</option>
    {(task?.model_id || model) && !models.some(item => item.id === (task?.model_id || model)) && <option value={task?.model_id || model} disabled>{task?.model_id || model}</option>}
    {models.map(item => <option key={item.id} value={item.id} disabled={!canUseAgentModel(item)}>{item.name} · {t(`source_${item.source}`, { defaultValue: item.source })}{!canUseAgentModel(item) ? ` — ${item.description || t(item.available ? "notSupported" : "notAvailable")}` : ""}</option>)}
  </select>;

  return <section className={`ra-workspace${tasksCollapsed ? " ra-tasks-collapsed" : ""}`} aria-label={t("title")}>
    <header className="ra-header">
      <div className="ra-header-left">
        <button className="ra-icon" aria-label={t(nav.collapsed ? "showNav" : "hideNav")} aria-expanded={!nav.collapsed} onClick={nav.toggle}>{nav.collapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}</button>
        <button className="ra-icon" aria-label={t(tasksCollapsed ? "showTasks" : "hideTasks")} aria-expanded={!tasksCollapsed} aria-controls="ryn-agent-task-list" onClick={() => { setTasksCollapsed(!tasksCollapsed); save("ryn.agent.tasks", tasksCollapsed ? "open" : "closed"); }}><ListChecks size={18} /></button>
        <strong>{t("title")}</strong>
        <button className="ra-icon" aria-label={t("newTask")} title={t("newTask")} onClick={newTask}><SquarePen size={17} /></button>
        {task && <span className="ra-current-title" title={task.title}>{task.title}</span>}
      </div>
      <div className="ra-header-right"><details className="ra-chat-menu" onKeyDown={event => { if (event.key === "Escape") { event.currentTarget.open = false; event.currentTarget.querySelector<HTMLElement>("summary")?.focus(); } }}><summary className="ra-icon" aria-label={t("chatActions")} title={t("chatActions")}><MoreHorizontal size={19} /></summary><div className="ra-chat-menu-panel">        {backgroundTasks.length > 0 && <div className="ra-menu-background" role="status"><span>{t("backgroundHelp")}</span><div>{backgroundTasks.map(item => <button key={item.id} aria-label={t("openBackground", { title: item.title, status: t(agentTaskState(item)) })} onClick={() => chooseTask(item.id)}>{item.title} · {t(agentTaskState(item))}</button>)}</div></div>}{task && <>            <div className="ra-status" role="status"><span title={t("steps", { count: task.steps })}>{t(agentTaskState(task))}</span><div>
              {canUseAgentAction(task, "pause", ["running", "queued"].includes(task.status)) && <button disabled={busy} onClick={() => void action("pause")}><Pause size={14} />{t("pause")}</button>}
              {canResumeAgentTask(task) && (task.allowed_actions || enabled) && <button disabled={busy || controlUncertain} onClick={() => void action("resume")}><Play size={14} />{t("resume")}</button>}
              {canUseAgentAction(task, "cancel", !["completed", "cancelled", "failed", "interrupted"].includes(task.status)) && <button disabled={busy} onClick={() => void action("cancel")}><X size={14} />{t("cancel")}</button>}
            </div></div>
            <details className="ra-task-options"><summary><SlidersHorizontal size={14} />{t("taskOptions")}</summary><div className="ra-task-controls">
              <TaskContext task={task} />
              <RynAgentBudget key={`budget/${task.id}`} task={task} busy={busy || controlUncertain} onChange={acceptTask} />
              <RynAgentModel key={`model/${task.id}`} task={task} models={models} defaultModelID={settings?.model_id} busy={busy || controlUncertain} onChange={acceptTask} />
              {task.allowed_actions?.scope && <RynAgentScope key={`scope/${task.id}/${task.scope_revision || 0}/${task.model_id}`} task={task} modelName={selectedModel?.name || task.model_id} busy={busy || controlUncertain} onSave={updateScope} />}
            </div></details>
            <TaskProgress task={task} />
            <details className="ra-activity"><summary>{t("activity")}</summary>{task.events.filter(event => ["tool_started", "tool_result", "approved", "denied", "approval_requested"].includes(event.type)).map(event => <div className="ra-event" key={event.seq}><strong>{t(event.type === "tool_result" ? event.internal === true ? "internalReceipt" : `receipt_${agentToolResultState(event)}` : event.type)} {event.tool && <span title={event.tool}>{t(`tool_${event.tool}`, { defaultValue: event.tool })}</span>}</strong>{isAgentLateToolResult(event, task.events) && <p>{t("lateReceipt")}</p>}{event.result !== undefined && <details className="ra-event-evidence"><summary>{t("viewEvidence")}</summary><pre aria-label={t("evidence")}>{json(event.result)}</pre></details>}</div>)}</details>            {task.outcome && (task.run_state === "ended" || ["completed", "failed", "cancelled", "interrupted"].includes(task.status)) && <section className="ra-result ra-outcome" aria-label={t("outcome")}>
              <strong>{t("outcome")} · {t(`outcome_${task.outcome.status}`)} · {t("goalRevision", { revision: task.outcome.goal_revision })}</strong>
              <p>{t(task.outcome.status === "verified" ? "outcomeVerifiedHelp" : "outcomeUnverifiedHelp")}</p>
              {task.outcome.summary && task.outcome.summary !== task.result && <ChatMarkdown>{outcomeReasons[task.outcome.summary] ? t(outcomeReasons[task.outcome.summary]) : task.outcome.summary}</ChatMarkdown>}
              {task.outcome.uncertainties.length > 0 && <ul>{task.outcome.uncertainties.map((item, index) => <li key={index}>{outcomeReasons[item] ? t(outcomeReasons[item]) : item}</li>)}</ul>}
              {(task.outcome.evidence_refs.length > 0 || task.outcome.criteria_results.length > 0) && <details className="ra-exact-details"><summary>{t("outcomeEvidence")}</summary><pre>{json({ criteria_results: task.outcome.criteria_results, evidence_refs: task.outcome.evidence_refs })}</pre></details>}
            </section>}
          <details className="ra-record-options"><summary>{t("manageRecords")}</summary><TaskRecords task={task} busy={busy || controlUncertain} onChange={changeRecord} onDelete={deleteRecord} /></details></>}</div></details><Link to="/services/sources">{t("models")}</Link><button className="ra-icon" aria-label={t("settings")} disabled={!settingsReady} onClick={() => setSettingsOpen(true)}><Settings size={18} /></button></div>
    </header>
    {error && <div role="alert" className="ra-banner"><CircleAlert size={17} /><span>{t(error)}</span><button onClick={() => void reload()} disabled={busy}>{t("retry")}</button></div>}
    {Object.values(resourceErrors).filter(Boolean).map(key => <div key={key} role="alert" className="ra-banner"><CircleAlert size={17} /><span>{t(key)}</span><button onClick={() => void reload()} disabled={busy}>{t("retry")}</button></div>)}
    {uncertain && <div role="alert" className="ra-banner">{t("uncertain")}<button disabled={busy} onClick={() => void recoverRequest()}>{t("recoverRequest")}</button></div>}
    {controlUncertain && <div role="alert" className="ra-banner">{t("controlUncertain")}<button disabled={busy} onClick={() => void recoverControl()}>{t("recoverControl")}</button></div>}
    {loading ? <div className="ra-empty" role="status">{t("loading")}</div> : <div className="ra-layout">
      <aside id="ryn-agent-task-list" className="ra-tasks" aria-label={t("tasks")} hidden={tasksCollapsed}>
        <button className="ra-new" onClick={newTask}><SquarePen size={17} />{t("newTask")}</button><small>{t("recent")}</small>
        <div className="ra-task-items">{tasks.length ? tasks.map(item => <button key={item.id} className={`ra-task-item${selected === item.id ? " selected" : ""}`} aria-pressed={selected === item.id} onClick={() => chooseTask(item.id)}><span>{item.title}</span><small>{t(agentTaskState(item))}</small></button>) : <p>{t("emptyTasks")}</p>}</div>
        <ArchivedTasks enabled={recordsSupported} selected={selected} onChoose={chooseTask} revision={recordsRevision} />
        <small className="ra-local"><Monitor size={14} />{t("localHistory")}</small>
      </aside>
      <div className="ra-chat">

        {settingsReady && !enabled && task && <div className="ra-disabled-banner"><span>{t("historyDisabled")}</span><button disabled={busy} onClick={() => void updateSettings({ enabled: true, ...(model ? { model_id: model } : {}) })}>{t("enable")}</button></div>}
        <div className="ra-scroll" aria-busy={detailLoading}>

          {detailLoading ? <div className="ra-empty" role="status">{t("loading")}</div> : task ? <div key={`thread/${task.id}`} className="ra-thread">
            {visibleMessages.map((message, index) => <div key={`${task.id}-${index}`} className={`ra-message ra-${message.role}`}>{message.role === "assistant" && !message.tool_calls?.length && message.ui?.kind !== "verification_feedback" && <div className="ra-reply-label"><Sparkles size={16} /><strong>Ryn</strong><small>{models.find(item => item.id === agentMessageModelID(task, message))?.name || agentMessageModelID(task, message) || t("replyModelUnknown")}</small></div>}{message.role === "assistant" && message.ui?.kind === "verification_feedback" ? <RynAgentVerification content={message.content} /> : message.role === "user" && message.ui?.kind === "source_credential" ? <section aria-label={t("credentialConfigured")}><strong>{t("credentialConfigured")}</strong><dl className="ra-answer-record"><div><dt>{t("sourceEndpoint")}</dt><dd>{message.ui.endpoint || t("unknownState")}</dd></div><div><dt>{t("credential")}</dt><dd>{t(message.ui.has_credential === true ? "credentialConfigured" : "unknownState")}</dd></div></dl></section> : message.role === "user" && message.ui?.kind === "input_answer" ? <section aria-label={t("answerRecorded")}><strong>{t("answerRecorded")}</strong><dl className="ra-answer-record">{message.ui.fields?.map((field, fieldIndex) => <div key={fieldIndex}><dt>{field.label}</dt><dd>{field.value}</dd></div>)}</dl></section> : message.role === "assistant" && Boolean(message.tool_calls?.length) ? <details className="ra-thinking"><summary>{t("thinkingDetails")}</summary><ChatMarkdown>{message.content || ""}</ChatMarkdown></details> : <><strong className="ra-message-kind">{message.role === "user" && message.ui?.kind === "steer" ? t("requirementsRecorded") : message.role === "user" && message.ui?.kind === "information" ? t("informationRecorded") : ""}</strong><ChatMarkdown>{message.role === "user" ? message.ui?.text ?? message.content ?? "" : message.content || ""}</ChatMarkdown></>}</div>)}
            {pendingAgentText(task) && <div className="ra-message ra-assistant" aria-live="polite"><div className="ra-reply-label"><Sparkles size={16} /><strong>Ryn</strong><small>{t("partialResponse")}</small></div><ChatMarkdown>{pendingAgentText(task)}</ChatMarkdown></div>}
            <ExecutionProgress task={task} />

            {task.pending_input && (task.pending_input.kind === "credential_ref" ? <RynAgentInputCard key={`${task.id}/${task.pending_input.id}`} task={task} busy={busy || controlUncertain} onSaved={acceptTask} onChangeMethod={() => { changeDraft(t("cliSourceClarification")); setMessageIntent("steer"); input.current?.focus(); }} /> : <InputRequest key={`${task.id}/${task.pending_input.id}`} task={task} busy={busy || controlUncertain} onAnswer={answer} />)}
            {task.pending_approval && <ApprovalCard approval={task.pending_approval} controlled={Boolean(task.allowed_actions)} enabled={canUseAgentAction(task, "approve", Boolean(enabled))} canDeny={canUseAgentAction(task, "deny", !settingsReady || Boolean(enabled))} busy={busy || controlUncertain} waiting={agentTaskState(task) === "waiting_approval"} onAction={action} />}
            {task.status === "interrupted" && <p className="ra-banner">{t(task.error === "agent_node_restarted" ? "restartHelp" : "interruptedHelp")}<code>{task.error}</code></p>}
            {task.status === "cancelled" && <p className="ra-turn-evidence">{t("cancelHelp")}</p>}
            {task.result && <section className="ra-result"><strong>{t("result")}</strong><ChatMarkdown>{task.result}</ChatMarkdown></section>}
            <TaskResultFiles key={`results/${task.id}`} task={task} />
            <TaskArtifacts key={`artifacts/${task.id}`} task={task} />
              {task.error && task.status !== "interrupted" && <section className="ra-task-error" role="alert"><strong>{t("error")}</strong><p>{t("unknownError")}</p><code>{task.error}</code></section>}
          </div> : !settingsReady ? <div className="ra-empty"><CircleAlert size={32} /><p>{t(resourceErrors.settings || "settingsLoadError")}</p></div> : !enabled ? <div key="welcome" className="ra-welcome"><Sparkles className="ra-welcome-symbol" size={42} /><h2>{t("disabledTitle")}</h2><p>{t("disabledHelp")}</p><label className="ra-enable-model">{t("model")}{modelSelect()}</label>{!models.some(canUseAgentModel) && <p>{t("noModel")} <Link to="/services/sources">{t("configureModels")}</Link></p>}<button className="ra-primary" disabled={busy || !selectedModel || !canUseAgentModel(selectedModel)} onClick={() => void updateSettings({ enabled: true, ...(model ? { model_id: model } : {}) })}>{t("enable")}</button><small>{t("enableHelp")}</small></div> : <div key="welcome" className="ra-welcome"><Sparkles className="ra-welcome-symbol" size={42} /><h2>{t("welcome")}</h2><p>{t("welcomeHelp")}</p><div className="ra-suggestions">{([["inspect", ListChecks], ["sharing", Share2], ["devices", Network]] as const).map(([key, Icon]) => <button key={key} onClick={() => { changeDraft(t(`${key}Prompt`)); input.current?.focus(); }}><Icon size={15} />{t(key)}</button>)}</div></div>}

        </div>
        {(enabled || task) && !task?.archived && <div className="ra-composer-area">


          {!models.some(canUseAgentModel) && <p className="ra-model-help">{t("noModel")} <Link to="/services/sources">{t("models")}</Link></p>}

          <div className="ra-composer"><textarea ref={input} aria-label={t("input")} placeholder={t("placeholder")} value={draft} disabled={(!task?.allowed_actions && !enabled) || detailLoading} onChange={event => changeDraft(event.target.value)} onKeyDown={event => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); void send(); } }} /><div className="ra-composer-controls"><div>{modelSelect(Boolean(selected))}<details className="ra-composer-options"><summary><SlidersHorizontal size={14} />{t("composerOptions")}</summary><div className="ra-composer-options-panel">          {!selected && enabled && <RynAgentScope key="new-scope" task={null} modelID={model} modelName={selectedModel?.name || model} destinationRevision={selectedModel?.destination_revision} sourceName={selectedModel?.source_name} sourceID={selectedModel?.source_id} providerID={selectedModel?.peer_id} initialSelection={scopeDraft?.scope} busy={busy || uncertain || controlUncertain} onSelect={(scope, model_id, destination_revision) => setScopeDraft({ scope, model_id, destination_revision })} />}{!selected && <SkillSelection value={skillIDs} onChange={setSkillIDs} disabled={busy || uncertain} />}<p className="ra-model-context">{task ? t("taskModel") : t("newTaskModel")}: {selectedModel?.name || task?.model_id || model || t("chooseModel")}{settings && <span> · {t("defaultModel")}: {models.find(item => item.id === settings.model_id)?.name || settings.model_id || t("chooseModel")}</span>}</p>{messageIntents.length > 1 && <select aria-label={t("messageIntent")} value={effectiveIntent} disabled={busy} onChange={event => setMessageIntent(event.target.value as AgentMessageIntent)}><option value="steer">{t("intentSteer")}</option><option value="information">{t("intentInformation")}</option></select>}<select aria-label={t("mode")} value={task ? task.permission_mode || "unknown" : settings?.permission_mode || "confirm_changes"} disabled={busy || Boolean(selected)} onChange={event => void updateSettings({ permission_mode: event.target.value as AgentSettings["permission_mode"] })}>{task && !task.permission_mode && <option value="unknown">{t("unknownMode")}</option>}<option value="read_only">{t("readOnly")}</option><option value="confirm_changes">{t("confirmChanges")}</option><option value="automatic">{t("automatic")}</option></select>{task?.allowed_actions && messageIntents.length > 0 && <p className="ra-model-context">{t(effectiveIntent === "steer" ? "steerHelp" : "informationHelp")}</p>}</div></details></div>{task && ["running", "queued"].includes(agentTaskState(task)) && canUseAgentAction(task, "cancel", true) && !draft.trim() ? <button className="ra-primary ra-send" aria-label={t("stop")} disabled={busy || controlUncertain} onClick={() => void action("cancel")}><Square size={14} fill="currentColor" /></button> : <button className="ra-primary ra-send" aria-label={t(sendLabel)} disabled={!canSend} onClick={() => void send()}><ArrowUp size={18} /></button>}</div></div>

        </div>}
      </div>
    </div>}
    {settingsOpen && settings && <AgentSettingsDialog settings={settings} busy={busy} onClose={() => setSettingsOpen(false)} onSave={async update => { if (await updateSettings(update)) setSettingsOpen(false); }} />}
  </section>;
}

function ExecutionProgress({ task }: { task: AgentTask }) {
  const { t } = useTranslation("rynAgent");
  if (!["running", "queued", "waiting_input", "waiting_approval"].includes(agentTaskState(task))) return null;
  const latest = [...task.events].reverse().find(event => ["model_started", "tool_started", "tool_result", "verification_started", "verification_model_started"].includes(event.type));
  const phase = task.pending_input ? "progressInput" : task.pending_approval ? "progressApproval"
    : latest?.type.startsWith("verification_") ? "progressVerify"
    : latest?.type === "tool_started" ? "progressWorking" : "progressThinking";
  const tool = latest?.type === "tool_started" && latest.tool
    ? t(`tool_${latest.tool}`, { defaultValue: t("progressWorking") }) : "";
  return <div className="ra-execution-progress" role="status" aria-live="polite"><Sparkles size={16} /><span>{t(phase)}{tool && tool !== t(phase) ? ` · ${tool}` : ""}</span></div>;
}

function TaskContext({ task }: { task: AgentTask }) {
  const { t } = useTranslation("rynAgent");
  return <>
    {task.goal && <details className="ra-task-context"><summary>{t("goal")} · {t("goalRevision", { revision: task.goal.revision })}</summary><ChatMarkdown>{task.goal.text}</ChatMarkdown>{task.goal.constraints.length > 0 && <ul>{task.goal.constraints.map(constraint => <li key={constraint.id}>{constraint.text}{constraint.required && <small> · {t("requiredConstraint")}</small>}</li>)}</ul>}{task.criteria && task.criteria.length > 0 && <><strong>{t("criteria")}</strong><ul>{task.criteria.map(criterion => <li key={criterion.id}>{criterion.description}<small> · {t(`criterion_${criterion.status}`)}{criterion.required && ` · ${t("requiredConstraint")}`}</small></li>)}</ul></>}</details>}
  </>;
}

function TaskProgress({ task }: { task: AgentTask }) {
  const { t } = useTranslation("rynAgent");
  return <>
    {task.plan && task.plan.steps.length > 0 && <section className="ra-plan" aria-label={t("plan")}><strong>{t("plan")} · {t("goalRevision", { revision: task.plan.goal_revision })}</strong><ol>{task.plan.steps.map(step => <li key={step.id}><span>{step.text}</span><small>{t(`plan_${step.status}`)}</small></li>)}</ol><small>{t("planHelp")}</small></section>}
    <TaskSkills skills={task.skills} />
  </>;
}

function InputRequest({ task, busy, onAnswer }: { task: AgentTask; busy: boolean; onAnswer: (answers: Record<string, string>) => Promise<void> }) {
  const { t } = useTranslation("rynAgent");
  const question = task.pending_input!;
  const storageKey = `ryn.agent.answer-draft.${task.id}.${question.id}`;
  const [answers, setAnswers] = useState<Record<string, string>>(() => { try { return JSON.parse(saved(storageKey) || "{}"); } catch { return {}; } });
  useEffect(() => { save(storageKey, JSON.stringify(answers)); }, [answers, storageKey]);
  const needsSecureInput = question.kind === "credential_ref" || question.fields.some(field => field.type === "credential_ref");
  const supported = question.fields.length > 0 && !needsSecureInput;
  const allowed = canUseAgentAction(task, "answer");
  const valid = supported && question.fields.every(field => !field.required || Boolean(answers[field.id]?.trim())) && question.fields.every(field => field.type !== "choice" || !answers[field.id] || field.options?.some(option => option.value === answers[field.id]));
  return <section className="ra-input-request ra-approval" aria-label={t("inputRequest")}>
    <strong>{t("inputRequest")}</strong><ChatMarkdown>{question.prompt}</ChatMarkdown>
    <p>{t("answerHelp")}</p>
    {needsSecureInput ? <p>{t("secureInputHelp")} <Link to="/services/sources">{t("configureModels")}</Link></p> : supported ? <div className="ra-answer-fields">{question.fields.map(field => <label key={field.id}><span>{field.label}{field.required ? ` · ${t("requiredField")}` : ""}</span>{field.type === "choice" ? <select value={answers[field.id] || ""} disabled={busy} onChange={event => setAnswers(current => ({ ...current, [field.id]: event.target.value }))}><option value="">{t("chooseAnswer")}</option>{field.options?.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select> : <input value={answers[field.id] || ""} disabled={busy} autoComplete="off" onChange={event => setAnswers(current => ({ ...current, [field.id]: event.target.value }))} />}</label>)}</div> : <p>{t("unsupportedInputHelp")}</p>}
    {supported && task.allowed_actions?.answer && <div className="ra-approval-actions"><button className="ra-primary" disabled={busy || !allowed || !valid} onClick={() => void onAnswer(answers)}>{t("answerQuestion")}</button></div>}
  </section>;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function ApprovalCard({ approval, controlled, enabled, canDeny, busy, waiting, onAction }: {
  approval: AgentApproval; controlled: boolean; enabled: boolean; canDeny: boolean; busy: boolean; waiting: boolean;
  onAction: (action: AgentAction) => Promise<void>;
}) {
  const { t } = useTranslation("rynAgent");
  const preview = record(approval.preview);
  const before = record(preview.before);
  const after = record(preview.after);
  const exactSharing = approval.tool === "ryn_share_model_with_friend";
  const sharing = approval.tool === "ryn_set_model_sharing" || exactSharing;
  const friend = approval.tool === "ryn_set_friend_model_access";
  const sourceDraft = approval.tool === "ryn_commit_source_draft";
  const defaultModel = approval.tool === "ryn_set_default_agent_model";
  const defaultTarget = record(preview.model);
  const configuration = record(preview.configuration);
  const sourceTest = record(preview.test);
  const testedModels = Array.isArray(sourceTest.models) ? sourceTest.models.map(record) : [];
  const target = sharing ? preview.model : friend ? preview.name : preview.title;
  const space = record(preview.space);
  const scopeValue = exactSharing ? after.scope : preview.scope;
  const sharingScope = scopeValue === "friends" ? "friendScope" : scopeValue === "personal_space" || typeof space.name === "string" ? "personalSpace" : "unknownState";
  const sharingGrants = Array.isArray(preview.allowed_friend_grants) ? preview.allowed_friend_grants.map(record) : null;
  const exactRecipients = Array.isArray(after.recipients) ? after.recipients.map(record) : null;
  const removedRecipients = exactRecipients && Array.isArray(before.friend_grants) ? before.friend_grants.map(record).filter(grant => grant.allowed === true && !exactRecipients.some(recipient => recipient.relationship_id === grant.relationship_id && recipient.peer_id === grant.peer_id)) : null;
  const oldState = sharing ? before.sharing : before.allowed;
  const newState = sharing ? after.sharing : after.allowed;
  const stateLabel = (state: unknown) => typeof state === "boolean"
    ? t(sharing ? state ? "shared" : "private" : state ? "allowed" : "notAllowed")
    : t("unknownState");
  return <section className="ra-approval" aria-label={t("approval")}>
    <div className="ra-result-head"><ListChecks size={18} /><strong>{t(sharing ? "modelSharingChange" : friend ? "friendAccessChange" : sourceDraft ? "sourceConfigChange" : defaultModel ? "defaultModelChange" : "approval")}</strong></div>
    <p>{t("approvalHelp")}</p>
    {defaultModel && <>
      <dl className="ra-change-summary">
        <dt>{t("before")}</dt><dd><code>{typeof before.model_id === "string" ? before.model_id : t("unknownState")}</code></dd>
        <dt>{t("after")}</dt><dd>{typeof defaultTarget.name === "string" ? defaultTarget.name : t("unknownState")}<br /><code>{typeof after.model_id === "string" ? after.model_id : t("unknownState")}</code></dd>
        <dt>{t("scopeSource")}</dt><dd>{typeof defaultTarget.source_name === "string" ? defaultTarget.source_name : typeof defaultTarget.source_id === "string" ? defaultTarget.source_id : t("unknownState")}</dd>
        <dt>{t("scopeProvider")}</dt><dd><code>{typeof defaultTarget.peer_id === "string" ? defaultTarget.peer_id : t("unknownState")}</code></dd>
      </dl>
      <p>{t("defaultModelChangeHelp")}</p>
    </>}
    {sourceDraft && <>
      <dl className="ra-change-summary">
        <dt>{t("sourceOperation")}</dt><dd>{t(preview.source_id ? "updateSource" : "createSource")}{preview.source_id ? ` · ${String(preview.source_id)}` : ""}</dd>
        <dt>{t("target")}</dt><dd>{typeof configuration.name === "string" ? configuration.name : t("unknownState")}</dd>
        <dt>{t("sourceEndpoint")}</dt><dd><code>{typeof configuration.base_url === "string" ? configuration.base_url : t("unknownState")}</code></dd>
        <dt>{t("models")}</dt><dd>{Array.isArray(configuration.models) ? configuration.models.filter(model => typeof model === "string").join(" · ") : t("unknownState")}</dd>
        <dt>{t("credential")}</dt><dd>{t(preview.has_credential === true ? "credentialConfigured" : "unknownState")}</dd>
        <dt>{t("sourceProbe")}</dt><dd>{testedModels.length ? testedModels.map((model, index) => <div key={index}>{String(model.model_id || "")} · {t("probeChat")}: {t(model.chat === "passed" ? "probePassed" : model.chat === "failed" ? "probeFailed" : "unknownState")} · {t("probeTools")}: {t(model.tool_support === "supported" ? "probePassed" : model.tool_support === "unsupported" ? "probeFailed" : "unknownState")}</div>) : t("unknownState")}</dd>
      </dl>
      <p>{t(preview.source_id ? "sourceUpdatePrivateHelp" : "sourceNewPrivateHelp")}</p>
      <p>{t("sourceProbeUsage", { count: typeof sourceTest.probe_requests === "number" ? sourceTest.probe_requests : "?" })}</p>
    </>}
    {(sharing || friend) && <dl className="ra-change-summary">
      <dt>{t("target")}</dt><dd>{typeof target === "string" && target ? target : String(preview.model_id || preview.service_id || approval.arguments.model_id || approval.arguments.service_id || "")}</dd>
      <dt>{t("scope")}</dt><dd>{t(sharing ? sharingScope : "friendScope")}{sharing && sharingScope === "personalSpace" && typeof space.name === "string" && space.name ? ` · ${space.name}` : ""}</dd>
      <dt>{t("before")}</dt><dd>{stateLabel(oldState)}</dd>
      <dt>{t("after")}</dt><dd className="ra-change-after">{stateLabel(newState)}</dd>
    </dl>}
    {sharing && typeof before.scope === "string" && before.scope !== scopeValue && <p>{t("sharingBeforeScope")}: {t(before.scope === "friends" ? "friendScope" : before.scope === "personal_space" ? "personalSpace" : "unknownState")}</p>}
    {exactSharing && <>
      <p><strong>{t("exactSharingRecipients")}</strong></p>
      {exactRecipients?.length ? <ul>{exactRecipients.map((recipient, index) => <li key={String(recipient.relationship_id || index)}>{typeof recipient.name === "string" ? recipient.name : ""} <code>{String(recipient.peer_id || "")}</code></li>)}</ul> : <p>{t("unknownState")}</p>}
      <p><strong>{t("exactSharingRevocations")}</strong></p>
      {removedRecipients === null ? <p>{t("unknownState")}</p> : removedRecipients.length ? <ul>{removedRecipients.map((recipient, index) => <li key={String(recipient.relationship_id || index)}><code>{String(recipient.peer_id || "")}</code></li>)}</ul> : <p>{t("exactSharingNoRevocations")}</p>}
      <p>{t("exactSharingHelp")}</p>
    </>}
    {sharing && !exactSharing && preview.scope === "friends" && sharingGrants !== null && <>
      <p><strong>{t("sharingRecipients")}</strong></p>
      {sharingGrants.length ? <ul>{sharingGrants.map((grant, index) => <li key={String(grant.relationship_id || index)}><code>{String(grant.peer_id || "")}</code> · {t(grant.effective === true ? "sharingGrantActive" : "sharingGrantInactive")}</li>)}</ul> : <p>{t("sharingNoRecipients")}</p>}
      <p>{t("sharingGrantHelp")}</p>
    </>}
    <details className="ra-exact-details">
      <summary>{t("exactDetails")}</summary>
      <dl>
        <dt>{t("tool")}</dt><dd><code>{approval.tool}</code></dd>
        <dt>{t("arguments")}</dt><dd><pre>{json(approval.arguments)}</pre></dd>
        {approval.preview !== undefined && <><dt>{t("preview")}</dt><dd><pre>{json(approval.preview)}</pre></dd></>}
        <dt>{t("fingerprint")}</dt><dd className="ra-fingerprint">{approval.arguments_sha256}</dd>
      </dl>
    </details>
    {!enabled && <p>{t(controlled ? "approvalUnavailable" : canDeny ? "approvalSettingsUnavailable" : "approvalDisabled")}</p>}
    <div className="ra-approval-actions">
      <button disabled={busy || !canDeny || !waiting} onClick={() => void onAction("deny")}><X size={15} />{t("deny")}</button>
      <button className="ra-primary" disabled={busy || !enabled || !waiting} onClick={() => void onAction("approve")}><Check size={15} />{t("approve")}</button>
    </div>
  </section>;
}

function AgentSettingsDialog({ settings, busy, onClose, onSave }: { settings: AgentSettings; busy: boolean; onClose: () => void; onSave: (settings: Partial<AgentSettings>) => Promise<void> }) {
  const { t } = useTranslation("rynAgent");
  const [enabled, setEnabled] = useState(settings.enabled);
  const [tools, setTools] = useState<AgentTool[] | null>(null);
  const [toolError, setToolError] = useState(false);
  const [allowed, setAllowed] = useState(settings.allowed_write_tools || []);
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => { const previous = document.activeElement as HTMLElement | null; close.current?.focus(); return () => previous?.focus(); }, []);
  useEffect(() => { let stopped = false; void rynAgent.tools().then(value => { if (!stopped) setTools(value.tools.filter(tool => tool.effect === "write")); }).catch(() => { if (!stopped) setToolError(true); }); return () => { stopped = true; }; }, []);
  return <div className="ra-modal-backdrop" onClick={event => { if (event.target === event.currentTarget && !busy) onClose(); }}><section className="ra-modal" role="dialog" aria-modal="true" aria-label={t("settings")} onKeyDown={event => { if (event.key === "Escape" && !busy) onClose(); if (event.key === "Tab") { const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled)')); const first = controls[0], last = controls.at(-1); if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); } } }}><div className="ra-modal-head"><h2>{t("settings")}</h2><button ref={close} className="ra-icon" disabled={busy} aria-label={t("close")} onClick={onClose}><X size={18} /></button></div><label className="ra-setting"><span>{t("enabledLabel")}<small>{t("enableHelp")}</small></span><input type="checkbox" checked={enabled} disabled={busy} onChange={event => setEnabled(event.target.checked)} /></label><PreferencesEditor /><p>{t("modeHelp")}</p><fieldset className="ra-tool-permissions"><legend>{t("allowedTools")}</legend><p>{t("allowedToolsHelp")}</p>{toolError ? <p role="alert">{t("toolsLoadError")}</p> : tools === null ? <p role="status">{t("loading")}</p> : tools.length ? tools.map(tool => <label key={tool.name}><input type="checkbox" disabled={busy} checked={allowed.includes(tool.name)} onChange={event => setAllowed(current => event.target.checked ? [...current, tool.name] : current.filter(name => name !== tool.name))} /><span>{t(`tool_${tool.name}`, { defaultValue: tool.title })}</span></label>) : <p>{t("noWriteTools")}</p>}</fieldset><button className="ra-primary" disabled={busy} onClick={() => void onSave({ enabled, ...(tools !== null ? { allowed_write_tools: allowed.filter(name => tools.some(tool => tool.name === name)) } : {}) })}>{t(busy ? "saving" : "save")}</button></section></div>;
}
