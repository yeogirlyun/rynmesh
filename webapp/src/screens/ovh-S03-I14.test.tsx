import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { resetAgentWorkspaceMemory } from "../domain/agentWorkspaceCache";
import { rynAgent, type AgentTask } from "../domain/rynAgent";
import RynAgent from "./RynAgent";

vi.mock("../personal/model", async original => ({ ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }),
}));
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); resetAgentWorkspaceMemory(); });

import { extractCodexCompactions, RynContextCompactionRow } from "./RynAgentProgress";
function api(tasks: AgentTask[]) {
  const updates = vi.fn();
  let settings = { enabled: true, model_id: "local:test", permission_mode: "workspace", desktop_access: false,
    allowed_write_tools: [], denied_tools: [], max_steps: 64, max_seconds: 900, max_context_chars: null, max_output_chars: 20000 };
  server.use(
    http.post("*/api/local/agent/tasks/:id/actions", async ({ request }) => { const body = await request.json(); updates(body); return HttpResponse.json({ ...tasks[0], status: "queued", revision: 4, control_revision: 4 }); }),
    http.post("*/api/local/agent/tasks/:id/compact", async ({ request }) => { const body = await request.json(); updates(body); return HttpResponse.json({ ...tasks[0], status: "queued" }); }),
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

afterEach(() => vi.restoreAllMocks());
function row(extra: Partial<AgentTask> = {}): AgentTask {
  return { id: "task-1", title: "Compact history", model_id: "local:test", status: "completed", revision: 3, control_revision: 3,
    created_at: 1, updated_at: 1, messages: [{ role: "user", content: "Inspect" }, { role: "assistant", content: "Done" }], events: [],
    pending_approval: null, steps: 3, result: "Done", error: null, limits: {}, permission_mode: "workspace", allowed_write_tools: [],
    allowed_actions: { compact: { enabled: true, reason: null, requires_control_revision: true } },
    ...extra } as AgentTask;
}
it("S03-I14 T2 displays the rejection reason separately from a stopped compaction", () => {
  const task = row({ events: [{ seq: 1, type: "context_compaction_started", source: "manual", at: 1 },
    { seq: 2, type: "context_compaction_rejected", error: "agent_compaction_incomplete", at: 2 }] });
  render(<RynContextCompactionRow item={extractCodexCompactions(task)[0]} />);
  expect(screen.getByText("Compaction rejected (Summary did not finish)")).toBeInTheDocument();
});
it("S03-I14 T2 labels an owner stop as stopped", () => {
  const task = row({ status: "paused", events: [{ seq: 1, type: "context_compaction_started", at: 1 }, { seq: 2, type: "paused", at: 2 }] });
  render(<RynContextCompactionRow item={extractCodexCompactions(task)[0]} />);
  expect(screen.getByText("Compaction stopped")).toBeInTheDocument();
});
it("S03-I14 T2 shows a manual rejection that happened before summary dispatch", () => {
  const task = row({ events: [{ seq: 1, type: "context_compaction_rejected", source: "manual", error: "agent_step_limit", at: 1 }] });
  const items = extractCodexCompactions(task);
  expect(items).toHaveLength(1);
  render(<RynContextCompactionRow item={items[0]} />);
  expect(screen.getByText(/Compaction rejected/)).toBeInTheDocument();
});
it("S03-I14 T2 shows manual completion and approximate before/after tokens", () => {
  const task = row({ events: [{ seq: 1, type: "context_compaction_started", source: "manual", at: 1, bytes_per_token: 4 },
    { seq: 2, type: "context_compacted", source: "manual", request_bytes_before: 800, after_bytes: 400, at: 2 }] });
  render(<RynContextCompactionRow item={extractCodexCompactions(task)[0]} />);
  expect(screen.getByText("Context compacted")).toBeInTheDocument();
  expect(screen.getByText("About 200 → 100 tokens")).toBeInTheDocument();
});
it("S03-I14 T2 accepts legacy completion events without size fields", () => {
  const task = row({ events: [{ seq: 1, type: "context_compacted", at: 1 }] });
  render(<RynContextCompactionRow item={extractCodexCompactions(task)[0]} />);
  expect(screen.getByText("Context automatically compacted")).toBeInTheDocument();
  expect(screen.queryByText(/NaN|undefined|tokens/)).not.toBeInTheDocument();
});
async function page(task: AgentTask) {
  const updates = api([task]);
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Compact history" }));
  return { user, updates };
}
it("S03-I14 T3 shows context occupancy and requires paid-request confirmation", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const task = row({ context_usage: { request_bytes: 1000, capacity_bytes: 2000, bytes_per_token: 4, step: 3, context_epoch: 0 } } as Partial<AgentTask>);
  const { user, updates } = await page(task);
  expect(await screen.findByLabelText("Context usage")).toHaveTextContent("50%");
  await user.click(await screen.findByRole("button", { name: "Compact context" }));
  expect(confirm).toHaveBeenCalledWith(expect.stringContaining("paid model request"));
  expect(updates).not.toHaveBeenCalled();
});
it("S03-I14 T3 confirmed compact uses the real versioned control request", async () => {
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const { user, updates } = await page(row());
  await user.click(await screen.findByRole("button", { name: "Compact context" }));
  await waitFor(() => expect(updates).toHaveBeenCalledWith(expect.objectContaining({ action: "compact", expected_control_revision: 3, client_request_id: expect.any(String) })));
});
it("S03-I14 T3 active or ineligible tasks disable the compact button", async () => {
  await page(row({ status: "running", allowed_actions: { compact: { enabled: false, reason: "agent_invalid_transition", requires_control_revision: true } } }));
  expect(await screen.findByRole("button", { name: "Compact context" })).toBeDisabled();
});
it("S03-I14 compact API method reaches the compact alias with revision and request identity", async () => {
  const task = row(); const updates = api([task]);
  await rynAgent.compact(task);
  expect(updates).toHaveBeenCalledWith(expect.objectContaining({ action: "compact", expected_control_revision: 3, client_request_id: expect.any(String) }));
});
