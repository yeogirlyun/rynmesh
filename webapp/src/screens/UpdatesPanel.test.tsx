import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { DesktopUpdateButton, DesktopUpdateOverlay, DesktopUpdatesPanel, PluginUpdatesPanel } from "./UpdatesPanel";
import { desktopUpdates, pluginUpdates, serviceUpdates, type ServiceUpdateRow } from "../domain/updates";
import { craftPlugins } from "../domain/craftPlugins";

let mockLang = "en";
vi.mock("../uiI18n", () => ({ useUILanguage: () => mockLang }));
vi.mock("../domain/updates", async () => {
  const actual = await vi.importActual<typeof import("../domain/updates")>("../domain/updates");
  return {
    ...actual,
    desktopUpdates: { status: vi.fn(), check: vi.fn(), download: vi.fn(), install: vi.fn(), cancel: vi.fn(), acknowledge: vi.fn(), preferences: vi.fn() },
    pluginUpdates: { status: vi.fn(), configure: vi.fn(), action: vi.fn() },
    serviceUpdates: { status: vi.fn(), checkAll: vi.fn(), check: vi.fn(), apply: vi.fn(), verify: vi.fn(), retry: vi.fn(), acknowledge: vi.fn() },
  };
});
vi.mock("../domain/craftPlugins", () => ({ craftPlugins: { catalog: vi.fn() } }));

const ready = {
  currentVersion: "0.8.0", availableVersion: "0.9.0", notes: "Bug fixes", state: "ready",
  configured: true, autoDownload: true, installOnQuit: true, downloaded: 100, total: 100, lastCheck: null,
  lastError: null, failures: 0, endpoint: "https://github.com/yeogirlyun/rynmesh/releases/latest/download/desktop-latest.json",
};

const sampleService: ServiceUpdateRow = {
  id: "suite",
  name: { "zh-CN": "套件插件", en: "Suite plugins" },
  plugins: ["suite-a", "suite-b", "suite-c"],
  plugin_names: ["Workspace", "Dashboard", "Affiliate"],
  auth_provider: "suite-a",
  auth_label: { "zh-CN": "套件账号", en: "suite account" },
  publisher: { "zh-CN": "套件服务器", en: "the suite server" },
  state: "up_to_date",
  current_version: "2026.11.10",
  available_version: null,
  notes: null,
  min_host: null,
  host_version: "0.8.0",
  waiting_for: null,
  rolled_back_version: null,
  updated: null,
  downloaded: 0,
  total: null,
  last_error: null,
  last_check: 1700000000,
};

const routed = (node: ReactNode) => render(<MemoryRouter>{node}</MemoryRouter>);

beforeEach(() => {
  mockLang = "en";
  vi.mocked(desktopUpdates.status).mockResolvedValue(ready);
  vi.mocked(serviceUpdates.status).mockResolvedValue({ services: [] });
});
afterEach(() => { cleanup(); vi.resetAllMocks(); });

it("allows cancelling a check while its original command is still pending", async () => {
  const idle = { ...ready, state: "idle", availableVersion: null };
  vi.mocked(desktopUpdates.status).mockResolvedValue(idle);
  let finishCheck!: () => void;
  vi.mocked(desktopUpdates.check).mockImplementation(() => {
    vi.mocked(desktopUpdates.status).mockResolvedValue({ ...idle, state: "checking" });
    return new Promise(resolve => { finishCheck = () => resolve({ ...idle, state: "cancelled" }); });
  });
  vi.mocked(desktopUpdates.cancel).mockImplementation(async () => {
    vi.mocked(desktopUpdates.status).mockResolvedValue({ ...idle, state: "cancelled" });
    finishCheck();
    return { ...idle, state: "cancelled" };
  });
  routed(<DesktopUpdatesPanel />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Check for updates" }));
  await user.click(await screen.findByRole("button", { name: "Cancel check" }, { timeout: 3000 }));
  expect(desktopUpdates.cancel).toHaveBeenCalledTimes(1);
  expect(await screen.findByText("Check cancelled")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Check for updates" })).toBeEnabled();
});

it("explains the bounded timeout and lets the owner retry", async () => {
  vi.mocked(desktopUpdates.status).mockResolvedValue({ ...ready, state: "error", availableVersion: null, lastError: "update_check_timeout" });
  routed(<DesktopUpdatesPanel />);
  expect(await screen.findByText("Update check timed out after 20 seconds. Try again later.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Check for updates" })).toBeEnabled();
});

it("stays hidden while nothing needs the owner, even when a plugin pack is ready", async () => {
  vi.mocked(desktopUpdates.status).mockResolvedValue({ ...ready, state: "downloading", availableVersion: "0.9.0" });
  vi.mocked(serviceUpdates.status).mockResolvedValue({
    services: [{ ...sampleService, state: "ready", available_version: "2026.11.12" }],
  });
  const { container } = routed(<DesktopUpdateButton />);
  await waitFor(() => expect(desktopUpdates.status).toHaveBeenCalled());
  expect(container).toBeEmptyDOMElement();
});

it("installs a downloaded update from the Update button with one more click", async () => {
  vi.mocked(desktopUpdates.install).mockResolvedValue({ ...ready, state: "installing" });
  routed(<DesktopUpdateButton />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Ryn 0.9.0 is ready to install" }));
  const dialog = screen.getByRole("dialog");
  expect(dialog).toHaveTextContent("Bug fixes");
  expect(dialog).toHaveTextContent("installs the next time you quit Ryn");
  expect(desktopUpdates.install).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Restart to update" }));
  expect(desktopUpdates.install).toHaveBeenCalledWith();
  expect(screen.queryByRole("alertdialog")).toBeNull();
});

it("waits for running work and offers to update now, but never closes open documents", async () => {
  vi.mocked(desktopUpdates.status).mockResolvedValue({ ...ready, state: "waiting", installRequested: true, waitingFor: "agent_running" });
  routed(<DesktopUpdateButton />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Waiting" }));
  expect(screen.getByRole("dialog")).toHaveTextContent("Updates when the Agent task finishes");
  await user.click(screen.getByRole("button", { name: "Update now" }));
  expect(desktopUpdates.install).toHaveBeenCalledWith(true);
  cleanup();
  vi.mocked(desktopUpdates.status).mockResolvedValue({ ...ready, state: "waiting", installRequested: true, waitingFor: "editors_open" });
  routed(<DesktopUpdateButton />);
  await user.click(await screen.findByRole("button", { name: "Waiting" }));
  expect(screen.getByRole("dialog")).toHaveTextContent("Close open documents");
  expect(screen.queryByRole("button", { name: "Update now" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  expect(desktopUpdates.cancel).toHaveBeenCalledTimes(1);
});

it("shows background failures only once they keep happening, in plain words", async () => {
  vi.mocked(desktopUpdates.status).mockResolvedValue({ ...ready, state: "error", failures: 1, lastError: "error sending request for url" });
  const first = routed(<DesktopUpdateButton />);
  await waitFor(() => expect(desktopUpdates.status).toHaveBeenCalled());
  expect(first.container).toBeEmptyDOMElement();
  cleanup();
  vi.mocked(desktopUpdates.status).mockResolvedValue({ ...ready, state: "error", failures: 3, lastError: "error sending request for url" });
  routed(<DesktopUpdateButton />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Update failed" }));
  expect(screen.getByRole("dialog")).toHaveTextContent("Could not reach GitHub");
  await user.click(screen.getByRole("button", { name: "Retry" }));
  expect(desktopUpdates.check).toHaveBeenCalledTimes(1);
});

it("covers the window while installing and says so once after the update", async () => {
  vi.mocked(desktopUpdates.status).mockResolvedValue({ ...ready, state: "installing" });
  routed(<DesktopUpdateOverlay />);
  expect(await screen.findByText("Updating Ryn…")).toBeInTheDocument();
  cleanup();
  vi.mocked(desktopUpdates.status).mockResolvedValue({ ...ready, state: "up_to_date", currentVersion: "0.9.0", availableVersion: null, updatedFrom: "0.8.0" });
  vi.mocked(desktopUpdates.acknowledge).mockResolvedValue({ ...ready, updatedFrom: null });
  routed(<DesktopUpdateOverlay />);
  expect(await screen.findByText("Updated to Ryn 0.9.0")).toBeInTheDocument();
  await userEvent.setup().click(screen.getByRole("link", { name: "What's new" }));
  expect(desktopUpdates.acknowledge).toHaveBeenCalled();
});

it("shows a one-time bottom-right toast after a plugin service pack finishes updating", async () => {
  mockLang = "zh-CN";
  vi.mocked(desktopUpdates.status).mockResolvedValue({ ...ready, state: "up_to_date", availableVersion: null });
  vi.mocked(serviceUpdates.status).mockResolvedValue({
    services: [{ ...sampleService, current_version: "2026.11.12", updated: { version: "2026.11.12", notes: "New features", at: 10 } }],
  });
  vi.mocked(serviceUpdates.acknowledge).mockResolvedValue({ services: [{ ...sampleService, updated: null }] });
  routed(<DesktopUpdateOverlay />);
  expect(await screen.findByText("套件插件已更新到 2026.11.12")).toBeInTheDocument();
  await userEvent.setup().click(screen.getByRole("link", { name: "看看新内容" }));
  expect(serviceUpdates.acknowledge).toHaveBeenCalledWith("suite");
});

it("hides the Installed plugins section completely when no plugin update sources exist", async () => {
  vi.mocked(serviceUpdates.status).mockResolvedValue({ services: [] });
  routed(<DesktopUpdatesPanel />);
  expect(await screen.findByText("0.9.0 is downloaded; restart to update")).toBeInTheDocument();
  expect(screen.queryByText("Installed plugins")).toBeNull();
});

it("renders all seven plugin service update states and actions in Chinese and English", async () => {
  mockLang = "zh-CN";
  vi.mocked(desktopUpdates.status).mockResolvedValue({ ...ready, state: "up_to_date", availableVersion: null });
  const user = userEvent.setup();

  // 1. ready (with release notes and Update now)
  vi.mocked(serviceUpdates.status).mockResolvedValue({
    services: [{ ...sampleService, state: "ready", available_version: "2026.11.12", notes: "加快导出速度" }],
  });
  vi.mocked(serviceUpdates.apply).mockResolvedValue({
    services: [{ ...sampleService, state: "up_to_date", current_version: "2026.11.12" }],
  });
  routed(<DesktopUpdatesPanel />);
  expect(await screen.findByText("已安装的插件")).toBeInTheDocument();
  expect(screen.getByText("Workspace · Dashboard · Affiliate")).toBeInTheDocument();
  expect(screen.getByText("2026.11.12 已下载好，下次打开 Ryn 时自动生效")).toBeInTheDocument();
  expect(screen.getByText("加快导出速度")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "立即更新" }));
  expect(serviceUpdates.apply).toHaveBeenCalledWith("suite");
  cleanup();

  // 2. downloading (progress)
  vi.mocked(serviceUpdates.status).mockResolvedValue({
    services: [{ ...sampleService, state: "downloading", available_version: "2026.11.12", downloaded: 62, total: 100 }],
  });
  routed(<DesktopUpdatesPanel />);
  expect(await screen.findByText("正在下载 2026.11.12 · 只下载有变化的文件 · 62%")).toBeInTheDocument();
  expect(screen.getByRole("progressbar", { name: "插件下载进度" })).toHaveValue(62);
  cleanup();

  // 3. waiting (tasks running, owner can still click Update now)
  vi.mocked(serviceUpdates.status).mockResolvedValue({
    services: [{ ...sampleService, state: "waiting", available_version: "2026.11.12", waiting_for: "plugin_busy" }],
  });
  routed(<DesktopUpdatesPanel />);
  expect(await screen.findByText("2026.11.12 已下载好，等插件里的任务结束后自动更新")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "立即更新" })).toBeEnabled();
  cleanup();

  // 4. rolled_back
  vi.mocked(serviceUpdates.status).mockResolvedValue({
    services: [{ ...sampleService, state: "rolled_back", rolled_back_version: "2026.11.12", last_error: "service_update_unhealthy" }],
  });
  vi.mocked(serviceUpdates.retry).mockResolvedValue({ services: [sampleService] });
  routed(<DesktopUpdatesPanel />);
  expect(await screen.findByText("2026.11.12 没能正常启动，已自动退回 2026.11.10，插件可以照常使用")).toBeInTheDocument();
  expect(screen.queryByText(/service_update_unhealthy/)).toBeNull();
  await user.click(screen.getByRole("button", { name: "重试" }));
  expect(serviceUpdates.retry).toHaveBeenCalledWith("suite");
  cleanup();

  // 5. requires_host
  vi.mocked(serviceUpdates.status).mockResolvedValue({
    services: [{ ...sampleService, state: "requires_host", available_version: "2026.12.01", min_host: "0.9.0" }],
  });
  routed(<DesktopUpdatesPanel />);
  expect(await screen.findByText("2026.12.01 需要 Ryn 0.9.0 或更高版本，Ryn 更新后会自动安装")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "更新 Ryn" })).toBeEnabled();
  cleanup();

  // 6. login_required
  vi.mocked(serviceUpdates.status).mockResolvedValue({
    services: [{ ...sampleService, state: "login_required", last_error: "service_update_login_required" }],
  });
  routed(<DesktopUpdatesPanel />);
  expect(await screen.findByText("登录套件账号后才能检查更新，现有版本可以继续用")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "去登录" })).toHaveAttribute("href", "/apps/plugins/suite-a");
  cleanup();

  // 7. up_to_date & plain-word signature error in English
  mockLang = "en";
  vi.mocked(serviceUpdates.status).mockResolvedValue({
    services: [{ ...sampleService, state: "error", last_error: "service_update_bad_signature" }],
  });
  routed(<DesktopUpdatesPanel />);
  expect(await screen.findByText(/signature did not match/)).toBeInTheDocument();
  expect(screen.queryByText(/service_update_bad_signature/)).toBeNull();
});

it("restarts to update from Settings without a confirmation step", async () => {
  vi.mocked(desktopUpdates.install).mockResolvedValue({ ...ready, state: "installing" });
  routed(<DesktopUpdatesPanel />);
  const user = userEvent.setup();
  expect(await screen.findByText("0.9.0 is downloaded; restart to update")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Restart to update" }));
  expect(desktopUpdates.install).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("alertdialog")).toBeNull();
});

it("saves each automatic-update switch on its own", async () => {
  vi.mocked(desktopUpdates.preferences).mockResolvedValue({ ...ready, installOnQuit: false });
  routed(<DesktopUpdatesPanel />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("checkbox", { name: /Install when Ryn quits/ }));
  expect(desktopUpdates.preferences).toHaveBeenCalledWith({ installOnQuit: false });
  await user.click(screen.getByRole("checkbox", { name: /Download updates automatically/ }));
  expect(desktopUpdates.preferences).toHaveBeenCalledWith({ autoDownload: false });
});

it("disables update checks if the signing key is missing", async () => {
  vi.mocked(desktopUpdates.status).mockResolvedValue({ ...ready, state: "idle", configured: false, availableVersion: null });
  routed(<DesktopUpdatesPanel />);
  expect(await screen.findByRole("alert")).toHaveTextContent("signing key");
  expect(screen.getByRole("button", { name: "Check for updates" })).toBeDisabled();
});

it("saves a company HTTPS source only after an explicit configuration action", async () => {
  vi.mocked(pluginUpdates.status).mockResolvedValue({ plugins: [] });
  vi.mocked(pluginUpdates.configure).mockResolvedValue({ plugins: [] });
  vi.mocked(craftPlugins.catalog).mockResolvedValue({ plugins: [{ id: "wordcraft", name: "WordCraft", installed: true, web: true }] } as never);
  render(<PluginUpdatesPanel />);
  const user = userEvent.setup();
  await user.selectOptions(await screen.findByLabelText("Plugin"), "wordcraft");
  await user.selectOptions(screen.getByLabelText("Source"), "company");
  await user.type(screen.getByLabelText("Manifest URL"), "https://plugins.company.test/latest");
  await user.type(screen.getByLabelText("Publisher public key"), "publisher-key");
  expect(pluginUpdates.configure).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Save source" }));
  await waitFor(() => expect(pluginUpdates.configure).toHaveBeenCalledWith("wordcraft", expect.objectContaining({
    kind: "company", url: "https://plugins.company.test/latest", auto_update: false,
  })));
});
