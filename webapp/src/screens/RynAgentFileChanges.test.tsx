import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { agentFileChanges, type ChangeReview } from "../domain/agentFileChanges";
import type { AgentTask } from "../domain/rynAgent";
import { latestFileToolResultSeq } from "./RynAgentArtifacts";
import RynAgentFileChanges from "./RynAgentFileChanges";
import "./rynAgentText";

afterEach(() => {
  vi.restoreAllMocks();
});

const sampleReview: ChangeReview = {
  files: [
    {
      id: "grp-1",
      path: "ok.txt",
      status: "created",
      source: "file_tool",
      can_undo: true,
      before_bytes: 0,
      after_bytes: 12,
      media_type: "text/plain",
      diff: "+hello",
    },
    {
      id: "grp-2",
      path: "over_limit.txt",
      status: "created",
      source: "file_tool",
      can_undo: false,
      problem: "snapshot_skipped_limit",
      before_bytes: 0,
      after_bytes: 64,
      media_type: "text/plain",
    },
  ],
  task_busy: false,
};

describe("S05-I11 RynAgentFileChanges refresh key & degradation UI", () => {
  it("T6: fetches review only once across 10 task.revision changes when changeKey stays unchanged, and renders snapshot_skipped_limit reason", async () => {
    const reviewSpy = vi.spyOn(agentFileChanges, "review").mockResolvedValue(sampleReview);
    const AnyFileChanges = RynAgentFileChanges as unknown as React.ComponentType<Record<string, unknown>>;

    const view = render(<AnyFileChanges taskId="task-1" changeKey={10} revision={1} />);
    fireEvent.click(screen.getByRole("button", { name: /查看文件修改与撤销|Review file changes and undo/ }));

    await waitFor(() => expect(reviewSpy).toHaveBeenCalledTimes(1));

    for (let rev = 2; rev <= 10; rev += 1) {
      const fakeTask = {
        events: [
          { seq: 10, type: "tool_result", tool: "ryn_file_write" },
          { seq: 10 + rev, type: "status", tool: undefined },
        ],
      } as unknown as AgentTask;
      expect(latestFileToolResultSeq(fakeTask)).toBe(10);
      view.rerender(<AnyFileChanges taskId="task-1" changeKey={latestFileToolResultSeq(fakeTask)} revision={rev} />);
    }

    expect(reviewSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/超限的修改照常写入，但只记哈希、不可撤销|Over-limit changes are still written/)).toBeInTheDocument();
    expect(screen.queryByText(/snapshot_skipped_limit/)).not.toBeInTheDocument();
    expect(screen.getByText(/超出快照限额|Snapshot skipped due to limit/)).toBeInTheDocument();
  });

  it("T7: refetches review while expanded when a new file-tool event updates changeKey", async () => {
    const reviewSpy = vi.spyOn(agentFileChanges, "review").mockResolvedValue(sampleReview);
    const AnyFileChanges = RynAgentFileChanges as unknown as React.ComponentType<Record<string, unknown>>;

    const view = render(<AnyFileChanges taskId="task-2" changeKey={3} revision={1} />);
    fireEvent.click(screen.getByRole("button", { name: /查看文件修改与撤销|Review file changes and undo/ }));
    await waitFor(() => expect(reviewSpy).toHaveBeenCalledTimes(1));

    // New file-tool tool_result seq arrives while task.revision also changes -> refetches once.
    view.rerender(<AnyFileChanges taskId="task-2" changeKey={8} revision={1} />);
    await waitFor(() => expect(reviewSpy).toHaveBeenCalledTimes(2));
  });

  it("T8: does not fetch review while collapsed, fetches once upon expanding, and shows real error instead of spinning", async () => {
    const reviewSpy = vi
      .spyOn(agentFileChanges, "review")
      .mockRejectedValueOnce(new Error("HTTP 502"));
    const AnyFileChanges = RynAgentFileChanges as unknown as React.ComponentType<Record<string, unknown>>;

    const view = render(<AnyFileChanges taskId="task-3" changeKey={1} revision={1} />);
    view.rerender(<AnyFileChanges taskId="task-3" changeKey={5} revision={2} />);
    expect(reviewSpy).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /查看文件修改与撤销|Review file changes and undo/ }));
    await waitFor(() => expect(reviewSpy).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole("alert")).toHaveTextContent(/502/);
    expect(screen.queryByText(/读取快照中|Loading snapshots/)).not.toBeInTheDocument();
    expect(screen.getByText(/超限的修改照常写入，但只记哈希、不可撤销|Over-limit changes are still written/)).toBeInTheDocument();
  });
});
