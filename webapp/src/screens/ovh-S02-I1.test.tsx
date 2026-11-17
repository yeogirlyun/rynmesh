import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { agentTaskCache } from "../domain/agentWorkspaceCache";
import type { AgentSettings, AgentTask } from "../domain/rynAgent";
import RynAgent from "./RynAgent";
import { agentTurnTiming } from "./rynAgentTiming";

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

it("S02-I1 hidden stop notes do not hide the resumed turn's timing", () => {
  const current = task("Running", "running", "read_only");
  current.messages.push({ role: "user", content: "Hidden", ui: { kind: "turn_aborted", hidden: true } });
  current.messages.push({ role: "user", content: "Continue" });
  current.events = [
    { seq: 1, type: "created", at: 1, run_elapsed_seconds: 0 },
    { seq: 2, type: "paused", at: 2, run_elapsed_seconds: 1 },
    { seq: 3, type: "message_added", at: 3, run_elapsed_seconds: 0 },
  ];
  current.run_timing = { elapsed_seconds: 3, recorded_seconds: 0, max_seconds: 600, measurement: "live" };
  expect(agentTurnTiming(current, 1)).toMatchObject({ seconds: 3, state: "running", live: true });
});

it("S02-I1 hides aborted-turn notes while preserving partial and ordinary messages", async () => {
  const current = task("Paused", "paused", "read_only");
  current.messages = [
    { role: "user", content: "VISIBLE_REQUEST" },
    { role: "assistant", content: "VISIBLE_PARTIAL" },
    { role: "user", content: "HIDDEN_ABORT_NOTE", ui: { kind: "turn_aborted", hidden: true } },
    { role: "assistant", content: "HIDDEN_OTHER_NOTE", ui: { kind: "partial_recovery", hidden: true } },
    { role: "user", content: "VISIBLE_FOLLOWUP", ui: { kind: "information", hidden: false } },
  ];
  backend([current]);
  const user = userEvent.setup(); render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await user.click(await screen.findByRole("button", { name: "Paused task" }));
  expect(await screen.findByText("VISIBLE_PARTIAL")).toBeVisible();
  expect(screen.getAllByText("VISIBLE_REQUEST").length).toBeGreaterThan(0);
  expect(screen.getAllByText("VISIBLE_FOLLOWUP").length).toBeGreaterThan(0);
  expect(screen.queryByText("HIDDEN_ABORT_NOTE")).not.toBeInTheDocument();
  expect(screen.queryByText("HIDDEN_OTHER_NOTE")).not.toBeInTheDocument();
  expect(screen.getByText("VISIBLE_FOLLOWUP").closest("[data-turn-user]")).toHaveAttribute("data-turn-user", "1");
});
