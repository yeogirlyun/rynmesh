export const PIN_CHANGE_EVENT = "ryn-pinned-apps-changed";
const PIN_STORAGE_KEY = "ryn.sidebar.pinned.plugins";
export interface PinnedPluginWorkspace { id: string; title: string }

export const pluginWorkspacePath = (pluginId: string) => `/apps/plugins/${encodeURIComponent(pluginId)}`;

export function pinnedPluginWorkspaces(): PinnedPluginWorkspace[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(PIN_STORAGE_KEY) || "[]");
    if (!Array.isArray(value)) return [];
    const seen = new Set<string>();
    return value.slice(0, 100).filter((item): item is PinnedPluginWorkspace => {
      if (!item || typeof item !== "object" || typeof item.id !== "string" ||
          !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$/.test(item.id) || seen.has(item.id) ||
          typeof item.title !== "string" || !item.title.trim() || item.title.length > 256) return false;
      seen.add(item.id); return true;
    });
  } catch { return []; }
}

export const isPluginWorkspacePinned = (id: string) => pinnedPluginWorkspaces().some(item => item.id === id);

export function setPluginWorkspacePinned(plugin: PinnedPluginWorkspace, pinned: boolean) {
  try {
    const items = pinnedPluginWorkspaces().filter(item => item.id !== plugin.id);
    if (pinned) items.push({ id: plugin.id, title: plugin.title });
    localStorage.setItem(PIN_STORAGE_KEY, JSON.stringify(items));
    window.dispatchEvent(new CustomEvent(PIN_CHANGE_EVENT, { detail: { pluginId: plugin.id, pinned } }));
  } catch { /* Pinning is optional when browser storage is unavailable. */ }
}
