import { beforeEach, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { http, HttpResponse } from "msw";
import i18n from "../i18n";
import { server } from "../test/server";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { newSource, type AISource } from "../domain/aiSources";
import type { AppOutletContext } from "../appContext";
import ModelPool from "./ModelPool";

beforeEach(async () => {
  await i18n.changeLanguage("zh-CN");
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
});
const source = (): AISource => ({ ...newSource(), id: "src-1", name: "My API", models: ["model-a", "model-b"], has_key: true, sharing: true, shared_models: ["model-a"], status: "ready" });
function page(value = source()) {
  const context = { client: makeFixtureNodeClient() } as AppOutletContext;
  const onRefresh = vi.fn().mockResolvedValue(undefined);
  render(<MemoryRouter initialEntries={["/models"]}><Routes><Route element={<Outlet context={context} />}><Route path="/models" element={<ModelPool sources={[value]} cli={null} native={null} peers={[]} loading={false} error="" onRefresh={onRefresh} onEditSource={vi.fn()} onCLI={vi.fn()} onNative={vi.fn()} />} /></Route></Routes></MemoryRouter>);
  return userEvent.setup();
}
const catalog = [{ id: "local/a", source_id: "src-1", model_id: "model-a", name: "model-a", source: "local_api", available: true, agent_supported: true }, { id: "local/b", source_id: "src-1", model_id: "model-b", name: "model-b", source: "local_api", available: true, agent_supported: false, tool_support: "unknown" }];
function endpoints(model_id = "local/a") {
  server.use(
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: catalog })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json({ enabled: false, model_id, permission_mode: "confirm_changes" })),
  );
}
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
it("does not infer tool support from a ready source and only probes after a click", async () => {
  endpoints(); let probes = 0;
  server.use(http.post("*/api/local/llm/sources/src-1/models/test", async ({ request }) => { probes++; expect(await request.json()).toEqual({ model_id: "model-b" }); return HttpResponse.json({ tool_support: "unsupported" }); }));
  const user = page(); await screen.findByText("助手默认"); expect(probes).toBe(0);
  expect(screen.queryByRole("button", { name: "用于助手" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "验证工具调用" }));
  await screen.findByText("此模型未通过工具调用验证，仍可用于普通推理。"); expect(probes).toBe(1);
});
it("keeps manual model configuration visible when Agent API is unavailable", async () => {
  server.use(http.get("*/api/local/agent/models", () => HttpResponse.json({}, { status: 503 })), http.get("*/api/local/agent/settings", () => HttpResponse.json({}, { status: 503 })));
  page(); await screen.findByText("暂时无法读取助手状态，模型配置和分享仍可使用。");
  expect(screen.getByRole("button", { name: "配置 model-a" })).toBeEnabled(); expect(screen.getByRole("button", { name: "分享 model-b" })).toBeEnabled();
});
it("verifies an unverified local service through its own capability endpoint", async () => {
  endpoints(); let probes = 0;
  server.use(
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [{ id: "local/native", name: "Local model", source: "local_service", service_id: "native", adapter: "llama_cpp", available: true, agent_supported: false }] })),
    http.post("*/api/local/llm/services/native/tools/test", () => { probes++; return HttpResponse.json({ tool_support: "supported" }); }),
  );
  const user = page({ ...source(), models: [] });
  await user.click(await screen.findByRole("button", { name: "验证工具调用" }));
  await screen.findByText("工具调用验证通过，可选择或重新确认用于助手。");
  expect(probes).toBe(1);
});

it("requires explicit same-model confirmation after saving parameters and re-verifying the default", async () => {
  endpoints(); const probe = vi.fn(); const parameterWrites = vi.fn(); const settingsWrites = vi.fn(); const otherWrites = vi.fn();
  let verified = true;
  const value: AISource = { ...source(), context_window: 32000, max_output_tokens: 4000, model_settings: { "model-a": { overrides: { max_output_tokens: 2000 }, effective: { context_window: 32000, max_output_tokens: 2000, request_defaults: {}, max_concurrent: 1, requests_per_day: null }, inherited_fields: ["context_window", "request_defaults", "max_concurrent", "requests_per_day"], rule_revision: "rule-1", destination_revision: "dest-1", evidence: {} } } };
  server.use(
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: catalog.map(item => item.id === "local/a" ? { ...item, agent_supported: verified, tool_support: verified ? "supported" : "unknown" } : item) })),
    http.put("*/api/local/llm/sources/src-1/models/config", async ({ request }) => { parameterWrites(await request.json()); verified = false; return HttpResponse.json(value); }),
    http.post("*/api/local/llm/sources/src-1/models/test", async ({ request }) => { probe(await request.json()); verified = true; return HttpResponse.json({ tool_support: "supported" }); }),
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
  expect(within(row).queryByRole("button", { name: "重新确认用于助手" })).not.toBeInTheDocument();
  expect(settingsWrites).not.toHaveBeenCalled(); expect(probe).not.toHaveBeenCalled();
  await user.click(within(row).getByRole("button", { name: "验证工具调用" }));
  await screen.findByText("工具调用验证通过，可选择或重新确认用于助手。");
  expect(probe).toHaveBeenCalledWith({ model_id: "model-a" });
  expect(settingsWrites).not.toHaveBeenCalled();
  await user.click(within(row).getByRole("button", { name: "重新确认用于助手" }));
  await screen.findByText("助手默认模型已更新，分享设置保持不变。");
  expect(settingsWrites).toHaveBeenCalledExactlyOnceWith({ model_id: "local/a" });
  expect(screen.getByText("可选功能")).toBeInTheDocument();
  expect(within(row).getByRole("button", { name: "分享 model-a" })).toHaveTextContent("已分享");
  expect(otherWrites).not.toHaveBeenCalled();
});

it("keeps an unverified default unselectable while another verified model remains selectable", async () => {
  endpoints(); const settingsWrites = vi.fn();
  server.use(
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: catalog.map(item => ({ ...item, agent_supported: item.id === "local/b", tool_support: item.id === "local/b" ? "supported" : "unknown" })) })),
    http.put("*/api/local/agent/settings", async ({ request }) => { settingsWrites(await request.json()); return HttpResponse.json({ enabled: false, model_id: "local/b" }); }),
  );
  const user = page();
  const defaultRow = (await screen.findByText("助手默认")).closest(".mp-model") as HTMLElement;
  expect(within(defaultRow).queryByRole("button", { name: /用于助手/ })).not.toBeInTheDocument();
  expect(within(defaultRow).getByRole("button", { name: "验证工具调用" })).toBeEnabled();
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
