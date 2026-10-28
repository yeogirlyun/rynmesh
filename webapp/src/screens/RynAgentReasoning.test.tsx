import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { agentTaskCache } from "../domain/agentWorkspaceCache";
import type { AgentModel, AgentSettings, AgentTask } from "../domain/rynAgent";
import RynAgent from "./RynAgent";

vi.mock("../personal/model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }),
}));

const cli: AgentModel = {
  id: "local/codex-cli/model/gpt-6.1-sol", name: "GPT-6.1 Sol", source: "local_service", source_name: "Codex",
  cli_source: "codex_cli", cli_model: "gpt-6.1-sol", service_id: "codex-cli", available: true,
  agent_supported: true, tool_support: "supported", supported_reasoning_efforts: ["low", "medium", "high"], default_reasoning_effort: "low",
};
const apiModel: AgentModel = { id: "api-model", name: "API model", source: "local_api", available: true, agent_supported: true, tool_support: "supported" };

function task(overrides: Partial<AgentTask> = {}): AgentTask {
  return {
    id: "reasoning-chat", title: "Existing reasoning chat", model_id: cli.id, reasoning_effort: "high",
    status: "running", run_state: "running", schema_version: 2, revision: 1000, control_revision: 7, event_cursor: 1000,
    goal: { revision: 2, kind: "action", original_message_id: "owner-1", text: "Inspect this project", constraints: [] },
    allowed_actions: Object.fromEntries(["pause", "cancel", "steer", "information"].map(action => [action, { enabled: true, reason: null, requires_control_revision: true }])),
    created_at: 1, updated_at: 1, messages: [{ role: "user", content: "Inspect this project" }], events: [],
    pending_approval: null, steps: 1, result: "", error: null, limits: {}, permission_mode: "confirm_changes", allowed_write_tools: [],
    ...overrides,
  };
}

function backend(initialTasks: AgentTask[] = []) {
  let tasks = initialTasks;
  const creates = vi.fn(), messages = vi.fn();
  const settings: AgentSettings = { enabled: true, model_id: cli.id, permission_mode: "confirm_changes", allowed_write_tools: [], max_steps: 16, max_seconds: 180, max_context_chars: 60000, max_output_chars: 20000 };
  server.use(
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json(settings)),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [cli, apiModel] })),
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [], create_scope_supported: false })),
    http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks })),
    http.get("*/api/local/agent/tasks/:id", ({ params }) => HttpResponse.json(tasks.find(item => item.id === params.id))),
    http.get("*/api/local/agent/tasks/:id/artifacts", () => HttpResponse.json({ artifacts: [] })),
    http.post("*/api/local/agent/tasks", async ({ request }) => {
      const body = await request.json() as { message: string; model_id: string; client_request_id: string; reasoning_effort?: string };
      creates(body);
      const accepted = task({ model_id: body.model_id, reasoning_effort: body.reasoning_effort, client_request_id: body.client_request_id, messages: [{ role: "user", content: body.message }] });
      tasks = [accepted];
      return HttpResponse.json(accepted);
    }),
    http.post("*/api/local/agent/tasks/:id/messages", async ({ params, request }) => {
      const body = await request.json() as { message: string; intent: "steer" | "information"; reasoning_effort?: string };
      messages({ id: params.id, body });
      const current = tasks.find(item => item.id === params.id)!;
      const accepted = { ...current, reasoning_effort: body.reasoning_effort, revision: current.revision + 1, control_revision: (current.control_revision || 0) + 1,
        messages: [...current.messages, { role: "user", content: body.message, ui: { kind: body.intent, text: body.message } }],
      };
      tasks = [accepted];
      return HttpResponse.json(accepted);
    }),
  );
  return { creates, messages, setTasks: (value: AgentTask[]) => { tasks = value; } };
}

async function mount() {
  const view = render(<MemoryRouter initialEntries={["/agent"]}><RynAgent /></MemoryRouter>);
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue(cli.id));
  return view;
}

beforeEach(() => {
  sessionStorage.clear(); localStorage.clear(); agentTaskCache().clear();
});

it("shows the supported model's actual low default and submits it when creating a task", async () => {
  const api = backend(), user = userEvent.setup();
  await mount();
  const picker = screen.getByRole("combobox", { name: "Model" });
  expect(picker).toHaveTextContent("GPT-6.1 SolLow");
  await user.click(picker);
  const efforts = screen.getByRole("radiogroup", { name: "Reasoning effort" });
  expect(within(efforts).getByRole("radio", { name: "Low" })).toBeChecked();
  expect(within(efforts).getAllByRole("radio")).toHaveLength(3);
  await user.keyboard("{Escape}");
  await user.type(screen.getByRole("textbox"), "Inspect the model default");
  await user.click(screen.getByRole("button", { name: "Send task" }));
  await waitFor(() => expect(api.creates).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ message: "Inspect the model default", model_id: cli.id, reasoning_effort: "low" })));
});

it("sends a newly selected high effort in the real create request", async () => {
  const api = backend(), user = userEvent.setup();
  await mount();
  await user.click(screen.getByRole("combobox", { name: "Model" }));
  await user.click(screen.getByRole("radio", { name: "High" }));
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("GPT-6.1 SolHigh");
  await user.type(screen.getByRole("textbox"), "Think through this change");
  await user.click(screen.getByRole("button", { name: "Send task" }));
  await waitFor(() => expect(api.creates).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ model_id: cli.id, reasoning_effort: "high" })));
});

it("sends the highest reported reasoning level without reducing the model's choices", async () => {
  const api = backend(), user = userEvent.setup();
  server.use(http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [{ ...cli, supported_reasoning_efforts: ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"] }] })));
  await mount();
  await user.click(screen.getByRole("combobox", { name: "Model" }));
  await user.click(screen.getByRole("radio", { name: "Ultra" }));
  await user.type(screen.getByRole("textbox"), "Use the selected reasoning level");
  await user.click(screen.getByRole("button", { name: "Send task" }));
  await waitFor(() => expect(api.creates).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ model_id: cli.id, reasoning_effort: "ultra" })));
});

it("retains the existing conversation's effort in its follow-up request", async () => {
  const existing = task(), api = backend([existing]), user = userEvent.setup();
  await mount();
  await user.click(await screen.findByRole("button", { name: /^Existing reasoning chat/ }));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("GPT-6.1 SolHigh"));
  await user.type(screen.getByRole("textbox"), "Also inspect the migration");
  await user.click(screen.getByRole("button", { name: "Send requirements update" }));
  await waitFor(() => expect(api.messages).toHaveBeenCalledExactlyOnceWith({ id: existing.id, body: expect.objectContaining({ message: "Also inspect the migration", intent: "steer", expected_goal_revision: 2, expected_control_revision: 7, client_request_id: expect.any(String), reasoning_effort: "high" }) }));
  expect(api.creates).not.toHaveBeenCalled();
});

it("does not label or submit an invented effort after switching to an API model without support", async () => {
  const api = backend(), user = userEvent.setup();
  const view = await mount();
  await user.click(screen.getByRole("combobox", { name: "Model" }));
  await user.click(screen.getByRole("radio", { name: "High" }));
  await user.click(screen.getByRole("combobox", { name: "Model" }));
  await user.click(screen.getByRole("option", { name: "API model" }));
  const picker = screen.getByRole("combobox", { name: "Model" });
  expect(picker).toHaveTextContent("API model");
  expect(within(picker).queryByText("High")).not.toBeInTheDocument();
  expect(view.container.querySelector(".ra-model-effort")).not.toBeInTheDocument();
  await user.click(picker);
  expect(screen.queryByRole("radiogroup", { name: "Reasoning effort" })).not.toBeInTheDocument();
  await user.keyboard("{Escape}");
  await user.type(screen.getByRole("textbox"), "Use the API model");
  await user.click(screen.getByRole("button", { name: "Send task" }));
  await waitFor(() => expect(api.creates).toHaveBeenCalledOnce());
  expect(api.creates.mock.calls[0][0]).toEqual(expect.objectContaining({ model_id: apiModel.id, message: "Use the API model" }));
  expect(api.creates.mock.calls[0][0]).not.toHaveProperty("reasoning_effort");
});

it("resets stale effort when the same conversation changes from CLI to API and back", async () => {
  const existing = task(), api = backend([existing]), user = userEvent.setup();
  await mount();
  await user.click(await screen.findByRole("button", { name: /^Existing reasoning chat/ }));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("GPT-6.1 SolHigh"));
  api.setTasks([{ ...existing, model_id: apiModel.id, reasoning_effort: undefined, revision: 1001 }]);
  window.dispatchEvent(new Event("focus"));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("API model"));
  expect(screen.getByRole("combobox", { name: "Model" })).not.toHaveTextContent("High");
  api.setTasks([{ ...existing, model_id: cli.id, reasoning_effort: undefined, revision: 1002 }]);
  window.dispatchEvent(new Event("focus"));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("GPT-6.1 SolLow"));
  await user.type(screen.getByRole("textbox"), "Continue with the current model default");
  await user.click(screen.getByRole("button", { name: "Send requirements update" }));
  await waitFor(() => expect(api.messages).toHaveBeenCalledExactlyOnceWith({ id: existing.id, body: expect.objectContaining({ message: "Continue with the current model default", reasoning_effort: "low" }) }));
});
