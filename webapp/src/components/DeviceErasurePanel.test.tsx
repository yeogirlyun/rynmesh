import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { deviceErasure, type ErasureStatus } from "../domain/deviceErasure";
import type { DevicePair } from "../domain/deviceSync";
import DeviceErasurePanel from "./DeviceErasurePanel";

const confirm = vi.hoisted(() => vi.fn());
vi.mock("../appContext", () => ({ useAppContext: () => ({ confirm }) }));
const pair = { id: "pair", status: "active", device: { name: "Laptop" } } as DevicePair;
let status: ErasureStatus;
beforeEach(() => {
  confirm.mockReset();
  status = { outgoing: [], incoming: [] };
  vi.spyOn(deviceErasure, "status").mockImplementation(async () => status);
  vi.spyOn(deviceErasure, "action").mockResolvedValue({});
  vi.spyOn(deviceErasure, "preview").mockResolvedValue({ id: "proposal", category: "reading", review_token: "review", counts: { local_items: 3 }, phases: ["source", "replica", "backups", "search"] });
});
const show = () => render(<MemoryRouter><DeviceErasurePanel devices={[pair]} /></MemoryRouter>);

it("requires a nonempty reviewed target set and sends only a proposal", async () => {
  const user = userEvent.setup(); show();
  expect(screen.getByRole("button", { name: "Review cleanup request" })).toBeDisabled();
  await user.click(screen.getByRole("checkbox", { name: "Laptop" }));
  await user.click(screen.getByRole("button", { name: "Review cleanup request" }));
  expect(deviceErasure.action).not.toHaveBeenCalled();
  const review = confirm.mock.calls[0][0];
  expect(review.body).toContain("ALL records in that category");
  expect(review.body).toContain("proposal only");
  await act(async () => review.onConfirm());
  expect(deviceErasure.action).toHaveBeenCalledWith(expect.objectContaining({ action: "begin", category: "reading", pair_ids: ["pair"] }));
});

it("requires local count review and separate destructive confirmation on the receiver", async () => {
  status.incoming = [{ id: "proposal", category: "reading", name: "Desktop", state: "awaiting_owner", available: true }];
  const user = userEvent.setup(); show();
  expect(deviceErasure.preview).not.toHaveBeenCalled();
  await user.click(await screen.findByRole("button", { name: "Review this computer's copies" }));
  const preview = await screen.findByRole("region", { name: "Local cleanup review" });
  expect(within(preview).getByText("local items: 3")).toBeInTheDocument();
  expect(deviceErasure.action).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Confirm local cleanup" }));
  expect(deviceErasure.action).not.toHaveBeenCalled();
  expect(confirm.mock.calls[0][0].body).toContain("Browser copies and exports");
  await act(async () => confirm.mock.calls[0][0].onConfirm());
  expect(deviceErasure.action).toHaveBeenCalledWith({ action: "approve", id: "proposal", review_token: "review" });
});

it("keeps the original offline target visible and does not show aggregate completion", async () => {
  status.outgoing = [{ id: "job", category: "reading", remote_confirmed: false, targets: [
    { pair_id: "one", name: "One", state: "confirmed", completed_at: 1000 },
    { pair_id: "two", name: "Two", state: "unconfirmed" },
  ] }];
  show();
  expect(await screen.findByText("Completion across the selected devices is not currently confirmed.")).toBeInTheDocument();
  expect(screen.getByText(/Two: Device unavailable/)).toBeInTheDocument();
  expect(screen.queryByText(/All originally selected devices returned/)).not.toBeInTheDocument();
});

it("offers only the previously approved job after partial cleanup", async () => {
  status.incoming = [{ id: "proposal", category: "reading", name: "Desktop", state: "partial", available: true }];
  const user = userEvent.setup(); show();
  await user.click(await screen.findByRole("button", { name: "Continue approved cleanup" }));
  expect(deviceErasure.action).toHaveBeenCalledWith({ action: "resume", id: "proposal" });
  expect(deviceErasure.preview).not.toHaveBeenCalled();
});
