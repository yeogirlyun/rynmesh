import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import i18n from "../i18n";
import { PersonalShell } from "./Shell";

// First-use state deliberately has no devices, configured services or NAS.
vi.mock("./model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }),
}));
vi.mock("../domain/nas", () => ({ useNasStatus: () => ({ status: { enabled: false } }) }));

const destinations = [
  ["我的服务", "/services"], ["浏览服务", "/services/catalog"],
  ["AI 工作台", "/services/private-ai/chat"], ["AI 来源", "/services/sources"],
  ["API 接入", "/services/api"], ["服务设置", "/services/manage"],
];
function Location() { return <output aria-label="当前路径">{useLocation().pathname}</output>; }
beforeEach(async () => { await i18n.changeLanguage("zh-CN"); });

it("exposes every service destination before setup and preserves the same navigation after each click", async () => {
  render(<MemoryRouter initialEntries={["/services"]}><PersonalShell><Location /></PersonalShell></MemoryRouter>);
  const user = userEvent.setup();
  for (const [label, path] of destinations) {
    const nav = within(screen.getByRole("navigation", { name: "服务" }));
    expect(nav.getAllByRole("link").map(link => link.textContent)).toEqual(destinations.map(([name]) => name));
    await user.click(nav.getByRole("link", { name: label }));
    expect(screen.getByLabelText("当前路径")).toHaveTextContent(path);
    expect(nav.getByRole("link", { name: label })).toHaveAttribute("aria-current", "page");
  }
});

it.each(["/services/model-mapping", "/services/agent-sharing", "/services/video-rendering", "/services/secure-web-access"])(
  "keeps all entry points available on existing detail route %s", path => {
    render(<MemoryRouter initialEntries={[path]}><PersonalShell><Location /></PersonalShell></MemoryRouter>);
    const nav = within(screen.getByRole("navigation", { name: "服务" }));
    for (const [label, href] of destinations) expect(nav.getByRole("link", { name: label })).toHaveAttribute("href", href);
  },
);
