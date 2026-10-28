import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useTranslation } from "react-i18next";
import { invoke } from "@tauri-apps/api/core";
import CraftSystemApp from "./CraftSystemApp";
import { setLanguagePreference } from "../i18n";
import type { CraftPlugin, SystemApp } from "../domain/craftPlugins";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));

const excel: CraftPlugin = {
  id: "gridcraft", name: "GridCraft", version: "v0.6.1", group: "office", web: true, formats: ["xlsx", "csv"],
  sha256: "a".repeat(64), source_url: "https://github.com/storytold/gridcraft", installed: true, enabled: true,
  running: false, busy: false, agent_tools: [], editing_supported: false, warning: null,
};
const windows: SystemApp = {
  name: "Ryn Excel", documents: [".xlsx", ".xls", ".ods", ".csv"], place: "start_menu", default_choice: "settings", state: "ready", error: null,
};

function Details({ app }: { app: CraftPlugin }) {
  const { t } = useTranslation();
  return <CraftSystemApp app={app} text={(key, options) => String(t(`craft.${key}`, options))} />;
}
/** Answers the desktop commands: `infos` in turn (the last one repeats), sync and the default choice. */
function desktop({ infos, sync = async () => undefined, choose = async () => undefined }: {
  infos: (SystemApp | null)[]; sync?: () => Promise<void>; choose?: () => Promise<void>;
}) {
  let next = 0;
  vi.mocked(invoke).mockImplementation(async (command: string) => {
    if (command === "system_app_info") return infos[Math.min(next++, infos.length - 1)];
    if (command === "sync_system_apps") return sync();
    return choose();
  });
}
const calls = (command: string) => vi.mocked(invoke).mock.calls.filter(([name]) => name === command).length;

beforeEach(() => {
  Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
  vi.mocked(invoke).mockReset();
  setLanguagePreference("en");
});
afterEach(() => Reflect.deleteProperty(window, "__TAURI_INTERNALS__"));

it("shows where the app lives and lets Windows users pick it as default in Settings", async () => {
  desktop({ infos: [windows] });
  const user = userEvent.setup();
  render(<Details app={excel} />);
  expect(await screen.findByText("Ryn Excel is in the Start menu and opens on its own, without the Ryn window.")).toBeInTheDocument();
  expect(screen.getByText(/offered under “Open with” for \.xlsx \.xls \.ods \.csv files/)).toBeInTheDocument();
  expect(invoke).toHaveBeenCalledWith("system_app_info", { appId: "gridcraft" });
  await user.click(screen.getByRole("button", { name: "Choose as default…" }));
  expect(invoke).toHaveBeenLastCalledWith("choose_default_system_app", { appId: "gridcraft" });
  expect(await screen.findByRole("status")).toHaveTextContent("Windows Settings is open. Choose Ryn Excel for the file types you want.");
});

it("sets the default directly on macOS and reports a failure", async () => {
  desktop({ infos: [{ ...windows, place: "applications", default_choice: "direct" }], choose: async () => { throw new Error("default_app_failed"); } });
  const user = userEvent.setup();
  setLanguagePreference("zh-CN");
  render(<Details app={excel} />);
  expect(await screen.findByText("Ryn Excel 已在「应用程序」和启动台中，可直接打开，不显示 Ryn 主界面。")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "设为默认打开方式" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("无法更改默认应用");
});

it("never claims the app is there while the sync is still running", async () => {
  desktop({ infos: [{ ...windows, state: "pending" }, windows] });
  render(<Details app={excel} />);
  expect(await screen.findByRole("status")).toHaveTextContent("Adding Ryn Excel to the Start menu…");
  expect(screen.queryByText(/is in the Start menu/)).toBeNull();
  expect(screen.queryByRole("button")).toBeNull();
  // It looks again until the sync has reported.
  expect(await screen.findByText(/Ryn Excel is in the Start menu/, undefined, { timeout: 3000 })).toBeInTheDocument();
  expect(calls("system_app_info")).toBe(2);
});

it("shows why adding failed and adds the app again on retry", async () => {
  desktop({ infos: [{ ...windows, state: "failed", error: "start_menu_failed: Access is denied." }, windows] });
  const user = userEvent.setup();
  render(<Details app={excel} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Ryn Excel could not be added to the Start menu.");
  expect(screen.getByText("Error: start_menu_failed: Access is denied.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Choose as default…" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "Try again" }));
  expect(calls("sync_system_apps")).toBe(1);
  expect(await screen.findByText(/Ryn Excel is in the Start menu/)).toBeInTheDocument();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("keeps the failure and the retry when trying again fails too", async () => {
  const failed: SystemApp = { ...windows, state: "failed", error: "file_types_failed: denied" };
  desktop({ infos: [failed], sync: async () => { throw "gridcraft: file_types_failed: denied"; } });
  const user = userEvent.setup();
  setLanguagePreference("zh-CN");
  render(<Details app={excel} />);
  await user.click(await screen.findByRole("button", { name: "重试" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "重试" })).toBeEnabled());
  expect(screen.getByRole("alert")).toHaveTextContent("没能把 Ryn Excel 添加到开始菜单。");
  expect(screen.getByText("错误：file_types_failed: denied")).toBeInTheDocument();
  expect(calls("sync_system_apps")).toBe(1);
});

it("offers to add an app that is not on the computer yet", async () => {
  desktop({ infos: [{ ...windows, state: "missing" }, { ...windows, state: "failed", error: "system_apps_disabled_in_development" }],
    sync: async () => { throw "system_apps_disabled_in_development"; } });
  const user = userEvent.setup();
  render(<Details app={excel} />);
  expect(await screen.findByText("Ryn Excel is not in the Start menu yet.")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Add to the Start menu" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Ryn Excel could not be added to the Start menu.");
  expect(screen.getByText("Error: system_apps_disabled_in_development")).toBeInTheDocument();
});

it("stays hidden for apps that are not on the computer", async () => {
  desktop({ infos: [windows] });
  const { container, rerender } = render(<Details app={{ ...excel, enabled: false }} />);
  rerender(<Details app={{ ...excel, id: "resume", web: false }} />);
  desktop({ infos: [null] });
  rerender(<Details app={{ ...excel, id: "cadcraft" }} />);
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("system_app_info", { appId: "cadcraft" }));
  expect(container).toBeEmptyDOMElement();
  expect(invoke).toHaveBeenCalledTimes(1);
});

it("hides the default choice for an app without documents", async () => {
  desktop({ infos: [{ ...windows, name: "Ryn CAD", documents: [] }] });
  render(<Details app={{ ...excel, id: "cadcraft" }} />);
  expect(await screen.findByText(/Ryn CAD is in the Start menu/)).toBeInTheDocument();
  expect(screen.queryByRole("button")).toBeNull();
});
