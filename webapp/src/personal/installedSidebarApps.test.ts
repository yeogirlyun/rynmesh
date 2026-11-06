import { expect, it } from "vitest";
import { installedSidebarApps } from "./installedSidebarApps";
import { normalizePlugin } from "../domain/agentExtensions";
import type { CraftPlugin } from "../domain/craftPlugins";

it("shows company applications without adding installed editors to the sidebar", () => {
  const craft = ["pdfcraft", "wordcraft", "gridcraft", "deckcraft"].map(id => ({ id, installed: true, enabled: true } as CraftPlugin));
  const extensions = [normalizePlugin({ id: "company-dashboard", title: "Dashboard", enabled: true,
    web_app: { url: "http://127.0.0.1:18140/apps/company-dashboard", permissions: [] } })];
  const apps = installedSidebarApps(craft, extensions);
  expect(apps.map(app => app.title)).toEqual(["Gshopper Dashboard"]);
  expect(apps[0].to).toBe("/apps/plugins/company-dashboard");
});

it("does not advertise uninstalled or disabled editors", () => {
  const apps = installedSidebarApps([
    { id: "pdfcraft", installed: false, enabled: true },
    { id: "wordcraft", installed: true, enabled: false },
  ] as CraftPlugin[], [normalizePlugin({ id: "company-dashboard", title: "Dashboard", enabled: false })]);
  expect(apps).toEqual([]);
});
