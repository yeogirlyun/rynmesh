import { beforeEach, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import i18n from "../i18n";
import { fixtureSpace } from "../domain/space";
import { makeLiveNodeClient } from "../domain/liveNodeClient";
import { SpaceControls } from "./Space";
import { DevicesWorkspace } from "./drop/DevicesWorkspace";

const state = vi.hoisted(() => ({ personal: {} as Record<string, unknown> }));
vi.mock("./model", async (original) => ({
  ...(await original<Record<string, unknown>>()), usePersonal: () => state.personal,
}));
vi.mock("../appContext", () => ({ useAppContext: () => ({ node: { node_name: "Laptop" } }) }));
vi.mock("./dropStore", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  useDrop: () => ({ status: "ready", data: { peers: {} }, transfers: [] }),
}));
beforeEach(() => {
  state.personal = { space: { ...fixtureSpace(), name_rejected: true }, demo: false,
    devices: [], refresh: vi.fn(), spaceAction: vi.fn() };
});

it("T1 shows the rejected name directly on the devices page", async () => {
  await i18n.changeLanguage("zh-CN");
  render(<MemoryRouter><DevicesWorkspace /></MemoryRouter>);
  expect(screen.getByText("名字过长未同步")).toBeInTheDocument();
});

it.each([false, true])("T1 shows the rejected name on the device controls (compact=%s)", async (compact) => {
  await i18n.changeLanguage("zh-CN");
  render(<MemoryRouter><SpaceControls compact={compact} /></MemoryRouter>);
  expect(screen.getByText("名字过长未同步")).toBeInTheDocument();
});

it.each([["zh-CN", "名称长度须为 1 至 32 个字符。"], ["en", "Use a name between 1 and 32 characters."]])(
  "T9 translates the server name code through the HTTP client (%s)", async (lang, message) => {
    await i18n.changeLanguage(lang);
    server.use(http.post("*/api/local/space/create", () => HttpResponse.json({
      detail: "space_name_invalid",
    }, { status: 400 })));
    await expect(makeLiveNodeClient().spaceAction("create", { name: "" })).rejects.toThrow(message);
  },
);

it("T9 translates a code received in a pending operation response", async () => {
  await i18n.changeLanguage("zh-CN");
  state.personal.space = { ...fixtureSpace(), last_error: "space_name_invalid" };
  render(<MemoryRouter><SpaceControls /></MemoryRouter>);
  expect(screen.getByRole("alert")).toHaveTextContent("名称长度须为 1 至 32 个字符。");
});
