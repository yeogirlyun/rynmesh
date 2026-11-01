import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import i18n from "../i18n";
import type { PeerMeta, Transfer } from "../domain/dropClient";
import { DropContext, emptyDropData, type DropState } from "./dropStore";
import { PersonalDevices } from "./Pages";

const state = vi.hoisted(() => ({ personal: {} as Record<string, unknown> }));
vi.mock("./model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => state.personal,
}));
vi.mock("../appContext", () => ({ useAppContext: () => ({ notify: vi.fn(), node: { node_name: "Laptop" } }) }));

const peer = (id: string, more: Partial<PeerMeta> = {}): PeerMeta => ({ peer_id: id, name: id, relationship: "own", online: true,
  route: "lan", address: "127.0.0.1", latency_ms: 3, sendable: true, send_mode: "send", reason: "",
  verification: { required: false, code: null, verified: true }, auto_accept: false, unread: 0, last: null, ...more });
const transfer = (more: Partial<Transfer> = {}): Transfer => ({ transfer_id: "t1", offer_id: "o1", peer_id: "home", direction: "out",
  kind: "folder", name: "Trip footage", file_count: 128, total_bytes: 4_000_000_000, confirmed_bytes: 2_500_000_000, status: "transferring",
  route: "lan", speed_bps: 48_000_000, eta_s: 32, error: null, note: "", save_dir: "", final_path: "", verification_code: null,
  actions: ["pause", "cancel"], created_at: "2026-10-22T09:31:00Z", updated_at: "2026-10-22T09:31:00Z", completed_at: null, version: 2, files_preview: [], ...more });

let drop: DropState;
beforeEach(async () => {
  await i18n.changeLanguage("en");
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  const devices = [
    { id: "home", name: "Home PC", note: "Study PC", kind: "desktop", own: true, online: true, hardware: "RTX 5090" },
    { id: "self", name: "Laptop", note: "For travel", kind: "laptop", own: true, self: true, online: true,
      network: { id: "local", label: "Home network", scope: "local", route: "local", address: "192.168.31.10" } },
    { id: "work", name: "Work PC", note: "For work", kind: "desktop", own: true, online: false },
    { id: "alex", name: "Alex's PC", note: "Transcription", kind: "desktop", own: false, online: true },
    { id: "studio", name: "Studio PC", note: "Video", kind: "desktop", own: false, online: true },
    { id: "cloud", name: "Cloud server", note: "", kind: "desktop", own: true, online: true },
  ];
  state.personal = {
    devices,
    services: [{ id: "home/model", title: "Local model", deviceId: "home", kind: "ai", online: true, availability: "ready", description: "", accessLabel: "API" }],
    loading: false, error: "", demo: false, refresh: vi.fn(), saveDevice: vi.fn(),
    resolveName: (id: string) => devices.find(device => device.id === id)?.name,
  };
  const data = emptyDropData();
  data.peers = {
    home: peer("home"),
    cloud: peer("cloud", { route: "public", latency_ms: 38 }),
    work: peer("work", { online: false, route: null, send_mode: "queue" }),
    alex: peer("alex", { relationship: "friend", route: "relay", latency_ms: 86, unread: 2 }),
    studio: peer("studio", { relationship: "nearby", latency_ms: 5, send_mode: "verify", verification: { required: true, code: "123456", verified: false } }),
  };
  const transfers = [transfer(), transfer({ transfer_id: "t2", direction: "in", kind: "file", name: "Proposal.docx", total_bytes: 3_400_000, confirmed_bytes: 3_400_000,
    status: "completed", actions: ["open", "reveal"], completed_at: "2026-10-22T10:02:00Z" })];
  drop = { status: "ready", capabilities: null, data, transfers, incoming: [], incomingOpen: false,
    setIncomingOpen: vi.fn(), notify: vi.fn(), isPending: () => false, run: async (_key, fn) => fn(), refresh: vi.fn(),
    loadConversation: vi.fn().mockResolvedValue(undefined), markRead: vi.fn(), sendMessage: vi.fn(), retryMessage: vi.fn(), transferAction: vi.fn(),
    acceptTransfer: vi.fn(), openTransfer: vi.fn(), verifyPeer: vi.fn(), preflight: vi.fn().mockResolvedValue({ ok: true }),
    createTransfer: vi.fn(), loadPrefs: vi.fn(), setAutoAccept: vi.fn(), notices: [], dismissNotice: vi.fn() };
});

const renderPage = (url = "/devices") => render(<MemoryRouter initialEntries={[url]}><DropContext.Provider value={drop}><PersonalDevices /></DropContext.Provider></MemoryRouter>);
const rows = () => Array.from(document.querySelectorAll<HTMLElement>(".rd-row"));
const rowName = (row: HTMLElement) => row.querySelector(".rd-name")?.textContent;

it("wears the redesign root and summarises devices, routes and what is waiting", () => {
  const { container } = renderPage();
  expect(container.querySelector(".rd-devices")).toBeInTheDocument();
  const band = screen.getByRole("region", { name: "Devices overview" });
  expect(within(band).getByText("6")).toBeInTheDocument();
  expect(within(band).getByText("devices · 5 online")).toBeInTheDocument();
  expect(within(band).getByRole("img", { name: "2 LAN direct, 1 Public direct, 1 Relay, 1 Offline" })).toBeInTheDocument();
  expect(within(band).getByRole("link", { name: "1 transfer in progress" })).toHaveAttribute("href", "/devices?tab=transfers");
  expect(within(band).getByRole("link", { name: "2 unread messages" })).toHaveAttribute("href", "/devices?tab=conversations");
});

it("lists this device first and offline devices last, and opens the first other device", () => {
  renderPage();
  expect(rows().map(rowName)).toEqual(["Laptop", "Home PC", "Cloud server", "Work PC"]);
  const aside = screen.getByRole("complementary", { name: "Home PC" });
  expect(within(aside).getByRole("heading", { level: 2, name: "Home PC" })).toBeInTheDocument();
  // The connection strip names both ends and the route.
  expect(within(aside).getByText("LAN direct · 3 ms")).toBeInTheDocument();
  expect(within(aside).getByText("My device · RTX 5090")).toBeInTheDocument();
  expect(within(aside).getByRole("button", { name: "Send files" })).toBeEnabled();
});

it("keeps filter counts out of the button names and filters by ownership and search", () => {
  renderPage();
  const mine = screen.getByRole("button", { name: "My devices" });
  expect(mine.querySelector("em")).toHaveAttribute("aria-hidden", "true");
  expect(mine.querySelector("em")).toHaveTextContent("4");
  expect(screen.getByRole("button", { name: "Discovered" }).querySelector("em")).toHaveTextContent("2");
  fireEvent.change(screen.getByRole("textbox", { name: "Search devices" }), { target: { value: "cloud" } });
  expect(rows().map(rowName)).toEqual(["Cloud server"]);
  fireEvent.change(screen.getByRole("textbox", { name: "Search devices" }), { target: { value: "zzz" } });
  expect(screen.getByText("No matching devices")).toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "Search devices" }), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Discovered" }));
  expect(rows().map(rowName)).toEqual(["Alex's PC", "Studio PC"]);
  expect(screen.getByRole("heading", { level: 2, name: "Alex's PC" })).toBeInTheDocument();
});

it("selects the device from ?device= and from a row, and switches the detail tabs", () => {
  renderPage("/devices?device=cloud");
  expect(screen.getByRole("heading", { level: 2, name: "Cloud server" })).toBeInTheDocument();
  fireEvent.click(within(rows()[1]).getByRole("button", { name: /^Home PC/ }));
  const aside = screen.getByRole("complementary", { name: "Home PC" });
  expect(within(aside).getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  fireEvent.click(within(aside).getByRole("tab", { name: "Services" }));
  expect(within(aside).getByText("Local model")).toBeInTheDocument();
  expect(within(aside).getByText("API")).toBeInTheDocument();
  expect(within(aside).getByRole("button", { name: "Choose services" })).toBeInTheDocument();
});

it("asks for verification on an unpaired nearby device and explains an offline one", () => {
  renderPage();
  fireEvent.click(screen.getByRole("button", { name: "Discovered" }));
  fireEvent.click(within(rows()[1]).getByRole("button", { name: /^Studio PC/ }));
  expect(screen.getByText(/Check the verification code before accepting/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Check verification code" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "My devices" }));
  fireEvent.click(within(rows()[3]).getByRole("button", { name: /^Work PC/ }));
  expect(screen.getByText(/The device is offline\. The transfer stays on this computer/)).toBeInTheDocument();
});

it("renders the three workspace tabs with their own content", () => {
  renderPage();
  const tabs = screen.getAllByRole("tab").filter(tab => tab.id.startsWith("drop-tab-"));
  expect(tabs.map(tab => tab.textContent)).toEqual(["Devices", "Device messages2", "Transfers1"]);
  fireEvent.click(screen.getByRole("tab", { name: /Transfers/ }));
  expect(screen.getByRole("region", { name: "In progress" })).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Completed" })).toBeInTheDocument();
  expect(screen.getByRole("article", { name: "Trip footage" })).toBeInTheDocument();
  expect(screen.getByText(/^Sending · 2\.5 \/ 4(\.0)? GB · 48 MB\/s · about 32 s$/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Completed" }));
  expect(screen.queryByRole("region", { name: "In progress" })).not.toBeInTheDocument();
  expect(screen.getByRole("article", { name: "Proposal.docx" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: /Device messages/ }));
  expect(screen.getByRole("complementary", { name: "Device conversations" })).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: /^Message / })).toBeInTheDocument();
});

it("is fully translated", async () => {
  await i18n.changeLanguage("zh-CN");
  renderPage();
  expect(screen.getByText("台设备 · 5 台在线")).toBeInTheDocument();
  expect(screen.getByRole("img", { name: "局域网直连 2 台，公网直连 1 台，中继连接 1 台，离线 1 台" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "1 个传输进行中" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "2 条未读消息" })).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "搜索设备" })).toBeInTheDocument();
});
