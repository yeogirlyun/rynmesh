import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { clearAgentRecords, flushAgentRecords } from "../domain/agentTaskStore";
import { rememberAgentTask, rememberAgentTaskList, resetAgentWorkspaceMemory } from "../domain/agentWorkspaceCache";
import type { AgentTask } from "../domain/rynAgent";
import RynAgent from "./RynAgent";

vi.mock("../personal/model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }),
}));

const flag = globalThis as { __rynAgentRecordsDisabled?: boolean };

function task(overrides: Partial<AgentTask> = {}): AgentTask {
  return { id: "saved-task", title: "Quarterly close", model_id: "model-a", status: "completed", run_state: "ended", revision: 4, control_revision: 2,
    created_at: 1, updated_at: 4, messages: [{ role: "user", content: "Summarise the close" }, { role: "assistant", content: "The close is balanced." }],
    events: [], pending_approval: null, steps: 1, result: "", error: null, limits: {}, permission_mode: "confirm_changes", allowed_write_tools: [], ...overrides };
}

function slowNode(answers: { tasks: () => Promise<Response>; detail: () => Promise<Response> }) {
  server.use(
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json({ enabled: true, model_id: "model-a", permission_mode: "confirm_changes", allowed_write_tools: [], max_steps: 16, max_seconds: 180, max_context_chars: 60000, max_output_chars: 20000 })),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [{ id: "model-a", name: "Available model", source: "local_api", available: true, agent_supported: true }] })),
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [], create_scope_supported: false })),
    http.get("*/api/local/agent/skills", () => HttpResponse.json({ skills: [] })),
    http.get("*/api/local/agent/automations*", () => HttpResponse.json({ automations: [], runs: [] })),
    http.get("*/api/local/agent/tasks", answers.tasks),
    http.get("*/api/local/agent/tasks/:id", answers.detail),
    http.get("*/api/local/agent/tasks/:id/artifacts", () => HttpResponse.json({ artifacts: [] })),
  );
}

beforeEach(async () => {
  sessionStorage.clear(); localStorage.clear();
  flag.__rynAgentRecordsDisabled = false;
  await clearAgentRecords();
  sessionStorage.setItem("ryn.agent.project", "__general__");
  vi.spyOn(document, "hasFocus").mockReturnValue(true);
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
});
afterEach(async () => {
  vi.restoreAllMocks();
  await clearAgentRecords();
  flag.__rynAgentRecordsDisabled = true;
});

async function saveFromEarlierRun(saved: AgentTask) {
  rememberAgentTaskList([saved, task({ id: "other-task", title: "Supplier invoices", updated_at: 2 })]);
  rememberAgentTask(saved);
  await flushAgentRecords();
  // A restart: nothing of the earlier run is left in memory.
  resetAgentWorkspaceMemory();
}

it("shows the saved list and the open conversation before the node answers", async () => {
  await saveFromEarlierRun(task());
  sessionStorage.setItem("ryn.agent.selected", "saved-task");
  const never = () => new Promise<Response>(() => undefined);
  slowNode({ tasks: never, detail: never });

  render(<MemoryRouter initialEntries={["/agent"]}><RynAgent /></MemoryRouter>);
  const sidebar = within(await screen.findByRole("complementary", { name: "Task list" }));
  expect(await sidebar.findByRole("button", { name: "Quarterly close" })).toBeInTheDocument();
  expect(sidebar.getByRole("button", { name: "Supplier invoices" })).toBeInTheDocument();
  expect(await screen.findByText("The close is balanced.")).toBeInTheDocument();
});

it("replaces the saved copy with the node's newer conversation", async () => {
  await saveFromEarlierRun(task());
  sessionStorage.setItem("ryn.agent.selected", "saved-task");
  const fresh = task({ revision: 9, updated_at: 9, messages: [{ role: "assistant", content: "Updated after the restart." }] });
  slowNode({ tasks: async () => HttpResponse.json({ tasks: [fresh] }), detail: async () => HttpResponse.json(fresh) });

  render(<MemoryRouter initialEntries={["/agent"]}><RynAgent /></MemoryRouter>);
  expect(await screen.findByText("Updated after the restart.")).toBeInTheDocument();
  const sidebar = within(screen.getByRole("complementary", { name: "Task list" }));
  // The node no longer lists the other task, so the saved row goes away.
  await waitFor(() => expect(sidebar.queryByRole("button", { name: "Supplier invoices" })).not.toBeInTheDocument());
  expect(screen.queryByText("The close is balanced.")).not.toBeInTheDocument();
});
