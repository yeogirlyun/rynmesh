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

it("keeps Stop visible with a typed draft, stops the turn, and sends the preserved draft afterwards", async () => {
  const backend = api(), user = userEvent.setup();
  const control = vi.spyOn(rynAgent, "control").mockImplementation(async (_id, route, body) => {
    const prior = backend.get();
    const next: AgentTask = { ...prior, revision: prior.revision + 1, control_revision: prior.control_revision! + 1,
      ...(route === "actions" ? { status: "paused", run_state: "paused" } : { status: "queued", run_state: "queued", messages: [...prior.messages, { role: "user", content: String(body.message) }] }),
    };
    backend.set(next); return next;
  });
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  const input = await screen.findByRole("textbox", { name: "Tell Ryn what to do" });
  await user.type(input, "Continue with the new direction");
  await user.click(screen.getByRole("button", { name: "Stop" }));
  expect(control).toHaveBeenCalledWith("editable-task", "actions", expect.objectContaining({ action: "stop" }));
  expect(input).toHaveValue("Continue with the new direction");
  await user.click(await screen.findByRole("button", { name: "Send information" }));
  expect(control).toHaveBeenLastCalledWith("editable-task", "messages", expect.objectContaining({ message: "Continue with the new direction" }));
});

it("edits an earlier user turn by its original transcript index and replaces later replies", async () => {
  const backend = api(), user = userEvent.setup();
  const control = vi.spyOn(rynAgent, "control").mockImplementation(async (_id, _route, body) => {
    const prior = backend.get(), index = Number(body.edit_message_index);
    const next: AgentTask = { ...prior, revision: prior.revision + 1, control_revision: prior.control_revision! + 1, status: "queued", run_state: "queued",
      messages: [...prior.messages.slice(0, index), { role: "user", content: String(body.message) }], result: "",
    };
    backend.set(next); return next;
  });
  const { container } = render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await screen.findByText("Earlier reply");
  const bubble = [...container.querySelectorAll<HTMLElement>(".ra-message.ra-user")].find(item => within(item).queryByText("First request"))!;
  await user.click(within(bubble).getByRole("button", { name: "Edit message" }));
  const editor = screen.getByRole("textbox", { name: "Edit previous message" });
  await user.clear(editor); await user.type(editor, "Revised first request");
  await user.click(screen.getByRole("button", { name: "Save and send" }));
  expect(control).toHaveBeenCalledWith("editable-task", "messages", expect.objectContaining({ message: "Revised first request", edit_message_index: 1, expected_control_revision: 3 }));
  await screen.findByText("Revised first request");
  await waitFor(() => expect(screen.queryByText("Later reply")).not.toBeInTheDocument());
  expect(screen.queryByText("Second request")).not.toBeInTheDocument();
});

it("sends an attachment without typed text and keeps the file after a rejected request", async () => {
  const backend = api(), user = userEvent.setup();
  backend.set({ ...backend.get(), status: "completed", run_state: "ended" });
  const control = vi.spyOn(rynAgent, "control").mockRejectedValue(new AgentAPIError("agent_model_unavailable", 400));
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  const picker = await screen.findByLabelText("Attach images or files", { selector: "input" });
  await user.upload(picker, new File(["Quarterly sales"], "sales.txt", { type: "text/plain" }));
  const send = screen.getByRole("button", { name: "Send information" });
  await waitFor(() => expect(send).toBeEnabled());
  await user.click(send);
  expect(control).toHaveBeenCalledWith("editable-task", "messages", expect.objectContaining({ message: "Please read the attached files.", attachments: [{ name: "sales.txt", media_type: "text/plain", data_base64: btoa("Quarterly sales") }] }));
  expect(await screen.findByRole("button", { name: "Remove sales.txt" })).toBeInTheDocument();
});

it("includes selected files when creating a chat and clears them after acknowledgement", async () => {
  api(); sessionStorage.removeItem("ryn.agent.selected");
  const user = userEvent.setup();
  const create = vi.spyOn(rynAgent, "create").mockImplementation(async message => ({ ...task(), id: "new-chat", messages: [{ role: "user", content: message }], revision: 20 }));
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  const picker = await screen.findByLabelText("Attach images or files", { selector: "input" });
  await user.upload(picker, new File(["A,B\n1,2"], "data.csv", { type: "text/csv" }));
  const send = screen.getByRole("button", { name: "Send task" });
  await waitFor(() => expect(send).toBeEnabled());
  await user.click(send);
  await waitFor(() => expect(create).toHaveBeenCalledOnce());
  expect(create.mock.calls[0][9]).toEqual([{ name: "data.csv", media_type: "text/csv", data_base64: btoa("A,B\n1,2") }]);
  await waitFor(() => expect(screen.queryByRole("button", { name: "Remove data.csv" })).not.toBeInTheDocument());
});

it("clears submitted attachments when polling recovers a lost create response", async () => {
  const backend = api(), user = userEvent.setup(); sessionStorage.removeItem("ryn.agent.selected");
  let reject!: (reason: unknown) => void;
  const response = new Promise<AgentTask>((_resolve, fail) => { reject = fail; });
  const create = vi.spyOn(rynAgent, "create").mockImplementation(() => response);
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await user.upload(await screen.findByLabelText("Attach images or files", { selector: "input" }), new File(["Sales"], "recovered.txt", { type: "text/plain" }));
  await user.click(screen.getByRole("button", { name: "Send task" }));
  await waitFor(() => expect(create).toHaveBeenCalledOnce());
  backend.set({ ...task(), client_request_id: create.mock.calls[0][2], messages: [{ role: "user", content: "Please read the attached files." }] });
  act(() => { window.dispatchEvent(new Event("focus")); });
  await waitFor(() => expect(sessionStorage.getItem("ryn.agent.pending-create")).toBeNull());
  await act(async () => { reject(new TypeError("Lost response")); });
  await user.click(screen.getAllByRole("button", { name: "New task" })[0]);
  expect(screen.queryByRole("button", { name: "Remove recovered.txt" })).not.toBeInTheDocument();
});

it("restores saved attachment inputs after remount and keeps them on definitive retry rejection", async () => {
  api(); sessionStorage.removeItem("ryn.agent.selected");
  const attachments = [{ name: "saved.txt", media_type: "text/plain", data_base64: btoa("Saved") }];
  sessionStorage.setItem("ryn.agent.pending-create", JSON.stringify({ client_request_id: "saved-create", message: "Read saved file", model_id: "model", draft_key: "new", attachments, attachment_ids: ["saved-file"] }));
  const create = vi.spyOn(rynAgent, "create").mockRejectedValue(new AgentAPIError("agent_model_unavailable", 400));
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await screen.findByRole("button", { name: "Remove saved.txt" });
  await userEvent.click(await screen.findByRole("button", { name: "Recover task request" }));
  await waitFor(() => expect(create).toHaveBeenCalledOnce());
  expect(create.mock.calls[0][9]).toEqual(attachments);
  expect(screen.getByRole("button", { name: "Remove saved.txt" })).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "Tell Ryn what to do" })).toHaveValue("Read saved file");
});
