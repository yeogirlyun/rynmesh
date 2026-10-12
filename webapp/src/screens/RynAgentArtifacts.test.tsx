import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import type { AgentTask } from "../domain/rynAgent";
import { TaskArtifacts } from "./RynAgentArtifacts";
import "./rynAgentText";

const task: AgentTask = { id: "artifact-task", title: "Report", model_id: "model", status: "failed", revision: 3,
  control_revision: 2, scope_revision: 1, created_at: 1, updated_at: 1, pending_approval: null,
  steps: 4, result: "A model-invented path: private/secret.md", error: "agent_time_limit", messages: [], events: [], limits: {} };
const artifact = { id: "op-confirmed", name: "report.md", bytes: 20, sha256: "bound-sha", created_at: "2026-10-11" };

it("opens the actual confirmed artifact for a failed task using its task-bound read endpoint", async () => {
  const read = vi.fn(); const nasRead = vi.fn(); const user = userEvent.setup();
  server.use(http.get("*/api/local/agent/tasks/artifact-task/artifacts", () => HttpResponse.json({ artifacts: [artifact] })),
    http.get("*/api/local/agent/tasks/artifact-task/artifacts/op-confirmed", () => { read(); return HttpResponse.json({ ...artifact, text: "Actual saved report." }); }),
    http.get("*/api/local/plugins/nas/sources/:source/content", () => { nasRead(); return HttpResponse.json({ text: "MUST NOT USE OWNER NAS BYPASS" }); }));
  render(<TaskArtifacts task={task} />);
  await user.click(await screen.findByRole("button", { name: "Open report.md" }));
  expect(await screen.findByText("Actual saved report.")).toBeVisible();
  expect(read).toHaveBeenCalledTimes(1); expect(nasRead).not.toHaveBeenCalled();
  expect(screen.queryByText(/private\/secret/)).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Download|Ask AI|Edit|Share/i })).not.toBeInTheDocument();
});

it("renders nothing for prose and saved tool outputs without confirmed artifacts", async () => {
  const listed = vi.fn();
  server.use(http.get("*/api/local/agent/tasks/artifact-task/artifacts", () => { listed(); return HttpResponse.json({ artifacts: [] }); }));
  const view = render(<TaskArtifacts task={task} />);
  await waitFor(() => expect(listed).toHaveBeenCalled());
  expect(view.container).toBeEmptyDOMElement();
});

it("clears old preview after scope change and displays denial instead of saved content", async () => {
  let revoked = false; const user = userEvent.setup();
  server.use(http.get("*/api/local/agent/tasks/artifact-task/artifacts", () => HttpResponse.json({ artifacts: [artifact] })),
    http.get("*/api/local/agent/tasks/artifact-task/artifacts/op-confirmed", () => revoked
      ? HttpResponse.json({ detail: "agent_artifact_access_changed" }, { status: 409 })
      : HttpResponse.json({ ...artifact, text: "Previously readable content" })));
  const view = render(<TaskArtifacts task={task} />);
  await user.click(await screen.findByRole("button", { name: "Open report.md" }));
  await screen.findByText("Previously readable content");
  revoked = true;
  view.rerender(<TaskArtifacts task={{ ...task, scope_revision: 2, control_revision: 3 }} />);
  expect(screen.queryByText("Previously readable content")).not.toBeInTheDocument();
  await user.click(await screen.findByRole("button", { name: "Open report.md" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("this task no longer has access");
  expect(screen.queryByText("Previously readable content")).not.toBeInTheDocument();
});
