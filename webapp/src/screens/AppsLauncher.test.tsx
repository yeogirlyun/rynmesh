import { useState } from "react";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import i18n, { setLanguagePreference } from "../i18n";
import type { CraftCatalog, CraftPlugin } from "../domain/craftPlugins";
import AppsLauncher, { type AppsFilter } from "./AppsLauncher";
import AppIcon, { appTone } from "./appIcons";

const app = (id: string, group: CraftPlugin["group"], over: Partial<CraftPlugin> = {}): CraftPlugin => ({
  id, name: id, version: "v1", group, web: true, formats: [], sha256: null, source_url: "",
  installed: false, enabled: false, running: false, busy: false, agent_tools: [], editing_supported: false, warning: null, ...over,
});
const on = { installed: true, enabled: true, agent_tools: ["editor_info"] };
const catalog: CraftCatalog = {
  node_id: "n", setup_completed: true, defaults: [], selection: [], storage_path: "",
  plugins: [
    app("pdfcraft", "office", on),
    app("wordcraft", "office", { ...on, warning: "cjk_font_coverage" }),
    app("gridcraft", "office", { installed: true, enabled: false }),
    app("deckcraft", "office"),
    app("soundcraft", "media", on),
    app("effectcraft", "media"),
    app("filmcraft", "media", { web: false, installable: false }),
  ],
};
const text = (key: string, options?: Record<string, unknown>) => String(i18n.t(`craft.${key}`, options));
const handlers = () => ({ onOpen: vi.fn(), onInstall: vi.fn(), onAgent: vi.fn() });
type Props = Omit<React.ComponentProps<typeof AppsLauncher>, "filter" | "onFilterChange">;
// CraftApps owns the selected filter; this stands in for it.
function Parent(props: Props) {
  const [filter, setFilter] = useState<AppsFilter>("all");
  return <AppsLauncher {...props} filter={filter} onFilterChange={setFilter} />;
}
const mount = (props: Partial<Props> = {}) => {
  const fns = handlers();
  render(<MemoryRouter><Parent catalog={catalog} busy={false} text={text} {...fns} {...props} /></MemoryRouter>);
  return fns;
};
const card = (title: string) => within(screen.getByRole("heading", { name: title }).closest("article")!);

beforeEach(() => setLanguagePreference("en"));

it("separates what is installed from what is not, with counts", () => {
  mount();
  expect(screen.getByRole("heading", { name: "Apps" })).toBeInTheDocument();
  expect(screen.getByText("4 installed")).toBeInTheDocument();
  expect(screen.getByText("3 not installed")).toBeInTheDocument();
  // Native-only apps cannot be installed here, so nothing on this page claims they can.
  expect(screen.queryByText("3 available")).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: /Available to install/ })).not.toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Installed (4)" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Not installed (3)" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Manage plugins" })).toHaveAttribute("href", "/settings/plugins");
  expect(screen.getByRole("link", { name: "Friend apps & permissions" })).toHaveAttribute("href", "/apps/workspaces");
});

it("filters both sections by category and names each filter without its count", async () => {
  const user = userEvent.setup();
  mount();
  expect(screen.getByRole("button", { name: "All" })).toHaveAttribute("aria-pressed", "true");
  await user.click(screen.getByRole("button", { name: "Audio & video" }));
  expect(screen.getByRole("button", { name: "Audio & video" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("heading", { name: "Installed (1)" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Not installed (2)" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Open Audio editing" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: "Open PDF" })).not.toBeInTheDocument();
  // A category without installed apps simply has no Installed section.
  await user.click(screen.getByRole("button", { name: "Storage & files" }));
  expect(screen.queryByRole("heading", { name: /^Installed/ })).not.toBeInTheDocument();
});

it("takes the selected filter from its parent and only reports changes", async () => {
  const user = userEvent.setup();
  const onFilterChange = vi.fn();
  render(<MemoryRouter><AppsLauncher catalog={catalog} busy={false} text={text} {...handlers()} filter="media" onFilterChange={onFilterChange} /></MemoryRouter>);
  expect(screen.getByRole("button", { name: "Audio & video" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.queryByRole("button", { name: "Open PDF" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Office" }));
  expect(onFilterChange).toHaveBeenCalledWith("office");
  expect(screen.getByRole("button", { name: "Audio & video" })).toHaveAttribute("aria-pressed", "true");
});

it("opens, installs and starts Agent with the same actions as before", async () => {
  const user = userEvent.setup();
  const fns = mount();
  await user.click(card("PDF").getByRole("button", { name: "Open" }));
  expect(fns.onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: "pdfcraft" }));
  await user.click(screen.getByRole("button", { name: "Open Word documents" }));
  expect(fns.onOpen).toHaveBeenLastCalledWith(expect.objectContaining({ id: "wordcraft" }));
  await user.click(card("Word documents").getByRole("button", { name: "Agent" }));
  expect(fns.onAgent).toHaveBeenCalledWith(expect.objectContaining({ id: "wordcraft" }));
  await user.click(card("PPT presentations").getByRole("button", { name: "Install" }));
  expect(fns.onInstall).toHaveBeenCalledWith(expect.objectContaining({ id: "deckcraft" }));
  await user.click(screen.getByRole("button", { name: "Install Video effects" }));
  expect(fns.onInstall).toHaveBeenLastCalledWith(expect.objectContaining({ id: "effectcraft" }));
  expect(fns.onInstall).toHaveBeenCalledTimes(2);
});

it("shows a disabled app as off and cannot open it or start Agent", async () => {
  const user = userEvent.setup();
  const fns = mount();
  const excel = card("Excel spreadsheets");
  expect(excel.getByText("Disabled")).toBeInTheDocument();
  expect(excel.getByRole("button", { name: "Open" })).toBeDisabled();
  expect(excel.getByRole("button", { name: "Agent" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Open Excel spreadsheets" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Open Excel spreadsheets" }));
  expect(fns.onOpen).not.toHaveBeenCalled();
});

it("keeps Agent off for an app without Agent tools and flags font coverage", () => {
  mount({ catalog: { ...catalog, plugins: [app("pdfcraft", "office", { installed: true, enabled: true })] } });
  expect(card("PDF").getByRole("button", { name: "Agent" })).toBeDisabled();
  mount();
  expect(screen.getByText("Limited Chinese font support")).toBeInTheDocument();
});

it("does not offer to install an app that only exists natively", () => {
  mount();
  const film = card("Video editing");
  expect(film.getByText("Native only")).toBeInTheDocument();
  expect(film.queryByRole("button", { name: "Install" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Install Video editing" })).toBeDisabled();
});

it("blocks opening and installing while an operation is running", () => {
  mount({ busy: true });
  for (const button of screen.getAllByRole("button", { name: /^(Open|Install)( |$)/ })) expect(button).toBeDisabled();
  // Known, pre-existing behaviour kept as is: Agent is gated by the app's own state, not by `busy`.
  expect(card("PDF").getByRole("button", { name: "Agent" })).toBeEnabled();
});

it("follows the language setting", async () => {
  setLanguagePreference("zh-CN");
  mount();
  expect(screen.getByRole("heading", { name: "应用" })).toBeInTheDocument();
  expect(screen.getByText("已安装 4 个")).toBeInTheDocument();
  expect(screen.getByText("未安装 3 个")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "打开 Word 文档" })).toBeEnabled();
});

it("draws a different icon for every app and falls back to a lettered sheet", () => {
  const ids = ["pdfcraft", "wordcraft", "gridcraft", "deckcraft", "cadcraft", "designcraft", "figcraft", "lightcraft", "photocraft",
    "vectorcraft", "soundcraft", "effectcraft", "filmcraft", "resume", "recruit", "desk", "nas"];
  const shapes = ids.map(id => {
    const { container, unmount } = render(<AppIcon id={id} name={id} />);
    const svg = container.querySelector("svg")!;
    const drawn = [...svg.querySelectorAll("path")].map(path => path.getAttribute("d")).join("|") + svg.textContent;
    expect(svg).toHaveAttribute("aria-hidden", "true");
    expect(svg.querySelector("path"), id).not.toBeNull();
    unmount();
    return drawn;
  });
  expect(new Set(shapes).size).toBe(ids.length);
  const { container } = render(<AppIcon id="someday" name="Lab notes" />);
  expect(container.querySelector("text")).toHaveTextContent("L");
  expect(appTone("someday")).toBe("blue");
  expect(appTone("pdfcraft")).not.toBe(appTone("wordcraft"));
});
