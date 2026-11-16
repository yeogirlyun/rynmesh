import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { setLanguagePreference } from "../i18n";
import { DesktopPage } from "./Desktop";
import * as desktop from "../domain/desktopClient";
import { isTauriDesktop } from "../domain/nodeUrl";

vi.mock("../domain/desktopClient");
vi.mock("../domain/nodeUrl", () => ({ isTauriDesktop: vi.fn(() => false) }));

const basePrefs: desktop.DesktopPreferences = {
  background: true,
  startup: false,
  startup_supported: true,
  silent_start: true,
  auto_recover: true,
  keep_awake: false,
  close_notice_seen: false,
};

const baseStatus: desktop.DesktopStatus = {
  node_online: true,
  configured: true,
  sharing: false,
  active_tasks: 0,
  agent_active_runs: 0,
  agent_waiting: 0,
  agent_task_titles: [],
  setup_active: false,
  recovery_attempts: 0,
  recovery: "healthy",
  awake_active: false,
  last_error: null,
};

beforeEach(() => {
  vi.resetAllMocks();
  window.history.replaceState({}, "", "/settings/desktop");
  vi.mocked(isTauriDesktop).mockReturnValue(true);
  vi.mocked(desktop.getDesktopPreferences).mockResolvedValue(basePrefs);
  vi.mocked(desktop.getDesktopStatus).mockResolvedValue(baseStatus);
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
});

function renderDesktop() {
  return render(
    <MemoryRouter>
      <DesktopPage />
    </MemoryRouter>,
  );
}

describe("S02-I12 Desktop agent activity counts, copy, and confirmation titles", () => {
  it("T4: shows updated keep-awake copy and separate Agent / model request / waiting badges", async () => {
    vi.mocked(desktop.getDesktopStatus).mockResolvedValue({
      ...baseStatus,
      active_tasks: 0,
      agent_active_runs: 2,
      agent_waiting: 1,
      agent_task_titles: ["整理周报", "检查账单"],
    });

    renderDesktop();
    await act(async () => {
      setLanguagePreference("zh-CN");
    });

    expect(await screen.findByRole("switch", { name: "有任务运行或共享时保持唤醒" })).toBeInTheDocument();
    expect(await screen.findByText("2 个 Agent 任务")).toBeInTheDocument();
    expect(screen.getByText("0 个模型请求")).toBeInTheDocument();
    expect(screen.getByText("1 个等你处理")).toBeInTheDocument();
  });

  it("T4: confirmation dialog lists task titles and appends '等 N 个' when more than 3 tasks are running", async () => {
    window.history.replaceState({}, "", "/settings/desktop?client=fixture");
    vi.mocked(isTauriDesktop).mockReturnValue(false);

    // Render in fixture mode first with 5 running tasks and 3 titles via native mock, then check confirmation dialog
    window.history.replaceState({}, "", "/settings/desktop");
    vi.mocked(isTauriDesktop).mockReturnValue(true);
    vi.mocked(desktop.getDesktopStatus).mockResolvedValue({
      ...baseStatus,
      agent_active_runs: 5,
      agent_waiting: 0,
      agent_task_titles: ["同步客户订单", "生成月度汇总", "巡检插件状态"],
    });

    // Exported confirmation formatter / preview modal check:
    const mod = await import("./Desktop");
    expect(typeof (mod as Record<string, unknown>).formatDesktopInterruptWarning).toBe("function");
    const fmt = (mod as unknown as {
      formatDesktopInterruptWarning: (s: desktop.DesktopStatus, zh?: boolean) => string;
    }).formatDesktopInterruptWarning;

    const twoMsg = fmt(
      {
        ...baseStatus,
        agent_active_runs: 2,
        agent_task_titles: ["整理周报", "检查账单"],
      },
      true,
    );
    expect(twoMsg).toContain("2 个 Agent 任务正在运行");
    expect(twoMsg).toContain("整理周报");
    expect(twoMsg).toContain("检查账单");
    expect(twoMsg).not.toContain("等 2 个");

    const fiveMsg = fmt(
      {
        ...baseStatus,
        agent_active_runs: 5,
        agent_task_titles: ["同步客户订单", "生成月度汇总", "巡检插件状态"],
      },
      true,
    );
    expect(fiveMsg).toContain("5 个 Agent 任务正在运行");
    expect(fiveMsg).toContain("同步客户订单");
    expect(fiveMsg).toContain("生成月度汇总");
    expect(fiveMsg).toContain("巡检插件状态");
    expect(fiveMsg).toContain("等 5 个");

    renderDesktop();
    await act(async () => {
      setLanguagePreference("zh-CN");
    });
    await waitFor(() => expect(screen.getByText("5 个 Agent 任务")).toBeInTheDocument());
  });
});
