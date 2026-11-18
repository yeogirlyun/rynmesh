import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { agentTaskCache } from "../domain/agentWorkspaceCache";
import { agentActionBody, rynAgent, type AgentTask } from "../domain/rynAgent";
import RynAgent from "./RynAgent";

vi.mock("../personal/model", async original => ({ ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }) }));

function setup(control = false) {
  sessionStorage.clear(); localStorage.clear(); agentTaskCache().clear();
  const approval = { id: "approval", call_id: "call", arguments_sha256: "sha", tool: "ryn_shell_execute",
    arguments: { command: "npm test -- --run", cwd: "D:/synthetic-project", shell: "powershell", timeout_seconds: 120 },
    remember_prefix_candidate: ["npm", "test"] };
  const task = { id: "stage", title: "Stage files", model_id: "model", status: "waiting_approval", revision: 1,
    created_at: 1, updated_at: 1, messages: [{ role: "user", content: "Run tests" }], events: [],
    pending_approval: approval, steps: 1, result: "", error: null, limits: {}, permission_mode: "confirm_changes", allowed_write_tools: [],
    ...(control ? { schema_version: 2, control_revision: 2, run_state: "waiting_approval", goal: { revision: 1, text: "Run tests", constraints: [] },
      allowed_actions: { approve: { enabled: true, requires_control_revision: true }, deny: { enabled: true }, information: { enabled: true } } } : {}) } as AgentTask;
  let settings = { enabled: true, model_id: "model", permission_mode: "confirm_changes", allowed_write_tools: [],
    shell_allow_prefixes: [["npm", "test"], ["git", "add"]], workspace_root: "D:/synthetic-project", desktop_access: false };
  const actions = vi.fn(), updates = vi.fn();
  server.use(
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
    http.get("*/api/local/agent/automations", () => HttpResponse.json({ automations: [], runs: [] })),
    http.get("*/api/local/agent/extensions", () => HttpResponse.json({ revision: 1, plugins: [], skills: [], mcp_servers: [] })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json(settings)),
    http.put("*/api/local/agent/settings", async ({ request }) => { const body = await request.json(); updates(body); settings = { ...settings, ...body as object }; return HttpResponse.json(settings); }),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [{ id: "model", name: "Model", available: true, agent_supported: true }] })),
    http.get("*/api/local/agent/tools", () => HttpResponse.json({ tools: [] })),
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [], create_scope_supported: false })),
    http.get("*/api/local/agent/extensions/model-availability", () => HttpResponse.json({ servers: [] })),
    http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks: [task] })),
    http.get("*/api/local/agent/tasks/stage", () => HttpResponse.json(task)),
    http.get("*/api/local/agent/tasks/stage/artifacts", () => HttpResponse.json({ artifacts: [] })),
    http.post("*/api/local/agent/tasks/stage/actions", async ({ request }) => { actions(await request.json()); return HttpResponse.json({ ...task, status: "queued", pending_approval: null }); }),
  );
  return { task, actions, updates };
}

it.each([false, true])("S04-I10 shell details and prefix approval send remember_prefix (control=%s)", async control => {
  const { actions } = setup(control);
  const user = userEvent.setup(); render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await user.click(await screen.findByRole("button", { name: "Stage files" }));
  const card = await screen.findByRole("region", { name: "Change awaiting approval" });
  expect(within(card).getByText("npm test -- --run")).toBeInTheDocument();
  expect(within(card).getByText("D:/synthetic-project")).toBeInTheDocument();
  expect(within(card).getByText("powershell")).toBeInTheDocument();
  expect(within(card).getByText("120s")).toBeInTheDocument();
  await user.click(within(card).getByRole("button", { name: "Allow similar commands in future: npm test" }));
  await waitFor(() => expect(actions).toHaveBeenCalledWith(expect.objectContaining({ action: "approve", remember_prefix: true })));
  expect(actions.mock.calls[0][0]).not.toHaveProperty("remember");
});

it("S04-I10 settings list permits deleting one precise prefix", async () => {
  const { updates } = setup(); const user = userEvent.setup();
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await user.click(await screen.findByRole("button", { name: "Agent settings" }));
  const dialog = screen.getByRole("dialog", { name: "Agent settings" });
  const group = within(dialog).getByRole("region", { name: "Allowed command prefixes" });
  expect(within(group).getByText("git add")).toBeInTheDocument();
  await user.click(within(group).getByRole("button", { name: "Remove prefix npm test" }));
  await user.click(within(dialog).getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect(updates).toHaveBeenCalledWith(expect.objectContaining({ shell_allow_prefixes: [["git", "add"]] })));
});

it("S04-I10 both action clients use the boolean field instead of broad tool remembrance", async () => {
  const { task, actions } = setup();
  expect(agentActionBody(task, "approve", "prefix")).toHaveProperty("remember_prefix", true);
  expect(agentActionBody(task, "approve", "prefix")).not.toHaveProperty("remember");
  await rynAgent.action(task, "approve", "prefix");
  expect(actions).toHaveBeenCalledWith(expect.objectContaining({ remember_prefix: true }));
});
