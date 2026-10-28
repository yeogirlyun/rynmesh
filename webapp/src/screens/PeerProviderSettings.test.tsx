import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { normalizePlugin } from "../domain/agentExtensions";
import { peerProviders } from "../domain/peerProviders";
import { PeerProviderSettings } from "./PeerProviderSettings";

const base = "http://localhost/api/local/peer-providers/fixture";
function plugin(enabled = true) {
  return normalizePlugin({ id: "fixture", instance_id: "installation-1", enabled,
    peer_provider: { url: "https://provider.example/connect", oauth: { clientId: "desktop", scopes: ["peer.connect"] } } });
}
function status(state = "connected", selected: string[] = []) {
  return { plugin_id: "fixture", resource: "https://provider.example/connect", status: state,
    bound: state === "connected", selected_service_ids: selected };
}

describe("plugin peer-provider settings", () => {
  it("keeps an installed but disabled declaration inert", async () => {
    render(<PeerProviderSettings plugin={plugin(false)} />);
    expect(screen.getByText("Enable the plugin first.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("starts login with an explicit action and only shows a safe browser link", async () => {
    let starts = 0;
    server.use(http.get(`${base}/status`, () => HttpResponse.json(status("signed_out"))),
      http.post(`${base}/login/start`, () => { starts++; return HttpResponse.json({ status: "pending", browser_url: "https://provider.example/oauth/authorize?state=synthetic&code_challenge=synthetic" }); }));
    const user = userEvent.setup();
    const view = render(<PeerProviderSettings plugin={plugin()} />);
    await screen.findByText("Signed out"); expect(starts).toBe(0);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    const link = await screen.findByRole("link", { name: "Continue sign-in in browser" });
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAttribute("target", "_blank");
    expect(starts).toBe(1);
    view.rerender(<PeerProviderSettings plugin={plugin(false)} />);
    await waitFor(() => expect(screen.queryByRole("link")).not.toBeInTheDocument());
  });

  it("publishes only explicitly selected local ids, refreshes a redacted directory, and signs out", async () => {
    let selected: string[] = [], published = 0, signedOut = false;
    server.use(http.get(`${base}/status`, () => HttpResponse.json(status(signedOut ? "signed_out" : "connected", selected))),
      http.get(`${base}/models`, () => HttpResponse.json({ models: [{ service_id: "one", name: "Local One", api_key: "synthetic-private" }, { service_id: "two", name: "Local Two" }] })),
      http.post(`${base}/publish`, async ({ request }) => { const body = await request.json(); expect(Object.keys(body as object)).toEqual(["service_ids"]); selected = (body as { service_ids: string[] }).service_ids; published++; return HttpResponse.json({ ok: true }); }),
      http.post(`${base}/refresh`, () => HttpResponse.json({ members: [{ id: "user", devices: [{ peer_id: "peer", token: "synthetic-private" }] }], models: [{ service: { name: "Remote One", package_id: "remote" }, secret: "synthetic-private" }] })),
      http.post(`${base}/logout`, () => { signedOut = true; return HttpResponse.json({ ok: true }); }));
    const user = userEvent.setup(); render(<PeerProviderSettings plugin={plugin()} />);
    await screen.findByText("Signed in · node bound");
    await user.click(screen.getByRole("button", { name: "Choose local models to publish" }));
    const one = await screen.findByRole("checkbox", { name: "Local One" });
    expect(one).not.toBeChecked(); expect(published).toBe(0);
    await user.click(one);
    await user.click(screen.getByRole("button", { name: "Save publication selection" }));
    await screen.findByText("Publication selection saved."); expect(selected).toEqual(["one"]);
    await user.click(screen.getByRole("button", { name: "Refresh directory" }));
    await screen.findByText("1 members · 1 nodes"); expect(screen.getByText("Remote One")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("synthetic-private");
    await user.click(screen.getByRole("button", { name: "Stop publishing all models" }));
    await screen.findByText("Model publication stopped."); expect(selected).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    await screen.findByText("Signed out"); expect(screen.queryByText("Remote One")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("never renders raw upstream errors", async () => {
    server.use(http.get(`${base}/status`, () => HttpResponse.json({ error: "access_token=synthetic-private" }, { status: 403 })));
    render(<PeerProviderSettings plugin={plugin()} />);
    await screen.findByRole("alert"); expect(document.body.textContent).not.toContain("synthetic-private");
  });

  it.each(["javascript:alert(1)", "http://provider.example/auth", "https://user:password@provider.example/auth", "https://provider.example/auth?code=synthetic", "https://provider.example/auth?access_token=synthetic"])("rejects unsafe browser authorization URL %s", async url => {
    server.use(http.post(`${base}/login/start`, () => HttpResponse.json({ browser_url: url })));
    await expect(peerProviders.login("fixture")).rejects.toThrow("peer_provider_invalid_response");
  });

  it("does not carry unknown credential fields from plugin metadata", () => {
    const value = normalizePlugin({ id: "fixture", peer_provider: { url: "https://provider.example/connect", secret: "synthetic-private", oauth: { clientId: "desktop", scopes: ["peer.connect"], token: "synthetic-private" } } });
    expect(JSON.stringify(value)).not.toContain("synthetic-private");
  });
});
