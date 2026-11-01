import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import type { AgentTask } from "../domain/rynAgent";
import "../i18n";
import "./rynAgentText";
import RynAgentOverview from "./RynAgentOverview";

const task: AgentTask = {
  id: "overview", title: "Overview", model_id: "model", status: "running", revision: 1,
  created_at: 1, updated_at: 1, messages: [], events: [], pending_approval: null,
  steps: 1, result: "", error: null, limits: {},
  plan: { revision: 1, goal_revision: 1, steps: [
    { id: "read", text: "Read the project", status: "done", criterion_ids: [], depends_on: [] },
    { id: "check", text: "Verify the change", status: "running", criterion_ids: [], depends_on: [] },
  ] },
};

it("shows the current server step while keeping the full plan collapsed", () => {
  const { container } = render(<RynAgentOverview task={task} />);
  expect(container.querySelector("summary")).toHaveTextContent("Verify the change");
  expect(container.querySelector("summary")).toHaveTextContent("1/2");
  expect(container.querySelector("details")).not.toHaveAttribute("open");
  fireEvent.click(screen.getByLabelText("View task progress"));
  expect(container.querySelector("details")).toHaveAttribute("open");
});

it("keeps waiting for input visible even with a stale running plan", () => {
  const { container } = render(<RynAgentOverview task={{ ...task, run_state: "waiting_input", pending_input: { id: "question", kind: "information", prompt: "Which project?", fields: [], goal_revision: 1, control_revision: 1, scope_revision: 1, business_revision: null, created_at: 1 } }} />);
  expect(container.querySelector("summary")).toHaveTextContent("Awaiting your answer");
  expect(container.querySelector("summary")).not.toHaveTextContent("Verify the change");
  expect(container.querySelector(".ra-overview-state")).toHaveClass("needs-attention");
});

it("does not turn finished plan steps into a success claim for a failed run", () => {
  const { container } = render(<RynAgentOverview task={{ ...task, status: "failed", plan: { ...task.plan!, steps: task.plan!.steps.map(step => ({...step, status: "done"})) } }} />);
  expect(container.querySelector("summary")).toHaveTextContent("Failed");
  expect(container.querySelector("summary")).toHaveTextContent("2/2");
});

it("does not invent plan progress when the server has no plan", () => {
  const { container } = render(<RynAgentOverview task={{ ...task, plan: undefined }} />);
  expect(screen.getByLabelText("Task activity")).toHaveTextContent("Running");
  expect(container.querySelector("details")).toBeNull();
  expect(container.querySelector(".ra-overview-count")).toBeNull();
});
