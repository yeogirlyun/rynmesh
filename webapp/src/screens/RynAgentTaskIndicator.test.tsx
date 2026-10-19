import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import type { AgentTaskSummary } from "../domain/rynAgent";
import RynAgentTaskIndicator from "./RynAgentTaskIndicator";
import "./rynAgentText";

const task: AgentTaskSummary = { id: "indicator", title: "Report", model_id: "model", status: "completed", revision: 4, created_at: 1, updated_at: 2, pending_approval: null, steps: 1, result: "Done", error: null };
const approval = { id: "approval", tool: "write", arguments: {}, call_id: "call", arguments_sha256: "hash" };
const pendingInput = { id: "question", kind: "information" as const, prompt: "Which folder?", fields: [], goal_revision: 1, control_revision: 1, scope_revision: 1, business_revision: null, created_at: 2 };

it("does not show persistent status text for completed replies that have been read", () => {
  const { container } = render(<RynAgentTaskIndicator task={task} />);
  expect(container).toBeEmptyDOMElement();
  expect(screen.queryByText("Turn finished")).not.toBeInTheDocument();
});

it("shows an accessible completion dot until the reply becomes read", () => {
  const view = render(<RynAgentTaskIndicator task={task} unread />);
  const indicator = screen.getByRole("img", { name: "Unread completed reply" });
  expect(indicator).toHaveAttribute("title", "Unread completed reply");
  expect(indicator).toHaveClass("ra-task-indicator-unread");
  expect(indicator).toHaveTextContent("");
  view.rerender(<RynAgentTaskIndicator task={task} unread={false} />);
  expect(view.container).toBeEmptyDOMElement();
});

it("keeps a required confirmation visible independently of read state and prioritizes it over input", () => {
  const waiting = { ...task, pending_approval: approval, pending_input: pendingInput };
  const view = render(<RynAgentTaskIndicator task={waiting} unread />);
  expect(screen.getByText("Needs confirmation")).toHaveClass("ra-task-indicator-approval");
  expect(screen.queryByRole("img", { name: "Unread completed reply" })).not.toBeInTheDocument();
  expect(screen.queryByText("Awaiting your answer")).not.toBeInTheDocument();
  view.rerender(<RynAgentTaskIndicator task={waiting} unread={false} />);
  expect(screen.getByText("Needs confirmation")).toBeVisible();
});

it("keeps a required answer visible independently of read state", () => {
  const waiting = { ...task, pending_input: pendingInput };
  const view = render(<RynAgentTaskIndicator task={waiting} unread />);
  expect(screen.getByText("Awaiting your answer")).toHaveClass("ra-task-indicator-input");
  expect(screen.queryByRole("img", { name: "Unread completed reply" })).not.toBeInTheDocument();
  view.rerender(<RynAgentTaskIndicator task={waiting} />);
  expect(screen.getByText("Awaiting your answer")).toBeVisible();
});

it.each([
  ["waiting_approval", "Needs confirmation"],
  ["waiting_input", "Awaiting your answer"],
] as const)("recognizes authoritative %s state even without its pending payload", (state, label) => {
  render(<RynAgentTaskIndicator task={{ ...task, status: state }} />);
  expect(screen.getByText(label)).toBeVisible();
});

it.each([
  { ...task, archived: true },
  { ...task, archived: true, pending_approval: approval },
  { ...task, archived: true, pending_input: pendingInput },
] as AgentTaskSummary[])("hides all indicators for archived task %#", archived => {
  const { container } = render(<RynAgentTaskIndicator task={archived} unread />);
  expect(container).toBeEmptyDOMElement();
});

it.each([
  ["running", "Running"],
  ["pausing", "Pausing"],
  ["reconciling", "Checking an unresolved operation"],
] as const)("shows a compact spinner for %s instead of status text", (state, label) => {
  render(<RynAgentTaskIndicator task={{ ...task, run_state: state }} />);
  const indicator = screen.getByRole("img", { name: label });
  expect(indicator).toHaveClass("ra-task-indicator-running");
  expect(indicator.querySelector("svg")).toHaveClass("ra-task-indicator-spinner");
  expect(indicator).toHaveTextContent("");
});

it.each([
  ["queued", "Queued"],
  ["paused", "Paused"],
  ["failed", "Failed"],
  ["interrupted", "Interrupted"],
] as const)("provides an accessible icon for %s without persistent text", (state, label) => {
  render(<RynAgentTaskIndicator task={{ ...task, status: state }} unread />);
  const indicator = screen.getByRole("img", { name: label });
  expect(indicator).toHaveAttribute("title", label);
  expect(indicator).toHaveClass(`ra-task-indicator-${state}`);
  expect(indicator).toHaveTextContent("");
});

it("does not show a completion dot for cancelled tasks", () => {
  const { container } = render(<RynAgentTaskIndicator task={{ ...task, status: "cancelled" }} unread />);
  expect(container).toBeEmptyDOMElement();
});
