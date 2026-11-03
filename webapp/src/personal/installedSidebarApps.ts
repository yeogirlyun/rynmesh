import type { CraftPlugin } from "../domain/craftPlugins";
import type { ExtensionOverview } from "../domain/agentExtensions";
import { pluginWorkspacePath } from "../screens/pluginWorkspaceNavigation";

export type SidebarApp = { id: string; title: string; to: string };
const officeNames: Record<string, string> = { pdfcraft: "PDF", wordcraft: "Word", gridcraft: "Excel", deckcraft: "PPT" };

export function installedSidebarApps(craft: CraftPlugin[], extensions: ExtensionOverview["plugins"]): SidebarApp[] {
  return [
    ...extensions.filter(p => p.enabled && (p.web_app || p.views?.length || p.peer_provider)).map(p => ({
      id: p.id, title: p.id === "company-dashboard" ? "Gshopper Dashboard" : p.title,
      to: pluginWorkspacePath(p.id),
    })),
    ...craft.filter(p => p.installed && p.enabled).map(p => ({
      id: p.id, title: officeNames[p.id] || p.name, to: `/apps?open=${encodeURIComponent(p.id)}`,
    })),
  ];
}
