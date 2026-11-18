import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { setLanguagePreference } from "../i18n";
import { DesktopPage } from "./Desktop";
import * as desktop from "../domain/desktopClient";
import { isTauriDesktop } from "../domain/nodeUrl";

vi.mock("../domain/desktopClient");
vi.mock("../domain/nodeUrl", () => ({ isTauriDesktop: vi.fn(() => false) }));

const basePrefs: desktop.DesktopPreferences = {
  background: true,
  startup: false,
  startup_supported: false,
  awake_supported: true,
  silent_start: true,
  auto_recover: true,
  keep_awake: false,
  close_notice_seen: false,
};

const baseStatus: desktop.DesktopStatus = {
  node_online: true,
  configured: true,
  sharing: true,
  active_tasks: 0,
  agent_active_runs: 1,
  agent_waiting: 0,
  agent_task_titles: ["Long task"],
  setup_active: false,
  recovery_attempts: 0,
  recovery: "healthy",
  awake_active: false,
  last_error: null,
};

beforeEach(() => {
  vi.resetAllMocks();
  setLanguagePreference("en");
  window.history.replaceState({}, "", "/settings/desktop");
  vi.mocked(isTauriDesktop).mockReturnValue(true);
  vi.mocked(desktop.getDesktopPreferences).mockResolvedValue(basePrefs);
  vi.mocked(desktop.getDesktopStatus).mockResolvedValue(baseStatus);
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
});

function renderDesktop() {
  return render(
    <MemoryRouter>
      <DesktopPage />
    </MemoryRouter>,
  );
}

describe("S02-I13 Desktop keep-awake toggle availability", () => {
  it("T3: enables keep-awake on non-Windows when awake_supported is true even though startup_supported is false", async () => {
    vi.mocked(desktop.setDesktopPreferences).mockImplementation(async next => next);

    renderDesktop();

    const startupToggle = await screen.findByRole("switch", { name: "Launch at sign-in" });
    const keepAwakeToggle = screen.getByRole("switch", { name: "Prevent idle sleep while sharing AI or running Agent tasks" });

    await waitFor(() => expect(keepAwakeToggle).toBeEnabled());
    expect(startupToggle).toBeDisabled();
    expect(keepAwakeToggle).toHaveAttribute("aria-checked", "false");

    fireEvent.click(keepAwakeToggle);

    await waitFor(() => expect(desktop.setDesktopPreferences).toHaveBeenCalledTimes(1));
    expect(vi.mocked(desktop.setDesktopPreferences).mock.calls[0]?.[0]).toMatchObject({
      startup_supported: false,
      awake_supported: true,
      keep_awake: true,
    });
    await waitFor(() => expect(keepAwakeToggle).toHaveAttribute("aria-checked", "true"));
  });

  it("T3: disables keep-awake when awake_supported is false regardless of startup_supported", async () => {
    const unsupportedPrefs: desktop.DesktopPreferences = {
      ...basePrefs,
      startup_supported: true,
      awake_supported: false,
    };
    vi.mocked(desktop.getDesktopPreferences).mockResolvedValue(unsupportedPrefs);

    renderDesktop();

    const startupToggle = await screen.findByRole("switch", { name: "Launch at sign-in" });
    const keepAwakeToggle = screen.getByRole("switch", { name: "Prevent idle sleep while sharing AI or running Agent tasks" });

    await waitFor(() => expect(startupToggle).toBeEnabled());
    expect(keepAwakeToggle).toBeDisabled();
  });
});
