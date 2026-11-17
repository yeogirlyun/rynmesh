import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { makeLiveNodeClient } from "../domain/liveNodeClient";
import i18n from "../i18n";
import MyContent from "./MyContent";

afterEach(async () => { cleanup(); vi.unstubAllGlobals(); await i18n.changeLanguage("en"); });

it.each(["en", "zh-CN"])("S13-I59 shows actionable encoding failure through the live client (%s)", async language => {
  await i18n.changeLanguage(language);
  const fixture = makeFixtureNodeClient();
  const live = makeLiveNodeClient("http://node.test/api/local");
  const notify = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "library_import_encoding_unknown" }), { status: 400 })));
  const context = { client: { ...fixture, importLibraryFile: live.importLibraryFile }, notify, confirm: vi.fn() };
  render(<MemoryRouter><Routes><Route element={<Outlet context={context} />}><Route path="/" element={<MyContent />} /></Route></Routes></MemoryRouter>);
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  await userEvent.upload(input, new File(["broken"], "broken.txt", { type: "text/plain" }));
  await waitFor(() => expect(notify).toHaveBeenCalledWith("danger", language === "en"
    ? "The file encoding could not be recognized. Save it as UTF-8 and import it again."
    : "无法识别文件编码。请另存为 UTF-8 后重新导入。"));
});

it("S13-I59 offers Office and CSV selection", () => {
  const context = { client: makeFixtureNodeClient(), notify: vi.fn(), confirm: vi.fn() };
  render(<MemoryRouter><Routes><Route element={<Outlet context={context} />}><Route path="/" element={<MyContent />} /></Route></Routes></MemoryRouter>);
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  for (const suffix of [".docx", ".xlsx", ".pptx", ".csv"]) expect(input.accept).toContain(suffix);
});
