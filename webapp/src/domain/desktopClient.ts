import { invoke } from "@tauri-apps/api/core";
export interface DesktopPreferences {
  background: boolean;
  startup: boolean;
  startup_supported: boolean;
}
export const getDesktopPreferences = () => invoke<DesktopPreferences>("get_desktop_preferences");
export const setDesktopPreferences = (background: boolean, startup: boolean) =>
  invoke<DesktopPreferences>("set_desktop_preferences", { background, startup });
