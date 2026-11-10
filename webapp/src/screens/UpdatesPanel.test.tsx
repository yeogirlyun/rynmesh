import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { DesktopUpdateButton, DesktopUpdateOverlay, DesktopUpdatesPanel, PluginUpdatesPanel } from "./UpdatesPanel";
import { desktopUpdates, pluginUpdates } from "../domain/updates";
import { craftPlugins } from "../domain/craftPlugins";

vi.mock("../uiI18n", () => ({ useUILanguage: () => "en" }));
vi.mock("../domain/updates", () => ({
  VISIBLE_UPDATE_FAILURES: 3,
  desktopUpdates: { status: vi.fn(), check: vi.fn(), download: vi.fn(), install: vi.fn(), cancel: vi.fn(), acknowledge: vi.fn(), preferences: vi.fn() },
  pluginUpdates: { status: vi.fn(), configure: vi.fn(), action: vi.fn() },
}));
vi.mock("../domain/craftPlugins", () => ({ craftPlugins: { catalog: vi.fn() } }));
const ready = {
  currentVersion: "0.8.0", availableVersion: "0.9.0", notes: "Bug fixes", state: "ready",
  configured: true, autoDownload: true, installOnQuit: true, downloaded: 100, total: 100, lastCheck: null,
  lastError: null, failures: 0, endpoint: "https://github.com/yeogirlyun/rynmesh/releases/latest/download/desktop-latest.json",
};
const routed = (node: ReactNode) => render(<MemoryRouter>{node}</MemoryRouter>);
beforeEach(() => { vi.mocked(desktopUpdates.status).mockResolvedValue(ready); });
afterEach(() => { cleanup(); vi.resetAllMocks(); });

it("stays hidden while nothing needs the owner", async () => {
  vi.mocked(desktopUpdates.status).mockResolvedValue({ ...ready, state: "downloading", availableVersion: "0.9.0" });
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
