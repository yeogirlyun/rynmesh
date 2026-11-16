import { render, screen } from "@testing-library/react";
import i18next from "i18next";
import { afterEach, expect, it, vi } from "vitest";
import type { AgentTaskSummary } from "../domain/rynAgent";
import RynAgentConnection, { hasConnectionRecovery } from "./RynAgentConnection";

const baseTask: AgentTaskSummary = {
  id: "task-s01-i3",
  title: "Inspect",
  model_id: "deepseek",
  status: "queued",
  revision: 2,
  created_at: 1,
  updated_at: 2,
  steps: 1,
  pending_approval: null,
  result: "",
  error: null,
};

afterEach(async () => {
  await i18next.changeLanguage("en");
  vi.clearAllMocks();
});

it("T6: suppresses reconnection UI on first retry (retry_count < 2) and shows it from retry_count >= 2", () => {
  const onAction = vi.fn();
  const firstRetryTask: AgentTaskSummary = {
    ...baseTask,
    connection_recovery: { state: "retrying", retry_count: 1, max_retries: null },
  };
  expect(hasConnectionRecovery(firstRetryTask)).toBe(false);
  const { container, rerender } = render(
    <RynAgentConnection task={firstRetryTask} modelName="DeepSeek" busy={false} enabled onAction={onAction} />,
  );
  expect(container).toBeEmptyDOMElement();

  const secondRetryTask: AgentTaskSummary = {
    ...baseTask,
    connection_recovery: { state: "retrying", retry_count: 2, max_retries: null },
  };
  expect(hasConnectionRecovery(secondRetryTask)).toBe(true);
  rerender(
    <RynAgentConnection task={secondRetryTask} modelName="DeepSeek" busy={false} enabled onAction={onAction} />,
  );
  expect(screen.getByText("Reconnecting to DeepSeek")).toBeVisible();
  expect(screen.getByText("Retry 2 · Waiting for network")).toBeVisible();
});

it("Change 6: renders '重试 n · 等待网络恢复' when max_retries is null and '重试 n / max' when finite", async () => {
  await i18next.changeLanguage("zh-CN");
  const onAction = vi.fn();
  const unlimitedTask: AgentTaskSummary = {
    ...baseTask,
    connection_recovery: { state: "retrying", retry_count: 3, max_retries: null, waiting_for_network: true },
  };
  const { rerender } = render(
    <RynAgentConnection task={unlimitedTask} modelName="DeepSeek" busy={false} enabled onAction={onAction} />,
  );
  expect(screen.getByText("正在重新连接 DeepSeek")).toBeVisible();
  expect(screen.getByText("重试 3 · 等待网络恢复")).toBeVisible();
  expect(screen.queryByText(/null/i)).not.toBeInTheDocument();

  const finiteTask: AgentTaskSummary = {
    ...baseTask,
    connection_recovery: { state: "retrying", retry_count: 2, max_retries: 5 },
  };
  rerender(
    <RynAgentConnection task={finiteTask} modelName="DeepSeek" busy={false} enabled onAction={onAction} />,
  );
  expect(screen.getByText("重试 2 / 5")).toBeVisible();
});
