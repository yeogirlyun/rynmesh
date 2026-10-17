import { useCallback, useEffect, useRef, useState } from "react";
import { agentTaskState, type AgentTask, type AgentTaskSummary, type AgentStatus } from "../domain/rynAgent";
import { nodeControlUrl } from "../domain/nodeUrl";

interface Observation {
  revision: number;
  control?: number;
  round?: number;
  goal?: number;
  event?: number;
  state: AgentStatus;
  archived: boolean;
  completion: number;
  completionRevision: number;
  read: number;
}
interface AttentionStore {
  key: string;
  initialized: boolean;
  entries: Record<string, Observation>;
}
interface AttentionOptions {
  tasks: AgentTaskSummary[];
  selected: string;
  /** Only pass a successfully loaded detail that is actually being displayed. */
  openedTask: AgentTask | null;
  visible: boolean;
  /** True after the first successful list response, including an empty list. */
  ready?: boolean;
}

const states = new Set<AgentStatus>(["queued", "running", "waiting_approval", "waiting_input", "pausing", "reconciling", "paused", "completed", "cancelled", "failed", "interrupted"]);
const number = (value: unknown): number | undefined => typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
const own = (entries: Record<string, Observation>, id: string) => Object.hasOwn(entries, id) ? entries[id] : undefined;

function load(key: string): AttentionStore {
  const empty: AttentionStore = { key, initialized: false, entries: Object.create(null) };
  try {
    const saved = JSON.parse(localStorage.getItem(key) || "null");
    if (saved?.version !== 1 || typeof saved.initialized !== "boolean" || !Array.isArray(saved.tasks)) return empty;
    for (const pair of saved.tasks) {
      if (!Array.isArray(pair) || typeof pair[0] !== "string" || !pair[0] || !pair[1] || typeof pair[1] !== "object") continue;
      const [id, value] = pair;
      const revision = number(value.revision), completion = number(value.completion), read = number(value.read), completionRevision = number(value.completionRevision);
      if (revision === undefined || completion === undefined || read === undefined || read > completion || completionRevision === undefined || !states.has(value.state)) continue;
      empty.entries[id] = { revision, completion, read, completionRevision, state: value.state, archived: value.archived === true, control: number(value.control), round: number(value.round), goal: number(value.goal), event: number(value.event) };
    }
    empty.initialized = saved.initialized;
  } catch { /* The in-memory state still works when browser storage is unavailable. */ }
  return empty;
}

function persist(store: AttentionStore) {
  try {
    localStorage.setItem(store.key, JSON.stringify({ version: 1, initialized: store.initialized, tasks: Object.entries(store.entries) }));
  } catch { /* Private browsing and full storage must not block the sidebar. */ }
}

function fingerprint(task: AgentTaskSummary) {
  return { revision: number(task.revision) ?? 0, control: number(task.control_revision), round: number(task.budget?.round), goal: number(task.goal?.revision), event: number(task.completion_event_seq), state: agentTaskState(task), archived: task.archived === true };
}

function changedRound(previous: Observation, incoming: ReturnType<typeof fingerprint>) {
  if (previous.round !== undefined && incoming.round !== undefined) return previous.round !== incoming.round || (previous.goal !== undefined && incoming.goal !== undefined && previous.goal !== incoming.goal);
  if (previous.goal !== undefined && incoming.goal !== undefined) return previous.goal !== incoming.goal;
  // Old nodes do not expose a completion event or budget round. A control
  // change is the best available signal; archive operations are metadata.
  return previous.control !== undefined && incoming.control !== undefined && previous.control !== incoming.control && previous.archived === incoming.archived;
}

function observe(store: AttentionStore, task: AgentTaskSummary): AttentionStore {
  const previous = own(store.entries, task.id), incoming = fingerprint(task);
  if (previous && (incoming.revision < previous.revision || (incoming.event !== undefined && previous.event !== undefined && incoming.event < previous.event))) return store;
  if (previous && incoming.revision === previous.revision) {
    // A node upgrade may add the reliable marker to an otherwise identical
    // cached version. Adopt that baseline without inventing a new completion.
    return previous.event === undefined && incoming.event !== undefined
      ? { ...store, entries: { ...store.entries, [task.id]: { ...previous, event: incoming.event } } }
      : store;
  }
  let completion = previous?.completion ?? 0;
  let completionRevision = previous?.completionRevision ?? 0;
  let read = previous?.read ?? 0;
  const completed = incoming.state === "completed";
  const newCompletion = completed && (incoming.event === 0 ? false : !previous || (incoming.event !== undefined && previous.event !== undefined
    ? incoming.event > previous.event
    : previous.state !== "completed" || changedRound(previous, incoming)));
  if (newCompletion) {
    completion += 1;
    completionRevision = incoming.revision;
    // Establish a quiet baseline for history on the first connection. Entries
    // already observed running still notify when they subsequently complete.
    if (!previous && !store.initialized) read = completion;
  }
  const next: Observation = { ...incoming, completion, completionRevision, read };
  return { ...store, entries: { ...store.entries, [task.id]: next } };
}

function acknowledge(store: AttentionStore, id: string, options: AttentionOptions): AttentionStore {
  const opened = options.openedTask, entry = own(store.entries, id);
  if (!entry || !options.visible || options.selected !== id || opened?.id !== id || agentTaskState(opened) !== "completed" || entry.state !== "completed" || entry.read === entry.completion) return store;
  const detail = fingerprint(opened);
  if (entry.event !== undefined) {
    if (detail.event === undefined || detail.event < entry.event) return store;
  } else if (detail.revision < entry.completionRevision || changedRound(entry, detail)) return store;
  return { ...store, entries: { ...store.entries, [id]: { ...entry, read: entry.completion } } };
}

/** Completion notifications track finished turns, independently of success verification. */
export function useAgentTaskAttention(options: AttentionOptions) {
  const key = `ryn.agent.attention.v1:${nodeControlUrl("/agent")}`;
  const [store, setStore] = useState(() => load(key));
  const current = useRef(store), latestOptions = useRef(options);
  latestOptions.current = options;
  const commit = useCallback((next: AttentionStore) => {
    if (next === current.current) return;
    current.current = next;
    persist(next);
    setStore(next);
  }, []);

  useEffect(() => {
    let next = current.current.key === key ? current.current : load(key);
    for (const task of options.tasks) next = observe(next, task);
    if (options.openedTask) next = observe(next, options.openedTask);
    if (!next.initialized && (options.ready ?? true)) next = { ...next, initialized: true };
    next = acknowledge(next, options.selected, options);
    commit(next);
  }, [key, options.tasks, options.openedTask, options.selected, options.visible, options.ready, commit]);

  const markRead = useCallback((id: string) => {
    // A click alone is insufficient: the matching detail must have loaded and
    // be visible, and must include the completion that raised the notification.
    commit(acknowledge(current.current, id, latestOptions.current));
  }, [commit]);
  const forget = useCallback((id: string) => {
    if (!own(current.current.entries, id)) return;
    const entries = { ...current.current.entries };
    delete entries[id];
    commit({ ...current.current, entries });
  }, [commit]);
  const isUnread = useCallback((task: Pick<AgentTaskSummary, "id">) => {
    const entry = store.key === key ? own(store.entries, task.id) : undefined;
    return entry?.state === "completed" && entry.completion > entry.read;
  }, [store, key]);
  return { isUnread, markRead, forget };
}
