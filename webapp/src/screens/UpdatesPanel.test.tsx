import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { DesktopUpdatesPanel, PluginUpdatesPanel } from "./UpdatesPanel";
import { desktopUpdates, pluginUpdates } from "../domain/updates";
import { craftPlugins } from "../domain/craftPlugins";

vi.mock("../uiI18n", () => ({ useUILanguage: () => "en" }));
vi.mock("../domain/updates", () => ({
  desktopUpdates: { status: vi.fn(), check: vi.fn(), download: vi.fn(), install: vi.fn(), preferences: vi.fn() },
  pluginUpdates: { status: vi.fn(), configure: vi.fn(), action: vi.fn() },
}));
vi.mock("../domain/craftPlugins", () => ({ craftPlugins: { catalog: vi.fn() } }));
const ready = {
  currentVersion: "0.7.0", availableVersion: "0.8.0", notes: "Bug fixes", state: "ready",
  configured: true, autoDownload: true, downloaded: 100, total: 100, lastCheck: null,
  lastError: null, endpoint: "https://github.com/yeogirlyun/rynmesh/releases/latest/download/desktop-latest.json",
};
beforeEach(() => { vi.mocked(desktopUpdates.status).mockResolvedValue(ready); });
afterEach(() => { cleanup(); vi.resetAllMocks(); });

it("does not install a downloaded update until the owner confirms restart", async () => {
  vi.mocked(desktopUpdates.install).mockRejectedValue(new Error("update_tasks_running"));
  render(<DesktopUpdatesPanel />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Restart and update" }));
  expect(desktopUpdates.install).not.toHaveBeenCalled();
  expect(screen.getByRole("alertdialog")).toHaveTextContent("Save your documents");
  await user.click(screen.getByRole("button", { name: "Saved — install now" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("update_tasks_running");
  expect(desktopUpdates.install).toHaveBeenCalledTimes(1);
});

it("disables update checks if the signing key is missing", async () => {
  vi.mocked(desktopUpdates.status).mockResolvedValue({ ...ready, state: "idle", configured: false, availableVersion: null });
  render(<DesktopUpdatesPanel />);
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
