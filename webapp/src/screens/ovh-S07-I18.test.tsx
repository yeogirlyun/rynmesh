import type { ReactNode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import type { AgentTask } from "../domain/rynAgent";
import RynAgentBudget, { budgetStopMessage } from "./RynAgentBudget";
import * as BudgetModule from "./RynAgentBudget";
import i18n from "../i18n";

const baseUsage = {
  model_calls: 85,
  tool_calls: 10,
  charged_model_calls: 85,
  charged_tool_calls: 10,
  total_tokens: 5000,
  charged_tokens: 5000,
  charged_seconds: 20,
  active_seconds: 20,
  cost: null,
  usage_complete: true,
  unsettled_operations: 0,
};

const baseTask: AgentTask = {
  id: "ovh-s07-i18-task",
  title: "Budget overhaul task",
  model_id: "local:test",
  status: "completed",
  revision: 4,
  control_revision: 2,
  created_at: 1,
  updated_at: 2,
  pending_approval: null,
  steps: 5,
  result: "ok",
  error: null,
  messages: [],
  events: [],
  limits: { max_seconds: 900 },
  allowed_actions: {
    budget: { enabled: true, reason: null, requires_control_revision: true },
  },
  budget: {
    round: 1,
    lifetime: baseUsage,
    current_round: baseUsage,
    limits: {
      max_total_model_calls: 100,
      max_total_tool_calls: 256,
      max_total_tokens: 2000000,
      max_total_seconds: 3600,
    },
    limit_sources: {
      max_total_model_calls: "owner",
      max_total_tool_calls: "default",
      max_total_tokens: "default",
      max_total_seconds: "default",
    },
    default_ceilings: {
      max_total_model_calls: null,
      max_total_tool_calls: null,
      max_total_tokens: null,
      max_total_seconds: null,
    } as unknown as Record<string, number>,
  },
};

describe("S07-I18 RynAgentBudget ranges, null ceiling labels, near_limit, quick-extend, and release_legacy", () => {
  beforeEach(async () => {
    sessionStorage.clear();
    await i18n.changeLanguage("zh-CN");
  });

  it("T14: reads limit input ranges from backend budget.ranges and quick-extends by one default step", async () => {
    const received = vi.fn();
    const onChange = vi.fn();
    const user = userEvent.setup();

    const taskWithBackendRanges: AgentTask = {
      ...baseTask,
      status: "failed",
      error: "agent_model_call_budget_exhausted",
      outcome: {
        status: "budget_limited",
        goal_revision: 1,
        criteria_results: [],
        summary: "Stopped at owner model limit",
        evidence_refs: [],
        uncertainties: [],
        budget_stop: {
          code: "agent_model_call_budget_exhausted",
          limit: "max_total_model_calls",
          source: "owner",
        },
      },
      budget: {
        ...baseTask.budget!,
        ranges: {
          max_total_model_calls: [1, 77777],
          max_total_tool_calls: [1, 400000],
          max_total_tokens: [1000, 2000000000],
          max_total_seconds: [10, 2592000],
        },
      } as AgentTask["budget"],
    };

    server.use(
      http.put("*/api/local/agent/tasks/ovh-s07-i18-task/budget", async ({ request }) => {
        const body = await request.json();
        received(body);
        return HttpResponse.json({
          ...taskWithBackendRanges,
          control_revision: 3,
          budget: {
            ...taskWithBackendRanges.budget!,
            limits: { ...taskWithBackendRanges.budget!.limits, ...(body as { limits?: Record<string, number> }).limits },
          },
        });
      }),
    );

    render(<RynAgentBudget task={taskWithBackendRanges} busy={false} onChange={onChange} />);

    // Step 4: null default_ceilings shows only "默认，随进展自动延长" without "最高"
    expect(screen.getAllByText("默认，随进展自动延长").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText(/最高/)).not.toBeInTheDocument();

    // Step 9: legacy default stop message compatibility copy (no longer tells user to narrow scope and create a new task)
    const compatDefaultStop = budgetStopMessage(
      {
        ...taskWithBackendRanges,
        outcome: {
          ...taskWithBackendRanges.outcome!,
          budget_stop: {
            code: "agent_token_budget_exhausted",
            limit: "max_total_tokens",
            source: "default",
          },
        },
        error: "agent_token_budget_exhausted",
      },
      true,
    )!;
    expect(compatDefaultStop).toContain("旧版本");
    expect(compatDefaultStop).not.toContain("缩小范围后新建任务");

    // Step 8: input max comes from backend budget.ranges (77777) and accepts 50000
    await user.click(screen.getByRole("button", { name: "调整预算" }));
    const modelInput = screen.getByLabelText("模型请求") as HTMLInputElement;
    expect(modelInput.max).toBe("77777");
    await user.clear(modelInput);
    await user.type(modelInput, "50000");
    expect(screen.getByRole("button", { name: "保存预算" })).not.toBeDisabled();
    await user.click(screen.getByRole("button", { name: "取消编辑" }));

    // Step 9 & T14: owner budget stop renders "一键续额" button which raises max_total_model_calls by +128 (100 -> 228)
    const quickExtendButtons = screen.getAllByRole("button", { name: "一键续额" });
    expect(quickExtendButtons.length).toBeGreaterThanOrEqual(1);
    await user.click(quickExtendButtons[0]);

    await waitFor(() => expect(received).toHaveBeenCalledTimes(1));
    expect(received.mock.calls[0][0]).toEqual(
      expect.objectContaining({
        limits: { max_total_model_calls: 228 },
        expected_control_revision: 2,
      }),
    );
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("T15: renders near_limit banner in budget card and chat top banner with working quick-extend and legacy release buttons", async () => {
    const received = vi.fn();
    const onChange = vi.fn();
    const user = userEvent.setup();

    const nearLimitTask: AgentTask = {
      ...baseTask,
      budget: {
        ...baseTask.budget!,
        near_limit: ["max_total_model_calls"],
      } as AgentTask["budget"],
    };

    server.use(
      http.put("*/api/local/agent/tasks/ovh-s07-i18-task/budget", async ({ request }) => {
        const body = await request.json();
        received(body);
        return HttpResponse.json(nearLimitTask);
      }),
    );

    // Exported chat-top banner component should also exist for RynAgent.tsx top-of-conversation display
    const TopBanner = (BudgetModule as Record<string, unknown>).RynAgentNearLimitBanner as
      | ((props: { task: AgentTask; busy: boolean; onChange: (task: AgentTask) => void }) => ReactNode)
      | undefined;
    expect(typeof TopBanner).toBe("function");

    const { unmount } = render(
      <>
        {TopBanner && <TopBanner task={nearLimitTask} busy={false} onChange={onChange} />}
        <RynAgentBudget task={nearLimitTask} busy={false} onChange={onChange} />
      </>,
    );

    const banners = screen.getAllByText(/即将达到你设的.*模型请求.*上限/);
    expect(banners.length).toBeGreaterThanOrEqual(2);

    const extendButtons = screen.getAllByRole("button", { name: "一键续额" });
    expect(extendButtons.length).toBeGreaterThanOrEqual(2);
    await user.click(extendButtons[0]);

    await waitFor(() => expect(received).toHaveBeenCalledTimes(1));
    expect(received.mock.calls[0][0]).toEqual(
      expect.objectContaining({
        limits: { max_total_model_calls: 228 },
        expected_control_revision: 2,
      }),
    );
    unmount();

    // Step 7: legacy_unknown budget stop renders "解除旧上限" button sending release_legacy: true
    const legacyStoppedTask: AgentTask = {
      ...baseTask,
      status: "failed",
      error: "agent_model_call_budget_exhausted",
      outcome: {
        status: "budget_limited",
        goal_revision: 1,
        criteria_results: [],
        summary: "Stopped at legacy limit",
        evidence_refs: [],
        uncertainties: [],
        budget_stop: {
          code: "agent_model_call_budget_exhausted",
          limit: "max_total_model_calls",
          source: "legacy_unknown",
        },
      },
      budget: {
        ...baseTask.budget!,
        limits: { ...baseTask.budget!.limits, max_total_model_calls: 300 },
        limit_sources: { ...baseTask.budget!.limit_sources, max_total_model_calls: "legacy_unknown" },
      },
    };

    render(<RynAgentBudget task={legacyStoppedTask} busy={false} onChange={onChange} />);
    const releaseBtn = screen.getByRole("button", { name: "解除旧上限" });
    await user.click(releaseBtn);

    await waitFor(() => expect(received).toHaveBeenCalledTimes(2));
    expect(received.mock.calls[1][0]).toEqual(
      expect.objectContaining({
        limits: {},
        release_legacy: true,
        expected_control_revision: 2,
      }),
    );
  });
});
