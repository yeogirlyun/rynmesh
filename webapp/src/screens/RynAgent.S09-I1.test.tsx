import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { agentMessageBody, rynAgent, type AgentTask } from "../domain/rynAgent";
import { SkillSelection, TaskSkills } from "./RynAgentGuidance";
import { CodexSkillsView } from "./CodexSkillsView";
import RynAgent from "./RynAgent";

vi.mock("../personal/model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }),
}));

const skill = { id: "demo-skill", title: "Demo workflow", version: "1", description: "Workflow" };
const base = { id: "existing", title: "Existing task", model_id: "model", status: "paused", revision: 1,
  created_at: 1, updated_at: 1, messages: [{ role: "user", content: "Original request" }], events: [],
  pending_approval: null, steps: 1, result: "", error: null, limits: {}, permission_mode: "confirm_changes", allowed_write_tools: [] } as AgentTask;

it.each(["legacy", "control", "queue"])("T5 existing task selector sends selected skill_ids (%s)", async mode => {
  localStorage.clear(); sessionStorage.clear();
  const task = mode !== "legacy" ? { ...base, schema_version: 2, control_revision: 3,
    status: mode === "queue" ? "running" : "paused", run_state: mode === "queue" ? "running" : "paused", goal: { revision: 1, constraints: [] },
    allowed_actions: { information: { enabled: true, requires_control_revision: true }, steer: { enabled: true }, resume: { enabled: true } } } : base;
  const received = vi.fn();
  server.use(
    http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json({ enabled: true, model_id: "model", permission_mode: "confirm_changes", allowed_write_tools: [] })),
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [{ id: "model", name: "Model", available: true, agent_supported: true }] })),
    http.get("*/api/local/agent/tools", () => HttpResponse.json({ tools: [] })),
    http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [], create_scope_supported: false })),
    http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks: [task, { ...task, id: "other", title: "Other task" }] })),
    http.get("*/api/local/agent/tasks/existing", () => HttpResponse.json(task)),
    http.get("*/api/local/agent/tasks/other", () => HttpResponse.json({ ...task, id: "other", title: "Other task" })),
    http.get("*/api/local/agent/tasks/other/artifacts", () => HttpResponse.json({ artifacts: [] })),
    http.get("*/api/local/agent/tasks/existing/artifacts", () => HttpResponse.json({ artifacts: [] })),
    http.get("*/api/local/agent/skills", () => HttpResponse.json({ skills: [skill] })),
    http.post("*/api/local/agent/tasks/existing/messages", async ({ request }) => { received(await request.json()); return HttpResponse.json(task); }),
    http.post("*/api/local/agent/tasks/existing/queue", async ({ request }) => { received(await request.json()); return HttpResponse.json(task); }),
  );
  const user = userEvent.setup();
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  await user.click(await screen.findByRole("button", { name: "Existing task" }));
  await screen.findByText("Original request");
  await user.click(screen.getByRole("button", { name: "Skills · optional" }));
  await user.click(await screen.findByRole("checkbox", { name: /Demo workflow/ }));
  await user.type(screen.getByRole("textbox"), "Use this workflow");
  await user.click(screen.getByRole("button", { name: mode === "queue" ? "Queue follow-up" : /Send information|Send message|Send follow-up/ }));
  await waitFor(() => expect(received).toHaveBeenCalledWith(expect.objectContaining({ message: "Use this workflow", skill_ids: ["demo-skill"] })));
  await waitFor(() => expect(screen.getByRole("button", { name: "Skills · optional" })).toBeInTheDocument());
  const remaining = screen.queryByRole("checkbox", { name: /Demo workflow/ });
  if (remaining) expect(remaining).not.toBeChecked();
  await user.click(screen.getByRole("button", { name: "Skills · optional" }));
  // If the prior selector survived the send it just closed; open it again.
  if (!screen.queryByRole("checkbox", { name: /Demo workflow/ })) await user.click(screen.getByRole("button", { name: "Skills · optional" }));
  await user.click(await screen.findByRole("checkbox", { name: /Demo workflow/ }));
  await user.click(screen.getByRole("button", { name: "Other task" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Skills · optional" })).toBeInTheDocument());
});

it("message API and reliable-control builder carry optional skill_ids", async () => {
  const received = vi.fn();
  server.use(http.post("*/api/local/agent/tasks/existing/messages", async ({ request }) => { received(await request.json()); return HttpResponse.json(base); }));
  await rynAgent.message(base, "More", "information", "request-id", undefined, undefined, ["demo-skill"]);
  expect(received).toHaveBeenCalledWith(expect.objectContaining({ skill_ids: ["demo-skill"] }));
  expect(agentMessageBody(base, "More", "information", undefined, undefined, ["demo-skill"])).toHaveProperty("skill_ids", ["demo-skill"]);
});

it("guidance explains all enabled skills, any-turn mentions and pinned versions", async () => {
  render(<><TaskSkills skills={[skill]} /><SkillSelection value={[]} onChange={vi.fn()} disabled={false} /></>);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Skills · optional" }));
  expect(screen.getByText(/All enabled skills are available/)).toBeInTheDocument();
  expect(screen.getByText(/\$skill-name/)).toBeInTheDocument();
  expect(screen.getByText(/Skill versions are fixed when added/)).toBeInTheDocument();
});

it("skills settings switch persists through the real PATCH API", async () => {
  let enabled = true;
  const received = vi.fn();
  server.use(
    http.get("*/api/local/agent/extensions", () => HttpResponse.json({ revision: 1, skills_catalog_in_context: enabled, skills: [], plugins: [], mcp_servers: [] })),
    http.get("*/api/local/agent/extensions/skills/recommended", () => HttpResponse.json([])),
    http.patch("*/api/local/agent/extensions/skills/settings", async ({ request }) => {
      const body = await request.json() as { skills_catalog_in_context: boolean }; received(body); enabled = body.skills_catalog_in_context;
      return HttpResponse.json({ skills_catalog_in_context: enabled });
    }),
  );
  const user = userEvent.setup();
  render(<MemoryRouter><CodexSkillsView /></MemoryRouter>);
  const toggle = await screen.findByRole("checkbox", { name: "Include skills catalog in context" });
  expect(toggle).toBeChecked();
  await user.click(toggle);
  await waitFor(() => expect(received).toHaveBeenCalledWith({ skills_catalog_in_context: false }));
  await waitFor(() => expect(toggle).not.toBeChecked());
});
