import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { setLanguagePreference } from "../i18n";
import { server } from "../test/server";
import PluginWorkspace from "./PluginWorkspace";
import { isPluginWorkspacePinned, pinnedPluginWorkspaces, setPluginWorkspacePinned } from "./pluginWorkspaceNavigation";

vi.mock("./PeerProviderSettings", () => ({ PeerProviderSettings: ({ plugin }: { plugin: { id: string } }) => <p>Provider: {plugin.id}</p> }));

const root = "http://localhost/api/local/agent/extensions";
const makePlugin = (id: string, provider = false) => ({
  id, title: `Workspace ${id}`, description: `Description ${id}`, version: "2.4.0", instance_id: `install-${id}`, revision: 2,
  enabled: true, mcp_server_ids: [`${id}-reader`],
  ...(provider ? { peer_provider: { url: "https://provider.example/connect", oauth: { clientId: "desktop", scopes: ["peer.connect"] } } } : {}),
  views: [{ id: "items", title: `${id} items`, mcpServer: `${id}-reader`, tool: "read_items", inputs: [], columns: [{ key: "label", label: "Label" }],
    rowsPath: "data.rows", summaryFields: [], pagination: { pageArgument: "page", sizeArgument: "pageSize", path: "data.pagination", pageSize: 10 } }],
});
const makeServer = (id: string) => ({
  id: `${id}-reader`, plugin_id: id, instance_id: `connection-${id}`, revision: 5, discovery_revision: 7,
  enabled: true, transport: "streamable_http", oauth: { state: "logged_in" },
  tools: [{ name: "read_items", title: "Read items", enabled: true, owner_read_only: false, annotations: { readOnlyHint: true }, schema_sha256: "a".repeat(64) }],
  server_hint_candidates: [{ name: "read_items", title: "Read items", schema_sha256: "a".repeat(64) }],
});
function NextPlugin() { const navigate = useNavigate(); return <button onClick={() => navigate("/apps/plugins/beta")}>Next plugin</button>; }
function mount(id = "alpha", search = "") {
  return render(<MemoryRouter initialEntries={[`/apps/plugins/${id}${search}`]}><NextPlugin /><Routes><Route path="/apps/plugins/:pluginId" element={<PluginWorkspace />} /></Routes></MemoryRouter>);
}
beforeEach(() => { localStorage.clear(); setLanguagePreference("en"); });

describe("manifest-driven plugin workspace", () => {
  it("loads different installed plugin ids without auto-trusting hints, querying or pinning", async () => {
    let writes = 0;
    server.use(http.get(root, () => HttpResponse.json({ plugins: [makePlugin("alpha"), makePlugin("beta")], mcp_servers: [makeServer("alpha"), makeServer("beta")] })),
      http.post(`${root}/*`, () => { writes++; return HttpResponse.json({}); }));
    mount();
    await screen.findByRole("heading", { name: "Workspace alpha" });
    expect(screen.getByText("Description alpha")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pages (1)" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Members & models" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Query" })).toBeDisabled();
    expect(writes).toBe(0); expect(pinnedPluginWorkspaces()).toEqual([]);
    await userEvent.setup().click(screen.getByRole("button", { name: "Next plugin" }));
    await screen.findByRole("heading", { name: "Workspace beta" });
    expect(screen.queryByRole("button", { name: "alpha items" })).not.toBeInTheDocument();
    expect(writes).toBe(0);
  });

  it("applies hints only after the existing explicit confirmation button", async () => {
    const connection = makeServer("alpha"); let applied: unknown = null;
    server.use(http.get(root, () => HttpResponse.json({ plugins: [makePlugin("alpha")], mcp_servers: [connection] })),
      http.post(`${root}/mcp/servers/alpha-reader/apply-server-hints`, async ({ request }) => {
        applied = await request.json(); connection.tools[0].owner_read_only = true; return HttpResponse.json(connection);
      }));
    mount(); await screen.findByRole("heading", { name: "Workspace alpha" });
    expect(applied).toBeNull();
    await userEvent.setup().click(screen.getByRole("button", { name: "Confirm read-only page tools" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Query" })).toBeEnabled());
    expect(applied).toEqual({ expected_revision: 5, instance_id: "connection-alpha", discovery_revision: 7,
      tools: [{ name: "read_items", schema_sha256: "a".repeat(64) }] });
  });

  it("shows member/model controls only for the declared provider and uses existing text messaging", async () => {
    server.use(http.get(root, () => HttpResponse.json({ plugins: [makePlugin("alpha", true)], mcp_servers: [makeServer("alpha")] })));
    mount("alpha", "?tab=peers");
    expect(await screen.findByText("Provider: alpha")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Device Drop" })).toHaveAttribute("href", "/devices");
    expect(screen.getByText(/Membership does not grant file transfer/)).toBeInTheDocument();
    expect(screen.queryByText(/CLI/)).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("does not offer another plugin's capabilities for an unknown route", async () => {
    server.use(http.get(root, () => HttpResponse.json({ plugins: [makePlugin("beta", true)], mcp_servers: [makeServer("beta")] })));
    mount(); await screen.findByText("This plugin is not installed. Import its package first.");
    expect(screen.queryByRole("button", { name: "Members & models" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pin to sidebar" })).not.toBeInTheDocument();
  });

  it("pins only the selected manifest id and preserves another plugin's pin", async () => {
    setPluginWorkspacePinned({ id: "beta", title: "Workspace beta" }, true);
    server.use(http.get(root, () => HttpResponse.json({ plugins: [makePlugin("alpha")], mcp_servers: [makeServer("alpha")] })));
    mount(); await screen.findByRole("heading", { name: "Workspace alpha" });
    await userEvent.setup().click(screen.getByRole("button", { name: "Pin to sidebar" }));
    expect(isPluginWorkspacePinned("alpha")).toBe(true); expect(isPluginWorkspacePinned("beta")).toBe(true);
    await userEvent.setup().click(screen.getByRole("button", { name: "Unpin from sidebar" }));
    expect(pinnedPluginWorkspaces()).toEqual([{ id: "beta", title: "Workspace beta" }]);
  });
});
