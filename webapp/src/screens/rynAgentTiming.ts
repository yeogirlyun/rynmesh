import { agentTaskState, type AgentEvent, type AgentTask } from "../domain/rynAgent";

const turnEvents = new Set(["created", "message_added", "goal_changed", "information_added", "input_answered"]);
const stopEvents = new Set(["completed", "failed", "cancelled", "interrupted", "paused", "approval_requested", "input_requested", "run_settled"]);
const startEvents = new Set(["model_started", "verification_model_started", "tool_started", "context_compaction_started", "reconciliation_started"]);
const endEvents = new Set(["assistant", "verification_finished", "tool_result", "context_compacted", "reconciliation_finished"]);

export interface AgentTurnTiming {
  seconds: number | null;
  approximate: boolean;
  live: boolean;
  state: "running" | "queued" | "waiting_approval" | "waiting_input" | "paused" | "ended";
}

function seconds(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function timestamp(value: number | string): number | null {
  const result = typeof value === "number" ? value * (value < 1e12 ? 1000 : 1) : Date.parse(value);
  return Number.isFinite(result) ? result : null;
}

function operationKey(event: AgentEvent): string {
  if (event.type === "tool_started" || event.type === "tool_result") return `tool:${String(event.call_id ?? event.operation_id ?? event.tool)}`;
  if (event.type === "verification_model_started" || event.type === "verification_finished") return "verification";
  if (event.type === "context_compaction_started" || event.type === "context_compacted") return "compaction";
  if (event.type === "reconciliation_started" || event.type === "reconciliation_finished") return "reconciliation";
  return "model";
}

function hasUnsettledOperation(events: AgentEvent[], before: number): boolean {
  const active = new Set<string>();
  for (const event of events) {
    if (event.seq >= before) break;
    if (startEvents.has(event.type)) active.add(operationKey(event));
    else if (endEvents.has(event.type)) active.delete(operationKey(event));
    else if (stopEvents.has(event.type) && event.operation_in_flight !== true) active.clear();
  }
  return active.size > 0;
}

/** Legacy records have only event timestamps. Count measured operation intervals,
 * never the span across user input, approvals, a pause, or queue time. Parallel
 * tools contribute their union rather than their sum. The caller labels this
 * fallback as approximate because it excludes preparation before dispatch. */
function activitySeconds(events: AgentEvent[], startSeq: number, endSeq: number): number | null {
  const active = new Map<string, number>();
  const intervals: Array<[number, number]> = [];
  const startAt = timestamp(events.find(event => event.seq === startSeq)!.at);
  const endAt = timestamp(events.find(event => event.seq === endSeq)?.at ?? "");
  let observed = false;
  for (const event of events) {
    if (event.seq > endSeq) break;
    const at = timestamp(event.at);
    if (at === null) continue;
    const key = operationKey(event);
    if (startEvents.has(event.type)) active.set(key, at);
    else if (endEvents.has(event.type)) {
      const began = active.get(key);
      if (began !== undefined) {
        if (event.seq >= startSeq && startAt !== null && at >= began) {
          intervals.push([Math.max(began, startAt), at]);
          observed = true;
        }
        active.delete(key);
      }
    } else if (stopEvents.has(event.type) && event.operation_in_flight !== true) {
      // An interrupted operation has no confirmed duration. Do not fill the
      // missing receipt with the time until the next request.
      active.clear();
    }
  }
  // Only a known stop closes unfinished historical work. A live interval is
  // extended from the server clock below, never from the browser's wall clock.
  if (endAt !== null && stopEvents.has(events.find(event => event.seq === endSeq)?.type || "")) {
    for (const began of active.values()) {
      if (startAt !== null && endAt >= began) intervals.push([Math.max(began, startAt), endAt]);
    }
  }
  if (!observed && !intervals.length) return null;
  intervals.sort((left, right) => left[0] - right[0]);
  let total = 0, through = -Infinity;
  for (const [began, ended] of intervals) {
    total += Math.max(0, ended - Math.max(began, through));
    through = Math.max(through, ended);
  }
  return total / 1000;
}

/** User messages and their corresponding host-authored boundary events share
 * order. Missing legacy boundaries stay unknown instead of shifting labels to
 * another user's turn. run_elapsed_seconds is a persisted monotonic counter. */
export function agentTurnTiming(task: AgentTask, turnIndex: number): AgentTurnTiming {
  const owners = task.messages.filter(message => message.role === "user");
  const latest = turnIndex === owners.length - 1;
  const taskState = agentTaskState(task);
  const state = !latest ? "ended" : taskState === "waiting_approval" || taskState === "waiting_input" || taskState === "paused" ? taskState
    : ["running", "pausing", "reconciling"].includes(taskState) ? "running" : taskState === "queued" ? "queued" : "ended";
  const unknown: AgentTurnTiming = { seconds: null, approximate: false, live: false, state };
  if (turnIndex < 0 || turnIndex >= owners.length) return unknown;
  const events = [...task.events].sort((left, right) => left.seq - right.seq);
  const boundaries: AgentEvent[] = [];
  for (const event of events) {
    if (turnEvents.has(event.type)) boundaries.push(event);
    else if (event.type === "message_edited" && typeof event.user_turn_index === "number" && Number.isInteger(event.user_turn_index) && event.user_turn_index >= 0) {
      boundaries.splice(event.user_turn_index);
      boundaries.push(event);
    }
  }
  const current = latest ? task.run_timing : undefined;
  const currentSeconds = seconds(current?.elapsed_seconds);
  const live = latest && state === "running" && current?.measurement === "live";
  if (boundaries.length !== owners.length) {
    // A single known run is still safe when its original creation event was
    // omitted from an old record. Multiple messages cannot be attributed.
    return latest && owners.length === 1 && currentSeconds !== null
      ? { seconds: currentSeconds, approximate: false, live, state } : unknown;
  }
  const start = boundaries[turnIndex];
  // A retained earlier turn ends at its original next owner boundary. Audit
  // events from a discarded branch must not add runtime to that earlier turn.
  const nextOriginal = events.find(event => event.seq > start.seq && (turnEvents.has(event.type) || event.type === "message_edited"));
  const scoped = events.filter(event => event.seq >= start.seq && (!nextOriginal || event.seq < nextOriginal.seq));
  const last = scoped.at(-1) || start;
  const baseline = seconds(start.run_elapsed_seconds) ?? (turnIndex === 0 ? 0 : null);
  const terminal = [...scoped].reverse().find(event => stopEvents.has(event.type));
  const endCounter = latest && currentSeconds !== null ? currentSeconds
    : seconds(last.run_elapsed_seconds) ?? seconds(terminal?.run_elapsed_seconds);
  // Events emitted while a model is in flight carry only its unsettled
  // baseline. Avoid presenting that baseline as an exact split of two turns.
  const unsettledBoundary = turnIndex > 0 && hasUnsettledOperation(events, start.seq);
  const unsettledEnd = nextOriginal && hasUnsettledOperation(events, nextOriginal.seq);
  if (baseline !== null && endCounter !== null && endCounter >= baseline && !unsettledBoundary && !unsettledEnd) {
    return { seconds: endCounter - baseline, approximate: false, live, state };
  }
  // Older, single-turn records retain an authoritative current-run snapshot.
  if (latest && turnIndex === 0 && currentSeconds !== null) return { seconds: currentSeconds, approximate: false, live, state };
  const measured = activitySeconds(events, start.seq, last.seq);
  return { seconds: measured, approximate: measured !== null, live: false, state };
}
