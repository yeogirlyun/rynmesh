import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import type { PeerMeta, Transfer } from "../domain/dropClient";
import { DropContext, emptyDropData, type DropState } from "./dropStore";
import { EditDevice } from "./Dialogs";
import { ConnectionDetails } from "./drop/ConnectionDetails";
import { IncomingTransferDialog } from "./drop/IncomingTransferDialog";
import { buildDropDevices } from "./drop/dropModel";

// Redesign checks for the device dialogs: each carries its modifier class and keeps its rules.
const state = vi.hoisted(() => ({ personal: {} as Record<string, unknown> }));
vi.mock("./model", async (original) => ({ ...(await original<Record<string, unknown>>()), usePersonal: () => state.personal }));
vi.mock("../appContext", () => ({ useAppContext: () => ({ notify: vi.fn(), node: { node_name: "Laptop" } }) }));

const devices = [
  { id: "self", name: "Laptop", note: "For travel", kind: "laptop", own: true, self: true, online: true },
  { id: "studio", name: "Studio PC", note: "", kind: "desktop", own: false, online: true },
];
const peer = (more: Partial<PeerMeta> = {}): PeerMeta => ({ peer_id: "studio", relationship: "nearby", name: "Studio PC", online: true, route: "lan", address: "192.168.31.40:7788",
  latency_ms: 5, sendable: true, send_mode: "verify", reason: "", verification: { required: true, code: "305614", verified: false }, auto_accept: false, unread: 0, last: null, ...more });
const transfer = (more: Partial<Transfer> = {}): Transfer => ({ transfer_id: "t1", offer_id: "o1", peer_id: "studio", direction: "in", kind: "files", name: "Render exports", file_count: 3,
  total_bytes: 1.2e9, confirmed_bytes: 0, status: "awaiting_accept", route: "lan", speed_bps: null, eta_s: null, error: null, note: "Final cut for review.", save_dir: "", final_path: "",
  verification_code: "482917", actions: ["accept", "reject"], created_at: "2026-10-27", updated_at: "2026-10-27", completed_at: null, version: 1, files_preview: [], ...more });
let drop: DropState;

beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  state.personal = { devices, services: [], demo: false, space: null, spaceAction: vi.fn(), refresh: vi.fn(), saveDevice: vi.fn().mockResolvedValue(undefined) };
  const data = emptyDropData();
  data.peers = { studio: peer() };
  data.transfers = { t1: transfer() };
  drop = { status: "ready", capabilities: null, data, transfers: [], incoming: [transfer()], incomingOpen: true, setIncomingOpen: vi.fn(), notify: vi.fn(),
    isPending: () => false, run: async (_key, fn) => fn(), refresh: vi.fn(), loadConversation: vi.fn(), markRead: vi.fn(), sendMessage: vi.fn(), retryMessage: vi.fn(),
    transferAction: vi.fn(), acceptTransfer: vi.fn().mockResolvedValue(undefined), openTransfer: vi.fn(), verifyPeer: vi.fn(), preflight: vi.fn().mockResolvedValue({ ok: true }),
    createTransfer: vi.fn(), loadPrefs: vi.fn().mockResolvedValue({ receive_dir: "" }), setAutoAccept: vi.fn().mockResolvedValue(undefined), notices: [], dismissNotice: vi.fn() } as DropState;
});

it("edit device carries its modifier class, counts characters and picks the icon with radio cards", async () => {
  render(<MemoryRouter><EditDevice device={devices[0] as never} onClose={vi.fn()} /></MemoryRouter>);
  const dialog = screen.getByRole("dialog", { name: "Edit device" });
  expect(dialog).toHaveClass("rf", "rf-dlg", "rf-dlg-edit");
  expect(dialog).not.toHaveClass("pf-dialog-edit");
  expect(within(dialog).getByText("Your device · This device")).toBeInTheDocument();
  const name = within(dialog).getByRole("textbox", { name: /^Device name/ });
  expect(name).toHaveAttribute("maxlength", "32");
  expect(within(dialog).getByText("6/32")).toBeInTheDocument();
  expect(within(dialog).getByText("10/200")).toBeInTheDocument();
  fireEvent.change(name, { target: { value: "Desk" } });
  expect(within(dialog).getByText("4/32")).toBeInTheDocument();
  expect(within(dialog).queryByRole("combobox")).not.toBeInTheDocument();
  const desktop = within(dialog).getByRole("radio", { name: "Desktop" });
  const laptop = within(dialog).getByRole("radio", { name: "Laptop" });
  expect(laptop).toBeChecked();
  fireEvent.click(desktop);
  expect(desktop).toBeChecked();
  fireEvent.click(within(dialog).getByRole("button", { name: "Save changes" }));
  await vi.waitFor(() => expect(state.personal.saveDevice).toHaveBeenCalledWith(devices[0], "Desk", "For travel", "desktop"));
});

it("incoming transfer keeps Accept gated on the code check and shows the code as boxed digits", () => {
  render(<MemoryRouter><DropContext.Provider value={drop}><IncomingTransferDialog /></DropContext.Provider></MemoryRouter>);
  const dialog = screen.getByRole("dialog");
  expect(dialog).toHaveClass("rf", "rf-dlg", "rf-dlg-incoming");
  expect(within(dialog).getByText("Render exports")).toBeInTheDocument();
  expect(within(dialog).getByRole("group", { name: "Verification code 482917" })).toBeInTheDocument();
  const accept = within(dialog).getByRole("button", { name: "Accept files" });
  expect(accept).toBeDisabled();
  fireEvent.click(within(dialog).getByRole("checkbox", { name: "Both devices show the same code" }));
  expect(accept).toBeEnabled();
  fireEvent.click(accept);
  expect(drop.acceptTransfer).toHaveBeenCalledWith(expect.objectContaining({ transfer_id: "t1" }), expect.objectContaining({ auto_accept: false }));
});

it("connection details show the route strip, the verification check and an auto-accept switch with its hint", () => {
  const device = buildDropDevices(devices as never, { studio: peer({ relationship: "own", verification: { required: false, code: null, verified: true }, auto_accept: true }) })[1];
  render(<MemoryRouter><DropContext.Provider value={drop}><ConnectionDetails device={device} onClose={vi.fn()} /></DropContext.Provider></MemoryRouter>);
  const dialog = screen.getByRole("dialog", { name: "Connection details" });
  expect(dialog).toHaveClass("rf", "rf-dlg", "rf-dlg-conn");
  expect(within(dialog).getByText("LAN direct · 5 ms")).toBeInTheDocument();
  const toggle = within(dialog).getByRole("switch", { name: /Accept files from this device automatically/ });
  expect(toggle).toBeChecked();
  expect(within(dialog).getByText("Files from Studio PC are saved without asking.")).toBeInTheDocument();
  fireEvent.click(toggle);
  expect(drop.setAutoAccept).toHaveBeenCalledWith("studio", false);
  expect(within(dialog).getByRole("link", { name: /More connection info/ })).toHaveAttribute("href", "/peers");
});
