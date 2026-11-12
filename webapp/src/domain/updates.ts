import { invoke } from "@tauri-apps/api/core";
import { isTauriDesktop, nodeControlUrl } from "./nodeUrl";

export interface DesktopUpdateStatus {
  currentVersion: string; availableVersion: string | null; notes: string | null;
  state: string; configured: boolean; autoDownload: boolean; downloaded: number;
  total: number | null; lastCheck: number | null; lastError: string | null; endpoint: string;
  /** Install a ready update when the owner quits Ryn. */
  installOnQuit?: boolean;
  /** Consecutive failures; background ones reach the app shell only from 3 on. */
  failures?: number;
  /** The owner asked to update while Ryn was busy; it installs once idle. */
  installRequested?: boolean; waitingFor?: string | null;
  /** First start after an update: the version Ryn was updated from. */
  updatedFrom?: string | null;
}
export const VISIBLE_UPDATE_FAILURES = 3;
export const desktopUpdates = {
  status: () => invoke<DesktopUpdateStatus>("desktop_update_status"),
  check: () => invoke<DesktopUpdateStatus>("desktop_update_check"),
  download: () => invoke<DesktopUpdateStatus>("desktop_update_download"),
  /** Installs now (Ryn closes and reopens) or, while busy, as soon as Ryn is idle.
   *  `now` interrupts running work; open documents still wait. */
  install: (now = false) => invoke<DesktopUpdateStatus>("desktop_update_install", { now }),
  cancel: () => invoke<DesktopUpdateStatus>("desktop_update_cancel"),
  acknowledge: () => invoke<DesktopUpdateStatus>("desktop_update_acknowledge"),
  preferences: (value: { autoDownload?: boolean; installOnQuit?: boolean }) =>
    invoke<DesktopUpdateStatus>("desktop_update_preferences", value),
};

export type LocalizedLabel = string | Record<string, string> | null | undefined;

export interface ServiceUpdateNotice {
  version: string | null;
  notes?: string | null;
  at?: number | null;
}

export interface ServiceUpdateRow {
  id: string;
  name: LocalizedLabel;
  plugins: string[];
  plugin_names: string[];
  auth_provider?: string | null;
  auth_label?: LocalizedLabel;
  publisher?: LocalizedLabel;
  state: string;
  current_version?: string | null;
  available_version?: string | null;
  notes?: string | null;
  min_host?: string | null;
  host_version?: string | null;
  waiting_for?: string | null;
  rolled_back_version?: string | null;
  updated?: ServiceUpdateNotice | null;
  downloaded: number;
  total: number | null;
  last_error?: string | null;
  last_check?: number | null;
}

export interface ServiceUpdateStatus {
  services: ServiceUpdateRow[];
  auto_download?: boolean;
  install_on_quit?: boolean;
  last_error?: string | null;
}

export const SERVICE_UPDATE_CHANGED_EVENT = "ryn:service-update-changed";

export function pickLocalizedLabel(value: LocalizedLabel, language: string, fallback = ""): string {
  if (!value) return fallback;
  if (typeof value === "string") return value;
  const zh = language.toLowerCase().startsWith("zh");
  if (zh) {
    return value["zh-CN"] || value.zh || value.en || Object.values(value)[0] || fallback;
  }
  return value.en || value["en-US"] || value["zh-CN"] || Object.values(value)[0] || fallback;
}

function notifyServiceUpdateChanged(status: ServiceUpdateStatus) {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent(SERVICE_UPDATE_CHANGED_EVENT, { detail: status }));
  }
}

async function requestServiceUpdates(path = "", method: "GET" | "POST" = "GET"): Promise<ServiceUpdateStatus> {
  const response = await fetch(nodeControlUrl(`/service-updates${path}`), {
    method,
    credentials: "include",
    cache: "no-store",
    headers: method === "POST" ? { "Content-Type": "application/json" } : undefined,
    ...(method === "POST" ? { body: "{}" } : {}),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "service_update_failed");
  return value as ServiceUpdateStatus;
}

export const serviceUpdates = {
  status: () => requestServiceUpdates(),
  checkAll: async () => {
    const status = await requestServiceUpdates("/check", "POST");
    notifyServiceUpdateChanged(status);
    return status;
  },
  check: async (id: string) => {
    const status = await requestServiceUpdates(`/${encodeURIComponent(id)}/check`, "POST");
    notifyServiceUpdateChanged(status);
    return status;
  },
  /** Switch to the staged release immediately, restart only that plugin service,
   *  and confirm the new revision is answering health checks. */
  apply: async (id: string) => {
    const switched = await requestServiceUpdates(`/${encodeURIComponent(id)}/apply?force=1`, "POST");
    notifyServiceUpdateChanged(switched);
    if (isTauriDesktop()) {
      try {
        await invoke<boolean>("restart_plugin_service", { serviceId: id });
      } catch {
        // Verification below still runs and rolls back if the new revision stays unhealthy.
      }
    }
    const verified = await requestServiceUpdates(`/${encodeURIComponent(id)}/verify`, "POST");
    if (isTauriDesktop() && verified.services.some((row) => row.id === id && row.state === "rolled_back")) {
      try {
        await invoke<boolean>("restart_plugin_service", { serviceId: id });
      } catch {
        // Supervisor loop will continue retrying the restored runtime if needed.
      }
    }
    notifyServiceUpdateChanged(verified);
    return verified;
  },
  verify: async (id: string) => {
    const status = await requestServiceUpdates(`/${encodeURIComponent(id)}/verify`, "POST");
    notifyServiceUpdateChanged(status);
    return status;
  },
  retry: async (id: string) => {
    const status = await requestServiceUpdates(`/${encodeURIComponent(id)}/retry`, "POST");
    notifyServiceUpdateChanged(status);
    return status;
  },
  acknowledge: async (id: string) => {
    const status = await requestServiceUpdates(`/${encodeURIComponent(id)}/acknowledge`, "POST");
    notifyServiceUpdateChanged(status);
    return status;
  },
};

export interface PluginUpdateSource {
  kind: "public" | "company"; url: string; public_key: string; auto_update: boolean; token_env?: string;
}
export interface PluginUpdateRow {
  id: string; current_version: string; source: PluginUpdateSource; state: string;
  available_version?: string | null; last_error?: string | null; last_check?: number; can_rollback: boolean;
}
export interface PluginUpdateStatus { plugins: PluginUpdateRow[]; last_error?: string | null }
async function request(path = "", body?: unknown): Promise<PluginUpdateStatus> {
  const response = await fetch(nodeControlUrl(`/plugin-updates${path}`), {
    method: body === undefined ? "GET" : "POST", credentials: "include", cache: "no-store",
    headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "update_operation_failed");
  return value;
}
export const pluginUpdates = {
  status: () => request(),
  configure: (id: string, source: PluginUpdateSource) => request(`/${encodeURIComponent(id)}/configure`, source),
  action: (id: string, action: "check" | "apply" | "rollback") => request(`/${encodeURIComponent(id)}/${action}`, {}),
};
