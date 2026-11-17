import { cleanup, render, screen, waitFor } from "@testing-library/react";
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

it.each(["zh-CN", "en"])("S03-I23 failed full conversation offers a real continuation message (%s)", async language => {
  localStorage.clear();
  await i18n.changeLanguage(language);
  const row: AgentTask & { context_exhausted: boolean } = { id: "full-task", title: "Unfinished report", model_id: "model",
    status: "failed", revision: 2, control_revision: 2, created_at: 1, updated_at: 1,
    messages: [{ role: "user", content: "Finish the financial report" }], events: [], steps: 2,
    result: "", error: "agent_context_limit", limits: {}, pending_approval: null,
    permission_mode: "workspace", context_exhausted: true, goal: { revision: 1, kind: "action", original_message_id: "owner-1", text: "Finish the financial report", constraints: [] },
    allowed_actions: { information: { enabled: true, reason: null, requires_control_revision: true } } };
  const child: AgentTask = { ...row, id: "continued-task", title: "Unfinished report（续）", status: "queued",
    revision: 1, control_revision: 1, error: null, continued_from: row.id };
  const messages = vi.fn();
  const rows: AgentTask[] = [row];
  server.use(
    http.get("*/api/local/agent/settings", () => HttpResponse.json({ enabled: true, model_id: "model", permission_mode: "workspace", allowed_write_tools: [], max_steps: 16, max_seconds: 180, max_context_chars: null, max_output_chars: 20000 })),
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [{ id: "model", name: "Model", available: true, agent_supported: true, source: "local_api" }] })),
    http.get("*/api/local/agent/tools", () => HttpResponse.json({ tools: [] })),
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [] })),
    http.get("*/api/local/agent/automations", () => HttpResponse.json({ automations: [] })),
    http.get("*/api/local/agent/extensions", () => HttpResponse.json({ plugins: [], mcp_servers: [], tools: [] })),
    http.get("*/api/local/agent/extensions/model-availability", () => HttpResponse.json({ available: true, missing_plugins: [], missing_servers: [] })),
    http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks: rows })),
    http.get("*/api/local/agent/tasks/full-task", () => HttpResponse.json(row)),
    http.get("*/api/local/agent/tasks/continued-task", () => HttpResponse.json(child)),
    http.get("*/api/local/agent/tasks/:id/artifacts", () => HttpResponse.json({ artifacts: [] })),
    http.post("*/api/local/agent/tasks/full-task/messages", async ({ request }) => {
      messages(await request.json());
      rows.push({ ...row, ...child });
      return HttpResponse.json(child);
    }),
  );
  const user = userEvent.setup();
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await user.click(await screen.findByRole("button", { name: "Unfinished report" }));
  const text = language.startsWith("zh") ? "对话已满，回复任意内容会自动整理并转到新对话" : "This conversation is full. Reply with anything to summarize it and continue in a new conversation.";
  expect(await screen.findByText(text)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: language.startsWith("zh") ? "继续" : "Continue" }));
  await waitFor(() => expect(messages).toHaveBeenCalledWith(expect.objectContaining({
    message: "继续", intent: "information", expected_control_revision: 2, expected_goal_revision: 1,
  })));
  expect(messages.mock.calls[0][0].client_request_id).toMatch(/^[0-9a-f-]{36}$/);
  await waitFor(() => expect(screen.queryByText(text)).not.toBeInTheDocument());
});
