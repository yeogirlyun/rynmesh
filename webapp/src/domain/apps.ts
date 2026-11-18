import { agentExtensions } from "./agentExtensions";
import { craftPlugins } from "./craftPlugins";
import { nodeSites } from "./nodeSites";
import { nodeControlUrl } from "./nodeUrl";
import { publicPlugins } from "./publicPlugins";
import { pluginWorkspacePath } from "../screens/pluginWorkspaceNavigation";

export interface PinProvider {
  resource: string;
  peer_id: string;
  member_name: string;
}

type PinIdentity = { key: string; app_id: string; title: string };
export type Pin = PinIdentity & (
  | { kind: "local"; open_path: string }
  | { kind: "shared"; provider: PinProvider }
);

export interface PinsResult {
  seeded: boolean;
  pins: Pin[];
}

export const APP_PINS_CHANGE_EVENT = "ryn-app-pins-changed";

export interface SharedAppProvider {
  provider_resource: string;
  peer_id: string;
  member_name: string;
  device_name: string;
  online: boolean;
  last_seen: string | null;
  summary: { state: "idle" | "busy"; queue_length: number; eta_seconds: number } | null;
}

export interface SharedApp {
  site_id: string;
  title: string;
  providers: SharedAppProvider[];
}

export interface SharedAppsResult {
  apps: SharedApp[];
  colleagues_total: number;
  sharing_total: number;
}

export interface MyApp {
  key: string;
  id: string;
  kind: "craft" | "extension" | "public";
  title: string;
  source: "builtin" | "company" | "public";
  version: string;
  openPath: string;
  detailPath: string | null;
  updateAvailable: boolean;
  shared: boolean | null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(nodeControlUrl(path), { credentials: "include", cache: "no-store", ...init });
  if (!response.ok) throw new Error(`App request failed (${response.status})`);
  return response.json();
}

export const getPins = () => request<PinsResult>("/apps/pins");

async function savePins(pins: Pin[]): Promise<PinsResult> {
  const result = await request<PinsResult>("/apps/pins", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pins }),
  });
  window.dispatchEvent(new Event(APP_PINS_CHANGE_EVENT));
  return result;
}

// A toggle writes the entire list, so wait for earlier changes before reading it.
let pendingPinChange = Promise.resolve();
function changePins(change: () => Promise<PinsResult>): Promise<PinsResult> {
  const result = pendingPinChange.then(change);
  pendingPinChange = result.then(() => undefined, () => undefined);
  return result;
}

export const putPins = (pins: Pin[]) => changePins(() => savePins(pins));

export const togglePin = (pin: Pin) => changePins(async () => {
  const { pins } = await getPins();
  return savePins(pins.some(item => item.key === pin.key)
    ? pins.filter(item => item.key !== pin.key)
    : [...pins, pin]);
});

export async function isPinned(key: string): Promise<boolean> {
  const { pins } = await getPins();
  return pins.some(pin => pin.key === key);
}

export const sharedApps = (refresh = false) =>
  request<SharedAppsResult>(`/node-sites/shared-apps?refresh=${refresh}`);

export async function loadMyApps(): Promise<MyApp[]> {
  const [craft, extensions, published, hosted] = await Promise.allSettled([
    craftPlugins.catalog(), agentExtensions.overview(), publicPlugins.overview(), nodeSites.mine(),
  ]);
  const sharedIds = hosted.status === "fulfilled"
    ? new Set(hosted.value.sites.filter(site => site.enabled).map(site => site.site_id))
    : null;
  const apps: MyApp[] = [];

  if (craft.status === "fulfilled") {
    for (const app of craft.value.plugins.filter(app => app.installed)) {
      apps.push({
        key: `local:${app.id}`, id: app.id, kind: "craft", title: app.name, source: "builtin",
        version: app.version, openPath: `/apps?open=${encodeURIComponent(app.id)}`, detailPath: null,
        updateAvailable: false, shared: sharedIds?.has(app.id) ?? null,
      });
    }
  }
  if (extensions.status === "fulfilled") {
    for (const app of extensions.value.plugins.filter(app => app.enabled && (app.web_app || app.views?.length || app.peer_provider))) {
      apps.push({
        key: `local:${app.id}`, id: app.id, kind: "extension", title: app.title, source: app.builtin ? "builtin" : "company",
        version: app.version, openPath: pluginWorkspacePath(app.id), detailPath: null,
        updateAvailable: false, shared: sharedIds?.has(app.id) ?? null,
      });
    }
  }
  if (published.status === "fulfilled") {
    for (const app of published.value.plugins) {
      if (!app.installed_version) continue;
      const detailPath = `/apps/app/public/${encodeURIComponent(app.id)}`;
      apps.push({
        key: `local:${app.id}`, id: app.id, kind: "public", title: app.name, source: "public",
        version: app.installed_version, openPath: detailPath, detailPath,
        updateAvailable: app.update_available, shared: sharedIds?.has(app.id) ?? null,
      });
    }
  }
  return apps;
}

export interface RecentSharedApp {
  key: string;
  site_id: string;
  title: string;
  provider: PinProvider;
}

const RECENT_SHARED_KEY = "ryn.sharedApps.recent";
const RECENT_SHARED_LIMIT = 6;

function getRecentShared(): RecentSharedApp[] {
  try {
    const entries: unknown = JSON.parse(localStorage.getItem(RECENT_SHARED_KEY) ?? "[]");
    return Array.isArray(entries) ? entries.slice(0, RECENT_SHARED_LIMIT) : [];
  } catch {
    return [];
  }
}

export const recentShared = {
  get: getRecentShared,
  add(app: RecentSharedApp): void {
    const entries = getRecentShared().filter(item => item.key !== app.key);
    try {
      localStorage.setItem(RECENT_SHARED_KEY, JSON.stringify([app, ...entries].slice(0, RECENT_SHARED_LIMIT)));
    } catch { /* Recent entries are optional when browser storage is unavailable. */ }
  },
};
