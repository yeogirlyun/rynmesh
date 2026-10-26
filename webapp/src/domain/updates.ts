import { invoke } from "@tauri-apps/api/core";
import { nodeControlUrl } from "./nodeUrl";

export interface DesktopUpdateStatus {
  currentVersion: string; availableVersion: string | null; notes: string | null;
  state: string; configured: boolean; autoDownload: boolean; downloaded: number;
  total: number | null; lastCheck: number | null; lastError: string | null; endpoint: string;
}
export const desktopUpdates = {
  status: () => invoke<DesktopUpdateStatus>("desktop_update_status"),
  check: () => invoke<DesktopUpdateStatus>("desktop_update_check"),
  download: () => invoke<DesktopUpdateStatus>("desktop_update_download"),
  install: () => invoke<void>("desktop_update_install"),
  preferences: (autoDownload: boolean) => invoke<DesktopUpdateStatus>("desktop_update_preferences", { autoDownload }),
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
