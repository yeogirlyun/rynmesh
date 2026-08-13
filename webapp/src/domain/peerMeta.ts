import type { JobCapacity, PeerHealth } from "./types";

export interface PeerMeta {
  online: boolean;
  capabilities: string[];
}

/** Join discovered peer ids with their live health + advertised capabilities. Pure. */
export function joinPeerMeta(
  peerIds: string[],
  health: PeerHealth[],
  capacities: JobCapacity[],
): Map<string, PeerMeta> {
  const onlineById = new Map(health.map((h) => [h.peerId, h.online]));
  const capsById = new Map<string, string[]>();
  for (const c of capacities) {
    const existing = capsById.get(c.peer_id) ?? [];
    for (const cap of c.capabilities ?? []) {
      if (!existing.includes(cap)) existing.push(cap);
    }
    capsById.set(c.peer_id, existing);
  }
  const out = new Map<string, PeerMeta>();
  for (const id of peerIds) {
    out.set(id, { online: onlineById.get(id) ?? false, capabilities: capsById.get(id) ?? [] });
  }
  return out;
}
