import { beforeEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { DesktopPage } from "./Desktop";
import * as desktop from "../domain/desktopClient";
import { setLanguagePreference } from "../i18n";
vi.mock("../domain/desktopClient");
vi.mock("../domain/nodeUrl", () => ({ isTauriDesktop: () => true }));
const prefs = { background: true, startup: false, startup_supported: false, awake_supported: true, silent_start: true, auto_recover: true, keep_awake: false, close_notice_seen: false };
const status = { node_online: true, configured: false, sharing: false, active_tasks: 1, agent_tasks: 1, setup_active: false, recovery_attempts: 0, recovery: "healthy", awake_active: false, last_error: null };
beforeEach(() => {
  vi.resetAllMocks(); localStorage.clear(); setLanguagePreference("zh-CN");
  window.history.replaceState({}, "", "/settings/desktop");
  vi.mocked(desktop.getDesktopPreferences).mockResolvedValue(prefs);
  vi.mocked(desktop.getDesktopStatus).mockResolvedValue(status);
  vi.mocked(desktop.setDesktopPreferences).mockImplementation(async p => p);
});
it("T4 offers the Agent awake preference on macOS without enabling it by default", async () => {
  render(<MemoryRouter><DesktopPage /></MemoryRouter>);
  const toggle = await screen.findByRole("switch", { name: "共享 AI 或 Agent 任务运行时防止空闲休眠" });
  expect(toggle).toBeEnabled(); expect(toggle).toHaveAttribute("aria-checked", "false");
  fireEvent.click(toggle);
  expect(desktop.setDesktopPreferences).toHaveBeenCalledWith({ ...prefs, keep_awake: true });
});
it("shows background Agent work separately from model requests", async () => {
  render(<MemoryRouter><DesktopPage /></MemoryRouter>);
  expect(await screen.findByText("1 个 Agent 任务")).toBeInTheDocument();
  expect(screen.getByText("0 个模型请求")).toBeInTheDocument();
  expect(desktop.setDesktopPreferences).not.toHaveBeenCalled();
});
