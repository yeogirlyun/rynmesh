import type { LLMServiceRecord } from "./nodeClient";
import type { Peer, PeerHealth } from "./types";

// One snapshot of devices and the services they offer, produced by the local node
// (GET /api/local/directory, rynmesh/peer_directory.py). Screens read this and nothing else:
// a service always belongs to a device in `peers`, and presence is the node's own observation.

export type Relationship = "self" | "space_member" | "friend" | "discovered";
export type PresenceState = "online" | "offline" | "unconfirmed";

export interface DirectoryPeer extends Peer {
  relationship: Relationship;
  presence: { state: PresenceState; basis: string };
  // What the node's probe reached; null means "never probed", not "unreachable".
  reachability?: { http: boolean | null; lan: boolean | null };
  freshness?: { source: string; age_s: number | null; stale: boolean };
  services: LLMServiceRecord[];
}

export interface DirectorySnapshot {
  revision: string;
  generated_at: string;
  network_id: string;
  self_id: string;
  peers: DirectoryPeer[];
}

/** online / offline / unconfirmed (null) in the shape the device list uses. */
export function presenceOnline(peer: Pick<DirectoryPeer, "presence">): boolean | null {
  return peer.presence.state === "online" ? true : peer.presence.state === "offline" ? false : null;
}

function presenceFromHealth(health: PeerHealth | undefined): DirectoryPeer["presence"] {
  if (!health || health.online === null || health.online === undefined) {
    return { state: "unconfirmed", basis: health?.presenceSource ?? "none" };
  }
  return { state: health.online ? "online" : "offline", basis: health.presenceSource ?? "unknown" };
}

/**
 * Build the same snapshot from the separate peer, service and health lists. Used only
 * against a node that predates /api/local/directory and by the fixture client, so the
 * screens have a single code path. A provider known only from its service records
 * still becomes a device here; that is the one place this synthesis lives.
 */
export function directoryFromLegacy(input: {
  selfId: string;
  peers: Peer[];
  records: LLMServiceRecord[];
  health: PeerHealth[];
  networkId?: string;
}): DirectorySnapshot {
  const healthById = new Map(input.health.map((row) => [row.peerId, row]));
  const entries = new Map<string, DirectoryPeer>();
  for (const peer of input.peers) {
    const health = healthById.get(peer.id);
    const self = Boolean(peer.isSelf) || peer.id === input.selfId;
    entries.set(peer.id, {
      ...peer,
      isSelf: self,
      relationship: self ? "self" : "discovered",
      presence: self ? { state: "online", basis: "self" } : presenceFromHealth(health),
      reachability: health
        ? { http: health.httpReachable ?? null, lan: health.lanReachable ?? null }
        : { http: null, lan: null },
      services: [],
    });
  }
  for (const record of input.records) {
    let entry = entries.get(record.peer_id);
    if (!entry) {
      const health = healthById.get(record.peer_id);
      entry = {
        id: record.peer_id, slug: "", name: record.node_name || record.peer_id, endpoint: "",
        network: record.network_id ?? input.networkId ?? "", tier: "unverified" as Peer["tier"],
        credits: 0, weight: 0, lastSeen: "", served: 0, fetched: 0,
        isSelf: record.peer_id === input.selfId,
        relationship: record.peer_id === input.selfId ? "self" : "discovered",
        // The provider's own `online` is a claim: unconfirmed until this node observed it.
        presence: record.presence ?? presenceFromHealth(health),
        reachability: { http: null, lan: null },
        services: [],
      };
      entries.set(record.peer_id, entry);
    }
    entry.services.push(record);
  }
  const peers = [...entries.values()].sort((a, b) =>
    Number(Boolean(b.isSelf)) - Number(Boolean(a.isSelf)) || a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
  return {
    revision: "legacy",
    generated_at: new Date().toISOString(),
    network_id: input.networkId ?? "",
    self_id: input.selfId,
    peers,
  };
}

/** Every service in the snapshot, each exactly once, in device order. */
export function directoryServices(snapshot: DirectorySnapshot): LLMServiceRecord[] {
  const seen = new Set<string>();
  const result: LLMServiceRecord[] = [];
  for (const peer of snapshot.peers) {
    for (const record of peer.services) {
      const key = `${record.peer_id}::${record.service.package_id}`;
      if (!seen.has(key)) {
        seen.add(key);
        result.push(record);
      }
    }
  }
  return result;
}
