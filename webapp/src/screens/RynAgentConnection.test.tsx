import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { AgentTaskSummary } from "../domain/rynAgent";
import RynAgentConnection from "./RynAgentConnection";

const task: AgentTaskSummary = { id: "task", title: "Inspect", model_id: "deepseek", status: "queued", revision: 2, created_at: 1, updated_at: 2, steps: 1, pending_approval: null, result: "", error: null,
  connection_recovery: { state: "retrying", retry_count: 2, max_retries: 3 } };
const props = { task, modelName: "DeepSeek", busy: false, enabled: true, onAction: vi.fn() };
afterEach(() => { vi.useRealTimers(); vi.clearAllMocks(); });

it("shows inline reconnection and stops through the existing pause control", () => {
  render(<RynAgentConnection {...props} />);
  expect(screen.getByText("Reconnecting to DeepSeek")).toBeVisible();
  expect(screen.getByText("Retry 2 / 3")).toBeVisible();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.queryByText(/Connection details/i)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Stop retrying" }));
  expect(props.onAction).toHaveBeenCalledWith("pause");
});

it("has only one action when disconnected, retains progress and resumes", () => {
  render(<RynAgentConnection {...props} task={{ ...task, status: "paused", connection_recovery: { state: "disconnected", retry_count: 3, max_retries: 3 } }} />);
  expect(screen.getByText("Unable to connect to DeepSeek")).toBeVisible();
  expect(screen.getAllByRole("button")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Retry and continue" }));
  expect(props.onAction).toHaveBeenCalledWith("resume");
});

it("disables pending actions and respects authoritative permission controls", () => {
  const view = render(<RynAgentConnection {...props} busy />);
  const stop = screen.getByRole("button", { name: "Stop retrying" });
  fireEvent.click(stop); fireEvent.click(stop);
  expect(props.onAction).not.toHaveBeenCalled();
  view.rerender(<RynAgentConnection {...props} task={{ ...task, allowed_actions: { pause: { enabled: false, reason: "agent_operation_unresolved", requires_control_revision: true } } }} />);
  expect(screen.getByRole("button")).toBeDisabled();
});

it("shows neutral stopped state instead of a failure", () => {
  render(<RynAgentConnection {...props} task={{ ...task, status: "paused", connection_recovery: { state: "stopped", retry_count: 1, max_retries: 3 } }} />);
  expect(screen.getByText("Reconnection stopped")).toBeVisible();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("briefly shows recovery, then removes the status", () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] }); vi.setSystemTime(100000);
  render(<RynAgentConnection {...props} task={{ ...task, status: "completed", connection_recovery: { state: "recovered", retry_count: 1, max_retries: 3, recovered_at: 100 } }} />);
  expect(screen.getByText("Connection restored")).toBeVisible();
  act(() => { vi.advanceTimersByTime(3001); });
  expect(screen.queryByText("Connection restored")).not.toBeInTheDocument();
});

it.each(["failed", "cancelled", "interrupted"] as const)("does not mask a subsequent %s state", status => {
  const { container } = render(<RynAgentConnection {...props} task={{ ...task, status, error: "agent_time_limit" }} />);
  expect(container).toBeEmptyDOMElement();
});
