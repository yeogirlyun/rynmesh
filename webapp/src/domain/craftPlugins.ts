import { invoke } from "@tauri-apps/api/core";
import { isTauriDesktop, nodeControlUrl } from "./nodeUrl";

export type CraftPlugin = {
  id: string; name: string; version: string; group: "office" | "design" | "media" | "workflow" | "storage";
  kind?: "web_editor" | "builtin_adapter"; installable?: boolean; launch_path?: string | null;
  web: boolean; formats: string[]; sha256: string | null; source_url: string;
  installed: boolean; enabled: boolean; running: boolean; busy: boolean;
  agent_tools: string[]; editing_supported: boolean; warning: string | null;
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
export const craftPlugins = {
  catalog: () => request<CraftCatalog>(),
  setup: () => request<CraftCatalog>("/setup", {}),
  install: (id: string) => request<CraftCatalog>(`/${encodeURIComponent(id)}/install`, {}),
  enable: (id: string, enabled: boolean) => request<CraftCatalog>(`/${encodeURIComponent(id)}/enable`, { enabled }),
  uninstall: (id: string, clear_preferences: boolean) => request<CraftCatalog>(`/${encodeURIComponent(id)}/uninstall`, { clear_preferences }),
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
