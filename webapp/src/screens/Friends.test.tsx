import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RELOAD_FAILURE_NOTICE } from "../domain/actThenReload";
import { friendsApi, invitationText, extractInvite } from "../domain/friendsClient";
import type { FriendInvitePreview, FriendRecord } from "../domain/friendTypes";
import Friends from "./Friends";
import { MemoryRouter } from "react-router-dom";
import FriendConversation from "./components/FriendConversation";

const mocks = vi.hoisted(() => ({ confirm: vi.fn() }));
vi.mock("../appContext", () => ({ useAppContext: () => ({ confirm: mocks.confirm }) }));
vi.mock("qrcode", () => ({ default: { toDataURL: vi.fn().mockResolvedValue("data:image/png;base64,AA==") } }));
vi.mock("./components/FriendAI", () => ({ default: () => null }));
const friend: FriendRecord = { peer_id: "alice", relationship_id: "r1", node_name: "Alice", endpoint: "http://192.168.1.2:8791", permissions: ["friend.message"], status: "active", created_at: "2026-09-10T00:00:00Z" };
const preview: FriendInvitePreview = { ...friend, invite_id: "i1", expires_at: "2026-09-10T00:15:00Z" };

beforeEach(() => {
  mocks.confirm.mockReset();
  vi.spyOn(friendsApi, "invitationContext").mockResolvedValue({ endpoint: friend.endpoint, address_category: "LAN private" });
  vi.spyOn(friendsApi, "list").mockResolvedValue({ friends: [] });
  vi.spyOn(friendsApi, "invites").mockResolvedValue({ invites: [] });
  vi.spyOn(friendsApi, "cards").mockResolvedValue({ cards: [] });
  vi.spyOn(friendsApi, "history").mockResolvedValue({ messages: [] });
});
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it("clears stale friend and card load warnings when background polling recovers", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  vi.mocked(friendsApi.list).mockRejectedValueOnce(new Error("offline"));
  vi.mocked(friendsApi.cards).mockRejectedValueOnce(new Error("offline"));
  render(<MemoryRouter><Friends /></MemoryRouter>);
  expect(await screen.findByText(/Could not load friends/)).toBeInTheDocument();
  expect(await screen.findByText(/Could not refresh shared content/)).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(screen.queryByText(/Could not load friends/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Could not refresh shared content/)).not.toBeInTheDocument();
  expect(screen.getByText(/No friends yet/)).toBeInTheDocument();
});

it("clears a recovered message load failure without hiding an unconfirmed send", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  vi.mocked(friendsApi.history).mockRejectedValueOnce(new Error("offline"));
  vi.spyOn(friendsApi, "send").mockRejectedValue(new Error("Send unconfirmed"));
  const user = userEvent.setup();
  render(<FriendConversation friend={friend} />);
  expect(await screen.findByText(/Could not refresh messages/)).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(screen.queryByText(/Could not refresh messages/)).not.toBeInTheDocument();
  await user.type(screen.getByLabelText("Message"), "Pending text");
  await user.click(screen.getByRole("button", { name: "Send message" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Send unconfirmed");
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(screen.getByRole("alert")).toHaveTextContent("Send unconfirmed");
  expect(screen.getByRole("button", { name: "Retry this send" })).toBeEnabled();
});

describe("Friend pairing recovery", () => {
  it("shows the reviewed sharing address before creating an invite without contacting a friend", async () => {
    const create = vi.spyOn(friendsApi, "createInvite").mockResolvedValue({ invite_uri: "rynmesh://join/test", invite: preview });
    const inspect = vi.spyOn(friendsApi, "inspect");
    const join = vi.spyOn(friendsApi, "join");
    render(<MemoryRouter><Friends /></MemoryRouter>);
    expect(screen.getByRole("button", { name: "Create invite" })).toBeDisabled();
    expect(await screen.findByText(/Sharing address:.*192.168.1.2/)).toHaveTextContent("same local network");
    expect(create).not.toHaveBeenCalled();
    expect(inspect).not.toHaveBeenCalled();
    expect(join).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Create invite" }));
    expect(create).toHaveBeenCalledWith(friend.endpoint);
  });

  it("keeps creation disabled after address load failure and recovers after retry", async () => {
    vi.mocked(friendsApi.invitationContext).mockRejectedValueOnce(new Error("offline"));
    render(<MemoryRouter><Friends /></MemoryRouter>);
    expect(await screen.findByText(/Could not load your sharing address/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create invite" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Refresh sharing address" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Create invite" })).toBeEnabled());
  });

  it("explains loopback limits before accepting an invite", async () => {
    vi.spyOn(friendsApi, "inspect").mockResolvedValue({ ...preview, address_category: "loopback" });
    const join = vi.spyOn(friendsApi, "join");
    render(<MemoryRouter><Friends /></MemoryRouter>);
    fireEvent.change(screen.getByLabelText("Friend invite"), { target: { value: "rynmesh://join/test" } });
    await userEvent.click(screen.getByRole("button", { name: "Review invite" }));
    expect(await screen.findByText(/This address works only on this computer/)).toHaveTextContent("Reachability has not been tested");
    expect(join).not.toHaveBeenCalled();
  });

  it("never accepts a stale preview after the pasted invite changes", async () => {
    let resolve!: (value: FriendInvitePreview) => void;
    vi.spyOn(friendsApi, "inspect").mockImplementationOnce(() => new Promise((done) => { resolve = done; }))
      .mockResolvedValue({ ...preview, node_name: "Bob" });
    const join = vi.spyOn(friendsApi, "join").mockResolvedValue(friend);
    const user = userEvent.setup();
    render(<MemoryRouter><Friends /></MemoryRouter>);
    await user.type(screen.getByLabelText("Friend invite"), "rynmesh://join/alice");
    await user.click(screen.getByRole("button", { name: "Review invite" }));
    fireEvent.change(screen.getByLabelText("Friend invite"), { target: { value: "rynmesh://join/bob" } });
    await act(async () => resolve(preview));
    expect(screen.queryByRole("button", { name: "Add this friend" })).not.toBeInTheDocument();
    expect(join).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Review invite" }));
    expect(await screen.findByText("Bob")).toBeInTheDocument();
    expect(join).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Add this friend" }));
    await waitFor(() => expect(join).toHaveBeenCalledWith("rynmesh://join/bob"));
  });

  it("keeps installation instructions copyable when clipboard permission fails", async () => {
    const value = { invite_uri: "rynmesh://join/invite", invite: preview };
    vi.spyOn(friendsApi, "createInvite").mockResolvedValue(value);
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("Clipboard unavailable. Select and copy the invitation text."));
    render(<MemoryRouter><Friends /></MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "Create invite" }));
    await user.click(await screen.findByRole("button", { name: "Copy invitation" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Clipboard unavailable");
    expect(screen.getByLabelText("Invitation and installation instructions")).toHaveValue(invitationText(value));
    expect(extractInvite(invitationText(value))).toBe(value.invite_uri);
  });

  it("cancels a pending invite and returns to the create-invite view", async () => {
    const value = { invite_uri: "rynmesh://join/invite", invite: preview };
    vi.spyOn(friendsApi, "createInvite").mockResolvedValue(value);
    const cancel = vi.spyOn(friendsApi, "cancel").mockResolvedValue({});
    const user = userEvent.setup();
    render(<MemoryRouter><Friends /></MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "Create invite" }));
    await screen.findByRole("button", { name: "Cancel invite" });
    expect(cancel).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Cancel invite" }));
    expect(cancel).toHaveBeenCalledWith(preview.invite_id);
    expect(await screen.findByText("Invitation cancelled.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create invite" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel invite" })).not.toBeInTheDocument();
  });
});

describe("Friend delivery recovery", () => {
  it("explains a full mailbox and clears the warning only after confirmed recovery", async () => {
    const failed = { msg_id: "id", dir: "out" as const, from: "me", to: "alice", text: "hello",
      delivery_state: "failed" as const, error: "recipient_full" };
    vi.mocked(friendsApi.history).mockResolvedValue({ messages: [failed] });
    const retry = vi.spyOn(friendsApi, "retry").mockImplementation(async () => {
      vi.mocked(friendsApi.history).mockResolvedValue({ messages: [{ ...failed, delivery_state: "delivered", error: "" }] });
      return {};
    });
    const user = userEvent.setup();
    render(<FriendConversation friend={friend} />);
    expect(await screen.findByText(/Your friend's mailbox is full/)).toBeInTheDocument();
    expect(screen.queryByText("Delivered · confirmed by your friend")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry next pending message" }));
    expect(retry).toHaveBeenCalledWith("alice");
    expect(await screen.findByText("Delivered · confirmed by your friend")).toBeInTheDocument();
    expect(screen.queryByText(/Your friend's mailbox is full/)).not.toBeInTheDocument();
  });

  it("does not claim non-delivery or resend automatically after unconfirmed expiry", async () => {
    vi.mocked(friendsApi.history).mockResolvedValue({ messages: [{ msg_id: "id", dir: "out", from: "me", to: "alice",
      text: "hello", delivery_state: "expired", error: "message_expired" }] });
    const send = vi.spyOn(friendsApi, "send");
    render(<FriendConversation friend={friend} />);
    expect(await screen.findByText(/Your friend may already have received it/)).toBeInTheDocument();
    expect(screen.queryByText("Expired · not delivered")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry next pending message" })).not.toBeInTheDocument();
    expect(send).not.toHaveBeenCalled();
  });

  it("retries an unconfirmed send with the same identity and content", async () => {
    const send = vi.spyOn(friendsApi, "send").mockRejectedValueOnce(new Error("Response lost"))
      .mockResolvedValue({ msg_id: "id", dir: "out", from: "me", to: "alice", delivery_state: "mailbox" });
    const user = userEvent.setup();
    render(<FriendConversation friend={friend} />);
    await screen.findByText("Send your first message.");
    await user.type(screen.getByLabelText("Message"), "第一次分享");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Response lost");
    expect(screen.getByLabelText("Message")).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Retry this send" }));
    await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
    expect(send.mock.calls[0]).toEqual(send.mock.calls[1]);
    expect(send.mock.calls[0][1].message_id).toMatch(/^[a-f0-9]{32}$/);
  });

  it("shows mailbox delivery as unconfirmed and blocks oversized files before sending", async () => {
    vi.mocked(friendsApi.history).mockResolvedValue({ messages: [{ msg_id: "id", dir: "out", from: "me", to: "alice", text: "hello", delivery_state: "mailbox" }] });
    const send = vi.spyOn(friendsApi, "send");
    render(<FriendConversation friend={friend} />);
    expect(await screen.findByText("In encrypted mailbox · waiting for confirmation")).toBeInTheDocument();
    const file = new File([new Uint8Array(5 * 1024 * 1024 + 1)], "large.bin");
    fireEvent.change(screen.getByLabelText("Attachment"), { target: { files: [file] } });
    expect(screen.getByRole("alert")).toHaveTextContent("exceeds 5 MiB");
    expect(send).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  });
});

describe("Friend removal", () => {
  it("removes a friend after confirmation and reflects only what the backend returns", async () => {
    vi.mocked(friendsApi.list).mockResolvedValue({ friends: [friend] });
    const revoke = vi.spyOn(friendsApi, "revoke").mockImplementation(async () => {
      vi.mocked(friendsApi.list).mockResolvedValue({ friends: [] });
      return {};
    });
    const user = userEvent.setup();
    render(<MemoryRouter><Friends /></MemoryRouter>);
    await user.click(await screen.findByRole("button", { name: "Remove" }));
    expect(revoke).not.toHaveBeenCalled();
    const request = mocks.confirm.mock.calls[0][0];
    expect(request.title).toBe("Remove Alice?");
    expect(request.body).toContain("Copies they already saved cannot be recalled");
    await act(() => request.onConfirm());
    expect(revoke).toHaveBeenCalledWith(friend.relationship_id);
    expect(await screen.findByText("No friends yet. Create or paste an invite above.")).toBeInTheDocument();
    expect(screen.queryByText("Alice")).not.toBeInTheDocument();
  });

  it("reports a confirmed removal whose status reload failed as done, not as an error", async () => {
    vi.mocked(friendsApi.list).mockResolvedValue({ friends: [friend] });
    const revoke = vi.spyOn(friendsApi, "revoke").mockResolvedValue({});
    render(<MemoryRouter><Friends /></MemoryRouter>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Remove" }));
    vi.mocked(friendsApi.list).mockRejectedValueOnce(new Error("list down"));
    await act(() => mocks.confirm.mock.calls[0][0].onConfirm());
    expect(revoke).toHaveBeenCalledWith(friend.relationship_id);
    expect(await screen.findByText(RELOAD_FAILURE_NOTICE)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("reports a confirmed retry whose status reload failed as done, not as an error", async () => {
    const pending: FriendRecord = { ...friend, relationship_id: "r2", node_name: "Bob", status: "revoked", revocation_delivery: "pending" };
    vi.mocked(friendsApi.list).mockResolvedValue({ friends: [pending] });
    const retry = vi.spyOn(friendsApi, "retryRevocation").mockResolvedValue({});
    render(<MemoryRouter><Friends /></MemoryRouter>);
    const user = userEvent.setup();
    await screen.findByText("Bob: removed locally; waiting to notify their device.");
    vi.mocked(friendsApi.list).mockRejectedValueOnce(new Error("list down"));
    await user.click(screen.getByRole("button", { name: "Retry removal notice" }));
    expect(retry).toHaveBeenCalledWith(pending.relationship_id);
    expect(await screen.findByText(RELOAD_FAILURE_NOTICE)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("clears a pending removal notice once a retry confirms delivery", async () => {
    const pending: FriendRecord = { ...friend, relationship_id: "r2", node_name: "Bob", status: "revoked", revocation_delivery: "pending" };
    vi.mocked(friendsApi.list).mockResolvedValue({ friends: [pending] });
    const retry = vi.spyOn(friendsApi, "retryRevocation").mockImplementation(async () => {
      vi.mocked(friendsApi.list).mockResolvedValue({ friends: [{ ...pending, revocation_delivery: "delivered" }] });
      return {};
    });
    render(<MemoryRouter><Friends /></MemoryRouter>);
    const user = userEvent.setup();
    expect(await screen.findByText("Bob: removed locally; waiting to notify their device.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry removal notice" }));
    expect(retry).toHaveBeenCalledWith(pending.relationship_id);
    await waitFor(() => expect(screen.queryByText(/waiting to notify their device/)).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Retry removal notice" })).not.toBeInTheDocument();
  });

  it("keeps showing the pending removal notice when a retry succeeds but delivery is still unconfirmed", async () => {
    const pending: FriendRecord = { ...friend, relationship_id: "r2", node_name: "Bob", status: "revoked", revocation_delivery: "pending" };
    vi.mocked(friendsApi.list).mockResolvedValue({ friends: [pending] });
    const retry = vi.spyOn(friendsApi, "retryRevocation").mockResolvedValue({});
    render(<MemoryRouter><Friends /></MemoryRouter>);
    const user = userEvent.setup();
    await screen.findByText("Bob: removed locally; waiting to notify their device.");
    await user.click(screen.getByRole("button", { name: "Retry removal notice" }));
    expect(retry).toHaveBeenCalledWith(pending.relationship_id);
    await waitFor(() => expect(friendsApi.list).toHaveBeenCalledTimes(2));
    expect(screen.getByText("Bob: removed locally; waiting to notify their device.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry removal notice" })).toBeEnabled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("Friend revocation delivery", () => {
  it("shows undeliverable removals as terminal, without a success claim or retry", async () => {
    const undeliverable: FriendRecord = { ...friend, relationship_id: "r2", node_name: "Bob",
      status: "revoked", revoked_at: "2026-09-15T00:00:00Z", revocation_delivery: "undeliverable" };
    vi.mocked(friendsApi.list).mockResolvedValue({ friends: [undeliverable] });
    render(<MemoryRouter><Friends /></MemoryRouter>);
    expect(await screen.findByText("Bob: Removed on this device. The removal notice could not be sent because "
      + "this friend's credentials were no longer available; they will see an error on their next request."))
      .toBeInTheDocument();
    expect(screen.queryByText(/waiting to notify their device/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry removal notice" })).not.toBeInTheDocument();
  });
});

describe("Friend attachments", () => {
  it("downloads and saves an attachment, keeping its object URL alive until the deferred revoke", async () => {
    vi.mocked(friendsApi.history).mockResolvedValue({ messages: [{ msg_id: "m1", dir: "in", from: "alice", to: "me",
      text: "here", attachment: { filename: "photo.png", mime: "image/png", size: 42 } }] });
    const fetchMock = vi.fn().mockResolvedValue(new Response("data", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const create = vi.fn(() => "blob:friend-attachment");
    vi.stubGlobal("URL", Object.assign(class extends URL {}, { createObjectURL: create, revokeObjectURL: vi.fn() }));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    render(<FriendConversation friend={friend} />);
    const button = await screen.findByRole("button", { name: "Save attachment: photo.png (42 bytes)" });
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      fireEvent.click(button);
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(fetchMock).toHaveBeenCalledWith(friendsApi.attachmentUrl("alice", "m1"), { credentials: "include" });
      expect(create).toHaveBeenCalledTimes(1);
      expect(click).toHaveBeenCalledTimes(1);
      expect(URL.revokeObjectURL).not.toHaveBeenCalled();
      await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
      expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:friend-attachment");
    } finally { vi.useRealTimers(); }
  });

  it("shows an honest error when a saved attachment cannot be downloaded", async () => {
    vi.mocked(friendsApi.history).mockResolvedValue({ messages: [{ msg_id: "m1", dir: "in", from: "alice", to: "me",
      text: "here", attachment: { filename: "photo.png", mime: "image/png", size: 42 } }] });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 404 })));
    const user = userEvent.setup();
    render(<FriendConversation friend={friend} />);
    await user.click(await screen.findByRole("button", { name: "Save attachment: photo.png (42 bytes)" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not download this attachment. Access may have been removed.");
  });
});
