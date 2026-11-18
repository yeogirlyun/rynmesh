import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { agentExtensions, normalizePlugin } from "../domain/agentExtensions";
import { craftPlugins, type CraftCatalog } from "../domain/craftPlugins";
import { publicPlugins, type PublicCatalog, type PublicPlugin } from "../domain/publicPlugins";
import AppsDiscover from "./AppsDiscover";

const studio: PublicPlugin = {
  id: "image-studio", name: "Image Studio", version: "1.1.0", installed_version: "1.0.0",
  description: "", notes: "", source: "public", enabled: true, running: true,
  update_available: true, rollback_available: false, error: "", permissions: [],
};
const extra: PublicPlugin = { ...studio, id: "sample-app", name: "Sample App", installed_version: null,
  update_available: false, enabled: false, running: false };
const publicCatalog: PublicCatalog = { plugins: [studio, extra], checked_at: null, error: "", source: "public", legacy_data: false };
const craftCatalog: CraftCatalog = {
  node_id: "sample-node", defaults: [], setup_completed: true, selection: [], storage_path: "",
  plugins: [{
    id: "wordcraft", name: "WordCraft", version: "1.0.0", group: "office", web: true,
    formats: ["docx"], sha256: "", source_url: "", installed: true, enabled: true, running: false,
    busy: false, agent_tools: [], editing_supported: false, warning: null,
  }],
};

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(craftPlugins, "catalog").mockResolvedValue(craftCatalog);
  vi.spyOn(agentExtensions, "overview").mockResolvedValue({
    revision: 1, desktop_access: false, skills: [], mcp_servers: [],
    plugins: [normalizePlugin({
      id: "company-dashboard", title: "Company Dashboard", builtin: false,
      enabled: true, web_app: { url: "https://company.test/", permissions: [] },
    })],
  });
  vi.spyOn(publicPlugins, "overview").mockResolvedValue(publicCatalog);
  vi.spyOn(publicPlugins, "action").mockResolvedValue(publicCatalog);
  vi.spyOn(publicPlugins, "refresh").mockResolvedValue(publicCatalog);
});

const show = () => render(<MemoryRouter><AppsDiscover /></MemoryRouter>);

it("shows the available update above the public catalog and links to its version tab", async () => {
  show();
  const notice = await screen.findByRole("complementary", { name: "App updates" });
  expect(notice).toHaveTextContent("1 app has updates: Image Studio 1.1.0");
  expect(within(notice).getByRole("link", { name: "Review updates" }))
    .toHaveAttribute("href", "/apps/app/public/image-studio?tab=version");
  expect(screen.getByRole("region", { name: "Public apps" })).toHaveTextContent("Sample App");
  expect(publicPlugins.overview).toHaveBeenCalledOnce();
  expect(publicPlugins.refresh).not.toHaveBeenCalled();
});

it("combines installed public and office counts and keeps company apps in their own row", async () => {
  show();
  expect(await screen.findByRole("button", { name: "Installed 2" })).toHaveAttribute("aria-pressed", "true");
  const company = await screen.findByRole("region", { name: "Company apps" });
  expect(company).toHaveTextContent("Company Dashboard");
  expect(company).toHaveTextContent("Installed · Automatic updates");
  expect(within(company).getByRole("link", { name: "Software updates" })).toHaveAttribute("href", "/settings/updates");
  expect(screen.getByRole("region", { name: "Public apps" })).not.toHaveTextContent("Company Dashboard");
});

it("recalculates the installed total after confirmed public installation", async () => {
  vi.mocked(publicPlugins.action).mockResolvedValue({
    ...publicCatalog, plugins: [studio, { ...extra, installed_version: extra.version }],
  });
  show();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Install locally" }));
  expect(publicPlugins.action).not.toHaveBeenCalled();
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Confirm" }));
  expect(publicPlugins.action).toHaveBeenCalledWith("sample-app", "install");
  expect(await screen.findByRole("button", { name: "Installed 3" })).toBeInTheDocument();
});

it("removes the notice when a catalog action leaves no available updates", async () => {
  vi.mocked(publicPlugins.action).mockResolvedValue({
    ...publicCatalog, plugins: [{ ...studio, installed_version: studio.version, update_available: false }, extra],
  });
  show();
  const user = userEvent.setup();
  expect(await screen.findByRole("complementary", { name: "App updates" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Update to 1.1.0" }));
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Confirm" }));
  await waitFor(() => expect(screen.queryByRole("complementary", { name: "App updates" })).not.toBeInTheDocument());
});
