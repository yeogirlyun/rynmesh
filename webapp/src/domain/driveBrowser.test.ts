import { afterEach, expect, it, vi } from "vitest";
import { invoke } from "@tauri-apps/api/core";
import { isTauriDesktop } from "./nodeUrl";
import { openDriveBrowser } from "./driveBrowser";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("./nodeUrl", () => ({ isTauriDesktop: vi.fn(() => false) }));
afterEach(() => { vi.restoreAllMocks(); vi.mocked(isTauriDesktop).mockReturnValue(false); });

it("uses the native browser command with the complete OAuth URL", async () => {
  vi.mocked(isTauriDesktop).mockReturnValue(true);
  vi.mocked(invoke).mockResolvedValue(undefined);
  const url = "https://accounts.google.com/o/oauth2/v2/auth?state=one&code_challenge=two";
  await openDriveBrowser(url);
  expect(invoke).toHaveBeenCalledWith("open_drive_browser", { url });
});
it("opens a web popup with no access to its opener", async () => {
  const popup = { opener: window, location: { replace: vi.fn() } };
  vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
  await openDriveBrowser("https://pan.baidu.com/union/console/applist");
  expect(popup.opener).toBeNull();
  expect(popup.location.replace).toHaveBeenCalledWith("https://pan.baidu.com/union/console/applist");
});
it.each(["https://microsoft.com/devicelogin", "https://login.microsoft.com/device", "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade"])("opens official Microsoft setup and device login: %s", async url => {
  vi.mocked(isTauriDesktop).mockReturnValue(true);
  vi.mocked(invoke).mockResolvedValue(undefined);
  await openDriveBrowser(url);
  expect(invoke).toHaveBeenCalledWith("open_drive_browser", { url });
});
it("reports blocked popups and native failures", async () => {
  vi.spyOn(window, "open").mockReturnValue(null);
  await expect(openDriveBrowser("https://openapi.baidu.com/device")).rejects.toThrow();
  vi.mocked(isTauriDesktop).mockReturnValue(true);
  vi.mocked(invoke).mockRejectedValue(new Error("launch failed"));
  await expect(openDriveBrowser("https://openapi.baidu.com/device")).rejects.toThrow("launch failed");
});
it.each(["file:///C:/Windows/cmd.exe", "http://accounts.google.com", "https://accounts.google.com.evil.example/", "https://login.microsoft.com.evil.example/device", "https://user@pan.baidu.com/"])("rejects unsupported targets: %s", async url => {
  await expect(openDriveBrowser(url)).rejects.toThrow("unsupported_drive_url");
});
