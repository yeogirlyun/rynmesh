import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { http, HttpResponse } from "msw";
import i18n from "../i18n";
import { server } from "../test/server";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { newSource, type AISource } from "../domain/aiSources";
import { rynAgent, type AgentModel, type AgentSettings } from "../domain/rynAgent";
import type { AppOutletContext } from "../appContext";
import ModelPool from "./ModelPool";

beforeEach(async () => {
  await i18n.changeLanguage("zh-CN");
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
});
afterEach(() => vi.restoreAllMocks());
const source = (): AISource => ({ ...newSource(), id: "src-1", name: "My API", models: ["model-a", "model-b"], has_key: true, sharing: true, shared_models: ["model-a"], status: "ready" });
function page(value = source()) {
  const context = { client: makeFixtureNodeClient() } as AppOutletContext;
  const onRefresh = vi.fn().mockResolvedValue(undefined);
  render(<MemoryRouter initialEntries={["/models"]}><Routes><Route element={<Outlet context={context} />}><Route path="/models" element={<ModelPool sources={[value]} cli={null} native={null} peers={[]} loading={false} error="" onRefresh={onRefresh} onEditSource={vi.fn()} onCLI={vi.fn()} onNative={vi.fn()} />} /></Route></Routes></MemoryRouter>);
  return userEvent.setup();
}
const catalog: Array<AgentModel & { source_id: string; model_id: string }> = [{ id: "local/a", source_id: "src-1", model_id: "model-a", name: "model-a", source: "local_api", available: true, agent_supported: true }, { id: "local/b", source_id: "src-1", model_id: "model-b", name: "model-b", source: "local_api", available: true, agent_supported: false, tool_support: "unknown" }];
const enabledSettings = (model_id = "local/a"): AgentSettings => ({ enabled: true, model_id, permission_mode: "confirm_changes", allowed_write_tools: [], max_steps: 16, max_seconds: 180, max_context_chars: 60000, max_output_chars: 20000 });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(accept => { resolve = accept; });
  return { promise, resolve };
}
function endpoints(model_id = "local/a") {
  server.use(
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: catalog })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json({ enabled: false, model_id, permission_mode: "confirm_changes" })),
  );
}
it("applies enabled settings immediately while the initial model catalog is still loading", async () => {
  const models = deferred<{ models: AgentModel[] }>();
  vi.spyOn(rynAgent, "models").mockReturnValue(models.promise);
  vi.spyOn(rynAgent, "settings").mockResolvedValue(enabledSettings());
  page();
  expect(await screen.findByText("已启用")).toBeVisible();
  expect(screen.getAllByText("正在读取助手模型…")).toHaveLength(2);
  expect(screen.queryByText("助手状态暂不可用")).not.toBeInTheDocument();
  expect(screen.queryByText("可选功能")).not.toBeInTheDocument();
  expect(screen.queryByText("尚未选择")).not.toBeInTheDocument();
  await act(async () => { models.resolve({ models: catalog }); });
  expect(await screen.findByRole("button", { name: "重新确认用于助手" })).toBeEnabled();
});

it("shows a loading status instead of disabled copy while the initial settings request is pending", async () => {
  const settings = deferred<AgentSettings>();
  vi.spyOn(rynAgent, "models").mockResolvedValue({ models: catalog });
  vi.spyOn(rynAgent, "settings").mockReturnValue(settings.promise);
  page();
  expect(await screen.findByRole("button", { name: "用于助手" })).toBeEnabled();
  expect(screen.getByText("正在读取助手状态…")).toBeVisible();
  expect(screen.queryByText("可选功能")).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "了解并启用 →" })).not.toBeInTheDocument();
  await act(async () => { settings.resolve(enabledSettings()); });
  expect(await screen.findByText("已启用")).toBeVisible();
});

it("keeps the loaded catalog and enabled settings visible during a refresh", async () => {
  const models = deferred<{ models: AgentModel[] }>(), settings = deferred<AgentSettings>();
  vi.spyOn(rynAgent, "models").mockResolvedValueOnce({ models: catalog }).mockReturnValueOnce(models.promise);
  vi.spyOn(rynAgent, "settings").mockResolvedValueOnce(enabledSettings()).mockReturnValueOnce(settings.promise);
  const user = page();
  await screen.findByRole("button", { name: "重新确认用于助手" });
  await user.click(screen.getByRole("button", { name: "刷新模型" }));
  expect(screen.getByText("已启用")).toBeVisible();
  expect(screen.getByText("默认使用 model-a")).toBeVisible();
  expect(screen.getByRole("button", { name: "重新确认用于助手" })).toBeVisible();
  expect(screen.queryByText("正在读取助手状态…")).not.toBeInTheDocument();
  expect(screen.queryByText("助手状态暂不可用")).not.toBeInTheDocument();
  await act(async () => { models.resolve({ models: catalog }); settings.resolve(enabledSettings()); });
  await waitFor(() => expect(screen.getByRole("button", { name: "刷新模型" })).toBeEnabled());
});

it("ignores older catalog and settings responses after a newer refresh completes", async () => {
  const oldModels = deferred<{ models: AgentModel[] }>(), oldSettings = deferred<AgentSettings>();
  const models = vi.spyOn(rynAgent, "models").mockReturnValueOnce(oldModels.promise).mockResolvedValueOnce({ models: catalog.map(model => ({ ...model, agent_supported: true })) });
  const settings = vi.spyOn(rynAgent, "settings").mockReturnValueOnce(oldSettings.promise).mockResolvedValueOnce(enabledSettings("local/b"));
  const user = page();
  await waitFor(() => { expect(models).toHaveBeenCalledOnce(); expect(settings).toHaveBeenCalledOnce(); });
  await user.click(screen.getByRole("button", { name: "刷新模型" }));
  expect(await screen.findByText("默认使用 model-b")).toBeVisible();
  await act(async () => { oldModels.resolve({ models: [] }); oldSettings.resolve({ ...enabledSettings(), enabled: false }); });
  expect(screen.getByText("已启用")).toBeVisible();
  expect(screen.getByText("默认使用 model-b")).toBeVisible();
  expect(screen.getByRole("button", { name: "重新确认用于助手" })).toBeEnabled();
  expect(screen.queryByText("可选功能")).not.toBeInTheDocument();
  expect(screen.queryByText("助手状态暂不可用")).not.toBeInTheDocument();
});

it("does not overwrite a newly saved default with an initial settings response arriving later", async () => {
  const settings = deferred<AgentSettings>();
  vi.spyOn(rynAgent, "models").mockResolvedValue({ models: catalog });
  vi.spyOn(rynAgent, "settings").mockReturnValue(settings.promise);
  const update = vi.spyOn(rynAgent, "updateSettings").mockResolvedValue(enabledSettings());
  const user = page();
  await user.click(await screen.findByRole("button", { name: "用于助手" }));
  expect(await screen.findByText("已启用")).toBeVisible();
  expect(update).toHaveBeenCalledExactlyOnceWith({ model_id: "local/a" });
  await act(async () => { settings.resolve({ ...enabledSettings("local/b"), enabled: false }); });
  expect(screen.getByText("已启用")).toBeVisible();
  expect(screen.getByText("默认使用 model-a")).toBeVisible();
  expect(screen.queryByRole("link", { name: "了解并启用 →" })).not.toBeInTheDocument();
});
it("shares a single model while the assistant is disabled and preserves its sibling", async () => {
  endpoints(); const writes: unknown[] = []; let settingsWrites = 0;
  server.use(
    http.get("*/api/local/llm/sources", () => HttpResponse.json({ sources: [source()] })),
    http.put("*/api/local/llm/sources/src-1", async ({ request }) => { writes.push(await request.json()); return HttpResponse.json({ ...source(), shared_models: ["model-a", "model-b"] }); }),
    http.put("*/api/local/agent/settings", () => { settingsWrites++; return HttpResponse.json({}); }),
  );
  const user = page(); await screen.findByText("助手默认");
  await user.click(screen.getByRole("button", { name: "分享 model-b" }));
  const dialog = within(screen.getByRole("dialog", { name: "分享 model-b" }));
  await user.click(dialog.getByRole("checkbox")); await user.click(dialog.getByRole("button", { name: "保存" }));
  await screen.findByText("这个模型的分享设置已保存，助手选择保持不变。");
  expect(writes).toEqual([{ shared_models: ["model-a", "model-b"] }]); expect(settingsWrites).toBe(0);
});
it("changes the default without enabling Agent or editing sharing", async () => {
  endpoints(""); const writes: unknown[] = [];
  server.use(http.put("*/api/local/agent/settings", async ({ request }) => { writes.push(await request.json()); return HttpResponse.json({ enabled: false, model_id: "local/a" }); }));
  const user = page(); await user.click(await screen.findByRole("button", { name: "用于助手" }));
  await screen.findByText("助手默认");
  expect(writes).toEqual([{ model_id: "local/a" }]); expect(screen.getByText("可选功能")).toBeInTheDocument();
});
it("does not infer assistant protocol support from a ready source or offer a manual verification gate", async () => {
  endpoints(); const probes = vi.fn();
  server.use(http.post("*/api/local/llm/sources/src-1/models/test", () => { probes(); return HttpResponse.json({ tool_support: "unsupported" }); }));
  page(); await screen.findByText("助手默认");
  expect(screen.queryByRole("button", { name: "用于助手" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "验证工具调用" })).not.toBeInTheDocument();
  expect(screen.getByText("不支持助手工具")).toBeVisible();
  expect(probes).not.toHaveBeenCalled();
});
it.each(["unknown", "unsupported"])("uses a configured API model with %s diagnostic evidence directly", async tool_support => {
  endpoints(); const probes = vi.fn(); const settingsWrites = vi.fn();
  server.use(
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: catalog.map(item => ({ ...item, agent_supported: true, tool_support })) })),
    http.post("*/api/local/llm/sources/src-1/models/test", () => { probes(); return HttpResponse.json({ tool_support: "unsupported" }); }),
    http.put("*/api/local/agent/settings", async ({ request }) => { settingsWrites(await request.json()); return HttpResponse.json({ enabled: false, model_id: "local/b" }); }),
  );
  const user = page();
  await user.click(await screen.findByRole("button", { name: "用于助手" }));
  await screen.findByText("助手默认模型已更新，分享设置保持不变。");
  expect(settingsWrites).toHaveBeenCalledExactlyOnceWith({ model_id: "local/b" });
  expect(probes).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "验证工具调用" })).not.toBeInTheDocument();
});

it("keeps manual model configuration visible when Agent API is unavailable", async () => {
  server.use(http.get("*/api/local/agent/models", () => HttpResponse.json({}, { status: 503 })), http.get("*/api/local/agent/settings", () => HttpResponse.json({}, { status: 503 })));
  page(); await screen.findByText("暂时无法读取助手状态，模型配置和分享仍可使用。");
  expect(screen.getByRole("button", { name: "配置 model-a" })).toBeEnabled(); expect(screen.getByRole("button", { name: "分享 model-b" })).toBeEnabled();
});
it("uses a configured local service directly without calling its diagnostic endpoint", async () => {
  endpoints(); const probes = vi.fn(); const settingsWrites = vi.fn();
  server.use(
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [{ id: "local/native", name: "Local model", source: "local_service", service_id: "native", adapter: "llama_cpp", available: true, agent_supported: true, tool_support: "unknown" }] })),
    http.post("*/api/local/llm/services/native/tools/test", () => { probes(); return HttpResponse.json({ tool_support: "supported" }); }),
    http.put("*/api/local/agent/settings", async ({ request }) => { settingsWrites(await request.json()); return HttpResponse.json({ enabled: false, model_id: "local/native" }); }),
  );
  const user = page({ ...source(), models: [] });
  await user.click(await screen.findByRole("button", { name: "用于助手" }));
  await screen.findByText("助手默认模型已更新，分享设置保持不变。");
  expect(settingsWrites).toHaveBeenCalledExactlyOnceWith({ model_id: "local/native" });
  expect(probes).not.toHaveBeenCalled();
});

it("keeps the supported default selectable after saving parameters without re-verifying", async () => {
  endpoints(); const probe = vi.fn(); const parameterWrites = vi.fn(); const settingsWrites = vi.fn(); const otherWrites = vi.fn();
  let toolSupport = "supported";
  const value: AISource = { ...source(), context_window: 32000, max_output_tokens: 4000, model_settings: { "model-a": { overrides: { max_output_tokens: 2000 }, effective: { context_window: 32000, max_output_tokens: 2000, request_defaults: {}, max_concurrent: 1, requests_per_day: null }, inherited_fields: ["context_window", "request_defaults", "max_concurrent", "requests_per_day"], rule_revision: "rule-1", destination_revision: "dest-1", evidence: {} } } };
  server.use(
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: catalog.map(item => item.id === "local/a" ? { ...item, agent_supported: true, tool_support: toolSupport } : item) })),
    http.put("*/api/local/llm/sources/src-1/models/config", async ({ request }) => { parameterWrites(await request.json()); toolSupport = "unknown"; return HttpResponse.json(value); }),
    http.post("*/api/local/llm/sources/src-1/models/test", async ({ request }) => { probe(await request.json()); toolSupport = "supported"; return HttpResponse.json({ tool_support: "supported" }); }),
    http.put("*/api/local/agent/settings", async ({ request }) => { settingsWrites(await request.json()); return HttpResponse.json({ enabled: false, model_id: "local/a", permission_mode: "confirm_changes" }); }),
    http.put("*/api/local/llm/sources/src-1", () => { otherWrites(); return HttpResponse.json(value); }),
    http.all("*/api/local/agent/tasks*", () => { otherWrites(); return HttpResponse.json({}); }),
  );
  const user = page(value);
  const row = (await screen.findByText("助手默认")).closest(".mp-model") as HTMLElement;
  expect(within(row).getByRole("button", { name: "重新确认用于助手" })).toBeEnabled();
  await user.click(within(row).getByRole("button", { name: "此模型参数" }));
  const dialog = within(screen.getByRole("dialog", { name: "此模型参数 · model-a" }));
  await user.clear(dialog.getByLabelText("最大输出 Token")); await user.type(dialog.getByLabelText("最大输出 Token"), "3000");
  await user.click(dialog.getByRole("button", { name: "保存此模型参数" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(parameterWrites).toHaveBeenCalledWith({ model_id: "model-a", expected_rule_revision: "rule-1", configuration: { max_output_tokens: 3000 } });
  expect(within(row).getByRole("button", { name: "重新确认用于助手" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: "验证工具调用" })).not.toBeInTheDocument();
  expect(settingsWrites).not.toHaveBeenCalled(); expect(probe).not.toHaveBeenCalled();
  await user.click(within(row).getByRole("button", { name: "重新确认用于助手" }));
  await screen.findByText("助手默认模型已更新，分享设置保持不变。");
  expect(settingsWrites).toHaveBeenCalledExactlyOnceWith({ model_id: "local/a" });
  expect(screen.getByText("可选功能")).toBeInTheDocument();
  expect(within(row).getByRole("button", { name: "分享 model-a" })).toHaveTextContent("已分享");
  expect(otherWrites).not.toHaveBeenCalled();
});

it("keeps a chat-only default unselectable while another supported model remains selectable", async () => {
  endpoints(); const settingsWrites = vi.fn();
  server.use(
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: catalog.map(item => ({ ...item, agent_supported: item.id === "local/b", tool_support: item.id === "local/b" ? "supported" : "unknown" })) })),
    http.put("*/api/local/agent/settings", async ({ request }) => { settingsWrites(await request.json()); return HttpResponse.json({ enabled: false, model_id: "local/b" }); }),
  );
  const user = page();
  const defaultRow = (await screen.findByText("助手默认")).closest(".mp-model") as HTMLElement;
  expect(within(defaultRow).queryByRole("button", { name: /用于助手/ })).not.toBeInTheDocument();
  expect(within(defaultRow).getByText("不支持助手工具")).toBeVisible();
  const otherRow = screen.getByText("model-b").closest(".mp-model") as HTMLElement;
  await user.click(within(otherRow).getByRole("button", { name: "用于助手" }));
  await waitFor(() => expect(within(otherRow).getByText("助手默认")).toBeVisible());
  expect(settingsWrites).toHaveBeenCalledExactlyOnceWith({ model_id: "local/b" });
  expect(screen.getByText("可选功能")).toBeInTheDocument();
});

it.each([
  ["unsupported", true, false],
  ["offline", false, true],
])("does not offer default reselection for an %s model", async (_state, available, agent_supported) => {
  endpoints(); const settingsWrites = vi.fn();
  server.use(
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: catalog.map(item => item.id === "local/a" ? { ...item, available, agent_supported } : item) })),
    http.put("*/api/local/agent/settings", () => { settingsWrites(); return HttpResponse.json({}); }),
  );
  page();
  const row = (await screen.findByText("助手默认")).closest(".mp-model") as HTMLElement;
  expect(within(row).queryByRole("button", { name: /用于助手/ })).not.toBeInTheDocument();
  expect(settingsWrites).not.toHaveBeenCalled();
});

it("reports a rejected default reselection without claiming success", async () => {
  endpoints(); const settingsWrites = vi.fn();
  server.use(http.put("*/api/local/agent/settings", async ({ request }) => { settingsWrites(await request.json()); return HttpResponse.json({ detail: "agent_model_unavailable" }, { status: 409 }); }));
  const user = page();
  await user.click(await screen.findByRole("button", { name: "重新确认用于助手" }));
  expect(await screen.findByRole("status")).toHaveTextContent("agent_model_unavailable");
  expect(screen.queryByText("助手默认模型已更新，分享设置保持不变。")).not.toBeInTheDocument();
  expect(settingsWrites).toHaveBeenCalledExactlyOnceWith({ model_id: "local/a" });
  expect(screen.getByText("可选功能")).toBeInTheDocument();
});


it("displays Codex-style daily token usage chart and per-model counter for both local and shared peer models", async () => {
  const localUsage = {
    usage_key: "source/s1/gpt-4.1",
    today: { day: "2026-10-22", requests: 8, completed_requests: 8, input_tokens: 14000, output_tokens: 4400, cached_tokens: 9000, total_tokens: 18400, cache_hit_rate: 0.64 },
    total: { requests: 80, completed_requests: 80, input_tokens: 600000, output_tokens: 260000, cached_tokens: 400000, total_tokens: 860000, cache_hit_rate: 0.66 },
    daily: [
      { day: "2026-10-21", requests: 6, total_tokens: 12000, tokens: 12000 },
      { day: "2026-10-22", requests: 8, total_tokens: 18400, tokens: 18400 },
    ],
  };
  const peerUsage = {
    usage_key: "peer/abc/shared-deepseek",
    today: { day: "2026-10-22", requests: 5, completed_requests: 5, input_tokens: 6000, output_tokens: 1500, cached_tokens: 4500, total_tokens: 7500, cache_hit_rate: 0.75 },
    total: { requests: 20, completed_requests: 20, input_tokens: 24000, output_tokens: 6000, cached_tokens: 18000, total_tokens: 30000, cache_hit_rate: 0.75 },
    daily: [
      { day: "2026-10-22", requests: 5, total_tokens: 7500, tokens: 7500 },
    ],
  };
  server.use(
    http.get("*/api/local/agent/models", () => HttpResponse.json({
      models: [
        { ...catalog[0], usage: localUsage },
        catalog[1],
        { id: "peer/abc/shared-deepseek", peer_id: "peer-1", service_id: "shared-deepseek", name: "Shared DeepSeek", source: "peer", available: true, agent_supported: true, usage: peerUsage },
      ],
    })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json({ enabled: false, model_id: "local/a", permission_mode: "confirm_changes" })),
  );
  const context = { client: makeFixtureNodeClient() } as AppOutletContext;
  const peerRecord = {
    peer_id: "peer-1",
    node_name: "Friend Mac",
    online: true,
    usage: peerUsage,
    service: { package_id: "shared-deepseek", model_alias: "Shared DeepSeek", capabilities: ["chat"], context_window: 64000, max_output_tokens: 4096, pricing: { currency: "USD", input_per_1k: 0, output_per_1k: 0, minimum: 0, maximum_per_task: 0 }, privacy: {} },
  };
  const srcWithUsage = {
    ...source(),
    model_usage: { "gpt-4.1": localUsage },
  };
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={["/models"]}>
      <Routes>
        <Route element={<Outlet context={context} />}>
          <Route path="/models" element={<ModelPool sources={[srcWithUsage]} cli={null} native={null} peers={[peerRecord]} loading={false} error="" onRefresh={vi.fn().mockResolvedValue(undefined)} onEditSource={vi.fn()} onCLI={vi.fn()} onNative={vi.fn()} />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
  const peerRow = (await screen.findByText("Shared DeepSeek")).closest(".mp-model") as HTMLElement;
  expect(within(peerRow).getByTestId("model-usage-peer-1/shared-deepseek")).toHaveTextContent("今日 7.5K·累计 30K");

  // Top Codex daily usage card aggregates all models by default (18.4K + 7.5K = 25.9K today)
  expect(screen.getByTestId("codex-usage-today")).toHaveTextContent("25.9K");
  expect(screen.getByTestId("codex-usage-total")).toHaveTextContent("890K");

  // Clicking the peer model's inline counter filters the daily chart to that model
  await user.click(within(peerRow).getByTestId("model-usage-peer-1/shared-deepseek"));
  expect(screen.getByTestId("codex-usage-today")).toHaveTextContent("7.5K");
  expect(screen.getByTestId("codex-usage-total")).toHaveTextContent("30K");
});
