// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import {
  APP_PINS_CHANGE_EVENT, getPins, isPinned, loadMyApps, recentShared, sharedApps, togglePin, type Pin,
} from "./apps";

const catalogs: Record<string, unknown> = {
  "/api/local/plugins": { plugins: [
    { id: "gridcraft", name: "Grid", installed: true, version: "1.0" },
    { id: "wordcraft", installed: false },
  ] },
  "/api/local/agent/extensions": { plugins: [
    { id: "company-workspace", title: "Workspace", enabled: true, version: "1.2", web_app: { url: "/" } },
    { id: "disabled", enabled: false, web_app: { url: "/" } },
    { id: "tools-only", enabled: true },
  ] },
  "/api/local/public-plugins": { plugins: [
    { id: "image-studio", name: "Image Studio", installed_version: "2.0", version: "2.1", update_available: true },
    { id: "other", installed_version: null },
  ] },
  "/api/local/node-sites/mine": { sites: [
    { site_id: "image-studio", enabled: true },
    { site_id: "gridcraft", enabled: false },
  ] },
};

const json = (data: unknown) => new Response(JSON.stringify(data));
const requestPath = (url: string) => {
  const { pathname, search } = new URL(url, window.location.origin);
  return `${pathname}${search}`;
};

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

it("combines the installed apps, launch paths and active sharing status", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => json(catalogs[requestPath(url)])));
  await expect(loadMyApps()).resolves.toEqual([
    {
      key: "local:gridcraft", id: "gridcraft", kind: "craft", title: "Grid", source: "builtin", version: "1.0",
      openPath: "/apps?open=gridcraft", detailPath: null, updateAvailable: false, shared: false,
    },
    {
      key: "local:company-workspace", id: "company-workspace", kind: "extension", title: "Workspace",
      source: "company", version: "1.2", openPath: "/apps/plugins/company-workspace", detailPath: null,
      updateAvailable: false, shared: false,
    },
    {
      key: "local:image-studio", id: "image-studio", kind: "public", title: "Image Studio", source: "public", version: "2.0",
      openPath: "/apps/app/public/image-studio", detailPath: "/apps/app/public/image-studio", updateAvailable: true, shared: true,
    },
  ]);
});

it("still returns other apps when one catalog fails", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => requestPath(url) === "/api/local/agent/extensions"
    ? new Response(null, { status: 503 }) : json(catalogs[requestPath(url)])));
  expect((await loadMyApps()).map(app => app.id)).toEqual(["gridcraft", "image-studio"]);
});

it("keeps sharing status unknown if the hosted sites request fails", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => requestPath(url) === "/api/local/node-sites/mine"
    ? new Response(null, { status: 503 }) : json(catalogs[requestPath(url)])));
  expect((await loadMyApps()).map(app => app.shared)).toEqual([null, null, null]);
});

it("adds and removes pins and emits an event for each successful save", async () => {
  const pin: Pin = { key: "local:gridcraft", kind: "local", app_id: "gridcraft", title: "Grid", open_path: "/apps?open=gridcraft" };
  const other: Pin = {
    key: "shared:company|device|image-studio", kind: "shared", app_id: "image-studio", title: "Image Studio",
    provider: { resource: "company", peer_id: "device", member_name: "Taylor" },
  };
  let stored = { seeded: true, pins: [other] };
  const fetcher = vi.fn(async (_url: string, options: RequestInit) => {
    if (options.method === "PUT") stored = { seeded: true, pins: JSON.parse(String(options.body)).pins };
    return json(stored);
  });
  vi.stubGlobal("fetch", fetcher);
  const onChange = vi.fn();
  window.addEventListener(APP_PINS_CHANGE_EVENT, onChange);
  try {
    await expect(isPinned(pin.key)).resolves.toBe(false);
    await expect(togglePin(pin)).resolves.toEqual({ seeded: true, pins: [other, pin] });
    await expect(isPinned(pin.key)).resolves.toBe(true);
    await expect(togglePin(pin)).resolves.toEqual({ seeded: true, pins: [other] });
    await expect(getPins()).resolves.toEqual({ seeded: true, pins: [other] });
    const writes = fetcher.mock.calls.filter(([, options]) => options.method === "PUT");
    expect(writes.map(([url]) => requestPath(url))).toEqual(["/api/local/apps/pins", "/api/local/apps/pins"]);
    expect(writes[0][1].credentials).toBe("include");
    expect(onChange).toHaveBeenCalledTimes(2);
  } finally {
    window.removeEventListener(APP_PINS_CHANGE_EVENT, onChange);
  }
});

it("preserves both pins when toggles overlap in the same window", async () => {
  const localPin = (id: string): Pin => ({
    key: `local:${id}`, kind: "local", app_id: id, title: id, open_path: `/apps?open=${id}`,
  });
  const first = localPin("gridcraft");
  const second = localPin("wordcraft");
  let pins: Pin[] = [];
  vi.stubGlobal("fetch", vi.fn(async (_url: string, options: RequestInit) => {
    if (options.method === "PUT") pins = JSON.parse(String(options.body)).pins;
    return json({ seeded: true, pins });
  }));
  await Promise.all([togglePin(first), togglePin(second)]);
  expect(pins).toEqual([first, second]);
});

it("passes the refresh option and returns app groups with their providers", async () => {
  const payload = {
    apps: [{ site_id: "image-studio", title: "Image Studio", providers: [{
      provider_resource: "company", peer_id: "device", member_name: "Taylor", device_name: "Laptop", online: true,
      last_seen: null, summary: { state: "busy", queue_length: 1, eta_seconds: 30 },
    }] }],
    colleagues_total: 2, sharing_total: 1,
  };
  const fetcher = vi.fn(async (_url: string) => json(payload));
  vi.stubGlobal("fetch", fetcher);
  await expect(sharedApps()).resolves.toEqual(payload);
  await expect(sharedApps(true)).resolves.toEqual(payload);
  expect(fetcher.mock.calls.map(([url]) => requestPath(url))).toEqual([
    "/api/local/node-sites/shared-apps?refresh=false", "/api/local/node-sites/shared-apps?refresh=true",
  ]);
});

it("stores six most recent entries and moves a reopened entry to the front", () => {
  const entries = Array.from({ length: 7 }, (_, index) => ({
    key: `shared:company|device-${index}|image-studio`, site_id: "image-studio", title: "Image Studio",
    provider: { resource: "company", peer_id: `device-${index}`, member_name: "Taylor" },
  }));
  entries.forEach(recentShared.add);
  expect(recentShared.get()).toEqual(entries.slice(1).reverse());
  recentShared.add(entries[2]);
  const expected = [entries[2], ...entries.slice(3).reverse(), entries[1]];
  expect(recentShared.get()).toEqual(expected);
  expect(JSON.parse(localStorage.getItem("ryn.sharedApps.recent")!)).toEqual(expected);
});
