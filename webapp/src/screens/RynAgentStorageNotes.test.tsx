import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import type { AgentTask } from "../domain/rynAgent";
import { agentTaskCache } from "../domain/agentWorkspaceCache";
import RynAgent from "./RynAgent";

vi.mock("../personal/model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }),
}));

const NOTE = "Recovered completed read calls from a partial model response; the original response did not finish.";
const call = (id: string) => ({ id, type: "function" as const, function: { name: "ryn_plugin_info", arguments: "{}" } });

function failedTask(messages: AgentTask["messages"]): AgentTask {
  return {
    id: "storage-task", title: "Caesar shops", model_id: "model", status: "failed", run_state: "ended", revision: 5,
    schema_version: 2, control_revision: 2, event_cursor: 5,
    created_at: 1, updated_at: 1, messages, events: [], pending_approval: null, steps: 2, result: "", error: "agent_storage_limit",
    limits: {}, permission_mode: "workspace", allowed_write_tools: [],
  };
}

function serve(current: AgentTask) {
  server.use(
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json({ enabled: true, model_id: "model", permission_mode: "workspace", allowed_write_tools: [], max_steps: 16, max_seconds: 180, max_context_chars: 60000, max_output_chars: 20000 })),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [{ id: "model", name: "Model", source: "local_api", available: true, agent_supported: true }] })),
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [] })),
    http.get("*/api/local/agent/skills", () => HttpResponse.json({ skills: [] })),
    http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks: [current] })),
    http.get("*/api/local/agent/tasks/:id", () => HttpResponse.json(current)),
    http.get("*/api/local/agent/tasks/:id/artifacts", () => HttpResponse.json({ artifacts: [] })),
  );
}

beforeEach(() => { sessionStorage.clear(); localStorage.clear(); agentTaskCache().clear(); sessionStorage.setItem("ryn.agent.selected", "storage-task"); });
afterEach(() => vi.restoreAllMocks());

it("shows its own wording for a recovered partial reply, new or legacy, and a clear full-storage error", async () => {
  serve(failedTask([
    { role: "user", content: "List Caesar shops" },
    { role: "assistant", content: NOTE, tool_calls: [call("call_a")], ui: { kind: "partial_recovery" } },
    { role: "tool", tool_call_id: "call_a", content: "{}" },
    { role: "assistant", content: NOTE, tool_calls: [call("call_b")] },
    { role: "tool", tool_call_id: "call_b", content: "{}" },
  ]));
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  expect(await screen.findAllByText("The model's reply stopped partway. Completed read results were kept.")).toHaveLength(2);
  expect(screen.queryByText(NOTE)).toBeNull();
  expect(screen.getByText(/The disk holding your tasks has insufficient free space/)).toBeVisible();
  expect(screen.getByText(/Free space on that disk and retry; your conversation is preserved/)).toBeVisible();
});
