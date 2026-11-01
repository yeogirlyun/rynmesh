import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { aiAccess } from "../domain/aiAccess";
import { friendsApi } from "../domain/friendsClient";
import type { FriendContentCard, FriendRecord } from "../domain/friendTypes";
import Friends from "./Friends";

const mocks = vi.hoisted(() => ({ confirm: vi.fn(), client: { getLLMServiceStatus: vi.fn(), publishLLMService: vi.fn() } }));
vi.mock("../appContext", () => ({ useAppContext: () => mocks }));
vi.mock("qrcode", () => ({ default: { toDataURL: vi.fn().mockResolvedValue("data:image/png;base64,AA==") } }));

const record = (id: string, name: string): FriendRecord => ({ peer_id: id, relationship_id: `r-${id}`, node_name: name, endpoint: `http://${id}.example:8791`,
  permissions: ["friend.message"], status: "active", created_at: "2026-10-12T09:00:00Z" });
const alice = record("alice", "Alice");
const bob = record("bob", "Bob");
const card = (id: string, from: string, title: string): FriendContentCard => ({ card_id: id, from, dir: "in", created_at: "", fetch_state: "available",
  card: { library_id: id, title, summary: "", source: "", kind: "document", fetch_available: true, size_bytes: 10 } });

beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  mocks.confirm.mockReset();
  mocks.client.getLLMServiceStatus.mockResolvedValue({ online: true, ready: true, publication_enabled: true, service: { package_id: "model-x", model_alias: "Local model" } });
  vi.spyOn(friendsApi, "list").mockResolvedValue({ friends: [alice, bob] });
  vi.spyOn(friendsApi, "invites").mockResolvedValue({ invites: [] });
  vi.spyOn(friendsApi, "aiJoins").mockResolvedValue({ operations: [] });
  vi.spyOn(friendsApi, "invitationContext").mockResolvedValue({ endpoint: "192.168.1.2:8791", address_category: "LAN private" });
  vi.spyOn(friendsApi, "cards").mockResolvedValue({ cards: [] });
  vi.spyOn(friendsApi, "history").mockResolvedValue({ messages: [] });
  vi.spyOn(friendsApi, "diagnostics").mockResolvedValue({ from_peer_id: "me", scope: "outbound_direct", running: false,
    links: [{ relationship_id: alice.relationship_id, peer_id: alice.peer_id, node_name: "Alice", state: "reachable", checked_at: 1789600000, latency_ms: 12, stale: false }] });
  vi.spyOn(aiAccess, "list").mockResolvedValue({ grants: [{ service_id: "model-x", relationship_id: alice.relationship_id, peer_id: alice.peer_id, allowed: true, effective: true, revision: 1 }] });
  vi.spyOn(aiAccess, "friends").mockResolvedValue({ friends: [] });
});
afterEach(() => vi.restoreAllMocks());

const renderAt = (url = "/friends") => render(<MemoryRouter initialEntries={[url]}><Friends /></MemoryRouter>);

describe("Friends page layout", () => {
  it("renders the redesigned root, summary band, friend list and the four tabs", async () => {
    const { container } = renderAt();
    expect(container.querySelector(".rd-friends")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Friends" })).toBeInTheDocument();
    const list = await screen.findByRole("region", { name: "Your friends" });
    expect(await within(list).findByRole("button", { name: /Alice/ })).toBeInTheDocument();
    expect(within(list).getByRole("button", { name: /Bob/ })).toBeInTheDocument();
    const band = screen.getByRole("region", { name: "Summary" });
    await waitFor(() => expect(within(band).getByText("Verified direct link").parentElement).toHaveTextContent("1"));
    expect(within(band).getByText("Friends").parentElement).toHaveTextContent("2");
    const tabs = screen.getByRole("group", { name: "Friend sections" });
    expect(within(tabs).getAllByRole("button").map((button) => button.textContent)).toEqual(["Messages", "Shared content", "AI", "Connection"]);
    expect(within(tabs).getByRole("button", { name: "Messages" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(tabs).getByRole("button", { name: "Connection" }));
    expect(within(tabs).getByRole("button", { name: "Connection" })).toHaveAttribute("aria-pressed", "true");
    expect(within(tabs).getByRole("button", { name: "Messages" })).toHaveAttribute("aria-pressed", "false");
  });

  it("selects the first friend by default and the one named by ?peer=", async () => {
    renderAt();
    await waitFor(() => expect(friendsApi.history).toHaveBeenCalledWith("alice"));
    expect(await screen.findByRole("region", { name: "Alice" })).toBeInTheDocument();
    renderAt("/friends?peer=bob");
    await waitFor(() => expect(friendsApi.history).toHaveBeenCalledWith("bob"));
    const detail = (await screen.findAllByRole("region", { name: "Bob" }))[0];
    expect(within(detail).getByRole("heading", { name: "Bob" })).toBeInTheDocument();
  });

  it("explains a ?peer= link to a friend that is gone, and shows no detail for it", async () => {
    renderAt("/friends?peer=ghost");
    expect(await screen.findByText("This friend is unavailable or access was removed.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Ghost" })).not.toBeInTheDocument();
    expect(friendsApi.history).not.toHaveBeenCalled();
  });

  it("opens the Shared content tab for ?card= and lists only the selected friend's cards", async () => {
    vi.mocked(friendsApi.cards).mockResolvedValue({ cards: [card("c1", "alice", "Alice's article"), card("c2", "bob", "Bob's article")] });
    renderAt("/friends?card=c1");
    const tabs = await screen.findByRole("group", { name: "Friend sections" });
    expect(within(tabs).getByRole("button", { name: /Shared content/ })).toHaveAttribute("aria-pressed", "true");
    expect(await screen.findByRole("heading", { name: "Alice's article" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Bob's article" })).not.toBeInTheDocument();
    expect(within(tabs).getByRole("button", { name: /Shared content/ })).toHaveTextContent("1");
  });

  it("shows the AI controls for the selected friend only", async () => {
    renderAt();
    expect(await screen.findByRole("button", { name: "Check Alice's AI" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Check Bob's AI" })).not.toBeInTheDocument();
    await userEvent.click(within(await screen.findByRole("region", { name: "Your friends" })).getByRole("button", { name: /Bob/ }));
    expect(await screen.findByRole("button", { name: "Check Bob's AI" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Check Alice's AI" })).not.toBeInTheDocument();
  });

  it("shows the empty state with an Add a friend button when there are no friends", async () => {
    vi.mocked(friendsApi.list).mockResolvedValue({ friends: [] });
    renderAt();
    expect(await screen.findByRole("heading", { name: "No friends yet" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Add a friend" })).toHaveLength(2);
  });
});

describe("Add a friend dialog", () => {
  it("opens from the header with both invitation tabs and keeps the invite fields reachable", async () => {
    renderAt();
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("button", { name: "Add a friend" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "Add a friend" });
    expect(dialog).toHaveClass("rq-dlg");
    const tabs = within(dialog).getByRole("group", { name: "Invitation" });
    expect(within(tabs).getByRole("button", { name: "Invite a friend" })).toHaveAttribute("aria-pressed", "true");
    expect(within(tabs).getByRole("button", { name: "Use an invite" })).toHaveAttribute("aria-pressed", "false");
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Create invite" })).toBeEnabled());
    await user.click(within(tabs).getByRole("button", { name: "Use an invite" }));
    expect(within(tabs).getByRole("button", { name: "Use an invite" })).toHaveAttribute("aria-pressed", "true");
    expect(within(dialog).getByLabelText("Friend invite")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Review invite" })).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("keeps an open invite when the dialog is closed and reopened", async () => {
    const invite = { invite_id: "i1", peer_id: "me", node_name: "Me", endpoint: "192.168.1.2:8791", permissions: [], created_at: "2026-10-12T09:00:00Z", expires_at: new Date(Date.now() + 600000).toISOString() };
    vi.spyOn(friendsApi, "createInvite").mockResolvedValue({ invite_uri: "rynmesh://join/x", invite });
    vi.mocked(friendsApi.invites).mockResolvedValue({ invites: [{ ...invite, status: "active" }] });
    renderAt();
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("button", { name: "Add a friend" })[0]);
    await waitFor(() => expect(screen.getByRole("button", { name: "Create invite" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Create invite" }));
    expect(await screen.findByRole("img", { name: "Friend invitation QR code" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: "Add a friend" })[0]);
    expect(await screen.findByRole("button", { name: "Cancel invite" })).toBeInTheDocument();
  });
});
