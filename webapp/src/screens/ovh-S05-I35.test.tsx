import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import type { AgentTask } from "../domain/rynAgent";
import { TaskArtifacts } from "./RynAgentArtifacts";
import "./rynAgentText";

afterEach(() => vi.unstubAllGlobals());
const task: AgentTask = { id: "i35-task", title: "Files", model_id: "model", status: "completed", revision: 1,
  control_revision: 1, scope_revision: 1, created_at: 1, updated_at: 1, pending_approval: null,
  steps: 1, result: "Saved output", error: null, messages: [], events: [], limits: {} };
const file = { id: "i35-file", name: "report.bat", path: "report.bat", media_type: "text/plain", preview: "text", role: "deliverable",
  status: "current", bytes: 20, sha256: "version", created_by_task: true, revisions: 1, goal_revisions: [1], mentioned_in_answer: true };

it("labels executable default-open as reveal and sends reveal, retaining text preview", async () => {
  vi.stubGlobal("__TAURI_INTERNALS__", { invoke: vi.fn().mockResolvedValue("test-local-token") });
  const actions: unknown[] = [];
  server.use(http.get("*/api/local/agent/tasks/i35-task/artifacts", () => HttpResponse.json({ artifacts: [], files: [file] })),
    http.post("*/api/local/agent/tasks/i35-task/files/i35-file/actions", async ({ request }) => {
      actions.push(await request.json()); return HttpResponse.json({ opened: false, revealed: true });
    }));
  const user = userEvent.setup();
  render(<TaskArtifacts task={task} />);
  await user.click(await screen.findByLabelText("Open with · report.bat"));
  expect(screen.queryByRole("button", { name: "Default application" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Preview" })).toBeEnabled();
  await user.click(screen.getAllByRole("button", { name: "Show in folder" })[0]);
  await waitFor(() => expect(actions).toEqual([{ action: "reveal" }]));
});

it("states the 256 MiB listing limit for an oversized file", async () => {
  server.use(http.get("*/api/local/agent/tasks/i35-task/artifacts", () => HttpResponse.json({ artifacts: [],
    files: [{ ...file, name: "huge.pdf", path: "huge.pdf", status: "too_large", bytes: 300 * 1048576, sha256: null, limit_bytes: 256 * 1048576 }] })));
  render(<TaskArtifacts task={task} />);
  expect(await screen.findByText(/256 MiB.*preview or transfer/i)).toBeVisible();
});
