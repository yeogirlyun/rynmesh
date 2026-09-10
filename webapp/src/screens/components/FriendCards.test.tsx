import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { friendsApi } from "../../domain/friendsClient";
import FriendCards from "./FriendCards";

vi.mock("../../appContext", () => ({ useAppContext: () => ({}) }));
afterEach(() => vi.restoreAllMocks());

it("shows card metadata without fetching private bytes and recovers a denied download", async () => {
  vi.spyOn(friendsApi, "cards").mockResolvedValue({ cards: [{ card_id: "card", from: "Alice", dir: "in", created_at: "", fetch_state: "available",
    card: { library_id: "doc", title: "Private reading", summary: "A metadata summary", source: "Original publisher", source_url: "https://example.test/article", kind: "document", fetch_available: true, size_bytes: 42 } }] });
  const download = vi.spyOn(friendsApi, "fetchCard").mockRejectedValue(new Error("Friend access was removed."));
  const user = userEvent.setup();
  render(<FriendCards />);
  await screen.findByRole("heading", { name: "Private reading" });
  expect(download).not.toHaveBeenCalled();
  expect(screen.getByText("Source: https://example.test/article")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Download and read (42 bytes)" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("access was removed");
  expect(screen.queryByRole("button", { name: "Read saved copy" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Download and read (42 bytes)" })).toBeEnabled();
});
