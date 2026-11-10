import { render, screen, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import i18n from "../i18n";
import { PersonalHome, PersonalServices } from "./Pages";

const state = vi.hoisted(() => ({ personal: {} as Record<string, unknown> }));
vi.mock("./model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => state.personal,
}));
vi.mock("../appContext", () => ({ useAppContext: () => ({ notify: vi.fn(), node: { node_name: "Local PC" } }) }));

beforeEach(async () => {
  await i18n.changeLanguage("en");
  const devices = [
    { id: "self", name: "Local PC", note: "", kind: "desktop", own: true, self: true, online: true,
      network: { label: "Current network", scope: "local", route: "local" } },
    { id: "ziling", name: "ziling", note: "", kind: "desktop", own: false, online: true },
  ];
  state.personal = {
    devices,
    services: [
      { id: "ziling/codex", title: "ChatGPT", deviceId: "ziling", kind: "ai", online: false,
        availability: "needs_connection", description: "Codex CLI" },
      { id: "self/local", title: "Local model", deviceId: "self", kind: "ai", online: true,
        availability: "ready", description: "On this device" },
    ],
    loading: false, error: "", demo: false, refresh: vi.fn(), saveDevice: vi.fn(),
    resolveName: (id: string) => devices.find(device => device.id === id)?.name,
  };
});

it("lists a discovered, unowned device on Home instead of hiding it", () => {
  render(<MemoryRouter><PersonalHome /></MemoryRouter>);
  const nearby = screen.getByRole("region", { name: "Discovered devices (1)" });
  expect(within(nearby).getByText("ziling")).toBeInTheDocument();
  expect(within(nearby).getByText(/Not connected · 1 service advertised/)).toBeInTheDocument();
  expect(within(nearby).getByRole("link", { name: "Connect device" })).toHaveAttribute("href", "/devices?device=ziling");
});

it("never shows Ready or Open chat for a service that only needs a connection", () => {
  render(<MemoryRouter><PersonalServices /></MemoryRouter>);
  const rows = screen.getAllByRole("article");
  const [ready, pending] = rows;
  expect(within(ready).getByRole("button", { name: "Open chat" })).toBeEnabled();
  expect(within(ready).getByText("Ready")).toBeInTheDocument();
  expect(within(pending).queryByRole("button", { name: "Open chat" })).not.toBeInTheDocument();
  expect(within(pending).queryByText("Ready")).not.toBeInTheDocument();
  expect(within(pending).getByText("Not connected")).toBeInTheDocument();
  expect(within(pending).getByRole("link", { name: "Connect device" })).toHaveAttribute("href", "/devices?device=ziling");
});
