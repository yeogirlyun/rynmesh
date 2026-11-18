import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { getPins, loadMyApps, recentShared, sharedApps, togglePin, type Pin } from "../domain/apps";
import { setLanguagePreference } from "../i18n";
import AppsHub from "./AppsHub";

vi.mock("./CraftApps", () => ({
  default: ({ manage }: { manage?: boolean }) => <div>{manage ? "Legacy plugin manager" : "Legacy app launcher"}</div>,
}));
vi.mock("./NodeSites", () => ({ default: () => <div>Legacy shared list</div> }));
vi.mock("../domain/apps", async importOriginal => ({
  ...await importOriginal<typeof import("../domain/apps")>(),
  getPins: vi.fn(),
  loadMyApps: vi.fn(),
  sharedApps: vi.fn(),
  togglePin: vi.fn(),
}));

const local: Pin = { key: "local:office", app_id: "office", title: "Office", kind: "local", open_path: "/apps?open=office" };
const provider = { resource: "company", peer_id: "demo-peer", member_name: "Alex" };
const shared: Pin = { key: "shared:company|demo-peer|studio", app_id: "studio", title: "Shared Studio", kind: "shared", provider };

function Location() {
  const location = useLocation();
  return <output aria-label="Location">{location.pathname}{location.search}</output>;
}

function mount(url = "/apps") {
  render(<MemoryRouter initialEntries={[url]}><AppsHub /><Location /></MemoryRouter>);
  return userEvent.setup();
}

beforeEach(() => {
  setLanguagePreference("en");
  localStorage.clear();
  vi.mocked(getPins).mockResolvedValue({ seeded: true, pins: [local, shared] });
  vi.mocked(togglePin).mockResolvedValue({ seeded: true, pins: [shared] });
  vi.mocked(loadMyApps).mockResolvedValue([{
    key: "local:studio", id: "studio", kind: "public", source: "public", title: "Image Studio",
    version: "1.0.0", openPath: "/apps/app/public/studio", detailPath: "/apps/app/public/studio",
    shared: true, updateAvailable: false,
  }]);
  vi.mocked(sharedApps).mockResolvedValue({ colleagues_total: 1, sharing_total: 1, apps: [{
    site_id: "studio", title: "Shared Studio", providers: [{
      provider_resource: provider.resource, peer_id: provider.peer_id, member_name: provider.member_name,
      device_name: "Test machine", online: true, last_seen: null, summary: null,
    }],
  }] });
});

it("selects the three tabs through the URL and opens Discover from Add apps", async () => {
  const user = mount();
  expect(screen.getByRole("link", { name: "My apps" })).toHaveAttribute("aria-current", "page");
  expect(screen.getByText("Legacy app launcher")).toBeInTheDocument();

  await user.click(screen.getByRole("link", { name: "Shared by colleagues" }));
  expect(screen.getByLabelText("Location")).toHaveTextContent("/apps?tab=shared");
  expect(screen.getByText("Legacy shared list")).toBeInTheDocument();

  await user.click(screen.getByRole("link", { name: "Add apps" }));
  expect(screen.getByLabelText("Location")).toHaveTextContent("/apps?tab=discover");
  expect(screen.getByText("Legacy plugin manager")).toBeInTheDocument();

  await user.click(screen.getByRole("link", { name: "My apps" }));
  expect(screen.getByLabelText("Location")).toHaveTextContent("/apps?tab=mine");
  await screen.findByRole("button", { name: "Unpin Office" });
});

it("shows and removes a pin, and displays the shared provider’s status", async () => {
  const user = mount();
  const pinned = within(screen.getByRole("region", { name: "Pinned" }));
  expect(await pinned.findByText("On Alex’s computer · Online")).toBeInTheDocument();
  await user.click(pinned.getByRole("button", { name: "Unpin Office" }));
  expect(togglePin).toHaveBeenCalledWith(local);
  await waitFor(() => expect(pinned.queryByText("Office")).toBeNull());
  expect(pinned.getByText("Shared Studio")).toBeInTheDocument();
});

it("shows public plugins in the installed section and filters recent shared apps", async () => {
  recentShared.add({ key: shared.key, title: "Shared Studio", site_id: "studio", provider });
  const user = mount();
  const installed = within(screen.getByRole("region", { name: "Installed on this device" }));
  expect(await installed.findByRole("heading", { name: "Image Studio" })).toBeInTheDocument();
  expect(installed.getByText("Shared with colleagues")).toBeInTheDocument();
  expect(installed.getByRole("link", { name: "Open" })).toHaveAttribute("href", "/apps/public-plugins/studio");
  const recent = within(screen.getByRole("region", { name: "Recently used shared apps" }));
  expect(recent.getByRole("link", { name: "View all shared apps" })).toHaveAttribute("href", "/apps?tab=shared");
  await user.type(screen.getByRole("searchbox"), "Alex");
  expect(recent.getByRole("heading", { name: "Shared Studio" })).toBeInTheDocument();
  expect(installed.queryByRole("heading", { name: "Image Studio" })).toBeNull();
});
