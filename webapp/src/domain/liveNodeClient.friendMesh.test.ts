import { afterEach, describe, expect, it, vi } from "vitest";
import { makeLiveNodeClient } from "./liveNodeClient";

afterEach(() => vi.restoreAllMocks());

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("live Friend Mesh client", () => {
  it("uses only the local control surface for friend operations", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async () => jsonResponse([]));
    const client = makeLiveNodeClient("http://127.0.0.1:8791/api/local");

    await client.listFriends();
    await client.listFriendInvites();
    await client.createFriendInvite({
      endpoints: ["https://friend.example:8791"],
      permissions: ["private-ai.use"],
      ttl_seconds: 900,
      allow_private_endpoints: false,
    });
    await client.reviewFriendInvite({ link: "rynmesh://join/opaque", allow_private_endpoints: true });
    await client.cancelFriendInvite("invite / one");
    await client.revokeFriend("peer:friend");

    expect(fetchSpy.mock.calls.map(([url]) => url)).toEqual([
      "http://127.0.0.1:8791/api/local/friends",
      "http://127.0.0.1:8791/api/local/friends/invites",
      "http://127.0.0.1:8791/api/local/friends/invites",
      "http://127.0.0.1:8791/api/local/friends/invites/review",
      "http://127.0.0.1:8791/api/local/friends/invites/invite%20%2F%20one",
      "http://127.0.0.1:8791/api/local/friends/revoke",
    ]);
    expect(JSON.parse(String(fetchSpy.mock.calls[2][1]?.body))).toEqual({
      endpoints: ["https://friend.example:8791"],
      permissions: ["private-ai.use"],
      ttl_seconds: 900,
      allow_private_endpoints: false,
    });
    expect(JSON.parse(String(fetchSpy.mock.calls[3][1]?.body))).toEqual({
      link: "rynmesh://join/opaque",
      allow_private_endpoints: true,
    });
    expect(JSON.parse(String(fetchSpy.mock.calls[5][1]?.body))).toEqual({
      peer_id: "peer:friend",
      reason_code: "owner_revoked",
    });
  });
});
