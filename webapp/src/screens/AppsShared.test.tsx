import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { getPins, recentShared, sharedApps, togglePin, type Pin, type SharedAppProvider, type SharedAppsResult } from "../domain/apps";
import { nodeSites } from "../domain/nodeSites";
import { setLanguagePreference } from "../i18n";
import AppsShared from "./AppsShared";
import AppsHub from "./AppsHub";

vi.mock("../domain/apps", async importOriginal => ({
  ...await importOriginal<typeof import("../domain/apps")>(),
  getPins: vi.fn(), sharedApps: vi.fn(), togglePin: vi.fn(),
}));
vi.mock("../domain/nodeSites", () => ({ nodeSites: { open: vi.fn() } }));

const alex: SharedAppProvider = {
  provider_resource: "team", peer_id: "alex", member_name: "Alex", device_name: "Desktop A",
  online: true, last_seen: null, summary: { state: "busy", queue_length: 2, eta_seconds: 150 },
};
const lee: SharedAppProvider = {
  ...alex, peer_id: "lee", member_name: "Lee", device_name: "Desktop L",
  online: false, last_seen: "2026-11-18T04:05:00Z", summary: null,
};
const sam: SharedAppProvider = {
  ...alex, peer_id: "sam", member_name: "Sam", device_name: "Desktop S",
  summary: { state: "idle", queue_length: 0, eta_seconds: 0 },
};
const leePin: Pin = {
  key: "shared:team|lee|images", kind: "shared", title: "Image Studio", app_id: "images",
  provider: { resource: "team", peer_id: "lee", member_name: "Lee" },
};
const payload: SharedAppsResult = {
  colleagues_total: 9, sharing_total: 3,
  apps: [
    { site_id: "images", title: "Image Studio", providers: [alex, lee] },
    { site_id: "reports", title: "Reports", providers: [sam] },
  ],
};

function Destination() {
  const location = useLocation();
  return <output aria-label="Destination">{location.pathname}{location.search}</output>;
}

async function mount() {
  render(<MemoryRouter initialEntries={["/apps?tab=shared"]}>
    <Routes>
      <Route path="/apps" element={<AppsHub />} />
      <Route path="/apps/sites/open/:key" element={null} />
    </Routes>
    <Destination />
  </MemoryRouter>);
  await screen.findByText("Of 9 colleagues, 3 have shared 2 apps.");
  return userEvent.setup();
}

beforeEach(() => {
  setLanguagePreference("en");
  localStorage.clear();
  vi.mocked(sharedApps).mockClear().mockResolvedValue(payload);
  vi.mocked(getPins).mockResolvedValue({ seeded: true, pins: [leePin] });
  vi.mocked(togglePin).mockClear().mockResolvedValue({ seeded: true, pins: [] });
  vi.mocked(nodeSites.open).mockClear().mockResolvedValue({
    key: "opened-preview", url: "", entry: "/preview", title: "Image Studio",
    peer_id: alex.peer_id, provider_resource: alex.provider_resource,
    member_name: alex.member_name, device_name: alex.device_name,
  });
});

it("groups providers, shows the duration and last active time, and disables opening offline devices", async () => {
  await mount();
  const images = within(screen.getByRole("region", { name: "Image Studio" }));
  expect(images.getByText("2 devices sharing · 1 online")).toBeInTheDocument();
  expect(images.getByText("Queue: 2, about 3 minutes")).toBeInTheDocument();
  expect(images.getByText(/Last seen/)).toBeInTheDocument();
  const open = images.getAllByRole("button", { name: "Open" });
  expect(open[0]).toBeEnabled();
  expect(open[1]).toBeDisabled();
  const reports = within(screen.getByRole("region", { name: "Reports" }));
  expect(reports.getByText("Idle now")).toBeInTheDocument();
  expect(reports.queryByText(/devices sharing/)).toBeNull();
});

it("filters by online state, pins and search, and can refresh", async () => {
  const user = await mount();
  await user.click(screen.getByRole("button", { name: "Online only" }));
  expect(screen.queryByText("Lee’s computer")).toBeNull();
  expect(screen.getByText("Alex’s computer")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "My pins" }));
  expect(screen.getByText("Lee’s computer")).toBeInTheDocument();
  expect(screen.queryByText("Alex’s computer")).toBeNull();
  await user.click(screen.getByRole("button", { name: "All" }));
  const search = screen.getByRole("searchbox");
  await user.type(search, "reports");
  expect(screen.queryByRole("region", { name: "Image Studio" })).toBeNull();
  expect(screen.getByRole("region", { name: "Reports" })).toBeInTheDocument();
  await user.clear(search);
  await user.type(search, "alex");
  expect(screen.getByText("Alex’s computer")).toBeInTheDocument();
  expect(screen.queryByText("Lee’s computer")).toBeNull();
  await user.click(screen.getByRole("button", { name: "Refresh" }));
  expect(sharedApps).toHaveBeenCalledWith(true);
});

it("toggles the chosen provider pin and opens the selected app while saving a recent entry", async () => {
  const user = await mount();
  await user.click(screen.getByRole("button", { name: "Unpin Image Studio from Lee" }));
  expect(togglePin).toHaveBeenCalledWith(leePin);
  expect(await screen.findByRole("button", { name: "Pin Image Studio from Lee" })).toHaveAttribute("aria-pressed", "false");
  const group = within(screen.getByRole("region", { name: "Image Studio" }));
  await user.click(group.getAllByRole("button", { name: "Open" })[0]);
  expect(nodeSites.open).toHaveBeenCalledWith(alex, "images");
  await waitFor(() => expect(screen.getByLabelText("Destination")).toHaveTextContent("/apps/sites/open/opened-preview?entry=%2Fpreview&title=Image+Studio"));
  expect(recentShared.get()).toEqual([{
    key: "shared:team|alex|images", site_id: "images", title: "Image Studio",
    provider: { resource: "team", peer_id: "alex", member_name: "Alex" },
  }]);
});

it("explains the empty state when nobody has shared any apps", async () => {
  vi.mocked(sharedApps).mockResolvedValue({ ...payload, sharing_total: 0, apps: [] });
  render(<MemoryRouter><AppsShared query="" /></MemoryRouter>);
  expect(await screen.findByText("Apps your colleagues share will appear here.")).toBeInTheDocument();
});
