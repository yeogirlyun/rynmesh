import type { CraftPlugin } from "../domain/craftPlugins";
import type { ExtensionOverview } from "../domain/agentExtensions";
import { pluginWorkspacePath } from "../screens/pluginWorkspaceNavigation";

export type SidebarApp = { id: string; title: string; to: string };
const companyApps = new Set(["company-dashboard", "company-workspace", "company-affiliate"]);

export function installedSidebarApps(_craft: CraftPlugin[], extensions: ExtensionOverview["plugins"]): SidebarApp[] {
  return [
    ...extensions.filter(p => companyApps.has(p.id) && p.enabled && (p.web_app || p.views?.length || p.peer_provider)).map(p => ({
      id: p.id, title: p.id === "company-dashboard" ? "Gshopper Dashboard" : p.title,
      to: pluginWorkspacePath(p.id),
    })),
  ];
}
