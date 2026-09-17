import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { friendsApi, type FriendDiagnostics } from "../../domain/friendsClient";
import type { FriendRecord } from "../../domain/friendTypes";
import FriendConnectionDiagnostics from "./FriendConnectionDiagnostics";

const friends: FriendRecord[] = Array.from({ length: 6 }, (_, index) => ({
  relationship_id: `rid-${index}`, peer_id: `peer-${index}`, node_name: `Node ${index}`,
  endpoint: `http://192.168.1.${index + 1}:8791`, permissions: ["friend.message"],
  status: "active", created_at: "2026-09-17T00:00:00Z",
}));
const result = (states = ["reachable", "unreachable"], running = false): FriendDiagnostics => ({
  from_peer_id: "owner", scope: "outbound_direct", running,
  links: states.map((state, index) => ({ ...friends[index], state, checked_at: 1789600000,
    latency_ms: state === "reachable" ? 12 : null, stale: false })),
});

beforeEach(() => {
  vi.spyOn(friendsApi, "diagnose").mockResolvedValue(result());
  vi.spyOn(friendsApi, "diagnostics").mockResolvedValue(result());
  vi.spyOn(friendsApi, "retry").mockResolvedValue({});
});
afterEach(() => vi.restoreAllMocks());

describe("friend connection checks", () => {
  it("requires an explicit bounded selection and never sends a chat as a probe", async () => {
    const user = userEvent.setup();
    render(<FriendConnectionDiagnostics friends={friends} />);
    expect(friendsApi.diagnose).not.toHaveBeenCalled();
    expect(screen.getByRole("checkbox", { name: "Node 5" })).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: "Node 0" }));
    await user.click(screen.getByRole("checkbox", { name: "Node 5" }));
    await user.click(screen.getByRole("button", { name: "Check selected connections" }));
    await waitFor(() => expect(friendsApi.diagnose).toHaveBeenCalledWith(["rid-1", "rid-2", "rid-3", "rid-4", "rid-5"]));
    expect(friendsApi.retry).not.toHaveBeenCalled();
    expect(screen.getByText(/Links between your friends are not checked here/)).toBeInTheDocument();
  });

  it("shows each observed failure with recovery and rechecks only that link", async () => {
    const user = userEvent.setup();
    render(<FriendConnectionDiagnostics friends={friends.slice(0, 2)} />);
    await user.click(screen.getByRole("button", { name: "Check selected connections" }));
    expect(await screen.findByText("This node → Node 0: Verified direct link")).toBeInTheDocument();
    const failed = screen.getByRole("region", { name: "Connection to Node 1" });
    expect(within(failed).getByText(/Check both connections/)).toBeInTheDocument();
    vi.mocked(friendsApi.diagnose).mockResolvedValue(result(["reachable", "reachable"]));
    await user.click(within(failed).getByRole("button", { name: "Recheck Node 1" }));
    await screen.findByText("This node → Node 1: Verified direct link");
    expect(friendsApi.diagnose).toHaveBeenLastCalledWith(["rid-1"]);
    await user.click(screen.getByRole("button", { name: "Retry queued deliveries to Node 1" }));
    await waitFor(() => expect(friendsApi.retry).toHaveBeenCalledWith("peer-1"));
    expect(await screen.findByText(/Open the conversation to check its delivery receipts/)).toBeInTheDocument();
  });

  it("does not mistake unsupported checks for a broken friendship and drops removed friends", async () => {
    vi.mocked(friendsApi.diagnose).mockResolvedValue(result(["unsupported", "identity_mismatch"]));
    const user = userEvent.setup();
    const view = render(<FriendConnectionDiagnostics friends={friends.slice(0, 2)} />);
    await user.click(screen.getByRole("button", { name: "Check selected connections" }));
    await screen.findByText(/does not mean ordinary messaging is broken/);
    expect(screen.getByText(/A different node answered/)).toBeInTheDocument();
    view.rerender(<FriendConnectionDiagnostics friends={friends.slice(0, 1)} />);
    expect(screen.queryByRole("region", { name: "Connection to Node 1" })).not.toBeInTheDocument();
  });

  it("keeps a stalled batch bounded and allows status recovery after an error", async () => {
    vi.mocked(friendsApi.diagnose).mockResolvedValue(result(["timed_out"], true));
    const user = userEvent.setup();
    render(<FriendConnectionDiagnostics friends={friends.slice(0, 1)} />);
    await user.click(screen.getByRole("button", { name: "Check selected connections" }));
    await screen.findByText(/A transport is still finishing/);
    expect(screen.getByRole("button", { name: "Check selected connections" })).toBeDisabled();
    vi.mocked(friendsApi.diagnostics).mockRejectedValueOnce(new Error("Node unavailable"));
    await user.click(screen.getByRole("button", { name: "Refresh check status" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Node unavailable");
    await user.click(screen.getByRole("button", { name: "Refresh check status" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Check selected connections" })).toBeEnabled());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
