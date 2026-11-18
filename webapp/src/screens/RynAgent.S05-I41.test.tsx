import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { agentTaskCache } from "../domain/agentWorkspaceCache";
import RynAgent from "./RynAgent";

vi.mock("../personal/model", async original => ({ ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }) }));

it.each(["failed", "paused", "waiting_input"])("S05-I41 unbound %s task keeps workspace setup without an error event", async status => {
  sessionStorage.clear(); localStorage.clear(); agentTaskCache().clear();
  const task = { id: "legacy", title: "Unbound legacy task", model_id: "model", status, revision: 1,
    created_at: 1, updated_at: 1, messages: [{ role: "user", content: "Continue task" }], events: [],
    pending_approval: null, pending_input: null, steps: 1, result: "", error: "agent_workspace_changed_new_task_required",
    limits: {}, permission_mode: "workspace", allowed_write_tools: [], local_execution: { workspace_root: "", desktop_access: false } };
  server.use(
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
    http.get("*/api/local/agent/automations", () => HttpResponse.json({ automations: [], runs: [] })),
    http.get("*/api/local/agent/extensions", () => HttpResponse.json({ revision: 1, plugins: [], skills: [], mcp_servers: [] })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json({ enabled: true, model_id: "model", permission_mode: "workspace", allowed_write_tools: [], workspace_root: "" })),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [{ id: "model", name: "Model", available: true, agent_supported: true }] })),
    http.get("*/api/local/agent/tools", () => HttpResponse.json({ tools: [] })),
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [] })),
    http.get("*/api/local/agent/extensions/model-availability", () => HttpResponse.json({ servers: [] })),
    http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks: [task] })),
    http.get("*/api/local/agent/tasks/legacy", () => HttpResponse.json(task)),
    http.get("*/api/local/agent/tasks/legacy/artifacts", () => HttpResponse.json({ artifacts: [] })),
  );
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await userEvent.setup().click(await screen.findByRole("button", { name: "Unbound legacy task" }));
  expect(await screen.findByRole("button", { name: "Use folder and continue" })).toBeInTheDocument();
});
