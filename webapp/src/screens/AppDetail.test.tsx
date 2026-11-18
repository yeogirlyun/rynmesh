import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { isPinned, togglePin } from "../domain/apps";
import { imageStudio, type StudioJob } from "../domain/imageStudio";
import { nodeSites, type HostedSite } from "../domain/nodeSites";
import { publicPlugins, type PublicCatalog } from "../domain/publicPlugins";
import AppDetail from "./AppDetail";
import PublicPluginPage, { LegacyImageStudioRedirect } from "./PublicPluginPage";

vi.mock("../domain/apps", () => ({
  APP_PINS_CHANGE_EVENT: "ryn-app-pins-changed", isPinned: vi.fn(), togglePin: vi.fn(),
}));
vi.mock("../domain/nodeSites", async original => ({
  ...await original<typeof import("../domain/nodeSites")>(),
  nodeSites: { mine: vi.fn(), update: vi.fn() },
}));
vi.mock("../domain/imageStudio", async original => ({
  ...await original<typeof import("../domain/imageStudio")>(),
  imageStudio: { status: vi.fn(), queue: vi.fn(), usage: vi.fn(), cancel: vi.fn(), prioritize: vi.fn() },
}));
vi.mock("../domain/publicPlugins", () => ({
  publicPlugins: { overview: vi.fn(), action: vi.fn(), open: vi.fn() },
}));

const id = "image-studio";
const path = `/apps/app/public/${id}`;
const localUrl = "http://0123456789abcdef0123456789abcdef.site.localhost:8791/";
const catalog: PublicCatalog = {
  checked_at: null, error: "", legacy_data: false, source: "public",
  plugins: [{
    id, name: "Image Studio", description: "", notes: "New templates", source: "public",
    installed_version: "1.0.0", version: "1.1.0", enabled: true, running: true,
    update_available: true, rollback_available: true, error: "", permissions: [],
  }],
};
const member = { provider_resource: "resource", member_id: "m1", member_name: "Example colleague" };
const site: HostedSite = {
  site_id: id, title: "Image Studio", port: 0, entry: "/", updated_at: "", enabled: false,
  audience: { mode: "all", members: [] },
};

function Location() {
  const { pathname, search } = useLocation();
  return <output data-testid="location">{pathname}{search}</output>;
}

function show(initial = path) {
  render(<MemoryRouter initialEntries={[initial]}>
    <Routes>
      <Route path="/apps/public-plugins/:pluginId" element={<PublicPluginPage />} />
      <Route path="/apps/sites/image-studio" element={<LegacyImageStudioRedirect />} />
      <Route path="/apps/app/public/:id" element={<AppDetail />} />
    </Routes>
    <Location />
  </MemoryRouter>);
  return userEvent.setup();
}

beforeEach(() => {
  vi.resetAllMocks();
  let current = site;
  vi.mocked(nodeSites.mine).mockImplementation(async () => ({ sites: [current], members: [member] }));
  vi.mocked(nodeSites.update).mockImplementation(async (_, change) => current = { ...current, ...change });
  vi.mocked(publicPlugins.overview).mockResolvedValue(catalog);
  vi.mocked(publicPlugins.open).mockResolvedValue({ url: localUrl, name: "Image Studio" });
  vi.mocked(isPinned).mockResolvedValue(false);
  vi.mocked(imageStudio.status).mockResolvedValue({
    model: { ready: true, code: "" }, enabled: false, audience: site.audience, running: null,
    today: 12, queue_length: 0, average_seconds: 30,
  });
  vi.mocked(imageStudio.queue).mockResolvedValue([]);
  vi.mocked(imageStudio.usage).mockResolvedValue([]);
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ providers: [] })));
});

it("selects three URL-backed tabs and opens the validated sandboxed frame", async () => {
  const user = show();
  const frame = await screen.findByTitle("Image Studio");
  expect(frame).toHaveAttribute("src", localUrl);
  expect(frame).toHaveAttribute("sandbox", "allow-scripts allow-forms allow-same-origin allow-downloads");
  expect(frame).toHaveAttribute("referrerpolicy", "no-referrer");
  expect(screen.getByRole("button", { name: "Use" })).toHaveAttribute("aria-current", "page");
  await user.click(screen.getByRole("button", { name: "Share with colleagues" }));
  expect(screen.getByTestId("location")).toHaveTextContent(`${path}?tab=share`);
  expect(await screen.findByRole("switch")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Version and updates" }));
  expect(screen.getByTestId("location")).toHaveTextContent(`${path}?tab=version`);
  expect(screen.getByText("New templates")).toBeInTheDocument();
  expect(screen.queryByTitle("Image Studio")).not.toBeInTheDocument();
});

it("hides sharing when the app has no hosted site and refuses an unsafe iframe origin", async () => {
  vi.mocked(nodeSites.mine).mockResolvedValue({ sites: [], members: [] });
  vi.mocked(publicPlugins.open).mockResolvedValue({ url: "https://public.test/", name: "Image Studio" });
  show(`${path}?tab=share`);
  expect(await screen.findByRole("alert")).toHaveTextContent("Install and enable");
  expect(screen.queryByRole("button", { name: "Share with colleagues" })).not.toBeInTheDocument();
  expect(screen.queryByTitle("Image Studio")).not.toBeInTheDocument();
});

it("sets and clears sharing, changes audience, and selects members on the hosted site", async () => {
  const user = show(`${path}?tab=share`);
  const toggle = await screen.findByRole("switch");
  expect(toggle).not.toBeChecked();
  await user.click(toggle);
  expect(nodeSites.update).toHaveBeenCalledWith(id, { enabled: true });
  expect(toggle).toBeChecked();
  expect(await screen.findByText(/isn't connected to a company/)).toBeInTheDocument();
  await user.click(screen.getByRole("radio", { name: "Only selected people" }));
  expect(nodeSites.update).toHaveBeenCalledWith(id, { audience: { mode: "members", members: [] } });
  const colleague = await screen.findByRole("checkbox", { name: member.member_name });
  await user.click(colleague);
  expect(nodeSites.update).toHaveBeenCalledWith(id, { audience: { mode: "members", members: [member.member_id] } });
  expect(colleague).toBeChecked();
  await user.click(toggle);
  expect(nodeSites.update).toHaveBeenCalledWith(id, { enabled: false });
  expect(toggle).not.toBeChecked();
});

it("includes stop, cancel, move-up and usage for Image Studio", async () => {
  const job = { purpose: "main", width: 1024, height: 1024, words: "", failure: null,
    created_at: 0, started_at: null, caller_name: "Example", caller: "member", priority: 0 } as const;
  const queue: StudioJob[] = [
    { ...job, id: "running", brief: "Running job", state: "running" },
    { ...job, id: "next", brief: "Next job", state: "queued" },
    { ...job, id: "last", brief: "Last job", state: "queued" },
  ];
  vi.mocked(imageStudio.queue).mockResolvedValue(queue);
  vi.mocked(imageStudio.usage).mockResolvedValue([{ caller: "member", name: "Example", today: 6, last_30_days: 84, failed: 0 }]);
  vi.mocked(imageStudio.cancel).mockResolvedValue(queue[0]);
  vi.mocked(imageStudio.prioritize).mockResolvedValue(queue[2]);
  const user = show(`${path}?tab=share`);
  await user.click(await screen.findByRole("button", { name: "Stop" }));
  expect(imageStudio.cancel).toHaveBeenCalledWith("running");
  await user.click(screen.getByRole("button", { name: "Move up" }));
  expect(imageStudio.prioritize).toHaveBeenCalledWith("last");
  const next = screen.getByText("Next job").closest("tr")!;
  await user.click(within(next).getByRole("button", { name: "Cancel" }));
  expect(imageStudio.cancel).toHaveBeenCalledWith("next");
  expect(screen.getByText("84 images")).toBeInTheDocument();
});

it("confirms updates and uninstalls and shows catalog action errors", async () => {
  vi.mocked(publicPlugins.action).mockResolvedValueOnce(catalog).mockRejectedValueOnce(new Error("plugin_busy"));
  const user = show(`${path}?tab=version`);
  await user.click(await screen.findByRole("button", { name: "Update" }));
  expect(screen.getByRole("dialog")).toHaveTextContent("1.1.0");
  expect(publicPlugins.action).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Confirm" }));
  expect(publicPlugins.action).toHaveBeenCalledWith(id, "update");
  const uninstall = screen.getByRole("button", { name: "Uninstall" });
  await waitFor(() => expect(uninstall).toBeEnabled());
  await user.click(uninstall);
  expect(screen.getByRole("dialog")).toHaveTextContent("Images and settings remain");
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  expect(publicPlugins.action).toHaveBeenCalledTimes(1);
  await user.click(uninstall);
  await user.click(screen.getByRole("button", { name: "Confirm" }));
  expect(publicPlugins.action).toHaveBeenCalledWith(id, "uninstall");
  expect(await screen.findByRole("alert")).toHaveTextContent("Finish or cancel active jobs");
});

it("pins the public app using its new detail URL", async () => {
  const pin = { kind: "local", key: `local:${id}`, app_id: id, title: "Image Studio", open_path: path } as const;
  vi.mocked(togglePin).mockResolvedValue({ pins: [pin], seeded: true });
  const user = show();
  const button = await screen.findByRole("button", { name: "Pin" });
  await waitFor(() => expect(button).toBeEnabled());
  await user.click(button);
  expect(togglePin).toHaveBeenCalledWith(pin);
  expect(await screen.findByRole("button", { name: "Pinned" })).toHaveAttribute("aria-pressed", "true");
});

it.each([
  [`/apps/public-plugins/${id}`, path],
  ["/apps/sites/image-studio", `${path}?tab=share`],
])("redirects %s to %s", async (start, destination) => {
  show(start);
  await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(destination));
  if (destination.endsWith("?tab=share")) {
    expect(await screen.findByRole("switch")).toBeInTheDocument();
    expect(publicPlugins.open).not.toHaveBeenCalled();
  } else {
    expect(await screen.findByTitle("Image Studio")).toBeInTheDocument();
  }
});
