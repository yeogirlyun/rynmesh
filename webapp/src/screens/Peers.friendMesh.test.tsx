import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { consumeFriendInviteDeepLink, queueFriendInviteDeepLink } from "../domain/friendDeepLink";
import type { FriendRecord, NodeClient } from "../domain/nodeClient";
import type { ConfirmRequest } from "../domain/types";
import Peers from "./Peers";

afterEach(() => {
  consumeFriendInviteDeepLink();
  vi.restoreAllMocks();
});

function renderPeers(confirm = vi.fn(), client: NodeClient = makeFixtureNodeClient()) {
  const context: AppOutletContext = {
    client,
    node: {
      node_name: "Test Ryn", peer_id: "peer:test", daemon_running: true,
      registry: "connected", peer_count: 0, local_items: 0, fetched_items: 0,
      pending_recs: 0, version: "test", uptime_seconds: 60,
    },
    registry: { status: "connected", url: "https://registry.test" },
    peers: [], refreshShell: vi.fn(async () => undefined), confirm, notify: vi.fn(),
  };
  return render(
    <MemoryRouter initialEntries={["/peers"]}>
      <Routes>
        <Route element={<Outlet context={context} />}>
          <Route path="/peers" element={<Peers />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

async function createInvite(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole("heading", { name: "Scoped access for people you know" });
  await user.type(screen.getByLabelText(/Advertised endpoints/), "https://friend.example:8791");
  await user.click(screen.getByRole("checkbox", { name: /I reviewed every endpoint/ }));
  await user.click(screen.getByRole("button", { name: "Create signed invitation" }));
  return screen.findByRole("heading", { name: "Invitation created" });
}

describe("Peers Friend Mesh", () => {
  it("creates and cancels a scoped invite with a local QR and focus handoff", async () => {
    const user = userEvent.setup();
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    renderPeers();

    const heading = await createInvite(user);
    expect(heading).toHaveFocus();
    expect(screen.getByRole("img", { name: /QR code for invitation/ })).toHaveAttribute("src", expect.stringMatching(/^data:image\/svg\+xml/));
    expect(screen.getAllByText("unresolved hostname").length).toBeGreaterThan(0);
    const link = (screen.getByLabelText("One-use invitation link") as HTMLTextAreaElement).value;
    expect(link).toMatch(/^rynmesh:\/\/join\//);
    expect(fetchSpy).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Copy invitation" }));
    expect(await navigator.clipboard.readText()).toBe(link);

    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(await screen.findByText("cancelled")).toBeInTheDocument();
    expect(screen.queryByLabelText("One-use invitation link")).not.toBeInTheDocument();
    fetchSpy.mockRestore();
  });

  it("reviews locally before contact, then joins only through the local node", async () => {
    const user = userEvent.setup();
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    renderPeers();
    await createInvite(user);
    const link = (screen.getByLabelText("One-use invitation link") as HTMLTextAreaElement).value;

    await user.type(screen.getByLabelText("Paste invitation link"), link);
    await user.click(screen.getByRole("button", { name: "Verify and review offline" }));

    const heading = await screen.findByRole("heading", { name: "Verified invitation" });
    expect(heading).toHaveFocus();
    const review = heading.parentElement!;
    expect(within(review).getByText("verified by local node")).toBeInTheDocument();
    expect(within(review).getByRole("button", { name: /Copy peer:7d2b9c4f-self/ })).toBeInTheDocument();
    expect(within(review).getByText("rynmesh-main")).toBeInTheDocument();
    expect(within(review).getByText("private-ai.use")).toBeInTheDocument();
    expect(within(review).getByText("https://friend.example:8791")).toBeInTheDocument();
    expect(within(review).getByText("unresolved hostname")).toBeInTheDocument();
    const join = within(review).getByRole("button", { name: "Join Friend Mesh" });
    expect(join).toBeEnabled();
    expect(within(review).getByText(/No endpoint was contacted during review/)).toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalled();

    await user.click(join);
    const friendsPanel = screen.getByRole("heading", { name: "Friends" }).parentElement!;
    expect(await within(friendsPanel).findByText("My Ryn Node")).toBeInTheDocument();
    expect(within(friendsPanel).getAllByText("active").length).toBeGreaterThan(0);
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  it("invalidates an offline review whenever the pasted link changes", async () => {
    const user = userEvent.setup();
    renderPeers();
    await createInvite(user);
    const field = screen.getByLabelText("Paste invitation link");
    const link = (screen.getByLabelText("One-use invitation link") as HTMLTextAreaElement).value;

    await user.type(field, link);
    await user.click(screen.getByRole("button", { name: "Verify and review offline" }));
    expect(await screen.findByRole("heading", { name: "Verified invitation" })).toBeInTheDocument();

    await user.type(field, "changed");
    expect(screen.queryByRole("heading", { name: "Verified invitation" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Join Friend Mesh" })).not.toBeInTheDocument();
  });

  it("keeps friendship separate from trust roots and uses high-risk immediate local revoke", async () => {
    const user = userEvent.setup();
    let confirmation: ConfirmRequest | undefined;
    const confirm = vi.fn((request: ConfirmRequest) => { confirmation = request; });
    renderPeers(confirm);

    await screen.findByText("Garden Studio");
    await user.click(screen.getByRole("button", { name: "Revoke" }));
    expect(confirm).toHaveBeenCalledOnce();
    expect(confirmation?.risk).toBe("high");
    expect(confirmation?.body).toMatch(/Local authorization.*removed first/);
    expect(confirmation?.confirmLabel).toBe("Revoke access now");

    await act(async () => { await confirmation?.onConfirm(); });
    const friendsPanel = screen.getByRole("heading", { name: "Friends" }).parentElement!;
    expect(within(friendsPanel).getByText("revoked")).toBeInTheDocument();
    expect(within(friendsPanel).getByText(/local denial active; remote delivery is best-effort/)).toBeInTheDocument();
    expect(screen.getByText(/does not modify trust roots/)).toBeInTheDocument();
    expect(within(friendsPanel).queryByRole("button", { name: /Trust/ })).not.toBeInTheDocument();
  });

  it("keeps create disabled until endpoint, permission, and explicit risk review are present", async () => {
    const user = userEvent.setup();
    renderPeers();
    const create = await screen.findByRole("button", { name: "Create signed invitation" });
    expect(create).toBeDisabled();
    await user.type(screen.getByLabelText(/Advertised endpoints/), "http://127.0.0.1:8791");
    await user.click(screen.getByRole("checkbox", { name: /I reviewed every endpoint/ }));
    expect(create).toBeDisabled();
    expect(screen.getByText("blocked local/link-local")).toBeInTheDocument();
  });

  it("retries a pending signed revocation without weakening local denial", async () => {
    const user = userEvent.setup();
    const client = makeFixtureNodeClient();
    const revoked: FriendRecord = {
      peer_id: "peer:offline-friend",
      display_name: "Offline Friend",
      network_id: "rynmesh-main",
      reviewed_endpoints: ["https://offline.example:8791"],
      granted_permissions: ["private-ai.use"],
      received_permissions: [],
      state: "revoked",
      created_at: "2026-09-02T00:00:00Z",
      accepted_at: "2026-09-02T00:00:00Z",
      last_contact_at: null,
      revoked_at: "2026-09-02T01:00:00Z",
      last_delivery_error: "remote_unreachable",
      source_invite_id: "invite-offline",
    };
    client.listFriends = vi.fn(async () => [{ ...revoked }]);
    client.retryFriendRevocation = vi.fn(async () => {
      revoked.last_delivery_error = null;
      return { ok: true, state: "revoked" as const, revocation_id: "revoke-offline", delivery: "delivered" as const };
    });
    renderPeers(vi.fn(), client);

    await user.click(await screen.findByRole("button", { name: "Retry signed notice" }));
    expect(client.retryFriendRevocation).toHaveBeenCalledWith("peer:offline-friend");
    expect((await screen.findAllByText(/local denial active/)).length).toBeGreaterThan(0);
  });

  it("prefills a desktop deep link for offline review without contacting its endpoint", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    expect(queueFriendInviteDeepLink("rynmesh://join/desktop-token")).toBe(true);
    renderPeers();

    expect(await screen.findByLabelText("Paste invitation link")).toHaveValue("rynmesh://join/desktop-token");
    expect(screen.queryByRole("heading", { name: "Verified invitation" })).not.toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
