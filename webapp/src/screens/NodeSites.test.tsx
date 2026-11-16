import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import NodeSites from "./NodeSites";
import NodeSiteWindow from "./NodeSiteWindow";
import { nodeSites, type OpenedSiteStatus } from "../domain/nodeSites";

vi.mock("../domain/nodeSites", async (original) => {
  const actual = await original<typeof import("../domain/nodeSites")>();
  return { ...actual, nodeSites: { remote: vi.fn(), open: vi.fn(), status: vi.fn(), mine: vi.fn(), update: vi.fn() } };
});
const api = vi.mocked(nodeSites);
const KEY = "0123456789abcdef0123456789abcdef";
const ORIGIN = `http://${KEY}.site.localhost:18797`;

function status(over: Partial<OpenedSiteStatus> = {}): OpenedSiteStatus {
  return { key: KEY, site_id: "image-studio", peer_id: "peer-b", online: true, route: "lan",
    member_name: "Ziling", device_name: "ZL-PC", last_seen: "2026-11-13T04:05:00Z", origin: ORIGIN, ...over };
}

function renderWindow(path: string) {
  return render(<MemoryRouter initialEntries={[path]}>
    <Routes>
      <Route path="/apps/sites" element={<p>list page</p>} />
      <Route path="/apps/sites/open/:key" element={<NodeSiteWindow />} />
    </Routes>
  </MemoryRouter>);
}

beforeEach(() => {
  vi.clearAllMocks();
  api.mine.mockResolvedValue({ sites: [], members: [] });
});

it("polls without overlapping a slow check and reloads after recovery", async () => {
  vi.useFakeTimers();
  let resolve!: (value: OpenedSiteStatus) => void;
  api.status.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
  const view = renderWindow(`/apps/sites/open/${KEY}`);
  try {
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(api.status).toHaveBeenCalledTimes(1);
    await act(async () => { resolve(status({ online: false })); });
    expect(view.container.querySelector("iframe")).toBeNull();
    api.status.mockResolvedValue(status());
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(view.container.querySelector("iframe")).not.toBeNull();
  } finally {
    view.unmount();
    vi.useRealTimers();
  }
});

it("shows the page in an isolated frame under a bar Ryn draws", async () => {
  api.status.mockResolvedValue(status());
  const { container } = renderWindow(`/apps/sites/open/${KEY}?entry=%2Fstudio%3Fx%3D1&title=Image`);
  const frame = await waitFor(() => {
    const value = container.querySelector("iframe");
    expect(value).not.toBeNull();
    return value as HTMLIFrameElement;
  });
  expect(frame.getAttribute("src")).toBe(`${ORIGIN}/studio?x=1&lang=en`);
  expect(frame.getAttribute("sandbox")).toBe("allow-scripts allow-forms allow-same-origin allow-downloads");
  expect(frame.getAttribute("referrerpolicy")).toBe("no-referrer");
  expect(screen.getByText("Ziling's computer · Image")).toBeInTheDocument();
  expect(screen.getByText("Online")).toBeInTheDocument();
  expect(api.status).toHaveBeenCalledWith(KEY, true);
});

it("shows an offline screen instead of the page, and never a foreign origin or entry", async () => {
  api.status.mockResolvedValue(status({ online: false }));
  const { container, unmount } = renderWindow(`/apps/sites/open/${KEY}`);
  expect(await screen.findByText("Ziling's computer is offline")).toBeInTheDocument();
  expect(container.querySelector("iframe")).toBeNull();
  expect(screen.getByRole("button", { name: "Reconnect" })).toBeInTheDocument();
  unmount();

  api.status.mockResolvedValue(status({ origin: "https://evil.example" }));
  const second = renderWindow(`/apps/sites/open/${KEY}`);
  await waitFor(() => expect(api.status).toHaveBeenCalledTimes(2));
  expect(second.container.querySelector("iframe")).toBeNull();
  second.unmount();

  api.status.mockResolvedValue(status());
  const third = renderWindow(`/apps/sites/open/${KEY}?entry=%2F%2Fevil.example%2Fx`);
  await waitFor(() => expect(third.container.querySelector("iframe")?.getAttribute("src")).toBe(`${ORIGIN}/?lang=en`));
});

it("lists colleagues' pages, opens one, and toggles a hosted page", async () => {
  api.remote.mockResolvedValue([
    { provider_resource: "https://p.test/connect", peer_id: "peer-b", member_name: "Ziling", device_name: "ZL-PC",
      online: true, sites: [{ site_id: "image-studio", title: "Image studio", entry: "/" }] },
    { provider_resource: "https://p.test/connect", peer_id: "peer-c", member_name: "Bai", device_name: "BF",
      online: false, sites: [] },
  ]);
  api.open.mockResolvedValue({ key: KEY, url: `${ORIGIN}/`, entry: "/", title: "Image studio", peer_id: "peer-b",
    provider_resource: "https://p.test/connect", member_name: "Ziling", device_name: "ZL-PC" });
  const hosted = { site_id: "image-studio", title: "Image studio", port: 18150, entry: "/", enabled: false,
    audience: { mode: "all" as const, members: [] }, updated_at: "" };
  api.mine.mockResolvedValue({ sites: [hosted], members: [] });
  api.update.mockResolvedValue({ ...hosted, enabled: true });
  render(<MemoryRouter initialEntries={["/apps/sites"]}>
    <Routes>
      <Route path="/apps/sites" element={<NodeSites />} />
      <Route path="/apps/sites/open/:key" element={<p>window for {KEY}</p>} />
    </Routes>
  </MemoryRouter>);
  expect(await screen.findByText("1 shared page")).toBeInTheDocument();
  expect(screen.getByText("Offline")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("checkbox", { name: "Open to colleagues" }));
  expect(api.update).toHaveBeenCalledWith("image-studio", { enabled: true });
  await userEvent.click(screen.getByRole("button", { name: "Open" }));
  expect(api.open).toHaveBeenCalledWith(expect.objectContaining({ peer_id: "peer-b" }), "image-studio");
  expect(await screen.findByText(`window for ${KEY}`)).toBeInTheDocument();
});
