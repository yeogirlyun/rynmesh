import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { deviceSyncApi } from "../domain/deviceSync";
import type { DevicePair, DeviceStatus } from "../domain/deviceSync";
import Devices from "./Devices";

// Redesign checks for /devices/sync: page root, summary band, tabbed pair panel, scope chips and the empty state.
vi.mock("../appContext", () => ({ useAppContext: () => ({ confirm: vi.fn() }) }));
vi.mock("../components/DeviceErasurePanel", () => ({ default: () => null }));

const identity = (name: string) => ({ name, peer_id: `peer:${name}`, actor: "a".repeat(64), endpoint: "http://192.168.1.2:8791" });
const base: DevicePair = { id: "pair", role: "inviter", status: "active", device: identity("Home PC"), review_token: "review", verification_code: "",
  expires: 4000000000, scopes: ["bookmarks", "reading"], remote_scopes: ["bookmarks"], paused: false, remote_paused: false, revision: 1,
  effective_scopes: ["bookmarks"], removal_pending: false,
  sync: { state: "confirmed", pending: 0, last_success_at: 1000, error_code: "", conflicts: 0, rejected_by_peer: {} } };
let state: DeviceStatus;
const show = () => render(<MemoryRouter><Devices /></MemoryRouter>);

beforeEach(() => {
  state = { pairing_available: true, reason: null, data_transfer_available: true, devices: [], invites: [],
    capture_failures: { count: 0, codes: {} }, quarantined: [], quarantined_count: 0 };
  vi.spyOn(deviceSyncApi, "status").mockImplementation(async () => structuredClone(state));
  vi.spyOn(deviceSyncApi, "readingConflicts").mockResolvedValue({ conflicts: [], local_actor: "local" });
});
afterEach(() => { vi.restoreAllMocks(); });

it("renders the redesigned page root, header and summary band", async () => {
  state.devices = [
    base,
    { ...base, id: "review", status: "awaiting_owner", device: identity("Studio PC"), effective_scopes: [], sync: undefined },
    { ...base, id: "conflict", device: identity("Cloud server"), sync: { ...base.sync!, state: "conflict", pending: 12, conflicts: 2 } },
  ];
  const { container } = show();
  await screen.findByRole("article", { name: "Device Home PC" });
  const root = container.querySelector(".rd-sync");
  expect(root).toHaveClass("rf");
  expect(screen.getByRole("heading", { level: 1, name: "Device sync" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Invite a computer" })).toHaveAttribute("href", "#add-computer");
  const band = container.querySelector(".rf-band")!;
  expect(band.querySelector(".rf-band-num")).toHaveTextContent("2");
  expect(band).toHaveTextContent("1 needs your review");
  expect(band).toHaveTextContent("12");
  expect(screen.getByRole("link", { name: /Review below/ })).toHaveAttribute("href", "#reading-sync-conflicts");
  expect(screen.getByRole("article", { name: "Device Studio PC" })).toHaveClass("review");
  expect(screen.getByText("Wants to pair with this computer")).toBeInTheDocument();
});

it("keeps both pairing panes mounted while the segmented control picks the visible one", async () => {
  const user = userEvent.setup();
  const { container } = show();
  await screen.findByText("No paired devices yet.");
  const method = screen.getByRole("group", { name: "Pairing method" });
  const create = within(method).getByRole("button", { name: "Create invite" });
  const use = within(method).getByRole("button", { name: "Use an invite" });
  expect(create).toHaveAttribute("aria-pressed", "true");
  const panes = container.querySelectorAll(".rf-pane");
  expect(panes).toHaveLength(2);
  expect(panes[0]).toHaveAttribute("data-on", "true");
  expect(panes[1]).toHaveAttribute("data-on", "false");
  await user.click(use);
  expect(use).toHaveAttribute("aria-pressed", "true");
  expect(panes[1]).toHaveAttribute("data-on", "true");
  // The paste box keeps what was typed when the user flips back and forth.
  fireEvent.change(screen.getByLabelText("Paste device invitation"), { target: { value: "ryn-device://invite/x" } });
  await user.click(create);
  await user.click(use);
  expect(screen.getByLabelText("Paste device invitation")).toHaveValue("ryn-device://invite/x");
}, 20000);

it("describes each offered category and shows the empty state with an invite action", async () => {
  show();
  await screen.findByText("No paired devices yet.");
  const offer = screen.getByRole("group", { name: "Offer to sync" });
  expect(within(offer).getByRole("checkbox", { name: "Saved content" })).toHaveAccessibleDescription("Bookmarks and saved items");
  expect(within(offer).getByRole("checkbox", { name: "Reading progress" })).toHaveAccessibleDescription("Where you stopped in each article");
  expect(within(offer).getByRole("checkbox", { name: "Ask Ryn history" })).toHaveAccessibleDescription("Your conversations with Ryn");
  expect(screen.getByText("Pair a computer to see its sync progress here.")).toBeInTheDocument();
  expect(screen.getAllByRole("link", { name: "Invite a computer" })).toHaveLength(2);
});

it("offers Save scope only after a category changes and keeps the checkbox semantics of the chips", async () => {
  state.devices = [base];
  const user = userEvent.setup();
  show();
  const card = await screen.findByRole("article", { name: "Device Home PC" });
  expect(within(card).getByText("Syncing")).toBeInTheDocument();
  expect(within(card).queryByRole("button", { name: "Save scope" })).not.toBeInTheDocument();
  const scope = within(card).getByRole("group", { name: "Your allowed scope" });
  await user.click(within(scope).getByRole("checkbox", { name: "Ask Ryn history" }));
  expect(within(card).getByRole("button", { name: "Save scope" })).toBeEnabled();
});
