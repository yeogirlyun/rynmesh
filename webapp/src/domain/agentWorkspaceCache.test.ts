import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { agentTaskCache, readAgentWorkspace, writeAgentWorkspace } from "./agentWorkspaceCache";
import { nodeControlUrl } from "./nodeUrl";
import type { AgentModel, AgentSettings } from "./rynAgent";

const settings: AgentSettings = {
  enabled: true, model_id: "saved-model", permission_mode: "confirm_changes", allowed_write_tools: [],
  max_steps: 16, max_seconds: 180, max_context_chars: 60000, max_output_chars: 20000,
};
const model: AgentModel = {
  id: "saved-model", name: "Saved model", source: "local_api", available: true, agent_supported: true,
  tool_support: "supported", destination_revision: "destination-1",
};
beforeEach(() => { localStorage.clear(); });
afterEach(() => { vi.unstubAllEnvs(); vi.restoreAllMocks(); });

it("persists the configured model while omitting extra server and credential fields", () => {
  writeAgentWorkspace({ ...settings, secret: "private credential" } as AgentSettings,
    [{ ...model, expected_model_binding: { authorization: "private binding" } } as AgentModel], model.id);
  expect(readAgentWorkspace()).toEqual({ settings, models: [model], model: model.id });
  const stored = localStorage.getItem(localStorage.key(0)!)!;
  expect(stored).not.toContain("private credential");
  expect(stored).not.toContain("private binding");
});

it("restores autonomous execution without discarding the cached model", () => {
  const autonomous: AgentSettings = { ...settings, permission_mode: "workspace" };
  writeAgentWorkspace(autonomous, [model], model.id);
  expect(readAgentWorkspace()).toEqual({ settings: autonomous, models: [model], model: model.id });
});

it("isolates configuration and conversation memory by node endpoint", () => {
  vi.stubEnv("VITE_RYN_NODE_BASE_URL", "https://node-a/api/local");
  writeAgentWorkspace(settings, [model], model.id);
  const firstTasks = agentTaskCache();
  vi.stubEnv("VITE_RYN_NODE_BASE_URL", "https://node-b/api/local");
  expect(readAgentWorkspace()).toEqual({ settings: null, models: [], model: "" });
  expect(agentTaskCache()).not.toBe(firstTasks);
  vi.stubEnv("VITE_RYN_NODE_BASE_URL", "https://node-a/api/local");
  expect(readAgentWorkspace().model).toBe(model.id);
  expect(agentTaskCache()).toBe(firstTasks);
});

it.each(["not JSON", JSON.stringify({ settings, models: [null], model: model.id }),
  JSON.stringify({ settings: { ...settings, enabled: "true" }, models: [model], model: model.id }),
])("ignores corrupt cache data without blocking discovery", value => {
  localStorage.setItem(`ryn.agent.workspace-v1:${nodeControlUrl("/agent")}`, value);
  expect(readAgentWorkspace()).toEqual({ settings: null, models: [], model: "" });
});

it("tolerates storage read and write failures", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("Storage disabled"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Storage full"); });
  expect(readAgentWorkspace()).toEqual({ settings: null, models: [], model: "" });
  expect(() => writeAgentWorkspace(settings, [model], model.id)).not.toThrow();
});
