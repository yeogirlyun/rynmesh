import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { normalizeMcpServer } from "../domain/agentExtensions";
import { reserveMcpAuthBrowser } from "../domain/mcpAuthBrowser";
import { McpAuthSettings } from "./McpAuthSettings";

vi.mock("../domain/mcpAuthBrowser", () => ({ reserveMcpAuthBrowser: vi.fn() }));
const base = "http://localhost/api/local/agent/extensions/mcp/servers/fixture";
const auth = "synthetic-flow";
const authorizationUrl = "https://login.example/auth?state=synthetic&code_challenge=synthetic";
const browser = { open: vi.fn(), cancel: vi.fn() };
function fixture(extra: Record<string, unknown> = {}) {
  return normalizeMcpServer({ id: "fixture", instance_id: "installation-1", revision: 7, config_generation: 3,
    transport: "streamable_http", enabled: true, auth: { mode: "oauth" }, oauth: { state: "logged_out" }, ...extra });
}
function start() {
  server.use(http.post(`${base}/oauth/login`, async ({ request }) => {
    expect(await request.json()).toEqual({ expected_revision: 7, language: "en" });
    return HttpResponse.json({ auth_id: auth, authorization_url: authorizationUrl, expires_in: 600 });
  }));
}
beforeEach(() => {
  browser.open.mockReset().mockResolvedValue(undefined); browser.cancel.mockReset();
  vi.mocked(reserveMcpAuthBrowser).mockReset().mockReturnValue(browser);
});

describe("MCP sign-in settings", () => {
  it("starts on owner click, polls to success, and refreshes the server overview", async () => {
    start(); const onRefresh = vi.fn();
    server.use(http.get(`${base}/oauth/login/${auth}`, () => HttpResponse.json({ state: "connected" })));
    const user = userEvent.setup(); const view = render(<McpAuthSettings server={fixture()} onRefresh={onRefresh} />);
    expect(reserveMcpAuthBrowser).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByText("Waiting for browser authorization…");
    expect(browser.open).toHaveBeenCalledWith(authorizationUrl);
    await waitFor(() => expect(onRefresh).toHaveBeenCalledOnce(), { timeout: 3500 });
    view.rerender(<McpAuthSettings server={fixture({ oauth: { state: "logged_in", issuer_host: "login.example", expires_at: 1800000000 } })} onRefresh={onRefresh} />);
    expect(screen.getByText("Signed in")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in again" })).toBeEnabled();
    expect(document.body.textContent).not.toContain(authorizationUrl);
  });

  it.each(["failed", "expired"])("shows a fixed failure and permits retry after %s", async state => {
    start(); const onRefresh = vi.fn();
    server.use(http.get(`${base}/oauth/login/${auth}`, () => HttpResponse.json({ state, error: "code=synthetic-private token=synthetic-private" })));
    const user = userEvent.setup(); render(<McpAuthSettings server={fixture()} onRefresh={onRefresh} />);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByRole("alert", {}, { timeout: 3500 });
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
    expect(document.body.textContent).not.toContain("synthetic-private");
    expect(onRefresh).toHaveBeenCalledOnce();
  });

  it("cancels the pending transaction and stops showing the waiting state", async () => {
    start(); const onRefresh = vi.fn(); let cancellations = 0;
    server.use(http.post(`${base}/oauth/login/${auth}/cancel`, () => { cancellations++; return HttpResponse.json({ state: "cancelled" }); }));
    const user = userEvent.setup(); render(<McpAuthSettings server={fixture()} onRefresh={onRefresh} />);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await user.click(await screen.findByRole("button", { name: "Cancel authorization" }));
    await waitFor(() => expect(screen.queryByText("Waiting for browser authorization…")).not.toBeInTheDocument());
    expect(cancellations).toBe(1); expect(onRefresh).toHaveBeenCalledOnce();
  });

  it("signs out with the current server revision", async () => {
    const onRefresh = vi.fn(); let signouts = 0;
    server.use(http.post(`${base}/oauth/logout`, async ({ request }) => {
      expect(await request.json()).toEqual({ expected_revision: 7 }); signouts++;
      return HttpResponse.json({ state: "logged_out" });
    }));
    const user = userEvent.setup(); render(<McpAuthSettings server={fixture({ oauth: { state: "logged_in" } })} onRefresh={onRefresh} />);
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    await screen.findByText("Signed out"); expect(signouts).toBe(1); expect(onRefresh).toHaveBeenCalledOnce();
  });

  it("can cancel an existing pending login after reopening the settings", async () => {
    const onRefresh = vi.fn(); let cancellations = 0;
    server.use(http.post(`${base}/oauth/logout`, async ({ request }) => {
      expect(await request.json()).toEqual({ expected_revision: 7 }); cancellations++;
      return HttpResponse.json({ state: "logged_out" });
    }));
    const user = userEvent.setup();
    render(<McpAuthSettings server={fixture({ oauth: { state: "pending" } })} onRefresh={onRefresh} />);
    expect(screen.getByText("Waiting for browser authorization…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Cancel authorization" }));
    await screen.findByText("Signed out");
    expect(cancellations).toBe(1); expect(onRefresh).toHaveBeenCalledOnce();
    expect(reserveMcpAuthBrowser).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });

  it("clears all local authorizations only after confirmation and supports cancelling confirmation", async () => {
    let removals = 0; const onRefresh = vi.fn();
    server.use(http.post("http://localhost/api/local/agent/extensions/mcp/oauth/clear-local-authorizations", async ({ request }) => {
      expect(await request.json()).toEqual({ confirm: true }); removals++; return HttpResponse.json({ cleared: true });
    }));
    const user = userEvent.setup(); render(<McpAuthSettings server={fixture({ oauth: { state: "logged_in" } })} onRefresh={onRefresh} />);
    await user.click(screen.getByText("Sign-in settings"));
    await user.click(screen.getByRole("button", { name: "Clear all local MCP authorizations" }));
    let dialog = screen.getByRole("alertdialog"); expect(removals).toBe(0);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument(); expect(removals).toBe(0);
    await user.click(screen.getByRole("button", { name: "Clear all local MCP authorizations" }));
    dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Confirm removal" }));
    await screen.findByText("Signed out"); expect(removals).toBe(1); expect(onRefresh).toHaveBeenCalledOnce();
  });

  it("cleans up the login transaction when the browser cannot open and redacts its error", async () => {
    start(); browser.open.mockRejectedValue(new Error("client_secret=synthetic-private")); let cancellations = 0;
    server.use(http.post(`${base}/oauth/login/${auth}/cancel`, () => { cancellations++; return HttpResponse.json({ state: "cancelled" }); }));
    const user = userEvent.setup(); render(<McpAuthSettings server={fixture()} onRefresh={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByRole("alert"); expect(cancellations).toBe(1); expect(browser.cancel).toHaveBeenCalledOnce();
    expect(document.body.textContent).not.toContain("synthetic-private");
  });

  it("redacts server startup errors and closes the reserved popup", async () => {
    server.use(http.post(`${base}/oauth/login`, () => HttpResponse.json({ detail: "access_token=synthetic-private" }, { status: 500 })));
    const user = userEvent.setup(); render(<McpAuthSettings server={fixture()} onRefresh={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByRole("alert"); expect(browser.cancel).toHaveBeenCalledOnce();
    expect(document.body.textContent).not.toContain("synthetic-private"); expect(browser.open).not.toHaveBeenCalled();
  });

  it.each([{ transport: "stdio" }, { auth: { mode: "none" } }, { bearer_token_env: "SYNTHETIC_TOKEN" },
    { header_keys: ["Authorization"] }, { env_http_headers: { authorization: "SYNTHETIC_TOKEN" } }])("hides OAuth controls for ineligible configuration %j", extra => {
    render(<McpAuthSettings server={fixture(extra)} onRefresh={vi.fn()} />);
    expect(screen.queryByRole("region", { name: "MCP sign-in" })).not.toBeInTheDocument();
  });

  it("keeps a disabled server inert", () => {
    render(<McpAuthSettings server={fixture({ enabled: false })} onRefresh={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
  });

  it("lets the owner supply a missing registered client ID while retaining the other login settings", async () => {
    const onRefresh = vi.fn(); let saves = 0;
    const authSettings = { mode: "auto", client_id: null, scopes: ["synthetic.read"], callback_port: 1245 };
    server.use(http.patch(base, async ({ request }) => {
      expect(await request.json()).toEqual({ expected_revision: 7, auth: { ...authSettings, client_id: "registered-desktop" } });
      saves++; return HttpResponse.json({ ...fixture(), auth: { ...authSettings, client_id: "registered-desktop" } });
    }));
    const user = userEvent.setup(); render(<McpAuthSettings server={fixture({ auth: authSettings })} onRefresh={onRefresh} />);
    await user.click(screen.getByText("Sign-in settings"));
    const input = screen.getByRole("textbox", { name: "Client ID" });
    expect(input).toHaveValue(""); expect(input).toHaveAttribute("autocomplete", "off");
    expect(screen.getByRole("button", { name: "Save sign-in settings" })).toBeDisabled();
    await user.type(input, " registered-desktop ");
    await user.click(screen.getByRole("button", { name: "Save sign-in settings" }));
    await waitFor(() => expect(onRefresh).toHaveBeenCalledOnce());
    expect(saves).toBe(1); expect(reserveMcpAuthBrowser).not.toHaveBeenCalled();
  });

  it("keeps a failed client ID save retryable without showing upstream credential details", async () => {
    const onRefresh = vi.fn();
    server.use(http.patch(base, () => HttpResponse.json({ detail: "client_secret=synthetic-private code=synthetic-private" }, { status: 409 })));
    const user = userEvent.setup(); render(<McpAuthSettings server={fixture()} onRefresh={onRefresh} />);
    await user.click(screen.getByText("Sign-in settings"));
    await user.type(screen.getByRole("textbox", { name: "Client ID" }), "registered-desktop");
    await user.click(screen.getByRole("button", { name: "Save sign-in settings" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "Save sign-in settings" })).toBeEnabled();
    expect(document.body.textContent).not.toContain("synthetic-private"); expect(onRefresh).not.toHaveBeenCalled();
  });
});
