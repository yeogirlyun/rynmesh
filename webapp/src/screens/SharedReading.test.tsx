import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { friendsApi } from "../domain/friendsClient";
import { sharedReading, type SharedList } from "../domain/sharedReading";
import SharedReading from "./SharedReading";

const confirm = vi.hoisted(() => vi.fn());
vi.mock("../appContext", () => ({ useAppContext: () => ({ confirm }) }));
const row: SharedList = { id: "list", title: "Together", owner: "alice", friend: "bob", local_peer: "bob", friend_name: "Alice", status: "active", revision: 2,
  items: { article: { id: "article", title: "Shared article", url: "https://example.test", added_by: "alice", read_by: { alice: true }, removed: false } }, pending_count: 0 };
beforeEach(() => {
  confirm.mockReset();
  vi.spyOn(friendsApi, "list").mockResolvedValue({ friends: [{ relationship_id: "relation", node_name: "Alice", status: "active" }] } as never);
  vi.spyOn(sharedReading, "status").mockResolvedValue({ lists: [row] });
  vi.spyOn(sharedReading, "action").mockResolvedValue({});
  vi.spyOn(sharedReading, "discover").mockResolvedValue({ invitations: [{ id: "invite", title: "New list" }] });
});
const show = () => render(<MemoryRouter><SharedReading /></MemoryRouter>);

it("requires explicit membership review and does not discover or join automatically", async () => {
  const user = userEvent.setup(); show();
  await screen.findByText("Together");
  expect(sharedReading.discover).not.toHaveBeenCalled();
  await user.selectOptions(screen.getByLabelText("Friend"), "relation");
  await user.click(screen.getByRole("button", { name: "Check invitations from friend" }));
  await user.click(await screen.findByRole("button", { name: "Review and join" }));
  expect(sharedReading.action).not.toHaveBeenCalled();
  const review = confirm.mock.calls[0][0];
  expect(review.body).toContain("Private reading history is not shared");
  await act(async () => review.onConfirm());
  expect(sharedReading.action).toHaveBeenCalledWith({ action: "accept", relationship_id: "relation", id: "invite" });
});

it("changes only my read status and retains request identity across an uncertain write", async () => {
  vi.mocked(sharedReading.action).mockRejectedValueOnce(new Error("Lost reply"));
  const user = userEvent.setup(); show();
  const card = await screen.findByRole("article", { name: "Shared list Together" });
  expect(within(card).getByText("You: Unread · Friend: Read")).toBeInTheDocument();
  await user.click(within(card).getByRole("button", { name: "Mark read" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Lost reply");
  await user.click(within(card).getByRole("button", { name: "Mark read" }));
  const calls = vi.mocked(sharedReading.action).mock.calls;
  expect(calls[0]).toEqual(calls[1]);
  expect(calls[0][0]).toMatchObject({ edit: "read", value: { id: "article", read: true } });
  expect((calls[0][0] as { value: object }).value).not.toHaveProperty("peer");
  expect(await screen.findByText(/Saved on this node/)).toBeInTheDocument();
});

it("shows queued changes without claiming they reached the friend and disables revoked edits", async () => {
  vi.mocked(sharedReading.status).mockResolvedValue({ lists: [{ ...row, pending_count: 2, blocked: true }] });
  show();
  expect(await screen.findByText(/2 changes awaiting/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Mark read" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Add to shared list" })).toBeDisabled();
});

it("requires review before removing an item for both members", async () => {
  const user = userEvent.setup(); show();
  await user.click(await screen.findByRole("button", { name: "Remove item" }));
  expect(sharedReading.action).not.toHaveBeenCalled();
  expect(confirm.mock.calls[0][0].body).toContain("for both members");
});

it("explains unaccepted offline changes after closure and disables edits", async () => {
  vi.mocked(sharedReading.status).mockResolvedValue({ lists: [{ ...row, status: "closed", cancelled_count: 3 }] });
  show();
  expect(await screen.findByText(/3 queued changes were not applied because this list closed/)).toHaveTextContent("remain saved on this node");
  expect(screen.getByText(/0 changes awaiting/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Mark read" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Close list" })).toBeDisabled();
});
