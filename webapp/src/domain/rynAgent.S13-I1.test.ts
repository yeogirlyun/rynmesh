import { afterEach, expect, it, vi } from "vitest";
import { rynAgent, agentActionBody, type AgentTask } from "./rynAgent";

afterEach(() => { vi.unstubAllGlobals(); });
it("T4 the API and action body transmit explicit task remembrance only when requested", async () => {
  const task = { id: "task-a", control_revision: 3, allowed_actions: {}, pending_approval: { id: "approval-a" } } as AgentTask;
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => task });
  vi.stubGlobal("fetch", fetch);
  expect(agentActionBody(task, "approve", "task")).toMatchObject({ remember: "task", approval_id: "approval-a" });
  expect(agentActionBody(task, "approve")).not.toHaveProperty("remember");
  await rynAgent.action(task, "approve", "task");
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toMatchObject({ action: "approve", remember: "task", approval_id: "approval-a" });
});
