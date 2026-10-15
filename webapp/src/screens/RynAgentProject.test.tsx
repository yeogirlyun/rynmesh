import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { canPickPaths, chooseDropDir } from "../domain/dropDesktop";
import type { AgentProject, AgentTaskSummary } from "../domain/rynAgent";
import RynAgentProject from "./RynAgentProject";

vi.mock("../domain/dropDesktop", () => ({ canPickPaths: vi.fn(), chooseDropDir: vi.fn() }));
beforeEach(() => { vi.mocked(canPickPaths).mockReturnValue(false); });

const project = { id: "project-chat", name: "chat-backend", path: "D:\\code\\chat-backend", generation: 1, entry_files: ["AGENTS.md"], manifests: ["package.json"], directories: ["src", "test"] };
const secondProject = { ...project, id: "project-ryn", name: "rynmesh", path: "D:\\code\\rynmesh" };
const task = (id: string, title: string, selectedProject?: AgentProject): AgentTaskSummary => ({ id, title, project: selectedProject, model_id: "model-a", status: "completed", revision: 1, created_at: 1, updated_at: 2, pending_approval: null, steps: 1, result: "", error: null });

it("loads an existing project and explicitly selects it without submitting a task", async () => {
  const changed = vi.fn(), requested = vi.fn(), projectsChanged = vi.fn(), user = userEvent.setup();
  server.use(http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
    http.post("*/api/local/agent/projects", async ({ request }) => { requested(await request.json()); return HttpResponse.json(project); }));
  render(<RynAgentProject value="" onChange={changed} onProjectsChange={projectsChanged} disabled={false} />);
  await user.click(screen.getByRole("button", { name: "Load project" }));
  await user.type(screen.getByRole("textbox", { name: "Local project folder" }), project.path);
  await user.click(screen.getAllByRole("button", { name: "Load project" })[1]);
  await waitFor(() => expect(changed).toHaveBeenCalledWith(project.id));
  expect(requested).toHaveBeenCalledWith({ path: project.path });
  expect(screen.getByRole("button", { name: /chat-backend ·/ })).toBeInTheDocument();
  expect(projectsChanged).toHaveBeenLastCalledWith([project]);
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
});

it("lists multiple saved projects and persists selection without dropping other projects", async () => {
  const changed = vi.fn(), selected = vi.fn(), user = userEvent.setup();
  server.use(http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [project, secondProject] })),
    http.post("*/api/local/agent/projects/selection", async ({ request }) => { selected(await request.json()); return HttpResponse.json({ selected_project_id: secondProject.id }); }));
  const view = render(<RynAgentProject value={project.id} onChange={changed} />);
  expect(await screen.findByRole("button", { name: /chat-backend ·/ })).toHaveAttribute("aria-pressed", "true");
  await user.click(screen.getByRole("button", { name: /rynmesh ·/ }));
  await waitFor(() => expect(changed).toHaveBeenCalledWith(secondProject.id));
  expect(selected).toHaveBeenCalledWith({ project_id: secondProject.id });
  view.rerender(<RynAgentProject value={secondProject.id} onChange={changed} />);
  expect(screen.getByRole("button", { name: /rynmesh ·/ })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: /chat-backend ·/ })).toHaveAttribute("aria-pressed", "false");
});

it("keeps project conversations grouped and can collapse, open and start them", async () => {
  const chooseTask = vi.fn(), newTask = vi.fn(), changed = vi.fn(), user = userEvent.setup();
  server.use(http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [project, secondProject] })));
  render(<RynAgentProject value={project.id} onChange={changed} tasks={[task("chat-task", "Repair backend", project), task("ryn-task", "Update agent", secondProject), task("general-task", "General question")]} activeTaskID="chat-task" onSelectTask={chooseTask} onNewTask={newTask} />);
  expect(await screen.findByRole("button", { name: /Repair backend/ })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: /Update agent/ })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /General question/ })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Hide tasks in chat-backend" }));
  expect(screen.queryByRole("button", { name: /Repair backend/ })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Show tasks in chat-backend" }));
  await user.click(screen.getByRole("button", { name: /Repair backend/ }));
  expect(chooseTask).toHaveBeenCalledWith("chat-task");
  await user.click(screen.getByRole("button", { name: "New task in chat-backend" }));
  expect(newTask).toHaveBeenCalledOnce();
  expect(newTask).toHaveBeenCalledWith(project.id);
  expect(changed).not.toHaveBeenCalled();
});

it("retains the selected project when saving another selection fails", async () => {
  const changed = vi.fn(), user = userEvent.setup();
  server.use(http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [project, secondProject] })),
    http.post("*/api/local/agent/projects/selection", () => HttpResponse.json({ detail: "agent_project_unavailable" }, { status: 400 })));
  render(<RynAgentProject value={project.id} onChange={changed} />);
  await user.click(await screen.findByRole("button", { name: /rynmesh ·/ }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Project selection was not saved");
  expect(screen.getByRole("button", { name: /chat-backend ·/ })).toHaveAttribute("aria-pressed", "true");
  expect(changed).not.toHaveBeenCalled();
});

it("preserves the chosen project and path when loading fails", async () => {
  const changed = vi.fn(), user = userEvent.setup();
  server.use(http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [project] })),
    http.post("*/api/local/agent/projects", () => HttpResponse.json({ detail: "agent_project_not_directory" }, { status: 400 })));
  render(<RynAgentProject value={project.id} onChange={changed} disabled={false} />);
  await user.click(screen.getByRole("button", { name: "Load project" }));
  await user.type(screen.getByRole("textbox", { name: "Local project folder" }), "D:\\missing");
  await user.click(screen.getAllByRole("button", { name: "Load project" })[1]);
  await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: /chat-backend ·/ })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("textbox", { name: "Local project folder" })).toHaveValue("D:\\missing");
  expect(changed).not.toHaveBeenCalled();
});

it("adds another project and reloads it without duplicating or removing saved rows", async () => {
  const changed = vi.fn(), projectsChanged = vi.fn(), user = userEvent.setup();
  server.use(http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [project] })),
    http.post("*/api/local/agent/projects", () => HttpResponse.json(secondProject)));
  render(<RynAgentProject value={project.id} onChange={changed} onProjectsChange={projectsChanged} />);
  await screen.findByRole("button", { name: /chat-backend ·/ });
  await user.click(screen.getByRole("button", { name: "Load project" }));
  await user.type(screen.getByRole("textbox", { name: "Local project folder" }), secondProject.path);
  await user.click(screen.getAllByRole("button", { name: "Load project" })[1]);
  await screen.findByRole("button", { name: /rynmesh ·/ });
  expect(projectsChanged).toHaveBeenLastCalledWith([project, secondProject]);
  await user.click(screen.getByRole("button", { name: "Reload rynmesh" }));
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(2));
  expect(screen.getAllByRole("button", { name: /rynmesh ·/ })).toHaveLength(1);
  expect(screen.getByRole("button", { name: /chat-backend ·/ })).toBeInTheDocument();
});

it("uses the desktop folder picker to load a project by its absolute path", async () => {
  vi.mocked(canPickPaths).mockReturnValue(true);
  vi.mocked(chooseDropDir).mockResolvedValue(secondProject.path);
  const changed = vi.fn(), requested = vi.fn(), user = userEvent.setup();
  server.use(http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [project] })),
    http.post("*/api/local/agent/projects", async ({ request }) => { requested(await request.json()); return HttpResponse.json(secondProject); }));
  render(<RynAgentProject value={project.id} onChange={changed} />);
  await user.click(screen.getByRole("button", { name: "Load project" }));
  await user.click(screen.getByRole("button", { name: "Choose folder" }));
  await waitFor(() => expect(changed).toHaveBeenCalledWith(secondProject.id));
  expect(chooseDropDir).toHaveBeenCalledOnce();
  expect(requested).toHaveBeenCalledWith({ path: secondProject.path });
  expect(screen.getByRole("button", { name: /chat-backend ·/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /rynmesh ·/ })).toBeInTheDocument();
});

it("cancelling a desktop folder pick keeps the form and saved projects", async () => {
  vi.mocked(canPickPaths).mockReturnValue(true);
  vi.mocked(chooseDropDir).mockResolvedValue(null);
  const changed = vi.fn(), user = userEvent.setup();
  server.use(http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [project] })));
  render(<RynAgentProject value={project.id} onChange={changed} />);
  await user.click(screen.getByRole("button", { name: "Load project" }));
  await user.click(screen.getByRole("button", { name: "Choose folder" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Choose folder" })).toBeEnabled());
  expect(screen.getByRole("textbox", { name: "Local project folder" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /chat-backend ·/ })).toBeInTheDocument();
  expect(changed).not.toHaveBeenCalled();
});

it("shows a list error immediately and can reload the saved projects", async () => {
  const changed = vi.fn(), user = userEvent.setup();
  server.use(http.get("*/api/local/agent/projects", () => HttpResponse.json({}, { status: 503 })));
  render(<RynAgentProject value="" onChange={changed} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the project");
  server.use(http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [project] })));
  await user.click(screen.getByRole("button", { name: "Reload projects" }));
  expect(await screen.findByRole("button", { name: /chat-backend ·/ })).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("restores a saved selection only when no conversation is already open", async () => {
  const changed = vi.fn();
  server.use(http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [project], selected_project_id: project.id })));
  const view = render(<RynAgentProject value="" onChange={changed} />);
  await waitFor(() => expect(changed).toHaveBeenCalledWith(project.id));
  view.unmount(); changed.mockClear();
  render(<RynAgentProject value="" activeTaskID="existing-task" onChange={changed} />);
  await screen.findByRole("button", { name: /chat-backend ·/ });
  expect(changed).not.toHaveBeenCalled();
});

it("shows cached task projects while their saved list is still loading", async () => {
  const changed = vi.fn();
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  server.use(http.get("*/api/local/agent/projects", async () => { await gate; return HttpResponse.json({ projects: [project] }); }));
  render(<RynAgentProject value={project.id} onChange={changed} tasks={[task("existing-task", "Cached conversation", project)]} activeTaskID="existing-task" onSelectTask={vi.fn()} />);
  expect(screen.getByRole("button", { name: /chat-backend ·/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Cached conversation/ })).toBeInTheDocument();
  release();
  await waitFor(() => expect(screen.queryByRole("status")).not.toBeInTheDocument());
});

it("does not restore a server project after the parent explicitly chooses a general draft", async () => {
  const changed = vi.fn();
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  server.use(http.get("*/api/local/agent/projects", async () => { await gate; return HttpResponse.json({ projects: [project], selected_project_id: project.id }); }));
  const view = render(<RynAgentProject value="" onChange={changed} restoreSelection />);
  view.rerender(<RynAgentProject value="" onChange={changed} restoreSelection={false} />);
  await act(async () => { release(); await gate; });
  await screen.findByRole("button", { name: /chat-backend ·/ });
  expect(changed).not.toHaveBeenCalled();
});

it.each(["select", "new task"])("does not replace a newer conversation when a deferred project %s completes", async action => {
  const changed = vi.fn(), newTask = vi.fn(), requested = vi.fn(), user = userEvent.setup();
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  server.use(http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [project, secondProject] })),
    http.post("*/api/local/agent/projects/selection", async () => { requested(); await gate; return HttpResponse.json({ selected_project_id: secondProject.id }); }));
  const props = { value: project.id, onChange: changed, onNewTask: newTask, tasks: [task("older", "Older task", project), task("newer", "Newer task", project)], onSelectTask: vi.fn() };
  const view = render(<RynAgentProject {...props} activeTaskID="older" />);
  await screen.findByRole("button", { name: /rynmesh ·/ });
  await user.click(screen.getByRole("button", { name: action === "select" ? /rynmesh ·/ : "New task in rynmesh" }));
  await waitFor(() => expect(requested).toHaveBeenCalledOnce());
  view.rerender(<RynAgentProject {...props} activeTaskID="newer" />);
  await act(async () => { release(); await gate; });
  await waitFor(() => expect(screen.getByRole("button", { name: /rynmesh ·/ })).toBeEnabled());
  expect(changed).not.toHaveBeenCalled();
  expect(newTask).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: /Newer task/ })).toHaveAttribute("aria-pressed", "true");
});

it("publishes a deferred project load without leaving a newer selected conversation", async () => {
  const changed = vi.fn(), projectsChanged = vi.fn(), requested = vi.fn(), user = userEvent.setup();
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  server.use(http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [project] })),
    http.post("*/api/local/agent/projects", async () => { requested(); await gate; return HttpResponse.json(secondProject); }));
  const props = { value: project.id, onChange: changed, onProjectsChange: projectsChanged, tasks: [task("newer", "Newer task", project)], onSelectTask: vi.fn() };
  const view = render(<RynAgentProject {...props} activeTaskID="" />);
  await user.click(screen.getByRole("button", { name: "Load project" }));
  await user.type(screen.getByRole("textbox", { name: "Local project folder" }), secondProject.path);
  await user.click(screen.getAllByRole("button", { name: "Load project" })[1]);
  await waitFor(() => expect(requested).toHaveBeenCalledOnce());
  view.rerender(<RynAgentProject {...props} activeTaskID="newer" />);
  await act(async () => { release(); await gate; });
  await screen.findByRole("button", { name: /rynmesh ·/ });
  expect(projectsChanged).toHaveBeenLastCalledWith([project, secondProject]);
  expect(changed).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: /Newer task/ })).toHaveAttribute("aria-pressed", "true");
});

it("keeps newer navigation when the native folder chooser returns after a task switch", async () => {
  vi.mocked(canPickPaths).mockReturnValue(true);
  let choose!: (path: string) => void;
  vi.mocked(chooseDropDir).mockImplementation(() => new Promise(resolve => { choose = resolve; }));
  const changed = vi.fn(), projectsChanged = vi.fn(), user = userEvent.setup();
  server.use(http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [project] })),
    http.post("*/api/local/agent/projects", () => HttpResponse.json(secondProject)));
  const props = { value: project.id, onChange: changed, onProjectsChange: projectsChanged, tasks: [task("newer", "Newer task", project)], onSelectTask: vi.fn() };
  const view = render(<RynAgentProject {...props} activeTaskID="" />);
  await user.click(screen.getByRole("button", { name: "Load project" }));
  await user.click(screen.getByRole("button", { name: "Choose folder" }));
  expect(chooseDropDir).toHaveBeenCalledOnce();
  view.rerender(<RynAgentProject {...props} activeTaskID="newer" />);
  await act(async () => { choose(secondProject.path); });
  await screen.findByRole("button", { name: /rynmesh ·/ });
  expect(projectsChanged).toHaveBeenLastCalledWith([project, secondProject]);
  expect(changed).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: /Newer task/ })).toHaveAttribute("aria-pressed", "true");
});
