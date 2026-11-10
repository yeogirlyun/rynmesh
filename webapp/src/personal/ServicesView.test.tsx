import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import i18n from "../i18n";
import { PersonalServices } from "./Pages";

const state = vi.hoisted(() => ({ personal: {} as Record<string, unknown> }));
vi.mock("./model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => state.personal,
}));
vi.mock("../appContext", () => ({ useAppContext: () => ({ notify: vi.fn(), node: { node_name: "Laptop" } }) }));

beforeEach(async () => {
  await i18n.changeLanguage("en");
  const devices = [
    { id: "self", name: "Laptop", note: "", kind: "laptop", own: true, self: true, online: true,
      network: { id: "local", label: "Current network", scope: "local", route: "local" } },
    { id: "home", name: "Home PC", note: "", kind: "desktop", own: true, online: true,
      network: { id: "local", label: "Current network", scope: "local", route: "lan" } },
    { id: "alex", name: "Alex's PC", note: "", kind: "desktop", own: false, online: true },
    { id: "work", name: "Work PC", note: "", kind: "desktop", own: true, online: false },
  ];
  state.personal = {
    devices,
    services: [
      { id: "alex/chat", title: "ChatGPT", deviceId: "alex", kind: "ai", online: false, availability: "needs_connection", description: "Codex CLI" },
      { id: "work/photos", title: "Photo library", deviceId: "work", kind: "network", online: false, availability: "offline", description: "Browse photos" },
      { id: "home/model", title: "Local model", deviceId: "home", kind: "ai", online: true, availability: "ready", description: "On this device", accessLabel: "API" },
    ],
    loading: false, error: "", demo: false, refresh: vi.fn(), saveDevice: vi.fn(),
    resolveName: (id: string) => devices.find(device => device.id === id)?.name,
  };
});

const renderPage = () => render(<MemoryRouter><PersonalServices /></MemoryRouter>);
const rows = () => screen.getAllByRole("article");

it("summarises every service, split by state, without changing the order of the list", () => {
  renderPage();
  expect(screen.getByRole("img", { name: "1 ready, 1 offline, 1 not connected" })).toBeInTheDocument();
  expect(screen.getByText("services on 3 devices")).toBeInTheDocument();
  // Usable first, then known-but-offline, then those that still need a connection.
  expect(rows().map(row => within(row).getByRole("heading", { level: 3 }).textContent)).toEqual(["Local modelAPI", "Photo library", "ChatGPT"]);
});

it("shows how each service is reached and keeps counts out of the filter names", () => {
  renderPage();
  const [ready, offline, pending] = rows();
  expect(within(ready).getByText("LAN direct")).toBeInTheDocument();
  expect(within(offline).getByText("Offline", { selector: "i" })).toBeInTheDocument();
  // The status pill already says "Not connected", so the device column says what is known about the route.
  expect(within(pending).getByText("Route unconfirmed")).toBeInTheDocument();
  const mine = screen.getByRole("button", { name: "My devices" });
  expect(mine.querySelector("em")).toHaveAttribute("aria-hidden", "true");
  expect(mine.querySelector("em")).toHaveTextContent("2");
  expect(screen.getByRole("button", { name: "Discovered" }).querySelector("em")).toHaveTextContent("1");
});

it("opens the chosen service in the detail panel, from the row button or anywhere on the row", () => {
  renderPage();
  const aside = () => screen.getByRole("complementary");
  expect(within(aside()).getByRole("heading", { level: 2, name: "Local model" })).toBeInTheDocument();
  expect(within(aside()).getByText("LAN direct")).toBeInTheDocument();
  fireEvent.click(within(rows()[1]).getByRole("button", { name: /Photo library/ }));
  expect(within(aside()).getByRole("heading", { level: 2, name: "Photo library" })).toBeInTheDocument();
  expect(within(aside()).getAllByText("Offline").length).toBeGreaterThan(0);
  fireEvent.click(within(rows()[2]).getByText("Alex's PC"));
  expect(within(aside()).getByRole("heading", { level: 2, name: "ChatGPT" })).toBeInTheDocument();
  expect(within(aside()).getByText("Route unconfirmed")).toBeInTheDocument();
  expect(within(aside()).getByRole("link", { name: "Connect device" })).toHaveAttribute("href", "/devices?device=alex");
  expect(within(aside()).queryByRole("button", { name: "Manage access" })).not.toBeInTheDocument();
});

it("keeps the API tab rules: configure for AI services, a note for the rest", () => {
  renderPage();
  const aside = () => screen.getByRole("complementary");
  // The first "API access" button is the tab; the Overview also offers a shortcut to it.
  fireEvent.click(within(aside()).getAllByRole("button", { name: "API access" })[0]);
  expect(within(aside()).getByRole("link", { name: "Configure API access" })).toHaveAttribute("href", "/services/api");
  fireEvent.click(within(rows()[1]).getByRole("button", { name: /Photo library/ }));
  expect(within(aside()).getByText("API details are provided by the service owner.")).toBeInTheDocument();
  expect(within(aside()).queryByRole("link", { name: "Configure API access" })).not.toBeInTheDocument();
});

it("filters by search and by ownership, and says so when nothing matches", () => {
  renderPage();
  fireEvent.change(screen.getByRole("textbox", { name: "Search services" }), { target: { value: "photo" } });
  expect(rows()).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Discovered" }));
  expect(screen.queryByRole("article")).not.toBeInTheDocument();
  expect(screen.getByText("No matching services")).toBeInTheDocument();
  expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
});

it("keeps the chosen service while a row's sample-data dialog is used", () => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  (state.personal as { services: unknown[] }).services = [
    { id: "home/model", title: "Local model", deviceId: "home", kind: "ai", online: true, availability: "ready", description: "On this device" },
    { id: "preview-converter", title: "Document converter", deviceId: "home", kind: "document", online: true, preview: true, description: "Convert documents" },
  ];
  renderPage();
  const aside = () => screen.getByRole("complementary");
  const pressed = (index: number) => within(rows()[index]).getAllByRole("button")[0].getAttribute("aria-pressed");
  expect(within(aside()).getByRole("heading", { level: 2, name: "Local model" })).toBeInTheDocument();

  // Open on another service shows its dialog but does not select that service.
  fireEvent.click(within(rows()[1]).getByRole("button", { name: "Open" }));
  const dialog = screen.getByRole("dialog", { name: "Document converter" });
  expect(within(aside()).getByRole("heading", { level: 2, name: "Local model" })).toBeInTheDocument();
  expect(pressed(1)).toBe("false");

  // Text, backdrop and Close of that dialog sit inside the row's markup, so none of them may select the row.
  fireEvent.click(within(dialog).getByText(/shown as sample data/i));
  fireEvent.click(dialog);
  expect(within(aside()).getByRole("heading", { level: 2, name: "Local model" })).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(within(aside()).getByRole("heading", { level: 2, name: "Local model" })).toBeInTheDocument();
  expect(pressed(0)).toBe("true");
  expect(pressed(1)).toBe("false");

  // Plain row content and the row's own select button still select.
  fireEvent.click(within(rows()[1]).getByText("Home PC"));
  expect(within(aside()).getByRole("heading", { level: 2, name: "Document converter" })).toBeInTheDocument();
  expect(pressed(1)).toBe("true");
  fireEvent.click(within(rows()[0]).getByRole("button", { name: /Local model/ }));
  expect(within(aside()).getByRole("heading", { level: 2, name: "Local model" })).toBeInTheDocument();
  expect(pressed(0)).toBe("true");
});

it("is fully translated", async () => {
  await i18n.changeLanguage("zh-CN");
  renderPage();
  expect(screen.getByText("项服务，分布在 3 台设备")).toBeInTheDocument();
  expect(screen.getByRole("img", { name: "1 项就绪，1 项离线，1 项未连接" })).toBeInTheDocument();
  expect(screen.getByText("提供设备")).toBeInTheDocument();
  expect(screen.getByText("状态")).toBeInTheDocument();
});
