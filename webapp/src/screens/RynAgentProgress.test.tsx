import { act, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { AgentEvent, AgentTask } from "../domain/rynAgent";
import { groupAgentMessages, RynAgentTurnDuration } from "./RynAgentProgress";
import { agentTurnTiming } from "./rynAgentTiming";
import i18n from "../i18n";
import "./rynAgentText";

function event(seq: number, type: string, at: number, extra: Record<string, unknown> = {}): AgentEvent {
  return { seq, type, at: new Date(Date.UTC(2026, 9, 17) + at * 1000).toISOString(), ...extra };
}
function task(extra: Partial<AgentTask> = {}): AgentTask {
  return { id: "timed-task", title: "Inspect", model_id: "test", status: "running", revision: 1, created_at: 1, updated_at: 1,
    pending_approval: null, steps: 1, result: "", error: null, limits: {}, messages: [{ role: "user", content: "Inspect" }],
    events: [event(1, "created", 0, { run_elapsed_seconds: 0 })], ...extra };
}
afterEach(() => vi.useRealTimers());

it("keeps timing associated with each user turn when tool messages are grouped", () => {
  const tool = { role: "assistant", content: null, tool_calls: [{ id: "a", function: { name: "inspect", arguments: "{}" } }] };
  const entries = groupAgentMessages([{ role: "user", content: "First" }, tool, { ...tool }, { role: "assistant", content: "Done" }, { role: "user", content: "Next" }, tool]);
  expect(entries.map(entry => [entry.kind, entry.turnIndex])).toEqual([["message", 0], ["progress", 0], ["message", 0], ["message", 1], ["progress", 1]]);
});

it("keeps each public update before its operations while coalescing tool-only messages", () => {
  const tool = { role: "assistant", content: null, tool_calls: [{ id: "a", function: { name: "inspect", arguments: "{}" } }] };
  const entries = groupAgentMessages([
    { role: "user", content: "Make an Excel template" },
    { ...tool, content: "I will create the sales template." }, tool,
    { ...tool, content: "The template is ready; I am checking the formulas." }, tool,
    { role: "assistant", content: "Created and checked the workbook." },
  ]);
  expect(entries.map(entry => entry.kind === "message" ? entry.message.content : entry.messages.length)).toEqual([
    "Make an Excel template", "I will create the sales template.", 2,
    "The template is ready; I am checking the formulas.", 2, "Created and checked the workbook.",
  ]);
  expect(entries.every(entry => entry.turnIndex === 0)).toBe(true);
});

it("retains exact historical durations after the next execution resets its clock", () => {
  const record = task({ status: "completed", messages: [{ role: "user", content: "First" }, { role: "assistant", content: "Done" }, { role: "user", content: "Next" }],
    events: [event(1, "created", 0, { run_elapsed_seconds: 0 }), event(2, "model_started", 1, { run_elapsed_seconds: 0 }),
      event(3, "assistant", 13, { run_elapsed_seconds: 12.2 }), event(4, "completed", 13, { run_elapsed_seconds: 12.2 }),
      event(5, "information_added", 100, { run_elapsed_seconds: 0 }), event(6, "model_started", 101, { run_elapsed_seconds: 0 }),
      event(7, "assistant", 104, { run_elapsed_seconds: 3 }), event(8, "completed", 104, { run_elapsed_seconds: 3 })],
    run_timing: { elapsed_seconds: 3, recorded_seconds: 3, max_seconds: 600, measurement: "recorded" } });
  expect(agentTurnTiming(record, 0)).toMatchObject({ seconds: 12.2, approximate: false, state: "ended" });
  expect(agentTurnTiming(record, 1)).toMatchObject({ seconds: 3, approximate: false, state: "ended" });
  render(<><RynAgentTurnDuration task={record} turnIndex={0} /><RynAgentTurnDuration task={record} turnIndex={1} /></>);
  expect(screen.getAllByRole("timer").map(item => item.textContent)).toEqual(["Worked for 12s", "Worked for 3s"]);
});

it("splits a continued execution at an input answer without counting the user's wait", () => {
  const record = task({ messages: [{ role: "user", content: "Inspect" }, { role: "user", content: "A", ui: { kind: "input_answer" } }],
    events: [event(1, "created", 0, { run_elapsed_seconds: 0 }), event(2, "model_started", 1, { run_elapsed_seconds: 0 }),
      event(3, "assistant", 5, { run_elapsed_seconds: 4 }), event(4, "input_requested", 5, { run_elapsed_seconds: 4 }),
      event(5, "input_answered", 3605, { run_elapsed_seconds: 4 }), event(6, "model_started", 3606, { run_elapsed_seconds: 4 })],
    run_timing: { elapsed_seconds: 9, recorded_seconds: 4, max_seconds: 600, measurement: "live" } });
  expect(agentTurnTiming(record, 0)).toMatchObject({ seconds: 4, live: false });
  expect(agentTurnTiming(record, 1)).toMatchObject({ seconds: 5, live: true });
});

it("keeps timings on the retained conversation after editing an earlier turn", () => {
  const record = task({ messages: [{ role: "user", content: "First" }, { role: "user", content: "Edited second" }],
    events: [event(1, "created", 0, { run_elapsed_seconds: 0 }), event(2, "model_started", 1, { run_elapsed_seconds: 0 }),
      event(3, "assistant", 5, { run_elapsed_seconds: 4 }), event(4, "completed", 5, { run_elapsed_seconds: 4 }),
      event(5, "information_added", 10, { run_elapsed_seconds: 0 }), event(6, "model_started", 11, { run_elapsed_seconds: 0 }),
      event(7, "assistant", 25, { run_elapsed_seconds: 14 }), event(8, "completed", 25, { run_elapsed_seconds: 14 }),
      event(9, "information_added", 30, { run_elapsed_seconds: 0 }), event(10, "completed", 45, { run_elapsed_seconds: 15 }),
      event(11, "message_edited", 50, { user_turn_index: 1, run_elapsed_seconds: 0 }), event(12, "model_started", 51, { run_elapsed_seconds: 0 })],
    run_timing: { elapsed_seconds: 3, recorded_seconds: 0, max_seconds: 600, measurement: "live" } });
  expect(agentTurnTiming(record, 0)).toMatchObject({ seconds: 4, state: "ended" });
  expect(agentTurnTiming(record, 1)).toMatchObject({ seconds: 3, state: "running", live: true });
});

it("retains settled in-flight pause time after the next question resets the run clock", () => {
  const record = task({ status: "completed", messages: [{ role: "user", content: "First" }, { role: "user", content: "Next" }],
    events: [event(1, "created", 0, { run_elapsed_seconds: 0 }), event(2, "model_started", 1, { run_elapsed_seconds: 0 }),
      event(3, "paused", 3, { run_elapsed_seconds: 0, operation_in_flight: true }),
      event(4, "run_settled", 6, { run_elapsed_seconds: 5, operation_in_flight: false, reason: "pause" }),
      event(5, "message_added", 100, { run_elapsed_seconds: 0 }), event(6, "model_started", 101, { run_elapsed_seconds: 0 }),
      event(7, "assistant", 104, { run_elapsed_seconds: 3 }), event(8, "completed", 104, { run_elapsed_seconds: 3 })],
    run_timing: { elapsed_seconds: 3, recorded_seconds: 3, max_seconds: 600, measurement: "recorded" } });
  expect(agentTurnTiming(record, 0)).toMatchObject({ seconds: 5, approximate: false, state: "ended" });
  expect(agentTurnTiming(record, 1)).toMatchObject({ seconds: 3, approximate: false, state: "ended" });
});

it("marks old activity as approximate and excludes approval waits and overlapping tool time", () => {
  const record = task({ status: "completed", events: [event(1, "created", 0), event(2, "model_started", 1), event(3, "assistant", 5),
    event(4, "approval_requested", 5), event(5, "approved", 605), event(6, "tool_started", 606, { call_id: "a" }),
    event(7, "tool_started", 608, { call_id: "b" }), event(8, "tool_result", 616, { call_id: "a" }),
    event(9, "tool_result", 618, { call_id: "b" }), event(10, "completed", 618)] });
  expect(agentTurnTiming(record, 0)).toMatchObject({ seconds: 16, approximate: true, live: false });
  render(<RynAgentTurnDuration task={record} turnIndex={0} />);
  expect(screen.getByRole("timer")).toHaveTextContent("Worked for about 16s");
});

it("does not attribute a current cumulative clock to an unidentifiable historical turn", () => {
  const record = task({ messages: [{ role: "user", content: "First" }, { role: "user", content: "Next" }], events: [],
    run_timing: { elapsed_seconds: 90, recorded_seconds: 90, max_seconds: 600, measurement: "recorded" } });
  expect(agentTurnTiming(record, 0).seconds).toBeNull();
  expect(agentTurnTiming(record, 1).seconds).toBeNull();
  render(<RynAgentTurnDuration task={record} turnIndex={0} />);
  expect(screen.getByRole("timer")).toHaveTextContent("Runtime unavailable");
});

it("does not call an unsettled in-flight boundary an exact zero-second turn", () => {
  const record = task({ messages: [{ role: "user", content: "Inspect" }, { role: "user", content: "Also inspect B" }],
    events: [event(1, "created", 0, { run_elapsed_seconds: 0 }), event(2, "model_started", 1, { run_elapsed_seconds: 0 }),
      event(3, "text_delta", 9, { run_elapsed_seconds: 0, text: "Inspecting" }),
      event(4, "information_added", 10, { run_elapsed_seconds: 0 })],
    run_timing: { elapsed_seconds: 10, recorded_seconds: 0, max_seconds: 600, measurement: "live" } });
  expect(agentTurnTiming(record, 0).seconds).toBeNull();
  expect(agentTurnTiming(record, 1).seconds).toBeNull();
});

it("ticks before the first token, caps stale interpolation, and stops immediately on pause", () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date", "performance"] });
  const running = task({ run_timing: { elapsed_seconds: 1, recorded_seconds: 0, max_seconds: 600, measurement: "live" } });
  const view = render(<RynAgentTurnDuration task={running} turnIndex={0} />);
  expect(screen.getByRole("timer")).toHaveTextContent("Running · 1s");
  expect(screen.getByRole("timer")).toHaveAttribute("aria-live", "off");
  act(() => vi.advanceTimersByTime(2000));
  expect(screen.getByRole("timer")).toHaveTextContent("Running · 3s");
  act(() => vi.advanceTimersByTime(10000));
  expect(screen.getByRole("timer")).toHaveTextContent("Running · 4s");
  view.rerender(<RynAgentTurnDuration task={{ ...running, status: "paused", run_timing: { ...running.run_timing!, elapsed_seconds: 3, recorded_seconds: 3, measurement: "recorded" } }} turnIndex={0} />);
  act(() => vi.advanceTimersByTime(60000));
  expect(screen.getByRole("timer")).toHaveTextContent("Ran for 3s · paused");
});

it("freezes waiting snapshots and formats long completed durations in Chinese", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date", "performance"] });
  const waiting = task({ status: "waiting_approval", run_timing: { elapsed_seconds: 65, recorded_seconds: 65, max_seconds: 600, measurement: "recorded" } });
  const view = render(<RynAgentTurnDuration task={waiting} turnIndex={0} />);
  act(() => vi.advanceTimersByTime(60000));
  expect(screen.getByRole("timer")).toHaveTextContent("Ran for 1m 5s · awaiting approval");
  await act(() => i18n.changeLanguage("zh-CN"));
  view.rerender(<RynAgentTurnDuration task={{ ...waiting, status: "completed", run_timing: { ...waiting.run_timing!, elapsed_seconds: 3661, recorded_seconds: 3661 } }} turnIndex={0} />);
  expect(screen.getByRole("timer")).toHaveTextContent("用时 1 小时 1 分 1 秒");
});
