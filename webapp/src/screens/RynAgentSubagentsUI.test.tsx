import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import type { AgentSubagentSummary } from "../domain/rynAgent";
import { SubagentComposerBar, SubagentRightDrawer } from "./RynAgentSubagents";

const agent: AgentSubagentSummary = { id: "a", parent_task_id: "root", nickname: "Ada", task_name: "inspect", agent_path: "/root/inspect", role: "explorer", fork_turns: "all", write_scope: [], instructions: "Inspect the project", status: "running", is_waiting: false, closed: false, model_id: "model", reasoning_effort: "medium", steps: 0, elapsed_seconds: 0, progress_percent: 0, current_step_text: "Reading", diff_stats: {added: 0, removed: 0, files: 0}, timeline: [], final_reply: null, notification_payload: null };

it("collapses duplicate member detail but preserves mention, selection and stop callbacks", async () => {
  const mention = vi.fn(), select = vi.fn(), stop = vi.fn();
  const user = userEvent.setup();
  const { container, rerender } = render(<SubagentComposerBar subagents={[]} onSelectSubagent={select} onMentionSubagent={mention} onStopAll={stop} />);
  rerender(<SubagentComposerBar subagents={[agent]} onSelectSubagent={select} onMentionSubagent={mention} onStopAll={stop} />);
  expect(screen.queryByRole("button", {name: "@Ada"})).not.toBeInTheDocument();
  await user.click(container.querySelector<HTMLButtonElement>(".ra-subagent-composer-bar-toggle")!);
  await user.click(screen.getByRole("button", {name: "@Ada"}));
  expect(mention).toHaveBeenCalledWith("Ada");
  await user.click(container.querySelector<HTMLElement>(".ra-subagent-composer-row-left")!);
  expect(select).toHaveBeenCalledWith("a");
  await user.click(container.querySelector<HTMLButtonElement>(".ra-subagent-stop-all-btn")!);
  expect(stop).toHaveBeenCalledTimes(1);
});

it("uses reported progress only and puts waiting members before running members", () => {
  const { container } = render(<SubagentRightDrawer subagents={[agent, {...agent, id:"b", nickname:"Turing", is_waiting:true}]} selectedSubagentId={null} onSelectSubagent={vi.fn()} onClose={vi.fn()} onInterruptSubagent={vi.fn()} onMentionSubagent={vi.fn()} />);
  expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  expect(container.querySelector(".ra-subagent-card-nick")).toHaveTextContent("Turing");
});
