import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import type { AgentTask } from "../domain/rynAgent";
import RynAgentFork from "./RynAgentFork";

beforeEach(() => sessionStorage.clear());

const task = { id: "parent", title: "Original", messages: [], status: "completed" } as unknown as AgentTask;
const boundary = { message_count: 3, token: "safe-prefix", role: "assistant", preview: "Original answer" };
function setup() {
  server.use(http.get("*/api/local/agent/tasks/parent/fork-boundaries", () => HttpResponse.json({ boundaries: [boundary] })));
}
it("requires an explicit create operation, uses the selected boundary and opens a paused child", async () => {
  setup(); const created = vi.fn(), received = vi.fn(); const user = userEvent.setup();
  server.use(http.post("*/api/local/agent/tasks/parent/fork", async ({ request }) => {
    received(await request.json()); return HttpResponse.json({ id: "child", status: "paused" });
  }));
  render(<RynAgentFork task={task} onCreated={created} onChoose={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: "Fork task" }));
  expect(screen.getByRole("dialog")).toHaveTextContent("Independent conversation, shared files");
  await screen.findByRole("option", { name: /Original answer/ });
  expect(received).not.toHaveBeenCalled();
  await user.clear(screen.getByRole("textbox", { name: "New task title" }));
  await user.type(screen.getByRole("textbox", { name: "New task title" }), "Alternative");
  await user.click(screen.getByRole("button", { name: "Create and open fork" }));
  await waitFor(() => expect(created).toHaveBeenCalledWith({ id: "child", status: "paused" }));
  expect(received).toHaveBeenCalledWith(expect.objectContaining({ title: "Alternative", message_count: 3, token: "safe-prefix", shared_workspace_confirmed: true }));
});
it("retries an uncertain response with the exact request and does not permit accidental edits", async () => {
  setup(); const received: unknown[] = []; const user = userEvent.setup(); const created = vi.fn();
  server.use(http.post("*/api/local/agent/tasks/parent/fork", async ({ request }) => {
    received.push(await request.json());
    return received.length === 1 ? HttpResponse.error() : HttpResponse.json({ id: "child", status: "paused" });
  }));
  render(<RynAgentFork task={task} onCreated={created} onChoose={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: "Fork task" }));
  await screen.findByRole("option", { name: /Original answer/ });
  await user.click(screen.getByRole("button", { name: "Create and open fork" }));
  await screen.findByRole("alert");
  expect(screen.getByRole("textbox", { name: "New task title" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Create and open fork" }));
  await waitFor(() => expect(created).toHaveBeenCalled());
  expect(received[0]).toEqual(received[1]);
});
it("shows reference failures and navigates to the source without running anything", async () => {
  setup(); const choose = vi.fn(), created = vi.fn(); const user = userEvent.setup();
  server.use(http.post("*/api/local/agent/tasks/parent/fork", () => HttpResponse.json({ detail: "agent_fork_attachment_unavailable" }, { status: 409 })));
  render(<RynAgentFork task={{ ...task, fork: { source_task_id: "source", source_title: "Source task", message_count: 2, shared_workspace: true, awaiting_instruction: true } }} onCreated={created} onChoose={choose} />);
  await user.click(screen.getByRole("button", { name: "Source task" }));
  expect(choose).toHaveBeenCalledWith("source");
  await user.click(screen.getByRole("button", { name: "Fork task" }));
  await screen.findByRole("option", { name: /Original answer/ });
  await user.click(screen.getByRole("button", { name: "Create and open fork" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("agent_fork_attachment_unavailable");
  expect(created).not.toHaveBeenCalled();
});
it("preserves a lost create response across component reloads", async () => {
  setup(); const received: unknown[] = []; const user = userEvent.setup(); const created = vi.fn();
  server.use(http.post("*/api/local/agent/tasks/parent/fork", async ({ request }) => {
    received.push(await request.json());
    return received.length === 1 ? HttpResponse.error() : HttpResponse.json({ id: "child", status: "paused" });
  }));
  const view = render(<RynAgentFork task={task} onCreated={created} onChoose={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: "Fork task" }));
  await screen.findByRole("option", { name: /Original answer/ });
  await user.click(screen.getByRole("button", { name: "Create and open fork" }));
  await screen.findByRole("alert");
  view.unmount();
  render(<RynAgentFork task={task} onCreated={created} onChoose={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: "Fork task" }));
  await screen.findByRole("option", { name: /Original answer/ });
  await user.click(screen.getByRole("button", { name: "Create and open fork" }));
  await waitFor(() => expect(created).toHaveBeenCalled());
  expect(received[0]).toEqual(received[1]);
  expect(sessionStorage.length).toBe(0);
});
