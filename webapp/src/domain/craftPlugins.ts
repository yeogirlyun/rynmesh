import { invoke } from "@tauri-apps/api/core";
import { isTauriDesktop, nodeControlUrl } from "./nodeUrl";

export type CraftPlugin = {
  id: string; name: string; version: string; group: "office" | "design" | "media" | "workflow" | "storage";
  kind?: "web_editor" | "builtin_adapter"; installable?: boolean; launch_path?: string | null;
  web: boolean; formats: string[]; sha256: string | null; source_url: string;
  installed: boolean; enabled: boolean; running: boolean; busy: boolean;
  agent_tools: string[]; editing_supported: boolean; warning: string | null;
  automation_supported?: boolean; automation_installed?: boolean; automation_error?: string;
};
export type CraftCatalog = {
  node_id: string; plugins: CraftPlugin[]; setup_completed: boolean;
  defaults: string[]; selection: string[]; setup_requested?: boolean; storage_path: string; migration_error?: string | null;
};
export type CraftLaunch = { app: string; version: string; node_id: string; url: string; ticket: string };

export class PluginError extends Error {}
async function request<T>(path = "", body?: Record<string, unknown>): Promise<T> {
  const response = await fetch(nodeControlUrl(`/plugins${path}`), {
    method: body ? "POST" : "GET", credentials: "include", cache: "no-store",
    headers: { "Content-Type": "application/json" },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (!response.ok) {
    const value = await response.json().catch(() => ({}));
    throw new PluginError(typeof value.detail === "string" ? value.detail : "plugin_operation_failed");
  }
  return response.json();
}
/** An editor as an app of the computer: "Ryn Excel" in the Start menu or Applications. */
export type SystemApp = {
  name: string; documents: string[];
  place: "start_menu" | "applications";
  /** "settings": Windows lets the user pick in Default apps. "direct": macOS sets it here. */
  default_choice: "settings" | "direct";
  /**
   * What is really on the computer: "ready" once the entries exist, "pending" while a sync runs,
   * "failed" when the last sync could not add the app (see `error`), "missing" otherwise.
   */
  state: "ready" | "pending" | "failed" | "missing";
  error?: string | null;
};
export const systemApps = {
  info: (id: string) => isTauriDesktop() ? invoke<SystemApp | null>("system_app_info", { appId: id }) : Promise.resolve(null),
  /** Rejects with the reason when an app could not be added; the outcome is also kept for `info`. */
  sync: () => isTauriDesktop() ? invoke<void>("sync_system_apps") : Promise.resolve(),
  chooseDefault: (id: string) => invoke<void>("choose_default_system_app", { appId: id }),
};
// Start menu, Launchpad and "Open with" follow every change to the installed, enabled editors.
async function changed(result: Promise<CraftCatalog>): Promise<CraftCatalog> {
  const catalog = await result;
  // The plugin change stands either way; a failed sync shows in the plugin's details.
  void systemApps.sync().catch(() => undefined);
  return catalog;
}
export const craftPlugins = {
  catalog: () => request<CraftCatalog>(),
  setup: () => changed(request<CraftCatalog>("/setup", {})),
  install: (id: string) => changed(request<CraftCatalog>(`/${encodeURIComponent(id)}/install`, {})),
  installAutomation: (id: string) => request<CraftCatalog>(`/${encodeURIComponent(id)}/install-automation`, {}),
  enable: (id: string, enabled: boolean) => changed(request<CraftCatalog>(`/${encodeURIComponent(id)}/enable`, { enabled })),
  uninstall: (id: string, clear_preferences: boolean) => changed(request<CraftCatalog>(`/${encodeURIComponent(id)}/uninstall`, { clear_preferences })),
  preview: (id: string) => request<CraftLaunch>(`/${encodeURIComponent(id)}/preview-launch`, {}),
  launch: (id: string, ticket: string) => request<Omit<CraftLaunch, "ticket">>(`/${encodeURIComponent(id)}/launch`, { ticket }),
};
export function verifiedPluginPath(plugin: CraftPlugin): string {
  const routes: Record<string, string> = {
    resume: "/apps/workspaces?app=resume", recruit: "/apps/workspaces?app=recruit",
    desk: "/apps/workspaces?app=desk", nas: "/nas",
  };
  const expected = routes[plugin.id];
  if (plugin.kind !== "builtin_adapter" || !expected || plugin.launch_path !== expected) throw new PluginError("plugin_launch_rejected");
  return expected;
}
// The trusted local manager is the only launch provider in this release. A model,
// friend catalog, URL query or arbitrary remote host can never supply a target.
const PORT_ORDER = ["cadcraft", "deckcraft", "designcraft", "effectcraft", "figcraft", "filmcraft", "gridcraft", "lightcraft", "pdfcraft", "photocraft", "soundcraft", "vectorcraft", "wordcraft"];
// Route state selects a known plugin only. Its text, permissions and capabilities
// are never trusted; Agent queries the live local plugin tool after the user sends.
export function craftAgentPluginID(state: unknown): string | null {
  const id = state && typeof state === "object" ? (state as { craftPluginID?: unknown }).craftPluginID : null;
  return typeof id === "string" && [...PORT_ORDER, "resume", "recruit", "desk", "nas"].includes(id) ? id : null;
}
export function verifiedCraftUrl(value: Omit<CraftLaunch, "ticket">, plugin: CraftPlugin, nodeId: string): string {
  const index = PORT_ORDER.indexOf(plugin.id);
  const controlHost = new URL(nodeControlUrl("/craft-plugins"), window.location.href).hostname;
  if (!isTauriDesktop() && !["127.0.0.1", "localhost", "[::1]"].includes(controlHost)) throw new PluginError("plugin_local_browser_required");
  if (index < 0 || !plugin.web || value.app !== plugin.id || value.node_id !== nodeId || value.version !== plugin.version
      || value.url !== `http://127.0.0.1:${8920 + index}/`) throw new PluginError("plugin_launch_rejected");
  return value.url;
}
export async function openCraftWindow(id: string, url: string): Promise<void> {
  if (isTauriDesktop()) await invoke("open_craft_browser", { appId: id });
  else {
    const opened = window.open(url, "_blank", "noopener,noreferrer");
    // Browsers may return null for noopener even when opening succeeded. Keep
    // the verified explicit link visible rather than claiming window success.
    if (opened) opened.opener = null;
  }
}
