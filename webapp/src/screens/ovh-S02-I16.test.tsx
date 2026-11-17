import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { AgentAPIError, rynAgent, type AgentTask } from "../domain/rynAgent";
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
    allowed_actions: Object.fromEntries(["pause", "stop", "cancel", "steer", "information", "edit_message", "rollback"].map(action => [action, { enabled: true, reason: null, requires_control_revision: true }])),
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

it("S02-I16 T1 returns before the selected owner turn and refills its text without sending", async () => {
  const backend = api(), user = userEvent.setup();
  const control = vi.spyOn(rynAgent, "control").mockImplementation(async (_id, _route, body) => {
    const prior = backend.get();
    const next: AgentTask = { ...prior, status: "completed", run_state: "ended", control_revision: 5,
      conversation_revision: 1, messages: prior.messages.slice(0, Number(body.message_index)) };
    backend.set(next); return next;
  });
  const { container } = render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await screen.findByText("Later reply");
  const bubbles = [...container.querySelectorAll<HTMLElement>(".ra-message.ra-user")];
  expect(within(bubbles[0]).queryByRole("button", { name: "Return here" })).not.toBeInTheDocument();
  await user.click(within(bubbles[1]).getByRole("button", { name: "Return here" }));
  const dialog = screen.getByRole("dialog", { name: "Return here" });
  expect(within(dialog).getByText("File changes already made will not be undone.")).toBeInTheDocument();
  expect(control).not.toHaveBeenCalled();
  await user.click(within(dialog).getByRole("button", { name: "Return here" }));
  await waitFor(() => expect(control).toHaveBeenCalledOnce());
  expect(control).toHaveBeenCalledWith("editable-task", "actions", expect.objectContaining({
    action: "rollback", message_index: 3, expected_control_revision: 3 }));
  expect(control.mock.calls[0][2]).not.toHaveProperty("message");
  await waitFor(() => expect(screen.queryByText("Later reply")).not.toBeInTheDocument());
  expect(screen.getByText("Earlier reply")).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "Tell Ryn what to do" })).toHaveValue("Second request");
});

it("S02-I16 T2 disables rollback when the host forbids it", async () => {
  const backend = api();
  backend.set({ ...backend.get(), allowed_actions: { ...backend.get().allowed_actions,
    rollback: { enabled: false, reason: "agent_operation_unresolved", requires_control_revision: true } } });
  const { container } = render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await screen.findByText("Second request");
  const bubble = [...container.querySelectorAll<HTMLElement>(".ra-message.ra-user")].find(item => within(item).queryByText("Second request"))!;
  expect(within(bubble).getByRole("button", { name: "Return here" })).toBeDisabled();
});

it("S02-I16 T3 a rejected rollback keeps the transcript and existing draft", async () => {
  api(); const user = userEvent.setup();
  vi.spyOn(rynAgent, "control").mockRejectedValue(new AgentAPIError("agent_control_revision_conflict", 409));
  const { container } = render(<MemoryRouter><RynAgent /></MemoryRouter>);
  const input = await screen.findByRole("textbox", { name: "Tell Ryn what to do" });
  await user.type(input, "Existing draft");
  const bubble = [...container.querySelectorAll<HTMLElement>(".ra-message.ra-user")].find(item => within(item).queryByText("Second request"))!;
  await user.click(within(bubble).getByRole("button", { name: "Return here" }));
  await user.click(within(screen.getByRole("dialog", { name: "Return here" })).getByRole("button", { name: "Return here" }));
  await waitFor(() => expect(screen.getByText("Later reply")).toBeInTheDocument());
  expect(input).toHaveValue("Existing draft");
});

it("S02-I16 T1 recovers a lost rollback response with the original draft and request id", async () => {
  const backend = api(), user = userEvent.setup();
  const control = vi.spyOn(rynAgent, "control").mockImplementation(async (_id, _route, body) => {
    if (control.mock.calls.length === 1) {
      backend.set({ ...backend.get(), status: "completed", run_state: "ended", control_revision: 5,
        conversation_revision: 1, messages: backend.get().messages.slice(0, Number(body.message_index)) });
      throw new Error("Synthetic lost response");
    }
    return backend.get();
  });
  const { container } = render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await screen.findByText("Later reply");
  const bubble = [...container.querySelectorAll<HTMLElement>(".ra-message.ra-user")].find(item => within(item).queryByText("Second request"))!;
  await user.click(within(bubble).getByRole("button", { name: "Return here" }));
  await user.click(within(screen.getByRole("dialog", { name: "Return here" })).getByRole("button", { name: "Return here" }));
  await user.click(await screen.findByRole("button", { name: "Check original request" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Tell Ryn what to do" })).toHaveValue("Second request"));
  expect(control.mock.calls[1][2]).toEqual(control.mock.calls[0][2]);
  expect(screen.queryByText("Later reply")).not.toBeInTheDocument();
});
