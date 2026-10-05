import { expect, it, vi } from "vitest";
import i18n from "./i18n";

it("synchronizes the resolved app language with the native tray without changing other preferences", async () => {
  const invoke = vi.fn(async () => undefined);
  vi.stubGlobal("__TAURI_INTERNALS__", { invoke });
  try {
    await i18n.changeLanguage("zh-CN");
    await vi.waitFor(() => expect(invoke).toHaveBeenLastCalledWith("set_desktop_language", { language: "zh-CN" }, undefined));
    await i18n.changeLanguage("en");
    await vi.waitFor(() => expect(invoke).toHaveBeenLastCalledWith("set_desktop_language", { language: "en" }, undefined));
  } finally { vi.unstubAllGlobals(); }
});
