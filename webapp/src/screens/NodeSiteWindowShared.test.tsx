import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { isPinned, recentShared, sharedApps, togglePin } from "../domain/apps";
import { nodeSites } from "../domain/nodeSites";
import NodeSiteWindow from "./NodeSiteWindow";

vi.mock("../domain/apps", () => ({
  isPinned: vi.fn(), togglePin: vi.fn(), recentShared: { add: vi.fn() }, sharedApps: vi.fn(),
}));
vi.mock("../domain/nodeSites", async original => ({
  ...await original<typeof import("../domain/nodeSites")>(), nodeSites: { status: vi.fn(), open: vi.fn() },
}));
const key = "0123456789abcdef0123456789abcdef";
const resource = "https://provider.test";
const here = { provider_resource: resource, peer_id: "current", member_name: "Lin", device_name: "Laptop",
  online: true, last_seen: null, summary: null };
const other = { ...here, peer_id: "second", member_name: "Lee", device_name: "Desk" };
const status = { ...here, key, site_id: "studio", route: "lan", origin: `http://${key}.site.localhost:18797` };
const pin = { key: `shared:${resource}|current|studio`, kind: "shared" as const, app_id: "studio", title: "Studio",
  provider: { resource, peer_id: here.peer_id, member_name: here.member_name } };

function Location() {
  const location = useLocation();
  return <output>{location.pathname + location.search}</output>;
}

function show() {
  render(<MemoryRouter initialEntries={[`/apps/sites/open/${key}?title=Studio`]}>
    <Routes><Route path="/apps/sites/open/:key" element={<NodeSiteWindow />} /></Routes>
    <Location />
  </MemoryRouter>);
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(nodeSites.status).mockResolvedValue(status);
  vi.mocked(isPinned).mockResolvedValue(false);
  vi.mocked(sharedApps).mockResolvedValue({ apps: [{ site_id: "studio", title: "Studio",
    providers: [here, other, { ...other, peer_id: "gone", device_name: "Offline PC", online: false }] }],
    colleagues_total: 3, sharing_total: 3 });
});

it("records a successful visit, toggles the provider pin and returns to shared apps", async () => {
  vi.mocked(togglePin).mockResolvedValueOnce({ seeded: true, pins: [pin] }).mockResolvedValueOnce({ seeded: true, pins: [] });
  show();
  await userEvent.click(await screen.findByRole("button", { name: "Pin" }));
  expect(togglePin).toHaveBeenCalledWith(pin);
  await userEvent.click(await screen.findByRole("button", { name: "Pinned", pressed: true }));
  expect(await screen.findByRole("button", { name: "Pin", pressed: false })).toBeEnabled();
  expect(recentShared.add).toHaveBeenCalledWith({ key: pin.key, site_id: "studio", title: "Studio", provider: pin.provider });
  await userEvent.click(screen.getByRole("button", { name: "Back" }));
  expect(screen.getByText("/apps?tab=shared")).toBeInTheDocument();
});

it.each([true, false])("switches to another online provider when current online=%s", async online => {
  vi.mocked(nodeSites.status).mockResolvedValue({ ...status, online });
  vi.mocked(nodeSites.open).mockResolvedValue({ ...other, key: "second", entry: "/page?x=1", title: "Next", url: "" });
  show();
  const pickers = await screen.findAllByRole("combobox", { name: "Switch computer" });
  expect(pickers).toHaveLength(online ? 1 : 2);
  if (!online) expect(recentShared.add).not.toHaveBeenCalled();
  const picker = pickers[pickers.length - 1];
  expect(within(picker).getAllByRole("option")).toHaveLength(2);
  await userEvent.selectOptions(picker, within(picker).getByRole("option", { name: "Lee · Desk" }));
  expect(nodeSites.open).toHaveBeenCalledWith(other, "studio");
  expect(await screen.findByText("/apps/sites/open/second?entry=%2Fpage%3Fx%3D1&title=Next")).toBeInTheDocument();
});

it("hides the picker when no other computer shares this site", async () => {
  vi.mocked(sharedApps).mockResolvedValue({ apps: [{ site_id: "different", title: "Other", providers: [other] }],
    sharing_total: 1, colleagues_total: 1 });
  show();
  await waitFor(() => expect(sharedApps).toHaveBeenCalled());
  expect(screen.queryByRole("combobox")).toBeNull();
});
