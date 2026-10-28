import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { AgentAPIError, rynAgent, type AgentProject, type AgentSettings, type AgentTask } from "../domain/rynAgent";
import { agentTaskCache } from "../domain/agentWorkspaceCache";
import RynAgent from "./RynAgent";

vi.mock("../personal/model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }),
}));

const alpha: AgentProject = { id: "alpha", name: "Alpha project", path: "D:\\code\\alpha", generation: 1, entry_files: [], manifests: [], directories: [] };
const beta: AgentProject = { id: "beta", name: "Beta project", path: "D:\\code\\beta", generation: 1, entry_files: [], manifests: [], directories: [] };

function task(overrides: Partial<AgentTask> = {}): AgentTask {
  return {
    id: "task-1", title: "Original conversation", model_id: "verified-model", status: "running", revision: 1000,
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
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [alpha, beta] })),
    http.post("*/api/local/agent/projects/selection", async ({ request }) => HttpResponse.json({ selected_project_id: (await request.json() as { project_id: string }).project_id })),
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

async function open() {
  const view = render(<MemoryRouter initialEntries={["/agent"]}><RynAgent /></MemoryRouter>);
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("verified-model"));
  await screen.findByRole("button", { name: /^Alpha project ·/ });
  return view;
}

function newTaskButton() {
  return within(screen.getByRole("complementary", { name: "Task list" })).getByRole("button", { name: "New task" });
}

async function writeDraft(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.click(screen.getByRole("textbox"));
  await user.paste(text);
}

function bubbles(container: HTMLElement, text: string) {
  return [...container.querySelectorAll<HTMLElement>(".ra-message.ra-user")]
    .filter(message => within(message).queryByText(text, { exact: true }));
}

beforeEach(() => {
  sessionStorage.clear(); localStorage.clear(); agentTaskCache().clear();
  sessionStorage.setItem("ryn.agent.project", "__general__");
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value() { this.setAttribute("open", ""); } });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value() { this.removeAttribute("open"); } });
});
afterEach(() => { vi.restoreAllMocks(); });

it("can switch projects and start a task while create waits, preserving the chosen project's draft after acknowledgement", async () => {
  const backend = api();
  const response = deferred<AgentTask>();
  const create = vi.spyOn(rynAgent, "create").mockImplementation(() => response.promise);
  const user = userEvent.setup();
  const { container } = await open();
  await user.click(screen.getByRole("button", { name: /^Alpha project ·/ }));
  await waitFor(() => expect(screen.getByRole("button", { name: /^Alpha project ·/ })).toHaveAttribute("aria-pressed", "true"));
  const message = "Inspect Alpha while I work elsewhere";
  await writeDraft(user, message);
  await user.click(screen.getByRole("button", { name: "Send task" }));
  expect(create).toHaveBeenCalledTimes(1);
  expect(create.mock.calls[0][7]).toBe(alpha.id);
  expect(bubbles(container, message)).toHaveLength(1);

  expect(screen.getByRole("button", { name: /^Beta project ·/ })).toBeEnabled();
  expect(screen.getByRole("button", { name: "New task in Beta project" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "New task without a project" })).toBeEnabled();
  await user.click(screen.getByRole("button", { name: "New task in Beta project" }));
  await waitFor(() => expect(screen.getByRole("button", { name: /^Beta project ·/ })).toHaveAttribute("aria-pressed", "true"));
  await writeDraft(user, "A separate Beta draft");
  expect(bubbles(container, message)).toHaveLength(0);
  const accepted = task({ project: alpha, title: "Alpha inspection", messages: [{ role: "user", content: message }] });
  backend.setTasks([accepted]);
  await act(async () => { response.resolve(accepted); await response.promise; });

  expect(screen.getByRole("button", { name: /^Beta project ·/ })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("textbox")).toHaveValue("A separate Beta draft");
  expect(sessionStorage.getItem("ryn.agent.selected")).toBeNull();
  expect(bubbles(container, message)).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "Alpha inspection" }));
  await waitFor(() => expect(bubbles(container, message)).toHaveLength(1));
});

it("can open a new task and another history during follow-up without moving the response or their drafts", async () => {
  const original = task();
  const other = task({ id: "task-2", title: "Other conversation", messages: [{ role: "assistant", content: "Independent conversation reply" }] });
  const backend = api([original, other]);
  const response = deferred<AgentTask>();
  const control = vi.spyOn(rynAgent, "control").mockImplementation(() => response.promise);
  const user = userEvent.setup();
  const { container } = await open();
  await user.click(screen.getByRole("button", { name: "Original conversation" }));
  await waitFor(() => expect(screen.getByRole("textbox")).toBeEnabled());
  const message = "Follow up only in the original conversation";
  await writeDraft(user, message);
  await user.click(screen.getByRole("button", { name: "Send requirements update" }));
  expect(control).toHaveBeenCalledTimes(1);
  await user.click(newTaskButton());
  await waitFor(() => expect(bubbles(container, message)).toHaveLength(0));
  expect(sessionStorage.getItem("ryn.agent.selected")).toBeNull();
  await writeDraft(user, "Keep the empty new chat's draft");
  await user.click(screen.getByRole("button", { name: "Other conversation" }));
  await screen.findByText("Independent conversation reply");
  await writeDraft(user, "Keep the other conversation's draft");

  const accepted = task({ revision: 1001, control_revision: 8, messages: [...original.messages, { role: "user", content: message, ui: { kind: "steer", text: message } }] });
  backend.setTasks([accepted, other]);
  await act(async () => { response.resolve(accepted); await response.promise; });
  expect(screen.getByText("Independent conversation reply")).toBeVisible();
  expect(screen.getByRole("textbox")).toHaveValue("Keep the other conversation's draft");
  expect(bubbles(container, message)).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "Original conversation" }));
  await waitFor(() => expect(bubbles(container, message)).toHaveLength(1));
  await user.click(newTaskButton());
  expect(screen.getByRole("textbox")).toHaveValue("Keep the empty new chat's draft");
});

it("discovers an uncertain create without leaving a deliberately chosen empty project or moving its draft", async () => {
  const backend = api();
  const response = deferred<AgentTask>();
  const create = vi.spyOn(rynAgent, "create").mockImplementation(() => response.promise);
  const user = userEvent.setup();
  const { container } = await open();
  const message = "A request whose acknowledgement is lost";
  await writeDraft(user, message);
  await user.click(screen.getByRole("button", { name: "Send task" }));
  await act(async () => { response.reject(new TypeError("Lost acknowledgement")); });
  await screen.findByRole("button", { name: "Recover task request" });

  expect(screen.getByRole("button", { name: "New task in Beta project" })).toBeEnabled();
  await user.click(screen.getByRole("button", { name: "New task in Beta project" }));
  await waitFor(() => expect(screen.getByRole("button", { name: /^Beta project ·/ })).toHaveAttribute("aria-pressed", "true"));
  await writeDraft(user, "Keep this project draft during discovery");
  const recovered = task({ client_request_id: String(create.mock.calls[0][2]), messages: [{ role: "user", content: message }] });
  backend.setTasks([recovered]);
  act(() => { window.dispatchEvent(new Event("focus")); });
  await waitFor(() => expect(screen.queryByRole("button", { name: "Recover task request" })).not.toBeInTheDocument());

  expect(screen.getByRole("button", { name: /^Beta project ·/ })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("textbox")).toHaveValue("Keep this project draft during discovery");
  expect(sessionStorage.getItem("ryn.agent.selected")).toBeNull();
  expect(bubbles(container, message)).toHaveLength(0);
});

it("keeps project navigation available during a create recovery and ignores its late acknowledgement", async () => {
  const backend = api();
  const first = deferred<AgentTask>();
  const recovery = deferred<AgentTask>();
  const create = vi.spyOn(rynAgent, "create").mockImplementationOnce(() => first.promise).mockImplementationOnce(() => recovery.promise);
  const user = userEvent.setup();
  const { container } = await open();
  const message = "Recover this earlier request";
  await writeDraft(user, message);
  await user.click(screen.getByRole("button", { name: "Send task" }));
  await act(async () => { first.reject(new TypeError("Lost acknowledgement")); });
  await user.click(await screen.findByRole("button", { name: "Recover task request" }));
  expect(create).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("button", { name: "New task in Beta project" })).toBeEnabled();
  await user.click(screen.getByRole("button", { name: "New task in Beta project" }));
  await waitFor(() => expect(screen.getByRole("button", { name: /^Beta project ·/ })).toHaveAttribute("aria-pressed", "true"));
  await writeDraft(user, "A draft independent of the recovering request");
  const accepted = task({ client_request_id: String(create.mock.calls[0][2]), messages: [{ role: "user", content: message }] });
  backend.setTasks([accepted]);
  await act(async () => { recovery.resolve(accepted); await recovery.promise; });

  expect(screen.getByRole("button", { name: /^Beta project ·/ })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("textbox")).toHaveValue("A draft independent of the recovering request");
  expect(sessionStorage.getItem("ryn.agent.selected")).toBeNull();
  expect(bubbles(container, message)).toHaveLength(0);
  expect(screen.queryByRole("button", { name: "Recover task request" })).not.toBeInTheDocument();
});

it.each(["initial request", "recovery retry"] as const)("does not revive an uncertain create when discovery confirms it before a late %s failure", async requestKind => {
  const backend = api();
  const response = deferred<AgentTask>();
  const recovery = deferred<AgentTask>();
  const create = vi.spyOn(rynAgent, "create").mockImplementationOnce(() => response.promise).mockImplementationOnce(() => recovery.promise);
  const user = userEvent.setup();
  const { container } = await open();
  const message = "The task is accepted before the POST fails";
  await writeDraft(user, message);
  await user.click(screen.getByRole("button", { name: "Send task" }));
  if (requestKind === "recovery retry") {
    await act(async () => { response.reject(new TypeError("Lost acknowledgement")); });
    await user.click(await screen.findByRole("button", { name: "Recover task request" }));
    expect(create).toHaveBeenCalledTimes(2);
  }
  await user.click(screen.getByRole("button", { name: "New task in Beta project" }));
  await waitFor(() => expect(screen.getByRole("button", { name: /^Beta project ·/ })).toHaveAttribute("aria-pressed", "true"));
  await writeDraft(user, "Keep the Beta draft while the old POST finishes");
  const accepted = task({ client_request_id: String(create.mock.calls[0][2]), messages: [{ role: "user", content: message }] });
  backend.setTasks([accepted]);
  act(() => { window.dispatchEvent(new Event("focus")); });
  await screen.findByRole("button", { name: "Original conversation" });
  await waitFor(() => expect(sessionStorage.getItem("ryn.agent.pending-create")).toBeNull());
  await act(async () => {
    if (requestKind === "recovery retry") recovery.reject(new AgentAPIError("agent_model_unavailable", 400));
    else response.reject(new TypeError("POST response disconnected after acceptance"));
  });

  expect(screen.queryByRole("button", { name: "Recover task request" })).not.toBeInTheDocument();
  expect(screen.queryByText("A message in another chat was not sent. Your text is saved here.")).not.toBeInTheDocument();
  expect(screen.queryByText("Waiting for confirmation · your message is saved")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^Beta project ·/ })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("textbox")).toHaveValue("Keep the Beta draft while the old POST finishes");
  expect(sessionStorage.getItem("ryn.agent.selected")).toBeNull();
  expect(bubbles(container, message)).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "Original conversation" }));
  await waitFor(() => expect(bubbles(container, message)).toHaveLength(1));
});

it("keeps a deliberate new task on the same draft key selected after an earlier create acknowledges", async () => {
  const backend = api();
  const response = deferred<AgentTask>();
  vi.spyOn(rynAgent, "create").mockImplementation(() => response.promise);
  const user = userEvent.setup();
  const { container } = await open();
  const message = "Start the first conversation";
  await writeDraft(user, message);
  await user.click(screen.getByRole("button", { name: "Send task" }));
  await user.click(newTaskButton());
  expect(bubbles(container, message)).toHaveLength(0);
  await writeDraft(user, "This belongs to a deliberately new chat");
  const accepted = task({ messages: [{ role: "user", content: message }] });
  backend.setTasks([accepted]);
  await act(async () => { response.resolve(accepted); await response.promise; });

  expect(sessionStorage.getItem("ryn.agent.selected")).toBeNull();
  expect(screen.getByRole("textbox")).toHaveValue("This belongs to a deliberately new chat");
  expect(bubbles(container, message)).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "Original conversation" }));
  await waitFor(() => expect(bubbles(container, message)).toHaveLength(1));
  expect(screen.getByRole("textbox")).toHaveValue("");
  await user.click(newTaskButton());
  expect(screen.getByRole("textbox")).toHaveValue("This belongs to a deliberately new chat");
});

it.each([false, true])("preserves a detached rejected create separately from a new chat with newer draft %s", async newerDraft => {
  api();
  const response = deferred<AgentTask>();
  vi.spyOn(rynAgent, "create").mockImplementation(() => response.promise);
  const user = userEvent.setup();
  const view = await open();
  const message = "Keep this rejected message available for recovery";
  const nextDraft = newerDraft ? "This is a different chat's draft" : "";
  await writeDraft(user, message);
  await user.click(screen.getByRole("button", { name: "Send task" }));
  await user.click(newTaskButton());
  if (newerDraft) await writeDraft(user, nextDraft);
  await act(async () => { response.reject(new AgentAPIError("agent_model_unavailable", 400)); });

  const disclosure = await screen.findByText("A message in another chat was not sent. Your text is saved here.");
  expect(sessionStorage.getItem("ryn.agent.selected")).toBeNull();
  expect(screen.getByRole("textbox")).toHaveValue(nextDraft);
  expect(screen.queryByText("Sending message…")).not.toBeInTheDocument();
  expect(bubbles(view.container, message)).toHaveLength(0);
  await user.click(disclosure);
  expect(screen.getByText(message)).toBeVisible();
  expect(screen.queryByRole("button", { name: "Recover task request" })).not.toBeInTheDocument();

  // A reload must retain the saved text without reviving the rejected send.
  view.unmount();
  const reloaded = await open();
  await user.click(screen.getByText("A message in another chat was not sent. Your text is saved here."));
  expect(screen.getByText(message)).toBeVisible();
  expect(screen.getByRole("textbox")).toHaveValue(nextDraft);
  expect(bubbles(reloaded.container, message)).toHaveLength(0);
  expect(screen.queryByRole("button", { name: "Recover task request" })).not.toBeInTheDocument();
});
