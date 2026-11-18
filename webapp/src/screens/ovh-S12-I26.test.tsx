import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { expect, it, vi } from "vitest";
import { newSource, type AISource } from "../domain/aiSources";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import type { AppOutletContext } from "../appContext";
import { server } from "../test/server";
import AISources from "./AISources";
import ModelPoolSettings from "./ModelPoolSettings";

it("S12-I26 labels a vendor family window as inferred instead of configured", () => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  const source: AISource = { ...newSource("custom"), id: "family-source", name: "Family API", models: ["deepseek-chat"],
    model_settings: { "deepseek-chat": { overrides: {}, context_window_known: false, context_window_basis: "vendor_family_guess",
      effective: { context_window: 1048576, max_output_tokens: 16384, request_defaults: {}, max_concurrent: 1, requests_per_day: null },
      inherited_fields: ["context_window"], rule_revision: "rule", destination_revision: "dest",
      evidence: { tools: { status: "unknown", checked_at: null } } } } };
  render(<ModelPoolSettings source={source} modelID="deepseek-chat" onClose={vi.fn()} onSaved={vi.fn().mockResolvedValue(undefined)} />);
  expect(screen.getByText(/inferred from the model family|由模型族名推测/)).toBeVisible();
  expect(screen.queryByText(/currently in use|当前使用手动配置/)).toBeNull();
});

it("S12-I26 source drafts inherit output limits and use the documented DeepSeek conservative default", () => {
  expect(newSource("custom").max_output_tokens).toBeNull();
  expect(newSource("deepseek").max_output_tokens).toBe(65536);
});

it.each([null, 4096])("S12-I26 source editor saves unset or explicitly typed output %s", async output => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  const stored = { ...newSource("custom"), id: "unset-source", name: "Unset output", models: ["model-a"],
    max_output_tokens: null, max_output_tokens_explicit: false };
  let received: Record<string, unknown> | undefined;
  server.use(
    http.get("*/api/local/llm/sources", () => HttpResponse.json({ sources: [stored], peer_id: "self", node_name: "My PC" })),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [] })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json({ enabled: false, model_id: "" })),
    http.get("*/api/local/llm/native", () => HttpResponse.json({ installed: false, supported: true, state: "not_installed" })),
    http.put("*/api/local/llm/sources/unset-source", async ({ request }) => {
      received = await request.json() as Record<string, unknown>;
      return HttpResponse.json({ ...stored, ...received });
    }),
  );
  const context = { client: makeFixtureNodeClient(), confirm: vi.fn(), notify: vi.fn() } as unknown as AppOutletContext;
  render(<MemoryRouter initialEntries={["/services/sources?edit=unset-source"]}><Routes><Route element={<Outlet context={context} />}>
    <Route path="/services/sources" element={<AISources />} />
  </Route></Routes></MemoryRouter>);
  const field = await screen.findByLabelText(/max.*output.*token|最大输出 Token/i);
  expect(field).toHaveValue(null);
  const user = userEvent.setup();
  const details = field.closest("details")!;
  if (!details.open) await user.click(details.querySelector("summary")!);
  if (output !== null) await user.type(field, String(output));
  const save = screen.getByRole("button", { name: /^Save$|^保存$/ });
  expect(save).toBeEnabled();
  await user.click(save);
  await waitFor(() => expect(received?.max_output_tokens).toBe(output));
  expect(received?.max_output_tokens_explicit).toBe(output !== null);
});
