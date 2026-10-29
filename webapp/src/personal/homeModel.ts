import type { DeviceNetwork } from "./deviceNetwork";
import type { Device, Service } from "./model";

export type HomeView = "list" | "map";
const VIEW_KEY = "ryn.home.view";

/** The last chosen Home view is only a per-person convenience; storage may be unavailable. */
export function readHomeView(): HomeView {
  try {
    return window.localStorage.getItem(VIEW_KEY) === "map" ? "map" : "list";
  } catch {
    return "list";
  }
}
export function writeHomeView(view: HomeView) {
  try {
    window.localStorage.setItem(VIEW_KEY, view);
  } catch {
    /* The view still changes for this visit. */
  }
}

export type Tone = "teal" | "blue" | "violet" | "slate" | "rose" | "indigo" | "cyan";
export type DeviceGroup = {
  id: string;
  /** Raw network label; translate with personalLabelKeys when it is an application-owned value. */
  label: string;
  scope: DeviceNetwork["scope"];
  devices: Device[];
  current: boolean;
  tone: Tone;
};

/**
 * Same grouping the device map draws: this device and its LAN peers first, then every
 * other network the node knows about, in the order they were discovered.
 */
export function groupHomeDevices(devices: Device[]): DeviceGroup[] {
  if (!devices.length) return [];
  const self = devices.find((device) => device.self);
  const local = devices.filter((device) => !device.self && device.network?.scope === "local");
  const groups: DeviceGroup[] = [
    {
      id: self?.network?.id || "current",
      label: self?.network?.label || "Current network",
      scope: "local",
      current: true,
      tone: "teal",
      devices: [...(self ? [self] : []), ...local],
    },
  ];
  const remote = new Map<string, DeviceGroup>();
  for (const device of devices) {
    if (device.self || device.network?.scope === "local") continue;
    const id = device.network?.id || "unconfirmed";
    if (!remote.has(id)) {
      const scope = device.network?.scope || "unknown";
      remote.set(id, {
        id,
        label: device.network?.label || "Network unconfirmed",
        scope,
        current: false,
        tone: scope === "public" ? "violet" : scope === "unknown" ? "slate" : "blue",
        devices: [],
      });
    }
    remote.get(id)!.devices.push(device);
  }
  return [...groups, ...remote.values()];
}

export const serviceTone = (service: Service): Tone =>
  ({ ai: "violet", document: "blue", audio: "teal", network: "cyan", video: "indigo" } as const)[service.kind];

export function serviceUrl(service: Service) {
  if (service.href) return service.href;
  const params = new URLSearchParams({
    peer: service.record?.peer_id || service.deviceId,
    service: service.record?.service.package_id || "",
  });
  return `/services/private-ai/chat?${params}`;
}
