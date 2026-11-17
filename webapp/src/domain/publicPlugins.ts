import { nodeControlUrl } from "./nodeUrl";

export interface PublicPlugin {
  id: string; name: string; description: string; version: string; notes: string; source: string;
  installed_version: string | null; enabled: boolean; running: boolean;
  update_available: boolean; rollback_available: boolean; error: string; permissions: string[];
}
export interface PublicCatalog {
  plugins: PublicPlugin[]; checked_at: number | null; error: string; source: string; legacy_data: boolean;
}
async function call<T>(path = "", method = "GET"): Promise<T> {
  const response = await fetch(nodeControlUrl(`/public-plugins${path}`), { method, credentials: "include", cache: "no-store" });
  const value = await response.json();
  if (!response.ok) throw new Error(value.code || "plugin_download_failed");
  return value as T;
}
export const publicPlugins = {
  overview: () => call<PublicCatalog>(),
  refresh: () => call<PublicCatalog>("/refresh", "POST"),
  action: (id: string, action: string) => call<PublicCatalog>(`/${encodeURIComponent(id)}/${action}`, "POST"),
  open: (id: string) => call<{ url: string; name: string }>(`/${encodeURIComponent(id)}/open`, "POST"),
};
