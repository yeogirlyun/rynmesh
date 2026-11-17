import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { agentTaskCache } from "../domain/agentWorkspaceCache";
import type { AgentTask } from "../domain/rynAgent";
import RynAgent from "./RynAgent";
import i18n from "../i18n";

vi.mock("../personal/model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }),
}));

beforeEach(async () => { sessionStorage.clear(); localStorage.clear(); agentTaskCache().clear(); await i18n.changeLanguage("en"); });

function setup() {
  const open = { id: "question-1", kind: "information", prompt: "Which output format?", blocking: false,
    fields: [{ id: "format", label: "Format", type: "text", required: false }],
    goal_revision: 1, control_revision: 1, scope_revision: 1, business_revision: null, created_at: 1 };
  let task = { id: "task-1", title: "Working task", model_id: "local:test", status: "running", run_state: "running",
    revision: 1, control_revision: 1, created_at: 1, updated_at: 1, messages: [{ role: "user", content: "Inspect" }],
    events: [], pending_approval: null, pending_input: null, open_questions: [open], steps: 1, result: "", error: null,
    limits: {}, permission_mode: "automatic", allowed_actions: { answer: { enabled: true, reason: null, requires_control_revision: true } },
  } as unknown as AgentTask;
  const actions = vi.fn();
  server.use(
    http.get("*/api/local/agent/settings", () => HttpResponse.json({ enabled: true, model_id: "local:test", permission_mode: "automatic", allowed_write_tools: [] })),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [{ id: "local:test", name: "Test model", source: "local_api", available: true, agent_supported: true }] })),
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [], create_scope_supported: false })),
    http.get("*/api/local/agent/tools", () => HttpResponse.json({ tools: [] })),
    http.get("*/api/local/agent/automations", () => HttpResponse.json({ automations: [], runs: [] })),
    http.get("*/api/local/agent/extensions", () => HttpResponse.json({ skills: [], plugins: [], mcp_servers: [] })),
    http.get("*/api/local/agent/extensions/model-availability", () => HttpResponse.json({ servers: [] })),
    http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks: [task] })),
    http.get("*/api/local/agent/tasks/task-1", () => HttpResponse.json(task)),
    http.get("*/api/local/agent/tasks/task-1/artifacts", () => HttpResponse.json({ artifacts: [] })),
    http.post("*/api/local/agent/tasks/task-1/actions", async ({ request }) => {
      actions(await request.json()); task = { ...task, open_questions: [], revision: 2 } as AgentTask;
      return HttpResponse.json(task);
    }),
  );
  render(<MemoryRouter initialEntries={["/agent"]}><RynAgent /></MemoryRouter>);
  return { actions, user: userEvent.setup() };
}

it("T1/T2 displays the open question while running and submits its own id", async () => {
  const { actions, user } = setup();
  await user.click(await screen.findByRole("button", { name: "Working task" }));
  const card = await screen.findByRole("region", { name: "Ryn needs your input" });
  expect(within(card).getByText(/continues working/i)).toBeInTheDocument();
  await user.type(within(card).getByLabelText("Format"), "markdown");
  await user.click(within(card).getByRole("button", { name: "Send answer" }));
  await waitFor(() => expect(actions).toHaveBeenCalledWith(expect.objectContaining({
    action: "answer", pending_input_id: "question-1", answers: { format: "markdown" }, expected_goal_revision: 1,
  })));
  await waitFor(() => expect(screen.queryByText("Which output format?")).not.toBeInTheDocument());
});

it("T4 can dismiss an open question with an explicit empty answer", async () => {
  const { actions, user } = setup();
  await user.click(await screen.findByRole("button", { name: "Working task" }));
  const card = await screen.findByRole("region", { name: "Ryn needs your input" });
  await user.click(within(card).getByRole("button", { name: "Continue with an assumption" }));
  await waitFor(() => expect(actions).toHaveBeenCalledWith(expect.objectContaining({ pending_input_id: "question-1", answers: {} })));
});
