import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import i18n from "../i18n";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import type { InferenceAccess } from "../domain/nodeClient";
import AIWorkspace from "./AIWorkspace";
import { APIAccessPage, AgentSharingPage, ModelMappingPage } from "./AIWorkspacePages";
import SecureWebAccess from "./SecureWebAccess";
import Services from "./Services";
import ServicesCatalog from "./ServicesCatalog";
import VideoRendering from "./VideoRendering";

// Structure of the redesigned service sub-pages: each page carries its own root class (the CSS hangs off it) and renders its key
// sections. Behaviour is covered by the existing page tests; these only pin the new structure.

const target = { id: "peer/home/codex-cli", rynmesh: { source: "peer", model_alias: "Codex CLI", adapter: "codex_cli", max_output_tokens: 512 } };
const access: InferenceAccess = {
  base_url: "http://127.0.0.1:8791/v1",
  keys: [{ id: "k1", name: "Notes app", used_output_tokens: 90, output_token_limit: 100, revoked: 0 }],
  models: [target], targets: [target], aliases: { "home-codex": target.id },
};

function renderPage(page: ReactNode, tune?: (client: ReturnType<typeof makeFixtureNodeClient>) => void, entry = "/page") {
  const client = makeFixtureNodeClient();
  client.getInferenceAccess = vi.fn(async () => structuredClone(access));
  client.getCLIServices = vi.fn(async () => ({ personal_space_required: true, personal_space_ready: false, services: [
    { kind: "codex_cli" as const, title: "Codex CLI", installed: true, configured: true, publication_enabled: false, online: true, api_text_only: true, service_id: "codex-cli" },
  ] }));
  tune?.(client);
  const context: AppOutletContext = {
    client, node: { node_name: "Laptop", peer_id: "peer:laptop", daemon_running: true, registry: "connected", peer_count: 0, local_items: 0, fetched_items: 0, pending_recs: 0, version: "test", uptime_seconds: 60 },
    registry: { status: "connected", url: "" }, peers: [], refreshShell: vi.fn(async () => undefined), confirm: vi.fn(), notify: vi.fn(),
  };
  return render(<MemoryRouter initialEntries={[entry]}><Routes><Route element={<Outlet context={context} />}><Route path="/page" element={page} /></Route></Routes></MemoryRouter>);
}

beforeEach(async () => { await i18n.changeLanguage("en"); });

describe("service sub-pages structure", () => {
  it("browse services: root class, filters with counts, cards and the footer line", async () => {
    const { container } = renderPage(<ServicesCatalog />);
    await screen.findByRole("heading", { name: "Browse services" });
    expect(container.querySelector(".rf.rd-svc.rd-catalog")).not.toBeNull();
    const filters = screen.getByRole("group", { name: "Service categories" });
    expect(within(filters).getAllByRole("button")).toHaveLength(4);
    expect(screen.getByRole("heading", { name: "Available now" })).toBeInTheDocument();
    expect(container.querySelectorAll(".sv-svc")).toHaveLength(3);
    fireEvent.change(screen.getByRole("textbox", { name: "Search services" }), { target: { value: "zzz" } });
    expect(screen.getByRole("heading", { name: "No matching services" })).toBeInTheDocument();
  });

  it("browse services: a failed discovery shows a retry alert", async () => {
    renderPage(<ServicesCatalog />, (client) => { client.listLLMServices = vi.fn(async () => { throw new Error("down"); }); });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Some services could not be refreshed");
    expect(within(alert).getByRole("button", { name: "Retry discovery" })).toBeInTheDocument();
  });

  it("AI workspace: root class and the hidden page heading", async () => {
    const { container } = renderPage(<AIWorkspace />, undefined, "/page?client=fixture");
    await waitFor(() => expect(container.querySelector(".rd-workspace")).not.toBeNull());
    expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
    expect(container.querySelector(".rd-workspace .sv-ws-history")).not.toBeNull();
  });

  it("API access: request path, endpoint card, key list and code example", async () => {
    const { container } = renderPage(<APIAccessPage />);
    await screen.findByRole("heading", { name: "Local API URL" });
    expect(container.querySelector(".rf.rd-svc.rd-api")).not.toBeNull();
    expect(screen.getByRole("region", { name: "Request path" })).toHaveTextContent("Laptop");
    expect(screen.getByRole("group", { name: "API format" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Create project key" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Existing project keys" })).toBeInTheDocument();
    expect(container.querySelector(".sv-key-bar")).not.toBeNull();
    expect(container.querySelector("pre.sv-pre")).toHaveTextContent("from openai import OpenAI");
  });

  it("model mapping: saved routes next to the editor", async () => {
    const { container } = renderPage(<ModelMappingPage />);
    await screen.findByRole("heading", { name: "Create or update mapping" });
    expect(container.querySelector(".rf.rd-svc.rd-mapping")).not.toBeNull();
    expect(screen.getByRole("heading", { name: "Saved mappings" })).toBeInTheDocument();
    expect(container.querySelectorAll(".sv-route")).toHaveLength(1);
  });

  it("service sharing: flow strip, CLI cards and the personal-space notice", async () => {
    const { container } = renderPage(<AgentSharingPage />);
    await screen.findByRole("heading", { name: "Codex CLI" });
    expect(container.querySelector(".rf.rd-svc.rd-sharing")).not.toBeNull();
    expect(screen.getByRole("region", { name: "How sharing works" })).toBeInTheDocument();
    expect([...container.querySelectorAll(".sv-alert")].some((node) => /personal space/i.test(node.textContent ?? ""))).toBe(true);
    expect(screen.getByRole("heading", { name: "Local model" })).toBeInTheDocument();
  });

  it("video rendering: head, create form and the before-you-start summary", async () => {
    const { container } = renderPage(<VideoRendering />);
    await screen.findByText("Renderer ready");
    expect(container.querySelector(".rf.rd-svc.rd-video")).not.toBeNull();
    expect(screen.getByRole("heading", { name: "Create a render" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Before you start" })).toBeInTheDocument();
  });

  it("secure web access: connect card and connection summary", async () => {
    const { container } = renderPage(<SecureWebAccess />);
    await screen.findByText("Connect when you are ready");
    expect(container.querySelector(".rf.rd-svc.rd-secure")).not.toBeNull();
    expect(screen.getByRole("heading", { name: "Connection" })).toBeInTheDocument();
  });

  it("service setup: three tabs, API access link card, no embedded API access copy", async () => {
    const { container } = renderPage(<Services />);
    await screen.findByRole("heading", { name: "Service setup" });
    expect(container.querySelector(".rf.rd-svc.rd-setup")).not.toBeNull();
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Local model", "Orders & history", "Video providers"]);
    const link = screen.getByRole("link", { name: /API keys and model aliases are on the API access page/ });
    expect(link.getAttribute("href")).toMatch(/^\/services\/api/);
    expect(screen.queryByRole("button", { name: "Configure API access" })).not.toBeInTheDocument();
    const pane = (id: string) => container.querySelector(`#sv-pane-${id}`)!;
    expect(pane("local")).toHaveAttribute("data-on", "true");
    expect(pane("orders")).toHaveAttribute("data-on", "false");
    // inactive panes stay mounted: their controls remain reachable
    expect(screen.getByRole("button", { name: "Place encrypted order" })).toBeInTheDocument();
    await act(async () => { fireEvent.click(tabs[1]); });
    expect(tabs[1]).toHaveAttribute("aria-selected", "true");
    expect(pane("orders")).toHaveAttribute("data-on", "true");
    expect(pane("local")).toHaveAttribute("data-on", "false");
    await act(async () => { fireEvent.click(tabs[2]); });
    expect(pane("video")).toHaveAttribute("data-on", "true");
    expect(within(pane("video") as HTMLElement).getByRole("heading", { name: "Signal50 Veo providers" })).toBeInTheDocument();
  });

  it("service setup: the setup-mode cards switch the fields shown", async () => {
    renderPage(<Services />);
    await screen.findByRole("heading", { name: "Service setup" });
    const group = screen.getByRole("group", { name: "Setup mode choices" });
    expect(screen.getByLabelText("Local API URL")).toBeInTheDocument();
    fireEvent.click(within(group).getByRole("button", { name: /Import a GGUF file/ }));
    expect(screen.getByLabelText("GGUF file path")).toBeInTheDocument();
    expect(screen.queryByLabelText("Local API URL")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Setup mode")).toHaveValue("import-gguf");
    expect(within(group).getByRole("button", { name: /Import a GGUF file/ })).toHaveAttribute("aria-pressed", "true");
  });
});
