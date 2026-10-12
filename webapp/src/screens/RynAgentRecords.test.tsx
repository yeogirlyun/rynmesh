import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import type { AgentTask } from "../domain/rynAgent";
import { ArchivedTasks, TaskRecords, TaskResultFiles } from "./RynAgentRecords";
import "./rynAgentText";

const task: AgentTask = { id: "saved-task", title: "Paused report", model_id: "model", status: "paused", run_state: "paused", revision: 3, control_revision: 3, created_at: 1, updated_at: 1, pending_approval: null, steps: 2, result: "", error: null, messages: [], events: [], limits: {}, archived: false, allowed_actions: { archive: { enabled: true, reason: null, requires_control_revision: true }, delete: { enabled: true, reason: null, requires_control_revision: true } } };
beforeEach(() => sessionStorage.clear());

it("archives only with the server-granted control version and never runs the task", async () => {
  const received = vi.fn(); const changed = vi.fn(); const user = userEvent.setup();
  server.use(http.post("*/api/local/agent/tasks/saved-task/archive", async ({ request }) => { received(await request.json()); return HttpResponse.json({ ...task, archived: true, revision: 4, control_revision: 4 }); }));
  render(<TaskRecords task={task} busy={false} onChange={changed} onDelete={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: /^Archive task$/ }));
  await waitFor(() => expect(changed).toHaveBeenCalledWith(expect.objectContaining({ archived: true, status: "paused" })));
  expect(received).toHaveBeenCalledWith({ expected_control_revision: 3, client_request_id: expect.stringMatching(/^[a-f0-9-]{36}$/) });
});

it("restores the original paused state without an automatic resume", async () => {
  const changed = vi.fn(); const user = userEvent.setup();
  server.use(http.post("*/api/local/agent/tasks/saved-task/restore", () => HttpResponse.json({ ...task, archived: false })));
  render(<TaskRecords task={{ ...task, archived: true, allowed_actions: { restore: { enabled: true, reason: null, requires_control_revision: true } } }} busy={false} onChange={changed} onDelete={vi.fn()} />);
  expect(screen.getByText(/Restoring keeps its original state/)).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Restore task" }));
  await waitFor(() => expect(changed).toHaveBeenCalledWith(expect.objectContaining({ status: "paused", archived: false })));
});

it("does not offer legacy actions or enable a server-blocked deletion", () => {
  const view = render(<TaskRecords task={{ ...task, allowed_actions: undefined }} busy={false} onChange={vi.fn()} onDelete={vi.fn()} />);
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  view.rerender(<TaskRecords task={{ ...task, allowed_actions: { delete: { enabled: false, reason: "task_in_flight", requires_control_revision: true } } }} busy={false} onChange={vi.fn()} onDelete={vi.fn()} />);
  expect(screen.getByRole("button", { name: "Delete local task record" })).toBeDisabled();
});

it("confirms local-only deletion and retries cleanup with the identical original request", async () => {
  const received = vi.fn(); const deleted = vi.fn(); const user = userEvent.setup();
  server.use(http.delete("*/api/local/agent/tasks/saved-task", async ({ request }) => { received(await request.json()); return HttpResponse.json({ id: "saved-task", deleted: true, cleanup_pending: received.mock.calls.length === 1 }); }));
  const view = render(<TaskRecords task={task} busy={false} onChange={vi.fn()} onDelete={deleted} />);
  await user.click(screen.getByRole("button", { name: "Delete local task record" }));
  expect(received).not.toHaveBeenCalled();
  expect(screen.getByRole("alertdialog")).toHaveTextContent("Business files, created artifacts and sharing settings will remain");
  await user.click(screen.getByRole("button", { name: "Delete local record permanently" }));
  await waitFor(() => expect(deleted).toHaveBeenCalledWith("saved-task"));
  view.rerender(<TaskRecords task={null} busy={false} onChange={vi.fn()} onDelete={deleted} />);
  await user.click(await screen.findByRole("button", { name: "Retry local cleanup" }));
  await waitFor(() => expect(received).toHaveBeenCalledTimes(2));
  expect(received.mock.calls[1][0]).toEqual(received.mock.calls[0][0]);
  await waitFor(() => expect(sessionStorage.getItem("ryn.agent.pending-record")).toBeNull());
});

it("keeps uncertain requests through reload without replaying automatically", async () => {
  const received = vi.fn(); const user = userEvent.setup();
  server.use(http.post("*/api/local/agent/tasks/saved-task/archive", async ({ request }) => { received(await request.json()); return received.mock.calls.length === 1 ? HttpResponse.json({}, { status: 503 }) : HttpResponse.json({ ...task, archived: true }); }));
  const first = render(<TaskRecords task={task} busy={false} onChange={vi.fn()} onDelete={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: /^Archive task$/ }));
  await screen.findByRole("button", { name: "Check original record request" }); first.unmount();
  render(<TaskRecords task={{ ...task, control_revision: 999 }} busy={false} onChange={vi.fn()} onDelete={vi.fn()} />);
  expect(received).toHaveBeenCalledTimes(1);
  await user.click(screen.getByRole("button", { name: "Check original record request" }));
  await waitFor(() => expect(received).toHaveBeenCalledTimes(2));
  expect(received.mock.calls[1][0]).toEqual(received.mock.calls[0][0]);
});

it("loads archived tasks independently without selecting or running them", async () => {
  const selected = vi.fn(); const user = userEvent.setup();
  server.use(http.get("*/api/local/agent/tasks", ({ request }) => new URL(request.url).searchParams.get("archived") === "true" ? HttpResponse.json({ tasks: [{ ...task, archived: true }] }) : HttpResponse.json({}, { status: 500 })));
  render(<ArchivedTasks enabled selected="" onChoose={selected} revision={0} />);
  await user.click(screen.getByText("Archived tasks"));
  await screen.findByRole("button", { name: /Paused report.*Archived/ });
  expect(selected).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: /Paused report.*Archived/ }));
  expect(selected).toHaveBeenCalledWith("saved-task");
});

it("reads saved tool output with authenticated local pagination and exposes its fingerprint", async () => {
  const pages = vi.fn(); const user = userEvent.setup();
  server.use(http.get("*/api/local/agent/tasks/saved-task/results/result_abc", ({ request }) => {
    const offset = Number(new URL(request.url).searchParams.get("offset")); pages(offset);
    return HttpResponse.json({ id: "result_abc", sha256: "trusted-hash", bytes: 9000, characters: 8200, offset, next_offset: offset ? null : 8192, content: offset ? "next saved fragment" : "first saved fragment" });
  }));
  render(<TaskResultFiles task={{ ...task, result_refs: [{ id: "result_abc", task_id: "saved-task", tool: "ryn_read_resource", operation_id: "op", goal_revision: 1, scope_revision: 2, context_epoch: 0, sha256: "trusted-hash", bytes: 9000 }] }} />);
  await user.click(screen.getByText("Saved tool outputs"));
  await user.click(screen.getByRole("button", { name: /Read selected file/ }));
  await screen.findByText("first saved fragment");
  await user.click(screen.getByRole("button", { name: "Next page" }));
  await screen.findByText("next saved fragment");
  expect(pages.mock.calls.map(call => call[0])).toEqual([0, 8192]);
  expect(screen.queryByText("first saved fragment")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Next page" })).not.toBeInTheDocument();
  await user.click(screen.getByText("Content fingerprint"));
  expect(screen.getByText("trusted-hash")).toBeVisible();
});

it("uses server character offsets for output containing non-BMP characters", async () => {
  const user = userEvent.setup();
  server.use(http.get("*/api/local/agent/tasks/saved-task/results/result_emoji", () => HttpResponse.json({ id: "result_emoji", sha256: "hash", bytes: 4, characters: 1, offset: 0, next_offset: null, content: "😀" })));
  render(<TaskResultFiles task={{ ...task, result_refs: [{ id: "result_emoji", task_id: "saved-task", tool: "ryn_read_resource", operation_id: "op", goal_revision: 1, scope_revision: 1, context_epoch: 0, sha256: "hash", bytes: 4 }] }} />);
  await user.click(screen.getByText("Saved tool outputs"));
  await user.click(screen.getByRole("button", { name: /Read selected file/ }));
  expect(await screen.findByText("Characters 0–1 of 1")).toBeVisible();
});
