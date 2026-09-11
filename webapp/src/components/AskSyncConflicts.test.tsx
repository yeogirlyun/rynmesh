import { webcrypto } from "node:crypto";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { askHistory, AskRequestError, recoveryConversationId, type AskSyncConflict } from "../domain/askHistory";
import AskSyncConflicts from "./AskSyncConflicts";

const row = {
  id: "original", title: "Private question", serviceKey: "original-provider::original-model", serviceName: "Original model",
  providerPeerId: "original-provider", networkId: "network", createdAt: "2026-09-11T00:00:00Z", updatedAt: "2026-09-11T00:00:00Z",
  messages: [{ id: "answer", role: "assistant" as const, content: "An answer retained on device B", status: "complete" as const, createdAt: "2026-09-11T00:00:00Z" }],
};
const issue: AskSyncConflict = { id: row.id, revision: "a".repeat(64), conflict: true, deleted: true, erased: false, deferred: false,
  common_messages: [], branches: [], recovery: [{ choice_id: "b".repeat(64) + ":2", value: row }] };

beforeEach(() => {
  vi.stubGlobal("crypto", webcrypto);
  vi.spyOn(askHistory, "syncConflicts").mockResolvedValue([issue]);
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("shows the original recipient and keeps recovery under a separate stable identity", async () => {
  const restored = vi.fn();
  const restore = vi.spyOn(askHistory, "restoreBranch").mockImplementation(async (_id, _choice, newId) => ({ ...row, id: newId }));
  const begin = vi.spyOn(askHistory, "beginRun");
  render(<AskSyncConflicts onRestored={restored} />);
  expect(await screen.findByText("Deleted conversation with pending recovery")).toBeInTheDocument();
  expect(screen.getByText(/Original recipient: original-provider/)).toBeInTheDocument();
  expect(screen.queryByText(row.messages[0].content)).not.toBeInTheDocument();
  const user = userEvent.setup();
  const read = screen.getByRole("button", { name: "Read branch 1" });
  read.focus(); await user.keyboard("{Enter}");
  expect(read).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByText(row.messages[0].content)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Keep branch 1 as a separate conversation" }));
  await waitFor(() => expect(restored).toHaveBeenCalledOnce());
  const expectedId = await recoveryConversationId(issue, issue.recovery[0]);
  expect(restore).toHaveBeenCalledWith(issue.id, issue.recovery[0].choice_id, expectedId, issue.revision);
  expect(expectedId).not.toBe(row.id);
  expect(restored.mock.calls[0][0].serviceKey).toBe(row.serviceKey);
  expect(begin).not.toHaveBeenCalled();
});

it("retries the same copy after response loss and after remount without claiming success", async () => {
  const restored = vi.fn();
  const restore = vi.spyOn(askHistory, "restoreBranch").mockRejectedValueOnce(new Error("Connection interrupted; result unconfirmed."))
    .mockImplementation(async (_id, _choice, newId) => ({ ...row, id: newId }));
  const first = render(<AskSyncConflicts onRestored={restored} />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Keep branch 1 as a separate conversation" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("result unconfirmed");
  expect(restored).not.toHaveBeenCalled();
  const identity = restore.mock.calls[0][2];
  first.unmount();
  render(<AskSyncConflicts onRestored={restored} />);
  await user.click(await screen.findByRole("button", { name: "Keep branch 1 as a separate conversation" }));
  await waitFor(() => expect(restored).toHaveBeenCalledOnce());
  expect(restore.mock.calls[1][2]).toBe(identity);
});

it("keeps both continuations separate and refreshes a stale reviewed revision", async () => {
  const branches = [{ ...issue.recovery[0] }, { choice_id: "c".repeat(64) + ":3", value: { ...row, messages: [{ ...row.messages[0], content: "Different context on device C" }] } }];
  vi.mocked(askHistory.syncConflicts).mockResolvedValue([{ ...issue, deleted: false, branches, recovery: [] }]);
  const restored = vi.fn();
  vi.spyOn(askHistory, "restoreBranch").mockRejectedValue(new AskRequestError(409, "Branches changed. Refresh and review."));
  render(<AskSyncConflicts onRestored={restored} />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Read branch 1" }));
  expect(screen.getByText(row.messages[0].content)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Read branch 2" }));
  expect(screen.queryByText(row.messages[0].content)).not.toBeInTheDocument();
  expect(screen.getByText("Different context on device C")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Keep branch 2 as a separate conversation" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Refresh and review");
  expect(restored).not.toHaveBeenCalled();
  vi.mocked(askHistory.syncConflicts).mockResolvedValue([]);
  await user.click(screen.getByRole("button", { name: "Refresh branches" }));
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  expect(screen.queryByText("Different context on device C")).not.toBeInTheDocument();
});

it("offers retry after loading fails and accurately describes a pending task", async () => {
  vi.mocked(askHistory.syncConflicts).mockRejectedValueOnce(new Error("Reconnect to load branches."))
    .mockResolvedValue([{ ...issue, conflict: false, deferred: true, recovery: [] }]);
  render(<AskSyncConflicts onRestored={vi.fn()} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Reconnect");
  await userEvent.click(screen.getByRole("button", { name: "Refresh branches" }));
  expect(await screen.findByText(/outcome has not been confirmed/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Keep branch/ })).not.toBeInTheDocument();
});

it("keeps an unsent local draft separate and never sends it to a model", async () => {
  const localDraft = { ...row, draft: "This unfinished question stayed on this device." };
  vi.mocked(askHistory.syncConflicts).mockResolvedValue([{ ...issue, conflict: false, recovery: [], local_draft: localDraft }]);
  const restored = vi.fn();
  const begin = vi.spyOn(askHistory, "beginRun");
  const restore = vi.spyOn(askHistory, "restoreBranch").mockImplementation(async (_id, _choice, newId) => ({ ...localDraft, id: newId }));
  render(<AskSyncConflicts onRestored={restored} />);
  expect(await screen.findByLabelText("Recovered unsent draft")).toHaveValue(localDraft.draft);
  await userEvent.click(screen.getByRole("button", { name: "Keep draft as a separate conversation" }));
  await waitFor(() => expect(restored).toHaveBeenCalledOnce());
  expect(restore.mock.calls[0][1]).toBe("local-draft");
  expect(restored.mock.calls[0][0].draft).toBe(localDraft.draft);
  expect(begin).not.toHaveBeenCalled();
});
