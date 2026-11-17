import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import RynAgent from "./RynAgent";
import i18n from "../i18n";
import type { AgentTask } from "../domain/rynAgent";

vi.mock("../personal/model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }),
}));
afterEach(cleanup);

it.each([
  ["en", "Forced removal", "Dangerous command", "Command"],
  ["zh-CN", "强制删除命令", "危险命令", "命令"],
])("shows %s shell approval reason and structured command before approving the bound call", async (language, reasonText, dangerLabel, commandLabel) => {
  await i18n.changeLanguage(language);
  let row: AgentTask = { id: "danger-task", title: "Dangerous shell review", model_id: "model", permission_mode: "workspace",
    status: "waiting_approval", run_state: "waiting_approval", revision: 1, control_revision: 1,
    created_at: 1, updated_at: 1, messages: [{ role: "user", content: "Review cleanup" }], events: [], steps: 1,
    result: "", error: null, limits: {},
    allowed_actions: { approve: { enabled: true, reason: null, requires_control_revision: true }, deny: { enabled: true, reason: null, requires_control_revision: true } },
    pending_approval: { id: "approval-danger", call_id: "call-danger", arguments_sha256: "bound-hash", tool: "ryn_shell_execute",
      reason: "dangerous_command", dangerous_reason: "forced_rm", arguments: { command: "rm -rf old", cwd: "/work", shell: "sh", timeout_seconds: 12 } } };
  const actions = vi.fn();
  server.use(
    http.get("*/api/local/agent/settings", () => HttpResponse.json({ enabled: true, model_id: "model", permission_mode: "workspace", allowed_write_tools: [], max_steps: 16, max_seconds: 180, max_context_chars: null, max_output_chars: 20000 })),
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [{ id: "model", name: "Model", available: true, agent_supported: true, source: "local_api" }] })),
    http.get("*/api/local/agent/tools", () => HttpResponse.json({ tools: [] })),
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [] })),
    http.get("*/api/local/agent/automations", () => HttpResponse.json({ automations: [] })),
    http.get("*/api/local/agent/extensions", () => HttpResponse.json({ plugins: [], mcp_servers: [], tools: [] })),
    http.get("*/api/local/agent/extensions/model-availability", () => HttpResponse.json({ available: true, missing_plugins: [], missing_servers: [] })),
    http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks: [row] })),
    http.get("*/api/local/agent/tasks/danger-task", () => HttpResponse.json(row)),
    http.get("*/api/local/agent/tasks/danger-task/artifacts", () => HttpResponse.json({ artifacts: [] })),
    http.post("*/api/local/agent/tasks/danger-task/actions", async ({ request }) => {
      actions(await request.json());
      row = { ...row, status: "completed", run_state: "ended", control_revision: 2, revision: 2, pending_approval: null };
      return HttpResponse.json(row);
    }),
  );
  const user = userEvent.setup();
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await user.click(await screen.findByRole("button", { name: "Dangerous shell review" }));
  const reason = await screen.findByText(reasonText);
  const approval = within(reason.closest("section")!);
  expect(approval.getByText(dangerLabel)).toBeInTheDocument();
  expect(approval.getByText(commandLabel)).toBeInTheDocument();
  expect(approval.getByText("rm -rf old")).toBeInTheDocument();
  expect(approval.getByText("/work")).toBeInTheDocument();
  expect(approval.getByText("sh")).toBeInTheDocument();
  expect(approval.getByText("12s")).toBeInTheDocument();
  expect(actions).not.toHaveBeenCalled();
  await user.click(approval.getByRole("button", { name: i18n.t("approve", { ns: "rynAgent" }) }));
  await waitFor(() => expect(actions).toHaveBeenCalledWith(expect.objectContaining({ action: "approve", approval_id: "approval-danger" })));
  await waitFor(() => expect(screen.queryByText(reasonText)).not.toBeInTheDocument());
});
