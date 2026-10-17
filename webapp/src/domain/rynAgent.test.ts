import { expect, it } from "vitest";
import { agentToolResultState, agentTurnToolCount, canUseAgentAction, canUseAgentModel, isAgentLateToolResult, mergeAgentTaskSnapshot, mergeUnchangedTask, pendingAgentText, type AgentTask } from "./rynAgent";

it("requires a supported assistant protocol and current availability, independently of diagnostic probes", () => {
  const ready = { id: "model", name: "Model", source: "peer", available: true, agent_supported: true };
  expect(canUseAgentModel(ready)).toBe(true);
  expect(canUseAgentModel({ ...ready, tool_support: "unknown" })).toBe(true);
  expect(canUseAgentModel({ ...ready, tool_support: "unsupported" })).toBe(true);
  expect(canUseAgentModel({ ...ready, agent_supported: false, tool_support: "supported" })).toBe(false);
  expect(canUseAgentModel({ ...ready, destination_binding_supported: false })).toBe(false);
  expect(canUseAgentModel({ ...ready, agent_supported: undefined })).toBe(false);
  expect(canUseAgentModel({ ...ready, available: false })).toBe(false);
});

it("reports only tool receipts in the current turn without treating a finished reply as tool execution", () => {
  const task: Pick<AgentTask, "events"> = { events: [
    { seq: 1, type: "created", at: 1 }, { seq: 2, type: "tool_result", at: 1, result: { ok: true } },
    { seq: 3, type: "message_added", at: 2 }, { seq: 4, type: "assistant", at: 2, content: "I cannot execute the tool." },
  ] };
  expect(agentTurnToolCount(task)).toBe(0);
  task.events.push({ seq: 5, type: "tool_result", at: 3, result: { devices: [] } });
  expect(agentTurnToolCount(task)).toBe(1);
  task.events.push({ seq: 6, type: "tool_result", at: 3, internal: true, tool: "ryn_update_plan", result: { accepted: true } });
  expect(agentTurnToolCount(task)).toBe(1);
});

it("trusts only explicit allowed actions with their control version", () => {
  const task = { allowed_actions: { approve: { enabled: false, reason: "stale", requires_control_revision: true }, cancel: { enabled: true, reason: null, requires_control_revision: false } }, control_revision: 2 };
  expect(canUseAgentAction(task, "approve", true)).toBe(false);
  expect(canUseAgentAction(task, "cancel")).toBe(true);
  expect(canUseAgentAction({ ...task, control_revision: undefined }, "cancel")).toBe(false);
  expect(canUseAgentAction(task, "resume", true)).toBe(false);
});

it("uses only the current streamed reply and replaces it when the complete assistant message arrives", () => {
  const task: Pick<AgentTask, "events"> = { events: [
    { seq: 1, type: "model_started", at: 1, step: 1 },
    { seq: 2, type: "text_delta", at: 1, step: 1, text: "Old reply" },
    { seq: 3, type: "assistant", at: 1, content: "Old reply" },
    { seq: 4, type: "model_started", at: 2, step: 2 },
    { seq: 5, type: "text_delta", at: 2, step: 2, text: "Checking " },
    { seq: 6, type: "text_delta", at: 2, step: 2, text: "the result" },
  ] };
  expect(pendingAgentText(task)).toBe("Checking the result");
  // Polling the same persisted events does not append another copy.
  expect(pendingAgentText(task)).toBe("Checking the result");
  task.events.push({ seq: 7, type: "assistant", at: 2, content: "Checking the result" });
  expect(pendingAgentText(task)).toBe("");
});

it("excludes proven non-execution receipts and recognizes late receipts from event order", () => {
  const rejected = { seq: 2, type: "tool_result", at: 1, result: { error: "agent_permission_denied", not_executed: true } };
  const returned = { seq: 4, type: "tool_result", at: 3, result: { sharing: true } };
  const events = [rejected, { seq: 3, type: "cancelled", at: 2 }, returned];
  expect(agentToolResultState(rejected)).toBe("not_executed");
  expect(agentToolResultState(returned)).toBe("returned");
  expect(agentToolResultState({ ...returned, result: { error: "read_failed", not_executed: false } })).toBe("error");
  expect(agentTurnToolCount({ events })).toBe(1);
  expect(isAgentLateToolResult(rejected, events)).toBe(false);
  expect(isAgentLateToolResult(returned, events)).toBe(true);
});

it("keeps the cached transcript when a poll reports the task unchanged", () => {
  const cached = { id: "t", revision: 4, status: "running", messages: [{ role: "user", content: "hi" }], events: [] } as unknown as AgentTask;
  const live = mergeUnchangedTask(cached, { id: "t", revision: 4, unchanged: true, run_state: "running", run_timing: { elapsed_seconds: 9 } as AgentTask["run_timing"] });
  expect(live.messages).toBe(cached.messages);
  expect(live.run_timing).toEqual({ elapsed_seconds: 9 });
  const newer = { ...cached, revision: 5 } as AgentTask;
  expect(mergeUnchangedTask(cached, newer)).toBe(newer);
  expect(mergeUnchangedTask(cached, { id: "t", revision: 3, unchanged: true })).toEqual({ id: "t", revision: 3, unchanged: true });
});

it("merges live text without a durable revision and clears it explicitly on completion", () => {
  const cached: AgentTask = { id: "live", revision: 4, title: "Inspect disk", model_id: "model", status: "running", created_at: 1, updated_at: 1,
    messages: [{ role: "user", content: "Inspect disk" }], limits: {}, pending_approval: null, steps: 1, result: "", error: null,
    events: [{ seq: 1, type: "model_started", at: 1, step: 1, context_epoch: 0 }] };
  const first = mergeUnchangedTask(cached, { id: "live", revision: 4, unchanged: true, streaming: { step: 1, context_epoch: 0, text: "我" } });
  const next = mergeUnchangedTask(first, { id: "live", revision: 4, unchanged: true, streaming: { step: 1, context_epoch: 0, text: "我先检查一下" } });
  expect(next.events).toBe(cached.events);
  expect(pendingAgentText(next)).toBe("我先检查一下");
  expect(pendingAgentText(mergeUnchangedTask(next, { id: "live", revision: 4, unchanged: true, streaming: null }))).toBe("");
});

it("never resurrects a live draft from an earlier model step or after the final assistant event", () => {
  const events = [
    { seq: 1, type: "model_started", at: 1, step: 2, context_epoch: 1 },
    { seq: 2, type: "text_delta", at: 1, text: "Current checkpoint" },
  ];
  expect(pendingAgentText({ events, streaming: { step: 1, context_epoch: 1, text: "Old draft" } })).toBe("Current checkpoint");
  expect(pendingAgentText({ events, streaming: { step: 2, context_epoch: 0, text: "Old model" } })).toBe("Current checkpoint");
  expect(pendingAgentText({ events: [...events, { seq: 3, type: "assistant", at: 2 }], streaming: { step: 2, context_epoch: 1, text: "Late draft" } })).toBe("");
});

it("prevents overlapping focus reads from rolling back live text or the clock at the same revision", () => {
  const current = { id: "t", revision: 4, streaming: { step: 1, context_epoch: 0, text: "Checking the whole disk" },
    run_timing: { elapsed_seconds: 4, measurement: "live" } } as unknown as AgentTask;
  const late = { ...current, streaming: { ...current.streaming!, text: "Checking" }, run_timing: { ...current.run_timing!, elapsed_seconds: 3 } };
  const merged = mergeAgentTaskSnapshot(current, late);
  expect(merged.streaming).toBe(current.streaming);
  expect(merged.run_timing?.elapsed_seconds).toBe(4);
  expect(mergeAgentTaskSnapshot(current, { ...late, streaming: null }).streaming).toBe(current.streaming);
  const completed = { ...late, revision: 5, streaming: null };
  expect(mergeAgentTaskSnapshot(current, completed)).toBe(completed);
  expect(mergeAgentTaskSnapshot(completed, current)).toBe(completed);
});
