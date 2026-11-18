import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { agentExtensions, normalizePlugin } from "../domain/agentExtensions";
import { getPins, loadMyApps, sharedApps, togglePin, type Pin } from "../domain/apps";
import { craftPlugins } from "../domain/craftPlugins";
import AppsHub from "./AppsHub";
import { EmbeddedAppsHub } from "./CraftApps";

vi.mock("../domain/apps", async original => ({
  ...await original<typeof import("../domain/apps")>(),
  getPins: vi.fn(), loadMyApps: vi.fn(), sharedApps: vi.fn(), togglePin: vi.fn(),
}));

const company = "Company Dashboard";
const office = "Word documents";
const renderer = () => render(<MemoryRouter initialEntries={["/apps"]}>
  <EmbeddedAppsHub.Provider value><AppsHub /></EmbeddedAppsHub.Provider>
</MemoryRouter>);

beforeEach(() => {
  vi.spyOn(craftPlugins, "catalog").mockResolvedValue({
    node_id: "sample-node", defaults: [], setup_completed: true, selection: [], storage_path: "",
    plugins: [{
      id: "wordcraft", name: "WordCraft", version: "1.0.0", group: "office", web: true,
      formats: ["docx"], sha256: "", source_url: "", installed: true, enabled: true, running: false,
      busy: false, agent_tools: [], editing_supported: false, warning: null,
    }],
  });
  vi.spyOn(agentExtensions, "overview").mockResolvedValue({
    revision: 1, desktop_access: false, skills: [], mcp_servers: [],
    plugins: [normalizePlugin({ id: "dashboard", title: company, builtin: false, enabled: true })],
  });
  vi.mocked(loadMyApps).mockResolvedValue([]);
  vi.mocked(sharedApps).mockResolvedValue({ colleagues_total: 0, sharing_total: 0, apps: [] });
  vi.mocked(getPins).mockResolvedValue({ seeded: true, pins: [] });
  let pins: Pin[] = [];
  vi.mocked(togglePin).mockImplementation(async pin => {
    pins = pins.some(item => item.key === pin.key) ? pins.filter(item => item.key !== pin.key) : [...pins, pin];
    return { seeded: true, pins };
  });
});

it("uses the same app pin action for office and company cards", async () => {
  renderer();
  const user = userEvent.setup();
  const installed = within(screen.getByRole("region", { name: "Installed on this device" }));
  for (const [id, title] of [["dashboard", company], ["wordcraft", office]]) {
    const star = await installed.findByRole("button", { name: `Pin ${title}` });
    expect(star).toHaveAttribute("aria-pressed", "false");
    await user.click(star);
    expect(togglePin).toHaveBeenLastCalledWith(expect.objectContaining({ key: `local:${id}`, app_id: id, kind: "local", title }));
    expect(await installed.findByRole("button", { name: `Unpin ${title}` })).toHaveAttribute("aria-pressed", "true");
  }
});

it("filters the installed launcher and shares the same search bar across My apps and Shared by colleagues", async () => {
  renderer();
  const user = userEvent.setup();
  await screen.findByRole("heading", { name: company });
  const search = screen.getByRole("searchbox", { name: "Search apps" });
  await user.type(search, "Word");
  await waitFor(() => expect(screen.queryByRole("heading", { name: company })).not.toBeInTheDocument());
  expect(screen.getByRole("heading", { name: office })).toBeInTheDocument();
  await user.click(screen.getByRole("link", { name: "Shared by colleagues" }));
  expect(screen.getAllByRole("searchbox")).toEqual([search]);
  expect(search).toHaveValue("Word");
});
