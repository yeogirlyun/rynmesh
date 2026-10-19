import { describe, expect, it } from "vitest";
import { directoryFromLegacy, directoryServices, presenceOnline, type DirectoryPeer } from "./directory";
import type { LLMServiceRecord } from "./nodeClient";
import type { Peer, PeerHealth } from "./types";

const peer = (id: string, more: Partial<Peer> = {}): Peer => ({
  id, slug: id, name: id, endpoint: `http://${id}:8791`, network: "net", tier: "unverified",
  credits: 0, weight: 0, lastSeen: "now", served: 0, fetched: 0, ...more,
});
const record = (peerId: string, packageId = "codex-cli", more: Partial<LLMServiceRecord> = {}): LLMServiceRecord => ({
  peer_id: peerId, node_name: peerId, online: true,
  service: {
    package_id: packageId, model_alias: "Codex", capabilities: [], context_window: 1, max_output_tokens: 1,
    pricing: { currency: "c", input_per_1k: 0, output_per_1k: 0, minimum: 0, maximum_per_task: 0 }, privacy: {},
  },
  ...more,
});
const health = (peerId: string, online: boolean | null, more: Partial<PeerHealth> = {}): PeerHealth =>
  ({ peerId, online, checkedAt: "", ...more });

describe("directoryFromLegacy", () => {
  it("maps probe results to observed presence, never to the provider's claim", () => {
    const snap = directoryFromLegacy({
      selfId: "me", peers: [peer("a"), peer("b"), peer("c")], records: [],
      health: [health("a", true, { httpReachable: true, lanReachable: true }), health("b", false)],
    });
    const by = Object.fromEntries(snap.peers.map((p) => [p.id, p]));
    expect(presenceOnline(by.a)).toBe(true);
    expect(by.a.reachability).toEqual({ http: true, lan: true });
    expect(presenceOnline(by.b)).toBe(false);
    expect(presenceOnline(by.c)).toBeNull();            // never probed: unconfirmed, not offline
    expect(by.c.reachability).toEqual({ http: null, lan: null });
  });

  it("makes a provider known only from its service a device, unconfirmed despite its online claim", () => {
    const snap = directoryFromLegacy({
      selfId: "me", peers: [peer("known")], records: [record("ad-only", "codex-cli", { online: true })], health: [],
    });
    const device = snap.peers.find((p) => p.id === "ad-only") as DirectoryPeer;
    expect(device).toBeDefined();
    expect(device.relationship).toBe("discovered");
    expect(presenceOnline(device)).toBeNull();
    expect(device.services).toHaveLength(1);
  });

  it("attaches each service to its device once and lists self first", () => {
    const snap = directoryFromLegacy({
      selfId: "me",
      peers: [peer("zed"), peer("me", { isSelf: true, name: "Me" }), peer("amy")],
      records: [record("amy"), record("amy", "other"), record("zed")],
      health: [],
    });
    expect(snap.peers.map((p) => p.id)).toEqual(["me", "amy", "zed"]);
    expect(snap.peers[0].relationship).toBe("self");
    expect(snap.peers[1].services.map((s) => s.service.package_id)).toEqual(["codex-cli", "other"]);
  });

  it("uses the node's own presence on a record when the health list has none", () => {
    const snap = directoryFromLegacy({
      selfId: "me", peers: [],
      records: [record("p", "x", { presence: { state: "online", basis: "heartbeat" } })], health: [],
    });
    expect(presenceOnline(snap.peers[0])).toBe(true);
  });
});

describe("directoryServices", () => {
  it("returns every service exactly once", () => {
    const snap = directoryFromLegacy({ selfId: "me", peers: [peer("a")], records: [record("a"), record("a")], health: [] });
    expect(directoryServices(snap)).toHaveLength(1);
  });
});
