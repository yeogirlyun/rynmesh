import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../domain/friendWeekly";
import FriendWeeklyRecap from "./FriendWeeklyRecap";

const week: api.FriendWeek = { timezone: "UTC", week_start: "2026-09-14T00:00:00+00:00", as_of: "2026-09-17T12:00:00+00:00", incomplete: false, available_count: 1,
  items: [{ relationship_id: "friend", peer_id: "peer", node_name: "Alice", publication_id: "story", revision: 2, published_at: 1789380000, title: "Private reading", checked_at: 1789380010, access_unconfirmed: false }] };
beforeEach(() => { vi.spyOn(api, "friendWeek").mockResolvedValue(week); });
const show = () => render(<MemoryRouter><FriendWeeklyRecap /></MemoryRouter>);

it("labels the time boundary and keeps private recap out of AI and email", async () => {
  show();
  expect(await screen.findByText("Private reading")).toBeInTheDocument();
  expect(screen.getByText(/Monday 00:00 UTC/)).toHaveTextContent("No friend content is sent to AI");
  expect(screen.getByText(/2026-09-14 · UTC/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Open friend updates" })).toHaveAttribute("href", "/friend-updates");
});

it("hides retained rows when a refresh fails and recovers without calling a peer", async () => {
  let poll!: () => void;
  vi.spyOn(window, "setInterval").mockImplementation((callback, delay) => { if (delay === 5000) poll = callback as () => void; return 1 as unknown as ReturnType<typeof setInterval>; });
  show();
  await screen.findByText("Private reading");
  vi.mocked(api.friendWeek).mockRejectedValueOnce(new Error("unavailable"));
  await act(async () => { poll(); });
  expect(screen.queryByText("Private reading")).not.toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("unavailable");
  vi.mocked(api.friendWeek).mockResolvedValue({ ...week, items: [], available_count: 0 });
  await userEvent.click(screen.getByRole("button", { name: "Refresh friend recap" }));
  expect(await screen.findByText(/No publications received this week/)).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("discloses incomplete pagination and offline access", async () => {
  vi.mocked(api.friendWeek).mockResolvedValue({ ...week, incomplete: true, items: [{ ...week.items[0], access_unconfirmed: true }] });
  show();
  expect(await screen.findByText(/This list may be incomplete/)).toBeInTheDocument();
  expect(screen.getByRole("listitem")).toHaveTextContent("Latest access unconfirmed");
});
