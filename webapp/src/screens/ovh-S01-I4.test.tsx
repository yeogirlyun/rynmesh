import { render, screen } from "@testing-library/react";
import i18next from "i18next";
import { afterEach, expect, it, vi } from "vitest";
import type { AgentTaskSummary } from "../domain/rynAgent";
import RynAgentConnection from "./RynAgentConnection";
import { agentErrorHelp, KNOWN_AGENT_ERROR_CODES } from "./rynAgentErrors";

const baseTask: AgentTaskSummary = {
  id: "task-s01-i4",
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

it("T10: renders new connection diagnostics reasons (idle_timeout/timeout, 5xx/overloaded, 429 with retry_after, context_overflow)", async () => {
  await i18next.changeLanguage("zh-CN");
  const onAction = vi.fn();

  // 1. idle_timeout / timeout
  const { rerender } = render(
    <RynAgentConnection
      task={{
        ...baseTask,
        connection_recovery: {
          state: "retrying",
          retry_count: 2,
          max_retries: 5,
          diagnostics: { network_reason: "idle_timeout" },
        },
      }}
      modelName="DeepSeek"
      busy={false}
      enabled
      onAction={onAction}
    />,
  );
  expect(screen.getByText(/模型长时间没有响应/)).toBeVisible();

  // 2. 5xx / overloaded
  rerender(
    <RynAgentConnection
      task={{
        ...baseTask,
        connection_recovery: {
          state: "retrying",
          retry_count: 2,
          max_retries: 5,
          diagnostics: { network_reason: "http", http_status: 529, overloaded: true },
        },
      }}
      modelName="DeepSeek"
      busy={false}
      enabled
      onAction={onAction}
    />,
  );
  expect(screen.getByText(/服务繁忙，稍后自动重试/)).toBeVisible();

  // 3. 429 with retry_after_seconds recovery time
  rerender(
    <RynAgentConnection
      task={{
        ...baseTask,
        connection_recovery: {
          state: "retrying",
          retry_count: 2,
          max_retries: 5,
          diagnostics: { network_reason: "http", http_status: 429, retry_after_seconds: 15 },
        },
      }}
      modelName="DeepSeek"
      busy={false}
      enabled
      onAction={onAction}
    />,
  );
  expect(screen.getByText(/请求太频繁/)).toBeVisible();
  expect(screen.getByText(/15/)).toBeVisible();

  // 4. context_overflow
  rerender(
    <RynAgentConnection
      task={{
        ...baseTask,
        status: "paused",
        connection_recovery: {
          state: "disconnected",
          retry_count: 0,
          max_retries: 0,
          diagnostics: { network_reason: "context_overflow", http_status: 400 },
        },
      }}
      modelName="DeepSeek"
      busy={false}
      enabled
      onAction={onAction}
    />,
  );
  expect(screen.getByText(/上下文/)).toBeVisible();
});

it("T10: explains agent_model_request_rejected and agent_model_content_filtered in rynAgentErrors", () => {
  expect(KNOWN_AGENT_ERROR_CODES).toContain("agent_model_request_rejected");
  expect(KNOWN_AGENT_ERROR_CODES).toContain("agent_model_content_filtered");

  const rejectedZh = agentErrorHelp("agent_model_request_rejected", true);
  const rejectedEn = agentErrorHelp("agent_model_request_rejected", false);
  expect(rejectedZh.text).not.toContain("agent_model_request_rejected");
  expect(rejectedEn.text).not.toContain("agent_model_request_rejected");
  expect(rejectedZh.actions.length).toBeGreaterThan(0);

  const filteredZh = agentErrorHelp("agent_model_content_filtered", true);
  const filteredEn = agentErrorHelp("agent_model_content_filtered", false);
  expect(filteredZh.text).not.toContain("agent_model_content_filtered");
  expect(filteredEn.text).not.toContain("agent_model_content_filtered");
  expect(filteredZh.actions.length).toBeGreaterThan(0);
});
