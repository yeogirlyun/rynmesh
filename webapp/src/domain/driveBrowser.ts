import { invoke } from "@tauri-apps/api/core";
import { isTauriDesktop } from "./nodeUrl";

export async function openDriveBrowser(url: string): Promise<void> {
  const parsed = new URL(url);
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.port ||
      !["console.cloud.google.com", "accounts.google.com", "pan.baidu.com", "openapi.baidu.com", "entra.microsoft.com", "login.microsoftonline.com", "login.microsoft.com", "microsoft.com", "www.microsoft.com"].includes(parsed.hostname)) {
    throw new Error("unsupported_drive_url");
  }
  if (isTauriDesktop()) {
    await invoke("open_drive_browser", { url });
    return;
  }
  // Open synchronously in the click handler so browser popup rules can allow it.
  const popup = window.open("about:blank", "_blank");
  if (!popup) throw new Error("browser_open_failed");
  popup.opener = null;
  popup.location.replace(url);
}
