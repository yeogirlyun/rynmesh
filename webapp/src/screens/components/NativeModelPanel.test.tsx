import { useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../../test/server";
import i18n from "../../i18n";
import type { NativeModelStatus } from "../../domain/nativeModel";
import NativeModelPanel from "./NativeModelPanel";

const stopped: NativeModelStatus = { id: "ryn-qwen3-1.7b", name: "Qwen3-1.7B", file_name: "Qwen3-1.7B-Q8_0.gguf", size_bytes: 1834426016, peer_id: "self", state: "stopped", stage: "", progress: null, error: "", backend: "windows-cpu", installed: true, supported: true, autostart: false, sharing: false, active_requests: 0 };
beforeEach(async () => { await i18n.changeLanguage("zh-CN"); });
function page(initial = stopped) {
  function Wrapper() {
    const [status, setStatus] = useState(initial);
    return <NativeModelPanel status={status} onUpdate={setStatus} onConnect={vi.fn()} />;
  }
  render(<MemoryRouter><Wrapper /></MemoryRouter>);
  return userEvent.setup();
}
it("saves app autostart without starting the model or enabling sharing", async () => {
  const changes: unknown[] = [];
  server.use(http.patch("*/api/local/llm/native", async ({ request }) => {
    const update = await request.json(); changes.push(update);
    return HttpResponse.json({ ...stopped, ...update as object });
  }));
  const user = page();
  const auto = screen.getByRole("switch", { name: "随 Ryn 启动" });
  expect(auto).not.toBeChecked();
  await user.click(auto);
  expect(auto).toBeChecked();
  expect(changes).toEqual([{ autostart: true }]);
  expect(screen.getByRole("button", { name: "启动" })).toBeEnabled();
  expect(screen.getByRole("switch", { name: "分享给我的设备" })).not.toBeChecked();
});
it("starts and stops in the same panel while preserving the autostart choice", async () => {
  server.use(
    http.post("*/api/local/llm/native/start", () => HttpResponse.json({ ...stopped, state: "running", autostart: true })),
    http.post("*/api/local/llm/native/stop", () => HttpResponse.json({ ...stopped, autostart: true })),
  );
  const user = page({ ...stopped, autostart: true });
  await user.click(screen.getByRole("button", { name: "启动" }));
  expect(await screen.findByRole("link", { name: "开始聊天" })).toHaveAttribute("href", expect.stringContaining("service=ryn-qwen3-1.7b"));
  await user.click(screen.getByRole("button", { name: "停止" }));
  expect(await screen.findByRole("button", { name: "启动" })).toBeEnabled();
  expect(screen.getByRole("switch", { name: "随 Ryn 启动" })).toBeChecked();
});
it("keeps first installation simple and exposes no API form", () => {
  page({ ...stopped, installed: false, state: "not_installed" });
  expect(screen.getByRole("button", { name: "选择模型文件" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: "一键安装" })).not.toBeInTheDocument();
  expect(screen.queryByText("Qwen3-1.7B")).not.toBeInTheDocument();
  expect(screen.getByText("缺少运行组件时会自动下载组件，不会下载模型。")).toBeInTheDocument();
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  expect(screen.queryByRole("switch")).not.toBeInTheDocument();
});
it("rejects a renamed non-GGUF before uploading anything", async () => {
  const user = page({ ...stopped, installed: false, state: "not_installed" });
  const file = new File(["not model weights"], "fake.gguf");
  await user.upload(screen.getByLabelText("选择模型文件", { selector: "input" }), file);
  expect(await screen.findByRole("alert")).toHaveTextContent("模型文件无效或不完整");
  expect(screen.queryByRole("button", { name: "导入并启动" })).not.toBeInTheDocument();
});
it("uses indeterminate progress for startup and hides duplicate start actions", () => {
  page({ ...stopped, state: "starting", stage: "testing", progress: null });
  expect(screen.getByRole("progressbar")).not.toHaveAttribute("value");
  expect(screen.queryByRole("button", { name: "启动" })).not.toBeInTheDocument();
});

const configured: NativeModelStatus = { ...stopped, model_settings: {
  configuration: { max_output_tokens: 1024, max_concurrent: 1, requests_per_day: null },
  context_window: 4096, rule_revision: "first",
  enforcement: { output_tokens: "upstream_request_limit", concurrency: "node_process", quota: "persisted_dispatch_attempts_utc_day" },
} };

it("saves only edited native parameters with CAS and leaves start and sharing independent", async () => {
  const changes: unknown[] = [];
  server.use(http.put("*/api/local/llm/native/model-config", async ({ request }) => {
    const body = await request.json() as { configuration: object }; changes.push(body);
    return HttpResponse.json({ ...configured, model_settings: { ...configured.model_settings,
      rule_revision: "second", configuration: { ...configured.model_settings!.configuration, ...body.configuration } } });
  }));
  const user = page(configured);
  expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "更多模型设置" }));
  const output = screen.getByRole("spinbutton", { name: "最大输出 Token" });
  expect(output).toHaveAttribute("max", "4096");
  await user.clear(output); await user.type(output, "512");
  await user.click(screen.getByRole("button", { name: "保存此模型参数" }));
  expect(await screen.findByText("参数已保存。")).toBeInTheDocument();
  expect(changes).toEqual([{ expected_rule_revision: "first", configuration: { max_output_tokens: 512 } }]);
  expect(screen.getByRole("switch", { name: "分享给我的设备" })).not.toBeChecked();
  expect(screen.getByRole("button", { name: "启动" })).toBeEnabled();
});

it("requires explicit reload after a native revision conflict and discards stale edits", async () => {
  const writes = vi.fn();
  const current = { ...configured, model_settings: { ...configured.model_settings!, rule_revision: "new", configuration: { ...configured.model_settings!.configuration, requests_per_day: 35 } } };
  server.use(
    http.put("*/api/local/llm/native/model-config", () => { writes(); return HttpResponse.json({ detail: "native_model_rule_revision_conflict" }, { status: 409 }); }),
    http.get("*/api/local/llm/native", () => HttpResponse.json(current)),
  );
  const user = page(configured);
  await user.click(screen.getByRole("button", { name: "更多模型设置" }));
  await user.type(screen.getByRole("spinbutton", { name: "每日请求上限" }), "20");
  await user.click(screen.getByRole("button", { name: "保存此模型参数" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("旧编辑不会自动覆盖新参数");
  expect(screen.getByRole("button", { name: "保存此模型参数" })).toBeDisabled();
  expect(writes).toHaveBeenCalledTimes(1);
  await user.click(screen.getByRole("button", { name: "重新读取" }));
  expect(screen.getByRole("spinbutton", { name: "每日请求上限" })).toHaveValue(35);
  expect(screen.getByRole("button", { name: "保存此模型参数" })).toBeDisabled();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("rejects output larger than the actual native context without sending a request", async () => {
  const writes = vi.fn();
  server.use(http.put("*/api/local/llm/native/model-config", () => { writes(); return HttpResponse.json(configured); }));
  const user = page(configured);
  await user.click(screen.getByRole("button", { name: "更多模型设置" }));
  const output = screen.getByRole("spinbutton", { name: "最大输出 Token" });
  await user.clear(output); await user.type(output, "9999");
  await user.click(screen.getByRole("button", { name: "保存此模型参数" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("请核对数值范围");
  expect(writes).not.toHaveBeenCalled();
});
