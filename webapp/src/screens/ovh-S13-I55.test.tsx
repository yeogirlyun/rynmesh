import { render, screen } from "@testing-library/react";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import i18n from "../i18n";
import { server } from "../test/server";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import type { AppOutletContext } from "../appContext";
import AISources from "./AISources";

beforeEach(async () => {
  await i18n.changeLanguage("zh-CN");
  server.use(
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [] })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json({ enabled: false, model_id: "" })),
  );
  server.use(http.get("*/api/local/llm/native", () => HttpResponse.json({ state: "not_installed", installed: false })));
});

it("T1 shows the recovery backup and re-add instruction from the real sources response", async () => {
  const backup = "sources.enc.corrupt-1000000000";
  server.use(http.get("*/api/local/llm/sources", () => HttpResponse.json({
    sources: [], node_name: "Test", peer_id: "self",
    storage_warning: { at: 1000000000, reason: "invalid_token", backup },
  })));
  const context = { client: makeFixtureNodeClient(), confirm: vi.fn(), notify: vi.fn() } as unknown as AppOutletContext;
  render(<MemoryRouter initialEntries={["/services/sources"]}><Routes><Route element={<Outlet context={context} />}>
    <Route path="/services/sources" element={<AISources />} />
  </Route></Routes></MemoryRouter>);
  const banner = await screen.findByRole("alert");
  expect(banner).toHaveTextContent(`模型来源配置无法读取，已备份到 ${backup}，请重新添加`);
});
