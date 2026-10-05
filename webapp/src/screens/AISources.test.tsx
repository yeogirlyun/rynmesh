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
  server.use(http.get("*/api/local/llm/native", () => HttpResponse.json({
    id: "ryn-qwen3-1.7b", name: "Qwen3-1.7B", state: "not_installed", installed: false,
    supported: true, autostart: false, sharing: false,
  })));
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
});
function page(entry = "/services/sources?preset=zai", empty = false) {
  const client = makeFixtureNodeClient();
  if (empty) client.listLLMServices = vi.fn().mockResolvedValue([]);
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

it("retains search, device filtering, source editing and add-source selection for existing sources", async () => {
  const stored = { ...newSource(), id: "src-zai", name: "My GLM", status: "ready" };
  server.use(http.get("*/api/local/llm/sources", () => HttpResponse.json({ sources: [stored], peer_id: "self", node_name: "My PC" })));
  const user = page("/services/sources", true);
  await screen.findByRole("button", { name: "编辑 My GLM" });
  expect(screen.queryByRole("region", { name: "连接你的第一个 AI 来源" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "设备筛选" }));
  await user.click(screen.getByRole("option", { name: "其他设备" }));
  expect(screen.queryByRole("button", { name: "编辑 My GLM" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "设备筛选" }));
  await user.click(screen.getByRole("option", { name: "全部设备" }));
  await user.type(screen.getByRole("textbox", { name: "搜索来源" }), "no-match");
  expect(screen.queryByRole("button", { name: "编辑 My GLM" })).not.toBeInTheDocument();
  await user.clear(screen.getByRole("textbox", { name: "搜索来源" }));
  await user.click(screen.getByRole("button", { name: "编辑 My GLM" }));
  expect(screen.getByRole("dialog", { name: "编辑来源" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "保存并测试" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "删除来源" })).toBeInTheDocument();
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "关闭弹窗" }));
  await user.click(screen.getByRole("button", { name: "添加来源" }));
  const dialog = within(screen.getByRole("dialog", { name: "添加来源" }));
  for (const kind of ["厂商 API", "本地 CLI", "本地模型"]) expect(dialog.getByRole("button", { name: new RegExp(kind) })).toBeEnabled();
});
