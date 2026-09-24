import { beforeEach, describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { readLanguagePreference, resolveLanguage, setLanguagePreference } from "../i18n";
import { LanguageSelect } from "./LanguageSelect";

beforeEach(() => {
  localStorage.clear();
  setLanguagePreference("en");
});

describe("app language", () => {
  it("uses simplified Chinese for Chinese system locales", () => {
    expect(resolveLanguage("system", "zh-CN")).toBe("zh-CN");
    expect(resolveLanguage("system", "zh-SG")).toBe("zh-CN");
    expect(resolveLanguage("system", "zh-TW")).toBe("zh-CN");
    expect(resolveLanguage("system", "en-US")).toBe("en");
  });

  it("switches language immediately and remembers the choice", () => {
    const { unmount } = render(<LanguageSelect />);
    fireEvent.click(screen.getByRole("button", { name: "App language" }));
    fireEvent.click(screen.getByRole("option", { name: "简体中文" }));
    expect(readLanguagePreference()).toBe("zh-CN");
    expect(document.documentElement.lang).toBe("zh-CN");
    expect(screen.getByRole("button", { name: "界面语言" })).toHaveTextContent("简体中文");
    unmount();
    render(<LanguageSelect />);
    expect(screen.getByRole("button", { name: "界面语言" })).toHaveTextContent("简体中文");
  });
});
