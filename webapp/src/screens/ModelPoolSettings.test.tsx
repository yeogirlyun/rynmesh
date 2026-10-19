import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { newSource, type AISource } from "../domain/aiSources";
import ModelPoolSettings from "./ModelPoolSettings";

beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
});
const source: AISource = { ...newSource("custom"), id: "source-a", name: "My source", context_window: 32000, max_output_tokens: 4000, models: ["model-a", "model-b"], model_settings: { "model-a": { overrides: { max_output_tokens: 2000 }, effective: { context_window: 32000, max_output_tokens: 2000, request_defaults: {}, max_concurrent: 1, requests_per_day: null }, inherited_fields: ["context_window", "request_defaults", "max_concurrent", "requests_per_day"], rule_revision: "rule-1", destination_revision: "dest-1", evidence: { tools: { status: "unknown", checked_at: null } } } } };

it("uses this exact model's discovered capacity when inheriting an automatic source", async () => {
  const settings = source.model_settings!["model-a"];
  const discovered: AISource = { ...source, context_window: 32768, context_window_mode: "auto",
    model_metadata: { "model-a": { context_window: 1050000 } }, model_settings: { "model-a": {
      ...settings, context_window_known: true, context_window_basis: "provider_model_metadata",
      effective: { ...settings.effective, context_window: 1050000 } } } };
  const writes = vi.fn();
  server.use(http.put("*/api/local/llm/sources/source-a/models/config", async ({ request }) => {
    writes(await request.json()); return HttpResponse.json(discovered);
  }));
  const user = userEvent.setup();
  render(<ModelPoolSettings source={discovered} modelID="model-a" onClose={vi.fn()} onSaved={vi.fn().mockResolvedValue(undefined)} />);
  expect(screen.getByText(/window comes from this source's model listing/)).toBeVisible();
  await user.clear(screen.getByLabelText("Maximum output tokens"));
  await user.type(screen.getByLabelText("Maximum output tokens"), "128000");
  await user.click(screen.getByRole("button", { name: "Save this model's parameters" }));
  await waitFor(() => expect(writes).toHaveBeenCalledOnce());
  expect(writes).toHaveBeenCalledWith(expect.objectContaining({ configuration: { max_output_tokens: 128000 } }));
});

it("shows inherited versus override values and saves only this model with its exact rule version", async () => {
  const received = vi.fn(); const saved = vi.fn().mockResolvedValue(undefined); const close = vi.fn();
  server.use(http.put("*/api/local/llm/sources/source-a/models/config", async ({ request }) => { received(await request.json()); return HttpResponse.json(source); }));
  const user = userEvent.setup(); render(<ModelPoolSettings source={source} modelID="model-a" onClose={close} onSaved={saved} />);
  expect(screen.getByLabelText("Maximum output tokens")).toHaveValue(2000);
  expect(screen.getByLabelText("Context window")).toHaveValue(null);
  expect(screen.getByText(/Currently effective: 2000.*Model override/)).toBeVisible();
  await user.clear(screen.getByLabelText("Maximum output tokens")); await user.type(screen.getByLabelText("Maximum output tokens"), "3000");
  await user.click(screen.getByRole("button", { name: "Save this model's parameters" }));
  await waitFor(() => expect(saved).toHaveBeenCalledOnce());
  expect(received).toHaveBeenCalledWith({ model_id: "model-a", expected_rule_revision: "rule-1", configuration: { max_output_tokens: 3000 } });
  expect(close).toHaveBeenCalledOnce();
});

it("does not save invalid effective output/context combinations", async () => {
  const received = vi.fn(); server.use(http.put("*/api/local/llm/sources/source-a/models/config", () => { received(); return HttpResponse.json(source); }));
  const user = userEvent.setup(); render(<ModelPoolSettings source={source} modelID="model-a" onClose={vi.fn()} onSaved={vi.fn()} />);
  await user.type(screen.getByLabelText("Context window"), "1000");
  await user.click(screen.getByRole("button", { name: "Save this model's parameters" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Maximum output cannot exceed the context window");
  expect(received).not.toHaveBeenCalled();
});

it("resets only the selected override to inheritance", async () => {
  const received = vi.fn();
  server.use(http.put("*/api/local/llm/sources/source-a/models/config", async ({ request }) => { received(await request.json()); return HttpResponse.json(source); }));
  const user = userEvent.setup(); render(<ModelPoolSettings source={source} modelID="model-a" onClose={vi.fn()} onSaved={vi.fn()} />);
  await user.click(screen.getAllByRole("button", { name: /^Inherit source$/ })[1]);
  await user.click(screen.getByRole("button", { name: "Save this model's parameters" }));
  await waitFor(() => expect(received).toHaveBeenCalledWith({ model_id: "model-a", expected_rule_revision: "rule-1", configuration: { max_output_tokens: null } }));
});

it("does not overwrite changed model rules and requires explicit reload", async () => {
  const received = vi.fn(); const updated = { ...source, model_settings: { "model-a": { ...source.model_settings!["model-a"], rule_revision: "rule-2", overrides: { max_output_tokens: 2400 } } } };
  server.use(http.put("*/api/local/llm/sources/source-a/models/config", async ({ request }) => { received(await request.json()); return received.mock.calls.length === 1 ? HttpResponse.json({ detail: "model_rule_revision_conflict" }, { status: 409 }) : HttpResponse.json(updated); }), http.get("*/api/local/llm/sources", () => HttpResponse.json({ sources: [updated] })));
  const user = userEvent.setup(); render(<ModelPoolSettings source={source} modelID="model-a" onClose={vi.fn()} onSaved={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: "Save this model's parameters" }));
  await screen.findByText(/configuration changed/);
  expect(screen.getByRole("button", { name: "Save this model's parameters" })).toBeDisabled();
  expect(received).toHaveBeenCalledOnce();
  await user.click(screen.getByRole("button", { name: "Reload" }));
  await waitFor(() => expect(screen.getByLabelText("Maximum output tokens")).toHaveValue(2400));
  await user.click(screen.getByRole("button", { name: "Save this model's parameters" }));
  await waitFor(() => expect(received).toHaveBeenCalledTimes(2));
  expect(received.mock.calls[1][0].expected_rule_revision).toBe("rule-2");
});

it("never probes on open and sends explicit per-model probe kinds without modifying defaults", async () => {
  const probes = vi.fn(); const saved = vi.fn();
  server.use(http.post("*/api/local/llm/sources/source-a/models/test", async ({ request }) => { probes(await request.json()); return HttpResponse.json({ status: "passed" }); }), http.get("*/api/local/llm/sources", () => HttpResponse.json({ sources: [source] })));
  const user = userEvent.setup(); render(<ModelPoolSettings source={source} modelID="model-a" onClose={vi.fn()} onSaved={saved} />);
  expect(probes).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Check model listing" }));
  await waitFor(() => expect(saved).toHaveBeenCalledOnce());
  expect(probes).toHaveBeenCalledWith({ model_id: "model-a", probe: "model_exists" });
});

it("reloads failed probe evidence instead of retaining a prior success", async () => {
  const failed = { ...source, model_settings: { "model-a": { ...source.model_settings!["model-a"], evidence: { connection: { status: "failed", checked_at: 123, method: "chat" } } } } };
  server.use(http.post("*/api/local/llm/sources/source-a/models/test", () => HttpResponse.json({ detail: "source_connection_failed" }, { status: 400 })), http.get("*/api/local/llm/sources", () => HttpResponse.json({ sources: [failed] })));
  const user = userEvent.setup(); render(<ModelPoolSettings source={source} modelID="model-a" onClose={vi.fn()} onSaved={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: "Probe connection" }));
  await screen.findByText(/The probe did not pass/);
  await user.click(screen.getByText("Effective request parameters and evidence"));
  expect(screen.getByText(/"status": "failed"/)).toHaveTextContent('"checked_at": 123');
});

it("does not pair old unsaved edits with a rule version changed during a probe", async () => {
  const writes = vi.fn();
  const changed = { ...source, model_settings: { "model-a": { ...source.model_settings!["model-a"], rule_revision: "rule-2", overrides: { max_output_tokens: 2500 } } } };
  server.use(http.post("*/api/local/llm/sources/source-a/models/test", () => HttpResponse.json({ status: "passed" })), http.get("*/api/local/llm/sources", () => HttpResponse.json({ sources: [changed] })), http.put("*/api/local/llm/sources/source-a/models/config", () => { writes(); return HttpResponse.json(changed); }));
  const user = userEvent.setup(); render(<ModelPoolSettings source={source} modelID="model-a" onClose={vi.fn()} onSaved={vi.fn()} />);
  await user.clear(screen.getByLabelText("Maximum output tokens")); await user.type(screen.getByLabelText("Maximum output tokens"), "3000");
  await user.click(screen.getByRole("button", { name: "Probe connection" }));
  await screen.findByText(/configuration changed during the probe/);
  expect(screen.getByRole("button", { name: "Save this model's parameters" })).toBeDisabled();
  expect(writes).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Reload" }));
  await waitFor(() => expect(screen.getByLabelText("Maximum output tokens")).toHaveValue(2500));
});
