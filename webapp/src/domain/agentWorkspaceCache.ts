import { nodeControlUrl } from "./nodeUrl";
import type { AgentModel, AgentSettings, AgentTask, AgentTaskSummary } from "./rynAgent";
import { agentTaskSummary, loadAgentRecords, removeAgentTask, saveAgentTask, saveAgentTaskList } from "./agentTaskStore";

interface AgentWorkspaceSnapshot {
  settings: AgentSettings | null;
  models: AgentModel[];
  model: string;
}

function key() { return `ryn.agent.workspace-v1:${nodeControlUrl("/agent")}`; }
const taskCaches = new Map<string, Map<string, AgentTask>>();
const taskLists = new Map<string, AgentTaskSummary[]>();
const hydrations = new Map<string, Promise<void>>();
/** Opened conversations held in memory; the device copy keeps a few more. */
export const AGENT_TASKS_IN_MEMORY = 20;
export function agentTaskCache(): Map<string, AgentTask> {
  const scope = key();
  let tasks = taskCaches.get(scope);
  if (!tasks) { tasks = new Map(); taskCaches.set(scope, tasks); }
  return tasks;
}

/** The last complete task list from the node, kept across page switches. */
export function agentTaskListCache(): AgentTaskSummary[] | undefined {
  return taskLists.get(key());
}

export function rememberAgentTaskList(tasks: AgentTaskSummary[]) {
  const scope = key();
  taskLists.set(scope, tasks.map(agentTaskSummary));
  saveAgentTaskList(scope, tasks);
}

export function rememberAgentTask(task: AgentTask) {
  saveAgentTask(key(), task);
}

export function forgetAgentTask(id: string) {
  const scope = key();
  const list = taskLists.get(scope);
  if (list?.some(item => item.id === id)) {
    const next = list.filter(item => item.id !== id);
    taskLists.set(scope, next);
    saveAgentTaskList(scope, next);
  }
  taskCaches.get(scope)?.delete(id);
  removeAgentTask(scope, id);
}

/**
 * Loads this node's saved Agent records into memory once per app run. Records
 * already refreshed from the node in this run are never replaced by older copies.
 */
export function hydrateAgentRecords(): Promise<void> {
  const scope = key();
  let hydration = hydrations.get(scope);
  if (!hydration) {
    hydration = loadAgentRecords(scope).then(({ tasks, details }) => {
      if (tasks && !taskLists.has(scope)) taskLists.set(scope, tasks);
      const cache = agentTaskCache();
      // Oldest first, so the most recently opened end up last (newest) in the Map.
      for (const task of details.slice(0, AGENT_TASKS_IN_MEMORY).reverse()) {
        const known = cache.get(task.id);
        if (known && known.revision >= task.revision) continue;
        cache.delete(task.id);
        cache.set(task.id, task);
        if (cache.size > AGENT_TASKS_IN_MEMORY) cache.delete(cache.keys().next().value!);
      }
    });
    hydrations.set(scope, hydration);
  }
  return hydration;
}

/** Test isolation: forgets everything this module holds in memory. */
export function resetAgentWorkspaceMemory() {
  taskCaches.clear();
  taskLists.clear();
  hydrations.clear();
}

// Only configuration and public model metadata persist. Conversation contents,
// credentials and pending controls keep their existing storage lifecycle.
export function readAgentWorkspace(): AgentWorkspaceSnapshot {
  const empty = { settings: null, models: [], model: "" };
  try {
    const value = JSON.parse(localStorage.getItem(key()) || "null");
    if (!value || !Array.isArray(value.models) || typeof value.model !== "string") return empty;
    if (value.settings && (typeof value.settings.enabled !== "boolean" || typeof value.settings.model_id !== "string" ||
        !["workspace", "read_only", "confirm_changes", "automatic"].includes(value.settings.permission_mode) ||
        !Array.isArray(value.settings.allowed_write_tools))) return empty;
    if (!value.models.every((item: AgentModel) => typeof item.id === "string" && typeof item.name === "string" &&
        typeof item.source === "string" && typeof item.available === "boolean")) return empty;
    return { settings: value.settings || null, models: value.models, model: value.model };
  } catch { return empty; }
}

export function writeAgentWorkspace(settings: AgentSettings | null, models: AgentModel[], model: string) {
  try {
    const publicSettings = settings ? {
      enabled: settings.enabled, model_id: settings.model_id, permission_mode: settings.permission_mode,
      allowed_write_tools: settings.allowed_write_tools, workspace_root: settings.workspace_root, desktop_access: settings.desktop_access,
      max_steps: settings.max_steps, max_seconds: settings.max_seconds,
      max_context_chars: settings.max_context_chars, max_output_chars: settings.max_output_chars,
    } : null;
    const publicModels = models.map(item => ({
      id: item.id, name: item.name, source: item.source, available: item.available,
      agent_supported: item.agent_supported, tool_support: item.tool_support,
      cli_model: item.cli_model, cli_source: item.cli_source, service_id: item.service_id,
      destination_revision: item.destination_revision, destination_binding_supported: item.destination_binding_supported,
      source_id: item.source_id, source_name: item.source_name, peer_id: item.peer_id,
      supported_reasoning_efforts: item.supported_reasoning_efforts, default_reasoning_effort: item.default_reasoning_effort,
      context_window: item.context_window, context_window_known: item.context_window_known, context_window_basis: item.context_window_basis,
    }));
    localStorage.setItem(key(), JSON.stringify({ settings: publicSettings, models: publicModels, model }));
  } catch { /* Disabled or full storage must not interrupt the workspace. */ }
}
