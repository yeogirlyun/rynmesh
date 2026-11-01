import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import i18n from "../i18n";
import { PersonalHome } from "./Pages";
import { groupHomeDevices, readHomeView, writeHomeView } from "./homeModel";
import type { Device } from "./model";

const state = vi.hoisted(() => ({ personal: {} as Record<string, unknown> }));
vi.mock("./model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => state.personal,
}));
vi.mock("../appContext", () => ({ useAppContext: () => ({ notify: vi.fn(), node: { node_name: "Laptop" } }) }));

const devices: Device[] = [
  { id: "self", name: "Laptop", note: "Daily driver", kind: "laptop", own: true, self: true, online: true,
    network: { id: "local", label: "Current network", scope: "local", route: "local" } },
  { id: "home", name: "Home PC", note: "", kind: "desktop", own: true, online: true,
    network: { id: "local", label: "Current network", scope: "local", route: "lan", address: "192.168.1.8" } },
  { id: "work", name: "Work PC", note: "", kind: "desktop", own: true, online: false,
    network: { id: "office", label: "Office network", scope: "remote", route: "unknown" } },
  { id: "cloud", name: "Cloud server", note: "", kind: "desktop", own: true, online: true,
    network: { id: "public", label: "Public endpoints", scope: "public", route: "public" } },
];
const services = [
  { id: "self/ai", title: "Local model", deviceId: "self", kind: "ai", online: true, availability: "ready", description: "On this device" },
  { id: "home/doc", title: "Document converter", deviceId: "home", kind: "document", online: true, availability: "ready", description: "" },
  { id: "home/ai", title: "Home model", deviceId: "home", kind: "ai", online: false, availability: "ready", description: "" },
];

beforeEach(async () => {
  await i18n.changeLanguage("en");
  window.localStorage.clear();
  state.personal = {
    devices, services, loading: false, error: "", demo: false, refresh: vi.fn(), saveDevice: vi.fn(),
    resolveName: (id: string) => devices.find(device => device.id === id)?.name,
  };
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
});

const home = () => render(<MemoryRouter><PersonalHome /></MemoryRouter>);

it("groups devices like the connection map: this network first, then each other network", () => {
  const groups = groupHomeDevices(devices);
  expect(groups.map(group => [group.label, group.devices.map(device => device.name)])).toEqual([
    ["Current network", ["Laptop", "Home PC"]],
    ["Office network", ["Work PC"]],
    ["Public endpoints", ["Cloud server"]],
  ]);
  expect(groups.map(group => group.tone)).toEqual(["teal", "blue", "violet"]);
  expect(groups[0].current).toBe(true);
  expect(groupHomeDevices([])).toEqual([]);
  // Without this device the current network group still exists, so LAN peers are never dropped.
  expect(groupHomeDevices(devices.slice(1, 2))[0].devices.map(device => device.name)).toEqual(["Home PC"]);
});

it("stores the Home view defensively and falls back to the list", () => {
  expect(readHomeView()).toBe("list");
  writeHomeView("map");
  expect(readHomeView()).toBe("map");
  window.localStorage.setItem("ryn.home.view", "anything else");
  expect(readHomeView()).toBe("list");
});

it("opens as a list: grouped device rail, status summary and the first device's services", () => {
  const { container } = home();
  expect(screen.getByRole("button", { name: "List" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "Map" })).toHaveAttribute("aria-pressed", "false");
  expect(container.querySelector(".pf-topology")).toBeNull();
  const rail = screen.getByRole("region", { name: "Device network" });
  expect(within(rail).getByText("Current network")).toBeInTheDocument();
  expect(within(rail).getByText("You are here · 2 devices")).toBeInTheDocument();
  expect(within(rail).getByText("Office network")).toBeInTheDocument();
  expect(within(rail).getByRole("button", { name: "Laptop, This device, Online" })).toHaveAttribute("aria-pressed", "true");
  expect(within(rail).getByRole("button", { name: "Work PC, Offline" })).toHaveAttribute("aria-pressed", "false");
  expect(screen.getByText("3 online")).toBeInTheDocument();
  expect(screen.getByText("1 offline")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Services on Laptop" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Open chat" })).toBeEnabled();
});

it("selecting a device in the rail changes the device card and its services", () => {
  home();
  fireEvent.click(screen.getByRole("button", { name: "Home PC, Online" }));
  expect(screen.getByRole("heading", { name: "Services on Home PC" })).toBeInTheDocument();
  const card = within(screen.getByRole("region", { name: "Home PC" }));
  expect(card.getByRole("heading", { name: "Home PC" })).toBeInTheDocument();
  expect(card.getByText("LAN direct")).toBeInTheDocument();
  expect(card.getByText("Advertised address: 192.168.1.8 (actual connection may use another address)")).toBeInTheDocument();
  expect(card.getByText("2 services · 1 ready")).toBeInTheDocument();
  expect(screen.getByText("Document converter")).toBeInTheDocument();
  // An offline service stays visible but cannot be opened.
  expect(screen.getByText("Home model").closest("article")!.querySelector("button")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Work PC, Offline" }));
  expect(screen.getByText("Offline · last known network")).toBeInTheDocument();
  expect(screen.getByText("No services on this device")).toBeInTheDocument();
});

it("switches to the existing connection map, keeps the selection and remembers the view", () => {
  const first = home();
  fireEvent.click(screen.getByRole("button", { name: "Home PC, Online" }));
  fireEvent.click(screen.getByRole("button", { name: "Map" }));
  expect(window.localStorage.getItem("ryn.home.view")).toBe("map");
  expect(first.container.querySelector(".pf-topology")).not.toBeNull();
  expect(screen.getByRole("button", { name: "Map" })).toHaveAttribute("aria-pressed", "true");
  // Home owns the heading and the selected-device card, so neither is drawn twice.
  expect(screen.getAllByRole("heading", { name: /^My devices/ })).toHaveLength(1);
  expect(screen.getAllByRole("button", { name: "Share service" })).toHaveLength(1);
  expect(screen.getByRole("heading", { name: "Services on Home PC" })).toBeInTheDocument();
  // The map's own nodes still select a device.
  fireEvent.click(screen.getByRole("button", { name: "Cloud server, Online" }));
  expect(screen.getByRole("heading", { name: "Services on Cloud server" })).toBeInTheDocument();
  first.unmount();
  const second = home();
  expect(second.container.querySelector(".pf-topology")).not.toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "List" }));
  expect(second.container.querySelector(".pf-topology")).toBeNull();
  expect(window.localStorage.getItem("ryn.home.view")).toBe("list");
});

it("keeps rename and sharing reachable from the device card in both views", async () => {
  home();
  for (const view of ["List", "Map"]) {
    fireEvent.click(screen.getByRole("button", { name: view }));
    // The map keeps its own per-node rename button, so look inside the device card.
    const card = within(screen.getByRole("region", { name: "Laptop" }));
    fireEvent.click(card.getByRole("button", { name: "Edit Laptop" }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close dialog" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.click(card.getByRole("button", { name: "Share service" }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close dialog" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  }
});

it("counts only a device that reports offline as offline; unknown status is neither", () => {
  const unknown: Device = { id: "nas", name: "Old NAS", note: "", kind: "desktop", own: true, online: null,
    network: { id: "local", label: "Current network", scope: "local", route: "lan" } };
  state.personal = { ...state.personal, devices: [...devices, unknown] };
  home();
  expect(screen.getByText("3 online")).toBeInTheDocument();
  expect(screen.getByText("1 offline")).toBeInTheDocument();
  expect(screen.queryByText("2 offline")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Old NAS, Status unknown" })).toBeInTheDocument();
});

it("shows no offline count when nothing reports offline", () => {
  state.personal = { ...state.personal, devices: devices.filter(device => device.online !== false).map(device => device.id === "cloud" ? { ...device, online: null } : device) };
  home();
  expect(screen.getByText("2 online")).toBeInTheDocument();
  expect(screen.queryByText(/offline/)).not.toBeInTheDocument();
});

it("shows the empty states while devices are still being found", () => {
  state.personal = { ...state.personal, devices: [], services: [], loading: true };
  home();
  expect(screen.getByText("Finding devices…")).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: /^Services on/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Map" }));
  expect(screen.getByText("Finding devices…")).toBeInTheDocument();
});
