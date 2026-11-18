import { cleanup, render, screen } from "@testing-library/react";
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

it.each(["images_resized_for_shared_route", "images_omitted_for_limits"])("shows the actual old-image omission count for %s", async type => {
  await i18n.changeLanguage("zh-CN");
  const row: AgentTask = { id: "image-task", title: "Image limit review", model_id: "model", status: "completed", run_state: "ended",
    revision: 1, control_revision: 1, created_at: 1, updated_at: 1, messages: [{ role: "user", content: "Look at pictures" }],
    events: [{ seq: 1, type, at: "2026-11-17T00:00:00Z", omitted: 3, omitted_old: 3, resized: 0 }],
    steps: 1, result: "seen", error: null, limits: {}, pending_approval: null };
  server.use(
    http.get("*/api/local/agent/settings", () => HttpResponse.json({ enabled: true, model_id: "model", permission_mode: "automatic", allowed_write_tools: [], max_steps: 16, max_seconds: 180, max_context_chars: null, max_output_chars: 20000 })),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [{ id: "model", name: "Model", available: true, agent_supported: true, source: "local_api" }] })),
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
    http.get("*/api/local/agent/tools", () => HttpResponse.json({ tools: [] })),
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [] })),
    http.get("*/api/local/agent/automations", () => HttpResponse.json({ automations: [] })),
    http.get("*/api/local/agent/extensions", () => HttpResponse.json({ plugins: [], mcp_servers: [], tools: [] })),
    http.get("*/api/local/agent/extensions/model-availability", () => HttpResponse.json({ available: true, missing_plugins: [], missing_servers: [] })),
    http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks: [row] })),
    http.get("*/api/local/agent/tasks/image-task", () => HttpResponse.json(row)),
    http.get("*/api/local/agent/tasks/image-task/artifacts", () => HttpResponse.json({ artifacts: [] })),
  );
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await userEvent.setup().click(await screen.findByRole("button", { name: "Image limit review" }));
  expect(await screen.findByText(/3 张旧图片未发送/)).toBeVisible();
});
