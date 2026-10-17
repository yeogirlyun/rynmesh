import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { rynAgent, type AgentTask } from "../domain/rynAgent";
import { useAgentTaskMenu } from "./RynAgentTaskMenu";
import { TaskRecords } from "./RynAgentRecords";
import "./rynAgentText";

const granted = { enabled: true, reason: null, requires_control_revision: true };
const task: AgentTask = { id: "sidebar-task", title: "Saved report", model_id: "model", status: "paused", run_state: "paused", revision: 3, control_revision: 3, created_at: 1, updated_at: 1, pending_approval: null, steps: 2, result: "", error: null, messages: [], events: [], limits: {}, archived: false, allowed_actions: { rename: granted, archive: granted, delete: granted } };
function Harness({ item = task, project = false, onChoose = vi.fn(), onChange = vi.fn(), onDelete = vi.fn(), header = false }: { item?: AgentTask; project?: boolean; onChoose?: (id: string) => void; onChange?: (value: AgentTask) => void; onDelete?: (id: string) => void; header?: boolean }) {
  const actions = useAgentTaskMenu({ selected: "", busy: false, onChoose, onChange, onDelete });
  return <div className="ra-workspace">{actions.renderTask(item, project)}{actions.dialogs}{header && <TaskRecords task={item} busy={false} onChange={onChange} onDelete={onDelete} />}</div>;
}
beforeEach(() => {
  sessionStorage.clear();
  server.use(http.get("*/api/local/agent/tasks/sidebar-task", () => HttpResponse.json(task)));
});

it("opens task actions by right click and keyboard without changing task selection", async () => {
  const choose = vi.fn(); const user = userEvent.setup();
  render(<Harness onChoose={choose} />);
  const row = screen.getByRole("button", { name: "Saved report" });
  expect(row).toHaveAccessibleDescription("Paused");
  expect(row).toHaveTextContent(/^Saved report$/);
  fireEvent.contextMenu(row, { clientX: 90, clientY: 120 });
  expect(screen.getByRole("menu", { name: "Task actions for Saved report" })).toBeVisible();
  expect(screen.getByRole("menuitem", { name: "Rename task" })).toHaveFocus();
  expect(choose).not.toHaveBeenCalled();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  expect(row).toHaveFocus();
  fireEvent.keyDown(row, { key: "F10", shiftKey: true });
  expect(screen.getByRole("menu")).toBeVisible();
  await user.keyboard("{Escape}");
  fireEvent.keyDown(row, { key: "ContextMenu" });
  expect(screen.getByRole("menu")).toBeVisible();
  expect(choose).not.toHaveBeenCalled();
});

it("uses the latest control revision for a trimmed rename from project task rows", async () => {
  const changed = vi.fn(); const chosen = vi.fn(); const received = vi.fn(); const user = userEvent.setup();
  server.use(http.get("*/api/local/agent/tasks/sidebar-task", () => HttpResponse.json({ ...task, control_revision: 9 })), http.patch("*/api/local/agent/tasks/sidebar-task", async ({ request }) => { const body = await request.json(); received(body); return HttpResponse.json({ ...task, title: "Updated report", control_revision: 10 }); }));
  render(<Harness project onChoose={chosen} onChange={changed} />);
  expect(screen.getByRole("button", { name: "Saved report" })).toHaveClass("ra-project-task");
  await user.click(screen.getByRole("button", { name: "Task actions for Saved report" }));
  await user.click(screen.getByRole("menuitem", { name: "Rename task" }));
  const input = screen.getByRole("textbox", { name: "Name" });
  expect(input).toHaveFocus(); expect(input).toHaveAttribute("maxlength", "160");
  await user.clear(input); await user.type(input, " Updated report ");
  await user.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(changed).toHaveBeenCalledWith(expect.objectContaining({ title: "Updated report" })));
  expect(received).toHaveBeenCalledWith({ title: "Updated report", expected_control_revision: 9, client_request_id: expect.any(String) });
  expect(chosen).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("archives and restores sidebar records while keeping their original paused state", async () => {
  const changed = vi.fn(); const user = userEvent.setup(); const received = vi.fn();
  server.use(http.post("*/api/local/agent/tasks/sidebar-task/archive", async ({ request }) => { received(await request.json()); return HttpResponse.json({ ...task, archived: true }); }), http.post("*/api/local/agent/tasks/sidebar-task/restore", () => HttpResponse.json({ ...task, archived: false })));
  const view = render(<Harness onChange={changed} />);
  await user.click(screen.getByRole("button", { name: "Task actions for Saved report" }));
  await user.click(screen.getByRole("menuitem", { name: "Archive task" }));
  await waitFor(() => expect(changed).toHaveBeenCalledWith(expect.objectContaining({ archived: true, status: "paused" })));
  expect(received).toHaveBeenCalledWith({ expected_control_revision: 3, client_request_id: expect.any(String) });
  const archived = { ...task, archived: true, allowed_actions: { rename: granted, restore: granted, delete: granted } };
  server.use(http.get("*/api/local/agent/tasks/sidebar-task", () => HttpResponse.json(archived)));
  view.rerender(<Harness item={archived} onChange={changed} />);
  expect(screen.getByRole("button", { name: "Saved report" })).toHaveClass("ra-task-item");
  expect(screen.getByRole("button", { name: "Saved report" })).toHaveAccessibleDescription("Archived");
  await user.click(screen.getByRole("button", { name: "Task actions for Saved report" }));
  expect(screen.queryByRole("menuitem", { name: "Archive task" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("menuitem", { name: "Restore task" }));
  await waitFor(() => expect(changed).toHaveBeenLastCalledWith(expect.objectContaining({ archived: false, status: "paused" })));
});

it("keeps running rename available and explains why archive and delete are disabled", async () => {
  const blocked = { enabled: false, reason: "task_in_flight", requires_control_revision: true };
  const running: AgentTask = { ...task, status: "running", run_state: "running", allowed_actions: { rename: granted, archive: blocked, delete: blocked } };
  const user = userEvent.setup(); render(<Harness item={running} />);
  await user.click(screen.getByRole("button", { name: "Task actions for Saved report" }));
  expect(screen.getByRole("menuitem", { name: "Rename task" })).toBeEnabled();
  expect(screen.getByRole("menuitem", { name: /^Archive task/ })).toBeDisabled();
  expect(screen.getByRole("menuitem", { name: /^Delete local task record/ })).toBeDisabled();
  expect(screen.getAllByText("Pause this task before archiving or deleting its record.")).toHaveLength(2);
});

it("confirms a named deletion and can cancel before any mutation", async () => {
  const deleted = vi.fn(); const received = vi.fn(); const user = userEvent.setup();
  server.use(http.delete("*/api/local/agent/tasks/sidebar-task", async ({ request }) => { received(await request.json()); return HttpResponse.json({ id: task.id, deleted: true, cleanup_pending: false }); }));
  render(<Harness onDelete={deleted} />);
  await user.click(screen.getByRole("button", { name: "Task actions for Saved report" }));
  await user.click(screen.getByRole("menuitem", { name: "Delete local task record" }));
  expect(screen.getByRole("alertdialog")).toHaveTextContent("Saved report");
  await user.click(screen.getByRole("button", { name: "Keep task" }));
  expect(received).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Task actions for Saved report" }));
  await user.click(screen.getByRole("menuitem", { name: "Delete local task record" }));
  await user.click(screen.getByRole("button", { name: "Delete local record permanently" }));
  await waitFor(() => expect(deleted).toHaveBeenCalledWith(task.id));
  expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
});

it("reconciles an uncertain rename with its identical request after closing the dialog and remounting", async () => {
  const changed = vi.fn(); const received = vi.fn(); const user = userEvent.setup();
  server.use(http.patch("*/api/local/agent/tasks/sidebar-task", async ({ request }) => { received(await request.json()); return received.mock.calls.length === 1 ? HttpResponse.json({}, { status: 503 }) : HttpResponse.json({ ...task, title: "Renamed" }); }));
  const first = render(<Harness onChange={changed} />);
  await user.click(screen.getByRole("button", { name: "Task actions for Saved report" }));
  await user.click(screen.getByRole("menuitem", { name: "Rename task" }));
  await user.clear(screen.getByRole("textbox", { name: "Name" }));
  await user.type(screen.getByRole("textbox", { name: "Name" }), "Renamed");
  await user.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByRole("button", { name: "Check original record request" });
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
  expect(screen.getByRole("button", { name: "Check original record request" })).toBeVisible();
  first.unmount(); render(<Harness onChange={changed} />);
  expect(received).toHaveBeenCalledTimes(1);
  await user.click(screen.getByRole("button", { name: "Check original record request" }));
  await waitFor(() => expect(changed).toHaveBeenCalledWith(expect.objectContaining({ title: "Renamed" })));
  expect(received.mock.calls[1][0]).toEqual(received.mock.calls[0][0]);
  expect(sessionStorage.getItem("ryn.agent.pending-record")).toBeNull();
});

it("refreshes grants before an archive so a task that started running is kept", async () => {
  const changed = vi.fn(); const received = vi.fn(); const user = userEvent.setup();
  server.use(http.get("*/api/local/agent/tasks/sidebar-task", () => HttpResponse.json({ ...task, status: "running", run_state: "running", allowed_actions: { rename: granted, archive: { ...granted, enabled: false } } })), http.post("*/api/local/agent/tasks/sidebar-task/archive", () => { received(); return HttpResponse.json(task); }));
  render(<Harness onChange={changed} />);
  await user.click(screen.getByRole("button", { name: "Task actions for Saved report" }));
  await user.click(screen.getByRole("menuitem", { name: "Archive task" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Pause this task before archiving or deleting its record.");
  expect(received).not.toHaveBeenCalled(); expect(changed).toHaveBeenCalledWith(expect.objectContaining({ status: "running" }));
});

it("shares the pending original request with header controls instead of sending another mutation", async () => {
  const user = userEvent.setup(); const received = vi.fn();
  server.use(http.post("*/api/local/agent/tasks/sidebar-task/archive", async ({ request }) => { received(await request.json()); return HttpResponse.json({}, { status: 503 }); }));
  render(<Harness header />);
  await user.click(screen.getByRole("button", { name: "Task actions for Saved report" }));
  await user.click(screen.getByRole("menuitem", { name: "Archive task" }));
  await screen.findAllByRole("button", { name: "Check original record request" });
  expect(screen.getByRole("button", { name: "Archive task" })).toBeDisabled();
  expect(received).toHaveBeenCalledTimes(1);
});

it("traps focus from outside and keeps Tab inside while all dialog controls are disabled", async () => {
  const user = userEvent.setup();
  let finish!: (value: AgentTask) => void;
  const preflight = vi.spyOn(rynAgent, "task").mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  server.use(http.patch("*/api/local/agent/tasks/sidebar-task", () => HttpResponse.json({ ...task, title: "Renamed" })));
  try {
    render(<Harness />);
    const outside = screen.getByRole("button", { name: "Saved report" });
    await user.click(screen.getByRole("button", { name: "Task actions for Saved report" }));
    await user.click(screen.getByRole("menuitem", { name: "Rename task" }));
    const dialog = screen.getByRole("dialog"); const input = screen.getByRole("textbox", { name: "Name" });
    outside.focus(); await user.tab(); expect(input).toHaveFocus();
    outside.focus(); await user.tab({ shift: true }); expect(screen.getByRole("button", { name: "Save" })).toHaveFocus();
    await user.clear(input); await user.type(input, "Renamed");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(input).toBeDisabled(); expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled(); expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    outside.focus(); await user.tab(); expect(dialog).toHaveFocus();
    await user.tab({ shift: true }); expect(dialog).toHaveFocus();
    await act(async () => { finish(task); });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  } finally { preflight.mockRestore(); }
});

it("aborts the preflight read on unmount before any record mutation is sent", async () => {
  const user = userEvent.setup(); const changed = vi.fn();
  let finish!: (value: AgentTask) => void; let signal: AbortSignal | undefined;
  const preflight = vi.spyOn(rynAgent, "task").mockImplementation((_id, nextSignal) => { signal = nextSignal; return new Promise(resolve => { finish = resolve; }); });
  const record = vi.spyOn(rynAgent, "record");
  try {
    const view = render(<Harness onChange={changed} />);
    await user.click(screen.getByRole("button", { name: "Task actions for Saved report" }));
    await user.click(screen.getByRole("menuitem", { name: "Archive task" }));
    view.unmount(); expect(signal?.aborted).toBe(true);
    await act(async () => { finish(task); });
    expect(record).not.toHaveBeenCalled(); expect(changed).not.toHaveBeenCalled();
  } finally { preflight.mockRestore(); record.mockRestore(); }
});

it("delivers a dispatched mutation to the currently mounted controller after remounting", async () => {
  const oldChanged = vi.fn(); const changed = vi.fn(); const user = userEvent.setup();
  let finish!: (value: AgentTask) => void;
  const record = vi.spyOn(rynAgent, "record").mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  try {
    const view = render(<Harness onChange={oldChanged} />);
    await user.click(screen.getByRole("button", { name: "Task actions for Saved report" }));
    await user.click(screen.getByRole("menuitem", { name: "Archive task" }));
    await waitFor(() => expect(record).toHaveBeenCalledTimes(1));
    view.unmount(); render(<Harness onChange={changed} />);
    expect(screen.getByRole("button", { name: "Check original record request" })).toBeDisabled();
    await act(async () => { finish({ ...task, archived: true }); });
    expect(changed).toHaveBeenCalledWith(expect.objectContaining({ archived: true }));
    expect(oldChanged).not.toHaveBeenCalled();
    expect(sessionStorage.getItem("ryn.agent.pending-record")).toBeNull();
  } finally { record.mockRestore(); }
});

