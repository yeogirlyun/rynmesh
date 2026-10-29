import { expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import i18n from "../i18n";
import en from "../locales/drives.en.json";
import zh from "../locales/drives.zh-CN.json";
import { nasRequest } from "./nas";

it("keeps every nested drive language key and interpolation in sync", () => {
  function compare(a: Record<string, unknown>, b: Record<string, unknown>) {
    expect(Object.keys(a).sort()).toEqual(Object.keys(b).sort());
    for (const key of Object.keys(a)) {
      if (typeof a[key] === "string") {
        expect(typeof b[key]).toBe("string");
        expect((b[key] as string).trim()).not.toBe("");
        expect((a[key] as string).match(/{{\w+}}/g)?.sort()).toEqual((b[key] as string).match(/{{\w+}}/g)?.sort());
      } else compare(a[key] as Record<string, unknown>, b[key] as Record<string, unknown>);
    }
  }
  compare(en, zh);
});

it.each(["drive_download_failed", "nas_connection_failed", "drive_provider_error_31034", "unrecognized_error"])(
  "translates storage failure %s without leaking raw backend copy", async code => {
    server.use(http.get("*/api/local/plugins/nas", () => HttpResponse.json({ detail: code }, { status: 502 })));
    await i18n.changeLanguage("zh-CN");
    await expect(nasRequest()).rejects.toThrow(/[\p{Script=Han}]/u);
    await i18n.changeLanguage("en");
    try { await nasRequest(); } catch (error) {
      expect((error as Error).message).not.toMatch(/[\p{Script=Han}]/u);
      expect((error as Error).message).not.toContain(code);
    }
  },
);
