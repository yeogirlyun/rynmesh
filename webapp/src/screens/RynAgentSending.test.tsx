import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { AgentAPIError, rynAgent, type AgentSettings, type AgentTask } from "../domain/rynAgent";
import { agentTaskCache } from "../domain/agentWorkspaceCache";
import RynAgent from "./RynAgent";

vi.mock("../personal/model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }),
}));

type SendKind = "create" | "followup";

function task(overrides: Partial<AgentTask> = {}): AgentTask {
  return {
    id: "task-1", title: "Inspect devices", model_id: "verified-model", status: "running", revision: 1000,
    schema_version: 2, control_revision: 7, event_cursor: 1000, run_state: "running",
    goal: { revision: 2, kind: "action", original_message_id: "owner-1", text: "Inspect devices", constraints: [] },
    allowed_actions: Object.fromEntries(["pause", "cancel", "steer", "information"].map(action => [action, { enabled: true, reason: null, requires_control_revision: true }])),
    created_at: 1, updated_at: 1, messages: [{ role: "user", content: "Inspect devices" }], events: [],
    pending_approval: null, steps: 1, result: "", error: null, limits: {}, permission_mode: "confirm_changes", allowed_write_tools: [],
    ...overrides,
  };
}

function api(initialTasks: AgentTask[] = []) {
  let tasks = initialTasks;
  const settings: AgentSettings = {
    enabled: true, model_id: "verified-model", permission_mode: "confirm_changes", allowed_write_tools: [],
    max_steps: 16, max_seconds: 180, max_context_chars: 60000, max_output_chars: 20000,
  };
  server.use(
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json(settings)),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [
      { id: "verified-model", name: "Verified model", source: "local_api", available: true, agent_supported: true, tool_support: "supported" },
    ] })),
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [], create_scope_supported: false })),
    http.get("*/api/local/agent/skills", () => HttpResponse.json({ skills: [] })),
    http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks })),
    http.get("*/api/local/agent/tasks/:id", ({ params }) => HttpResponse.json(tasks.find(item => item.id === params.id))),
    http.get("*/api/local/agent/tasks/:id/artifacts", () => HttpResponse.json({ artifacts: [] })),
  );
  return { setTasks: (next: AgentTask[]) => { tasks = next; } };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}

function pendingSend(kind: SendKind) {
  const response = deferred<AgentTask>();
  const request = kind === "create"
    ? vi.spyOn(rynAgent, "create").mockImplementation(() => response.promise)
    : vi.spyOn(rynAgent, "control").mockImplementation(() => response.promise);
  return { ...response, request };
}

function bubbles(container: HTMLElement, text: string) {
  return [...container.querySelectorAll<HTMLElement>(".ra-message.ra-user")]
    .filter(message => within(message).queryByText(text, { exact: true }));
}

async function openComposer(kind: SendKind) {
  const view = render(<MemoryRouter initialEntries={["/agent"]}><RynAgent /></MemoryRouter>);
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("verified-model"));
  if (kind === "followup") {
    await userEvent.click(await screen.findByRole("button", { name: "Inspect devices" }));
    await waitFor(() => expect(screen.getByRole("textbox")).toBeEnabled());
  }
  return view;
}

function accepted(kind: SendKind, text: string, original = task()): AgentTask {
  return task({ ...original, revision: original.revision + 1, control_revision: 8,
    messages: kind === "create" ? [{ role: "user", content: text }] : [...original.messages, { role: "user", content: text, ui: { kind: "steer", text } }],
  });
}

beforeEach(() => {
  sessionStorage.clear(); localStorage.clear(); agentTaskCache().clear();
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value() { this.setAttribute("open", ""); } });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value() { this.removeAttribute("open"); } });
});
afterEach(() => { vi.restoreAllMocks(); });

it.each<SendKind>(["create", "followup"])("shows a pending %s immediately and preserves the next draft after acknowledgement", async kind => {
  const backend = api(kind === "followup" ? [task()] : []);
  const pending = pendingSend(kind);
  const user = userEvent.setup();
  const { container } = await openComposer(kind);
  const message = "Only inspect the disk devices";
  const input = screen.getByRole("textbox");
  await user.type(input, message);
  await user.click(screen.getByRole("button", { name: kind === "create" ? "Send task" : "Send requirements update" }));

  expect(pending.request).toHaveBeenCalledTimes(1);
  expect(bubbles(container, message)).toHaveLength(1);
  expect(screen.getByText("Sending message…")).toBeVisible();
  expect(input).toHaveValue("");
  expect(input).toBeEnabled();
  await user.type(input, "The next requirement is still a draft");
  expect(container.querySelector(".ra-send")).toBeDisabled();
  await user.keyboard("{Control>}{Enter}{/Control}");
  expect(pending.request).toHaveBeenCalledTimes(1);

  const next = accepted(kind, message);
  backend.setTasks([next]);
  await act(async () => { pending.resolve(next); await pending.promise; });
  await waitFor(() => expect(screen.queryByText("Sending message…")).not.toBeInTheDocument());
  expect(bubbles(container, message)).toHaveLength(1);
  expect(screen.getByRole("textbox")).toHaveValue("The next requirement is still a draft");
});

it.each([
  { kind: "create" as const, status: 400, newerDraft: false },
  { kind: "create" as const, status: 409, newerDraft: true },
  { kind: "followup" as const, status: 409, newerDraft: false },
  { kind: "followup" as const, status: 400, newerDraft: true },
])("removes a rejected $kind bubble for HTTP $status and restores only an untouched draft", async ({ kind, status, newerDraft }) => {
  api(kind === "followup" ? [task()] : []);
  const pending = pendingSend(kind);
  const user = userEvent.setup();
  const { container } = await openComposer(kind);
  const message = "Please inspect the network adapters";
  await user.type(screen.getByRole("textbox"), message);
  await user.click(screen.getByRole("button", { name: kind === "create" ? "Send task" : "Send requirements update" }));
  expect(bubbles(container, message)).toHaveLength(1);
  expect(screen.getByRole("textbox")).toHaveValue("");
  if (newerDraft) await user.type(screen.getByRole("textbox"), "Keep this newer draft");

  await act(async () => { pending.reject(new AgentAPIError(status === 409 ? "agent_revision_conflict" : "agent_model_unavailable", status)); });
  await waitFor(() => expect(bubbles(container, message)).toHaveLength(0));
  expect(screen.getByRole("textbox")).toHaveValue(newerDraft ? "Keep this newer draft" : message);
  expect(screen.queryByText("Sending message…")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Recover task request" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Check original request" })).not.toBeInTheDocument();
});

it.each([
  { kind: "create" as const, failure: "network" },
  { kind: "create" as const, failure: "500" },
  { kind: "followup" as const, failure: "network" },
  { kind: "followup" as const, failure: "500" },
])("retains a $kind bubble and recovery control after an uncertain $failure response", async ({ kind, failure }) => {
  api(kind === "followup" ? [task()] : []);
  const pending = pendingSend(kind);
  const user = userEvent.setup();
  const { container } = await openComposer(kind);
  const message = "Preserve this potentially accepted message";
  await user.type(screen.getByRole("textbox"), message);
  await user.click(screen.getByRole("button", { name: kind === "create" ? "Send task" : "Send requirements update" }));
  await user.type(screen.getByRole("textbox"), "Keep my next draft");
  await act(async () => { pending.reject(failure === "500" ? new AgentAPIError("agent_request_failed", 500) : new TypeError("Network disconnected")); });

  expect(await screen.findByRole("button", { name: kind === "create" ? "Recover task request" : "Check original request" })).toBeEnabled();
  expect(bubbles(container, message)).toHaveLength(1);
  expect(screen.getByRole("textbox")).toHaveValue("Keep my next draft");
  expect(screen.queryByText("Sending message…")).not.toBeInTheDocument();
  expect(pending.request).toHaveBeenCalledTimes(1);
});

it.each([
  { kind: "create" as const, status: 400, newerDraft: false },
  { kind: "create" as const, status: 409, newerDraft: true },
  { kind: "followup" as const, status: 409, newerDraft: false },
  { kind: "followup" as const, status: 400, newerDraft: true },
])("releases an uncertain $kind after definitive HTTP $status recovery rejection without losing a newer draft", async ({ kind, status, newerDraft }) => {
  api(kind === "followup" ? [task()] : []);
  const pending = pendingSend(kind);
  const user = userEvent.setup();
  const { container } = await openComposer(kind);
  const message = "Recover the original disk inspection request";
  await user.type(screen.getByRole("textbox"), message);
  await user.click(screen.getByRole("button", { name: kind === "create" ? "Send task" : "Send requirements update" }));
  await act(async () => { pending.reject(new TypeError("Lost response")); });
  const recovery = await screen.findByRole("button", { name: kind === "create" ? "Recover task request" : "Check original request" });
  if (newerDraft) await user.type(screen.getByRole("textbox"), "A newer draft survives the rejected recovery");
  pending.request.mockRejectedValueOnce(new AgentAPIError(status === 409 ? "agent_revision_conflict" : "agent_model_unavailable", status));
  await user.click(recovery);

  await waitFor(() => expect(bubbles(container, message)).toHaveLength(0));
  expect(screen.getByRole("textbox")).toHaveValue(newerDraft ? "A newer draft survives the rejected recovery" : message);
  expect(screen.queryByRole("button", { name: "Recover task request" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Check original request" })).not.toBeInTheDocument();
  expect(container.querySelector(".ra-send")).toBeEnabled();
  expect(pending.request).toHaveBeenCalledTimes(2);
});

it.each([
  { failure: "network", newerDraft: false },
  { failure: "500", newerDraft: true },
])("returns a legacy follow-up to an editable draft after $failure without leaving an unrecoverable pending message", async ({ failure, newerDraft }) => {
  const legacy = task({ status: "completed", run_state: undefined, schema_version: undefined, allowed_actions: undefined, control_revision: undefined });
  api([legacy]);
  const response = deferred<AgentTask>();
  const request = vi.spyOn(rynAgent, "message").mockImplementation(() => response.promise);
  const user = userEvent.setup();
  const { container } = await openComposer("create");
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await waitFor(() => expect(screen.getByRole("textbox")).toBeEnabled());
  const message = "Try another inspection in this legacy conversation";
  await user.type(screen.getByRole("textbox"), message);
  await user.click(screen.getByRole("button", { name: "Send follow-up" }));
  expect(screen.getByRole("textbox")).toHaveValue("");
  expect(bubbles(container, message)).toHaveLength(1);
  expect(screen.getByText("Sending message…")).toBeVisible();
  if (newerDraft) await user.type(screen.getByRole("textbox"), "Keep this newer legacy draft");
  await act(async () => { response.reject(failure === "500" ? new AgentAPIError("agent_request_failed", 500) : new TypeError("Disconnected")); });

  await waitFor(() => expect(bubbles(container, message)).toHaveLength(0));
  expect(screen.getByRole("textbox")).toHaveValue(newerDraft ? "Keep this newer legacy draft" : message);
  expect(screen.queryByText("Sending message…")).not.toBeInTheDocument();
  expect(screen.queryByText("Waiting for confirmation · your message is saved")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Recover task request" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Check original request" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Send follow-up" })).toBeEnabled();
  expect(request).toHaveBeenCalledTimes(1);
});

it("keeps project conversations reachable when their task index arrives after the project list failed", async () => {
  const project = { id: "unlisted-project", name: "Recovered project", path: "D:\\code\\recovered", generation: 1, entry_files: [], manifests: [], directories: [] };
  const conversation = task({ project, messages: [{ role: "assistant", content: "Project history remains readable" }] });
  api();
  server.use(http.get("*/api/local/agent/projects", () => HttpResponse.json({ detail: "agent_request_failed" }, { status: 500 })));
  const taskIndex = deferred<{ tasks: AgentTask[] }>();
  vi.spyOn(rynAgent, "tasks").mockImplementation(() => taskIndex.promise);
  const user = userEvent.setup();
  await openComposer("create");
  await screen.findByText("Could not load the project. Check the folder and node connection.");
  // The sidebar mounted with no tasks; late project history must have a visible fallback.
  await act(async () => { taskIndex.resolve({ tasks: [conversation] }); await taskIndex.promise; });
  server.use(http.get("*/api/local/agent/tasks/:id", () => HttpResponse.json(conversation)));
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  expect(await screen.findByText("Project history remains readable")).toBeVisible();
});

it.each(["create retry", "followup retry", "create discovery"] as const)("preserves a deliberately retyped identical next draft after %s succeeds", async recovery => {
  const kind = recovery === "followup retry" ? "followup" : "create";
  const backend = api(kind === "followup" ? [task()] : []);
  const pending = pendingSend(kind);
  const user = userEvent.setup();
  const { container } = await openComposer(kind);
  const message = "Repeat this inspection after the pending request";
  await user.type(screen.getByRole("textbox"), message);
  await user.click(screen.getByRole("button", { name: kind === "create" ? "Send task" : "Send requirements update" }));
  await act(async () => { pending.reject(new TypeError("Lost acknowledgement")); });
  const recoveryButton = await screen.findByRole("button", { name: kind === "create" ? "Recover task request" : "Check original request" });
  await user.type(screen.getByRole("textbox"), message);
  const next = accepted(kind, message);
  if (kind === "create") next.client_request_id = String(pending.request.mock.calls[0][2]);
  backend.setTasks([next]);
  if (recovery === "create discovery") {
    act(() => { window.dispatchEvent(new Event("focus")); });
  } else {
    pending.request.mockResolvedValueOnce(next);
    await user.click(recoveryButton);
  }
  await waitFor(() => expect(screen.queryByRole("button", { name: kind === "create" ? "Recover task request" : "Check original request" })).not.toBeInTheDocument());
  expect(screen.getByRole("textbox")).toHaveValue(message);
  await waitFor(() => expect(bubbles(container, message)).toHaveLength(1));
});

it.each<SendKind>(["create", "followup"])("keeps a pending %s scoped to its conversation while another task is open", async kind => {
  const other = task({ id: "task-2", title: "Independent task", messages: [{ role: "assistant", content: "Independent conversation reply" }] });
  const backend = api(kind === "followup" ? [task(), other] : [other]);
  const pending = pendingSend(kind);
  const user = userEvent.setup();
  const { container } = await openComposer(kind);
  const message = "Pending in the original conversation";
  await user.type(screen.getByRole("textbox"), message);
  await user.click(screen.getByRole("button", { name: kind === "create" ? "Send task" : "Send requirements update" }));
  expect(bubbles(container, message)).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "Independent task" }));
  await screen.findByText("Independent conversation reply");
  expect(bubbles(container, message)).toHaveLength(0);
  expect(screen.queryByText("Sending message…")).not.toBeInTheDocument();
  await user.type(screen.getByRole("textbox"), "A draft for the independent conversation");

  const next = accepted(kind, message);
  backend.setTasks([next, other]);
  await act(async () => { pending.resolve(next); await pending.promise; });
  expect(screen.getByText("Independent conversation reply")).toBeVisible();
  expect(screen.getByRole("textbox")).toHaveValue("A draft for the independent conversation");
  expect(bubbles(container, message)).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "Inspect devices" }));
  await waitFor(() => expect(bubbles(container, message)).toHaveLength(1));
});

it("deduplicates a follow-up accepted by a refreshed snapshot before POST resolves without mistaking an earlier identical message for its receipt", async () => {
  const message = "Check the disk again";
  const original = task({ messages: [
    { role: "user", content: "Inspect devices" },
    { role: "user", content: message, ui: { kind: "steer", text: message } },
    { role: "assistant", content: "Earlier disk inspection reply" },
  ] });
  const backend = api([original]);
  const pending = pendingSend("followup");
  const user = userEvent.setup();
  const { container } = await openComposer("followup");
  await user.type(screen.getByRole("textbox"), message);
  await user.click(screen.getByRole("button", { name: "Send requirements update" }));
  expect(bubbles(container, message)).toHaveLength(2);

  const background = task({ ...original, revision: original.revision + 1,
    messages: [...original.messages, { role: "assistant", content: "Background update without message receipt" }],
  });
  backend.setTasks([background]);
  act(() => { window.dispatchEvent(new Event("focus")); });
  await screen.findByText("Background update without message receipt");
  expect(bubbles(container, message)).toHaveLength(2);

  const next = accepted("followup", message, background);
  next.messages.push({ role: "assistant", content: "Receipt obtained before the POST response" });
  backend.setTasks([next]);
  act(() => { window.dispatchEvent(new Event("focus")); });
  await screen.findByText("Receipt obtained before the POST response");
  expect(bubbles(container, message)).toHaveLength(2);
  expect(pending.request).toHaveBeenCalledTimes(1);
  await act(async () => { pending.resolve(next); await pending.promise; });
  expect(bubbles(container, message)).toHaveLength(2);
});
