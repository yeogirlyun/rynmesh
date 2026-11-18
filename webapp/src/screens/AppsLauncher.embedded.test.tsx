import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import type { CraftCatalog, CraftPlugin } from "../domain/craftPlugins";
import AppsLauncher from "./AppsLauncher";

function renderLauncher(embedded: boolean, installedCount = 1) {
  const app: CraftPlugin = {
    id: "pdfcraft", name: "PDF", version: "v1", group: "office", web: true, formats: [], sha256: null, source_url: "",
    installed: true, enabled: true, running: false, busy: false, agent_tools: ["editor"], editing_supported: false, warning: null,
  };
  const catalog: CraftCatalog = {
    node_id: "node", setup_completed: true, defaults: [], selection: [], storage_path: "",
    plugins: [app, ...Array.from({ length: installedCount - 1 }, (_, i) => ({ ...app, id: "office-" + i })),
      { ...app, id: "deckcraft", installed: false }],
  };
  const onOpen = vi.fn(), onAgent = vi.fn();
  render(<MemoryRouter><AppsLauncher embedded={embedded} catalog={catalog} busy={false} filter="all"
    onFilterChange={vi.fn()} onOpen={onOpen} onAgent={onAgent} onInstall={vi.fn()} text={key => key} /></MemoryRouter>);
  return { onOpen, onAgent, app };
}

it("embeds only installed cards and keeps their open and Agent actions", async () => {
  const { onOpen, onAgent, app } = renderLauncher(true);
  expect(screen.queryByRole("heading", { level: 1 })).toBeNull();
  expect(screen.queryByRole("link", { name: "manage" })).toBeNull();
  expect(screen.queryByRole("navigation")).toBeNull();
  expect(screen.queryByRole("heading", { name: "deckcraft" })).toBeNull();
  const card = within(screen.getByRole("article"));
  await userEvent.click(card.getByRole("button", { name: /^open$/ }));
  await userEvent.click(card.getByRole("button", { name: "agent" }));
  expect(onOpen).toHaveBeenCalledWith(app);
  expect(onAgent).toHaveBeenCalledWith(app);
});

it.each([7, 8])("uses the number of installed apps for the filter threshold (%i)", count => {
  renderLauncher(true, count);
  expect(Boolean(screen.queryByRole("navigation"))).toBe(count >= 8);
});

it("keeps the standalone heading, management link, filters and installable apps", () => {
  renderLauncher(false);
  expect(screen.getByRole("heading", { level: 1, name: "title" })).toBeVisible();
  expect(screen.getByRole("link", { name: "manage" })).toHaveAttribute("href", "/settings/plugins");
  expect(screen.getByRole("navigation")).toBeVisible();
  expect(screen.getByRole("heading", { name: "deckcraft" })).toBeVisible();
});
