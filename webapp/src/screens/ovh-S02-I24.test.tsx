import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import uiZh from "../locales/ui.zh-CN.json";
import uiEn from "../locales/ui.en.json";
import RynAgent from "./RynAgent";

const invokeMock = vi.fn();

vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

vi.mock("../personal/model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({
    demo: false,
    appearance: "light",
    setAppearance: vi.fn(),
    devices: [],
    services: [],
    space: null,
  }),
}));

describe("S02-I24 Agent needs_attention recovery banner and notification locale keys", () => {
  beforeEach(() => {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    invokeMock.mockReset();
    server.use(
      http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
      http.get("*/api/local/agent/settings", () => new HttpResponse(null, { status: 503 })),
      http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [] })),
      http.get("*/api/local/agent/resources", () =>
        HttpResponse.json({ connections: [], supported_formats: [], create_scope_supported: false }),
      ),
      http.get("*/api/local/agent/skills", () => HttpResponse.json({ skills: [] })),
      http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks: [] })),
      http.get("*/api/local/agent/automations", () => HttpResponse.json({ automations: [], runs: [] })),
    );
  });

  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  });

  it("shows needs_attention banner with last_error and opens logs via desktop_action('logs')", async () => {
    let currentRecovery = "needs_attention";
    let nodeOnline = false;
    invokeMock.mockImplementation((cmd: string, payload?: unknown) => {
      if (cmd === "desktop_status") {
        return Promise.resolve({
          node_online: nodeOnline,
          configured: false,
          sharing: false,
          active_tasks: 0,
          agent_active_runs: 0,
          agent_waiting: 0,
          agent_task_titles: [],
          setup_active: false,
          recovery_attempts: 3,
          recovery: currentRecovery,
          awake_active: false,
          last_error: nodeOnline ? null : "node_port_unavailable",
        });
      }
      if (cmd === "desktop_action") {
        return Promise.resolve("Log folder opened.");
      }
      return Promise.resolve(null);
    });

    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <RynAgent />
      </MemoryRouter>,
    );

    const openLogsBtn = await screen.findByRole("button", { name: /打开日志|Open logs/i });
    expect(openLogsBtn).toBeVisible();
    expect(screen.getByText("node_port_unavailable")).toBeVisible();

    await user.click(openLogsBtn);
    expect(invokeMock).toHaveBeenCalledWith("desktop_action", { action: "logs" });

    // Once node recovers to healthy, banner disappears on refresh.
    currentRecovery = "healthy";
    nodeOnline = true;
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 3200));
    });
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: /打开日志|Open logs/i })).not.toBeInTheDocument();
    });
  });

  it("includes Chinese and English watchdog notification catalog entries", () => {
    const attentionKey = "Ryn node could not start. Retrying periodically; open logs from the tray.";
    const recoveredKey = "Ryn node has recovered.";
    expect((uiZh as Record<string, string>)[attentionKey]).toBe(
      "Ryn 节点无法启动，正在定期重试，可从托盘打开日志",
    );
    expect((uiZh as Record<string, string>)[recoveredKey]).toContain("已恢复");
    expect((uiEn as Record<string, string>)[attentionKey]).toBe(attentionKey);
    expect((uiEn as Record<string, string>)[recoveredKey]).toBe(recoveredKey);
  });
});
