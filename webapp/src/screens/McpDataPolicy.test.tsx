import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { normalizeMcpServer, type ExtensionDataPolicy } from "../domain/agentExtensions";
import * as extensionApi from "../domain/agentExtensions";
import { server } from "../test/server";
import { McpDataPolicy, McpDataPolicySummary } from "./McpDataPolicy";

const url = "http://localhost/api/local/agent/extensions/mcp/servers/fixture/apply-server-hints";
const hash = "a".repeat(64);
const candidates = [{ name: "read_items", title: "Read items", schema_sha256: hash }, { name: "list_items", title: "List items", schema_sha256: "b".repeat(64) }];
function fixture(extra: Record<string, unknown> = {}) {
  return normalizeMcpServer({ id: "fixture", instance_id: "installation-1", revision: 7, discovery_revision: 6,
    transport: "streamable_http", enabled: true, server_hint_candidates: candidates,
    data_policy: { models: { mode: "any" }, memory: "deny" }, ...extra });
}

describe("MCP data policy and read-only hints", () => {
  it("shows immutable default policy without assuming old fixtures contain new fields", () => {
    render(<McpDataPolicy server={fixture({ data_policy: undefined, server_hint_candidates: undefined })} onRefresh={vi.fn()} />);
    expect(screen.getByText("Models: unrestricted")).toBeInTheDocument();
    expect(screen.getByText(/Long-term memory: allowed, subject/)).toBeInTheDocument();
    expect(screen.getByText(/does not prevent the owner from editing local source or data files/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("shows all allowlist rule types and the conservative memory restriction", () => {
    const policy: ExtensionDataPolicy = { models: { mode: "allowlist", allow: [{ on_device: true },
      { api_base_url_prefix: "https://models.example.test/api/" }, { catalog_model_id: "verified-model" }] }, memory: "allow" };
    render(<McpDataPolicySummary policy={policy} compact />);
    expect(screen.getByText("Built-in inference on this device")).toBeInTheDocument();
    expect(screen.getByText("https://models.example.test/api/")).toBeInTheDocument();
    expect(screen.getByText("verified-model")).toBeInTheDocument();
    expect(screen.getByText(/this version still disables automatic long-term memory for model-restricted tasks/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("shows an empty allowlist as denying all models and explains memory deny", () => {
    render(<McpDataPolicySummary policy={{ models: { mode: "allowlist", allow: [] }, memory: "deny" }} />);
    expect(screen.getByText("The allowlist is empty; no model can use these tools.")).toBeInTheDocument();
    expect(screen.getByText(/Tasks using these tools do not create automatic long-term memories/)).toBeInTheDocument();
  });

  it("requires owner selection and a separate confirmation, then sends exact reviewed bindings", async () => {
    let calls = 0;
    const onRefresh = vi.fn();
    server.use(http.post(url, async ({ request }) => {
      calls++;
      expect(await request.json()).toEqual({ expected_revision: 7, instance_id: "installation-1", discovery_revision: 6,
        tools: [{ name: "read_items", schema_sha256: hash }] });
      return HttpResponse.json(fixture({ revision: 8, read_only_tools: ["read_items"], read_only_sources: { read_items: "server_hint" } }));
    }));
    const user = userEvent.setup(); render(<McpDataPolicy server={fixture()} onRefresh={onRefresh} />);
    expect(screen.getByRole("checkbox", { name: /Read items/ })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Review read-only settings" })).toBeDisabled();
    expect(calls).toBe(0);
    await user.click(screen.getByRole("checkbox", { name: /Read items/ }));
    await user.click(screen.getByRole("button", { name: "Review read-only settings" }));
    const dialog = screen.getByRole("alertdialog", { name: "Confirm read-only tools" });
    expect(calls).toBe(0); expect(within(dialog).getByText("read_items")).toBeInTheDocument();
    expect(within(dialog).queryByText("list_items")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Confirm read-only hints" }));
    await screen.findByText("Selected read-only hints applied.");
    expect(calls).toBe(1); expect(onRefresh).toHaveBeenCalledOnce();
  });

  it("cancels without writing and clears the selection", async () => {
    let calls = 0;
    server.use(http.post(url, () => { calls++; return HttpResponse.json(fixture()); }));
    const user = userEvent.setup(); render(<McpDataPolicy server={fixture()} onRefresh={vi.fn()} />);
    await user.click(screen.getByRole("checkbox", { name: /Read items/ }));
    await user.click(screen.getByRole("button", { name: "Review read-only settings" }));
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Cancel" }));
    expect(calls).toBe(0); expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Read items/ })).not.toBeChecked();
  });

  it("selects all or clears locally and cancelling the bulk confirmation sends no POST", async () => {
    let calls = 0;
    server.use(http.post(url, () => { calls++; return HttpResponse.json(fixture()); }));
    const user = userEvent.setup(); render(<McpDataPolicy server={fixture()} onRefresh={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Select all" }));
    for (const checkbox of screen.getAllByRole("checkbox")) expect(checkbox).toBeChecked();
    expect(calls).toBe(0);
    await user.click(screen.getByRole("button", { name: "Clear selection" }));
    for (const checkbox of screen.getAllByRole("checkbox")) expect(checkbox).not.toBeChecked();
    expect(calls).toBe(0);
    await user.click(screen.getByRole("button", { name: "Select all" }));
    await user.click(screen.getByRole("button", { name: "Review read-only settings" }));
    const dialog = screen.getByRole("alertdialog");
    expect(within(dialog).getByText("read_items")).toBeInTheDocument();
    expect(within(dialog).getByText("list_items")).toBeInTheDocument();
    expect(calls).toBe(0);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(calls).toBe(0); expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    for (const checkbox of screen.getAllByRole("checkbox")) expect(checkbox).not.toBeChecked();
  });

  it.each([{ instance_id: "installation-2" }, { revision: 8 }, { discovery_revision: 9 },
    { server_hint_candidates: [{ ...candidates[0], schema_sha256: "c".repeat(64) }] }])("invalidates selection and confirmation when its anchor changes: %j", async update => {
    const user = userEvent.setup(), onRefresh = vi.fn();
    const view = render(<McpDataPolicy server={fixture()} onRefresh={onRefresh} />);
    await user.click(screen.getByRole("checkbox", { name: /Read items/ }));
    await user.click(screen.getByRole("button", { name: "Review read-only settings" }));
    view.rerender(<McpDataPolicy server={fixture(update)} onRefresh={onRefresh} />);
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Read items/ })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Review read-only settings" })).toBeDisabled();
  });

  it("does not echo backend error details and requires selection again after failure", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    server.use(http.post(url, () => HttpResponse.json({ detail: "access_token=synthetic-private code=synthetic-private" }, { status: 409 })));
    const user = userEvent.setup(); render(<McpDataPolicy server={fixture()} onRefresh={vi.fn()} />);
    await user.click(screen.getByRole("checkbox", { name: /Read items/ }));
    await user.click(screen.getByRole("button", { name: "Review read-only settings" }));
    await user.click(screen.getByRole("button", { name: "Confirm read-only hints" }));
    await screen.findByRole("alert");
    expect(document.body.textContent).not.toContain("synthetic-private");
    expect(consoleError.mock.calls.flat().join(" ")).not.toContain("synthetic-private");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review read-only settings" })).toBeDisabled();
    consoleError.mockRestore();
  });

  it("ignores a late success for an installation that has been replaced", async () => {
    let release!: (value: ReturnType<typeof fixture>) => void;
    const gate = new Promise<ReturnType<typeof fixture>>(resolve => { release = resolve; });
    const request = vi.spyOn(extensionApi, "applyMcpServerHints").mockReturnValue(gate);
    const onRefresh = vi.fn(), user = userEvent.setup();
    const view = render(<McpDataPolicy server={fixture()} onRefresh={onRefresh} />);
    await user.click(screen.getByRole("checkbox", { name: /Read items/ }));
    await user.click(screen.getByRole("button", { name: "Review read-only settings" }));
    await user.click(screen.getByRole("button", { name: "Confirm read-only hints" }));
    expect(request).toHaveBeenCalledOnce();
    view.rerender(<McpDataPolicy server={fixture({ instance_id: "installation-2" })} onRefresh={onRefresh} />);
    await act(async () => { release(fixture()); await gate; });
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(onRefresh).not.toHaveBeenCalled(); expect(screen.queryByText("Selected read-only hints applied.")).not.toBeInTheDocument();
    request.mockRestore();
  });

  it.each([{ instance_id: undefined }, { discovery_revision: undefined }, { discovery_revision: 0 }])("cannot confirm without a complete discovery binding: %j", extra => {
    render(<McpDataPolicy server={fixture(extra)} onRefresh={vi.fn()} />);
    expect(screen.getByRole("checkbox", { name: /Read items/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Review read-only settings" })).toBeDisabled();
  });
});
