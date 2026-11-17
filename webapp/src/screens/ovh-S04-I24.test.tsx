import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { resetAgentWorkspaceMemory } from "../domain/agentWorkspaceCache";
import { type AgentTask } from "../domain/rynAgent";
import RynAgent from "./RynAgent";

vi.mock("../personal/model", async original => ({ ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }),
}));
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); resetAgentWorkspaceMemory(); });

function api(mode: string, tasks: AgentTask[] = []) {
  const updates = vi.fn();
  let settings = { enabled: true, model_id: "local:test", permission_mode: mode, desktop_access: false,
    allowed_write_tools: [], denied_tools: [], max_steps: 64, max_seconds: 900, max_context_chars: null, max_output_chars: 20000 };
  server.use(
    http.get("*/api/local/agent/settings", () => HttpResponse.json(settings)),
    http.put("*/api/local/agent/settings", async ({ request }) => {
      const body = await request.json(); updates(body); settings = { ...settings, ...body as object }; return HttpResponse.json(settings);
    }),
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [] })),
    http.get("*/api/local/agent/tools", () => HttpResponse.json({ tools: [] })),
    http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks })),
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [] })),
    http.get("*/api/local/agent/automations", () => HttpResponse.json({ automations: [], runs: [] })),
    http.get("*/api/local/agent/extensions", () => HttpResponse.json({ skills: [], plugins: [], mcp_servers: [] })),
    http.get("*/api/local/agent/extensions/model-availability", () => HttpResponse.json({ servers: [] })),
    http.get("*/api/local/agent/tasks/:id", () => HttpResponse.json(tasks[0])),
    http.get("*/api/local/agent/tasks/:id/artifacts", () => HttpResponse.json({ artifacts: [] })),
  );
  return updates;
}
async function openSettings() {
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  const user = userEvent.setup();
  await user.click(await screen.findByLabelText("Chat actions"));
  const open = await screen.findByRole("button", { name: "Agent settings" });
  await waitFor(() => expect(open).toBeEnabled());
  await user.click(open);
  return { user, dialog: await screen.findByRole("dialog", { name: "Agent settings" }) };
}

it("S04-I24 automatic desktop follows mode while saving the original false flag", async () => {
  const updates = api("automatic");
  const { user, dialog } = await openSettings();
  const desktop = within(dialog).getByRole("checkbox", { name: "Allow computer and browser operations" });
  expect(desktop).toBeChecked();
  expect(desktop).toBeDisabled();
  expect(within(dialog).getByText("Enabled with Automatic")).toBeInTheDocument();
  expect(within(dialog).getByText(/includes document component installs, app operations and data sent to the model, browser and desktop control/)).toBeInTheDocument();
  await user.click(within(dialog).getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect(updates).toHaveBeenCalledWith(expect.objectContaining({ desktop_access: false })));
});
it("S04-I24 workspace uses the stored desktop flag", async () => {
  api("workspace");
  const { dialog } = await openSettings();
  const desktop = within(dialog).getByRole("checkbox", { name: "Allow computer and browser operations" });
  expect(desktop).not.toBeChecked();
  expect(desktop).toBeEnabled();
});
it("S04-I24 activity explains an explicit approval bypass", async () => {
  const row = { id: "task-1", title: "Install component", model_id: "local:test", permission_mode: "automatic", status: "completed",
    revision: 1, created_at: 1, updated_at: 1, messages: [{ role: "user", content: "Install component" }], events: [
      { seq: 1, type: "automatically_authorized", at: 1, tool: "ryn_install_craft_automation", explicit_approval_bypassed: true }],
    pending_approval: null, steps: 1, result: "Installed", error: null, limits: {}, allowed_write_tools: [] } as AgentTask;
  api("automatic", [row]);
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Install component" }));
  await user.click(await screen.findByText("Execution details"));
  expect(await screen.findByText("Approval bypassed in Automatic", { exact: false })).toBeInTheDocument();
});
