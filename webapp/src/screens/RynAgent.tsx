import { agentTaskCache, readAgentWorkspace, writeAgentWorkspace } from "../domain/agentWorkspaceCache";
import RynAgentProgress, { groupAgentMessages, RynAgentTurnDuration } from "./RynAgentProgress";
import { Fragment, useCallback, useContext, useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate as useRouterNavigate } from "react-router-dom";
import { Trans, useTranslation } from "react-i18next";
import { ArrowUp, BookOpen, Clock3, LoaderCircle, MoreHorizontal, Square, Check, CircleAlert, ListChecks, Monitor, Network, PanelLeftClose, PanelLeftOpen, Pause, Play, Settings, SlidersHorizontal, Share2, Sparkles, SquarePen, X } from "lucide-react";
import { AgentAPIError, agentReadBackoff, isTransientAgentReadError, mergeAgentTaskSnapshot, agentMessageModelID, agentActionBody, agentAnswerBody, agentMessageBody, agentScopeBody, agentToolResultState, canContinueAgentTask, canResumeAgentTask, canUseAgentModel, canUseAgentAction, agentTaskState, isAgentActive, isAgentLateToolResult, pendingAgentText, sortAgentTasks, rynAgent, type AgentAction, type AgentApproval, type AgentMessageIntent, type AgentModel, type AgentProject, type AgentScopeSelection, type AgentScopeSnapshot, type AgentSettings, type AgentTask, type AgentTaskSummary, type AgentTool } from "../domain/rynAgent";
import { AgentNavigationContext } from "../personal/agentNavigation";
import { craftAgentPluginID } from "../domain/craftPlugins";
import ChatMarkdown from "./ChatMarkdown";
import RynAgentStream from "./RynAgentStream";
import RynAgentScope from "./RynAgentScope";
import RynAgentModel from "./RynAgentModel";
import RynAgentModelPicker from "./RynAgentModelPicker";
import RynAgentComposer from "./RynAgentComposer";
import RynAgentMessageEditor from "./RynAgentMessageEditor";
import AgentMessageAttachments from "./AgentMessageAttachments";
import { agentAttachmentInputs, type AgentAttachmentInput, type AgentDraftAttachment } from "../domain/agentAttachments";
import RynAgentProject from "./RynAgentProject";
import { useAgentTaskMenu } from "./RynAgentTaskMenu";
import { useAgentTaskAttention } from "./useAgentTaskAttention";
import RynAgentInputCard from "./RynAgentInputCard";
import RynAgentVerification from "./RynAgentVerification";
import { MemoryEditor, PreferencesEditor, SkillSelection, TaskSkills } from "./RynAgentGuidance";
import RynAgentBudget from "./RynAgentBudget";
import { ArchivedTasks, TaskRecords, TaskResultFiles } from "./RynAgentRecords";
import { TaskArtifacts, TurnDeliverables } from "./RynAgentArtifacts";
import AgentTaskComputerStatus from "./AgentTaskComputerStatus";
import { AutomationChatCard, AutomationDialog, AutomationsPanel, extractTurnAutomations, HeartbeatWakeupDivider } from "./RynAgentAutomations";
import { CodexSkillsView, consumePendingSkillPrefill } from "./CodexSkillsView";
import type { AgentAutomation, AgentAutomationRun } from "../domain/rynAgent";

function playAutomationChime() {
  try {
    const AudioCtx = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(587.33, ctx.currentTime);
    osc.frequency.setValueAtTime(880, ctx.currentTime + 0.12);
    gain.gain.setValueAtTime(0.08, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.38);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.4);
  } catch { /* Ignore audio autoplay restrictions */ }
}
import "./rynAgentText";
import "./RynAgent.css";

function isMissingAgentTaskError(reason: unknown): boolean {
  return reason instanceof AgentAPIError && (
    reason.code === "agent_task_deleted" ||
    reason.code === "agent_task_not_found" ||
    reason.status === 404 ||
    reason.status === 410
  );
}

function mutationErrorKey(reason: unknown): string {
  if (!(reason instanceof AgentAPIError)) return "mutationError";
  if (reason.code === "agent_model_destination_unverifiable") return "modelDestinationUnavailable";
  if (reason.code === "agent_model_destination_changed") return "modelDestinationChanged";
  if (reason.code === "agent_model_unavailable") return "selectedModelUnavailable";
  if (["agent_attachment_limit", "agent_attachment_image_limit"].includes(reason.code)) return "attachmentLimit";
  if (reason.code.startsWith("agent_attachment_") || reason.code === "agent_invalid_attachment") return "attachmentReadError";
  if (reason.status === 409 && /(?:revision_conflict|invalid_transition)$/.test(reason.code)) return "conflict";
  return "mutationError";
}

const SELECTED_KEY = "ryn.agent.selected";
const DRAFT_KEY = "ryn.agent.drafts";
const PENDING_KEY = "ryn.agent.pending-create";
const CONTROL_KEY = "ryn.agent.pending-control";
const SCOPE_DRAFT_KEY = "ryn.agent.scope-draft";
const PROJECT_KEY = "ryn.agent.project";
const NAVIGATION_KEY = "ryn.agent.navigation";
const PENDING_MESSAGE_KEY = "ryn.agent.pending-message";
interface PendingMessage { requestID: string; taskID: string; draftKey: string; message: string; baseMessageCount: number; navigationRevision?: number; phase: "sending" | "uncertain" | "rejected"; }
function newDraftKey(projectID = "") { return projectID ? `new:${projectID}` : "new"; }
function readPendingMessage(): PendingMessage | null {
  try { const value = JSON.parse(saved(PENDING_MESSAGE_KEY)); return typeof value?.message === "string" && typeof value?.draftKey === "string" && typeof value?.taskID === "string" ? { ...value, phase: value.phase === "rejected" ? "rejected" : "uncertain" } : null; } catch { return null; }
}
interface PendingControl { task_id: string; route: "messages" | "actions" | "scope" | "workspace"; body: Record<string, unknown>; attachment_ids?: string[]; }
interface ScopeDraft { scope: AgentScopeSelection; model_id: string; destination_revision: string | null; }
function readScopeDraft(): ScopeDraft | null { try { const value = JSON.parse(saved(SCOPE_DRAFT_KEY)); return Array.isArray(value?.scope?.resources) ? value : null; } catch { return null; } }
const volatileSaved = new Map<string, string>();
function saved(key: string): string { if (volatileSaved.has(key)) return volatileSaved.get(key)!; try { return sessionStorage.getItem(key) || ""; } catch { return ""; } }
function save(key: string, value: string) {
  try { if (value) sessionStorage.setItem(key, value); else sessionStorage.removeItem(key); volatileSaved.delete(key); }
  catch { if (value) volatileSaved.set(key, value); else volatileSaved.delete(key); /* Retain exact attachment requests if browser storage is full. */ }
}
function readDrafts(): Record<string, string> { try { return JSON.parse(saved(DRAFT_KEY) || "{}"); } catch { return {}; } }
function readPendingAttachments(): Record<string, AgentDraftAttachment[]> {
  const drafts: Record<string, AgentDraftAttachment[]> = {};
  for (const key of [PENDING_KEY, CONTROL_KEY]) {
    try {
      const pending = JSON.parse(saved(key) || "null");
      const inputs = key === PENDING_KEY ? pending?.attachments : pending?.route === "messages" ? pending.body?.attachments : undefined;
      if (!Array.isArray(inputs)) continue;
      const target = key === PENDING_KEY ? pending.draft_key || newDraftKey(pending.project_id) : pending.task_id;
      if (typeof target !== "string") continue;
      drafts[target] = inputs.filter((file: AgentAttachmentInput) => typeof file?.name === "string" && (typeof file.path === "string" || typeof file.data_base64 === "string")).map((file: AgentAttachmentInput, index: number) => ({
        ...file, id: pending.attachment_ids?.[index] || `pending-${index}`,
        ...(file.data_base64 ? { size: Math.floor(file.data_base64.length * 3 / 4), ...(/^image\/(?:png|jpeg|webp|gif|bmp)$/i.test(file.media_type || "") ? { preview: `data:${file.media_type};base64,${file.data_base64}` } : {}) } : {}),
      }));
    } catch { /* Ignore unavailable or obsolete pending records. */ }
  }
  return drafts;
}
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
  const { t, i18n } = useTranslation("rynAgent");
  const location = useLocation();
  const routerNavigate = useRouterNavigate();
  const [pluginEntry] = useState(() => {
    const id = craftAgentPluginID(location.state);
    return id ? { id, draftKey: `new:plugin:${id}` } : null;
  });
  const [pluginDraftKey, setPluginDraftKey] = useState(pluginEntry?.draftKey || "");
  const nav = useContext(AgentNavigationContext);
  const taskCache = useRef(agentTaskCache());
  const [cachedWorkspace] = useState(readAgentWorkspace);
  const [settings, setSettings] = useState<AgentSettings | null>(cachedWorkspace.settings);
  const [models, setModels] = useState<AgentModel[]>(cachedWorkspace.models);
  const [tasks, setTasks] = useState<AgentTaskSummary[]>(() => sortAgentTasks([...taskCache.current.values()].filter(item => !item.archived)));
  const [tasksReady, setTasksReady] = useState(false);
  const [recordsSupported, setRecordsSupported] = useState(false);
  const [recordsRevision, setRecordsRevision] = useState(0);
  const [selected, setSelected] = useState(() => pluginEntry ? "" : saved(SELECTED_KEY));
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const [task, setTask] = useState<AgentTask | null>(() => taskCache.current.get(selected) || null);
  const [loading, setLoading] = useState(!cachedWorkspace.settings);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");
  const [resourceErrors, setResourceErrors] = useState({ settings: "", models: "", tasks: "", detail: "" });
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(() => Boolean(saved(PENDING_KEY)));
  const [controlUncertain, setControlUncertain] = useState(() => Boolean(saved(CONTROL_KEY)));
  const [tasksCollapsed, setTasksCollapsed] = useState(() => saved("ryn.agent.tasks") === "closed" || (window.innerWidth <= 760 && saved("ryn.agent.tasks") !== "open"));
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [automationsList, setAutomationsList] = useState<AgentAutomation[]>([]);
  const [automationRuns, setAutomationRuns] = useState<AgentAutomationRun[]>([]);
  const [activeAutoToast, setActiveAutoToast] = useState<NonNullable<AgentTaskSummary["automation_notification"]> | null>(null);
  const seenAutoNotifIds = useRef<Set<string> | null>(null);
  const [showAutomationsView, setShowAutomationsView] = useState(false);
  const [showSkillsView, setShowSkillsView] = useState(false);
  const [editingAutomation, setEditingAutomation] = useState<Partial<AgentAutomation> | null>(null);
  const [automationDialogOpen, setAutomationDialogOpen] = useState(false);
  const refreshAutomations = useCallback(() => {
    void rynAgent.automations().then(snap => { setAutomationsList(snap.items || []); setAutomationRuns(snap.runs || []); }).catch(() => undefined);
  }, []);
  const [taskModelOpen, setTaskModelOpen] = useState(false);
  const [taskModelTarget, setTaskModelTarget] = useState("");
  const [reasoningEffort, setReasoningEffort] = useState("");
  const [projectID, setProjectID] = useState(() => pluginEntry || saved(PROJECT_KEY) === "__general__" ? "" : saved(PROJECT_KEY));
  const [projects, setProjects] = useState<AgentProject[]>([]);
  const [navigationRevision, setNavigationRevision] = useState(() => (Number(saved(NAVIGATION_KEY)) || 0) + (pluginEntry ? 1 : 0));
  const navigationRef = useRef(navigationRevision);
  const initialNavigationRef = useRef(navigationRevision - (pluginEntry ? 1 : 0));
  const navigate = () => {
    const revision = ++navigationRef.current;
    setNavigationRevision(revision); save(NAVIGATION_KEY, String(revision));
    setShowAutomationsView(false);
    setShowSkillsView(false);
    refreshAutomations();
  };
  const chooseProject = (id: string) => {
    navigate();
    if (pluginDraftKey) setScopeDraft(readScopeDraft());
    setPluginDraftKey("");
    setProjectID(id); save(PROJECT_KEY, id || "__general__");
    selectedRef.current = ""; setSelected(""); setTask(null); setDetailLoading(false);
    input.current?.focus();
  };
  const [drafts, setDrafts] = useState(() => {
    const current = readDrafts();
    if (pluginEntry && !current[pluginEntry.draftKey]) current[pluginEntry.draftKey] = i18n.t("craft.agentQueryDraft", { app: pluginEntry.id, name: i18n.t(`craft.${pluginEntry.id}`) });
    return current;
  });
  const [pendingMessage, setPendingMessage] = useState<PendingMessage | null>(readPendingMessage);
  const [model, setModel] = useState(cachedWorkspace.model || cachedWorkspace.settings?.model_id || "");
  const [messageIntent, setMessageIntent] = useState<AgentMessageIntent>("steer");
  const [scopeDraft, setScopeDraft] = useState<ScopeDraft | null>(() => pluginEntry ? null : readScopeDraft());
  const [skillIDs, setSkillIDs] = useState<string[]>([]);
  const defaultModelRef = useRef(cachedWorkspace.settings?.model_id || "");
  const draftKey = selected || pluginDraftKey || newDraftKey(projectID);
  const draft = drafts[draftKey] || "";
  const [attachmentsByDraft, setAttachmentsByDraft] = useState(readPendingAttachments);
  const draftAttachments = attachmentsByDraft[draftKey] || [];
  const [attachmentsBusy, setAttachmentsBusy] = useState(false);
  useEffect(() => { setAttachmentsBusy(false); }, [draftKey]);
  useEffect(() => {
    const pendingPrefill = consumePendingSkillPrefill();
    if (!pendingPrefill) return;
    setShowSkillsView(false);
    setShowAutomationsView(false);
    selectedRef.current = "";
    setSelected("");
    setTask(null);
    const targetKey = pluginDraftKey || newDraftKey(projectID);
    setDrafts(current => ({ ...current, [targetKey]: pendingPrefill.prompt }));
    if (pendingPrefill.skillId) {
      setSkillIDs(current => (current.includes(pendingPrefill.skillId!) ? current : [...current, pendingPrefill.skillId!]));
    }
    setTimeout(() => input.current?.focus(), 0);
  }, [location.key]);
  const finishAttachments = (key: string, ids: string[], nextKey?: string) => setAttachmentsByDraft(current => {
    const remaining = (current[key] || []).filter(file => !ids.includes(file.id));
    return { ...current, [key]: nextKey ? [] : remaining, ...(nextKey && remaining.length ? { [nextKey]: [...(current[nextKey] || []), ...remaining] } : {}) };
  });
  const input = useRef<HTMLTextAreaElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const mutationRef = useRef(false);
  const deletedTaskIDs = useRef(new Set<string>());
  const recordMutationRevision = useRef(0);
  const confirmedCreateRequests = useRef(new Set<string>());
  const refreshRevision = useRef(0);
  const refreshAbort = useRef<AbortController | null>(null);
  const [pollRevision, setPollRevision] = useState(0);
  const cachedSettingsRef = useRef(settings);
  const cachedModelsRef = useRef(models);
  cachedSettingsRef.current = settings;
  cachedModelsRef.current = models;
  const [attentionVisible, setAttentionVisible] = useState(() => document.visibilityState === "visible" && document.hasFocus());
  useEffect(() => {
    const update = () => setAttentionVisible(document.visibilityState === "visible" && document.hasFocus());
    window.addEventListener("focus", update);
    window.addEventListener("blur", update);
    document.addEventListener("visibilitychange", update);
    return () => { window.removeEventListener("focus", update); window.removeEventListener("blur", update); document.removeEventListener("visibilitychange", update); };
  }, []);
  const taskAttention = useAgentTaskAttention({ tasks, selected, openedTask: detailLoading ? null : task, visible: attentionVisible, ready: tasksReady });
  useEffect(() => {
    if (!tasksReady) return;
    const allTasks = task ? [task, ...tasks.filter(tItem => tItem.id !== task.id)] : tasks;
    if (seenAutoNotifIds.current === null) {
      seenAutoNotifIds.current = new Set(allTasks.map(tItem => tItem.automation_notification?.id).filter((id): id is string => Boolean(id)));
      return;
    }
    for (const tItem of allTasks) {
      const notif = tItem.automation_notification;
      if (!notif?.id || seenAutoNotifIds.current.has(notif.id)) continue;
      seenAutoNotifIds.current.add(notif.id);
      if (notif.decision === "DONT_NOTIFY") continue;
      setActiveAutoToast(notif);
      playAutomationChime();
      try {
        if ("Notification" in window) {
          const fireDesktopNotif = () => {
            const n = new Notification(`⏰ ${notif.title}`, { body: notif.message || notif.automation_name, tag: notif.id });
            n.onclick = () => { window.focus(); if (notif.task_id) chooseTask(notif.task_id); n.close(); };
          };
          if (Notification.permission === "granted") fireDesktopNotif();
          else if (Notification.permission === "default") void Notification.requestPermission().then(p => { if (p === "granted") fireDesktopNotif(); });
        }
      } catch { /* Ignore Notification API restrictions */ }
    }
  }, [tasksReady, tasks, task]);
  const selectedExecuting = Boolean(task?.id === selected && ["queued", "running", "pausing", "reconciling"].includes(agentTaskState(task)));

  const acceptTask = useCallback((incoming: AgentTask) => {
    if (deletedTaskIDs.current.has(incoming.id)) return;
    const previous = taskCache.current.get(incoming.id);
    const latest = mergeAgentTaskSnapshot(previous, incoming);
    if (!previous || incoming.revision >= previous.revision) {
      taskCache.current.delete(incoming.id);
      taskCache.current.set(incoming.id, latest);
      if (taskCache.current.size > 20) taskCache.current.delete(taskCache.current.keys().next().value!);
    }
    if (latest.archived !== undefined) setRecordsSupported(true);
    setTasks(current => {
      const known = current.find(item => item.id === latest.id);
      // Live text and clock updates do not reorder or rerender the sidebar.
      if (known && known.revision >= latest.revision && !latest.archived) return current;
      return sortAgentTasks([...(latest.archived ? [] : [latest]), ...current.filter(item => item.id !== latest.id)]);
    });
    if (selectedRef.current === latest.id) setTask(current => current?.id === latest.id && current.revision > latest.revision ? current : latest);
  }, []);
  const acceptTaskList = useCallback((incoming: AgentTaskSummary[]) => {
    setTasksReady(true);
    if (incoming.some(item => item.archived !== undefined)) setRecordsSupported(true);
    setTasks(current => sortAgentTasks(incoming.filter(item => !deletedTaskIDs.current.has(item.id)).map(item => {
      const known = current.find(savedTask => savedTask.id === item.id) || taskCache.current.get(item.id);
      return known && known.revision > item.revision ? known : item;
    }).filter(item => !item.archived)));
  }, []);
  const recoverCreate = useCallback((summaries: AgentTaskSummary[]) => {
    let pendingID = "", pendingProject = "", submitted = "";
    let pendingDraftKey = "";
    let pendingAttachmentIDs: string[] = [];
    let originNavigation: number | undefined;
    try { const pending = JSON.parse(saved(PENDING_KEY) || "{}"); pendingID = pending.client_request_id || ""; pendingProject = pending.project_id || ""; submitted = pending.message || ""; originNavigation = pending.navigation_revision; pendingDraftKey = pending.draft_key || ""; pendingAttachmentIDs = Array.isArray(pending.attachment_ids) ? pending.attachment_ids.filter((id: unknown): id is string => typeof id === "string") : []; } catch { /* Ignore old or unavailable storage. */ }
    const recovered = summaries.find(item => pendingID && item.client_request_id === pendingID && !deletedTaskIDs.current.has(item.id));
    if (recovered) {
      recordMutationRevision.current++;
      confirmedCreateRequests.current.add(pendingID);
      if (confirmedCreateRequests.current.size > 32) confirmedCreateRequests.current.delete(confirmedCreateRequests.current.values().next().value!);
      const optimistic = readPendingMessage()?.requestID === pendingID;
      const ownsDraft = (originNavigation ?? initialNavigationRef.current) === navigationRef.current;
      if (ownsDraft && !selectedRef.current) { selectedRef.current = recovered.id; setSelected(recovered.id); }
      finishAttachments(pendingDraftKey || newDraftKey(pendingProject), pendingAttachmentIDs, ownsDraft ? recovered.id : undefined);
      save(PENDING_KEY, ""); setUncertain(false);
      setPendingMessage(current => current?.requestID === pendingID ? null : current);
      setDrafts(current => {
        if (!ownsDraft) return current;
        const key = pendingDraftKey || newDraftKey(pendingProject);
        const nextDraft = current[key] && (optimistic || current[key] !== submitted) ? current[key] : "";
        return { ...current, [key]: "", ...(nextDraft && !current[recovered.id] ? { [recovered.id]: nextDraft } : {}) };
      });
    }
  }, []);
  const refresh = useCallback(async () => {
    const refreshID = ++refreshRevision.current;
    refreshAbort.current?.abort();
    const controller = new AbortController();
    refreshAbort.current = controller;
    setPollRevision(current => current + 1);
    refreshAutomations();
    const recordsVersion = recordMutationRevision.current;
    const currentID = selectedRef.current;
    // Apply each response independently. A slow catalog must not hold settings,
    // task history or a cached composer behind a connection check.
    await Promise.all([
      rynAgent.settings(controller.signal).then(nextSettings => {
        if (refreshID !== refreshRevision.current) return;
        const previousDefault = defaultModelRef.current;
        const nextDefault = nextSettings.model_id;
        defaultModelRef.current = nextDefault;
        cachedSettingsRef.current = nextSettings;
        setSettings(nextSettings);
        setModel(current => !current || current === previousDefault ? nextDefault || cachedModelsRef.current.find(canUseAgentModel)?.id || "" : current);
        setResourceErrors(current => ({ ...current, settings: "" }));
      }).catch(reason => {
        if (refreshID !== refreshRevision.current) return;
        if (!cachedSettingsRef.current) setResourceErrors(current => ({ ...current, settings: reason instanceof AgentAPIError && [404, 501].includes(reason.status) ? "unavailable" : "settingsLoadError" }));
      }).finally(() => { if (refreshID === refreshRevision.current) setLoading(false); }),
      rynAgent.models(controller.signal).then(nextModels => {
        if (refreshID !== refreshRevision.current) return;
        cachedModelsRef.current = nextModels.models;
        setModels(nextModels.models);
        setModel(current => current || cachedSettingsRef.current?.model_id || nextModels.models.find(canUseAgentModel)?.id || "");
        setResourceErrors(current => ({ ...current, models: "" }));
      }).catch(() => {
        if (refreshID === refreshRevision.current && !cachedModelsRef.current.length) setResourceErrors(current => ({ ...current, models: "modelsLoadError" }));
      }),
      rynAgent.tasks(controller.signal).then(nextTasks => {
        if (refreshID !== refreshRevision.current) return;
        if (recordsVersion === recordMutationRevision.current) acceptTaskList(nextTasks.tasks);
        recoverCreate(nextTasks.tasks);
        setResourceErrors(current => ({ ...current, tasks: "" }));
      }).catch(() => {
        if (refreshID === refreshRevision.current) setResourceErrors(current => ({ ...current, tasks: "historyLoadError" }));
      }),
      currentID ? rynAgent.task(currentID, controller.signal).then(detail => {
        if (refreshID !== refreshRevision.current || currentID !== selectedRef.current) return;
        acceptTask(detail); setResourceErrors(current => ({ ...current, detail: "" }));
      }).catch(reason => {
        if (refreshID !== refreshRevision.current || currentID !== selectedRef.current) return;
        if (isMissingAgentTaskError(reason)) { deleteRecord(currentID); return; }
        setResourceErrors(current => ({ ...current, detail: "detailLoadError" }));
      }) : Promise.resolve(),
    ]);
  }, [acceptTask, acceptTaskList, recoverCreate]);

  const defaultWriteReceipt = task?.events.filter(item => item.type === "tool_result" && item.tool === "ryn_set_default_agent_model").at(-1)?.seq;
  const refreshedDefaults = useRef(new Set<string>());
  useEffect(() => {
    if (!pluginEntry) return;
    save(NAVIGATION_KEY, String(navigationRef.current));
    // The handoff creates an ordinary editable message, never a system prompt.
    // Consume it so a later reload can resume the conversation created by Send.
    routerNavigate(`${location.pathname}${location.search}`, { replace: true, state: null });
  }, [pluginEntry, routerNavigate]);
  useEffect(() => {
    if (defaultWriteReceipt === undefined) return;
    const receipt = `${task?.id}/${defaultWriteReceipt}`;
    if (refreshedDefaults.current.has(receipt)) return;
    refreshedDefaults.current.add(receipt);
    void refresh().catch(() => setError("loadError"));
  }, [task?.id, defaultWriteReceipt, refresh]);

  useEffect(() => {
    let active = true;
    void refresh().catch(reason => {
      if (active) setError(reason instanceof AgentAPIError && [404, 501].includes(reason.status) ? "unavailable" : "loadError");
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; refreshRevision.current++; refreshAbort.current?.abort(); };
  }, [refresh]);
  useEffect(() => { writeAgentWorkspace(settings, models, model); }, [settings, models, model]);
  // Startup/reconnect failures recover without requiring the owner to press Refresh.
  useEffect(() => {
    if (!resourceErrors.settings && !resourceErrors.models) return;
    if (resourceErrors.settings === "unavailable") return;
    let active = true;
    let timer: number;
    const retry = async () => {
      await refresh();
      if (active) timer = window.setTimeout(retry, 3000);
    };
    timer = window.setTimeout(retry, 3000);
    return () => { active = false; window.clearTimeout(timer); };
  }, [resourceErrors.settings, resourceErrors.models, refresh]);
  useEffect(() => {
    const onFocus = () => { void refresh().catch(() => setError("loadError")); };
    const onVisible = () => { if (document.visibilityState === "visible") onFocus(); };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisible);
    return () => { window.removeEventListener("focus", onFocus); document.removeEventListener("visibilitychange", onVisible); };
  }, [refresh]);
  useEffect(() => { save(DRAFT_KEY, JSON.stringify(drafts)); }, [drafts]);
  useEffect(() => { save(PENDING_MESSAGE_KEY, pendingMessage ? JSON.stringify(pendingMessage) : ""); }, [pendingMessage]);
  useEffect(() => { if (!pluginDraftKey) save(SCOPE_DRAFT_KEY, scopeDraft ? JSON.stringify(scopeDraft) : ""); }, [scopeDraft, pluginDraftKey]);
  useEffect(() => {
    save(SELECTED_KEY, selected);
    if (!selected) { setTask(null); setDetailLoading(false); setResourceErrors(current => ({ ...current, detail: "" })); return; }
    const abort = new AbortController();
    const cachedTask = taskCache.current.get(selected);
    setTask(cachedTask || null);
    setDetailLoading(!cachedTask);
    void rynAgent.task(selected, abort.signal).then(incoming => { if (!abort.signal.aborted) { acceptTask(incoming); setResourceErrors(current => ({ ...current, detail: "" })); } })
      .catch(reason => {
        if (abort.signal.aborted) return;
        if (isMissingAgentTaskError(reason)) { deleteRecord(selected); return; }
        setResourceErrors(current => ({ ...current, detail: "detailLoadError" }));
      })
      .finally(() => { if (!abort.signal.aborted) setDetailLoading(false); });
    return () => abort.abort();
  }, [selected, acceptTask]);
  useEffect(() => {
    if (loading) return;
    let stopped = false;
    let timer: number;
    let failures = 0;
    const abort = new AbortController();
    const poll = async () => {
      const recordsVersion = recordMutationRevision.current;
      let delay = 5000;
      try {
        const summary = await rynAgent.tasks(abort.signal);
        if (stopped) return;
        failures = 0;
        if (recordsVersion === recordMutationRevision.current) acceptTaskList(summary.tasks);
        setResourceErrors(current => ({ ...current, tasks: "" }));
        recoverCreate(summary.tasks);
        if (summary.tasks.some(item => isAgentActive(agentTaskState(item)))) delay = 1200;
      } catch (reason) {
        if (stopped) return;
        const transient = isTransientAgentReadError(reason);
        failures++;
        if (!transient || failures > 1) setResourceErrors(current => ({ ...current, tasks: "historyLoadError" }));
        if (!transient) return;
        delay = agentReadBackoff(failures);
      }
      if (!stopped) timer = window.setTimeout(poll, delay);
    };
    timer = window.setTimeout(poll, 1200);
    return () => { stopped = true; abort.abort(); window.clearTimeout(timer); };
  }, [loading, pollRevision, acceptTaskList, recoverCreate]);
  useEffect(() => {
    if (loading || !selected) return;
    let stopped = false;
    let timer: number;
    let failures = 0;
    const abort = new AbortController();
    const poll = async () => {
      const cached = taskCache.current.get(selected);
      let delay = 5000;
      try {
        const detail = await (cached ? rynAgent.taskSince(cached, abort.signal) : rynAgent.task(selected, abort.signal));
        if (stopped || selectedRef.current !== selected) return;
        failures = 0;
        acceptTask(detail); setResourceErrors(current => ({ ...current, detail: "" }));
        // Only the open executing task needs the low-latency live projection.
        // Requests remain sequential so a slow node never builds a request queue.
        if (["queued", "running", "pausing", "reconciling"].includes(agentTaskState(detail))) delay = document.visibilityState === "hidden" ? 1500 : 500;
        else if (isAgentActive(agentTaskState(detail))) delay = 1200;
      } catch (reason) {
        if (stopped || selectedRef.current !== selected) return;
        if (isMissingAgentTaskError(reason)) { deleteRecord(selected); return; }
        const transient = isTransientAgentReadError(reason);
        failures++;
        if (!transient || !cached || failures > 1) setResourceErrors(current => ({ ...current, detail: "detailLoadError" }));
        if (!transient) return;
        delay = agentReadBackoff(failures);
      }
      if (!stopped) timer = window.setTimeout(poll, delay);
    };
    timer = window.setTimeout(poll, selectedExecuting ? 0 : 1200);
    return () => { stopped = true; abort.abort(); window.clearTimeout(timer); };
  // Each resource advances independently; a stalled index cannot hold the current response.
  }, [loading, selected, selectedExecuting, pollRevision, acceptTask]);

  const changeDraft = (value: string) => setDrafts(current => ({ ...current, [draftKey]: value }));
  const newTask = () => {
    navigate();
    if (pluginDraftKey) setScopeDraft(readScopeDraft());
    setPluginDraftKey("");
    const id = task?.project ? projects.some(project => project.id === task.project?.id) ? task.project.id : "" : projectID;
    setProjectID(id); save(PROJECT_KEY, id || "__general__");
    selectedRef.current = ""; setSelected(""); setTask(null); setDetailLoading(false);
    input.current?.focus();
  };
  const chooseTask = (id: string) => {
    navigate();
    if (pluginDraftKey) setScopeDraft(readScopeDraft());
    setPluginDraftKey("");
    const project = projects.find(project => project.id === tasks.find(item => item.id === id)?.project?.id);
    setProjectID(project?.id || ""); save(PROJECT_KEY, project?.id || "__general__");
    selectedRef.current = id; setSelected(id); setTask(taskCache.current.get(id) || null);
  };
  const changeRecord = (incoming: AgentTask) => { recordMutationRevision.current++; acceptTask(incoming); setRecordsRevision(current => current + 1); };
  const deleteRecord = (id: string) => {
    taskAttention.forget(id);
    recordMutationRevision.current++;
    deletedTaskIDs.current.add(id);
    taskCache.current.delete(id);
    setTasks(current => current.filter(item => item.id !== id)); setRecordsRevision(current => current + 1);
    setDrafts(current => { const next = { ...current }; delete next[id]; return next; });
    if (selectedRef.current === id) { selectedRef.current = ""; setSelected(""); setTask(null); setResourceErrors(current => ({ ...current, detail: "" })); }
  };
  const chooseGeneralProject = () => {
    chooseProject("");
    void rynAgent.selectProject("").catch(() => setError("projectSelectionError"));
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
    if (!task || mutationRef.current || (controlUncertain && !["pause", "stop", "cancel"].includes(kind)) || !canUseAgentAction(task, kind, true) || (resourceErrors.settings && !task.allowed_actions && !["pause", "stop", "cancel", "deny"].includes(kind))) return;
    mutationRef.current = true; setBusy(true); setError("");
    try { acceptTask(task.allowed_actions && !controlUncertain ? await submitControl({ task_id: task.id, route: "actions", body: agentActionBody(task, kind) }) : await rynAgent.action(task, kind)); }
    catch (reason) {
      setError(mutationErrorKey(reason));
      await rynAgent.task(task.id).then(acceptTask).catch(() => undefined);
    } finally { mutationRef.current = false; setBusy(false); }
  };
  const editMessage = async (messageIndex: number, text: string) => {
    if (!task || mutationRef.current || uncertain || controlUncertain || !canUseAgentAction(task, "edit_message")) return false;
    mutationRef.current = true; setBusy(true); setError("");
    try {
      acceptTask(await submitControl({ task_id: task.id, route: "messages", body: { ...agentMessageBody(task, text, "information"), edit_message_index: messageIndex } }));
      return true;
    } catch (reason) {
      setError(mutationErrorKey(reason));
      await rynAgent.task(task.id).then(acceptTask).catch(() => undefined);
      return false;
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
    if ((!draft.trim() && !draftAttachments.length) || attachmentsBusy || mutationRef.current || uncertain || controlUncertain || (!selected && !newScopeReady)) return;
    if (selected ? !currentTask || (currentTask.allowed_actions ? !canUseAgentAction(currentTask, effectiveIntent) : !legacyReady || !canContinueAgentTask(currentTask)) : !legacyReady) return;
    mutationRef.current = true; setBusy(true); setError("");
    const submittedMessage = draft.trim() || t("attachmentOnlyMessage");
    const submittedAttachmentIDs = draftAttachments.map(file => file.id);
    const submittedAttachments = agentAttachmentInputs(draftAttachments);
    const attachmentArgs: [AgentAttachmentInput[]] | [] = submittedAttachments.length ? [submittedAttachments] : [];
    const originNavigation = navigationRef.current;
    const sending: PendingMessage = { requestID: crypto.randomUUID(), taskID: currentTask?.id || "", draftKey, message: submittedMessage, baseMessageCount: currentTask?.messages.length || 0, navigationRevision: originNavigation, phase: "sending" };
    setPendingMessage(sending);
    setDrafts(current => ({ ...current, [draftKey]: "" }));
    const responseLanguage = i18n.resolvedLanguage?.startsWith("zh") ? "zh-CN" : "en";
    const requestID = sending.requestID;
    const newScope = !currentTask && scopeDraft?.scope.resources.length ? scopeDraft.scope : undefined;
    const destinationRevision = newScope ? scopeDraft?.destination_revision || undefined : undefined;
    const submittedEffort = selectedModel?.supported_reasoning_efforts?.includes(effectiveEffort) ? effectiveEffort : undefined;
    if (!currentTask) {
      save(PROJECT_KEY, projectID || "__general__");
      save(PENDING_KEY, JSON.stringify({ client_request_id: requestID, message: submittedMessage, draft_key: draftKey, model_id: model, response_language: responseLanguage, navigation_revision: originNavigation, ...(projectID ? { project_id: projectID } : {}), ...(newScope ? { scope: newScope, expected_destination_revision: destinationRevision } : {}), ...(skillIDs.length ? { skill_ids: skillIDs } : {}), ...(submittedEffort ? { reasoning_effort: submittedEffort } : {}), ...(submittedAttachments.length ? { attachments: submittedAttachments, attachment_ids: submittedAttachmentIDs } : {}), at: Date.now() }));
    }
    try {
      const next = currentTask ? currentTask.allowed_actions ? await submitControl({ task_id: currentTask.id, route: "messages", attachment_ids: submittedAttachmentIDs, body: agentMessageBody(currentTask, submittedMessage, effectiveIntent, submittedEffort, submittedAttachments) }) : await rynAgent.message(currentTask, submittedMessage, effectiveIntent, requestID, submittedEffort, ...attachmentArgs) : await rynAgent.create(submittedMessage, model, requestID, newScope, destinationRevision, skillIDs, responseLanguage, projectID || undefined, submittedEffort, ...attachmentArgs);
      const ownsDraft = originNavigation === navigationRef.current;
      if (!currentTask && ownsDraft && !selectedRef.current) { selectedRef.current = next.id; setSelected(next.id); }
      if (!currentTask) recordMutationRevision.current++;
      finishAttachments(draftKey, submittedAttachmentIDs, !currentTask && ownsDraft ? next.id : undefined);
      acceptTask(next);
      if (!currentTask) setDrafts(current => {
        if (!ownsDraft) return current;
        // A next draft typed during submission follows its newly created conversation.
        const nextDraft = current[draftKey];
        return nextDraft && !current[next.id] ? { ...current, [draftKey]: "", [next.id]: nextDraft } : current;
      });
      setPendingMessage(current => current?.requestID === requestID ? null : current);
      save(PENDING_KEY, ""); setUncertain(false);
      if (!currentTask && ownsDraft) { setScopeDraft(null); setSkillIDs([]); }
    } catch (reason) {
      // A polled server record is a receipt even if the original HTTP response fails later.
      if (!currentTask && confirmedCreateRequests.current.has(requestID)) return;
      const ambiguous = !(reason instanceof AgentAPIError) || reason.status >= 500;
      if (!currentTask && ambiguous) setUncertain(true);
      else if (!currentTask) save(PENDING_KEY, "");
      if (ambiguous && (!currentTask || currentTask.allowed_actions)) setPendingMessage(current => current?.requestID === requestID ? { ...current, phase: "uncertain" } : current);
      else {
        const detachedCreate = !currentTask && originNavigation !== navigationRef.current;
        setPendingMessage(current => current?.requestID === requestID ? detachedCreate ? { ...current, phase: "rejected" } : null : current);
        if (!detachedCreate) setDrafts(current => current[draftKey] ? current : { ...current, [draftKey]: submittedMessage });
      }
      setError(mutationErrorKey(reason));
      void refresh().catch(() => undefined);
    } finally { mutationRef.current = false; setBusy(false); }
  };
  const recoverRequest = async () => {
    if (mutationRef.current) return;
    let pending: { client_request_id: string; message: string; model_id: string; draft_key?: string; scope?: AgentScopeSelection; expected_destination_revision?: string; skill_ids?: string[]; project_id?: string; response_language?: "zh-CN" | "en"; reasoning_effort?: string; navigation_revision?: number; attachments?: AgentAttachmentInput[]; attachment_ids?: string[] };
    try { pending = JSON.parse(saved(PENDING_KEY)); if (!pending.client_request_id || !pending.message || !pending.model_id) return; }
    catch { return; }
    mutationRef.current = true; setBusy(true);
    const originNavigation = pending.navigation_revision ?? initialNavigationRef.current;
    setPendingMessage(current => current ? { ...current, phase: "sending" } : current);
    try {
      const attachmentArgs: [AgentAttachmentInput[]] | [] = pending.attachments?.length ? [pending.attachments] : [];
      const next = await rynAgent.create(pending.message, pending.model_id, pending.client_request_id, pending.scope, pending.expected_destination_revision, pending.skill_ids, pending.response_language, pending.project_id, pending.reasoning_effort, ...attachmentArgs);
      const ownsDraft = originNavigation === navigationRef.current;
      finishAttachments(pending.draft_key || newDraftKey(pending.project_id), pending.attachment_ids || [], ownsDraft ? next.id : undefined);
      if (ownsDraft && !selectedRef.current) { selectedRef.current = next.id; setSelected(next.id); }
      recordMutationRevision.current++;
      acceptTask(next);
      save(PENDING_KEY, ""); setUncertain(false); setError("");
      setPendingMessage(null);
      setDrafts(current => {
        if (!ownsDraft) return current;
        const key = pending.draft_key || newDraftKey(pending.project_id);
        const nextDraft = current[key] && (pendingMessage || current[key] !== pending.message) ? current[key] : "";
        return { ...current, [key]: "", ...(nextDraft && !current[next.id] ? { [next.id]: nextDraft } : {}) };
      });
      if (ownsDraft) { setScopeDraft(null); setSkillIDs([]); }
    } catch (reason) {
      if (confirmedCreateRequests.current.has(pending.client_request_id)) return;
      if (reason instanceof AgentAPIError && reason.status < 500) {
        save(PENDING_KEY, ""); setUncertain(false);
        const key = pending.draft_key || newDraftKey(pending.project_id);
        if (originNavigation === navigationRef.current) {
          setPendingMessage(null);
          setDrafts(current => current[key] ? current : { ...current, [key]: pending.message });
        } else setPendingMessage(current => ({ ...(current || { requestID: pending.client_request_id, taskID: "", draftKey: key, message: pending.message, baseMessageCount: 0, navigationRevision: originNavigation }), phase: "rejected" }));
      } else setPendingMessage(current => current ? { ...current, phase: "uncertain" } : current);
      setError(mutationErrorKey(reason)); await refresh().catch(() => undefined);
    }
    finally { mutationRef.current = false; setBusy(false); }
  };
  const recoverControl = async () => {
    if (mutationRef.current) return;
    let pending: PendingControl;
    try { pending = JSON.parse(saved(CONTROL_KEY)); if (!pending.task_id || !["messages", "actions", "scope"].includes(pending.route) || !pending.body?.client_request_id) return; } catch { return; }
    mutationRef.current = true; setBusy(true); setError("");
    setPendingMessage(current => current?.taskID === pending.task_id ? { ...current, phase: "sending" } : current);
    try {
      acceptTask(await submitControl(pending));
      if (pending.route === "messages") setPendingMessage(current => current?.taskID === pending.task_id ? null : current);
      if (pending.route === "messages") finishAttachments(pending.task_id, pending.attachment_ids || []);
      if (pending.route === "messages" && pending.body.edit_message_index === undefined && !pendingMessage) setDrafts(current => current[pending.task_id]?.trim() === pending.body.message ? { ...current, [pending.task_id]: "" } : current);
    } catch (reason) {
      if (reason instanceof AgentAPIError && reason.status < 500) {
        if (pending.route === "messages" && pending.body.edit_message_index === undefined) {
          setPendingMessage(current => current?.taskID === pending.task_id ? null : current);
          setDrafts(current => current[pending.task_id] ? current : { ...current, [pending.task_id]: String(pending.body.message || "") });
        }
      } else setPendingMessage(current => current ? { ...current, phase: "uncertain" } : current);
      setError(mutationErrorKey(reason)); await refresh().catch(() => undefined);
    }
    finally { mutationRef.current = false; setBusy(false); }
  };
  const settingsReady = Boolean(settings) && !resourceErrors.settings;
  const enabled = settingsReady && settings?.enabled;
  const messageIntents: AgentMessageIntent[] = task?.allowed_actions ? (["steer", "information"] as const).filter(intent => canUseAgentAction(task, intent)) : [];
  const effectiveIntent = messageIntents.includes(messageIntent) ? messageIntent : messageIntents[0] || "information";
  const backgroundTasks = tasks.filter(item => item.id !== selected && isAgentActive(agentTaskState(item)));
  const selectedModel = models.find(item => item.id === (task?.model_id || model));
  const supportedEfforts = selectedModel?.supported_reasoning_efforts || [];
  const effectiveEffort = supportedEfforts.includes(reasoningEffort) ? reasoningEffort : supportedEfforts.includes(task?.reasoning_effort || "") ? task!.reasoning_effort! : selectedModel?.default_reasoning_effort || "";
  useEffect(() => { setReasoningEffort(task?.reasoning_effort || ""); setTaskModelOpen(false); }, [task?.id, task?.model_id]);
  const newScopeReady = !scopeDraft?.scope.resources.length || (scopeDraft.scope.allow_model_data && scopeDraft.model_id === model && Boolean(scopeDraft.destination_revision) && scopeDraft.destination_revision === selectedModel?.destination_revision);
  const visibleMessages = task?.messages.filter((message, index, messages) =>
    (message.role === "user" || message.role === "assistant") && (message.content || message.tool_calls?.length)
    && !(task.result && index === messages.length - 1 && message.role === "assistant" && message.content === task.result),
  ) || [];
  const transcript = groupAgentMessages(visibleMessages);
  const maxTurnIndex = transcript.reduce((max, entry) => Math.max(max, entry.turnIndex), 0);
  const streamedText = task ? pendingAgentText(task) : "";
  const streamStart = task && [...task.events].reverse().find(event => event.type === "model_started");
  const legacySend = enabled && selectedModel && canUseAgentModel(selectedModel) && (!selected || (task && canContinueAgentTask(task)));
  const canSend = Boolean(draft.trim() || draftAttachments.length) && !attachmentsBusy && !busy && !uncertain && !controlUncertain && (!selected ? newScopeReady : true) && (task?.allowed_actions ? canUseAgentAction(task, effectiveIntent) : legacySend);
  const sendLabel = task?.allowed_actions ? effectiveIntent === "steer" ? "sendSteer" : "sendInformation" : selected ? "followup" : "send";
  const currentProject = task?.project || (!selected ? projects.find(item => item.id === projectID) : undefined);
  const generalTasks = tasks.filter(item => !item.project || !projects.some(project => project.id === item.project?.id));
  const visiblePending = pendingMessage && pendingMessage.phase !== "rejected" && (pendingMessage.taskID ? pendingMessage.taskID === selected : !selected && pendingMessage.draftKey === draftKey && (pendingMessage.navigationRevision ?? initialNavigationRef.current) === navigationRevision) ? pendingMessage : null;
  const pendingRecorded = Boolean(visiblePending && task?.messages.slice(visiblePending.baseMessageCount).some(message => message.role === "user" && (message.ui?.text ?? message.content)?.trim() === visiblePending.message));
  const pendingFeedback = visiblePending && <>
    {!pendingRecorded && <div className="ra-message ra-user ra-message-pending"><div className="ra-user-bubble"><ChatMarkdown>{visiblePending.message}</ChatMarkdown></div></div>}
    <div className="ra-execution-progress ra-send-progress" role="status" aria-live="polite">{visiblePending.phase === "sending" ? <LoaderCircle size={16} className="ra-spinner" /> : <CircleAlert size={16} />}<span>{t(visiblePending.phase === "sending" ? "sendingMessage" : "messageReceiptPending")}</span></div>
  </>;
  useEffect(() => {
    if (!visiblePending || !scroll.current) return;
    scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [visiblePending?.requestID]);
  useEffect(() => { if (task) setMessageIntent(isAgentActive(agentTaskState(task)) ? "steer" : "information"); }, [task?.id, task ? isAgentActive(agentTaskState(task)) : false]);
  const modelSelect = (disabled = false) => <RynAgentModelPicker models={models} value={task?.model_id || model} disabled={disabled || busy} selectionLocked={task ? !canUseAgentAction(task, "model") : false} title={t(task ? "taskModel" : "newTaskModel")} reasoningEfforts={supportedEfforts} effort={effectiveEffort} onEffortChange={setReasoningEffort} onManageTaskModel={task?.allowed_actions?.model ? () => { setTaskModelTarget(""); setTaskModelOpen(true); } : undefined} onChange={id => {
    if (task) { setTaskModelTarget(id); setTaskModelOpen(true); }
    else { setModel(id); setReasoningEffort(""); }
  }} />;
  const taskMenu = useAgentTaskMenu({ selected, busy: busy || uncertain || controlUncertain, onChoose: chooseTask, onChange: changeRecord, onDelete: deleteRecord, isUnread: taskAttention.isUnread });
  const stopButton = visiblePending?.phase !== "sending" && task && ["running", "queued", "waiting_approval", "waiting_input", "pausing"].includes(agentTaskState(task)) && (task.allowed_actions?.stop ? canUseAgentAction(task, "stop") : canUseAgentAction(task, "pause", true))
    ? <button type="button" className="ra-primary ra-send ra-stop" aria-label={t("stop")} disabled={busy || agentTaskState(task) === "pausing"} onClick={() => void action(task.allowed_actions?.stop ? "stop" : "pause")}><Square size={14} fill="currentColor" /></button> : null;

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
              {task.allowed_actions?.model && <button type="button" onClick={() => { setTaskModelTarget(""); setTaskModelOpen(true); }}>{t("taskModel")} · {selectedModel?.name || task.model_id}</button>}
              {task.allowed_actions?.scope && <RynAgentScope key={`scope/${task.id}/${task.scope_revision || 0}/${task.model_id}`} task={task} modelName={selectedModel?.name || task.model_id} busy={busy || controlUncertain} onSave={updateScope} />}
            </div></details>
            <TaskProgress task={task} settings={settings} />
            <details className="ra-activity"><summary>{t("activity")}</summary>{task.events.filter(event => ["tool_started", "tool_result", "approved", "denied", "approval_requested"].includes(event.type)).map(event => <div className="ra-event" key={event.seq}><strong>{t(event.type === "tool_result" ? event.internal === true ? "internalReceipt" : `receipt_${agentToolResultState(event)}` : event.type)} {event.tool && <span title={event.tool}>{t(`tool_${event.tool}`, { defaultValue: event.tool })}</span>}</strong>{isAgentLateToolResult(event, task.events) && <p>{t("lateReceipt")}</p>}{event.result !== undefined && <details className="ra-event-evidence"><summary>{t("viewEvidence")}</summary><pre aria-label={t("evidence")}>{json(event.result)}</pre></details>}</div>)}</details>            {task.outcome && task.outcome.assessment_requested !== false && (task.run_state === "ended" || ["completed", "failed", "cancelled", "interrupted"].includes(task.status)) && <section className="ra-result ra-outcome" aria-label={t("outcome")}>
              <strong>{t("outcome")} · {t(`outcome_${task.outcome.status}`)} · {t("goalRevision", { revision: task.outcome.goal_revision })}</strong>
              <p>{t(task.outcome.status === "verified" ? "outcomeVerifiedHelp" : "outcomeUnverifiedHelp")}</p>
              {task.outcome.summary && task.outcome.summary !== task.result && <ChatMarkdown>{outcomeReasons[task.outcome.summary] ? t(outcomeReasons[task.outcome.summary]) : task.outcome.summary}</ChatMarkdown>}
              {task.outcome.uncertainties.length > 0 && <ul>{task.outcome.uncertainties.map((item, index) => <li key={index}>{outcomeReasons[item] ? t(outcomeReasons[item]) : item}</li>)}</ul>}
              {(task.outcome.evidence_refs.length > 0 || task.outcome.criteria_results.length > 0) && <details className="ra-exact-details"><summary>{t("outcomeEvidence")}</summary><pre>{json({ criteria_results: task.outcome.criteria_results, evidence_refs: task.outcome.evidence_refs })}</pre></details>}
            </section>}
          <details className="ra-record-options"><summary>{t("manageRecords")}</summary><TaskRecords task={task} busy={busy || controlUncertain} onChange={changeRecord} onDelete={deleteRecord} /></details></>}</div></details><Link to="/services/sources">{t("models")}</Link><Link to="/settings/extensions">{t("extensions")}</Link><button className="ra-icon" aria-label={t("settings")} disabled={!settingsReady} onClick={() => setSettingsOpen(true)}><Settings size={18} /></button></div>
    </header>
    {taskMenu.dialogs}
    {pendingMessage?.phase === "rejected" && <div className="ra-banner ra-unsent-message" role="alert"><CircleAlert size={17} /><details><summary>{t("unsentMessage")}</summary><p>{pendingMessage.message}</p></details><button className="ra-icon" aria-label={t("close")} onClick={() => setPendingMessage(null)}><X size={16} /></button></div>}
    {error && <div role="alert" className="ra-banner"><CircleAlert size={17} /><span>{t(error)}</span><button onClick={() => void reload()} disabled={busy}>{t("retry")}</button></div>}
    {Object.values(resourceErrors).filter(Boolean).map(key => <div key={key} role="alert" className="ra-banner"><CircleAlert size={17} /><span>{t(key)}</span><button onClick={() => void reload()} disabled={busy}>{t("retry")}</button></div>)}
    {uncertain && <div role="alert" className="ra-banner">{t("uncertain")}<button disabled={busy} onClick={() => void recoverRequest()}>{t("recoverRequest")}</button></div>}
    {controlUncertain && <div role="alert" className="ra-banner">{t("controlUncertain")}<button disabled={busy} onClick={() => void recoverControl()}>{t("recoverControl")}</button></div>}
    {activeAutoToast && <div className="ra-auto-toast-wrap" role="status"><div className="ra-auto-toast" onClick={() => { const tid = activeAutoToast.task_id; setActiveAutoToast(null); if (tid) chooseTask(tid); }}><div className="ra-auto-toast-left"><div className="ra-auto-toast-icon" aria-hidden="true"><Clock3 size={16} /></div><div className="ra-auto-toast-content"><span className="ra-auto-toast-title">⏰ {activeAutoToast.title}</span>{activeAutoToast.message && <><span className="ra-auto-toast-sep">|</span><span className="ra-auto-toast-msg">{activeAutoToast.message}</span></>}<span className="ra-auto-toast-live-pill">{i18n.language.startsWith("zh") ? "刚刚" : "Just now"}</span></div></div><div className="ra-auto-toast-right"><button type="button" className="ra-auto-toast-action" onClick={e => { e.stopPropagation(); const tid = activeAutoToast.task_id; setActiveAutoToast(null); if (tid) chooseTask(tid); }}><span>{i18n.language.startsWith("zh") ? "查看对话" : "Open"}</span><span>→</span></button><button type="button" className="ra-auto-toast-close" aria-label={t("close")} onClick={e => { e.stopPropagation(); setActiveAutoToast(null); }}>✕</button></div></div></div>}
    {loading ? <div className="ra-empty" role="status">{t("loading")}</div> : <div className="ra-layout">
      <aside id="ryn-agent-task-list" className="ra-tasks" aria-label={t("tasks")} hidden={tasksCollapsed}>
        <button className="ra-new" onClick={newTask}><SquarePen size={17} />{t("newTask")}</button>
        <button type="button" className={`ra-nav-automations${showAutomationsView ? " is-active" : ""}`} onClick={() => { refreshAutomations(); setShowSkillsView(false); setShowAutomationsView(v => !v); }}><Clock3 size={16} /><span>{t("automations")}</span>{automationsList.filter(a => a.status !== "PAUSED").length > 0 && <span className="ra-nav-auto-badge">{automationsList.filter(a => a.status !== "PAUSED").length}</span>}</button>
        <button type="button" className={`ra-nav-automations${showSkillsView ? " is-active" : ""}`} onClick={() => { setShowAutomationsView(false); setShowSkillsView(v => !v); }}><BookOpen size={16} /><span>{i18n.language.startsWith("zh") ? "技能" : "Skills"}</span></button>
        <div className="ra-task-items"><RynAgentProject value={currentProject?.id || projectID} onChange={chooseProject} navigationRevision={navigationRevision} tasks={tasks} activeTaskID={selected} onSelectTask={chooseTask} onNewTask={chooseProject} onProjectsChange={setProjects} restoreSelection={!pluginEntry && !saved(PROJECT_KEY)} renderTask={item => taskMenu.renderTask(item, true)} /><div className="ra-recent-heading"><small>{t("recent")}</small><button className="ra-icon" aria-label={t("projectGeneralTask")} title={t("projectGeneralTask")} onClick={chooseGeneralProject}><SquarePen size={14} /></button></div>{generalTasks.length ? generalTasks.map(item => taskMenu.renderTask(item)) : <p>{t("emptyTasks")}</p>}</div>
        <ArchivedTasks enabled={recordsSupported} selected={selected} onChoose={chooseTask} revision={recordsRevision} renderTask={item => taskMenu.renderTask(item)} />
        <small className="ra-local"><Monitor size={14} />{t("localHistory")}</small>
      </aside>
      <div className="ra-chat">
        {showSkillsView ? <CodexSkillsView embeddedInAgent onTrySkillPrompt={(prefillPrompt, skillId) => { setShowSkillsView(false); setShowAutomationsView(false); selectedRef.current = ""; setSelected(""); setTask(null); const targetKey = pluginDraftKey || newDraftKey(projectID); setDrafts(current => ({ ...current, [targetKey]: prefillPrompt })); if (skillId) { setSkillIDs(current => (current.includes(skillId) ? current : [...current, skillId])); } setTimeout(() => input.current?.focus(), 0); }} /> : showAutomationsView ? <AutomationsPanel automations={automationsList} runs={automationRuns} tasks={tasks} activeTaskId={selected} zh={i18n.language.startsWith("zh")} onRefresh={refreshAutomations} onCreateNew={() => { setEditingAutomation({ kind: selected ? "heartbeat" : "cron", target_task_id: selected || "", repeat: "daily", time: "09:00", status: "ACTIVE" }); setAutomationDialogOpen(true); }} onEdit={item => { setEditingAutomation(item); setAutomationDialogOpen(true); }} onSelectTask={id => { setShowAutomationsView(false); chooseTask(id); }} /> : <>

        {settingsReady && !enabled && task && <div className="ra-disabled-banner"><span>{t("historyDisabled")}</span><button disabled={busy} onClick={() => void updateSettings({ enabled: true, ...(model ? { model_id: model } : {}) })}>{t("enable")}</button></div>}
        <div ref={scroll} className="ra-scroll" aria-busy={detailLoading}>

          {detailLoading ? <div className="ra-empty" role="status">{t("loading")}</div> : task ? <div key={`thread/${task.id}`} className="ra-thread"><TaskArtifacts key={`artifacts/${task.id}`} task={task} maxTurnIndex={maxTurnIndex}>
            {transcript.map((entry, index) => { const isTurnEnd = entry.turnIndex >= 0 && entry.turnIndex < maxTurnIndex && transcript[index + 1]?.turnIndex !== entry.turnIndex; if (entry.kind === "progress") { const callIds = new Set(entry.messages.flatMap(m => m.tool_calls?.map(c => c.id) || [])); const autoCards = extractTurnAutomations(task.events.filter(ev => typeof ev.call_id === "string" && callIds.has(ev.call_id))); return <Fragment key={`${task.id}-progress-${index}`}><RynAgentProgress messages={entry.messages} task={task} />{autoCards.map(autoItem => <AutomationChatCard key={autoItem.id} automation={automationsList.find(a => a.id === autoItem.id) || autoItem} zh={i18n.language.startsWith("zh")} onEdit={item => { setEditingAutomation(item); setAutomationDialogOpen(true); }} onRunNow={async item => { const res = await rynAgent.runAutomationNow(item.id).catch(() => null); refreshAutomations(); if (res?.task_id) chooseTask(res.task_id); }} />)}{isTurnEnd && <TurnDeliverables turnIndex={entry.turnIndex} />}</Fragment>; } const message = entry.message; if (message.ui?.kind === "silent_heartbeat" || message.ui?.hidden === true) return isTurnEnd ? <TurnDeliverables key={`${task.id}-${index}-files`} turnIndex={entry.turnIndex} /> : null; if (message.role === "user" && message.ui?.kind === "heartbeat") return <Fragment key={`${task.id}-${index}`}><HeartbeatWakeupDivider text={message.ui.text || message.content || ""} automationName={message.ui.automation_name} zh={i18n.language.startsWith("zh")} /><RynAgentTurnDuration task={task} turnIndex={entry.turnIndex} />{isTurnEnd && <TurnDeliverables turnIndex={entry.turnIndex} />}</Fragment>; if (message.role === "assistant" && message.ui?.kind === "change_review") return <Fragment key={`${task.id}-${index}`}><details className="ra-review-note"><summary>{t("completionReview")}</summary><pre>{message.content}</pre></details>{isTurnEnd && <TurnDeliverables turnIndex={entry.turnIndex} />}</Fragment>; return <Fragment key={`${task.id}-${index}`}><div className={`ra-message ra-${message.role}`}>{message.role === "user" && <AgentMessageAttachments attachments={message.attachments} />}{message.role === "assistant" && !message.tool_calls?.length && message.ui?.kind !== "verification_feedback" &&<div className="ra-reply-label"><Sparkles size={16} /><strong>Ryn</strong><small>{models.find(item => item.id === agentMessageModelID(task, message))?.name || agentMessageModelID(task, message) || t("replyModelUnknown")}</small></div>}{message.role === "assistant" && message.ui?.kind === "verification_feedback" ? <RynAgentVerification content={message.content} /> : message.role === "user" && message.ui?.kind === "source_credential" ? <section className="ra-user-bubble" aria-label={t("credentialConfigured")}><strong>{t("credentialConfigured")}</strong><dl className="ra-answer-record"><div><dt>{t("sourceEndpoint")}</dt><dd>{message.ui.endpoint || t("unknownState")}</dd></div><div><dt>{t("credential")}</dt><dd>{t(message.ui.has_credential === true ? "credentialConfigured" : "unknownState")}</dd></div></dl></section> : message.role === "user" && message.ui?.kind === "input_answer" ? <section className="ra-user-bubble" aria-label={t("answerRecorded")}><strong>{t("answerRecorded")}</strong><dl className="ra-answer-record">{message.ui.fields?.map((field, fieldIndex) => <div key={fieldIndex}><dt>{field.label}</dt><dd>{field.value}</dd></div>)}</dl></section> : <><strong className="ra-message-kind">{message.role === "user" && message.ui?.kind === "steer" ? t("requirementsRecorded") : message.role === "user" && message.ui?.kind === "information" ? t("informationRecorded") : ""}</strong>{message.role === "user" && task.allowed_actions?.edit_message ? <RynAgentMessageEditor key={`${message.message_id || index}/${message.content}`} text={message.ui?.text ?? message.content ?? ""} disabled={busy || uncertain || controlUncertain || Boolean(task.archived) || !canUseAgentAction(task, "edit_message")} onSend={text => editMessage(task.messages.indexOf(message), text)}><ChatMarkdown>{message.ui?.text ?? message.content ?? ""}</ChatMarkdown></RynAgentMessageEditor> : message.role === "user" ? <div className="ra-user-bubble"><ChatMarkdown>{message.ui?.text ?? message.content ?? ""}</ChatMarkdown></div> : <ChatMarkdown fileLinks>{message.content || ""}</ChatMarkdown>}</>}</div>{message.role === "user" && <RynAgentTurnDuration task={task} turnIndex={entry.turnIndex} />}{isTurnEnd && <TurnDeliverables turnIndex={entry.turnIndex} />}</Fragment>; })}
            {streamedText && <div className="ra-message ra-assistant" aria-live="polite"><div className="ra-reply-label"><Sparkles size={16} /><strong>Ryn</strong><small>{t("partialResponse")}</small></div><RynAgentStream text={streamedText} streamKey={`${task.id}/${streamStart?.seq}`} active={selectedExecuting} /></div>}
            {pendingFeedback}
            {!visiblePending && <ExecutionProgress task={task} />}

            {task.pending_input && (task.pending_input.kind === "credential_ref" ? <RynAgentInputCard key={`${task.id}/${task.pending_input.id}`} task={task} busy={busy || controlUncertain} onSaved={acceptTask} onChangeMethod={() => { changeDraft(t("cliSourceClarification")); setMessageIntent("steer"); input.current?.focus(); }} /> : <InputRequest key={`${task.id}/${task.pending_input.id}`} task={task} busy={busy || controlUncertain} onAnswer={answer} />)}
            {task.pending_approval && <ApprovalCard approval={task.pending_approval} controlled={Boolean(task.allowed_actions)} enabled={canUseAgentAction(task, "approve", Boolean(enabled))} canDeny={canUseAgentAction(task, "deny", !settingsReady || Boolean(enabled))} busy={busy || controlUncertain} waiting={agentTaskState(task) === "waiting_approval"} onAction={action} />}
            {task.status === "interrupted" && <p className="ra-banner">{t(task.error === "agent_node_restarted" ? "restartHelp" : "interruptedHelp")}<code>{task.error}</code></p>}
            {task.status === "cancelled" && <p className="ra-turn-evidence">{t("cancelHelp")}</p>}
            {task.result && <section className="ra-result"><strong>{t("result")}</strong><ChatMarkdown fileLinks>{task.result}</ChatMarkdown></section>}
            <TaskResultFiles key={`results/${task.id}`} task={task} />
            </TaskArtifacts>
              {task.error && task.status !== "interrupted" && <section className="ra-task-error" role="alert"><strong>{t("error")}</strong><p>{t(task.error === "agent_model_destination_unverifiable" ? "taskDestinationUnavailable" : task.error === "agent_model_destination_changed" ? "modelDestinationChanged" : task.error === "agent_model_protocol_unsupported" ? "protocolUnsupported" : task.error === "agent_model_protocol_invalid" ? "protocolInvalid" : "unknownError")}</p><code>{task.error}</code></section>}
          </div> : visiblePending ? <div className="ra-thread">{pendingFeedback}</div> : !settingsReady ? <div className="ra-empty"><CircleAlert size={32} /><p>{t(resourceErrors.settings || "settingsLoadError")}</p></div> : !enabled ? <div key="welcome" className="ra-welcome"><Sparkles className="ra-welcome-symbol" size={42} /><h2>{t("disabledTitle")}</h2><p>{t("disabledHelp")}</p><label className="ra-enable-model">{t("model")}{modelSelect()}</label>{!models.some(canUseAgentModel) && <p>{t("noModel")} <Link to="/services/sources">{t("configureModels")}</Link></p>}<button className="ra-primary" disabled={busy || !selectedModel || !canUseAgentModel(selectedModel)} onClick={() => void updateSettings({ enabled: true, ...(model ? { model_id: model } : {}) })}>{t("enable")}</button><small>{t("enableHelp")}</small></div> : <div key="welcome" className="ra-welcome"><Sparkles className="ra-welcome-symbol" size={42} /><h2>{currentProject ? <Trans ns="rynAgent" i18nKey="projectWelcome" values={{ name: currentProject.path }} components={{ project: <span className="ra-welcome-project" /> }} /> : t("welcome")}</h2>{!currentProject && <p>{t("welcomeHelp")}</p>}{!currentProject && <div className="ra-suggestions">{([["inspect", ListChecks], ["sharing", Share2], ["devices", Network]] as const).map(([key, Icon]) => <button key={key} onClick={() => { changeDraft(t(`${key}Prompt`)); input.current?.focus(); }}><Icon size={15} />{t(key)}</button>)}</div>}</div>}

        </div>
        {(enabled || task) && !task?.archived && <div className="ra-composer-area">


          {!models.some(canUseAgentModel) && <p className="ra-model-help">{t("noModel")} <Link to="/services/sources">{t("models")}</Link></p>}

          {!selected && selectedModel && !canUseAgentModel(selectedModel) && <p className="ra-model-help" role="status">{selectedModel.name} · {t(!selectedModel.available ? "selectedModelUnavailable" : selectedModel.source === "peer" && selectedModel.destination_binding_supported === false ? "modelDestinationUnavailable" : "notSupported")}</p>}
          {task?.local_execution && !task.local_execution.workspace_root && (task.pending_input?.fields.some(field => field.id === "workspace_root") || task.events.some(event => event.type === "tool_result" && (event.result as {error?: string} | undefined)?.error === "agent_workspace_required")) && ["failed", "paused", "waiting_input"].includes(task.status) && <WorkspaceSetup initial={settings?.workspace_root || ""} busy={busy || controlUncertain} onBind={async workspace_root => {
            if (mutationRef.current) return;
            mutationRef.current = true; setBusy(true); setError("");
            try { acceptTask(await submitControl({task_id: task.id, route: "workspace", body: {workspace_root, expected_control_revision: task.control_revision, client_request_id: crypto.randomUUID()}})); await refresh(); }
            catch (reason) { setError(mutationErrorKey(reason)); }
            finally { mutationRef.current = false; setBusy(false); }
          }} />}
          <RynAgentComposer key={`composer/${draftKey}`} attachments={draftAttachments} onAttachmentsChange={attachments => setAttachmentsByDraft(current => ({ ...current, [draftKey]: attachments }))} onAttachmentsBusyChange={setAttachmentsBusy} project={currentProject} workspace={settings?.workspace_root} value={draft} inputRef={input} inputDisabled={(!task?.allowed_actions && !enabled) || detailLoading} busy={busy} onChange={changeDraft} onSend={() => void send()} onProject={() => { setTasksCollapsed(false); save("ryn.agent.tasks", "open"); }} onSettings={() => setSettingsOpen(true)} settingsDisabled={!settingsReady} permissionMode={task ? task.permission_mode || "unknown" : settings?.permission_mode || "workspace"} permissionLocked={Boolean(selected)} onPermission={permission_mode => void updateSettings({ permission_mode })} modelPicker={modelSelect()} options={<>          {!selected && enabled && <RynAgentScope key="new-scope" task={null} modelID={model} modelName={selectedModel?.name || model} destinationRevision={selectedModel?.destination_revision} sourceName={selectedModel?.source_name} sourceID={selectedModel?.source_id} providerID={selectedModel?.peer_id} initialSelection={scopeDraft?.scope} busy={busy || uncertain || controlUncertain} onSelect={(scope, model_id, destination_revision) => setScopeDraft({ scope, model_id, destination_revision })} />}{!selected && <SkillSelection value={skillIDs} onChange={setSkillIDs} disabled={busy || uncertain} />}<p className="ra-model-context">{task ? t("taskModel") : t("newTaskModel")}: {selectedModel?.name || task?.model_id || model || t("chooseModel")}{settings && <span> · {t("defaultModel")}: {models.find(item => item.id === settings.model_id)?.name || settings.model_id || t("chooseModel")}</span>}</p>{messageIntents.length > 1 && <select aria-label={t("messageIntent")} value={effectiveIntent} disabled={busy} onChange={event => setMessageIntent(event.target.value as AgentMessageIntent)}><option value="steer">{t("intentSteer")}</option><option value="information">{t("intentInformation")}</option></select>}{task?.allowed_actions && messageIntents.length > 0 && <p className="ra-model-context">{t(effectiveIntent === "steer" ? "steerHelp" : "informationHelp")}</p>}</>} sendButton={<>{stopButton}{(!stopButton || Boolean(draft.trim() || draftAttachments.length)) && <button type="button" className="ra-primary ra-send" aria-label={t(sendLabel)} disabled={!canSend} onClick={() => void send()}>{visiblePending?.phase === "sending" ? <LoaderCircle size={18} className="ra-spinner" /> : <ArrowUp size={18} />}</button>}</>} />

        </div>}</>}
      </div>
    </div>}
    {taskModelOpen && task && <div className="ra-modal-backdrop" onClick={event => { if (event.target === event.currentTarget && !busy) setTaskModelOpen(false); }}><section className="ra-modal ra-model-dialog" role="dialog" aria-modal="true" aria-label={t("taskModel")} onKeyDown={event => { if (event.key === "Escape" && !busy) setTaskModelOpen(false); }}><div className="ra-modal-head"><h2>{t("taskModel")}</h2><button type="button" className="ra-icon" aria-label={t("close")} disabled={busy} onClick={() => setTaskModelOpen(false)}><X size={18} /></button></div><RynAgentModel key={`model/${task.id}/${taskModelTarget}`} task={task} models={models} defaultModelID={settings?.model_id} busy={busy || controlUncertain} expanded initialModelID={taskModelTarget} onChange={next => { acceptTask(next); setTaskModelOpen(false); }} /></section></div>}
    {settingsOpen && settings && <AgentSettingsDialog settings={settings} projects={projects} currentProject={currentProject} busy={busy} onClose={() => setSettingsOpen(false)} onSave={async update => { if (await updateSettings(update)) setSettingsOpen(false); }} />}
    <AutomationDialog open={automationDialogOpen} initial={editingAutomation} activeTaskId={selected} tasks={tasks} runs={automationRuns} zh={i18n.language.startsWith("zh")} onClose={() => setAutomationDialogOpen(false)} onSaved={() => refreshAutomations()} onDeleted={() => refreshAutomations()} />
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

function TaskProgress({ task, settings }: { task: AgentTask; settings?: AgentSettings | null }) {
  const { t } = useTranslation("rynAgent");
  return <>
    {task.plan && task.plan.steps.length > 0 && <section className="ra-plan" aria-label={t("plan")}><strong>{t("plan")} · {t("goalRevision", { revision: task.plan.goal_revision })}</strong><ol>{task.plan.steps.map(step => <li key={step.id}><span>{step.text}</span><small>{t(`plan_${step.status}`)}</small></li>)}</ol><small>{t("planHelp")}</small></section>}
    <TaskSkills skills={task.skills} />
    <AgentTaskComputerStatus task={task} settings={settings} />
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
  const localFile = approval.tool.startsWith("ryn_file_") && typeof preview.path === "string";
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
    {localFile && <dl className="ra-change-summary">
      <dt>{t("target")}</dt><dd><code>{String(preview.path)}</code>{typeof preview.destination === "string" ? <> → <code>{preview.destination}</code></> : null}</dd>
      {typeof preview.bytes === "number" && <><dt>{t("fileSize")}</dt><dd>{preview.bytes < 1048576 ? `${(preview.bytes / 1024).toFixed(1)} KiB` : `${(preview.bytes / 1048576).toFixed(1)} MiB`}</dd></>}
      {typeof preview.added_lines === "number" && <><dt>{t("fileChange")}</dt><dd>{t(preview.before_sha256 ? "fileModify" : "fileCreate")} · +{preview.added_lines} −{typeof preview.removed_lines === "number" ? preview.removed_lines : 0}</dd></>}
    </dl>}
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

function AgentSettingsDialog({ settings, projects, currentProject, busy, onClose, onSave }: { settings: AgentSettings; projects?: AgentProject[]; currentProject?: AgentProject; busy: boolean; onClose: () => void; onSave: (settings: Partial<AgentSettings>) => Promise<void> }) {
  const { t } = useTranslation("rynAgent");
  const [enabled, setEnabled] = useState(settings.enabled);
  const [tools, setTools] = useState<AgentTool[] | null>(null);
  const [toolError, setToolError] = useState(false);
  const [allowed, setAllowed] = useState(settings.allowed_write_tools || []);
  const [workspace, setWorkspace] = useState(settings.workspace_root || "");
  const [desktopAccess, setDesktopAccess] = useState(settings.desktop_access || false);
  const [contextMode, setContextMode] = useState(settings.max_context_chars == null ? "auto" : "manual");
  const [contextLimit, setContextLimit] = useState(String(settings.max_context_chars ?? 600000));
  const contextBytes = Number(contextLimit);
  const contextValid = contextMode === "auto" || (Number.isInteger(contextBytes) && contextBytes >= 4000 && contextBytes <= 2000000);
  const contextValue = contextMode === "auto" ? null : contextBytes;
  const contextChange = contextValue === settings.max_context_chars ? {} : { max_context_chars: contextValue };
  const contextPolicy = settings.context_budget_policy;
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => { const previous = document.activeElement as HTMLElement | null; close.current?.focus(); return () => previous?.focus(); }, []);
  useEffect(() => { let stopped = false; void rynAgent.tools().then(value => { if (!stopped) setTools(value.tools.filter(tool => tool.effect === "write")); }).catch(() => { if (!stopped) setToolError(true); }); return () => { stopped = true; }; }, []);
  return <div className="ra-modal-backdrop" onClick={event => { if (event.target === event.currentTarget && !busy) onClose(); }}><section className="ra-modal" role="dialog" aria-modal="true" aria-label={t("settings")} onKeyDown={event => { if (event.key === "Escape" && !busy) onClose(); if (event.key === "Tab") { const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled)')); const first = controls[0], last = controls.at(-1); if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); } } }}><div className="ra-modal-head"><h2>{t("settings")}</h2><button ref={close} className="ra-icon" disabled={busy} aria-label={t("close")} onClick={onClose}><X size={18} /></button></div><label className="ra-setting"><span>{t("enabledLabel")}<small>{t("enableHelp")}</small></span><input type="checkbox" checked={enabled} disabled={busy} onChange={event => setEnabled(event.target.checked)} /></label><label className="ra-setting"><span>{t("workspaceRoot")}<small>{t("workspaceHelp")}</small></span><input aria-label={t("workspaceRoot")} value={workspace} disabled={busy} onChange={event => setWorkspace(event.target.value)} placeholder={t("defaultOutputFolder")} /></label><label className="ra-setting"><span>{t("desktopAccess")}<small>{t("desktopAccessHelp")}</small></span><input type="checkbox" aria-label={t("desktopAccess")} checked={desktopAccess} disabled={busy} onChange={event => setDesktopAccess(event.target.checked)} /></label>
    <label className="ra-setting"><span>{t("contextBudget")}<small>{t("contextBudgetHelp")}</small></span>
      <select aria-label={t("contextBudget")} value={contextMode} disabled={busy} onChange={event => setContextMode(event.target.value)}>
        <option value="auto">{t("contextAuto")}</option><option value="manual">{t("contextManual")}</option>
      </select>
    </label>
    {contextMode === "manual" && <label className="ra-setting"><span>{t("contextManualBytes")}</span>
      <input type="number" aria-label={t("contextManualBytes")} min={4000} max={2000000} step={1} value={contextLimit} disabled={busy} onChange={event => setContextLimit(event.target.value)} />
    </label>}
    {!contextValid && <p role="alert">{t("contextBudgetInvalid")}</p>}
    {contextPolicy && <p className="ra-model-context">{contextPolicy.context_window_known
      ? t("contextWindowKnown", { count: contextPolicy.context_window_tokens }) : t("contextWindowUnknown")}</p>}
    <div className="ra-setting"><span>{t("extensions")}<small>{t("manageExtensions")}</small></span><Link className="ra-link" to="/settings/extensions" onClick={onClose}>{t("extensions")}</Link></div>
    <PreferencesEditor /><MemoryEditor projects={projects} currentProject={currentProject} /><p>{t("modeHelp")}</p><fieldset className="ra-tool-permissions"><legend>{t("allowedTools")}</legend><p>{t("allowedToolsHelp")}</p>{toolError ? <p role="alert">{t("toolsLoadError")}</p> : tools === null ? <p role="status">{t("loading")}</p> : tools.length ? tools.map(tool => <label key={tool.name}><input type="checkbox" disabled={busy} checked={allowed.includes(tool.name)} onChange={event => setAllowed(current => event.target.checked ? [...current, tool.name] : current.filter(name => name !== tool.name))} /><span>{t(`tool_${tool.name}`, { defaultValue: tool.title })}</span></label>) : <p>{t("noWriteTools")}</p>}</fieldset><button className="ra-primary" disabled={busy || !contextValid} onClick={() => void onSave({ enabled, workspace_root: workspace.trim(), desktop_access: desktopAccess, ...contextChange, ...(tools !== null ? { allowed_write_tools: allowed.filter(name => tools.some(tool => tool.name === name)) } : {}) })}>{t(busy ? "saving" : "save")}</button></section></div>;
}

function WorkspaceSetup({initial, busy, onBind}: {initial: string; busy: boolean; onBind: (path: string) => Promise<void>}) {
  const {t} = useTranslation("rynAgent");
  const [path, setPath] = useState(initial);
  return <section className="ra-workspace-setup"><label>{t("chooseTaskWorkspace")}<small>{t("chooseTaskWorkspaceHelp")}</small><input aria-label={t("workspaceRoot")} value={path} disabled={busy} onChange={e => setPath(e.target.value)} placeholder={t("workspacePathExample")} /></label><button className="ra-primary" disabled={busy || !path.trim()} onClick={() => void onBind(path.trim())}>{t("bindWorkspaceContinue")}</button></section>;
}
