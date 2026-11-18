import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { agentFileChanges, type ChangeReview } from "../domain/agentFileChanges";
import RynAgentFileChanges from "./RynAgentFileChanges";
import "./rynAgentText";

afterEach(() => vi.restoreAllMocks());

it("S05-I33 T3/T4/T8 shows restorable partial writes, unwritten files and occupied undo", async () => {
  const value = {
    files: [
      { id: "partial", path: "partial.txt", status: "modified", source: "file_tool", can_undo: true,
        before_bytes: 8, after_bytes: 4, media_type: "text/plain", state: "partial_write",
        problem: "partial_write", restore_available: true },
      { id: "pending", path: "pending.txt", status: "modified", source: "file_tool", can_undo: false,
        before_bytes: 8, after_bytes: 9, media_type: "text/plain", state: "not_applied" },
    ],
    undo_operations: [{ id: "undo", status: "blocked_file_in_use", completed: [],
      remaining: [{ path: "office.xlsx", state: "file_in_use" }] }],
  } as ChangeReview;
  vi.spyOn(agentFileChanges, "review").mockResolvedValue(value);
  render(<RynAgentFileChanges taskId="S05-I33" />);
  fireEvent.click(screen.getByRole("button", { name: /查看文件修改与撤销|Review file changes and undo/ }));
  const partial = (await screen.findByText("partial.txt")).closest("article")!;
  expect(within(partial).getByRole("alert")).toHaveTextContent(/写入被中断，文件可能不完整；可从本任务快照还原|Write interrupted.*snapshot/);
  expect(within(partial).getByRole("alert")).not.toHaveTextContent(/不可安全撤销|Cannot safely undo/);
  expect(within(partial).getByRole("button")).toBeEnabled();
  const pending = screen.getByText("pending.txt").closest("article")!;
  expect(within(pending).getAllByText(/未写入|Not written/).length).toBeGreaterThan(0);
  expect(within(pending).queryByText(/确认文件工具修改|Confirmed file-tool change/)).toBeNull();
  expect(within(pending).getByRole("button")).toBeDisabled();
  expect(screen.getByText(/office.xlsx/)).toHaveTextContent(/文件正被其他程序占用|File is open in another program/);
  expect(screen.queryByText(/撤销中断，请检查|Undo interrupted: inspect/)).toBeNull();
});
