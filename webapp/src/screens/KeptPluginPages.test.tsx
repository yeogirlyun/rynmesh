import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { agentExtensions } from "../domain/agentExtensions";
import { setLanguagePreference } from "../i18n";
import { KEPT_PLUGIN_PAGES, KeptPluginPages, KeptPluginPagesProvider } from "./KeptPluginPages";
import PluginWorkspace from "./PluginWorkspace";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("../appContext", () => ({ useAppContext: () => ({ client: {} }) }));
vi.mock("../domain/agentExtensions", () => ({ agentExtensions: { overview: vi.fn() }, queryPluginTool: vi.fn() }));
vi.mock("../domain/peerProviders", () => ({ peerProviders: {} }));

const page = (id: string, enabled = true) => ({
  id, title: `Page ${id}`, enabled, mcp_server_ids: [],
  web_app: { url: `https://${id}.example.test/apps/${id}`, permissions: [] },
});
let plugins = ["dash", "work", "affiliate", "extra"].map(id => page(id));

function Shell() {
  const navigate = useNavigate();
  return <>
    <nav>{["dash", "work", "affiliate", "extra"].map(id => <button key={id} onClick={() => navigate(`/apps/plugins/${id}`)}>Open {id}</button>)}
      <button onClick={() => navigate("/agent")}>Open agent</button></nav>
    <main><Outlet /><KeptPluginPages client={{} as never} /></main>
  </>;
}

function mount(path = "/apps/plugins/dash") {
  return render(<MemoryRouter initialEntries={[path]}><KeptPluginPagesProvider><Routes>
    <Route element={<Shell />}>
      <Route path="/apps/plugins/:pluginId" element={<PluginWorkspace />} />
      <Route path="/agent" element={<p>Agent page</p>} />
    </Route>
  </Routes></KeptPluginPagesProvider></MemoryRouter>);
}

const holder = (frame: HTMLElement) => frame.closest(".pf-kept-plugin") as HTMLElement;

beforeEach(() => {
  setLanguagePreference("en");
  plugins = ["dash", "work", "affiliate", "extra"].map(id => page(id));
  vi.mocked(agentExtensions.overview).mockImplementation(async () => ({ plugins, mcp_servers: [] }) as never);
});

describe("kept plugin pages", () => {
  it("returns to the same loaded page instead of starting the plugin again", async () => {
    const user = userEvent.setup();
    mount();
    const frame = await screen.findByTitle("Page dash");
    const post = vi.spyOn((frame as HTMLIFrameElement).contentWindow!, "postMessage");

    await user.click(screen.getByRole("button", { name: "Open agent" }));
    expect(screen.getByText("Agent page")).toBeInTheDocument();
    expect(frame).toBeInTheDocument();
    expect(holder(frame)).toHaveAttribute("aria-hidden", "true");
    expect(holder(frame)).toHaveAttribute("inert");
    expect(post).toHaveBeenLastCalledWith({ type: "ryn.plugin.visibility", visible: false }, "https://dash.example.test");

    await user.click(screen.getByRole("button", { name: "Open dash" }));
    // The same element: the plugin did not load again.
    expect(screen.getByTitle("Page dash")).toBe(frame);
    expect(holder(frame)).not.toHaveAttribute("aria-hidden");
    expect(holder(frame)).not.toHaveAttribute("inert");
    expect(holder(frame)).toHaveClass("is-active");
    expect(post).toHaveBeenLastCalledWith({ type: "ryn.plugin.visibility", visible: true }, "https://dash.example.test");
  });

  it("switches between plugins without reloading either", async () => {
    const user = userEvent.setup();
    mount();
    const dash = await screen.findByTitle("Page dash");
    await user.click(screen.getByRole("button", { name: "Open work" }));
    const work = await screen.findByTitle("Page work");
    expect(holder(dash)).not.toHaveClass("is-active");
    expect(holder(work)).toHaveClass("is-active");
    await user.click(screen.getByRole("button", { name: "Open dash" }));
    expect(screen.getByTitle("Page dash")).toBe(dash);
    expect(screen.getByTitle("Page work")).toBe(work);
    expect(holder(dash)).toHaveClass("is-active");
  });

  it("preserves sibling order on revisits while evicting the least recently used page", async () => {
    const user = userEvent.setup();
    mount();
    const dash = await screen.findByTitle("Page dash");
    for (const id of ["work", "affiliate"]) {
      await user.click(screen.getByRole("button", { name: `Open ${id}` }));
      await screen.findByTitle(`Page ${id}`);
    }
    const siblings = Array.from(holder(dash).parentElement!.children);
    for (const id of ["work", "affiliate", "dash"]) {
      await user.click(screen.getByRole("button", { name: `Open ${id}` }));
      expect(Array.from(holder(dash).parentElement!.children)).toEqual(siblings);
      expect(holder(screen.getByTitle(`Page ${id}`))).toHaveClass("is-active");
    }
    await user.click(screen.getByRole("button", { name: "Open extra" }));
    await screen.findByTitle("Page extra");
    expect(screen.queryByTitle("Page work")).not.toBeInTheDocument();
    expect(screen.getByTitle("Page dash")).toBe(dash);
    expect(Array.from(holder(dash).parentElement!.children).map(node => node.getAttribute("data-plugin-id")))
      .toEqual(["dash", "affiliate", "extra"]);
    expect(document.querySelectorAll(".pf-kept-plugin")).toHaveLength(KEPT_PLUGIN_PAGES);
  });

  it("keeps a refreshed but unchanged manifest from reloading the page", async () => {
    const user = userEvent.setup();
    mount();
    const frame = await screen.findByTitle("Page dash");
    plugins = plugins.map(item => ({ ...item, web_app: { ...item.web_app } }));
    await user.click(screen.getByRole("button", { name: "Open agent" }));
    await user.click(screen.getByRole("button", { name: "Open dash" }));
    await waitFor(() => expect(agentExtensions.overview).toHaveBeenCalledTimes(2));
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByTitle("Page dash")).toBe(frame);
  });

  it(`closes the oldest page beyond ${KEPT_PLUGIN_PAGES}`, async () => {
    const user = userEvent.setup();
    mount();
    await screen.findByTitle("Page dash");
    for (const id of ["work", "affiliate", "extra"]) {
      await user.click(screen.getByRole("button", { name: `Open ${id}` }));
      await screen.findByTitle(`Page ${id}`);
    }
    expect(screen.queryByTitle("Page dash")).not.toBeInTheDocument();
    expect(document.querySelectorAll(".pf-kept-plugin")).toHaveLength(KEPT_PLUGIN_PAGES);
  });

  it("closes a kept page once its plugin is turned off", async () => {
    const user = userEvent.setup();
    mount();
    await screen.findByTitle("Page dash");
    await user.click(screen.getByRole("button", { name: "Open agent" }));
    plugins = plugins.map(item => item.id === "dash" ? page("dash", false) : item);
    await user.click(screen.getByRole("button", { name: "Open dash" }));
    expect(await screen.findByText("Plugin unavailable. Check that it is enabled.")).toBeInTheDocument();
    await waitFor(() => expect(document.querySelector('[data-plugin-id="dash"]')).toBeNull());
  });
});
