import { expect, it } from "vitest";
import { agentErrorActionLabel, agentErrorHelp, KNOWN_AGENT_ERROR_CODES } from "./rynAgentErrors";

it("explains every known error in both languages with at least one way forward", () => {
  expect(KNOWN_AGENT_ERROR_CODES.length).toBeGreaterThanOrEqual(20);
  for (const code of KNOWN_AGENT_ERROR_CODES) {
    const zh = agentErrorHelp(code, true), en = agentErrorHelp(code, false);
    expect(zh.text, code).toMatch(/[一-鿿]/);
    expect(en.text, code).toMatch(/[A-Za-z]/);
    expect(zh.actions.length, code).toBeGreaterThan(0);
    for (const action of zh.actions) expect(agentErrorActionLabel(action, true)).toBeTruthy();
  }
});

it("never leaves an unknown error without retry, model switch and a new conversation", () => {
  const help = agentErrorHelp("agent_something_new", true);
  expect(help.text).toContain("agent_something_new");
  expect(help.actions).toEqual(["retry", "switch_model", "new_task"]);
});

it("covers the errors that used to end a conversation", () => {
  for (const code of ["agent_image_input_unsupported", "agent_write_outcome_unknown", "agent_task_limit",
    "agent_operation_unresolved", "agent_context_limit", "agent_reconciliation_running", "agent_model_image_rejected"]) {
    expect(KNOWN_AGENT_ERROR_CODES).toContain(code);
  }
});
