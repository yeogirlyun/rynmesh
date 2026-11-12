import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { http, HttpResponse } from "msw";
import i18n from "../i18n";
import { server } from "../test/server";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { newSource, type AISource } from "../domain/aiSources";
import type { AgentModel } from "../domain/rynAgent";
import type { AppOutletContext } from "../appContext";
import ModelPool from "./ModelPool";

beforeEach(async () => { await i18n.changeLanguage("zh-CN"); });
afterEach(() => vi.restoreAllMocks());

const PLUGIN = "company-workspace";
const source = (): AISource => ({ ...newSource(), id: "src-1", name: "My API", models: ["model-a", "model-b"], has_key: true, sharing: false, shared_models: [], status: "ready" });
const own: Array<AgentModel & { source_id: string; model_id: string }> = [
  { id: "local/svc-a", source_id: "src-1", model_id: "model-a", service_id: "svc-a", name: "model-a", source: "local_api", available: true, agent_supported: true },
  { id: "local/svc-b", source_id: "src-1", model_id: "model-b", service_id: "svc-b", name: "model-b", source: "local_api", available: true, agent_supported: true },
];
const shared: AgentModel = { id: "provider/abc/peer/def/codex-cli", name: "Codex", source: "peer", source_name: "老板", service_id: "codex-cli", peer_id: "boss-device", provider_resource: "https://company.test/ryn/connect", available: true, agent_supported: true };

function company({ publisher = true, signedIn = true, selected = [] as string[], models = own as AgentModel[], usage = { day: "2026-11-12", sharing: false, published_service_ids: [] as string[], rows: [] as unknown[] } } = {}) {
  const published: string[][] = [];
  let current = [...selected];
  const status = () => ({ plugin_id: PLUGIN, status: signedIn ? "connected" : "signed_out", bound: signedIn, selected_service_ids: current });
  server.use(
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json({ enabled: false, model_id: "", permission_mode: "confirm_changes" })),
    http.get("*/api/local/peer-providers", () => HttpResponse.json({ providers: [status()] })),
    http.get(`*/api/local/peer-providers/${PLUGIN}/status`, () => HttpResponse.json(status())),
    http.get(`*/api/local/peer-providers/${PLUGIN}/workspace`, () => HttpResponse.json({ ...status(), can_publish_models: publisher, current_member: { id: "m1", name: "老板", role: "member", can_publish_models: true }, colleagues: [], models: [] })),
    // Only model-a is eligible right now (model-b is, say, not ready).
    http.get(`*/api/local/peer-providers/${PLUGIN}/models`, () => HttpResponse.json({ models: [{ service_id: "svc-a", name: "model-a" }] })),
    http.post(`*/api/local/peer-providers/${PLUGIN}/publish`, async ({ request }) => {
      const body = await request.json() as { service_ids: string[] };
      published.push(body.service_ids);
      current = body.service_ids;
      return HttpResponse.json(status());
    }),
    http.get("*/api/local/llm/models/usage/company", () => HttpResponse.json(usage)),
  );
  return published;
}

function page() {
  const context = { client: makeFixtureNodeClient() } as AppOutletContext;
  render(<MemoryRouter initialEntries={["/models"]}><Routes><Route element={<Outlet context={context} />}><Route path="/models" element={<ModelPool sources={[source()]} cli={null} native={null} peers={[]} loading={false} error="" onRefresh={vi.fn().mockResolvedValue(undefined)} onEditSource={vi.fn()} onCLI={vi.fn()} onNative={vi.fn()} />} /></Route></Routes></MemoryRouter>);
  return userEvent.setup();
}

it("lets a publisher share one model with company colleagues without touching other choices", async () => {
  const published = company({ selected: ["other-service"] });
  const user = page();
  const box = await screen.findByRole("checkbox", { name: "共享给公司同事 model-a" });
  expect(box).not.toBeChecked();
  // A model the company channel cannot take right now gets no switch.
  expect(screen.queryByRole("checkbox", { name: "共享给公司同事 model-b" })).not.toBeInTheDocument();
  await user.click(box);
  await waitFor(() => expect(published).toEqual([["other-service", "svc-a"]]));
  expect(await screen.findByRole("checkbox", { name: "共享给公司同事 model-a" })).toBeChecked();
  expect(screen.getByRole("status")).toHaveTextContent("已把 model-a 共享给公司同事。家庭组和好友设置不变。");
  // The personal-space switch of the same model is independent and unchanged.
  expect(screen.getByRole("button", { name: "分享 model-a" })).toHaveTextContent("仅自己使用");
  await user.click(screen.getByRole("checkbox", { name: "共享给公司同事 model-a" }));
  await waitFor(() => expect(published[1]).toEqual(["other-service"]));
});

it.each([["not a publisher", { publisher: false }], ["not signed in", { signedIn: false }]])("shows no company switch when %s", async (_label, options) => {
  company(options);
  page();
  expect(await screen.findByRole("button", { name: "分享 model-a" })).toBeVisible();
  await waitFor(() => expect(screen.getByText("2 个模型")).toBeVisible());
  expect(screen.queryByRole("checkbox", { name: /共享给公司同事/ })).not.toBeInTheDocument();
  expect(screen.queryByText("公司同事用量")).not.toBeInTheDocument();
});

it("groups models shared by colleagues with the provider's name and reachability", async () => {
  company({ publisher: false, models: [...own, shared, { ...shared, id: shared.id + "-2", name: "Claude Code", source_name: "管理员", available: false, agent_supported: false }] });
  page();
  expect(await screen.findByRole("heading", { name: /公司共享/ })).toBeVisible();
  const row = screen.getByText("Codex").closest(".mp-model") as HTMLElement;
  expect(within(row).getByText("提供人 老板 · 在线")).toBeVisible();
  expect(within(row).getByRole("button", { name: "用于助手" })).toBeEnabled();
  expect(within(screen.getByText("Claude Code").closest(".mp-model") as HTMLElement).getByText("提供人 管理员 · 离线")).toBeVisible();
  // Received models carry no sharing switch of their own.
  expect(within(row).queryByRole("checkbox")).not.toBeInTheDocument();
});

it("lists per-colleague usage only while this device shares with the company", async () => {
  company({ selected: ["svc-a"], usage: { day: "2026-11-12", sharing: true, published_service_ids: ["svc-a"], rows: [
    { model_key: "local/svc-a", model_name: "My API · model-a", peer_id: "peer-colleague-1", member_name: "李同事", last_used_at: null,
      today: { requests: 3, input_tokens: 1000, output_tokens: 500, total_tokens: 1500 },
      last_30_days: { requests: 40, input_tokens: 90000, output_tokens: 30000, total_tokens: 120000 } },
    { model_key: "local/svc-a", model_name: "My API · model-a", peer_id: "peer-unknown-2", member_name: "", last_used_at: null,
      today: { requests: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0 },
      last_30_days: { requests: 2, input_tokens: 10, output_tokens: 10, total_tokens: 20 } },
  ] } });
  page();
  const table = within(await screen.findByRole("region", { name: "公司同事用量" })).getByRole("table");
  const cells = (name: string) => within(within(table).getByText(name).closest("tr") as HTMLElement).getAllByRole("cell").map(cell => cell.textContent);
  expect(cells("李同事")).toEqual(["李同事", "My API · model-a", "3", "1.5K", "40", "120K"]);
  expect(cells("未知设备 peer-unk")).toEqual(["未知设备 peer-unk", "My API · model-a", "0", "0", "2", "20"]);
  expect(screen.getByText(/不设限额/)).toBeVisible();
});
