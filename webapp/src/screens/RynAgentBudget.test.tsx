import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import type { AgentTask } from "../domain/rynAgent";
import RynAgentBudget from "./RynAgentBudget";
import i18n from "../i18n";

const usage = { model_calls: 3, tool_calls: 4, total_tokens: null, charged_tokens: 2500, charged_seconds: 12, active_seconds: 12, cost: null, usage_complete: false, unsettled_operations: 0 };
const task: AgentTask = { id: "budget-task", title: "Report", model_id: "model", status: "failed", revision: 8, control_revision: 4, created_at: 1, updated_at: 1, pending_approval: null, steps: 3, result: "", error: "agent_token_budget_exhausted", messages: [], events: [], limits: {},
  allowed_actions: { budget: { enabled: true, reason: null, requires_control_revision: true } }, budget: { round: 2, lifetime: usage, current_round: { ...usage, model_calls: 1 }, limits: { max_total_model_calls: 128, max_total_tool_calls: 256, max_total_tokens: 10000, max_total_seconds: 3600 } } };
beforeEach(() => sessionStorage.clear());

it("edits the actual per-run timeout separately from lifetime allowance", async () => {
  const received = vi.fn(); const user = userEvent.setup();
  server.use(http.put("*/api/local/agent/tasks/budget-task/budget", async ({ request }) => { received(await request.json()); return HttpResponse.json(task); }));
  render(<RynAgentBudget task={{ ...task, limits: { max_seconds: 180 } }} busy={false} onChange={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: "Adjust limits" }));
  await user.clear(screen.getByLabelText("Run limit (seconds)"));
  await user.type(screen.getByLabelText("Run limit (seconds)"), "900");
  await user.click(screen.getByRole("button", { name: "Save limits" }));
  await waitFor(() => expect(received).toHaveBeenCalledWith(expect.objectContaining({ run_max_seconds: 900, limits: task.budget!.limits, expected_control_revision: 4 })));
});

it("shows unknown measured usage separately from conservative charges", () => {
  render(<RynAgentBudget task={task} busy={false} onChange={vi.fn()} />);
  expect(screen.getAllByText("Unknown")).toHaveLength(2);
  expect(screen.getByText(/2,500 Token/)).toBeVisible();
  expect(screen.getByText(/Cost: unknown/)).toBeVisible();
  expect(screen.getByRole("status")).toHaveTextContent("Remaining token allowance cannot cover the next request");
});

it("saves the version captured at edit time without resuming execution", async () => {
  const received = vi.fn(); const changed = vi.fn(); const user = userEvent.setup();
  server.use(http.put("*/api/local/agent/tasks/budget-task/budget", async ({ request }) => { received(await request.json()); return HttpResponse.json({ ...task, control_revision: 5 }); }));
  const view = render(<RynAgentBudget task={task} busy={false} onChange={changed} />);
  await user.click(screen.getByRole("button", { name: "Adjust limits" }));
  await user.clear(screen.getByLabelText("Token")); await user.type(screen.getByLabelText("Token"), "20000");
  view.rerender(<RynAgentBudget task={{ ...task, control_revision: 6 }} busy={false} onChange={changed} />);
  await user.click(screen.getByRole("button", { name: "Save limits" }));
  await waitFor(() => expect(changed).toHaveBeenCalled());
  expect(received).toHaveBeenCalledWith(expect.objectContaining({ expected_control_revision: 4, limits: expect.objectContaining({ max_total_tokens: 20000 }) }));
  expect(changed.mock.calls[0][0].status).toBe("failed");
});

it("retains an uncertain budget request across remount and retries exact body", async () => {
  const received = vi.fn(); const user = userEvent.setup();
  server.use(http.put("*/api/local/agent/tasks/budget-task/budget", async ({ request }) => { received(await request.json()); return received.mock.calls.length === 1 ? HttpResponse.json({}, { status: 503 }) : HttpResponse.json(task); }));
  const view = render(<RynAgentBudget task={task} busy={false} onChange={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: "Adjust limits" }));
  await user.click(screen.getByRole("button", { name: "Save limits" }));
  await screen.findByRole("alert"); view.unmount();
  render(<RynAgentBudget task={{ ...task, control_revision: 99 }} busy={false} onChange={vi.fn()} />);
  expect(received).toHaveBeenCalledTimes(1);
  await user.click(screen.getByRole("button", { name: "Check original save" }));
  await waitFor(() => expect(received).toHaveBeenCalledTimes(2));
  expect(received.mock.calls[0][0]).toEqual(received.mock.calls[1][0]);
  await waitFor(() => expect(sessionStorage.getItem("ryn.agent.pending-budget.budget-task")).toBeNull());
});

it("does not permit adjustments while the server forbids them", () => {
  render(<RynAgentBudget task={{ ...task, allowed_actions: { budget: { enabled: false, reason: "agent_invalid_transition", requires_control_revision: true } } }} busy={false} onChange={vi.fn()} />);
  expect(screen.getByRole("button", { name: "Adjust limits" })).toBeDisabled();
});

it("shows incomplete legacy call totals as unknown while retaining known floors", () => {
  const legacy = { ...usage, model_calls: null, tool_calls: null, active_seconds: null, legacy_usage_unknown: true, charged_model_calls: 3, charged_tool_calls: 2, charged_seconds: 0.47 };
  render(<RynAgentBudget task={{ ...task, budget: { ...task.budget!, lifetime: legacy, current_round: legacy } }} busy={false} onChange={vi.fn()} />);
  expect(screen.getAllByText("Unknown")).toHaveLength(8);
  expect(screen.getByText(/known lifetime call floors are/)).toHaveTextContent("3 model requests, 2 tool calls");
  expect(screen.getByText(/2,500 Token/)).toHaveTextContent("0.47 seconds");
});

it("shows the actual run limit separately from narrower call time on timeout", () => {
  const failed = { ...task, error: "agent_time_limit", run_timing: { recorded_seconds: 600.2, elapsed_seconds: 600.2, max_seconds: 600, measurement: "recorded" as const },
    budget: { ...task.budget!, current_round: { ...usage, active_seconds: 398.175 }, lifetime: { ...usage, active_seconds: 398.175 } } };
  render(<RynAgentBudget task={failed} busy={false} onChange={vi.fn()} />);
  expect(screen.getByRole("status")).toHaveTextContent("The run time limit was reached");
  expect(screen.getByText(/Run time limit \(seconds\):/)).toHaveTextContent("600.2 / 600 · Recorded time");
  expect(within(screen.getByRole("row", { name: /Model and tool call time/ })).getAllByRole("cell").map(cell => cell.textContent)).toEqual(["398.18", "398.18", "3,600"]);
  expect(screen.getByText(/The run limit includes request preparation/)).toHaveTextContent("waiting for approval or input, pauses, and queue time are excluded");
});

it("uses server snapshots while running and recorded values when waiting", () => {
  const live: AgentTask = { ...task, status: "running", error: null, run_timing: { recorded_seconds: 12, elapsed_seconds: 18, max_seconds: 180, measurement: "live" } };
  const view = render(<RynAgentBudget task={live} busy={false} onChange={vi.fn()} />);
  expect(screen.getByText(/Run time limit \(seconds\):/)).toHaveTextContent("18 / 180 · Snapshot at this query");
  view.rerender(<RynAgentBudget task={{ ...live, status: "waiting_approval", run_timing: { ...live.run_timing!, elapsed_seconds: 20, recorded_seconds: 20, measurement: "recorded" } }} busy={false} onChange={vi.fn()} />);
  expect(screen.getByText(/Run time limit \(seconds\):/)).toHaveTextContent("20 / 180 · Recorded time");
  expect(screen.queryByText(/Snapshot at this query/)).not.toBeInTheDocument();
});

it("marks unavailable and unsettled timing without inventing elapsed time", () => {
  const view = render(<RynAgentBudget task={{ ...task, limits: { max_seconds: 600 } }} busy={false} onChange={vi.fn()} />);
  expect(screen.getByText(/Run time limit \(seconds\):/)).toHaveTextContent("Unknown / 600");
  expect(screen.getByText(/Run timing is unknown/)).toBeVisible();
  view.rerender(<RynAgentBudget task={{ ...task, run_timing: { recorded_seconds: 7, elapsed_seconds: 7, max_seconds: 600, measurement: "pending" } }} busy={false} onChange={vi.fn()} />);
  expect(screen.getByText(/Run time limit \(seconds\):/)).toHaveTextContent("7 / 600 · Recorded time");
  expect(screen.getByText(/The current operation has not settled/)).toBeVisible();
  expect(screen.getByText(/unsettled time before a restart is not reconstructed/)).toBeVisible();
});

it("explains the distinct run clock in Chinese", async () => {
  await i18n.changeLanguage("zh-CN");
  render(<RynAgentBudget task={{ ...task, error: "agent_time_limit", run_timing: { recorded_seconds: 600, elapsed_seconds: 600, max_seconds: 600, measurement: "recorded" } }} busy={false} onChange={vi.fn()} />);
  expect(screen.getByRole("status")).toHaveTextContent("本次运行时间达到上限");
  expect(screen.getByText(/本次运行时限（秒）/)).toHaveTextContent("600 / 600 · 已记录时间");
  expect(screen.getByRole("row", { name: /模型与工具调用时间/ })).toBeVisible();
});
