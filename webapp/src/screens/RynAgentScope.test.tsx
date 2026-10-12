import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { agentScopeBody, type AgentScopeSelection, type AgentScopeSnapshot, type AgentTask } from "../domain/rynAgent";
import RynAgentScope from "./RynAgentScope";
import "./rynAgentText";

const task: AgentTask = { id: "scoped-task", title: "Read reports", model_id: "model-a", status: "paused", revision: 40, control_revision: 5, created_at: 1, updated_at: 1, pending_approval: null, steps: 2, result: "", error: null, messages: [], events: [], limits: {}, allowed_actions: { scope: { enabled: true, reason: null, requires_control_revision: true } } };
const emptyScope: AgentScopeSnapshot = { scope: { resource_grants: [], egress_grants: [], selection: { resources: [], allow_model_data: false } }, scope_revision: 2, control_revision: 5, current_model_destination: { catalog_model_id: "model-a", source_id: "api-source", destination_revision: "destination-a" } };
function api(snapshot = emptyScope, fileScope = false) {
  const listing = vi.fn();
  server.use(
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ create_scope_supported: true, file_scope_supported: fileScope, connections: [
      { connection_id: "google", name: "Google reports", protocol: "google", enabled: true, allow_ai: true, agent_supported: true, reason: "" },
      { connection_id: "blocked", name: "Private drive", protocol: "smb", enabled: true, allow_ai: false, agent_supported: true, reason: "" },
      { connection_id: "dav", name: "Writable reports", protocol: "webdav", enabled: true, allow_ai: true, agent_supported: true, reason: "", writable: true, create_supported: true },
    ], supported_formats: [".txt", ".pdf"] })),
    http.get("*/api/local/agent/tasks/scoped-task/scope", () => HttpResponse.json(snapshot)),
    http.get("*/api/local/plugins/nas/sources/google/files", ({ request }) => {
      const path = new URL(request.url).searchParams.get("path"); listing(path);
      return HttpResponse.json({ entries: path === "" ? [{ path: "opaque-id-42", name: "Reports", directory: true }, { path: "file-id", name: "Secret text.txt", directory: false }] : [] });
    }),
  );
  return listing;
}

it("selects an exact file without parent-directory or write permission", async () => {
  api(emptyScope, true); const save = vi.fn().mockResolvedValue(true); const user = userEvent.setup();
  render(<MemoryRouter><RynAgentScope task={task} modelName="Model A" busy={false} onSave={save} /></MemoryRouter>);
  await user.click(screen.getByText("Task data access"));
  await user.click(await screen.findByRole("button", { name: "Add one file" }));
  await user.selectOptions(screen.getByRole("combobox", { name: "Connection 1" }), "google");
  expect(screen.queryByRole("combobox", { name: "Directory access 1" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Browse files" }));
  await user.click(await screen.findByRole("button", { name: "Select Secret text.txt" }));
  expect(screen.getByRole("button", { name: "Save task data access" })).toBeDisabled();
  await user.click(screen.getByRole("checkbox", { name: /Allow this task/ }));
  await user.click(screen.getByRole("button", { name: "Save task data access" }));
  await waitFor(() => expect(save).toHaveBeenCalledWith(emptyScope, {
    resources: [{ connection_id: "google", root_path: "file-id", root_kind: "file", recursive: false, actions: ["read_ai"] }],
    allow_model_data: true,
  }));
});

it("does not offer file grants when the backend lacks that capability", async () => {
  api(); const user = userEvent.setup();
  render(<MemoryRouter><RynAgentScope task={task} modelName="Model A" busy={false} onSave={vi.fn()} /></MemoryRouter>);
  await user.click(screen.getByText("Task data access"));
  await screen.findByRole("button", { name: "Add directory" });
  expect(screen.queryByRole("button", { name: "Add one file" })).not.toBeInTheDocument();
});

it("requires explicit create permissions and fresh data consent, and resets permissions on connection change", async () => {
  api(); const select = vi.fn(); const user = userEvent.setup();
  render(<MemoryRouter><RynAgentScope task={null} modelID="model-a" modelName="Model A" destinationRevision="destination-a" busy={false} onSelect={select} /></MemoryRouter>);
  await user.click(await screen.findByText("Task data access"));
  await user.click(screen.getByRole("button", { name: "Add directory" }));
  await user.selectOptions(screen.getByRole("combobox", { name: "Connection 1" }), "google");
  expect(screen.getByRole("option", { name: "Allow new files and directories" })).toBeDisabled();
  await user.selectOptions(screen.getByRole("combobox", { name: "Connection 1" }), "dav");
  const access = screen.getByRole("combobox", { name: "Directory access 1" });
  expect(access).toHaveValue("read");
  await user.click(screen.getByRole("checkbox", { name: /Allow this task/ }));
  await user.selectOptions(access, "create");
  expect(screen.getByRole("checkbox", { name: /Allow this task/ })).not.toBeChecked();
  await waitFor(() => expect(select).toHaveBeenLastCalledWith(expect.objectContaining({ resources: [expect.objectContaining({ actions: ["list", "read_ai", "create_file", "create_directory"] })], allow_model_data: false }), "model-a", "destination-a"));
  await user.click(screen.getByRole("checkbox", { name: /Allow this task/ }));
  await waitFor(() => expect(select).toHaveBeenLastCalledWith(expect.objectContaining({ allow_model_data: true }), "model-a", "destination-a"));
  await user.selectOptions(screen.getByRole("combobox", { name: "Connection 1" }), "google");
  expect(access).toHaveValue("read");
  expect(screen.getByRole("checkbox", { name: /Allow this task/ })).not.toBeChecked();
});

it("does not reuse saved data consent after the model destination changes", async () => {
  api({ ...emptyScope, model_destination: { catalog_model_id: "model-a", destination_revision: "old-destination" }, scope: { resource_grants: [], egress_grants: [], selection: { resources: [{ connection_id: "google", root_path: "opaque-id-42", recursive: false, actions: ["list", "read_ai"] }], allow_model_data: true } } });
  const save = vi.fn(); const user = userEvent.setup();
  render(<MemoryRouter><RynAgentScope task={task} modelName="Model A" busy={false} onSave={save} /></MemoryRouter>);
  await user.click(screen.getByText("Task data access"));
  expect(await screen.findByRole("checkbox", { name: /Allow this task/ })).not.toBeChecked();
  expect(screen.getByRole("button", { name: "Save task data access" })).toBeDisabled();
  expect(screen.getByText("opaque-id-42")).toBeVisible();
  await user.click(screen.getByRole("checkbox", { name: /Allow this task/ }));
  expect(screen.getByRole("button", { name: "Save task data access" })).toBeEnabled();
  expect(save).not.toHaveBeenCalled();
});

it("selects real directories by opaque ID and binds saved permissions to the current destination", async () => {
  const listing = api(); const save = vi.fn().mockResolvedValue(true); const user = userEvent.setup();
  render(<MemoryRouter><RynAgentScope task={task} modelName="Model A" busy={false} onSave={save} /></MemoryRouter>);
  await user.click(screen.getByText("Task data access"));
  await user.click(await screen.findByRole("button", { name: "Add directory" }));
  expect(screen.getByRole("option", { name: /Private drive.*AI access disabled/ })).toBeDisabled();
  await user.selectOptions(screen.getByRole("combobox", { name: "Connection 1" }), "google");
  await user.click(screen.getByRole("button", { name: "Browse directories" }));
  await user.click(await screen.findByRole("button", { name: "Reports" }));
  await user.click(await screen.findByRole("button", { name: "Use this directory" }));
  expect(listing).toHaveBeenCalledWith("opaque-id-42");
  expect(screen.queryByRole("button", { name: "Secret text.txt" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Save task data access" })).toBeDisabled();
  await user.click(screen.getByRole("checkbox", { name: "Allow this task to send selected directory contents to Model A." }));
  await user.click(screen.getByRole("button", { name: "Save task data access" }));
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
  const scope: AgentScopeSelection = { resources: [{ connection_id: "google", root_path: "opaque-id-42", recursive: false, actions: ["list", "read_ai"] }], allow_model_data: true };
  expect(save).toHaveBeenCalledWith(emptyScope, scope);
  expect(agentScopeBody(emptyScope, scope)).toEqual({ scope, expected_control_revision: 5, expected_scope_revision: 2, expected_destination_revision: "destination-a", client_request_id: expect.stringMatching(/^[a-f0-9-]{36}$/) });
});

it("allows revoking every directory without inventing a destination or keeping egress consent", async () => {
  const snapshot: AgentScopeSnapshot = { ...emptyScope, scope: { resource_grants: [{ grant_id: "real-grant" }], egress_grants: [], selection: { resources: [{ connection_id: "google", root_path: "opaque-id-42", recursive: true, actions: ["list", "read_ai"] }], allow_model_data: true } }, current_model_destination: null };
  api(snapshot); const save = vi.fn().mockResolvedValue(true); const user = userEvent.setup();
  render(<MemoryRouter><RynAgentScope task={task} modelName="Model A" busy={false} onSave={save} /></MemoryRouter>);
  await user.click(screen.getByText("Task data access"));
  await user.click(await screen.findByRole("button", { name: "Remove directory" }));
  await user.click(screen.getByRole("button", { name: "Save task data access" }));
  await waitFor(() => expect(save).toHaveBeenCalledWith(snapshot, { resources: [], allow_model_data: false }));
  expect(agentScopeBody(snapshot, { resources: [], allow_model_data: false })).not.toHaveProperty("expected_destination_revision");
});

it("requires fresh data consent after changing the new task model and preserves the directory", async () => {
  api(); const select = vi.fn(); const user = userEvent.setup();
  const view = render(<MemoryRouter><RynAgentScope task={null} modelID="model-a" modelName="Model A" destinationRevision="destination-a" busy={false} onSelect={select} /></MemoryRouter>);
  await user.click(await screen.findByText("Task data access"));
  await user.click(screen.getByRole("button", { name: "Add directory" }));
  await user.selectOptions(screen.getByRole("combobox", { name: "Connection 1" }), "google");
  await user.click(screen.getByRole("checkbox", { name: "Allow this task to send selected directory contents to Model A." }));
  await waitFor(() => expect(select).toHaveBeenLastCalledWith(expect.objectContaining({ allow_model_data: true }), "model-a", "destination-a"));
  view.rerender(<MemoryRouter><RynAgentScope task={null} modelID="model-b" modelName="Model B" destinationRevision="destination-b" busy={false} onSelect={select} /></MemoryRouter>);
  await waitFor(() => expect(select).toHaveBeenLastCalledWith(expect.objectContaining({ resources: [expect.objectContaining({ connection_id: "google" })], allow_model_data: false }), "model-b", "destination-b"));
  expect(screen.getByRole("checkbox", { name: "Allow this task to send selected directory contents to Model B." })).not.toBeChecked();
  expect(screen.queryByRole("button", { name: "Save task data access" })).not.toBeInTheDocument();
});

it("does not let an unverified data destination receive directory contents", async () => {
  api({ ...emptyScope, current_model_destination: null }); const save = vi.fn(); const user = userEvent.setup();
  render(<MemoryRouter><RynAgentScope task={task} modelName="CLI model" busy={false} onSave={save} /></MemoryRouter>);
  await user.click(screen.getByText("Task data access")); await user.click(await screen.findByRole("button", { name: "Add directory" }));
  await user.selectOptions(screen.getByRole("combobox", { name: "Connection 1" }), "google");
  expect(screen.getByText(/This model has no verified data destination/)).toBeVisible();
  expect(screen.getByRole("checkbox", { name: /Allow this task/ })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Save task data access" })).toBeDisabled();
  expect(save).not.toHaveBeenCalled();
});

it("keeps a failed directory listing from selecting an unseen path", async () => {
  api(); server.use(http.get("*/api/local/plugins/nas/sources/google/files", () => HttpResponse.json({ detail: "offline" }, { status: 503 })));
  const user = userEvent.setup(); render(<MemoryRouter><RynAgentScope task={task} modelName="Model A" busy={false} onSave={vi.fn()} /></MemoryRouter>);
  await user.click(screen.getByText("Task data access")); await user.click(await screen.findByRole("button", { name: "Add directory" }));
  await user.selectOptions(screen.getByRole("combobox", { name: "Connection 1" }), "google"); await user.click(screen.getByRole("button", { name: "Browse directories" }));
  const picker = screen.getByRole("group", { name: "Browse directories" });
  await within(picker).findByRole("alert"); expect(within(picker).queryByRole("button", { name: "Use this directory" })).not.toBeInTheDocument();
});
