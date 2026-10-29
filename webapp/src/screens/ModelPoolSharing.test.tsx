import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { newSource, type AISource } from "../domain/aiSources";
import type { AIGrant } from "../domain/aiAccess";
import ModelPoolSharing from "./ModelPoolSharing";

const modelSettings = { overrides: {}, effective: { context_window: 32000, max_output_tokens: 4000, max_concurrent: 1, requests_per_day: null, request_defaults: {}, share_scope: "personal_space" as const }, inherited_fields: [], rule_revision: "rule-1", destination_revision: "dest-1", evidence: {} };
const source: AISource = { ...newSource("custom"), id: "source-a", name: "My source", models: ["alpha", "beta"], source_revision: "source-1", sharing: true, shared_models: ["beta"], model_settings: { alpha: modelSettings } };
beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
});
function api(initial = source, cas = true) {
  let current = initial; let grants: AIGrant[] = [];
  const rules = vi.fn(); const shares = vi.fn(); const permissions = vi.fn();
  server.use(
    http.get("*/api/local/llm/sources", () => HttpResponse.json({ sources: [current], source_revision_cas_supported: cas })),
    http.get("*/api/local/friends", () => HttpResponse.json({ friends: [{ relationship_id: "friend-a", peer_id: "peer-a", node_name: "Receiver A", status: "active" }, { relationship_id: "friend-b", peer_id: "peer-b", node_name: "Receiver B", status: "active" }, { relationship_id: "old-friend", peer_id: "old-peer", node_name: "Revoked receiver", status: "revoked" }] })),
    http.get("*/api/local/ai-access", () => HttpResponse.json({ grants })),
    http.put("*/api/local/llm/sources/source-a/models/config", async ({ request }) => {
      const body = await request.json() as { configuration: { share_scope: "friends" }; expected_rule_revision: string }; rules(body);
      current = { ...current, source_revision: "source-2", model_settings: { alpha: { ...modelSettings, effective: { ...modelSettings.effective, ...body.configuration }, rule_revision: "rule-2" } } }; return HttpResponse.json(current);
    }),
    http.put("*/api/local/llm/sources/source-a", async ({ request }) => {
      const body = await request.json() as { shared_models: string[]; expected_source_revision: string }; shares(body);
      current = { ...current, source_revision: "source-3", shared_models: body.shared_models, sharing: body.shared_models.length > 0 }; return HttpResponse.json(current);
    }),
    http.put("*/api/local/ai-access/:service/:relationship", async ({ request, params }) => {
      const body = await request.json() as { allowed: boolean; expected_revision: number }; permissions(params.service, params.relationship, body);
      const grant = { service_id: String(params.service), relationship_id: String(params.relationship), peer_id: "peer-a", allowed: body.allowed, effective: body.allowed, revision: body.expected_revision + 1 };
      grants = [...grants.filter(row => row.relationship_id !== grant.relationship_id), grant]; return HttpResponse.json({ grant, cancellation: "not_requested" });
    }),
  );
  return { rules, shares, permissions };
}
async function mount(initial = source, cas = true, serviceID: string | undefined = "catalog-alpha-service") {
  const calls = api(initial, cas); const user = userEvent.setup();
  render(<ModelPoolSharing source={initial} modelID="alpha" serviceID={serviceID} onClose={vi.fn()} onSaved={vi.fn().mockResolvedValue(undefined)} />);
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model audience" })).toBeEnabled());
  return { user, ...calls };
}

it("saves audience, sharing and exact receiver grants as independent versioned choices", async () => {
  const { user, rules, shares, permissions } = await mount();
  await user.selectOptions(screen.getByRole("combobox"), "friends");
  expect(rules).not.toHaveBeenCalled(); expect(shares).not.toHaveBeenCalled(); expect(permissions).not.toHaveBeenCalled();
  expect(screen.queryByText("Revoked receiver")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Save audience" }));
  await screen.findByText(/Scope saved/);
  expect(rules).toHaveBeenCalledWith({ model_id: "alpha", expected_rule_revision: "rule-1", configuration: { share_scope: "friends" } });
  expect(screen.getByRole("checkbox")).not.toBeChecked();
  const receiverA = screen.getByText("Receiver A").closest(".mp-friend-grant") as HTMLElement;
  await user.click(within(receiverA).getByRole("button", { name: "Allow this friend" }));
  await waitFor(() => expect(permissions).toHaveBeenCalledWith("catalog-alpha-service", "friend-a", { allowed: true, expected_revision: 0 }));
  expect(shares).not.toHaveBeenCalled();
  expect(await within(receiverA).findByText("Grant recorded; not currently effective")).toBeVisible();
  await user.click(screen.getByRole("checkbox")); await user.click(screen.getByRole("button", { name: "Save sharing switch" }));
  await waitFor(() => expect(shares).toHaveBeenCalledWith({ shared_models: ["beta", "alpha"], expected_source_revision: "source-2" }));
  await user.click(within(receiverA).getByRole("button", { name: "Revoke this friend" }));
  await waitFor(() => expect(permissions).toHaveBeenLastCalledWith("catalog-alpha-service", "friend-a", { allowed: false, expected_revision: 1 }));
});

it("requires disabling already-shared models before changing their audience", async () => {
  const { user, rules, shares } = await mount({ ...source, shared_models: ["alpha", "beta"] });
  await user.selectOptions(screen.getByRole("combobox"), "friends");
  expect(screen.getByRole("button", { name: "Save audience" })).toBeDisabled();
  await user.click(screen.getByRole("checkbox")); await user.click(screen.getByRole("button", { name: "Save sharing switch" }));
  await waitFor(() => expect(shares).toHaveBeenCalledWith({ shared_models: ["beta"], expected_source_revision: "source-1" }));
  expect(rules).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Save audience" })).toBeEnabled();
});

it("does not mistake a source revision alone for server CAS support", async () => {
  const { user, shares } = await mount(source, false);
  await user.click(screen.getByRole("checkbox"));
  expect(screen.getByRole("button", { name: "Save sharing switch" })).toBeDisabled();
  expect(screen.getByText(/version-checked sharing interface/)).toBeVisible();
  expect(shares).not.toHaveBeenCalled();
});

it("blocks stale or unconfirmed sharing changes until explicit reload", async () => {
  const { user, shares } = await mount();
  server.use(http.put("*/api/local/llm/sources/source-a", async ({ request }) => { shares(await request.json()); return HttpResponse.json({ detail: "source_revision_conflict" }, { status: 409 }); }));
  await user.click(screen.getByRole("checkbox")); await user.click(screen.getByRole("button", { name: "Save sharing switch" }));
  await screen.findByText(/operation was not confirmed/);
  expect(screen.getByRole("button", { name: "Save sharing switch" })).toBeDisabled();
  expect(screen.getByRole("combobox")).toBeDisabled();
  expect(shares).toHaveBeenCalledOnce();
  await user.click(screen.getByRole("button", { name: "Reload actual state" }));
  await waitFor(() => expect(screen.getByRole("combobox")).toBeEnabled());
  expect(screen.getByRole("checkbox")).not.toBeChecked();
});

it("never guesses the service identity when the model catalog omits it", async () => {
  const initial: AISource = { ...source, model_settings: { alpha: { ...modelSettings, effective: { ...modelSettings.effective, share_scope: "friends" } } } };
  const calls = api(initial);
  render(<ModelPoolSharing source={initial} modelID="alpha" onClose={vi.fn()} onSaved={vi.fn()} />);
  expect(await screen.findByText(/catalog has not provided a stable service identity/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "Allow this friend" })).not.toBeInTheDocument();
  expect(calls.permissions).not.toHaveBeenCalled();
});
