import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import type { AgentModel, AgentModelSwitchPreview, AgentTask } from "../domain/rynAgent";
import RynAgentModel from "./RynAgentModel";

const task: AgentTask = { id: "task-a", title: "Report", model_id: "old", status: "paused", control_revision: 7, revision: 100, scope_revision: 4, created_at: 1, updated_at: 1, pending_approval: null, steps: 5, result: "", error: null, messages: [], events: [], limits: {}, allowed_actions: { model: { enabled: true, reason: null, requires_control_revision: true } } };
const models: AgentModel[] = [{ id: "old", name: "Old model", source: "local_api", available: true, agent_supported: true }, { id: "new", name: "New model", source: "local_api", source_name: "New API", available: true, agent_supported: true }, { id: "offline", name: "Offline model", source: "local_api", available: false, agent_supported: true }];
const preview: AgentModelSwitchPreview = { model: models[1], current_model_id: "old", destination: { catalog_model_id: "new", source_id: "new-source", provider_peer_id: "this-node", destination_revision: "new-generation" }, control_revision: 7, scope_revision: 4, current_scope_selection: { resources: [{ connection_id: "docs", root_path: "reports", recursive: false, actions: ["list", "read_ai"] }], allow_model_data: true }, current_scope_authorized: true, confirmation_required: true, data_policy: { sent_after_resume: ["owner_messages", "current_goal", "selected_skills", "current_saved_preferences"], excluded: ["previous_model_messages", "previous_tool_results", "previous_summaries", "previous_resource_grants"], resources_require_explicit_transfer_or_new_authorization: true, automatic_resume: false } };
beforeEach(() => sessionStorage.clear());

async function page(value = task, scope = preview) {
  const writes = vi.fn(); const changed = vi.fn();
  server.use(http.get("*/api/local/agent/tasks/task-a/model", ({ request }) => { expect(new URL(request.url).searchParams.get("model_id")).toBe(scope.model.id); return HttpResponse.json(scope); }), http.put("*/api/local/agent/tasks/task-a/model", async ({ request }) => { writes(await request.json()); return HttpResponse.json({ ...task, model_id: "new", status: "paused", scope_revision: 5 }); }));
  const view = render(<RynAgentModel task={value} models={models} defaultModelID="old" busy={false} onChange={changed} />);
  const user = userEvent.setup(); await user.click(screen.getByText("Task model · Old model"));
  return { user, writes, changed, view };
}

it("requires explicit owner-text destination confirmation and defaults to revoking resource access", async () => {
  const { user, writes, changed } = await page();
  expect(screen.getByRole("option", { name: /Offline model/ })).toBeDisabled();
  await user.selectOptions(screen.getByRole("combobox", { name: "Switch task model" }), "new");
  await screen.findByText(/previous owner messages/);
  expect(screen.getByText(/Source.*new-source/)).toBeVisible();
  expect(screen.getByRole("checkbox", { name: /Reauthorize .*directories/ })).not.toBeChecked();
  expect(screen.getByRole("button", { name: "Save task model and keep paused" })).toBeDisabled();
  await user.click(screen.getByRole("checkbox", { name: /I confirm/ }));
  await user.click(screen.getByRole("button", { name: "Save task model and keep paused" }));
  await waitFor(() => expect(changed).toHaveBeenCalledWith(expect.objectContaining({ model_id: "new", status: "paused" })));
  expect(writes).toHaveBeenCalledWith({ model_id: "new", expected_control_revision: 7, expected_scope_revision: 4, expected_destination: preview.destination, confirm_data_destination: true, transfer_resource_scope: false, client_request_id: expect.stringMatching(/^[a-f0-9-]{36}$/) });
});

it.each(["unknown", "unsupported"] as const)("allows a supported model destination with %s diagnostic probe evidence", async tool_support => {
  const { user, view } = await page();
  view.rerender(<RynAgentModel task={task} models={models.map(model => ({ ...model, tool_support }))} defaultModelID="old" busy={false} onChange={vi.fn()} />);
  expect(screen.getByRole("option", { name: /New model/ })).toBeEnabled();
  expect(screen.getByRole("option", { name: /Offline model/ })).toBeDisabled();
  await user.selectOptions(screen.getByRole("combobox", { name: "Switch task model" }), "new");
  expect(await screen.findByRole("checkbox", { name: /I confirm/ })).toBeEnabled();
});

it("resets destination consent when resource transfer changes and does not revive retained selections", async () => {
  const { user } = await page();
  await user.selectOptions(screen.getByRole("combobox"), "new"); await screen.findByText(/previous owner messages/);
  const consent = screen.getByRole("checkbox", { name: /I confirm/ });
  await user.click(consent); await user.click(screen.getByRole("checkbox", { name: /Reauthorize .*directories/ }));
  expect(consent).not.toBeChecked();
});

it("blocks transfer for an ungranted retained selection while keeping its directories visible", async () => {
  const { user } = await page(task, { ...preview, current_scope_authorized: false });
  await user.selectOptions(screen.getByRole("combobox"), "new");
  expect(await screen.findByRole("checkbox", { name: /Reauthorize .*directories/ })).toBeDisabled();
  expect(screen.getByText("docs · reports")).toBeVisible();
});

it("allows explicit destination reconfirmation for the current model after its source changes", async () => {
  const sameModel = { ...preview, model: models[0], destination: { ...preview.destination, catalog_model_id: "old", destination_revision: "changed-generation" } };
  const { user, writes } = await page(task, sameModel);
  await user.selectOptions(screen.getByRole("combobox"), "old");
  await screen.findByText(/previous owner messages/);
  await user.click(screen.getByRole("checkbox", { name: /I confirm/ }));
  await user.click(screen.getByRole("button", { name: "Save task model and keep paused" }));
  await waitFor(() => expect(writes).toHaveBeenCalledWith(expect.objectContaining({ model_id: "old", expected_destination: sameModel.destination, transfer_resource_scope: false })));
});

it("shows no legacy empty control and respects server checkpoint restrictions", async () => {
  const view = render(<RynAgentModel task={{ ...task, allowed_actions: undefined }} models={models} busy={false} onChange={vi.fn()} />);
  expect(screen.queryByText(/Task model/)).not.toBeInTheDocument();
  view.rerender(<RynAgentModel task={{ ...task, status: "running", allowed_actions: { model: { enabled: false, reason: "agent_invalid_transition", requires_control_revision: true } } }} models={models} busy={false} onChange={vi.fn()} />);
  await userEvent.setup().click(screen.getByText("Task model · Old model"));
  expect(screen.getByRole("combobox")).toBeDisabled();
  expect(screen.getByText(/Pause at a safe checkpoint/)).toBeVisible();
});

it("keeps an uncertain full request for explicit retry across changed versions without auto-resume", async () => {
  const { user, view } = await page(); const received = vi.fn();
  server.use(http.put("*/api/local/agent/tasks/task-a/model", async ({ request }) => { received(await request.json()); return received.mock.calls.length === 1 ? HttpResponse.json({}, { status: 503 }) : HttpResponse.json({ ...task, model_id: "new", status: "paused" }); }));
  await user.selectOptions(screen.getByRole("combobox"), "new"); await screen.findByText(/previous owner messages/);
  await user.click(screen.getByRole("checkbox", { name: /I confirm/ })); await user.click(screen.getByRole("button", { name: "Save task model and keep paused" }));
  await screen.findByRole("button", { name: "Check original switch request" }); view.unmount();
  render(<RynAgentModel task={{ ...task, control_revision: 999 }} models={models} busy={false} onChange={vi.fn()} />);
  await user.click(screen.getByText("Task model · Old model"));
  expect(received).toHaveBeenCalledTimes(1);
  await user.click(screen.getByRole("button", { name: "Check original switch request" }));
  await waitFor(() => expect(received).toHaveBeenCalledTimes(2));
  expect(received.mock.calls[1][0]).toEqual(received.mock.calls[0][0]);
});


it("shows the project when no NAS resources are selected and defaults to revoking its access", async () => {
  const { user, writes } = await page(task, { ...preview, current_project: { id: "project-1", name: "sample-backend", path: "D:/code/sample-backend" }, current_scope_selection: { resources: [], allow_model_data: false } });
  await user.selectOptions(screen.getByRole("combobox"), "new");
  expect(await screen.findByText("D:/code/sample-backend")).toBeVisible();
  expect(screen.getByRole("checkbox", { name: /Reauthorize this project/ })).not.toBeChecked();
  await user.click(screen.getByRole("checkbox", { name: /I confirm/ }));
  await user.click(screen.getByRole("button", { name: "Save task model and keep paused" }));
  await waitFor(() => expect(writes).toHaveBeenCalledWith(expect.objectContaining({ transfer_resource_scope: false })));
});
