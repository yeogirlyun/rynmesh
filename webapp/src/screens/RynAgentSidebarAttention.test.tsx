import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { agentTaskCache } from "../domain/agentWorkspaceCache";
import type { AgentTask } from "../domain/rynAgent";
import RynAgent from "./RynAgent";

vi.mock("../personal/model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }),
}));

function task(overrides: Partial<AgentTask> = {}): AgentTask {
  return { id: "background-task", title: "Background conversation", model_id: "model-a", status: "running", run_state: "running", revision: 1, control_revision: 1, completion_event_seq: 0,
    created_at: 1, updated_at: 1, messages: [{ role: "user", content: "A background request" }], events: [], pending_approval: null, steps: 1, result: "", error: null, limits: {}, permission_mode: "confirm_changes", allowed_write_tools: [], ...overrides };
}

function api(initialTasks: AgentTask[]) {
  let tasks = initialTasks;
  server.use(
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json({ enabled: true, model_id: "model-a", permission_mode: "confirm_changes", allowed_write_tools: [], max_steps: 16, max_seconds: 180, max_context_chars: 60000, max_output_chars: 20000 })),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [{ id: "model-a", name: "Available model", source: "local_api", available: true, agent_supported: true }] })),
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [], create_scope_supported: false })),
    http.get("*/api/local/agent/skills", () => HttpResponse.json({ skills: [] })),
    http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks })),
    http.get("*/api/local/agent/tasks/:id", ({ params }) => HttpResponse.json(tasks.find(item => item.id === params.id))),
    http.get("*/api/local/agent/tasks/:id/artifacts", () => HttpResponse.json({ artifacts: [] })),
  );
  return { setTasks: (next: AgentTask[]) => { tasks = next; } };
}

async function open() {
  render(<MemoryRouter initialEntries={["/agent"]}><RynAgent /></MemoryRouter>);
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("model-a"));
  return within(screen.getByRole("complementary", { name: "Task list" }));
}

beforeEach(() => {
  sessionStorage.clear(); localStorage.clear(); agentTaskCache().clear();
  sessionStorage.setItem("ryn.agent.project", "__general__");
  vi.spyOn(document, "hasFocus").mockReturnValue(true);
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
});
afterEach(() => { vi.restoreAllMocks(); });

it("shows a background completion after focus refresh and reads it only after its detail loads", async () => {
  const original = task(), backend = api([original]);
  const sidebar = await open(), user = userEvent.setup();
  const row = await sidebar.findByRole("button", { name: original.title });
  expect(within(row).queryByRole("img", { name: "Unread completed reply" })).not.toBeInTheDocument();
  const finished = task({ status: "completed", run_state: "ended", revision: 5, control_revision: 3, completion_event_seq: 24, messages: [{ role: "assistant", content: "The background reply has arrived." }] });
  backend.setTasks([finished]);
  act(() => window.dispatchEvent(new Event("focus")));
  await within(row).findByRole("img", { name: "Unread completed reply" });
  expect(within(row).queryByText("Turn finished")).not.toBeInTheDocument();

  let completeDetail!: () => void;
  const detailGate = new Promise<void>(resolve => { completeDetail = resolve; });
  const detailRequested = vi.fn();
  server.use(http.get("*/api/local/agent/tasks/:id", async () => {
    detailRequested(); await detailGate; return HttpResponse.json(finished);
  }));
  await user.click(row);
  await waitFor(() => expect(detailRequested).toHaveBeenCalled());
  expect(within(row).getByRole("img", { name: "Unread completed reply" })).toBeInTheDocument();
  await act(async () => { completeDetail(); });
  await screen.findByText("The background reply has arrived.");
  await waitFor(() => expect(within(row).queryByRole("img", { name: "Unread completed reply" })).not.toBeInTheDocument());
});

it("keeps confirmation and answer badges while their conversations are open and read", async () => {
  const approval = task({ id: "approval-task", title: "Confirm a change", status: "waiting_approval", run_state: "waiting_approval", pending_approval: { id: "approval-a", tool: "write_file", arguments: { path: "note.txt", content: "A note" }, call_id: "call-a", arguments_sha256: "abc" } });
  const input = task({ id: "input-task", title: "Answer a question", status: "waiting_input", run_state: "waiting_input", pending_input: { id: "input-a", kind: "information", prompt: "Which folder?", fields: [], goal_revision: 1, control_revision: 1, scope_revision: 1, business_revision: null, created_at: 1 } });
  api([approval, input]);
  const sidebar = await open(), user = userEvent.setup();
  const approvalRow = await sidebar.findByRole("button", { name: approval.title });
  const inputRow = sidebar.getByRole("button", { name: input.title });
  expect(within(approvalRow).getByText("Needs confirmation")).toBeVisible();
  expect(within(inputRow).getByText("Awaiting your answer")).toBeVisible();
  await user.click(approvalRow);
  await waitFor(() => expect(approvalRow).toHaveAttribute("aria-pressed", "true"));
  expect(within(approvalRow).getByText("Needs confirmation")).toBeVisible();
  await user.click(inputRow);
  await screen.findByText("Which folder?");
  expect(within(inputRow).getByText("Awaiting your answer")).toBeVisible();
});
