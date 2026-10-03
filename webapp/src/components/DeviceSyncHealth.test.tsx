import { render, screen, within } from "@testing-library/react";
import { expect, it } from "vitest";
import type { DevicePair, DeviceStatus } from "../domain/deviceSync";
import DeviceSyncHealth from "./DeviceSyncHealth";

const pair: DevicePair = { id: "laptop", role: "inviter", status: "active", device: { name: "Laptop", actor: "actor", peer_id: "peer", endpoint: "http://localhost" }, review_token: "", verification_code: "", expires: 0, scopes: ["reading"], remote_scopes: ["reading"], effective_scopes: ["reading"], paused: false, remote_paused: false, revision: 0, removal_pending: false,
  sync: { state: "confirmed", pending: 0, last_success_at: 12345, error_code: "", conflicts: 0, rejected_by_peer: {} } };
const status: DeviceStatus = { pairing_available: true, reason: null, data_transfer_available: true, devices: [pair], invites: [], capture_failures: { count: 0, codes: {} }, quarantined: [], quarantined_count: 0 };
const show = (device: DevicePair, extra: Partial<DeviceStatus> = {}) => render(<DeviceSyncHealth status={{ ...status, ...extra, devices: [device] }} stale={false} readAt={12345000} />);

it("shows outgoing acknowledgement without claiming current connectivity", () => {
  show(pair);
  expect(screen.getByText("Selected local changes acknowledged")).toBeInTheDocument();
  expect(screen.getByText(/do not prove another device is online/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Laptop" })).toHaveAttribute("href", "#device-laptop");
});

it.each([
  [{ ...pair, paused: true }, "Paused"],
  [{ ...pair, effective_scopes: [] }, "No shared categories"],
  [{ ...pair, sync: undefined }, "Sync status unavailable"],
  [{ ...pair, sync: { ...pair.sync!, state: "waiting", pending: 8 } }, "Transfer not confirmed · retry connection"],
  [{ ...pair, sync: { ...pair.sync!, conflicts: 1 } }, "Conflicts need review"],
  [{ ...pair, sync: { ...pair.sync!, rejected_by_peer: { reading: 3 } } }, "Rejected records need attention"],
  [{ ...pair, status: "revoked" }, "Device removed"],
] as const)("does not hide incomplete transfers behind an old success timestamp", (device, label) => {
  show(device as DevicePair);
  expect(screen.getByText(label)).toBeInTheDocument();
  expect(screen.queryByText("Selected local changes acknowledged")).not.toBeInTheDocument();
});

it("keeps unknown and truncated counts distinct from zero", () => {
  show({ ...pair, sync: { ...pair.sync!, pending: null, rejected_details_truncated: true } });
  expect(screen.getByText("At least 0")).toBeInTheDocument();
  expect(screen.getByText("Unknown")).toBeInTheDocument();
  expect(screen.getByText("Rejected records · incomplete counts")).toBeInTheDocument();
});

it("marks all retained rows stale and keeps local failures separate", () => {
  render(<DeviceSyncHealth status={{ ...status, capture_failures: { count: 2, codes: {} }, quarantined_count: 3 }} stale readAt={12345000} />);
  expect(screen.getByText("Status out of date")).toBeInTheDocument();
  expect(screen.getByText(/2 changes not queued; 3 records held/)).toBeInTheDocument();
  expect(screen.queryByText("Selected local changes acknowledged")).not.toBeInTheDocument();
});

it("does not present transfer counters as current when transfer is unavailable", () => {
  show(pair, { data_transfer_available: false });
  expect(screen.getByText("Content transfer unavailable")).toBeInTheDocument();
  expect(within(screen.getAllByRole("row")[1]).getAllByText("Unknown")).toHaveLength(2);
});
