import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import i18n from "../i18n";
import { agentTaskCache } from "../domain/agentWorkspaceCache";
import type { AgentTask } from "../domain/rynAgent";
import { agentErrorActionLabel, agentErrorHelp } from "./rynAgentErrors";
import AgentExtensions from "./AgentExtensions";
import RynAgent from "./RynAgent";

vi.mock("../personal/model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }),
}));

beforeEach(() => {
  sessionStorage.clear(); localStorage.clear(); agentTaskCache().clear();
});

it("S13-I33 T5 distinguishes extension state size from disk exhaustion in both languages", () => {
  const zh = agentErrorHelp("agent_extension_state_limit", true);
  const en = agentErrorHelp("agent_extension_state_limit", false);
  expect(zh.text).toBe("插件和 MCP 配置太大，请移除不用的插件或服务器");
  expect(en.text).toBe("Plugin and MCP configuration is too large. Remove unused plugins or servers.");
  expect(zh.actions).toEqual(["open_extensions"]);
  expect(en.actions).toEqual(["open_extensions"]);
  expect(agentErrorActionLabel(zh.actions[0], true)).toBe("打开扩展");
  expect(agentErrorActionLabel(en.actions[0], false)).toBe("Open extensions");
});

it.each(["zh", "en"])("S13-I33 T1 displays previous-configuration recovery in %s", async language => {
  await i18n.changeLanguage(language);
  serveOverview({ status: "recovered_from_prev" });
  renderExtensions();
  expect(await screen.findByText(language === "zh" ? "已恢复到上一次的配置" : "Restored the previous extension configuration.")).toBeVisible();
});

it.each(["zh", "en"])("S13-I33 T2 displays quarantine location and re-add guidance in %s", async language => {
  await i18n.changeLanguage(language);
  serveOverview({ status: "reset", quarantine_path: "agent/quarantine/synthetic-test" });
  renderExtensions();
  expect(await screen.findByText(language === "zh"
    ? "扩展配置已损坏，已备份到 agent/quarantine/synthetic-test，请重新添加"
    : "Extension configuration was damaged and backed up to agent/quarantine/synthetic-test. Please add your extensions again.")).toBeVisible();
});

function serveOverview(recovery: { status: string; quarantine_path?: string }) {
  server.use(
    http.get("*/api/local/agent/extensions", () => HttpResponse.json({
      revision: 1, desktop_access: false, plugins: [], skills: [], mcp_servers: [],
      recovered_from_prev: recovery.status === "recovered_from_prev", recovery,
    })),
    http.get("*/api/local/agent/extensions/skills/recommended", () => HttpResponse.json({ skills: [], count: 0 })),
    http.get("*/api/local/agent/computer/status", () => HttpResponse.json({ enabled: false, desktop_supported: true })),
  );
}

function renderExtensions() {
  render(<MemoryRouter initialEntries={["/agent/extensions"]}>
    <Routes><Route path="/agent/extensions" element={<AgentExtensions />} /></Routes>
  </MemoryRouter>);
}

it.each([
  ["recovered_from_prev", false], ["recovered_from_prev", true],
  ["reset", false], ["reset", true],
] as const)("S13-I33 T1/T2 accepts a recovery rollback on an already open page (%s, prior notice %s)", async (status, priorNotice) => {
  await i18n.changeLanguage("en");
  const oldPlugin = { id: "removed", name: "removed", title: "Old plugin", enabled: false, revision: 5 };
  let current: Record<string, unknown> = { revision: 5, desktop_access: false, plugins: [oldPlugin], skills: [], mcp_servers: [],
    ...(priorNotice ? { recovery: { status: "recovered_from_prev", id: "prior-recovery" } } : {}) };
  serveOverview({ status: "recovered_from_prev" });
  server.use(http.get("*/api/local/agent/extensions", () => HttpResponse.json(current)));
  renderExtensions();
  expect(await screen.findByText("Old plugin")).toBeVisible();
  current = { revision: status === "reset" ? 1 : 4, desktop_access: false, plugins: [], skills: [], mcp_servers: [],
    recovery: { status, id: "next-recovery", quarantine_path: "agent/quarantine/new-event" } };
  await userEvent.setup().click(screen.getByRole("button", { name: "Refresh" }));
  await waitFor(() => expect(screen.queryByText("Old plugin")).not.toBeInTheDocument());
  expect(await screen.findByText(status === "reset"
    ? "Extension configuration was damaged and backed up to agent/quarantine/new-event. Please add your extensions again."
    : "Restored the previous extension configuration.")).toBeVisible();
});

it("S13-I33 keeps rejecting stale responses from the same recovery event", async () => {
  let current = { revision: 5, desktop_access: false, plugins: [{ id: "current", name: "current", title: "Current plugin", revision: 5 }], skills: [], mcp_servers: [],
    recovery: { status: "recovered_from_prev", id: "same-event" } };
  serveOverview({ status: "recovered_from_prev" });
  server.use(http.get("*/api/local/agent/extensions", () => HttpResponse.json(current)));
  renderExtensions();
  expect(await screen.findByText("Current plugin")).toBeVisible();
  current = { ...current, revision: 4, plugins: [{ id: "stale", name: "stale", title: "Stale plugin", revision: 4 }] };
  const refresh = screen.getByRole("button", { name: "Refresh" });
  await userEvent.setup().click(refresh);
  await waitFor(() => expect(refresh).toBeEnabled());
  expect(screen.getByText("Current plugin")).toBeVisible();
  expect(screen.queryByText("Stale plugin")).not.toBeInTheDocument();
});

it("S13-I33 T5 opens the actual extension page from the task error action", async () => {
  const task: AgentTask = {
    id: "state-limit-task", title: "Extension task", model_id: "model", status: "failed", run_state: "ended", revision: 5,
    schema_version: 2, control_revision: 2, event_cursor: 5, created_at: 1, updated_at: 1,
    messages: [{ role: "user", content: "Use an extension" }], events: [], pending_approval: null,
    steps: 2, result: "", error: "agent_extension_state_limit", limits: {}, permission_mode: "workspace", allowed_write_tools: [],
  };
  sessionStorage.setItem("ryn.agent.selected", task.id);
  server.use(
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json({ enabled: true, model_id: "model", permission_mode: "workspace", allowed_write_tools: [], max_steps: 16, max_seconds: 180, max_context_chars: 60000, max_output_chars: 20000 })),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [{ id: "model", name: "Model", source: "local_api", available: true, agent_supported: true }] })),
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [] })),
    http.get("*/api/local/agent/skills", () => HttpResponse.json({ skills: [] })),
    http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks: [task] })),
    http.get("*/api/local/agent/tasks/:id", () => HttpResponse.json(task)),
    http.get("*/api/local/agent/tasks/:id/artifacts", () => HttpResponse.json({ artifacts: [] })),
  );
  render(<MemoryRouter initialEntries={["/agent"]}><Routes>
    <Route path="/agent" element={<RynAgent />} />
    <Route path="/agent/extensions" element={<p>Extension destination</p>} />
  </Routes></MemoryRouter>);
  await userEvent.setup().click(await screen.findByRole("button", { name: "Open extensions" }));
  expect(await screen.findByText("Extension destination")).toBeVisible();
});
