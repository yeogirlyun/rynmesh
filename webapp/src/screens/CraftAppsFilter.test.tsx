import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import CraftApps from "./CraftApps";
import { craftPlugins, type CraftCatalog, type CraftPlugin } from "../domain/craftPlugins";
import { setLanguagePreference } from "../i18n";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));

const app = (id: string, group: CraftPlugin["group"], over: Partial<CraftPlugin> = {}): CraftPlugin => ({
  id, name: id, version: "v1", group, web: true, formats: [], sha256: null, source_url: "",
  installed: false, enabled: false, running: false, busy: false, agent_tools: [], editing_supported: false, warning: null, ...over,
});
const builtin = (id: string, group: CraftPlugin["group"]) =>
  app(id, group, { kind: "builtin_adapter", installable: true, web: false, version: "1.0.0", launch_path: `/apps/workspaces?app=${id}` });
let state: CraftCatalog;

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
});
beforeEach(() => {
  setLanguagePreference("en");
  state = {
    node_id: "n", setup_completed: true, defaults: [], selection: [], storage_path: "",
    plugins: [
      app("pdfcraft", "office", { installed: true, enabled: true, agent_tools: ["editor_info"] }),
      app("wordcraft", "office"),
      builtin("resume", "workflow"),
      builtin("recruit", "workflow"),
      app("soundcraft", "media"),
    ],
  };
  vi.spyOn(craftPlugins, "catalog").mockImplementation(async () => structuredClone(state));
  vi.spyOn(craftPlugins, "install").mockImplementation(async id => {
    Object.assign(state.plugins.find(p => p.id === id)!, { installed: true, enabled: true });
    return structuredClone(state);
  });
});
const pressed = (name: string) => screen.getByRole("button", { name }).getAttribute("aria-pressed");

it("keeps the selected filter after the install flow returns to the Apps page", async () => {
  const user = userEvent.setup();
  render(<MemoryRouter><CraftApps /></MemoryRouter>);
  await user.click(await screen.findByRole("button", { name: "Business apps" }));
  expect(pressed("Business apps")).toBe("true");
  await user.click(screen.getByRole("button", { name: "Install Resume & cover letters" }));
  // The installer replaces the page; going back must not reset the view to "All".
  await user.click(await screen.findByRole("button", { name: "Back" }));
  expect(await screen.findByRole("heading", { name: "Apps" })).toBeInTheDocument();
  expect(pressed("Business apps")).toBe("true");
  expect(pressed("All")).toBe("false");
  expect(screen.getByRole("button", { name: "Install Recruiting" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Install Word documents" })).not.toBeInTheDocument();
  expect(craftPlugins.install).not.toHaveBeenCalled();
});

it("keeps the selected filter after an app is installed from the flow", async () => {
  const user = userEvent.setup();
  render(<MemoryRouter><CraftApps /></MemoryRouter>);
  await user.click(await screen.findByRole("button", { name: "Business apps" }));
  await user.click(screen.getByRole("button", { name: "Install Resume & cover letters" }));
  await user.click(await screen.findByRole("button", { name: "Add plugins" }));
  expect(await screen.findByRole("button", { name: "Open Resume & cover letters" })).toBeEnabled();
  expect(craftPlugins.install).toHaveBeenCalledWith("resume");
  expect(pressed("Business apps")).toBe("true");
  expect(screen.queryByRole("button", { name: "Open PDF" })).not.toBeInTheDocument();
});
