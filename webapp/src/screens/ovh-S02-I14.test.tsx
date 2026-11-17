import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { AgentAPIError, rynAgent, type AgentProject, type AgentTask } from "../domain/rynAgent";
import { agentTaskCache } from "../domain/agentWorkspaceCache";
import RynAgent from "./RynAgent";

vi.mock("../personal/model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }),
}));

function task(): AgentTask {
  return {
    id: "editable-task", title: "First request", model_id: "model", status: "running", revision: 12,
    schema_version: 2, control_revision: 3, event_cursor: 12, run_state: "running",
    goal: { revision: 1, kind: "action", original_message_id: "owner", text: "First request", constraints: [] },
    allowed_actions: Object.fromEntries(["pause", "stop", "cancel", "steer", "information", "edit_message"].map(action => [action, { enabled: true, reason: null, requires_control_revision: true }])),
    created_at: 1, updated_at: 1, messages: [
      { role: "system", content: "System" }, { role: "user", content: "First request" },
      { role: "assistant", content: "Earlier reply" }, { role: "user", content: "Second request", ui: { kind: "information", text: "Second request" } },
      { role: "assistant", content: "Later reply" },
    ], events: [], pending_approval: null, steps: 1, result: "", error: null, limits: {}, permission_mode: "workspace", allowed_write_tools: [],
  };
}

function api() {
  let current = task();
  server.use(
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json({ enabled: true, model_id: "model", permission_mode: "workspace", allowed_write_tools: [], max_steps: 16, max_seconds: 180, max_context_chars: 60000, max_output_chars: 20000 })),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [{ id: "model", name: "Model", source: "local_api", available: true, agent_supported: true }] })),
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [] })),
    http.get("*/api/local/agent/skills", () => HttpResponse.json({ skills: [] })),
    http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks: [current] })),
    http.get("*/api/local/agent/tasks/:id", () => HttpResponse.json(current)),
    http.get("*/api/local/agent/tasks/:id/artifacts", () => HttpResponse.json({ artifacts: [] })),
  );
  return { get: () => current, set: (next: AgentTask) => { current = next; } };
}

beforeEach(() => { sessionStorage.clear(); localStorage.clear(); agentTaskCache().clear(); sessionStorage.setItem("ryn.agent.selected", "editable-task"); });
afterEach(() => vi.restoreAllMocks());

it("T7 enqueues through backend and never dispatches from the browser", async () => {
  const backend = api(), user = userEvent.setup(), enqueue = vi.fn();
  server.use(http.post("*/api/local/agent/tasks/:id/queue", async ({ request }) => {
    const body = await request.json() as { message: string }; enqueue(body);
    const next: AgentTask = { ...backend.get(), revision: 13, queued_messages: [{ id: "queued-1", seq: 1, message: body.message, intent: "information", created_at: "2026-11-17T00:00:00Z" }] };
    backend.set(next); return HttpResponse.json(next);
  }));
  const control = vi.spyOn(rynAgent, "control");
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await screen.findByText("Earlier reply");
  await user.type(screen.getByRole("textbox", { name: "Tell Ryn what to do" }), "Server owned followup");
  await user.click(screen.getByRole("button", { name: "Queue follow-up" }));
  await waitFor(() => expect(enqueue).toHaveBeenCalledWith(expect.objectContaining({ message: "Server owned followup" })));
  expect(await screen.findByText("Server owned followup")).toBeInTheDocument();
  backend.set({ ...backend.get(), revision: 14, status: "completed", run_state: "ended" });
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 1200)); });
  expect(control).not.toHaveBeenCalled();
});
it("T7 migrates local queues once and clears only acknowledged items", async () => {
  api(); const enqueue = vi.fn();
  sessionStorage.setItem("ryn.agent.queued-messages", JSON.stringify({ "editable-task": [{ id: "b22fa4f7-699e-47e9-a413-b22d41a11ac2", text: "Migrate persisted draft", intent: "information", pausedReason: "failed", createdAt: Date.now() }] }));
  server.use(http.post("*/api/local/agent/tasks/:id/queue", async ({ request }) => {
    enqueue(await request.json());
    return HttpResponse.json({ ...task(), queued_messages: [{ id: "b22fa4f7-699e-47e9-a413-b22d41a11ac2", seq: 1, message: "Migrate persisted draft", intent: "information", created_at: "2026-11-17T00:00:00Z" }] });
  }));
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await waitFor(() => expect(enqueue).toHaveBeenCalledTimes(1));
  expect(sessionStorage.getItem("ryn.agent.queued-messages")).toBeFalsy();
  expect(enqueue).toHaveBeenCalledWith(expect.objectContaining({ paused_reason: "failed" }));
});

it("T7 edits, reorders, deletes and sends now through backend without replacing the draft", async () => {
  const backend = api(), user = userEvent.setup(), writes = vi.fn();
  backend.set({ ...backend.get(), queued_messages: ["first", "second"].map((message, index) => ({
    id: `q${index}`, seq: index + 1, message, intent: "information", created_at: "2026-11-17T00:00:00Z",
  })) });
  server.use(
    http.patch("*/api/local/agent/tasks/:id/queue/:item", async ({ params, request }) => {
      const body = await request.json() as { message?: string; send_now?: boolean }; writes("PATCH", params.item, body);
      const next = { ...backend.get(), revision: backend.get().revision + 1, queued_messages: body.send_now
        ? backend.get().queued_messages!.filter(item => item.id !== params.item)
        : backend.get().queued_messages!.map(item => item.id === params.item ? { ...item, message: body.message! } : item) };
      backend.set(next); return HttpResponse.json(next);
    }),
    http.post("*/api/local/agent/tasks/:id/queue/reorder", async ({ request }) => {
      const body = await request.json() as { ids: string[] }; writes("REORDER", body);
      const next = { ...backend.get(), revision: backend.get().revision + 1, queued_messages: body.ids.map(id => backend.get().queued_messages!.find(item => item.id === id)!) };
      backend.set(next); return HttpResponse.json(next);
    }),
    http.delete("*/api/local/agent/tasks/:id/queue/:item", ({ params }) => {
      writes("DELETE", params.item);
      const next = { ...backend.get(), revision: backend.get().revision + 1, queued_messages: backend.get().queued_messages!.filter(item => item.id !== params.item) };
      backend.set(next); return HttpResponse.json(next);
    }),
  );
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  const queue = await screen.findByRole("region", { name: "Queued follow-up messages" });
  await user.click(screen.getByRole("textbox", { name: "Tell Ryn what to do" }));
  await user.paste("Keep my unsent draft");
  await user.click(within(queue).getAllByRole("button", { name: "Edit queued message" })[0]);
  const edit = screen.getByRole("textbox", { name: "Edit queued message" });
  await user.clear(edit); await user.click(edit); await user.paste("edited first");
  await user.click(screen.getByRole("button", { name: "Save message" }));
  await screen.findByText("edited first");
  expect(writes).toHaveBeenCalledWith("PATCH", "q0", { message: "edited first" });
  expect(screen.getByRole("textbox", { name: "Tell Ryn what to do" })).toHaveValue("Keep my unsent draft");
  await user.click(screen.getByRole("button", { name: "Move queued message up" }));
  await waitFor(() => expect(writes).toHaveBeenCalledWith("REORDER", { ids: ["q1", "q0"] }));
  await waitFor(() => expect([...queue.querySelectorAll(".ra-queued-text")].map(item => item.textContent)).toEqual(["second", "edited first"]));
  await user.click(within(queue).getAllByRole("button", { name: "Delete queued message" })[0]);
  await waitFor(() => expect(writes).toHaveBeenCalledWith("DELETE", "q1"));
  await waitFor(() => expect(within(queue).queryByText("second")).not.toBeInTheDocument());
  await user.click(within(queue).getByRole("button", { name: "Steer queued message now" }));
  await waitFor(() => expect(writes).toHaveBeenCalledWith("PATCH", "q0", { send_now: true }));
});

it("T7 keeps an unacknowledged migration in local storage", async () => {
  api();
  const saved = JSON.stringify({ "editable-task": [{ id: "b22fa4f7-699e-47e9-a413-b22d41a11ac2", text: "Retain on failure", intent: "information", createdAt: Date.now() }] });
  sessionStorage.setItem("ryn.agent.queued-messages", saved);
  const enqueue = vi.fn();
  server.use(http.post("*/api/local/agent/tasks/:id/queue", () => { enqueue(); return HttpResponse.json({ detail: "agent_queue_limit" }, { status: 400 }); }));
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await waitFor(() => expect(enqueue).toHaveBeenCalledTimes(1));
  expect(sessionStorage.getItem("ryn.agent.queued-messages")).toBe(saved);
});

it("T7 preserves a newer draft typed while the enqueue response is pending", async () => {
  api(); const user = userEvent.setup();
  let release!: (value: AgentTask) => void;
  const enqueue = vi.spyOn(rynAgent, "queueMessage").mockImplementation(() => new Promise(resolve => { release = resolve; }));
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await screen.findByText("Earlier reply");
  const input = screen.getByRole("textbox", { name: "Tell Ryn what to do" });
  await user.click(input); await user.paste("Queued first");
  await user.click(screen.getByRole("button", { name: "Queue follow-up" }));
  await waitFor(() => expect(enqueue).toHaveBeenCalledTimes(1));
  await user.clear(input); await user.click(input); await user.paste("New unsent draft");
  await act(async () => release({ ...task(), revision: 13, queued_messages: [{
    id: "pending-q", seq: 1, message: "Queued first", intent: "information", created_at: "2026-11-17T00:00:00Z",
  }] }));
  await screen.findByText("Queued first");
  expect(input).toHaveValue("New unsent draft");
});

it("T7 keeps project drafts and selection while a legacy queue migration completes", async () => {
  const backend = api(), user = userEvent.setup();
  const projects: AgentProject[] = ["Alpha", "Beta"].map(name => ({
    id: name.toLowerCase(), name, path: `D:\\code\\${name}`, generation: 1, entry_files: [], manifests: [], directories: [],
  }));
  backend.set({ ...backend.get(), project: projects[0] });
  sessionStorage.setItem("ryn.agent.queued-messages", JSON.stringify({ "editable-task": [{
    id: "b22fa4f7-699e-47e9-a413-b22d41a11ac2", text: "Previously queued", intent: "information", createdAt: Date.now(),
  }] }));
  let release!: (value: AgentTask) => void;
  const migration = vi.spyOn(rynAgent, "queueMessage").mockImplementation(() => new Promise(resolve => { release = resolve; }));
  server.use(
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects, selected_project_id: "alpha" })),
    http.post("*/api/local/agent/projects/selection", async ({ request }) => HttpResponse.json(await request.json())),
  );
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await screen.findByText("Earlier reply");
  await waitFor(() => expect(migration).toHaveBeenCalledTimes(1));
  const sidebar = within(screen.getByRole("region", { name: "Projects" }));
  await user.click(await sidebar.findByRole("button", { name: /^Alpha ·/ }));
  await screen.findByRole("heading", { name: "What would you like to build in D:\\code\\Alpha?" });
  await user.click(screen.getByRole("textbox")); await user.paste("Alpha draft kept");
  await user.click(sidebar.getByRole("button", { name: /^Beta ·/ }));
  await screen.findByRole("heading", { name: "What would you like to build in D:\\code\\Beta?" });
  await user.click(screen.getByRole("textbox")); await user.paste("Beta draft kept");
  const next: AgentTask = { ...backend.get(), revision: 13, queued_messages: [{
    id: "b22fa4f7-699e-47e9-a413-b22d41a11ac2", seq: 1, message: "Previously queued", intent: "information", created_at: "2026-11-17T00:00:00Z",
  }] };
  backend.set(next);
  await act(async () => release(next));
  await waitFor(() => expect(sessionStorage.getItem("ryn.agent.queued-messages")).toBeFalsy());
  expect(screen.getByRole("textbox")).toHaveValue("Beta draft kept");
  expect(sidebar.getByRole("button", { name: /^Beta ·/ })).toHaveAttribute("aria-pressed", "true");
  await user.click(sidebar.getByRole("button", { name: /^Alpha ·/ }));
  await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("Alpha draft kept"));
  await user.click(sidebar.getByRole("button", { name: "First request" }));
  expect(await screen.findByText("Previously queued")).toBeInTheDocument();
});
