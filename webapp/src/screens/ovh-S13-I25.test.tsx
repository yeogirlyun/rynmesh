import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { agentTaskCache } from "../domain/agentWorkspaceCache";
import type { AgentSettings, AgentTask } from "../domain/rynAgent";
import RynAgent from "./RynAgent";

vi.mock("../personal/model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }),
}));

function task(id: string, status: "running" | "paused" | "completed", mode: AgentSettings["permission_mode"]): AgentTask {
  return { id, title: `${id} task`, model_id: "local:test", status, run_state: status === "completed" ? "ended" : status,
    revision: 1, control_revision: 1, created_at: 1, updated_at: 1, messages: [{ role: "user", content: "Do work" }],
    events: [], pending_approval: null, steps: 0, result: "", error: null, limits: {}, permission_mode: mode,
    allowed_actions: Object.fromEntries(["pause", "information", "set_permission"].map(action => [action, {
      enabled: action !== "set_permission" || status !== "running", reason: null, requires_control_revision: true,
    }])),
  };
}

function backend(initial: AgentTask[]) {
  let tasks = initial;
  let settings: AgentSettings = { enabled: true, model_id: "local:test", permission_mode: "automatic", allowed_write_tools: [], max_steps: 16, max_seconds: 180, max_context_chars: 60000, max_output_chars: 20000 };
  const updates = vi.fn();
  const actions = vi.fn();
  server.use(
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json(settings)),
    http.put("*/api/local/agent/settings", async ({ request }) => {
      const update = await request.json(); updates(update); settings = { ...settings, ...update as object };
      return HttpResponse.json(settings);
    }),
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [], create_scope_supported: false })),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [{ id: "local:test", name: "Local model", source: "local_api", available: true, agent_supported: true }] })),
    http.get("*/api/local/agent/tools", () => HttpResponse.json({ tools: [] })),
    http.get("*/api/local/agent/automations", () => HttpResponse.json({ automations: [], runs: [] })),
    http.get("*/api/local/agent/extensions", () => HttpResponse.json({ skills: [], plugins: [], mcp_servers: [] })),
    http.get("*/api/local/agent/extensions/model-availability", () => HttpResponse.json({ servers: [] })),
    http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks })),
    http.get("*/api/local/agent/tasks/:id", ({ params }) => HttpResponse.json(tasks.find(item => item.id === params.id))),
    http.get("*/api/local/agent/tasks/:id/artifacts", () => HttpResponse.json({ artifacts: [] })),
    http.post("*/api/local/agent/tasks/:id/actions", async ({ params, request }) => {
      const body = await request.json() as { action: string; permission_mode?: AgentSettings["permission_mode"]; expected_control_revision?: number };
      const current = tasks.find(item => item.id === params.id)!;
      actions(current.id, body);
      if (body.action === "set_permission" && current.control_revision !== body.expected_control_revision) return HttpResponse.json({ detail: "agent_control_revision_conflict" }, { status: 409 });
      const next = { ...current, revision: current.revision + 1, control_revision: current.control_revision! + 1 };
      if (body.action === "pause") Object.assign(next, task(current.id, "paused", current.permission_mode!));
      next.revision = current.revision + 1; next.control_revision = current.control_revision! + 1;
      if (body.action === "set_permission") next.permission_mode = body.permission_mode;
      tasks = tasks.map(item => item.id === current.id ? next : item);
      return HttpResponse.json(next);
    }),
  );
  return { updates, actions };
}

beforeEach(() => { sessionStorage.clear(); localStorage.clear(); agentTaskCache().clear(); vi.restoreAllMocks(); });

it("S13-I25 changes a paused task through set_permission and locks a running task", async () => {
  const api = backend([task("Paused", "paused", "read_only"), task("Running", "running", "automatic")]);
  const user = userEvent.setup(); render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await user.click(await screen.findByRole("button", { name: "Paused task" }));
  await user.click(screen.getByLabelText("Tool permissions", { selector: "summary" }));
  expect(screen.getByRole("menuitemradio", { name: "Work autonomously" })).toBeEnabled();
  await user.click(screen.getByRole("menuitemradio", { name: "Work autonomously" }));
  await waitFor(() => expect(api.actions).toHaveBeenCalledWith("Paused", expect.objectContaining({ action: "set_permission", permission_mode: "workspace", expected_control_revision: 1 })));
  expect(api.updates).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Running task" }));
  await user.click(screen.getByLabelText("Tool permissions", { selector: "summary" }));
  expect(screen.getByRole("menuitemradio", { name: "Read only" })).toBeDisabled();
});

it.each([false, true])("S13-I25 read-only default asks whether to synchronize live tasks: %s", async synchronize => {
  const api = backend([task("Running", "running", "automatic"), task("Paused", "paused", "workspace"), task("Ended", "completed", "automatic")]);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(synchronize);
  const user = userEvent.setup(); render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await user.click(await screen.findByRole("button", { name: "New task" }));
  await screen.findByRole("heading", { name: "What would you like me to do?" });
  await user.click(screen.getByLabelText("Tool permissions", { selector: "summary" }));
  await user.click(screen.getByRole("menuitemradio", { name: "Read only" }));
  await waitFor(() => expect(api.updates).toHaveBeenCalledWith({ permission_mode: "read_only" }));
  expect(confirm).toHaveBeenCalledTimes(1);
  if (synchronize) {
    await waitFor(() => expect(api.actions).toHaveBeenCalledWith("Paused", expect.objectContaining({ action: "set_permission", permission_mode: "read_only" })));
    expect(api.actions).toHaveBeenCalledWith("Running", expect.objectContaining({ action: "pause" }));
    expect(api.actions).toHaveBeenCalledWith("Running", expect.objectContaining({ action: "set_permission", permission_mode: "read_only", expected_control_revision: 2 }));
    expect(api.actions.mock.calls.some(([id]) => id === "Ended")).toBe(false);
  } else {
    expect(api.actions).not.toHaveBeenCalled();
  }
});
