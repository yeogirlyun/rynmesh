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
