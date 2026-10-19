import { act, renderHook } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { AgentTask, AgentTaskSummary } from "../domain/rynAgent";
import { nodeControlUrl } from "../domain/nodeUrl";
import { useAgentTaskAttention } from "./useAgentTaskAttention";

vi.mock("../domain/nodeUrl", () => ({ nodeControlUrl: vi.fn(() => "/api/local/agent") }));

type Summary = AgentTaskSummary & { completion_event_seq?: number };
const task = (overrides: Partial<Summary> = {}): Summary => ({ id: "task-a", title: "Private task title", model_id: "model-a", status: "running", revision: 1, control_revision: 1, created_at: 1, updated_at: 1, pending_approval: null, steps: 1, result: "Private result", error: null, completion_event_seq: 0, ...overrides });
const detail = (summary: Summary): AgentTask => ({ ...summary, messages: [{ role: "assistant", content: "Private message" }], events: [], limits: {} });
const completed = (overrides: Partial<Summary> = {}) => task({ status: "completed", run_state: "ended", revision: 2, control_revision: 2, completion_event_seq: 8, ...overrides });
interface Options { tasks: Summary[]; selected: string; openedTask: AgentTask | null; visible: boolean; ready: boolean; }
const options = (overrides: Partial<Options> = {}): Options => ({ tasks: [], selected: "", openedTask: null, visible: false, ready: true, ...overrides });
const start = (initialProps: Options = options()) => renderHook(props => useAgentTaskAttention(props), { initialProps });

beforeEach(() => { localStorage.clear(); vi.mocked(nodeControlUrl).mockReturnValue("/api/local/agent"); });

it("establishes a quiet baseline for completed historical tasks after the initial list loads", () => {
  const first = completed(), second = completed({ id: "task-b" });
  const hook = start(options({ ready: false }));
  hook.rerender(options({ tasks: [first, second], ready: true }));
  expect(hook.result.current.isUnread(first)).toBe(false);
  expect(hook.result.current.isUnread(second)).toBe(false);
  expect(localStorage.getItem("ryn.agent.attention.v1:/api/local/agent")).not.toMatch(/Private|model-a|messages|result|title/);
});

it("notifies when an observed queued or running task completes in the background", () => {
  const hook = start(options({ tasks: [task({ status: "queued" })] }));
  hook.rerender(options({ tasks: [task({ revision: 2 })] }));
  const finished = completed({ revision: 3 });
  hook.rerender(options({ tasks: [finished] }));
  expect(hook.result.current.isUnread(finished)).toBe(true);
});

it("notifies for a new completed task after a successfully loaded empty account", () => {
  const hook = start();
  const finished = completed();
  hook.rerender(options({ tasks: [finished] }));
  expect(hook.result.current.isUnread(finished)).toBe(true);
});

it("does not mistake failed, waiting or verified-outcome text for a finished turn", () => {
  const hook = start(options({ tasks: [task()] }));
  for (const [index, status] of (["waiting_approval", "waiting_input", "failed"] as const).entries()) {
    const next = task({ status, revision: 2 + index, completion_event_seq: 0, result: "Task completed successfully" });
    hook.rerender(options({ tasks: [next] }));
    expect(hook.result.current.isUnread(next)).toBe(false);
  }
});

it("keeps the dot until the matching successfully loaded detail is displayed", () => {
  const hook = start(options({ tasks: [task()] }));
  const finished = completed();
  hook.rerender(options({ tasks: [finished], selected: finished.id, visible: true }));
  act(() => hook.result.current.markRead(finished.id));
  expect(hook.result.current.isUnread(finished)).toBe(true);
  hook.rerender(options({ tasks: [finished], selected: finished.id, openedTask: detail(completed({ id: "other" })), visible: true }));
  expect(hook.result.current.isUnread(finished)).toBe(true);
  hook.rerender(options({ tasks: [finished], selected: finished.id, openedTask: detail(finished), visible: true }));
  expect(hook.result.current.isUnread(finished)).toBe(false);
});

it("does not acknowledge a stale detail that predates the completed reply", () => {
  const first = completed(), hook = start(options({ tasks: [first] }));
  const second = completed({ revision: 8, completion_event_seq: 29 });
  hook.rerender(options({ tasks: [second], selected: second.id, openedTask: detail(first), visible: true }));
  expect(hook.result.current.isUnread(second)).toBe(true);
  hook.rerender(options({ tasks: [second], selected: second.id, openedTask: detail(second), visible: true }));
  expect(hook.result.current.isUnread(second)).toBe(false);
});

it("can acknowledge the same completion from a detail that predates a metadata-only revision", () => {
  const hook = start();
  const finished = completed({ revision: 10 });
  hook.rerender(options({ tasks: [finished] }));
  expect(hook.result.current.isUnread(finished)).toBe(true);
  hook.rerender(options({ tasks: [finished], selected: finished.id, openedTask: detail(completed({ revision: 9 })), visible: true }));
  expect(hook.result.current.isUnread(finished)).toBe(false);
});

it("notifies for the selected task while the app is hidden and reads it on return", () => {
  const active = task(), hook = start(options({ tasks: [active], selected: active.id, openedTask: detail(active), visible: true }));
  const finished = completed();
  hook.rerender(options({ tasks: [finished], selected: finished.id, openedTask: detail(finished), visible: false }));
  act(() => hook.result.current.markRead(finished.id));
  expect(hook.result.current.isUnread(finished)).toBe(true);
  hook.rerender(options({ tasks: [finished], selected: finished.id, openedTask: detail(finished), visible: true }));
  expect(hook.result.current.isUnread(finished)).toBe(false);
});

it("considers a completion already read when it arrives in the visible open detail", () => {
  const active = task(), hook = start(options({ tasks: [active], selected: active.id, openedTask: detail(active), visible: true }));
  const finished = completed();
  hook.rerender(options({ tasks: [finished], selected: finished.id, openedTask: detail(finished), visible: true }));
  expect(hook.result.current.isUnread(finished)).toBe(false);
});

it("keeps an unread completion through duplicate polls, renaming and archive metadata", () => {
  const hook = start(options({ tasks: [task()] })), finished = completed();
  hook.rerender(options({ tasks: [finished] }));
  hook.rerender(options({ tasks: [{ ...finished }] }));
  hook.rerender(options({ tasks: [{ ...finished, title: "Renamed", revision: 3 }] }));
  hook.rerender(options({ tasks: [{ ...finished, revision: 4, control_revision: 3, archived: true }] }));
  expect(hook.result.current.isUnread(finished)).toBe(true);
});

it("does not relight a read dot for rename, archive, goal or control changes without a new completion event", () => {
  const finished = completed(), hook = start(options({ tasks: [finished] }));
  hook.rerender(options({ tasks: [completed({ revision: 8, control_revision: 8, title: "Changed metadata", archived: true })] }));
  expect(hook.result.current.isUnread(finished)).toBe(false);
});

it("tracks a second completed turn even when polling skipped its running state", () => {
  const finished = completed(), hook = start(options({ tasks: [finished] }));
  const second = completed({ revision: 10, control_revision: 10, completion_event_seq: 32 });
  hook.rerender(options({ tasks: [second] }));
  expect(hook.result.current.isUnread(second)).toBe(true);
  hook.rerender(options({ tasks: [second], selected: second.id, openedTask: detail(second), visible: true }));
  const third = completed({ revision: 20, control_revision: 20, completion_event_seq: 64 });
  hook.rerender(options({ tasks: [third] }));
  expect(hook.result.current.isUnread(third)).toBe(true);
});

it("rejects stale running or completed snapshots without losing unread or read progress", () => {
  const hook = start(options({ tasks: [task()] })), finished = completed({ revision: 10, completion_event_seq: 32 });
  hook.rerender(options({ tasks: [finished] }));
  hook.rerender(options({ tasks: [task({ revision: 8 })] }));
  hook.rerender(options({ tasks: [completed({ revision: 9, completion_event_seq: 16 })] }));
  expect(hook.result.current.isUnread(finished)).toBe(true);
  hook.rerender(options({ tasks: [finished], selected: finished.id, openedTask: detail(finished), visible: true }));
  hook.rerender(options({ tasks: [completed({ revision: 11, completion_event_seq: 16 })] }));
  expect(hook.result.current.isUnread(finished)).toBe(false);
  hook.rerender(options({ tasks: [completed({ revision: 12, completion_event_seq: 33 })] }));
  expect(hook.result.current.isUnread(finished)).toBe(true);
});

it("persists unread and read markers across reloads and notices offline replies", () => {
  const active = task(), first = start(options({ tasks: [active] }));
  first.unmount();
  const finished = completed(), second = start(options({ tasks: [finished] }));
  expect(second.result.current.isUnread(finished)).toBe(true);
  second.unmount();
  const third = start(options({ tasks: [finished] }));
  expect(third.result.current.isUnread(finished)).toBe(true);
  third.rerender(options({ tasks: [finished], selected: finished.id, openedTask: detail(finished), visible: true }));
  third.unmount();
  const fourth = start(options({ tasks: [finished] }));
  expect(fourth.result.current.isUnread(finished)).toBe(false);
  fourth.unmount();
  const offlineReply = completed({ revision: 20, completion_event_seq: 64 }), fifth = start(options({ tasks: [offlineReply] }));
  expect(fifth.result.current.isUnread(offlineReply)).toBe(true);
});

it("retains observations when a list is temporarily empty or tasks are absent", () => {
  const hook = start(options({ tasks: [task()] }));
  hook.rerender(options());
  hook.rerender(options({ tasks: [completed()] }));
  expect(hook.result.current.isUnread(completed())).toBe(true);
  hook.rerender(options());
  hook.rerender(options({ tasks: [completed()] }));
  expect(hook.result.current.isUnread(completed())).toBe(true);
  act(() => hook.result.current.forget("task-a"));
  expect(hook.result.current.isUnread(completed())).toBe(false);
});

it("isolates read history by local node control URL", () => {
  const hook = start(options({ tasks: [task()] }));
  hook.rerender(options({ tasks: [completed()] }));
  expect(hook.result.current.isUnread(completed())).toBe(true);
  vi.mocked(nodeControlUrl).mockReturnValue("http://127.0.0.1:9999/api/local/agent");
  hook.rerender(options({ tasks: [completed()] }));
  expect(hook.result.current.isUnread(completed())).toBe(false);
  vi.mocked(nodeControlUrl).mockReturnValue("/api/local/agent");
  hook.rerender(options({ tasks: [completed()] }));
  expect(hook.result.current.isUnread(completed())).toBe(true);
});

it("falls back to round/control versions for older nodes without completion markers", () => {
  const old = completed({ completion_event_seq: undefined, budget: { round: 1 } as Summary["budget"] });
  const hook = start(options({ tasks: [old] }));
  hook.rerender(options({ tasks: [{ ...old, revision: 3, control_revision: 3, title: "Rename or archive" }] }));
  expect(hook.result.current.isUnread(old)).toBe(false);
  const next = { ...old, revision: 5, control_revision: 5, budget: { round: 2 } as Summary["budget"] };
  hook.rerender(options({ tasks: [next] }));
  expect(hook.result.current.isUnread(next)).toBe(true);
});

it("uses control revisions for legacy nodes and ignores ordinary rename revisions", () => {
  const old = completed({ completion_event_seq: undefined }), hook = start(options({ tasks: [old] }));
  hook.rerender(options({ tasks: [{ ...old, revision: 3, title: "Renamed" }] }));
  expect(hook.result.current.isUnread(old)).toBe(false);
  hook.rerender(options({ tasks: [{ ...old, revision: 4, control_revision: 3 }] }));
  expect(hook.result.current.isUnread(old)).toBe(true);
});

it("adopts a newly available completion marker at the same cached revision without relighting history", () => {
  const old = completed({ completion_event_seq: undefined }), hook = start(options({ tasks: [old] }));
  hook.rerender(options({ tasks: [completed()] }));
  expect(hook.result.current.isUnread(old)).toBe(false);
  hook.rerender(options({ tasks: [completed({ revision: 3, control_revision: 2, completion_event_seq: 16 })] }));
  expect(hook.result.current.isUnread(old)).toBe(true);
});

it("works in memory when localStorage reads and writes throw", () => {
  const read = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("unavailable"); });
  const write = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("full"); });
  try {
    const hook = start(options({ tasks: [task()] }));
    const finished = completed();
    hook.rerender(options({ tasks: [finished] }));
    expect(hook.result.current.isUnread(finished)).toBe(true);
    hook.rerender(options({ tasks: [finished], selected: finished.id, openedTask: detail(finished), visible: true }));
    expect(hook.result.current.isUnread(finished)).toBe(false);
  } finally { read.mockRestore(); write.mockRestore(); }
});

it("ignores malformed persisted records rather than crashing or inventing unread history", () => {
  localStorage.setItem("ryn.agent.attention.v1:/api/local/agent", JSON.stringify({ version: 1, initialized: false, tasks: [["task-a", { revision: "bad", state: "completed" }], ["other", null]] }));
  const hook = start(options({ tasks: [completed()] }));
  expect(hook.result.current.isUnread(completed())).toBe(false);
});
