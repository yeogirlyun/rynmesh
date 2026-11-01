import i18n from "../i18n";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { newSource } from "../domain/aiSources";
import type { AppOutletContext } from "../appContext";
import AISources from "./AISources";

beforeEach(() => {
  server.use(
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [] })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json({ enabled: false, model_id: "", permission_mode: "confirm_changes" })),
  );
  server.use(http.get("*/api/local/llm/native", () => HttpResponse.json({
    id: "ryn-qwen3-1.7b", name: "Qwen3-1.7B", state: "not_installed", installed: false,
    supported: true, autostart: false, sharing: false,
  })));
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
});
function page(entry = "/services/sources?preset=zai", empty = false, configureClient?: (client: ReturnType<typeof makeFixtureNodeClient>) => void) {
  const client = makeFixtureNodeClient();
  if (empty) client.listLLMServices = vi.fn().mockResolvedValue([]);
  configureClient?.(client);
  const context = { client, confirm: vi.fn(), notify: vi.fn() } as unknown as AppOutletContext;
  render(<MemoryRouter initialEntries={[entry]}><Routes><Route element={<Outlet context={context} />}><Route path="/services/sources" element={<AISources />} /></Route></Routes></MemoryRouter>);
  return userEvent.setup();
}
it("opens the GLM configuration with an empty key and saves before testing", async () => {
  let stored = { ...newSource(), id: "src-zai", status: "needs_key" };
  const calls: string[] = [];
  server.use(
    http.get("*/api/local/llm/sources", () => HttpResponse.json({ sources: [stored], peer_id: "self", node_name: "My PC" })),
    http.put("*/api/local/llm/sources/src-zai", async ({ request }) => {
      const body = await request.json() as typeof stored & { api_key?: string };
      expect(body.base_url).toBe("https://api.z.ai/api/paas/v4");
      expect(body.models).toEqual(["glm-5.3-flash"]);
      expect(body.api_key).toBe("user-entered-test-key");
      calls.push("save"); stored = { ...stored, has_key: true, status: "untested" };
      return HttpResponse.json(stored);
    }),
    http.post("*/api/local/llm/sources/src-zai/test", () => { calls.push("test"); return HttpResponse.json({ ok: true, source: { ...stored, status: "ready" } }); }),
  );
  const user = page();
  const key = await screen.findByLabelText("API 密钥");
  expect(key).toHaveValue("");
  expect(key).toHaveAttribute("type", "password");
  expect(calls).toEqual([]);
  await user.type(key, "user-entered-test-key");
  await user.click(screen.getByRole("button", { name: "保存并测试" }));
  await screen.findByText(/连接成功，已保存/);
  expect(calls).toEqual(["save", "test"]);
  expect(screen.getByLabelText("API 密钥")).toHaveValue("");
});

it("keeps a failed test editable and never marks it successful", async () => {
  const stored = { ...newSource(), id: "src-zai", has_key: true };
  server.use(
    http.get("*/api/local/llm/sources", () => HttpResponse.json({ sources: [stored], peer_id: "self", node_name: "My PC" })),
    http.put("*/api/local/llm/sources/src-zai", () => HttpResponse.json(stored)),
    http.post("*/api/local/llm/sources/src-zai/test", () => HttpResponse.json({ detail: "连接测试失败" }, { status: 400 })),
  );
  const user = page();
  await screen.findByLabelText("API 密钥");
  await user.click(screen.getByRole("button", { name: "保存并测试" }));
  expect(await screen.findByRole("status")).toHaveTextContent("连接测试失败");
  await waitFor(() => expect(screen.getByRole("button", { name: "保存" })).toBeEnabled());
  expect(screen.getByRole("dialog")).toBeInTheDocument();
});

beforeEach(async () => { await i18n.changeLanguage("zh-CN"); });

it("edits legacy Chat sources and saves explicit native protocols with the matching endpoint hint", async () => {
  const stored = { ...newSource("custom"), protocol: undefined, id: "src-protocol", name: "Protocol source", models: ["m"] };
  let savedProtocol: string | undefined;
  server.use(
    http.get("*/api/local/llm/sources", () => HttpResponse.json({ sources: [stored], peer_id: "self", node_name: "My PC" })),
    http.put("*/api/local/llm/sources/src-protocol", async ({ request }) => {
      const body = await request.json() as Omit<typeof stored, "protocol"> & { protocol: string };
      savedProtocol = body.protocol;
      return HttpResponse.json(body);
    }),
  );
  const user = page("/services/sources?edit=src-protocol");
  const protocol = await screen.findByRole("button", { name: "API 协议" });
  expect(protocol).toHaveTextContent("OpenAI Chat Completions");
  await user.click(protocol);
  await user.click(screen.getByRole("option", { name: "OpenAI Responses" }));
  expect(screen.getByText(/系统会追加 \/responses/)).toBeInTheDocument();
  await user.click(protocol);
  await user.click(screen.getByRole("option", { name: "Anthropic Messages" }));
  expect(screen.getByText(/系统会追加 \/messages/)).toHaveTextContent("https://api.anthropic.com/v1");
  await user.click(screen.getByRole("button", { name: /^保存$/ }));
  await waitFor(() => expect(savedProtocol).toBe("anthropic_messages"));
});

it.each(["12", "10000.5", "2000001"])("blocks invalid manual source context %s before save or test", async input => {
  const stored = { ...newSource("custom"), id: "src-manual", name: "Manual source", context_window_mode: "manual" as const,
    context_window: 60000, max_output_tokens: 4096, models: ["manual-model"] };
  const saves = vi.fn();
  server.use(
    http.get("*/api/local/llm/sources", () => HttpResponse.json({ sources: [stored], peer_id: "self", node_name: "My PC" })),
    http.put("*/api/local/llm/sources/src-manual", () => { saves(); return HttpResponse.json(stored); }),
  );
  const user = page("/services/sources?edit=src-manual");
  const field = await screen.findByLabelText("上下文窗口");
  const details = field.closest("details")!;
  if (!details.open) await user.click(details.querySelector("summary")!);
  await user.clear(field); await user.type(field, input);
  expect(screen.getByRole("alert")).toHaveTextContent("必须为有效范围内的整数");
  expect(screen.getByRole("button", { name: /^保存$/ })).toBeDisabled();
  expect(screen.getByRole("button", { name: "保存并测试" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: /^保存$/ }));
  expect(saves).not.toHaveBeenCalled();
});

it("offers all three first-use connections without requiring an existing service", async () => {
  server.use(http.get("*/api/local/llm/sources", () => HttpResponse.json({ sources: [], peer_id: "self", node_name: "My PC" })));
  const user = page("/services/sources", true);
  const welcome = await screen.findByRole("region", { name: "连接你的第一个 AI 来源" });
  expect(screen.getByRole("link", { name: "查看 API 接入" })).toHaveAttribute("href", "/services/api?client=fixture");
  expect(screen.getByRole("link", { name: "管理共享" })).toHaveAttribute("href", "/services/agent-sharing?client=fixture");
  expect(screen.getByRole("link", { name: "模型映射" })).toHaveAttribute("href", "/services/model-mapping?client=fixture");
  await user.click(within(welcome).getByRole("button", { name: /厂商 API/ }));
  expect(await screen.findByLabelText("API 密钥")).toBeInTheDocument();
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "关闭弹窗" }));
  await user.click(within(welcome).getByRole("button", { name: /本地模型/ }));
  expect(screen.getByRole("dialog", { name: "本地模型" })).toBeInTheDocument();
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "关闭弹窗" }));
  await user.click(within(welcome).getByRole("button", { name: /本地 CLI/ }));
  expect(screen.getByRole("dialog", { name: "连接本地 CLI" })).toBeInTheDocument();
});

it("shows individual models in one pool and retains source configuration", async () => {
  const stored = { ...newSource(), id: "src-zai", name: "My GLM", status: "ready" };
  server.use(http.get("*/api/local/llm/sources", () => HttpResponse.json({ sources: [stored], peer_id: "self", node_name: "My PC" })));
  const user = page("/services/sources", true);
  await screen.findByRole("button", { name: "配置 glm-5.3-flash" });
  expect(screen.queryByRole("region", { name: "连接你的第一个 AI 来源" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "设备筛选" })).not.toBeInTheDocument();
  await user.type(screen.getByRole("textbox", { name: "搜索模型" }), "no-match");
  expect(screen.queryByRole("button", { name: "配置 glm-5.3-flash" })).not.toBeInTheDocument();
  await user.clear(screen.getByRole("textbox", { name: "搜索模型" }));
  await user.click(screen.getByRole("button", { name: "配置 glm-5.3-flash" }));
  expect(screen.getByRole("dialog", { name: "编辑来源" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "保存并测试" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "删除来源" })).toBeInTheDocument();
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "关闭弹窗" }));
  await user.click(screen.getByRole("button", { name: "添加模型" }));
  const dialog = within(screen.getByRole("dialog", { name: "添加模型" }));
  for (const kind of ["厂商 API", "本地 CLI", "本地模型"]) expect(dialog.getByRole("button", { name: new RegExp(kind) })).toBeEnabled();
});

it.each([
  [true, "连接已完成，可直接聊天或用于助手。"],
  [false, "连接已完成，此接入支持聊天，不支持助手工具。"],
] as const)("reports CLI connection protocol support without claiming probe verification (%s)", async (agent_usable, expected) => {
  server.use(http.get("*/api/local/llm/sources", () => HttpResponse.json({ sources: [], peer_id: "self", node_name: "My PC" })));
  const setup = vi.fn().mockResolvedValue({ configured: true, agent_usable, tool_support: "unknown" });
  const user = page("/services/sources", true, client => {
    client.getCLIServices = vi.fn().mockResolvedValue({ services: [{ kind: "codex_cli", title: "Codex CLI", installed: true, configured: false, publication_enabled: false, online: false, api_text_only: true, service_id: "codex-cli" }], personal_space_required: false, personal_space_ready: true });
    client.setupCLIService = setup;
  });
  const welcome = await screen.findByRole("region", { name: "连接你的第一个 AI 来源" });
  await user.click(within(welcome).getByRole("button", { name: /本地 CLI/ }));
  const dialog = within(screen.getByRole("dialog", { name: "连接本地 CLI" }));
  await user.click(dialog.getByRole("button", { name: "配置接入" }));
  expect(await dialog.findByRole("status")).toHaveTextContent(expected);
  expect(setup).toHaveBeenCalledExactlyOnceWith("codex_cli");
  expect(dialog.queryByText(/工具验证|配置并验证|重新验证/)).not.toBeInTheDocument();
});
