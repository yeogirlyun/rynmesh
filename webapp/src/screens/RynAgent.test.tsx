import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { rynAgent, type AgentProject, type AgentSettings, type AgentTask } from "../domain/rynAgent";
import { agentTaskCache } from "../domain/agentWorkspaceCache";
import { PersonalShell } from "../personal/Shell";
import RynAgent from "./RynAgent";
import i18n from "../i18n";

vi.mock("../personal/model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }),
}));

function task(overrides: Partial<AgentTask> = {}): AgentTask {
  return { id: "task-1", title: "Inspect devices", model_id: "verified-model", status: "running", revision: 1,
    created_at: 1, updated_at: 1, messages: [{ role: "user", content: "Inspect devices" }], events: [],
    pending_approval: null, steps: 1, result: "", error: null, limits: {}, permission_mode: "confirm_changes", allowed_write_tools: [], ...overrides };
}
function controlled(overrides: Partial<AgentTask> = {}): AgentTask {
  return task({ schema_version: 2, control_revision: 7, event_cursor: 1000, revision: 1000, run_state: "running",
    goal: { revision: 2, kind: "action", original_message_id: "owner-1", text: "Inspect devices", constraints: [] },
    allowed_actions: Object.fromEntries(["pause", "cancel", "steer", "information"].map(action => [action, { enabled: true, reason: null, requires_control_revision: true }])), ...overrides });
}
function api(enabled = true, initialTasks: AgentTask[] = []) {
  let settings: AgentSettings = { enabled, model_id: "verified-model", permission_mode: "confirm_changes", allowed_write_tools: [], max_steps: 16, max_seconds: 180, max_context_chars: 60000, max_output_chars: 20000 };
  let tasks = initialTasks;
  const creates = vi.fn();
  const updates = vi.fn();
  const actions = vi.fn();
  let createTask = task();
  server.use(
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json(settings)),
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [], create_scope_supported: false })),
    http.put("*/api/local/agent/settings", async ({ request }) => {
      const change = await request.json() as Partial<AgentSettings>;
      updates(change); settings = { ...settings, ...change };
      if (!settings.enabled) tasks = tasks.map(item => ["running", "queued", "waiting_approval"].includes(item.status) ? { ...item, status: "paused", revision: item.revision + 1 } : item);
      return HttpResponse.json(settings);
    }),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [
      { id: "verified-model", name: "Verified model", source: "local_api", available: true, agent_supported: true, tool_support: "supported" },
      { id: "unknown-model", name: "Unverified model", source: "local_api", available: true, agent_supported: false, tool_support: "unknown", description: "Tool support unverified" },
      { id: "offline-model", name: "Offline model", source: "peer", available: false, agent_supported: true },
    ] })),
    http.get("*/api/local/agent/tools", () => HttpResponse.json({ tools: [
      { name: "inspect_devices", title: "Inspect devices", effect: "read" },
      { name: "set_model_sharing", title: "Manage model sharing", effect: "write" },
    ] })),
    http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks })),
    http.get("*/api/local/agent/tasks/:id/artifacts", () => HttpResponse.json({ artifacts: [] })),
    http.get("*/api/local/agent/tasks/:id", ({ params }) => HttpResponse.json(tasks.find(item => item.id === params.id))),
    http.post("*/api/local/agent/tasks", async ({ request }) => {
      const body = await request.json() as { message: string; model_id: string; client_request_id: string };
      creates(body); const next = { ...createTask, client_request_id: body.client_request_id, messages: [{ role: "user", content: body.message }, ...createTask.messages.filter(message => message.role !== "user")] };
      tasks = [next, ...tasks]; return HttpResponse.json(next);
    }),
    http.post("*/api/local/agent/tasks/:id/actions", async ({ params, request }) => {
      const body = await request.json() as { action: string; expected_revision?: number; expected_control_revision?: number; approval_id?: string };
      actions(body); const current = tasks.find(item => item.id === params.id)!;
      if (current.allowed_actions ? body.expected_control_revision !== current.control_revision : body.expected_revision !== current.revision) return HttpResponse.json({ detail: "agent_revision_conflict" }, { status: 409 });
      const statuses = { pause: "paused", resume: current.pending_approval ? "waiting_approval" : "running", cancel: "cancelled", approve: "completed", deny: "running", answer: "running" } as const;
      const status = statuses[body.action as keyof typeof statuses];
      const next = { ...current, status, ...(current.run_state ? { run_state: status === "cancelled" || status === "completed" ? "ended" as const : status, control_revision: (current.control_revision || 0) + 1 } : {}), revision: current.revision + 1, ...(body.action === "approve" ? { result: "Sharing change applied", pending_approval: null } : {}), ...(body.action === "answer" ? { pending_input: null } : {}) };
      tasks = tasks.map(item => item.id === next.id ? next : item); return HttpResponse.json(next);
    }),
  );
  return { creates, updates, actions, setCreate: (next: AgentTask) => { createTask = next; }, setSettings: (next: Partial<AgentSettings>) => { settings = { ...settings, ...next }; }, setTasks: (next: AgentTask[]) => { tasks = next; } };
}
function mount(shell = false) {
  return render(<MemoryRouter initialEntries={["/agent"]}>{shell ? <PersonalShell><RynAgent /></PersonalShell> : <RynAgent />}</MemoryRouter>);
}

it("renders live text between durable checkpoints without waiting for the old 1.2 second poll", async () => {
  const current = task({ events: [{ seq: 1, type: "model_started", at: 1, step: 1, context_epoch: 0 }] });
  api(true, [current]);
  let liveText = "";
  server.use(http.get("*/api/local/agent/tasks/task-1", ({ request }) => HttpResponse.json(new URL(request.url).searchParams.has("since_revision")
    ? { id: current.id, revision: current.revision, unchanged: true, streaming: { step: 1, context_epoch: 0, text: liveText } }
    : current)));
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await screen.findByRole("button", { name: "Pause" });
  liveText = "我先检查磁盘的可用空间。";
  await screen.findByText(liveText, {}, { timeout: 900 });
  liveText += "接下来查看大文件。";
  await screen.findByText(liveText, {}, { timeout: 900 });
  expect(screen.getAllByText(liveText)).toHaveLength(1);
});

it("wakes an idle conversation immediately after an accepted follow-up", async () => {
  const original = controlled({ status: "completed", run_state: "ended", allowed_actions: { information: { enabled: true, reason: null, requires_control_revision: true } } });
  const backend = api(true, [original]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  const next = controlled({ revision: original.revision + 1, control_revision: 8, messages: [...original.messages, { role: "user", content: "Check again" }], events: [{ seq: 2, type: "model_started", at: 2, step: 1, context_epoch: 0 }] });
  vi.spyOn(rynAgent, "control").mockImplementationOnce(async () => { backend.setTasks([next]); return next; });
  await user.type(screen.getByRole("textbox"), "Check again");
  await user.click(screen.getByRole("button", { name: "Send information" }));
  await screen.findByRole("button", { name: "Pause" });
  backend.setTasks([{ ...next, streaming: { step: 1, context_epoch: 0, text: "Follow-up text arrived promptly" } }]);
  await screen.findByText("Follow-up text arrived promptly", {}, { timeout: 900 });
});

it("keeps multiple project conversations grouped and restores a separate new draft for each project", async () => {
  const projects: AgentProject[] = ["Alpha", "Beta"].map(name => ({ id: name.toLowerCase(), name, path: `D:\\code\\${name}`, generation: 1, entry_files: [], manifests: [], directories: [] }));
  const alphaTask = task({ id: "alpha-task", title: "Alpha history", project: projects[0], status: "completed", messages: [{ role: "assistant", content: "Existing Alpha conversation" }] });
  const betaTask = task({ id: "beta-task", title: "Beta history", project: projects[1], status: "completed", messages: [{ role: "assistant", content: "Existing Beta conversation" }] });
  api(true, [alphaTask, betaTask]);
  const selectProject = vi.fn();
  server.use(
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects })),
    http.post("*/api/local/agent/projects/selection", async ({ request }) => { const body = await request.json(); selectProject(body); return HttpResponse.json(body); }),
  );
  const user = userEvent.setup(); mount();
  const sidebar = within(await screen.findByRole("region", { name: "Projects" }));
  await user.click(await sidebar.findByRole("button", { name: /^Alpha ·/ }));
  await screen.findByRole("heading", { name: "What would you like to build in D:\\code\\Alpha?" });
  await user.type(screen.getByRole("textbox"), "Alpha draft");
  await user.click(sidebar.getByRole("button", { name: /^Beta ·/ }));
  await screen.findByRole("heading", { name: "What would you like to build in D:\\code\\Beta?" });
  expect(screen.getByRole("textbox")).toHaveValue("");
  await user.type(screen.getByRole("textbox"), "Beta draft");
  await user.click(sidebar.getByRole("button", { name: /^Alpha ·/ }));
  await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("Alpha draft"));
  await user.click(sidebar.getByRole("button", { name: "Beta history" }));
  await screen.findByText("Existing Beta conversation");
  expect(screen.getByRole("textbox")).toHaveValue("");
  expect(screen.getByRole("button", { name: "D:\\code\\Beta" })).toBeVisible();
  await user.click(within(screen.getByRole("complementary", { name: "Task list" })).getByRole("button", { name: "New task" }));
  await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("Beta draft"));
  expect(sidebar.getByRole("button", { name: /^Beta ·/ })).toHaveAttribute("aria-pressed", "true");
  expect(selectProject).toHaveBeenCalledWith({ project_id: "alpha" });
});

it("submits the selected project ID and retains its task under that project after creation", async () => {
  const project: AgentProject = { id: "alpha", name: "Alpha", path: "D:\\code\\Alpha", generation: 1, entry_files: [], manifests: [], directories: [] };
  const backend = api();
  backend.setCreate(task({ title: "New Alpha task", project }));
  server.use(
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [project] })),
    http.post("*/api/local/agent/projects/selection", () => HttpResponse.json({ selected_project_id: project.id })),
  );
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: /^Alpha ·/ }));
  await screen.findByRole("heading", { name: "What would you like to build in D:\\code\\Alpha?" });
  await user.type(screen.getByRole("textbox"), "Inspect the Alpha project");
  await user.click(screen.getByRole("button", { name: "Send task" }));
  await waitFor(() => expect(backend.creates).toHaveBeenCalledWith(expect.objectContaining({ project_id: "alpha", message: "Inspect the Alpha project" })));
  const sidebar = within(screen.getByRole("region", { name: "Projects" }));
  expect(await sidebar.findByRole("button", { name: "New Alpha task" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "D:\\code\\Alpha" })).toBeVisible();
});

it("keeps an explicit general draft when reopening with an older server project selection", async () => {
  const project: AgentProject = { id: "alpha", name: "Alpha", path: "D:\\code\\Alpha", generation: 1, entry_files: [], manifests: [], directories: [] };
  api();
  const selections = vi.fn();
  server.use(
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [project], selected_project_id: "alpha" })),
    http.post("*/api/local/agent/projects/selection", async ({ request }) => { const body = await request.json(); selections(body); return HttpResponse.json(body); }),
  );
  const user = userEvent.setup(); const first = mount();
  await screen.findByRole("heading", { name: "What would you like to build in D:\\code\\Alpha?" });
  await user.click(screen.getByRole("button", { name: "New task without a project" }));
  await screen.findByRole("heading", { name: "What would you like me to do?" });
  await user.type(screen.getByRole("textbox"), "General task draft");
  await waitFor(() => expect(selections).toHaveBeenCalledWith({ project_id: "" }));
  first.unmount(); mount();
  await screen.findByRole("button", { name: /^Alpha ·/ });
  await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("General task draft"));
  expect(screen.getByRole("heading", { name: "What would you like me to do?" })).toBeVisible();
  expect(screen.getByRole("button", { name: /^Alpha ·/ })).toHaveAttribute("aria-pressed", "false");
});

it.each(["unknown", "unsupported"] as const)("sends a configured CLI model directly with %s diagnostic evidence and no verification request", async tool_support => {
  await i18n.changeLanguage("en");
  const backend = api();
  const probes = vi.fn();
  server.use(
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [
      { id: "local/codex-cli", name: "Codex CLI · CLI default", source: "local_service", available: true, agent_supported: true },
      { id: "local/codex-cli/model/model-a", name: "Model A", source: "local_service", source_name: "Codex", cli_source: "codex_cli", cli_model: "model-a", service_id: "codex-cli", available: true, agent_supported: true, tool_support },
    ] })),
    http.post("*/api/local/llm/services/codex-cli/tools/test", () => { probes(); return HttpResponse.json({ tool_support: "supported" }); }),
  );
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("combobox", { name: /^Model$/ }));
  await user.click(screen.getByRole("option", { name: /^Model A/ }));
  expect(screen.queryByRole("button", { name: "Verify and use this model" })).not.toBeInTheDocument();
  expect(screen.queryByText("Needs verification")).not.toBeInTheDocument();
  await user.type(screen.getByRole("textbox"), "Hello");
  await user.click(screen.getByRole("button", { name: "Send task" }));
  await waitFor(() => expect(backend.creates).toHaveBeenCalledWith(expect.objectContaining({ model_id: "local/codex-cli/model/model-a" })));
  expect(probes).not.toHaveBeenCalled();
});

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  agentTaskCache().clear();
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value() { this.setAttribute("open", ""); } });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value() { this.removeAttribute("open"); } });
});

it("can select autonomous workstation execution and retain it in the workspace cache", async () => {
  await i18n.changeLanguage("en");
  const backend = api();
  const user = userEvent.setup(); mount();
  await screen.findByRole("heading", { name: "What would you like me to do?" });
  await user.click(screen.getByLabelText("Tool permissions", { selector: "summary" }));
  await user.click(screen.getByRole("menuitemradio", { name: "Work autonomously" }));
  await waitFor(() => expect(backend.updates).toHaveBeenCalledWith({ permission_mode: "workspace" }));
  await waitFor(() => expect(screen.getByText("Full access", { selector: ".ra-composer-permissions summary span" })).toBeVisible());
});

it("shows a normal completed answer without an independent-assessment warning card", async () => {
  await i18n.changeLanguage("en");
  server.use(http.get("*/api/local/agent/tasks/:id/artifacts", () => HttpResponse.json({ artifacts: [] })));
  api(true, [task({ status: "completed", run_state: "ended", result: "Balance: CNY 123.45",
    messages: [{ role: "assistant", content: "Balance: CNY 123.45" }],
    outcome: { assessment_requested: false, status: "unverified", goal_revision: 1, summary: "Balance: CNY 123.45", criteria_results: [], evidence_refs: [], uncertainties: [] } })]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  expect(await screen.findByText("Balance: CNY 123.45", { selector: "p" })).toBeVisible();
  await user.click(screen.getByLabelText("Chat actions"));
  expect(screen.queryByRole("region", { name: "Goal outcome" })).not.toBeInTheDocument();
});

it("reopens with cached configuration and sends while startup checks are still pending", async () => {
  const backend = api();
  const first = mount();
  await screen.findByRole("heading", { name: "What would you like me to do?" });
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("verified-model"));
  await waitFor(() => expect(localStorage.length).toBeGreaterThan(0));
  first.unmount();
  // These checks never settle during the assertion; usability must come from storage.
  const settings = vi.spyOn(rynAgent, "settings").mockImplementation(() => new Promise(() => {}));
  const models = vi.spyOn(rynAgent, "models").mockImplementation(() => new Promise(() => {}));
  const tasks = vi.spyOn(rynAgent, "tasks").mockImplementation(() => new Promise(() => {}));
  try {
    const user = userEvent.setup(); mount();
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("verified-model");
    expect(screen.getByRole("textbox")).toBeEnabled();
    await user.type(screen.getByRole("textbox"), "Use the saved model immediately");
    expect(screen.getByRole("button", { name: "Send task" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Send task" }));
    await waitFor(() => expect(backend.creates).toHaveBeenCalledWith(expect.objectContaining({ model_id: "verified-model" })));
  } finally { settings.mockRestore(); models.mockRestore(); tasks.mockRestore(); }
});

it("loads settings and task history independently of a pending model catalog", async () => {
  api(true, [controlled({ messages: [{ role: "assistant", content: "History without waiting for models" }] })]);
  const models = vi.spyOn(rynAgent, "models").mockImplementation(() => new Promise(() => {}));
  try {
    const user = userEvent.setup(); mount();
    await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
    expect(await screen.findByText("History without waiting for models")).toBeVisible();
    expect(screen.getByRole("textbox")).toBeEnabled();
    await user.type(screen.getByRole("textbox"), "Additional requirements");
    expect(screen.getByRole("button", { name: "Send requirements update" })).toBeEnabled();
  } finally { models.mockRestore(); }
});

it("starts a new draft and restores a visited conversation without checking models again", async () => {
  api(true, [controlled({ messages: [{ role: "assistant", content: "Cached conversation" }] })]);
  const modelRequests = vi.spyOn(rynAgent, "models");
  const user = userEvent.setup(); mount();
  try {
    await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
    await screen.findByText("Cached conversation");
    await user.type(screen.getByRole("textbox"), "Conversation draft");
    const count = modelRequests.mock.calls.length;
    await user.click(screen.getAllByRole("button", { name: "New task" })[0]);
    await user.type(screen.getByRole("textbox"), "New draft");
    expect(screen.getByRole("button", { name: "Send task" })).toBeEnabled();
    expect(modelRequests).toHaveBeenCalledTimes(count);
    const detail = vi.spyOn(rynAgent, "task").mockImplementation(() => new Promise(() => {}));
    try {
      await user.click(screen.getByRole("button", { name: "Inspect devices" }));
      expect(screen.getByText("Cached conversation")).toBeVisible();
      expect(screen.getByRole("textbox")).toBeEnabled();
      expect(screen.getByRole("textbox")).toHaveValue("Conversation draft");
      expect(screen.getByRole("button", { name: "Send requirements update" })).toBeEnabled();
      expect(modelRequests).toHaveBeenCalledTimes(count);
    } finally { detail.mockRestore(); }
  } finally { modelRequests.mockRestore(); }
});

it("restores the selected conversation when reopening while all refresh requests are pending", async () => {
  api(true, [controlled({ messages: [{ role: "assistant", content: "Conversation survives navigation" }] })]);
  const user = userEvent.setup();
  const first = mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await screen.findByText("Conversation survives navigation");
  await user.type(screen.getByRole("textbox"), "Saved conversation draft");
  first.unmount();
  const requests = ["settings", "models", "tasks", "task"].map(method =>
    vi.spyOn(rynAgent, method as "settings").mockImplementation(() => new Promise(() => {})));
  try {
    mount();
    expect(screen.getByText("Conversation survives navigation")).toBeVisible();
    expect(screen.getByRole("textbox")).toBeEnabled();
    expect(screen.getByRole("textbox")).toHaveValue("Saved conversation draft");
    expect(screen.getByRole("button", { name: "Send requirements update" })).toBeEnabled();
  } finally { requests.forEach(request => request.mockRestore()); }
});

it("keeps cached models usable after refresh errors and reports actual send rejection", async () => {
  const backend = api();
  const user = userEvent.setup(); mount();
  await screen.findByRole("heading", { name: "What would you like me to do?" });
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("verified-model"));
  const settings = vi.spyOn(rynAgent, "settings").mockRejectedValue(new Error("Disconnected"));
  const models = vi.spyOn(rynAgent, "models").mockRejectedValue(new Error("Disconnected"));
  try {
    window.dispatchEvent(new Event("focus"));
    await waitFor(() => expect(models).toHaveBeenCalled());
    await user.type(screen.getByRole("textbox"), "Keep this message");
    expect(screen.getByRole("button", { name: "Send task" })).toBeEnabled();
    expect(screen.queryByText(/Could not load the model catalog/)).not.toBeInTheDocument();
    server.use(http.post("*/api/local/agent/tasks", () => HttpResponse.json({ detail: "agent_model_unavailable" }, { status: 400 })));
    await user.click(screen.getByRole("button", { name: "Send task" }));
    expect(await screen.findByText(/The selected model is unavailable/)).toBeVisible();
    expect(screen.getByRole("textbox")).toHaveValue("Keep this message");
    await waitFor(() => expect(screen.getByRole("button", { name: "Send task" })).toBeEnabled());
    expect(backend.creates).not.toHaveBeenCalled();
  } finally { settings.mockRestore(); models.mockRestore(); }
});

it("opens a confirmed created file while the task is still running without a final outcome", async () => {
  await i18n.changeLanguage("en");
  api(true, [controlled()]);
  const artifact = { id: "confirmed-write", name: "report.md", bytes: 19, sha256: "bound" };
  server.use(
    http.get("*/api/local/agent/tasks/task-1/artifacts", () => HttpResponse.json({ artifacts: [artifact] })),
    http.get("*/api/local/agent/tasks/task-1/artifacts/confirmed-write", () => HttpResponse.json({ ...artifact, text: "Saved during a run." })),
  );
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await user.click(await screen.findByRole("button", { name: "Open report.md" }));
  expect(await screen.findByText("Saved during a run.")).toBeVisible();
});

it("refreshes the saved default after a background tool receipt while retaining this task's model", async () => {
  await i18n.changeLanguage("en");
  const current = controlled();
  const backend = api(true, [current]);
  server.use(http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [
    { id: "verified-model", name: "Original model", source: "local_api", available: true, agent_supported: true },
    { id: "replacement-model", name: "Future model", source: "local_api", available: true, agent_supported: true },
  ] })));
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  backend.setSettings({ model_id: "replacement-model" });
  backend.setTasks([{ ...current, revision: 1001, event_cursor: 1001, events: [
    { seq: 1001, type: "tool_result", tool: "ryn_set_default_agent_model", at: 2, result: { ok: true } },
  ] }]);
  await waitFor(() => expect(document.querySelector(".ra-model-context")).toHaveTextContent(/This task's model.*Original model.*Default for new tasks.*Future model/), { timeout: 4000 });
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("verified-model");
  await user.click(screen.getAllByRole("button", { name: "New task" })[0]);
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("replacement-model"));
});

it("shows the exact future-task default target and keeps the current-task boundary explicit", async () => {
  await i18n.changeLanguage("zh-CN");
  const backend = api(true, [controlled({ status: "waiting_approval", run_state: "waiting_approval", allowed_actions: { approve: { enabled: true, reason: null, requires_control_revision: true }, deny: { enabled: true, reason: null, requires_control_revision: true } }, pending_approval: { id: "default-approval", tool: "ryn_set_default_agent_model", call_id: "default-call", arguments_sha256: "exact-default-hash", arguments: { model_id: "new-model", expected_settings_revision: 8 }, preview: { before: { model_id: "verified-model" }, after: { model_id: "new-model" }, model: { id: "new-model", name: "新任务模型", source_name: "本机测试来源", peer_id: "provider-1" }, applies_to: "future_tasks_only" } } })]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  const card = screen.getByRole("region", { name: "变更待确认" });
  expect(within(card).getByText("更改新任务默认模型")).toBeVisible();
  expect(within(card).getByText("new-model")).toBeVisible();
  expect(within(card).getByText("本机测试来源")).toBeVisible();
  expect(within(card).getByText("provider-1")).toBeVisible();
  expect(within(card).getByText(/当前任务继续使用原模型和资料权限/)).toBeVisible();
  expect(backend.actions).not.toHaveBeenCalled();
});

it("shows trusted verification feedback as a collapsed localized system record, not a model reply", async () => {
  await i18n.changeLanguage("zh-CN");
  const feedback = 'Server verification feedback (read-only evidence): {"uncovered_requirements":["<img src=x onerror=alert(1)>"]}';
  api(true, [task({ status: "completed", messages: [
    { role: "assistant", content: feedback, ui: { kind: "verification_feedback" } },
    { role: "assistant", content: "Server verification feedback: ordinary model text" },
  ] })]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  const card = screen.getByRole("region", { name: "核验发现待解决项" });
  expect(within(card).getByText(/当前进展请以最新目标结果为准/)).toBeVisible();
  expect(within(card).getByText(feedback)).not.toBeVisible();
  expect(card.closest(".ra-message")?.querySelector(".ra-reply-label")).toBeNull();
  expect(screen.getByText("Server verification feedback: ordinary model text")).toBeVisible();
  await user.click(within(card).getByText("查看原始核验反馈"));
  expect(within(card).getByText(feedback)).toBeVisible();
  expect(card.querySelector("img")).toBeNull();
});

it("localizes only trusted secure-credential metadata without exposing provider wrapping", async () => {
  await i18n.changeLanguage("zh-CN");
  api(true, [task({ status: "completed", messages: [{ role: "user", content: "Source endpoint provider wrapper", ui: { kind: "source_credential", endpoint: "http://127.0.0.1:19004/v1", has_credential: true, fields: [{ label: "Credential", value: "Configured in secure storage" }] } }] })]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  const card = screen.getByRole("region", { name: "已配置到本机安全存储" });
  expect(within(card).getByText("服务地址")).toBeVisible();
  expect(within(card).getByText("http://127.0.0.1:19004/v1")).toBeVisible();
  expect(screen.queryByText("Source endpoint provider wrapper")).not.toBeInTheDocument();
  expect(screen.queryByText("Credential")).not.toBeInTheDocument();
});

it("shows exact tested source destination and per-model evidence before approval", async () => {
  api(true, [controlled({ status: "waiting_approval", run_state: "waiting_approval", allowed_actions: { approve: { enabled: true, reason: null, requires_control_revision: true }, deny: { enabled: true, reason: null, requires_control_revision: true } }, pending_approval: { id: "source-approval", call_id: "call-source", tool: "ryn_commit_source_draft", arguments: { draft_id: "draft-source", expected_draft_version: 1 }, arguments_sha256: "source-fingerprint", preview: { source_id: "", has_credential: true, configuration: { name: "Test source", base_url: "http://127.0.0.1:19004/v1", models: ["alpha", "beta"] }, test: { probe_requests: 6, models: [{ model_id: "alpha", chat: "passed", tool_support: "supported" }, { model_id: "beta", chat: "passed", tool_support: "unknown" }] } } } })]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  const card = screen.getByRole("region", { name: "Change awaiting approval" });
  expect(within(card).getByText("Save tested source")).toBeVisible();
  expect(within(card).getByText("Test source")).toBeVisible();
  expect(within(card).getByText("http://127.0.0.1:19004/v1")).toBeVisible();
  expect(within(card).getByText(/alpha.*Chat: Passed.*Tools: Passed/)).toBeVisible();
  expect(within(card).getByText(/beta.*Chat: Passed.*Tools: Not recorded/)).toBeVisible();
  expect(within(card).getByText(/new models will be private/)).toBeVisible();
  expect(within(card).getByText(/used 6 test requests/)).toBeVisible();
  await user.click(within(card).getByText("Exact request details"));
  expect(within(card).getByText("source-fingerprint")).toBeVisible();
});

it("retains the original model attribution for historical replies after switching", async () => {
  const original = controlled({ status: "paused", run_state: "paused", messages: [{ role: "assistant", content: "Reply before switch", context_epoch: 0 }] });
  const backend = api(true, [original]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  expect(screen.getByText("Reply before switch").closest(".ra-message")).toHaveTextContent("Verified model");
  backend.setTasks([{ ...original, model_id: "unknown-model", messages: [...original.messages,
    { role: "assistant", content: "Reply after switch", context_epoch: 1 }, { role: "assistant", content: "Legacy reply without epoch" }],
    events: [{ seq: 1001, type: "model_changed", at: 2, context_epoch: 1, previous: { model_id: "verified-model", context_epoch: 0 }, destination: { catalog_model_id: "unknown-model" } }] }]);
  window.dispatchEvent(new Event("focus"));
  await screen.findByText("Reply after switch");
  expect(screen.getByText("Reply before switch").closest(".ra-message")).toHaveTextContent("Verified model");
  expect(screen.getByText("Reply before switch").closest(".ra-message")).not.toHaveTextContent("Unverified model");
  expect(screen.getByText("Reply after switch").closest(".ra-message")).toHaveTextContent("Unverified model");
  expect(screen.getByText("Legacy reply without epoch").closest(".ra-message")).toHaveTextContent("Model not recorded for this historical reply");
});

it("replaces the selected scope panel with exactly one new-task scope panel", async () => {
  api(true, [controlled({ allowed_actions: { scope: { enabled: true, reason: null, requires_control_revision: true } } })]);
  server.use(
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [], create_scope_supported: true })),
  );
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  expect(screen.getAllByText("Task data access")).toHaveLength(1);
  await user.click(screen.getAllByRole("button", { name: "New task" })[0]);
  await waitFor(() => expect(screen.getAllByText("Task data access")).toHaveLength(1));
  expect(screen.getByRole("heading", { name: "What would you like me to do?" })).toBeVisible();
});

it("moves an archived task out of active history, keeps its detail readable and restores without resuming", async () => {
  api(true);
  let record = controlled({ archived: false, status: "paused", run_state: "paused", allowed_actions: { archive: { enabled: true, reason: null, requires_control_revision: true }, delete: { enabled: true, reason: null, requires_control_revision: true }, resume: { enabled: true, reason: null, requires_control_revision: true } } });
  server.use(
    http.get("*/api/local/agent/tasks", ({ request }) => HttpResponse.json({ tasks: record.archived === (new URL(request.url).searchParams.get("archived") === "true") ? [record] : [] })),
    http.get("*/api/local/agent/tasks/task-1", () => HttpResponse.json(record)),
    http.post("*/api/local/agent/tasks/task-1/archive", () => { record = { ...record, archived: true, revision: record.revision + 1, control_revision: 8, allowed_actions: { restore: { enabled: true, reason: null, requires_control_revision: true }, delete: { enabled: true, reason: null, requires_control_revision: true } } }; return HttpResponse.json(record); }),
    http.post("*/api/local/agent/tasks/task-1/restore", () => { record = { ...record, archived: false, revision: record.revision + 1, control_revision: 9, allowed_actions: { archive: { enabled: true, reason: null, requires_control_revision: true }, resume: { enabled: true, reason: null, requires_control_revision: true } } }; return HttpResponse.json(record); }),
  );
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await user.click(await screen.findByRole("button", { name: /^Archive task$/ }));
  await screen.findByText(/This task is archived/);
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Inspect devices" })).not.toBeInTheDocument();
  await user.click(screen.getByText("Archived tasks"));
  const archivedRows = within(screen.getByText("Archived tasks").closest("details")!);
  await archivedRows.findByRole("button", { name: "Inspect devices" });
  await user.click(screen.getByRole("button", { name: "Restore task" }));
  await screen.findByRole("button", { name: "Resume" });
  expect(screen.getByRole("status")).toHaveTextContent("Paused");
  expect(screen.getByRole("textbox")).toBeVisible();
  expect(await screen.findByText("No archived tasks.")).toBeVisible();
});

it("presents trusted owner input metadata and translates fixed verification reasons without guessing model text", async () => {
  await i18n.changeLanguage("zh-CN");
  api(true, [task({ status: "completed", run_state: "ended", messages: [{ role: "user", content: "Provider wrapper should stay internal", ui: { kind: "input_answer", prompt: "Which model?", fields: [{ label: "模型", value: "acceptance-model" }] } }], outcome: { status: "unverified", goal_revision: 1, summary: "Verification did not establish the complete goal.", criteria_results: [], evidence_refs: [], uncertainties: ["agent_invalid_verification_response", "Keep actual unknown evidence text"] } })]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  expect(await screen.findByText("已补充信息")).toBeVisible();
  expect(screen.getByText("acceptance-model")).toBeVisible();
  expect(screen.queryByText("Provider wrapper should stay internal")).not.toBeInTheDocument();
  await user.click(screen.getByLabelText("聊天操作"));
  expect(screen.getByText("核验尚未确认整个任务目标已达成。")).toBeVisible();
  expect(screen.getByText("核验回复无法校验，任务目标仍待核验。")).toBeVisible();
  expect(screen.getByText("Keep actual unknown evidence text")).toBeVisible();
});

it.each([
  ["zh-CN", "已登记的检查通过，独立判断认为当前目标已覆盖。"],
  ["en", "Registered checks passed, and an independent assessment found the current goal covered."],
])("localizes the current runtime verified summary in %s", async (language, expectedSummary) => {
  await i18n.changeLanguage(language);
  const summary = "Registered mechanical checks passed within their stated coverage; model semantic assessment found the current goal covered.";
  api(true, [task({ status: "completed", run_state: "ended", outcome: { status: "verified", goal_revision: 1, summary, criteria_results: [], evidence_refs: [], uncertainties: [] } })]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await user.click(screen.getByLabelText(language === "zh-CN" ? "聊天操作" : "Chat actions"));
  expect(await screen.findByText(expectedSummary)).toBeVisible();
  expect(screen.queryByText(summary)).not.toBeInTheDocument();
});

it("uses the stable control version for pause despite stream revisions", async () => {
  const backend = api(true, [controlled()]); const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await user.click(await screen.findByRole("button", { name: "Pause" }));
  await waitFor(() => expect(backend.actions).toHaveBeenCalledTimes(1));
  expect(backend.actions).toHaveBeenCalledWith({ action: "pause", expected_control_revision: 7, client_request_id: expect.stringMatching(/^[a-f0-9-]{36}$/) });
});

it("does not offer new controls without a server grant or a control version", async () => {
  api(true, [controlled({ status: "paused", run_state: "paused", control_revision: undefined, allowed_actions: { resume: { enabled: true, reason: null, requires_control_revision: true } } })]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await screen.findByText("Inspect devices", { selector: ".ra-message p" });
  expect(screen.queryByRole("button", { name: "Resume" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Pause" })).not.toBeInTheDocument();
  await user.type(screen.getByRole("textbox"), "More information");
  expect(screen.getByRole("button", { name: "Send information" })).toBeDisabled();
});

it("submits only the current bound question and requires its mandatory choice", async () => {
  const pending = controlled({ status: "waiting_input", run_state: "waiting_input", allowed_actions: { answer: { enabled: true, reason: null, requires_control_revision: true } },
    pending_input: { id: "question-2", kind: "choice", prompt: "Which device should I inspect?", goal_revision: 2, control_revision: 6, scope_revision: 1, business_revision: {}, created_at: 1,
      fields: [{ id: "device", label: "Device", type: "choice", required: true, options: [{ value: "desktop", label: "Desktop" }, { value: "phone", label: "Phone" }] }] } });
  const backend = api(true, [pending]); const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  const card = await screen.findByRole("region", { name: "Ryn needs your input" });
  expect(within(card).getByRole("button", { name: "Send answer" })).toBeDisabled();
  await user.selectOptions(within(card).getByRole("combobox"), "desktop");
  await user.click(within(card).getByRole("button", { name: "Send answer" }));
  await waitFor(() => expect(backend.actions).toHaveBeenCalledTimes(1));
  expect(backend.actions).toHaveBeenCalledWith({ action: "answer", pending_input_id: "question-2", expected_goal_revision: 2, expected_control_revision: 7, answers: { device: "desktop" }, client_request_id: expect.stringMatching(/^[a-f0-9-]{36}$/) });
  await waitFor(() => expect(screen.queryByRole("region", { name: "Ryn needs your input" })).not.toBeInTheDocument());
});

it("never collects a credential in an ordinary answer field", async () => {
  api(true, [controlled({ status: "waiting_input", run_state: "waiting_input", allowed_actions: { answer: { enabled: true, reason: null, requires_control_revision: true } },
    pending_input: { id: "secret", kind: "credential_ref", prompt: "Configure a model credential", goal_revision: 2, control_revision: 7, scope_revision: 1, business_revision: {}, created_at: 1, fields: [{ id: "key", label: "API key", type: "credential_ref", required: true }] } })]);
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  expect(screen.queryByRole("region", { name: "Secure credential form" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Enter API connection details" }));
  const card = await screen.findByRole("region", { name: "Secure credential form" });
  expect(within(card).getByLabelText("API Key")).toHaveAttribute("type", "password");
  expect(within(card).queryByRole("button", { name: "Send answer" })).not.toBeInTheDocument();
  expect(within(card).getByText(/never chat, model prompts or browser drafts/)).toBeVisible();
  expect(within(card).getByRole("button", { name: "Save securely and continue" })).toBeDisabled();
  await user.type(within(card).getByLabelText("API Key"), "unsaved-secret-123");
  await user.click(screen.getByRole("button", { name: "Close dialog" }));
  expect(screen.queryByRole("region", { name: "Secure credential form" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Enter API connection details" }));
  expect(screen.getByLabelText("API Key")).toHaveValue("");
});

it("retains the answer draft on a stale question conflict and never rebinds it automatically", async () => {
  const waiting = controlled({ status: "waiting_input", run_state: "waiting_input", allowed_actions: { answer: { enabled: true, reason: null, requires_control_revision: true } }, pending_input: { id: "question", kind: "information", prompt: "Which device?", goal_revision: 2, control_revision: 7, scope_revision: 1, business_revision: {}, created_at: 1, fields: [{ id: "device", label: "Device", type: "text", required: true }] } });
  const backend = api(true, [waiting]); const attempts = vi.fn();
  server.use(http.post("*/api/local/agent/tasks/:id/actions", async ({ request }) => { attempts(await request.json()); backend.setTasks([{ ...waiting, revision: 1001, control_revision: 8 }]); return HttpResponse.json({ detail: "agent_control_revision_conflict" }, { status: 409 }); }));
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  const card = await screen.findByRole("region", { name: "Ryn needs your input" });
  await user.type(within(card).getByRole("textbox", { name: "Device · Required" }), "Desktop");
  await user.click(within(card).getByRole("button", { name: "Send answer" }));
  await screen.findByText(/This question or goal changed/);
  expect(attempts).toHaveBeenCalledTimes(1);
  expect(within(card).getByRole("textbox")).toHaveValue("Desktop");
  expect(screen.queryByRole("button", { name: "Check original request" })).not.toBeInTheDocument();
});

it("preserves the exact steer request for explicit recovery without another execution", async () => {
  const running = controlled(); const backend = api(true, [running]); const bodies: unknown[] = [];
  server.use(http.post("*/api/local/agent/tasks/:id/messages", async ({ request }) => {
    bodies.push(await request.json());
    const changed = { ...running, control_revision: 8, revision: 1002, goal: { ...running.goal!, revision: 3 }, allowed_actions: {} };
    backend.setTasks([changed]);
    return bodies.length === 1 ? HttpResponse.error() : HttpResponse.json(changed);
  }));
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await user.type(screen.getByRole("textbox"), "Only inspect the desktop");
  await user.click(screen.getByRole("button", { name: "Send requirements update" }));
  const retry = await screen.findByRole("button", { name: "Check original request" });
  expect(bodies).toHaveLength(1);
  expect(bodies[0]).toEqual({ message: "Only inspect the desktop", intent: "steer", expected_goal_revision: 2, expected_control_revision: 7, client_request_id: expect.stringMatching(/^[a-f0-9-]{36}$/) });
  await user.click(retry);
  await waitFor(() => expect(bodies).toHaveLength(2));
  expect(bodies[1]).toEqual(bodies[0]);
  await waitFor(() => expect(screen.queryByRole("button", { name: "Check original request" })).not.toBeInTheDocument());
  expect(screen.getByRole("textbox")).toHaveValue("");
});

it("sends an ended task follow-up as information without rewriting its goal", async () => {
  const ended = controlled({ status: "completed", run_state: "ended", allowed_actions: { information: { enabled: true, reason: null, requires_control_revision: true } } });
  api(true, [ended]); const send = vi.fn();
  server.use(http.post("*/api/local/agent/tasks/:id/messages", async ({ request }) => { send(await request.json()); return HttpResponse.json(ended); }));
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await user.type(screen.getByRole("textbox"), "What does this device state mean?");
  await user.click(screen.getByRole("button", { name: "Send information" }));
  await waitFor(() => expect(send).toHaveBeenCalledWith(expect.objectContaining({ intent: "information", expected_goal_revision: 2, expected_control_revision: 7 })));
});

it("keeps cancellation available while a requirements update has an uncertain receipt", async () => {
  const running = controlled(); const backend = api(true, [running]);
  server.use(http.post("*/api/local/agent/tasks/:id/messages", () => HttpResponse.error()));
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await user.type(screen.getByRole("textbox"), "Inspect only the desktop");
  await user.click(screen.getByRole("button", { name: "Send requirements update" }));
  await screen.findByRole("button", { name: "Check original request" });
  const original = sessionStorage.getItem("ryn.agent.pending-control");
  await user.click(screen.getByRole("button", { name: "Cancel task" }));
  await waitFor(() => expect(backend.actions).toHaveBeenCalledWith(expect.objectContaining({ action: "cancel", expected_control_revision: 7 })));
  expect(sessionStorage.getItem("ryn.agent.pending-control")).toBe(original);
});

it("creates a data task with scope and destination consent in the same request before execution", async () => {
  const backend = api();
  server.use(
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ create_scope_supported: true, connections: [{ connection_id: "reports", name: "Reports drive", protocol: "smb", enabled: true, allow_ai: true, agent_supported: true, reason: "" }], supported_formats: [".txt"] })),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [{ id: "verified-model", name: "Verified model", source: "local_api", available: true, agent_supported: true, destination_revision: "api-version-1" }] })),
  );
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByText("Task data access"));
  await user.click(screen.getByRole("button", { name: "Add directory" }));
  await user.selectOptions(screen.getByRole("combobox", { name: "Connection 1" }), "reports");
  await user.type(screen.getByRole("textbox"), "Summarize the reports");
  expect(screen.getByRole("button", { name: "Send task" })).toBeDisabled();
  expect(backend.creates).not.toHaveBeenCalled();
  await user.click(screen.getByRole("checkbox", { name: "Allow this task to send selected directory contents to Verified model." }));
  await user.click(screen.getByRole("button", { name: "Send task" }));
  await waitFor(() => expect(backend.creates).toHaveBeenCalledTimes(1));
  expect(backend.creates).toHaveBeenCalledWith({ message: "Summarize the reports", model_id: "verified-model", response_language: "en", client_request_id: expect.stringMatching(/^[a-f0-9-]{36}$/), scope: { resources: [{ connection_id: "reports", root_path: "", recursive: false, actions: ["list", "read_ai"] }], allow_model_data: true }, expected_destination_revision: "api-version-1" });
});

it("starts disabled, persists enabling, and excludes unavailable or unsupported connections", async () => {
  const backend = api(false);
  const user = userEvent.setup();
  mount();
  await screen.findByRole("heading", { name: "Let Ryn take care of a task" });
  expect(screen.queryByRole("textbox", { name: "Tell Ryn what to do" })).not.toBeInTheDocument();
  expect(backend.creates).not.toHaveBeenCalled();
  await user.click(screen.getByRole("combobox", { name: "Model" }));
  expect(screen.getByRole("option", { name: /Unverified model/ })).toHaveAttribute("aria-disabled", "true");
  expect(screen.getByRole("option", { name: /Offline model/ })).toHaveAttribute("aria-disabled", "true");
  await user.keyboard("{Escape}");
  await user.click(screen.getAllByRole("button", { name: "Enable Ryn Agent" })[0]);
  await screen.findByRole("heading", { name: "What would you like me to do?" });
  expect(backend.updates).toHaveBeenCalledWith({ enabled: true, model_id: "verified-model" });
  await user.type(screen.getByRole("textbox"), "Inspect my devices");
  await user.click(screen.getByRole("button", { name: "Send task" }));
  await screen.findByText("Inspect my devices");
  expect(backend.creates).toHaveBeenCalledTimes(1);
  expect(backend.creates).toHaveBeenCalledWith(expect.objectContaining({ message: "Inspect my devices", model_id: "verified-model", client_request_id: expect.stringMatching(/^[a-f0-9-]{36}$/) }));
  expect(screen.queryByRole("region", { name: "Task plan" })).not.toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "Goal outcome" })).not.toBeInTheDocument();
});

it("presents server goal and plan without upgrading a finished model reply to verified", async () => {
  api(true, [task({ status: "completed", run_state: "ended", result: "The model says every goal is verified.",
    goal: { revision: 2, kind: "action", original_message_id: "owner-1", text: "Inspect sharing without changing it.", constraints: [{ id: "no-write", text: "Do not change sharing", source: "user", message_id: "owner-1", required: true }] },
    plan: { revision: 1, goal_revision: 2, steps: [{ id: "inspect", text: "Read the current sharing state", criterion_ids: ["goal"], depends_on: [], status: "done" }] },
    outcome: { status: "unverified", goal_revision: 2, summary: "", criteria_results: [], evidence_refs: [], uncertainties: ["No readback evidence yet"] },
  })]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await user.click(screen.getByLabelText("Chat actions"));
  const plan = await screen.findByRole("region", { name: "Task plan" });
  expect(within(plan).getByText("Read the current sharing state")).toBeVisible();
  expect(within(plan).getByText("Step finished")).toBeVisible();
  await user.click(screen.getByText("Task settings"));
  await user.click(screen.getByText("Task goal · Revision 2"));
  expect(screen.getByText("Do not change sharing")).toBeVisible();
  const outcome = screen.getByRole("region", { name: "Goal outcome" });
  expect(within(outcome).getByText(/Goal outcome · Not verified/)).toBeVisible();
  expect(within(outcome).getByText("No readback evidence yet")).toBeVisible();
  expect(within(outcome).queryByText(/server has verified/)).not.toBeInTheDocument();
});

it("keeps model setup available before enabling when no supported model exists", async () => {
  const backend = api(false);
  server.use(http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [] })));
  mount();
  await screen.findByRole("heading", { name: "Let Ryn take care of a task" });
  expect(screen.getByText(/No assistant model is available/)).toBeVisible();
  expect(screen.getByRole("link", { name: "Configure models" })).toHaveAttribute("href", "/services/sources");
  expect(screen.getByRole("button", { name: "Enable Ryn Agent" })).toBeDisabled();
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  expect(backend.creates).not.toHaveBeenCalled();
});

it("shows trusted friend sharing scope without inferring personal space from the model text", async () => {
  api(true, [task({ status: "waiting_approval", pending_approval: { id: "friend-share", tool: "ryn_set_model_sharing", call_id: "call", arguments: { model_id: "verified-model", enabled: true }, arguments_sha256: "exact", preview: { model: "Verified model", scope: "friends", before: { sharing: false }, after: { sharing: true }, space: { name: "Irrelevant personal space" } } }, messages: [{ role: "assistant", content: "Share this in personal space" }] })]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  const card = screen.getByRole("region", { name: "Change awaiting approval" });
  expect(within(card).getByText("Friend")).toBeVisible();
  expect(within(card).getByText(/Irrelevant personal space/)).not.toBeVisible();
});

it("shows the sole recipient and revoked grants for an atomic sharing approval", async () => {
  const backend = api(true, [task({ status: "waiting_approval", pending_approval: {
    id: "atomic-share", tool: "ryn_share_model_with_friend", call_id: "call", arguments: { model_id: "verified-model", relationship_id: "friend-b" }, arguments_sha256: "exact",
    preview: { model: "Verified model", before: { sharing: false, scope: "personal_space", friend_grants: [
      { relationship_id: "friend-a", peer_id: "old-peer-a", allowed: true, effective: false },
      { relationship_id: "friend-c", peer_id: "denied-peer-c", allowed: false },
    ] }, after: { sharing: true, scope: "friends", recipients: [{ name: "Node B", relationship_id: "friend-b", peer_id: "selected-peer-b" }] } },
  } })]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  const card = await screen.findByRole("region", { name: "Change awaiting approval" });
  expect(within(card).getByText("Only this friend will have access")).toBeVisible();
  expect(within(card).getByText("selected-peer-b")).toBeVisible();
  expect(within(card).getByText("Existing friend access to revoke")).toBeVisible();
  expect(within(card).getByText("old-peer-a")).toBeVisible();
  expect(within(card).queryByText("denied-peer-c")).not.toBeInTheDocument();
  expect(within(card).getByText(/One approval enables sharing/)).toBeVisible();
  await user.click(within(card).getByRole("button", { name: "Approve exact change" }));
  expect(backend.actions).toHaveBeenCalledWith({ action: "approve", expected_revision: 1, approval_id: "atomic-share" });
});

it("shows all saved friend recipients and the prior scope before enabling sharing", async () => {
  api(true, [task({ status: "waiting_approval", pending_approval: {
    id: "scope-change", tool: "ryn_set_model_sharing", call_id: "call", arguments: { model_id: "verified-model", enabled: true, scope: "friends" }, arguments_sha256: "exact",
    preview: { model: "Verified model", scope: "friends", before: { sharing: false, scope: "personal_space" }, after: { sharing: true, scope: "friends" },
      allowed_friend_grants: [{ relationship_id: "a", peer_id: "node-a", effective: true }, { relationship_id: "b", peer_id: "node-b", effective: false }] },
  } })]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  const card = screen.getByRole("region", { name: "Change awaiting approval" });
  expect(within(card).getByText("Previous scope: Personal space")).toBeVisible();
  expect(within(card).getByText("node-a")).toBeVisible();
  expect(within(card).getByText("node-b")).toBeVisible();
  expect(within(card).getByText(/Grant saved · relationship inactive/)).toBeVisible();
  expect(within(card).getByText(/Receiver connectivity is verified separately/)).toBeVisible();
});

it("shows exact pending arguments and preview, and approves only the current revision and approval id", async () => {
  const backend = api();
  const approval = { id: "approval-9", tool: "ryn_set_model_sharing", call_id: "call-1", arguments: { model_id: "verified-model", enabled: true }, arguments_sha256: "abc123", preview: { model: "Verified model", scope: "personal_space", before: { sharing: false }, after: { sharing: true }, space: { name: "My space" } } };
  backend.setCreate(task({ status: "waiting_approval", revision: 4, pending_approval: approval, messages: [{ role: "assistant", content: "Please review the sharing change." }], events: [{ seq: 1, type: "tool_result", at: 1, tool: "inspect_models", result: { note: "<script>bad()</script>", count: 2 } }] }));
  const user = userEvent.setup();
  mount();
  await screen.findByRole("heading", { name: "What would you like me to do?" });
  await user.type(screen.getByRole("textbox"), "Share the model");
  await user.click(screen.getByRole("button", { name: "Send task" }));
  const review = await screen.findByRole("region", { name: "Change awaiting approval" });
  expect(within(review).getByText("Only me")).toBeVisible();
  expect(within(review).getByText("Shared")).toBeVisible();
  expect(within(review).getByText("Personal space · My space")).toBeVisible();
  expect(within(review).getByText(/"enabled": true/)).not.toBeVisible();
  await user.click(within(review).getByText("Exact request details"));
  expect(within(review).getByText(/"enabled": true/)).toBeVisible();
  expect(screen.queryByText("Sharing change applied")).not.toBeInTheDocument();
  await user.click(screen.getByLabelText("Chat actions"));
  await user.click(screen.getByText("Execution details"));
  await user.click(screen.getByText("View result"));
  expect(screen.getByLabelText("Tool result")).toHaveTextContent("<script>bad()</script>");
  expect(document.querySelector("script")).toBeNull();
  await user.click(within(review).getByRole("button", { name: "Approve exact change" }));
  await screen.findByText("Sharing change applied");
  expect(backend.actions).toHaveBeenCalledWith({ action: "approve", expected_revision: 4, approval_id: "approval-9" });
  expect(screen.queryByRole("region", { name: "Change awaiting approval" })).not.toBeInTheDocument();
});

it("pauses, resumes and cancels with the latest task revision", async () => {
  const backend = api(true, [task()]);
  const user = userEvent.setup();
  mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await user.click(await screen.findByRole("button", { name: "Pause" }));
  await user.click(await screen.findByRole("button", { name: "Resume" }));
  await user.click(await screen.findByRole("button", { name: "Cancel task" }));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Cancelled"));
  expect(backend.actions.mock.calls.map(([body]) => body)).toEqual([
    { action: "pause", expected_revision: 1 }, { action: "resume", expected_revision: 2 }, { action: "cancel", expected_revision: 3 },
  ]);
  expect(screen.getByRole("button", { name: "Send follow-up" })).toBeDisabled();
});

it("collapses task list and main navigation independently while preserving input and the standalone route", async () => {
  api();
  const user = userEvent.setup();
  mount(true);
  await screen.findByRole("heading", { name: "What would you like me to do?" });
  await user.type(screen.getByRole("textbox"), "Keep this draft");
  const navigation = screen.getByRole("complementary", { name: "Main navigation" });
  const tasks = screen.getByRole("complementary", { name: "Task list" });
  expect(within(screen.getByRole("navigation", { name: "Primary" })).getByRole("link", { name: "Ryn Agent" })).toHaveAttribute("href", "/agent");
  expect(screen.queryByRole("navigation", { name: "Services" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Hide main navigation" }));
  expect(navigation).not.toBeVisible();
  expect(tasks).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Hide task list" }));
  expect(tasks).not.toBeVisible();
  await user.click(screen.getByRole("button", { name: "Show main navigation" }));
  expect(navigation).toBeVisible();
  expect(tasks).not.toBeVisible();
  await user.click(screen.getByRole("button", { name: "Show task list" }));
  expect(tasks).toBeVisible();
  expect(screen.getByRole("textbox")).toHaveValue("Keep this draft");
});

it("recovers an uncertain create after reload from its server request id without another POST", async () => {
  const id = "6ff9a8e9-6db3-45ee-a21d-f2b4ec07dfd6";
  const backend = api(true, [task({ client_request_id: id, status: "completed", result: "Actual inspected state", messages: [{ role: "user", content: "Inspect devices" }, { role: "assistant", content: "Actual inspected state" }] })]);
  sessionStorage.setItem("ryn.agent.pending-create", JSON.stringify({ client_request_id: id, message: "Inspect devices" }));
  sessionStorage.setItem("ryn.agent.drafts", JSON.stringify({ new: "Inspect devices" }));
  const view = mount();
  await screen.findByText("Actual inspected state");
  expect(screen.getAllByText("Actual inspected state")).toHaveLength(1);
  expect(backend.creates).not.toHaveBeenCalled();
  expect(sessionStorage.getItem("ryn.agent.pending-create")).toBeNull();
  view.unmount();
  mount();
  await screen.findByText("Actual inspected state");
  expect(screen.getAllByText("Actual inspected state")).toHaveLength(1);
  expect(backend.creates).not.toHaveBeenCalled();
  expect(screen.getAllByText("Inspect devices")).toHaveLength(3); // Header, task list and server message.
});

it("disables Agent without erasing tasks and saves only explicitly opted-in write tools", async () => {
  const backend = api(true, [task({ status: "completed", result: "Saved evidence" })]);
  const user = userEvent.setup();
  mount();
  await screen.findByRole("heading", { name: "What would you like me to do?" });
  await user.click(screen.getByRole("button", { name: "Agent settings" }));
  const dialog = screen.getByRole("dialog", { name: "Agent settings" });
  await screen.findByText("Manage model sharing");
  expect(within(dialog).queryByText("inspect_devices")).not.toBeInTheDocument();
  await user.click(within(dialog).getByRole("checkbox", { name: /Manage model sharing/ }));
  await user.type(within(dialog).getByRole("textbox", { name: "Working directory" }), "D:\\RynWorkspace");
  await user.click(within(dialog).getByRole("checkbox", { name: "Allow computer and browser operations" }));
  await user.click(within(dialog).getByRole("checkbox", { name: /Enable Agent on this device/ }));
  await user.click(within(dialog).getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(backend.updates).toHaveBeenCalledWith({ enabled: false, workspace_root: "D:\\RynWorkspace", desktop_access: true, allowed_write_tools: ["set_model_sharing"] });
  await user.click(screen.getByRole("button", { name: "Inspect devices" }));
  await screen.findByText("Saved evidence");
  expect(screen.getByRole("textbox")).toBeDisabled();
  expect(backend.creates).not.toHaveBeenCalled();
});

it("reports unsupported API honestly and never creates a task", async () => {
  const backend = api();
  server.use(http.get("*/api/local/agent/settings", () => HttpResponse.json({ detail: "not_found" }, { status: 404 })));
  mount();
  await screen.findByRole("alert");
  expect(screen.getAllByText(/Agent is unavailable on this node/).length).toBeGreaterThan(0);
  expect(screen.queryByRole("button", { name: "Send task" })).not.toBeInTheDocument();
  expect(backend.creates).not.toHaveBeenCalled();
});

it("resumes a safe restart interruption and keeps the task's recorded permission mode", async () => {
  const backend = api(true, [task({ status: "interrupted", error: "agent_node_restarted", permission_mode: "read_only" })]);
  const user = userEvent.setup();
  mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await screen.findByText(/The node restarted before this task finished/);
  expect(screen.getByText("Read only", { selector: ".ra-composer-permissions summary span" })).toBeVisible();
  await user.click(screen.getByLabelText("Tool permissions", { selector: "summary" }));
  expect(screen.getByRole("menuitemradio", { name: "Work autonomously" })).toBeDisabled();
  expect(screen.queryByText(/A change may have started without a confirmed result/)).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Resume" }));
  expect(backend.actions).toHaveBeenCalledWith({ action: "resume", expected_revision: 1 });
  await screen.findByRole("button", { name: "Pause" });
});

it("never offers resume when a dispatched write has an unknown outcome", async () => {
  const backend = api(true, [task({ status: "interrupted", error: "agent_write_outcome_unknown" })]);
  const user = userEvent.setup();
  mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await screen.findByText(/A change may have started without a confirmed result/);
  expect(screen.queryByRole("button", { name: "Resume" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Send follow-up" })).toBeDisabled();
  expect(backend.actions).not.toHaveBeenCalled();
});

it("keeps tasks in update-time order when an older task is selected and refreshed", async () => {
  api(true, [task({ id: "newer", title: "Recent task", updated_at: 10 }), task()]);
  const user = userEvent.setup();
  mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await screen.findByRole("button", { name: "Pause" });
  const list = within(screen.getByRole("complementary", { name: "Task list" }));
  expect(list.getAllByRole("button").filter(button => button.classList.contains("ra-task-item")).map(button => button.textContent)).toEqual(["Recent task", "Inspect devices"]);
  await user.click(screen.getByRole("button", { name: "Hide task list" }));
  await user.click(screen.getByRole("button", { name: "Show task list" }));
  expect(list.getAllByRole("button").filter(button => button.classList.contains("ra-task-item")).map(button => button.textContent)).toEqual(["Recent task", "Inspect devices"]);
});


it("keeps history and draft readable when only the model catalog fails, then refreshes on return", async () => {
  const backend = api(true, [task({ status: "completed", result: "Saved inspection evidence" })]);
  let available = false;
  server.use(http.get("*/api/local/agent/models", () => available
    ? HttpResponse.json({ models: [{ id: "new-default", name: "New verified default", source: "local_api", available: true, agent_supported: true }] })
    : HttpResponse.json({}, { status: 503 })));
  const user = userEvent.setup(); mount();
  await screen.findByText(/Could not load the model catalog/);
  await user.click(screen.getByRole("button", { name: "Inspect devices" }));
  await screen.findByText("Saved inspection evidence");
  await user.type(screen.getByRole("textbox"), "Keep my follow-up");
  expect(screen.getByRole("button", { name: "Send follow-up" })).toBeDisabled();
  available = true; backend.setSettings({ model_id: "new-default" });
  window.dispatchEvent(new Event("focus"));
  await waitFor(() => expect(screen.queryByText(/Could not load the model catalog/)).not.toBeInTheDocument());
  expect(screen.getByText(/This task's model: verified-model/)).toHaveTextContent("Default for new tasks: New verified default");
  expect(screen.getByRole("textbox")).toHaveValue("Keep my follow-up");
  await user.click(screen.getAllByRole("button", { name: "New task" })[0]);
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("new-default");
  expect(backend.creates).not.toHaveBeenCalled();
});

it("reads history even when settings cannot be read, and disables actions", async () => {
  api(true, [task({ status: "completed", result: "Readable history" })]);
  server.use(http.get("*/api/local/agent/settings", () => HttpResponse.json({}, { status: 503 })));
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await screen.findByText("Readable history");
  expect(screen.getByRole("textbox")).toBeDisabled();
  expect(screen.getByRole("button", { name: "Send follow-up" })).toBeDisabled();
});

it("distinguishes rejected calls from execution and explains receipts arriving after cancellation", async () => {
  api(true, [task({ status: "cancelled", events: [
    { seq: 1, type: "tool_result", at: 1, tool: "ryn_set_model_sharing", result: { error: "agent_permission_denied", not_executed: true, no_mutation: true } },
    { seq: 2, type: "cancelled", at: 2, operation_in_flight: true },
    { seq: 3, type: "tool_result", at: 3, tool: "ryn_set_model_sharing", result: { sharing: true } },
  ] })]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await screen.findByText(/Cancellation stops further work/);
  await user.click(screen.getByLabelText("Chat actions"));
  await user.click(screen.getByText("Execution details"));
  expect(screen.getByText(/Tool rejected · not executed/)).toBeVisible();
  expect(screen.getByText(/Tool result received/)).toBeVisible();
  expect(screen.getByText(/This receipt arrived after cancellation/)).toBeVisible();
  expect(screen.getAllByText("View result")).toHaveLength(2);
});

it("shows other tasks' server states without pausing them when switching or starting a draft", async () => {
  const backend = api(true, [task(), task({ id: "waiting", title: "Review sharing", status: "waiting_approval" })]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByLabelText("Chat actions"));
  await screen.findByText(/Switching views does not pause them/);
  await user.click(screen.getByRole("button", { name: /^Open background task: Inspect devices · Running/ }));
  await screen.findByRole("button", { name: "Pause" });
  await user.click(screen.getByRole("button", { name: "Hide task list" }));
  expect(screen.getByRole("button", { name: /^Open background task: Review sharing · Awaiting approval/ })).toBeVisible();
  await user.click(screen.getAllByRole("button", { name: "New task" })[0]);
  expect(screen.getByRole("button", { name: /^Open background task: Inspect devices · Running/ })).toBeVisible();
  expect(backend.actions).not.toHaveBeenCalled();
});


it.each(["pause", "cancel", "deny"] as const)("keeps %s available when only settings refresh fails", async kind => {
  const approval = { id: "approval-1", tool: "ryn_set_model_sharing", call_id: "call-1", arguments: { model_id: "verified-model", enabled: true }, arguments_sha256: "abc" };
  const backend = api(true, [task(kind === "deny" ? { status: "waiting_approval", pending_approval: approval } : {})]);
  server.use(http.get("*/api/local/agent/settings", () => HttpResponse.json({}, { status: 503 })));
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await user.click(await screen.findByRole("button", { name: ({ pause: "Pause", cancel: "Cancel task", deny: "Reject change" })[kind] }));
  await waitFor(() => expect(backend.actions).toHaveBeenCalledWith({ action: kind, expected_revision: 1, ...(kind === "deny" ? { approval_id: "approval-1" } : {}) }));
  expect(backend.updates).not.toHaveBeenCalled();
});

it("polls the selected task independently when the task index fails", async () => {
  const backend = api(true, [task()]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await screen.findByRole("button", { name: "Pause" });
  server.use(http.get("*/api/local/agent/tasks", () => HttpResponse.json({}, { status: 503 })));
  backend.setTasks([task({ status: "completed", revision: 2, result: "Fresh evidence despite index failure" })]);
  await screen.findByText("Fresh evidence despite index failure", {}, { timeout: 3500 });
  // The reply now arrives before the independent 1.2 s sidebar poll.
  expect(await screen.findByText(/Could not refresh the task list/, {}, { timeout: 3500 })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Pause" })).not.toBeInTheDocument();
});

it("updates the current reply while the task index request remains hung", async () => {
  const backend = api(true, [task()]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await screen.findByRole("button", { name: "Pause" });
  let release!: () => void;
  let started = false;
  const pending = new Promise<void>(resolve => { release = resolve; });
  server.use(http.get("*/api/local/agent/tasks", async () => {
    started = true; await pending; return HttpResponse.json({ tasks: [] });
  }));
  backend.setTasks([task({ status: "completed", revision: 2, result: "Reply arrived while index is pending" })]);
  try {
    await screen.findByText("Reply arrived while index is pending", {}, { timeout: 3500 });
    await waitFor(() => expect(started).toBe(true));
  } finally { release(); }
});

it("updates the task index while the selected detail request remains hung", async () => {
  const backend = api(true, [task()]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await screen.findByRole("button", { name: "Pause" });
  let release!: () => void;
  let started = false;
  const pending = new Promise<void>(resolve => { release = resolve; });
  server.use(http.get("*/api/local/agent/tasks/:id", async () => {
    started = true; await pending; return HttpResponse.json(task());
  }));
  backend.setTasks([task(), task({ id: "new-task", title: "New task while detail is pending", status: "completed" })]);
  try {
    await screen.findByRole("button", { name: "New task while detail is pending" }, { timeout: 3500 });
    await waitFor(() => expect(started).toBe(true));
  } finally { release(); }
});

it("recovers task reads after a node outage without losing the current evidence or draft", async () => {
  api(true, [task({ status: "completed", result: "Saved evidence before restart" })]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await screen.findByText("Saved evidence before restart");
  await user.type(screen.getByRole("textbox"), "Keep this draft during restart");
  let online = false;
  server.use(
    http.get("*/api/local/agent/tasks", () => online
      ? HttpResponse.json({ tasks: [task({ status: "completed", revision: 2 })] })
      : HttpResponse.json({ detail: "agent_unavailable" }, { status: 503 })),
    http.get("*/api/local/agent/tasks/:id", () => online
      ? HttpResponse.json(task({ status: "completed", revision: 2, result: "Current evidence after restart" }))
      : HttpResponse.json({ detail: "agent_unavailable" }, { status: 503 })),
  );
  window.dispatchEvent(new Event("focus"));
  await screen.findByText(/Could not refresh the task list/);
  await screen.findByText(/Could not refresh this task/);
  expect(screen.getByText("Saved evidence before restart")).toBeVisible();
  expect(screen.getByRole("textbox")).toHaveValue("Keep this draft during restart");
  online = true;
  await screen.findByText("Current evidence after restart", {}, { timeout: 4000 });
  await waitFor(() => expect(screen.queryByText(/Could not refresh the task list/)).not.toBeInTheDocument());
  expect(screen.queryByText(/Could not refresh this task/)).not.toBeInTheDocument();
  expect(screen.getByRole("textbox")).toHaveValue("Keep this draft during restart");
});

it("reports a selected-detail failure on focus without an unhandled rejection or losing evidence", async () => {
  api(true, [task({ status: "completed", result: "Preserved evidence" })]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await screen.findByText("Preserved evidence");
  await user.type(screen.getByRole("textbox"), "Preserved draft");
  server.use(http.get("*/api/local/agent/tasks/:id", () => HttpResponse.json({}, { status: 503 })));
  window.dispatchEvent(new Event("focus"));
  await screen.findByText(/Could not refresh this task/);
  expect(screen.getByText("Preserved evidence")).toBeVisible();
  expect(screen.getByRole("textbox")).toHaveValue("Preserved draft");
});

it("ignores an older focus refresh that resolves after a newer visibility refresh", async () => {
  api(); mount(); await screen.findByRole("heading", { name: "What would you like me to do?" });
  let releaseOld!: () => void;
  const oldPending = new Promise<void>(resolve => { releaseOld = resolve; });
  let notifyStarted!: () => void;
  const started = new Promise<void>(resolve => { notifyStarted = resolve; });
  let count = 0;
  server.use(
    http.get("*/api/local/agent/settings", async () => {
      const request = ++count;
      if (request === 1) { notifyStarted(); await oldPending; }
      return HttpResponse.json({ enabled: true, model_id: request === 1 ? "old-default" : "new-default", permission_mode: "confirm_changes" });
    }),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: ["old-default", "new-default"].map(id => ({ id, name: id, source: "local_api", available: true, agent_supported: true })) })),
  );
  window.dispatchEvent(new Event("focus")); await started;
  document.dispatchEvent(new Event("visibilitychange"));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("new-default"));
  releaseOld();
  // Wait for the older response to finish crossing fetch's asynchronous boundary.
  await new Promise(resolve => setTimeout(resolve, 30));
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("new-default");
  await userEvent.setup().click(screen.getByLabelText("Options", { selector: "summary" }));
  expect(screen.getByText(/Default for new tasks: new-default/)).toBeVisible();
});


it("shows public progress and keeps technical receipts and interim commentary collapsed", async () => {
  await i18n.changeLanguage("en");
  api(true, [task({ messages: [
    { role: "user", content: "Inspect devices" },
    { role: "assistant", content: "I will inspect the device configuration.", tool_calls: [{ id: "c1", function: { name: "inspect_devices", arguments: "{}" } }] },
    { role: "tool", content: "internal result", tool_call_id: "c1" },
    { role: "assistant", content: null, tool_calls: [{ id: "c2", function: { name: "inspect_devices", arguments: "{}" } }] },
    { role: "assistant", content: "The device is online." },
  ], events: [{ seq: 1, at: 1, type: "tool_started", tool: "inspect_devices" }, { seq: 2, at: 2, type: "tool_result", tool: "inspect_devices", result: { online: true } }] })]);
  const user = userEvent.setup(); const { container } = mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  expect(await screen.findByText("Thinking…")).toBeVisible();
  expect(screen.getByText("The device is online.")).toBeVisible();
  expect(container.querySelector(".ra-thread .ra-activity")).toBeNull();
  expect(container.querySelector(".ra-scroll .ra-record-options")).toBeNull();
  expect(container.querySelector(".ra-chat-menu .ra-activity")).not.toHaveAttribute("open");
  expect(screen.getByRole("button", { name: "Stop" })).toBeVisible();
  expect(container.querySelectorAll("details.ra-process-details")).toHaveLength(1);
  expect(screen.getByText("2 actions")).toBeVisible();
  expect(container.querySelector("details.ra-process-details")).not.toHaveAttribute("open");
  await user.click(screen.getByText("View activity"));
  expect(container.querySelector("details.ra-process-details")).toHaveAttribute("open");
  expect(screen.getByText("I will inspect the device configuration.")).toBeVisible();
});


it("lets an owner clarify a CLI source without submitting credentials or starting a model call", async () => {
  await i18n.changeLanguage("en");
  const backend = api(true, [controlled({ status: "waiting_input", run_state: "waiting_input", pending_input: { id: "secret", kind: "credential_ref", prompt: "Configure", goal_revision: 2, control_revision: 7, scope_revision: 1, business_revision: null, created_at: 1, fields: [] } })]);
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Inspect devices" }));
  await user.click(screen.getByRole("button", { name: "I use a local app / CLI" }));
  expect(screen.getByRole("textbox")).toHaveValue("I use a local AI app or CLI. Confirm the application and supported connection method first; do not ask me for an API endpoint or key.");
  expect(backend.actions).not.toHaveBeenCalled();
  expect(backend.creates).not.toHaveBeenCalled();
});


it("reports an unverifiable shared-model destination instead of pretending the task changed", async () => {
  await i18n.changeLanguage("en"); api();
  server.use(http.post("*/api/local/agent/tasks", () => HttpResponse.json({ detail: "agent_model_destination_unverifiable" }, { status: 409 })));
  const user = userEvent.setup(); mount();
  await user.type(await screen.findByRole("textbox"), "Inspect this computer without changing files");
  await user.click(screen.getByRole("button", { name: "Send task" }));
  expect(await screen.findByText(/Cannot verify this shared model's destination/)).toBeVisible();
  expect(screen.queryByText(/This task changed in another view/)).toBeNull();
  expect(screen.getByRole("textbox")).toHaveValue("Inspect this computer without changing files");
});

it("binds a missing workspace through an explicit owner action rather than a chat answer", async () => {
  await i18n.changeLanguage('en');
  const current = controlled({status:'failed', run_state:'ended', error:'agent_operation_failed', local_execution:{workspace_root:''}, events:[{seq:1,at:1,type:'tool_result',tool:'ryn_file_list',result:{error:'agent_workspace_required'}}]});
  api(true,[current]);
  const binds = vi.fn();
  server.use(http.get('*/api/local/agent/tasks/task-1/artifacts',()=>HttpResponse.json({artifacts:[]})),
    http.post('*/api/local/agent/tasks/task-1/workspace',async ({request})=>{const body=await request.json(); binds(body); return HttpResponse.json({...current,status:'queued',local_execution:{workspace_root:'D:\\Drawings'}})}));
  const user=userEvent.setup(); mount();
  await user.click(await screen.findByRole('button',{name:"Inspect devices"}));
  await user.type(await screen.findByRole('textbox',{name:'Working directory'}),'D:\\Drawings');
  expect(binds).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button',{name:'Use folder and continue'}));
  await waitFor(()=>expect(binds).toHaveBeenCalledWith({workspace_root:'D:\\Drawings',expected_control_revision:7,client_request_id:expect.any(String)}));
});

it("manages a project conversation from sidebar menus without changing the open conversation", async () => {
  const project: AgentProject = { id: "alpha", name: "Alpha", path: "D:\\code\\Alpha", generation: 1, entry_files: [], manifests: [], directories: [] };
  const action = { enabled: true, reason: null, requires_control_revision: true };
  const current = controlled({ title: "Open conversation", messages: [{ role: "assistant", content: "Keep this conversation open" }] });
  let record = controlled({ id: "project-history", title: "Project history", project, status: "paused", run_state: "paused", archived: false,
    allowed_actions: { rename: action, archive: action, delete: action } });
  let deleted = false;
  api(true, [current, record]);
  const writes = vi.fn();
  server.use(
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [project] })),
    http.get("*/api/local/agent/tasks", ({ request }) => HttpResponse.json({ tasks: new URL(request.url).searchParams.get("archived") === "true" ? !deleted && record.archived ? [record] : [] : [current, ...(!deleted && !record.archived ? [record] : [])] })),
    http.get("*/api/local/agent/tasks/project-history", () => HttpResponse.json(record)),
    http.post("*/api/local/agent/tasks/project-history/:operation", async ({ params, request }) => {
      writes(params.operation, await request.json());
      record = { ...record, archived: params.operation === "archive", revision: record.revision + 1, control_revision: record.control_revision! + 1,
        allowed_actions: { rename: action, delete: action, [params.operation === "archive" ? "restore" : "archive"]: action } };
      return HttpResponse.json(record);
    }),
    http.delete("*/api/local/agent/tasks/project-history", async ({ request }) => { writes("delete", await request.json()); deleted = true; return HttpResponse.json({ id: record.id, deleted: true, cleanup_pending: false }); }),
  );
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Open conversation" }));
  await screen.findByText("Keep this conversation open");
  const activeRows = within(screen.getByRole("complementary", { name: "Task list" }).querySelector<HTMLElement>(".ra-task-items")!);
  const archivedRows = within(screen.getByText("Archived tasks").closest("details")!);
  const menuForRecord = (archived = false) => {
    fireEvent.contextMenu((archived ? archivedRows : activeRows).getByRole("button", { name: "Project history" }), { clientX: 170, clientY: 240 });
    return within(screen.getByRole("menu", { name: "Task actions for Project history" }));
  };
  await user.click(menuForRecord().getByRole("menuitem", { name: "Archive task" }));
  await waitFor(() => expect(activeRows.queryByRole("button", { name: "Project history" })).not.toBeInTheDocument());
  expect(screen.getByText("Keep this conversation open")).toBeVisible();
  await user.click(screen.getByText("Archived tasks"));
  await archivedRows.findByRole("button", { name: "Project history" });
  await user.click(menuForRecord(true).getByRole("menuitem", { name: "Restore task" }));
  await activeRows.findByRole("button", { name: "Project history" });
  await waitFor(() => expect(archivedRows.queryByRole("button", { name: "Project history" })).not.toBeInTheDocument());
  await user.click(menuForRecord().getByRole("menuitem", { name: "Delete local task record" }));
  const confirm = await screen.findByRole("alertdialog", { name: "Delete local task record" });
  expect(confirm).toHaveTextContent("Project history");
  await user.click(within(confirm).getByRole("button", { name: "Delete local record permanently" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "Project history" })).not.toBeInTheDocument());
  expect(screen.getByText("Keep this conversation open")).toBeVisible();
  expect(agentTaskCache().has(record.id)).toBe(false);
  expect(writes.mock.calls.map(call => call[0])).toEqual(["archive", "restore", "delete"]);
});

it("keeps a restored task visible when an older active-list request omitted it", async () => {
  const allowed = { enabled: true, reason: null, requires_control_revision: true };
  const other = controlled({ archived: false });
  let record = controlled({ id: "archived-history", title: "Restored history", status: "paused", run_state: "paused", archived: true,
    allowed_actions: { restore: allowed, delete: allowed, rename: allowed } });
  api(true, [other]);
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const started = vi.fn();
  let calls = 0;
  vi.spyOn(rynAgent, "tasks").mockImplementation(async () => {
    if (++calls === 2) { started(); await gate; return { tasks: [other] }; }
    return { tasks: [other, ...(!record.archived ? [record] : [])] };
  });
  server.use(
    http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks: record.archived ? [record] : [] })),
    http.get("*/api/local/agent/tasks/archived-history", () => HttpResponse.json(record)),
    http.post("*/api/local/agent/tasks/archived-history/restore", () => { record = { ...record, archived: false, revision: record.revision + 1, control_revision: 8, allowed_actions: { archive: allowed, delete: allowed, rename: allowed } }; return HttpResponse.json(record); }),
  );
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByText("Archived tasks"));
  const archivedRows = within(screen.getByText("Archived tasks").closest("details")!);
  const activeRows = within(screen.getByRole("complementary", { name: "Task list" }).querySelector<HTMLElement>(".ra-task-items")!);
  const archived = await archivedRows.findByRole("button", { name: "Restored history" });
  await waitFor(() => expect(started).toHaveBeenCalled(), { timeout: 2500 });
  fireEvent.contextMenu(archived, { clientX: 160, clientY: 400 });
  await user.click(screen.getByRole("menuitem", { name: "Restore task" }));
  await activeRows.findByRole("button", { name: "Restored history" });
  await act(async () => { release(); await gate; });
  expect(activeRows.getByRole("button", { name: "Restored history" })).toBeVisible();
});
