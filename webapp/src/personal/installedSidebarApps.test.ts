import { expect, it } from "vitest";
import { installedSidebarApps } from "./installedSidebarApps";
import { normalizePlugin } from "../domain/agentExtensions";
import type { CraftPlugin } from "../domain/craftPlugins";

it("shows installed applications on a fresh profile without manual pins", () => {
  const craft = ["pdfcraft", "wordcraft", "gridcraft", "deckcraft"].map(id => ({ id, installed: true, enabled: true } as CraftPlugin));
  const extensions = [normalizePlugin({ id: "company-dashboard", title: "Dashboard", enabled: true,
    web_app: { url: "http://127.0.0.1:18140/apps/company-dashboard", permissions: [] } })];
  const apps = installedSidebarApps(craft, extensions);
  expect(apps.map(app => app.title)).toEqual(["Gshopper Dashboard", "PDF", "Word", "Excel", "PPT"]);
  expect(apps[0].to).toBe("/apps/plugins/company-dashboard");
  expect(apps[1].to).toBe("/apps?open=pdfcraft");
});

it("does not advertise uninstalled or disabled editors", () => {
  const apps = installedSidebarApps([
    { id: "pdfcraft", installed: false, enabled: true },
    { id: "wordcraft", installed: true, enabled: false },
  ] as CraftPlugin[], [normalizePlugin({ id: "company-dashboard", title: "Dashboard", enabled: false })]);
  expect(apps).toEqual([]);
});
