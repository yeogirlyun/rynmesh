import { expect, it } from "vitest";
import { agentToolResultState, agentTurnToolCount, canUseAgentAction, canUseAgentModel, isAgentLateToolResult, pendingAgentText, type AgentTask } from "./rynAgent";

it("requires explicit verified tool support and current availability before offering a model", () => {
  const ready = { id: "model", name: "Model", source: "peer", available: true, agent_supported: true };
  expect(canUseAgentModel(ready)).toBe(true);
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
