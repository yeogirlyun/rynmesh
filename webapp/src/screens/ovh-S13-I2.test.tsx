import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { resetAgentWorkspaceMemory } from "../domain/agentWorkspaceCache";
import { agentActionBody, type AgentTask } from "../domain/rynAgent";
import RynAgent from "./RynAgent";

vi.mock("../personal/model", async original => ({ ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }),
}));
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); resetAgentWorkspaceMemory(); });

it("S13-I2 settings show both owner lists and remove rules through the real save request", async () => {
  const updates = vi.fn();
  let settings = { enabled: true, model_id: "local:test", permission_mode: "confirm_changes",
    allowed_write_tools: ["ryn_file_write"], denied_tools: ['mcp:["company","publish"]'],
    max_steps: 64, max_seconds: 900, max_context_chars: null, max_output_chars: 20000 };
  server.use(
    http.get("*/api/local/agent/settings", () => HttpResponse.json(settings)),
    http.put("*/api/local/agent/settings", async ({ request }) => {
      const body = await request.json(); updates(body); settings = { ...settings, ...body as object }; return HttpResponse.json(settings);
    }),
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [] })),
    http.get("*/api/local/agent/tools", () => HttpResponse.json({ tools: [] })),
    http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks: [] })),
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [] })),
    http.get("*/api/local/agent/automations", () => HttpResponse.json({ automations: [], runs: [] })),
    http.get("*/api/local/agent/extensions", () => HttpResponse.json({ skills: [], plugins: [], mcp_servers: [] })),
    http.get("*/api/local/agent/extensions/model-availability", () => HttpResponse.json({ servers: [] })),
  );
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  const user = userEvent.setup();
  await user.click(await screen.findByLabelText("Chat actions"));
  const open = await screen.findByRole("button", { name: "Agent settings" });
  await waitFor(() => expect(open).toBeEnabled());
  await user.click(open);
  const dialog = await screen.findByRole("dialog", { name: "Agent settings" });
  expect(within(dialog).getByText("Always allowed")).toBeInTheDocument();
  expect(within(dialog).getByText("Denied tools")).toBeInTheDocument();
  await user.click(within(dialog).getByRole("button", { name: "Remove ryn_file_write" }));
  await user.click(within(dialog).getByRole("button", { name: "Remove company / publish" }));
  await user.click(within(dialog).getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect(updates).toHaveBeenCalledWith(expect.objectContaining({ allowed_write_tools: [], denied_tools: [] })));
});

it("S13-I2 permanent approval keeps the always value in the real control body", () => {
  const pending = { id: "task", revision: 3, control_revision: 3, pending_approval: { id: "approval" } } as AgentTask;
  expect(agentActionBody(pending, "approve", "always")).toMatchObject({ action: "approve", remember: "always", approval_id: "approval" });
});
