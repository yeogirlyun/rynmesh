import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { normalizePlugin, agentExtensions } from "../domain/agentExtensions";
import { APP_PINS_CHANGE_EVENT, getPins, putPins, type Pin, type PinsResult } from "../domain/apps";
import { craftPlugins, type CraftCatalog } from "../domain/craftPlugins";
import { nodeSites, type OpenedSite } from "../domain/nodeSites";
import { publicPlugins, type PublicCatalog } from "../domain/publicPlugins";
import i18n from "../i18n";
import { PersonalShell } from "./Shell";

vi.mock("./model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }),
}));
vi.mock("../domain/apps", async original => ({
  ...(await original<Record<string, unknown>>()), getPins: vi.fn(), putPins: vi.fn(),
}));

const dashboard: Pin = {
  key: "local:company-dashboard", kind: "local", app_id: "company-dashboard", title: "Gshopper Dashboard",
  open_path: "/apps/plugins/company-dashboard",
};
const imageStudio: Pin = {
  key: "local:image-studio", kind: "local", app_id: "image-studio", title: "做图", open_path: "/apps/app/public/image-studio",
};
const shared: Pin = {
  key: "shared:company|laptop|image-studio", kind: "shared", app_id: "image-studio", title: "做图",
  provider: { resource: "company", peer_id: "laptop", member_name: "小林" },
};
let stored: PinsResult;

beforeEach(async () => {
  await i18n.changeLanguage("zh-CN");
  stored = { seeded: true, pins: [] };
  vi.mocked(getPins).mockImplementation(async () => stored);
  vi.mocked(putPins).mockImplementation(async pins => (stored = { seeded: true, pins }));
  vi.spyOn(craftPlugins, "catalog").mockResolvedValue({ plugins: [
    { id: "gridcraft", installed: true, enabled: false },
  ] } as CraftCatalog);
  vi.spyOn(publicPlugins, "overview").mockResolvedValue({ plugins: [
    { id: "image-studio", installed_version: "1.0.0", enabled: true },
  ] } as PublicCatalog);
  vi.spyOn(agentExtensions, "overview").mockResolvedValue({ revision: 1, desktop_access: false, skills: [], mcp_servers: [], plugins: [
    normalizePlugin({ id: "company-dashboard", title: "Dashboard", enabled: true, web_app: { url: "/dashboard" } }),
    normalizePlugin({ id: "unrelated", title: "Other App", enabled: true, web_app: { url: "/other" } }),
  ] });
});

function CurrentPath() {
  const location = useLocation();
  return <output aria-label="当前地址">{location.pathname}{location.search}</output>;
}

function showShell() {
  render(<MemoryRouter><PersonalShell><CurrentPath /></PersonalShell></MemoryRouter>);
}

it("seeds only the existing company apps and removes the old shared pages link", async () => {
  stored = { seeded: false, pins: [] };
  showShell();
  expect(await screen.findByRole("link", { name: dashboard.title })).toHaveAttribute("href", dashboard.open_path);
  expect(putPins).toHaveBeenCalledExactlyOnceWith([dashboard]);
  expect(screen.getByText("置顶应用")).toBeVisible();
  expect(within(screen.getByRole("navigation", { name: "更多" })).queryByRole("link", { name: "开放的页面" })).toBeNull();
});

it("refreshes the sidebar on pin changes and when the window regains focus", async () => {
  showShell();
  await waitFor(() => expect(getPins).toHaveBeenCalled());
  stored = { seeded: true, pins: [imageStudio] };
  act(() => { window.dispatchEvent(new Event(APP_PINS_CHANGE_EVENT)); });
  expect(await screen.findByRole("link", { name: imageStudio.title })).toHaveAttribute("href", imageStudio.open_path);
  stored = { seeded: true, pins: [] };
  act(() => { window.dispatchEvent(new Event("focus")); });
  await waitFor(() => expect(screen.queryByRole("link", { name: imageStudio.title })).toBeNull());
  expect(putPins).not.toHaveBeenCalled();
});

it("labels shared pins with the colleague name and opens the selected device", async () => {
  stored = { seeded: true, pins: [shared] };
  const open = vi.spyOn(nodeSites, "open").mockResolvedValue({ key: "opened", entry: "/", title: "Image Studio" } as OpenedSite);
  showShell();
  await userEvent.setup().click(await screen.findByRole("link", { name: "做图 · 小林" }));
  expect(open).toHaveBeenCalledExactlyOnceWith({ provider_resource: "company", peer_id: "laptop" }, "image-studio");
  await waitFor(() => expect(screen.getByLabelText("当前地址")).toHaveTextContent("/apps/sites/open/opened?entry=%2F&title=Image+Studio"));
});

it("hides removed and disabled local apps without clearing their saved pins", async () => {
  const missing: Pin = { key: "local:missing", kind: "local", app_id: "missing", title: "Removed App", open_path: "/apps/app/public/missing" };
  const disabled: Pin = { key: "local:gridcraft", kind: "local", app_id: "gridcraft", title: "Disabled App", open_path: "/apps?open=gridcraft" };
  stored = { seeded: true, pins: [dashboard, missing, disabled] };
  showShell();
  await screen.findByRole("link", { name: dashboard.title });
  expect(screen.queryByRole("link", { name: missing.title })).toBeNull();
  expect(screen.queryByRole("link", { name: disabled.title })).toBeNull();
  expect(putPins).not.toHaveBeenCalled();
});
