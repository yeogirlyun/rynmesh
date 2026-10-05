import { beforeEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { setLanguagePreference } from "../i18n";
import { MemoryRouter } from "react-router-dom";
import { DesktopPage } from "./Desktop";
import * as desktop from "../domain/desktopClient";
import { isTauriDesktop } from "../domain/nodeUrl";
vi.mock("../domain/desktopClient");
vi.mock("../domain/nodeUrl", () => ({ isTauriDesktop: vi.fn(() => false) }));
const prefs = { background: true, startup: false, startup_supported: true, silent_start: true, auto_recover: true, keep_awake: false, close_notice_seen: false };
const status = { node_online: true, configured: true, sharing: true, active_tasks: 0, setup_active: false, recovery_attempts: 0, recovery: "healthy", awake_active: false, last_error: null };
beforeEach(() => {
  vi.resetAllMocks();
  window.history.replaceState({}, "", "/settings/desktop");
  vi.mocked(isTauriDesktop).mockReturnValue(false);
  vi.mocked(desktop.getDesktopPreferences).mockResolvedValue(prefs);
  vi.mocked(desktop.getDesktopStatus).mockResolvedValue(status);
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
});
function view() { render(<MemoryRouter><DesktopPage /></MemoryRouter>); }
it("translates desktop controls and keeps preview actions working in Chinese", async () => {
  window.history.replaceState({}, "", "/settings/desktop?client=fixture");
  view();
  await act(async () => { setLanguagePreference("zh-CN"); });
  expect(screen.getByRole("switch", { name: "登录时启动" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "启动与后台运行" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "快捷操作" })).toBeInTheDocument();
  expect(screen.queryByText("Startup & background")).not.toBeInTheDocument();
  expect(screen.queryByText("At your fingertips")).not.toBeInTheDocument();
  expect(screen.getByText("0 个进行中的 AI 任务")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "暂停 AI 共享" }));
  expect(await screen.findByRole("heading", { name: "AI 共享已暂停" })).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("预览：AI 共享已暂停");
  expect(desktop.runDesktopAction).not.toHaveBeenCalled();
});
it("fixture controls never invoke Windows actions", async () => {
  window.history.replaceState({}, "", "/settings/desktop?client=fixture");
  vi.mocked(isTauriDesktop).mockReturnValue(true);
  view();
  fireEvent.click(screen.getByRole("button", { name: "Pause AI sharing" }));
  expect(await screen.findByRole("heading", { name: "AI sharing is paused" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Export diagnostics" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Preview only");
  expect(desktop.runDesktopAction).not.toHaveBeenCalled();
  expect(desktop.getDesktopPreferences).not.toHaveBeenCalled();
});
it("keeps the saved setting visible if Windows rejects a change", async () => {
  vi.mocked(isTauriDesktop).mockReturnValue(true);
  vi.mocked(desktop.setDesktopPreferences).mockRejectedValue(new Error("registry denied"));
  view();
  const toggle = screen.getByRole("switch", { name: "Launch at sign-in" });
  await waitFor(() => expect(toggle).toBeEnabled());
  fireEvent.click(toggle);
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not save");
  expect(toggle).toHaveAttribute("aria-checked", "false");
});
it("regular browser does not pretend to control Windows", () => {
  view();
  expect(screen.getByRole("switch", { name: "Launch at sign-in" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Restart node" })).toBeDisabled();
  expect(desktop.getDesktopPreferences).not.toHaveBeenCalled();
});
it("native action failure is shown instead of a successful sharing state", async () => {
  vi.mocked(isTauriDesktop).mockReturnValue(true);
  vi.mocked(desktop.runDesktopAction).mockRejectedValue("Node unavailable");
  view();
  fireEvent.click(await screen.findByRole("button", { name: "Pause AI sharing" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Node unavailable");
  expect(screen.getByRole("heading", { name: "AI sharing is enabled" })).toBeInTheDocument();
});
it("fixture quit opens a cancellable confirmation", () => {
  window.history.replaceState({}, "", "/settings/desktop?client=fixture"); view();
  fireEvent.click(screen.getByRole("button", { name: "Quit Ryn" }));
  expect(screen.getByRole("dialog")).toHaveTextContent("Running tasks may be interrupted");
  fireEvent.click(screen.getByRole("button", { name: "Keep running" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(desktop.runDesktopAction).not.toHaveBeenCalled();
});
