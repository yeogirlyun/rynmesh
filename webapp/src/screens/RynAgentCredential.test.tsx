import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import type { AgentTask } from "../domain/rynAgent";
import RynAgentCredential from "./RynAgentCredential";

const task: AgentTask = { id: "credential-task", title: "Configure source", model_id: "model", status: "waiting_input", revision: 3, control_revision: 3, created_at: 1, updated_at: 1, pending_approval: null, steps: 2, result: "", error: null, messages: [], events: [], limits: {},
  goal: { revision: 1, kind: "action", original_message_id: "owner", text: "Configure", constraints: [] },
  pending_input: { id: "question", kind: "credential_ref", prompt: "Credential", fields: [], goal_revision: 1, control_revision: 3, scope_revision: 1, business_revision: null, created_at: 1 },
  allowed_actions: { source_credential: { enabled: true, reason: null, requires_control_revision: true } } };
beforeEach(() => { sessionStorage.clear(); localStorage.clear(); });
async function fill() {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Confirm endpoint"), "https://vendor.test/v1");
  await user.type(screen.getByLabelText("API Key"), "fixture-secret-731");
  await user.click(screen.getByLabelText(/I confirm this endpoint/));
  return user;
}

it("sends secrets only to the bound vault endpoint and clears them on success", async () => {
  const received = vi.fn(); const saved = vi.fn();
  server.use(http.post("*/api/local/agent/tasks/credential-task/source-credential", async ({ request }) => {
    received(await request.json()); return HttpResponse.json({ credential: { has_credential: true }, task: { ...task, status: "queued", pending_input: null } });
  }));
  render(<RynAgentCredential task={task} busy={false} onSaved={saved} />);
  const user = await fill();
  expect(sessionStorage.length).toBe(0); expect(localStorage.length).toBe(0);
  await user.click(screen.getByRole("button", { name: "Save securely and continue" }));
  await waitFor(() => expect(saved).toHaveBeenCalled());
  expect(received).toHaveBeenCalledWith(expect.objectContaining({ api_key: "fixture-secret-731", expected_control_revision: 3, expected_goal_revision: 1, pending_input_id: "question", base_url: "https://vendor.test/v1" }));
  expect(screen.getByLabelText("API Key")).toHaveValue("");
});

it("keeps uncertain retry in memory with the exact original versions and UUID", async () => {
  const received = vi.fn();
  server.use(http.post("*/api/local/agent/tasks/credential-task/source-credential", async ({ request }) => {
    received(await request.json()); return received.mock.calls.length === 1 ? HttpResponse.json({}, { status: 503 }) : HttpResponse.json({ credential: {}, task });
  }));
  const view = render(<RynAgentCredential task={task} busy={false} onSaved={vi.fn()} />);
  const user = await fill(); await user.click(screen.getByRole("button", { name: "Save securely and continue" }));
  await screen.findByRole("button", { name: "Retry the same save" });
  expect(screen.getByLabelText("Confirm endpoint")).toBeDisabled();
  expect(sessionStorage.length).toBe(0); expect(localStorage.length).toBe(0);
  view.rerender(<RynAgentCredential task={{ ...task, control_revision: 99 }} busy={false} onSaved={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: "Retry the same save" }));
  await waitFor(() => expect(received).toHaveBeenCalledTimes(2));
  expect(received.mock.calls[0][0]).toEqual(received.mock.calls[1][0]);
});

it("requires explicit endpoint consent and a current server grant", async () => {
  const view = render(<RynAgentCredential task={task} busy={false} onSaved={vi.fn()} />);
  const user = await fill();
  expect(screen.getByRole("button", { name: "Save securely and continue" })).toBeEnabled();
  await user.type(screen.getByLabelText("Confirm endpoint"), "/changed");
  expect(screen.getByRole("button", { name: "Save securely and continue" })).toBeDisabled();
  await user.click(screen.getByLabelText(/I confirm this endpoint/));
  view.rerender(<RynAgentCredential task={{ ...task, allowed_actions: {} }} busy={false} onSaved={vi.fn()} />);
  expect(screen.getByRole("button", { name: "Save securely and continue" })).toBeDisabled();
});
