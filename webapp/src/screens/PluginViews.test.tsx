import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { normalizeMcpServer, normalizePlugin, type ExtensionPluginView } from "../domain/agentExtensions";
import { server } from "../test/server";
import { PluginViews, formatViewValue, readViewPath } from "./PluginViews";

const view: ExtensionPluginView = {
  id: "items", title: "Items", mcpServer: "demo-reader", tool: "read_items", description: "Read items with a date range.",
  inputs: [{ name: "start", label: "Start", type: "date", required: true, default: "month_start" }, { name: "end", label: "End", type: "date", required: true, default: "today" }, { name: "search", label: "Search text", type: "text" }],
  columns: [{ key: "label", label: "Label" }, { key: "amount", label: "Amount", format: "money" }], rowsPath: "data.rows",
  summaryFields: [{ path: "data.sum", label: "Full-range sum", format: "money" }], pagination: { pageArgument: "page", sizeArgument: "pageSize", path: "data.pagination", pageSize: 20 },
};
const plugin = () => normalizePlugin({ id: "demo", instance_id: "install-a", revision: 4, enabled: true, views: [view] });
const connection = () => normalizeMcpServer({ id: "demo-reader", instance_id: "connection-a", revision: 3, enabled: true, plugin_id: "demo", transport: "streamable_http", tools: [{ name: "read_items", title: "Read items", enabled: true, owner_read_only: true, annotations: { readOnlyHint: true } }] });
const endpoint = "http://localhost/api/local/agent/extensions/plugins/demo/views/items/query";
const response = (page = 1, rows: unknown[] = [{ label: `Item page ${page}`, amount: "10000000000000001.123400" }]) => ({ view_id: "items", result: { success: true, data: { rows, sum: "9999.25", pagination: { page, pageSize: 20, total: 21, totalPages: 2 } } } });

describe("plugin tool pages", () => {
  it("keeps missing legacy declarations empty and ignores malformed views", () => {
    expect(normalizePlugin({ id: "old" }).views).toEqual([]);
    expect(normalizePlugin({ views: [null, { ...view, inputs: "bad" }] }).views).toEqual([]);
    expect(readViewPath({ data: { 指标: 0 } }, "data.指标")).toBe(0);
    expect(readViewPath({}, "constructor.name")).toBeUndefined();
    expect(formatViewValue("10000000000000001.123400", "money")).toBe("10,000,000,000,000,001.123400");
    expect(formatViewValue(0, "money")).toBe("0");
    expect(formatViewValue({ secret: "not scalar" })).toBe("—");
  });

  it("queries only on a click, binds the install, pages results and keeps server totals", async () => {
    const calls: Array<Record<string, unknown>> = [];
    server.use(http.post(endpoint, async ({ request }) => { const body = await request.json() as Record<string, unknown>; calls.push(body); return HttpResponse.json(response(Number(body.page))); }));
    const user = userEvent.setup();
    render(<PluginViews plugin={plugin()} servers={[connection()]} onOpenServer={vi.fn()} />);
    expect(calls).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Query" }));
    await screen.findByText("Item page 1");
    expect(calls[0]).toMatchObject({ page: 1, expected_instance_id: "install-a", expected_revision: 4 });
    expect(Object.keys(calls[0]).sort()).toEqual(["arguments", "expected_instance_id", "expected_revision", "page"]);
    expect(calls[0].arguments).not.toHaveProperty("search");
    expect(screen.getByText("9,999.25")).toBeInTheDocument();
    expect(screen.getByText("10,000,000,000,000,001.123400")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next page" }));
    await screen.findByText("Item page 2");
    expect(calls[1]).toMatchObject({ page: 2, arguments: calls[0].arguments });
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "View row 1 details" }));
    expect(within(screen.getByRole("region", { name: "Record details" })).getByText("Item page 2")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close details" }));
    expect(screen.queryByRole("region", { name: "Record details" })).not.toBeInTheDocument();
  });

  it("does not query without enabled, authenticated, read-only capabilities", async () => {
    const onOpen = vi.fn(); const user = userEvent.setup();
    const c = connection(); c.oauth = { state: "logged_out" };
    const { rerender } = render(<PluginViews plugin={plugin()} servers={[c]} onOpenServer={onOpen} />);
    expect(screen.getByRole("button", { name: "Query" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Sign-in and connection" }));
    expect(onOpen).toHaveBeenCalledWith("demo-reader");
    rerender(<PluginViews plugin={{ ...plugin(), enabled: false }} servers={[connection()]} onOpenServer={onOpen} />);
    expect(screen.getByRole("button", { name: "Query" })).toBeDisabled();
    const unsafe = connection(); unsafe.tools[0].annotations = { readOnlyHint: false };
    rerender(<PluginViews plugin={plugin()} servers={[unsafe]} onOpenServer={onOpen} />);
    expect(screen.getByRole("button", { name: "Query" })).toBeDisabled();
    const unconfirmed = connection(); unconfirmed.tools[0].owner_read_only = false;
    rerender(<PluginViews plugin={plugin()} servers={[unconfirmed]} onOpenServer={onOpen} />);
    expect(screen.getByRole("button", { name: "Query" })).toBeDisabled();
    const desktop = connection(); desktop.computer_access = true;
    rerender(<PluginViews plugin={plugin()} servers={[desktop]} onOpenServer={onOpen} />);
    expect(screen.getByRole("button", { name: "Query" })).toBeDisabled();
    expect(screen.getByText("Pages do not support connections that need desktop control.")).toBeInTheDocument();
  });

  it("escapes table content and never displays raw upstream errors", async () => {
    const user = userEvent.setup();
    server.use(http.post(endpoint, () => HttpResponse.json(response(1, [{ label: '<img src=x onerror="alert(1)">', amount: null }]))));
    render(<PluginViews plugin={plugin()} servers={[connection()]} onOpenServer={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Query" }));
    await screen.findByText('<img src=x onerror="alert(1)">');
    expect(document.querySelector(".plugin-view-table img")).toBeNull();
    server.use(http.post(endpoint, () => HttpResponse.json({ detail: "upstream-secret-sentinel" }, { status: 403 })));
    await user.click(screen.getByRole("button", { name: "Query" }));
    await screen.findByRole("alert");
    expect(document.body.textContent).not.toContain("upstream-secret-sentinel");
    expect(screen.queryByText('<img src=x onerror="alert(1)">')).not.toBeInTheDocument();
  });

  it("clears the old page on filter edits and discards a late response", async () => {
    let release!: () => void; let entered!: () => void;
    const received = new Promise<void>(resolve => { entered = resolve; });
    const pending = new Promise<void>(resolve => { release = resolve; });
    server.use(http.post(endpoint, async () => { entered(); await pending; return HttpResponse.json(response()); }));
    const user = userEvent.setup();
    render(<PluginViews plugin={plugin()} servers={[connection()]} onOpenServer={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Query" }));
    await received;
    await user.type(screen.getByRole("textbox", { name: "Search text" }), "new");
    await act(async () => { release(); await pending; });
    expect(screen.queryByText("Item page 1")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Query" })).toBeEnabled();
  });

  it("clears private results after disabling or reimporting the plugin", async () => {
    server.use(http.post(endpoint, () => HttpResponse.json(response())));
    const user = userEvent.setup(); const original = plugin();
    const { rerender } = render(<PluginViews plugin={original} servers={[connection()]} onOpenServer={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Query" })); await screen.findByText("Item page 1");
    rerender(<PluginViews plugin={{ ...original, enabled: false }} servers={[connection()]} onOpenServer={vi.fn()} />);
    expect(screen.queryByText("Item page 1")).not.toBeInTheDocument();
    rerender(<PluginViews plugin={{ ...original, instance_id: "install-b" }} servers={[connection()]} onOpenServer={vi.fn()} />);
    expect(screen.queryByText("9,999.25")).not.toBeInTheDocument();
  });

  it("reports genuine empty results and a truncated response without inventing totals", async () => {
    const user = userEvent.setup(); const empty = response(1, []); empty.result.data.pagination.total = 0; empty.result.data.pagination.totalPages = 0;
    server.use(http.post(endpoint, () => HttpResponse.json({ ...empty, result: { ...empty.result, truncated: true } })));
    render(<PluginViews plugin={plugin()} servers={[connection()]} onOpenServer={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Query" }));
    await screen.findByText("No records match these filters.");
    expect(screen.getByText(/The server truncated part/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
  });

  it("rejects mismatched pagination or unstructured results", async () => {
    const user = userEvent.setup();
    server.use(http.post(endpoint, () => HttpResponse.json(response(2))));
    render(<PluginViews plugin={plugin()} servers={[connection()]} onOpenServer={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Query" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Query failed"));
    expect(screen.queryByText("Item page 2")).not.toBeInTheDocument();
  });

  it.each(["wrong total pages", "oversized page"])("rejects %s rather than showing an incomplete page", async kind => {
    const invalid = response();
    if (kind === "wrong total pages") invalid.result.data.pagination.totalPages = 9;
    else invalid.result.data.rows = Array.from({ length: 21 }, () => ({ label: "Unexpected row" }));
    server.use(http.post(endpoint, () => HttpResponse.json(invalid)));
    render(<PluginViews plugin={plugin()} servers={[connection()]} onOpenServer={vi.fn()} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Query" }));
    await screen.findByRole("alert");
    expect(screen.queryByText("Unexpected row")).not.toBeInTheDocument();
    expect(screen.queryByText("9,999.25")).not.toBeInTheDocument();
  });

  it("explains a stale installation without displaying upstream details", async () => {
    server.use(http.post(endpoint, () => HttpResponse.json({ error: "agent_stale_revision" }, { status: 409 })));
    render(<PluginViews plugin={plugin()} servers={[connection()]} onOpenServer={vi.fn()} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Query" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("The plugin changed. Reopen this page."));
  });

  it("clears displayed results when the read-only confirmation is revoked", async () => {
    server.use(http.post(endpoint, () => HttpResponse.json(response())));
    const c = connection();
    const { rerender } = render(<PluginViews plugin={plugin()} servers={[c]} onOpenServer={vi.fn()} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Query" })); await screen.findByText("Item page 1");
    c.tools[0].owner_read_only = false;
    rerender(<PluginViews plugin={plugin()} servers={[c]} onOpenServer={vi.fn()} />);
    expect(screen.queryByText("Item page 1")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Query" })).toBeDisabled();
  });

  it.each([
    ["agent_plugin_view_read_only_confirmation_required", "Confirm this tool as read-only"],
    ["agent_plugin_view_unsupported_server", "Pages do not support connections that need desktop control."],
  ])("explains the host restriction %s", async (code, message) => {
    server.use(http.post(endpoint, () => HttpResponse.json({ error: code }, { status: 400 })));
    render(<PluginViews plugin={plugin()} servers={[connection()]} onOpenServer={vi.fn()} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Query" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(message));
  });
});
