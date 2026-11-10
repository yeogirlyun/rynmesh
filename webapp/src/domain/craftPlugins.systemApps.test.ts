import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { invoke } from "@tauri-apps/api/core";
import { craftPlugins, systemApps } from "./craftPlugins";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));

const catalog = { node_id: "n", plugins: [], setup_completed: true, defaults: [], selection: [], storage_path: "C:/p" };
const desktop = () => Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });

beforeEach(() => {
  vi.mocked(invoke).mockReset().mockResolvedValue(undefined);
  vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify(catalog), { status: 200 }));
});
afterEach(() => {
  Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
  vi.restoreAllMocks();
});

it("syncs the Start menu and Open with entries after every plugin change on the desktop", async () => {
  desktop();
  await craftPlugins.install("gridcraft");
  await craftPlugins.enable("gridcraft", false);
  await craftPlugins.uninstall("gridcraft", false);
  await craftPlugins.setup();
  expect(vi.mocked(invoke).mock.calls).toEqual([["sync_system_apps"], ["sync_system_apps"], ["sync_system_apps"], ["sync_system_apps"]]);
  await craftPlugins.catalog();
  expect(invoke).toHaveBeenCalledTimes(4);
});

it("never syncs after a failed change or in a browser", async () => {
  desktop();
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ detail: "plugin_busy" }), { status: 409 }));
  await expect(craftPlugins.install("gridcraft")).rejects.toThrow("plugin_busy");
  Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
  await craftPlugins.install("gridcraft");
  expect(invoke).not.toHaveBeenCalled();
  await expect(systemApps.info("gridcraft")).resolves.toBeNull();
});

it("keeps a plugin change when the system sync fails", async () => {
  desktop();
  vi.mocked(invoke).mockRejectedValue(new Error("start_menu_failed"));
  await expect(craftPlugins.install("gridcraft")).resolves.toEqual(catalog);
});

it("reports a failed sync to whoever asked for it", async () => {
  desktop();
  vi.mocked(invoke).mockRejectedValue("gridcraft: start_menu_failed: denied");
  await expect(systemApps.sync()).rejects.toBe("gridcraft: start_menu_failed: denied");
});
